"""Feishu / Lark event handlers + A2A invocation."""

from __future__ import annotations

import asyncio
import base64
import functools
import hashlib
import json
import logging
import os
import re
import time
import uuid
from typing import Any

from Crypto.Cipher import AES

import httpx

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
# Session management
# ---------------------------------------------------------------------------

# In-memory session store: chat_id -> {"context_id": str, "last_active": float}
_sessions: dict[str, dict[str, Any]] = {}
_SESSION_TTL = 1800  # 30 minutes


def _get_session(chat_id: str) -> dict[str, Any]:
    """Get or create a session for the given chat."""
    session = _sessions.get(chat_id)
    if session:
        session["last_active"] = time.time()
        return session
    session = {"context_id": None, "last_active": time.time()}
    _sessions[chat_id] = session
    return session


def _update_session(chat_id: str, context_id: str | None) -> None:
    """Update the session with a new context_id."""
    if context_id:
        _sessions[chat_id] = {"context_id": context_id, "last_active": time.time()}


def _cleanup_sessions() -> None:
    """Remove expired sessions."""
    now = time.time()
    expired = [cid for cid, s in _sessions.items() if now - s["last_active"] > _SESSION_TTL]
    for cid in expired:
        del _sessions[cid]
    if expired:
        logger.debug("Cleaned up %d expired sessions", len(expired))


# ---------------------------------------------------------------------------
# Feishu token management
# ---------------------------------------------------------------------------

_ACCESS_TOKEN: str = ""
_TOKEN_EXPIRES: float = 0


@functools.lru_cache(maxsize=1)
def _get_http_client() -> httpx.AsyncClient:
    """Get a shared HTTP client with IPv4 preference."""
    transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0")
    return httpx.AsyncClient(transport=transport, timeout=600.0)


async def get_access_token() -> str:
    """Get or refresh the tenant access token."""
    global _ACCESS_TOKEN, _TOKEN_EXPIRES

    if _ACCESS_TOKEN and time.time() < _TOKEN_EXPIRES:
        return _ACCESS_TOKEN

    if not APP_ID or not APP_SECRET:
        raise RuntimeError("FEISHU_APP_ID and FEISHU_APP_SECRET must be set")

    base_url = DOMAIN_BASES.get(DOMAIN, DOMAIN_BASES["feishu"])
    client = _get_http_client()
    resp = await client.post(
        f"{base_url}/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": APP_ID, "app_secret": APP_SECRET},
    )
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"Failed to get access token: {data}")
    _ACCESS_TOKEN = data["tenant_access_token"]
    # Token expires in `expire` seconds, refresh 5 min early
    _TOKEN_EXPIRES = time.time() + data.get("expire", 7200) - 300
    return _ACCESS_TOKEN


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


async def invoke_a2a(input_text: str, context_id: str | None = None) -> tuple[str, str | None]:
    """Call the kagent A2A endpoint (JSON-RPC) and return the response text.

    Returns (response_text, new_context_id).
    If context_id is provided, the conversation continues from that context.
    """
    if not KAGENT_A2A_URL:
        return "KAGENT_A2A_URL 未设置，请先配置环境变量。", None

    # A2A JSON-RPC request (see https://google.github.io/A2A/)
    params: dict[str, Any] = {
        "message": {
            "role": "user",
            "messageId": str(uuid.uuid4()),
            "parts": [{"kind": "text", "text": input_text}],
        },
    }
    if context_id:
        params["contextId"] = context_id

    request_body = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "message/send",
        "params": params,
    }

    try:
        client = _get_http_client()
        response = await client.post(KAGENT_A2A_URL, json=request_body)
        response.raise_for_status()
        result = response.json()
    except httpx.TimeoutException:
        logger.exception("A2A timeout")
        return None, None  # signal: timeout
    except httpx.HTTPStatusError as e:
        logger.exception("A2A HTTP error")
        return None, None  # signal: error
    except Exception as e:
        logger.exception("A2A call failed")
        return None, None  # signal: error

    # Extract text from response
    if "error" in result:
        err = result["error"]
        return f"kagent 返回错误: {err.get('message', err)}", None

    task = result.get("result", {})
    parts: list[str] = []
    for artifact in task.get("artifacts", []):
        for part in artifact.get("parts", []):
            if part.get("kind") == "text" or "text" in part:
                parts.append(part.get("text", ""))

    # Extract the new context_id from the response
    new_context_id = task.get("contextId")

    reply = "".join(parts) if parts else "kagent 没有返回内容。"
    return reply, new_context_id


# ---------------------------------------------------------------------------
# Reply to Feishu
# ---------------------------------------------------------------------------


async def reply_to_feishu(
    chat_id: str, text: str, thread_id: str | None = None, message_id: str | None = None
) -> None:
    """Send a text message back to a Feishu chat.

    If thread_id is provided, reply in that thread/topic.
    If message_id is provided, use the reply API (for thread replies).
    """
    token = await get_access_token()
    base_url = DOMAIN_BASES.get(DOMAIN, DOMAIN_BASES["feishu"])

    # Feishu has a 4KB per-message limit. Split long replies.
    chunks = _split_text(text, max_bytes=3800)

    client = _get_http_client()
    for chunk in chunks:
        body = {
            "msg_type": "text",
            "content": json.dumps({"text": chunk}),
        }

        if message_id and thread_id:
            # Use reply API for thread replies
            url = f"{base_url}/open-apis/im/v1/messages/{message_id}/reply"
            body["reply_in_thread"] = True
            params = {}
        else:
            # Use create API for normal messages
            url = f"{base_url}/open-apis/im/v1/messages"
            body["receive_id"] = chat_id
            params = {"receive_id_type": "chat_id"}

        resp = await client.post(
            url,
            params=params,
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


async def reply_to_feishu_with_card(
    chat_id: str,
    text: str,
    thread_id: str | None = None,
    message_id: str | None = None,
    card_color: str = "blue",
    header_text: str = "🤖 kagent 回复",
) -> None:
    """Send an interactive card message back to a Feishu chat.

    Card supports markdown, code blocks, tables, etc.
    If message_id is provided, use the reply API (for thread replies).
    """
    token = await get_access_token()
    base_url = DOMAIN_BASES.get(DOMAIN, DOMAIN_BASES["feishu"])

    # Convert text to card-compatible markdown
    markdown_text = _text_to_card_markdown(text)

    # Cards have a generous limit (~100KB), but we still split for safety
    chunks = _split_text_for_card(markdown_text, max_bytes=80000)

    client = _get_http_client()
    for chunk in chunks:
        card = _build_card(chunk, card_color, header_text)

        body = {
            "msg_type": "interactive",
            "content": json.dumps(card),
        }

        if message_id and thread_id:
            # Use reply API for thread replies
            url = f"{base_url}/open-apis/im/v1/messages/{message_id}/reply"
            body["reply_in_thread"] = True
            params = {}
        else:
            # Use create API for normal messages
            url = f"{base_url}/open-apis/im/v1/messages"
            body["receive_id"] = chat_id
            params = {"receive_id_type": "chat_id"}

        resp = await client.post(
            url,
            params=params,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json=body,
        )
        data = resp.json()
        if data.get("code") != 0:
            logger.error(
                "Failed to send card to Feishu: code=%s msg=%s",
                data.get("code"),
                data.get("msg"),
            )
        if len(chunks) > 1:
            await asyncio.sleep(0.25)


def _build_card(markdown_text: str, card_color: str, header_text: str) -> dict:
    """Build a Feishu interactive card JSON object."""
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": header_text},
            "template": card_color,
        },
        "elements": [
            {"tag": "markdown", "content": markdown_text},
            {"tag": "hr"},
            {
                "tag": "note",
                "elements": [
                    {"tag": "plain_text", "content": "Powered by kagent"}
                ],
            },
        ],
    }


def _text_to_card_markdown(text: str) -> str:
    """Convert plain text to card-compatible markdown.

    - Preserves code blocks (```)
    - Preserves tables (| ... |)
    - Preserves lists (-, 1.)
    - Preserves bold (**)
    - Escapes special characters that might break the card
    """
    lines = text.split("\n")
    result = []
    in_code_block = False

    for line in lines:
        if line.startswith("```"):
            in_code_block = not in_code_block
            result.append(line)
        elif in_code_block:
            result.append(line)
        else:
            # Escape problematic characters for Feishu card markdown
            line = line.replace("<", "&lt;").replace(">", "&gt;")
            result.append(line)

    return "\n".join(result)


def _split_text_for_card(text: str, max_bytes: int = 80000) -> list[str]:
    """Split text for card messages.

    Cards have a larger limit than text messages. Split on paragraph
    boundaries to keep the content coherent.
    """
    if len(text.encode("utf-8")) <= max_bytes:
        return [text]

    chunks: list[str] = []
    remaining = text

    while remaining:
        if len(remaining.encode("utf-8")) <= max_bytes:
            chunks.append(remaining)
            break

        cut = _find_split_point(remaining, max_bytes)
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()

    return chunks


async def send_error_message(
    chat_id: str,
    error_type: str,
    thread_id: str | None = None,
    message_id: str | None = None,
    detail: str = "",
) -> None:
    """Send a friendly error message to the user."""
    messages = {
        "timeout": "⏰ kagent 响应超时，请稍后重试。",
        "unavailable": f"🔌 暂时无法连接到 kagent，请稍后重试。{detail}",
        "unknown": "😅 处理出错了，请稍后重试。",
    }
    text = messages.get(error_type, messages["unknown"])
    await reply_to_feishu_with_card(
        chat_id,
        text,
        thread_id=thread_id,
        message_id=message_id,
        card_color="red",
        header_text="⚠️ 出错了",
    )


# ---------------------------------------------------------------------------
# Text splitting utilities
# ---------------------------------------------------------------------------


def _split_text(text: str, max_bytes: int = 3800) -> list[str]:
    """Split text into chunks that fit within Feishu's message size limit."""
    if len(text.encode("utf-8")) <= max_bytes:
        return [text]

    chunks: list[str] = []
    remaining = text

    while remaining:
        if len(remaining.encode("utf-8")) <= max_bytes:
            chunks.append(remaining)
            break

        cut = _find_split_point(remaining, max_bytes)
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()

    return chunks


def _find_split_point(text: str, max_bytes: int) -> int:
    """Find a good place to cut `text` so its UTF-8 size is <= max_bytes."""
    target = text.encode("utf-8")[:max_bytes]
    candidate = target.decode("utf-8", errors="ignore")
    cut = len(candidate)

    para = candidate.rfind("\n\n")
    if para > max_bytes // 2:
        return para + 2

    line = candidate.rfind("\n")
    if line > max_bytes // 2:
        return line + 1

    space = candidate.rfind(" ")
    if space > max_bytes // 2:
        return space + 1

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
    thread_id = message.get("thread_id")  # None if not in a thread
    msg_id = message.get("message_id")  # Used for reply API in thread replies

    # Clean up expired sessions occasionally
    _cleanup_sessions()

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
        text = _extract_text_from_post(content)
    else:
        await reply_to_feishu(
            chat_id,
            f"暂不支持 {message_type} 类型的消息，请发送文字消息。",
            thread_id=thread_id,
            message_id=msg_id,
        )
        return

    if not text:
        return

    # In group chats, strip the @bot mention prefix
    if chat_type == "group":
        text = _strip_mentions(text).strip()
        if not text:
            return

    logger.info(
        "Received message: chat_id=%s thread_id=%s sender=%s text=%s",
        chat_id,
        thread_id,
        sender_id,
        text,
    )

    # Get session for context
    session = _get_session(chat_id)
    context_id = session.get("context_id")

    # Call A2A with context
    reply, new_context_id = await invoke_a2a(text, context_id=context_id)

    # Update session with new context
    if new_context_id:
        _update_session(chat_id, new_context_id)

    # Handle errors
    if reply is None:
        await send_error_message(
            chat_id, "timeout", thread_id=thread_id, message_id=msg_id
        )
        return

    # Send reply as card
    # Use a short summary of user's message as card header
    header_text = f"💬 {text[:30]}{'...' if len(text) > 30 else ''}"
    await reply_to_feishu_with_card(
        chat_id,
        reply,
        thread_id=thread_id,
        message_id=msg_id,
        header_text=header_text,
    )


def _strip_mentions(text: str) -> str:
    """Remove @_user_N mentions from message text."""
    return re.sub(r"@_user_\d+\s*", "", text)


def _extract_text_from_post(content: dict) -> str:
    """Extract plain text from a Feishu post (rich text) message.

    Post messages have two possible formats:
    1. With language key (zh_cn, en_us, ja_jp):
       {"zh_cn": {"title": "...", "content": [[...]]}}
    2. Direct format:
       {"title": "", "content": [[{"tag": "text", "text": "..."}]]}
    """
    post = content.get("zh_cn") or content.get("en_us") or content.get("ja_jp")
    if not post:
        post = content

    parts = []

    title = post.get("title", "")
    if title:
        parts.append(title)

    paragraphs = post.get("content", [])
    for paragraph in paragraphs:
        para_text = []
        for element in paragraph:
            tag = element.get("tag", "")
            if tag == "text":
                para_text.append(element.get("text", ""))
            elif tag == "a":
                para_text.append(element.get("text", ""))
            elif tag == "at":
                para_text.append(element.get("user_name", ""))
        if para_text:
            parts.append("".join(para_text))

    return "".join(parts).strip()