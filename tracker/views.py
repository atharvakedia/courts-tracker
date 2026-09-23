"""The dashboard's answers, one per view: sport x window x (optionally) venue.

A view is everything the page draws for one choice of sport, window and
venue. The slot data changes once a day, so every view is built once, by the
daily pass, and stored whole; the API then answers a click with one row
instead of recomputing from hundreds of thousands of slots.

Building a window reads its rows once and judges every court once, then
narrows to each venue from there, so the market view and every venue view of
a window share one pass over the data.

Windows are settled business dates only (the day before ``today`` and back),
in Jaipur time. Each view also carries the same figures for the window before
it, so the page can say whether occupancy rose or fell.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from tracker.insights import (
    Row,
    Verdict,
    by,
    by_date,
    by_hour,
    by_weekday,
    court_day_spread,
    heatmap,
    lead_histogram,
    lead_summary,
    lead_times,
    occupancy,
    peak,
    price_per_hour,
    reliability,
    settled,
)
from tracker.store import Court, Store
from tracker.types import Sport, local_wall_clock

WINDOWS = {"7": 7, "30": 30, "all": 90}
#: Reliability looks this far ahead for venues holding every slot.
LOOKAHEAD_DAYS = 14
#: A venue first seen this recently is flagged as new on the dashboard.
NEW_VENUE_DAYS = 14
#: From this hour on, a build is for the next day's dashboard: the evening pass
#: has read the day that is about to end, and that is what readers see after
#: midnight.
NEXT_DAY_FROM_HOUR = 22


class UnknownVenueError(LookupError):
    """The venue has no rows for this sport in this window."""


def view_key(sport: Sport, window: str, venue: str | None) -> str:
    return f"{sport.value}|{window}|{venue or ''}"


def views_as_of(now: dt.datetime, tz: str) -> dt.date:
    """The Jaipur date the views built now will be read on."""
    local = local_wall_clock(now, tz)
    return local.date() + dt.timedelta(days=1 if local.hour >= NEXT_DAY_FROM_HOUR else 0)


def rows_for(store: Store, sport: Sport, today: dt.date) -> Sequence[Row]:
    """Every row any window of this sport needs: the longest window, the one
    before it, and the look-ahead reliability reads."""
    longest = max(WINDOWS.values())
    return store.slots_between(
        sport=sport,
        date_from=today - dt.timedelta(days=2 * longest),
        date_to=today + dt.timedelta(days=LOOKAHEAD_DAYS),
    )


@dataclass(frozen=True, slots=True)
class _Window:
    """One window of one sport, judged once; views are narrowings of it."""

    sport: Sport
    key: str
    today: dt.date
    start: dt.date
    end: dt.date
    prev_start: dt.date
    rows: list[Row]
    counted: list[Row]
    previous: list[Row]
    per_court: dict[str, list[Row]]
    venue_rows: list[dict[str, Any]]


def _window(
    rows: Sequence[Row],
    courts: Mapping[str, Court],
    venues: Mapping[str, dict[str, Any]],
    *,
    sport: Sport,
    window: str,
    today: dt.date,
) -> _Window:
    days = WINDOWS[window]
    start, end = today - dt.timedelta(days=days), today - dt.timedelta(days=1)
    prev_start = start - dt.timedelta(days=days)
    last = today + dt.timedelta(days=LOOKAHEAD_DAYS)
    before = [r for r in rows if prev_start <= r["business_date"] < start]
    current = [r for r in rows if start <= r["business_date"] <= last]

    per_court = by(current, "facility_uuid")
    verdicts = {fid: reliability(court_rows, today) for fid, court_rows in per_court.items()}
    past = settled(current, today)
    # Every court except a dead listing is counted; low-activity courts included,
    # since their quiet days are real demand and leaving them out inflates it.
    counted_verdicts = {Verdict.RELIABLE.value, Verdict.PARTIAL.value}
    counted = [r for r in past if verdicts[r["facility_uuid"]]["verdict"] in counted_verdicts]
    counted_courts = {r["facility_uuid"] for r in counted}
    # The previous window is judged on the courts counted now, so the change
    # figure compares like with like rather than a different court set.
    previous = [r for r in before if r["facility_uuid"] in counted_courts]

    # "New" means it appeared after tracking began, not that it arrived in the
    # first load: the backfill makes every venue first-seen on the same day.
    tracking_start = min((v["first_seen_at"] for v in venues.values()), default=None)

    def is_new(v: Mapping[str, Any]) -> bool:
        seen = v.get("first_seen_at")
        if not seen or not tracking_start:
            return False
        recent = seen.date() >= today - dt.timedelta(days=NEW_VENUE_DAYS)
        return bool(seen - tracking_start > dt.timedelta(days=1) and recent)

    venue_rows = []
    for venue_uuid, vrows in by(past, "venue_uuid").items():
        court_ids = sorted({r["facility_uuid"] for r in vrows})
        v = venues.get(venue_uuid, {})
        venue_rows.append(
            {
                "venue_uuid": venue_uuid,
                "name": v.get("name", venue_uuid),
                "new": is_new(v),
                "latitude": v.get("latitude"),
                "longitude": v.get("longitude"),
                "courts": [
                    {
                        "facility_uuid": f,
                        "name": courts[f].name if f in courts else f,
                        **verdicts[f],
                    }
                    for f in court_ids
                ],
                "verdict": _venue_verdict([verdicts[f]["verdict"] for f in court_ids]),
                "price_per_hour": price_per_hour(vrows),
                **occupancy(vrows).as_dict(),
            }
        )
    venue_rows.sort(key=lambda v: (-(v["occupancy"] or 0), v["name"]))
    return _Window(
        sport,
        window,
        today,
        start,
        end,
        prev_start,
        current,
        counted,
        previous,
        per_court,
        venue_rows,
    )


def _payload(w: _Window, venue: str | None) -> dict[str, Any]:
    counted, previous = w.counted, w.previous
    if venue is not None:
        if venue not in {r["venue_uuid"] for r in w.rows}:
            raise UnknownVenueError(venue)
        counted = [r for r in counted if r["venue_uuid"] == venue]
        previous = [r for r in previous if r["venue_uuid"] == venue]
    hours = lead_times(counted)
    hourly = by_hour(counted)
    weekdays = by_weekday(counted)
    return {
        "sport": w.sport.value,
        "venue": venue,
        "window": {
            "key": w.key,
            "start": w.start.isoformat(),
            "end": w.end.isoformat(),
            "days": len({r["business_date"] for r in counted}),
        },
        "rule": "A slot is booked or vacant; slots a venue blocks count as booked.",
        "totals": {
            **occupancy(counted).as_dict(),
            "courts_counted": len({r["facility_uuid"] for r in counted}),
            "courts_tracked": len(
                {
                    f
                    for f, court_rows in w.per_court.items()
                    if venue is None or court_rows[0]["venue_uuid"] == venue
                }
            ),
        },
        "previous": {
            "start": w.prev_start.isoformat(),
            "end": (w.start - dt.timedelta(days=1)).isoformat(),
            **occupancy(previous).as_dict(),
        },
        "peaks": {"hour": peak(hourly), "weekday": peak(weekdays)},
        "venues": w.venue_rows,
        "days": by_date(counted),
        "hours": hourly,
        "weekdays": weekdays,
        "spread": court_day_spread(counted),
        "heatmap": heatmap(counted),
        "lead_time": lead_summary(hours),
        "lead_histogram": lead_histogram(hours),
    }


def overview(
    rows: Sequence[Row],
    courts: Mapping[str, Court],
    venues: Mapping[str, dict[str, Any]],
    *,
    sport: Sport,
    window: str,
    today: dt.date,
    venue: str | None = None,
) -> dict[str, Any]:
    """One view, computed on the spot. Raises UnknownVenueError for a venue
    with no rows of this sport in the window."""
    w = _window(rows, courts, venues, sport=sport, window=window, today=today)
    return _payload(w, venue)


def all_views(store: Store, today: dt.date) -> Iterator[tuple[str, dict[str, Any]]]:
    """Every view the page can ask for, keyed by :func:`view_key`: each sport
    and window for the whole market and for each of its venues."""
    venues = store.venues()
    for sport in Sport:
        rows = rows_for(store, sport, today)
        courts = {c.facility_uuid: c for c in store.tracked_courts(sport)}
        for window in WINDOWS:
            w = _window(rows, courts, venues, sport=sport, window=window, today=today)
            yield view_key(sport, window, None), _payload(w, None)
            for v in w.venue_rows:
                yield view_key(sport, window, v["venue_uuid"]), _payload(w, v["venue_uuid"])


def publish_views(store: Store, *, now: dt.datetime, tz: str) -> int:
    """Build every view and replace the stored set. Returns how many were stored."""
    as_of = views_as_of(now, tz)
    built = [(key, json.dumps(payload, default=str)) for key, payload in all_views(store, as_of)]
    store.replace_views(built, as_of=as_of, built_at=now)
    return len(built)


def _venue_verdict(court_verdicts: list[str]) -> str:
    """A venue is as trustworthy as its best court: courts share a calendar."""
    for v in (Verdict.RELIABLE, Verdict.PARTIAL, Verdict.UNRELIABLE):
        if v.value in court_verdicts:
            return v.value
    return Verdict.NO_DATA.value
