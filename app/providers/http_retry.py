
"""HTTP call with timeout and bounded retry.

Retries:
- Timeouts and connection errors
- HTTP 429 and 5xx

Does not retry other 4xx errors.
Provider error messages are extracted for debugging, with API-key
redaction and length limits. Request payloads are never logged.
"""
from __future__ import annotations

import logging
import random
import time

import httpx

from app.domain.errors import UpstreamUnavailableError
from app.observability import log

logger = logging.getLogger("provider.http")

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def _safe_provider_message(resp: httpx.Response, headers: dict) -> str:
    """Extract a short provider error message without exposing API keys."""
    try:
        error_data = resp.json()
        error_info = error_data.get("error", {})
        message = str(error_info.get("message", "No provider message"))
    except (ValueError, AttributeError, TypeError):
        message = "Provider returned a non-JSON error response"

    # Redact API keys if the provider happens to echo one.
    for value in headers.values():
        if value and "key" in str(value).lower():
            message = message.replace(str(value), "[REDACTED]")

    return message[:300]


def post_json(
    client: httpx.Client,
    url: str,
    *,
    json: dict,
    headers: dict,
    timeout_s: float,
    max_retries: int,
    op: str,
    sleep=time.sleep,
) -> dict:
    attempt = 0

    while True:
        attempt += 1

        try:
            resp = client.post(
                url,
                json=json,
                headers=headers,
                timeout=timeout_s,
            )

        except (httpx.TimeoutException, httpx.TransportError) as e:
            reason = type(e).__name__
            retry_after = None

        else:
            if resp.status_code < 400:
                return resp.json()

            if resp.status_code not in RETRYABLE_STATUS:
                provider_message = _safe_provider_message(resp, headers)

                log(
                    logger,
                    "provider.error",
                    logging.ERROR,
                    op=op,
                    status=resp.status_code,
                    provider_message=provider_message,
                )

                raise UpstreamUnavailableError(
                    f"{op} failed with HTTP {resp.status_code}: "
                    f"{provider_message}",
                    {
                        "status": resp.status_code,
                        "retryable": False,
                    },
                )

            reason = f"HTTP {resp.status_code}"

            ra = resp.headers.get("retry-after")
            retry_after = (
                float(ra)
                if ra and ra.replace(".", "", 1).isdigit()
                else None
            )

        if attempt > max_retries:
            log(
                logger,
                "provider.gave_up",
                logging.ERROR,
                op=op,
                attempts=attempt,
                reason=reason,
            )

            raise UpstreamUnavailableError(
                f"{op} unavailable after {attempt} attempts ({reason})",
                {"retryable": True},
            )

        delay = (
            retry_after
            if retry_after is not None
            else min(8.0, 0.5 * 2 ** (attempt - 1))
            * (0.5 + random.random())
        )

        log(
            logger,
            "provider.retry",
            logging.WARNING,
            op=op,
            attempt=attempt,
            reason=reason,
            sleep_s=round(delay, 2),
        )

        sleep(delay)
