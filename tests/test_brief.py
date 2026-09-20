"""Tests for ai.brief — advisory session analysis (market-brief, phase-07 T4).

One test per spec AC. Fail-safe variants are a single table (AC 2).
"""

from __future__ import annotations

import ast
import json
import logging
import socket
from pathlib import Path
from typing import Any

import pytest

from ai.brief import (
    MarketBrief,
    SessionAnalysis,
    SessionVerdict,
    parse_session_analysis,
    session_analysis,
)

_REPO_ROOT = Path(__file__).parent.parent.resolve()
_FALLBACK_TEXT = "analysis unavailable"
_CANDIDATES = "1. EUR_USD H1 BollingerReversion LONG\n2. GBP_USD D Donchian SHORT"
_CALENDAR = "2026-09-20T12:30:00Z USD high NFP"
_STATS = "EUR_USD close=1.08 ATR=0.0012\nGBP_USD close=1.27 ATR=0.0020"


def _valid_payload(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "brief": {
            "summary": "Quiet London open.",
            "landmines": ["USD NFP 12:30Z"],
            "invalidators": ["Dollar spike through 1.09"],
        },
        "session": {"verdict": "caution", "reasons": ["high-impact USD print"]},
        "regimes": {"EUR_USD": "trending", "GBP_USD": "ranging"},
    }
    data.update(overrides)
    return data


class _StubClient:
    def __init__(self, raw: str) -> None:
        self.raw = raw
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.raw


class _RaisingClient:
    def complete(self, prompt: str) -> str:
        raise ConnectionError("transport failed")


def _is_full_fallback(result: SessionAnalysis, *, instruments: list[str]) -> bool:
    return (
        result.session.verdict == "unavailable"
        and result.brief.summary == _FALLBACK_TEXT
        and all(result.regimes.get(inst) == "unavailable" for inst in instruments)
        and set(result.regimes) == set(instruments)
    )


# ---------------------------------------------------------------------------
# AC 1
# ---------------------------------------------------------------------------


def test_session_analysis_valid_json_and_partial_regimes() -> None:
    """AC 1: stub client valid JSON → all three parts; missing regime → unavailable."""
    payload = _valid_payload(
        regimes={"EUR_USD": "trending", "AUD_USD": "quiet"},
    )
    stub = _StubClient(json.dumps(payload))
    result = session_analysis(_CANDIDATES, _CALENDAR, _STATS, client=stub)
    assert isinstance(result, SessionAnalysis)
    assert result.brief.summary == "Quiet London open."
    assert result.brief.landmines == ["USD NFP 12:30Z"]
    assert result.brief.invalidators == ["Dollar spike through 1.09"]
    assert result.session.verdict == "caution"
    assert result.session.reasons == ["high-impact USD print"]
    assert result.regimes["EUR_USD"] == "trending"
    assert result.regimes["GBP_USD"] == "unavailable"
    assert "AUD_USD" not in result.regimes


# ---------------------------------------------------------------------------
# AC 2 — one table of fail-safe classes (NOT INV-02 skip)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "label,kwargs",
    [
        ("invalid_json", {"client": _StubClient("not json at all")}),
        (
            "missing_field",
            {
                "client": _StubClient(
                    json.dumps(
                        {
                            "brief": {
                                "summary": "x",
                                "landmines": [],
                                "invalidators": [],
                            },
                            "regimes": {},
                        }
                    )
                )
            },
        ),
        (
            "out_of_enum_verdict",
            {
                "client": _StubClient(
                    json.dumps(
                        _valid_payload(
                            session={"verdict": "panic", "reasons": ["x"]}
                        )
                    )
                )
            },
        ),
        (
            "raw_unavailable_verdict",
            {
                "client": _StubClient(
                    json.dumps(
                        _valid_payload(
                            session={"verdict": "unavailable", "reasons": ["x"]}
                        )
                    )
                )
            },
        ),
        (
            "raw_unavailable_regime",
            {
                "client": _StubClient(
                    json.dumps(
                        _valid_payload(regimes={"EUR_USD": "unavailable"})
                    )
                )
            },
        ),
        ("transport_error", {"client": _RaisingClient()}),
        ("no_client_no_key", {"client": None}),
    ],
)
def test_session_analysis_fail_safe_fallback_classes(
    label: str,
    kwargs: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 2: every failure class → full advisory fallback; never raises; no skip/veto."""
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    import ai.brief as brief_mod

    assert "skip" not in brief_mod.__doc__.lower() or "must not add a skip" in brief_mod.__doc__.lower()
    assert "veto" in brief_mod.__doc__.lower()
    assert "must NOT add a skip/veto default" in brief_mod.__doc__ or (
        "must not add a skip/veto" in brief_mod.__doc__.lower()
    )

    sock_calls = {"n": 0}

    class _GuardedSocket(socket.socket):
        def __init__(self, *args: Any, **kw: Any) -> None:
            sock_calls["n"] += 1
            raise AssertionError("network I/O on advisory offline path")

    if label == "no_client_no_key":
        monkeypatch.setattr(socket, "socket", _GuardedSocket)

        class _NoHttpx:
            def __init__(self, *args: Any, **kw: Any) -> None:
                raise AssertionError("httpx Client constructed")

        monkeypatch.setattr("httpx.Client", _NoHttpx)

    try:
        result = session_analysis(
            _CANDIDATES, _CALENDAR, _STATS, **kwargs
        )
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"{label}: session_analysis raised {type(exc).__name__}: {exc}")

    assert _is_full_fallback(result, instruments=["EUR_USD", "GBP_USD"]), label
    if label == "no_client_no_key":
        assert sock_calls["n"] == 0


# ---------------------------------------------------------------------------
# AC 3
# ---------------------------------------------------------------------------


def test_session_prompt_placeholders_substituted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 3: all four placeholders filled; template instructs JSON-only wire format."""
    frozen = "2026-09-20T10:15:00Z"
    monkeypatch.setattr(
        "ai.brief._utc_now_rfc3339",
        lambda: frozen,
    )
    stub = _StubClient(json.dumps(_valid_payload()))
    session_analysis(_CANDIDATES, _CALENDAR, _STATS, client=stub)
    assert stub.prompts, "client.complete was not called"
    prompt = stub.prompts[0]
    for leftover in (
        "{{candidates_summary}}",
        "{{calendar_events}}",
        "{{market_stats}}",
        "{{utc_now}}",
    ):
        assert leftover not in prompt
    assert _CANDIDATES in prompt
    assert _CALENDAR in prompt
    assert _STATS in prompt
    assert frozen in prompt
    template = (_REPO_ROOT / "ai" / "prompts" / "session.md").read_text(
        encoding="utf-8"
    )
    assert "{{candidates_summary}}" in template
    assert "{{calendar_events}}" in template
    assert "{{market_stats}}" in template
    assert "{{utc_now}}" in template
    assert "JSON" in template
    assert "normal" in template and "caution" in template and "stand_aside" in template
    assert "trending" in template and "pre_event" in template
    assert "advisory" in template.lower()
    assert "cannot block" in template.lower() or "cannot" in template.lower()


# ---------------------------------------------------------------------------
# AC 4
# ---------------------------------------------------------------------------


def test_parse_session_analysis_strict_validation() -> None:
    """AC 4: extra=forbid + Literal enums; any validation failure → fallback."""
    good = parse_session_analysis(
        json.dumps(_valid_payload()),
        instruments=["EUR_USD", "GBP_USD"],
    )
    assert good.session.verdict == "caution"
    assert MarketBrief.model_config.get("extra") == "forbid"
    assert SessionVerdict.model_config.get("extra") == "forbid"
    assert SessionAnalysis.model_config.get("extra") == "forbid"

    extra_root = _valid_payload()
    extra_root["confidence"] = 0.9
    extra_brief = _valid_payload()
    extra_brief["brief"] = {
        **extra_brief["brief"],
        "tone": "cheerful",
    }
    bad_rows = [
        json.dumps(extra_root),
        json.dumps(extra_brief),
        json.dumps(
            _valid_payload(session={"verdict": "normal", "reasons": ["x"], "note": "no"})
        ),
        "",
        "[]",
        json.dumps(_valid_payload(regimes={"EUR_USD": "choppy"})),
    ]
    for raw in bad_rows:
        parsed = parse_session_analysis(raw, instruments=["EUR_USD"])
        assert parsed.session.verdict == "unavailable"
        assert parsed.brief.summary == _FALLBACK_TEXT
        assert parsed.regimes == {"EUR_USD": "unavailable"}


# ---------------------------------------------------------------------------
# AC 5
# ---------------------------------------------------------------------------


def test_brief_module_imports_no_store_execution_risk_signals() -> None:
    """AC 5: ai/brief.py AST-imports none of store, execution, risk, or signals."""
    path = _REPO_ROOT / "ai" / "brief.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    forbidden = ("store", "execution", "risk", "signals")
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mod = alias.name
                for bad in forbidden:
                    if mod == bad or mod.startswith(bad + ".") or f".{bad}" in mod:
                        if bad == "store" and "store" not in mod.split("."):
                            continue
                        if (
                            bad == "store"
                            and "store" in mod.split(".")
                            or bad != "store"
                            and (mod == bad or mod.startswith(bad + ".") or mod.endswith("." + bad))
                        ):
                            violations.append(f"import {mod}")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            parts = mod.split(".")
            for bad in forbidden:
                if bad in parts or mod == bad:
                    violations.append(f"from {mod} import ...")
    assert not violations, f"INV-01 boundary: {violations}"


# ---------------------------------------------------------------------------
# AC 6
# ---------------------------------------------------------------------------


def test_inv08_no_llm_api_key_in_logs_or_repr(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 6 / INV-08: LLM_API_KEY never appears in logs or repr on this path."""
    secret = "sk-llm-SUPER-SECRET-KEY-T4"
    monkeypatch.setenv("LLM_API_KEY", secret)
    stub = _StubClient("not-json")
    with caplog.at_level(logging.DEBUG, logger="ai.brief"):
        result = session_analysis(_CANDIDATES, _CALENDAR, _STATS, client=stub)
        parse_session_analysis(secret, instruments=["EUR_USD"])
    blob = " ".join(r.getMessage() for r in caplog.records)
    blob += repr(result) + str(result)
    blob += repr(result.brief) + repr(result.session)
    assert secret not in blob
    assert secret not in caplog.text
