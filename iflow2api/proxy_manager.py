"""Proxy manager that supports multi-account routing."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Optional

from fastapi import HTTPException, Request

from .config import IFlowConfig, load_iflow_config
from .proxy import IFlowProxy
from .resilience import is_retryable_exception
from .routing import (
    ApiKeyRoute,
    KeyRoutingConfig,
    get_routing_file_path_in_use,
    load_routing_config,
)


def _extract_bearer_token(request: Request) -> Optional[str]:
    auth = request.headers.get("Authorization", "")
    if not auth:
        return None
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return auth.strip()


@dataclass(frozen=True)
class ResolvedRoute:
    upstream_account_id: Optional[str]
    upstream_config: IFlowConfig


@dataclass
class _AccountState:
    consecutive_failures: int = 0
    circuit_open_until: float = 0.0  # unix timestamp
    last_error: str = ""

    def is_available(self) -> bool:
        return time.time() >= self.circuit_open_until


class ProxyManager:
    def __init__(self, routing: KeyRoutingConfig):
        self._routing = routing
        self._proxies: dict[str, IFlowProxy] = {}
        self._lock = asyncio.Lock()
        self._rr_index: dict[str, int] = {}
        self._account_state: dict[str, _AccountState] = {}
        self._routing_path = get_routing_file_path_in_use()
        self._routing_mtime: float = 0.0
        if self._routing_path and self._routing_path.exists():
            try:
                self._routing_mtime = self._routing_path.stat().st_mtime
            except Exception:
                self._routing_mtime = 0.0

    @property
    def routing(self) -> KeyRoutingConfig:
        return self._routing

    def has_upstream_accounts(self) -> bool:
        return bool(self._routing.accounts)

    def _fallback_iflow_config(self) -> IFlowConfig:
        return load_iflow_config()

    async def _maybe_reload_routing(self) -> None:
        """
        Auto-reload routing config when ~/.iflow2api/keys.json changes.

        This enables adding accounts from the GUI without restarting the server.
        """
        if not self._routing_path:
            return
        try:
            if not self._routing_path.exists():
                return
            mtime = self._routing_path.stat().st_mtime
        except Exception:
            return
        if mtime <= self._routing_mtime:
            return

        try:
            new_cfg = load_routing_config()
        except Exception:
            # Keep old config; still advance mtime to avoid tight loops.
            self._routing_mtime = mtime
            return

        # Swap config + clear caches.
        async with self._lock:
            old_proxies = list(self._proxies.values())
            self._proxies.clear()
            self._rr_index.clear()
            self._account_state.clear()
            self._routing = new_cfg
            self._routing_mtime = mtime

        for p in old_proxies:
            try:
                await p.close()
            except Exception:
                pass

    def _get_state(self, account_id: str) -> _AccountState:
        st = self._account_state.get(account_id)
        if st is None:
            st = _AccountState()
            self._account_state[account_id] = st
        return st

    def _record_success(self, account_id: Optional[str]) -> None:
        if not account_id:
            return
        st = self._get_state(account_id)
        st.consecutive_failures = 0
        st.circuit_open_until = 0.0
        st.last_error = ""

    def _record_failure(self, account_id: Optional[str], error: Exception) -> None:
        if not account_id:
            return
        st = self._get_state(account_id)
        st.consecutive_failures += 1
        st.last_error = f"{type(error).__name__}: {error}"
        if (
            self._routing.resilience.enabled
            and st.consecutive_failures >= self._routing.resilience.failure_threshold
        ):
            st.circuit_open_until = time.time() + float(self._routing.resilience.cool_down_seconds)

    async def get_any_proxy(self) -> IFlowProxy:
        """
        Get any available proxy instance.

        Used for endpoints that don't need per-request routing (e.g. /v1/models).
        """
        await self._maybe_reload_routing()
        if self._routing.accounts:
            acc_id = next(iter(self._routing.accounts.keys()))
            acc = self._routing.accounts[acc_id]
            async with self._lock:
                proxy = self._proxies.get(acc_id)
                if proxy is None:
                    proxy = IFlowProxy(
                        IFlowConfig(api_key=acc.api_key, base_url=acc.base_url),
                        max_concurrency=acc.max_concurrency,
                    )
                    self._proxies[acc_id] = proxy
                return proxy

        async with self._lock:
            proxy = self._proxies.get("__default__")
            if proxy is None:
                proxy = IFlowProxy(self._fallback_iflow_config())
                self._proxies["__default__"] = proxy
            return proxy

    def _resolve_route(self, request: Request) -> ResolvedRoute:
        token = _extract_bearer_token(request)

        if self._routing.auth.enabled:
            if not token:
                if self._routing.auth.required:
                    raise HTTPException(status_code=401, detail="Missing Authorization: Bearer <api-key>")
            else:
                route = self._routing.keys.get(token)
                if route is None:
                    if self._routing.auth.required:
                        raise HTTPException(status_code=401, detail="Invalid API key")
                else:
                    return self._resolve_upstream_from_route(route)

        # Optional auth: token may exist but not mapped; fall through to default.
        if self._routing.default is not None:
            return self._resolve_upstream_from_route(self._routing.default)

        # Backwards-compatible fallback: use iFlow CLI config.
        cfg = self._fallback_iflow_config()
        return ResolvedRoute(upstream_account_id=None, upstream_config=cfg)

    def _resolve_upstream_from_route(self, route: ApiKeyRoute) -> ResolvedRoute:
        if route.account:
            acc_id = route.account
            acc = self._routing.accounts[acc_id]
            return ResolvedRoute(
                upstream_account_id=acc_id,
                upstream_config=IFlowConfig(api_key=acc.api_key, base_url=acc.base_url),
            )

        # Pooling route
        account_ids = route.accounts or []
        if not account_ids:
            raise HTTPException(status_code=500, detail="Invalid routing config: empty accounts pool")

        # Prefer available accounts when resilience is enabled.
        candidates = [aid for aid in account_ids if self._routing.accounts.get(aid) and self._routing.accounts[aid].enabled]
        if not candidates:
            candidates = account_ids
        if self._routing.resilience.enabled:
            available = [aid for aid in candidates if self._get_state(aid).is_available()]
            if available:
                candidates = available

        if route.strategy == "round_robin":
            group_key = ",".join(account_ids)
            start = self._rr_index.get(group_key, 0)
            acc_id = candidates[start % len(candidates)]
            self._rr_index[group_key] = (start + 1) % len(candidates)
        else:
            # least_busy: prefer the account with lower in-flight requests.
            best_id: Optional[str] = None
            best_inflight: Optional[int] = None
            for acc_id_candidate in candidates:
                proxy = self._proxies.get(acc_id_candidate)
                inflight = proxy.in_flight if proxy else 0
                if best_inflight is None or inflight < best_inflight:
                    best_inflight = inflight
                    best_id = acc_id_candidate
            acc_id = best_id or candidates[0]

        acc = self._routing.accounts[acc_id]
        return ResolvedRoute(
            upstream_account_id=acc_id,
            upstream_config=IFlowConfig(api_key=acc.api_key, base_url=acc.base_url),
        )

    async def _get_or_create_account_proxy(self, account_id: str) -> IFlowProxy:
        acc = self._routing.accounts[account_id]
        async with self._lock:
            proxy = self._proxies.get(account_id)
            if proxy is None:
                proxy = IFlowProxy(
                    IFlowConfig(api_key=acc.api_key, base_url=acc.base_url),
                    max_concurrency=acc.max_concurrency,
                )
                self._proxies[account_id] = proxy
            return proxy

    async def _pick_account(self, candidates: list[str], strategy: str, exclude: set[str]) -> str:
        # Prefer healthy accounts.
        if self._routing.resilience.enabled:
            healthy = [aid for aid in candidates if self._get_state(aid).is_available() and aid not in exclude]
        else:
            healthy = []
        pool = healthy or [aid for aid in candidates if aid not in exclude] or candidates

        if strategy == "round_robin":
            key = ",".join(candidates)
            start = self._rr_index.get(key, 0)
            picked = pool[start % len(pool)]
            self._rr_index[key] = (start + 1) % len(pool)
            return picked

        # least_busy
        best_id: Optional[str] = None
        best_inflight: Optional[int] = None
        async with self._lock:
            for aid in pool:
                p = self._proxies.get(aid)
                inflight = p.in_flight if p else 0
                if best_inflight is None or inflight < best_inflight:
                    best_inflight = inflight
                    best_id = aid
        return best_id or pool[0]

    async def chat_completions(
        self,
        request: Request,
        body: dict,
        stream: bool,
    ) -> dict | AsyncIterator[bytes]:
        """
        Chat completions with best-effort failover.

        Non-streaming:
        - On retryable failures (timeouts/network/429/5xx), switches to another account up to `retry_attempts`.

        Streaming:
        - Only retries before the first byte is yielded (mid-stream failover is not supported).
        """
        await self._maybe_reload_routing()
        token = _extract_bearer_token(request)
        route: Optional[ApiKeyRoute] = None

        if self._routing.auth.enabled:
            if not token:
                if self._routing.auth.required:
                    raise HTTPException(status_code=401, detail="Missing Authorization: Bearer <api-key>")
            else:
                route = self._routing.keys.get(token)
                if route is None and self._routing.auth.required:
                    raise HTTPException(status_code=401, detail="Invalid API key")

        # Optional auth: token may exist but not mapped; fall back to default.
        if route is None and self._routing.default is not None:
            route = self._routing.default

        # No route or no accounts -> fallback original behavior.
        if route is None or not self._routing.accounts:
            proxy = await self.get_proxy_for_request(request)
            return await proxy.chat_completions(body, stream=stream)

        if route.account:
            candidates = [route.account]
        else:
            candidates = list(route.accounts or [])

        # Filter disabled accounts.
        candidates = [aid for aid in candidates if self._routing.accounts.get(aid) and self._routing.accounts[aid].enabled]

        if not candidates:
            proxy = await self.get_proxy_for_request(request)
            return await proxy.chat_completions(body, stream=stream)

        max_extra = 0
        if self._routing.resilience.enabled:
            max_extra = int(self._routing.resilience.retry_attempts)
        attempts = 1 + (max_extra if (self._routing.resilience.enabled and (not stream)) else 0)
        # For streaming, only retry before the first byte; cap to 1 extra attempt.
        if stream and self._routing.resilience.enabled:
            attempts = min(len(candidates), 1 + min(1, max_extra))
        backoff_ms = int(self._routing.resilience.retry_backoff_ms) if self._routing.resilience.enabled else 0
        tried: set[str] = set()
        last_exc: Optional[Exception] = None

        for i in range(attempts):
            account_id = await self._pick_account(candidates, route.strategy, tried)
            tried.add(account_id)
            proxy = await self._get_or_create_account_proxy(account_id)

            try:
                if not stream:
                    result = await proxy.chat_completions(body, stream=False)
                    self._record_success(account_id)
                    return result

                # stream: validate by pulling first chunk
                stream_iter = await proxy.chat_completions(body, stream=True)
                try:
                    first = await stream_iter.__anext__()
                except StopAsyncIteration:
                    self._record_success(account_id)
                    async def empty() -> AsyncIterator[bytes]:
                        if False:
                            yield b""
                    return empty()

                async def gen() -> AsyncIterator[bytes]:
                    yield first
                    try:
                        async for chunk in stream_iter:
                            yield chunk
                        self._record_success(account_id)
                    except Exception as e:
                        self._record_failure(account_id, e)
                        raise

                return gen()

            except Exception as e:
                self._record_failure(account_id, e)
                last_exc = e
                if not self._routing.resilience.enabled:
                    break
                if stream:
                    if not is_retryable_exception(e, self._routing.resilience.retry_status_codes):
                        break
                    if len(tried) >= len(candidates):
                        break
                    if backoff_ms and i < attempts - 1:
                        await asyncio.sleep(backoff_ms / 1000.0)
                    continue
                if not is_retryable_exception(e, self._routing.resilience.retry_status_codes):
                    break
                if len(tried) >= len(candidates):
                    break
                if backoff_ms and i < attempts - 1:
                    await asyncio.sleep(backoff_ms / 1000.0)

        if last_exc is not None:
            raise last_exc
        raise HTTPException(status_code=500, detail="Upstream error")

    def get_account_metrics(self) -> dict[str, Any]:
        """
        Return lightweight health metrics for upstream accounts.
        Secrets are never included.
        """
        if self._routing_path and self._routing_path.exists():
            try:
                mtime = self._routing_path.stat().st_mtime
                if mtime > self._routing_mtime:
                    self._routing = load_routing_config()
                    self._routing_mtime = mtime
            except Exception:
                pass
        out: dict[str, Any] = {}
        now = time.time()
        for account_id, acc in self._routing.accounts.items():
            st = self._account_state.get(account_id) or _AccountState()
            proxy = self._proxies.get(account_id)
            key_mask = ""
            try:
                key_mask = f"...{acc.api_key[-4:]}" if acc.api_key else ""
            except Exception:
                key_mask = ""
            out[account_id] = {
                "label": acc.label or account_id,
                "enabled": bool(acc.enabled),
                "api_key_mask": key_mask,
                "base_url": acc.base_url,
                "in_flight": proxy.in_flight if proxy else 0,
                "max_concurrency": acc.max_concurrency,
                "consecutive_failures": st.consecutive_failures,
                "circuit_open": (not st.is_available()),
                "circuit_open_for_seconds": max(0, int(st.circuit_open_until - now)),
                "last_error": st.last_error,
            }
        return out

    async def get_proxy_for_request(self, request: Request) -> IFlowProxy:
        await self._maybe_reload_routing()
        async with self._lock:
            resolved = self._resolve_route(request)

            # No account id means fallback (single default config). Use a singleton.
            if resolved.upstream_account_id is None:
                proxy = self._proxies.get("__default__")
                if proxy is None:
                    proxy = IFlowProxy(resolved.upstream_config)
                    self._proxies["__default__"] = proxy
                return proxy

            # Account-specific proxy: cache by account id.
            acc_id = resolved.upstream_account_id
            acc_cfg = self._routing.accounts.get(acc_id)
            max_concurrency = acc_cfg.max_concurrency if acc_cfg else 0

            proxy = self._proxies.get(acc_id)
            if proxy is None:
                proxy = IFlowProxy(resolved.upstream_config, max_concurrency=max_concurrency)
                self._proxies[acc_id] = proxy
            return proxy

    async def close(self) -> None:
        async with self._lock:
            proxies = list(self._proxies.values())
            self._proxies.clear()
        for proxy in proxies:
            try:
                await proxy.close()
            except Exception:
                pass
