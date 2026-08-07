from __future__ import annotations


class SkillRuntimeError(Exception):
    """Base exception for runtime-specific failures."""


class LLMConfigError(SkillRuntimeError, ValueError):
    """Raised when llm_config.json is missing or invalid."""


class MissingAPIKeyError(LLMConfigError):
    """Raised when the configured API key environment variable is empty."""


class LLMRequestError(SkillRuntimeError, RuntimeError):
    """Raised when a model request cannot be completed successfully."""


class LLMHTTPError(LLMRequestError):
    """Raised when the model endpoint returns a non-2xx HTTP response."""


class LLMConnectionError(LLMRequestError):
    """Raised when the model endpoint cannot be reached."""


class LLMResponseFormatError(LLMRequestError):
    """Raised when the response payload does not match the expected schema."""
