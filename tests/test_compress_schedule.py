"""Key tests for the /compress-schedule endpoint.

Run from the repository root:  python -m pytest tests/ -v
"""

import datetime as dt

from fastapi.testclient import TestClient

from app.main import app
from app.expander import expand_calendar

client = TestClient(app)

WINDOW_START = "2025-12-01T00:00:00Z"
WINDOW_END = "2026-06-01T00:00:00Z"


def compress(uid: str, instances):
    return client.post(
        "/compress-schedule", json={"uid": uid, "instances": instances}
    )


def iso(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def daily(days, base_day=5, hour=9, length_hours=1):
    """Equal-length UTC instances on January (base_day+d) at ``hour``."""
    base = dt.datetime(2026, 1, base_day, hour, tzinfo=dt.timezone.utc)
    return [
        {
            "start": iso(base + dt.timedelta(days=d)),
            "end": iso(base + dt.timedelta(days=d, hours=length_hours)),
        }
        for d in days
    ]


def roundtrip_matches(ics: str, instances):
    expanded = expand_calendar(
        ics,
        dt.datetime(2025, 12, 1, tzinfo=dt.timezone.utc),
        dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc),
    )
    return (
        [iso(i.start) for i in expanded] == [i["start"] for i in instances]
        and [iso(i.end) for i in expanded] == [i["end"] for i in instances]
    )


# ---------------------------------------------------------------------------
# Strategy selection
# ---------------------------------------------------------------------------


def test_perfect_daily_week_uses_daily_rrule():
    instances = daily(range(5))
    resp = compress("series", instances)
    assert resp.status_code == 200
    body = resp.json()
    assert body["strategy"] == "RRULE:FREQ=DAILY;INTERVAL=1;UNTIL=20260109T090000Z"
    assert body["exception_count"] == 0
    assert roundtrip_matches(body["ics"], instances)


def test_gap_ridden_daily_week_uses_rdate_fallback():
    # Missing Wednesday/Thursday: an EXDATE grid is larger than plain RDATEs.
    instances = daily([0, 1, 4])
    resp = compress("series", instances)
    assert resp.status_code == 200
    body = resp.json()
    assert body["strategy"] == "DTSTART+RDATE"
    assert body["exception_count"] == 0
    assert "RRULE" not in body["ics"]
    assert roundtrip_matches(body["ics"], instances)


def test_long_daily_series_with_one_gap_uses_rrule_and_exdate():
    days = [d for d in range(14) if d != 7]
    instances = daily(days)
    resp = compress("series", instances)
    assert resp.status_code == 200
    body = resp.json()
    assert body["strategy"].startswith("RRULE:FREQ=DAILY;INTERVAL=1;UNTIL=")
    assert body["exception_count"] == 1
    assert "EXDATE:20260112T090000Z" in body["ics"]
    assert roundtrip_matches(body["ics"], instances)


def test_off_grid_instance_becomes_rdate_alongside_rrule():
    instances = daily(range(0, 30, 2))  # 15 instances every 2 days
    # An extra same-duration slot later on the last grid day: off the grid,
    # and since it is the latest start it also becomes UNTIL.
    instances.append(
        {
            "start": "2026-02-02T15:00:00Z",
            "end": "2026-02-02T16:00:00Z",
        }
    )
    resp = compress("series", instances)
    assert resp.status_code == 200
    body = resp.json()
    assert body["strategy"].startswith("RRULE:FREQ=DAILY;INTERVAL=2;UNTIL=")
    assert "UNTIL=20260202T150000Z" in body["strategy"]
    assert "RDATE:20260202T150000Z" in body["ics"]
    assert body["exception_count"] == 0
    assert roundtrip_matches(body["ics"], instances)


def test_weekly_series_uses_weekly_rrule():
    instances = daily([0, 7, 14, 21, 28])  # five Mondays
    resp = compress("series", instances)
    body = resp.json()
    assert body["strategy"].startswith("RRULE:FREQ=WEEKLY;INTERVAL=1;UNTIL=")
    assert body["exception_count"] == 0
    assert roundtrip_matches(body["ics"], instances)


def test_interval_2_daily_grid():
    instances = daily(range(0, 8, 2))
    resp = compress("series", instances)
    body = resp.json()
    assert body["strategy"].startswith("RRULE:FREQ=DAILY;INTERVAL=2;UNTIL=")
    assert roundtrip_matches(body["ics"], instances)


def test_single_and_double_instances_use_fallback():
    for days in ([0], [0, 1]):
        instances = daily(days)
        resp = compress("series", instances)
        assert resp.status_code == 200
        assert resp.json()["strategy"] == "DTSTART+RDATE"
        assert roundtrip_matches(resp.json()["ics"], instances)


def test_irregular_series_falls_back_to_rdate():
    instances = daily([0, 1, 4, 9, 16, 25])
    resp = compress("series", instances)
    body = resp.json()
    assert body["strategy"] == "DTSTART+RDATE"
    assert roundtrip_matches(body["ics"], instances)


def test_winner_is_smallest_complete_ics():
    # Five perfect days: the RRULE ICS must beat an explicit RDATE fallback.
    from app.compressor import _build_ics

    base = dt.datetime(2026, 1, 5, 9, tzinfo=dt.timezone.utc)
    starts = [base + dt.timedelta(days=d) for d in range(5)]
    end = base + dt.timedelta(hours=1)
    fallback = _build_ics("series", starts[0], end, rdates=starts[1:])

    instances = daily(range(5))
    body = compress("series", instances).json()
    assert len(body["ics"].encode("utf-8")) < len(fallback.encode("utf-8"))
    assert "RRULE" in body["ics"]


# ---------------------------------------------------------------------------
# Tie-breaking
# ---------------------------------------------------------------------------


def test_tie_prefers_rrule_then_daily_then_smaller_interval():
    from app.compressor import Candidate

    def cand(strategy, has_rrule, freq, interval, exc, size):
        return Candidate(
            strategy=strategy,
            has_rrule=has_rrule,
            freq=freq,
            interval=interval,
            exception_count=exc,
            ics="x" * size,
        )

    tied = [
        cand("RRULE:FREQ=WEEKLY;INTERVAL=2", True, "WEEKLY", 2, 0, 100),
        cand("RRULE:FREQ=DAILY;INTERVAL=2", True, "DAILY", 2, 0, 100),
        cand("RRULE:FREQ=DAILY;INTERVAL=1", True, "DAILY", 1, 0, 100),
        cand("DTSTART+RDATE", False, "", 0, 0, 100),
    ]
    tied.sort(
        key=lambda c: (
            len(c.ics.encode()),
            c.exception_count,
            0 if c.has_rrule else 1,
            {"DAILY": 0, "WEEKLY": 1, "": 2}[c.freq],
            c.interval,
        )
    )
    assert tied[0].strategy == "RRULE:FREQ=DAILY;INTERVAL=1"


def test_tie_prefers_fewer_exceptions():
    from app.compressor import Candidate

    a = Candidate("a", True, "DAILY", 1, 2, "x" * 50)
    b = Candidate("b", False, "", 0, 0, "x" * 50)
    assert min(
        [a, b],
        key=lambda c: (
            len(c.ics.encode()),
            c.exception_count,
            0 if c.has_rrule else 1,
        ),
    ) is b


# ---------------------------------------------------------------------------
# Round trip through the real /expand endpoint
# ---------------------------------------------------------------------------


def test_compress_then_expand_reproduces_instances():
    instances = daily([d for d in range(21) if d not in (3, 10)])
    body = compress("roundtrip-uid", instances).json()
    resp = client.post(
        "/expand",
        json={"ics": body["ics"], "window_start": WINDOW_START,
              "window_end": WINDOW_END},
    )
    assert resp.status_code == 200
    expanded = resp.json()["instances"]
    assert [(i["start"], i["end"]) for i in expanded] == [
        (i["start"], i["end"]) for i in instances
    ]
    assert all(i["uid"] == "roundtrip-uid" for i in expanded)


def test_ics_is_folded_rfc5545_and_parseable():
    # Many irregular instances force a long RDATE line that must be folded.
    instances = daily([0, 1, 4, 9, 16, 25, 36, 49, 64, 81])
    body = compress("series", instances).json()
    for physical_line in body["ics"].split("\r\n"):
        assert len(physical_line.encode("utf-8")) <= 75
    assert roundtrip_matches(body["ics"], instances)


# ---------------------------------------------------------------------------
# Input handling and 422s
# ---------------------------------------------------------------------------


def test_shuffled_input_is_sorted_and_normalized():
    instances = list(reversed(daily(range(10))))
    body = compress("series", instances).json()
    assert roundtrip_matches(body["ics"], daily(range(10)))


def test_naive_input_treated_as_utc():
    instances = [
        {"start": f"2026-01-{5+d:02d}T09:00:00",
         "end": f"2026-01-{5+d:02d}T10:00:00"}
        for d in range(10)
    ]
    body = compress("series", instances).json()
    assert roundtrip_matches(body["ics"], daily(range(10)))


def test_offset_input_is_normalized_to_utc():
    instances = [
        {"start": f"2026-01-{5+d:02d}T10:00:00+01:00",
         "end": f"2026-01-{5+d:02d}T11:00:00+01:00"}
        for d in range(10)
    ]
    body = compress("series", instances).json()
    assert roundtrip_matches(body["ics"], daily(range(10)))


def test_back_to_back_instances_are_allowed():
    instances = [
        {"start": "2026-01-05T09:00:00Z", "end": "2026-01-05T10:00:00Z"},
        {"start": "2026-01-05T10:00:00Z", "end": "2026-01-05T11:00:00Z"},
    ]
    resp = compress("series", instances)
    assert resp.status_code == 200
    assert roundtrip_matches(resp.json()["ics"], instances)


def test_empty_instances_returns_422():
    resp = compress("series", [])
    assert resp.status_code == 422


def test_missing_uid_returns_422():
    resp = client.post("/compress-schedule", json={"instances": daily([0])})
    assert resp.status_code == 422


def test_blank_uid_returns_422():
    resp = compress("   ", daily([0]))
    assert resp.status_code == 422
    assert "uid" in resp.json()["detail"]


def test_zero_or_negative_duration_returns_422():
    resp = compress("series", [
        {"start": "2026-01-05T09:00:00Z", "end": "2026-01-05T09:00:00Z"}
    ])
    assert resp.status_code == 422
    assert "later than" in resp.json()["detail"]


def test_duplicate_start_returns_422():
    instances = daily([0, 1])
    instances.append(daily([0])[0])
    resp = compress("series", instances)
    assert resp.status_code == 422
    assert "duplicate" in resp.json()["detail"]


def test_unequal_duration_returns_422():
    instances = daily([0]) + [
        {"start": "2026-01-06T09:00:00Z", "end": "2026-01-06T11:00:00Z"}
    ]
    resp = compress("series", instances)
    assert resp.status_code == 422
    assert "same duration" in resp.json()["detail"]


def test_overlapping_instances_return_422():
    resp = compress("series", [
        {"start": "2026-01-05T09:00:00Z", "end": "2026-01-05T12:00:00Z"},
        {"start": "2026-01-06T09:00:00Z", "end": "2026-01-06T12:00:00Z"},
        {"start": "2026-01-06T11:00:00Z", "end": "2026-01-06T14:00:00Z"},
    ])
    assert resp.status_code == 422
    assert "overlap" in resp.json()["detail"]


def test_malformed_timestamps_return_422():
    resp = compress("series", [{"start": "not-a-date", "end": "also-bad"}])
    assert resp.status_code == 422
