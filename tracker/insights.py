"""The dashboard's numbers, computed from slot rows. Pure: no I/O, no clock.

Every figure is in court-minutes (shown as court-hours), so a 30-minute grid
and a 60-minute grid compare. A slot is booked or vacant; booked includes slots
a venue blocked, which is how venues record sales made off Hudle.

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


@dataclass(frozen=True, slots=True)
class Occupancy:
    booked_minutes: int
    total_minutes: int
    hudle_booked_minutes: int
    blocked_minutes: int

    @property
    def rate(self) -> float | None:
        return self.booked_minutes / self.total_minutes if self.total_minutes else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "occupancy": round(self.rate, 4) if self.rate is not None else None,
            "booked_hours": round(self.booked_minutes / 60, 1),
            "total_hours": round(self.total_minutes / 60, 1),
            "hudle_booked_hours": round(self.hudle_booked_minutes / 60, 1),
            "blocked_hours": round(self.blocked_minutes / 60, 1),
        }


def settled(rows: Iterable[Row], today: dt.date) -> list[Row]:
    return [r for r in rows if r["business_date"] < today]


def occupancy(rows: Iterable[Row]) -> Occupancy:
    booked = total = hudle = blocked = 0
    for r in rows:
        m = int(r["duration_minutes"])
        total += m
        if r["booked"]:
            booked += m
            if r["hudle_booked"]:
                hudle += m
            else:
                blocked += m
    return Occupancy(booked, total, hudle, blocked)


def by(rows: Iterable[Row], key: str) -> dict[Any, list[Row]]:
    out: dict[Any, list[Row]] = collections.defaultdict(list)
    for r in rows:
        out[r[key]].append(r)
    return out


def daily_series(rows: Iterable[Row]) -> list[dict[str, Any]]:
    """Occupancy per venue per business date."""
    out = []
    for (venue, day), group in sorted(
        by([{**r, "_k": (r["venue_uuid"], r["business_date"])} for r in rows], "_k").items()
    ):
        occ = occupancy(group)
        out.append({"venue_uuid": venue, "business_date": day.isoformat(), **occ.as_dict()})
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


def lead_times(rows: Iterable[Row]) -> list[float]:
    """Hours from booking to play, for customer bookings stamped before play.

    Venue blocks are excluded (their stamp is when the venue blocked, not when
    anyone booked), and so are bookings stamped after the slot started --
    offline sales entered later, whose lead time is meaningless.
    """
    out = []
    for r in rows:
        if r["hudle_booked"] and r["booked_at"] and r["booked_at"] < r["start_utc"]:
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
    days_booked = {r["business_date"] for r in past if r["booked"]}
    taken = [r for r in past if r["booked"]]
    late = sum(1 for r in taken if r["booked_at"] and r["booked_at"] > r["start_utc"])
    stamp_days: dict[Any, set[dt.date]] = collections.defaultdict(set)
    for r in taken:
        if r["booked_at"]:
            stamp_days[r["booked_at"]].add(r["business_date"])
    bulk = sum(len(d) for d in stamp_days.values() if len(d) >= 2)
    ahead_days = {r["business_date"] for r in ahead}
    held: dict[int, set[dt.date]] = collections.defaultdict(set)
    for r in ahead:
        if r["booked"] and not r["hudle_booked"]:
            held[r["start_local"].hour].add(r["business_date"])
    permanent = sorted(
        h for h, d in held.items() if len(ahead_days) >= 5 and len(d) >= 0.8 * len(ahead_days)
    )

    total = occ.total_minutes
    hb = occ.hudle_booked_minutes / total if total else 0.0
    bl = occ.blocked_minutes / total if total else 0.0
    evidence = {
        "settled_days": len(days),
        "occupancy": round(occ.rate, 3) if occ.rate is not None else None,
        "hudle_booked_share": round(hb, 3),
        "blocked_share": round(bl, 3),
        "days_with_bookings": round(len(days_booked) / len(days), 2) if days else None,
        "late_entry_share": round(late / len(taken), 2) if taken else None,
        "bulk_share": round(bulk / len(taken), 2) if taken else None,
        "permanent_hold_hours": permanent,
    }
    verdict, reasons = _verdict(evidence)
    return {"verdict": verdict.value, "reasons": reasons, **evidence}


def _verdict(e: Mapping[str, Any]) -> tuple[Verdict, list[str]]:
    if not e["settled_days"]:
        return Verdict.NO_DATA, ["no settled days yet"]
    occ = e["occupancy"] or 0.0
    hb, bl = e["hudle_booked_share"], e["blocked_share"]
    if occ < 0.02:
        return Verdict.UNRELIABLE, [
            "almost nothing is ever booked or blocked: a listing, not their booking system"
        ]
    if bl > 0.85:
        return Verdict.UNRELIABLE, [
            "nearly every slot is blocked: closed, or the calendar is not kept"
        ]
    reasons: list[str] = []
    if hb >= 0.03 and bl >= 0.01:
        verdict = Verdict.RELIABLE
        reasons.append("customers book on Hudle and the venue blocks slots there too")
    elif hb >= 0.03:
        verdict = Verdict.PARTIAL
        reasons.append(
            "customers book on Hudle, but the venue never blocks: offline sales may be missing"
        )
    else:
        verdict = Verdict.PARTIAL
        reasons.append(
            "almost all taken slots are venue blocks: "
            "Hudle is their calendar, demand is off-platform"
        )
    if (e["days_with_bookings"] or 0) < 0.3:
        verdict = Verdict.PARTIAL if verdict is Verdict.RELIABLE else verdict
        reasons.append(f"bookings on only {int(100 * (e['days_with_bookings'] or 0))}% of days")
    if (e["late_entry_share"] or 0) >= 0.2:
        reasons.append(f"{int(100 * e['late_entry_share'])}% entered after the slot started")
    if (e["bulk_share"] or 0) >= 0.4:
        reasons.append(f"{int(100 * e['bulk_share'])}% set in bulk admin actions")
    if e["permanent_hold_hours"]:
        reasons.append(
            f"hours {e['permanent_hold_hours']} held every day ahead (counted as booked)"
        )
    return verdict, reasons
