"""Tests for config/settings.py.

Two responsibilities:
1. Drift guard — assert that .env.example keys match the declared Settings fields.
2. Validation guard — assert that Settings() raises a clear error when required fields are absent.
3. Phase 5 live-trading gate fields — live_trading_enabled / live_risk_fraction defaults +
   Field(gt=0.0, le=0.0025) boundary validation (INV-05).
"""

import os
import re
import tempfile
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).parent.parent
_ENV_EXAMPLE = _ROOT / ".env.example"

# Fields that are auto-derived (not required in .env) — excluded from the
# drift check because they do not need a corresponding .env.example key.
_DERIVED_FIELDS = {"oanda_base_url"}


def _parse_env_example_keys() -> set[str]:
    """Return the set of variable names declared in .env.example (lowercased)."""
    keys: set[str] = set()
    for line in _ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            # Match lines of the form KEY=value or KEY =value
            m = re.match(r"^([A-Z_][A-Z0-9_]*)\s*=", stripped)
            if m:
                keys.add(m.group(1).lower())
    return keys


# ---------------------------------------------------------------------------
# Test 1: .env.example key drift guard
# ---------------------------------------------------------------------------

def test_env_example_keys_match_settings_fields() -> None:
    """Every non-derived Settings field must appear as a key in .env.example,
    and .env.example must not declare keys that Settings does not know about.

    This test prevents silent config drift: if a new required field is added
    to Settings but forgotten in .env.example (or vice versa), this test fails.
    """
    from config.settings import Settings

    # Fields pydantic-settings injects or that are derived — skip them.
    _PYDANTIC_INTERNAL = {"model_config"}

    settings_fields = {
        name.lower()
        for name in Settings.model_fields
        if name.lower() not in _DERIVED_FIELDS | _PYDANTIC_INTERNAL
    }

    env_example_keys = _parse_env_example_keys()

    missing_from_example = settings_fields - env_example_keys
    assert not missing_from_example, (
        f"Settings fields missing from .env.example: {missing_from_example}. "
        "Add them with placeholder values."
    )

    extra_in_example = env_example_keys - settings_fields
    assert not extra_in_example, (
        f".env.example declares keys unknown to Settings: {extra_in_example}. "
        "Remove or add them to Settings."
    )


# ---------------------------------------------------------------------------
# Test 2: Validation error on missing required fields
# ---------------------------------------------------------------------------

def test_settings_raises_on_missing_required_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings() must raise pydantic.ValidationError (not return None silently)
    when required fields oanda_api_token and oanda_account_id are absent.
    """
    # Wipe any env vars that could satisfy the required fields.
    for key in ("OANDA_API_TOKEN", "OANDA_ACCOUNT_ID", "oanda_api_token", "oanda_account_id"):
        monkeypatch.delenv(key, raising=False)

    # Point pydantic-settings at a nonexistent .env so it cannot load from disk.
    monkeypatch.chdir(tmp_dir := Path(os.environ.get("TMPDIR", "/tmp")))
    _ = tmp_dir  # silence unused-variable lint

    with pytest.raises(ValidationError) as exc_info:
        from config.settings import Settings
        Settings()

    errors: list[Any] = exc_info.value.errors()
    missing_fields = {e["loc"][0] for e in errors if e["type"] == "missing"}
    assert "oanda_api_token" in missing_fields, (
        "Expected ValidationError for missing oanda_api_token"
    )
    assert "oanda_account_id" in missing_fields, (
        "Expected ValidationError for missing oanda_account_id"
    )


# ---------------------------------------------------------------------------
# Test 3: oanda_base_url is auto-derived from env field
# ---------------------------------------------------------------------------

def test_oanda_base_url_derived_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """oanda_base_url must be set automatically based on the env field."""
    monkeypatch.setenv("OANDA_API_TOKEN", "test-token")
    monkeypatch.setenv("OANDA_ACCOUNT_ID", "test-account")

    # Temporarily prevent loading from any .env file on disk.
    # We achieve this by pointing cwd at a directory with no .env.
    with tempfile.TemporaryDirectory() as tmpdir:
        monkeypatch.chdir(tmpdir)

        from importlib import reload
        import config.settings as settings_mod
        reload(settings_mod)
        Settings = settings_mod.Settings

        demo_settings = Settings(env="demo")
        assert demo_settings.oanda_base_url == "https://api-fxpractice.oanda.com"

        live_settings = Settings(env="live")
        assert live_settings.oanda_base_url == "https://api-fxtrade.oanda.com"


# ---------------------------------------------------------------------------
# Test 4: Phase 5 live-trading gate fields (INV-05 / INV-07) P5-T-01
# ---------------------------------------------------------------------------


class TestLiveTradingGateFields:
    """live_trading_enabled + live_risk_fraction defaults + boundary validation."""

    @pytest.fixture(autouse=True)
    def _isolated_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Inject required fields and isolate from .env on disk."""
        monkeypatch.setenv("OANDA_API_TOKEN", "test-token")
        monkeypatch.setenv("OANDA_ACCOUNT_ID", "test-account")
        monkeypatch.chdir(tempfile.mkdtemp())

    def _make_settings(self, **kwargs: Any) -> Any:
        from importlib import reload
        import config.settings as settings_mod
        reload(settings_mod)
        return settings_mod.Settings(**kwargs)

    def test_live_trading_enabled_default_false(self) -> None:
        """live_trading_enabled must default to False (INV-07: demo first)."""
        s = self._make_settings()
        assert s.live_trading_enabled is False

    def test_live_trading_enabled_can_be_set_true(self) -> None:
        s = self._make_settings(live_trading_enabled=True)
        assert s.live_trading_enabled is True

    def test_live_risk_fraction_default(self) -> None:
        """live_risk_fraction must default to 0.001 (0.10%, D-P5-3)."""
        s = self._make_settings()
        assert s.live_risk_fraction == pytest.approx(0.001)

    def test_live_risk_fraction_upper_bound_exact(self) -> None:
        """Exactly 0.0025 (the INV-05 cap) is allowed (le=0.0025)."""
        s = self._make_settings(live_risk_fraction=0.0025)
        assert s.live_risk_fraction == pytest.approx(0.0025)

    def test_live_risk_fraction_above_cap_raises(self) -> None:
        """Any value > 0.0025 must raise ValidationError at construction (INV-05, B-5)."""
        with pytest.raises(ValidationError) as exc_info:
            self._make_settings(live_risk_fraction=0.0026)
        errors = exc_info.value.errors()
        assert any(e["loc"] == ("live_risk_fraction",) for e in errors), (
            f"Expected ValidationError on live_risk_fraction field, got: {errors}"
        )

    def test_live_risk_fraction_zero_raises(self) -> None:
        """live_risk_fraction=0.0 must raise ValidationError (gt=0.0)."""
        with pytest.raises(ValidationError) as exc_info:
            self._make_settings(live_risk_fraction=0.0)
        errors = exc_info.value.errors()
        assert any(e["loc"] == ("live_risk_fraction",) for e in errors)

    def test_live_risk_fraction_negative_raises(self) -> None:
        """Negative live_risk_fraction must raise ValidationError (gt=0.0)."""
        with pytest.raises(ValidationError) as exc_info:
            self._make_settings(live_risk_fraction=-0.001)
        errors = exc_info.value.errors()
        assert any(e["loc"] == ("live_risk_fraction",) for e in errors)

    def test_live_risk_fraction_custom_within_bounds(self) -> None:
        """A valid fraction within (0, 0.0025] is accepted."""
        s = self._make_settings(live_risk_fraction=0.0015)
        assert s.live_risk_fraction == pytest.approx(0.0015)
