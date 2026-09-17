"""Bounded partner reads. Never buffer arbitrary error bodies or decompress payloads."""

import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx


def bounded_request(
    client: httpx.Client, method: str, url: str, *, limit=1024 * 1024, seconds=5, **kwargs
) -> httpx.Response:
    started = time.monotonic()
    headers = {**kwargs.pop("headers", {}), "Accept-Encoding": "identity"}
    with client.stream(method, url, headers=headers, **kwargs) as response:
        content = bytearray()
        if 200 <= response.status_code < 300:
            if response.headers.get("Content-Encoding", "identity") != "identity":
                raise httpx.ReadError("unsupported-content-encoding")
            for chunk in response.iter_raw():
                if len(content) + len(chunk) > limit or time.monotonic() - started > seconds:
                    raise httpx.ReadError("partner-response-budget-exceeded")
                content.extend(chunk)
        return httpx.Response(
            response.status_code,
            headers=response.headers,
            content=bytes(content),
            request=response.request,
        )


def retry_after(value: str | None) -> float:
    try:
        delay = float(value or "0")
    except ValueError:
        try:
            delay = (parsedate_to_datetime(value or "") - datetime.now(UTC)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            delay = 5
    return max(0, min(delay, 900))
