"""Discovery-layer tests, run entirely against the committed real fixtures.

Every payload here is a response Hudle actually returned on 2026-09-11. The
regressions being guarded are the ones that already happened once: a paginated
search silently truncating, a substring name hint calling a court "equipment",
and a venue renaming itself under a stable UUID.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import pytest

from tests.conftest import (
    PADEL_FORT_COURT,
    PADEL_FORT_VENUE,
    PADEL_UP_COURT,
    PADEL_UP_VENUE,
    PLAY_PADEL_COURT,
    PLAY_PADEL_VENUE,
)
from tracker.config import Config, DiscoveryConfig, VenueConfig
from tracker.discover import (
    DiscoveryClient,
    DriftReport,
    NextDataNotFoundError,
    NextDataShapeError,
    VenueSearchParseError,
    compare_facilities,
    compare_venue_names,
    compare_venue_sets,
    configured_venues_for_sport,
    extract_next_data,
    is_equipment_facility,
    parse_facilities,
    parse_search_pagination,
    parse_share_url,
    parse_venue_search,
    run_discovery,
    search_padel_venues,
    search_pickleball_venues,
    short_name_for,
    suggest_facility_kind,
    unknown_facilities,
    venue_location,
)
from tracker.types import FacilityKind, Sport, VenueDim


@pytest.fixture()
def test_config(test_config: Config) -> Config:
    """Padel Up re-activated for this module.

    These tests exercise the snapshot collector's mechanics against the three
    recorded padel grids, one of which is Padel Up's 60-minute court. The live
    config no longer polls Padel Up; the mechanics under test do not change.
    """
    return dataclasses.replace(
        test_config,
        venues=tuple(
            dataclasses.replace(v, active=True) if v.short_name == "Padel Up" else v
            for v in test_config.venues
        ),
    )


OBSERVED_AT = dt.datetime(2026, 9, 11, 10, 0, tzinfo=dt.UTC)

#: Play Padel's display name before it renamed itself. Matching is by UUID, so
#: the rename is cosmetic for collection but must reach venue_name_history.
PLAY_PADEL_OLD_NAME = "Play Padel | Clarks Amer Hotel"

NEXT_DATA_KEY_BY_UUID = {
    PADEL_UP_VENUE: "padel_up",
    PLAY_PADEL_VENUE: "play_padel",
    PADEL_FORT_VENUE: "padel_fort",
}


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def build_ssr_html(venue_details: Mapping[str, Any]) -> str:
    """Wrap an unwrapped ``venueDetails`` fixture back into a real page shape.

    The committed fixtures are the ``venueDetails`` object itself; the page
    Hudle serves nests it under ``props.pageProps``, which is the path the
    extractor has to walk.
    """
    payload = {
        "props": {"pageProps": {"venueDetails": venue_details}},
        "page": "/venues/[slug]/[id]",
        "buildId": "test-build",
    }
    return (
        "<!DOCTYPE html><html><head><title>Hudle</title></head><body>"
        '<div id="__next">rendered markup</div>'
        f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script>'
        "</body></html>"
    )


class FakeHudleClient:
    """An in-memory :class:`DiscoveryClient`. No sockets, no retries, no clock."""

    def __init__(
        self,
        *,
        search_pages_by_sport_id: Mapping[int, Sequence[Mapping[str, Any]]],
        html_by_path: Mapping[str, str],
    ) -> None:
        self._search_pages_by_sport_id = search_pages_by_sport_id
        self._html_by_path = html_by_path
        self.search_calls: list[tuple[int, int, int]] = []
        self.page_calls: list[str] = []

    def search_venues_all(
        self, *, sport_id: int, city_id: int, per_page: int
    ) -> Sequence[Mapping[str, Any]]:
        self.search_calls.append((sport_id, city_id, per_page))
        return self._search_pages_by_sport_id.get(sport_id, [])

    def fetch_venue_page(self, ssr_path: str) -> str:
        self.page_calls.append(ssr_path)
        return self._html_by_path[ssr_path]


@pytest.fixture
def ssr_html_by_path(test_config: Config, raw_next_data: Mapping[str, Any]) -> dict[str, str]:
    return {
        venue.ssr_path: build_ssr_html(raw_next_data[NEXT_DATA_KEY_BY_UUID[venue.uuid]])
        for venue in test_config.venues
    }


@pytest.fixture
def fake_client(
    test_config: Config,
    raw_venue_search_padel: Mapping[str, Any],
    raw_venue_search_pickleball: Mapping[str, Any],
    ssr_html_by_path: Mapping[str, str],
) -> FakeHudleClient:
    discovery = test_config.discovery
    return FakeHudleClient(
        search_pages_by_sport_id={
            discovery.sport_id(Sport.PADEL): [raw_venue_search_padel],
            discovery.sport_id(Sport.PICKLEBALL): [
                raw_venue_search_pickleball,
                synthetic_pickleball_page_two(raw_venue_search_pickleball),
            ],
        },
        html_by_path=ssr_html_by_path,
    )


def synthetic_pickleball_page_two(page_one: Mapping[str, Any]) -> dict[str, Any]:
    """The 7 venues page 1 left behind, so a complete search can be exercised.

    Only page 1 was recorded (that is what proves the truncation); page 2's
    rows are synthesised in the recorded envelope's shape so the paginating
    path has something to exhaust.
    """
    pagination = dict(page_one["meta"]["pagination"])
    remaining = int(pagination["total"]) - int(pagination["count"])
    return {
        "code": page_one.get("code", 200),
        "data": [
            {
                "id": f"00000000-0000-4000-8000-{index:012d}",
                "name": f"Synthetic Pickleball {index}",
                "share_url": f"https://hudle.in/venues/synthetic-pickleball-{index}/90000{index}",
            }
            for index in range(remaining)
        ],
        "meta": {
            "pagination": {
                **pagination,
                "count": remaining,
                "current_page": 2,
            }
        },
    }


def venue_config_named(venue: VenueConfig, name: str) -> VenueConfig:
    return dataclasses.replace(venue, name=name)


# --------------------------------------------------------------------------
# venue-search parsing
# --------------------------------------------------------------------------


def test_parse_venue_search_padel_yields_the_three_known_venues(
    raw_venue_search_padel: Mapping[str, Any],
) -> None:
    """The padel market is exactly three venues, with slugs read from share_url."""
    venues = parse_venue_search(raw_venue_search_padel, observed_at=OBSERVED_AT)

    assert [(v.venue_uuid, v.name, v.slug, v.numeric_id) for v in venues] == [
        (
            PADEL_UP_VENUE,
            "Padel Up | Sanskar School",
            "padel-up",
            "772576",
        ),
        (
            PLAY_PADEL_VENUE,
            "Play Padel | Pickleball | Clarks Amer Hotel",
            "pickleball-by-play-padel-clarks-amer-hotel",
            "417565",
        ),
        (
            PADEL_FORT_VENUE,
            "Padel Fort",
            "padel-fort",
            "155289",
        ),
    ]


def test_parse_venue_search_padel_matches_the_frozen_config_exactly(
    test_config: Config, raw_venue_search_padel: Mapping[str, Any]
) -> None:
    """Every configured slug/numeric_id pair is what the live search returns.

    The SSR path is built from these two fields, so a stale slug in config
    silently breaks facility discovery for that venue.
    """
    discovered = {
        v.venue_uuid: v for v in parse_venue_search(raw_venue_search_padel, observed_at=OBSERVED_AT)
    }

    for venue in test_config.venues:
        found = discovered[venue.uuid]
        assert (found.slug, found.numeric_id) == (venue.slug, venue.numeric_id)
        assert found.short_name == venue.short_name


def test_parse_venue_search_pickleball_sees_its_own_truncation(
    raw_venue_search_pickleball: Mapping[str, Any],
) -> None:
    """One pickleball call returns 50 of 57 rows and must say so.

    A single unpaginated call looks like a complete 50-venue market. The
    pagination block is the only evidence otherwise, so the parser has to
    surface it rather than hand back a bare list.
    """
    venues = parse_venue_search(raw_venue_search_pickleball, observed_at=OBSERVED_AT)
    pagination = parse_search_pagination(raw_venue_search_pickleball)

    assert len(venues) == 50
    assert pagination is not None
    assert (pagination.total, pagination.count, pagination.per_page) == (57, 50, 50)
    assert pagination.is_truncated is True
    assert pagination.has_more is True


def test_parse_search_pagination_reads_the_nested_block(
    raw_venue_search_padel: Mapping[str, Any],
) -> None:
    """Counters live under meta.pagination, not on meta directly."""
    assert "pagination" in raw_venue_search_padel["meta"]
    assert "total" not in raw_venue_search_padel["meta"]

    pagination = parse_search_pagination(raw_venue_search_padel)

    assert pagination is not None
    assert pagination.total == 3
    assert pagination.is_truncated is False
    assert pagination.has_more is False


def test_parse_search_pagination_absent_block_is_none_not_complete() -> None:
    """No pagination block means unknown, never "everything fits"."""
    assert parse_search_pagination({"data": []}) is None
    assert parse_search_pagination({"data": [], "meta": {}}) is None


@pytest.mark.parametrize(
    ("share_url", "expected"),
    [
        ("https://hudle.in/venues/padel-up/772576", ("padel-up", "772576")),
        (
            "https://hudle.in/venues/pickleball-by-play-padel-clarks-amer-hotel/417565",
            ("pickleball-by-play-padel-clarks-amer-hotel", "417565"),
        ),
        (
            "https://hudle.in/venues/he-south-pickleball-arena/395796",
            ("he-south-pickleball-arena", "395796"),
        ),
        ("https://hudle.in/venues/picka-ball/606102/", ("picka-ball", "606102")),
        ("https://hudle.in/venues/smashcity/120108?ref=share", ("smashcity", "120108")),
    ],
)
def test_parse_share_url_handles_the_real_awkward_slugs(
    share_url: str, expected: tuple[str, str]
) -> None:
    """Slugs are read from the URL because they disagree with display names.

    "Ṭhe South PickleBall Arena" is served at ``he-south-pickleball-arena`` --
    Hudle's slugifier dropped the non-ASCII leading character -- and Play Padel
    is served at a slug that does not contain its brand name at all. Any
    attempt to slugify the name would address the wrong page.
    """
    assert parse_share_url(share_url) == expected


def test_awkward_slug_venues_round_trip_from_the_real_pickleball_fixture(
    raw_venue_search_pickleball: Mapping[str, Any],
) -> None:
    """Every one of the 50 recorded rows parses; no row is silently dropped."""
    venues = parse_venue_search(raw_venue_search_pickleball, observed_at=OBSERVED_AT)
    by_name = {v.name: v for v in venues}

    assert by_name["Ṭhe South PickleBall Arena"].slug == "he-south-pickleball-arena"
    assert by_name["RPM | Rally Play More"].slug == "rally-play-more"
    assert by_name["Jaipur Pickleball Club | Narayan Vihar"].slug == (
        "the-pickleball-club-narayan-vihar"
    )
    assert len(by_name) == len(raw_venue_search_pickleball["data"])


def test_parse_venue_search_raises_on_an_unparseable_row() -> None:
    """A row we cannot key is raised, never skipped.

    Skipping would hide exactly the event this endpoint is watched for: an
    unfamiliar venue appearing in the market.
    """
    payload = {"data": [{"id": "x", "name": "Mystery", "share_url": "https://hudle.in/venues/x"}]}

    with pytest.raises(VenueSearchParseError, match="share_url"):
        parse_venue_search(payload, observed_at=OBSERVED_AT)


def test_parse_venue_search_raises_when_data_is_not_a_list() -> None:
    with pytest.raises(VenueSearchParseError, match="'data'"):
        parse_venue_search({"data": {"venues": []}}, observed_at=OBSERVED_AT)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Padel Up | Sanskar School", "Padel Up"),
        ("Play Padel | Pickleball | Clarks Amer Hotel", "Play Padel"),
        ("Padel Fort", "Padel Fort"),
    ],
)
def test_short_name_for_takes_the_brand_half(name: str, expected: str) -> None:
    assert short_name_for(name) == expected


# --------------------------------------------------------------------------
# Paginating search through the injected client
# --------------------------------------------------------------------------


def test_search_padel_venues_returns_three_complete(
    fake_client: FakeHudleClient, test_config: Config
) -> None:
    result = search_padel_venues(fake_client, config=test_config, observed_at=OBSERVED_AT)

    assert result.sport is Sport.PADEL
    assert len(result.venues) == 3
    assert result.reported_total == 3
    assert result.complete is True
    assert fake_client.search_calls == [(44, 8, 50)]


def test_search_pickleball_venues_needs_both_pages_to_be_complete(
    fake_client: FakeHudleClient, test_config: Config
) -> None:
    """57 across 2 pages: exhausting pagination is the whole point."""
    result = search_pickleball_venues(fake_client, config=test_config, observed_at=OBSERVED_AT)

    assert result.pages_fetched == 2
    assert len(result.venues) == 57
    assert result.reported_total == 57
    assert result.complete is True


def test_search_reports_incomplete_when_the_client_fetches_one_page(
    test_config: Config,
    raw_venue_search_pickleball: Mapping[str, Any],
    ssr_html_by_path: Mapping[str, str],
) -> None:
    """A client that forgets to paginate must not look like a 50-venue market."""
    single_page_client = FakeHudleClient(
        search_pages_by_sport_id={56: [raw_venue_search_pickleball]},
        html_by_path=ssr_html_by_path,
    )

    result = search_pickleball_venues(
        single_page_client, config=test_config, observed_at=OBSERVED_AT
    )

    assert len(result.venues) == 50
    assert result.reported_total == 57
    assert result.complete is False


# --------------------------------------------------------------------------
# __NEXT_DATA__ extraction
# --------------------------------------------------------------------------


def test_extract_next_data_returns_venue_details(raw_next_data: Mapping[str, Any]) -> None:
    details = extract_next_data(build_ssr_html(raw_next_data["padel_fort"]))

    assert details["id"] == PADEL_FORT_VENUE
    assert details["name"] == "Padel Fort"
    assert details["slug"] == "padel-fort"


def test_extract_next_data_raises_when_the_script_tag_is_absent() -> None:
    """A missing script tag means the SSR shape changed: fail loudly.

    Returning an empty facility list would be indistinguishable from a venue
    that closed every court, and would be reported as legitimate drift.
    """
    html = "<!DOCTYPE html><html><body><div id='__next'>markup only</div></body></html>"

    with pytest.raises(NextDataNotFoundError, match="__NEXT_DATA__"):
        extract_next_data(html)


def test_extract_next_data_raises_when_venue_details_is_gone() -> None:
    html = (
        '<script id="__NEXT_DATA__" type="application/json">{"props": {"pageProps": {}}}</script>'
    )

    with pytest.raises(NextDataShapeError, match="venueDetails"):
        extract_next_data(html)


def test_extract_next_data_raises_on_invalid_json() -> None:
    html = '<script id="__NEXT_DATA__" type="application/json">{not json}</script>'

    with pytest.raises(NextDataShapeError, match="valid JSON"):
        extract_next_data(html)


def test_extract_next_data_tolerates_reordered_attributes(
    raw_next_data: Mapping[str, Any],
) -> None:
    """Next.js attribute order is not a contract; the id is."""
    payload = json.dumps({"props": {"pageProps": {"venueDetails": raw_next_data["padel_up"]}}})
    html = f"<script type='application/json' id='__NEXT_DATA__' defer>{payload}</script>"

    assert extract_next_data(html)["id"] == PADEL_UP_VENUE


# --------------------------------------------------------------------------
# Facility extraction
# --------------------------------------------------------------------------


def test_parse_facilities_padel_up_has_exactly_one_facility(
    test_config: Config, raw_next_data: Mapping[str, Any]
) -> None:
    details = extract_next_data(build_ssr_html(raw_next_data["padel_up"]))
    facilities = parse_facilities(details, config=test_config)

    assert len(facilities) == 1
    (court,) = facilities
    assert court.facility_uuid == PADEL_UP_COURT
    assert court.facility_name == "Padel Court"
    assert court.suggested_kind is FacilityKind.COURT
    assert court.in_config is True
    assert court.activity_name == "Padel Court"
    assert court.activity_id == 1637
    assert court.venue_uuid == PADEL_UP_VENUE


def test_parse_facilities_play_padel_has_two_courts_and_four_equipment(
    test_config: Config, raw_next_data: Mapping[str, Any]
) -> None:
    """Six facilities across two activities; only two are sellable court time."""
    details = extract_next_data(build_ssr_html(raw_next_data["play_padel"]))
    facilities = parse_facilities(details, config=test_config)

    assert len(facilities) == 6
    by_name = {f.facility_name: f for f in facilities}
    assert set(by_name) == {
        "Padel Court (Outdoor)",
        "Pickleball Court (Outdoor)",
        "Padel Ball",
        "Padel Racquet",
        "Pickleball Ball",
        "Pickleball Racquet",
    }
    courts = [f for f in facilities if f.suggested_kind is FacilityKind.COURT]
    equipment = [f for f in facilities if f.suggested_kind is FacilityKind.EQUIPMENT]
    assert {f.facility_name for f in courts} == {
        "Padel Court (Outdoor)",
        "Pickleball Court (Outdoor)",
    }
    assert len(equipment) == 4
    assert by_name["Padel Court (Outdoor)"].facility_uuid == PLAY_PADEL_COURT
    assert by_name["Padel Court (Outdoor)"].activity_name == "Padel (Outdoor)"
    assert by_name["Pickleball Ball"].activity_name == "Pickleball (Outdoor)"
    assert all(f.in_config for f in facilities)


def test_parse_facilities_padel_fort_has_three_courts_and_one_racket(
    test_config: Config, raw_next_data: Mapping[str, Any]
) -> None:
    """Court 1 / Court 2 are pickleball courts whose names carry no sport."""
    details = extract_next_data(build_ssr_html(raw_next_data["padel_fort"]))
    facilities = parse_facilities(details, config=test_config)

    assert len(facilities) == 4
    by_name = {f.facility_name: f for f in facilities}
    assert set(by_name) == {"Padel Court", "Court 1", "Court 2", "Padel Racket"}
    assert by_name["Padel Court"].facility_uuid == PADEL_FORT_COURT
    assert by_name["Court 1"].suggested_kind is FacilityKind.COURT
    assert by_name["Court 2"].suggested_kind is FacilityKind.COURT
    assert by_name["Court 1"].activity_name == "Pickleball (Outdoor)"
    assert by_name["Padel Racket"].suggested_kind is FacilityKind.EQUIPMENT
    assert all(f.in_config for f in facilities)


def test_parse_facilities_marks_an_unconfigured_facility(
    test_config: Config, raw_next_data: Mapping[str, Any]
) -> None:
    details = dict(raw_next_data["padel_up"])
    details["activities"] = [
        {
            "id": 1637,
            "name": "Padel Court",
            "facilities": [{"id": "11111111-2222-4333-8444-555555555555", "name": "Padel Court 2"}],
        }
    ]

    (facility,) = parse_facilities(details, config=test_config)

    assert facility.in_config is False
    assert unknown_facilities([facility]) == [facility]


def test_parse_facilities_raises_when_activities_is_missing(
    test_config: Config, raw_next_data: Mapping[str, Any]
) -> None:
    details = {k: v for k, v in raw_next_data["padel_up"].items() if k != "activities"}

    with pytest.raises(NextDataShapeError, match="activities"):
        parse_facilities(details, config=test_config)


# --------------------------------------------------------------------------
# THE FALSE-POSITIVE GUARD
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        # The real false positive: "ball" is a substring of "Pickleball".
        ("Pickleball Court (Outdoor)", FacilityKind.COURT),
        ("Pickleball Arena", FacilityKind.COURT),
        ("Padel Court (Outdoor)", FacilityKind.COURT),
        ("Padel Court", FacilityKind.COURT),
        ("Court 1", FacilityKind.COURT),
        ("Court 2", FacilityKind.COURT),
        # Genuine rental items: the hint is a whole word.
        ("Padel Ball", FacilityKind.EQUIPMENT),
        ("Pickleball Ball", FacilityKind.EQUIPMENT),
        ("Pickleball Racquet", FacilityKind.EQUIPMENT),
        ("Padel Racquet", FacilityKind.EQUIPMENT),
        ("Padel Racket", FacilityKind.EQUIPMENT),
        ("Shoe Rental", FacilityKind.EQUIPMENT),
    ],
)
def test_suggest_facility_kind_never_calls_a_court_equipment(
    test_config: Config, name: str, expected: FacilityKind
) -> None:
    """Substring hints mis-suggested "Pickleball Court (Outdoor)" as equipment.

    Two guards now prevent it: hints match whole word tokens only, so "ball"
    does not fire inside "Pickleball"; and any name carrying a court override
    token is a court regardless of hints.
    """
    assert suggest_facility_kind(name, discovery=test_config.discovery) is expected


def test_is_equipment_facility_court_override_beats_an_explicit_hint(
    test_config: Config,
) -> None:
    """A name with both signals is a court: courts are what we collect."""
    assert is_equipment_facility("Padel Racquet", discovery=test_config.discovery) is True
    assert is_equipment_facility("Racquet Court", discovery=test_config.discovery) is False


def _with_vocabulary(
    discovery: DiscoveryConfig, *, hints: tuple[str, ...], overrides: tuple[str, ...]
) -> DiscoveryConfig:
    """The frozen discovery config with its name-matching vocabulary swapped."""
    return dataclasses.replace(discovery, equipment_name_hints=hints, court_name_override=overrides)


def test_multi_word_hints_match_across_the_separating_space(test_config: Config) -> None:
    """Regression: a multi-word hint must match as a phrase, not as a token set.

    Config hints are single words today, but "padel ball" is the obvious thing
    a maintainer adds next. Intersecting a name's word tokens with the hint
    list never fires on it -- the list holds "padel ball" whole while the name
    contributes only "padel" and "ball" -- so the hint reads as configured and
    silently matches nothing.
    """
    discovery = _with_vocabulary(test_config.discovery, hints=("padel ball",), overrides=("court",))
    assert is_equipment_facility("Padel Ball Rental", discovery=discovery) is True
    assert is_equipment_facility("padel-ball", discovery=discovery) is True
    assert is_equipment_facility("Padel Court", discovery=discovery) is False


def test_multi_word_court_overrides_match_across_the_separating_space(
    test_config: Config,
) -> None:
    """Regression: the override is a phrase too, and it fails the same way.

    Dropping a multi-word override lets the equipment hint win, so a real court
    is suggested as equipment. Equipment is marked inactive and never polled,
    which on a forward-only dataset loses that court's slots for good.
    """
    discovery = _with_vocabulary(test_config.discovery, hints=("ball",), overrides=("court side",))
    assert is_equipment_facility("Ball Court Side", discovery=discovery) is False


def test_an_unknown_facility_name_defaults_to_court(test_config: Config) -> None:
    """Regression: an unrecognised name must never default to equipment.

    A court is the safe default precisely because the wrong guess is expensive
    in only one direction: a human confirms the ``kind:`` in config either way,
    but a court guessed as equipment stops being collected in the meantime.
    """
    assert is_equipment_facility("Turf A", discovery=test_config.discovery) is False
    assert is_equipment_facility("Padel", discovery=test_config.discovery) is False
    assert (
        suggest_facility_kind("Something New", discovery=test_config.discovery)
        is FacilityKind.COURT
    )


def test_an_empty_hint_list_matches_nothing(test_config: Config) -> None:
    """An unconfigured vocabulary must suggest nothing rather than everything."""
    discovery = _with_vocabulary(test_config.discovery, hints=(), overrides=("court",))
    assert is_equipment_facility("Anything", discovery=discovery) is False
    assert is_equipment_facility("Padel Racquet", discovery=discovery) is False


def test_every_real_facility_suggestion_agrees_with_the_frozen_config(
    test_config: Config, raw_next_data: Mapping[str, Any]
) -> None:
    """The suggester reproduces all 11 human-reviewed `kind:` values.

    This is the regression that mattered: a wrong suggestion for a court would
    be pasted in as equipment and that court would never be polled.
    """
    mismatches: list[tuple[str, str, str]] = []
    for key in ("padel_up", "play_padel", "padel_fort"):
        details = extract_next_data(build_ssr_html(raw_next_data[key]))
        for facility in parse_facilities(details, config=test_config):
            found = test_config.facility_by_uuid(facility.facility_uuid)
            assert found is not None
            _, configured = found
            if configured.kind is not facility.suggested_kind:
                mismatches.append(
                    (facility.facility_name, str(configured.kind), str(facility.suggested_kind))
                )

    assert mismatches == []


# --------------------------------------------------------------------------
# Venue-set drift
# --------------------------------------------------------------------------


def test_compare_venue_sets_flags_an_appeared_and_a_disappeared_venue(
    test_config: Config, raw_venue_search_padel: Mapping[str, Any]
) -> None:
    """A fourth padel venue in Jaipur is a competitor entering the market."""
    discovered = parse_venue_search(raw_venue_search_padel, observed_at=OBSERVED_AT)
    newcomer = VenueDim(
        venue_uuid="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
        name="Smash Padel | Malviya Nagar",
        short_name="Smash Padel",
        slug="smash-padel-malviya-nagar",
        numeric_id="999999",
        tz=test_config.timezone,
        active=True,
        first_seen=OBSERVED_AT,
        last_seen=OBSERVED_AT,
    )
    configured = configured_venues_for_sport(test_config, Sport.PADEL)
    # Drop Padel Fort from the configured set so it reads as an appearance too.
    without_fort = [v for v in configured if v.uuid != PADEL_FORT_VENUE]
    # And keep a configured venue that Hudle no longer lists.
    closed = dataclasses.replace(
        without_fort[0],
        uuid="dddddddd-eeee-4fff-8000-111111111111",
        name="Closed Padel Club",
    )

    drift = compare_venue_sets(
        [*without_fort, closed],
        [*discovered, newcomer],
        sport=Sport.PADEL,
        alert_sport=test_config.discovery.alert_on_venue_set_change,
    )

    assert {v.venue_uuid for v in drift.appeared} == {
        PADEL_FORT_VENUE,
        newcomer.venue_uuid,
    }
    assert [v.uuid for v in drift.disappeared] == [closed.uuid]
    assert drift.has_drift is True
    assert drift.alerting is True
    assert drift.should_alert is True


def test_compare_venue_sets_reports_no_drift_against_the_frozen_padel_config(
    test_config: Config, raw_venue_search_padel: Mapping[str, Any]
) -> None:
    """Today's live padel market is exactly the configured one."""
    discovered = parse_venue_search(raw_venue_search_padel, observed_at=OBSERVED_AT)

    drift = compare_venue_sets(
        configured_venues_for_sport(test_config, Sport.PADEL),
        discovered,
        sport=Sport.PADEL,
        alert_sport=Sport.PADEL,
        pagination=parse_search_pagination(raw_venue_search_padel),
    )

    assert drift.appeared == ()
    assert drift.disappeared == ()
    assert drift.has_drift is False
    assert drift.should_alert is False
    assert len(drift.matched) == 3
    assert drift.truncated is False


def test_compare_venue_sets_never_alerts_per_venue_on_pickleball(
    test_config: Config, raw_venue_search_pickleball: Mapping[str, Any]
) -> None:
    """A 57-venue market reports a count and top entrants, never 55 alerts."""
    discovered = parse_venue_search(raw_venue_search_pickleball, observed_at=OBSERVED_AT)

    drift = compare_venue_sets(
        configured_venues_for_sport(test_config, Sport.PICKLEBALL),
        discovered,
        sport=Sport.PICKLEBALL,
        alert_sport=test_config.discovery.alert_on_venue_set_change,
        pagination=parse_search_pagination(raw_venue_search_pickleball),
    )

    assert drift.alerting is False
    assert drift.has_drift is True
    assert drift.should_alert is False
    assert drift.discovered_count == 50
    assert drift.reported_total == 57
    assert drift.truncated is True
    assert len(drift.appeared) == 48
    assert len(drift.top_entrants) == 5
    assert drift.top_entrants[0].name == "Maidaan | Jaipur"
    rendered = DriftReport(observed_at=OBSERVED_AT, venue_sets=(drift,)).render()
    assert "Maidaan | Jaipur" in rendered
    assert "The Kitchen Court" not in rendered


def test_configured_venues_for_sport_splits_the_two_markets(test_config: Config) -> None:
    padel = configured_venues_for_sport(test_config, Sport.PADEL)
    pickleball = configured_venues_for_sport(test_config, Sport.PICKLEBALL)

    assert [v.uuid for v in padel] == [
        PADEL_UP_VENUE,
        PLAY_PADEL_VENUE,
        PADEL_FORT_VENUE,
    ]
    assert [v.uuid for v in pickleball] == [PLAY_PADEL_VENUE, PADEL_FORT_VENUE]


# --------------------------------------------------------------------------
# Venue rename
# --------------------------------------------------------------------------


def test_compare_venue_names_detects_the_play_padel_rename(
    test_config: Config, raw_venue_search_padel: Mapping[str, Any]
) -> None:
    """Play Padel already renamed itself once; the UUID never moved.

    Fed the pre-rename config name, discovery must return the change for
    venue_name_history rather than treat it as a new venue plus a dead one.
    """
    discovered = parse_venue_search(raw_venue_search_padel, observed_at=OBSERVED_AT)
    play_padel = test_config.venue_by_uuid(PLAY_PADEL_VENUE)
    assert play_padel is not None
    stale_config = [
        venue_config_named(play_padel, PLAY_PADEL_OLD_NAME)
        if venue.uuid == PLAY_PADEL_VENUE
        else venue
        for venue in configured_venues_for_sport(test_config, Sport.PADEL)
    ]

    renames = compare_venue_names(stale_config, discovered, observed_at=OBSERVED_AT)

    assert len(renames) == 1
    (rename,) = renames
    assert rename.venue_uuid == PLAY_PADEL_VENUE
    assert rename.old_name == PLAY_PADEL_OLD_NAME
    assert rename.new_name == "Play Padel | Pickleball | Clarks Amer Hotel"
    assert rename.observed_at == OBSERVED_AT

    # A rename is not a venue-set change.
    drift = compare_venue_sets(stale_config, discovered, sport=Sport.PADEL, alert_sport=Sport.PADEL)
    assert drift.has_drift is False


def test_compare_venue_names_is_quiet_when_names_match(
    test_config: Config, raw_venue_search_padel: Mapping[str, Any]
) -> None:
    discovered = parse_venue_search(raw_venue_search_padel, observed_at=OBSERVED_AT)

    assert (
        compare_venue_names(
            configured_venues_for_sport(test_config, Sport.PADEL),
            discovered,
            observed_at=OBSERVED_AT,
        )
        == []
    )


# --------------------------------------------------------------------------
# Facility drift
# --------------------------------------------------------------------------


def test_compare_facilities_reports_no_drift_when_hudle_matches_config(
    test_config: Config, raw_next_data: Mapping[str, Any]
) -> None:
    """All three venues' live facility sets equal their config blocks today.

    This is the baseline that makes the drift warning meaningful: if it fired
    on a clean run, nobody would read it.
    """
    for venue in test_config.venues:
        details = extract_next_data(
            build_ssr_html(raw_next_data[NEXT_DATA_KEY_BY_UUID[venue.uuid]])
        )
        facilities = parse_facilities(details, config=test_config)

        drift = compare_facilities(
            venue,
            facilities,
            discovered_venue_name=str(details["name"]),
            observed_at=OBSERVED_AT,
        )

        assert drift.new_facilities == ()
        assert drift.missing_facilities == ()
        assert drift.renamed_facilities == ()
        assert drift.renamed_venue is None
        assert drift.has_drift is False
        assert drift.suggestion_yaml() == ""


def test_compare_facilities_flags_a_renamed_facility(
    test_config: Config, raw_next_data: Mapping[str, Any]
) -> None:
    """A stable UUID under a new name is a rename, not new plus missing.

    Treating it as new-plus-missing would suggest deleting the config entry the
    collector polls, and the dataset cannot be backfilled.
    """
    details = extract_next_data(build_ssr_html(raw_next_data["padel_fort"]))
    facilities = parse_facilities(details, config=test_config)
    renamed = [
        dataclasses.replace(f, facility_name="Padel Court (Covered)")
        if f.facility_uuid == PADEL_FORT_COURT
        else f
        for f in facilities
    ]
    venue = test_config.venue_by_uuid(PADEL_FORT_VENUE)
    assert venue is not None

    drift = compare_facilities(venue, renamed, observed_at=OBSERVED_AT)

    assert drift.new_facilities == ()
    assert drift.missing_facilities == ()
    assert len(drift.renamed_facilities) == 1
    (rename,) = drift.renamed_facilities
    assert rename.facility_uuid == PADEL_FORT_COURT
    assert rename.old_name == "Padel Court"
    assert rename.new_name == "Padel Court (Covered)"
    assert drift.has_drift is True
    assert "renamed" in drift.suggestion_yaml()


def test_compare_facilities_flags_new_and_missing_separately(
    test_config: Config, raw_next_data: Mapping[str, Any]
) -> None:
    details = extract_next_data(build_ssr_html(raw_next_data["padel_fort"]))
    facilities = parse_facilities(details, config=test_config)
    venue = test_config.venue_by_uuid(PADEL_FORT_VENUE)
    assert venue is not None
    without_padel_court = [f for f in facilities if f.facility_uuid != PADEL_FORT_COURT]
    added = dataclasses.replace(
        facilities[0],
        facility_uuid="99999999-8888-4777-8666-555555555555",
        facility_name="Padel Court 2",
    )

    drift = compare_facilities(venue, [*without_padel_court, added], observed_at=OBSERVED_AT)

    assert [f.facility_name for f in drift.new_facilities] == ["Padel Court 2"]
    assert [f.uuid for f in drift.missing_facilities] == [PADEL_FORT_COURT]
    assert drift.renamed_facilities == ()
    assert drift.has_drift is True


def test_compare_facilities_detects_a_venue_name_change(
    test_config: Config, raw_next_data: Mapping[str, Any]
) -> None:
    details = extract_next_data(build_ssr_html(raw_next_data["play_padel"]))
    facilities = parse_facilities(details, config=test_config)
    play_padel = test_config.venue_by_uuid(PLAY_PADEL_VENUE)
    assert play_padel is not None
    stale = venue_config_named(play_padel, PLAY_PADEL_OLD_NAME)

    drift = compare_facilities(
        stale,
        facilities,
        discovered_venue_name=str(details["name"]),
        observed_at=OBSERVED_AT,
    )

    assert drift.renamed_venue is not None
    assert drift.renamed_venue.old_name == PLAY_PADEL_OLD_NAME
    assert drift.renamed_venue.new_name == "Play Padel | Pickleball | Clarks Amer Hotel"
    assert drift.has_drift is True
    assert drift.new_facilities == ()
    assert drift.missing_facilities == ()


def test_suggestion_yaml_is_paste_able_and_never_activates_a_new_facility(
    test_config: Config, raw_next_data: Mapping[str, Any]
) -> None:
    """A suggestion must not switch on collection for an unprobed facility.

    Its grid length is unknown, so polling it would write slot rows whose
    duration nobody verified.
    """
    details = extract_next_data(build_ssr_html(raw_next_data["padel_up"]))
    facilities = parse_facilities(details, config=test_config)
    venue = test_config.venue_by_uuid(PADEL_UP_VENUE)
    assert venue is not None
    added = dataclasses.replace(
        facilities[0],
        facility_uuid="12121212-3434-4565-8787-989898989898",
        facility_name="Padel Court 2",
        in_config=False,
    )

    suggestion = compare_facilities(
        venue, [*facilities, added], observed_at=OBSERVED_AT
    ).suggestion_yaml()

    assert "12121212-3434-4565-8787-989898989898" in suggestion
    assert "active: false" in suggestion
    assert "grid_minutes: null" in suggestion
    assert "price_per_court_hour: null" in suggestion
    assert "kind: court" in suggestion


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def test_run_discovery_on_todays_real_payloads_finds_no_drift(
    fake_client: FakeHudleClient, test_config: Config
) -> None:
    """The frozen config and the live 2026-09-11 payloads agree completely."""
    report = run_discovery(fake_client, test_config, observed_at=OBSERVED_AT)

    padel_drift = next(s for s in report.venue_sets if s.sport is Sport.PADEL)
    assert padel_drift.has_drift is False
    assert report.venue_renames == ()
    assert all(f.has_drift is False for f in report.facilities)
    assert report.should_alert is False
    assert report.suggestion_yaml() == ""
    assert len(report.discovered_facilities) == 11
    assert fake_client.page_calls == [v.ssr_path for v in test_config.venues]


def test_run_discovery_pickleball_market_is_counted_not_alerted(
    fake_client: FakeHudleClient, test_config: Config
) -> None:
    report = run_discovery(fake_client, test_config, observed_at=OBSERVED_AT)

    pickleball = next(s for s in report.venue_sets if s.sport is Sport.PICKLEBALL)
    assert pickleball.discovered_count == 57
    assert pickleball.has_drift is True
    assert pickleball.should_alert is False
    # has_drift is honest about the market; should_alert stays quiet.
    assert report.has_drift is True
    assert report.should_alert is False


def test_run_discovery_surfaces_a_renamed_venue_once(
    test_config: Config,
    raw_venue_search_padel: Mapping[str, Any],
    raw_venue_search_pickleball: Mapping[str, Any],
    ssr_html_by_path: Mapping[str, str],
) -> None:
    """Play Padel sells both sports; its rename is reported once, not twice."""
    play_padel = test_config.venue_by_uuid(PLAY_PADEL_VENUE)
    assert play_padel is not None
    stale_config = dataclasses.replace(
        test_config,
        venues=tuple(
            venue_config_named(v, PLAY_PADEL_OLD_NAME) if v.uuid == PLAY_PADEL_VENUE else v
            for v in test_config.venues
        ),
    )
    client = FakeHudleClient(
        search_pages_by_sport_id={
            44: [raw_venue_search_padel],
            56: [raw_venue_search_pickleball],
        },
        html_by_path=ssr_html_by_path,
    )

    report = run_discovery(client, stale_config, observed_at=OBSERVED_AT)

    assert len(report.venue_renames) == 1
    assert report.venue_renames[0].old_name == PLAY_PADEL_OLD_NAME
    assert report.should_alert is True


def test_run_discovery_raises_when_a_venue_page_loses_its_next_data(
    test_config: Config,
    raw_venue_search_padel: Mapping[str, Any],
    raw_venue_search_pickleball: Mapping[str, Any],
    ssr_html_by_path: Mapping[str, str],
) -> None:
    """Discovery must stop, not report an empty facility set as drift."""
    broken = dict(ssr_html_by_path)
    broken[test_config.venues[0].ssr_path] = "<html><body>no next data</body></html>"
    client = FakeHudleClient(
        search_pages_by_sport_id={
            44: [raw_venue_search_padel],
            56: [raw_venue_search_pickleball],
        },
        html_by_path=broken,
    )

    with pytest.raises(NextDataNotFoundError):
        run_discovery(client, test_config, observed_at=OBSERVED_AT)


def test_drift_report_render_names_the_appeared_padel_venue(
    test_config: Config, raw_venue_search_padel: Mapping[str, Any]
) -> None:
    discovered = parse_venue_search(raw_venue_search_padel, observed_at=OBSERVED_AT)
    drift = compare_venue_sets(
        [
            v
            for v in configured_venues_for_sport(test_config, Sport.PADEL)
            if v.uuid != PADEL_FORT_VENUE
        ],
        discovered,
        sport=Sport.PADEL,
        alert_sport=Sport.PADEL,
    )

    rendered = DriftReport(observed_at=OBSERVED_AT, venue_sets=(drift,)).render()

    assert "drift: YES" in rendered
    assert f"APPEARED  {PADEL_FORT_VENUE}" in rendered
    assert "Padel Fort" in rendered


def test_fake_client_satisfies_the_discovery_protocol(fake_client: FakeHudleClient) -> None:
    """The injected client contract is structural: any shape-match works."""
    assert isinstance(fake_client, DiscoveryClient)


def test_venue_location_reads_the_venue_not_the_city_centre(
    load_fixture: Callable[[str], Any],
) -> None:
    """Regression: taking ``city.latitude`` would stack every venue on one
    point in the middle of Jaipur; a blank pair must read as unknown, not as
    the Gulf of Guinea."""
    details = load_fixture("next_data_venue_details_play_padel")
    assert venue_location(details) == (26.8463907, 75.8009269)
    assert venue_location({"latitude": 0, "longitude": 0}) is None
    assert venue_location({"latitude": None, "longitude": "75.8"}) is None
    assert venue_location({"city": {"latitude": 26.9, "longitude": 75.8}}) is None
