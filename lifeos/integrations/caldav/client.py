"""Apple iCloud CalDAV client for busy-slot reads and strict event-only writes."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import caldav
from icalendar import Calendar as ICalendar
from icalendar import Event as ICalEvent

from lifeos.config import Settings
from lifeos.db.models import Task
from lifeos.logging import get_logger
from lifeos.schemas.planner import CalendarSlot

logger = get_logger(__name__)

_BUCKET_DURATION_MINUTES: dict[int, int] = {
    1: 15,
    2: 30,
    3: 60,
    4: 90,
    5: 120,
}


def _resolve_tz(tz_name: str) -> tzinfo:
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        return UTC


def _normalize_ical_dt(value: date | datetime, default_tz: tzinfo) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=default_tz)
        return value
    return datetime(value.year, value.month, value.day, tzinfo=default_tz)


class ICloudCalDAVService:
    """Events-only Apple iCloud CalDAV integration executed off the async event loop."""

    def __init__(self, settings: Settings, caldav_client: Any = None) -> None:
        self.settings = settings
        self._caldav_client = caldav_client
        self._tz = _resolve_tz(settings.timezone)

    def _get_calendar_sync(self) -> Any:
        client = self._caldav_client
        if client is None:
            if not self.settings.caldav_username or not self.settings.caldav_app_password:
                raise RuntimeError(
                    "Apple iCloud CalDAV credentials are not configured"
                )
            client = caldav.DAVClient(
                url=self.settings.caldav_url,
                username=self.settings.caldav_username,
                password=self.settings.caldav_app_password,
            )
            self._caldav_client = client

        principal = client.principal()
        calendars = list(principal.calendars())
        if not calendars:
            raise RuntimeError("No CalDAV calendars found for configured account")

        target_name = self.settings.caldav_calendar_name.strip().lower()
        for cal in calendars:
            cal_name = (getattr(cal, "name", None) or "").strip().lower()
            if cal_name == target_name:
                return cal

        logger.warning(
            "Configured CalDAV calendar '%s' not found; falling back to '%s'",
            self.settings.caldav_calendar_name,
            getattr(calendars[0], "name", "default"),
        )
        return calendars[0]

    def _fetch_events_sync(self, start_dt: datetime, end_dt: datetime) -> list[CalendarSlot]:
        cal = self._get_calendar_sync()
        if hasattr(cal, "search"):
            raw_events = cal.search(start=start_dt, end=end_dt, event=True, expand=True)
        else:
            raw_events = cal.date_search(start_dt, end_dt, expand=True)

        slots: list[CalendarSlot] = []
        for item in raw_events:
            raw_data = getattr(item, "data", None)
            if raw_data:
                ical = ICalendar.from_ical(raw_data)
            elif hasattr(item, "icalendar_instance"):
                ical = item.icalendar_instance
            else:
                continue

            for component in ical.walk():
                if component.name != "VEVENT":
                    continue
                dtstart_prop = component.get("DTSTART")
                if dtstart_prop is None:
                    continue
                start_at = _normalize_ical_dt(dtstart_prop.dt, self._tz)

                dtend_prop = component.get("DTEND")
                if dtend_prop is not None:
                    end_at = _normalize_ical_dt(dtend_prop.dt, self._tz)
                else:
                    duration_prop = component.get("DURATION")
                    delta = duration_prop.dt if duration_prop is not None else timedelta(hours=1)
                    end_at = start_at + delta

                uid = str(component.get("UID") or "")
                summary = str(component.get("SUMMARY") or "Busy")
                slots.append(
                    CalendarSlot(
                        uid=uid,
                        title=summary,
                        start_at=start_at,
                        end_at=end_at,
                    )
                )

        slots.sort(key=lambda slot: slot.start_at)
        return slots

    async def fetch_busy_slots(self, start_dt: datetime, end_dt: datetime) -> list[CalendarSlot]:
        """Fetch VEVENT busy slots in [start_dt, end_dt] from Apple iCloud CalDAV."""
        return await asyncio.to_thread(self._fetch_events_sync, start_dt, end_dt)

    def _build_vevent_ical(self, task: Task, uid: str) -> str:
        assert task.due_at is not None
        start_at = _normalize_ical_dt(task.due_at, self._tz)
        bucket = int(task.duration_bucket) if task.duration_bucket is not None else 2
        minutes = _BUCKET_DURATION_MINUTES.get(bucket, 60)
        end_at = start_at + timedelta(minutes=minutes)

        cal = ICalendar()
        cal.add("prodid", "-//LifeOS//iCloud CalDAV Sync//EN")
        cal.add("version", "2.0")

        event = ICalEvent()
        event.add("uid", uid)
        event.add("dtstamp", datetime.now(UTC))
        event.add("dtstart", start_at)
        event.add("dtend", end_at)
        event.add("summary", task.title)
        description = f"DoD: {task.dod}\nLifeOS ID: {task.id}"
        event.add("description", description)

        cal.add_component(event)
        return cal.to_ical().decode("utf-8")

    def _upsert_event_sync(self, ical_payload: str) -> None:
        cal = self._get_calendar_sync()
        cal.save_event(ical_payload)

    async def upsert_event(self, task: Task) -> str:
        """Write an event-typed Task to Apple Calendar. Rejects non-event items."""
        if task.item_type != "event":
            raise ValueError("Only items with item_type='event' may be written to Apple Calendar")
        if task.due_at is None:
            raise ValueError("Calendar event requires task.due_at")

        uid = task.caldav_uid or task.id
        ical_payload = self._build_vevent_ical(task, uid)
        await asyncio.to_thread(self._upsert_event_sync, ical_payload)
        return uid
