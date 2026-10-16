from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

LOG = logging.getLogger(__name__)


def redact_url(url: str) -> str:
    try:
        value = urlsplit(url)
        return urlunsplit((value.scheme, value.netloc, value.path, "", ""))
    except (TypeError, ValueError):
        return "<redacted-url>"


class ResilientHttpClient:
    def __init__(self, attempts: int = 4) -> None:
        self.attempts = attempts
        self.client = httpx.Client(
            timeout=httpx.Timeout(60.0, connect=30.0),
            transport=httpx.HTTPTransport(retries=3),
            follow_redirects=True,
        )

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response | None:
        retryable = (
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
            httpx.ConnectError,
            httpx.RemoteProtocolError,
        )
        for attempt in range(1, self.attempts + 1):
            try:
                response = self.client.request(method, url, **kwargs)
                if response.status_code in {408, 425, 429, 500, 502, 503, 504}:
                    raise httpx.HTTPStatusError(
                        f"retryable HTTP {response.status_code}",
                        request=response.request,
                        response=response,
                    )
                return response
            except retryable + (httpx.HTTPStatusError,) as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if isinstance(exc, httpx.HTTPStatusError) and status not in {
                    408, 425, 429, 500, 502, 503, 504
                }:
                    raise
                LOG.warning(
                    "HTTP %s %s failed attempt %d/%d: %s",
                    method.upper(),
                    redact_url(url),
                    attempt,
                    self.attempts,
                    exc,
                )
                if attempt < self.attempts:
                    time.sleep(min(5 * attempt, 15))
        LOG.error("HTTP %s %s exhausted retries", method.upper(), redact_url(url))
        return None

    def close(self) -> None:
        self.client.close()
