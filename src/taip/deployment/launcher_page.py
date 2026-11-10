"""Static, short-lived browser handoff page for the local Windows launcher."""

import base64
import hashlib


_SCRIPT = """(async () => {
  const fragment = location.hash.slice(1);
  history.replaceState(null, "", location.pathname);
  const fail = () => {
    document.getElementById("status").textContent =
      "Automatic sign-in failed. Start TAIP from its launcher again.";
  };
  const parameters = new URLSearchParams(fragment);
  const code = parameters.get("code");
  if ([...parameters.keys()].length !== 1 || !code || !/^[A-Za-z0-9_-]{43}$/.test(code)) {
    fail();
    return;
  }
  try {
    const response = await fetch("/_launcher/session", {
      method: "POST",
      credentials: "same-origin",
      redirect: "error",
      cache: "no-store",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({code})
    });
    if (!response.ok) {
      fail();
      return;
    }
    location.replace("/");
  } catch {
    fail();
  }
})();"""

_STYLE = "body{font:16px system-ui,sans-serif;margin:12vh auto;padding:0 24px;max-width:560px;color:#143e35}h1{font-size:28px}"


def _source_hash(source: str) -> str:
    return base64.b64encode(hashlib.sha256(source.encode("utf-8")).digest()).decode("ascii")


_BODY = (
    '<!doctype html><html lang="en"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1">'
    '<title>Opening TAIP</title><link rel="icon" href="data:,">'
    '<style>' + _STYLE + '</style></head><body><main><h1>TAIP</h1>'
    '<p id="status" role="status">Opening your application...</p></main>'
    '<script>' + _SCRIPT + '</script></body></html>'
).encode("utf-8")
_POLICY = (
    "default-src 'none'; "
    "script-src 'sha256-" + _source_hash(_SCRIPT) + "'; "
    "style-src 'sha256-" + _source_hash(_STYLE) + "'; "
    "connect-src 'self'; img-src data:; frame-ancestors 'none'; "
    "base-uri 'none'; form-action 'none'"
).encode("ascii")


def launcher_page() -> tuple[int, list[tuple[bytes, bytes]], bytes]:
    """Return fixed HTML without embedding application credentials or a code."""
    headers = [
        (b"content-type", b"text/html; charset=utf-8"),
        (b"content-length", str(len(_BODY)).encode("ascii")),
        (b"cache-control", b"no-store"),
        (b"content-security-policy", _POLICY),
        (b"referrer-policy", b"no-referrer"),
        (b"x-content-type-options", b"nosniff"),
        (b"x-frame-options", b"DENY"),
        (b"cross-origin-resource-policy", b"same-origin"),
    ]
    return 200, headers, _BODY
