"""Pluggable AI-provider interface.

This MVP ships only with NullProvider, which makes no external calls. A future
provider (for example an OpenAI-compatible endpoint, a private deployment or a
local model) would implement AIProvider.extract_fields. Invoice text must never
be sent to an external provider without explicit administrator authorisation,
which is NOT implemented here.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class AIProviderUnavailable(Exception):
    pass


class AIProvider(ABC):
    name: str = "base"
    is_external: bool = False

    @abstractmethod
    def extract_fields(self, text: str) -> dict[str, Any]:
        """Return a dict of candidate fields. Callers must validate it with Pydantic."""


class NullProvider(AIProvider):
    name = "none"

    def extract_fields(self, text: str) -> dict[str, Any]:
        raise AIProviderUnavailable("No AI provider is configured. Rule-based extraction and manual review are in use.")


def get_provider() -> AIProvider:
    return NullProvider()
