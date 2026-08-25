"""Feishu WebSocket client for receiving events without public webhook.

Uses urllib3 + websockets directly to avoid SSL issues with the requests library.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import urllib.parse

import urllib3
import websockets

logger = logging.getLogger(__name__)

# Disable SSL warnings for urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------------------------------------------------------------------------
# Raw event handler (no SDK dependency)
# ---------------------------------------------------------------------------


def _get_ws_endpoint(
    app_id: str, app_secret: str, base_url: str
) -> tuple[str, str]:
    """Get the WebSocket endpoint URL and token from Feishu API.

    Returns (url, token).
    """
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

    ws_url = data["data"]["URL"]
    return ws_url, access_token


async def _ws_listener(
    ws_url: str,
    token: str,  # noqa: ARG001
    process_event_callback,
) -> None:
    """Connect to Feishu WebSocket and listen for events.

    Handles:
    - Heartbeat (ping/pong)
    - Event parsing
    - Reconnection
    """
    async for websocket in websockets.connect(
        ws_url,
        ping_interval=30,
        ping_timeout=10,
        max_size=10 * 1024 * 1024,  # 10MB
        proxy=None,  # Disable system proxy (macOS may have SOCKS proxy)
    ):
        try:
            logger.info("WebSocket connected")
            async for message in websocket:
                try:
                    data = json.loads(message)
                    msg_type = data.get("type", "")

                    if msg_type == "heartbeat":
                        # Respond to heartbeat
                        await websocket.send(
                            json.dumps({"type": "pong"})
                        )
                    elif msg_type == "event":
                        # Process the event
                        event_data = data.get("event", {})
                        event_type = event_data.get("type", "")
                        if event_type == "im.message.receive_v1":
                            # Convert to the format expected by process_event
                            payload = _convert_raw_event(event_data)
                            await process_event_callback(payload)
                        else:
                            logger.debug("Ignoring event type: %s", event_type)
                    else:
                        logger.debug("Unknown WS message type: %s", msg_type)
                except json.JSONDecodeError:
                    logger.warning("Invalid JSON from WS: %s", message[:200])
                except Exception:
                    logger.exception("Error handling WS message")
        except websockets.ConnectionClosed:
            logger.info("WebSocket disconnected, reconnecting...")
            continue
        except Exception:
            logger.exception("WebSocket error, reconnecting...")
            continue


def _convert_raw_event(event_data: dict) -> dict:
    """Convert a raw Feishu WS event to the format expected by process_event.

    Process_event expects:
    {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": "..."}},
            "message": {"chat_id": "...", "message_type": "text", "content": "..."}
        }
    }
    """
    return {
        "header": {
            "event_id": event_data.get("event_id", ""),
            "event_type": event_data.get("type", ""),
        },
        "event": event_data,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def _run_ws_client_async(
    app_id: str,
    app_secret: str,
    base_url: str,
    process_event_callback,
) -> None:
    """Run the WebSocket client (async version)."""
    while True:
        try:
            # Get WebSocket endpoint
            ws_url, token = _get_ws_endpoint(app_id, app_secret, base_url)
            logger.info("Got WebSocket endpoint, connecting...")

            # Listen for events
            await _ws_listener(ws_url, token, process_event_callback)
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