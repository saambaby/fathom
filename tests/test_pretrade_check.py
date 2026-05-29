"""Tests for hermes_integration.pretrade_check — INV-02 enforcement.

Coverage:
    - PretradeVerdict: valid construction, field access, strict enum rejection.
    - parse_pretrade_verdict: well-formed JSON → valid verdict.
    - Malformed inputs → safe default (decision="block"), no exception,
      never "proceed" (INV-02 exhaustive battery).
    - pretrade_check with an injected stub client routes through the parser.
    - No client + no key → safe-default block (offline path).
    - No order/execution/risk imports callable from this module (INV-01).

No live Claude / Anthropic calls anywhere in this module.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from hermes_integration.pretrade_check import (
    PretradeVerdict,
    _safe_default,
    parse_pretrade_verdict,
    pretrade_check,
)
from signals.ranker import Candidate

# ---------------------------------------------------------------------------
# Helpers and fixtures
# ---------------------------------------------------------------------------

_SAFE_DEFAULT_REASON_SUBSTR = "defaulting to block"


def _is_safe_default(verdict: PretradeVerdict) -> bool:
    """Return True if verdict is the INV-02 safe default (block)."""
    return verdict.decision == "block" and _SAFE_DEFAULT_REASON_SUBSTR in verdict.reason


def _make_candidate(**overrides: Any) -> Candidate:
    """Build a minimal valid Candidate for testing."""
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


class _StubClient:
    """Stub adapter: returns a fixed raw response without any network call."""

    def __init__(self, raw_response: str) -> None:
        self._raw = raw_response

    def complete(self, prompt: str) -> str:  # noqa: ARG002
        return self._raw


class _RaisingClient:
    """Stub adapter: raises an exception to simulate SDK/network failure."""

    def complete(self, prompt: str) -> str:  # noqa: ARG002
        raise RuntimeError("simulated SDK error")


# ---------------------------------------------------------------------------
# PretradeVerdict — model validation
# ---------------------------------------------------------------------------


class TestPretradeVerdictModel:
    """PretradeVerdict field validation and strict enum enforcement."""

    def test_valid_proceed(self) -> None:
        v = PretradeVerdict(decision="proceed", reason="all checks pass")
        assert v.decision == "proceed"
        assert v.reason == "all checks pass"

    def test_valid_block(self) -> None:
        v = PretradeVerdict(decision="block", reason="stop is zero")
        assert v.decision == "block"
        assert v.reason == "stop is zero"

    def test_rejects_bad_decision_go(self) -> None:
        with pytest.raises(ValidationError):
            PretradeVerdict.model_validate({"decision": "go", "reason": "ok"})

    def test_rejects_bad_decision_trade(self) -> None:
        with pytest.raises(ValidationError):
            PretradeVerdict.model_validate({"decision": "trade", "reason": "ok"})

    def test_rejects_bad_decision_allow(self) -> None:
        with pytest.raises(ValidationError):
            PretradeVerdict.model_validate({"decision": "allow", "reason": "ok"})

    def test_rejects_bad_decision_none(self) -> None:
        with pytest.raises(ValidationError):
            PretradeVerdict.model_validate({"decision": None, "reason": "ok"})

    def test_rejects_missing_decision(self) -> None:
        with pytest.raises(ValidationError):
            PretradeVerdict(reason="ok")  # type: ignore[call-arg]

    def test_rejects_missing_reason(self) -> None:
        with pytest.raises(ValidationError):
            PretradeVerdict(decision="proceed")  # type: ignore[call-arg]

    def test_rejects_extra_fields(self) -> None:
        with pytest.raises(ValidationError):
            PretradeVerdict(
                decision="proceed",
                reason="ok",
                confidence=0.9,  # type: ignore[call-arg]
            )


# ---------------------------------------------------------------------------
# _safe_default — factory
# ---------------------------------------------------------------------------


class TestSafeDefault:
    """_safe_default() returns a stable INV-02 block verdict."""

    def test_returns_pretrade_verdict(self) -> None:
        v = _safe_default()
        assert isinstance(v, PretradeVerdict)

    def test_decision_is_block(self) -> None:
        v = _safe_default()
        assert v.decision == "block"

    def test_reason_contains_abort_phrase(self) -> None:
        v = _safe_default()
        assert _SAFE_DEFAULT_REASON_SUBSTR in v.reason


# ---------------------------------------------------------------------------
# parse_pretrade_verdict — well-formed input
# ---------------------------------------------------------------------------


class TestParsePretradeVerdictWellFormed:
    """parse_pretrade_verdict returns a valid verdict for well-formed Claude JSON."""

    def test_proceed_verdict(self) -> None:
        raw = json.dumps({"decision": "proceed", "reason": "all checks pass"})
        v = parse_pretrade_verdict(raw)
        assert v.decision == "proceed"
        assert v.reason == "all checks pass"

    def test_block_verdict(self) -> None:
        raw = json.dumps({"decision": "block", "reason": "stop distance is zero"})
        v = parse_pretrade_verdict(raw)
        assert v.decision == "block"
        assert v.reason == "stop distance is zero"

    def test_returns_pretrade_verdict_type(self) -> None:
        raw = json.dumps({"decision": "proceed", "reason": "ok"})
        v = parse_pretrade_verdict(raw)
        assert isinstance(v, PretradeVerdict)

    def test_both_decision_values_round_trip(self) -> None:
        for decision in ("proceed", "block"):
            raw = json.dumps({"decision": decision, "reason": "test"})
            v = parse_pretrade_verdict(raw)
            assert v.decision == decision


# ---------------------------------------------------------------------------
# parse_pretrade_verdict — malformed input → safe default (INV-02 exhaustive)
# ---------------------------------------------------------------------------


class TestParsePretradeVerdictMalformedInputs:
    """Each malformed input must: return safe default, never raise, never proceed (INV-02)."""

    # -- Invalid JSON --

    def test_invalid_json_string(self) -> None:
        v = parse_pretrade_verdict("this is not json")
        assert _is_safe_default(v)
        assert v.decision == "block"

    def test_invalid_json_partial(self) -> None:
        v = parse_pretrade_verdict('{"decision": "proceed"')  # truncated
        assert _is_safe_default(v)

    def test_invalid_json_prose_response(self) -> None:
        v = parse_pretrade_verdict("The trade looks fine to me, you should proceed.")
        assert _is_safe_default(v)

    def test_invalid_json_markdown_wrapped(self) -> None:
        # Claude sometimes wraps in code fences despite instructions.
        v = parse_pretrade_verdict(
            '```json\n{"decision":"proceed","reason":"ok"}\n```'
        )
        assert _is_safe_default(v)

    # -- Empty / whitespace --

    def test_empty_string(self) -> None:
        v = parse_pretrade_verdict("")
        assert _is_safe_default(v)

    def test_whitespace_only(self) -> None:
        v = parse_pretrade_verdict("   \n\t  ")
        assert _is_safe_default(v)

    # -- Missing required fields --

    def test_missing_decision(self) -> None:
        raw = json.dumps({"reason": "all looks fine"})
        v = parse_pretrade_verdict(raw)
        assert _is_safe_default(v)

    def test_missing_reason(self) -> None:
        raw = json.dumps({"decision": "proceed"})
        v = parse_pretrade_verdict(raw)
        assert _is_safe_default(v)

    def test_missing_all_fields(self) -> None:
        v = parse_pretrade_verdict("{}")
        assert _is_safe_default(v)

    # -- Out-of-enum decision values --

    def test_bad_decision_go(self) -> None:
        raw = json.dumps({"decision": "go", "reason": "looks good"})
        v = parse_pretrade_verdict(raw)
        assert _is_safe_default(v)

    def test_bad_decision_trade(self) -> None:
        raw = json.dumps({"decision": "trade", "reason": "fine"})
        v = parse_pretrade_verdict(raw)
        assert _is_safe_default(v)

    def test_bad_decision_allow(self) -> None:
        raw = json.dumps({"decision": "allow", "reason": "ok"})
        v = parse_pretrade_verdict(raw)
        assert _is_safe_default(v)

    def test_bad_decision_yes(self) -> None:
        raw = json.dumps({"decision": "yes", "reason": "ok"})
        v = parse_pretrade_verdict(raw)
        assert _is_safe_default(v)

    def test_bad_decision_approve(self) -> None:
        raw = json.dumps({"decision": "approve", "reason": "ok"})
        v = parse_pretrade_verdict(raw)
        assert _is_safe_default(v)

    # -- Wrong JSON types --

    def test_json_array_not_object(self) -> None:
        raw = json.dumps([{"decision": "proceed", "reason": "ok"}])
        v = parse_pretrade_verdict(raw)
        assert _is_safe_default(v)

    def test_json_null(self) -> None:
        v = parse_pretrade_verdict("null")
        assert _is_safe_default(v)

    def test_json_bare_string(self) -> None:
        v = parse_pretrade_verdict('"proceed"')
        assert _is_safe_default(v)

    def test_json_number(self) -> None:
        v = parse_pretrade_verdict("42")
        assert _is_safe_default(v)

    def test_json_boolean(self) -> None:
        v = parse_pretrade_verdict("true")
        assert _is_safe_default(v)

    def test_decision_is_null(self) -> None:
        raw = json.dumps({"decision": None, "reason": "ok"})
        v = parse_pretrade_verdict(raw)
        assert _is_safe_default(v)

    def test_reason_missing_decision_null(self) -> None:
        raw = json.dumps({"decision": None, "reason": None})
        v = parse_pretrade_verdict(raw)
        assert _is_safe_default(v)

    # -- Extra fields (model forbids extras) --

    def test_extra_field_rejected(self) -> None:
        raw = json.dumps(
            {"decision": "proceed", "reason": "ok", "confidence": 0.9}
        )
        v = parse_pretrade_verdict(raw)
        assert _is_safe_default(v)

    # -- INV-02 core invariant: never "proceed" on parse failure --

    def test_never_returns_proceed_on_failure(self) -> None:
        """Malformed inputs must never return decision='proceed' (INV-02)."""
        bad_inputs = [
            "",
            "not json",
            "null",
            "[]",
            "{}",
            '{"decision":"go","reason":"ok"}',
            '{"decision":"trade","reason":"ok"}',
            '{"reason":"ok"}',
            '{"decision":"proceed"}',  # missing reason
        ]
        for raw in bad_inputs:
            v = parse_pretrade_verdict(raw)
            assert v.decision != "proceed", (
                f"INV-02 violated: parse_pretrade_verdict returned 'proceed' "
                f"for bad input: {raw!r}"
            )

    # -- No exception raised (INV-02) --

    def test_never_raises_on_any_input(self) -> None:
        """parse_pretrade_verdict must never raise, regardless of input (INV-02)."""
        bad_inputs = [
            "",
            "not json",
            '{"decision":"go","reason":"x"}',
            '{"reason":"ok"}',
            "null",
            "[]",
            '{"decision": "proceed"}',
            "true",
            "42",
            '"a bare string"',
        ]
        for raw in bad_inputs:
            try:
                parse_pretrade_verdict(raw)
            except Exception as exc:  # noqa: BLE001
                pytest.fail(
                    f"parse_pretrade_verdict raised {type(exc).__name__} "
                    f"for input {raw!r}: {exc}"
                )


# ---------------------------------------------------------------------------
# parse_pretrade_verdict — logging behaviour
# ---------------------------------------------------------------------------


class TestParsePretradeVerdictLogging:
    """Failures are logged at WARNING; no secrets leak (INV-08)."""

    def test_logs_warning_on_invalid_json(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="hermes_integration.pretrade_check"):
            parse_pretrade_verdict("not json at all")
        assert any("safe default" in r.message.lower() for r in caplog.records)

    def test_logs_warning_on_empty(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="hermes_integration.pretrade_check"):
            parse_pretrade_verdict("")
        assert any("safe default" in r.message.lower() for r in caplog.records)

    def test_logs_warning_on_bad_enum(self, caplog: pytest.LogCaptureFixture) -> None:
        raw = json.dumps({"decision": "go", "reason": "x"})
        with caplog.at_level(logging.WARNING, logger="hermes_integration.pretrade_check"):
            parse_pretrade_verdict(raw)
        assert any("safe default" in r.message.lower() for r in caplog.records)

    def test_no_secret_token_in_logs(self, caplog: pytest.LogCaptureFixture) -> None:
        """INV-08: no secret values appear in log output."""
        sensitive = "sk-ant-VERY_SECRET_TOKEN_12345"
        with caplog.at_level(logging.DEBUG, logger="hermes_integration.pretrade_check"):
            parse_pretrade_verdict(sensitive)
        for record in caplog.records:
            assert sensitive not in record.message, (
                "INV-08: a secret token appeared in the log output"
            )


# ---------------------------------------------------------------------------
# pretrade_check — stub-client routing (offline, no API key)
# ---------------------------------------------------------------------------


class TestPretradeCheckStubClient:
    """pretrade_check with an injected stub routes through parse_pretrade_verdict."""

    def test_stub_returning_proceed_routes_to_proceed(self) -> None:
        raw = json.dumps({"decision": "proceed", "reason": "all checks pass"})
        stub = _StubClient(raw)
        candidate = _make_candidate()
        v = pretrade_check(candidate, client=stub)
        assert v.decision == "proceed"
        assert v.reason == "all checks pass"

    def test_stub_returning_block_routes_to_block(self) -> None:
        raw = json.dumps({"decision": "block", "reason": "stop is zero"})
        stub = _StubClient(raw)
        candidate = _make_candidate()
        v = pretrade_check(candidate, client=stub)
        assert v.decision == "block"
        assert v.reason == "stop is zero"

    def test_stub_returning_malformed_json_gives_safe_default(self) -> None:
        stub = _StubClient("not valid json at all")
        candidate = _make_candidate()
        v = pretrade_check(candidate, client=stub)
        assert _is_safe_default(v)

    def test_stub_returning_empty_gives_safe_default(self) -> None:
        stub = _StubClient("")
        candidate = _make_candidate()
        v = pretrade_check(candidate, client=stub)
        assert _is_safe_default(v)

    def test_stub_returning_bad_enum_gives_safe_default(self) -> None:
        raw = json.dumps({"decision": "go", "reason": "fine"})
        stub = _StubClient(raw)
        candidate = _make_candidate()
        v = pretrade_check(candidate, client=stub)
        assert _is_safe_default(v)

    def test_stub_returning_extra_field_gives_safe_default(self) -> None:
        raw = json.dumps({"decision": "proceed", "reason": "ok", "confidence": 0.9})
        stub = _StubClient(raw)
        candidate = _make_candidate()
        v = pretrade_check(candidate, client=stub)
        assert _is_safe_default(v)

    def test_returns_pretrade_verdict_type(self) -> None:
        raw = json.dumps({"decision": "proceed", "reason": "ok"})
        stub = _StubClient(raw)
        candidate = _make_candidate()
        v = pretrade_check(candidate, client=stub)
        assert isinstance(v, PretradeVerdict)

    def test_raising_client_gives_safe_default(self) -> None:
        """SDK/network failure → safe default block (INV-02)."""
        candidate = _make_candidate()
        v = pretrade_check(candidate, client=_RaisingClient())
        assert _is_safe_default(v)

    def test_raising_client_never_raises(self) -> None:
        """pretrade_check must never propagate SDK exceptions (INV-02)."""
        candidate = _make_candidate()
        try:
            pretrade_check(candidate, client=_RaisingClient())
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"pretrade_check raised {type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# pretrade_check — no client + no key → offline safe default
# ---------------------------------------------------------------------------


class TestPretradeCheckOfflinePath:
    """With no client and no ANTHROPIC_API_KEY, pretrade_check returns block."""

    def test_no_client_no_key_returns_block(self) -> None:
        candidate = _make_candidate()
        with patch.dict(os.environ, {}, clear=False):
            # Ensure key is absent for this test.
            env_backup = os.environ.pop("ANTHROPIC_API_KEY", None)
            try:
                v = pretrade_check(candidate, client=None)
                assert _is_safe_default(v)
                assert v.decision == "block"
            finally:
                if env_backup is not None:
                    os.environ["ANTHROPIC_API_KEY"] = env_backup

    def test_no_client_no_key_never_raises(self) -> None:
        candidate = _make_candidate()
        env_backup = os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            try:
                pretrade_check(candidate, client=None)
            except Exception as exc:  # noqa: BLE001
                pytest.fail(f"pretrade_check raised {type(exc).__name__}: {exc}")
        finally:
            if env_backup is not None:
                os.environ["ANTHROPIC_API_KEY"] = env_backup

    def test_no_client_no_key_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        candidate = _make_candidate()
        env_backup = os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            with caplog.at_level(
                logging.WARNING, logger="hermes_integration.pretrade_check"
            ):
                pretrade_check(candidate, client=None)
            assert any(
                "safe default" in r.message.lower() or "no client" in r.message.lower()
                for r in caplog.records
            )
        finally:
            if env_backup is not None:
                os.environ["ANTHROPIC_API_KEY"] = env_backup


# ---------------------------------------------------------------------------
# INV-01: no order/execution/risk imports from this module
# ---------------------------------------------------------------------------


class TestModuleIsolation:
    """No order, execution, or risk function is importable from pretrade_check (INV-01)."""

    def test_no_execution_import(self) -> None:
        import hermes_integration.pretrade_check as mod

        assert not hasattr(mod, "execution"), (
            "INV-01: execution module exposed from pretrade_check"
        )

    def test_no_orders_import(self) -> None:
        import hermes_integration.pretrade_check as mod

        assert not hasattr(mod, "orders"), (
            "INV-01: orders callable exposed from pretrade_check"
        )

    def test_no_risk_import(self) -> None:
        import hermes_integration.pretrade_check as mod

        assert not hasattr(mod, "risk"), (
            "INV-01: risk module exposed from pretrade_check"
        )

    def test_no_sizing_import(self) -> None:
        import hermes_integration.pretrade_check as mod

        assert not hasattr(mod, "sizing"), (
            "INV-01: sizing callable exposed from pretrade_check"
        )

    def test_only_expected_public_names(self) -> None:
        """Public API is limited to: PretradeVerdict, parse_pretrade_verdict,
        pretrade_check, MODEL.  No order/risk/execution names present."""
        import hermes_integration.pretrade_check as mod

        public_names = {n for n in dir(mod) if not n.startswith("_")}
        # These must be present
        assert "PretradeVerdict" in public_names
        assert "parse_pretrade_verdict" in public_names
        assert "pretrade_check" in public_names
        assert "MODEL" in public_names
        # These must NOT be present
        forbidden = {"place_order", "submit_order", "size_position", "kill_switch"}
        overlap = public_names & forbidden
        assert not overlap, f"INV-01: forbidden names exposed: {overlap}"
