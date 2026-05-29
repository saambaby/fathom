"""Deviation monitor delivery layer — DiscordWebhookClient + Alerter (P3-T-09).

Turns a ``DeviationEvent`` (from ``monitoring/watcher.py``) into a one-line
Discord alert posted directly to ``DISCORD_WEBHOOK_URL``.  This is the same
channel the Phase 2 watchlist uses (DRIFT-06 resolution: the monitor is a
standalone Python process, not a Hermes job — it posts directly via
``DiscordWebhookClient``, no Hermes gateway).

Invariants
----------
* **INV-01** — outbound notification only; this module exposes no order /
  execution capability and is NOT registered as a Hermes tool.  The Alerter
  sends a Discord message and nothing else.
* **INV-03** — every alert contains a UTC RFC 3339 timestamp; ``created_at``
  is formatted with ``Z`` suffix.
* **INV-08** — ``DISCORD_WEBHOOK_URL`` is read via ``Settings.discord_webhook_url``
  (a ``SecretStr``).  The raw URL is NEVER printed, logged, or included in
  the alert text.

Delivery contract (DRIFT-06 + monitor-alerts spec)
---------------------------------------------------
1. **Persist FIRST** — ``Alerter.send`` writes the event to ``deviation_log``
   before any HTTP attempt.  The event is durable even if Discord is down.
2. **POST** — after persistence, POST the one-line formatted message to Discord.
3. **Retry with backoff** — on HTTP failure, retry up to ``max_retries`` times
   with exponential backoff (no jitter for simplicity; deterministic in tests).
4. **Never raise into the caller** — a down Discord must not crash the monitor
   loop.  Delivery failures are logged at WARNING; the event remains in
   ``deviation_log`` with ``delivered=False`` for a catch-up pass.
5. **Idempotent persistence** — ``write_deviation_event`` uses
   ``INSERT OR IGNORE`` on ``event_id``; re-sending the same event is a no-op
   at the store layer.
6. **Mark delivered** — after a successful POST, ``mark_deviation_delivered``
   sets ``delivered=True`` on the row.

Alert format
------------
One line::

    ⚠️ <instrument> <deviation_type> | <detail> | <UTC RFC-3339 time>

Example::

    ⚠️ EUR_USD adverse | adverse excursion 0.00300 (threshold 0.00250) | 2026-05-29T15:10:00Z
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Protocol

import httpx

from data.store import Store, _to_rfc3339
from monitoring.watcher import DeviationEvent

logger = logging.getLogger("fathom.monitoring.alerts")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default number of retry attempts on delivery failure.
DEFAULT_MAX_RETRIES: int = 3

#: Base backoff in seconds; doubles on each retry (exponential, no jitter).
DEFAULT_BACKOFF_BASE: float = 1.0

#: httpx timeout for the Discord webhook POST.
_HTTP_TIMEOUT: float = 10.0


# ---------------------------------------------------------------------------
# Alert formatter — pure function, unit-testable
# ---------------------------------------------------------------------------


def format_alert(event: DeviationEvent) -> str:
    """Format a ``DeviationEvent`` as a one-line Discord alert.

    Format::

        ⚠️ <instrument> <deviation_type> | <detail> | <UTC RFC-3339 time>

    The UTC timestamp uses ``Z`` suffix (INV-03).  No secret appears in the
    output (INV-08).

    Args:
        event: The deviation event to format.

    Returns:
        A single-line string suitable for a Discord webhook message.

    Examples
    --------
    >>> from datetime import datetime, timezone
    >>> from monitoring.watcher import DeviationEvent
    >>> ev = DeviationEvent(
    ...     event_id="abc123",
    ...     instrument="EUR_USD",
    ...     deviation_type="adverse",
    ...     detail="adverse excursion 0.00300",
    ...     severity="warn",
    ...     created_at=datetime(2026, 5, 29, 15, 10, 0, tzinfo=timezone.utc),
    ... )
    >>> format_alert(ev)
    '⚠️ EUR_USD adverse | adverse excursion 0.00300 | 2026-05-29T15:10:00Z'
    """
    ts = _to_rfc3339(event.created_at)
    return f"⚠️ {event.instrument} {event.deviation_type} | {event.detail} | {ts}"


# ---------------------------------------------------------------------------
# WebhookClient protocol — lets tests inject a stub without live HTTP
# ---------------------------------------------------------------------------


class WebhookClientProtocol(Protocol):
    """Minimal webhook client interface.  Tests inject a stub."""

    def post(self, message: str) -> None:
        """POST ``message`` to the configured webhook endpoint.

        Raises:
            httpx.HTTPStatusError: on a 4xx/5xx response (after raising_for_status).
            httpx.RequestError: on a network / timeout error.
        """
        ...


# ---------------------------------------------------------------------------
# DiscordWebhookClient — thin httpx POST wrapper (INV-08: URL never logged)
# ---------------------------------------------------------------------------


class DiscordWebhookClient:
    """Thin ``httpx`` POST wrapper around ``DISCORD_WEBHOOK_URL``.

    The webhook URL is accepted as a plain string (already extracted from
    ``Settings.discord_webhook_url.get_secret_value()`` by the caller, so
    this class never imports ``Settings`` itself — it is purely a transport
    layer).

    INV-08: the URL is stored in a private attribute and is NEVER logged,
    printed, or included in alert text.

    Args:
        webhook_url: The raw Discord webhook URL (extracted from
            ``Settings.discord_webhook_url.get_secret_value()``).
        timeout: HTTP timeout in seconds (default: 10.0).
    """

    def __init__(self, webhook_url: str, *, timeout: float = _HTTP_TIMEOUT) -> None:
        self._url = webhook_url  # INV-08: never log this
        self._timeout = timeout

    def post(self, message: str) -> None:
        """POST ``message`` to Discord as ``{"content": message}``.

        Raises:
            httpx.HTTPStatusError: on a 4xx/5xx Discord response.
            httpx.RequestError: on a network / timeout failure.
        """
        with httpx.Client(timeout=self._timeout) as client:
            resp = client.post(self._url, json={"content": message})
            resp.raise_for_status()


# ---------------------------------------------------------------------------
# Alerter — implements the watcher's Alerter protocol with persist-then-deliver
# ---------------------------------------------------------------------------


class Alerter:
    """Concrete ``Alerter`` implementing persist-then-deliver with retry.

    Satisfies the ``monitoring.watcher.Alerter`` protocol (duck-typed —
    no explicit inheritance needed, just ``send(event: DeviationEvent) -> None``).

    Delivery contract:
    1. Persist the event to ``deviation_log`` via ``store.write_deviation_event``
       BEFORE any HTTP attempt (durable even if Discord is down).
    2. Format the one-line alert via ``format_alert``.
    3. POST via the injected ``webhook_client``.
    4. On failure: retry with exponential backoff up to ``max_retries`` times.
    5. After all retries exhausted: log a warning and return (never raise).
    6. On success: call ``store.mark_deviation_delivered`` to set
       ``delivered=True``.

    Idempotency: ``write_deviation_event`` uses ``INSERT OR IGNORE`` on
    ``event_id`` — re-calling ``send`` with the same event is a no-op at the
    persistence layer (the first persistence wins).

    INV-01: no order / execution capability exposed.
    INV-08: no secret in logs (the webhook client holds the URL privately).

    Args:
        webhook_client: Any object with ``post(message: str) -> None``.
            Use ``DiscordWebhookClient`` in production; inject a stub in tests.
        store: The ``Store`` instance used to persist events.
        max_retries: Maximum POST retry attempts (default: 3).
        backoff_base: Base backoff seconds; doubles on each retry
            (default: 1.0).  Pass 0.0 in tests to skip real sleeps.
    """

    def __init__(
        self,
        webhook_client: WebhookClientProtocol,
        store: Store,
        *,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base: float = DEFAULT_BACKOFF_BASE,
    ) -> None:
        self._webhook = webhook_client
        self._store = store
        self._max_retries = max_retries
        self._backoff_base = backoff_base

    def send(self, event: DeviationEvent) -> None:
        """Persist and deliver one ``DeviationEvent`` (INV-01, INV-03, INV-08).

        Step 1: persist to ``deviation_log`` (durable; idempotent on event_id).
        Step 2: format the one-line alert.
        Step 3: POST to Discord with retry + backoff.
        Step 4: mark ``delivered=True`` on success; log warning on exhaustion.

        Never raises into the caller — a down Discord must not crash the monitor
        loop (the watcher calls ``send`` on every emitted deviation event).

        Args:
            event: The ``DeviationEvent`` to persist and deliver.
        """
        # --- Step 1: persist FIRST (durability guarantee) ---
        self._store.write_deviation_event(event)

        # --- Step 2: format the alert (INV-03: UTC RFC-3339 ts; INV-08: no URL) ---
        message = format_alert(event)

        # --- Step 3: POST with retry + exponential backoff ---
        last_exc: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                self._webhook.post(message)
                # --- Step 4: mark delivered after successful POST ---
                self._store.mark_deviation_delivered(event.event_id)
                logger.info(
                    "alerts.Alerter: delivered event_id=%s instrument=%s type=%s",
                    event.event_id,
                    event.instrument,
                    event.deviation_type,
                )
                return
            except Exception as exc:
                last_exc = exc
                backoff = self._backoff_base * (2 ** attempt)
                logger.warning(
                    "alerts.Alerter: delivery attempt %d/%d failed for event_id=%s: %s"
                    " — retrying in %.1fs",
                    attempt + 1,
                    self._max_retries,
                    event.event_id,
                    type(exc).__name__,
                    backoff,
                )
                if attempt < self._max_retries - 1:
                    time.sleep(backoff)

        # All retries exhausted — log and return without raising (INV-01: never crash loop)
        logger.warning(
            "alerts.Alerter: all %d delivery attempts failed for event_id=%s"
            " (instrument=%s type=%s). Event remains in deviation_log with delivered=False."
            " Last error: %s",
            self._max_retries,
            event.event_id,
            event.instrument,
            event.deviation_type,
            last_exc,
        )


# ---------------------------------------------------------------------------
# Factory helper — build a live Alerter from Settings (INV-08)
# ---------------------------------------------------------------------------


def build_alerter_from_settings(store: Store) -> Alerter:
    """Construct a live ``Alerter`` from ``Settings.discord_webhook_url``.

    INV-08: the URL is read via ``SecretStr.get_secret_value()`` and passed
    directly to ``DiscordWebhookClient``, which stores it privately.  It is
    never logged, printed, or otherwise exposed.

    Args:
        store: The open ``Store`` instance for deviation_log persistence.

    Returns:
        A fully-wired ``Alerter`` using ``DiscordWebhookClient``.

    Raises:
        ValueError: If ``DISCORD_WEBHOOK_URL`` is not set in ``.env`` /
            environment (required at runtime by the monitor).
    """
    from config.settings import Settings  # local import — avoids top-level coupling

    settings = Settings()
    if settings.discord_webhook_url is None:
        raise ValueError(
            "DISCORD_WEBHOOK_URL is not set in .env / environment.  "
            "The deviation monitor alerter requires it.  "
            "Add DISCORD_WEBHOOK_URL=<url> to .env (never commit the value — INV-08)."
        )
    # Extract the secret URL once; pass to the client (which stores it privately).
    # INV-08: we do NOT log, print, or store the URL outside the client object.
    webhook_url: str = settings.discord_webhook_url.get_secret_value()
    client = DiscordWebhookClient(webhook_url)
    return Alerter(webhook_client=client, store=store)
