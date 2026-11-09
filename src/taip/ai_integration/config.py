"""Explicit provider configuration, loaded only when requested by the caller."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from hashlib import sha256
import math
import os
from pathlib import Path
import stat

from taip.ai_integration.errors import AIConfigurationError
from taip.ai_integration.rate_limits import RateLimitPolicy


class ProviderKind(str, Enum):
    GEMINI = "gemini"
    GROQ = "groq"
    OPENROUTER = "openrouter"


def _require_string(value: object, name: str) -> str:
    if (not isinstance(value, str) or not value or value != value.strip()
            or any(ord(character) < 32 or ord(character) == 127 for character in value)):
        raise AIConfigurationError(name)
    return value


def _is_finite_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """Separate the stable logical alias from the exact vendor model slug."""

    model_id: str
    provider: ProviderKind
    api_model_id: str
    api_key: str = field(repr=False)
    timeout_seconds: float = 60.0
    max_output_tokens: int = 8192
    temperature: float = 0.0
    max_response_bytes: int = 1048576
    use_json_schema: bool = False
    max_prompt_bytes: int = 16384
    max_input_bytes: int = 65536
    max_request_bytes: int = 131072
    rate_limits: RateLimitPolicy = field(default_factory=RateLimitPolicy)

    def __post_init__(self) -> None:
        for name in ("model_id", "api_model_id", "api_key"):
            _require_string(getattr(self, name), name)
        for name, maximum in (("model_id", 128), ("api_model_id", 512), ("api_key", 4096)):
            try:
                if len(getattr(self, name).encode("utf-8")) > maximum:
                    raise AIConfigurationError(name)
            except UnicodeError:
                raise AIConfigurationError(name) from None
        if not isinstance(self.provider, ProviderKind):
            raise AIConfigurationError("provider")
        if not _is_finite_number(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise AIConfigurationError("timeout_seconds")
        if not _is_finite_number(self.temperature) or not 0 <= self.temperature <= 2:
            raise AIConfigurationError("temperature")
        for name in ("max_output_tokens", "max_response_bytes", "max_prompt_bytes", "max_input_bytes", "max_request_bytes"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise AIConfigurationError(name)
        if type(self.use_json_schema) is not bool:
            raise AIConfigurationError("use_json_schema")
        if self.max_response_bytes > 1048576:
            raise AIConfigurationError("max_response_bytes")
        if not isinstance(self.rate_limits, RateLimitPolicy):
            raise AIConfigurationError("rate_limits")


def provider_quota_key(config: ProviderConfig) -> tuple[str, bytes]:
    """Group aliases by service and credential without retaining a plaintext key."""
    try:
        fingerprint = sha256(config.api_key.encode("utf-8")).digest()
    except UnicodeError:
        raise AIConfigurationError("api_key") from None
    return config.provider.value, fingerprint


def _number_option(
    environment: Mapping[str, str],
    name: str,
    default: int | float,
    convert: Callable[[str], int | float],
) -> int | float:
    if name not in environment:
        return default
    value = _require_string(environment[name], name)
    try:
        return convert(value)
    except (ValueError, OverflowError):
        raise AIConfigurationError(name) from None


def _schema_option(environment: Mapping[str, str], name: str) -> bool:
    value = environment.get(name, "false")
    if not isinstance(value, str) or value not in ("true", "false"):
        raise AIConfigurationError(name)
    return value == "true"


def _rate_policy(environment: Mapping[str, str], prefix: str) -> RateLimitPolicy:
    defaults = RateLimitPolicy()
    controls = {
        name: _number_option(environment, f"{prefix}_{name.upper()}", getattr(defaults, name), convert)
        for name, convert in (
            ("requests_per_minute", int), ("requests_per_day", int),
            ("tokens_per_minute", int), ("max_concurrent", int), ("warning_fraction", float),
        )
    }
    try:
        return RateLimitPolicy(**controls)
    except AIConfigurationError as error:
        raise AIConfigurationError(f"{prefix}_{error.field.upper()}") from None


def load_provider_configs(
    environment: Mapping[str, str] | None = None,
) -> tuple[ProviderConfig, ...]:
    """Load all three providers from a supplied mapping or the current environment.

    Every model slug and credential is required. A credential is supplied inline
    or through an explicit *_API_KEY_FILE, never both. No dotenv state is merged;
    an explicit mapping never falls back to the ambient environment.
    """
    source = os.environ if environment is None else environment
    controls = {
        "timeout_seconds": _number_option(source, "TAIP_AI_TIMEOUT_SECONDS", 60.0, float),
        "max_output_tokens": _number_option(source, "TAIP_AI_MAX_OUTPUT_TOKENS", 8192, int),
        "temperature": _number_option(source, "TAIP_AI_TEMPERATURE", 0.0, float),
        "max_response_bytes": _number_option(source, "TAIP_AI_MAX_RESPONSE_BYTES", 1048576, int),
        "max_prompt_bytes": _number_option(source, "TAIP_AI_MAX_PROMPT_BYTES", 16384, int),
        "max_input_bytes": _number_option(source, "TAIP_AI_MAX_INPUT_BYTES", 65536, int),
        "max_request_bytes": _number_option(source, "TAIP_AI_MAX_REQUEST_BYTES", 131072, int),
    }
    control_fields = {
        "timeout_seconds": "TAIP_AI_TIMEOUT_SECONDS",
        "max_output_tokens": "TAIP_AI_MAX_OUTPUT_TOKENS",
        "temperature": "TAIP_AI_TEMPERATURE",
        "max_response_bytes": "TAIP_AI_MAX_RESPONSE_BYTES",
        "max_prompt_bytes": "TAIP_AI_MAX_PROMPT_BYTES",
        "max_input_bytes": "TAIP_AI_MAX_INPUT_BYTES",
        "max_request_bytes": "TAIP_AI_MAX_REQUEST_BYTES",
    }
    configs = []
    for provider in ProviderKind:
        prefix = provider.value.upper()
        key_name, file_name = f"{prefix}_API_KEY", f"{prefix}_API_KEY_FILE"
        if file_name in source:
            if key_name in source:
                raise AIConfigurationError(file_name)
            path = _require_string(source[file_name], file_name)
            try:
                secret_path = Path(path)
                if not stat.S_ISREG(secret_path.stat().st_mode):
                    raise ValueError
                with secret_path.open("rb") as stream:
                    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                        raise ValueError
                    content = stream.read(4097)
                if len(content) > 4096:
                    raise ValueError
                api_key = _require_string(content.decode("utf-8").rstrip("\r\n"), file_name)
            except (OSError, ValueError, UnicodeError):
                raise AIConfigurationError(file_name) from None
        else:
            api_key = _require_string(source.get(key_name), key_name)
        api_model_id = _require_string(source.get(f"{prefix}_MODEL"), f"{prefix}_MODEL")
        use_json_schema = _schema_option(source, f"{prefix}_JSON_SCHEMA")
        rate_limits = _rate_policy(source, prefix)
        try:
            config = ProviderConfig(
                model_id=provider.value,
                provider=provider,
                api_model_id=api_model_id,
                api_key=api_key,
                use_json_schema=use_json_schema,
                rate_limits=rate_limits,
                **controls,
            )
        except AIConfigurationError as error:
            field_name = {"api_model_id": f"{prefix}_MODEL", "api_key": f"{prefix}_API_KEY"}.get(error.field)
            raise AIConfigurationError(field_name or control_fields.get(error.field, error.field)) from None
        configs.append(config)
    return tuple(configs)
