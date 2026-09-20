"""Pre-trade check — INV-02 enforcement boundary (Phase 3).

The final in-process LLM veto before order submission.  Given an approved
``Candidate``, it asks the model for a ``{decision, reason}`` verdict:

* ``PretradeVerdict`` (pydantic v2) — ``{decision: "proceed"|"block", reason: str}``.
* ``parse_pretrade_verdict(raw)`` — the INV-02 safe-default boundary.  Any
  failure (invalid JSON, missing field, out-of-enum value, empty string, wrong
  type) returns ``decision="block"`` and logs at WARNING.  It **never raises**
  and **never returns** ``decision="proceed"`` on a parse/validation failure.
* ``pretrade_check(candidate, *, client=None) -> PretradeVerdict`` — builds the
  prompt from ``prompts/pretrade.md``, calls the LLM via an injectable ``client``
  adapter, and routes the raw response through ``parse_pretrade_verdict``.
  With ``client=None`` and no ``LLM_API_KEY`` set → returns the safe
  default ``block`` (testable and safe offline).

The asymmetry (INV-02):
    A false ``block`` costs an opportunity; a false ``proceed`` costs money.
    The parser is therefore the safe boundary — wrap everything in
    try/except → block default, not raise.

INV-01: this module returns a verdict only.  No order, execution, or risk
    function is imported or callable from here.
INV-08: the ``LLM_API_KEY`` is never logged.  ``OpenAICompatClient`` holds it
    privately and excludes it from ``repr``.

Provider-agnostic: ``OpenAICompatClient`` speaks the OpenAI chat-completions
    wire format, so any compatible endpoint works — OpenAI, Groq, NVIDIA NIM,
    OpenRouter, Gemini's compat layer, or a local Ollama — selected via
    ``LLM_BASE_URL`` + ``LLM_MODEL`` (or the ``Settings`` llm_* fields).

**Model constant (D-P3-E):** ``gpt-5-nano`` — cheapest reliable structured-JSON
    tier at the veto's call volume; override via ``LLM_MODEL``.

Offline testability: inject a stub via the ``client`` parameter.  Tests do not
    need a real API key; the live adapter is only exercised at the acceptance gate.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
from typing import Any, Literal, Optional, Protocol

import httpx
from pydantic import BaseModel, ValidationError

from signals.ranker import Candidate

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module constants (D-P3-E) — pinned defaults for the pre-trade veto LLM
# ---------------------------------------------------------------------------

MODEL: str = "gpt-5-nano"

#: Default OpenAI-compatible endpoint; any compatible provider works via
#: ``LLM_BASE_URL`` (e.g. https://api.groq.com/openai/v1, http://localhost:11434/v1).
DEFAULT_BASE_URL: str = "https://api.openai.com/v1"

#: HTTP timeout for the veto call — generous but bounded; a hang must not
#: stall the execute gate (the caller's except → block covers a timeout).
_HTTP_TIMEOUT_S: float = 30.0

# ---------------------------------------------------------------------------
# Prompt template path
# ---------------------------------------------------------------------------

_PROMPTS_DIR = pathlib.Path(__file__).parent / "prompts"
_PROMPT_PATH = _PROMPTS_DIR / "pretrade.md"

# ---------------------------------------------------------------------------
# Safe default (INV-02) — returned whenever parse_pretrade_verdict fails
# ---------------------------------------------------------------------------

_SAFE_DEFAULT_DECISION: Literal["block"] = "block"
_SAFE_DEFAULT_REASON: str = "unparseable response — defaulting to block (INV-02 safe abort)"


# ---------------------------------------------------------------------------
# PretradeVerdict pydantic model
# ---------------------------------------------------------------------------


class PretradeVerdict(BaseModel):
    """Structured pre-trade veto verdict returned by the LLM (in-process).

    Wire format (snake_case, exact enum spellings):
        ``{"decision": "proceed"|"block", "reason": "..."}``

    Pydantic v2 strict enum validation rejects any value outside the declared
    literals at construction time, satisfying the INV-02 strict-enum requirement.

    Attributes:
        decision: The veto verdict.
            - ``"proceed"`` — trade may continue to sizing/submission.
            - ``"block"``   — trade is aborted; this is the INV-02 safe default.
        reason: Human-readable explanation from the LLM (or from the safe-default
            factory when the parse boundary fails).
    """

    decision: Literal["proceed", "block"]
    reason: str

    model_config = {"extra": "forbid"}


# ---------------------------------------------------------------------------
# Safe-default factory
# ---------------------------------------------------------------------------


def _safe_default() -> PretradeVerdict:
    """Return the INV-02 safe default verdict (block)."""
    return PretradeVerdict(
        decision=_SAFE_DEFAULT_DECISION,
        reason=_SAFE_DEFAULT_REASON,
    )


# ---------------------------------------------------------------------------
# parse_pretrade_verdict — INV-02 enforcement boundary
# ---------------------------------------------------------------------------


def parse_pretrade_verdict(raw: str) -> PretradeVerdict:
    """Parse the LLM's JSON response into a ``PretradeVerdict``.

    This is the INV-02 enforcement boundary.  **On ANY failure** — invalid
    JSON, missing required field, out-of-enum value, empty string, wrong
    JSON type — returns the safe default
    ``PretradeVerdict(decision="block", reason="unparseable response — defaulting to block (INV-02 safe abort)")``
    and logs at WARNING level.  It **never raises** and **never returns**
    ``decision="proceed"`` on a failure path.

    Args:
        raw: The raw string returned by the LLM (expected to be a JSON object).

    Returns:
        A validated ``PretradeVerdict``.  On any failure, the safe-default
        ``block`` verdict is returned instead.

    Examples:
        >>> parse_pretrade_verdict('{"decision":"proceed","reason":"all checks pass"}')
        PretradeVerdict(decision='proceed', reason='all checks pass')

        >>> parse_pretrade_verdict("this is not json")
        PretradeVerdict(decision='block', reason='unparseable response — defaulting to block (INV-02 safe abort)')
    """
    # Guard: reject empty / whitespace-only input immediately.
    if not raw or not raw.strip():
        _log.warning(
            "parse_pretrade_verdict: empty response — returning safe default (INV-02)"
        )
        return _safe_default()

    try:
        data: Any = json.loads(raw)
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        _log.warning(
            "parse_pretrade_verdict: JSON decode failed (%s) — returning safe default (INV-02)",
            exc,
        )
        return _safe_default()

    # json.loads can return non-dict values (e.g. a bare string, list, or null).
    if not isinstance(data, dict):
        _log.warning(
            "parse_pretrade_verdict: expected a JSON object, got %s — returning safe default (INV-02)",
            type(data).__name__,
        )
        return _safe_default()

    try:
        verdict = PretradeVerdict.model_validate(data)
    except ValidationError as exc:
        _log.warning(
            "parse_pretrade_verdict: pydantic validation failed (%s) — returning safe default (INV-02)",
            exc,
        )
        return _safe_default()
    except Exception as exc:  # noqa: BLE001 — catch-all for absolute safety (INV-02)
        _log.warning(
            "parse_pretrade_verdict: unexpected error (%s: %s) — returning safe default (INV-02)",
            type(exc).__name__,
            exc,
        )
        return _safe_default()

    return verdict


# ---------------------------------------------------------------------------
# Client adapter protocol — injectable for tests
# ---------------------------------------------------------------------------


class _ClientAdapter(Protocol):
    """Thin, provider-agnostic adapter interface for the veto LLM.

    The live implementation (``OpenAICompatClient``) speaks the OpenAI
    chat-completions wire format over httpx.  Tests inject a stub that returns
    a fixed payload without any network call.  ``pretrade_check`` accepts any
    object satisfying this protocol via the ``client`` parameter.
    """

    def complete(self, prompt: str) -> str:
        """Send a text prompt to the model and return the raw text response.

        Args:
            prompt: The full prompt string.

        Returns:
            The raw assistant text.

        Raises:
            Any transport/protocol exception — callers must wrap in try/except
            and fall back to the safe default (INV-02).
        """
        ...


# ---------------------------------------------------------------------------
# Live adapter — OpenAI-compatible chat completions over httpx (INV-08)
# ---------------------------------------------------------------------------


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
            (the offline fail-closed path — INV-02).
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
                the INV-02 safe default.
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


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------


def _build_prompt(candidate: Candidate) -> str:
    """Render the prompt template with the candidate's facts.

    Reads ``prompts/pretrade.md`` once per call.  All candidate fields are
    substituted into ``{{placeholder}}`` slots.

    Args:
        candidate: The ranked ``Candidate`` to be vetted.

    Returns:
        The rendered prompt string ready for submission to the LLM.

    Raises:
        FileNotFoundError: if the prompt template is missing (programming error).
    """
    template = _PROMPT_PATH.read_text(encoding="utf-8")
    rendered = template.replace("{{instrument}}", candidate.instrument)
    rendered = rendered.replace("{{timeframe}}", candidate.timeframe)
    rendered = rendered.replace("{{strategy_name}}", candidate.strategy_name)
    rendered = rendered.replace("{{direction}}", candidate.direction)
    rendered = rendered.replace("{{entry_ref}}", str(candidate.entry_ref))
    rendered = rendered.replace("{{stop_distance}}", str(candidate.stop_distance))
    rendered = rendered.replace("{{target_distance}}", str(candidate.target_distance))
    rendered = rendered.replace("{{oos_sharpe_mean}}", str(candidate.oos_sharpe_mean))
    rendered = rendered.replace("{{quality_score}}", str(candidate.quality_score))
    rendered = rendered.replace("{{rank}}", str(candidate.rank))
    rendered = rendered.replace("{{spread_ok}}", str(candidate.spread_ok))
    rendered = rendered.replace("{{session_ok}}", str(candidate.session_ok))
    rendered = rendered.replace("{{news_flag}}", str(candidate.news_flag))
    rendered = rendered.replace("{{generated_at}}", candidate.generated_at)
    return rendered


# ---------------------------------------------------------------------------
# pretrade_check — public API
# ---------------------------------------------------------------------------


def pretrade_check(
    candidate: Candidate,
    *,
    client: _ClientAdapter | None = None,
) -> PretradeVerdict:
    """Run the pre-trade LLM veto for a ranked ``Candidate``.

    This is the INV-02 safe gate.  The live HTTP call is isolated in the
    ``OpenAICompatClient`` adapter so this function (and the whole module) is
    fully testable offline.  Inject a stub via ``client`` in tests.

    Algorithm:
        1. If no ``client`` is provided and ``LLM_API_KEY`` is not set,
           return the safe default ``block`` immediately (offline-safe).
        2. If no ``client`` is provided and a key is available, build the live
           client via ``OpenAICompatClient.from_env()``.
        3. Build the prompt from ``prompts/pretrade.md``.
        4. Call ``client.complete(prompt)`` — any SDK/network exception is
           caught, logged at WARNING, and returns the safe default ``block``.
        5. Route the raw response through ``parse_pretrade_verdict`` (the INV-02
           enforcement boundary).

    Args:
        candidate: The ranked ``Candidate`` to be vetted.
        client: An injectable adapter satisfying ``_ClientAdapter``.  Pass a
            stub in tests.  Defaults to ``None`` (auto-detect live vs offline).

    Returns:
        A ``PretradeVerdict``.  Always ``block`` on any failure path (INV-02).
    """
    # Step 1 — no client and no key → safe offline default (no crash, no key error).
    if client is None and not os.environ.get("LLM_API_KEY"):
        _log.warning(
            "pretrade_check: no client and LLM_API_KEY not set — "
            "returning safe default block (INV-02 offline path)"
        )
        return _safe_default()

    # Step 2 — build the live client if none injected.
    active_client: _ClientAdapter
    if client is None:
        try:
            from_env_client = OpenAICompatClient.from_env()
            if from_env_client is None:
                raise ValueError("LLM_API_KEY not set")
            active_client = from_env_client
        except Exception as exc:  # noqa: BLE001 — client construction failure
            _log.warning(
                "pretrade_check: failed to initialise live client (%s: %s) — "
                "returning safe default (INV-02)",
                type(exc).__name__,
                exc,
            )
            return _safe_default()
    else:
        active_client = client

    # Step 3 — build the prompt.
    try:
        prompt = _build_prompt(candidate)
    except Exception as exc:  # noqa: BLE001 — prompt template missing / IO error
        _log.warning(
            "pretrade_check: failed to build prompt (%s: %s) — "
            "returning safe default (INV-02)",
            type(exc).__name__,
            exc,
        )
        return _safe_default()

    # Step 4 — call the LLM.
    try:
        raw = active_client.complete(prompt)
    except Exception as exc:  # noqa: BLE001 — SDK/network error → safe default
        _log.warning(
            "pretrade_check: API call failed (%s: %s) — returning safe default (INV-02)",
            type(exc).__name__,
            exc,
        )
        return _safe_default()

    # Step 5 — parse through the INV-02 enforcement boundary.
    return parse_pretrade_verdict(raw)
