"""Transport/configuration tests with fake HTTPS; never contact a service."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from email.utils import format_datetime
import json
import os
from pathlib import Path
import socket
import ssl
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from ctn.config import JevConfig, Settings
from ctn.jev import HttpResponse, HttpTransport, JevClient, JevPacker, PackingLimits, TransportError
from ctn.models import to_jsonable


# Deliberately synthetic, not shaped like a production credential.
KEY = "test-only-credential-abcXYZ"
DATA = json.loads((Path(__file__).parent / "fixtures/jev_input/minimal.json").read_text())
RESPONSE = b'{"model":"synthetic-model","answers":{},"usage":{"input_tokens":4,"output_tokens":2}}'


class Clock:
    def __init__(self):
        self.now = 0.0
        self.waits = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds


class SequenceTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def send(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if callable(response):
            return response()
        return response


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.packed = JevPacker().pack_post(DATA)
        self.clock = Clock()

    def client(self, transport, **settings):
        return JevClient(JevConfig(api_key=KEY, **settings), transport=transport,
                         monotonic=self.clock.monotonic, sleep=self.clock.sleep,
                         wall_clock=lambda: 1_800_000_000, random_value=lambda: 1.0)

    def test_exact_body_header_credential_and_detached_private_response(self):
        transport = SequenceTransport(HttpResponse(200, body=RESPONSE), HttpResponse(200, body=RESPONSE))
        client = self.client(transport)
        result = client.evaluate(self.packed)
        again = client.evaluate(self.packed)
        self.assertEqual(result.status, "success")
        self.assertEqual(transport.calls[0]["body"], self.packed.to_provider_bytes())
        self.assertEqual(transport.calls[0]["api_key"], KEY)
        self.assertEqual(transport.calls[0]["timeout_s"], 30)
        self.assertEqual(result.request_id, again.request_id)
        self.assertNotEqual(result.call_id, again.call_id)
        self.assertIs(result.packed_request, self.packed)
        response = result.provider_response()
        response["model"] = "changed"
        self.assertEqual(result.provider_response()["model"], "synthetic-model")
        safe = json.dumps(to_jsonable(result)) + repr(result) + repr(client.config)
        self.assertNotIn(KEY, safe)
        self.assertNotIn("synthetic-model", safe)
        self.assertNotIn(DATA["post"]["text"], safe)
        # Business validation is deliberately T06: empty answers are accepted
        # by transport alone and cannot be mistaken for a valid assessment.
        self.assertEqual(result.provider_response()["answers"], {})

    def test_disabled_missing_key_and_deferred_do_not_send(self):
        transport = SequenceTransport()
        disabled = self.client(transport, enabled=False).evaluate(self.packed)
        missing = JevClient(JevConfig(), transport=transport).evaluate(self.packed)
        deferred = JevPacker(limits=PackingLimits(max_state_bytes=10)).pack_post(DATA)
        skipped = self.client(transport).evaluate(deferred)
        self.assertEqual([disabled.error_code, missing.error_code, skipped.error_code],
                         ["disabled", "missing_credentials", "packing_deferred"])
        self.assertEqual(transport.calls, [])
        with self.assertRaises(ValueError):
            missing.provider_response()

    def test_model_mismatch_never_silently_rewrites_or_sends(self):
        transport = SequenceTransport()
        result = self.client(transport, model="another-model").evaluate(self.packed)
        self.assertEqual(result.error_code, "model_mismatch")
        self.assertEqual(transport.calls, [])

    def test_exact_key_in_json_escaped_state_is_not_sent(self):
        data = json.loads(json.dumps(DATA))
        data["post"]["text"] = "Test " + KEY
        packed = JevPacker().pack_post(data)
        self.assertEqual(packed.status, "ready")
        escaped = packed._body_json.replace(KEY, "\\u0074" + KEY[1:])
        packed = replace(packed, _body_json=escaped)
        transport = SequenceTransport()
        result = self.client(transport).evaluate(packed)
        self.assertEqual(result.error_code, "credentials_in_request")
        self.assertEqual(transport.calls, [])
        self.assertNotIn(KEY, json.dumps(result.to_dict()) + repr(result))

    def test_bad_packed_body_not_sent(self):
        for body in ("[]", '{"model":"jev-latest","state":{},"questions":{}}', '{"model":NaN}'):
            with self.subTest(body=body):
                transport = SequenceTransport()
                result = self.client(transport).evaluate(replace(self.packed, _body_json=body))
                self.assertNotEqual(result.status, "success")
                self.assertEqual(transport.calls, [])

    def test_permanent_http_errors_never_retry_or_echo_error_body(self):
        for status, expected in ((401, "authentication_error"), (403, "authentication_error"),
                                 (400, "request_rejected"), (422, "request_rejected"),
                                 (302, "redirect_rejected"), (404, "http_error"), (501, "http_error")):
            with self.subTest(status=status):
                transport = SequenceTransport(HttpResponse(status, {"Location": "https://other.test"}, KEY.encode()))
                result = self.client(transport).evaluate(self.packed)
                self.assertEqual(result.error_code, expected)
                self.assertFalse(result.retryable)
                self.assertEqual(len(transport.calls), 1)
                self.assertNotIn(KEY, repr(result) + json.dumps(result.to_dict()))

    def test_rate_limit_and_overload_back_off_before_success(self):
        transport = SequenceTransport(HttpResponse(429, {"Retry-After": "3"}),
                                      HttpResponse(529), HttpResponse(200, body=RESPONSE))
        result = self.client(transport).evaluate(self.packed)
        self.assertEqual(result.status, "success")
        self.assertEqual(self.clock.waits, [3, 2])
        self.assertEqual([a.http_status for a in result.attempts], [429, 529, 200])
        self.assertEqual(result.elapsed_s, 5)

    def test_retry_after_date_is_honored(self):
        date = format_datetime(datetime.fromtimestamp(1_800_000_005, timezone.utc), usegmt=True)
        transport = SequenceTransport(HttpResponse(429, {"Retry-After": date}), HttpResponse(200, body=RESPONSE))
        result = self.client(transport).evaluate(self.packed)
        self.assertEqual(result.status, "success")
        self.assertEqual(self.clock.waits, [5])

    def test_invalid_retry_after_uses_bounded_backoff(self):
        for header in ("garbage", "NaN", "infinity", "-10"):
            with self.subTest(header=header):
                self.clock = Clock()
                transport = SequenceTransport(HttpResponse(429, {"Retry-After": header}),
                                              HttpResponse(200, body=RESPONSE))
                self.assertEqual(self.client(transport).evaluate(self.packed).status, "success")
                self.assertEqual(self.clock.waits, [1])

    def test_retries_have_an_absolute_attempt_limit(self):
        transport = SequenceTransport(*(HttpResponse(503) for _ in range(3)))
        result = self.client(transport).evaluate(self.packed)
        self.assertEqual(result.error_code, "service_error")
        self.assertEqual(len(result.attempts), 3)
        self.assertEqual(self.clock.waits, [1, 2])

    def test_zero_retries_still_sets_cooldown_for_next_call(self):
        transport = SequenceTransport(HttpResponse(429, {"Retry-After": "4"}), HttpResponse(200, body=RESPONSE))
        client = self.client(transport, max_retries=0)
        self.assertEqual(client.evaluate(self.packed).error_code, "rate_limited")
        self.assertEqual(client.evaluate(self.packed).status, "success")
        self.assertEqual(self.clock.waits, [4])

    def test_long_retry_after_stops_without_shortening_provider_wait(self):
        transport = SequenceTransport(HttpResponse(429, {"Retry-After": "900"}))
        result = self.client(transport).evaluate(self.packed)
        self.assertEqual(result.error_code, "retry_wait_exceeds_budget")
        self.assertEqual(result.retry_after_s, 900)
        self.assertEqual(self.clock.waits, [])
        self.assertEqual(len(transport.calls), 1)

    def test_second_call_respects_shared_cooldown_longer_than_its_budget(self):
        transport = SequenceTransport(HttpResponse(429, {"Retry-After": "900"}))
        client = self.client(transport)
        client.evaluate(self.packed)
        result = client.evaluate(self.packed)
        self.assertEqual(result.error_code, "wait_budget_exceeded")
        self.assertEqual(len(result.attempts), 0)
        self.assertEqual(len(transport.calls), 1)

    def test_network_retry_caps_remaining_operation_timeout(self):
        def slow_timeout():
            self.clock.now += 29
            raise TransportError("timeout")
        transport = SequenceTransport(slow_timeout, HttpResponse(200, body=RESPONSE))
        result = self.client(transport, total_wait_budget_s=32).evaluate(self.packed)
        self.assertEqual(result.status, "success")
        self.assertEqual([c["timeout_s"] for c in transport.calls], [30, 2])
        self.assertEqual(result.attempts[0].elapsed_s, 29)

    def test_late_success_is_recorded_and_not_retried(self):
        def late_response():
            self.clock.now += 61
            return HttpResponse(200, body=RESPONSE)
        transport = SequenceTransport(late_response)
        result = self.client(transport).evaluate(self.packed)
        self.assertEqual(result.error_code, "wait_budget_exceeded")
        self.assertEqual(len(result.attempts), 1)
        self.assertEqual(result.attempts[0].http_status, 200)
        with self.assertRaises(ValueError):
            result.provider_response()

    def test_late_authentication_failure_stays_nonretryable(self):
        def late_auth():
            self.clock.now += 61
            return HttpResponse(401)
        result = self.client(SequenceTransport(late_auth)).evaluate(self.packed)
        self.assertEqual(result.error_code, "authentication_error")
        self.assertFalse(result.retryable)

    def test_malformed_transport_status_cannot_leak_exception_or_key(self):
        result = self.client(SequenceTransport(HttpResponse(KEY))).evaluate(self.packed)
        self.assertEqual(result.error_code, "transport_error")
        self.assertIsNone(result.attempts[0].http_status)
        self.assertNotIn(KEY, json.dumps(result.to_dict()) + repr(result))

    def test_network_error_can_recover_but_tls_failure_cannot(self):
        transport = SequenceTransport(TransportError("network_error"), HttpResponse(200, body=RESPONSE))
        self.assertEqual(self.client(transport).evaluate(self.packed).status, "success")
        transport = SequenceTransport(TransportError("tls_error"))
        result = self.client(transport).evaluate(self.packed)
        self.assertEqual(result.error_code, "tls_error")
        self.assertEqual(len(result.attempts), 1)
        self.assertFalse(result.retryable)

    def test_response_size_and_json_envelope_fail_without_retries(self):
        for body, code in ((b"x" * 1025, "response_too_large"), (b"not-json", "invalid_response_json"),
                           (b"[]", "invalid_response_json"), (b'{"x":NaN}', "invalid_response_json"),
                           (b'{"x":1e999}', "invalid_response_json"),
                           (b'{"x":1,"x":2}', "invalid_response_json"), (b'\xff', "invalid_response_json")):
            with self.subTest(code=code, body=body[:20]):
                transport = SequenceTransport(HttpResponse(200, body=body))
                result = self.client(transport, max_response_bytes=1024).evaluate(self.packed)
                self.assertEqual(result.error_code, code)
                self.assertEqual(len(transport.calls), 1)

    def test_echoed_credential_and_transport_exception_are_never_returned(self):
        escaped = ('{"echo":"\\u0074' + KEY[1:] + '"}').encode()
        transport = SequenceTransport(HttpResponse(200, body=escaped))
        result = self.client(transport).evaluate(self.packed)
        self.assertEqual(result.error_code, "credentials_in_response")
        with self.assertRaises(ValueError):
            result.provider_response()
        transport = SequenceTransport(RuntimeError(KEY))
        result = self.client(transport).evaluate(self.packed)
        self.assertEqual(result.error_code, "transport_error")
        self.assertNotIn(KEY, str(result) + json.dumps(result.to_dict()))

    def test_queue_deadline_and_semaphore_release_after_failure(self):
        client = self.client(SequenceTransport(HttpResponse(200, body=RESPONSE)), max_concurrency=1,
                             total_wait_budget_s=0.01)
        client._slots.acquire()
        result = client.evaluate(self.packed)
        client._slots.release()
        self.assertEqual(result.error_code, "queue_wait_exceeded")
        self.assertEqual(len(result.attempts), 0)
        self.assertEqual(client.evaluate(self.packed).status, "success")

    def test_threads_share_concurrency_limit_and_failures_release_slots(self):
        entered = threading.Event()
        release = threading.Event()
        lock = threading.Lock()
        active, maximum, total = 0, 0, 0

        class BlockingTransport:
            def send(inner, **kwargs):
                nonlocal active, maximum, total
                with lock:
                    active += 1
                    maximum = max(maximum, active)
                    total += 1
                    if active == 2:
                        entered.set()
                try:
                    if not release.wait(2):
                        raise RuntimeError("test synchronization failed")
                    raise TransportError("tls_error")
                finally:
                    with lock:
                        active -= 1

        client = JevClient(JevConfig(api_key=KEY, max_concurrency=2), transport=BlockingTransport())
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = [pool.submit(client.evaluate, self.packed) for _ in range(5)]
            try:
                self.assertTrue(entered.wait(2))
                self.assertEqual(maximum, 2)
            finally:
                release.set()
            self.assertEqual([f.result().error_code for f in futures], ["tls_error"] * 5)
        self.assertEqual(total, 5)
        self.assertEqual(maximum, 2)


class HttpTransportTests(unittest.TestCase):
    def send(self, connection, **overrides):
        with patch("ctn.jev.client.http.client.HTTPSConnection", return_value=connection) as factory:
            result = HttpTransport().send(url="https://api.typesafe.ai/v1/systemone", api_key=KEY,
                                         body=b'{"example":true}', timeout_s=1,
                                         max_response_bytes=overrides.get("limit", 100))
            self.assertEqual(factory.call_args.args, ("api.typesafe.ai", None))
            return result

    def response(self, status=200, chunks=(b'{"ok":true}', b""), length=""):
        response = Mock(status=status)
        response.getheader.side_effect = lambda name, default="": length if name == "Content-Length" else default
        response.read1.side_effect = chunks
        connection = Mock()
        connection.getresponse.return_value = response
        return connection, response

    def test_https_request_headers_exact_bytes_and_connection_close(self):
        connection, response = self.response()
        result = self.send(connection)
        self.assertEqual(result.body, b'{"ok":true}')
        args = connection.request.call_args
        self.assertEqual(args.args, ("POST", "/v1/systemone"))
        self.assertEqual(args.kwargs["headers"]["Authorization"], "Bearer " + KEY)
        self.assertEqual(args.kwargs["headers"]["Content-Type"], "application/json")
        self.assertEqual(args.kwargs["body"], b'{"example":true}')
        response.close.assert_called_once()
        connection.close.assert_called_once()

    def test_redirect_and_error_body_are_not_followed_or_read(self):
        for status in (302, 401, 429, 529):
            connection, response = self.response(status=status)
            result = self.send(connection)
            self.assertEqual(result.status, status)
            self.assertEqual(result.body, b"")
            response.read1.assert_not_called()
            response.close.assert_called_once()
            self.assertEqual(connection.request.call_count, 1)
            connection.close.assert_called_once()

    def test_declared_and_streaming_size_limit(self):
        for length, chunks in (("101", ()), ("", (b"x" * 60, b"y" * 41))):
            connection, response = self.response(chunks=chunks, length=length)
            with self.assertRaises(TransportError) as caught:
                self.send(connection)
            self.assertEqual(caught.exception.code, "response_too_large")
            connection.close.assert_called_once()

    def test_low_level_timeout_tls_network_errors_are_sanitized(self):
        for exception, code in ((socket.timeout(KEY), "timeout"),
                                (ssl.SSLError(KEY), "tls_error"), (OSError(KEY), "network_error")):
            connection = Mock()
            connection.request.side_effect = exception
            with self.assertRaises(TransportError) as caught:
                self.send(connection)
            self.assertEqual(caught.exception.code, code)
            self.assertNotIn(KEY, str(caught.exception))
            connection.close.assert_called_once()

    def test_short_body_with_content_length_is_not_accepted(self):
        connection, response = self.response(length="50")
        with self.assertRaises(TransportError) as caught:
            self.send(connection)
        self.assertEqual(caught.exception.code, "network_error")
        connection.close.assert_called_once()

    def test_drip_fed_body_stops_when_read_budget_expires(self):
        connection, response = self.response(chunks=(b"{", b'"x":', b"1}", b""))
        with patch("ctn.jev.client.time.monotonic", side_effect=[0, 0.4, 0.8, 1.2]):
            with self.assertRaises(TransportError) as caught:
                self.send(connection)
        self.assertEqual(caught.exception.code, "timeout")
        self.assertEqual(response.read1.call_count, 2)
        connection.close.assert_called_once()


class ConfigurationTests(unittest.TestCase):
    def test_invalid_limits_endpoints_and_headers_rejected_without_values(self):
        for kwargs in ({"base_url": "http://example.test"}, {"base_url": "https://user:pass@example.test"},
                       {"base_url": "https://example.test?secret=1"}, {"base_url": "https://example.test\n/path"},
                       {"api_key": KEY + "\n"}, {"api_key": KEY, "model": KEY},
                       {"max_retries": -1}, {"max_concurrency": 0}, {"enabled": "true"},
                       {"request_timeout_s": float("nan")}, {"retry_initial_s": 9},
                       {"max_response_bytes": True}, {"total_wait_budget_s": 0}):
            with self.subTest(kwargs=list(kwargs)):
                with self.assertRaises(ValueError) as caught:
                    JevConfig(**kwargs)
                self.assertNotIn(KEY, str(caught.exception))

    def test_dotenv_environment_precedence_and_all_transport_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("TYPESAFE_API_KEY=" + KEY + "\nCTN_JEV_TIMEOUT_S=12\nCTN_JEV_CONCURRENCY=2\n"
                            "CTN_JEV_MAX_RETRIES=0\nCTN_JEV_WAIT_BUDGET_S=25\n"
                            "CTN_JEV_RETRY_INITIAL_S=2\nCTN_JEV_RETRY_MAX_S=4\n"
                            "CTN_JEV_MAX_RESPONSE_BYTES=2048\nCTN_JEV_ENABLED=false\n")
            with patch.dict(os.environ, {"CTN_JEV_TIMEOUT_S": "7"}, clear=True):
                config = JevConfig.load(path)
            self.assertEqual(config.api_key, KEY)
            self.assertNotIn(KEY, repr(config))
            self.assertEqual((config.request_timeout_s, config.max_concurrency, config.max_retries,
                              config.total_wait_budget_s, config.retry_initial_s, config.retry_max_s,
                              config.max_response_bytes, config.enabled), (7, 2, 0, 25, 2, 4, 2048, False))
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_bad_env_value_does_not_silently_revert_to_default(self):
        for name, value in (("CTN_JEV_TIMEOUT_S", KEY), ("CTN_JEV_MAX_RETRIES", "broken"),
                            ("CTN_JEV_ENABLED", "maybe")):
            with patch.dict(os.environ, {name: value}, clear=True):
                with self.assertRaises(ValueError) as caught:
                    JevConfig.load("nonexistent.env")
                self.assertNotIn(KEY, str(caught.exception))

    def test_settings_uses_same_jev_loader(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"CTN_STATE_DIR": directory + "/state", "CTN_LOG_DIR": directory + "/logs",
                                         "CTN_JEV_TIMEOUT_S": "9", "CTN_JEV_MAX_RETRIES": "1"}, clear=True):
                settings = Settings.load(Path(directory) / "absent.env")
            self.assertEqual(settings.jev.request_timeout_s, 9)
            self.assertEqual(settings.jev.max_retries, 1)


if __name__ == "__main__":
    unittest.main()
