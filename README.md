# A2A Feishu Bot Template / 飞书 A2A 机器人模板

<p align="center">
  <img src="https://img.shields.io/badge/python-3.12+-blue?logo=python&logoColor=white" alt="Python 3.12+" />
  <img src="https://img.shields.io/badge/license-Apache%202.0-green" alt="License" />
  <img src="https://img.shields.io/badge/A2A-0.3-purple?logo=google&logoColor=white" alt="A2A 0.3" />
  <img src="https://img.shields.io/badge/Feishu-Bot-blue?logo=feishu" alt="Feishu Bot" />
  <img src="https://img.shields.io/badge/kagent-integrated-orange?logo=kubernetes" alt="kagent" />
  <img src="https://img.shields.io/badge/FastAPI-0.115+-009688?logo=fastapi&logoColor=white" alt="FastAPI" />
</p>

A [Feishu](https://open.feishu.cn) (飞书) / [Lark](https://open.larksuite.com) bot that connects to [kagent](https://github.com/kagent-dev/kagent) agents via the [A2A protocol](https://github.com/google/A2A).

飞书 / Lark 机器人模板，通过 [A2A 协议](https://github.com/google/A2A) 连接到 [kagent](https://github.com/kagent-dev/kagent) agent。

---

## Features

| Feature | Status | Details |
|---|---|---|
| **WebSocket mode** (recommended) | ✅ | No public URL needed — connects directly to Feishu |
| **Webhook mode** (fallback) | ✅ | HTTPS webhook endpoint at `/webhook/feishu` |
| **Streaming card replies** | ✅ | Real-time card updates as the agent responds |
| **"Thinking" reaction** | ✅ | 👀 reaction on message → removed on completion |
| **Error reaction** | ✅ | ❌ reaction on failure |
| **Interactive card replies** | ✅ | Rich markdown cards with header, footer |
| **Session management** | ✅ | 30-min context window per chat |
| **Text messages** | ✅ | Group chats and DMs |
| **Rich text (post) messages** | ✅ | Extracts text from post messages |
| **Thread replies** | ✅ | Replies in the same thread |
| **Long text splitting** | ✅ | Handles Feishu's 4KB limit |
| **AES encryption** | ✅ | Optional event payload decryption |
| **Feishu / Lark dual support** | ✅ | Switch via `FEISHU_DOMAIN` env var |

## Architecture / 架构

```
Feishu user (@bot hello)
       │
       │ WebSocket (default, no public URL)  or  HTTPS webhook (fallback)
       ▼
┌──────────────────────────────────────────┐
│  This bot (FastAPI + WebSocket Client)   │
│                                         │
│  ┌─ WebSocket ───────────────────────┐  │
│  │  Connects to Feishu push channel  │  │
│  │  Auto-reconnect on disconnect     │  │
│  └───────────────────────────────────┘  │
│                                         │
│  ┌─ Event Processing ───────────────┐  │
│  │  im.message.receive_v1 → A2A     │  │
│  │  👀 thinking reaction            │  │
│  │  🤔 thinking card sent           │  │
│  │  🔄 streaming card update        │  │
│  │  ✅ reaction removed on success   │  │
│  └───────────────────────────────────┘  │
│                                         │
│  ┌─ A2A Client ─────────────────────┐  │
│  │  JSON-RPC to kagent              │  │
│  │  Session context preserved       │  │
│  └───────────────────────────────────┘  │
└──────────────────────────────────────────┘
       │  A2A JSON-RPC (HTTP)
       ▼
kagent (Agent A2A endpoint)
       │  LLM + tools
       ▼
   Reply → Feishu user
```

## Prerequisites / 前置条件

- Python 3.12+
- A Feishu / Lark custom app with:
  - App ID and App Secret (in "Credentials & Basic Info")
  - **Event subscription** configured with `im.message.receive_v1` event
  - Permissions granted **and published as a version**:
    - `im:message` — send messages
    - `im:message.p2p_msg` — receive direct messages
    - `im:message.group_at_msg` — receive group @bot messages
    - `im:chat:readonly` — read chat info
  - **WebSocket mode**: No event subscription URL needed
  - **Webhook mode**: Set event subscription URL to `https://your-bot.example.com/webhook/feishu`
- A kagent agent with an A2A endpoint exposed (e.g. `http://<controller>:8083/api/a2a/<ns>/<name>`)

## Quick Start / 快速开始

### 1. Install

```bash
git clone https://github.com/kagent-dev/a2a-feishu-template.git
cd a2a-feishu-template
uv sync
```

### 2. Configure

```bash
cp .env.example .env
```

Edit `.env`:

| Variable | Required | Default | Description |
|---|---|---|---|
| `FEISHU_APP_ID` | ✅ | — | Feishu app ID (`cli_xxxx`) |
| `FEISHU_APP_SECRET` | ✅ | — | Feishu app secret |
| `KAGENT_A2A_URL` | ✅ | — | kagent A2A endpoint URL |
| `FEISHU_DOMAIN` | ❌ | `feishu` | `feishu` (China) or `lark` (international) |
| `FEISHU_VERIFICATION_TOKEN` | ❌ | — | Webhook verification token (webhook mode only) |
| `FEISHU_ENCRYPT_KEY` | ❌ | — | Event payload encryption key (webhook mode only) |
| `PORT` | ❌ | `9000` | HTTP server port |

### 3. Run

```bash
uv run main.py
```

The bot starts with:
- **WebSocket client** → connects to Feishu automatically, no public URL needed
- **FastAPI server** → health check at `/health`, webhook fallback at `/webhook/feishu`

### 4. Add the bot to a Feishu group

In Feishu, open the group chat → Settings → Bots → Add bot → select your custom app. Then `@mention` the bot to chat.

## Usage / 使用

```
用户: @bot 帮我查一下集群状态
  → bot 👀 (thinking reaction)
  → bot 🤔 思考中... (card appears)
  → bot 💬 集群状态如下: ... (card updates with reply)
  → bot 👀 removed
```

## References / 参考

- [kagent docs](https://kagent.dev/docs/)
- [Feishu Open Platform docs](https://open.feishu.cn/document/)
- [Lark Developer docs](https://open.larksuite.com/document/)
- [A2A protocol](https://github.com/google/A2A)
- [Hermes Agent Feishu adapter](https://github.com/NousResearch/hermes-agent) (reference implementation)

## License

Apache 2.0