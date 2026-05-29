"""Tests for monitoring/alerts.py — deviation monitor delivery layer (P3-T-09).

Coverage (per ACs from monitor-alerts spec):

1. Each DeviationEvent is persisted to deviation_log BEFORE the delivery
   attempt (durable even if Discord is down).
2. The formatted alert is one line, includes instrument, deviation_type,
   detail, and a UTC RFC-3339 timestamp (INV-03); no secret appears (INV-08).
3. Delivery POSTs to the webhook; a delivery failure is retried with backoff
   and does NOT raise into the monitor loop.
4. Alerts are outbound-only — the module exposes no order/execution capability
   and is not a Hermes tool (INV-01).
5. A duplicate event_id is not double-logged (idempotent persistence).

Implementation notes:
- All tests inject a stub webhook client (no live HTTP).
- Store is an in-memory SQLite (:memory:) instance.
- ``backoff_base=0.0`` in Alerter so retries are instantaneous in tests.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Iterator
from unittest.mock import MagicMock, call, patch

import httpx
import pytest

from data.store import Store
from monitoring.alerts import (
    DEFAULT_MAX_RETRIES,
    Alerter,
    DiscordWebhookClient,
    format_alert,
)
from monitoring.watcher import DeviationEvent

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_UTC = timezone.utc


def _make_event(
    *,
    event_id: str = "evt001",
    instrument: str = "EUR_USD",
    deviation_type: str = "adverse",
    detail: str = "adverse excursion 0.00300 (threshold 0.00250)",
    broker_trade_id: str | None = "T1",
    severity: str = "warn",
    ts: datetime | None = None,
) -> DeviationEvent:
    return DeviationEvent(
        event_id=event_id,
        instrument=instrument,
        deviation_type=deviation_type,
        detail=detail,
        broker_trade_id=broker_trade_id,
        severity=severity,
        created_at=ts or datetime(2026, 5, 29, 15, 10, 0, tzinfo=_UTC),
    )


@pytest.fixture
def store() -> Iterator[Store]:
    """In-memory Store with deviation_log table."""
    s = Store(":memory:")
    yield s
    s.close()


class _OkWebhook:
    """Stub webhook client that always succeeds (no HTTP)."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def post(self, message: str) -> None:
        self.calls.append(message)


class _FailingWebhook:
    """Stub webhook client that always raises httpx.RequestError."""

    def __init__(self) -> None:
        self.call_count: int = 0

    def post(self, message: str) -> None:
        self.call_count += 1
        raise httpx.RequestError("simulated network failure")


class _FailThenSucceedWebhook:
    """Stub webhook client that fails N times then succeeds."""

    def __init__(self, fail_count: int) -> None:
        self.fail_count = fail_count
        self._calls = 0
        self.success_messages: list[str] = []

    def post(self, message: str) -> None:
        self._calls += 1
        if self._calls <= self.fail_count:
            raise httpx.RequestError("simulated transient failure")
        self.success_messages.append(message)


# ---------------------------------------------------------------------------
# AC-2: format_alert — one line, correct fields, UTC RFC-3339, no secret
# ---------------------------------------------------------------------------


def test_format_alert_one_line() -> None:
    """Alert must be exactly one line (no newlines)."""
    ev = _make_event()
    result = format_alert(ev)
    assert "\n" not in result, f"Alert must be one line, got: {result!r}"


def test_format_alert_includes_instrument() -> None:
    """Alert must contain the instrument name."""
    ev = _make_event(instrument="GBP_USD")
    assert "GBP_USD" in format_alert(ev)


def test_format_alert_includes_deviation_type() -> None:
    """Alert must contain the deviation type."""
    ev = _make_event(deviation_type="slippage", detail="fill slippage 0.0005")
    result = format_alert(ev)
    assert "slippage" in result


def test_format_alert_includes_detail() -> None:
    """Alert must contain the detail string."""
    ev = _make_event(detail="some detail text")
    assert "some detail text" in format_alert(ev)


def test_format_alert_utc_rfc3339_timestamp() -> None:
    """Alert must contain a UTC RFC-3339 timestamp (INV-03): ends with Z."""
    ev = _make_event(ts=datetime(2026, 5, 29, 15, 10, 0, tzinfo=_UTC))
    result = format_alert(ev)
    # Must contain the RFC-3339 UTC string
    assert "2026-05-29T15:10:00Z" in result, (
        f"Expected UTC RFC-3339 timestamp in alert, got: {result!r}"
    )


def test_format_alert_no_secret_in_text() -> None:
    """Alert text must NOT contain a webhook URL or any secret (INV-08).

    We verify this by checking that the format function does not accept
    or use a URL, and the output only contains event fields.
    """
    ev = _make_event()
    result = format_alert(ev)
    # A webhook URL would look like https://discord.com/api/...
    assert "https://" not in result, (
        f"Alert must not contain a URL (INV-08), got: {result!r}"
    )
    # Verify expected structure
    assert result.startswith("⚠️"), f"Alert should start with warning emoji: {result!r}"


def test_format_alert_pipe_delimited() -> None:
    """Alert fields are separated by ' | '."""
    ev = _make_event(
        instrument="EUR_USD",
        deviation_type="vol",
        detail="vol range 0.01100",
        ts=datetime(2026, 5, 29, 15, 10, 0, tzinfo=_UTC),
    )
    result = format_alert(ev)
    expected = "⚠️ EUR_USD vol | vol range 0.01100 | 2026-05-29T15:10:00Z"
    assert result == expected, f"Expected {expected!r}, got {result!r}"


def test_format_alert_feed_health_no_broker_id() -> None:
    """Feed-health events (no broker_trade_id) must still format correctly."""
    ev = _make_event(
        deviation_type="feed_health",
        detail="no tick for 20.0s",
        broker_trade_id=None,
        ts=datetime(2026, 5, 29, 12, 0, 0, tzinfo=_UTC),
    )
    result = format_alert(ev)
    assert "feed_health" in result
    assert "2026-05-29T12:00:00Z" in result
    assert "\n" not in result


# ---------------------------------------------------------------------------
# AC-1: event persisted BEFORE delivery attempt (durable even if POST fails)
# ---------------------------------------------------------------------------


def test_event_persisted_before_delivery_on_success(store: Store) -> None:
    """Event row exists in deviation_log even when delivery succeeds (basic case)."""
    ev = _make_event()
    webhook = _OkWebhook()
    alerter = Alerter(webhook_client=webhook, store=store, backoff_base=0.0)

    alerter.send(ev)

    rows = store.load_deviation_log()
    assert len(rows) == 1
    assert rows[0]["event_id"] == ev.event_id


def test_event_persisted_before_delivery_when_post_fails(store: Store) -> None:
    """Event row must be in deviation_log even when Discord POST fails.

    This is the core 'durable even if Discord is down' guarantee: the
    persist step happens before any HTTP attempt.  We verify by checking
    that after a fully-failed send (all retries exhausted), the row is
    present in deviation_log with delivered=False.
    """
    ev = _make_event()
    failing_webhook = _FailingWebhook()
    alerter = Alerter(
        webhook_client=failing_webhook,
        store=store,
        max_retries=2,
        backoff_base=0.0,
    )

    # Must not raise even with a failing webhook
    alerter.send(ev)

    rows = store.load_deviation_log()
    assert len(rows) == 1, "Event must be persisted even when delivery fails"
    assert rows[0]["event_id"] == ev.event_id
    assert rows[0]["delivered"] is False, (
        "delivered must remain False after all retries fail"
    )


def test_persist_before_delivery_ordering(store: Store) -> None:
    """Verify persist happens before POST by using a spy that records order.

    The sequence must be: write_deviation_event → post, not post → write.
    We verify this by a webhook stub that checks the store on first call.
    """
    ev = _make_event()

    class _OrderCheckWebhook:
        """Verifies the row is already in store when post() is called."""

        def __init__(self, store: Store) -> None:
            self._store = store
            self.row_existed_at_post_time: bool = False

        def post(self, message: str) -> None:
            rows = self._store.load_deviation_log()
            self.row_existed_at_post_time = any(
                r["event_id"] == ev.event_id for r in rows
            )

    webhook = _OrderCheckWebhook(store)
    alerter = Alerter(webhook_client=webhook, store=store, backoff_base=0.0)
    alerter.send(ev)

    assert webhook.row_existed_at_post_time, (
        "deviation_log row must exist BEFORE the POST attempt (durable-first guarantee)"
    )


# ---------------------------------------------------------------------------
# AC-3: delivery failure retried with backoff; does NOT raise
# ---------------------------------------------------------------------------


def test_delivery_failure_does_not_raise(store: Store) -> None:
    """A fully-failing webhook must NOT raise into the caller (INV-01)."""
    ev = _make_event()
    alerter = Alerter(
        webhook_client=_FailingWebhook(),
        store=store,
        max_retries=3,
        backoff_base=0.0,
    )
    # Must complete without raising
    alerter.send(ev)


def test_delivery_retried_on_failure(store: Store) -> None:
    """Webhook POST is retried up to max_retries times on failure."""
    ev = _make_event()
    webhook = _FailingWebhook()
    max_retries = 3
    alerter = Alerter(
        webhook_client=webhook,
        store=store,
        max_retries=max_retries,
        backoff_base=0.0,
    )
    alerter.send(ev)
    assert webhook.call_count == max_retries, (
        f"Expected {max_retries} POST attempts, got {webhook.call_count}"
    )


def test_delivery_succeeds_after_transient_failures(store: Store) -> None:
    """Webhook succeeds on the 3rd attempt (2 transient failures first)."""
    ev = _make_event()
    webhook = _FailThenSucceedWebhook(fail_count=2)
    alerter = Alerter(
        webhook_client=webhook,
        store=store,
        max_retries=3,
        backoff_base=0.0,
    )
    alerter.send(ev)

    # Delivery was eventually successful
    assert len(webhook.success_messages) == 1

    # Row is marked delivered=True
    rows = store.load_deviation_log()
    assert len(rows) == 1
    assert rows[0]["delivered"] is True


def test_delivered_flag_true_after_success(store: Store) -> None:
    """After a successful POST, delivered=True in deviation_log."""
    ev = _make_event()
    webhook = _OkWebhook()
    alerter = Alerter(webhook_client=webhook, store=store, backoff_base=0.0)

    alerter.send(ev)

    rows = store.load_deviation_log()
    assert rows[0]["delivered"] is True


def test_delivered_flag_false_after_exhausted_retries(store: Store) -> None:
    """After all retries fail, delivered=False remains in deviation_log."""
    ev = _make_event()
    alerter = Alerter(
        webhook_client=_FailingWebhook(),
        store=store,
        max_retries=2,
        backoff_base=0.0,
    )
    alerter.send(ev)

    rows = store.load_deviation_log()
    assert rows[0]["delivered"] is False


# ---------------------------------------------------------------------------
# AC-5: idempotent on event_id — duplicate not double-logged
# ---------------------------------------------------------------------------


def test_idempotent_persistence_same_event_id(store: Store) -> None:
    """Re-sending the same event_id must not create a second deviation_log row."""
    ev = _make_event(event_id="dup001")
    webhook = _OkWebhook()
    alerter = Alerter(webhook_client=webhook, store=store, backoff_base=0.0)

    alerter.send(ev)
    alerter.send(ev)  # same event_id — second call must be a no-op at store

    rows = store.load_deviation_log()
    assert len(rows) == 1, (
        f"Expected exactly 1 row for event_id='dup001', got {len(rows)}"
    )


def test_idempotent_different_event_ids_creates_two_rows(store: Store) -> None:
    """Two events with different event_ids create two separate rows."""
    ev1 = _make_event(event_id="evt001")
    ev2 = _make_event(event_id="evt002")
    webhook = _OkWebhook()
    alerter = Alerter(webhook_client=webhook, store=store, backoff_base=0.0)

    alerter.send(ev1)
    alerter.send(ev2)

    rows = store.load_deviation_log()
    assert len(rows) == 2
    ids = {r["event_id"] for r in rows}
    assert ids == {"evt001", "evt002"}


# ---------------------------------------------------------------------------
# AC-4 / INV-01: outbound-only — no order / execution capability
# ---------------------------------------------------------------------------


def test_alerter_has_no_order_capability() -> None:
    """Alerter must not expose any order / execution / position-opening method."""
    store_mock = MagicMock(spec=Store)
    webhook = _OkWebhook()
    alerter = Alerter(webhook_client=webhook, store=store_mock, backoff_base=0.0)

    for forbidden in (
        "submit_order",
        "place_order",
        "open_position",
        "close_position",
        "execute",
        "flatten",
    ):
        assert not hasattr(alerter, forbidden), (
            f"Alerter must not have method '{forbidden}' (INV-01)"
        )


def test_discord_webhook_client_has_no_order_capability() -> None:
    """DiscordWebhookClient exposes only 'post' — no order capability (INV-01)."""
    client = DiscordWebhookClient("https://example.com/webhook")
    for forbidden in ("submit_order", "place_order", "open_position", "execute"):
        assert not hasattr(client, forbidden), (
            f"DiscordWebhookClient must not have '{forbidden}' (INV-01)"
        )


# ---------------------------------------------------------------------------
# INV-08: no secret in alert text or logs
# ---------------------------------------------------------------------------


def test_no_secret_in_alert_text() -> None:
    """The formatted alert must not contain the webhook URL (INV-08)."""
    secret_url = "https://discord.com/api/webhooks/1234567890/FAKE_SECRET_TOKEN"
    ev = _make_event()
    message = format_alert(ev)
    assert secret_url not in message
    assert "discord.com" not in message
    assert "FAKE_SECRET_TOKEN" not in message


def test_discord_webhook_client_does_not_log_url(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """DiscordWebhookClient must not log the webhook URL (INV-08).

    We verify this by attempting a POST to a non-existent endpoint (which
    raises an error), then confirming the URL does not appear in any log record.
    """
    import logging

    secret_url = "https://discord.com/api/webhooks/9999/SUPER_SECRET_TOKEN"
    client = DiscordWebhookClient(secret_url, timeout=0.001)

    with caplog.at_level(logging.DEBUG, logger="fathom"):
        try:
            client.post("test message")
        except Exception:
            pass  # Expected — URL is invalid / unreachable

    for record in caplog.records:
        assert "SUPER_SECRET_TOKEN" not in record.getMessage(), (
            f"Webhook URL secret appeared in log: {record.getMessage()!r}"
        )


def test_alerter_warning_does_not_log_url(
    store: Store,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Alerter retry-failure log messages must not contain the webhook URL."""
    import logging

    secret_url = "https://discord.com/api/webhooks/9999/ANOTHER_SECRET"

    class _SecretFailing:
        """Failing webhook that would expose the URL if the alerter logged it."""

        def post(self, message: str) -> None:
            raise httpx.RequestError("network error")

    ev = _make_event()
    alerter = Alerter(
        webhook_client=_SecretFailing(),
        store=store,
        max_retries=2,
        backoff_base=0.0,
    )

    with caplog.at_level(logging.WARNING, logger="fathom.monitoring.alerts"):
        alerter.send(ev)

    # The secret URL must not appear in any warning log record
    for record in caplog.records:
        assert "ANOTHER_SECRET" not in record.getMessage(), (
            f"Secret appeared in alert log: {record.getMessage()!r}"
        )


# ---------------------------------------------------------------------------
# Store: deviation_log read/write (unit tests for the store layer itself)
# ---------------------------------------------------------------------------


def test_store_write_and_load_deviation_event(store: Store) -> None:
    """write_deviation_event persists the event; load_deviation_log returns it."""
    ev = _make_event(
        event_id="store_test_001",
        instrument="USD_JPY",
        deviation_type="vol",
        detail="vol range test",
        broker_trade_id="T99",
        severity="severe",
        ts=datetime(2026, 5, 29, 10, 0, 0, tzinfo=_UTC),
    )
    inserted = store.write_deviation_event(ev)
    assert inserted is True

    rows = store.load_deviation_log()
    assert len(rows) == 1
    row = rows[0]
    assert row["event_id"] == "store_test_001"
    assert row["instrument"] == "USD_JPY"
    assert row["deviation_type"] == "vol"
    assert row["detail"] == "vol range test"
    assert row["broker_trade_id"] == "T99"
    assert row["severity"] == "severe"
    assert row["created_at"] == "2026-05-29T10:00:00Z"  # RFC-3339 UTC (INV-03)
    assert row["delivered"] is False


def test_store_write_idempotent_same_event_id(store: Store) -> None:
    """Re-writing the same event_id returns False (no-op)."""
    ev = _make_event(event_id="idem_001")
    first = store.write_deviation_event(ev)
    second = store.write_deviation_event(ev)

    assert first is True
    assert second is False  # INSERT OR IGNORE → 0 rowcount

    rows = store.load_deviation_log()
    assert len(rows) == 1, "Duplicate event_id must not create a second row"


def test_store_mark_delivered(store: Store) -> None:
    """mark_deviation_delivered sets delivered=True on the row."""
    ev = _make_event(event_id="mark_del_001")
    store.write_deviation_event(ev)

    rows_before = store.load_deviation_log()
    assert rows_before[0]["delivered"] is False

    store.mark_deviation_delivered("mark_del_001")

    rows_after = store.load_deviation_log()
    assert rows_after[0]["delivered"] is True


def test_store_load_undelivered_only(store: Store) -> None:
    """load_deviation_log(undelivered_only=True) returns only undelivered rows."""
    ev1 = _make_event(event_id="del_001")
    ev2 = _make_event(event_id="del_002")
    store.write_deviation_event(ev1)
    store.write_deviation_event(ev2)
    store.mark_deviation_delivered("del_001")

    undelivered = store.load_deviation_log(undelivered_only=True)
    ids = {r["event_id"] for r in undelivered}
    assert "del_001" not in ids, "Delivered event must not appear in undelivered_only"
    assert "del_002" in ids, "Undelivered event must appear"


def test_store_feed_health_event_null_broker_trade_id(store: Store) -> None:
    """feed_health events with broker_trade_id=None store NULL correctly."""
    ev = _make_event(
        event_id="fh_001",
        deviation_type="feed_health",
        broker_trade_id=None,
    )
    store.write_deviation_event(ev)

    rows = store.load_deviation_log()
    assert rows[0]["broker_trade_id"] is None


def test_store_load_with_limit(store: Store) -> None:
    """load_deviation_log(limit=N) returns at most N rows."""
    for i in range(5):
        ev = _make_event(event_id=f"lim_{i:03d}")
        store.write_deviation_event(ev)

    rows = store.load_deviation_log(limit=3)
    assert len(rows) == 3


# ---------------------------------------------------------------------------
# Integration: Alerter sends correct message text to the webhook
# ---------------------------------------------------------------------------


def test_alerter_sends_formatted_message(store: Store) -> None:
    """The message POSTed to the webhook matches format_alert output."""
    ev = _make_event(
        instrument="GBP_USD",
        deviation_type="slippage",
        detail="fill slippage 0.00050",
        ts=datetime(2026, 5, 29, 12, 30, 0, tzinfo=_UTC),
    )
    webhook = _OkWebhook()
    alerter = Alerter(webhook_client=webhook, store=store, backoff_base=0.0)

    alerter.send(ev)

    expected = format_alert(ev)
    assert webhook.calls == [expected], (
        f"Expected webhook called with {expected!r}, got {webhook.calls!r}"
    )


def test_alerter_message_contains_utc_timestamp(store: Store) -> None:
    """The POSTed message must contain a UTC RFC-3339 timestamp (INV-03)."""
    ts = datetime(2026, 5, 29, 9, 45, 0, tzinfo=_UTC)
    ev = _make_event(ts=ts)
    webhook = _OkWebhook()
    alerter = Alerter(webhook_client=webhook, store=store, backoff_base=0.0)

    alerter.send(ev)

    assert len(webhook.calls) == 1
    message = webhook.calls[0]
    # Must contain the RFC-3339 string with Z suffix
    assert "2026-05-29T09:45:00Z" in message, (
        f"UTC RFC-3339 timestamp not found in message: {message!r}"
    )


# ---------------------------------------------------------------------------
# DiscordWebhookClient — unit test the HTTP layer (mocked httpx)
# ---------------------------------------------------------------------------


def test_discord_webhook_client_posts_json_content() -> None:
    """DiscordWebhookClient POSTs {"content": message} to the webhook URL."""
    captured: list[dict[str, object]] = []

    class _MockClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        def __enter__(self) -> "_MockClient":
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def post(self, url: str, json: dict[str, object]) -> "_MockResponse":
            captured.append({"url": url, "json": json})
            return _MockResponse()

    class _MockResponse:
        def raise_for_status(self) -> None:
            pass

    with patch("monitoring.alerts.httpx.Client", _MockClient):
        client = DiscordWebhookClient("https://fake-webhook.example.com/hook")
        client.post("test alert message")

    assert len(captured) == 1
    assert captured[0]["json"] == {"content": "test alert message"}


def test_discord_webhook_client_raises_on_http_error() -> None:
    """DiscordWebhookClient propagates httpx.HTTPStatusError on 4xx/5xx."""

    class _MockClientBad:
        def __init__(self, **kwargs: object) -> None:
            pass

        def __enter__(self) -> "_MockClientBad":
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def post(self, url: str, json: dict[str, object]) -> "_MockBadResponse":
            return _MockBadResponse()

    class _MockBadResponse:
        def raise_for_status(self) -> None:
            # Simulate httpx raising on a 4xx/5xx response
            mock_request = MagicMock()
            mock_response = MagicMock()
            mock_response.status_code = 429
            raise httpx.HTTPStatusError(
                "429 Too Many Requests",
                request=mock_request,
                response=mock_response,
            )

    with patch("monitoring.alerts.httpx.Client", _MockClientBad):
        client = DiscordWebhookClient("https://fake-webhook.example.com/hook")
        with pytest.raises(httpx.HTTPStatusError):
            client.post("test")
