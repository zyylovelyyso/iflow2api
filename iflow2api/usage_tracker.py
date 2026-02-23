"""Token usage tracking for local gateway observability."""

from __future__ import annotations

import copy
import json
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_key() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _normalize_int(value: Any) -> int:
    try:
        v = int(value)
        return v if v >= 0 else 0
    except Exception:
        return 0


def _bucket() -> dict[str, int]:
    return {
        "requests": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }


def _empty_stats() -> dict[str, Any]:
    return {
        "updated_at": _now_iso(),
        "totals": _bucket(),
        "days": {},
        "models": {},
    }


def get_usage_stats_path() -> Path:
    return Path.home() / ".iflow2api" / "usage.json"


class TokenUsageTracker:
    def __init__(self, path: Optional[Path] = None):
        self._path = path or get_usage_stats_path()
        self._lock = threading.Lock()
        self._stats = self._load()

    def _load(self) -> dict[str, Any]:
        if not self._path.exists():
            return _empty_stats()
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            return _empty_stats()
        if not isinstance(data, dict):
            return _empty_stats()
        stats = _empty_stats()
        stats["updated_at"] = str(data.get("updated_at") or _now_iso())
        for key in ("totals",):
            bucket = data.get(key)
            if isinstance(bucket, dict):
                stats[key]["requests"] = _normalize_int(bucket.get("requests"))
                stats[key]["prompt_tokens"] = _normalize_int(bucket.get("prompt_tokens"))
                stats[key]["completion_tokens"] = _normalize_int(bucket.get("completion_tokens"))
                stats[key]["total_tokens"] = _normalize_int(bucket.get("total_tokens"))

        days = data.get("days")
        if isinstance(days, dict):
            for day_key, bucket in days.items():
                if not isinstance(day_key, str) or not isinstance(bucket, dict):
                    continue
                b = _bucket()
                b["requests"] = _normalize_int(bucket.get("requests"))
                b["prompt_tokens"] = _normalize_int(bucket.get("prompt_tokens"))
                b["completion_tokens"] = _normalize_int(bucket.get("completion_tokens"))
                b["total_tokens"] = _normalize_int(bucket.get("total_tokens"))
                stats["days"][day_key] = b

        models = data.get("models")
        if isinstance(models, dict):
            for model_id, bucket in models.items():
                if not isinstance(model_id, str) or not isinstance(bucket, dict):
                    continue
                b = _bucket()
                b["requests"] = _normalize_int(bucket.get("requests"))
                b["prompt_tokens"] = _normalize_int(bucket.get("prompt_tokens"))
                b["completion_tokens"] = _normalize_int(bucket.get("completion_tokens"))
                b["total_tokens"] = _normalize_int(bucket.get("total_tokens"))
                stats["models"][model_id] = b
        return stats

    def _persist(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self._stats, ensure_ascii=False, indent=2)
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=str(self._path.parent),
                delete=False,
                prefix=self._path.name + ".tmp.",
            ) as f:
                f.write(payload)
                tmp_path = Path(f.name)
            tmp_path.replace(self._path)
        finally:
            if tmp_path and tmp_path.exists():
                try:
                    tmp_path.unlink(missing_ok=True)  # type: ignore[arg-type]
                except Exception:
                    pass

    def _apply_usage(self, bucket: dict[str, int], usage: Optional[dict[str, Any]]) -> None:
        bucket["requests"] += 1
        if not isinstance(usage, dict):
            return
        prompt = _normalize_int(usage.get("prompt_tokens"))
        completion = _normalize_int(usage.get("completion_tokens"))
        total = _normalize_int(usage.get("total_tokens"))
        if total <= 0:
            total = prompt + completion
        bucket["prompt_tokens"] += prompt
        bucket["completion_tokens"] += completion
        bucket["total_tokens"] += total

    def record(self, *, model: Any, usage: Optional[dict[str, Any]]) -> None:
        model_key = str(model).strip() if isinstance(model, str) else ""
        if not model_key:
            model_key = "unknown"
        day_key = _today_key()
        with self._lock:
            totals = self._stats.setdefault("totals", _bucket())
            self._apply_usage(totals, usage)

            days = self._stats.setdefault("days", {})
            day_bucket = days.get(day_key)
            if not isinstance(day_bucket, dict):
                day_bucket = _bucket()
                days[day_key] = day_bucket
            self._apply_usage(day_bucket, usage)

            models = self._stats.setdefault("models", {})
            model_bucket = models.get(model_key)
            if not isinstance(model_bucket, dict):
                model_bucket = _bucket()
                models[model_key] = model_bucket
            self._apply_usage(model_bucket, usage)

            self._stats["updated_at"] = _now_iso()
            self._persist()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._stats)

    def reset(self) -> dict[str, Any]:
        with self._lock:
            self._stats = _empty_stats()
            self._persist()
            return copy.deepcopy(self._stats)


_tracker: Optional[TokenUsageTracker] = None
_tracker_lock = threading.Lock()


def get_usage_tracker() -> TokenUsageTracker:
    global _tracker
    if _tracker is not None:
        return _tracker
    with _tracker_lock:
        if _tracker is None:
            _tracker = TokenUsageTracker()
        return _tracker
