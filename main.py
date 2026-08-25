"""Feishu / Lark A2A bridge bot.

This bot receives webhook events from Feishu and forwards them to a kagent
agent via the A2A protocol. Replies are sent back to Feishu.

Usage:
    uv sync
    cp .env.example .env   # fill in your credentials
    uv run main.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response
import uvicorn

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

from handlers import (  # noqa: E402  (import after logging setup)
    ENCRYPT_KEY,
    VERIFICATION_TOKEN,
    decrypt_payload,
    process_event,
    verify_signature,
)

app = FastAPI(title="kagent Feishu A2A Bridge", version="0.1.0")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/webhook/feishu")
async def feishu_webhook(request: Request) -> Response:
    """Receive webhook events from Feishu.

    Two kinds of requests arrive here:
    1. Challenge handshake (type=url_verification) — Feishu verifies the URL
       when you first configure the event subscription. Must respond with the
       challenge value.
    2. Event callback (type=event_callback) — actual events like
       im.message.receive_v1.
    """
    body_bytes = await request.body()

    # --- Signature verification (if Encrypt Key is configured) ---------------
    if ENCRYPT_KEY:
        timestamp = request.headers.get("X-Lark-Request-Timestamp", "")
        nonce = request.headers.get("X-Lark-Request-Nonce", "")
        signature = request.headers.get("X-Lark-Signature", "")
        if not verify_signature(timestamp, nonce, ENCRYPT_KEY, body_bytes, signature):
            logger.warning("Invalid signature; rejecting request")
            return Response(status_code=403, content="invalid signature")

    # --- Parse (possibly decrypt) the payload --------------------------------
    try:
        payload = await request.json()
    except Exception:
        return Response(status_code=400, content="invalid json")

    # If Encrypt Key is set, the payload is encrypted under the "encrypt" field.
    if ENCRYPT_KEY and "encrypt" in payload:
        try:
            payload = decrypt_payload(ENCRYPT_KEY, payload["encrypt"])
        except Exception as e:
            logger.exception("Failed to decrypt payload")
            return Response(status_code=400, content=f"decrypt failed: {e}")

    # --- Challenge handshake --------------------------------------------------
    if payload.get("type") == "url_verification":
        challenge = payload.get("challenge", "")
        token = payload.get("token", "")
        if VERIFICATION_TOKEN and token != VERIFICATION_TOKEN:
            logger.warning("Challenge token mismatch")
            return Response(status_code=403, content="invalid token")
        logger.info("Responding to Feishu challenge handshake")
        return {"challenge": challenge}

    # --- Verify event token (for event_callback) -----------------------------
    if payload.get("type") == "event_callback":
        token = payload.get("token", "")
        if VERIFICATION_TOKEN and token != VERIFICATION_TOKEN:
            logger.warning("Event callback token mismatch")
            return Response(status_code=403, content="invalid token")

    # --- Process the event asynchronously ------------------------------------
    # Feishu requires a 200 response within ~3 seconds, so we process in the
    # background and return immediately.
    asyncio.create_task(_safe_process(payload))

    return Response(status_code=200, content="ok")


async def _safe_process(payload: dict) -> None:
    """Run process_event and swallow exceptions so they don't crash the task."""
    try:
        await process_event(payload)
    except Exception:
        logger.exception("Error processing event")


def main() -> None:
    port = int(os.environ.get("PORT", "9000"))
    logger.info("Starting Feishu A2A bridge on port %d", port)
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
