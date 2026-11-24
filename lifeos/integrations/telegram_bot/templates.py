"""HTML Widget and InlineKeyboard fallback templates for Telegram Today view."""

from __future__ import annotations

import html
import json

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from lifeos.integrations.telegram_bot.widgets import validate_widget_payload
from lifeos.schemas.planner import CandidateItemDTO, DailyPlanView, TaskCardDTO

_ENERGY_BADGE: dict[str, str] = {
    "deep": "D",
    "medium": "M",
    "low": "L",
}

_WIDGET_CSS = (
    "body{margin:0;padding:10px;font:13px/1.35 system-ui,sans-serif;"
    "background:#11161d;color:#eef2f7}"
    ".hd{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px}"
    ".dt{font-weight:700;font-size:14px}"
    ".st{font-size:11px;color:#9ab}"
    ".bar{height:6px;background:#243040;border-radius:4px;overflow:hidden;margin:4px 0 8px}"
    ".fl{height:100%;background:#4cc38a}"
    ".sc{margin:8px 0 4px;font-size:11px;font-weight:700;color:#8fb3d9;text-transform:uppercase}"
    ".rw{display:flex;align-items:center;gap:6px;padding:5px 0;border-bottom:1px solid #1e2835}"
    ".dn .tt{text-decoration:line-through;color:#789}"
    ".tt{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}"
    ".bg{font-size:10px;padding:1px 5px;border-radius:8px;background:#1f2c3d;color:#bcd}"
    ".bt{border:0;border-radius:6px;padding:4px 8px;font-size:11px;cursor:pointer;"
    "background:#253548;color:#eef}"
    ".ok{background:#1f5e42}.no{background:#5c2830}"
    "details{margin-top:8px;background:#161e28;border-radius:6px;padding:6px 8px}"
    "summary{cursor:pointer;font-size:12px;font-weight:600;color:#f0c674}"
)

_WIDGET_JS = (
    "<script>"
    "function q(a,i){var p=JSON.stringify({a:a,id:i});"
    "if(window.Telegram&&Telegram.Widget&&Telegram.Widget.sendQuery)"
    "{Telegram.Widget.sendQuery(p);}"
    "else if(window.TelegramWidget&&TelegramWidget.request)"
    "{TelegramWidget.request(p);}}"
    "document.addEventListener('click',function(e){"
    "var b=e.target.closest('button[data-a]');if(!b)return;"
    "q(b.getAttribute('data-a'),b.getAttribute('data-id'));});"
    "if(window.Telegram&&Telegram.Widget&&Telegram.Widget.onData){"
    "Telegram.Widget.onData(function(d){try{var s=typeof d==='string'?JSON.parse(d):d;"
    "if(s&&s.pct!==undefined){var f=document.getElementById('fl');"
    "if(f)f.style.width=s.pct+'%';}"
    "if(s&&s.txt){var t=document.getElementById('rs');if(t)t.textContent=s.txt;}"
    "}catch(_){}});}"
    "</script>"
)


def _esc(value: str) -> str:
    return html.escape(value, quote=True)


def _pct(done: float, budget: float) -> int:
    if budget <= 0:
        return 0
    return max(0, min(100, round((done / budget) * 100)))


def _format_task_row(task: TaskCardDTO) -> str:
    done_cls = " dn" if task.status == "done" else ""
    mark = "&#x2705;" if task.status == "done" else "&#x2B1C;"
    tier = _ENERGY_BADGE.get(task.energy_tier, "M")
    time_tag = ""
    if task.has_exact_time and task.due_at is not None:
        time_tag = f"{task.due_at.strftime('%H:%M')} "
    title = _esc(f"{time_tag}{task.title}")
    tid = _esc(task.id)
    return (
        f'<div class="rw{done_cls}">'
        f'<button class="bt" data-a="t" data-id="{tid}">{mark}</button>'
        f'<span class="tt">{title}</span>'
        f'<span class="bg">{tier}&middot;{task.effort:g}</span>'
        "</div>"
    )


def _format_candidate_row(cand: CandidateItemDTO) -> str:
    cid = _esc(cand.id)
    title = _esc(cand.title)
    pct = round(cand.confidence * 100)
    return (
        '<div class="rw">'
        f'<span class="tt">{title} ({pct}%)</span>'
        f'<button class="bt ok" data-a="y" data-id="{cid}">В план</button>'
        f'<button class="bt no" data-a="n" data-id="{cid}">Не надо</button>'
        "</div>"
    )


def render_today_widget_state(plan: DailyPlanView) -> str:
    """Serialize compact Today widget state (<= 1024 bytes) for `editMessageWidgetData`."""
    pct = _pct(plan.effort_done, plan.budget.daily_budget)
    done_ids = [
        task.id
        for task in (*plan.morning, *plan.afternoon, *plan.evening)
        if task.status == "done"
    ]
    payload: dict[str, object] = {
        "d": plan.day.isoformat(),
        "pct": pct,
        "txt": f"{plan.effort_done:g}/{plan.budget.daily_budget:g}",
        "done": done_ids,
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(raw.encode("utf-8")) > 1000:
        payload.pop("done", None)
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return raw


def render_today_widget_html(plan: DailyPlanView) -> str:
    """Render compact, validated Bot API 10.4/10.5 HTML widget for Today view."""
    pct = _pct(plan.effort_done, plan.budget.daily_budget)
    deep_flag = " | Light" if plan.budget.strip_deep_tasks else ""
    cal_count = len(plan.calendar_events)
    status_line = (
        f"H:{plan.budget.k_health:.2f} C:{plan.budget.k_calendar:.2f}"
        f" | Cal:{cal_count}{deep_flag}"
    )

    sections: list[str] = []
    for label, tasks in (
        ("Утро", plan.morning),
        ("День", plan.afternoon),
        ("Вечер", plan.evening),
    ):
        rows = "".join(_format_task_row(task) for task in tasks) if tasks else (
            '<div class="st">—</div>'
        )
        sections.append(f'<div class="sc">{label}</div>{rows}')

    candidates_block = ""
    if plan.candidates:
        cand_rows = "".join(_format_candidate_row(cand) for cand in plan.candidates)
        candidates_block = (
            f"<details><summary>Вероятные дела ({len(plan.candidates)})</summary>"
            f"{cand_rows}</details>"
        )

    rendered = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<style>{_WIDGET_CSS}</style></head><body>"
        '<div class="hd">'
        f'<span class="dt">{_esc(plan.day.isoformat())}</span>'
        f'<span class="st" id="rs">{plan.effort_done:g}/{plan.budget.daily_budget:g}</span>'
        "</div>"
        f'<div class="st">{_esc(status_line)}</div>'
        f'<div class="bar"><div class="fl" id="fl" style="width:{pct}%"></div></div>'
        f"{''.join(sections)}{candidates_block}{_WIDGET_JS}</body></html>"
    )
    validate_widget_payload(rendered)
    return rendered


def render_today_fallback_message(plan: DailyPlanView) -> tuple[str, InlineKeyboardMarkup]:
    """Render HTML summary message and InlineKeyboardMarkup fallback for Today view."""
    pct = _pct(plan.effort_done, plan.budget.daily_budget)
    lines: list[str] = [
        f"<b>План на {_esc(plan.day.isoformat())}</b>",
        (
            f"Ресурс: <b>{plan.effort_done:g} / {plan.budget.daily_budget:g}</b> ({pct}%) | "
            f"Health x{plan.budget.k_health:.2f} | Cal x{plan.budget.k_calendar:.2f}"
        ),
    ]
    if plan.budget.strip_deep_tasks:
        lines.append("<i>Режим восстановления: глубокие задачи сняты.</i>")
    if plan.calendar_events:
        lines.append(f"Календарь: {len(plan.calendar_events)} событий")

    buttons: list[list[InlineKeyboardButton]] = []
    for label, tasks in (
        ("Утро", plan.morning),
        ("День", plan.afternoon),
        ("Вечер", plan.evening),
    ):
        lines.append(f"\n<b>{label}</b>")
        if not tasks:
            lines.append("• —")
            continue
        for task in tasks:
            icon = "✅" if task.status == "done" else "⬜"
            time_prefix = (
                f"{task.due_at.strftime('%H:%M')} "
                if task.has_exact_time and task.due_at is not None
                else ""
            )
            lines.append(
                f"{icon} {_esc(time_prefix + task.title)} "
                f"<i>({task.energy_tier}, {task.effort:g})</i>"
            )
            short_title = task.title[:28] + ("…" if len(task.title) > 28 else "")
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=f"{icon} {short_title}",
                        callback_data=f"t:{task.id}",
                    )
                ]
            )

    if plan.candidates:
        lines.append(f"\n<b>Вероятные дела ({len(plan.candidates)})</b>")
        for cand in plan.candidates:
            pct_conf = round(cand.confidence * 100)
            lines.append(f"• {_esc(cand.title)} ({pct_conf}%)")
            short_cand = cand.title[:18] + ("…" if len(cand.title) > 18 else "")
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=f"В план: {short_cand}",
                        callback_data=f"y:{cand.id}",
                    ),
                    InlineKeyboardButton(
                        text=f"Не надо: {short_cand}",
                        callback_data=f"n:{cand.id}",
                    ),
                ]
            )

    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)
