"""Read/write helpers for ~/.iflow2api/keys.json (multi-account routing)."""

from __future__ import annotations

import json
import secrets
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .routing import (
    ApiKeyRoute,
    IFlowUpstreamAccount,
    KeyRoutingConfig,
    get_keys_config_path,
    get_routing_file_path_in_use,
    load_routing_config,
)


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def load_keys_config() -> KeyRoutingConfig:
    # load_routing_config already supports env overrides, but GUI mainly uses file.
    return load_routing_config()


def save_keys_config(cfg: KeyRoutingConfig, path: Optional[Path] = None) -> Path:
    if path is None:
        # Respect env overrides (IFLOW2API_KEYS_PATH). If config is provided via
        # IFLOW2API_KEYS_JSON, there's no file to write to; callers should pass
        # an explicit path or handle None upstream.
        path = get_routing_file_path_in_use() or get_keys_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Use JSON mode to safely serialize datetime fields (oauth_expires_at, etc.).
    data = cfg.model_dump(mode="json")
    payload = json.dumps(data, ensure_ascii=False, indent=2)

    # Atomic write to reduce the chance of partial reads by the server.
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(path.parent),
            delete=False,
            prefix=path.name + ".tmp.",
        ) as f:
            f.write(payload)
            tmp_path = Path(f.name)
        tmp_path.replace(path)
    finally:
        try:
            if tmp_path and tmp_path.exists():
                tmp_path.unlink(missing_ok=True)  # type: ignore[arg-type]
        except Exception:
            pass
    return path


def generate_client_key(prefix: str = "sk-iflow") -> str:
    # Human-friendly + urlsafe; not an OpenAI key.
    return f"{prefix}-{secrets.token_urlsafe(18)}"


def _next_account_id(existing: set[str]) -> str:
    # Try acc1, acc2, ... first for readability; fall back to random id if needed.
    for i in range(1, 1000):
        candidate = f"acc{i}"
        if candidate not in existing:
            return candidate
    return f"acc-{secrets.token_hex(4)}"


@dataclass(frozen=True)
class AddAccountResult:
    account_id: str
    label: str


def add_upstream_account(
    cfg: KeyRoutingConfig,
    *,
    api_key: str,
    base_url: str = "https://apis.iflow.cn/v1",
    label: Optional[str] = None,
    max_concurrency: int = 4,
    auth_type: Optional[str] = None,
    oauth_access_token: Optional[str] = None,
    oauth_refresh_token: Optional[str] = None,
    oauth_expires_at: Optional[datetime] = None,
) -> AddAccountResult:
    account_id = _next_account_id(set(cfg.accounts.keys()))
    label_final = label or account_id
    cfg.accounts[account_id] = IFlowUpstreamAccount(
        api_key=api_key,
        base_url=base_url,
        max_concurrency=max(0, int(max_concurrency)),
        enabled=True,
        label=label_final,
        created_at=_now_iso(),
        auth_type=auth_type,
        oauth_access_token=oauth_access_token,
        oauth_refresh_token=oauth_refresh_token,
        oauth_expires_at=oauth_expires_at,
    )
    return AddAccountResult(account_id=account_id, label=label_final)


def ensure_opencode_route(
    cfg: KeyRoutingConfig,
    *,
    token: str,
    strategy: str = "least_busy",
    include_disabled: bool = False,
) -> None:
    if not cfg.accounts:
        # No upstream accounts yet; keep routing empty until first login.
        return

    # Force auth (self-use local gateway): require callers to present the token.
    cfg.auth.enabled = True
    cfg.auth.required = True

    account_ids = list(cfg.accounts.keys())
    if not include_disabled:
        account_ids = [aid for aid in account_ids if cfg.accounts[aid].enabled]
    if not account_ids:
        account_ids = list(cfg.accounts.keys())

    cfg.keys[token] = ApiKeyRoute(accounts=account_ids, strategy=strategy)
    cfg.default = ApiKeyRoute(accounts=account_ids, strategy=strategy)


def get_or_create_first_token(cfg: KeyRoutingConfig) -> str:
    if cfg.keys:
        # Prefer stable ordering for UX.
        for k in cfg.keys.keys():
            return k
    # Do not mutate cfg here: routes require non-empty accounts and the config
    # loader validates it. The caller should persist the token only after at
    # least one upstream account exists.
    return generate_client_key()
