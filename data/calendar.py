"""Economic calendar — CalendarEvent model, EconomicCalendar ABC, and
FairEconomyCalendar provider (FairEconomy / ForexFactory weekly XML feed).

INV-03 (sharp edge):
    The ForexFactory XML feed publishes event times in a fixed display timezone
    — historically US Eastern (America/New_York, UTC-5 in winter / UTC-4 in
    summer, i.e. with DST).  The feed does NOT emit UTC.  Every date+time pair
    read from the XML is therefore interpreted as America/New_York and converted
    to UTC before constructing CalendarEvent.time.

    If the feed TZ assumption is wrong, every event will be silently shifted.
    The assumption is documented here and verified by a fixture test that checks
    a known event against its expected UTC instant.

    Source TZ constant: FEED_TZ = "America/New_York"

Impact normalisation (from FF XML <impact> field):
    High    → Impact.high
    Medium  → Impact.medium
    Low     → Impact.low
    Holiday → Impact.low  (treated as low-impact, not skipped, so calendar
                            consumers can filter or highlight holidays explicitly)

INV-08: The free FairEconomy feed requires no API key.  If a configurable URL
    is added, it lives in .env (Settings), never hardcoded with a secret.

INV-09: No demo/live branch in logic; any future provider-URL config is read
    from Settings the same way regardless of env.
"""

from __future__ import annotations

import logging
import sqlite3
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Optional
from zoneinfo import ZoneInfo

import httpx

from data.store import _to_rfc3339  # shared RFC 3339 formatter (INV-03)

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Feed timezone assumption (INV-03)
# ---------------------------------------------------------------------------

#: The ForexFactory XML feed publishes times in US Eastern (with DST).
#: This constant is the single place that records and enforces that assumption.
FEED_TZ: str = "America/New_York"

#: Default feed URLs (no auth required — INV-08).
FF_THIS_WEEK_URL: str = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"
FF_NEXT_WEEK_URL: str = "https://nfs.faireconomy.media/ff_calendar_nextweek.xml"

#: httpx timeout in seconds.  httpx default is None (no timeout); we set an
#: explicit value so a stalled feed does not hang the process indefinitely.
HTTP_TIMEOUT_SECONDS: float = 10.0

# ---------------------------------------------------------------------------
# Impact enum
# ---------------------------------------------------------------------------


class Impact(str, Enum):
    """Normalised event-impact level (high/medium/low)."""

    high = "high"
    medium = "medium"
    low = "low"


# Mapping from raw FF XML <impact> strings to our enum.
# Holiday is mapped to low (not skipped) so consumers can filter explicitly.
_IMPACT_MAP: dict[str, Impact] = {
    "High": Impact.high,
    "Medium": Impact.medium,
    "Low": Impact.low,
    "Holiday": Impact.low,
}

# ---------------------------------------------------------------------------
# CalendarEvent model
# ---------------------------------------------------------------------------


class CalendarEvent:
    """A single economic calendar event, fully normalised and UTC-timestamped.

    Args:
        currency: ISO 4217 currency code (e.g. "USD", "EUR", "JPY").
        event_name: Human-readable event title (e.g. "Non-Farm Payrolls").
        time: UTC-aware datetime for the event (INV-03).  For ``All Day`` or
            tentative events, the time is set to midnight UTC on the event date.
        impact: Normalised impact level (high/medium/low).
        actual: Actual released value (optional; typically populated after the
            event has occurred).
        forecast: Consensus forecast value (optional).
        previous: Previous period's value (optional).
    """

    __slots__ = (
        "currency",
        "event_name",
        "time",
        "impact",
        "actual",
        "forecast",
        "previous",
    )

    def __init__(
        self,
        *,
        currency: str,
        event_name: str,
        time: datetime,
        impact: Impact,
        actual: Optional[str] = None,
        forecast: Optional[str] = None,
        previous: Optional[str] = None,
    ) -> None:
        if time.tzinfo is None:
            raise ValueError(
                f"CalendarEvent.time must be UTC-aware (INV-03); got naive "
                f"datetime for event '{event_name}'."
            )
        self.currency = currency
        self.event_name = event_name
        self.time = time
        self.impact = impact
        self.actual = actual
        self.forecast = forecast
        self.previous = previous

    def __repr__(self) -> str:
        return (
            f"CalendarEvent(currency={self.currency!r}, "
            f"event_name={self.event_name!r}, "
            f"time={self.time.isoformat()!r}, "
            f"impact={self.impact.value!r})"
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, CalendarEvent):
            return NotImplemented
        return (
            self.currency == other.currency
            and self.event_name == other.event_name
            and self.time == other.time
            and self.impact == other.impact
        )

    def __hash__(self) -> int:
        return hash((self.currency, self.event_name, self.time))


# ---------------------------------------------------------------------------
# EconomicCalendar ABC
# ---------------------------------------------------------------------------


class EconomicCalendar(ABC):
    """Abstract base for economic calendar providers.

    Concrete implementations (e.g. FairEconomyCalendar) fetch events from a
    specific data source, normalise them into CalendarEvent objects, and persist
    them to SQLite.  Swapping providers only requires a new concrete class —
    no logic in consumers needs to change.
    """

    @abstractmethod
    def refresh(self) -> int:
        """Fetch the latest calendar data from the upstream source and persist
        it to the local store.

        Returns:
            The number of events upserted (new + updated).
        """
        ...

    @abstractmethod
    def upcoming_events(
        self,
        currencies: list[str],
        window: timedelta,
    ) -> list[CalendarEvent]:
        """Return events starting within ``window`` from now for the given
        currencies.

        Args:
            currencies: List of ISO 4217 currency codes to filter by
                (e.g. ["USD", "EUR"]).  An empty list returns events for
                all currencies.
            window: How far ahead from ``datetime.now(timezone.utc)`` to look.

        Returns:
            List of CalendarEvent objects sorted by time ascending.
        """
        ...


# ---------------------------------------------------------------------------
# FairEconomyCalendar — concrete provider
# ---------------------------------------------------------------------------


class FairEconomyCalendar(EconomicCalendar):
    """EconomicCalendar backed by the free FairEconomy/ForexFactory weekly XML.

    Fetches ``ff_calendar_thisweek.xml`` (and optionally ``nextweek``) via
    httpx, parses with stdlib xml.etree.ElementTree, normalises, and persists
    to a ``calendar_events`` SQLite table.

    Feed timezone:
        Event times are published in US Eastern (America/New_York, DST-aware).
        Each date+time is converted to UTC before CalendarEvent.time is set
        (INV-03 enforcement).  See FEED_TZ constant.

    Args:
        db_path: File path (or ``":memory:"``) for the SQLite database.
            The ``calendar_events`` table is created here (alongside the
            existing candles/instruments tables if the same DB is used).
        include_next_week: If True, also fetches the ``nextweek`` XML feed.
            Defaults to True (gives ~2 weeks of lookahead).
        this_week_url: Override for the thisweek feed URL (for testing).
        next_week_url: Override for the nextweek feed URL (for testing).
        http_timeout: Request timeout in seconds (default 10 s).
    """

    #: DDL for the calendar_events table.
    _CREATE_CALENDAR_SQL: str = """
        CREATE TABLE IF NOT EXISTS calendar_events (
            currency    TEXT    NOT NULL,
            event_name  TEXT    NOT NULL,
            time        TEXT    NOT NULL,
            impact      TEXT    NOT NULL,
            actual      TEXT,
            forecast    TEXT,
            previous    TEXT,
            PRIMARY KEY (currency, event_name, time)
        )
    """

    #: Upsert SQL — idempotent on (currency, event_name, time) PK.
    _UPSERT_CALENDAR_SQL: str = """
        INSERT OR REPLACE INTO calendar_events
            (currency, event_name, time, impact, actual, forecast, previous)
        VALUES
            (?, ?, ?, ?, ?, ?, ?)
    """

    def __init__(
        self,
        db_path: str,
        *,
        include_next_week: bool = False,
        this_week_url: str = FF_THIS_WEEK_URL,
        next_week_url: str = FF_NEXT_WEEK_URL,
        http_timeout: float = HTTP_TIMEOUT_SECONDS,
    ) -> None:
        self._db_path = db_path
        self._include_next_week = include_next_week
        self._this_week_url = this_week_url
        self._next_week_url = next_week_url
        self._http_timeout = http_timeout
        self._feed_tz = ZoneInfo(FEED_TZ)

        self._conn: sqlite3.Connection = sqlite3.connect(db_path)
        self._conn.execute(self._CREATE_CALENDAR_SQL)
        self._conn.commit()

    # ------------------------------------------------------------------
    # Public API (EconomicCalendar ABC)
    # ------------------------------------------------------------------

    def refresh(self) -> int:
        """Fetch the weekly feed(s) and upsert all events into the DB.

        Returns:
            Total number of events upserted (including both weeks if
            include_next_week is True).
        """
        events: list[CalendarEvent] = []
        xml_bytes = self._fetch(self._this_week_url)
        events.extend(self._parse_xml(xml_bytes))

        if self._include_next_week:
            # Best-effort: the next-week feed is an optional extension and its
            # URL is not always available (e.g. returns 404). A failure here
            # must NOT lose the this-week events we already have — log and
            # continue rather than propagating.
            try:
                xml_bytes_next = self._fetch(self._next_week_url)
                events.extend(self._parse_xml(xml_bytes_next))
            except httpx.HTTPError as exc:
                _log.warning(
                    "next-week calendar feed unavailable (%s) — "
                    "continuing with this-week events only.",
                    exc,
                )

        self._upsert(events)
        return len(events)

    def upcoming_events(
        self,
        currencies: list[str],
        window: timedelta,
    ) -> list[CalendarEvent]:
        """Query the local DB for events within ``window`` from now.

        Args:
            currencies: ISO 4217 currency codes to include.  Empty → all.
            window: Look-ahead window from ``datetime.now(timezone.utc)``.

        Returns:
            CalendarEvent list sorted by time ascending.
        """
        now = datetime.now(timezone.utc)
        end = now + window

        now_str = _to_rfc3339(now)
        end_str = _to_rfc3339(end)

        if currencies:
            placeholders = ", ".join("?" * len(currencies))
            sql = f"""
                SELECT currency, event_name, time, impact, actual, forecast, previous
                FROM   calendar_events
                WHERE  currency IN ({placeholders})
                  AND  time >= ?
                  AND  time <= ?
                ORDER  BY time ASC
            """
            params: tuple[str | None, ...] = (*currencies, now_str, end_str)
        else:
            sql = """
                SELECT currency, event_name, time, impact, actual, forecast, previous
                FROM   calendar_events
                WHERE  time >= ?
                  AND  time <= ?
                ORDER  BY time ASC
            """
            params = (now_str, end_str)

        cursor = self._conn.execute(sql, params)
        results: list[CalendarEvent] = []
        for row in cursor.fetchall():
            currency, event_name, time_str, impact_str, actual, forecast, previous = row
            # Parse stored UTC RFC 3339 string back to UTC-aware datetime.
            dt = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
            results.append(
                CalendarEvent(
                    currency=currency,
                    event_name=event_name,
                    time=dt,
                    impact=Impact(impact_str),
                    actual=actual,
                    forecast=forecast,
                    previous=previous,
                )
            )
        return results

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        self._conn.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fetch(self, url: str) -> bytes:
        """Fetch an XML URL and return the raw bytes.

        Uses an explicit timeout (HTTP_TIMEOUT_SECONDS) because httpx's
        default is None (no timeout), which would hang indefinitely on a
        stalled feed.

        Args:
            url: URL to fetch.

        Returns:
            Raw response bytes.

        Raises:
            httpx.HTTPStatusError: If the server returns a 4xx/5xx response.
            httpx.TimeoutException: If the request exceeds the configured timeout.
        """
        with httpx.Client(timeout=self._http_timeout) as client:
            resp = client.get(url)
            resp.raise_for_status()
            return resp.content

    def _parse_xml(self, xml_bytes: bytes) -> list[CalendarEvent]:
        """Parse a ForexFactory XML feed and return CalendarEvent objects.

        Each ``<event>`` element is expected to contain:
            <title>       — event name
            <country>     — currency code (e.g. USD, EUR, JPY)
            <date>        — date string (e.g. "01-06-2026")
            <time>        — time string in feed TZ (e.g. "8:30am") or "All Day"
            <impact>      — High / Medium / Low / Holiday
            <forecast>    — optional consensus value
            <previous>    — optional prior value

        Times are parsed as America/New_York (see FEED_TZ) and converted to UTC
        (INV-03).  Missing or ``All Day`` / tentative times are set to midnight
        on the event date in the feed TZ (then converted to UTC).

        Args:
            xml_bytes: Raw XML bytes from the feed.

        Returns:
            List of CalendarEvent objects.  Events with unrecognised impact
            values are assigned Impact.low and included (defensive; don't drop).
        """
        root = ET.fromstring(xml_bytes.decode("utf-8"))
        events: list[CalendarEvent] = []

        for event_el in root.iter("event"):
            title = _text(event_el, "title") or ""
            country = _text(event_el, "country") or ""
            date_str = _text(event_el, "date") or ""
            time_str = _text(event_el, "time") or ""
            impact_str = _text(event_el, "impact") or ""
            forecast = _text(event_el, "forecast") or None
            previous = _text(event_el, "previous") or None
            actual = _text(event_el, "actual") or None

            if not title or not country or not date_str:
                # Skip malformed entries silently (defensive).
                continue

            # Normalise impact — default to low for unknown strings.
            impact = _IMPACT_MAP.get(impact_str, Impact.low)

            # Parse date+time from the feed TZ and convert to UTC.
            utc_time = self._parse_feed_datetime(date_str, time_str)
            if utc_time is None:
                # If date parsing fails completely, skip the event.
                continue

            events.append(
                CalendarEvent(
                    currency=country.strip().upper(),
                    event_name=title.strip(),
                    time=utc_time,
                    impact=impact,
                    actual=actual if actual else None,
                    forecast=forecast if forecast else None,
                    previous=previous if previous else None,
                )
            )

        return events

    def _parse_feed_datetime(
        self,
        date_str: str,
        time_str: str,
    ) -> Optional[datetime]:
        """Convert a (date_str, time_str) pair from feed TZ to UTC.

        The feed uses dates like "01-06-2026" (MM-DD-YYYY) and times like
        "8:30am", "12:00pm", or "All Day" / "Tentative" / empty.

        Missing / ``All Day`` / tentative times are treated as midnight in the
        feed TZ on the given date.

        Args:
            date_str: Date string from ``<date>`` element (MM-DD-YYYY).
            time_str: Time string from ``<time>`` element.

        Returns:
            UTC-aware datetime, or None if the date cannot be parsed.
        """
        # Parse the date portion.
        try:
            naive_date = datetime.strptime(date_str.strip(), "%m-%d-%Y")
        except ValueError:
            return None

        # Parse the time portion; fall back to midnight for missing/All Day.
        clean_time = time_str.strip().lower()
        hour = 0
        minute = 0

        if clean_time and clean_time not in ("all day", "tentative", ""):
            try:
                # Parse "8:30am" / "12:00pm" style times.
                t = datetime.strptime(clean_time, "%I:%M%p")
                hour = t.hour
                minute = t.minute
            except ValueError:
                # Unrecognised format — use midnight.
                pass

        # Combine into a feed-TZ-aware datetime, then convert to UTC.
        feed_naive = naive_date.replace(hour=hour, minute=minute, second=0, microsecond=0)
        feed_aware = feed_naive.replace(tzinfo=self._feed_tz)
        utc_aware = feed_aware.astimezone(timezone.utc)
        return utc_aware

    def _upsert(self, events: list[CalendarEvent]) -> None:
        """Persist events to calendar_events with INSERT OR REPLACE (idempotent).

        The PK is (currency, event_name, time), so re-parsing the same feed
        only updates fields like actual/forecast without creating duplicates.

        Args:
            events: List of CalendarEvent objects to persist.
        """
        if not events:
            return

        params = [
            (
                ev.currency,
                ev.event_name,
                _to_rfc3339(ev.time),   # UTC RFC 3339 (INV-03)
                ev.impact.value,
                ev.actual,
                ev.forecast,
                ev.previous,
            )
            for ev in events
        ]
        self._conn.executemany(self._UPSERT_CALENDAR_SQL, params)
        self._conn.commit()


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


def _text(el: ET.Element, tag: str) -> Optional[str]:
    """Return the text of the first child with the given tag, or None."""
    child = el.find(tag)
    if child is None or child.text is None:
        return None
    return child.text.strip() or None
