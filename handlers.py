"""Feishu / Lark event handlers + A2A invocation."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import uuid
from typing import Any

import httpx
import lark_oapi as lark

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

APP_ID = os.environ.get("FEISHU_APP_ID", "")
APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
VERIFICATION_TOKEN = os.environ.get("FEISHU_VERIFICATION_TOKEN", "")
ENCRYPT_KEY = os.environ.get("FEISHU_ENCRYPT_KEY", "")
DOMAIN = os.environ.get("FEISHU_DOMAIN", "feishu")
KAGENT_A2A_URL = os.environ.get("KAGENT_A2A_URL", "")

# Feishu API base URLs
DOMAIN_BASES = {
    "feishu": "https://open.feishu.cn",
    "lark": "https://open.larksuite.com",
}

# ---------------------------------------------------------------------------
# Feishu token management
# ---------------------------------------------------------------------------

_access_token: str = ""
_token_expires: float = 0


async def get_access_token() -> str:
    """Get or refresh the tenant access token."""
    global _access_token, _token_expires
    import time

    if _access_token and time.time() < _token_expires:
        return _access_token

    if not APP_ID or not APP_SECRET:
        raise RuntimeError("FEISHU_APP_ID and FEISHU_APP_SECRET must be set")

    base_url = DOMAIN_BASES.get(DOMAIN, DOMAIN_BASES["feishu"])
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{base_url}/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": APP_ID, "app_secret": APP_SECRET},
        )
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"Failed to get access token: {data}")
        _access_token = data["tenant_access_token"]
        # Token expires in `expire` seconds, refresh 5 min early
        _token_expires = time.time() + data.get("expire", 7200) - 300
        return _access_token


# ---------------------------------------------------------------------------
# Signature verification + decryption
# ---------------------------------------------------------------------------


def verify_signature(
    timestamp: str, nonce: str, encrypt_key: str, body_bytes: bytes, signature: str
) -> bool:
    """Verify the X-Lark-Signature header.

    Algorithm: sha256(timestamp + nonce + encrypt_key + body)
    """
    if not signature:
        return False
    content = f"{timestamp}{nonce}{encrypt_key}{body_bytes.decode('utf-8')}"
    computed = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return computed == signature


def decrypt_payload(encrypt_key: str, encrypted: str) -> dict[str, Any]:
    """Decrypt an AES-256-CBC encrypted event payload.

    Feishu uses AES-256-CBC with the key = sha256(encrypt_key).
    """
    from Crypto.Cipher import AES

    if not encrypt_key:
        raise ValueError("encrypt_key is required to decrypt payload")

    key = hashlib.sha256(encrypt_key.encode("utf-8")).digest()
    encrypted_bytes = base64.b64decode(encrypted)
    iv = encrypted_bytes[:16]
    cipher = AES.new(key, AES.MODE_CBC, iv)
    decrypted = cipher.decrypt(encrypted_bytes[16:])

    # Remove PKCS7 padding
    pad_len = decrypted[-1]
    decrypted = decrypted[:-pad_len]

    return json.loads(decrypted.decode("utf-8"))


# ---------------------------------------------------------------------------
# A2A invocation
# ---------------------------------------------------------------------------


async def invoke_a2a(input_text: str) -> str:
    """Call the kagent A2A endpoint (JSON-RPC) and return the response text."""
    if not KAGENT_A2A_URL:
        return "KAGENT_A2A_URL 未设置，请先配置环境变量。"

    # A2A JSON-RPC request (see https://google.github.io/A2A/)
    request_body = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "message/send",
        "params": {
            "message": {
                "role": "user",
                "messageId": str(uuid.uuid4()),
                "parts": [{"kind": "text", "text": input_text}],
            },
        },
    }

    try:
        async with httpx.AsyncClient(timeout=600.0) as client:
            response = await client.post(KAGENT_A2A_URL, json=request_body)
            response.raise_for_status()
            result = response.json()
    except httpx.HTTPStatusError as e:
        logger.exception("A2A HTTP error")
        return f"调用 kagent 失败 (HTTP {e.response.status_code})"
    except Exception as e:
        logger.exception("A2A call failed")
        return f"调用 kagent 失败: {e}"

    # Extract text from response
    if "error" in result:
        err = result["error"]
        return f"kagent 返回错误: {err.get('message', err)}"

    task = result.get("result", {})
    parts: list[str] = []
    for artifact in task.get("artifacts", []):
        for part in artifact.get("parts", []):
            if part.get("kind") == "text" or "text" in part:
                parts.append(part.get("text", ""))

    return "".join(parts) if parts else "kagent 没有返回内容。"


# ---------------------------------------------------------------------------
# Reply to Feishu
# ---------------------------------------------------------------------------


async def reply_to_feishu(
    chat_id: str, text: str, thread_id: str | None = None
) -> None:
    """Send a text message back to a Feishu chat.

    If thread_id is provided, reply in that thread/topic.
    """
    token = await get_access_token()
    base_url = DOMAIN_BASES.get(DOMAIN, DOMAIN_BASES["feishu"])

    # Feishu has a 4KB per-message limit. Split long replies.
    chunks = _split_text(text, max_bytes=3800)

    async with httpx.AsyncClient() as client:
        for chunk in chunks:
            body = {
                "receive_id": chat_id,
                "msg_type": "text",
                "content": json.dumps({"text": chunk}),
            }
            # If replying in a thread, add these fields
            if thread_id:
                body["reply_in_thread"] = True
                body["thread_id"] = thread_id

            resp = await client.post(
                f"{base_url}/open-apis/im/v1/messages",
                params={"receive_id_type": "chat_id"},
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
            data = resp.json()
            if data.get("code") != 0:
                logger.error(
                    "Failed to send message to Feishu: code=%s msg=%s",
                    data.get("code"),
                    data.get("msg"),
                )
            # Simple rate-limit guard: Feishu allows 5 msg/s/chat
            if len(chunks) > 1:
                await asyncio.sleep(0.25)


def _split_text(text: str, max_bytes: int = 3800) -> list[str]:
    """Split text into chunks that fit within Feishu's message size limit.

    Tries to split on paragraph boundaries (double newline) first,
    then on single newlines, then on spaces, finally hard-breaks.
    """
    if len(text.encode("utf-8")) <= max_bytes:
        return [text]

    chunks: list[str] = []
    remaining = text

    while remaining:
        if len(remaining.encode("utf-8")) <= max_bytes:
            chunks.append(remaining)
            break

        # Try to find a split point
        cut = _find_split_point(remaining, max_bytes)
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()

    return chunks


def _find_split_point(text: str, max_bytes: int) -> int:
    """Find a good place to cut `text` so its UTF-8 size is <= max_bytes."""
    # Walk backwards from max_bytes to find a boundary
    target = text.encode("utf-8")[:max_bytes]
    # Decode, allowing partial last char
    candidate = target.decode("utf-8", errors="ignore")
    cut = len(candidate)

    # Try paragraph break
    para = candidate.rfind("\n\n")
    if para > max_bytes // 2:
        return para + 2

    # Try line break
    line = candidate.rfind("\n")
    if line > max_bytes // 2:
        return line + 1

    # Try space
    space = candidate.rfind(" ")
    if space > max_bytes // 2:
        return space + 1

    # Hard break
    return cut


# ---------------------------------------------------------------------------
# Event processing
# ---------------------------------------------------------------------------


async def process_event(event: dict[str, Any]) -> None:
    """Process a single Feishu event (already decrypted and parsed)."""
    header = event.get("header", {})
    event_type = header.get("event_type", "")

    if event_type == "im.message.receive_v1":
        await _handle_message(event.get("event", {}))
    else:
        logger.debug("Ignoring event type: %s", event_type)


async def _handle_message(event: dict[str, Any]) -> None:
    """Handle an incoming message event."""
    sender = event.get("sender", {})
    sender_id = sender.get("sender_id", {}).get("open_id", "")
    message = event.get("message", {})
    chat_id = message.get("chat_id", "")
    chat_type = message.get("chat_type", "")  # "p2p" or "group"
    message_type = message.get("message_type", "")
    # Extract thread_id for topic/thread replies
    thread_id = message.get("thread_id")  # None if not in a thread

    # Parse message content based on type
    content_str = message.get("content", "{}")
    try:
        content = json.loads(content_str)
    except json.JSONDecodeError:
        logger.warning("Failed to parse message content: %s", content_str)
        return

    # Extract text based on message type
    text = ""
    if message_type == "text":
        text = content.get("text", "").strip()
    elif message_type == "post":
        # Post (rich text) messages have a different structure
        text = _extract_text_from_post(content)
    else:
        await reply_to_feishu(
            chat_id,
            f"暂不支持 {message_type} 类型的消息，请发送文字消息。",
            thread_id=thread_id,
        )
        return

    if not text:
        return

    # In group chats, strip the @bot mention prefix
    if chat_type == "group":
        # Feishu puts mentions in the content as @_user_N; strip them
        text = _strip_mentions(text).strip()
        if not text:
            return  # Was just an @mention with no content

    logger.info(
        "Received message: chat_id=%s thread_id=%s sender=%s text=%s",
        chat_id,
        thread_id,
        sender_id,
        text,
    )

    # Optional: acknowledge immediately with a "thinking" message
    # (disabled by default; uncomment if you want)
    # await reply_to_feishu(chat_id, "思考中...", thread_id=thread_id)

    # Call A2A
    reply = await invoke_a2a(text)

    # Send reply
    await reply_to_feishu(chat_id, reply, thread_id=thread_id)


def _strip_mentions(text: str) -> str:
    """Remove @_user_N mentions from message text."""
    import re

    return re.sub(r"@_user_\d+\s*", "", text)


def _extract_text_from_post(content: dict) -> str:
    """Extract plain text from a Feishu post (rich text) message.

    Post messages have structure like:
    {
      "zh_cn": {
        "title": "...",
        "content": [[{"tag": "text", "text": "..."}, ...], ...]
      }
    }
    """
    # Try different language keys
    post = content.get("zh_cn") or content.get("en_us") or content.get("ja_jp")
    if not post:
        return ""

    parts = []

    # Extract title if present
    title = post.get("title", "")
    if title:
        parts.append(title)

    # Extract text from content paragraphs
    paragraphs = post.get("content", [])
    for paragraph in paragraphs:
        para_text = []
        for element in paragraph:
            if element.get("tag") == "text":
                para_text.append(element.get("text", ""))
            elif element.get("tag") == "a":
                # For links, just use the text
                para_text.append(element.get("text", ""))
            # Skip other element types (images, etc.) for now
        if para_text:
            parts.append("".join(para_text))

    return "\n".join(parts).strip()
