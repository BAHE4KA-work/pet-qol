"""Async Notion 5-database bidirectional sync service."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from notion_client import AsyncClient

from lifeos.config import Settings
from lifeos.db.models import DailyTelemetry, Task, WeeklyInsight
from lifeos.integrations.notion.schema import (
    build_goals_db_properties,
    build_insights_db_properties,
    build_parent_task_relation_property,
    build_projects_db_properties,
    build_tasks_db_properties,
    build_telemetry_db_properties,
)
from lifeos.logging import get_logger

logger = get_logger(__name__)

_NOTION_RICH_TEXT_CHUNK = 2000

_TASK_STATUS_TO_NOTION: dict[str, str] = {
    "active": "Active",
    "candidate": "Candidate",
    "someday": "Someday",
    "done": "Done",
    "dismissed": "Dismissed",
}


def _rich_text(value: str | None) -> list[dict[str, Any]]:
    if not value:
        return []
    chunks = [
        value[i : i + _NOTION_RICH_TEXT_CHUNK]
        for i in range(0, len(value), _NOTION_RICH_TEXT_CHUNK)
    ]
    return [{"type": "text", "text": {"content": chunk}} for chunk in chunks]


def _title_prop(value: str) -> dict[str, Any]:
    return {"title": _rich_text(value or "Untitled")}


def _extract_plain_text(items: list[dict[str, Any]] | None) -> str:
    if not items:
        return ""
    return "".join(part.get("plain_text", "") for part in items).strip()


def _extract_select(prop: dict[str, Any] | None) -> str | None:
    if not prop:
        return None
    sel = prop.get("select")
    return sel.get("name") if isinstance(sel, dict) else None


def _extract_number(prop: dict[str, Any] | None) -> float | None:
    if not prop:
        return None
    val = prop.get("number")
    return float(val) if val is not None else None


def _extract_date_start(prop: dict[str, Any] | None) -> str | None:
    if not prop:
        return None
    dt = prop.get("date")
    return dt.get("start") if isinstance(dt, dict) else None


def _extract_relation_first_id(prop: dict[str, Any] | None) -> str | None:
    if not prop:
        return None
    rel = prop.get("relation")
    if isinstance(rel, list) and rel:
        first = rel[0]
        return first.get("id") if isinstance(first, dict) else None
    return None


def _extract_data_source_id(db_obj: dict[str, Any]) -> str | None:
    data_sources = db_obj.get("data_sources")
    if isinstance(data_sources, list) and data_sources:
        first = data_sources[0]
        if isinstance(first, dict) and first.get("id"):
            return str(first["id"])
    return None


class NotionSyncService:
    """Bidirectional synchronization wrapper around Notion's async API."""

    def __init__(self, settings: Settings, client: Any = None) -> None:
        self.settings = settings
        if client is not None:
            self._client: Any = client
        elif settings.notion_token:
            self._client = AsyncClient(auth=settings.notion_token)
        else:
            self._client = None
        self._data_source_cache: dict[str, str] = {}

    def _require_client(self) -> Any:
        if self._client is None:
            raise RuntimeError("Notion client is not configured (missing notion_token)")
        return self._client

    def _uses_data_sources(self) -> bool:
        client = self._require_client()
        return hasattr(client, "data_sources") and not hasattr(client.databases, "query")

    async def _resolve_data_source_id(self, database_id: str) -> str:
        if database_id in self._data_source_cache:
            return self._data_source_cache[database_id]
        client = self._require_client()
        db_obj = await client.databases.retrieve(database_id=database_id)
        ds_id = _extract_data_source_id(db_obj) or database_id
        self._data_source_cache[database_id] = ds_id
        return ds_id

    async def _create_database(
        self,
        parent_page_id: str,
        title: str,
        properties: dict[str, Any],
    ) -> tuple[str, str]:
        client = self._require_client()
        title_payload = [{"type": "text", "text": {"content": title}}]
        parent_payload = {"type": "page_id", "page_id": parent_page_id}
        if self._uses_data_sources():
            resp = await client.databases.create(
                parent=parent_payload,
                title=title_payload,
                initial_data_source={"properties": properties},
            )
            db_id = str(resp["id"])
            ds_id = _extract_data_source_id(resp) or db_id
            self._data_source_cache[db_id] = ds_id
            return db_id, ds_id
        resp = await client.databases.create(
            parent=parent_payload,
            title=title_payload,
            properties=properties,
        )
        db_id = str(resp["id"])
        return db_id, db_id

    async def _create_page(self, database_id: str, properties: dict[str, Any]) -> str:
        client = self._require_client()
        if self._uses_data_sources():
            ds_id = await self._resolve_data_source_id(database_id)
            parent = {"type": "data_source_id", "data_source_id": ds_id}
        else:
            parent = {"database_id": database_id}
        resp = await client.pages.create(parent=parent, properties=properties)
        return str(resp["id"])

    async def _query_database(
        self,
        database_id: str,
        *,
        filter_payload: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        client = self._require_client()
        results: list[dict[str, Any]] = []
        cursor: str | None = None
        use_ds = self._uses_data_sources()
        ds_id = await self._resolve_data_source_id(database_id) if use_ds else database_id

        while True:
            kwargs: dict[str, Any] = {}
            if filter_payload is not None:
                kwargs["filter"] = filter_payload
            if cursor is not None:
                kwargs["start_cursor"] = cursor

            if use_ds:
                resp = await client.data_sources.query(data_source_id=ds_id, **kwargs)
            else:
                resp = await client.databases.query(database_id=database_id, **kwargs)

            results.extend(resp.get("results", []))
            if not resp.get("has_more"):
                break
            cursor = resp.get("next_cursor")
            if not cursor:
                break
        return results

    async def bootstrap_databases(self, parent_page_id: str) -> dict[str, str]:
        """Create all 5 linked LifeOS Notion databases under parent_page_id."""
        goals_db_id, goals_ds_id = await self._create_database(
            parent_page_id, "LifeOS Global Goals", build_goals_db_properties()
        )
        projects_db_id, projects_ds_id = await self._create_database(
            parent_page_id,
            "LifeOS Projects",
            build_projects_db_properties(goals_ds_id),
        )
        tasks_db_id, tasks_ds_id = await self._create_database(
            parent_page_id,
            "LifeOS Tasks",
            build_tasks_db_properties(goals_ds_id, projects_ds_id),
        )

        client = self._require_client()
        parent_task_prop = {"Parent Task": build_parent_task_relation_property(tasks_ds_id)}
        if self._uses_data_sources():
            await client.data_sources.update(
                data_source_id=tasks_ds_id,
                properties=parent_task_prop,
            )
        else:
            await client.databases.update(
                database_id=tasks_db_id,
                properties=parent_task_prop,
            )

        telemetry_db_id, _ = await self._create_database(
            parent_page_id, "LifeOS Daily Telemetry", build_telemetry_db_properties()
        )
        insights_db_id, _ = await self._create_database(
            parent_page_id, "LifeOS Weekly Insights", build_insights_db_properties()
        )

        logger.info("Bootstrapped 5 Notion databases under parent page %s", parent_page_id)
        return {
            "goals": goals_db_id,
            "projects": projects_db_id,
            "tasks": tasks_db_id,
            "telemetry": telemetry_db_id,
            "insights": insights_db_id,
        }

    async def push_task(
        self,
        task: Task,
        *,
        goal_notion_id: str | None = None,
        project_notion_id: str | None = None,
    ) -> str:
        """Create or update a Task page in Notion and return its page ID."""
        status_key = (task.status or "active").lower()
        status_label = _TASK_STATUS_TO_NOTION.get(status_key, "Active")
        due_iso: str | None = None
        if task.due_at is not None:
            due_iso = task.due_at.isoformat()
        elif task.scheduled_date is not None:
            due_iso = task.scheduled_date.isoformat()

        confidence = float(task.confidence) if task.confidence is not None else 1.0
        effort = float(task.effort) if task.effort is not None else 6.0
        deferral_count = int(task.deferral_count) if task.deferral_count is not None else 0

        properties: dict[str, Any] = {
            "Name": _title_prop(task.title),
            "Status": {"select": {"name": status_label}},
            "Confidence": {"number": round(confidence, 4)},
            "Energy": {"select": {"name": task.energy_tier or "medium"}},
            "Effort": {"number": effort},
            "Deferral Count": {"number": deferral_count},
            "DoD": {"rich_text": _rich_text(task.dod)},
            "LifeOS ID": {"rich_text": _rich_text(task.id)},
        }
        if task.day_anchor:
            properties["Day Anchor"] = {"select": {"name": task.day_anchor}}
        if due_iso is not None:
            properties["Due"] = {"date": {"start": due_iso}}
        if task.source_link:
            properties["Source Link"] = {"url": task.source_link}
        if goal_notion_id:
            properties["Goal"] = {"relation": [{"id": goal_notion_id}]}
        if project_notion_id:
            properties["Project"] = {"relation": [{"id": project_notion_id}]}

        client = self._require_client()
        if task.notion_page_id:
            resp = await client.pages.update(page_id=task.notion_page_id, properties=properties)
            return str(resp.get("id", task.notion_page_id))

        if not self.settings.notion_tasks_db_id:
            raise ValueError("settings.notion_tasks_db_id is required to create a Notion task")
        return await self._create_page(self.settings.notion_tasks_db_id, properties)

    async def push_daily_telemetry(self, telemetry: DailyTelemetry) -> str:
        """Create or update a DailyTelemetry page in Notion and return its page ID."""
        day_iso = telemetry.day.isoformat()
        effort_planned = float(telemetry.effort_planned or 0.0)
        effort_done = float(telemetry.effort_done or 0.0)
        completion_rate = float(telemetry.completion_rate or 0.0)
        deferrals_count = int(telemetry.deferrals_count or 0)
        overload_index = float(telemetry.overload_index or 0.0)
        properties: dict[str, Any] = {
            "Day": _title_prop(day_iso),
            "Date": {"date": {"start": day_iso}},
            "Effort Planned": {"number": effort_planned},
            "Effort Done": {"number": effort_done},
            "Completion %": {"number": round(completion_rate, 4)},
            "Deferrals": {"number": deferrals_count},
            "Overload Index": {"number": round(overload_index, 4)},
        }
        if telemetry.sleep_hours is not None:
            properties["Sleep Hours"] = {"number": float(telemetry.sleep_hours)}
        if telemetry.active_energy_kcal is not None:
            properties["Activity"] = {"number": float(telemetry.active_energy_kcal)}

        client = self._require_client()
        if telemetry.notion_page_id:
            resp = await client.pages.update(
                page_id=telemetry.notion_page_id,
                properties=properties,
            )
            return str(resp.get("id", telemetry.notion_page_id))

        if not self.settings.notion_telemetry_db_id:
            raise ValueError(
                "settings.notion_telemetry_db_id is required to create Notion telemetry"
            )
        return await self._create_page(self.settings.notion_telemetry_db_id, properties)

    async def push_weekly_insight(self, insight: WeeklyInsight) -> str:
        """Create or update a WeeklyInsight page in Notion and return its page ID."""
        week_iso = insight.week_start.isoformat()
        patterns = insight.procrastination_patterns_json or []
        goal_progress = insight.goal_progress_json or {}
        stalling_text = "\n".join(f"- {p}" for p in patterns)
        goal_progress_text = (
            json.dumps(goal_progress, ensure_ascii=False)
            if goal_progress
            else ""
        )
        properties: dict[str, Any] = {
            "Week Start": _title_prop(week_iso),
            "Summary": {"rich_text": _rich_text(insight.summary_markdown)},
            "Stalling Patterns": {"rich_text": _rich_text(stalling_text)},
            "Goal Progress": {"rich_text": _rich_text(goal_progress_text)},
        }
        if insight.sleep_productivity_corr is not None:
            properties["Sleep-Productivity Correlation"] = {
                "number": round(float(insight.sleep_productivity_corr), 4)
            }

        client = self._require_client()
        if insight.notion_page_id:
            resp = await client.pages.update(
                page_id=insight.notion_page_id,
                properties=properties,
            )
            return str(resp.get("id", insight.notion_page_id))

        if not self.settings.notion_insights_db_id:
            raise ValueError(
                "settings.notion_insights_db_id is required to create Notion weekly insights"
            )
        return await self._create_page(self.settings.notion_insights_db_id, properties)

    async def pull_recent_changes(
        self,
        since: datetime | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """Query Goals and Tasks databases for recent edits and return normalized dicts."""
        filter_payload: dict[str, Any] | None = None
        if since is not None:
            filter_payload = {
                "timestamp": "last_edited_time",
                "last_edited_time": {"after": since.isoformat()},
            }

        goals: list[dict[str, Any]] = []
        if self.settings.notion_goals_db_id:
            raw_goals = await self._query_database(
                self.settings.notion_goals_db_id,
                filter_payload=filter_payload,
            )
            goals = [self._parse_goal_page(page) for page in raw_goals]

        tasks: list[dict[str, Any]] = []
        if self.settings.notion_tasks_db_id:
            raw_tasks = await self._query_database(
                self.settings.notion_tasks_db_id,
                filter_payload=filter_payload,
            )
            tasks = [self._parse_task_page(page) for page in raw_tasks]

        return {"goals": goals, "tasks": tasks}

    @staticmethod
    def _parse_goal_page(page: dict[str, Any]) -> dict[str, Any]:
        props = page.get("properties", {})
        priority_raw = _extract_number(props.get("Priority"))
        status_raw = _extract_select(props.get("Status"))
        return {
            "notion_page_id": str(page.get("id", "")),
            "title": _extract_plain_text((props.get("Name") or {}).get("title")),
            "horizon": _extract_select(props.get("Horizon")) or "quarter",
            "priority": int(priority_raw) if priority_raw is not None else 2,
            "progress_pct": _extract_number(props.get("Progress %")) or 0.0,
            "status": status_raw.lower() if status_raw else "active",
            "last_edited_time": page.get("last_edited_time"),
        }

    @staticmethod
    def _parse_task_page(page: dict[str, Any]) -> dict[str, Any]:
        props = page.get("properties", {})
        status_raw = _extract_select(props.get("Status"))
        deferral_raw = _extract_number(props.get("Deferral Count"))
        return {
            "notion_page_id": str(page.get("id", "")),
            "lifeos_id": _extract_plain_text((props.get("LifeOS ID") or {}).get("rich_text"))
            or None,
            "title": _extract_plain_text((props.get("Name") or {}).get("title")),
            "status": status_raw.lower() if status_raw else "active",
            "confidence": _extract_number(props.get("Confidence")),
            "energy_tier": _extract_select(props.get("Energy")),
            "effort": _extract_number(props.get("Effort")),
            "day_anchor": _extract_select(props.get("Day Anchor")),
            "due_at": _extract_date_start(props.get("Due")),
            "deferral_count": int(deferral_raw) if deferral_raw is not None else 0,
            "dod": _extract_plain_text((props.get("DoD") or {}).get("rich_text")),
            "source_link": (props.get("Source Link") or {}).get("url"),
            "goal_notion_id": _extract_relation_first_id(props.get("Goal")),
            "project_notion_id": _extract_relation_first_id(props.get("Project")),
            "parent_task_notion_id": _extract_relation_first_id(props.get("Parent Task")),
            "last_edited_time": page.get("last_edited_time"),
        }
