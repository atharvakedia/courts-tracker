"""Pure analytics over the observation stream.

Every module in this package is side-effect free: no database handle, no HTTP,
no :func:`datetime.now`. Functions take sequences of
:class:`~tracker.types.SlotObservation`, :class:`~tracker.types.SnapshotRecord`
and :class:`~tracker.types.FacilityFetch` and return frozen dataclasses, so the
web layer never sees a SQLAlchemy row and every number is reproducible from the
inputs alone.

Each module exports its own names and is imported from its own path::

    from tracker.analytics.occupancy import occupancy_by_venue_day

This file deliberately re-exports nothing. A package-level surface here would
be a second, hand-maintained copy of six modules' signatures that drifts the
moment one of them changes, and it would make import order matter between
modules that are otherwise independent.

The modules, and what each owns:

``coverage``
    Whether the collector actually ran: expected versus observed polls, and
    the gaps nothing may interpolate across.
``occupancy``
    Both denominators -- ``occupancy_strict`` (booked over sellable) and
    ``occupancy_gross`` (booked plus blocked over listed) -- per venue-day,
    hour and weekday/weekend split.
``pricing``
    Price per court-hour, never per slot, plus rank and change timelines.
``leadtime``
    How far ahead a slot sells, measured from the first poll that saw it
    booked, with left-censored slots excluded.
``transitions``
    State changes between consecutive polls: bookings, cancellations and
    inventory pulled from sale.
``market``
    Cross-venue comparisons, all normalized to court-minutes first.
"""
