"""Narrow structural views of provider clients for the Lab 04 Adapter design.

These protocols declare dependencies without importing or invoking an SDK.
They cover text generation only, not complete vendor APIs. ``ThirdModelClient``
is a proposed port because the third provider has not been selected.
"""

from typing import Protocol


class OpenAITextResponse(Protocol):
    """Text exposed by an OpenAI Responses API result."""

    @property
    def output_text(self) -> str:
        ...


class OpenAIResponses(Protocol):
    """The text-generation portion of the OpenAI responses resource."""

    def create(self, *, model: str, input: str) -> OpenAITextResponse:
        ...


class OpenAIClient(Protocol):
    """Provider-facing client composed by ``OpenAIAdapter``."""

    @property
    def responses(self) -> OpenAIResponses:
        ...


class GeminiTextResponse(Protocol):
    """Optional text exposed by a Gemini generation result."""

    @property
    def text(self) -> str | None:
        ...


class GeminiModels(Protocol):
    """The text-generation portion of the Google Gen AI models resource."""

    def generate_content(self, *, model: str, contents: str) -> GeminiTextResponse:
        ...


class GeminiClient(Protocol):
    """Provider-facing client composed by ``GeminiAdapter``."""

    @property
    def models(self) -> GeminiModels:
        ...


class ThirdModelClient(Protocol):
    """Proposed text-generation port, without selecting a third provider."""

    def generate(self, *, model: str, prompt: str) -> str:
        ...
