"""Feishu WebSocket client for receiving events without public webhook.

Uses the official lark_oapi.ws.Client from the SDK, which handles:
- Getting the WebSocket endpoint URL
- Protobuf frame encoding/decoding
- Ping/pong keepalive
- Auto-reconnection
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Callable

import lark_oapi as lark
from lark_oapi.ws import Client as WSClient

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Event adapter
# ---------------------------------------------------------------------------

_callback: Callable | None = None


async def _on_message_received(data: lark.im.v1.P2ImMessageReceiveV1) -> None:
    """Convert SDK event to plain dict and pass to our handler."""
    event = data.event
    if not event:
        return

    payload = {
        "header": {
            "event_id": data.header.event_id if data.header else "",
            "event_type": "im.message.receive_v1",
        },
        "event": _event_to_dict(event),
    }
    if _callback:
        await _callback(payload)


def _event_to_dict(event: Any) -> dict[str, Any]:
    """Convert SDK event object to plain dict."""
    result: dict = {}

    if hasattr(event, "sender") and event.sender:
        s = event.sender
        sid = {}
        if hasattr(s, "sender_id") and s.sender_id:
            for attr in ("open_id", "user_id", "union_id"):
                if hasattr(s.sender_id, attr) and getattr(s.sender_id, attr):
                    sid[attr] = getattr(s.sender_id, attr)
        result["sender"] = {"sender_id": sid, "sender_type": getattr(s, "sender_type", "")}

    if hasattr(event, "message") and event.message:
        msg = event.message
        m = {}
        for attr in ("chat_id", "chat_type", "message_type", "message_id", "content", "thread_id", "parent_id", "root_id"):
            if hasattr(msg, attr) and getattr(msg, attr):
                m[attr] = getattr(msg, attr)
        result["message"] = m

    return result


def _ws_event_adapter(data: lark.im.v1.P2ImMessageReceiveV1) -> None:
    """Schedule the async callback on the running event loop.

    This is called from within the WS client's event loop, so we use
    create_task to schedule the async work without blocking.
    """
    try:
        asyncio.create_task(_on_message_received(data))
    except Exception:
        logger.exception("Error in WS event callback")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_ws_client(
    app_id: str,
    app_secret: str,
    base_url: str,
    process_event_callback: Callable,
    encrypt_key: str = "",
    verification_token: str = "",
) -> threading.Thread:
    """Run the WebSocket client in a background daemon thread.

    Uses the official lark_oapi.ws.Client which handles endpoint discovery,
    protobuf frames, keepalive, and auto-reconnection.

    Args:
        app_id: Feishu app ID.
        app_secret: Feishu app secret.
        base_url: Feishu API base URL.
        process_event_callback: async function to call with (dict) events.
        encrypt_key: Event subscription encrypt key (optional).
        verification_token: Event subscription verification token (optional).
    """
    global _callback
    _callback = process_event_callback

    handler = (
        lark.EventDispatcherHandler.builder(encrypt_key, verification_token, lark.LogLevel.WARNING)
        .register_p2_im_message_receive_v1(_ws_event_adapter)
        .build()
    )

    client = WSClient(
        app_id=app_id,
        app_secret=app_secret,
        event_handler=handler,
        domain=base_url,
        auto_reconnect=True,
        log_level=lark.LogLevel.WARNING,
    )

    def _run():
        logger.info("Starting Feishu WebSocket client...")
        try:
            client.start()
        except Exception:
            logger.exception("WebSocket client exited with error")

    thread = threading.Thread(target=_run, daemon=True, name="feishu-ws-client")
    thread.start()
    return thread