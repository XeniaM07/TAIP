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
