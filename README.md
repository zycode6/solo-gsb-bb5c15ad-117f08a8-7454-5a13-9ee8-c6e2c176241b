# iCalendar Recurrence Expansion & Availability API

A local HTTP API that expands iCalendar (RFC 5545) recurring events into
concrete UTC instances inside a query window, and finds the **common free
time** across several named calendars when intersected with per-weekday
local working hours.

- Expands `DTSTART` + `RRULE` + `RDATE` − `EXDATE`
- Merges overrides by `UID` + `RECURRENCE-ID`: an override can **reschedule**
  an instance (judged against the window by its *actual* new start/end; the
  original slot is never kept) or **cancel** it via `STATUS:CANCELLED`
- Interprets `TZID` as IANA timezones with correct DST handling
- `/agenda` renders the same expanded instances as a **local agenda** for an
  IANA display timezone: timed events crossing local midnight are split into
  per-date fragments carrying `continues_before`/`continues_after`, while
  `DATE` all-day events are returned day by day on their authored calendar
  dates (never shifted by UTC offsets); entries are ordered by local date,
  all-day-first, then start and `UID`
- `/availability` builds working hours per **local date** (DST-correct),
  clips all busy instances to them, merges half-open intervals (adjacent
  included), and returns the complement shared by every calendar
- `/conflicts` scans named calendars together and reports the UTC segments
  where at least `minimum_concurrency` instances overlap, with the
  participating instances for each segment
- `/calendar-diff` compares two ICS versions over one UTC window by
  `UID + RECURRENCE-ID` and returns identity-sorted `added`/`removed`/
  `modified` instances (with `before`/`after`)
- `/alarms` expands `VALARM` reminders for every instance in a window:
  a master event's alarms apply to each recurring instance, overrides
  carry their own, and `REPEAT`/`DURATION` fan alarms out
- `/attendance` reports each windowed instance's `ATTENDEE` responses:
  recurring instances inherit the master's attendees, a `RECURRENCE-ID`
  override that declares any `ATTENDEE` replaces the whole list (one
  without keeps inheriting), with `PARTSTAT` counts and stable address
  ordering
- `/validate` structurally checks raw iCalendar text and always replies
  `200` — even unparsable input — with a sorted list of stable-coded
  diagnostics (missing `UID`/`DTSTART`, `DTEND` + `DURATION`, inverted
  times, duplicate master events, orphan overrides)
- Results sorted by UTC start time, then `UID` (stable)
- Clear `422` errors for invalid rules, unknown timezones, or bad input

Stack: Python · FastAPI · icalendar · python-dateutil · zoneinfo

## Quick start (Docker Compose)

```bash
docker compose up --build
```

The API listens on **http://localhost:8000** (interactive docs at `/docs`,
health check at `/health`).

## API

### `POST /expand`

Request body:

| Field          | Type   | Description                                        |
| -------------- | ------ | -------------------------------------------------- |
| `ics`          | string | iCalendar text (one or more `VEVENT`s)             |
| `window_start` | string | ISO 8601 timestamp, converted to UTC (naive = UTC) |
| `window_end`   | string | ISO 8601 timestamp, converted to UTC (naive = UTC) |

An instance is returned when it **overlaps** `[window_start, window_end)`
(a zero-duration event must start inside the window). Each instance carries
`uid`, `recurrence_id` (UTC, `null` for non-recurring events), `summary`,
`start`, `end`, and `status`; all timestamps are UTC (`Z`).

### Example

```bash
curl -s http://localhost:8000/expand \
  -H 'Content-Type: application/json' \
  -d '{
    "ics": "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//demo//EN\r\nBEGIN:VEVENT\r\nUID:weekly-1\r\nDTSTART;TZID=America/New_York:20260301T090000\r\nDTEND;TZID=America/New_York:20260301T100000\r\nRRULE:FREQ=WEEKLY;COUNT=3\r\nEXDATE;TZID=America/New_York:20260308T090000\r\nSUMMARY:Weekly sync\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\nUID:weekly-1\r\nRECURRENCE-ID;TZID=America/New_York:20260315T090000\r\nDTSTART;TZID=America/New_York:20260316T110000\r\nDTEND;TZID=America/New_York:20260316T120000\r\nSUMMARY:Weekly sync (moved)\r\nEND:VEVENT\r\nEND:VCALENDAR",
    "window_start": "2026-02-25T00:00:00Z",
    "window_end": "2026-04-01T00:00:00Z"
  }'
```

Response — note the DST shift (14:00Z → 13:00Z), the `EXDATE`-removed
March 8 occurrence, and the March 15 instance replaced by its override at
the new time (the original slot is gone):

```json
{
  "window_start": "2026-02-25T00:00:00Z",
  "window_end": "2026-04-01T00:00:00Z",
  "count": 2,
  "instances": [
    {
      "uid": "weekly-1",
      "recurrence_id": "2026-03-01T14:00:00Z",
      "summary": "Weekly sync",
      "start": "2026-03-01T14:00:00Z",
      "end": "2026-03-01T15:00:00Z",
      "status": "CONFIRMED"
    },
    {
      "uid": "weekly-1",
      "recurrence_id": "2026-03-15T13:00:00Z",
      "summary": "Weekly sync (moved)",
      "start": "2026-03-16T15:00:00Z",
      "end": "2026-03-16T16:00:00Z",
      "status": "CONFIRMED"
    }
  ]
}
```

### `POST /agenda`

Renders the expanded instances as an agenda ordered by **local date**, using
an IANA display timezone. It reuses the exact recurrence engine behind
`/expand` (same overrides, exclusions, window overlap and `422`s) and then
projects each instance onto local days. Request body:

| Field          | Type   | Description                                                    |
| -------------- | ------ | -------------------------------------------------------------- |
| `ics`          | string | iCalendar text (one or more `VEVENT`s)                         |
| `window_start` | string | Start of the UTC query window (ISO 8601, naive = UTC)          |
| `window_end`   | string | End of the UTC query window (ISO 8601, naive = UTC)            |
| `timezone`     | string | IANA timezone for local dates and timestamps, e.g. `Asia/Tokyo` |

Agenda semantics:

- A **timed event** is converted to the display timezone. When it spans more
  than one local date it is **split at every local midnight** into one
  fragment per date; each fragment carries `uid`, `summary`, the local
  `date`, local `start`/`end` (with the zone offset), and the booleans
  `continues_before` (the instance began on an earlier date) and
  `continues_after` (it continues onto a later date). Midnight boundaries are
  built day by day with `zoneinfo`, so fragments stay wall-clock correct
  across DST spring-forward/fall-back days.
- A **`DATE` all-day event** (including multi-day `DTSTART`/`DTEND` ranges and
  recurring `DATE` series) is returned once per **authored calendar date**
  with `all_day: true`. DATE values carry no timezone, so the dates are the
  exact dates written in the ICS for *every* display timezone — they are
  **never converted through a UTC midnight** (a 00:00 UTC DATE rendered in
  Tokyo would otherwise jump to the previous day). Each day's local
  `start`/`end` are that date's local midnights; `continues_before`/
  `continues_after` mark interior days of a multi-day range.
- Fragment `start`/`end` are the true instance bounds clipped to the local
  day only — they are **not** clipped to the UTC query window, so an event
  overlapping the window from before still shows its real local start.
- Entries are sorted by local date ascending; within a date **all-day
  entries come first**, then timed fragments by local `start`, then `uid`.
  `STATUS:CANCELLED` instances (master or override) are omitted.
- An unknown display `timezone` returns `422`, as do all the calendar and
  window failures described for `/expand`.

### Example

```bash
curl -s http://localhost:8000/agenda \
  -H 'Content-Type: application/json' \
  -d '{
    "ics": "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//demo//EN\r\nBEGIN:VEVENT\r\nUID:night-1\r\nDTSTART:20260105T140000Z\r\nDTEND:20260106T020000Z\r\nSUMMARY:Night shift\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\nUID:trip\r\nDTSTART;VALUE=DATE:20260106\r\nDTEND;VALUE=DATE:20260108\r\nSUMMARY:Conference\r\nEND:VEVENT\r\nEND:VCALENDAR",
    "window_start": "2026-01-01T00:00:00Z",
    "window_end": "2026-02-01T00:00:00Z",
    "timezone": "Asia/Tokyo"
  }'
```

The timed event runs 23:00–11:00 JST across the Jan 5/6 midnight and is
split into two fragments with continuation flags; the two-day all-day
conference stays on its authored Jan 6–7 dates even though Tokyo is UTC+9,
and sorts ahead of the timed fragment on Jan 6:

```json
{
  "window_start": "2026-01-01T00:00:00Z",
  "window_end": "2026-02-01T00:00:00Z",
  "timezone": "Asia/Tokyo",
  "count": 4,
  "entries": [
    {
      "date": "2026-01-05",
      "uid": "night-1",
      "summary": "Night shift",
      "start": "2026-01-05T23:00:00+09:00",
      "end": "2026-01-06T00:00:00+09:00",
      "all_day": false,
      "continues_before": false,
      "continues_after": true
    },
    {
      "date": "2026-01-06",
      "uid": "trip",
      "summary": "Conference",
      "start": "2026-01-06T00:00:00+09:00",
      "end": "2026-01-07T00:00:00+09:00",
      "all_day": true,
      "continues_before": false,
      "continues_after": true
    },
    {
      "date": "2026-01-06",
      "uid": "night-1",
      "summary": "Night shift",
      "start": "2026-01-06T00:00:00+09:00",
      "end": "2026-01-06T11:00:00+09:00",
      "all_day": false,
      "continues_before": true,
      "continues_after": false
    },
    {
      "date": "2026-01-07",
      "uid": "trip",
      "summary": "Conference",
      "start": "2026-01-07T00:00:00+09:00",
      "end": "2026-01-08T00:00:00+09:00",
      "all_day": true,
      "continues_before": true,
      "continues_after": false
    }
  ]
}
```

### `POST /availability`

Finds free slots that lie inside the working hours **and** avoid busy
instances in *every* supplied calendar. Request body:

| Field             | Type                | Description                                                       |
| ----------------- | ------------------- | ----------------------------------------------------------------- |
| `calendars`       | object              | Map of name → iCalendar text; may be empty or omitted             |
| `window_start`    | string              | ISO 8601 timestamp, converted to UTC (naive = UTC)                |
| `window_end`      | string              | ISO 8601 timestamp, converted to UTC (naive = UTC)                |
| `timezone`        | string              | IANA timezone in which `working_hours` are interpreted            |
| `working_hours`   | object              | Keys `monday`…`sunday`; each a list of `{start, end}` local times |
| `minimum_minutes` | integer             | Only free slots at least this long are returned (must be > 0)     |

Working periods are generated for each local date in the window and then
converted to UTC, so a fixed local time shifts correctly across DST
transitions. A missing weekday (or an empty list) means no working time
that day; multiple periods per day are supported (e.g. a lunch break).
Periods must not be inverted (`end <= start`) or cross midnight.

Busy time is every expanded instance that is not `STATUS:CANCELLED` and
has a positive duration; instances are clipped to the working periods and
merged as half-open `[start, end)` intervals, with touching intervals
merged (back-to-back meetings leave no free instant between them). The
complement inside the working periods is returned, sorted by `start`,
keeping only slots of at least `minimum_minutes` (inclusive at the
boundary). Each slot carries UTC `start`, `end`, and integer `minutes`.

### Example

```bash
curl -s http://localhost:8000/availability \
  -H 'Content-Type: application/json' \
  -d '{
    "calendars": {
      "alice": "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//demo//EN\r\nBEGIN:VEVENT\r\nUID:a1\r\nDTSTART;TZID=Europe/Berlin:20260309T100000\r\nDTEND;TZID=Europe/Berlin:20260309T110000\r\nSUMMARY:Dentist\r\nEND:VEVENT\r\nEND:VCALENDAR",
      "bob": "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//demo//EN\r\nBEGIN:VEVENT\r\nUID:b1\r\nDTSTART:20260309T140000Z\r\nDTEND:20260309T153000Z\r\nSUMMARY:Interview\r\nEND:VEVENT\r\nEND:VCALENDAR"
    },
    "window_start": "2026-03-09T00:00:00Z",
    "window_end": "2026-03-10T00:00:00Z",
    "timezone": "Europe/Berlin",
    "working_hours": {
      "monday": [
        {"start": "09:00", "end": "12:00"},
        {"start": "13:00", "end": "17:00"}
      ]
    },
    "minimum_minutes": 30
  }'
```

Berlin is UTC+1 on this date, so the working periods are `08:00–11:00Z`
and `12:00–16:00Z`; Alice blocks `09:00–10:00Z` and Bob `14:00–15:30Z`.
The lunch break is not free time, and the final 30-minute tail is kept
because the minimum length is inclusive:

```json
{
  "window_start": "2026-03-09T00:00:00Z",
  "window_end": "2026-03-10T00:00:00Z",
  "timezone": "Europe/Berlin",
  "count": 4,
  "slots": [
    {"start": "2026-03-09T08:00:00Z", "end": "2026-03-09T09:00:00Z", "minutes": 60},
    {"start": "2026-03-09T10:00:00Z", "end": "2026-03-09T11:00:00Z", "minutes": 60},
    {"start": "2026-03-09T12:00:00Z", "end": "2026-03-09T14:00:00Z", "minutes": 120},
    {"start": "2026-03-09T15:30:00Z", "end": "2026-03-09T16:00:00Z", "minutes": 30}
  ]
}
```

### `POST /conflicts`

Finds the UTC segments where at least `minimum_concurrency` instances from
the named calendars overlap. Each calendar is expanded with the same
recurrence engine used by `/expand`; only instances that are **not**
`STATUS:CANCELLED` and have a **positive duration** take part, and every
participating interval is clipped to the half-open query window
`[window_start, window_end)`. Request body:

| Field                 | Type   | Description                                                    |
| --------------------- | ------ | -------------------------------------------------------------- |
| `calendars`           | object | Map of name → iCalendar text                                   |
| `window_start`        | string | ISO 8601 timestamp, converted to UTC (naive = UTC)             |
| `window_end`          | string | ISO 8601 timestamp, converted to UTC (naive = UTC)             |
| `minimum_concurrency` | int    | Report segments with at least this many active instances (≥ 2) |

A sweep line visits every interval start and end instant in chronological
order. Intervals are half-open `[start, end)`, so at one and the same
instant **ends are processed before starts**: a meeting ending at 11:00 and
one starting at 11:00 are never concurrent. Each non-empty segment whose
active count reaches the threshold is returned with its UTC `start`/`end`,
its `count`, and the participating instances. Participants are sorted by
`calendar`, then `UID`, then `RECURRENCE-ID` (a `null` `RECURRENCE-ID`
sorts first). Two adjacent segments are merged only when their participant
sets are exactly identical; the segments are sorted by `start`. Each
participant is serialized like an `/expand` instance plus the `calendar`
name; its `start`/`end` are the instance times clipped to the window.

### Example

```bash
curl -s http://localhost:8000/conflicts \
  -H 'Content-Type: application/json' \
  -d '{
    "calendars": {
      "alice": "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//demo//EN\r\nBEGIN:VEVENT\r\nUID:a1\r\nDTSTART:20260309T090000Z\r\nDTEND:20260309T110000Z\r\nSUMMARY:Deep work\r\nEND:VEVENT\r\nEND:VCALENDAR",
      "bob": "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//demo//EN\r\nBEGIN:VEVENT\r\nUID:b1\r\nDTSTART:20260309T100000Z\r\nDTEND:20260309T120000Z\r\nSUMMARY:Interview\r\nEND:VEVENT\r\nEND:VCALENDAR",
      "carol": "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//demo//EN\r\nBEGIN:VEVENT\r\nUID:c1\r\nDTSTART:20260309T103000Z\r\nDTEND:20260309T113000Z\r\nSUMMARY:Review\r\nEND:VEVENT\r\nEND:VCALENDAR"
    },
    "window_start": "2026-03-09T00:00:00Z",
    "window_end": "2026-03-10T00:00:00Z",
    "minimum_concurrency": 2
  }'
```

Alice is busy 09:00–11:00, Bob 10:00–12:00 and Carol 10:30–11:30. The
three-way overlap 10:30–11:00 is bracketed by two two-instance segments;
all three are reported and stay separate because the participants differ:

```json
{
  "window_start": "2026-03-09T00:00:00Z",
  "window_end": "2026-03-10T00:00:00Z",
  "minimum_concurrency": 2,
  "count": 3,
  "segments": [
    {
      "start": "2026-03-09T10:00:00Z",
      "end": "2026-03-09T10:30:00Z",
      "count": 2,
      "participants": [
        {
          "calendar": "alice",
          "uid": "a1",
          "recurrence_id": null,
          "summary": "Deep work",
          "start": "2026-03-09T09:00:00Z",
          "end": "2026-03-09T11:00:00Z",
          "status": "CONFIRMED"
        },
        {
          "calendar": "bob",
          "uid": "b1",
          "recurrence_id": null,
          "summary": "Interview",
          "start": "2026-03-09T10:00:00Z",
          "end": "2026-03-09T12:00:00Z",
          "status": "CONFIRMED"
        }
      ]
    },
    {
      "start": "2026-03-09T10:30:00Z",
      "end": "2026-03-09T11:00:00Z",
      "count": 3,
      "participants": [
        {
          "calendar": "alice",
          "uid": "a1",
          "recurrence_id": null,
          "summary": "Deep work",
          "start": "2026-03-09T09:00:00Z",
          "end": "2026-03-09T11:00:00Z",
          "status": "CONFIRMED"
        },
        {
          "calendar": "bob",
          "uid": "b1",
          "recurrence_id": null,
          "summary": "Interview",
          "start": "2026-03-09T10:00:00Z",
          "end": "2026-03-09T12:00:00Z",
          "status": "CONFIRMED"
        },
        {
          "calendar": "carol",
          "uid": "c1",
          "recurrence_id": null,
          "summary": "Review",
          "start": "2026-03-09T10:30:00Z",
          "end": "2026-03-09T11:30:00Z",
          "status": "CONFIRMED"
        }
      ]
    },
    {
      "start": "2026-03-09T11:00:00Z",
      "end": "2026-03-09T11:30:00Z",
      "count": 2,
      "participants": [
        {
          "calendar": "bob",
          "uid": "b1",
          "recurrence_id": null,
          "summary": "Interview",
          "start": "2026-03-09T10:00:00Z",
          "end": "2026-03-09T12:00:00Z",
          "status": "CONFIRMED"
        },
        {
          "calendar": "carol",
          "uid": "c1",
          "recurrence_id": null,
          "summary": "Review",
          "start": "2026-03-09T10:30:00Z",
          "end": "2026-03-09T11:30:00Z",
          "status": "CONFIRMED"
        }
      ]
    }
  ]
}
```

### `POST /calendar-diff`

Compares an **old** and a **new** version of a calendar over one shared UTC
window. Both ICS texts are expanded with the same recurrence engine used by
`/expand`; instance identity is fixed to `UID + RECURRENCE-ID` for recurring
instances and to `UID` alone for non-recurring events (`recurrence_id` is
`null`). Only instances that overlap the common window take part in the
comparison.

| Field          | Type   | Description                                         |
| -------------- | ------ | --------------------------------------------------- |
| `old_ics`      | string | Old version of the iCalendar text                   |
| `new_ics`      | string | New version of the iCalendar text                   |
| `window_start` | string | Shared ISO 8601 window start, converted to UTC      |
| `window_end`   | string | Shared ISO 8601 window end, converted to UTC        |

The response lists:

- `added` — identities present only in the new calendar,
- `removed` — identities present only in the old calendar,
- `modified` — identities present on both sides whose `start`, `end`,
  `summary` or `status` changed, each with a `before` and an `after` object
  in the same shape as an `/expand` instance.

Completely identical instances are not returned. All three lists are sorted
by identity (`UID`, then `RECURRENCE-ID`; a `null` `RECURRENCE-ID` sorts
first). Cancelling an occurrence with a `STATUS:CANCELLED` override makes it
disappear from the expansion, so it is reported as `removed`; likewise an
instance rescheduled across the window boundary appears as
`removed`/`added`, while one that still overlaps the window on both sides is
`modified` (its identity never changes).

### Example

```bash
curl -s http://localhost:8000/calendar-diff \
  -H 'Content-Type: application/json' \
  -d '{
    "old_ics": "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//demo//EN\r\nBEGIN:VEVENT\r\nUID:standup\r\nDTSTART:20260302T090000Z\r\nDTEND:20260302T091500Z\r\nRRULE:FREQ=WEEKLY;COUNT=4\r\nSUMMARY:Standup\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\nUID:oneoff\r\nDTSTART:20260304T140000Z\r\nDTEND:20260304T150000Z\r\nSUMMARY:Old review\r\nEND:VEVENT\r\nEND:VCALENDAR",
    "new_ics": "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//demo//EN\r\nBEGIN:VEVENT\r\nUID:standup\r\nDTSTART:20260302T090000Z\r\nDTEND:20260302T091500Z\r\nRRULE:FREQ=WEEKLY;COUNT=4\r\nSUMMARY:Standup\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\nUID:standup\r\nRECURRENCE-ID:20260309T090000Z\r\nDTSTART:20260309T100000Z\r\nDTEND:20260309T101500Z\r\nSUMMARY:Standup (15 min later)\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\nUID:workshop\r\nDTSTART:20260320T130000Z\r\nDTEND:20260320T150000Z\r\nSUMMARY:Workshop\r\nEND:VEVENT\r\nEND:VCALENDAR",
    "window_start": "2026-03-01T00:00:00Z",
    "window_end": "2026-04-01T00:00:00Z"
  }'
```

The new calendar adds `workshop`, drops `oneoff`, and moves the March 9
standup instance (its `RECURRENCE-ID` stays `2026-03-09T09:00:00Z`); the
unchanged March 2/16/23 standups are not reported:

```json
{
  "window_start": "2026-03-01T00:00:00Z",
  "window_end": "2026-04-01T00:00:00Z",
  "added": [
    {
      "uid": "workshop",
      "recurrence_id": null,
      "summary": "Workshop",
      "start": "2026-03-20T13:00:00Z",
      "end": "2026-03-20T15:00:00Z",
      "status": "CONFIRMED"
    }
  ],
  "removed": [
    {
      "uid": "oneoff",
      "recurrence_id": null,
      "summary": "Old review",
      "start": "2026-03-04T14:00:00Z",
      "end": "2026-03-04T15:00:00Z",
      "status": "CONFIRMED"
    }
  ],
  "modified": [
    {
      "before": {
        "uid": "standup",
        "recurrence_id": "2026-03-09T09:00:00Z",
        "summary": "Standup",
        "start": "2026-03-09T09:00:00Z",
        "end": "2026-03-09T09:15:00Z",
        "status": "CONFIRMED"
      },
      "after": {
        "uid": "standup",
        "recurrence_id": "2026-03-09T09:00:00Z",
        "summary": "Standup (15 min later)",
        "start": "2026-03-09T10:00:00Z",
        "end": "2026-03-09T10:15:00Z",
        "status": "CONFIRMED"
      }
    }
  ]
}
```

### `POST /alarms`

Expands the `VALARM` reminders of every `VEVENT` instance that overlaps the
query window, returning only reminders whose **trigger instant** falls
inside `[window_start, window_end)`. Request body is the same shape as
`/expand`:

| Field          | Type   | Description                                        |
| -------------- | ------ | -------------------------------------------------- |
| `ics`          | string | iCalendar text (one or more `VEVENT`s)             |
| `window_start` | string | ISO 8601 timestamp, converted to UTC (naive = UTC) |
| `window_end`   | string | ISO 8601 timestamp, converted to UTC (naive = UTC) |

Alarm semantics:

- Only `ACTION:DISPLAY` alarms are supported (`AUDIO`, `EMAIL`, … → `422`);
  a `DESCRIPTION` is required.
- `TRIGGER` may be **absolute** (`VALUE=DATE-TIME`, optionally with a
  `TZID`; a floating value is assumed UTC) or a **duration** relative to
  the instance start (`TRIGGER:-PT15M`) or end
  (`TRIGGER;RELATED=END:PT10M`). Both positive and negative durations are
  accepted; `RELATED=START` is the default.
- `REPEAT` plus `DURATION` (the pair must appear together) fans one alarm
  out into `REPEAT + 1` reminders, spaced by `DURATION` (e.g.
  `REPEAT:2`/`DURATION:PT5M` fires at the initial trigger and twice more,
  five minutes apart). `REPEAT` must be non-negative and `DURATION`
  positive; a single alarm may not fan out beyond 1000 reminders.
- A **master** event's alarms apply to *every* expanded instance of the
  series; a `RECURRENCE-ID` override carries **its own** alarms and the
  master's alarms do not apply to it (an override without `VALARM`s alarms
  nobody). A cancelled occurrence produces no reminders.
- The window is half-open: a trigger exactly at `window_start` is included,
  one exactly at `window_end` is excluded.

Each reminder carries `uid`, `recurrence_id` (UTC, `null` for
non-recurring events), `summary` (of the instance it belongs to), `trigger`
(UTC, `Z`), and the alarm `description`. Reminders are stably sorted by
trigger time, then `UID`, then `RECURRENCE-ID` (a `null` `RECURRENCE-ID`
sorts first).

### Example

```bash
curl -s http://localhost:8000/alarms \
  -H 'Content-Type: application/json' \
  -d '{
    "ics": "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//demo//EN\r\nBEGIN:VEVENT\r\nUID:weekly-1\r\nDTSTART:20260105T090000Z\r\nDTEND:20260105T100000Z\r\nRRULE:FREQ=WEEKLY;COUNT=2\r\nSUMMARY:Weekly sync\r\nBEGIN:VALARM\r\nTRIGGER:-PT15M\r\nACTION:DISPLAY\r\nDESCRIPTION:Join the call\r\nREPEAT:1\r\nDURATION:PT10M\r\nEND:VALARM\r\nEND:VEVENT\r\nEND:VCALENDAR",
    "window_start": "2026-01-01T00:00:00Z",
    "window_end": "2026-02-01T00:00:00Z"
  }'
```

The one master alarm applies to both Mondays and repeats once ten minutes
after the initial trigger:

```json
{
  "window_start": "2026-01-01T00:00:00Z",
  "window_end": "2026-02-01T00:00:00Z",
  "count": 4,
  "alarms": [
    {
      "uid": "weekly-1",
      "recurrence_id": "2026-01-05T09:00:00Z",
      "summary": "Weekly sync",
      "trigger": "2026-01-05T08:45:00Z",
      "description": "Join the call"
    },
    {
      "uid": "weekly-1",
      "recurrence_id": "2026-01-05T09:00:00Z",
      "summary": "Weekly sync",
      "trigger": "2026-01-05T08:55:00Z",
      "description": "Join the call"
    },
    {
      "uid": "weekly-1",
      "recurrence_id": "2026-01-12T09:00:00Z",
      "summary": "Weekly sync",
      "trigger": "2026-01-12T08:45:00Z",
      "description": "Join the call"
    },
    {
      "uid": "weekly-1",
      "recurrence_id": "2026-01-12T09:00:00Z",
      "summary": "Weekly sync",
      "trigger": "2026-01-12T08:55:00Z",
      "description": "Join the call"
    }
  ]
}
```

### `POST /attendance`

Reports, for every `VEVENT` instance overlapping the query window, the
attendee list in effect for that instance together with a `PARTSTAT`
summary. Request body is the same shape as `/expand`:

| Field          | Type   | Description                                        |
| -------------- | ------ | -------------------------------------------------- |
| `ics`          | string | iCalendar text (one or more `VEVENT`s)             |
| `window_start` | string | ISO 8601 timestamp, converted to UTC (naive = UTC) |
| `window_end`   | string | ISO 8601 timestamp, converted to UTC (naive = UTC) |

Attendance semantics:

- Every instance of a recurring series **inherits the master event's**
  `ATTENDEE` list. A `RECURRENCE-ID` override that declares **any**
  `ATTENDEE` **replaces the whole list** for that instance (the master's
  attendees no longer apply); an override with no `ATTENDEE` keeps
  inheriting, just like its `SUMMARY` and duration. Cancelled and
  `EXDATE`d occurrences are not returned.
- Each attendee carries a normalized `address`, `partstat`, and `role`.
  A `mailto:` address has its scheme prefix removed and is lowercased
  (`mailto:Alice@Example.com` → `alice@example.com`); every other URI
  (e.g. `https:`, `tel:`, `urn:`) is kept verbatim. Missing `PARTSTAT`
  defaults to `NEEDS-ACTION`; missing `ROLE` defaults to
  `REQ-PARTICIPANT`.
- `counts` maps each `PARTSTAT` present on the instance to its number of
  attendees (an empty attendee list yields `{}`).
- Attendees are **stably sorted by normalized address**. The same address
  may repeat within one effective list only when its parameters agree
  (it then collapses to one entry); a repeat with conflicting `PARTSTAT`
  or `ROLE` returns `422`. Every `VEVENT`'s own list is validated
  eagerly, including events outside the window.

### Example

```bash
curl -s http://localhost:8000/attendance \
  -H 'Content-Type: application/json' \
  -d '{
    "ics": "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//demo//EN\r\nBEGIN:VEVENT\r\nUID:att-1\r\nDTSTART:20260105T090000Z\r\nDTEND:20260105T100000Z\r\nRRULE:FREQ=WEEKLY;COUNT=3\r\nSUMMARY:Planning\r\nATTENDEE;PARTSTAT=ACCEPTED;CN=Alice:mailto:Alice@Example.com\r\nATTENDEE;PARTSTAT=DECLINED:mailto:bob@example.com\r\nATTENDEE:https://cal.example.com/users/carol\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\nUID:att-1\r\nRECURRENCE-ID:20260112T090000Z\r\nDTSTART:20260112T090000Z\r\nDTEND:20260112T093000Z\r\nSUMMARY:Planning (small group)\r\nATTENDEE;PARTSTAT=TENTATIVE:mailto:dave@example.com\r\nEND:VEVENT\r\nEND:VCALENDAR",
    "window_start": "2026-01-01T00:00:00Z",
    "window_end": "2026-02-01T00:00:00Z"
  }'
```

The January 5 and 19 instances inherit the master's three attendees
(`Alice@Example.com` is normalized to `alice@example.com`; the non-mailto
URI is preserved). The January 12 override declares its own attendee and
therefore replaces the entire list; `counts` follows each effective list:

```json
{
  "window_start": "2026-01-01T00:00:00Z",
  "window_end": "2026-02-01T00:00:00Z",
  "count": 3,
  "instances": [
    {
      "uid": "att-1",
      "recurrence_id": "2026-01-05T09:00:00Z",
      "start": "2026-01-05T09:00:00Z",
      "end": "2026-01-05T10:00:00Z",
      "counts": {
        "ACCEPTED": 1,
        "DECLINED": 1,
        "NEEDS-ACTION": 1
      },
      "attendees": [
        {"address": "alice@example.com", "partstat": "ACCEPTED", "role": "REQ-PARTICIPANT"},
        {"address": "bob@example.com", "partstat": "DECLINED", "role": "REQ-PARTICIPANT"},
        {"address": "https://cal.example.com/users/carol", "partstat": "NEEDS-ACTION", "role": "REQ-PARTICIPANT"}
      ]
    },
    {
      "uid": "att-1",
      "recurrence_id": "2026-01-12T09:00:00Z",
      "start": "2026-01-12T09:00:00Z",
      "end": "2026-01-12T09:30:00Z",
      "counts": {
        "TENTATIVE": 1
      },
      "attendees": [
        {"address": "dave@example.com", "partstat": "TENTATIVE", "role": "REQ-PARTICIPANT"}
      ]
    },
    {
      "uid": "att-1",
      "recurrence_id": "2026-01-19T09:00:00Z",
      "start": "2026-01-19T09:00:00Z",
      "end": "2026-01-19T10:00:00Z",
      "counts": {
        "ACCEPTED": 1,
        "DECLINED": 1,
        "NEEDS-ACTION": 1
      },
      "attendees": [
        {"address": "alice@example.com", "partstat": "ACCEPTED", "role": "REQ-PARTICIPANT"},
        {"address": "bob@example.com", "partstat": "DECLINED", "role": "REQ-PARTICIPANT"},
        {"address": "https://cal.example.com/users/carol", "partstat": "NEEDS-ACTION", "role": "REQ-PARTICIPANT"}
      ]
    }
  ]
}
```

### `POST /validate`

Structurally validates iCalendar text and reports every problem found as a
diagnostic — **without any query window**. Unlike the other endpoints,
validation never fails a request for bad calendar content: both valid and
invalid calendars return `200`, and even text that cannot be parsed yields a
structured issue rather than an error status.

| Field | Type   | Description                            |
| ----- | ------ | -------------------------------------- |
| `ics` | string | iCalendar text (zero or more `VEVENT`s) |

The response is:

| Field         | Type    | Description                                                |
| ------------- | ------- | ---------------------------------------------------------- |
| `valid`       | boolean | `true` only when there are no issues                       |
| `error_count` | integer | Number of issues (`issues.length`)                         |
| `issues`      | array   | Diagnostics sorted by `event_index` (nulls first), `code`  |

Each issue carries:

| Field         | Type           | Description                                                       |
| ------------- | -------------- | ----------------------------------------------------------------- |
| `code`        | string         | Stable machine-readable code (see below)                          |
| `message`     | string         | Human-readable description                                        |
| `event_index` | integer \| null | 0-based index of the offending `VEVENT` in source order; `null` when no event can be located |
| `uid`         | string \| null  | The event's `UID`; `null` when the event has no usable UID        |

The checks run per `VEVENT`, in the order the events appear in the source
text; a single event can accumulate several issues. Cross-event checks run
afterwards. The stable codes are:

| Code                     | Meaning                                                                          |
| ------------------------ | -------------------------------------------------------------------------------- |
| `parse_error`            | The text could not be parsed as iCalendar at all (`event_index`/`uid` are null); no further checks are possible |
| `missing_uid`            | A `VEVENT` lacks the required `UID` property                                     |
| `missing_dtstart`        | A `VEVENT` lacks `DTSTART`, or its `DTSTART` is not a valid DATE/DATE-TIME value |
| `dtend_and_duration`     | A `VEVENT` declares both `DTEND` and `DURATION`, which RFC 5545 forbids          |
| `end_before_start`       | The declared end (`DTEND`, or `DTSTART + DURATION`) is equal to or earlier than the start, after normalization to UTC |
| `duplicate_master_event` | More than one `VEVENT` with the same `UID` has no `RECURRENCE-ID`; every such master is flagged |
| `orphan_override`        | A `VEVENT` with `RECURRENCE-ID` has no master event (a same-`UID` `VEVENT` without `RECURRENCE-ID`) |

Notes on semantics:

- Floating (timezone-less) values are assumed UTC, as on the other
  endpoints, so cross-timezone comparisons are correct (including DST);
  `DATE`-valued starts count as midnight UTC and a one-day all-day event
  has a next-day `DTEND`.
- An event with only `DTSTART` (zero-duration) is not flagged; only an
  *explicitly declared* end is order-checked. When `DTSTART` is unusable
  the ordering check is skipped (the `missing_dtstart` issue stands
  instead).
- Events without a usable `UID` take part only in the per-event checks;
  they can neither count as masters nor bind overrides.
- When both `DTEND` and `DURATION` are present, `DTEND` is still used for
  the ordering check, so an inverted event may report both
  `dtend_and_duration` and `end_before_start`.
- A missing `ics` request field is a normal schema failure (`422`); every
  well-formed request, even with garbage `ics`, gets `200`.

### Example

```bash
curl -s http://localhost:8000/validate \
  -H 'Content-Type: application/json' \
  -d '{
    "ics": "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//demo//EN\r\nBEGIN:VEVENT\r\nUID:series\r\nDTSTART:20260105T090000Z\r\nDURATION:PT1H\r\nRRULE:FREQ=WEEKLY;COUNT=3\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\nUID:series\r\nDTSTART:20260106T090000Z\r\nDTEND:20260106T100000Z\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\nUID:series\r\nRECURRENCE-ID:20260120T090000Z\r\nDTSTART:20260120T110000Z\r\nDTEND:20260120T100000Z\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\nDTSTART:20260121T090000Z\r\nDTEND:20260121T080000Z\r\nEND:VEVENT\r\nEND:VCALENDAR"
  }'
```

The second event is a second master for `UID:series` (both masters are
flagged); the override's end precedes its start; the last event is missing
its `UID` and also inverted. Issues come back ordered by event index and
then code:

```json
{
  "valid": false,
  "error_count": 5,
  "issues": [
    {
      "code": "duplicate_master_event",
      "message": "UID 'series' has 2 VEVENTs without RECURRENCE-ID; a recurring series may have only one master event",
      "event_index": 0,
      "uid": "series"
    },
    {
      "code": "duplicate_master_event",
      "message": "UID 'series' has 2 VEVENTs without RECURRENCE-ID; a recurring series may have only one master event",
      "event_index": 1,
      "uid": "series"
    },
    {
      "code": "end_before_start",
      "message": "VEVENT end 2026-01-20T10:00:00Z is not later than its start 2026-01-20T11:00:00Z",
      "event_index": 2,
      "uid": "series"
    },
    {
      "code": "end_before_start",
      "message": "VEVENT end 2026-01-21T08:00:00Z is not later than its start 2026-01-21T09:00:00Z",
      "event_index": 3,
      "uid": null
    },
    {
      "code": "missing_uid",
      "message": "VEVENT is missing the required UID property",
      "event_index": 3,
      "uid": null
    }
  ]
}
```

### Errors (`422 Unprocessable Entity`)

Returned with a descriptive `detail` message for: unparsable iCalendar
text, invalid `RRULE` (e.g. `FREQ=BOGUS`), unknown `TZID`
(non-IANA name), missing `DTSTART`/`UID`, `RECURRENCE-ID` with
`RANGE=THISANDFUTURE` (unsupported), `window_start >= window_end`, and
expansions exceeding 10 000 occurrences per UID within the window.

`/availability` additionally returns `422` for: an unknown `timezone`
(non-IANA name), a working period with `end <= start` or crossing
midnight, an offset-bearing working time, an unknown weekday key, and a
non-positive `minimum_minutes`.

`/conflicts` additionally returns `422` when `minimum_concurrency` is
missing or less than 2, and reuses the calendar and window validation of
the other endpoints (`window_start >= window_end`, unparsable ICS, invalid
recurrence, unknown `TZID`).

`/calendar-diff` reuses the same calendar and window validation, so it
returns `422` when `window_start >= window_end` or when either `old_ics` or
`new_ics` fails any of the `/expand` checks above.

`/agenda` additionally returns `422` for an unknown display `timezone`
(non-IANA name) and reuses the calendar and window validation of `/expand`
(unparsable ICS, invalid recurrence, unknown `TZID`,
`window_start >= window_end`).

`/alarms` additionally returns `422` for: an `ACTION` other than
`DISPLAY` (or a missing `ACTION`), a `DISPLAY` alarm without
`DESCRIPTION`, a missing or unsupported `TRIGGER` (only `DURATION` and
`DATE-TIME` values are supported; `RELATED` other than `START`/`END`, or
`RELATED` on an absolute trigger), exactly one of `REPEAT`/`DURATION`
present, a negative `REPEAT`, a non-positive alarm `DURATION`, a fan-out
beyond 1000 reminders, or an unknown `TZID` on an absolute trigger. All
`VALARM`s are validated eagerly, so an unsupported alarm yields `422`
even when its trigger lies outside the window; calendar-level failures
(unparsable ICS, bad window, invalid recurrence) reuse the `/expand`
checks.

`/attendance` additionally returns `422` when one effective attendee
list contains the same address more than once with conflicting
`PARTSTAT` or `ROLE` (addresses are compared after `mailto:` stripping
and lowercasing; explicit `NEEDS-ACTION`/`REQ-PARTICIPANT` agrees with an
omitted parameter). Every `VEVENT`'s own `ATTENDEE` list is validated
eagerly, so a conflict yields `422` even for events outside the window.
Calendar-level failures (unparsable ICS, bad window, invalid recurrence,
unknown `TZID`) reuse the `/expand` checks.

`/validate` is the deliberate exception: it never returns `422` for
calendar content. Unparsable text and malformed events are reported as
`200` responses with one or more issues (a `parse_error` issue has null
`event_index`/`uid`). The only `422` from `/validate` is a malformed
request body (e.g. a missing `ics` field).

## Semantics and limitations

- **Window overlap**: an instance is included when
  `start < window_end and end > window_start`; zero-duration instances when
  `window_start <= start < window_end`.
- **Overrides** replace the occurrence identified by `RECURRENCE-ID` even
  when the original falls outside the expansion window (e.g. moved into the
  window from outside). A `STATUS:CANCELLED` override removes the instance.
  An override without `DTEND`/`DURATION` inherits the master's duration.
- **Floating times** (no `Z`, no `TZID`) are interpreted as UTC.
- **All-day events** (`DATE`-valued `DTSTART`) start at local midnight and
  last one day when no `DTEND` is given; `/agenda` renders them on their
  authored calendar dates regardless of the display timezone.
- `RDATE`/`EXDATE` support `DATE` and `DATE-TIME` values (not `PERIOD`);
  `RECURRENCE-ID` with `RANGE=THISANDFUTURE` is not supported.
- A master event with `STATUS:CANCELLED` suppresses its whole series.

### Alarm semantics

- **Instance binding**: a master `VEVENT`'s `VALARM`s apply to every
  instance the recurrence engine expands from it; a `RECURRENCE-ID`
  override replaces the master's alarms for that instance with its own
  (an override with no `VALARM`s has no reminders). Cancelled instances
  and `EXDATE`d occurrences never produce reminders.
- **Triggers**: a duration-valued `TRIGGER` is added to the instance's
  DTSTART (default / `RELATED=START`) or DTEND (`RELATED=END`); signed
  durations point before (negative) or after (positive) the anchor. An
  absolute `TRIGGER;VALUE=DATE-TIME` is independent of the instance time
  and is normalized with its `Z`/`TZID` (floating = UTC).
- **Repeats**: `REPEAT:n` plus `DURATION:d` produces `n + 1` triggers at
  `base, base+d, …, base+n*d`. Reminders are filtered afterwards, so only
  the ones landing in the half-open window are returned.
- **Eager validation**: every `VALARM` in the supplied calendar is
  validated regardless of whether its event or trigger intersects the
  window; an unsupported or malformed alarm fails the whole request with
  `422`. Events without any `VALARM` simply contribute no reminders.

### Attendance semantics

- **Inheritance**: recurring instances use the master event's
  `ATTENDEE` list. A `RECURRENCE-ID` override that declares at least one
  `ATTENDEE` replaces that list wholesale for its instance; an override
  without `ATTENDEE` keeps inheriting the master's list. Inheritance
  never merges two lists, so a non-cancelled event with an empty
  effective list simply reports zero attendees.
- **Cancellations**: a `STATUS:CANCELLED` master or override, and an
  `EXDATE`d occurrence, produce no instance and therefore no attendance.
- **Address normalization**: the `mailto:` scheme prefix (matched
  case-insensitively) is stripped and the remainder lowercased; any
  other URI scheme is preserved exactly as written.
- **Defaults and casing**: missing `PARTSTAT` is `NEEDS-ACTION` and
  missing `ROLE` is `REQ-PARTICIPANT`; supplied values are uppercased.
  `counts` counts attendees per `PARTSTAT` in address order.
- **Duplicates within one effective list**: equal addresses with equal
  effective parameters collapse to a single attendee; conflicting
  `PARTSTAT` or `ROLE` fails the request with `422`. Attendees are
  sorted by normalized address for a stable response.

### Availability semantics

- **Working hours are local**: each weekday's periods are materialized per
  local date with `zoneinfo` and converted to UTC, so local times shift at
  DST transitions (the UTC timeline is built day by day — never by a fixed
  offset). Periods are clipped to the query window.
- **Busy intervals** are the expanded instances of every calendar,
  excluding `STATUS:CANCELLED` and non-positive-duration instances. Each is
  intersected with the working periods before merging, so events outside
  working time never remove free slots.
- **Half-open intervals** `[start, end)` are used throughout: two busy
  intervals that touch at an instant merge, and a busy event ending exactly
  when a free slot begins does not consume it.
- **Common free time** is the complement of the merged union of *all*
  calendars' busy intervals within the merged working periods; gaps between
  a day's working periods (e.g. lunch) and non-working weekdays are never
  returned.
- `minimum_minutes` is applied to slot length and is inclusive at the
  boundary; `minutes` is the integer UTC duration. Slots are sorted by
  `start` and serialized with the `Z` suffix.
- Working periods crossing local midnight are not supported (a range must
  start and end on the same local date).

### Conflict semantics

- **Participants** are the expanded instances of every named calendar,
  excluding `STATUS:CANCELLED` (master or override) and non-positive-duration
  instances. Each participating interval is clipped to the query window
  before the sweep, so an instance starting before `window_start` or ending
  after `window_end` contributes only the in-window part; an instance merely
  touching a boundary contributes nothing.
- **Half-open sweep**: the line visits all start/end instants sorted by time;
  at one instant every end is applied before every start, so back-to-back
  intervals `[a, b)` and `[b, c)` share no instant and never count as
  concurrent. The active set is constant between two consecutive instants.
- **Segments** are the non-empty maximal intervals whose active count is at
  least `minimum_concurrency` (which must be ≥ 2). Each segment carries its
  `count`, equal to the number of `participants`.
- **Merging** joins two qualifying segments only when they are adjacent
  *and* their participating instances are exactly the same; segments with
  the same participants separated by a gap (or by a below-threshold stretch)
  remain distinct. The result is sorted by `start`.
- **Participant order and shape**: participants are sorted by `calendar`,
  then `UID`, then `RECURRENCE-ID` with `null` first; each carries the
  `calendar` name plus the usual `/expand` fields (`uid`, `recurrence_id`,
  `summary`, `start`, `end`, `status`), with `start`/`end` clipped to the
  window.

### Agenda semantics

- **Expansion first**: `/agenda` runs the same `/expand` engine against the
  UTC window, so window overlap, overrides, `EXDATE`, floating-time handling
  and the 10 000-occurrence cap are identical; only non-cancelled instances
  are rendered.
- **Local-day splitting**: timed instances are converted to the display
  timezone with `astimezone` and intersected with each local date
  `[00:00, next 00:00)`. Day boundaries are constructed per date with
  `zoneinfo` (not by adding 24 hours), keeping fragments correct on DST
  transition days. A fragment ending exactly where the instance ends (or
  starting exactly where it starts) gets the matching continuation flag
  cleared; fragments never spill outside the instance.
- **All-day dates are timezone-free**: a `DATE` value denotes a calendar
  date, not a UTC midnight. The agenda recovers the authored dates (the
  expander stores DATE midnights as floating/UTC) and renders each as local
  midnight in the display timezone, so a DATE on Jan 5 appears on Jan 5 in
  Tokyo, Los Angeles and everywhere else. A `DTEND;VALUE=DATE` is exclusive,
  as in RFC 5545, and recurring all-day series expand day by day.
- **Ordering** is by `(date, all_day first, local start, uid)`; `uid` is the
  final tie-breaker for fragments that start at the same instant.
- A `RECURRENCE-ID` override with a `DATE` `DTSTART` turns that occurrence
  into an all-day entry, and vice versa — each instance is rendered
  according to its own effective `DTSTART` type.
- Zero-duration timed instances produce one fragment on their local date
  with both continuation flags `false`.

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -v          # run the test suite
uvicorn app.main:app --reload       # run locally on port 8000
```

## Project layout

```
app/main.py          FastAPI app: POST /expand, POST /agenda, POST /alarms,
                     POST /attendance, POST /calendar-diff,
                     POST /availability, POST /conflicts, POST /validate,
                     GET /health
app/expander.py      recurrence expansion, override merging, TZ/DST handling
app/agenda.py        local-day agenda fragments, midnight splitting, DATE handling
app/validation.py    structural VEVENT checks, stable issue codes, sorting
app/alarms.py        VALARM parsing, REPEAT/DURATION fan-out, window filtering
app/attendance.py    ATTENDEE inheritance, address normalization, PARTSTAT counts
app/diff.py          two-calendar instance diff by UID + RECURRENCE-ID
app/availability.py  working-period timeline, busy clipping/merge, free complement
app/conflicts.py     half-open sweep for minimum-concurrency segments
tests/               key tests for all endpoints (recurrences, DST, 422s)
Dockerfile           python:3.12-slim image running uvicorn on port 8000
docker-compose.yml   single-command startup
```
