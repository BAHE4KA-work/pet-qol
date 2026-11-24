"""Goldilocks adaptive capacity controller for daily effort budgeting."""

from __future__ import annotations

from lifeos.config import Settings, get_settings
from lifeos.db.models import DailyTelemetry
from lifeos.schemas.planner import BudgetBreakdown, HealthSnapshot


def calculate_effort(duration_bucket: int, cognitive_load: int) -> float:
    """Compute task effort as duration_bucket multiplied by cognitive_load."""
    return float(duration_bucket * cognitive_load)


def compute_k_calendar(
    busy_hours: float,
    work_day_hours: float = 10.0,
    rest_buffer_hours: float = 1.5,
) -> float:
    """Compute calendar availability multiplier clamped to [0.30, 1.00]."""
    available = max(0.0, work_day_hours - busy_hours - rest_buffer_hours)
    denominator = max(1.0, work_day_hours - rest_buffer_hours)
    ratio = available / denominator
    return min(1.00, max(0.30, ratio))


def compute_k_health(health: HealthSnapshot | None) -> tuple[float, bool]:
    """Compute health multiplier and whether heavy deep tasks should be stripped."""
    if health is None:
        return 1.0, False

    low_sleep = health.sleep_hours is not None and health.sleep_hours < 6.0
    low_hrv = health.hrv_ms is not None and health.hrv_ms < 30.0
    if low_sleep or low_hrv:
        return 0.75, True

    if health.sleep_hours is not None and health.sleep_hours < 6.8:
        return 0.85, False

    if (
        health.sleep_hours is not None
        and health.sleep_hours >= 7.5
        and (health.hrv_ms is None or health.hrv_ms >= 55.0)
    ):
        return 1.10, False

    return 1.0, False


def _row_completion_rate(row: DailyTelemetry) -> float:
    if row.completion_rate == 0.0 and row.effort_done > 0.0 and row.effort_planned > 0.0:
        return row.effort_done / row.effort_planned
    return float(row.completion_rate)


def compute_k_velocity(
    recent_telemetry: list[DailyTelemetry],
    current_k: float = 1.0,
    low: float = 0.75,
    high: float = 0.85,
) -> tuple[float, float | None]:
    """Adjust velocity multiplier toward the 75-85% Goldilocks completion corridor."""
    ordered = sorted(recent_telemetry, key=lambda row: row.day)[-14:]
    valid = [row for row in ordered if row.effort_planned > 0]
    if not valid:
        return current_k, None

    rates = [_row_completion_rate(row) for row in valid]
    avg_rate = sum(rates) / len(rates)

    tail3 = ordered[-3:]
    deferrals_rising = (
        len(tail3) == 3
        and tail3[0].deferrals_count < tail3[1].deferrals_count < tail3[2].deferrals_count
        and tail3[2].deferrals_count >= 2
    )

    if avg_rate < 0.65 or deferrals_rising:
        new_k = current_k * 0.90
    elif len(rates) >= 3 and all(rate > 0.90 for rate in rates[-3:]):
        new_k = current_k * 1.10
    elif low <= avg_rate <= high:
        new_k = current_k
    elif avg_rate < low:
        new_k = current_k * 0.95
    else:
        new_k = current_k * 1.05

    clamped_k = min(1.35, max(0.65, new_k))
    return clamped_k, avg_rate


def compute_daily_budget(
    base_capacity: float,
    busy_hours: float,
    health: HealthSnapshot | None,
    recent_telemetry: list[DailyTelemetry],
    current_k_velocity: float = 1.0,
    settings: Settings | None = None,
) -> BudgetBreakdown:
    """Compute daily Goldilocks capacity budget breakdown."""
    cfg = settings or get_settings()
    k_calendar = compute_k_calendar(busy_hours)
    k_health, strip_deep_tasks = compute_k_health(health)
    k_velocity, rolling_rate = compute_k_velocity(
        recent_telemetry,
        current_k=current_k_velocity,
        low=cfg.goldilocks_low,
        high=cfg.goldilocks_high,
    )
    daily_budget = round(base_capacity * k_calendar * k_health * k_velocity, 2)
    return BudgetBreakdown(
        base_capacity=base_capacity,
        k_calendar=k_calendar,
        k_health=k_health,
        k_velocity=k_velocity,
        daily_budget=daily_budget,
        strip_deep_tasks=strip_deep_tasks,
        rolling_completion_rate=rolling_rate,
    )
