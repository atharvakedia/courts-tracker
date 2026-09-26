"""The dashboard's numbers, computed from slot rows. Pure: no I/O, no clock.

Every figure is in court-minutes (shown as court-hours), so a 30-minute grid
and a 60-minute grid compare. Occupancy is measured on the court time a venue
offered to customers: a slot is **booked** when a customer bought it on Hudle
and **vacant** when it stayed on sale. A slot the venue **blocked** was never
offered, so it is left out of both sides of the ratio and reported on its own;
a court blocked all day is not a full court.

Only **settled** days count toward occupancy: business dates that have fully
elapsed. The forward book is mostly unbooked at any moment and would read as
near-zero demand.

Reliability is judged per court from the same rows, so a venue that starts
running its bookings through Hudle is upgraded without anyone editing a list.
"""

from __future__ import annotations

import collections
import datetime as dt
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

Row = Mapping[str, Any]
EVENING = range(17, 23)


class Verdict(StrEnum):
    RELIABLE = "reliable"
    PARTIAL = "partial"
    UNRELIABLE = "unreliable"
    NO_DATA = "no_data"


#: Set on a row a person marked as blocked on the dashboard (tracker.blocks).
MARKED_BLOCKED = "marked_blocked"


def is_blocked(r: Row) -> bool:
    """Taken off sale by the venue: not bought and not available to buy, or
    marked blocked by a person, whatever Hudle says about it."""
    return bool(r.get(MARKED_BLOCKED)) or (not r["hudle_booked"] and not r["hudle_available"])


def is_booked(r: Row) -> bool:
    """Bought by a customer on Hudle, in hours nobody marked as a venue block."""
    return bool(r["hudle_booked"]) and not r.get(MARKED_BLOCKED)


@dataclass(frozen=True, slots=True)
class Occupancy:
    """``total_minutes`` is the court time offered to customers (booked +
    vacant); ``blocked_minutes`` is kept beside it, outside the ratio."""

    booked_minutes: int
    total_minutes: int
    blocked_minutes: int

    @property
    def rate(self) -> float | None:
        return self.booked_minutes / self.total_minutes if self.total_minutes else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "occupancy": round(self.rate, 4) if self.rate is not None else None,
            "booked_hours": round(self.booked_minutes / 60, 1),
            "total_hours": round(self.total_minutes / 60, 1),
            "vacant_hours": round((self.total_minutes - self.booked_minutes) / 60, 1),
            "blocked_hours": round(self.blocked_minutes / 60, 1),
        }


def settled(rows: Iterable[Row], today: dt.date) -> list[Row]:
    return [r for r in rows if r["business_date"] < today]


def occupancy(rows: Iterable[Row]) -> Occupancy:
    booked = total = blocked = 0
    for r in rows:
        m = int(r["duration_minutes"])
        if is_blocked(r):
            blocked += m
            continue
        total += m
        if is_booked(r):
            booked += m
    return Occupancy(booked, total, blocked)


def by(rows: Iterable[Row], key: str) -> dict[Any, list[Row]]:
    out: dict[Any, list[Row]] = collections.defaultdict(list)
    for r in rows:
        out[r[key]].append(r)
    return out


def heatmap(rows: Iterable[Row]) -> list[dict[str, Any]]:
    """Occupancy by weekday (of the business date) and hour of the slot's start."""
    cells: dict[tuple[int, int], list[Row]] = collections.defaultdict(list)
    for r in rows:
        cells[(r["business_date"].weekday(), r["start_local"].hour)].append(r)
    return [
        {"weekday": wd, "hour": hr, **occupancy(group).as_dict()}
        for (wd, hr), group in sorted(cells.items())
    ]


def revenue(rows: Iterable[Row]) -> int:
    """Rupees from customer bookings, at Hudle's listed price for each slot.

    A venue block is not a sale, so it earns nothing here. Listed prices are
    what Hudle shows before any offer or discount, so this is an upper bound
    on what customers paid.
    """
    return round(sum(float(r["price"]) for r in rows if is_booked(r) and r["price"] is not None))


def by_date(rows: Iterable[Row]) -> list[dict[str, Any]]:
    """Occupancy and revenue per business date across every row given."""
    return [
        {"business_date": day.isoformat(), **occupancy(group).as_dict(), "revenue": revenue(group)}
        for day, group in sorted(by(rows, "business_date").items())
    ]


def _hour_order(hour: int) -> int:
    """Position of a clock hour in the business day, which starts at 04:00."""
    return (hour - 4) % 24


def by_hour(rows: Iterable[Row]) -> list[dict[str, Any]]:
    """Occupancy per hour of the slot's local start, in business-day order.

    ``days`` is how many business dates the hour was on sale, so a reader can
    turn window totals into court-hours on an average day.
    """
    cells: dict[int, list[Row]] = collections.defaultdict(list)
    for r in rows:
        cells[r["start_local"].hour].append(r)
    return [
        {
            "hour": hr,
            "days": len({r["business_date"] for r in cells[hr]}),
            **occupancy(cells[hr]).as_dict(),
        }
        for hr in sorted(cells, key=_hour_order)
    ]


def by_weekday(rows: Iterable[Row]) -> list[dict[str, Any]]:
    """Occupancy per weekday (Monday = 0) of the business date."""
    cells: dict[int, list[Row]] = collections.defaultdict(list)
    for r in rows:
        cells[r["business_date"].weekday()].append(r)
    return [
        {
            "weekday": wd,
            "days": len({r["business_date"] for r in cells[wd]}),
            **occupancy(cells[wd]).as_dict(),
        }
        for wd in sorted(cells)
    ]


def peak(entries: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """The best-booked entry among those with a real amount of court time.

    An hour one court sells at dawn can read 100% on two slots; entries with
    under a quarter of the largest entry's court-hours are not eligible.
    """
    floor = 0.25 * max((e["total_hours"] for e in entries), default=0)
    eligible = [e for e in entries if e["occupancy"] is not None and e["total_hours"] >= floor]
    return max(eligible, key=lambda e: e["occupancy"], default=None)


def lead_times(rows: Iterable[Row]) -> list[float]:
    """Hours from booking to play, for customer bookings stamped before play.

    Venue blocks are excluded (their stamp is when the venue blocked, not when
    anyone booked), and so are bookings stamped after the slot started --
    offline sales entered later, whose lead time is meaningless.
    """
    out = []
    for r in rows:
        if is_booked(r) and r["booked_at"] and r["booked_at"] < r["start_utc"]:
            out.append((r["start_utc"] - r["booked_at"]).total_seconds() / 3600)
    return out


def lead_summary(hours: Sequence[float]) -> dict[str, Any]:
    if not hours:
        return {"n": 0, "median_hours": None, "p90_hours": None}
    ordered = sorted(hours)
    p90 = ordered[min(len(ordered) - 1, round(0.9 * (len(ordered) - 1)))]
    return {
        "n": len(ordered),
        "median_hours": round(statistics.median(ordered), 1),
        "p90_hours": round(p90, 1),
    }


def price_per_hour(rows: Iterable[Row]) -> float | None:
    rates = [
        float(r["price"]) * 60 / int(r["duration_minutes"])
        for r in rows
        if r["price"] is not None and int(r["duration_minutes"]) > 0
    ]
    return round(statistics.median(rates)) if rates else None


def reliability(court_rows: Sequence[Row], today: dt.date) -> dict[str, Any]:
    """Whether a court's Hudle calendar reflects its real bookings.

    Judged on settled days, plus the published days ahead for permanent holds.
    Evidence, not ground truth: Hudle cannot say what a venue sold by phone
    and never recorded.
    """
    past = settled(court_rows, today)
    ahead = [r for r in court_rows if r["business_date"] >= today]
    occ = occupancy(past)
    days = {r["business_date"] for r in past}
    days_booked = {r["business_date"] for r in past if is_booked(r)}
    taken = [r for r in past if is_booked(r)]
    late = sum(1 for r in taken if r["booked_at"] and r["booked_at"] > r["start_utc"])
    stamp_days: dict[Any, set[dt.date]] = collections.defaultdict(set)
    for r in taken:
        if r["booked_at"]:
            stamp_days[r["booked_at"]].add(r["business_date"])
    bulk = sum(len(d) for d in stamp_days.values() if len(d) >= 2)
    ahead_days = {r["business_date"] for r in ahead}
    held: dict[int, set[dt.date]] = collections.defaultdict(set)
    for r in ahead:
        if is_blocked(r):
            held[r["start_local"].hour].add(r["business_date"])
    permanent = sorted(
        h for h, d in held.items() if len(ahead_days) >= 5 and len(d) >= 0.8 * len(ahead_days)
    )

    listed = occ.total_minutes + occ.blocked_minutes
    evidence = {
        "settled_days": len(days),
        "occupancy": round(occ.rate, 3) if occ.rate is not None else None,
        "blocked_share": round(occ.blocked_minutes / listed, 3) if listed else None,
        "days_with_bookings": round(len(days_booked) / len(days), 2) if days else None,
        "late_entry_share": round(late / len(taken), 2) if taken else None,
        "bulk_share": round(bulk / len(taken), 2) if taken else None,
        "permanent_hold_hours": permanent,
    }
    verdict, reasons = _verdict(evidence)
    return {"verdict": verdict.value, "reasons": reasons, **evidence}


def _verdict(e: Mapping[str, Any]) -> tuple[Verdict, list[str]]:
    """Whether to count a court, and why.

    Left out: a court the venue blocks entirely (nothing was offered, so there
    is no occupancy to measure) and a dead listing, whose 0% says nothing about
    demand. A quiet court is counted: leaving it out would inflate the market.
    """
    if not e["settled_days"]:
        return Verdict.NO_DATA, ["no settled days yet"]
    if e["occupancy"] is None:
        return Verdict.UNRELIABLE, ["every slot blocked by the venue: nothing offered on Hudle"]
    if e["occupancy"] < 0.02:
        return Verdict.UNRELIABLE, [
            "almost nothing booked on Hudle in weeks: a listing, not their booking system"
        ]
    reasons: list[str] = []
    verdict = Verdict.RELIABLE
    if (e["days_with_bookings"] or 0) < 0.3:
        verdict = Verdict.PARTIAL
        reasons.append(
            f"low activity: bookings on {int(100 * (e['days_with_bookings'] or 0))}% of days"
        )
    if (e["blocked_share"] or 0) >= 0.25:
        reasons.append(
            f"{int(100 * e['blocked_share'])}% of court time blocked by the venue, left out"
        )
    if (e["late_entry_share"] or 0) >= 0.2:
        reasons.append(f"{int(100 * e['late_entry_share'])}% entered after the slot started")
    if e["permanent_hold_hours"]:
        reasons.append(f"hours {e['permanent_hold_hours']} held every day ahead")
    return verdict, reasons
