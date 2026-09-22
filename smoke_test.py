"""One-file smoke test for the Hudle public API.

Confirms the endpoints documented in the project brief still work, captures raw
responses as fixtures, and reports what it found. Writes nothing but fixtures.

Run:  python3 smoke_test.py
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

API_BASE = "https://api.hudle.in"
WEB_BASE = "https://hudle.in"

HEADERS = {
    "Api-Secret": os.environ["HUDLE_API_SECRET"],
    "Accept": "application/json, text/plain, */*",
    "x-app-id": os.environ["HUDLE_APP_ID"],
    "x-device-source": "3",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
}

JAIPUR_CITY_ID = 8
SPORT_PADEL = 44
SPORT_PICKLEBALL = 56

EXPECTED_VENUES = {
    "e606e880-0b2c-4c69-b1a8-193c8f915328": "Padel Up | Sanskar School",
    "9b288765-eee9-4d8a-b309-a4f09b11abcc": "Play Padel | Clarks Amer Hotel",
    "e161ebf7-78c7-4a45-bad8-49841f38b18a": "Padel Fort",
}

KNOWN_PADEL_FACILITY = {
    "e606e880-0b2c-4c69-b1a8-193c8f915328": "e27518b2-9cee-49a9-aa6f-8685e5c543f3",
    "9b288765-eee9-4d8a-b309-a4f09b11abcc": "f40a05d6-e336-43be-bdc6-28405176ed9c",
    "e161ebf7-78c7-4a45-bad8-49841f38b18a": "e03fdd0f-f8d8-4bb4-b707-d64a7244036f",
}

# Names that are add-on SKUs, not courts. Substring match, case-insensitive.
EQUIPMENT_HINTS = ("racket", "racquet", "ball", "rental", "gear")

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "raw"
REQUEST_GAP_SECONDS = 1.5

_last_request_at = 0.0


def _throttle() -> None:
    global _last_request_at
    elapsed = time.monotonic() - _last_request_at
    if elapsed < REQUEST_GAP_SECONDS:
        time.sleep(REQUEST_GAP_SECONDS - elapsed)
    _last_request_at = time.monotonic()


def get(client: httpx.Client, url: str, **kwargs: Any) -> httpx.Response:
    _throttle()
    response = client.get(url, **kwargs)
    return response


def save_fixture(name: str, payload: Any) -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    path = FIXTURE_DIR / f"{name}.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"    saved fixture -> {path.relative_to(Path(__file__).parent)}")


def classify(slot: dict[str, Any]) -> str:
    """The three states from the brief. is_booked wins over is_available."""
    if slot.get("is_booked"):
        return "BOOKED"
    if not slot.get("is_available"):
        return "BLOCKED"
    return "OPEN"


def parse_slug_and_id(share_url: str) -> tuple[str, str] | None:
    match = re.search(r"/venues/([^/]+)/(\d+)", share_url or "")
    if not match:
        return None
    return match.group(1), match.group(2)


def section(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def check_venue_search(client: httpx.Client) -> dict[str, Any]:
    section("1. VENUE DISCOVERY  /api/v1/venue-search")
    found: dict[str, Any] = {}
    for sport_id, sport_name in ((SPORT_PADEL, "padel"), (SPORT_PICKLEBALL, "pickleball")):
        response = get(
            client,
            f"{API_BASE}/api/v1/venue-search",
            params={
                "page": 1,
                "per_page": 50,
                "cityId": JAIPUR_CITY_ID,
                "venueName": "",
                "preferred_sports": sport_id,
            },
        )
        print(f"\n  sport={sport_name} ({sport_id}) -> HTTP {response.status_code}")
        if response.status_code != 200:
            print(f"    FAIL body: {response.text[:400]}")
            continue
        payload = response.json()
        save_fixture(f"venue_search_{sport_name}", payload)
        venues = payload.get("data", {})
        if isinstance(venues, dict):
            venues = venues.get("data") or venues.get("venues") or []
        print(f"    {len(venues)} venue(s):")
        for venue in venues:
            uuid = venue.get("id") or venue.get("uuid")
            name = venue.get("name")
            share_url = venue.get("share_url", "")
            price = venue.get("price_onwards")
            marker = "known" if uuid in EXPECTED_VENUES else "*** NEW ***"
            print(f"      [{marker}] {name}")
            print(f"           uuid={uuid}  price_onwards={price}")
            print(f"           share_url={share_url}")
            if sport_id == SPORT_PADEL:
                found[uuid] = {"name": name, "share_url": share_url}
    return found


def check_venue_detail(client: httpx.Client, venue_uuid: str, label: str) -> None:
    response = get(client, f"{API_BASE}/api/v1/venues/{venue_uuid}")
    print(f"    GET /api/v1/venues/{venue_uuid[:8]}... -> HTTP {response.status_code}")
    if response.status_code != 200:
        print(f"      FAIL body: {response.text[:300]}")
        return
    payload = response.json()
    save_fixture(f"venue_detail_{label}", payload)
    data = payload.get("data", {})
    activities = data.get("activities") or []
    print(f"      name={data.get('name')!r}  activities={len(activities)}")
    for activity in activities:
        facilities = activity.get("facilities")
        print(
            f"        activity id={activity.get('id')} "
            f"name={activity.get('name')!r} "
            f"facilities_key={'present' if facilities else 'ABSENT'}"
        )


def check_next_data(client: httpx.Client, share_url: str, label: str) -> list[dict[str, Any]]:
    parsed = parse_slug_and_id(share_url)
    if not parsed:
        print(f"    cannot parse slug/id from share_url={share_url!r}")
        return []
    slug, numeric_id = parsed
    url = f"{WEB_BASE}/venues/{slug}/{numeric_id}"
    response = get(client, url, headers={"Accept": "text/html,*/*"})
    print(f"    GET {url} -> HTTP {response.status_code}")
    if response.status_code != 200:
        return []
    match = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        response.text,
        re.DOTALL,
    )
    if not match:
        print("      __NEXT_DATA__ NOT FOUND -- SSR shape changed")
        return []
    next_data = json.loads(match.group(1))
    venue_details = next_data.get("props", {}).get("pageProps", {}).get("venueDetails", {})
    save_fixture(f"next_data_venue_details_{label}", venue_details)
    rows: list[dict[str, Any]] = []
    for activity in venue_details.get("activities", []) or []:
        for facility in activity.get("facilities", []) or []:
            name = facility.get("name", "")
            is_equipment = any(hint in name.lower() for hint in EQUIPMENT_HINTS)
            rows.append(
                {
                    "activity_id": activity.get("id"),
                    "activity_name": activity.get("name"),
                    "facility_uuid": facility.get("id"),
                    "facility_name": name,
                    "suggested": "EQUIPMENT" if is_equipment else "COURT",
                }
            )
    print(f"      {len(rows)} facility row(s) across activities:")
    for row in rows:
        print(
            f"        [{row['suggested']:9}] {row['facility_name']!r} "
            f"({row['facility_uuid']})  activity={row['activity_name']!r} "
            f"id={row['activity_id']}"
        )
    return rows


def check_slots(
    client: httpx.Client,
    venue_uuid: str,
    facility_uuid: str,
    label: str,
    days: int,
) -> None:
    start = date.today()
    end = start + timedelta(days=days - 1)
    response = get(
        client,
        f"{API_BASE}/api/v1/web/venues/{venue_uuid}/facilities/{facility_uuid}/slots",
        params={
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "grid": 1,
        },
    )
    print(f"    GET slots {start}..{end} ({days}d) -> HTTP {response.status_code}")
    if response.status_code != 200:
        print(f"      FAIL body: {response.text[:400]}")
        return
    payload = response.json()
    save_fixture(f"slots_{label}_{days}d", payload)

    data = payload.get("data", {})
    timings = data.get("slot_timings") or []
    slot_data = data.get("slot_data") or []

    duration_minutes = None
    if timings:
        first = timings[0]
        t_from = datetime.strptime(first["from"], "%H:%M:%S")
        t_to = datetime.strptime(first["to"], "%H:%M:%S")
        duration_minutes = int((t_to - t_from).total_seconds() // 60)
        print(
            f"      slot_timings: {len(timings)} rows, "
            f"{timings[0]['from']}..{timings[-1]['to']}, "
            f"grid={duration_minutes}min"
        )
    print(f"      slot_data: {len(slot_data)} day(s) returned")
    if slot_data:
        first_date = slot_data[0].get("date")
        last_date = slot_data[-1].get("date")
        print(f"        first date={first_date}  last date={last_date}")

    states: Counter[str] = Counter()
    prices: Counter[str] = Counter()
    empty_days = 0
    per_day_today: list[str] = []
    for day in slot_data:
        if day.get("is_empty"):
            empty_days += 1
        for slot in day.get("slots") or []:
            state = classify(slot)
            states[state] += 1
            prices[str(slot.get("price"))] += 1
            if day.get("date") == start.isoformat():
                per_day_today.append(
                    f"{slot.get('start_time', '')[11:16]} {state} "
                    f"avail={slot.get('available_count')}/{slot.get('total_count')} "
                    f"rs{slot.get('price')}"
                )

    total = sum(states.values())
    booked, blocked, open_ = states["BOOKED"], states["BLOCKED"], states["OPEN"]
    sellable = booked + open_
    print(
        f"      slots total={total}  BOOKED={booked}  BLOCKED={blocked}  OPEN={open_}  "
        f"empty_days={empty_days}"
    )
    if sellable:
        print(f"      occupancy_strict={booked / sellable:.1%}", end="")
    if total:
        print(f"   occupancy_gross={(booked + blocked) / total:.1%}")
    print(f"      distinct prices seen: {dict(prices)}")

    if per_day_today:
        print(f"      --- today ({start}) slot-by-slot ---")
        for line in per_day_today:
            print(f"        {line}")

    # Contradiction check: does is_booked ever disagree with available_count?
    odd = []
    for day in slot_data:
        for slot in day.get("slots") or []:
            if slot.get("is_booked") and slot.get("available_count", 0) >= slot.get(
                "total_count", 0
            ):
                odd.append((day.get("date"), slot.get("start_time"), slot))
    if odd:
        print(
            f"      NOTE: {len(odd)} slot(s) with is_booked=true but "
            f"available_count >= total_count (partial-capacity courts?)"
        )
        counts = ("total_count", "available_count", "is_available", "is_booked")
        for sample in odd[:3]:
            fields = json.dumps({k: sample[2].get(k) for k in counts})
            print(f"        {sample[1]}  {fields}")


def check_slots_meta(client: httpx.Client, venue_uuid: str, facility_uuid: str) -> None:
    start = date.today()
    end = start + timedelta(days=20)
    _throttle()
    with httpx.Client(headers=HEADERS, timeout=30.0, follow_redirects=True) as poster:
        response = poster.post(
            f"{API_BASE}/api/v1/web/venues/{venue_uuid}/facilities/{facility_uuid}/slots/meta",
            params={"start_date": start.isoformat(), "end_date": end.isoformat(), "grid": 1},
        )
    print(f"    POST slots/meta -> HTTP {response.status_code}  body={response.text[:200]}")


def main() -> int:
    print(f"Hudle API smoke test  |  {datetime.now().isoformat(timespec='seconds')}")
    print(f"fixtures -> {FIXTURE_DIR}")

    with httpx.Client(headers=HEADERS, timeout=30.0, follow_redirects=True) as client:
        discovered = check_venue_search(client)

        section("2. VENUE LIST DRIFT CHECK")
        discovered_ids = set(discovered)
        expected_ids = set(EXPECTED_VENUES)
        if discovered_ids == expected_ids:
            print("  OK - exactly the three expected padel venues, no drift.")
        else:
            for uuid in discovered_ids - expected_ids:
                print(f"  NEW VENUE: {discovered[uuid]['name']} ({uuid})")
            for uuid in expected_ids - discovered_ids:
                print(f"  MISSING VENUE: {EXPECTED_VENUES[uuid]} ({uuid})")

        section("3. VENUE DETAIL  /api/v1/venues/{uuid}")
        for uuid, name in EXPECTED_VENUES.items():
            label = name.split("|")[0].strip().lower().replace(" ", "_")
            print(f"\n  {name}")
            check_venue_detail(client, uuid, label)

        section("4. FACILITY DISCOVERY  hudle.in __NEXT_DATA__")
        for uuid, name in EXPECTED_VENUES.items():
            label = name.split("|")[0].strip().lower().replace(" ", "_")
            share_url = discovered.get(uuid, {}).get("share_url", "")
            print(f"\n  {name}")
            check_next_data(client, share_url, label)

        section("5. SLOT GRID  (31-day range, per known padel facility)")
        for uuid, name in EXPECTED_VENUES.items():
            label = name.split("|")[0].strip().lower().replace(" ", "_")
            facility = KNOWN_PADEL_FACILITY[uuid]
            print(f"\n  {name}  facility={facility}")
            check_slots(client, uuid, facility, label, days=31)

        section("6. SLOTS META (optional endpoint)")
        first_uuid = next(iter(EXPECTED_VENUES))
        check_slots_meta(client, first_uuid, KNOWN_PADEL_FACILITY[first_uuid])

    print("\nDone.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
