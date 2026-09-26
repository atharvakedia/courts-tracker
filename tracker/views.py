"""The dashboard's answers, one per view: sport x window x (optionally) venue.

A view is everything the page draws for one choice of sport, window and
venue. The slot data changes once a day, so every view is built once, by the
daily pass, and stored whole; the API then answers a click with one row
instead of recomputing from hundreds of thousands of slots. It never computes
one itself: a build reads three months of slots, and the database's monthly
transfer allowance pays for every byte of them.

A build given a mirror (a local copy of the slots, see
``Store.refresh_mirror``) first brings it up to date and reads the slots from
it, so the database sends only what changed since the last build.

Building a window reads its rows once and judges every court once, then
narrows to each venue from there, so the market view and every venue view of
a window share one pass over the data.

Windows are settled business dates only (the day before ``today`` and back),
in Jaipur time.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import logging
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from tracker.blocks import apply_blocks
from tracker.insights import (
    Row,
    Verdict,
    by,
    by_date,
    by_hour,
    by_weekday,
    heatmap,
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

logger = logging.getLogger("tracker.views")

WINDOWS = {"7": 7, "30": 30, "all": 90}
#: Reliability looks this far ahead for venues holding every slot.
LOOKAHEAD_DAYS = 14
#: A venue first seen this recently is flagged as new on the dashboard.
NEW_VENUE_DAYS = 14
#: From this hour on, a build is for the next day's dashboard: the evening pass
#: has read the day that is about to end, and that is what readers see after
#: midnight.
NEXT_DAY_FROM_HOUR = 22


RULE = (
    "Booked = bought by a customer on Hudle. % booked is of the court time offered "
    "to customers; slots a venue blocks, or that are marked as blocked, are left out."
)


#: Bumped whenever a view's shape or meaning changes. Stored views carry it in
#: their key, so code reading a newer shape (a PR preview on the production
#: database, or a deploy before the next build) never draws views built for
#: another shape; its own are built by running the views workflow on its branch.
VIEWS_VERSION = 4


def view_key(sport: Sport, window: str, venue: str | None) -> str:
    return f"v{VIEWS_VERSION}|{sport.value}|{window}|{venue or ''}"


def views_as_of(now: dt.datetime, tz: str) -> dt.date:
    """The Jaipur date the views built now will be read on."""
    local = local_wall_clock(now, tz)
    return local.date() + dt.timedelta(days=1 if local.hour >= NEXT_DAY_FROM_HOUR else 0)


def rows_for(
    store: Store, sport: Sport, today: dt.date, *, mirror: Store | None = None
) -> Sequence[Row]:
    """Every row any window of this sport needs: the longest window and the
    look-ahead reliability reads, with the hours people marked as blocked.
    The slots come from ``mirror`` when given, the blocks always from ``store``."""
    longest = max(WINDOWS.values())
    rows = (mirror or store).slots_between(
        sport=sport,
        date_from=today - dt.timedelta(days=longest),
        date_to=today + dt.timedelta(days=LOOKAHEAD_DAYS),
    )
    return apply_blocks(rows, store.blocks())


@dataclass(frozen=True, slots=True)
class _Window:
    """One window of one sport, judged once; views are narrowings of it."""

    sport: Sport
    key: str
    today: dt.date
    start: dt.date
    end: dt.date
    counted: list[Row]
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
    last = today + dt.timedelta(days=LOOKAHEAD_DAYS)
    current = [r for r in rows if start <= r["business_date"] <= last]

    per_court = by(current, "facility_uuid")
    verdicts = {
        fid: _judge(court_rows, courts.get(fid), today) for fid, court_rows in per_court.items()
    }
    past = settled(current, today)
    # Every court except a dead listing or a failing one is counted; low-activity
    # courts included, since their quiet days are real demand and leaving them
    # out inflates it.
    counted_verdicts = {Verdict.RELIABLE.value, Verdict.PARTIAL.value}
    counted = [r for r in past if verdicts[r["facility_uuid"]]["verdict"] in counted_verdicts]

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
        per_venue_court = by(vrows, "facility_uuid")
        court_ids = sorted(per_venue_court)
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
                        "booked_hours": occupancy(per_venue_court[f]).as_dict()["booked_hours"],
                        **verdicts[f],
                    }
                    for f in court_ids
                ],
                "verdict": _venue_verdict([verdicts[f]["verdict"] for f in court_ids]),
                "failing": any(verdicts[f]["failing"] for f in court_ids),
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
        counted,
        per_court,
        venue_rows,
    )


def _payload(w: _Window, venue: str | None) -> dict[str, Any]:
    counted = w.counted
    if venue is not None:
        counted = [r for r in counted if r["venue_uuid"] == venue]
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
        "rule": RULE,
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
        "peaks": {"hour": peak(hourly), "weekday": peak(weekdays)},
        "venues": w.venue_rows,
        "days": by_date(counted),
        "hours": hourly,
        "weekdays": weekdays,
        "heatmap": heatmap(counted),
        "lead_time": lead_summary(hours),
    }


def all_views(
    store: Store, today: dt.date, *, mirror: Store | None = None
) -> Iterator[tuple[str, dict[str, Any]]]:
    """Every view the page can ask for, keyed by :func:`view_key`: each sport
    and window for the whole market and for each of its venues."""
    venues = store.venues()
    last_pass = store.last_pass_started_at()
    for sport in Sport:
        rows = rows_for(store, sport, today, mirror=mirror)
        courts = {c.facility_uuid: _unread(c, last_pass) for c in store.tracked_courts(sport)}
        for window in WINDOWS:
            w = _window(rows, courts, venues, sport=sport, window=window, today=today)
            yield view_key(sport, window, None), _payload(w, None)
            for v in w.venue_rows:
                yield view_key(sport, window, v["venue_uuid"]), _payload(w, v["venue_uuid"])


def publish_views(store: Store, *, now: dt.datetime, tz: str, mirror: Store | None = None) -> int:
    """Build every view and replace the stored set, reading the slots from
    ``mirror`` (refreshed first) when given. Returns how many were stored."""
    as_of = views_as_of(now, tz)
    if mirror is not None:
        first = as_of - dt.timedelta(days=max(WINDOWS.values()))
        read = mirror.refresh_mirror(store, from_date=first)
        logger.info("mirror_refreshed", extra={"rows_read": read})
    built = [
        (key, json.dumps(payload, default=str))
        for key, payload in all_views(store, as_of, mirror=mirror)
    ]
    store.replace_views(built, version=VIEWS_VERSION, as_of=as_of, built_at=now)
    return len(built)


def _unread(court: Court, last_pass: dt.datetime | None) -> Court:
    """The court, failing too if the latest daily pass stopped before reading
    it: its latest days were read only while they were still ahead."""
    if court.read_error or not court.last_read_at or not last_pass:
        return court
    if court.last_read_at >= last_pass:
        return court
    return dataclasses.replace(court, read_error="not read in the latest daily pass")


def _judge(court_rows: Sequence[Row], court: Court | None, today: dt.date) -> dict[str, Any]:
    """A court's reliability verdict, overruled for a court the last pass could
    not read: its recent days were never read, so it is left out of every
    figure (as unreliable) and flagged ``failing`` until a read succeeds."""
    verdict = reliability(court_rows, today)
    if court is None or court.read_error is None:
        return {**verdict, "failing": False}
    reason = "the latest daily pass did not read it from Hudle; left out until one does"
    return {
        **verdict,
        "verdict": Verdict.UNRELIABLE.value,
        "reasons": [reason, *verdict["reasons"]],
        "failing": True,
    }


def _venue_verdict(court_verdicts: list[str]) -> str:
    """A venue is as trustworthy as its best court: courts share a calendar."""
    for v in (Verdict.RELIABLE, Verdict.PARTIAL, Verdict.UNRELIABLE):
        if v.value in court_verdicts:
            return v.value
    return Verdict.NO_DATA.value
