"""Notion database property schema definitions for the 5 linked LifeOS databases."""

from __future__ import annotations

from typing import Any


def _select_options(*names: str) -> dict[str, Any]:
    return {"select": {"options": [{"name": name} for name in names]}}


def _relation_prop(target_data_source_id: str) -> dict[str, Any]:
    return {
        "relation": {
            "data_source_id": target_data_source_id,
            "type": "single_property",
            "single_property": {},
        }
    }


def build_goals_db_properties() -> dict[str, Any]:
    """Build Notion property schema for the Global Goals database."""
    return {
        "Name": {"title": {}},
        "Horizon": _select_options("week", "month", "quarter", "year"),
        "Priority": {"number": {"format": "number"}},
        "Progress %": {"number": {"format": "percent"}},
        "Status": _select_options("active", "paused", "completed", "archived"),
    }


def build_projects_db_properties(goals_db_id: str) -> dict[str, Any]:
    """Build Notion property schema for the Projects database linked to Goals."""
    return {
        "Name": {"title": {}},
        "Goal": _relation_prop(goals_db_id),
        "Status": _select_options("active", "paused", "completed", "archived"),
        "Milestone": {"rich_text": {}},
    }


def build_parent_task_relation_property(tasks_db_id: str) -> dict[str, Any]:
    """Build Notion self-relation property schema for Parent Task on the Tasks database."""
    return _relation_prop(tasks_db_id)


def build_tasks_db_properties(
    goals_db_id: str,
    projects_db_id: str,
    *,
    tasks_db_id: str | None = None,
) -> dict[str, Any]:
    """Build Notion property schema for the Tasks database linked to Goals and Projects."""
    props: dict[str, Any] = {
        "Name": {"title": {}},
        "Goal": _relation_prop(goals_db_id),
        "Project": _relation_prop(projects_db_id),
        "Status": _select_options("Active", "Candidate", "Someday", "Done", "Dismissed"),
        "Confidence": {"number": {"format": "number"}},
        "Energy": _select_options("deep", "medium", "low"),
        "Effort": {"number": {"format": "number"}},
        "Day Anchor": _select_options("morning", "afternoon", "evening"),
        "Due": {"date": {}},
        "Deferral Count": {"number": {"format": "number"}},
        "DoD": {"rich_text": {}},
        "Source Link": {"url": {}},
        "LifeOS ID": {"rich_text": {}},
    }
    if tasks_db_id is not None:
        props["Parent Task"] = build_parent_task_relation_property(tasks_db_id)
    return props


def build_telemetry_db_properties() -> dict[str, Any]:
    """Build Notion property schema for the Daily Telemetry database."""
    return {
        "Day": {"title": {}},
        "Date": {"date": {}},
        "Sleep Hours": {"number": {"format": "number"}},
        "Activity": {"number": {"format": "number"}},
        "Effort Planned": {"number": {"format": "number"}},
        "Effort Done": {"number": {"format": "number"}},
        "Completion %": {"number": {"format": "percent"}},
        "Deferrals": {"number": {"format": "number"}},
        "Overload Index": {"number": {"format": "number"}},
    }


def build_insights_db_properties() -> dict[str, Any]:
    """Build Notion property schema for the Weekly Insights database."""
    return {
        "Week Start": {"title": {}},
        "Summary": {"rich_text": {}},
        "Sleep-Productivity Correlation": {"number": {"format": "number"}},
        "Stalling Patterns": {"rich_text": {}},
        "Goal Progress": {"rich_text": {}},
    }
