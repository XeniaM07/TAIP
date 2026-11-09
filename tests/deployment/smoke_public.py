"""Bounded HTTPS smoke driver for the ephemeral, mock-only Compose CI stack.

This script accepts only HTTPS loopback origins, reads only that test setup's
generated access token, and never prints credentials or response bodies.
It is intentionally separate from unittest discovery: it needs running Docker.
"""

import argparse
from pathlib import Path
import ipaddress
import re
import sys
import time
from urllib.parse import urlsplit

import httpx

from taip.application.workflow import MOCK_DEMO_PROMPT, MOCK_MODEL_IDS


class SmokeFailure(RuntimeError):
    """A diagnostic containing only route names and HTTP statuses."""


def _loopback_origin(value: str) -> str:
    parsed = urlsplit(value)
    host = parsed.hostname
    try:
        local = host == "localhost" or (host is not None and ipaddress.ip_address(host).is_loopback)
    except ValueError:
        local = False
    if (parsed.scheme != "https" or not local or parsed.username or parsed.password
            or parsed.path or parsed.query or parsed.fragment
            or parsed.port is None or not 1 <= parsed.port <= 65535):
        raise ValueError("smoke origin must be HTTPS loopback with an explicit port")
    return value


def _expect(response: httpx.Response, status: int, route: str) -> httpx.Response:
    if response.status_code != status:
        raise SmokeFailure(f"{route}: expected HTTP {status}, received {response.status_code}")
    return response


def run_smoke(directory: Path, origin: str, *, http_port: int, redis_unavailable: bool = False) -> None:
    origin = _loopback_origin(origin)
    if not 1 <= http_port <= 65535:
        raise ValueError("HTTP port must be between 1 and 65535")
    token = (directory / "access-token.txt").read_text(encoding="utf-8").strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{64}", token) is None:
        raise ValueError("smoke setup must contain a freshly generated test token")
    bearer = {"Authorization": "Bearer " + token}
    # localhost uses an ephemeral Caddy local CA; this exemption is test-only.
    with httpx.Client(base_url=origin, verify=False, trust_env=False, timeout=15,
                      follow_redirects=False) as client:
        if redis_unavailable:
            _expect(client.get("/api/health", headers=bearer), 503, "Redis outage")
            print("PASS: an authenticated HTTPS request fails closed during Redis outage.")
            return
        deadline = time.monotonic() + 30
        while True:
            try:
                response = client.get("/")
                if response.status_code != 502:
                    break
            except httpx.RequestError:
                if time.monotonic() >= deadline:
                    raise SmokeFailure("HTTPS gateway did not become ready within 30 seconds") from None
            if time.monotonic() >= deadline:
                raise SmokeFailure("HTTPS gateway did not become ready within 30 seconds")
            time.sleep(0.25)
        _expect(response, 401, "anonymous frontend")
        if "Basic" not in response.headers.get("www-authenticate", ""):
            raise SmokeFailure("anonymous frontend did not offer the browser Basic dialog")
        _expect(client.get("/api/health", headers={"Authorization": "Bearer invalid"}), 401, "invalid API token")
        page = _expect(client.get("/", auth=("taip", token)), 200, "Basic frontend")
        if "text/html" not in page.headers.get("content-type", ""):
            raise SmokeFailure("Basic frontend did not serve HTML")
        _expect(client.get("/api/health", auth=("taip", token)), 200, "Basic API health")
        _expect(client.get("/api/health", headers=bearer), 200, "Bearer API health")
        _expect(client.post("/api/sessions", auth=("taip", token), json={"prompt": MOCK_DEMO_PROMPT}),
                403, "Basic mutation without Origin")
        _expect(client.post("/api/sessions", auth=("taip", token), headers={"Origin": origin},
                            json={"prompt": MOCK_DEMO_PROMPT}), 201, "same-origin Basic mutation")
        created = _expect(client.post("/api/sessions", headers=bearer, json={"prompt": MOCK_DEMO_PROMPT}),
                          201, "Bearer session creation")
        session_id = created.json()["session_id"]
        _expect(client.post(f"/api/sessions/{session_id}/extract", headers=bearer,
                            json={"model_ids": None}), 200, "mock extraction")
        for action in ("confirm", "solve", "verify"):
            _expect(client.post(f"/api/sessions/{session_id}/models/{MOCK_MODEL_IDS[0]}/{action}",
                                headers=bearer), 200, "mock " + action)
        summary = _expect(client.get(f"/api/sessions/{session_id}/compare", headers=bearer),
                          200, "mock comparison")
        if summary.json()["models"][0]["verification_status"] != "verified":
            raise SmokeFailure("mock comparison was not independently verified")
        _expect(client.post("/api/sessions", headers=bearer, content=b"x" * 1_048_577),
                413, "oversized body")
        hostname = urlsplit(origin).hostname
        authority = f"[{hostname}]" if ":" in hostname else hostname
        redirect = _expect(client.get(f"http://{authority}:{http_port}/?smoke=1"), 308, "HTTP redirect")
        if redirect.headers.get("location") != origin + "/?smoke=1":
            raise SmokeFailure("HTTP redirect did not retain the configured HTTPS origin and URI")
    print("PASS: loopback HTTPS, browser Basic, API Bearer, private mock workflow, body limit and HTTP redirect.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--origin", default="https://localhost:8443")
    parser.add_argument("--http-port", type=int, default=8080)
    parser.add_argument("--expect-redis-unavailable", action="store_true")
    arguments = parser.parse_args()
    try:
        run_smoke(arguments.directory, arguments.origin, http_port=arguments.http_port,
                  redis_unavailable=arguments.expect_redis_unavailable)
    except (SmokeFailure, OSError, ValueError, KeyError, httpx.HTTPError) as error:
        print(f"Public deployment smoke failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
