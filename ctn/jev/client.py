"""Bounded, synchronous JEV transport. No classification or platform actions.

Call evaluate from worker threads to share this client's concurrency limit.
Queue waits, shared cooldown and retries use a local wait budget. Socket timeouts
are per network operation, not a hard wall-clock cancellation guarantee (DNS and
slow headers can overrun it). Late responses are reported as budget failures.
Retries after ambiguous network failures can incur another provider charge;
attempt counts are recorded and no idempotency guarantee is assumed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
import http.client
import json
import math
import random
import socket
import ssl
import threading
import time
from typing import Mapping, Protocol
from urllib.parse import urlsplit
from uuid import uuid4

from ..config import JevConfig
from .packing import PackingResult


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)
    body: bytes = field(default=b"", repr=False)


class TransportError(Exception):
    """Fixed error codes only; never retain a third-party exception/body."""

    def __init__(self, code: str):
        allowed = {"timeout", "network_error", "tls_error", "response_too_large"}
        self.code = code if code in allowed else "transport_error"
        super().__init__(self.code)


class Transport(Protocol):
    def send(self, *, url: str, api_key: str, body: bytes,
             timeout_s: float, max_response_bytes: int) -> HttpResponse: ...


class HttpTransport:
    """Direct HTTPS, normal certificate verification, no automatic redirects.

    No provider error body is read or exposed. Response size is bounded even
    without Content-Length. Every call uses and closes its own connection.
    """

    def send(self, *, url: str, api_key: str, body: bytes,
             timeout_s: float, max_response_bytes: int) -> HttpResponse:
        endpoint = urlsplit(url)
        connection = http.client.HTTPSConnection(endpoint.hostname, endpoint.port, timeout=timeout_s)
        response = None
        try:
            connection.request("POST", endpoint.path or "/", body=body, headers={
                "Authorization": "Bearer " + api_key,
                "Content-Type": "application/json",
                "Accept": "application/json", "Accept-Encoding": "identity",
            })
            response = connection.getresponse()
            headers = {"retry-after": response.getheader("Retry-After", "")}
            if not 200 <= response.status < 300:
                return HttpResponse(response.status, headers)
            declared_size = response.getheader("Content-Length")
            if declared_size and declared_size.isdecimal() and int(declared_size) > max_response_bytes:
                raise TransportError("response_too_large")
            chunks = bytearray()
            read_started = time.monotonic()
            while True:
                remaining = timeout_s - (time.monotonic() - read_started)
                if remaining <= 0:
                    raise TransportError("timeout")
                # read1 makes one underlying read, so drip-fed bodies cannot
                # reset the loop budget indefinitely.
                chunk = response.read1(min(65_536, max_response_bytes + 1 - len(chunks)))
                if not chunk:
                    break
                chunks.extend(chunk)
                if len(chunks) > max_response_bytes:
                    raise TransportError("response_too_large")
            if declared_size and declared_size.isdecimal() and len(chunks) != int(declared_size):
                raise TransportError("network_error")
            return HttpResponse(response.status, headers, bytes(chunks))
        except (socket.timeout, TimeoutError):
            raise TransportError("timeout") from None
        except ssl.SSLError:
            raise TransportError("tls_error") from None
        except (OSError, http.client.HTTPException):
            raise TransportError("network_error") from None
        finally:
            try:
                if response is not None:
                    response.close()
            finally:
                connection.close()


@dataclass(frozen=True)
class CallAttempt:
    number: int
    http_status: int | None
    error_code: str | None
    elapsed_s: float

    def to_dict(self) -> dict:
        return {"number": self.number, "http_status": self.http_status,
                "error_code": self.error_code, "elapsed_s": self.elapsed_s}


@dataclass(frozen=True)
class JevCallResult:
    """Transport success is NOT a valid assessment. T06 validates answers.

    packed_request retains the original question/target binding for T06.
    Default serialization contains no content, headers, key or raw response.
    """

    status: str
    call_id: str
    request_id: str | None
    error_code: str | None
    retryable: bool
    attempts: tuple[CallAttempt, ...]
    elapsed_s: float
    retry_after_s: float | None = None
    packed_request: PackingResult | None = field(default=None, repr=False)
    _response_bytes: bytes | None = field(default=None, repr=False)

    def to_dict(self) -> dict:
        return {"schema_version": "jev-call.v1", "status": self.status,
                "call_id": self.call_id, "request_id": self.request_id,
                "error_code": self.error_code, "retryable": self.retryable,
                "attempt_count": len(self.attempts),
                "attempts": [item.to_dict() for item in self.attempts],
                "elapsed_s": self.elapsed_s, "retry_after_s": self.retry_after_s,
                "response_bytes": len(self._response_bytes) if self._response_bytes is not None else None}

    def provider_response(self) -> dict:
        """Explicit, detached access for the parser; never log this by default."""
        if self.status != "success" or self._response_bytes is None:
            raise ValueError("JEV call has no successful provider response")
        return _decode_object(self._response_bytes)


def _decode_object(body: bytes) -> dict:
    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result
    def reject_constant(_):
        raise ValueError("non-finite JSON number")
    def finite_float(raw):
        value = float(raw)
        if not math.isfinite(value):
            raise ValueError("non-finite JSON number")
        return value
    result = json.loads(body.decode("utf-8"), object_pairs_hook=object_pairs,
                        parse_constant=reject_constant, parse_float=finite_float)
    if type(result) is not dict:
        raise ValueError("expected JSON object")
    return result


def _contains_secret(value, secret: str) -> bool:
    # Scan decoded strings/keys too, catching JSON-escaped representations.
    if isinstance(value, str):
        return secret in value
    if isinstance(value, dict):
        return any(_contains_secret(k, secret) or _contains_secret(v, secret) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(_contains_secret(v, secret) for v in value)
    return False


def _http_error(status: int) -> tuple[str, bool]:
    if status in (401, 403):
        return "authentication_error", False
    if status in (400, 422):
        return "request_rejected", False
    if status == 429:
        return "rate_limited", True
    if status == 529:
        return "overloaded", True
    if status == 408 or (500 <= status <= 599 and status not in (501, 505)):
        return "service_error", True
    if 300 <= status < 400:
        return "redirect_rejected", False
    return "http_error", False


class JevClient:
    """One reusable client per process; no global/multi-process quota claim."""

    def __init__(self, config: JevConfig, *, transport: Transport | None = None,
                 monotonic=time.monotonic, sleep=time.sleep, wall_clock=time.time,
                 random_value=random.random):
        self.config = config
        self._transport = transport if transport is not None else HttpTransport()
        self._clock, self._sleep = monotonic, sleep
        self._wall_clock, self._random = wall_clock, random_value
        self._slots = threading.BoundedSemaphore(config.max_concurrency)
        self._lock = threading.Lock()
        self._cooldown_until = 0.0

    def _retry_after(self, headers: Mapping[str, str]) -> float | None:
        raw = next((v for k, v in headers.items() if k.lower() == "retry-after"), "")
        try:
            seconds = float(raw)
        except (ValueError, TypeError):
            try:
                seconds = parsedate_to_datetime(raw).timestamp() - self._wall_clock()
            except (ValueError, TypeError, OverflowError, AttributeError):
                return None
        return max(0.0, seconds) if math.isfinite(seconds) else None

    def evaluate(self, packed: PackingResult) -> JevCallResult:
        """Send the exact T04 body; no hidden repacking or automatic action."""
        started = self._clock()
        deadline = started + self.config.total_wait_budget_s
        call_id = str(uuid4())
        attempts = []

        def finish(status, code=None, retryable=False, retry_after=None, body=None):
            return JevCallResult(status, call_id, packed.request_id, code, retryable,
                                 tuple(attempts), max(0.0, self._clock() - started),
                                 retry_after, packed, body)

        if not self.config.enabled:
            return finish("skipped", "disabled")
        if packed.status != "ready":
            return finish("skipped", "packing_deferred")
        if not self.config.api_key:
            return finish("error", "missing_credentials")
        try:
            body = packed.to_provider_bytes()
            request = _decode_object(body)
            if _contains_secret(request, self.config.api_key):
                return finish("error", "credentials_in_request")
            if request.get("model") != self.config.model:
                return finish("error", "model_mismatch")
            if (set(request) != {"model", "state", "questions"} or
                type(request["questions"]) is not dict or not request["questions"]):
                return finish("error", "invalid_packed_request")
        except (ValueError, UnicodeError, RecursionError):
            return finish("error", "invalid_packed_request")

        if not self._slots.acquire(timeout=max(0.0, deadline - self._clock())):
            return finish("error", "queue_wait_exceeded", True)
        try:
            for number in range(1, self.config.max_retries + 2):
                with self._lock:
                    delay = max(0.0, self._cooldown_until - self._clock())
                remaining = deadline - self._clock()
                if delay >= remaining or remaining <= 0:
                    return finish("error", "wait_budget_exceeded", True, delay or None)
                if delay:
                    self._sleep(delay)
                    # Another caller may have extended the shared cooldown.
                    while True:
                        with self._lock:
                            delay = max(0.0, self._cooldown_until - self._clock())
                        if delay <= 0:
                            break
                        if delay >= deadline - self._clock():
                            return finish("error", "wait_budget_exceeded", True, delay)
                        self._sleep(delay)
                attempt_started = self._clock()
                remaining = deadline - attempt_started
                if remaining <= 0:
                    return finish("error", "wait_budget_exceeded", True)
                status, response_body, retry_after = None, None, None
                try:
                    response = self._transport.send(url=self.config.base_url, api_key=self.config.api_key,
                        body=body, timeout_s=min(self.config.request_timeout_s, remaining),
                        max_response_bytes=self.config.max_response_bytes)
                    status = response.status
                    if type(status) is not int or not 100 <= status <= 599:
                        status = None
                        code, retryable = "transport_error", False
                    elif 200 <= status < 300:
                        if len(response.body) > self.config.max_response_bytes:
                            code, retryable = "response_too_large", False
                        else:
                            try:
                                decoded = _decode_object(response.body)
                                if _contains_secret(decoded, self.config.api_key):
                                    code, retryable = "credentials_in_response", False
                                else:
                                    code, retryable, response_body = None, False, response.body
                            except (ValueError, UnicodeError, RecursionError):
                                code, retryable = "invalid_response_json", False
                    else:
                        code, retryable = _http_error(status)
                        retry_after = self._retry_after(response.headers)
                except TransportError as exc:
                    code = exc.code
                    retryable = code in {"timeout", "network_error"}
                except Exception:
                    # Includes misbehaving injected transports. Do not expose
                    # their exception text or retry programming errors.
                    code, retryable = "transport_error", False
                attempts.append(CallAttempt(number, status, code, max(0.0, self._clock() - attempt_started)))
                # Apply shared cooldown even on the final allowed attempt.
                delay = 0.0
                if retryable:
                    backoff = min(self.config.retry_max_s, self.config.retry_initial_s * 2 ** min(number - 1, 30))
                    delay = max(retry_after or 0.0, backoff * (0.5 + 0.5 * self._random()))
                if code in {"rate_limited", "overloaded"}:
                    with self._lock:
                        self._cooldown_until = max(self._cooldown_until, self._clock() + delay)
                if code is not None and not retryable:
                    return finish("error", code)
                if self._clock() >= deadline:
                    return finish("error", "wait_budget_exceeded", True, retry_after)
                if code is None:
                    return finish("success", body=response_body)
                if not retryable or number > self.config.max_retries:
                    return finish("error", code, retryable,
                                  delay if code in {"rate_limited", "overloaded"} else retry_after)
                if delay >= deadline - self._clock():
                    return finish("error", "retry_wait_exceeds_budget", True, delay)
                self._sleep(delay)
        finally:
            self._slots.release()
