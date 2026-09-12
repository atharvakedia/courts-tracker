"""Venue-set monitoring and facility-UUID extraction.

Two jobs live here, both read-only with respect to ``config.yaml``:

1. **Venue-set monitoring.** Hudle's ``venue-search`` endpoint answers "which
   venues in Jaipur sell this sport". A new padel venue appearing there is a
   competitor entering a three-venue market, which is itself the signal the
   operator wants. The padel set is small enough to alert on venue by venue;
   pickleball is a 57-venue market, so only its count and its top entrants are
   reported.

2. **Facility-UUID extraction.** Facility UUIDs are the keys the slot grid is
   addressed by, and they are not in the JSON API at all: ``/api/v1/venues/{uuid}``
   returns ``activities[]`` with no ``facilities`` key. They exist only in the
   Next.js SSR payload embedded in the public venue page, so the extractor
   parses ``<script id="__NEXT_DATA__">`` and walks
   ``props.pageProps.venueDetails.activities[].facilities[]``.

Everything in this module except :func:`run_discovery` is a pure function over
already-fetched payloads. :func:`run_discovery` takes an injected client, so no
test here touches the network.

Discovery **never** adopts a change. It compares what Hudle says against the
frozen config, and emits a :class:`DriftReport` carrying a human-reviewable YAML
block for the operator to paste. A silent auto-adopt would let a renamed or
re-keyed facility quietly redirect collection to the wrong court, and the
dataset is forward-looking only, so a wrong day cannot be re-collected.

Failure is loud by design. A missing ``__NEXT_DATA__`` script tag means Hudle
changed its SSR shape; that raises :class:`NextDataNotFoundError` rather than
returning an empty facility list, because an empty list is indistinguishable
from "this venue closed all its courts" and would read as legitimate drift.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlparse

from tracker.config import Config, DiscoveryConfig, FacilityConfig, VenueConfig
from tracker.types import DEFAULT_TZ, DiscoveredFacility, FacilityKind, Sport, VenueDim

logger = logging.getLogger("tracker.discover")

#: How many entrants a non-alerting sport lists in :meth:`DriftReport.render`.
#: Pickleball has 57 venues; printing them all buries the padel signal.
DEFAULT_TOP_ENTRANTS = 5

#: Display names are pipe-separated ("Padel Up | Sanskar School"); the leading
#: segment is the brand and matches the ``short_name`` humans use in the config.
_NAME_SEPARATOR = "|"

_NEXT_DATA_RE = re.compile(
    r"<script\b[^>]*\bid=[\"']__NEXT_DATA__[\"'][^>]*>(.*?)</script>",
    re.DOTALL | re.IGNORECASE,
)


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class DiscoveryError(RuntimeError):
    """Base class for every way discovery can fail loudly."""


class NextDataNotFoundError(DiscoveryError):
    """The venue page carried no ``<script id="__NEXT_DATA__">`` block.

    Hudle changed its server-rendered page shape. Facility UUIDs are available
    nowhere else, so this must surface rather than degrade into an empty set.
    """


class NextDataShapeError(DiscoveryError):
    """``__NEXT_DATA__`` parsed but did not carry ``props.pageProps.venueDetails``."""


class VenueSearchParseError(DiscoveryError):
    """A ``venue-search`` row could not be turned into a :class:`VenueDim`.

    Raised rather than skipped: the whole point of monitoring the venue set is
    to notice unfamiliar rows, so dropping one silently would hide exactly the
    event being watched for.
    """


# --------------------------------------------------------------------------
# Injected client
# --------------------------------------------------------------------------


@runtime_checkable
class VenueSearchClient(Protocol):
    """The one search method discovery needs from the HTTP client.

    ``search_venues_all`` must exhaust Hudle's pagination and return **every
    page payload in order**, each still wrapped in its ``{code, data, meta}``
    envelope. Returning a single flattened venue list would discard
    ``meta.pagination``, and a padel search that silently truncated at
    ``per_page`` is how the 57-venue pickleball market first looked like 50
    venues.
    """

    def search_venues_all(
        self, *, sport_id: int, city_id: int, per_page: int
    ) -> Sequence[Mapping[str, Any]]: ...


@runtime_checkable
class DiscoveryClient(VenueSearchClient, Protocol):
    """Search plus the SSR page fetch that carries facility UUIDs.

    ``fetch_venue_page`` takes a path such as ``/venues/padel-up/772576``
    (:attr:`VenueConfig.ssr_path`) and returns the raw HTML body.
    """

    def fetch_venue_page(self, ssr_path: str) -> str: ...


# --------------------------------------------------------------------------
# venue-search parsing
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SearchPagination:
    """Hudle's ``meta.pagination`` block.

    Note the nesting: the counters are under ``meta["pagination"]``, not on
    ``meta`` directly.
    """

    total: int
    count: int
    per_page: int
    current_page: int
    total_pages: int

    @property
    def is_truncated(self) -> bool:
        """True when this single page does not hold the whole result set."""
        return self.count < self.total

    @property
    def has_more(self) -> bool:
        """True when a further page exists and must be fetched."""
        return self.current_page < self.total_pages


@dataclass(frozen=True, slots=True)
class VenueSearchResult:
    """Every venue one sport's search returned, plus how it was paginated."""

    sport: Sport
    venues: tuple[VenueDim, ...]
    pagination: SearchPagination | None
    pages_fetched: int

    @property
    def reported_total(self) -> int | None:
        """What Hudle said the full result set size is."""
        return None if self.pagination is None else self.pagination.total

    @property
    def complete(self) -> bool:
        """True when we hold as many venues as Hudle claims exist."""
        if self.pagination is None:
            return True
        return len(self.venues) >= self.pagination.total


def parse_search_pagination(payload: Mapping[str, Any]) -> SearchPagination | None:
    """Read ``meta.pagination`` from a ``venue-search`` payload.

    Returns ``None`` when the block is absent, which is the only honest answer:
    a caller cannot then claim the result is complete.
    """
    meta = payload.get("meta")
    if not isinstance(meta, Mapping):
        return None
    pagination = meta.get("pagination")
    if not isinstance(pagination, Mapping):
        return None
    return SearchPagination(
        total=int(pagination.get("total", 0)),
        count=int(pagination.get("count", 0)),
        per_page=int(pagination.get("per_page", 0)),
        current_page=int(pagination.get("current_page", 1)),
        total_pages=int(pagination.get("total_pages", 1)),
    )


def short_name_for(name: str) -> str:
    """The brand half of a pipe-separated Hudle display name.

    ``"Padel Up | Sanskar School"`` -> ``"Padel Up"``;
    ``"Play Padel | Pickleball | Clarks Amer Hotel"`` -> ``"Play Padel"``;
    a name with no separator is returned unchanged.
    """
    return name.split(_NAME_SEPARATOR, 1)[0].strip()


def parse_share_url(share_url: str) -> tuple[str, str]:
    """Split a venue ``share_url`` into ``(slug, numeric_id)``.

    ``https://hudle.in/venues/padel-up/772576`` -> ``("padel-up", "772576")``.

    The slug is read from the URL and never derived from the display name: they
    disagree in the real data. ``"Ṭhe South PickleBall Arena"`` is served at
    ``he-south-pickleball-arena`` (Hudle's slugifier dropped the non-ASCII
    leading character), and ``"Play Padel | Pickleball | Clarks Amer Hotel"``
    at ``pickleball-by-play-padel-clarks-amer-hotel``.
    """
    segments = [s for s in urlparse(share_url).path.split("/") if s]
    if len(segments) != 3 or segments[0] != "venues" or not segments[2].isdigit():
        raise VenueSearchParseError(f"unrecognised venue share_url: {share_url!r}")
    return segments[1], segments[2]


def parse_venue_search(
    payload: Mapping[str, Any],
    *,
    observed_at: dt.datetime,
    tz: str = DEFAULT_TZ,
) -> list[VenueDim]:
    """Turn one ``venue-search`` page into venue dimension rows.

    ``data`` is a flat list of venue objects (not nested under a ``venues``
    key). ``slug`` and ``numeric_id`` are extracted from each row's
    ``share_url`` because they are the only route to the SSR page that holds
    the facility UUIDs.

    This reads a single page. Use :func:`search_venues` to exhaust pagination;
    :func:`parse_search_pagination` reports what this page left behind.
    """
    rows = payload.get("data")
    if not isinstance(rows, Sequence) or isinstance(rows, str | bytes):
        raise VenueSearchParseError("venue-search payload has no list under 'data'")

    venues: list[VenueDim] = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise VenueSearchParseError(f"venue-search data[{index}] is not an object")
        venue_uuid = row.get("id")
        name = row.get("name")
        share_url = row.get("share_url")
        if not venue_uuid or not name or not share_url:
            raise VenueSearchParseError(
                f"venue-search data[{index}] is missing id, name or share_url"
            )
        slug, numeric_id = parse_share_url(str(share_url))
        venues.append(
            VenueDim(
                venue_uuid=str(venue_uuid),
                name=str(name),
                short_name=short_name_for(str(name)),
                slug=slug,
                numeric_id=numeric_id,
                tz=tz,
                active=True,
                first_seen=observed_at,
                last_seen=observed_at,
            )
        )
    return venues


def search_venues(
    client: VenueSearchClient,
    sport: Sport,
    *,
    config: Config,
    observed_at: dt.datetime,
) -> VenueSearchResult:
    """Search one sport's venues in the configured city, across every page.

    The client is responsible for exhausting pagination; this checks its work
    against ``meta.pagination`` and logs when the two disagree, so a truncated
    search is a visible event and not a quietly shrunken market.
    """
    discovery = config.discovery
    pages = client.search_venues_all(
        sport_id=discovery.sport_id(sport),
        city_id=discovery.city_id,
        per_page=discovery.per_page,
    )
    venues: list[VenueDim] = []
    pagination: SearchPagination | None = None
    for page in pages:
        venues.extend(parse_venue_search(page, observed_at=observed_at, tz=config.timezone))
        page_pagination = parse_search_pagination(page)
        if page_pagination is not None:
            pagination = page_pagination

    result = VenueSearchResult(
        sport=sport,
        venues=tuple(venues),
        pagination=pagination,
        pages_fetched=len(pages),
    )
    if not result.complete:
        logger.warning(
            "venue_search_truncated",
            extra={
                "sport": str(sport),
                "venues_seen": len(result.venues),
                "reported_total": result.reported_total,
                "pages_fetched": result.pages_fetched,
            },
        )
    else:
        logger.info(
            "venue_search_complete",
            extra={
                "sport": str(sport),
                "venues_seen": len(result.venues),
                "pages_fetched": result.pages_fetched,
            },
        )
    return result


def search_padel_venues(
    client: VenueSearchClient, *, config: Config, observed_at: dt.datetime
) -> VenueSearchResult:
    """Every padel venue in the configured city. Verified: exactly 3, one page."""
    return search_venues(client, Sport.PADEL, config=config, observed_at=observed_at)


def search_pickleball_venues(
    client: VenueSearchClient, *, config: Config, observed_at: dt.datetime
) -> VenueSearchResult:
    """Every pickleball venue in the configured city.

    Verified: 57 across 2 pages at ``per_page=50``. A single call truncates
    silently, which is why the client must paginate and why
    :attr:`VenueSearchResult.complete` is checked.
    """
    return search_venues(client, Sport.PICKLEBALL, config=config, observed_at=observed_at)


# --------------------------------------------------------------------------
# __NEXT_DATA__ / facility extraction
# --------------------------------------------------------------------------


def extract_next_data(html: str) -> dict[str, Any]:
    """Pull ``props.pageProps.venueDetails`` out of a venue page's SSR payload.

    Raises :class:`NextDataNotFoundError` when the script tag is absent and
    :class:`NextDataShapeError` when it is present but no longer carries
    ``venueDetails``. Both mean Hudle changed the page and facility discovery
    must stop, not return an empty set.
    """
    match = _NEXT_DATA_RE.search(html)
    if match is None:
        raise NextDataNotFoundError(
            'no <script id="__NEXT_DATA__"> block in the venue page; '
            "Hudle's SSR shape changed and facility UUIDs cannot be read"
        )
    try:
        parsed = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise NextDataShapeError(f"__NEXT_DATA__ is not valid JSON: {exc}") from exc
    if not isinstance(parsed, Mapping):
        raise NextDataShapeError("__NEXT_DATA__ is not a JSON object")

    props = parsed.get("props")
    page_props = props.get("pageProps") if isinstance(props, Mapping) else None
    venue_details = page_props.get("venueDetails") if isinstance(page_props, Mapping) else None
    if not isinstance(venue_details, Mapping):
        raise NextDataShapeError(
            "__NEXT_DATA__ has no props.pageProps.venueDetails; Hudle's SSR shape changed"
        )
    return dict(venue_details)


_WORD_RE = re.compile(r"[a-z0-9]+")


def _matches_word(lowered_name: str, term: str) -> bool:
    """Whether ``term`` appears in ``lowered_name`` on word boundaries.

    ``term`` may be several words. Its tokens are matched as a phrase separated
    by any run of non-word characters, so a hint of "padel ball" fires on
    "Padel Ball Rental" and on "padel-ball" alike. Matching token sets instead
    would silently never fire on a multi-word hint, because the set holds the
    whole phrase as one element while the name contributes only single words.
    """
    tokens = _WORD_RE.findall(term.lower())
    if not tokens:
        return False
    pattern = r"\b" + r"\W+".join(re.escape(token) for token in tokens) + r"\b"
    return re.search(pattern, lowered_name) is not None


def is_equipment_facility(name: str, *, discovery: DiscoveryConfig) -> bool:
    """Whether a facility name looks like a rental item rather than court time.

    A **suggestion only**; ``config.yaml`` is authoritative for ``kind``.

    Two rules, in order:

    1. any name matching ``court_name_override`` is a court, whatever else it
       says;
    2. otherwise a name is equipment if an ``equipment_name_hints`` entry
       matches on **word boundaries**.

    The boundary rule is the fix for a real false positive: substring matching
    found "ball" inside "Pickleball Court (Outdoor)" and suggested that a court
    was a rental ball. Either rule alone would still get a real name wrong, so
    both are applied.
    """
    lowered = name.lower()
    if any(_matches_word(lowered, override) for override in discovery.court_name_override):
        return False
    return any(_matches_word(lowered, hint) for hint in discovery.equipment_name_hints)


def suggest_facility_kind(name: str, *, discovery: DiscoveryConfig) -> FacilityKind:
    """Suggest a ``kind`` for a newly discovered facility, for human review."""
    if is_equipment_facility(name, discovery=discovery):
        return FacilityKind.EQUIPMENT
    return FacilityKind.COURT


def parse_facilities(
    venue_details: Mapping[str, Any], *, config: Config
) -> list[DiscoveredFacility]:
    """Walk ``activities[].facilities[]`` into discovery rows.

    ``activity_id`` and ``activity_name`` are carried through because they are
    the only hint of which sport a facility belongs to (Hudle's own
    ``activities`` are named "Padel (Outdoor)" / "Pickleball (Outdoor)"), and
    the operator needs them to fill in ``sport`` when adopting a suggestion.

    ``suggested_kind`` is advisory; ``in_config`` says whether the frozen config
    already knows this UUID.
    """
    venue_uuid = str(venue_details.get("id") or "")
    if not venue_uuid:
        raise NextDataShapeError("venueDetails has no 'id'")

    activities = venue_details.get("activities")
    if not isinstance(activities, Sequence) or isinstance(activities, str | bytes):
        raise NextDataShapeError(f"venueDetails.activities is not a list for venue {venue_uuid}")

    discovered: list[DiscoveredFacility] = []
    for activity in activities:
        if not isinstance(activity, Mapping):
            raise NextDataShapeError(
                f"venueDetails.activities entry is not an object: {activity!r}"
            )
        activity_id = activity.get("id")
        activity_name = activity.get("name")
        facilities = activity.get("facilities") or []
        if not isinstance(facilities, Sequence) or isinstance(facilities, str | bytes):
            raise NextDataShapeError(
                f"activity {activity_id!r} facilities is not a list for venue {venue_uuid}"
            )
        for facility in facilities:
            if not isinstance(facility, Mapping):
                raise NextDataShapeError(f"facility entry is not an object: {facility!r}")
            facility_uuid = str(facility.get("id") or "")
            facility_name = str(facility.get("name") or "")
            if not facility_uuid or not facility_name:
                raise NextDataShapeError(
                    f"facility under activity {activity_id!r} is missing id or name"
                )
            discovered.append(
                DiscoveredFacility(
                    venue_uuid=venue_uuid,
                    facility_uuid=facility_uuid,
                    facility_name=facility_name,
                    activity_id=None if activity_id is None else int(activity_id),
                    activity_name=None if activity_name is None else str(activity_name),
                    in_config=config.facility_by_uuid(facility_uuid) is not None,
                    suggested_kind=suggest_facility_kind(facility_name, discovery=config.discovery),
                )
            )
    return discovered


def discover_facilities(
    client: DiscoveryClient, venue: VenueConfig, *, config: Config
) -> tuple[dict[str, Any], list[DiscoveredFacility]]:
    """Fetch one venue's SSR page and return ``(venueDetails, facilities)``."""
    html = client.fetch_venue_page(venue.ssr_path)
    venue_details = extract_next_data(html)
    facilities = parse_facilities(venue_details, config=config)
    logger.info(
        "facilities_discovered",
        extra={
            "venue_uuid": venue.uuid,
            "ssr_path": venue.ssr_path,
            "facility_count": len(facilities),
            "unknown_count": sum(1 for f in facilities if not f.in_config),
        },
    )
    return venue_details, facilities


# --------------------------------------------------------------------------
# Drift
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VenueRename:
    """A configured venue whose Hudle display name has changed.

    Play Padel already did this once ("Play Padel | Clarks Amer Hotel" ->
    "Play Padel | Pickleball | Clarks Amer Hotel"). Matching is by UUID, so a
    rename is cosmetic for collection but belongs in ``venue_name_history``.
    """

    venue_uuid: str
    old_name: str
    new_name: str
    observed_at: dt.datetime


@dataclass(frozen=True, slots=True)
class FacilityRename:
    """A configured facility whose Hudle name has changed, matched by UUID."""

    venue_uuid: str
    facility_uuid: str
    old_name: str
    new_name: str


@dataclass(frozen=True, slots=True)
class VenueSetDrift:
    """How one sport's venue set compares to the frozen config.

    ``appeared`` carries every discovered venue the config does not know.
    For an alerting sport that is the competitor signal; for pickleball's
    57-venue market it is most of the list, so :meth:`DriftReport.render`
    prints only ``top_entrants``.
    """

    sport: Sport
    appeared: tuple[VenueDim, ...]
    disappeared: tuple[VenueConfig, ...]
    matched: tuple[VenueDim, ...]
    alerting: bool
    discovered_count: int
    reported_total: int | None
    truncated: bool
    top_entrants: tuple[VenueDim, ...]

    @property
    def has_drift(self) -> bool:
        return bool(self.appeared or self.disappeared)

    @property
    def should_alert(self) -> bool:
        """Only an alerting sport's per-venue changes are worth waking someone."""
        return self.alerting and self.has_drift


@dataclass(frozen=True, slots=True)
class VenueFacilityDrift:
    """How one venue's discovered facilities compare to its config block."""

    venue_uuid: str
    venue_name: str
    new_facilities: tuple[DiscoveredFacility, ...]
    missing_facilities: tuple[FacilityConfig, ...]
    renamed_facilities: tuple[FacilityRename, ...]
    renamed_venue: VenueRename | None

    @property
    def has_drift(self) -> bool:
        return bool(
            self.new_facilities
            or self.missing_facilities
            or self.renamed_facilities
            or self.renamed_venue is not None
        )

    def suggestion_yaml(self) -> str:
        """A config block for the operator to review and paste. Never applied."""
        return _facility_suggestion_yaml(self)


@dataclass(frozen=True, slots=True)
class DriftReport:
    """Everything one discovery run found, ready for the CLI and for storage."""

    observed_at: dt.datetime
    venue_sets: tuple[VenueSetDrift, ...] = ()
    facilities: tuple[VenueFacilityDrift, ...] = ()
    venue_renames: tuple[VenueRename, ...] = ()
    discovered_facilities: tuple[DiscoveredFacility, ...] = field(default=(), repr=False)

    @property
    def has_drift(self) -> bool:
        return bool(
            any(s.has_drift for s in self.venue_sets)
            or any(f.has_drift for f in self.facilities)
            or self.venue_renames
        )

    @property
    def should_alert(self) -> bool:
        return bool(
            any(s.should_alert for s in self.venue_sets)
            or any(f.has_drift for f in self.facilities)
            or self.venue_renames
        )

    def suggestion_yaml(self) -> str:
        """The concatenated paste-able suggestion blocks, or an empty string."""
        blocks = [f.suggestion_yaml() for f in self.facilities if f.has_drift]
        return "\n".join(b for b in blocks if b)

    def render(self) -> str:
        """Human-readable summary for ``tracker discover``."""
        return _render_report(self)


def compare_venue_sets(
    configured: Sequence[VenueConfig],
    discovered: Sequence[VenueDim],
    *,
    sport: Sport,
    alert_sport: Sport | None = None,
    pagination: SearchPagination | None = None,
    top_entrants: int = DEFAULT_TOP_ENTRANTS,
) -> VenueSetDrift:
    """Diff a sport's discovered venue set against the configured one, by UUID.

    ``configured`` should already be narrowed to the venues that sell ``sport``
    (see :func:`configured_venues_for_sport`). Names are ignored here: a venue
    that renames itself is still the same venue, and a rename is reported
    separately by :func:`compare_venue_names`.
    """
    configured_by_uuid = {v.uuid: v for v in configured}
    discovered_by_uuid = {v.venue_uuid: v for v in discovered}

    appeared = tuple(v for v in discovered if v.venue_uuid not in configured_by_uuid)
    disappeared = tuple(v for v in configured if v.uuid not in discovered_by_uuid)
    matched = tuple(v for v in discovered if v.venue_uuid in configured_by_uuid)
    alerting = alert_sport is not None and sport is alert_sport

    drift = VenueSetDrift(
        sport=sport,
        appeared=appeared,
        disappeared=disappeared,
        matched=matched,
        alerting=alerting,
        discovered_count=len(discovered),
        reported_total=None if pagination is None else pagination.total,
        truncated=len(discovered) < (pagination.total if pagination else len(discovered)),
        top_entrants=appeared[:top_entrants],
    )
    if drift.has_drift:
        logger.warning(
            "venue_set_drift",
            extra={
                "sport": str(sport),
                "appeared": len(appeared),
                "disappeared": len(disappeared),
                "alerting": alerting,
                "discovered_count": drift.discovered_count,
            },
        )
    return drift


def configured_venues_for_sport(config: Config, sport: Sport) -> list[VenueConfig]:
    """Configured venues that list at least one facility for ``sport``.

    Includes inactive facilities: a venue is still in the market even if we do
    not currently poll its court for that sport.
    """
    return [v for v in config.venues if any(f.sport is sport for f in v.facilities)]


def compare_venue_names(
    configured: Sequence[VenueConfig],
    discovered: Sequence[VenueDim],
    *,
    observed_at: dt.datetime,
) -> list[VenueRename]:
    """Configured venues whose Hudle display name no longer matches, by UUID."""
    discovered_by_uuid = {v.venue_uuid: v for v in discovered}
    renames: list[VenueRename] = []
    for venue in configured:
        found = discovered_by_uuid.get(venue.uuid)
        if found is not None and found.name != venue.name:
            renames.append(
                VenueRename(
                    venue_uuid=venue.uuid,
                    old_name=venue.name,
                    new_name=found.name,
                    observed_at=observed_at,
                )
            )
            logger.warning(
                "venue_renamed",
                extra={
                    "venue_uuid": venue.uuid,
                    "old_name": venue.name,
                    "new_name": found.name,
                },
            )
    return renames


def compare_facilities(
    venue: VenueConfig,
    discovered: Sequence[DiscoveredFacility],
    *,
    discovered_venue_name: str | None = None,
    observed_at: dt.datetime,
) -> VenueFacilityDrift:
    """Diff one venue's discovered facilities against its config block.

    Matching is by UUID, so a facility whose name changed is a **rename**, not
    a new facility plus a missing one. Nothing is adopted: the result carries a
    paste-able suggestion for a human instead.
    """
    configured_by_uuid = {f.uuid: f for f in venue.facilities}
    discovered_by_uuid = {f.facility_uuid: f for f in discovered}

    new_facilities = tuple(f for f in discovered if f.facility_uuid not in configured_by_uuid)
    missing_facilities = tuple(f for f in venue.facilities if f.uuid not in discovered_by_uuid)
    renamed_facilities = tuple(
        FacilityRename(
            venue_uuid=venue.uuid,
            facility_uuid=configured.uuid,
            old_name=configured.name,
            new_name=discovered_by_uuid[configured.uuid].facility_name,
        )
        for configured in venue.facilities
        if configured.uuid in discovered_by_uuid
        and discovered_by_uuid[configured.uuid].facility_name != configured.name
    )
    renamed_venue = (
        VenueRename(
            venue_uuid=venue.uuid,
            old_name=venue.name,
            new_name=discovered_venue_name,
            observed_at=observed_at,
        )
        if discovered_venue_name is not None and discovered_venue_name != venue.name
        else None
    )

    drift = VenueFacilityDrift(
        venue_uuid=venue.uuid,
        venue_name=discovered_venue_name or venue.name,
        new_facilities=new_facilities,
        missing_facilities=missing_facilities,
        renamed_facilities=renamed_facilities,
        renamed_venue=renamed_venue,
    )
    if drift.has_drift:
        logger.warning(
            "facility_drift",
            extra={
                "venue_uuid": venue.uuid,
                "new": len(new_facilities),
                "missing": len(missing_facilities),
                "renamed": len(renamed_facilities),
                "venue_renamed": renamed_venue is not None,
            },
        )
    return drift


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def run_discovery(
    client: DiscoveryClient,
    config: Config,
    *,
    observed_at: dt.datetime,
) -> DriftReport:
    """Run a full discovery pass and report drift. Reads only; writes nothing.

    Searches every configured sport's venue set, then fetches each configured
    venue's SSR page for its facility list. A venue page that fails to parse
    raises: a missing facility list is indistinguishable from a venue that
    closed its courts, and guessing wrong would silently stop collection.
    """
    venue_sets: list[VenueSetDrift] = []
    renames: list[VenueRename] = []
    seen_rename_uuids: set[str] = set()

    for sport in Sport:
        result = search_venues(client, sport, config=config, observed_at=observed_at)
        venue_sets.append(
            compare_venue_sets(
                configured_venues_for_sport(config, sport),
                result.venues,
                sport=sport,
                alert_sport=config.discovery.alert_on_venue_set_change,
                pagination=result.pagination,
            )
        )
        for rename in compare_venue_names(
            configured_venues_for_sport(config, sport), result.venues, observed_at=observed_at
        ):
            if rename.venue_uuid not in seen_rename_uuids:
                seen_rename_uuids.add(rename.venue_uuid)
                renames.append(rename)

    facility_drifts: list[VenueFacilityDrift] = []
    all_discovered: list[DiscoveredFacility] = []
    for venue in config.venues:
        if not venue.active:
            continue
        venue_details, facilities = discover_facilities(client, venue, config=config)
        all_discovered.extend(facilities)
        facility_drifts.append(
            compare_facilities(
                venue,
                facilities,
                discovered_venue_name=(
                    str(venue_details["name"]) if venue_details.get("name") else None
                ),
                observed_at=observed_at,
            )
        )

    report = DriftReport(
        observed_at=observed_at,
        venue_sets=tuple(venue_sets),
        facilities=tuple(facility_drifts),
        venue_renames=tuple(renames),
        discovered_facilities=tuple(all_discovered),
    )
    logger.info(
        "discovery_complete",
        extra={
            "has_drift": report.has_drift,
            "should_alert": report.should_alert,
            "facilities_seen": len(all_discovered),
        },
    )
    return report


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def _yaml_scalar(value: object) -> str:
    """Render a scalar the way ``config.yaml`` writes it."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value)
    return str(value)


def _facility_suggestion_yaml(drift: VenueFacilityDrift) -> str:
    """A reviewable config block for one venue's facility drift.

    Suggestions land with ``active: false`` so that pasting one cannot start
    collecting an unprobed facility on an unknown grid, which would write slot
    rows whose ``duration_minutes`` nobody has verified.
    """
    if not drift.has_drift:
        return ""

    lines: list[str] = [
        f"# --- suggestion for venue {drift.venue_uuid} ({drift.venue_name}) ---",
        "# Review every line. Discovery never edits config.yaml.",
    ]
    if drift.renamed_venue is not None:
        lines.append(
            f"# venue renamed: {drift.renamed_venue.old_name!r} "
            f"-> {drift.renamed_venue.new_name!r} (display only; matching is by uuid)"
        )
    for rename in drift.renamed_facilities:
        lines.append(
            f"# facility {rename.facility_uuid} renamed: {rename.old_name!r} -> {rename.new_name!r}"
        )
    for missing in drift.missing_facilities:
        lines.append(
            f"# MISSING from Hudle: {missing.uuid} ({missing.name}) "
            "-- confirm before removing; collection will now fail for it"
        )
    if drift.new_facilities:
        lines.append("facilities:")
        for facility in drift.new_facilities:
            activity = facility.activity_name or "unknown activity"
            lines.extend(
                [
                    f"  - uuid: {facility.facility_uuid}",
                    f"    name: {_yaml_scalar(facility.facility_name)}",
                    f"    kind: {facility.suggested_kind}"
                    "  # SUGGESTED from the name only -- confirm",
                    f"    sport: # TODO confirm; Hudle activity: {activity}",
                    "    grid_minutes: null  # unprobed",
                    "    price_per_court_hour: null  # unprobed",
                    "    active: false  # leave false until the grid is probed",
                ]
            )
    return "\n".join(lines)


def _render_venue_set(drift: VenueSetDrift) -> list[str]:
    total = "unknown" if drift.reported_total is None else str(drift.reported_total)
    lines = [
        f"{drift.sport} venues: {drift.discovered_count} seen, {total} reported"
        f"{' [TRUNCATED]' if drift.truncated else ''}"
        f"{' [alerting]' if drift.alerting else ' [count only]'}"
    ]
    if not drift.has_drift:
        lines.append("  no venue-set change")
        return lines
    if drift.alerting:
        for venue in drift.appeared:
            lines.append(f"  APPEARED  {venue.venue_uuid}  {venue.name}")
        for configured in drift.disappeared:
            lines.append(f"  DISAPPEARED  {configured.uuid}  {configured.name}")
    else:
        lines.append(f"  {len(drift.appeared)} unconfigured venues; top entrants:")
        lines.extend(f"    - {v.name}" for v in drift.top_entrants)
        for configured in drift.disappeared:
            lines.append(f"  DISAPPEARED  {configured.uuid}  {configured.name}")
    return lines


def _render_report(report: DriftReport) -> str:
    lines: list[str] = [
        f"discovery at {report.observed_at.isoformat()}",
        f"drift: {'YES' if report.has_drift else 'none'}",
        "",
    ]
    for venue_set in report.venue_sets:
        lines.extend(_render_venue_set(venue_set))
        lines.append("")

    for rename in report.venue_renames:
        lines.append(
            f"VENUE RENAMED  {rename.venue_uuid}: {rename.old_name!r} -> {rename.new_name!r}"
        )
    if report.venue_renames:
        lines.append("")

    for facility_drift in report.facilities:
        if not facility_drift.has_drift:
            lines.append(f"{facility_drift.venue_name}: facilities match config")
            continue
        lines.append(f"{facility_drift.venue_name}: FACILITY DRIFT")
        for facility in facility_drift.new_facilities:
            lines.append(
                f"  NEW  {facility.facility_uuid}  {facility.facility_name}"
                f"  (suggested kind: {facility.suggested_kind})"
            )
        for missing in facility_drift.missing_facilities:
            lines.append(f"  MISSING  {missing.uuid}  {missing.name}")
        for facility_rename in facility_drift.renamed_facilities:
            lines.append(
                f"  RENAMED  {facility_rename.facility_uuid}  "
                f"{facility_rename.old_name!r} -> {facility_rename.new_name!r}"
            )
    lines.append("")

    suggestions = report.suggestion_yaml()
    if suggestions:
        lines.extend(
            [
                "Suggested config.yaml changes -- REVIEW, then paste by hand:",
                suggestions,
            ]
        )
    return "\n".join(lines)


def unknown_facilities(discovered: Iterable[DiscoveredFacility]) -> list[DiscoveredFacility]:
    """Discovered facilities the frozen config does not know about."""
    return [f for f in discovered if not f.in_config]
