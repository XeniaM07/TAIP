"""Public interfaces for AI model adapters and their creators.

Operational methods are intentionally unimplemented for Lab 04.
"""

from taip.ai_integration.adapters import (
    AIModelAdapter,
    GeminiAdapter,
    OpenAIAdapter,
    ThirdModelAdapter,
)
from taip.ai_integration.creators import (
    GeminiAdapterCreator,
    ModelAdapterCreator,
    OpenAIAdapterCreator,
    ThirdModelAdapterCreator,
)

__all__ = [
    "AIModelAdapter",
    "GeminiAdapter",
    "GeminiAdapterCreator",
    "ModelAdapterCreator",
    "OpenAIAdapter",
    "OpenAIAdapterCreator",
    "ThirdModelAdapter",
    "ThirdModelAdapterCreator",
]
