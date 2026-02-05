"""Auto-refresh OAuth tokens stored in ~/.iflow2api/keys.json (multi-account).

Why this exists:
- iFlow's upstream `apiKey` can expire, requiring a re-login.
- When accounts are added via OAuth, we can store refresh credentials per account.
- This refresher keeps those credentials fresh so the gateway can run long-term.

Security notes:
- Never prints or returns raw tokens.
- Writes back only to the routing file (if provided via env JSON, refresh is skipped).
"""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timezone
from typing import Callable, Optional

from .keys_store import save_keys_config
from .oauth import IFlowOAuth
from .routing import get_routing_file_path_in_use, load_routing_config


class RoutingOAuthRefresher:
    def __init__(
        self,
        *,
        check_interval_seconds: int = 900,
        refresh_buffer_seconds: int = 300,
        log: Optional[Callable[[str], None]] = None,
    ):
        self.check_interval_seconds = int(check_interval_seconds)
        self.refresh_buffer_seconds = int(refresh_buffer_seconds)
        self._log = log
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5.0)
            self._thread = None

    def is_running(self) -> bool:
        return self._running

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.refresh_once()
            except Exception:
                pass
            self._stop_event.wait(max(10, self.check_interval_seconds))

    def refresh_once(self) -> None:
        path = get_routing_file_path_in_use()
        if path is None:
            # Config provided via env JSON; cannot persist refreshed credentials.
            return
        if not path.exists():
            return

        cfg = load_routing_config()
        if not cfg.accounts:
            return

        async def _refresh_async() -> bool:
            oauth = IFlowOAuth()
            changed = False

            for account_id, acc in cfg.accounts.items():
                if not acc.oauth_refresh_token:
                    continue

                needs = False
                if acc.oauth_expires_at is None:
                    needs = True
                else:
                    try:
                        needs = oauth.is_token_expired(
                            acc.oauth_expires_at, self.refresh_buffer_seconds
                        )
                    except Exception:
                        needs = True

                if not needs:
                    continue

                label = acc.label or account_id
                try:
                    token_data = await oauth.refresh_token(acc.oauth_refresh_token)
                    access_token = token_data.get("access_token") or ""
                    user_info = await oauth.get_user_info(access_token)
                    api_key = user_info.get("apiKey") or user_info.get("searchApiKey")
                    if not api_key:
                        raise ValueError("missing apiKey from user info")

                    acc.api_key = api_key
                    acc.auth_type = acc.auth_type or "oauth-iflow"
                    acc.oauth_access_token = access_token
                    if token_data.get("refresh_token"):
                        acc.oauth_refresh_token = token_data["refresh_token"]
                    if token_data.get("expires_at"):
                        acc.oauth_expires_at = token_data["expires_at"]
                    acc.last_refresh_at = datetime.now(timezone.utc)
                    changed = True

                    if self._log:
                        self._log(f"[refresh] {label}: ok")
                except Exception as ex:
                    if self._log:
                        self._log(f"[refresh] {label}: failed ({type(ex).__name__})")

            await oauth.close()
            return changed

        changed = asyncio.run(_refresh_async())
        if changed:
            save_keys_config(cfg, path)


_global_refresher: Optional[RoutingOAuthRefresher] = None


def start_global_routing_refresher(log: Optional[Callable[[str], None]] = None) -> None:
    global _global_refresher
    if _global_refresher is None:
        _global_refresher = RoutingOAuthRefresher(log=log)
    _global_refresher.start()


def stop_global_routing_refresher() -> None:
    global _global_refresher
    if _global_refresher:
        _global_refresher.stop()
        _global_refresher = None

