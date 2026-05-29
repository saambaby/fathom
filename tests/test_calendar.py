"""Tests for data/calendar.py — EconomicCalendar, FairEconomyCalendar.

All tests use a fixture XML string; NO live HTTP calls are made.
httpx.Client.get is monkeypatched to return the fixture bytes.

INV-03 fixture test:
    The "Non-Farm Payrolls" event in the fixture is published as:
        date="06-06-2025"  (June 6, 2025)
        time="8:30am"      (America/New_York = UTC-4 in June, EDT)

    Expected UTC conversion:
        2025-06-06 08:30 EDT = 2025-06-06 12:30 UTC
    This is the load-bearing UTC-instant assertion (INV-03 sharp edge).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import httpx
import pytest

from data.calendar import (
    CalendarEvent,
    EconomicCalendar,
    FairEconomyCalendar,
    Impact,
    _IMPACT_MAP,
)

# ---------------------------------------------------------------------------
# Fixture XML
# ---------------------------------------------------------------------------

#: Representative ForexFactory-style XML with:
#:   - A USD high-impact event (NFP) at 8:30am on June 6 2025 (EDT, UTC-4 in summer)
#:   - A JPY medium-impact event at 11:00pm on June 5 2025 (EDT, UTC-4)
#:   - A EUR low-impact event
#:   - A Holiday (should map to Impact.low)
#:   - An "All Day" event (time should fall back to midnight feed TZ)
#:   - A tentative-time event (time falls back to midnight feed TZ)
#:   - A malformed event (no title, should be skipped)

FIXTURE_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<weeklyevents>
  <event>
    <title>Non-Farm Employment Change</title>
    <country>USD</country>
    <date>06-06-2025</date>
    <time>8:30am</time>
    <impact>High</impact>
    <forecast>185K</forecast>
    <previous>177K</previous>
    <actual></actual>
  </event>
  <event>
    <title>BOJ Monetary Policy Meeting Minutes</title>
    <country>JPY</country>
    <date>06-05-2025</date>
    <time>11:50pm</time>
    <impact>Medium</impact>
    <forecast></forecast>
    <previous></previous>
    <actual></actual>
  </event>
  <event>
    <title>German Industrial Production m/m</title>
    <country>EUR</country>
    <date>06-06-2025</date>
    <time>2:00am</time>
    <impact>Low</impact>
    <forecast>0.3%</forecast>
    <previous>-0.4%</previous>
    <actual></actual>
  </event>
  <event>
    <title>Bank Holiday</title>
    <country>GBP</country>
    <date>06-09-2025</date>
    <time>All Day</time>
    <impact>Holiday</impact>
    <forecast></forecast>
    <previous></previous>
    <actual></actual>
  </event>
  <event>
    <title>RBA Meeting Minutes</title>
    <country>AUD</country>
    <date>06-09-2025</date>
    <time>All Day</time>
    <impact>Medium</impact>
    <forecast></forecast>
    <previous></previous>
    <actual></actual>
  </event>
  <event>
    <title>Flash GDP q/q</title>
    <country>EUR</country>
    <date>06-10-2025</date>
    <time>Tentative</time>
    <impact>High</impact>
    <forecast></forecast>
    <previous>0.4%</previous>
    <actual></actual>
  </event>
  <event>
    <title></title>
    <country>USD</country>
    <date>06-10-2025</date>
    <time>9:00am</time>
    <impact>High</impact>
    <forecast></forecast>
    <previous></previous>
    <actual></actual>
  </event>
</weeklyevents>
"""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_calendar(
    include_next_week: bool = False,
    fixture: bytes = FIXTURE_XML,
    db_path: str = ":memory:",
) -> FairEconomyCalendar:
    """Return a FairEconomyCalendar with httpx mocked to return fixture bytes."""
    cal = FairEconomyCalendar(
        db_path,
        include_next_week=include_next_week,
        this_week_url="https://mock.test/thisweek.xml",
        next_week_url="https://mock.test/nextweek.xml",
    )
    return cal


def _mock_fetch(cal: FairEconomyCalendar, fixture: bytes = FIXTURE_XML) -> None:
    """Patch cal._fetch to return fixture bytes unconditionally."""
    cal._fetch = MagicMock(return_value=fixture)  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# ABC / interface test
# ---------------------------------------------------------------------------


class TestEconomicCalendarABC:
    def test_is_abstract(self) -> None:
        """EconomicCalendar cannot be instantiated directly."""
        with pytest.raises(TypeError):
            EconomicCalendar()  # type: ignore[abstract]

    def test_fair_economy_is_subclass(self) -> None:
        """FairEconomyCalendar is a concrete EconomicCalendar."""
        assert issubclass(FairEconomyCalendar, EconomicCalendar)


# ---------------------------------------------------------------------------
# Impact mapping
# ---------------------------------------------------------------------------


class TestImpactMap:
    def test_all_ff_strings_covered(self) -> None:
        for raw, expected in [
            ("High", Impact.high),
            ("Medium", Impact.medium),
            ("Low", Impact.low),
            ("Holiday", Impact.low),
        ]:
            assert _IMPACT_MAP[raw] == expected

    def test_unknown_defaults_to_low(self) -> None:
        cal = _make_calendar()
        _mock_fetch(cal)
        # Directly test the method that applies the default.
        events = cal._parse_xml(
            b"""<weeklyevents>
              <event>
                <title>Mystery Event</title>
                <country>USD</country>
                <date>06-06-2025</date>
                <time>9:00am</time>
                <impact>Unknown</impact>
              </event>
            </weeklyevents>"""
        )
        assert len(events) == 1
        assert events[0].impact == Impact.low


# ---------------------------------------------------------------------------
# XML parsing — basic
# ---------------------------------------------------------------------------


class TestParseXML:
    def setup_method(self) -> None:
        self.cal = _make_calendar()
        _mock_fetch(self.cal)
        self.events = self.cal._parse_xml(FIXTURE_XML)

    def test_malformed_event_skipped(self) -> None:
        """The empty-title event in the fixture is silently skipped."""
        titles = [e.event_name for e in self.events]
        # 7 events in fixture, 1 malformed (no title) → 6 valid
        assert len(self.events) == 6
        assert "" not in titles

    def test_currency_tagged(self) -> None:
        """Each event carries the correct currency code."""
        currencies = {e.currency for e in self.events}
        assert "USD" in currencies
        assert "JPY" in currencies
        assert "EUR" in currencies
        assert "GBP" in currencies

    def test_impact_normalised(self) -> None:
        """FF impact strings are converted to our enum."""
        nfp = next(e for e in self.events if e.event_name == "Non-Farm Employment Change")
        assert nfp.impact == Impact.high

        boj = next(e for e in self.events if "BOJ" in e.event_name)
        assert boj.impact == Impact.medium

        german_ip = next(e for e in self.events if "German" in e.event_name)
        assert german_ip.impact == Impact.low

        holiday = next(e for e in self.events if e.event_name == "Bank Holiday")
        assert holiday.impact == Impact.low  # Holiday → low

    def test_forecast_and_previous_captured(self) -> None:
        nfp = next(e for e in self.events if e.event_name == "Non-Farm Employment Change")
        assert nfp.forecast == "185K"
        assert nfp.previous == "177K"
        assert nfp.actual is None  # empty tag → None

    def test_all_times_utc_aware(self) -> None:
        """Every parsed event has a UTC-aware datetime (INV-03)."""
        for ev in self.events:
            assert ev.time.tzinfo is not None
            assert ev.time.tzinfo == timezone.utc or str(ev.time.tzinfo) in (
                "UTC",
                "datetime.timezone.utc",
            )
            # Confirm it's actually UTC by normalising.
            assert ev.time.utcoffset() == timedelta(0)


# ---------------------------------------------------------------------------
# INV-03 sharp edge: feed TZ → UTC conversion
# ---------------------------------------------------------------------------


class TestFeedTZConversion:
    """The load-bearing UTC-instant assertions for INV-03.

    Non-Farm Employment Change in the fixture:
        date = 2025-06-06
        time = 8:30am   (America/New_York, EDT = UTC-4 in June)
        → expected UTC: 2025-06-06T12:30:00+00:00

    BOJ Meeting Minutes:
        date = 2025-06-05
        time = 11:50pm  (America/New_York, EDT = UTC-4)
        → expected UTC: 2025-06-06T03:50:00+00:00  (next calendar day!)
    """

    def setup_method(self) -> None:
        self.cal = _make_calendar()
        _mock_fetch(self.cal)
        self.events = self.cal._parse_xml(FIXTURE_XML)

    def test_nfp_utc_instant(self) -> None:
        """NFP at 8:30am EDT converts to 12:30 UTC (INV-03 sharp edge)."""
        nfp = next(e for e in self.events if e.event_name == "Non-Farm Employment Change")
        expected_utc = datetime(2025, 6, 6, 12, 30, 0, tzinfo=timezone.utc)
        assert nfp.time == expected_utc, (
            f"NFP time {nfp.time.isoformat()} != expected {expected_utc.isoformat()}. "
            "Feed TZ→UTC conversion is broken (INV-03)."
        )

    def test_boj_utc_crosses_midnight(self) -> None:
        """BOJ at 11:50pm EDT on June 5 → 03:50 UTC on June 6 (crosses midnight)."""
        boj = next(e for e in self.events if "BOJ" in e.event_name)
        expected_utc = datetime(2025, 6, 6, 3, 50, 0, tzinfo=timezone.utc)
        assert boj.time == expected_utc, (
            f"BOJ time {boj.time.isoformat()} != expected {expected_utc.isoformat()}. "
            "Feed TZ→UTC conversion is broken for events near midnight (INV-03)."
        )

    def test_german_ip_utc(self) -> None:
        """German IP at 2:00am EDT → 06:00 UTC."""
        german_ip = next(e for e in self.events if "German" in e.event_name)
        expected_utc = datetime(2025, 6, 6, 6, 0, 0, tzinfo=timezone.utc)
        assert german_ip.time == expected_utc

    def test_all_day_falls_back_to_midnight_utc(self) -> None:
        """'All Day' events use midnight feed TZ → midnight + UTC offset."""
        holiday = next(e for e in self.events if e.event_name == "Bank Holiday")
        # June 9 2025, midnight EDT (UTC-4) = 04:00 UTC
        expected_utc = datetime(2025, 6, 9, 4, 0, 0, tzinfo=timezone.utc)
        assert holiday.time == expected_utc

    def test_tentative_falls_back_to_midnight_utc(self) -> None:
        """'Tentative' times use midnight feed TZ → midnight + UTC offset."""
        flash_gdp = next(e for e in self.events if e.event_name == "Flash GDP q/q")
        # June 10 2025, midnight EDT (UTC-4) = 04:00 UTC
        expected_utc = datetime(2025, 6, 10, 4, 0, 0, tzinfo=timezone.utc)
        assert flash_gdp.time == expected_utc


# ---------------------------------------------------------------------------
# Persistence — idempotent upsert
# ---------------------------------------------------------------------------


class TestIdempotentUpsert:
    def test_refresh_idempotent(self) -> None:
        """Refreshing twice does not create duplicate rows."""
        cal = _make_calendar()
        _mock_fetch(cal)

        count1 = cal.refresh()
        assert count1 == 6  # 6 valid events in fixture

        count2 = cal.refresh()
        assert count2 == 6  # same count returned (upsert, no dupes)

        # Verify at the DB level.
        cursor = cal._conn.execute("SELECT COUNT(*) FROM calendar_events")
        row_count = cursor.fetchone()[0]
        assert row_count == 6, f"Expected 6 rows in DB, got {row_count} after 2 refreshes."

    def test_upsert_updates_fields(self) -> None:
        """Upserting an event with updated forecast replaces the old row."""
        cal = _make_calendar()
        _mock_fetch(cal)
        cal.refresh()

        # Build a modified version of the fixture with an updated forecast for NFP.
        modified_xml = FIXTURE_XML.replace(b"<forecast>185K</forecast>", b"<forecast>200K</forecast>")
        cal._fetch = MagicMock(return_value=modified_xml)  # type: ignore[method-assign]
        cal.refresh()

        # Should still be 6 rows.
        cursor = cal._conn.execute("SELECT COUNT(*) FROM calendar_events")
        assert cursor.fetchone()[0] == 6

        # The forecast should be updated to 200K.
        cursor = cal._conn.execute(
            "SELECT forecast FROM calendar_events WHERE event_name = ?",
            ("Non-Farm Employment Change",),
        )
        row = cursor.fetchone()
        assert row is not None
        assert row[0] == "200K"


# ---------------------------------------------------------------------------
# upcoming_events query
# ---------------------------------------------------------------------------


class TestUpcomingEvents:
    def _seed(self) -> FairEconomyCalendar:
        cal = _make_calendar()
        _mock_fetch(cal)
        cal.refresh()
        return cal

    def test_currency_filter(self) -> None:
        """upcoming_events respects the currency filter."""
        cal = self._seed()

        # Patch now so NFP (2025-06-06 12:30 UTC) is within a 7-day window.
        mock_now = datetime(2025, 6, 5, 0, 0, 0, tzinfo=timezone.utc)
        with patch("data.calendar.datetime") as mock_dt:
            mock_dt.now.return_value = mock_now
            mock_dt.fromisoformat = datetime.fromisoformat
            events = cal.upcoming_events(["USD"], timedelta(days=7))

        assert all(e.currency == "USD" for e in events)
        titles = [e.event_name for e in events]
        assert "Non-Farm Employment Change" in titles

    def test_empty_currency_returns_all(self) -> None:
        """Empty currencies list returns events for all currencies."""
        cal = self._seed()

        mock_now = datetime(2025, 6, 5, 0, 0, 0, tzinfo=timezone.utc)
        with patch("data.calendar.datetime") as mock_dt:
            mock_dt.now.return_value = mock_now
            mock_dt.fromisoformat = datetime.fromisoformat
            events = cal.upcoming_events([], timedelta(days=7))

        currencies_found = {e.currency for e in events}
        assert "USD" in currencies_found
        assert "JPY" in currencies_found
        assert "EUR" in currencies_found

    def test_no_events_before_window(self) -> None:
        """Events before now are excluded."""
        cal = self._seed()

        # Set now to far in the future — no events should be within the window.
        mock_now = datetime(2030, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        with patch("data.calendar.datetime") as mock_dt:
            mock_dt.now.return_value = mock_now
            mock_dt.fromisoformat = datetime.fromisoformat
            events = cal.upcoming_events([], timedelta(days=7))

        assert events == []

    def test_sorted_by_time(self) -> None:
        """upcoming_events returns events sorted by time ascending."""
        cal = self._seed()

        mock_now = datetime(2025, 6, 5, 0, 0, 0, tzinfo=timezone.utc)
        with patch("data.calendar.datetime") as mock_dt:
            mock_dt.now.return_value = mock_now
            mock_dt.fromisoformat = datetime.fromisoformat
            events = cal.upcoming_events([], timedelta(days=14))

        times = [e.time for e in events]
        assert times == sorted(times)


# ---------------------------------------------------------------------------
# CalendarEvent model validation
# ---------------------------------------------------------------------------


class TestCalendarEventModel:
    def test_rejects_naive_datetime(self) -> None:
        """CalendarEvent raises ValueError for naive datetimes (INV-03)."""
        with pytest.raises(ValueError, match="UTC-aware"):
            CalendarEvent(
                currency="USD",
                event_name="Test",
                time=datetime(2025, 6, 6, 12, 30),  # naive — not UTC-aware
                impact=Impact.high,
            )

    def test_accepts_utc_aware_datetime(self) -> None:
        ev = CalendarEvent(
            currency="USD",
            event_name="NFP",
            time=datetime(2025, 6, 6, 12, 30, tzinfo=timezone.utc),
            impact=Impact.high,
            forecast="185K",
        )
        assert ev.time.tzinfo is not None
        assert ev.currency == "USD"
        assert ev.forecast == "185K"
        assert ev.actual is None

    def test_equality_and_hash(self) -> None:
        t = datetime(2025, 6, 6, 12, 30, tzinfo=timezone.utc)
        ev1 = CalendarEvent(currency="USD", event_name="NFP", time=t, impact=Impact.high)
        ev2 = CalendarEvent(currency="USD", event_name="NFP", time=t, impact=Impact.high)
        assert ev1 == ev2
        assert hash(ev1) == hash(ev2)


# ---------------------------------------------------------------------------
# next-week fetch
# ---------------------------------------------------------------------------


class TestNextWeekFetch:
    def test_include_next_week_calls_fetch_twice(self) -> None:
        """With include_next_week=True, _fetch is called twice."""
        cal = _make_calendar(include_next_week=True)
        mock_fetch = MagicMock(return_value=FIXTURE_XML)
        cal._fetch = mock_fetch  # type: ignore[method-assign]

        cal.refresh()

        assert mock_fetch.call_count == 2

    def test_exclude_next_week_calls_fetch_once(self) -> None:
        """With include_next_week=False, _fetch is called once."""
        cal = _make_calendar(include_next_week=False)
        mock_fetch = MagicMock(return_value=FIXTURE_XML)
        cal._fetch = mock_fetch  # type: ignore[method-assign]

        cal.refresh()

        assert mock_fetch.call_count == 1

    def test_next_week_404_is_best_effort_not_fatal(self) -> None:
        """A failing next-week fetch must NOT lose this-week events (live 404).

        The real ff_calendar_nextweek.xml URL returns 404; refresh() must
        swallow the httpx error, log, and still persist this-week events.
        """
        cal = _make_calendar(include_next_week=True)
        # this-week fetch succeeds, next-week fetch raises (as the live 404 does)
        mock_fetch = MagicMock(
            side_effect=[FIXTURE_XML, httpx.HTTPError("simulated 404 Not Found")]
        )
        cal._fetch = mock_fetch  # type: ignore[method-assign]

        # Must not raise despite the next-week failure.
        n = cal.refresh()

        # Both feeds were attempted (best-effort, not short-circuited)...
        assert mock_fetch.call_count == 2
        # ...and the this-week events were still parsed and stored despite the
        # next-week 404 (the events that were lost in the live acceptance bug).
        assert n > 0
        row_count = cal._conn.execute(
            "SELECT COUNT(*) FROM calendar_events"
        ).fetchone()[0]
        assert row_count == n
