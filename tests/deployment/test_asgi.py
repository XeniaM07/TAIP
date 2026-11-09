"""Bounded ASGI body processing before invoking workflow code."""

import asyncio
import json
import unittest


class ASGIHelperTests(unittest.IsolatedAsyncioTestCase):
    def helpers(self):
        from taip.deployment import asgi
        return asgi

    async def test_body_is_bounded_even_without_content_length(self):
        helper = self.helpers()
        messages = iter([{"type": "http.request", "body": b"123", "more_body": True},
                         {"type": "http.request", "body": b"45", "more_body": False}])
        async def receive(): return next(messages)
        with self.assertRaises(helper.RequestRejected) as error:
            await helper.read_body({"headers": []}, receive, max_bytes=4, timeout_seconds=1)
        self.assertEqual(error.exception.status_code, 413)

    async def test_duplicate_or_incorrect_lengths_are_rejected(self):
        helper = self.helpers()
        async def receive(): return {"type": "http.request", "body": b"x", "more_body": False}
        for headers in ([(b"content-length", b"1"), (b"content-length", b"1")],
                        [(b"content-length", b"2")], [(b"content-length", b"-1")]):
            with self.assertRaises(helper.RequestRejected):
                await helper.read_body({"headers": headers}, receive, max_bytes=10, timeout_seconds=1)

    async def test_absolute_read_deadline_rejects_slow_body(self):
        helper = self.helpers()
        async def receive():
            await asyncio.sleep(1)
            return {"type": "http.request", "body": b"x", "more_body": True}
        with self.assertRaises(helper.RequestRejected) as error:
            await helper.read_body({"headers": []}, receive, max_bytes=100, timeout_seconds=.01)
        self.assertEqual(error.exception.status_code, 408)

    async def test_disconnect_never_becomes_a_complete_request(self):
        helper = self.helpers()
        async def receive(): return {"type": "http.disconnect"}
        with self.assertRaises(helper.RequestRejected):
            await helper.read_body({"headers": []}, receive, max_bytes=100, timeout_seconds=1)

    async def test_ambiguous_or_unsupported_transfer_framing_is_rejected_before_read(self):
        helper = self.helpers()
        async def receive():
            self.fail("Ambiguous framing must be rejected before reading the body")
        for headers in ([(b"content-length", b"1"), (b"transfer-encoding", b"chunked")],
                        [(b"transfer-encoding", b"gzip")],
                        [(b"transfer-encoding", b"chunked"), (b"transfer-encoding", b"chunked")]):
            with self.assertRaises(helper.RequestRejected) as error:
                await helper.read_body({"headers": headers}, receive, max_bytes=100, timeout_seconds=1)
            self.assertEqual(error.exception.status_code, 400)

    def test_strict_json_and_nodes_are_checked_without_echoing_input(self):
        helper = self.helpers()
        self.assertEqual(helper.validate_json_payload(b'{"x":1}', max_bytes=100), {"x": 1})
        for raw in (b'{"private":NaN}', b'{"x":1,"x":2}', b'not-json', b'{"x":' + b'[' * 40 + b'0' + b']' * 40 + b'}'):
            with self.assertRaises(helper.RequestRejected) as error:
                helper.validate_json_payload(raw, max_bytes=10000)
            self.assertNotIn("private", str(error.exception))

    async def test_safe_response_has_authentication_and_retry_headers(self):
        helper = self.helpers()
        messages = []
        async def send(message): messages.append(message)
        await helper.send_json(send, 401, "authentication_required", retry_after_seconds=2.1, challenge="Basic")
        self.assertEqual(messages[0]["status"], 401)
        headers = dict(messages[0]["headers"])
        self.assertIn(b"Basic", headers[b"www-authenticate"])
        self.assertEqual(headers[b"retry-after"], b"3")
        self.assertEqual(json.loads(messages[1]["body"])["code"], "authentication_required")


if __name__ == "__main__": unittest.main()
