"""Bounded DeepSeek calls; no tools, redirects, payload logging or raw errors."""

from dataclasses import dataclass, field
import hashlib
import json
import threading
import time

import httpx

from ctn.config import LLMConfig
from .models import LlmValidationError


@dataclass(frozen=True)
class LlmCallResult:
    reason: str
    attempt_count: int = 0
    request_id: str | None = None
    body: bytes | None = field(default=None, repr=False)

    def to_dict(self):
        return {"reason": self.reason, "attempt_count": self.attempt_count, "request_id": self.request_id}


class DeepSeekClient:
    def __init__(self, config: LLMConfig, *, transport=None):
        if type(config) is not LLMConfig:
            raise LlmValidationError("client: expected LLM configuration")
        self.config = config
        self._slots = threading.BoundedSemaphore(config.max_concurrency)
        self._http = httpx.Client(transport=transport, trust_env=False, follow_redirects=False,
                                  timeout=config.request_timeout_s)

    def close(self):
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def call(self, messages):
        cfg = self.config
        try:
            body = json.dumps({"model": cfg.model, "messages": messages, "stream": False,
                               "thinking": {"type": "disabled"}, "max_tokens": cfg.max_tokens,
                               "temperature": cfg.temperature},
                              ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
        except (TypeError, ValueError, UnicodeError):
            return LlmCallResult("invalid_request")
        request_id = hashlib.sha256(body).hexdigest()
        def result(reason, attempts=0, data=None):
            return LlmCallResult(reason, attempts, request_id, data)
        if len(body) > cfg.max_request_bytes:
            return result("request_budget_exceeded")
        if not cfg.api_key:
            return result("credentials_missing")
        started = time.monotonic()
        if not self._slots.acquire(timeout=cfg.total_wait_budget_s):
            return result("queue_budget_exceeded")
        wait_used = time.monotonic() - started
        attempts = 0
        try:
            for index in range(cfg.max_retries + 1):
                attempts += 1
                retry_after = None
                try:
                    # Socket timeouts apply to each network operation, not the whole request.
                    with self._http.stream("POST", cfg.base_url.rstrip("/") + "/chat/completions",
                                           content=body, headers={"Authorization": "Bearer " + cfg.api_key,
                                                                  "Content-Type": "application/json"}) as response:
                        status = response.status_code
                        if status == 200:
                            chunks, size = [], 0
                            for chunk in response.iter_bytes():
                                size += len(chunk)
                                if size > cfg.max_response_bytes:
                                    return result("response_budget_exceeded", attempts)
                                chunks.append(chunk)
                            return result("ok", attempts, b"".join(chunks))
                        if status not in {408, 429, 500, 502, 503, 504}:
                            return result("authentication_failed" if status in {401, 403} else "request_rejected", attempts)
                        reason = "rate_limited" if status == 429 else "service_unavailable"
                        try:
                            retry_after = float(response.headers.get("Retry-After", ""))
                        except ValueError:
                            pass
                except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError):
                    reason = "network_error"
                except httpx.HTTPError:
                    return result("transport_failed", attempts)
                if index == cfg.max_retries:
                    return result(reason, attempts)
                delay = min(cfg.retry_max_s, cfg.retry_initial_s * (2 ** min(index, 30)))
                if retry_after is not None and 0 <= retry_after < float("inf"):
                    delay = max(delay, retry_after)
                if delay > cfg.total_wait_budget_s - wait_used:
                    return result("wait_budget_exceeded", attempts)
                before = time.monotonic()
                time.sleep(delay)
                wait_used += time.monotonic() - before
            return result("transport_failed", attempts)
        finally:
            self._slots.release()
