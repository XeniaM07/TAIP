"""Launcher handoff, browser cookie boundaries, and executable bootstrap behavior."""

import asyncio
import base64
from dataclasses import fields, replace
import hashlib
from http.cookies import SimpleCookie
import importlib
from importlib.util import find_spec
import json
import re
import shutil
import subprocess
import unittest
from unittest.mock import patch

import httpx

try:
    import fakeredis
except ImportError:
    fakeredis = None

from taip.ai_integration.rate_limits import RateLimiter
from tests.deployment.test_gateway import INTERNAL, TOKEN, settings


ORIGIN = "https://localhost:8443"
CODE = "A" * 43
NODE = shutil.which("node")


class LauncherPageTests(unittest.TestCase):
    def page(self):
        name = "taip.deployment.launcher_page"
        self.assertIsNotNone(find_spec(name), "The launcher must serve a browser handoff page")
        return importlib.import_module(name).launcher_page()

    def script(self):
        status, headers, body = self.page()
        self.assertEqual(status, 200)
        scripts = re.findall(r"<script>(.*?)</script>", body.decode("utf-8"), re.DOTALL)
        self.assertEqual(len(scripts), 1)
        return scripts[0]

    def test_page_policies_allow_only_its_static_script_and_same_origin_exchange(self):
        status, raw_headers, body = self.page()
        headers = dict(raw_headers)
        self.assertEqual(status, 200)
        self.assertEqual(headers[b"cache-control"], b"no-store")
        self.assertEqual(headers[b"referrer-policy"], b"no-referrer")
        self.assertEqual(headers[b"x-content-type-options"], b"nosniff")
        self.assertEqual(headers[b"x-frame-options"], b"DENY")
        self.assertIn(b"text/html", headers[b"content-type"])
        self.assertEqual(int(headers[b"content-length"]), len(body))
        policy = headers[b"content-security-policy"].decode("ascii")
        for directive in ("default-src 'none'", "connect-src 'self'", "frame-ancestors 'none'",
                          "base-uri 'none'", "form-action 'none'"):
            self.assertIn(directive, policy)
        script_hash = base64.b64encode(hashlib.sha256(self.script().encode()).digest()).decode()
        self.assertIn("'sha256-" + script_hash + "'", policy)
        self.assertNotIn("'unsafe-inline'", policy)
        self.assertIn(b'href="data:,"', body)
        self.assertNotIn(TOKEN.encode(), body)

    @unittest.skipUnless(NODE, "Node is required to execute the browser bootstrap script")
    def execute(self, fragment, *, success=True, fail_fetch=False):
        runner = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const events = [];
const status = {textContent: ''};
const location = {
  pathname: '/_launcher/', hash: input.fragment,
  replace(target) { events.push({kind: 'redirect', target}); }
};
const context = vm.createContext({
  URLSearchParams, location,
  history: {replaceState(state, title, target) {
    events.push({kind: 'clear', target}); location.hash = '';
  }},
  document: {getElementById(name) {
    if (name !== 'status') throw new Error('Unexpected document access');
    return status;
  }},
  console: Object.fromEntries(['log', 'warn', 'error'].map(method => [method, (...values) => {
    events.push({kind: 'console', value: values.map(String).join(' ')});
  }])),
  fetch: async (target, options) => {
    events.push({kind: 'fetch', target, options, fragment: location.hash});
    if (input.failFetch) throw new Error('Private network detail ' + input.fragment);
    return {ok: input.success};
  }
});
(async () => {
  await Promise.resolve(vm.runInContext(input.script, context, {timeout: 1000}));
  await new Promise(resolve => setImmediate(resolve));
  process.stdout.write(JSON.stringify({events, status: status.textContent, hash: location.hash}));
})().catch(error => { process.stderr.write(error.message); process.exitCode = 1; });
"""
        completed = subprocess.run(
            [NODE, "-e", runner],
            input=json.dumps({"script": self.script(), "fragment": fragment,
                              "success": success, "failFetch": fail_fetch}),
            text=True, encoding="utf-8", capture_output=True, timeout=10,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertFalse(completed.stderr)
        return json.loads(completed.stdout)

    def test_bootstrap_clears_fragment_before_one_exchange_and_redirects_only_to_root(self):
        result = self.execute("#code=" + CODE)
        self.assertEqual([event["kind"] for event in result["events"]], ["clear", "fetch", "redirect"])
        clear, exchange, redirect = result["events"]
        self.assertEqual(clear["target"], "/_launcher/")
        self.assertEqual(exchange["fragment"], "")
        self.assertEqual(exchange["target"], "/_launcher/session")
        self.assertEqual(exchange["options"]["method"], "POST")
        self.assertEqual(exchange["options"]["credentials"], "same-origin")
        self.assertEqual(exchange["options"]["cache"], "no-store")
        self.assertEqual(exchange["options"]["redirect"], "error")
        self.assertEqual(exchange["options"]["headers"]["Content-Type"], "application/json")
        self.assertEqual(json.loads(exchange["options"]["body"]), {"code": CODE})
        self.assertEqual(redirect["target"], "/")
        self.assertEqual(result["hash"], "")

    def test_missing_malformed_or_ambiguous_fragments_never_exchange_or_redirect(self):
        for fragment in ("", "#code=bad", "#code=" + CODE + "&code=" + CODE,
                         "#code=" + CODE + "&next=https://attacker.invalid", "#other=" + CODE):
            with self.subTest(fragment_length=len(fragment)):
                result = self.execute(fragment)
                self.assertEqual([event["kind"] for event in result["events"]], ["clear"])
                self.assertTrue(result["status"])
                self.assertNotIn(CODE, result["status"])

    def test_failed_exchange_never_redirects_or_displays_code_or_network_details(self):
        for options in ({"success": False}, {"fail_fetch": True}):
            with self.subTest(options=options):
                result = self.execute("#code=" + CODE, **options)
                self.assertEqual([event["kind"] for event in result["events"]], ["clear", "fetch"])
                self.assertTrue(result["status"])
                self.assertNotIn(CODE, result["status"])
                self.assertNotIn("Private network detail", result["status"])


@unittest.skipUnless(fakeredis, "fakeredis[lua] is required for browser gateway contracts")
class BrowserLoginGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def client(self, *, enabled=True, backend=None, config=None):
        self.assertIn("launcher_login_enabled", {field.name for field in fields(settings())},
                      "The gateway needs an explicit opt-in for automatic launcher login")
        name = "taip.deployment.browser_sessions"
        self.assertIsNotNone(find_spec(name), "Browser handoffs need a shared one-use session store")
        sessions = importlib.import_module(name)
        from taip.deployment.gateway import create_gateway_app
        backend = fakeredis.FakeRedis() if backend is None else backend
        self.addCleanup(backend.close)
        config = config or replace(settings(), public_origin=ORIGIN, launcher_login_enabled=enabled)
        store = sessions.RedisBrowserSessions(backend, namespace=config.redis.namespace,
                                              public_origin=config.public_origin,
                                              access_token_digest=config.access_token_digest)
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={"status": "ok", "mode": "mock"},
                                  headers={"Set-Cookie": "upstream_cookie=must-not-escape"})
        app = create_gateway_app(config, limiter=RateLimiter(), browser_sessions=store,
                                 transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(app.aclose)
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=config.public_origin)
        self.addAsyncCleanup(client.aclose)
        return client, store, calls, backend

    async def mint(self, client):
        response = await client.post("/_launcher/code", headers={"Authorization": "Bearer " + TOKEN}, content=b"")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(set(response.json()), {"code"})
        code = response.json()["code"]
        self.assertRegex(code, r"^[A-Za-z0-9_-]{43}$")
        self.assertNotIn("www-authenticate", response.headers)
        self.assertEqual(response.headers["cache-control"], "no-store")
        return code

    async def login(self, client, store):
        code = await self.mint(client)
        response = await client.post("/_launcher/session", headers={"Origin": ORIGIN}, json={"code": code})
        self.assertEqual(response.status_code, 200, response.text)
        cookies = SimpleCookie()
        cookies.load(response.headers["set-cookie"])
        cookie = cookies[store.cookie_name]
        self.assertTrue(cookie["secure"])
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"].lower(), "strict")
        self.assertEqual(cookie["path"], "/")
        self.assertEqual(cookie["domain"], "")
        self.assertEqual(cookie["max-age"], "43200")
        self.assertRegex(cookie.value, r"^[A-Za-z0-9_-]{43}$")
        self.assertNotEqual(cookie.value, code)
        self.assertNotIn(TOKEN, response.text + response.headers["set-cookie"])
        return code, cookie.value

    async def test_public_bootstrap_needs_no_password_and_never_reaches_an_upstream(self):
        client, _, calls, _ = await self.client()
        response = await client.get("/_launcher/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertNotIn("www-authenticate", response.headers)
        self.assertNotIn("set-cookie", response.headers)
        self.assertEqual(calls, [])
        unauthenticated = await client.get("/api/health")
        self.assertEqual(unauthenticated.status_code, 401)
        self.assertNotIn("www-authenticate", unauthenticated.headers)

    async def test_cookie_login_reaches_core_without_forwarding_browser_or_public_credentials(self):
        client, store, calls, _ = await self.client()
        await self.login(client, store)
        response = await client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("set-cookie", response.headers)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].headers["Authorization"], "Bearer " + INTERNAL)
        self.assertNotIn("cookie", calls[0].headers)
        self.assertNotIn(TOKEN, str(calls[0].url) + calls[0].content.decode())

    async def test_mint_requires_explicit_bearer_even_after_cookie_login(self):
        client, store, calls, _ = await self.client()
        basic = "Basic " + base64.b64encode(("taip:" + TOKEN).encode()).decode()
        for headers in ({}, {"Authorization": basic}):
            response = await client.post("/_launcher/code", headers=headers, content=b"")
            self.assertEqual(response.status_code, 401)
        await self.login(client, store)
        self.assertEqual((await client.post("/_launcher/code", content=b"")).status_code, 401)
        self.assertEqual(calls, [])

    async def test_exchange_consumes_code_once_and_invalid_codes_never_create_a_cookie(self):
        client, store, calls, _ = await self.client()
        code, _ = await self.login(client, store)
        for value in (code, "B" * 43):
            response = await client.post("/_launcher/session", headers={"Origin": ORIGIN}, json={"code": value})
            self.assertEqual(response.status_code, 401)
            self.assertNotIn("set-cookie", response.headers)
            self.assertNotIn("www-authenticate", response.headers)
            self.assertNotIn(value, response.text)
        self.assertEqual(calls, [])

    async def test_exchange_requires_one_exact_origin_and_does_not_consume_rejected_codes(self):
        client, _, calls, _ = await self.client()
        code = await self.mint(client)
        for headers in ({}, {"Origin": "null"}, {"Origin": "https://attacker.invalid"},
                        [("Origin", ORIGIN), ("Origin", ORIGIN)]):
            response = await client.post("/_launcher/session", headers=headers, json={"code": code})
            self.assertEqual(response.status_code, 403)
        self.assertEqual((await client.post("/_launcher/session", headers={"Origin": ORIGIN},
                                           json={"code": code})).status_code, 200)
        self.assertEqual(calls, [])

    async def test_exchange_rejects_non_json_oversized_or_ambiguous_bodies_before_cookie_creation(self):
        client, _, calls, _ = await self.client()
        code = await self.mint(client)
        response = await client.post("/_launcher/session", headers={"Origin": ORIGIN,
                                     "Content-Type": "text/plain"}, content=json.dumps({"code": code}))
        self.assertEqual(response.status_code, 415)
        invalid = (b"[]", b'{"code":null}', b'{"code":"' + code.encode() + b'","extra":1}',
                   b'{"code":"' + code.encode() + b'","code":"' + code.encode() + b'"}')
        for body in invalid:
            response = await client.post("/_launcher/session", headers={"Origin": ORIGIN,
                                         "Content-Type": "application/json"}, content=body)
            self.assertEqual(response.status_code, 400)
            self.assertNotIn("set-cookie", response.headers)
        response = await client.post("/_launcher/session", headers={"Origin": ORIGIN,
                                     "Content-Type": "application/json"}, content=b" " * 257)
        self.assertEqual(response.status_code, 413)
        self.assertEqual((await client.post("/_launcher/session", headers={"Origin": ORIGIN},
                                           json={"code": code})).status_code, 200)
        self.assertEqual(calls, [])

    async def test_cookie_mutations_require_origin_and_cross_origin_reads_are_rejected(self):
        client, store, calls, _ = await self.client()
        await self.login(client, store)
        for headers in ({}, {"Origin": "null"}, {"Origin": "https://attacker.invalid"}):
            self.assertEqual((await client.post("/api/sessions", headers=headers, json={})).status_code, 403)
        for origin in ("null", "https://attacker.invalid"):
            self.assertEqual((await client.get("/api/health", headers={"Origin": origin})).status_code, 403)
        response = await client.post("/api/sessions", headers={"Origin": ORIGIN}, json={})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(calls), 1)

    async def test_invalid_or_duplicate_explicit_authorization_never_falls_back_to_valid_cookie(self):
        client, store, calls, _ = await self.client()
        await self.login(client, store)
        for headers in ({"Authorization": "Bearer " + "B" * 64},
                        [("Authorization", "Bearer " + TOKEN), ("Authorization", "Bearer " + TOKEN)]):
            response = await client.get("/api/health", headers=headers)
            self.assertEqual(response.status_code, 401)
        self.assertEqual(calls, [])
        self.assertEqual((await client.get("/api/health", headers={"Authorization": "Bearer " + TOKEN})).status_code, 200)

    async def test_duplicate_session_cookies_never_authenticate(self):
        client, store, calls, _ = await self.client()
        _, value = await self.login(client, store)
        item = store.cookie_name + "=" + value
        for headers in ({"Cookie": item + "; " + item}, [("Cookie", item), ("Cookie", item)]):
            self.assertEqual((await client.get("/api/health", headers=headers)).status_code, 401)
        self.assertEqual(calls, [])

    async def test_disabled_handoff_routes_are_not_exposed_even_to_valid_bearer(self):
        client, _, calls, _ = await self.client(enabled=False)
        for method, path in (("GET", "/_launcher/"), ("POST", "/_launcher/code"),
                             ("POST", "/_launcher/session")):
            response = await client.request(method, path, headers={"Authorization": "Bearer " + TOKEN})
            self.assertEqual(response.status_code, 404)
        self.assertEqual(calls, [])

    async def test_nonlocal_origins_cannot_enable_launcher_login(self):
        self.assertIn("launcher_login_enabled", {field.name for field in fields(settings())})
        with self.assertRaises(ValueError):
            replace(settings(), launcher_login_enabled=True)

    async def test_session_backend_failures_never_accept_a_cookie_or_leak_backend_details(self):
        client, store, calls, backend = await self.client()
        await self.login(client, store)
        with patch.object(backend, "eval", side_effect=RuntimeError("private-backend-value")):
            for method, path, headers in (("GET", "/api/health", {}),
                                          ("POST", "/_launcher/code", {"Authorization": "Bearer " + TOKEN})):
                response = await client.request(method, path, headers=headers)
                self.assertEqual(response.status_code, 503)
                self.assertNotIn("private-backend-value", response.text)
                self.assertNotIn("set-cookie", response.headers)
        self.assertEqual(calls, [])

    async def test_bootstrap_rejects_wrong_host_plain_http_queries_and_unsupported_methods(self):
        client, _, calls, _ = await self.client()
        self.assertEqual((await client.get("/_launcher/", headers={"Host": "attacker.invalid"})).status_code, 400)
        self.assertEqual((await client.get("http://localhost:8443/_launcher/")).status_code, 403)
        self.assertEqual((await client.get("/_launcher/?next=https://attacker.invalid")).status_code, 400)
        for method, path in (("POST", "/_launcher/"), ("GET", "/_launcher/code"), ("GET", "/_launcher/session")):
            self.assertEqual((await client.request(method, path)).status_code, 405)
        self.assertEqual(calls, [])

    async def test_code_exchange_is_single_use_across_distinct_gateway_workers(self):
        backend = fakeredis.FakeRedis()
        first, _, calls_one, _ = await self.client(backend=backend)
        second, _, calls_two, _ = await self.client(backend=backend)
        code = await self.mint(first)
        responses = await asyncio.gather(*(
            client.post("/_launcher/session", headers={"Origin": ORIGIN}, json={"code": code})
            for client in (first, second)
        ))
        self.assertEqual(sorted(response.status_code for response in responses), [200, 401])
        self.assertEqual(sum("set-cookie" in response.headers for response in responses), 1)
        self.assertEqual(calls_one + calls_two, [])

    async def test_only_authenticated_code_mints_consume_normal_request_quota(self):
        config = replace(settings(http_requests_per_day=1), public_origin=ORIGIN, launcher_login_enabled=True)
        client, _, calls, _ = await self.client(config=config)
        for _ in range(3):
            self.assertEqual((await client.post("/_launcher/code")).status_code, 401)
        await self.mint(client)
        response = await client.post("/_launcher/code", headers={"Authorization": "Bearer " + TOKEN})
        self.assertEqual(response.status_code, 429)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
