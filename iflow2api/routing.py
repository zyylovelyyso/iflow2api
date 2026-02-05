"""Request routing for multi-account support.

This module allows mapping incoming API keys (client-side) to upstream iFlow
accounts (server-side). It is optional and fully backwards compatible: if no
routing config exists, the server falls back to ~/.iflow/settings.json.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal, Optional

from datetime import datetime

from pydantic import BaseModel, Field, ValidationError

from .settings import get_config_dir


RoutingStrategy = Literal["round_robin", "least_busy"]


class IFlowUpstreamAccount(BaseModel):
    """Upstream iFlow account credentials."""

    api_key: str = Field(..., description="Upstream iFlow apiKey")
    base_url: str = Field(default="https://apis.iflow.cn/v1", description="Upstream base URL")
    max_concurrency: int = Field(default=0, ge=0, description="0 means unlimited")
    enabled: bool = Field(default=True, description="Whether this upstream account can be used")
    label: Optional[str] = Field(default=None, description="Human-readable label (e.g. username/phone)")
    created_at: Optional[str] = Field(default=None, description="ISO timestamp when added")
    # Optional OAuth fields (used for auto-refresh); safe to leave empty for api-key accounts.
    auth_type: Optional[str] = Field(default=None, description="Auth type: oauth-iflow / api-key")
    oauth_access_token: Optional[str] = Field(default=None, description="OAuth access token")
    oauth_refresh_token: Optional[str] = Field(default=None, description="OAuth refresh token")
    oauth_expires_at: Optional[datetime] = Field(default=None, description="OAuth expiry time")
    last_refresh_at: Optional[datetime] = Field(default=None, description="Last refresh time")


class ApiKeyRoute(BaseModel):
    """Route definition for a client API key."""

    account: Optional[str] = Field(default=None, description="Single upstream account id")
    accounts: Optional[list[str]] = Field(default=None, description="Upstream account ids for pooling")
    strategy: RoutingStrategy = Field(default="least_busy", description="Pooling strategy")

    def normalize(self) -> "ApiKeyRoute":
        if self.account and self.accounts:
            raise ValueError("Route must specify either 'account' or 'accounts', not both")
        if not self.account and not self.accounts:
            raise ValueError("Route must specify 'account' or 'accounts'")
        return self


class KeyRoutingAuth(BaseModel):
    enabled: bool = False
    required: bool = False


class ResilienceConfig(BaseModel):
    """
    Lightweight stability options for multi-account usage.

    Notes:
    - Failover is best-effort and mainly applies to request setup (HTTP connect,
      timeout, status codes). Mid-stream failover is not supported.
    """

    enabled: bool = True
    failure_threshold: int = Field(default=3, ge=1, description="Open circuit after N consecutive failures")
    cool_down_seconds: int = Field(default=30, ge=1, description="How long to keep a failing account disabled")
    retry_attempts: int = Field(default=1, ge=0, description="Extra attempts on other accounts (non-streaming)")
    retry_backoff_ms: int = Field(default=200, ge=0, description="Backoff between failover retries")
    retry_status_codes: list[int] = Field(
        default_factory=lambda: [429, 500, 502, 503, 504],
        description="HTTP status codes that trigger failover",
    )


class KeyRoutingConfig(BaseModel):
    """Routing config loaded from ~/.iflow2api/keys.json (or env)."""

    auth: KeyRoutingAuth = Field(default_factory=KeyRoutingAuth)
    resilience: ResilienceConfig = Field(default_factory=ResilienceConfig)
    accounts: dict[str, IFlowUpstreamAccount] = Field(default_factory=dict)
    keys: dict[str, ApiKeyRoute] = Field(default_factory=dict)
    default: Optional[ApiKeyRoute] = None

    def validate_routes(self) -> None:
        for key, route in list(self.keys.items()):
            self.keys[key] = route.normalize()
        if self.default is not None:
            self.default = self.default.normalize()

        missing: set[str] = set()
        for route in [*self.keys.values(), *( [self.default] if self.default else [] )]:
            if route is None:
                continue
            account_ids = [route.account] if route.account else (route.accounts or [])
            for account_id in account_ids:
                if account_id and account_id not in self.accounts:
                    missing.add(account_id)
        if missing:
            raise ValueError(f"Routing config references missing accounts: {sorted(missing)}")


def get_keys_config_path() -> Path:
    return get_config_dir() / "keys.json"


def get_routing_file_path_in_use() -> Optional[Path]:
    """
    Return the file path used for routing config, or None if config is provided via env JSON.
    """
    if os.getenv("IFLOW2API_KEYS_JSON"):
        return None
    path = os.getenv("IFLOW2API_KEYS_PATH")
    return Path(path) if path else get_keys_config_path()


def _load_json_from_env() -> Optional[dict]:
    raw = os.getenv("IFLOW2API_KEYS_JSON")
    if not raw:
        return None
    return json.loads(raw)


def load_routing_config() -> KeyRoutingConfig:
    """
    Load routing config.

    Precedence:
    1) IFLOW2API_KEYS_JSON (inline json)
    2) IFLOW2API_KEYS_PATH (json file path)
    3) ~/.iflow2api/keys.json
    """
    data: Optional[dict] = None
    source: str = ""

    try:
        data = _load_json_from_env()
        if data is not None:
            source = "env:IFLOW2API_KEYS_JSON"
    except Exception as e:
        raise ValueError(f"Invalid IFLOW2API_KEYS_JSON: {e}")

    if data is None:
        path = os.getenv("IFLOW2API_KEYS_PATH")
        config_path = Path(path) if path else get_keys_config_path()
        source = str(config_path)
        if config_path.exists():
            try:
                data = json.loads(config_path.read_text(encoding="utf-8"))
            except Exception as e:
                raise ValueError(f"Failed to read routing config {config_path}: {e}")

    if data is None:
        return KeyRoutingConfig()

    try:
        cfg = KeyRoutingConfig.model_validate(data)
        cfg.validate_routes()
        cfg._source = source  # type: ignore[attr-defined]
        return cfg
    except ValidationError as e:
        raise ValueError(f"Invalid routing config ({source}): {e}")
