"""OpenCode config integration (local-only convenience).

This edits the user's OpenCode config to add a local OpenAI-compatible provider.
It is intentionally minimal: no dependency on OpenCode internals beyond config.json schema.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .model_catalog import get_known_models, to_opencode_models


def _safe_read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _safe_write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def discover_config_paths(explicit: str = "") -> list[Path]:
    paths: list[Path] = []
    if explicit:
        p = Path(explicit)
        if p.exists():
            return [p]

    # Common locations (Windows)
    candidates = [
        Path.home() / ".config" / "opencode" / "opencode.json",
        Path.home() / "AppData" / "Roaming" / "opencode" / "opencode.json",
        Path("C:/opencode-xdg/config/opencode/opencode.json"),
    ]
    for p in candidates:
        if p.exists():
            paths.append(p)
    # De-dup
    dedup: list[Path] = []
    seen: set[str] = set()
    for p in paths:
        key = str(p.resolve()).lower()
        if key not in seen:
            seen.add(key)
            dedup.append(p)
    return dedup


@dataclass(frozen=True)
class UpdateResult:
    path: Path
    backup_path: Optional[Path]


def ensure_iflow_provider(
    *,
    config_path: Path,
    provider_name: str,
    base_url: str,
    api_key: str,
    set_default_model: bool = False,
    default_model: str = "qwen3-coder-plus",
    set_small_model: bool = False,
    small_model: str = "iFlow-ROME-30BA3B",
    create_backup: bool = True,
) -> UpdateResult:
    cfg = _safe_read_json(config_path)

    provider = cfg.get("provider")
    if not isinstance(provider, dict):
        provider = {}
        cfg["provider"] = provider

    provider[provider_name] = {
        "api": "openai",
        "models": to_opencode_models(get_known_models()),
        "options": {
            "baseURL": base_url,
            "apiKey": api_key,
        },
    }

    if set_default_model:
        cfg["model"] = f"{provider_name}/{default_model}"
    if set_small_model:
        cfg["small_model"] = f"{provider_name}/{small_model}"

    backup_path: Optional[Path] = None
    if create_backup:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = config_path.with_suffix(f".bak.{ts}.json")
        try:
            shutil.copyfile(config_path, backup_path)
        except Exception:
            backup_path = None

    _safe_write_json(config_path, cfg)
    return UpdateResult(path=config_path, backup_path=backup_path)

