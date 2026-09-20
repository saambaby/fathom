"""Shared OpenAI-compatible LLM adapter (INV-20).

Owns ``OpenAICompatClient`` and the injectable ``_ClientAdapter`` protocol.
Call sites (pretrade, news-risk, narration, session brief) import from here
so all in-process LLM traffic shares one adapter, one timeout, and one
offline predicate (no client + no ``LLM_API_KEY`` → no network I/O).

INV-08: the API key is stored privately and excluded from ``__repr__``.
"""

from __future__ import annotations

import os
from typing import Optional, Protocol

import httpx

# ---------------------------------------------------------------------------
# Module constants (D-P3-E) — pinned defaults for in-process LLM calls
# ---------------------------------------------------------------------------

MODEL: str = "gpt-5-nano"

#: Default OpenAI-compatible endpoint; any compatible provider works via
#: ``LLM_BASE_URL`` (e.g. https://api.groq.com/openai/v1, http://localhost:11434/v1).
DEFAULT_BASE_URL: str = "https://api.openai.com/v1"

#: HTTP timeout for an LLM call — generous but bounded; a hang must not
#: stall the caller (except → safe default / fallback covers a timeout).
_HTTP_TIMEOUT_S: float = 30.0


class _ClientAdapter(Protocol):
    """Thin, provider-agnostic adapter interface for in-process LLM calls.

    The live implementation (``OpenAICompatClient``) speaks the OpenAI
    chat-completions wire format over httpx.  Tests inject a stub that returns
    a fixed payload without any network call.  Call sites accept any object
    satisfying this protocol via the ``client`` parameter.
    """

    def complete(self, prompt: str) -> str:
        """Send a text prompt to the model and return the raw text response.

        Args:
            prompt: The full prompt string.

        Returns:
            The raw assistant text.

        Raises:
            Any transport/protocol exception — callers must wrap in try/except
            and fall back to their documented safe default.
        """
        ...


class OpenAICompatClient:
    """Provider-agnostic adapter for any OpenAI-compatible chat endpoint.

    Works with OpenAI, Groq, NVIDIA NIM, OpenRouter, Gemini's compatibility
    layer, or a local Ollama — pick the provider with ``base_url``/``model``.

    INV-08: the API key is stored privately and excluded from ``__repr__``,
    so it can never leak into a log line or traceback rendering of the client.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        model: str = MODEL,
    ) -> None:
        self.__api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model

    def __repr__(self) -> str:
        """Key-free representation (INV-08)."""
        return (
            f"OpenAICompatClient(base_url={self.base_url!r}, model={self.model!r})"
        )

    @classmethod
    def from_env(cls) -> Optional["OpenAICompatClient"]:
        """Build a client from ``LLM_API_KEY``/``LLM_BASE_URL``/``LLM_MODEL``.

        Returns:
            A configured client, or ``None`` when ``LLM_API_KEY`` is unset
            (the offline fail-closed path — INV-02 / INV-20).
        """
        api_key = os.environ.get("LLM_API_KEY")
        if not api_key:
            return None
        return cls(
            api_key=api_key,
            base_url=os.environ.get("LLM_BASE_URL") or DEFAULT_BASE_URL,
            model=os.environ.get("LLM_MODEL") or MODEL,
        )

    def complete(self, prompt: str) -> str:
        """POST a single user turn to ``{base_url}/chat/completions``.

        Args:
            prompt: The full prompt string.

        Returns:
            ``choices[0].message.content``.

        Raises:
            ValueError: if the response has no choices or a non-string content.
            Exception: any httpx transport error, or the ``raise_for_status``
                error on a non-2xx response.  The caller converts these into
                the documented safe default.
        """
        with httpx.Client(timeout=_HTTP_TIMEOUT_S) as client:
            response = client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.__api_key}"},
                json={
                    "model": self.model,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            response.raise_for_status()
            payload = response.json()

        choices = payload.get("choices") if isinstance(payload, dict) else None
        if not choices:
            raise ValueError("LLM response contained no choices")
        content = (choices[0].get("message") or {}).get("content")
        if not isinstance(content, str):
            raise ValueError(
                f"LLM response content was not a string: {type(content).__name__}"
            )
        return content
