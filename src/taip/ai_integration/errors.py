"""Safe, inspectable failures at the AI response boundary."""


class AIResponseError(ValueError):
    """Reject an AI response without including its content in public messages.

    The caller can retain raw_response in its private invocation record. It
    should not copy that field into logs or user-facing exception messages.
    """

    def __init__(self, code: str, path: str, message: str) -> None:
        self.code = code
        self.path = path
        self.raw_response: str | None = None
        super().__init__(f"{code} at {path}: {message}")


class AIConfigurationError(ValueError):
    """Identify an invalid configuration field without retaining its value."""

    def __init__(self, field: str) -> None:
        self.code = "configuration_error"
        self.field = field
        super().__init__("Invalid AI provider configuration.")


class AIProviderError(RuntimeError):
    """Expose a failure category without retaining request or response secrets."""

    def __init__(
        self,
        code: str,
        *,
        status_code: int | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        self.code = code
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        self.retryable = code in {
            "timeout", "network_error", "rate_limit", "provider_unavailable",
        }
        super().__init__(f"AI provider failure ({code}).")


def provider_error_for_status(
    status_code: int, *, retry_after_seconds: float | None = None
) -> AIProviderError:
    """Classify HTTP failures and provider envelopes using the same safe codes."""
    code = {
        401: "auth_error",
        402: "quota_exceeded",
        403: "permission_denied",
        404: "model_unavailable",
        408: "timeout",
        429: "rate_limit",
        498: "provider_unavailable",
        504: "timeout",
    }.get(status_code)
    if code is None:
        code = "provider_unavailable" if 500 <= status_code <= 599 else "http_error"
    return AIProviderError(code, status_code=status_code, retry_after_seconds=retry_after_seconds)
