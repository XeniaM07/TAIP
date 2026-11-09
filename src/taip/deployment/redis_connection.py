"""Explicit, bounded Redis connections without automatic command retries."""

from redis import Redis
from redis.backoff import NoBackoff
from redis.retry import Retry

from taip.deployment.settings import RedisSettings


def create_redis_client(settings: RedisSettings) -> Redis:
    return Redis(host=settings.host, port=settings.port, username=settings.username,
                 password=settings.password, db=0, ssl=settings.tls,
                 socket_connect_timeout=2, socket_timeout=2, max_connections=32,
                 health_check_interval=30, decode_responses=False,
                 retry=Retry(NoBackoff(), 0), retry_on_error=[])
