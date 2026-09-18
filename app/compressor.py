"""Compression of concrete UTC instances into compact iCalendar text.

Given a flat list of equal-duration, non-overlapping UTC instances sharing a
``UID``, :func:`compress_schedule` searches a small space of recurrence
descriptions and returns the smallest (fewest UTF-8 bytes) complete
``VCALENDAR`` document that the expansion engine turns back into the exact
same set of instances:

* one ``RRULE`` per candidate with ``FREQ`` DAILY or WEEKLY and ``INTERVAL``
  1..4, ``DTSTART`` at the earliest instance and ``UNTIL`` at the latest;
  grid points that are not instances become ``EXDATE``; instances off the
  grid become ``RDATE``;
* a guaranteed fallback with no ``RRULE`` at all — ``DTSTART`` plus one
  ``RDATE`` for every remaining instance.

Ties are broken by fewer exceptions (``EXDATE`` count), then preferring a
candidate with an ``RRULE``, then DAILY over WEEKLY, then smaller
``INTERVAL``.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from .expander import MAX_OCCURRENCES_PER_UID, UTC, expand_calendar

PRODID = "-//compress-schedule-api//EN"

DAY = dt.timedelta(days=1)
WEEK = dt.timedelta(weeks=1)


class CompressionError(Exception):
    """The supplied instances cannot be compressed; surfaced as HTTP 422."""


@dataclass(frozen=True)
class Candidate:
    """One recurrence description of the instances plus its rendered ICS."""

    strategy: str
    has_rrule: bool
    freq: str  # "DAILY" / "WEEKLY" / "" for the RDATE fallback
    interval: int
    exception_count: int
    ics: str


# ---------------------------------------------------------------------------
# ICS rendering
# ---------------------------------------------------------------------------


def _fold(line: str) -> str:
    """RFC 5545 line folding: first physical line <= 75 UTF-8 octets, each
    continuation line <= 75 octets including the single inserted space (so
    its content is capped at 74)."""
    parts: list[str] = []
    limit = 75
    current = ""
    width = 0
    for char in line:
        size = len(char.encode("utf-8"))
        if width + size > limit:
            parts.append(current)
            current = char
            width = size
            limit = 74  # one continuation octet is used by the folding space
        else:
            current += char
            width += size
    parts.append(current)
    return "\r\n ".join(parts) + "\r\n"


def _escape_text(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("\r\n", r"\n")
        .replace("\r", r"\n")
        .replace("\n", r"\n")
        .replace(";", r"\;")
        .replace(",", r"\,")
    )


def _stamp(value: dt.datetime) -> str:
    return value.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _build_ics(
    uid: str,
    dtstart: dt.datetime,
    dtend: dt.datetime,
    *,
    rrule: str | None = None,
    rdates: list[dt.datetime] | None = None,
    exdates: list[dt.datetime] | None = None,
) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "BEGIN:VEVENT",
        f"UID:{_escape_text(uid)}",
        f"DTSTART:{_stamp(dtstart)}",
        f"DTEND:{_stamp(dtend)}",
    ]
    if rrule is not None:
        lines.append(rrule)
    if rdates:
        lines.append("RDATE:" + ",".join(_stamp(d) for d in rdates))
    if exdates:
        lines.append("EXDATE:" + ",".join(_stamp(d) for d in exdates))
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "".join(_fold(line) for line in lines)


# ---------------------------------------------------------------------------
# Input normalization / validation
# ---------------------------------------------------------------------------


def _normalize_instances(
    instances: list[tuple[dt.datetime, dt.datetime]],
) -> list[tuple[dt.datetime, dt.datetime]]:
    if not instances:
        raise CompressionError("at least one instance is required")
    normalized: list[tuple[dt.datetime, dt.datetime]] = []
    duration: dt.timedelta | None = None
    for index, (raw_start, raw_end) in enumerate(instances):
        start = raw_start.astimezone(UTC)
        end = raw_end.astimezone(UTC)
        if end <= start:
            raise CompressionError(
                f"instance {index}: end {_stamp(end)} must be later than "
                f"start {_stamp(start)}"
            )
        if duration is None:
            duration = end - start
        elif end - start != duration:
            raise CompressionError(
                "all instances must have the same duration "
                f"(instance 0 is {duration}, instance {index} is {end - start})"
            )
        normalized.append((start, end))
    normalized.sort(key=lambda item: item[0])
    seen: set[dt.datetime] = set()
    for index, (start, _) in enumerate(normalized):
        if start in seen:
            raise CompressionError(f"duplicate instance start {_stamp(start)}")
        seen.add(start)
        if index and start < normalized[index - 1][1]:
            raise CompressionError(
                f"instances starting {_stamp(normalized[index - 1][0])} and "
                f"{_stamp(start)} overlap"
            )
    return normalized


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------


def _grid_candidate(
    uid: str,
    first: dt.datetime,
    last: dt.datetime,
    end: dt.datetime,
    starts: list[dt.datetime],
    freq: str,
    interval: int,
) -> Candidate:
    step = (interval * DAY) if freq == "DAILY" else (interval * WEEK)
    # UNTIL is always the latest start (it may itself sit off the grid and be
    # carried by an RDATE); the grid enumerates every step point <= last.
    periods = (last - first) // step
    if periods > MAX_OCCURRENCES_PER_UID:
        raise CompressionError("grid is too large")
    wanted = set(starts)
    grid = [first + k * step for k in range(periods + 1)]
    missing = [point for point in grid if point not in wanted]
    extra = [point for point in starts if (point - first) % step != dt.timedelta(0)]
    rrule = f"RRULE:FREQ={freq};INTERVAL={interval};UNTIL={_stamp(last)}"
    ics = _build_ics(
        uid,
        first,
        end,
        rrule=rrule,
        rdates=extra,
        exdates=missing,
    )
    return Candidate(
        strategy=rrule,
        has_rrule=True,
        freq=freq,
        interval=interval,
        exception_count=len(missing),
        ics=ics,
    )


def _round_trip(
    ics: str,
    starts: list[dt.datetime],
    ends: list[dt.datetime],
) -> bool:
    """The rendered ICS must expand, through the live /expand engine, back
    into exactly the requested instance starts and ends."""
    duration = ends[0] - starts[0]
    window_start = starts[0] - DAY
    window_end = ends[-1] + DAY + duration
    expanded = expand_calendar(ics, window_start, window_end)
    return ([i.start for i in expanded] == starts and
            [i.end for i in expanded] == ends)


# RRULE candidates win over the RDATE fallback only in listed tie-break
# order; freq/interval ordering only ever compares two RRULE candidates.
_FREQ_RANK = {"DAILY": 0, "WEEKLY": 1, "": 2}


def compress_schedule(
    uid: str,
    instances: list[tuple[dt.datetime, dt.datetime]],
) -> Candidate:
    if not isinstance(uid, str) or not uid.strip():
        raise CompressionError("uid must be a non-empty string")
    normalized = _normalize_instances(instances)
    starts = [start for start, _ in normalized]
    ends = [end for _, end in normalized]
    first, last = starts[0], starts[-1]
    end_of_first = ends[0]

    candidates: list[Candidate] = []

    # Guaranteed fallback: DTSTART + one RDATE per remaining instance.
    fallback_ics = _build_ics(uid, first, end_of_first, rdates=starts[1:])
    candidates.append(
        Candidate(
            strategy="DTSTART+RDATE",
            has_rrule=False,
            freq="",
            interval=0,
            exception_count=0,
            ics=fallback_ics,
        )
    )

    for freq in ("DAILY", "WEEKLY"):
        for interval in range(1, 5):
            # The builder turns grid gaps into EXDATE and off-grid starts
            # into RDATE, so every (freq, interval) pair is a legal sketch;
            # only its rendered size decides whether it can win.
            try:
                candidate = _grid_candidate(
                    uid, first, last, end_of_first, starts, freq, interval
                )
            except CompressionError:
                continue
            if _round_trip(candidate.ics, starts, ends):
                candidates.append(candidate)

    if not candidates:  # pragma: no cover - the fallback always round-trips
        raise CompressionError("no compressible representation found")

    candidates.sort(
        key=lambda c: (
            len(c.ics.encode("utf-8")),
            c.exception_count,
            0 if c.has_rrule else 1,
            _FREQ_RANK[c.freq],
            c.interval,
        )
    )
    return candidates[0]
