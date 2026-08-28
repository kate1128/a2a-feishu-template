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
_HTTP_CLIENT: httpx.AsyncClient | None = None


def _get_http_client() -> httpx.AsyncClient:
    """Get a shared HTTP client."""
    global _HTTP_CLIENT
    if _HTTP_CLIENT is None:
        _HTTP_CLIENT = httpx.AsyncClient(timeout=600.0)
    return _HTTP_CLIENT


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
# Feishu reactions (thinking / error indicators)
# ---------------------------------------------------------------------------

# Feishu emoji types for processing status
_FEISHU_REACTION_THINKING = "Typing"     # 👀 "typing" indicator
_FEISHU_REACTION_DONE = None             # removed on success
_FEISHU_REACTION_ERROR = "CrossMark"     # ❌ — error indicator


async def _add_reaction(message_id: str, emoji_type: str) -> str | None:
    """Add a reaction emoji to a message. Returns reaction_id on success."""
    token = await get_access_token()
    base_url = DOMAIN_BASES.get(DOMAIN, DOMAIN_BASES["feishu"])
    client = _get_http_client()
    resp = await client.post(
        f"{base_url}/open-apis/im/v1/messages/{message_id}/reactions",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"reaction_type": {"emoji_type": emoji_type}},
    )
    data = resp.json()
    if data.get("code") == 0:
        return data.get("data", {}).get("reaction_id")
    logger.debug("Add reaction %s on %s: code=%s", emoji_type, message_id, data.get("code"))
    return None


async def _remove_reaction(message_id: str, reaction_id: str) -> bool:
    """Remove a reaction by its reaction_id."""
    token = await get_access_token()
    base_url = DOMAIN_BASES.get(DOMAIN, DOMAIN_BASES["feishu"])
    client = _get_http_client()
    resp = await client.delete(
        f"{base_url}/open-apis/im/v1/messages/{message_id}/reactions/{reaction_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    return resp.status_code == 200


# ---------------------------------------------------------------------------
# Streaming card support
# ---------------------------------------------------------------------------

# Track active streaming cards: message_id -> {"card_id": str, "content": str}
_streaming_cards: dict[str, dict[str, Any]] = {}


async def _send_thinking_card(chat_id: str, reply_to_msg_id: str | None = None) -> str | None:
    """Send a 'thinking' card and return the message_id for later updates.

    If reply_to_msg_id is provided, the card is sent as a reply (appears in
    the same thread/topic). Otherwise it's sent as a new message.
    """
    token = await get_access_token()
    base_url = DOMAIN_BASES.get(DOMAIN, DOMAIN_BASES["feishu"])
    client = _get_http_client()
    card = {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": "🤔 思考中..."}, "template": "blue"},
        "elements": [
            {"tag": "markdown", "content": "正在思考，请稍候..."},
            {"tag": "hr"},
            {"tag": "note", "elements": [{"tag": "plain_text", "content": "kagent"}]},
        ],
    }

    if reply_to_msg_id:
        # Reply to the original message (appears in the same thread)
        resp = await client.post(
            f"{base_url}/open-apis/im/v1/messages/{reply_to_msg_id}/reply",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"msg_type": "interactive", "content": json.dumps(card)},
        )
    else:
        # Send as new message
        resp = await client.post(
            f"{base_url}/open-apis/im/v1/messages",
            params={"receive_id_type": "chat_id"},
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"receive_id": chat_id, "msg_type": "interactive", "content": json.dumps(card)},
        )

    data = resp.json()
    if data.get("code") == 0:
        msg_id = data.get("data", {}).get("message_id")
        if msg_id:
            _streaming_cards[msg_id] = {"card_id": None, "content": ""}
            return msg_id
    return None


async def _update_streaming_card(message_id: str, new_content: str) -> None:
    """Update the content of a streaming card.

    Uses the Feishu message update API:
    PATCH /open-apis/im/v1/messages/{message_id}
    """
    tracked = _streaming_cards.get(message_id)
    if not tracked:
        return

    token = await get_access_token()
    base_url = DOMAIN_BASES.get(DOMAIN, DOMAIN_BASES["feishu"])
    client = _get_http_client()

    markdown_text = _text_to_card_markdown(new_content)
    card = {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": "💬 kagent 回复"}, "template": "blue"},
        "elements": [
            {"tag": "markdown", "content": markdown_text},
            {"tag": "hr"},
            {"tag": "note", "elements": [{"tag": "plain_text", "content": "kagent"}]},
        ],
    }

    resp = await client.patch(
        f"{base_url}/open-apis/im/v1/messages/{message_id}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"msg_type": "interactive", "content": json.dumps(card)},
    )
    if resp.status_code == 200:
        tracked["card_id"] = "active"
        tracked["content"] = new_content
    else:
        logger.debug("Card update failed: %s %s", resp.status_code, resp.text)


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
    """Convert text to card-compatible content.

    Feishu card markdown supports: **bold**, *italic*, ~~strikethrough~~,
    `inline code`, ```code blocks```, [links](url), headers, lists.

    Tables (| ... |) are NOT supported by the card markdown tag.
    We convert them to plain text for readability.
    """
    lines = text.split("\n")
    result = []
    in_table = False

    for line in lines:
        stripped = line.strip()
        # Skip separator rows like |---|---|
        if re.match(r'^\|[\s\-:]+\|[\s\-:]+\|', stripped):
            # Replace with a thin separator
            result.append("")
            continue
        # Detect table rows: | col1 | col2 |
        if stripped.startswith("|") and stripped.endswith("|") and stripped.count("|") >= 3:
            cells = [c.strip() for c in stripped.split("|")[1:-1]]
            if not in_table:
                result.append("")
                # Add header underline
                header = " | ".join(cells)
                result.append(f"**{header}**")
                in_table = True
            else:
                result.append(" | ".join(cells))
            continue
        # End of table
        if in_table:
            result.append("")
            in_table = False
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

    # 1. Add "thinking" reaction to indicate processing
    thinking_reaction_id = None
    if msg_id:
        thinking_reaction_id = await _add_reaction(msg_id, _FEISHU_REACTION_THINKING)

    # 2. Send a "thinking" card immediately (reply to the original message for thread support)
    card_msg_id = await _send_thinking_card(chat_id, reply_to_msg_id=msg_id)

    # 3. Get session for context
    session = _get_session(chat_id)
    context_id = session.get("context_id")

    # 4. Call A2A
    reply, new_context_id = await invoke_a2a(text, context_id=context_id)

    # 5. Update session with new context
    if new_context_id:
        _update_session(chat_id, new_context_id)

    # 6. Handle errors
    if reply is None:
        if card_msg_id:
            await _update_streaming_card(card_msg_id, "⏰ kagent 响应超时，请稍后重试。")
        if thinking_reaction_id and msg_id:
            await _remove_reaction(msg_id, thinking_reaction_id)
            await _add_reaction(msg_id, _FEISHU_REACTION_ERROR)
        return

    # 7. Update the streaming card with the final reply
    if card_msg_id:
        await _update_streaming_card(card_msg_id, reply)

    # 8. Remove thinking reaction on success
    if thinking_reaction_id and msg_id:
        await _remove_reaction(msg_id, thinking_reaction_id)


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