"""Tests for ai.news_risk — INV-02 enforcement.

Coverage:
    - NewsRiskVerdict: valid construction, field access, strict enum rejection.
    - parse_news_risk: well-formed JSON → valid verdict.
    - Malformed inputs → safe default (suggest_action="skip"), no exception,
      never "proceed" (INV-02 exhaustive battery).

No live Claude / Anthropic calls anywhere in this module.
"""

from __future__ import annotations

import json
import logging
import socket
from typing import Any

import pytest
from pydantic import ValidationError

from ai.news_risk import NewsRiskVerdict, news_risk_check, parse_news_risk
from signals.ranker import Candidate

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_GOOD_JSON_PROCEED = json.dumps(
    {
        "event_risk": "low",
        "reason": "No high-impact events in window",
        "suggest_action": "proceed",
    }
)

_GOOD_JSON_SKIP = json.dumps(
    {
        "event_risk": "high",
        "reason": "NFP release within 2 hours of entry window",
        "suggest_action": "skip",
    }
)

_GOOD_JSON_REDUCE = json.dumps(
    {
        "event_risk": "medium",
        "reason": "Medium-impact CPI near entry; reduce exposure",
        "suggest_action": "reduce_size",
    }
)

_SAFE_DEFAULT_REASON = "unparseable response — defaulting to skip"


def _is_safe_default(verdict: NewsRiskVerdict) -> bool:
    """Return True if verdict is the INV-02 safe default (skip)."""
    return (
        verdict.event_risk == "high"
        and verdict.suggest_action == "skip"
        and "defaulting to skip" in verdict.reason
    )


# ---------------------------------------------------------------------------
# NewsRiskVerdict — model validation
# ---------------------------------------------------------------------------


class TestNewsRiskVerdictModel:
    """NewsRiskVerdict field validation and strict enum enforcement."""

    def test_valid_proceed(self) -> None:
        v = NewsRiskVerdict(event_risk="low", reason="quiet week", suggest_action="proceed")
        assert v.event_risk == "low"
        assert v.suggest_action == "proceed"
        assert v.reason == "quiet week"

    def test_valid_skip(self) -> None:
        v = NewsRiskVerdict(event_risk="high", reason="NFP imminent", suggest_action="skip")
        assert v.event_risk == "high"
        assert v.suggest_action == "skip"

    def test_valid_reduce_size(self) -> None:
        v = NewsRiskVerdict(event_risk="medium", reason="CPI nearby", suggest_action="reduce_size")
        assert v.event_risk == "medium"
        assert v.suggest_action == "reduce_size"

    def test_all_event_risk_values(self) -> None:
        for risk in ("high", "medium", "low"):
            v = NewsRiskVerdict(event_risk=risk, reason="ok", suggest_action="proceed")
            assert v.event_risk == risk

    def test_all_suggest_action_values(self) -> None:
        for action in ("proceed", "reduce_size", "skip"):
            v = NewsRiskVerdict(event_risk="low", reason="ok", suggest_action=action)
            assert v.suggest_action == action

    def test_rejects_bad_event_risk_catastrophic(self) -> None:
        with pytest.raises(ValidationError):
            NewsRiskVerdict(event_risk="catastrophic", reason="x", suggest_action="skip")

    def test_rejects_bad_event_risk_critical(self) -> None:
        with pytest.raises(ValidationError):
            NewsRiskVerdict(event_risk="critical", reason="x", suggest_action="skip")

    def test_rejects_bad_event_risk_none(self) -> None:
        with pytest.raises(ValidationError):
            NewsRiskVerdict(event_risk=None, reason="x", suggest_action="skip")

    def test_rejects_bad_suggest_action_trade(self) -> None:
        with pytest.raises(ValidationError):
            NewsRiskVerdict(event_risk="low", reason="x", suggest_action="trade")

    def test_rejects_bad_suggest_action_allow(self) -> None:
        with pytest.raises(ValidationError):
            NewsRiskVerdict(event_risk="low", reason="x", suggest_action="allow")

    def test_rejects_missing_event_risk(self) -> None:
        with pytest.raises(ValidationError):
            NewsRiskVerdict(reason="x", suggest_action="skip")  # type: ignore[call-arg]

    def test_rejects_missing_suggest_action(self) -> None:
        with pytest.raises(ValidationError):
            NewsRiskVerdict(event_risk="low", reason="x")  # type: ignore[call-arg]

    def test_rejects_missing_reason(self) -> None:
        with pytest.raises(ValidationError):
            NewsRiskVerdict(event_risk="low", suggest_action="proceed")  # type: ignore[call-arg]

    def test_rejects_extra_fields(self) -> None:
        with pytest.raises(ValidationError):
            NewsRiskVerdict(
                event_risk="low",
                reason="x",
                suggest_action="proceed",
                extra_field="unexpected",  # type: ignore[call-arg]
            )


# ---------------------------------------------------------------------------
# parse_news_risk — well-formed input
# ---------------------------------------------------------------------------


class TestParseNewsRiskWellFormed:
    """parse_news_risk returns a valid verdict for well-formed Claude JSON."""

    def test_proceed_verdict(self) -> None:
        v = parse_news_risk(_GOOD_JSON_PROCEED)
        assert v.event_risk == "low"
        assert v.suggest_action == "proceed"
        assert v.reason == "No high-impact events in window"

    def test_skip_verdict(self) -> None:
        v = parse_news_risk(_GOOD_JSON_SKIP)
        assert v.event_risk == "high"
        assert v.suggest_action == "skip"

    def test_reduce_size_verdict(self) -> None:
        v = parse_news_risk(_GOOD_JSON_REDUCE)
        assert v.event_risk == "medium"
        assert v.suggest_action == "reduce_size"

    def test_returns_news_risk_verdict_type(self) -> None:
        v = parse_news_risk(_GOOD_JSON_PROCEED)
        assert isinstance(v, NewsRiskVerdict)

    def test_all_event_risk_values_round_trip(self) -> None:
        for risk in ("high", "medium", "low"):
            raw = json.dumps(
                {"event_risk": risk, "reason": "test", "suggest_action": "proceed"}
            )
            v = parse_news_risk(raw)
            assert v.event_risk == risk

    def test_all_suggest_action_values_round_trip(self) -> None:
        for action in ("proceed", "reduce_size", "skip"):
            raw = json.dumps(
                {"event_risk": "low", "reason": "test", "suggest_action": action}
            )
            v = parse_news_risk(raw)
            assert v.suggest_action == action


# ---------------------------------------------------------------------------
# parse_news_risk — malformed input → safe default (INV-02 exhaustive battery)
# ---------------------------------------------------------------------------


class TestParseNewsRiskMalformedInputs:
    """Each malformed input must: return safe default, never raise, never proceed (INV-02)."""

    # -- Invalid JSON --

    def test_invalid_json_string(self) -> None:
        v = parse_news_risk("this is not json")
        assert _is_safe_default(v)
        assert v.suggest_action == "skip"

    def test_invalid_json_partial(self) -> None:
        v = parse_news_risk('{"event_risk": "high"')  # truncated
        assert _is_safe_default(v)
        assert v.suggest_action == "skip"

    def test_invalid_json_prose_response(self) -> None:
        v = parse_news_risk("The news risk is high because of the upcoming NFP.")
        assert _is_safe_default(v)
        assert v.suggest_action == "skip"

    def test_invalid_json_markdown_wrapped(self) -> None:
        # Claude sometimes wraps in code fences despite instructions.
        v = parse_news_risk(
            '```json\n{"event_risk":"low","reason":"ok","suggest_action":"proceed"}\n```'
        )
        assert _is_safe_default(v)
        assert v.suggest_action == "skip"

    # -- Empty / whitespace --

    def test_empty_string(self) -> None:
        v = parse_news_risk("")
        assert _is_safe_default(v)
        assert v.suggest_action == "skip"

    def test_whitespace_only(self) -> None:
        v = parse_news_risk("   \n\t  ")
        assert _is_safe_default(v)
        assert v.suggest_action == "skip"

    # -- Missing required fields --

    def test_missing_suggest_action(self) -> None:
        raw = json.dumps({"event_risk": "low", "reason": "quiet week"})
        v = parse_news_risk(raw)
        assert _is_safe_default(v)
        assert v.suggest_action == "skip"

    def test_missing_event_risk(self) -> None:
        raw = json.dumps({"reason": "quiet week", "suggest_action": "proceed"})
        v = parse_news_risk(raw)
        assert _is_safe_default(v)
        assert v.suggest_action == "skip"

    def test_missing_reason(self) -> None:
        raw = json.dumps({"event_risk": "low", "suggest_action": "proceed"})
        v = parse_news_risk(raw)
        assert _is_safe_default(v)
        assert v.suggest_action == "skip"

    def test_missing_all_fields(self) -> None:
        v = parse_news_risk("{}")
        assert _is_safe_default(v)
        assert v.suggest_action == "skip"

    # -- Out-of-enum values --

    def test_bad_event_risk_catastrophic(self) -> None:
        raw = json.dumps(
            {"event_risk": "catastrophic", "reason": "doom", "suggest_action": "skip"}
        )
        v = parse_news_risk(raw)
        assert _is_safe_default(v)
        assert v.suggest_action == "skip"

    def test_bad_event_risk_extreme(self) -> None:
        raw = json.dumps(
            {"event_risk": "extreme", "reason": "doom", "suggest_action": "skip"}
        )
        v = parse_news_risk(raw)
        assert _is_safe_default(v)
        assert v.suggest_action == "skip"

    def test_bad_suggest_action_trade(self) -> None:
        raw = json.dumps(
            {"event_risk": "low", "reason": "ok", "suggest_action": "trade"}
        )
        v = parse_news_risk(raw)
        assert _is_safe_default(v)
        assert v.suggest_action == "skip"

    def test_bad_suggest_action_allow(self) -> None:
        raw = json.dumps(
            {"event_risk": "low", "reason": "ok", "suggest_action": "allow"}
        )
        v = parse_news_risk(raw)
        assert _is_safe_default(v)
        assert v.suggest_action == "skip"

    def test_bad_suggest_action_go(self) -> None:
        raw = json.dumps(
            {"event_risk": "low", "reason": "ok", "suggest_action": "go"}
        )
        v = parse_news_risk(raw)
        assert _is_safe_default(v)
        assert v.suggest_action == "skip"

    # -- Wrong JSON types --

    def test_json_array_not_object(self) -> None:
        raw = json.dumps(
            [{"event_risk": "low", "reason": "ok", "suggest_action": "proceed"}]
        )
        v = parse_news_risk(raw)
        assert _is_safe_default(v)
        assert v.suggest_action == "skip"

    def test_json_null(self) -> None:
        v = parse_news_risk("null")
        assert _is_safe_default(v)
        assert v.suggest_action == "skip"

    def test_json_bare_string(self) -> None:
        v = parse_news_risk('"proceed"')
        assert _is_safe_default(v)
        assert v.suggest_action == "skip"

    def test_json_number(self) -> None:
        v = parse_news_risk("42")
        assert _is_safe_default(v)
        assert v.suggest_action == "skip"

    def test_json_boolean(self) -> None:
        v = parse_news_risk("true")
        assert _is_safe_default(v)
        assert v.suggest_action == "skip"

    def test_event_risk_is_null(self) -> None:
        raw = json.dumps({"event_risk": None, "reason": "ok", "suggest_action": "proceed"})
        v = parse_news_risk(raw)
        assert _is_safe_default(v)
        assert v.suggest_action == "skip"

    def test_suggest_action_is_null(self) -> None:
        raw = json.dumps({"event_risk": "low", "reason": "ok", "suggest_action": None})
        v = parse_news_risk(raw)
        assert _is_safe_default(v)
        assert v.suggest_action == "skip"

    # -- Extra fields (model forbids extras) --

    def test_extra_field_rejected(self) -> None:
        raw = json.dumps(
            {
                "event_risk": "low",
                "reason": "ok",
                "suggest_action": "proceed",
                "confidence": 0.9,
            }
        )
        v = parse_news_risk(raw)
        assert _is_safe_default(v)
        assert v.suggest_action == "skip"

    # -- Low-confidence / ambiguous prose from Claude --

    def test_ambiguous_text_response(self) -> None:
        v = parse_news_risk(
            "I'm not sure about the risk level. It could be high or medium "
            "depending on the market reaction."
        )
        assert _is_safe_default(v)
        assert v.suggest_action == "skip"

    # -- INV-02 core invariant: never "proceed" on parse failure --

    def test_never_returns_proceed_on_failure(self) -> None:
        """Malformed inputs must never return suggest_action='proceed' (INV-02)."""
        bad_inputs = [
            "",
            "not json",
            "null",
            "[]",
            "{}",
            '{"event_risk":"catastrophic","reason":"x","suggest_action":"skip"}',
            '{"event_risk":"low","reason":"x","suggest_action":"trade"}',
            '{"reason":"x","suggest_action":"proceed"}',
        ]
        for raw in bad_inputs:
            v = parse_news_risk(raw)
            assert v.suggest_action != "proceed", (
                f"INV-02 violated: parse_news_risk returned 'proceed' for bad input: {raw!r}"
            )

    # -- No exception raised (INV-02) --

    def test_never_raises_on_any_input(self) -> None:
        """parse_news_risk must never raise, regardless of input (INV-02)."""
        bad_inputs = [
            "",
            "not json",
            '{"event_risk":"catastrophic","reason":"x","suggest_action":"skip"}',
            '{"reason":"x","suggest_action":"proceed"}',
            "null",
            "[]",
            '{"event_risk": "low"}',
            "true",
            "42",
            '"a bare string"',
        ]
        for raw in bad_inputs:
            try:
                parse_news_risk(raw)
            except Exception as exc:  # noqa: BLE001
                pytest.fail(
                    f"parse_news_risk raised {type(exc).__name__} for input {raw!r}: {exc}"
                )


# ---------------------------------------------------------------------------
# parse_news_risk — logging behaviour
# ---------------------------------------------------------------------------


class TestParseNewsRiskLogging:
    """Failures are logged at WARNING; no secrets leak."""

    def test_logs_warning_on_invalid_json(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="ai.news_risk"):
            parse_news_risk("not json at all")
        assert any("safe default" in r.message.lower() for r in caplog.records)

    def test_logs_warning_on_empty(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="ai.news_risk"):
            parse_news_risk("")
        assert any("safe default" in r.message.lower() for r in caplog.records)

    def test_logs_warning_on_bad_enum(self, caplog: pytest.LogCaptureFixture) -> None:
        raw = json.dumps(
            {"event_risk": "catastrophic", "reason": "x", "suggest_action": "skip"}
        )
        with caplog.at_level(logging.WARNING, logger="ai.news_risk"):
            parse_news_risk(raw)
        assert any("safe default" in r.message.lower() for r in caplog.records)

    def test_no_secret_token_in_logs(self, caplog: pytest.LogCaptureFixture) -> None:
        """INV-08: no secret values appear in log output."""
        sensitive = "sk-live-VERY_SECRET_TOKEN_12345"
        with caplog.at_level(logging.DEBUG, logger="ai.news_risk"):
            parse_news_risk(sensitive)
        for record in caplog.records:
            assert sensitive not in record.message, (
                "INV-08: a secret token appeared in the log output"
            )


# ---------------------------------------------------------------------------
# news_risk_check — AC 3 (one table: offline / stub placeholders / fail-closed)
# ---------------------------------------------------------------------------

_CALENDAR_TEXT = "2026-09-20T12:30:00Z USD high NFP"
_ENTRY_WINDOW = "2026-09-20T10:00:00Z/2026-09-20T14:00:00Z"
_PLACEHOLDERS = (
    "{{instrument}}",
    "{{base_currency}}",
    "{{quote_currency}}",
    "{{direction}}",
    "{{entry_window_utc}}",
    "{{calendar_events}}",
)


def _candidate(**overrides: Any) -> Candidate:
    defaults: dict[str, Any] = {
        "instrument": "EUR_USD",
        "timeframe": "H1",
        "strategy_name": "macrossover_10_50",
        "direction": "LONG",
        "entry_ref": 1.0850,
        "stop_distance": 0.0030,
        "target_distance": 0.0045,
        "oos_sharpe_mean": 0.42,
        "quality_score": 0.75,
        "rank": 1,
        "spread_ok": True,
        "session_ok": True,
        "news_flag": False,
        "generated_at": "2026-05-29T10:00:00Z",
    }
    defaults.update(overrides)
    return Candidate(**defaults)


class _CapturingStub:
    def __init__(self, raw: str) -> None:
        self._raw = raw
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self._raw


class _RaisingStub:
    def complete(self, prompt: str) -> str:  # noqa: ARG002
        raise RuntimeError("simulated transport error")


@pytest.mark.parametrize(
    "label",
    ["no_client_no_key", "stub_six_placeholders", "transport_or_parse_failure"],
)
def test_news_risk_check_offline_paths(
    label: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 3: no-key skip (zero I/O); stub fills six placeholders; failure → skip."""
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    candidate = _candidate()
    proceed = json.dumps(
        {
            "event_risk": "low",
            "reason": "quiet calendar",
            "suggest_action": "proceed",
        }
    )

    sock_calls = {"n": 0}

    class _GuardedSocket(socket.socket):
        def __init__(self, *args: Any, **kw: Any) -> None:
            sock_calls["n"] += 1
            raise AssertionError("network I/O on news_risk offline path")

    if label == "no_client_no_key":
        monkeypatch.setattr(socket, "socket", _GuardedSocket)

        class _NoHttpx:
            def __init__(self, *args: Any, **kw: Any) -> None:
                raise AssertionError("httpx Client constructed")

        monkeypatch.setattr("httpx.Client", _NoHttpx)
        v = news_risk_check(
            candidate, _CALENDAR_TEXT, _ENTRY_WINDOW, client=None
        )
        assert v.suggest_action == "skip"
        assert v.event_risk == "high"
        assert sock_calls["n"] == 0
        return

    if label == "stub_six_placeholders":
        stub = _CapturingStub(proceed)
        v = news_risk_check(
            candidate, _CALENDAR_TEXT, _ENTRY_WINDOW, client=stub
        )
        assert v.suggest_action == "proceed"
        assert stub.prompts, "complete was not called"
        prompt = stub.prompts[0]
        for leftover in _PLACEHOLDERS:
            assert leftover not in prompt
        assert "EUR_USD" in prompt
        assert "EUR" in prompt
        assert "USD" in prompt
        assert candidate.direction in prompt
        assert _ENTRY_WINDOW in prompt
        assert _CALENDAR_TEXT in prompt
        return

    # transport + parse failure both route to parse_news_risk / skip default
    transport = news_risk_check(
        candidate, _CALENDAR_TEXT, _ENTRY_WINDOW, client=_RaisingStub()
    )
    parsed = news_risk_check(
        candidate,
        _CALENDAR_TEXT,
        _ENTRY_WINDOW,
        client=_CapturingStub("not json"),
    )
    assert transport.suggest_action == "skip"
    assert parsed.suggest_action == "skip"
    assert transport.event_risk == "high"
    assert parsed.event_risk == "high"


def test_news_risk_check_key_absent_from_logs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """AC 6: LLM_API_KEY never appears in news_risk_check logs or verdict repr."""
    secret = "sk-super-secret-news-risk"
    monkeypatch.setenv("LLM_API_KEY", secret)
    with caplog.at_level(logging.DEBUG, logger="ai.news_risk"):
        v = news_risk_check(
            _candidate(),
            _CALENDAR_TEXT,
            _ENTRY_WINDOW,
            client=_RaisingStub(),
        )
    blob = " ".join(r.message for r in caplog.records) + repr(v)
    assert secret not in blob
