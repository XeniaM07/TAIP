"""Offline HTTP boundary checks using real HTTPX requests and streamed replies."""

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from importlib import import_module, util
import json
import unittest
from unittest.mock import patch

import httpx


class TrackedStream(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.read_count = 0
        self.closed = False

    def __iter__(self):
        for chunk in self.chunks:
            self.read_count += 1
            yield chunk

    def close(self):
        self.closed = True


class TrackedTransport(httpx.BaseTransport):
    def __init__(self):
        self.requests = []
        self.closed = False

    def handle_request(self, request):
        self.requests.append(request)
        return httpx.Response(200, json={"ok": True})

    def close(self):
        self.closed = True


class HTTPTransportTests(unittest.TestCase):
    def setUp(self):
        name = "taip.ai_integration.http_transport"
        self.assertIsNotNone(util.find_spec(name), "The HTTP transport must exist")
        self.module = import_module(name)
        self.error = import_module("taip.ai_integration.errors").AIProviderError

    def make_transport(self, handler):
        transport = self.module.HTTPJSONTransport(transport=httpx.MockTransport(handler))
        self.addCleanup(transport.close)
        return transport

    def post(self, transport, **overrides):
        arguments = {
            "url": "https://provider.example/v1/chat",
            "headers": {"Authorization": "Bearer fake-private-key"},
            "body": {"messages": [{"role": "user", "content": "orare românești"}]},
            "timeout_seconds": 12.5,
            "max_response_bytes": 1024,
        }
        arguments.update(overrides)
        return transport.post(**arguments)

    def test_post_preserves_headers_json_and_per_call_timeouts(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"model": "exact-model", "choices": []})

        transport = self.make_transport(handler)
        result = self.post(transport)
        request = requests[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(str(request.url), "https://provider.example/v1/chat")
        self.assertEqual(request.headers["authorization"], "Bearer fake-private-key")
        self.assertEqual(request.headers["content-type"], "application/json")
        self.assertEqual(request.headers["accept-encoding"], "identity")
        self.assertEqual(json.loads(request.content)["messages"][0]["content"], "orare românești")
        self.assertEqual(request.extensions["timeout"], {
            "connect": 12.5, "read": 12.5, "write": 12.5, "pool": 12.5,
        })
        self.assertEqual(result, {"model": "exact-model", "choices": []})

    def test_successful_provider_error_envelope_is_left_for_adapter(self):
        envelope = {"error": {"code": 429, "message": "provider detail"}}
        transport = self.make_transport(lambda request: httpx.Response(200, json=envelope))
        self.assertEqual(self.post(transport), envelope)

    def test_http_statuses_are_safe_typed_failures_without_retries(self):
        cases = {
            301: "http_error", 307: "http_error", 400: "http_error",
            401: "auth_error", 402: "quota_exceeded", 403: "permission_denied",
            404: "model_unavailable", 408: "timeout", 429: "rate_limit", 498: "provider_unavailable",
            500: "provider_unavailable", 502: "provider_unavailable",
            503: "provider_unavailable", 504: "timeout",
        }
        for status, code in cases.items():
            with self.subTest(status=status):
                requests = []

                def handler(request):
                    requests.append(request)
                    return httpx.Response(status, headers={"location": "https://elsewhere.example"},
                                          text="fake-private-key secret prompt")

                transport = self.make_transport(handler)
                with self.assertRaises(self.error) as caught:
                    self.post(transport)
                error = caught.exception
                self.assertEqual(error.code, code)
                self.assertEqual(error.status_code, status)
                self.assertEqual(len(requests), 1)
                self.assertNotIn("fake-private-key", str(error))
                self.assertNotIn("secret prompt", repr(error))
                self.assertIsNone(error.__cause__)

    def test_retryable_reports_transient_conditions_only(self):
        for code in ("timeout", "network_error", "rate_limit", "provider_unavailable"):
            self.assertTrue(self.error(code).retryable)
        for code in ("auth_error", "permission_denied", "quota_exceeded", "invalid_envelope"):
            self.assertFalse(self.error(code).retryable)

    def test_retry_after_accepts_delta_seconds_and_http_date(self):
        tomorrow = datetime.now(timezone.utc) + timedelta(days=1)
        for value, expected in (("3", 3.0), (format_datetime(tomorrow, usegmt=True), None)):
            with self.subTest(value=value):
                transport = self.make_transport(lambda request: httpx.Response(
                    429, headers={"Retry-After": value}, content=b"private error"))
                with self.assertRaises(self.error) as caught:
                    self.post(transport)
                delay = caught.exception.retry_after_seconds
                if expected is not None:
                    self.assertEqual(delay, expected)
                else:
                    self.assertGreater(delay, 86000)
                    self.assertLessEqual(delay, 86400)

    def test_retry_after_discards_invalid_negative_or_nonfinite_values(self):
        for value in ("-1", "nan", "inf", "garbage", "", "1e999"):
            with self.subTest(value=value):
                transport = self.make_transport(lambda request: httpx.Response(
                    503, headers={"Retry-After": value}))
                with self.assertRaises(self.error) as caught:
                    self.post(transport)
                self.assertIsNone(caught.exception.retry_after_seconds)

    def test_timeout_and_request_errors_are_normalized_without_secret_text(self):
        for exception_type, code in ((httpx.ReadTimeout, "timeout"),
                                     (httpx.ConnectTimeout, "timeout"),
                                     (httpx.ConnectError, "network_error"),
                                     (httpx.RemoteProtocolError, "network_error")):
            with self.subTest(exception_type=exception_type):
                calls = []

                def handler(request):
                    calls.append(request)
                    raise exception_type("fake-private-key", request=request)

                transport = self.make_transport(handler)
                with self.assertRaises(self.error) as caught:
                    self.post(transport)
                self.assertEqual(caught.exception.code, code)
                self.assertNotIn("fake-private-key", str(caught.exception))
                self.assertEqual(len(calls), 1)

    def test_unexpected_programming_errors_are_not_misclassified(self):
        def handler(request):
            raise RuntimeError("programming error")
        transport = self.make_transport(handler)
        with self.assertRaisesRegex(RuntimeError, "programming error"):
            self.post(transport)

    def test_strict_json_and_utf8_failures_are_rejected(self):
        values = (
            b"", b"plain text", b"```json\n{}\n```", b"{}{}", b"[]", b"null",
            b'{"x":1,"x":2}', b'{"nested":{"a":1,"a":2}}',
            b'{"x":NaN}', b'{"x":Infinity}', b'{"x":1e999}', b'{"x":"\xff"}',
        )
        for value in values:
            with self.subTest(value=value):
                transport = self.make_transport(lambda request: httpx.Response(200, content=value))
                with self.assertRaises(self.error) as caught:
                    self.post(transport)
                self.assertEqual(caught.exception.code, "invalid_envelope")

    def test_streaming_limit_stops_reading_and_closes_response(self):
        stream = TrackedStream([b'{"x":"', b"a" * 30, b"must not be consumed"])
        transport = self.make_transport(lambda request: httpx.Response(200, stream=stream))
        with self.assertRaises(self.error) as caught:
            self.post(transport, max_response_bytes=20)
        self.assertEqual(caught.exception.code, "response_too_large")
        self.assertEqual(stream.read_count, 2)
        self.assertTrue(stream.closed)

    def test_response_at_exact_byte_limit_is_accepted(self):
        payload = '{"name":"școală"}'.encode()
        stream = TrackedStream([payload[:7], payload[7:]])
        transport = self.make_transport(lambda request: httpx.Response(200, stream=stream))
        self.assertEqual(self.post(transport, max_response_bytes=len(payload)), {"name": "școală"})
        self.assertTrue(stream.closed)

    def test_content_length_does_not_bypass_streaming_limit(self):
        stream = TrackedStream([b" " * 30])
        transport = self.make_transport(lambda request: httpx.Response(
            200, headers={"Content-Length": "1"}, stream=stream))
        with self.assertRaises(self.error) as caught:
            self.post(transport, max_response_bytes=20)
        self.assertEqual(caught.exception.code, "response_too_large")

    def test_unexpected_compression_is_rejected_before_decompression(self):
        stream = TrackedStream([b"compressed bytes are never read"])
        transport = self.make_transport(lambda request: httpx.Response(
            200, headers={"Content-Encoding": "gzip"}, stream=stream))
        with self.assertRaises(self.error) as caught:
            self.post(transport)
        self.assertEqual(caught.exception.code, "unsupported_encoding")
        self.assertEqual(stream.read_count, 0)
        self.assertTrue(stream.closed)

    def test_stream_read_timeout_is_normalized_and_response_closed(self):
        class TimeoutStream(TrackedStream):
            def __iter__(self):
                yield b'{"x":'
                raise httpx.ReadTimeout("fake-private-key")
        stream = TimeoutStream([])
        transport = self.make_transport(lambda request: httpx.Response(200, stream=stream))
        with self.assertRaises(self.error) as caught:
            self.post(transport)
        self.assertEqual(caught.exception.code, "timeout")
        self.assertTrue(stream.closed)

    def test_drip_feed_cannot_extend_an_operation_beyond_its_elapsed_budget(self):
        stream = TrackedStream([b'{"x":', b'1}'])
        transport = self.make_transport(lambda request: httpx.Response(200, stream=stream))
        with patch.object(self.module, "monotonic", side_effect=[0, .1, 3.1], create=True):
            with self.assertRaises(self.error) as caught:
                self.post(transport, timeout_seconds=1)
        self.assertEqual(caught.exception.code, "timeout")
        self.assertTrue(stream.closed)
        self.assertEqual(stream.read_count, 1)

    def test_connection_transport_is_reused_and_closed_by_context(self):
        wire = TrackedTransport()
        with self.module.HTTPJSONTransport(transport=wire) as transport:
            self.post(transport)
            self.post(transport)
            self.assertEqual(len(wire.requests), 2)
            self.assertFalse(wire.closed)
        self.assertTrue(wire.closed)
        transport.close()
        with self.assertRaises(RuntimeError):
            self.post(transport)


if __name__ == "__main__":
    unittest.main()
