"""Create an operator-owned deployment directory outside every Git worktree.

Run ``python -m taip.deployment.setup_public --directory <private-directory>``
from the checkout, then run the printed Compose command. The browser Basic
dialog accepts username ``taip`` and the password in access-token.txt;
API clients use the same password as a Bearer token. Keep this file private.

The default localhost deployment uses Caddy's local CA, ports 8080/8443, and
publishes only on loopback 127.0.0.1. For public access, explicitly supply
--bind-address 0.0.0.0, --domain and --http-port 80 --https-port 443;
point its DNS records at the host before starting automatic certificate issuance.
No provider keys are needed: the private core starts with mock AI providers.
"""

import argparse
from hashlib import sha256
import ipaddress
import os
from pathlib import Path
import re
import secrets
import shlex
from typing import Sequence


_PRIVATE_FILES = (
    "access-token.txt", "access-token.sha256", "internal-token.txt",
    "redis-password.txt", "redis.acl", "public.env",
)
_PRIVATE_DIRECTORIES = ("redis-data", "caddy-data", "caddy-config")
_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", re.ASCII)


def _domain(value: str) -> str:
    if not isinstance(value, str) or not value.isascii() or len(value) > 253:
        raise ValueError("domain must be an ASCII DNS name or localhost")
    domain = value.lower()
    if not domain or not all(_DNS_LABEL.fullmatch(label) for label in domain.split(".")):
        raise ValueError("domain must be a DNS name without scheme, port or path")
    try:
        ipaddress.ip_address(domain)
    except ValueError:
        return domain
    raise ValueError("domain must be a DNS name or localhost, not an IP address")


def _integer(value: int, name: str, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"{name} must be an integer between 1 and {maximum}")
    return value


def _bind_address(value: str) -> str:
    if type(value) is not str or "%" in value:
        raise ValueError("bind_address must be an IPv4 or IPv6 address without a scope")
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        raise ValueError("bind_address must be an IPv4 or IPv6 address") from None


def _private_write(path: Path, content: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(content)


def _env_path(path: Path) -> str:
    # Compose single quotes suppress interpolation of operator directory names.
    return "'" + path.as_posix().replace("'", "\\'") + "'"


def prepare_deployment(
    directory: Path, *, domain: str = "localhost", http_port: int = 8080,
    https_port: int = 8443, workers: int = 2, bind_address: str = "127.0.0.1",
) -> Path:
    """Generate fresh private files and return public.env; never overwrite a setup.

    POSIX modes are 0700 for directories and 0600 for files. On Windows, place
    the directory under a user-private profile; POSIX chmod is not a Windows ACL.
    Containers run with the creating operator's UID/GID (1000/1000 on Windows),
    so Docker bind mounts can read private files without widening their modes.
    """
    domain = _domain(domain)
    http_port = _integer(http_port, "http_port", 65535)
    https_port = _integer(https_port, "https_port", 65535)
    workers = _integer(workers, "workers", 32)
    bind_address = _bind_address(bind_address)
    if http_port == https_port:
        raise ValueError("HTTP and HTTPS ports must differ")
    directory = Path(directory).expanduser().resolve()
    if any(character in str(directory) for character in ("\n", "\r", "\x00")):
        raise ValueError("directory must not contain line breaks or NUL characters")
    for ancestor in (directory, *directory.parents):
        if (ancestor / ".git").exists():
            raise ValueError("deployment files must be outside Git worktrees")
    for name in (*_PRIVATE_FILES, *_PRIVATE_DIRECTORIES):
        path = directory / name
        if path.exists() or path.is_symlink():
            raise FileExistsError(f"refusing to overwrite an existing deployment path: {path}")

    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "posix":
        directory.chmod(0o700)
    for name in _PRIVATE_DIRECTORIES:
        (directory / name).mkdir(mode=0o700)

    access_token, internal_token, redis_password = (
        secrets.token_urlsafe(48) for _ in range(3)
    )
    _private_write(directory / "access-token.txt", access_token + "\n")
    _private_write(directory / "access-token.sha256", sha256(access_token.encode()).hexdigest() + "\n")
    _private_write(directory / "internal-token.txt", internal_token + "\n")
    _private_write(directory / "redis-password.txt", redis_password + "\n")
    redis_commands = (
        "+ping +hello +client|setinfo +client|setname "
        "+get +set +del +time +exists +pexpire "
        "+zadd +zrem +zrange +zcard +zcount +zremrangebyscore +zrangebyscore "
        "+hset +hget +hdel +sadd +srem +eval"
    )
    _private_write(
        directory / "redis.acl",
        "user default off\nuser taip on #" + sha256(redis_password.encode()).hexdigest()
        + " ~taip:* -@all " + redis_commands + "\n",
    )
    origin = "https://" + domain + (f":{https_port}" if https_port != 443 else "")
    environment = {
        "TAIP_DOMAIN": domain,
        "TAIP_BIND_ADDRESS": bind_address,
        "TAIP_HTTP_PORT": str(http_port),
        "TAIP_HTTPS_PORT": str(https_port),
        "TAIP_PUBLIC_ORIGIN": origin,
        "TAIP_GATEWAY_WORKERS": str(workers),
        "TAIP_APP_UID": str(os.getuid() if hasattr(os, "getuid") else 1000),
        "TAIP_APP_GID": str(os.getgid() if hasattr(os, "getgid") else 1000),
    }
    for key, name in {
        "TAIP_ACCESS_TOKEN_SHA256_FILE": "access-token.sha256",
        "TAIP_INTERNAL_TOKEN_FILE": "internal-token.txt",
        "TAIP_REDIS_PASSWORD_FILE": "redis-password.txt",
        "TAIP_REDIS_ACL_FILE": "redis.acl",
        "TAIP_REDIS_DATA_DIR": "redis-data",
        "TAIP_CADDY_DATA_DIR": "caddy-data",
        "TAIP_CADDY_CONFIG_DIR": "caddy-config",
    }.items():
        environment[key] = _env_path(directory / name)
    config = directory / "public.env"
    _private_write(config, "".join(f"{key}={value}\n" for key, value in environment.items()))
    return config


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--domain", default="localhost")
    parser.add_argument(
        "--bind-address", default="127.0.0.1",
        help="host IP to publish on; default loopback, use 0.0.0.0 explicitly for public IPv4",
    )
    parser.add_argument("--http-port", type=int, default=8080)
    parser.add_argument("--https-port", type=int, default=8443)
    parser.add_argument("--workers", type=int, default=2)
    arguments = parser.parse_args(argv)
    try:
        config = prepare_deployment(
            arguments.directory, domain=arguments.domain, http_port=arguments.http_port,
            https_port=arguments.https_port, workers=arguments.workers,
            bind_address=arguments.bind_address,
        )
    except (ValueError, OSError) as error:
        parser.error(str(error))
    print(config)
    quote = (lambda value: "'" + value.replace("'", "''") + "'") if os.name == "nt" else shlex.quote
    print("docker compose --env-file " + quote(str(config)) + " -f compose.public.yaml up --build -d")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
