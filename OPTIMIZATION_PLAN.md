# Feishu A2A Bot 优化计划

## 当前状态

✅ 基础功能完成
- 接收飞书消息
- 调用 kagent A2A
- 回复纯文本消息
- 长文本自动分片

---

## 优化方向

### 🎨 1. 交互体验优化

#### 1.1 卡片消息（Interactive Cards）
**难度**: ⭐⭐ | **价值**: ⭐⭐⭐⭐⭐

将纯文本改为富文本卡片：
- 支持 Markdown 格式（加粗、列表、代码块、表格）
- 彩色标题栏
- 分隔线、图片
- 更好的可读性

**改动**:
```python
# 当前
{"msg_type": "text", "content": json.dumps({"text": reply})}

# 优化后
{
  "msg_type": "interactive",
  "card": {
    "header": {"title": {...}, "template": "blue"},
    "elements": [{"tag": "markdown", "content": reply}]
  }
}
```

#### 1.2 流式回复（打字效果）
**难度**: ⭐⭐⭐ | **价值**: ⭐⭐⭐⭐

模拟 ChatGPT 的打字效果：
1. 先发送"⏳ 思考中..."
2. 逐步更新消息内容
3. 完成后显示最终回复

**改动**:
- 需要 kagent 支持流式输出（SSE/WebSocket）
- 使用飞书"更新消息" API
- 添加节流（避免 API 限流）

#### 1.3 交互按钮
**难度**: ⭐⭐⭐ | **价值**: ⭐⭐⭐

在卡片中添加按钮：
- "重新生成"
- "展开详情"
- "执行操作"（需要确认的危险操作）
- "复制代码"

**改动**:
- 注册按钮回调 webhook
- 处理 `card_action_callback` 事件
- 根据按钮 ID 执行不同逻辑

#### 1.4 Typing 指示器
**难度**: ⭐ | **价值**: ⭐⭐

在回复前发送"正在输入..."状态：
```python
# 飞书没有官方 typing API，但可以发一条临时消息
send_message("🤔 正在思考...", delete_after=3)
```

---

### 🧠 2. 智能对话优化

#### 2.1 Session 管理
**难度**: ⭐⭐⭐ | **价值**: ⭐⭐⭐⭐⭐

当前每条消息都是独立的，没有上下文。添加 session：
- 使用 `contextId` 维持对话上下文
- 支持多轮对话
- 可选：会话超时、手动清除

**改动**:
```python
# 在 A2A 请求中传递 contextId
{"params": {"message": {...}, "contextId": session_id}}
```

#### 2.2 命令系统
**难度**: ⭐⭐ | **价值**: ⭐⭐⭐

支持特殊命令：
- `/help` - 显示帮助
- `/clear` - 清除会话
- `/status` - 显示系统状态
- `/model xxx` - 切换模型

#### 2.3 消息历史
**难度**: ⭐⭐ | **价值**: ⭐⭐⭐

- 保存对话历史到数据库（SQLite/Redis）
- 支持导出对话
- 支持搜索历史消息

---

### 📦 3. 消息类型扩展

#### 3.1 支持图片
**难度**: ⭐⭐⭐ | **价值**: ⭐⭐⭐

- 接收图片消息
- 下载并保存到临时目录
- 传递给 kagent（如果支持）
- 显示 kagent 返回的图片

#### 3.2 支持文件
**难度**: ⭐⭐⭐ | **价值**: ⭐⭐⭐

- 接收文件（PDF、文档、代码）
- 提取文本内容
- 传递给 kagent 分析

#### 3.3 支持富文本
**难度**: ⭐⭐ | **价值**: ⭐⭐

- 解析飞书 post 类型消息
- 提取纯文本内容

#### 3.4 支持@提及
**难度**: ⭐ | **价值**: ⭐⭐

- 在群聊中只响应 @机器人 的消息
- 自动去除 @mention 文本

---

### ⚡ 4. 性能优化

#### 4.1 连接池
**难度**: ⭐ | **价值**: ⭐⭐⭐

当前每次请求都创建新的 httpx 客户端：
```python
# 当前
async with httpx.AsyncClient() as client:
    resp = await client.post(...)

# 优化
# 全局复用客户端
http_client = httpx.AsyncClient()
resp = await http_client.post(...)
```

#### 4.2 Token 缓存
**难度**: ⭐ | **价值**: ⭐⭐⭐

当前每次都获取新的 access_token：
```python
# 当前
token = await get_access_token()  # 每次都请求

# 优化
# 缓存 token，过期前 5 分钟刷新
if time.time() > token_expires - 300:
    token = await refresh_token()
```

**状态**: ✅ 已实现

#### 4.3 异步队列
**难度**: ⭐⭐⭐ | **价值**: ⭐⭐⭐

当前消息处理是同步的，可能阻塞：
- 使用 Celery/RQ 处理消息
- 立即返回 200，异步处理
- 避免飞书超时

**状态**: ✅ 已实现（使用 asyncio）

#### 4.4 请求去重
**难度**: ⭐⭐ | **价值**: ⭐⭐

飞书可能重复发送事件：
- 使用 `event_id` 去重
- Redis 存储已处理的 event_id
- 避免重复调用 kagent

---

### 🔒 5. 安全加固

#### 5.1 签名验证
**难度**: ⭐ | **价值**: ⭐⭐⭐⭐⭐

验证请求来自飞书：
```python
# 验证 X-Lark-Signature
signature = headers.get("X-Lark-Signature")
if not verify_signature(timestamp, nonce, encrypt_key, body, signature):
    return 403
```

**状态**: ✅ 已实现

#### 5.2 加密解密
**难度**: ⭐⭐ | **价值**: ⭐⭐⭐⭐

解密飞书发送的加密消息：
- AES-256-CBC 解密
- 使用 Encrypt Key

**状态**: ✅ 已实现

#### 5.3 Rate Limiting
**难度**: ⭐⭐ | **价值**: ⭐⭐⭐

防止滥用：
- 每个用户每分钟最多 N 条消息
- 使用 Redis 存储计数
- 超限返回友好提示

#### 5.4 输入验证
**难度**: ⭐⭐ | **价值**: ⭐⭐⭐

- 限制消息长度（避免超长攻击）
- 过滤特殊字符
- 防止注入攻击

#### 5.5 敏感信息脱敏
**难度**: ⭐⭐ | **价值**: ⭐⭐⭐⭐

kagent 可能返回敏感信息：
- 检测并隐藏密码、token
- 日志中脱敏
- 回复中提醒用户

---

### 🐛 6. 错误处理优化

#### 6.1 友好的错误提示
**难度**: ⭐ | **价值**: ⭐⭐⭐⭐

当前错误只记录到日志，用户看不到：
```python
# 当前
logger.error("Failed to send message")

# 优化
await reply_to_feishu(chat_id, "❌ 处理失败，请稍后重试")
```

#### 6.2 超时处理
**难度**: ⭐⭐ | **价值**: ⭐⭐⭐

kagent 可能超时：
- 设置合理超时（30s）
- 超时后发送提示
- 支持重试

#### 6.3 重试机制
**难度**: ⭐⭐ | **价值**: ⭐⭐⭐

网络错误自动重试：
- 指数退避（1s, 2s, 4s）
- 最多重试 3 次
- 记录重试日志

#### 6.4 健康检查
**难度**: ⭐ | **价值**: ⭐⭐

添加 `/health` 端点：
- 检查 kagent 连接
- 检查飞书 API 连接
- 返回详细状态

**状态**: ✅ 部分实现（只检查服务本身）

---

### 📊 7. 监控与日志

#### 7.1 结构化日志
**难度**: ⭐⭐ | **价值**: ⭐⭐⭐⭐

使用 JSON 格式日志：
```json
{
  "timestamp": "2026-08-21T20:54:13Z",
  "level": "INFO",
  "event": "message_received",
  "chat_id": "oc_xxx",
  "user_id": "ou_xxx",
  "text": "hello"
}
```

#### 7.2 Metrics
**难度**: ⭐⭐⭐ | **价值**: ⭐⭐⭐

Prometheus metrics：
- 消息处理延迟
- 错误率
- 活跃用户数
- kagent 调用次数

#### 7.3 Tracing
**难度**: ⭐⭐⭐ | **价值**: ⭐⭐⭐

OpenTelemetry tracing：
- 跟踪消息从接收到回复的完整链路
- 找出性能瓶颈

#### 7.4 告警
**难度**: ⭐⭐⭐ | **价值**: ⭐⭐⭐

错误告警：
- 错误率超过阈值
- kagent 不可用
- 飞书 API 错误

---

### 🚀 8. 部署优化

#### 8.1 Docker 镜像
**难度**: ⭐⭐ | **价值**: ⭐⭐⭐⭐

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN pip install -r requirements.txt
CMD ["python", "main.py"]
```

#### 8.2 Kubernetes Manifests
**难度**: ⭐⭐⭐ | **价值**: ⭐⭐⭐

部署到 k8s：
- Deployment
- Service
- Ingress（自动 HTTPS）
- ConfigMap（环境变量）
- Secrets（敏感信息）

#### 8.3 CI/CD
**难度**: ⭐⭐⭐ | **价值**: ⭐⭐⭐

GitHub Actions：
- 自动测试
- 自动构建镜像
- 自动部署

#### 8.4 固定域名
**难度**: ⭐⭐ | **价值**: ⭐⭐⭐⭐

替换 cloudflared 临时域名：
- 购买域名
- 配置 DNS
- Let's Encrypt 证书
- 或部署到有固定域名的平台

---

### 🧪 9. 测试

#### 9.1 单元测试
**难度**: ⭐⭐ | **价值**: ⭐⭐⭐⭐

测试核心逻辑：
- 消息解析
- 文本分片
- 签名验证
- A2A 调用

#### 9.2 集成测试
**难度**: ⭐⭐⭐ | **价值**: ⭐⭐⭐⭐

测试完整流程：
- 模拟飞书 webhook
- 验证消息发送

#### 9.3 E2E 测试
**难度**: ⭐⭐⭐⭐ | **价值**: ⭐⭐⭐

真实环境测试：
- 创建测试飞书应用
- 自动发送消息
- 验证回复

---

### 📝 10. 文档与配置

#### 10.1 README 完善
**难度**: ⭐ | **价值**: ⭐⭐⭐⭐

- 架构图
- 快速开始
- 配置说明
- FAQ
-  troubleshooting

**状态**: ✅ 部分完成

#### 10.2 配置灵活性
**难度**: ⭐⭐ | **价值**: ⭐⭐⭐

支持更多配置：
- 回复模板
- 错误消息
- 功能开关（卡片/流式/session）
- 多 kagent 实例

#### 10.3 API 文档
**难度**: ⭐⭐ | **价值**: ⭐⭐

自动生成 API 文档：
- FastAPI 自带 Swagger
- 添加详细描述

---

## 优先级排序

### 🔴 P0 - 立即做（影响用户体验）

1. **卡片消息** - 改动小，效果明显
2. **友好的错误提示** - 用户需要知道发生了什么
3. **Session 管理** - 支持多轮对话

### 🟡 P1 - 近期做（提升质量）

4. **流式回复** - 体验更好
5. **Rate Limiting** - 防止滥用
6. **Docker 镜像** - 方便部署
7. **固定域名** - 生产环境必需

### 🟢 P2 - 后续做（锦上添花）

8. **交互按钮** - 高级功能
9. **Metrics/Monitoring** - 运维友好
10. **图片/文件支持** - 扩展功能
11. **CI/CD** - 自动化

---

## 实施计划

### Phase 1: 基础优化（1-2 天）

- [ ] 卡片消息支持
- [ ] 友好的错误提示
- [ ] Session 管理（contextId）

### Phase 2: 体验优化（3-5 天）

- [ ] 流式回复
- [ ] 交互按钮
- [ ] Rate Limiting
- [ ] 连接池优化

### Phase 3: 生产就绪（1 周）

- [ ] Docker 镜像
- [ ] Kubernetes 部署
- [ ] 固定域名 + HTTPS
- [ ] 完整测试

### Phase 4: 高级功能（持续）

- [ ] 图片/文件支持
- [ ] Metrics/Monitoring
- [ ] CI/CD
- [ ] 多 kagent 支持

---

## 技术选型建议

### 卡片消息
- 使用飞书卡片 JSON Schema
- 参考：https://open.feishu.cn/document/common-capabilities/message-card/card-introduction

### Session 存储
- 开发/测试：内存字典
- 生产：Redis

### 部署
- 小型：单机 Docker + Nginx
- 中型：Kubernetes
- 大型：多集群 + 自动扩缩容

### 监控
- 日志：ELK Stack 或 Loki
- Metrics：Prometheus + Grafana
- Tracing：Jaeger 或 Tempo

---

## 参考资料

- [飞书消息卡片](https://open.feishu.cn/document/common-capabilities/message-card/card-introduction)
- [飞书消息 API](https://open.feishu.cn/document/server-docs/im-v1/message/create)
- [A2A Protocol](https://github.com/google/A2A)
- [kagent 文档](https://kagent.dev/docs/)

---

## 备注

- 所有优化都应该向后兼容
- 新功能应该可以配置开关
- 保持代码简洁，避免过度设计
- 优先解决用户痛点
