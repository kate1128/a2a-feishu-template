# A2A Feishu Bot Template / 飞书 A2A 机器人模板

A template for a [Feishu](https://open.feishu.cn) (飞书) / [Lark](https://open.larksuite.com) bot that connects to [kagent](https://github.com/kagent-dev/kagent) via the [A2A protocol](https://github.com/google/A2A).

飞书 / Lark 机器人模板，通过 [A2A 协议](https://github.com/google/A2A) 连接到 [kagent](https://github.com/kagent-dev/kagent)。

---

## Architecture / 架构

```
Feishu user (@bot hello)
       │
       ▼
Feishu Open Platform (event webhook)
       │  HTTPS POST
       ▼
┌──────────────────────────────┐
│  This bot (FastAPI)          │
│  - Verifies signature        │
│  - Decrypts payload (opt)    │
│  - Parses message            │
│  - Calls kagent via A2A      │
│  - Sends reply to Feishu     │
└──────────────────────────────┘
       │  A2A JSON-RPC
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
  - Event subscription configured with Verification Token and Encrypt Key
  - Permissions granted:
    - `im:message` — send messages
    - `im:message.p2p_msg` — receive direct messages
    - `im:message.group_at_msg` — receive group @bot messages
    - `im:chat:readonly` — read chat info
  - Event subscription URL set to your bot's public URL (e.g. `https://your-bot.example.com/webhook/feishu`)
  - Event added: `im.message.receive_v1`

- A kagent agent with an A2A endpoint exposed (see [kagent docs](https://kagent.dev/docs/))

## Setup / 设置

### 1. Clone / 克隆

```bash
git clone https://github.com/kagent-dev/a2a-feishu-template.git
cd a2a-feishu-template
```

### 2. Install dependencies / 安装依赖

```bash
uv sync
```

Or with pip:
```bash
pip install -r requirements.txt  # if you generate one from pyproject.toml
```

### 3. Configure / 配置

```bash
cp .env.example .env
```

Edit `.env` and fill in:

| Variable | Description | Example |
|---|---|---|
| `FEISHU_APP_ID` | Feishu app ID (cli_xxxx) | `cli_a1b2c3d4e5` |
| `FEISHU_APP_SECRET` | Feishu app secret | `xxxxxxxxxxxxxxxx` |
| `FEISHU_VERIFICATION_TOKEN` | Event subscription verification token | `xxxxxxxxxxxxxxxx` |
| `FEISHU_ENCRYPT_KEY` | Event subscription encrypt key (optional but recommended) | `xxxxxxxxxxxxxxxx` |
| `FEISHU_DOMAIN` | `feishu` (China) or `lark` (international) | `feishu` |
| `KAGENT_A2A_URL` | kagent A2A endpoint URL | `http://localhost:8080/a2a/default/my-agent` |
| `PORT` | HTTP port (default 9000) | `9000` |

### 4. Run / 运行

```bash
uv run main.py
```

The bot starts on port 9000 by default. Feishu will POST events to `/webhook/feishu`.

### 5. Expose to the internet / 暴露到公网

Feishu webhooks require a public HTTPS endpoint. For local testing, use [ngrok](https://ngrok.com/) or [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/):

```bash
ngrok http 9000
```

Then set the resulting URL (e.g. `https://abcd-1234.ngrok.io/webhook/feishu`) as the event subscription URL in the Feishu developer console.

## Usage / 使用

1. Add the bot to a Feishu group chat, or DM it directly.
2. Send a message (in a group, `@bot` it first).
3. The bot forwards the message to kagent via A2A, and replies with the agent's response.

## Features / 功能

- ✅ Text messages (in group chats and DMs)
- ✅ Auto-splits long replies (Feishu has a 4KB message limit)
- ✅ Supports both Feishu (China) and Lark (international)
- ✅ Optional payload encryption (AES-256-CBC)
- ✅ Strips `@bot` mentions in group chats

## Limitations / 限制

- ⚠️ v1: only text messages (no images, files, rich text yet)
- ⚠️ No streaming replies (Feishu API doesn't support message editing well)
- ⚠️ No session management yet (each message is an independent A2A call)

## References / 参考

- [Slack A2A template](https://github.com/kagent-dev/a2a-slack-template) — the template this was modeled after
- [kagent docs](https://kagent.dev/docs/)
- [Feishu Open Platform docs](https://open.feishu.cn/document/)
- [Lark Developer docs](https://open.larksuite.com/document/)
- [A2A protocol](https://github.com/google/A2A)

## License

Apache 2.0 — same as kagent.
