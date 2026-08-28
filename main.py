"""Feishu / Lark A2A bridge bot.

This bot receives webhook events from Feishu and forwards them to a kagent
agent via the A2A protocol. Replies are sent back to Feishu.

Supports both WebSocket (recommended) and HTTP webhook event subscriptions.

Usage:
    uv sync
    cp .env.example .env   # fill in your credentials
    uv run main.py
"""

from __future__ import annotations

import asyncio
import logging
import os

from dotenv import load_dotenv

# Load .env BEFORE importing handlers (which reads env vars at module level)
load_dotenv()

from fastapi import FastAPI, Request, Response
import uvicorn

from handlers import (
    APP_ID,
    APP_SECRET,
    ENCRYPT_KEY,
    VERIFICATION_TOKEN,
    DOMAIN,
    DOMAIN_BASES,
    decrypt_payload,
    process_event,
    verify_signature,
)
from ws_client import run_ws_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="kagent Feishu A2A Bridge", version="0.1.0")


@app.get("/health")
async def health() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "ok"}


@app.post("/webhook/feishu")
async def feishu_webhook(request: Request) -> Response:
    """Receive webhook events from Feishu (fallback if WebSocket is not used)."""
    body_bytes = await request.body()

    # --- Signature verification (if Encrypt Key is configured) ---------------
    if ENCRYPT_KEY:
        if not _verify_signature_from_request(request, body_bytes):
            return Response(status_code=403, content="invalid signature")

    # --- Parse (possibly decrypt) the payload --------------------------------
    try:
        payload = await request.json()
    except Exception:
        return Response(status_code=400, content="invalid json")

    if ENCRYPT_KEY and "encrypt" in payload:
        try:
            payload = decrypt_payload(ENCRYPT_KEY, payload["encrypt"])
        except Exception as e:
            logger.exception("Failed to decrypt payload")
            return Response(status_code=400, content=f"decrypt failed: {e}")

    # --- Challenge handshake --------------------------------------------------
    if payload.get("type") == "url_verification":
        return _handle_challenge(payload)

    # --- Verify event token (for event_callback) -----------------------------
    if payload.get("type") == "event_callback" and not _verify_token(payload):
        return Response(status_code=403, content="invalid token")

    # --- Process the event asynchronously ------------------------------------
    asyncio.create_task(_safe_process(payload))
    return Response(status_code=200, content="ok")


def _verify_signature_from_request(request: Request, body_bytes: bytes) -> bool:
    """Extract signature headers and verify the request."""
    timestamp = request.headers.get("X-Lark-Request-Timestamp", "")
    nonce = request.headers.get("X-Lark-Request-Nonce", "")
    signature = request.headers.get("X-Lark-Signature", "")
    if not verify_signature(timestamp, nonce, ENCRYPT_KEY, body_bytes, signature):
        logger.warning("Invalid signature; rejecting request")
        return False
    return True


def _handle_challenge(payload: dict) -> dict:
    """Handle a Feishu URL verification challenge."""
    challenge = payload.get("challenge", "")
    token = payload.get("token", "")
    if VERIFICATION_TOKEN and token != VERIFICATION_TOKEN:
        logger.warning("Challenge token mismatch")
        return {"challenge": ""}
    logger.info("Responding to Feishu challenge handshake")
    return {"challenge": challenge}


def _verify_token(payload: dict) -> bool:
    """Verify the event callback token."""
    token = payload.get("token", "")
    if VERIFICATION_TOKEN and token != VERIFICATION_TOKEN:
        logger.warning("Event callback token mismatch")
        return False
    return True


async def _safe_process(payload: dict) -> None:
    """Run process_event and swallow exceptions so they don't crash the task."""
    try:
        await process_event(payload)
    except Exception:
        logger.exception("Error processing event")


def main() -> None:
    """Start the bot server.

    Runs both:
    - FastAPI web server (for health check and webhook fallback)
    - Feishu WebSocket client (for real-time event subscription)
    """
    port = int(os.environ.get("PORT", "9000"))

    # Start WebSocket client in a background thread
    base_url = DOMAIN_BASES.get(DOMAIN, DOMAIN_BASES["feishu"])
    ws_thread = run_ws_client(
        app_id=APP_ID,
        app_secret=APP_SECRET,
        base_url=base_url,
        process_event_callback=process_event,
        encrypt_key=ENCRYPT_KEY,
        verification_token=VERIFICATION_TOKEN,
    )
    logger.info("Feishu WebSocket client started (thread: %s)", ws_thread.name)

    # Start FastAPI server (blocking)
    logger.info("Starting FastAPI server on port %d", port)
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()