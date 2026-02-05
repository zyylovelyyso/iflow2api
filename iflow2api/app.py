"""FastAPI 应用 - OpenAI 兼容 API 服务"""

import sys
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from .config import load_iflow_config, check_iflow_login, IFlowConfig
from .proxy_manager import ProxyManager
from .routing import load_routing_config
from .routing_refresher import start_global_routing_refresher, stop_global_routing_refresher


# 全局代理管理器
_proxy_manager: Optional[ProxyManager] = None


def get_proxy_manager() -> ProxyManager:
    global _proxy_manager
    if _proxy_manager is None:
        routing = load_routing_config()
        _proxy_manager = ProxyManager(routing)
    return _proxy_manager


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # 启动时检查配置
    try:
        manager = get_proxy_manager()
        routing = manager.routing

        if routing.accounts:
            print(f"[iflow2api] 已加载多账号配置: {len(routing.accounts)} 个账号")
            print(
                f"[iflow2api] 来访鉴权: {'启用' if routing.auth.enabled else '关闭'}"
                + (" (required)" if (routing.auth.enabled and routing.auth.required) else "")
            )
            # Multi-account auto-refresh (best-effort, no secrets printed).
            start_global_routing_refresher(log=lambda s: print(f"[iflow2api] {s}"))
        else:
            config = load_iflow_config()
            print(f"[iflow2api] 已加载 iFlow 配置")
            print(f"[iflow2api] API Base URL: {config.base_url}")
            print(f"[iflow2api] API Key: {config.api_key[:10]}...")
            if config.model_name:
                print(f"[iflow2api] 默认模型: {config.model_name}")
    except FileNotFoundError as e:
        print(f"[错误] {e}", file=sys.stderr)
        print("[提示] 未检测到多账号配置时，需要先运行 'iflow' 命令并完成登录", file=sys.stderr)
        sys.exit(1)
    except ValueError as e:
        print(f"[错误] {e}", file=sys.stderr)
        sys.exit(1)

    yield

    # 关闭时清理
    stop_global_routing_refresher()
    global _proxy_manager
    if _proxy_manager:
        await _proxy_manager.close()
        _proxy_manager = None


# 创建 FastAPI 应用
app = FastAPI(
    title="iflow2api",
    description="将 iFlow CLI 的 AI 服务暴露为 OpenAI 兼容 API",
    version="0.1.0",
    lifespan=lifespan,
)

# 添加 CORS 中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============ 请求/响应模型 ============

class ChatMessage(BaseModel):
    role: str
    content: Any  # 可以是字符串或内容块列表


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    max_tokens: Optional[int] = None
    stream: Optional[bool] = False
    stop: Optional[list[str] | str] = None
    presence_penalty: Optional[float] = None
    frequency_penalty: Optional[float] = None
    user: Optional[str] = None

    class Config:
        extra = "allow"  # 允许额外字段


# ============ API 端点 ============

@app.get("/")
async def root():
    """根路径"""
    return {
        "service": "iflow2api",
        "version": "0.1.0",
        "description": "iFlow CLI AI 服务 → OpenAI 兼容 API",
        "endpoints": {
            "models": "/v1/models",
            "chat_completions": "/v1/chat/completions",
            "health": "/health",
        },
    }


@app.get("/health")
async def health():
    """健康检查"""
    manager = get_proxy_manager()
    routing = manager.routing
    is_logged_in = bool(routing.accounts) or check_iflow_login()
    accounts_total = len(routing.accounts)
    accounts_enabled = sum(1 for a in routing.accounts.values() if getattr(a, "enabled", True)) if routing.accounts else 0
    oauth_accounts = sum(1 for a in routing.accounts.values() if getattr(a, "oauth_refresh_token", None)) if routing.accounts else 0
    return {
        "status": "healthy" if is_logged_in else "degraded",
        "iflow_logged_in": is_logged_in,
        "multi_account": bool(routing.accounts),
        "accounts_total": accounts_total,
        "accounts_enabled": accounts_enabled,
        "oauth_accounts": oauth_accounts,
    }


@app.get("/v1/models")
async def list_models():
    """获取可用模型列表"""
    try:
        manager = get_proxy_manager()
        proxy = await manager.get_any_proxy()
        return await proxy.get_models()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/debug/accounts")
async def debug_accounts(request: Request):
    """
    Debug endpoint for local observability.

    - Never returns upstream secrets.
    - If `keys.json` auth is enabled+required, requires a valid Bearer token.
    """
    manager = get_proxy_manager()
    routing = manager.routing
    if routing.auth.enabled and routing.auth.required:
        token = request.headers.get("Authorization", "")
        token = token[7:].strip() if token.lower().startswith("bearer ") else token.strip()
        if not token or token not in routing.keys:
            raise HTTPException(status_code=401, detail="Invalid API key")
    return {
        "resilience": routing.resilience.model_dump(),
        "accounts": manager.get_account_metrics(),
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """Chat Completions API - OpenAI 兼容"""
    try:
        # 解析请求体 - 使用 bytes 然后手动解码以处理编码问题
        body_bytes = await request.body()
        import json
        body = json.loads(body_bytes.decode("utf-8"))
        stream = body.get("stream", False)

        manager = get_proxy_manager()

        if stream:
            # 流式响应
            async def generate():
                async for chunk in await manager.chat_completions(request, body, stream=True):
                    yield chunk

            return StreamingResponse(
                generate(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )
        else:
            # 非流式响应
            result = await manager.chat_completions(request, body, stream=False)
            return JSONResponse(content=result)

    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")
    except Exception as e:
        error_msg = str(e)
        # 尝试解析 iFlow API 的错误响应
        if hasattr(e, "response"):
            try:
                error_data = e.response.json()
                error_msg = error_data.get("msg", error_msg)
            except Exception:
                pass
        raise HTTPException(status_code=500, detail=error_msg)


# ============ 兼容端点 ============

@app.post("/chat/completions")
async def chat_completions_compat(request: Request):
    """Chat Completions API - 兼容不带 /v1 前缀的请求"""
    return await chat_completions(request)


@app.get("/models")
async def list_models_compat():
    """Models API - 兼容不带 /v1 前缀的请求"""
    return await list_models()


def main():
    """主入口"""
    import uvicorn

    # 检查是否已登录
    try:
        manager = get_proxy_manager()
        if not manager.routing.accounts and not check_iflow_login():
            print("[错误] iFlow 未登录", file=sys.stderr)
            print("[提示] 请先运行 'iflow' 命令并完成登录，或配置 ~/.iflow2api/keys.json", file=sys.stderr)
            sys.exit(1)
    except Exception as e:
        print(f"[错误] {e}", file=sys.stderr)
        sys.exit(1)

    print("=" * 50)
    print("  iflow2api - iFlow CLI AI 服务代理")
    print("=" * 50)
    print()

    # 启动服务 - 直接传入 app 对象而非字符串，避免打包后导入失败
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        reload=False,
    )


if __name__ == "__main__":
    main()
