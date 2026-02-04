"""Resilience helpers (retryability, status extraction)."""

from __future__ import annotations

from typing import Optional

import httpx


def get_http_status_code(exc: Exception) -> Optional[int]:
    if isinstance(exc, httpx.HTTPStatusError):
        try:
            return int(exc.response.status_code)
        except Exception:
            return None
    # Some code paths may attach response on custom exceptions
    resp = getattr(exc, "response", None)
    if resp is not None:
        try:
            return int(getattr(resp, "status_code", None))
        except Exception:
            return None
    return None


def is_retryable_exception(exc: Exception, retry_status_codes: list[int]) -> bool:
    if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError, httpx.NetworkError)):
        return True
    status = get_http_status_code(exc)
    if status is None:
        return False
    return status in set(int(x) for x in retry_status_codes)

