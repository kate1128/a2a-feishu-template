# A2A Feishu Bot Template / 飞书 A2A 机器人模板

A [Feishu](https://open.feishu.cn) (飞书) / [Lark](https://open.larksuite.com) bot that connects to [kagent](https://github.com/kagent-dev/kagent) agents via the [A2A protocol](https://github.com/google/A2A).

## Features

| Feature | Status |
|---|---|
| **WebSocket mode** (no public URL needed) | ✅ |
| **Webhook mode** (fallback) | ✅ |
| **Interactive card replies** (schema 2.0) | ✅ |
| **"Thinking" reaction** (👀 during processing) | ✅ |
| **Cancel button** (cancel while agent is thinking) | ✅ |
| **Session management** (30-min context window) | ✅ |
| **Conversation history** (`/history` command) | ✅ |
| **Token usage display** (input/output/total) | ✅ |
| **Response time display** | ✅ |
| **Agent name display** | ✅ |
| **Text messages** (group chats and DMs) | ✅ |
| **Rich text (post) messages** | ✅ |
| **Thread replies** | ✅ |
| **Long text splitting** (handles 4KB limit) | ✅ |
| **Markdown tables** | ✅ |
| **AES encryption** (optional) | ✅ |
| **Feishu / Lark dual support** | ✅ |

## Quick Start

```bash
git clone https://github.com/kagent-dev/a2a-feishu-template.git
cd a2a-feishu-template
uv sync
cp .env.example .env
# Edit .env with your credentials
uv run main.py
```

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `FEISHU_APP_ID` | ✅ | — | Feishu app ID (`cli_xxxx`) |
| `FEISHU_APP_SECRET` | ✅ | — | Feishu app secret |
| `KAGENT_A2A_URL` | ✅ | — | kagent A2A endpoint URL, format `http://<kagent-controller-service-ip>:<port>/api/a2a/kagent/<your-agent-name>/` |
| `FEISHU_DOMAIN` | ❌ | `feishu` | `feishu` (China) or `lark` (international) |
| `FEISHU_VERIFICATION_TOKEN` | ❌ | — | Webhook verification token |
| `FEISHU_ENCRYPT_KEY` | ❌ | — | Event payload encryption key |
| `PORT` | ❌ | `9000` | HTTP server port |

## Commands

| Command | Description |
|---|---|
| `/history` or `history` or `会话历史` | Show recent conversation history |

## Architecture

```
Feishu user (@bot hello)
       │
       │ WebSocket (default, no public URL)
       ▼
┌──────────────────────────────────────────┐
│  This bot (FastAPI + WebSocket Client)   │
│  WebSocket → event handler → A2A → card  │
└──────────────────────────────────────────┘
       │  A2A JSON-RPC (HTTP)
       ▼
kagent (Agent A2A endpoint)
```

## Interaction Flow

```
User: @bot hello
  → 👀 reaction added
  → 🤔 "thinking..." card with ❌ cancel button
  → A2A message/send (background task)
  → User clicks ❌ cancel → background task cancelled
  → Card updated: "已取消 ✅"
  → OR A2A response received
  → Card updated: reply with agent name, time, token usage
  → 👀 reaction removed
```

## Deployment

### Docker

```bash
docker build -t feishu-a2a-bot:latest .
docker run --rm -d --name feishu-bot \
  -p 9000:9000 \
  --env-file .env \
  feishu-a2a-bot:latest
```

The image runs as a non-root user on port 9000 and exposes a `/health` endpoint
(used by the Docker `HEALTHCHECK` and the Kubernetes probes). `.env` is excluded
from the build via [.dockerignore](.dockerignore) — credentials are injected at
runtime, never baked into the image.

### Kubernetes

Manifests live in [deploy/k8s/](deploy/k8s/) (kustomize):

```bash
# 1. Build the image, push it to your registry, then update the `image:` field
#    in deploy/k8s/deployment.yaml
docker build -t <registry>/feishu-a2a-bot:latest .

# 2. Create the credentials Secret (edit the manifest first)
cp deploy/k8s/secret.example.yaml deploy/k8s/secret.yaml
# fill in the real values, then:
kubectl apply -f deploy/k8s/secret.yaml

# 3. Deploy everything
kubectl apply -k deploy/k8s/
kubectl rollout status -n feishu-bot deploy/feishu-bot
```

Notes:

- The Deployment intentionally runs **1 replica**: WebSocket mode keeps a single
  long-lived Feishu connection and conversation sessions are in-memory. Scaling
  out would open duplicate connections and duplicate/race events.
- The bot dials Feishu **outbound**, so no Ingress is needed for WebSocket mode.
  Webhook-mode fallback only: `kubectl apply -f deploy/k8s/ingress.yaml`.

## Prerequisites

- Python 3.12+
- A Feishu / Lark custom app with:
  - `im:message`, `im:message.p2p_msg`, `im:message.group_at_msg`, `im:chat:readonly` permissions
  - `im.message.receive_v1` event subscription
  - **Card callback interaction** enabled (for cancel button)
- A kagent agent with an A2A endpoint

## References

- [kagent docs](https://kagent.dev/docs/)
- [Feishu Open Platform docs](https://open.feishu.cn/document/)
- [A2A protocol](https://github.com/google/A2A)
- [Hermes Agent Feishu adapter](https://github.com/NousResearch/hermes-agent)

## License

Apache 2.0
