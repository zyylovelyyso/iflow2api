# iflow2api

将 iFlow CLI 的 AI 服务暴露为 OpenAI 兼容 API。

> 说明：本仓库为 `cacaview/iflow2api` 的 fork，用于个人桌面使用场景的增强（多账号账号池 + 负载均衡/并发上限/熔断-failover + OpenCode 一键接入 + Windows 桌面快捷方式）。

## 功能

- 自动读取 iFlow 配置文件 (`~/.iflow/settings.json`)
- 提供 OpenAI 兼容的 API 端点
- 支持流式和非流式响应
- 通过 `User-Agent: iFlow-Cli` 解锁 CLI 专属高级模型
- 内置 GUI OAuth 登录界面，无需安装 iFlow CLI
- 支持 OAuth token 自动刷新

## 支持的模型

| 模型 ID | 名称 | 说明 |
|---------|------|------|
| `glm-4.7` | GLM-4.7 | 智谱 GLM-4.7 |
| `minimax-m2.1` | MiniMax-M2.1 | MiniMax M2.1 |
| `iflow-rome-30ba3b` | iFlow-ROME-30BA3B | iFlow ROME 30B |
| `deepseek-r1` | DeepSeek-R1 | DeepSeek 推理（思考）模型 |
| `deepseek-v3.2` | DeepSeek-V3.2 | DeepSeek V3.2 |
| `qwen3-coder-plus` | Qwen3-Coder-Plus | 通义千问 Qwen3 Coder Plus |
| `kimi-k2-0905` | Kimi-K2-0905 | Moonshot Kimi K2 0905 |

> 实际可用模型以 `GET /v1/models` 返回为准（优先直连上游 `/models`；失败时回退到内置“已知集合”）。

## 前置条件

### 登录方式（二选一）

#### 方式 1: 使用内置 GUI 登录（推荐）

无需安装 iFlow CLI，直接使用内置登录界面：

```bash
# 启动服务时会自动打开登录界面
python -m iflow2api
```

点击界面上的 "OAuth 登录" 按钮，完成登录即可。

#### 方式 2: 使用 iFlow CLI 登录

如果你已安装 iFlow CLI，可以直接使用：

```bash
# 安装 iFlow CLI
npm i -g @iflow-ai/iflow-cli

# 运行登录
iflow
```

### 配置文件

登录后配置文件会自动生成：
- Windows: `C:\Users\<用户名>\.iflow\settings.json`
- Linux/Mac: `~/.iflow/settings.json`

## 安装

```bash
# 使用 uv (推荐)
uv pip install -e .

# 或使用 pip
pip install -e .
```

## 使用

### 启动服务

```bash
# 方式 1: 使用模块
python -m iflow2api

# 方式 2: 使用命令行
iflow2api
```

服务默认运行在 `http://localhost:8000`

Web 控制台（推荐）：`http://localhost:8000/ui`

### 自定义端口

```bash
python -c "import uvicorn; from iflow2api.app import app; uvicorn.run(app, host='0.0.0.0', port=8001)"
```

## API 端点

| 端点 | 方法 | 说明 |
|------|------|------|
| `/health` | GET | 健康检查 |
| `/v1/models` | GET | 获取可用模型列表 |
| `/v1/chat/completions` | POST | Chat Completions API |
| `/ui` | GET | 本地 Web 控制台（账号池管理 / OAuth 登录） |
| `/models` | GET | 兼容端点 (不带 /v1 前缀) |
| `/chat/completions` | POST | 兼容端点 (不带 /v1 前缀) |

## 客户端配置示例

### Python (OpenAI SDK)

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8000/v1",
    api_key="sk-your-local-key"  # 从 http://localhost:8000/ui 复制 Client API Key
)

# 非流式请求
response = client.chat.completions.create(
    model="glm-4.7",
    messages=[{"role": "user", "content": "你好！"}]
)
print(response.choices[0].message.content)

# 流式请求
stream = client.chat.completions.create(
    model="glm-4.7",
    messages=[{"role": "user", "content": "写一首诗"}],
    stream=True
)
for chunk in stream:
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="")
```

> 思考（Reasoning）默认开启：对 `glm-*`、`deepseek-r1`、以及模型名包含 `thinking` 的模型，iflow2api 会在请求中默认补上 `enable_thinking=true`（除非你显式传了 `enable_thinking` / `thinking` / `reasoning` 等字段）。如果想关掉，在请求 JSON 里加 `"enable_thinking": false`。

### curl

```bash
# 获取模型列表
curl http://localhost:8000/v1/models \
  -H "Authorization: Bearer sk-your-local-key"

# 非流式请求
curl http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer sk-your-local-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "glm-4.7",
    "messages": [{"role": "user", "content": "你好！"}]
  }'

# 流式请求
curl http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer sk-your-local-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "glm-4.7",
    "messages": [{"role": "user", "content": "你好！"}],
    "stream": true
  }'
```

### 第三方客户端

本服务兼容以下 OpenAI 兼容客户端:

- **ChatGPT-Next-Web**: 设置 API 地址为 `http://localhost:8000`
- **LobeChat**: 添加 OpenAI 兼容提供商，Base URL 设为 `http://localhost:8000/v1`
- **Open WebUI**: 添加 OpenAI 兼容连接
- **其他 OpenAI SDK 兼容应用**

## 多账号 / 多并发（可选）

默认模式下，服务会读取本机的 `~/.iflow/settings.json`，所有请求共用同一个 iFlow 账号。

如果你希望：
- 用“本服务自己的 API Key”隔离不同调用方（OpenCode/多个设备/多个用户）
- 将不同调用方路由到不同的 iFlow 账号
- 对每个 iFlow 账号做并发上限（避免单账号被打爆/风控）

可以创建配置文件：
- Windows: `C:\\Users\\<用户名>\\.iflow2api\\keys.json`
- Linux/Mac: `~/.iflow2api/keys.json`

### OAuth 多账号（推荐：可自动续期）

如果你用的是 iFlow 的 **浏览器登录（OAuth）**，推荐用本项目的 **Web 控制台** 添加账号：

1. 在 Edge 里创建多个 Profile（每个 Profile 登录一个 iFlow 账号）
2. 启动服务后打开 `http://localhost:8000/ui`
3. 选择 `Edge Profile` → 点击“添加账号（Edge Profile OAuth）”
4. 登录成功后会写入 `keys.json`，并保存 `oauth_refresh_token / oauth_expires_at`（不会在日志里打印明文）
5. 服务启动后会后台自动刷新 OAuth token，并在遇到上游 `439 Token expired` 时自动刷新后重试

示例 `keys.json`：

```json
{
  "auth": { "enabled": true, "required": true },
  "accounts": {
    "acc1": { "api_key": "iflow_apiKey_1", "base_url": "https://apis.iflow.cn/v1", "max_concurrency": 2 },
    "acc2": { "api_key": "iflow_apiKey_2", "base_url": "https://apis.iflow.cn/v1", "max_concurrency": 2 }
  },
  "keys": {
    "sk-local-user-a": { "account": "acc1" },
    "sk-local-user-b": { "account": "acc2" },
    "sk-local-pool": { "accounts": ["acc1", "acc2"], "strategy": "least_busy" }
  },
  "default": { "account": "acc1" }
}
```

说明：
- `auth.enabled=true` 才会校验来访请求的 `Authorization: Bearer <token>`。
- `auth.required=true` 时，缺少/错误 token 会直接返回 401。
- `keys.<token>` 用来把“来访 API Key”映射到上游 `accounts`。
- `max_concurrency` 为单个上游账号的并发上限（0 表示不限制）。
- `accounts` 池支持 `least_busy`（按 in-flight 选择）或 `round_robin`。
- `resilience`（可选）控制失败熔断与 failover（例如连续失败 N 次后临时禁用该账号一段时间，并在可重试错误上自动切换到另一个账号）。

可选的 OAuth 字段（如果你用 GUI 的 OAuth 登录添加账号，会自动写入）：
- `auth_type`: `"oauth-iflow"`
- `oauth_access_token`, `oauth_refresh_token`
- `oauth_expires_at`, `last_refresh_at`

### 稳定性（熔断 / failover）

你可以在 `keys.json` 顶层加入（不填会用默认值）：

```json
{
  "resilience": {
    "enabled": true,
    "failure_threshold": 3,
    "cool_down_seconds": 30,
    "retry_attempts": 1,
    "retry_backoff_ms": 200,
    "retry_status_codes": [429, 500, 502, 503, 504]
  }
}
```

并提供一个本地调试端点（不返回任何上游密钥）：
- `GET /debug/accounts`

### OpenCode 接入建议

如果 OpenCode 支持 OpenAI 兼容 `Chat Completions` + 自定义 Base URL：
- Base URL: `http://127.0.0.1:8000/v1`
- API Key: 填 `keys.json` 里配置的任意一个 token（例如 `sk-local-user-a`）
- Model: 使用 iFlow 模型 ID（例如 `glm-4.7`）

## 架构

```
┌─────────────────────────────────────────────────────────────┐
│                      客户端请求                              │
│  (OpenAI SDK / curl / ChatGPT-Next-Web / LobeChat)         │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                    iflow2api 本地代理                        │
│  ┌─────────────────────────────────────────────────────┐   │
│  │  /v1/chat/completions  │  /v1/models  │  /health   │   │
│  └─────────────────────────────────────────────────────┘   │
│                              │                              │
│  ┌─────────────────────────────────────────────────────┐   │
│  │  1. 读取 ~/.iflow/settings.json 获取认证信息         │   │
│  │  2. 添加 User-Agent: iFlow-Cli 解锁高级模型          │   │
│  │  3. 转发请求到 iFlow API                            │   │
│  └─────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                    iFlow API 服务                            │
│                https://apis.iflow.cn/v1                      │
└─────────────────────────────────────────────────────────────┘
```

## 工作原理

iFlow API 通过 `User-Agent` header 区分普通 API 调用和 CLI 调用:

- **普通 API 调用**: 只能使用基础模型
- **CLI 调用** (`User-Agent: iFlow-Cli`): 可使用 GLM-4.7、DeepSeek、Kimi 等高级模型

本项目通过在请求中添加 `User-Agent: iFlow-Cli` header，让普通 API 客户端也能访问 CLI 专属模型。

## 项目结构

```
iflow2api/
├── __init__.py          # 包初始化
├── __main__.py          # CLI 入口 (python -m iflow2api)
├── main.py              # 主入口
├── config.py            # iFlow 配置读取器 (从 ~/.iflow/settings.json)
├── proxy.py             # API 代理 (添加 User-Agent header)
├── app.py               # FastAPI 应用 (OpenAI 兼容端点)
├── oauth.py             # OAuth 认证逻辑
├── oauth_login.py       # OAuth 登录处理器
├── token_refresher.py   # OAuth token 自动刷新
├── settings.py          # 应用配置管理
└── gui.py               # GUI 界面
```

## 常见问题

### Q: 提示 "iFlow 未登录"

确保已完成登录：
- **GUI 方式**：点击界面上的 "OAuth 登录" 按钮
- **CLI 方式**：运行 `iflow` 命令并完成登录

检查 `~/.iflow/settings.json` 文件是否存在且包含 `apiKey` 字段。

### Q: 模型调用失败

1. 确认使用的模型 ID 正确（参考上方模型列表）
2. 检查 iFlow 账户是否有足够的额度
3. 查看服务日志获取详细错误信息

### Q: 如何更新模型列表

模型列表硬编码在 `proxy.py` 中，来源于 iflow-cli 源码。如果 iFlow 更新了支持的模型，需要手动更新此列表。

### Q: 是否必须安装 iFlow CLI？

不是。从 v0.4.1 开始，项目内置了 GUI OAuth 登录功能，无需安装 iFlow CLI 即可使用。

### Q: GUI 登录和 CLI 登录的配置可以共用吗？

可以。两种登录方式都使用同一个配置文件 `~/.iflow/settings.json`，GUI 登录后命令行模式可以直接使用，反之亦然。

### Q: macOS 上下载的应用无法执行

如果在 macOS 上通过浏览器下载 `iflow2api.app` 后无法执行，通常有两个原因：

1. **缺少执行权限**：可执行文件没有执行位
2. **隔离标记**：文件带有 `com.apple.quarantine` 属性

**修复方法**：

```bash
# 移除隔离标记
xattr -cr iflow2api.app

# 添加执行权限
chmod +x iflow2api.app/Contents/MacOS/iflow2api
```

执行上述命令后，应用就可以正常运行了。

## License

MIT
