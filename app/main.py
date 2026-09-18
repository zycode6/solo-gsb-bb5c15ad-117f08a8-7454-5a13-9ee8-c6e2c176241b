"""FastAPI surface for the iCalendar expansion and availability service."""

from __future__ import annotations

import datetime as dt

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, field_serializer, model_validator

from .agenda import AgendaError, AgendaFragment, build_agenda
from .alarms import AlarmError, Reminder, expand_alarms
from .attendance import (
    AttendanceError,
    InstanceAttendance,
    attendance_for_calendar,
    partstat_counts,
)
from .availability import (
    WEEKDAY_NAMES,
    AvailabilityError,
    LocalRange,
    build_working_periods,
    busy_intervals,
    common_free_slots,
)
from .conflicts import ConflictSegment, ConflictsError, Participant, find_conflicts
from .diff import CalendarDiff, diff_calendars
from .expander import ExpansionError, Instance, expand_calendar
from .validation import Issue, validate_ics

UTC = dt.timezone.utc

app = FastAPI(
    title="iCalendar Recurrence Expansion and Availability API",
    version="1.7.0",
    summary="Expand DTSTART/RRULE/RDATE/EXDATE and RECURRENCE-ID overrides "
    "into concrete UTC instances, intersect named calendars with "
    "local working hours to find common free time, find high-concurrency "
    "segments across named calendars, expand VALARM reminders, report "
    "per-instance ATTENDEE participation status inside a query window, and "
    "validate raw iCalendar text structurally.",
)


def _to_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:  # naive input is assumed to be UTC
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _iso_z(value: dt.datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class ExpandRequest(BaseModel):
    ics: str = Field(..., description="iCalendar (RFC 5545) text to expand")
    window_start: dt.datetime = Field(
        ..., description="Start of the query window (ISO 8601, converted to UTC)"
    )
    window_end: dt.datetime = Field(
        ..., description="End of the query window (ISO 8601, converted to UTC)"
    )


class InstanceOut(BaseModel):
    uid: str
    recurrence_id: dt.datetime | None
    summary: str | None
    start: dt.datetime
    end: dt.datetime
    status: str

    @field_serializer("recurrence_id", "start", "end")
    def _serialize_dt(self, value: dt.datetime | None) -> str | None:
        return _iso_z(value)


class ExpandResponse(BaseModel):
    window_start: dt.datetime
    window_end: dt.datetime
    count: int
    instances: list[InstanceOut]

    @field_serializer("window_start", "window_end")
    def _serialize_window(self, value: dt.datetime) -> str:
        return _iso_z(value)


# ---------------------------------------------------------------------------
# POST /agenda
# ---------------------------------------------------------------------------


class AgendaRequest(BaseModel):
    ics: str = Field(..., description="iCalendar (RFC 5545) text to render")
    window_start: dt.datetime = Field(
        ..., description="Start of the UTC query window (ISO 8601, naive = UTC)"
    )
    window_end: dt.datetime = Field(
        ..., description="End of the UTC query window (ISO 8601, naive = UTC)"
    )
    timezone: str = Field(
        ...,
        description="IANA timezone used for local dates and timestamps, "
        "e.g. 'Asia/Tokyo'",
    )


class AgendaEntryOut(BaseModel):
    date: dt.date = Field(..., description="Local date this fragment is shown under")
    uid: str
    summary: str | None
    start: dt.datetime = Field(..., description="Local start in the display timezone")
    end: dt.datetime = Field(..., description="Local end in the display timezone")
    all_day: bool
    continues_before: bool = Field(
        ..., description="The instance already began on an earlier local date"
    )
    continues_after: bool = Field(
        ..., description="The instance continues onto a later local date"
    )


class AgendaResponse(BaseModel):
    window_start: dt.datetime
    window_end: dt.datetime
    timezone: str
    count: int
    entries: list[AgendaEntryOut]

    @field_serializer("window_start", "window_end")
    def _serialize_window(self, value: dt.datetime) -> str:
        return _iso_z(value)


# ---------------------------------------------------------------------------
# POST /alarms
# ---------------------------------------------------------------------------


class AlarmsRequest(BaseModel):
    ics: str = Field(..., description="iCalendar (RFC 5545) text to scan for alarms")
    window_start: dt.datetime = Field(
        ..., description="Start of the query window (ISO 8601, converted to UTC)"
    )
    window_end: dt.datetime = Field(
        ..., description="End of the query window (ISO 8601, converted to UTC)"
    )


class AlarmOut(BaseModel):
    uid: str
    recurrence_id: dt.datetime | None
    summary: str | None
    trigger: dt.datetime
    description: str

    @field_serializer("recurrence_id", "trigger")
    def _serialize_dt(self, value: dt.datetime | None) -> str | None:
        return _iso_z(value)


class AlarmsResponse(BaseModel):
    window_start: dt.datetime
    window_end: dt.datetime
    count: int
    alarms: list[AlarmOut]

    @field_serializer("window_start", "window_end")
    def _serialize_window(self, value: dt.datetime) -> str:
        return _iso_z(value)


# ---------------------------------------------------------------------------
# POST /attendance
# ---------------------------------------------------------------------------


class AttendanceRequest(BaseModel):
    ics: str = Field(
        ..., description="iCalendar (RFC 5545) text whose attendees are reported"
    )
    window_start: dt.datetime = Field(
        ..., description="Start of the query window (ISO 8601, converted to UTC)"
    )
    window_end: dt.datetime = Field(
        ..., description="End of the query window (ISO 8601, converted to UTC)"
    )


class AttendeeOut(BaseModel):
    address: str
    partstat: str
    role: str


class AttendanceInstanceOut(BaseModel):
    uid: str
    recurrence_id: dt.datetime | None
    start: dt.datetime
    end: dt.datetime
    counts: dict[str, int]
    attendees: list[AttendeeOut]

    @field_serializer("recurrence_id", "start", "end")
    def _serialize_dt(self, value: dt.datetime | None) -> str | None:
        return _iso_z(value)


class AttendanceResponse(BaseModel):
    window_start: dt.datetime
    window_end: dt.datetime
    count: int
    instances: list[AttendanceInstanceOut]

    @field_serializer("window_start", "window_end")
    def _serialize_window(self, value: dt.datetime) -> str:
        return _iso_z(value)


# ---------------------------------------------------------------------------
# POST /availability
# ---------------------------------------------------------------------------


class WorkRange(BaseModel):
    """One local working period within a day (wall-clock times, no zone)."""

    model_config = {"extra": "forbid"}

    start: dt.time = Field(
        ..., description="Local start time, e.g. '09:00' (no timezone offset)"
    )
    end: dt.time = Field(
        ..., description="Local end time, same local date as start"
    )

    @model_validator(mode="after")
    def _validate_range(self) -> "WorkRange":
        if self.start.tzinfo is not None or self.end.tzinfo is not None:
            raise ValueError("working hours must be naive local times (no offset)")
        if self.start >= self.end:
            raise ValueError(
                "working period end must be after start on the same local date "
                "(overnight ranges are not supported)"
            )
        return self


class WorkingHours(BaseModel):
    """Per-weekday working periods; a missing weekday means no work that day."""

    model_config = {"extra": "forbid"}

    monday: list[WorkRange] = Field(default_factory=list)
    tuesday: list[WorkRange] = Field(default_factory=list)
    wednesday: list[WorkRange] = Field(default_factory=list)
    thursday: list[WorkRange] = Field(default_factory=list)
    friday: list[WorkRange] = Field(default_factory=list)
    saturday: list[WorkRange] = Field(default_factory=list)
    sunday: list[WorkRange] = Field(default_factory=list)


class AvailabilityRequest(BaseModel):
    calendars: dict[str, str] = Field(
        default_factory=dict,
        description="Named iCalendar texts; free time must avoid events in all "
        "of them. May be empty.",
    )
    window_start: dt.datetime = Field(
        ..., description="Start of the query window (ISO 8601, converted to UTC)"
    )
    window_end: dt.datetime = Field(
        ..., description="End of the query window (ISO 8601, converted to UTC)"
    )
    timezone: str = Field(
        ..., description="IANA timezone for the working hours, e.g. 'Europe/Berlin'"
    )
    working_hours: WorkingHours
    minimum_minutes: int = Field(
        ..., gt=0, description="Only free slots at least this long are returned"
    )


class FreeSlotOut(BaseModel):
    start: dt.datetime
    end: dt.datetime
    minutes: int

    @field_serializer("start", "end")
    def _serialize_dt(self, value: dt.datetime) -> str:
        return _iso_z(value)


class AvailabilityResponse(BaseModel):
    window_start: dt.datetime
    window_end: dt.datetime
    timezone: str
    count: int
    slots: list[FreeSlotOut]

    @field_serializer("window_start", "window_end")
    def _serialize_window(self, value: dt.datetime) -> str:
        return _iso_z(value)


# ---------------------------------------------------------------------------
# POST /conflicts
# ---------------------------------------------------------------------------


class ConflictsRequest(BaseModel):
    calendars: dict[str, str] = Field(
        ...,
        description="Named iCalendar texts whose instances are scanned together",
    )
    window_start: dt.datetime = Field(
        ..., description="Start of the query window (ISO 8601, converted to UTC)"
    )
    window_end: dt.datetime = Field(
        ..., description="End of the query window (ISO 8601, converted to UTC)"
    )
    minimum_concurrency: int = Field(
        ...,
        ge=2,
        description="Report segments with at least this many concurrent "
        "instances (must be >= 2)",
    )


class ParticipantOut(BaseModel):
    """One participating instance and the calendar it came from."""

    calendar: str
    uid: str
    recurrence_id: dt.datetime | None
    summary: str | None
    start: dt.datetime
    end: dt.datetime
    status: str

    @field_serializer("recurrence_id", "start", "end")
    def _serialize_dt(self, value: dt.datetime | None) -> str | None:
        return _iso_z(value)


class ConflictSegmentOut(BaseModel):
    start: dt.datetime
    end: dt.datetime
    count: int
    participants: list[ParticipantOut]

    @field_serializer("start", "end")
    def _serialize_dt(self, value: dt.datetime) -> str:
        return _iso_z(value)


class ConflictsResponse(BaseModel):
    window_start: dt.datetime
    window_end: dt.datetime
    minimum_concurrency: int
    count: int
    segments: list[ConflictSegmentOut]

    @field_serializer("window_start", "window_end")
    def _serialize_window(self, value: dt.datetime) -> str:
        return _iso_z(value)


def _instance_out(instance: Instance) -> InstanceOut:
    return InstanceOut(
        uid=instance.uid,
        recurrence_id=instance.recurrence_id,
        summary=instance.summary,
        start=instance.start,
        end=instance.end,
        status=instance.status,
    )


def _agenda_entry_out(fragment: AgendaFragment) -> AgendaEntryOut:
    return AgendaEntryOut(
        date=fragment.date,
        uid=fragment.uid,
        summary=fragment.summary,
        start=fragment.start,
        end=fragment.end,
        all_day=fragment.all_day,
        continues_before=fragment.continues_before,
        continues_after=fragment.continues_after,
    )


def _alarm_out(reminder: Reminder) -> AlarmOut:
    return AlarmOut(
        uid=reminder.uid,
        recurrence_id=reminder.recurrence_id,
        summary=reminder.summary,
        trigger=reminder.trigger,
        description=reminder.description,
    )


def _attendance_instance_out(item: InstanceAttendance) -> AttendanceInstanceOut:
    attendees = [
        AttendeeOut(
            address=attendee.address,
            partstat=attendee.partstat,
            role=attendee.role,
        )
        for attendee in item.attendees
    ]
    return AttendanceInstanceOut(
        uid=item.uid,
        recurrence_id=item.recurrence_id,
        start=item.start,
        end=item.end,
        counts=partstat_counts(item.attendees),
        attendees=attendees,
    )


def _participant_out(participant: Participant) -> ParticipantOut:
    return ParticipantOut(
        calendar=participant.calendar,
        uid=participant.uid,
        recurrence_id=participant.recurrence_id,
        summary=participant.summary,
        start=participant.start,
        end=participant.end,
        status=participant.status,
    )


def _issue_out(issue: Issue) -> IssueOut:
    return IssueOut(
        code=issue.code,
        message=issue.message,
        event_index=issue.event_index,
        uid=issue.uid,
    )


# ---------------------------------------------------------------------------
# POST /calendar-diff
# ---------------------------------------------------------------------------


class CalendarDiffRequest(BaseModel):
    old_ics: str = Field(..., description="Old version of the iCalendar text")
    new_ics: str = Field(..., description="New version of the iCalendar text")
    window_start: dt.datetime = Field(
        ..., description="Start of the shared query window (ISO 8601, UTC)"
    )
    window_end: dt.datetime = Field(
        ..., description="End of the shared query window (ISO 8601, UTC)"
    )


class ModifiedInstanceOut(BaseModel):
    """One identity present on both sides whose content changed."""

    before: InstanceOut
    after: InstanceOut


class CalendarDiffResponse(BaseModel):
    window_start: dt.datetime
    window_end: dt.datetime
    added: list[InstanceOut]
    removed: list[InstanceOut]
    modified: list[ModifiedInstanceOut]

    @field_serializer("window_start", "window_end")
    def _serialize_window(self, value: dt.datetime) -> str:
        return _iso_z(value)


# ---------------------------------------------------------------------------
# POST /validate
# ---------------------------------------------------------------------------


class ValidateRequest(BaseModel):
    ics: str = Field(..., description="iCalendar (RFC 5545) text to validate")


class IssueOut(BaseModel):
    code: str = Field(..., description="Stable machine-readable issue code")
    message: str = Field(..., description="Human-readable diagnostic")
    event_index: int | None = Field(
        None,
        description="0-based index of the offending VEVENT in source order; "
        "null when no event can be located",
    )
    uid: str | None = Field(None, description="UID of the offending event, if any")


class ValidateResponse(BaseModel):
    valid: bool
    error_count: int
    issues: list[IssueOut]


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/validate", response_model=ValidateResponse)
def validate(request: ValidateRequest) -> ValidateResponse:
    # Validation is diagnostic: even unparsable text yields 200 with issues.
    report = validate_ics(request.ics)
    return ValidateResponse(
        valid=report.valid,
        error_count=report.error_count,
        issues=[_issue_out(issue) for issue in report.issues],
    )


@app.post("/expand", response_model=ExpandResponse)
def expand(request: ExpandRequest) -> ExpandResponse:
    window_start = _to_utc(request.window_start)
    window_end = _to_utc(request.window_end)
    if window_start >= window_end:
        raise HTTPException(
            status_code=422, detail="window_start must be before window_end"
        )
    try:
        instances = expand_calendar(request.ics, window_start, window_end)
    except ExpansionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ExpandResponse(
        window_start=window_start,
        window_end=window_end,
        count=len(instances),
        instances=[_instance_out(i) for i in instances],
    )


@app.post("/agenda", response_model=AgendaResponse)
def agenda(request: AgendaRequest) -> AgendaResponse:
    window_start = _to_utc(request.window_start)
    window_end = _to_utc(request.window_end)
    if window_start >= window_end:
        raise HTTPException(
            status_code=422, detail="window_start must be before window_end"
        )
    try:
        fragments = build_agenda(
            request.ics, window_start, window_end, request.timezone
        )
    except AgendaError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return AgendaResponse(
        window_start=window_start,
        window_end=window_end,
        timezone=request.timezone,
        count=len(fragments),
        entries=[_agenda_entry_out(f) for f in fragments],
    )


@app.post("/alarms", response_model=AlarmsResponse)
def alarms(request: AlarmsRequest) -> AlarmsResponse:
    window_start = _to_utc(request.window_start)
    window_end = _to_utc(request.window_end)
    if window_start >= window_end:
        raise HTTPException(
            status_code=422, detail="window_start must be before window_end"
        )
    try:
        reminders = expand_alarms(request.ics, window_start, window_end)
    except (ExpansionError, AlarmError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return AlarmsResponse(
        window_start=window_start,
        window_end=window_end,
        count=len(reminders),
        alarms=[_alarm_out(r) for r in reminders],
    )


@app.post("/attendance", response_model=AttendanceResponse)
def attendance(request: AttendanceRequest) -> AttendanceResponse:
    window_start = _to_utc(request.window_start)
    window_end = _to_utc(request.window_end)
    if window_start >= window_end:
        raise HTTPException(
            status_code=422, detail="window_start must be before window_end"
        )
    try:
        items = attendance_for_calendar(request.ics, window_start, window_end)
    except (ExpansionError, AttendanceError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return AttendanceResponse(
        window_start=window_start,
        window_end=window_end,
        count=len(items),
        instances=[_attendance_instance_out(item) for item in items],
    )


@app.post("/calendar-diff", response_model=CalendarDiffResponse)
def calendar_diff(request: CalendarDiffRequest) -> CalendarDiffResponse:
    window_start = _to_utc(request.window_start)
    window_end = _to_utc(request.window_end)
    if window_start >= window_end:
        raise HTTPException(
            status_code=422, detail="window_start must be before window_end"
        )
    try:
        diff: CalendarDiff = diff_calendars(
            request.old_ics, request.new_ics, window_start, window_end
        )
    except ExpansionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return CalendarDiffResponse(
        window_start=window_start,
        window_end=window_end,
        added=[_instance_out(i) for i in diff.added],
        removed=[_instance_out(i) for i in diff.removed],
        modified=[
            ModifiedInstanceOut(
                before=_instance_out(before),
                after=_instance_out(after),
            )
            for before, after in diff.modified
        ],
    )


@app.post("/availability", response_model=AvailabilityResponse)
def availability(request: AvailabilityRequest) -> AvailabilityResponse:
    window_start = _to_utc(request.window_start)
    window_end = _to_utc(request.window_end)
    if window_start >= window_end:
        raise HTTPException(
            status_code=422, detail="window_start must be before window_end"
        )

    ranges_by_weekday: dict[int, list[LocalRange]] = {
        idx: [LocalRange(start=r.start, end=r.end) for r in getattr(request.working_hours, name)]
        for idx, name in enumerate(WEEKDAY_NAMES)
    }

    try:
        working = build_working_periods(
            window_start, window_end, request.timezone, ranges_by_weekday
        )
        busy = busy_intervals(
            request.calendars, window_start, window_end, working
        )
        slots = common_free_slots(
            busy, working, dt.timedelta(minutes=request.minimum_minutes)
        )
    except AvailabilityError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return AvailabilityResponse(
        window_start=window_start,
        window_end=window_end,
        timezone=request.timezone,
        count=len(slots),
        slots=[
            FreeSlotOut(
                start=start,
                end=end,
                minutes=int((end - start).total_seconds() // 60),
            )
            for start, end in slots
        ],
    )


@app.post("/conflicts", response_model=ConflictsResponse)
def conflicts(request: ConflictsRequest) -> ConflictsResponse:
    window_start = _to_utc(request.window_start)
    window_end = _to_utc(request.window_end)
    if window_start >= window_end:
        raise HTTPException(
            status_code=422, detail="window_start must be before window_end"
        )
    try:
        segments: list[ConflictSegment] = find_conflicts(
            request.calendars,
            window_start,
            window_end,
            request.minimum_concurrency,
        )
    except ConflictsError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ConflictsResponse(
        window_start=window_start,
        window_end=window_end,
        minimum_concurrency=request.minimum_concurrency,
        count=len(segments),
        segments=[
            ConflictSegmentOut(
                start=segment.start,
                end=segment.end,
                count=segment.count,
                participants=[_participant_out(p) for p in segment.participants],
            )
            for segment in segments
        ],
    )
