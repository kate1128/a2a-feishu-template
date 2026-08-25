"""Feishu WebSocket client for receiving events without public webhook.

Uses the protobuf-based protocol from lark_oapi SDK for frame handling.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading

import urllib3
import websockets

from lark_oapi.ws.enum import FrameType, MessageType
from lark_oapi.ws.pb.pbbp2_pb2 import Frame

logger = logging.getLogger(__name__)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Header key constants (same as lark_oapi.ws.const)
HEADER_TYPE = "type"
HEADER_MESSAGE_ID = "message_id"
HEADER_TRACE_ID = "trace_id"
HEADER_SUM = "sum"
HEADER_BIZ_RT = "biz_rt"


def _get_by_key(headers, key: str) -> str:
    """Get a header value by key from a protobuf repeated field."""
    for header in headers:
        if header.key == key:
            return header.value
    raise KeyError(f"Header not found: {key}")


def _get_ws_endpoint(app_id: str, app_secret: str, base_url: str) -> str:
    """Get the WebSocket endpoint URL from Feishu API."""
    http = urllib3.PoolManager()

    # 1. Get tenant access token
    resp = http.request(
        "POST",
        f"{base_url}/open-apis/auth/v3/tenant_access_token/internal",
        body=json.dumps({"app_id": app_id, "app_secret": app_secret}),
        headers={"Content-Type": "application/json"},
        timeout=10.0,
    )
    data = json.loads(resp.data)
    if data.get("code") != 0:
        raise RuntimeError(f"Failed to get access token: {data}")
    access_token = data["tenant_access_token"]

    # 2. Get WebSocket endpoint
    resp = http.request(
        "POST",
        f"{base_url}/callback/ws/endpoint",
        headers={"Authorization": f"Bearer {access_token}"},
        body=json.dumps({"AppID": app_id, "AppSecret": app_secret}),
        timeout=10.0,
    )
    data = json.loads(resp.data)
    if data.get("code") != 0:
        raise RuntimeError(f"Failed to get WS endpoint: {data}")

    return data["data"]["URL"]


async def _new_ping_frame(service_id: int) -> Frame:
    """Create a new ping frame."""
    frame = Frame()
    frame.SeqID = 0
    frame.LogID = 0
    frame.service = service_id
    frame.method = FrameType.CONTROL.value
    header = frame.headers.add()
    header.key = HEADER_TYPE
    header.value = str(MessageType.PING.value)
    return frame


async def _ws_listener(ws_url: str, process_event_callback) -> None:
    """Connect to Feishu WebSocket and listen for events using protobuf frames."""
    async for websocket in websockets.connect(
        ws_url,
        ping_interval=None,
        ping_timeout=None,
        max_size=10 * 1024 * 1024,
        proxy=None,
    ):
        try:
            logger.info("WebSocket connected")

            async for message in websocket:
                if not isinstance(message, bytes):
                    logger.debug("Skipping non-binary message")
                    continue

                try:
                    frame = Frame()
                    frame.ParseFromString(message)
                except Exception:
                    logger.debug("Failed to parse frame")
                    continue

                ft = FrameType(frame.method)

                if ft == FrameType.CONTROL:
                    await _handle_control_frame(frame, websocket)
                elif ft == FrameType.DATA:
                    await _handle_data_frame(frame, process_event_callback)
                else:
                    logger.debug("Unknown frame type: %s", ft)

        except websockets.ConnectionClosed:
            logger.info("WebSocket disconnected, reconnecting...")
            continue
        except Exception:
            logger.exception("WebSocket error, reconnecting...")
            continue


async def _handle_control_frame(frame: Frame, websocket) -> None:
    """Handle CONTROL frames (ping/pong)."""
    try:
        message_type = MessageType(int(_get_by_key(frame.headers, HEADER_TYPE)))
    except (KeyError, ValueError):
        return

    if message_type == MessageType.PING:
        logger.debug("Received ping, sending pong")
        pong = Frame()
        pong.SeqID = frame.SeqID + 1
        pong.LogID = frame.LogID
        pong.service = frame.service
        pong.method = FrameType.CONTROL.value
        header = pong.headers.add()
        header.key = HEADER_TYPE
        header.value = str(MessageType.PONG.value)
        await websocket.send(pong.SerializeToString())
    elif message_type == MessageType.PONG:
        logger.debug("Received pong")
        if frame.payload:
            try:
                config = json.loads(frame.payload.decode("utf-8"))
                logger.debug("Received config: %s", config)
            except Exception:
                pass


async def _handle_data_frame(frame: Frame, process_event_callback) -> None:
    """Handle DATA frames (events)."""
    if not frame.payload:
        return

    try:
        message_type = MessageType(int(_get_by_key(frame.headers, HEADER_TYPE)))
    except (KeyError, ValueError):
        return

    try:
        payload_str = frame.payload.decode("utf-8")
    except UnicodeDecodeError:
        return

    try:
        data = json.loads(payload_str)
    except json.JSONDecodeError:
        return

    event_type = data.get("type", "")
    logger.info("Data frame received: type=%s", event_type)

    if event_type == "im.message.receive_v1":
        payload = _convert_raw_event(data)
        await process_event_callback(payload)


def _convert_raw_event(event_data: dict) -> dict:
    """Convert a raw Feishu WS event to the format expected by process_event."""
    return {
        "header": {
            "event_id": event_data.get("event_id", ""),
            "event_type": event_data.get("type", ""),
        },
        "event": event_data,
    }


async def _run_ws_client_async(
    app_id: str,
    app_secret: str,
    base_url: str,
    process_event_callback,
) -> None:
    """Run the WebSocket client (async version) with auto-reconnect."""
    while True:
        try:
            ws_url = _get_ws_endpoint(app_id, app_secret, base_url)
            logger.info("Got WebSocket endpoint, connecting...")
            await _ws_listener(ws_url, process_event_callback)
        except Exception:
            logger.exception("WebSocket error, reconnecting in 5s...")
            await asyncio.sleep(5)


def _run_ws_client_sync(
    app_id: str,
    app_secret: str,
    base_url: str,
    process_event_callback,
) -> None:
    """Run the WebSocket client (sync version, for use in a thread)."""
    asyncio.run(
        _run_ws_client_async(app_id, app_secret, base_url, process_event_callback)
    )


def run_ws_client_in_thread(
    app_id: str,
    app_secret: str,
    base_url: str,
    process_event_callback,
) -> threading.Thread:
    """Run the WebSocket client in a separate daemon thread."""
    thread = threading.Thread(
        target=_run_ws_client_sync,
        args=(app_id, app_secret, base_url, process_event_callback),
        daemon=True,
        name="feishu-ws-client",
    )
    thread.start()
    return thread