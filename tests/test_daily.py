"""The daily pass: every tracked court fetched once, only changes written.

A fake client stands in for Hudle, serving the recorded Padel Fort grid; no
test touches the network. Each test names the regression it guards.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from tracker.config import Config
from tracker.daily import daily_window, fetch_range, run_daily, seed_configured_courts
from tracker.hudle import CircuitOpenError, HudleHttpError
from tracker.store import Court, Store
from tracker.types import Sport

NOW = dt.datetime(2026, 9, 23, 0, 30, tzinfo=dt.UTC)  # 06:00 IST on the 23rd
FORT = "e161ebf7-78c7-4a45-bad8-49841f38b18a"


class FakeHudle:
    """Serves one payload per facility; can be told to fail."""

    def __init__(
        self,
        payloads: dict[str, Any],
        *,
        fail: dict[str, Exception] | None = None,
        fail_long_ranges: bool = False,
        no_history_from: dt.date | None = None,
    ) -> None:
        self.payloads, self.fail = payloads, fail or {}
        self.fail_long_ranges = fail_long_ranges
        self.no_history_from = no_history_from
        self.calls: list[tuple[str, dt.date, dt.date]] = []

    def fetch_slots(
        self, venue: str, facility: str, start: dt.date, end: dt.date
    ) -> dict[str, Any]:
        self.calls.append((facility, start, end))
        if facility in self.fail:
            raise self.fail[facility]
        if self.no_history_from and start < self.no_history_from:
            raise HudleHttpError(
                403,
                '{"success":false,"code":403,'
                '"message":"Start date cannot be earlier than the current date."}',
            )
        if self.fail_long_ranges and (end - start).days > 7:
            raise HudleHttpError(502, "<html>502 Bad Gateway</html>")
        days = [
            d
            for d in self.payloads[facility]["data"]["slot_data"]
            if start.isoformat() <= d["date"] <= end.isoformat()
        ]
        return {"data": {"slot_timings": [], "slot_data": days}}


@pytest.fixture()
def store() -> Store:
    s = Store("sqlite://")
    s.initialize()
    return s


def court(facility: str, name: str = "Court") -> Court:
    return Court(facility, FORT, name, Sport.PADEL, True)


def test_the_window_is_yesterday_through_two_weeks_ahead_in_jaipur_dates() -> None:
    """Regression: a UTC 'today' that is still yesterday in Jaipur, so the pass
    never re-reads the day that just closed."""
    assert daily_window(dt.datetime(2026, 9, 22, 20, 0, tzinfo=dt.UTC), "Asia/Kolkata") == (
        dt.date(2026, 9, 22),
        dt.date(2026, 10, 7),
    )


def test_a_pass_records_every_slot_then_writes_nothing_the_next_day(
    test_config: Config, store: Store, raw_slots_padel_fort: Any
) -> None:
    """Regression: storage growing with every pass. The second identical pass
    must find nothing to write."""
    client = FakeHudle({"f1": raw_slots_padel_fort})
    window = (dt.date(2026, 9, 11), dt.date(2026, 9, 17))
    first = run_daily(test_config, store, client, now=NOW, courts=[court("f1")], window=window)  # type: ignore[arg-type]
    assert first.ok and first.courts_ok == 1 and first.slots_seen == 36 * 7 == first.slots_written
    second = run_daily(
        test_config,
        store,
        client,
        now=NOW + dt.timedelta(days=1),
        courts=[court("f1")],
        window=window,
    )  # type: ignore[arg-type]
    assert second.slots_seen == 252 and second.slots_written == 0
    assert store.latest_runs(1)[0]["courts_ok"] == 1


def test_one_court_failing_does_not_lose_the_others(
    test_config: Config, store: Store, raw_slots_padel_fort: Any
) -> None:
    """Regression: a single bad court aborting the whole daily pass."""
    client = FakeHudle(
        {"f1": raw_slots_padel_fort, "f3": raw_slots_padel_fort},
        fail={"f2": HudleHttpError(404, "gone")},
    )
    result = run_daily(
        test_config,
        store,
        client,
        now=NOW,
        window=(dt.date(2026, 9, 11), dt.date(2026, 9, 11)),
        courts=[court("f1"), court("f2"), court("f3")],
    )  # type: ignore[arg-type]
    assert (result.courts_ok, result.courts_failed) == (2, 1)
    assert not result.ok
    assert "f2" in (store.latest_runs(1)[0]["error"] or "")


def test_a_hudle_gateway_timeout_splits_the_range_instead_of_failing(
    raw_slots_padel_fort: Any,
) -> None:
    """Regression: a venue whose long grid makes Hudle return 502 dropping out
    of the dataset. The range is retried in pieces and merged."""
    client = FakeHudle({"f1": raw_slots_padel_fort}, fail_long_ranges=True)
    payload = fetch_range(client, FORT, "f1", dt.date(2026, 9, 11), dt.date(2026, 9, 25))  # type: ignore[arg-type]
    dates = [d["date"] for d in payload["data"]["slot_data"]]
    assert dates == [(dt.date(2026, 9, 11) + dt.timedelta(days=i)).isoformat() for i in range(15)]
    assert len(client.calls) == 1 + 3


def test_an_open_circuit_stops_the_pass(
    test_config: Config, store: Store, raw_slots_padel_fort: Any
) -> None:
    """Regression: hammering Hudle after it has started refusing us."""
    client = FakeHudle({"f1": raw_slots_padel_fort}, fail={"f2": CircuitOpenError(5, 5)})
    result = run_daily(
        test_config,
        store,
        client,
        now=NOW,
        window=(dt.date(2026, 9, 11), dt.date(2026, 9, 11)),
        courts=[court("f1"), court("f2"), court("f3")],
    )  # type: ignore[arg-type]
    assert result.stopped_early and result.courts_ok == 1
    assert [c[0] for c in client.calls] == ["f1", "f2"], "no request after the breaker opened"


def test_config_seeds_the_padel_courts_and_not_padel_up(test_config: Config, store: Store) -> None:
    """Regression: Padel Up polled again after it was dropped, or a tracked
    padel court missing from the daily pass."""
    seed_configured_courts(test_config, store, now=NOW)
    padel = {c.name for c in store.tracked_courts(Sport.PADEL)}
    venues = {c.venue_uuid for c in store.tracked_courts(Sport.PADEL)}
    assert padel == {"Padel Court (Outdoor)", "Padel Court"}
    assert "e606e880-0b2c-4c69-b1a8-193c8f915328" not in venues


def test_a_venue_that_refuses_past_dates_still_yields_today_onwards(
    raw_slots_padel_fort: Any,
) -> None:
    """Regression: a venue that keeps no public history (PlayAll Orbit Mall
    answers 403 to any past start date) failing every day -- and, across its
    seven courts, tripping the breaker and stopping the whole pass."""
    client = FakeHudle({"f1": raw_slots_padel_fort}, no_history_from=dt.date(2026, 9, 15))
    payload = fetch_range(
        client,
        FORT,
        "f1",
        dt.date(2026, 9, 11),
        dt.date(2026, 9, 17),  # type: ignore[arg-type]
        today=dt.date(2026, 9, 15),
    )
    assert [d["date"] for d in payload["data"]["slot_data"]] == [
        "2026-09-15",
        "2026-09-16",
        "2026-09-17",
    ]
    with pytest.raises(HudleHttpError):
        fetch_range(client, FORT, "f1", dt.date(2026, 9, 11), dt.date(2026, 9, 17))  # type: ignore[arg-type]
