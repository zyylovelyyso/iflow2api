"""API 代理服务 - 转发请求到 iFlow API"""

import asyncio
import httpx
from typing import AsyncIterator, Optional
from contextlib import asynccontextmanager
from .config import IFlowConfig


# iFlow CLI 特殊 User-Agent，用于解锁更多模型
IFLOW_CLI_USER_AGENT = "iFlow-Cli"


class IFlowProxy:
    """iFlow API 代理"""

    def __init__(self, config: IFlowConfig, max_concurrency: int = 0):
        self.config = config
        self.base_url = config.base_url.rstrip("/")
        self._client: Optional[httpx.AsyncClient] = None
        self._max_concurrency = max_concurrency
        self._semaphore: Optional[asyncio.Semaphore] = (
            asyncio.Semaphore(max_concurrency) if max_concurrency and max_concurrency > 0 else None
        )
        self._in_flight: int = 0

    @property
    def in_flight(self) -> int:
        return self._in_flight

    @asynccontextmanager
    async def _limit(self):
        if self._semaphore is None:
            self._in_flight += 1
            try:
                yield
            finally:
                self._in_flight -= 1
            return

        await self._semaphore.acquire()
        self._in_flight += 1
        try:
            yield
        finally:
            self._in_flight -= 1
            self._semaphore.release()

    def _get_headers(self) -> dict:
        """获取请求头"""
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config.api_key}",
            "User-Agent": IFLOW_CLI_USER_AGENT,  # 大写以解锁 CLI 专属模型
        }

    async def _get_client(self) -> httpx.AsyncClient:
        """获取或创建 HTTP 客户端"""
        if self._client is None or self._client.is_closed:
            # Tuning for speed: keep connections warm and allow enough concurrency.
            if self._max_concurrency and self._max_concurrency > 0:
                max_connections = max(20, int(self._max_concurrency) * 4)
                max_keepalive = max(10, int(self._max_concurrency) * 2)
            else:
                max_connections = 100
                max_keepalive = 20

            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(300.0, connect=10.0),
                follow_redirects=True,
                http2=True,
                limits=httpx.Limits(
                    max_connections=max_connections,
                    max_keepalive_connections=max_keepalive,
                ),
            )
        return self._client

    async def close(self):
        """关闭 HTTP 客户端"""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def get_models(self) -> dict:
        """
        获取可用模型列表

        iFlow API 没有公开的 /models 端点，因此返回已知的模型列表。
        模型列表为常见模型的“已知集合”（可用性取决于账号权限与 iFlow 侧更新）。
        使用 iFlow-Cli User-Agent 可以解锁这些高级模型。
        """
        import time

        from .model_catalog import get_known_models, to_openai_models_list

        current_time = int(time.time())
        return to_openai_models_list(get_known_models(), owned_by="iflow", created=current_time)

    async def chat_completions(
        self,
        request_body: dict,
        stream: bool = False,
    ) -> dict | AsyncIterator[bytes]:
        """
                调用 chat completions API

                Args:
                    request_body: 请求体
                    stream: 是否流式响应

        Returns:
                    非流式: 返回完整响应 dict
                    流式: 返回字节流迭代器
        """
        client = await self._get_client()

        if stream:
            return self._stream_chat_completions(client, request_body)
        else:
            async with self._limit():
                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers=self._get_headers(),
                    json=request_body,
                )
                response.raise_for_status()
                result = response.json()

            # 确保 usage 统计信息存在 (OpenAI 兼容)
            if "usage" not in result:
                result["usage"] = {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                }

            return result

    async def _stream_chat_completions(
        self,
        client: httpx.AsyncClient,
        request_body: dict,
    ) -> AsyncIterator[bytes]:
        """流式调用 chat completions API"""
        async with self._limit():
            async with client.stream(
                "POST",
                f"{self.base_url}/chat/completions",
                headers=self._get_headers(),
                json=request_body,
            ) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes():
                    yield chunk

    async def proxy_request(
        self,
        method: str,
        path: str,
        body: Optional[dict] = None,
        stream: bool = False,
    ) -> dict | AsyncIterator[bytes]:
        """
        通用请求代理

        Args:
            method: HTTP 方法
            path: API 路径 (不含 base_url)
            body: 请求体
            stream: 是否流式响应

        Returns:
            响应数据
        """
        client = await self._get_client()
        url = f"{self.base_url}{path}"

        if stream and method.upper() == "POST":
            return self._stream_request(client, url, body)

        if method.upper() == "GET":
            response = await client.get(url, headers=self._get_headers())
        elif method.upper() == "POST":
            response = await client.post(url, headers=self._get_headers(), json=body)
        else:
            raise ValueError(f"不支持的 HTTP 方法: {method}")

        response.raise_for_status()
        return response.json()

    async def _stream_request(
        self,
        client: httpx.AsyncClient,
        url: str,
        body: Optional[dict],
    ) -> AsyncIterator[bytes]:
        """流式请求"""
        async with client.stream(
            "POST",
            url,
            headers=self._get_headers(),
            json=body,
        ) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                yield chunk
