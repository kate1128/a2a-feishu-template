"""Feishu WebSocket client for receiving events without public webhook."""

from __future__ import annotations

import asyncio
import logging
import threading

import lark_oapi as lark
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
from lark_oapi.ws.client import Client

logger = logging.getLogger(__name__)


def _convert_event_to_dict(event: P2ImMessageReceiveV1) -> dict:
    """Convert a WSClient event object to the dict format expected by process_event."""
    result = {
        "schema": event.schema,
        "header": {
            "event_id": event.header.event_id,
            "event_type": event.header.event_type,
            "token": event.header.token,
            "create_time": event.header.create_time,
            "tenant_key": event.header.tenant_key,
            "app_id": event.header.app_id,
        },
    }

    if event.event:
        event_data = {"sender": {}, "message": {}}

        if event.event.sender:
            sender = event.event.sender
            event_data["sender"] = {
                "sender_id": {
                    "open_id": sender.sender_id.open_id if sender.sender_id else "",
                    "user_id": sender.sender_id.user_id if sender.sender_id else "",
                    "union_id": sender.sender_id.union_id if sender.sender_id else "",
                },
                "sender_type": sender.sender_type,
                "tenant_key": sender.tenant_key,
            }

        if event.event.message:
            msg = event.event.message
            event_data["message"] = {
                "message_id": msg.message_id,
                "root_id": msg.root_id,
                "parent_id": msg.parent_id,
                "create_time": str(msg.create_time) if msg.create_time else None,
                "update_time": str(msg.update_time) if msg.update_time else None,
                "chat_id": msg.chat_id,
                "thread_id": msg.thread_id,
                "chat_type": msg.chat_type,
                "message_type": msg.message_type,
                "content": msg.content or "{}",
            }

        result["event"] = event_data

    return result


def create_ws_client(
    app_id: str,
    app_secret: str,
    verification_token: str,
    encrypt_key: str,
    domain: str,
    process_event_callback,
) -> Client:
    """Create a Feishu WebSocket client.

    Args:
        app_id: Feishu app ID
        app_secret: Feishu app secret
        verification_token: Event subscription verification token
        encrypt_key: Event subscription encrypt key (empty if not used)
        domain: API domain (https://open.feishu.cn or https://open.larksuite.com)
        process_event_callback: Async function to call when an event is received

    Returns:
        A configured Client instance (not yet started).
    """

    async def handle_message(event: P2ImMessageReceiveV1) -> None:
        """Handle incoming message events from WebSocket."""
        try:
            event_dict = _convert_event_to_dict(event)
            await process_event_callback(event_dict)
        except Exception:
            logger.exception("Error handling WS message event")

    handler = (
        EventDispatcherHandler.builder(
            encrypt_key=encrypt_key,
            verification_token=verification_token,
        )
        .register_p2_im_message_receive_v1(handle_message)
        .build()
    )

    client = Client(
        app_id=app_id,
        app_secret=app_secret,
        event_handler=handler,
        domain=domain,
        auto_reconnect=True,
        log_level=lark.LogLevel.INFO,
    )

    return client


def start_ws_client(client: Client) -> None:
    """Start the WebSocket client in the current thread (blocking).

    The WSClient uses its own event loop internally, so this should
    be run in a separate thread when using with uvicorn/asyncio.
    """
    logger.info("Starting Feishu WebSocket client...")
    try:
        client.start()
    except Exception:
        logger.exception("WebSocket client error")
    finally:
        logger.info("WebSocket client stopped")


def run_ws_client_in_thread(client: Client) -> threading.Thread:
    """Run the WebSocket client in a separate daemon thread."""
    thread = threading.Thread(
        target=start_ws_client,
        args=(client,),
        daemon=True,
        name="feishu-ws-client",
    )
    thread.start()
    return thread