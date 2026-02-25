"""API 代理服务 - 转发请求到 iFlow API"""

import asyncio
import hashlib
import hmac
import json
import time
import uuid
import httpx
from typing import AsyncIterator, Optional
from contextlib import asynccontextmanager
from .config import IFlowConfig


# iFlow CLI 特殊 User-Agent，用于解锁更多模型
IFLOW_CLI_USER_AGENT = "iFlow-Cli"


class _UpstreamErrorResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = int(status_code)
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class IFlowUpstreamError(Exception):
    """Upstream business error returned in JSON payload (often with HTTP 200)."""

    def __init__(self, status_code: int, message: str, payload: dict):
        super().__init__(message)
        self.response = _UpstreamErrorResponse(status_code, payload)


def _raise_iflow_payload_error(payload: dict) -> None:
    if not isinstance(payload, dict):
        return
    code_raw = payload.get("status")
    msg_raw = payload.get("msg") or payload.get("message")
    if code_raw is None:
        return
    try:
        code = int(str(code_raw).strip())
    except Exception:
        return
    if code in (0, 200):
        return
    msg = str(msg_raw or f"Upstream error {code}")
    raise IFlowUpstreamError(code, msg, payload)


def _add_reasoning_aliases(payload: dict) -> dict:
    """
    Normalize reasoning fields for wider OpenAI-compatible client support.

    iFlow often uses `reasoning_content`; some clients render gray "thinking"
    blocks only when a `reasoning` field exists.
    """
    if not isinstance(payload, dict):
        return payload
    choices = payload.get("choices")
    if not isinstance(choices, list):
        return payload
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        msg = choice.get("message")
        if isinstance(msg, dict):
            rc = msg.get("reasoning_content")
            if rc is not None:
                if "reasoning" not in msg:
                    msg["reasoning"] = rc
                # Some upstreams only return reasoning_content; fill content for OpenAI compatibility.
                content = msg.get("content")
                if content is None or content == "":
                    msg["content"] = rc
        delta = choice.get("delta")
        if isinstance(delta, dict):
            rc = delta.get("reasoning_content")
            if rc is not None:
                if "reasoning" not in delta:
                    delta["reasoning"] = rc
                # Mirror to content for streaming clients that only read delta.content.
                content = delta.get("content")
                if content is None or content == "":
                    delta["content"] = rc
    return payload


class IFlowProxy:
    """iFlow API 代理"""

    def __init__(self, config: IFlowConfig, max_concurrency: int = 0):
        self.config = config
        self.base_url = config.base_url.rstrip("/")
        self._client: Optional[httpx.AsyncClient] = None
        self._session_id = uuid.uuid4().hex
        self._conversation_id = uuid.uuid4().hex
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

    def _get_chat_headers(self) -> dict:
        """
        获取 chat/completions 请求头（含 iFlow CLI 风格签名字段）。

        iFlow 某些模型（如 glm-5 / minimax-m2.5 / kimi-k2.5）需要额外头部：
        - session-id
        - conversation-id
        - x-iflow-timestamp (ms)
        - x-iflow-signature (hmac-sha256)
        """
        headers = self._get_headers()
        api_key = str(self.config.api_key or "")
        if not api_key:
            return headers

        timestamp_ms = str(int(time.time() * 1000))
        payload = f"{IFLOW_CLI_USER_AGENT}:{self._session_id}:{timestamp_ms}"
        signature = hmac.new(
            api_key.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        headers.update(
            {
                "session-id": self._session_id,
                "conversation-id": self._conversation_id,
                "x-iflow-timestamp": timestamp_ms,
                "x-iflow-signature": signature,
            }
        )
        return headers

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

            # Keep HTTP/1.1 by default for better upstream stability.
            # Some users observed intermittent stream resets on HTTP/2 paths.
            http2_enabled = False

            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(connect=10.0, read=1200.0, write=120.0, pool=30.0),
                follow_redirects=True,
                http2=http2_enabled,
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

        优先调用上游 iFlow 的 `/models` 获取“真实可用”列表；失败时回退到内置的已知集合。
        （可用性仍取决于账号权限与 iFlow 侧更新。）
        """
        client = await self._get_client()
        try:
            async with self._limit():
                resp = await client.get(
                    f"{self.base_url}/models",
                    headers=self._get_headers(),
                )
                resp.raise_for_status()
                data = resp.json()
            if isinstance(data, dict) and isinstance(data.get("data"), list):
                return data
        except Exception:
            pass

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
                    headers=self._get_chat_headers(),
                    json=request_body,
                )
                response.raise_for_status()
                result = response.json()
                _raise_iflow_payload_error(result)
                result = _add_reasoning_aliases(result)

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
                headers=self._get_chat_headers(),
                json=request_body,
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    out = line
                    if line.startswith("data:"):
                        raw = line[5:].strip()
                        if raw and raw != "[DONE]":
                            try:
                                obj = json.loads(raw)
                                obj = _add_reasoning_aliases(obj)
                                out = "data:" + json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
                            except Exception:
                                out = line
                    else:
                        # Upstream sometimes returns a raw JSON error payload instead of SSE.
                        # Detect and surface it as an exception so callers can retry/fallback.
                        trimmed = line.strip()
                        if trimmed.startswith("{") and trimmed.endswith("}"):
                            try:
                                payload = json.loads(trimmed)
                                _raise_iflow_payload_error(payload)
                                # If payload has explicit error fields, treat as upstream error too.
                                if isinstance(payload, dict) and (
                                    payload.get("error") is not None or payload.get("detail") is not None
                                ):
                                    raise IFlowUpstreamError(
                                        payload.get("status") or 500,
                                        str(payload.get("detail") or payload.get("error") or "Upstream error"),
                                        payload,
                                    )
                            except IFlowUpstreamError:
                                raise
                            except Exception:
                                # If parsing fails, fall through and emit raw line.
                                pass
                    yield (out + "\n").encode("utf-8")

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

        headers = self._get_chat_headers() if path.rstrip("/").endswith("/chat/completions") else self._get_headers()

        if method.upper() == "GET":
            response = await client.get(url, headers=self._get_headers())
        elif method.upper() == "POST":
            response = await client.post(url, headers=headers, json=body)
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
            headers=self._get_chat_headers() if url.rstrip("/").endswith("/chat/completions") else self._get_headers(),
            json=body,
        ) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                yield chunk
