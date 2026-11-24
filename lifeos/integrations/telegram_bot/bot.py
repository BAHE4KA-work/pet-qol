"""Single-user Telegram bot with Bot API 10.4/10.5 HTML Widgets and fallback cards."""

from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message, TelegramObject
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lifeos.config import Settings
from lifeos.db.models import Task, WeeklyInsight
from lifeos.db.repositories import LifeOSRepository, compute_sha256
from lifeos.integrations.telegram_bot.templates import (
    render_today_fallback_message,
    render_today_widget_html,
    render_today_widget_state,
)
from lifeos.integrations.telegram_bot.widgets import (
    TelegramWidgetAPI,
    WidgetAPIError,
    WidgetPayloadError,
    WidgetQueryUpdate,
)
from lifeos.logging import get_logger
from lifeos.schemas.ai import ConfidenceTier, DayAnchor, EnergyTier, ItemType
from lifeos.schemas.planner import (
    BudgetBreakdown,
    CandidateItemDTO,
    DailyPlanView,
    TaskCardDTO,
)

logger = get_logger(__name__)

_WIDGET_STATE_KEY = "tg_widget_message_id"

EnqueueRawItemFn = Callable[[str], Awaitable[Any] | Any]


def is_authorized_user(user_id: int | None, allowed_user_id: int) -> bool:
    """Enforce fail-closed single-user ACL against `allowed_user_id`."""
    if allowed_user_id <= 0 or user_id is None:
        return False
    return user_id == allowed_user_id


class _SingleUserACLMiddleware(BaseMiddleware):
    """Reject messages and callbacks from any user other than `allowed_user_id`."""

    def __init__(self, allowed_user_id: int) -> None:
        self.allowed_user_id = allowed_user_id

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        from_user = getattr(event, "from_user", None)
        user_id = from_user.id if from_user is not None else None
        if not is_authorized_user(user_id, self.allowed_user_id):
            logger.warning("Rejected unauthorized Telegram event from user_id=%s", user_id)
            if isinstance(event, CallbackQuery):
                try:
                    await event.answer("Access denied", show_alert=True)
                except Exception:
                    pass
            return None
        return await handler(event, data)


def _to_item_type(value: str) -> ItemType:
    if value in ("event", "task", "goal_idea", "noise"):
        return value  # type: ignore[return-value]
    return "task"


def _to_energy_tier(value: str) -> EnergyTier:
    if value in ("deep", "medium", "low"):
        return value  # type: ignore[return-value]
    return "medium"


def _to_confidence_tier(value: str) -> ConfidenceTier:
    if value in ("high", "medium", "low"):
        return value  # type: ignore[return-value]
    return "high"


def _to_day_anchor(value: str | None) -> DayAnchor | None:
    if value in ("morning", "afternoon", "evening"):
        return value  # type: ignore[return-value]
    return None


def _parse_action_payload(raw_data: str) -> tuple[str, str] | None:
    text = raw_data.strip()
    if not text:
        return None
    if text.startswith("{"):
        try:
            obj = json.loads(text)
        except ValueError:
            return None
        if isinstance(obj, dict) and "a" in obj and "id" in obj:
            return str(obj["a"]), str(obj["id"])
        return None
    if ":" in text:
        action, item_id = text.split(":", 1)
        if action in ("t", "y", "n") and item_id:
            return action, item_id
    return None


def _extract_forward_origin(message: Message) -> str | None:
    origin = getattr(message, "forward_origin", None)
    if origin is not None:
        return type(origin).__name__
    fwd_from = getattr(message, "forward_from", None)
    if fwd_from is not None:
        return str(getattr(fwd_from, "full_name", None) or fwd_from.id)
    fwd_chat = getattr(message, "forward_from_chat", None)
    if fwd_chat is not None:
        return str(getattr(fwd_chat, "title", None) or fwd_chat.id)
    return None


class LifeOSTelegramBot:
    """Single-user Telegram bot supporting HTML widgets, inline fallback, and capture."""

    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        enqueue_raw_item_fn: EnqueueRawItemFn | None = None,
        widget_api: TelegramWidgetAPI | None = None,
        bot: Bot | None = None,
    ) -> None:
        """Initialize bot, router, ACL middleware, and widget API client."""
        self.settings = settings
        self.session_factory = session_factory
        self.enqueue_raw_item_fn = enqueue_raw_item_fn
        self.widget_api = widget_api or TelegramWidgetAPI(settings.telegram_bot_token)
        self.bot: Bot | None = (
            bot
            if bot is not None
            else (Bot(token=settings.telegram_bot_token) if settings.telegram_bot_token else None)
        )

        self.dp = Dispatcher()
        self.router = Router(name="lifeos_telegram")
        acl_mw = _SingleUserACLMiddleware(settings.telegram_allowed_user_id)
        self.router.message.middleware(acl_mw)
        self.router.callback_query.middleware(acl_mw)
        self._register_handlers()
        self.dp.include_router(self.router)

    def _today(self) -> date:
        try:
            tz = ZoneInfo(self.settings.timezone)
        except Exception:
            return date.today()
        return datetime.now(tz).date()

    def _register_handlers(self) -> None:
        self.router.message.register(self._on_start, Command("start"))
        self.router.message.register(self._on_today, Command("today"))
        self.router.message.register(self._on_week, Command("week"))
        self.router.callback_query.register(
            self._on_callback_query,
            F.data.startswith(("t:", "y:", "n:")),
        )
        self.router.message.register(self._on_direct_capture)

    async def _on_start(self, message: Message) -> None:
        user_id = message.from_user.id if message.from_user else None
        if not is_authorized_user(user_id, self.settings.telegram_allowed_user_id):
            return
        await message.answer(
            "LifeOS готов к работе.\n"
            "/today — план на сегодня\n"
            "/week — обзор недели\n"
            "Любой текст, пересланное сообщение, фото или голос (.ogg) сохранятся во входящие."
        )

    async def _on_today(self, message: Message) -> None:
        user_id = message.from_user.id if message.from_user else None
        if not is_authorized_user(user_id, self.settings.telegram_allowed_user_id):
            return
        plan = await self.build_today_plan()
        await self.send_or_update_today_widget(message.chat.id, plan)

    async def _on_week(self, message: Message) -> None:
        user_id = message.from_user.id if message.from_user else None
        if not is_authorized_user(user_id, self.settings.telegram_allowed_user_id):
            return
        today = self._today()
        week_end = today + timedelta(days=6)
        async with self.session_factory() as session:
            tasks_res = await session.execute(
                select(Task)
                .where(
                    Task.scheduled_date.is_not(None),
                    Task.scheduled_date >= today,
                    Task.scheduled_date <= week_end,
                    Task.status.in_(["active", "done"]),
                )
                .order_by(Task.scheduled_date.asc())
            )
            tasks = list(tasks_res.scalars().all())
            insight_res = await session.execute(
                select(WeeklyInsight).order_by(WeeklyInsight.week_start.desc()).limit(1)
            )
            insight = insight_res.scalar_one_or_none()

        lines = [f"Неделя {today.isoformat()} — {week_end.isoformat()}: задач {len(tasks)}"]
        for task in tasks[:15]:
            mark = "✅" if task.status == "done" else "⬜"
            day_str = task.scheduled_date.isoformat() if task.scheduled_date else "?"
            lines.append(f"{mark} {day_str}: {task.title}")
        if insight is not None:
            lines.append("")
            lines.append(insight.summary_markdown[:600])
        await message.answer("\n".join(lines))

    async def _on_callback_query(self, callback: CallbackQuery) -> None:
        user_id = callback.from_user.id if callback.from_user else None
        if not is_authorized_user(user_id, self.settings.telegram_allowed_user_id):
            await callback.answer("Access denied", show_alert=True)
            return
        data = callback.data or ""
        parsed = _parse_action_payload(data)
        if parsed is None:
            await callback.answer("Неизвестное действие")
            return

        action, item_id = parsed
        status_msg = await self._apply_action(action, item_id)
        try:
            await callback.answer(status_msg)
        except TelegramAPIError:
            pass

        chat_id = callback.message.chat.id if callback.message else user_id
        if chat_id is not None:
            plan = await self.build_today_plan()
            await self.send_or_update_today_widget(chat_id, plan)

    async def _on_direct_capture(self, message: Message) -> None:
        user_id = message.from_user.id if message.from_user else None
        if not is_authorized_user(user_id, self.settings.telegram_allowed_user_id):
            return

        modality = "text"
        media_mime_type: str | None = None
        media_uri: str | None = None
        raw_text: str | None = message.text or message.caption

        if message.voice is not None:
            modality = "voice"
            media_mime_type = message.voice.mime_type or "audio/ogg"
            media_uri = f"telegram://file/{message.voice.file_id}"
        elif message.photo:
            modality = "photo"
            media_mime_type = "image/jpeg"
            media_uri = f"telegram://file/{message.photo[-1].file_id}"

        forward_origin = _extract_forward_origin(message)
        if forward_origin:
            prefix = f"[Forwarded from {forward_origin}] "
            raw_text = f"{prefix}{raw_text}" if raw_text else prefix.strip()

        if not raw_text and not media_uri:
            return

        source_ref = f"tg_msg:{message.chat.id}:{message.message_id}"
        hash_seed = f"{modality}|{raw_text or ''}|{media_uri or ''}|{source_ref}"
        content_hash = compute_sha256(hash_seed)

        metadata_json: dict[str, Any] = {
            "confidence": 1.0,
            "priority": "high",
            "chat_id": message.chat.id,
            "message_id": message.message_id,
            "user_id": user_id,
        }
        if forward_origin:
            metadata_json["forwarded_from"] = forward_origin

        async with self.session_factory() as session:
            repo = LifeOSRepository(session)
            raw_item, created = await repo.create_raw_item_if_new(
                source="telegram_bot",
                source_ref=source_ref,
                content_hash=content_hash,
                modality=modality,
                raw_text=raw_text,
                media_mime_type=media_mime_type,
                media_uri=media_uri,
                metadata_json=metadata_json,
            )
            await session.commit()

        if created and self.enqueue_raw_item_fn is not None:
            maybe_coro = self.enqueue_raw_item_fn(raw_item.id)
            if inspect.isawaitable(maybe_coro):
                await maybe_coro

        await message.answer("Принято в LifeOS.")

    async def _apply_action(self, action: str, item_id: str) -> str:
        today = self._today()
        async with self.session_factory() as session:
            repo = LifeOSRepository(session)
            if action == "t":
                task = await repo.toggle_task_done(item_id)
                await session.commit()
                if task is None:
                    return "Задача не найдена"
                return "Выполнено" if task.status == "done" else "Возвращено в план"

            task = await repo.get_task(item_id)
            if task is None:
                return "Элемент не найден"

            if action == "y":
                task.status = "active"
                task.confidence = 1.0
                task.confidence_tier = "high"
                if task.scheduled_date is None:
                    task.scheduled_date = today
                await session.commit()
                return "Добавлено в план"

            if action == "n":
                task.status = "dismissed"
                await session.commit()
                return "Отклонено"

        return "Неизвестное действие"

    async def build_today_plan(self, day: date | None = None) -> DailyPlanView:
        """Assemble Today `DailyPlanView` from DB tasks, candidates, and telemetry."""
        target_day = day or self._today()
        async with self.session_factory() as session:
            repo = LifeOSRepository(session)
            tasks = await repo.list_tasks_for_date(target_day)
            candidates = await repo.list_candidates()
            telemetry = await repo.get_or_create_telemetry(target_day)
            await session.commit()

        morning: list[TaskCardDTO] = []
        afternoon: list[TaskCardDTO] = []
        evening: list[TaskCardDTO] = []
        effort_planned = 0.0
        effort_done = 0.0

        for task in tasks:
            effort_planned += float(task.effort)
            if task.status == "done":
                effort_done += float(task.effort)
            anchor = _to_day_anchor(task.day_anchor)
            card = TaskCardDTO(
                id=task.id,
                title=task.title,
                dod=task.dod,
                item_type=_to_item_type(task.item_type),
                status=task.status,
                confidence=float(task.confidence),
                confidence_tier=_to_confidence_tier(task.confidence_tier),
                energy_tier=_to_energy_tier(task.energy_tier),
                effort=float(task.effort),
                day_anchor=anchor,
                scheduled_date=task.scheduled_date,
                due_at=task.due_at,
                has_exact_time=task.has_exact_time,
                deferral_count=task.deferral_count,
            )
            if anchor == "afternoon":
                afternoon.append(card)
            elif anchor == "evening":
                evening.append(card)
            else:
                morning.append(card)

        candidate_dtos = [
            CandidateItemDTO(
                id=cand.id,
                title=cand.title,
                dod=cand.dod,
                confidence=float(cand.confidence),
                energy_tier=_to_energy_tier(cand.energy_tier),
                effort=float(cand.effort),
                source_link=cand.source_link,
            )
            for cand in candidates
        ]

        daily_budget = float(telemetry.daily_budget or self.settings.base_daily_capacity)
        budget = BudgetBreakdown(
            base_capacity=self.settings.base_daily_capacity,
            k_calendar=float(telemetry.k_calendar),
            k_health=float(telemetry.k_health),
            k_velocity=float(telemetry.k_velocity),
            daily_budget=daily_budget,
            strip_deep_tasks=float(telemetry.k_health) < 0.8,
        )
        return DailyPlanView(
            day=target_day,
            budget=budget,
            effort_planned=effort_planned,
            effort_done=effort_done,
            morning=morning,
            afternoon=afternoon,
            evening=evening,
            candidates=candidate_dtos,
        )

    async def send_or_update_today_widget(
        self,
        chat_id: int,
        plan: DailyPlanView,
    ) -> int | None:
        """Send or update Today HTML widget, falling back to inline card on any error."""
        async with self.session_factory() as session:
            repo = LifeOSRepository(session)
            state = await repo.get_sync_state(_WIDGET_STATE_KEY) or {}

        existing_msg_id = state.get("message_id")
        same_target = (
            state.get("chat_id") == chat_id
            and state.get("day") == plan.day.isoformat()
            and isinstance(existing_msg_id, int)
        )

        if self.settings.telegram_widget_enabled:
            try:
                html = render_today_widget_html(plan)
                chat_data = render_today_widget_state(plan)
                if same_target and state.get("mode") == "widget":
                    await self.widget_api.edit_message_widget_data(
                        chat_id=chat_id,
                        message_id=int(existing_msg_id),
                        chat_data=chat_data,
                    )
                    return int(existing_msg_id)

                result = await self.widget_api.send_widget(
                    chat_id=chat_id,
                    html=html,
                    chat_data=chat_data,
                )
                msg_id = int(result["message_id"])
                await self._save_widget_state(chat_id, msg_id, "widget", plan.day)
                return msg_id
            except (WidgetPayloadError, WidgetAPIError) as exc:
                logger.warning("Widget send/update failed, falling back to inline card: %s", exc)

        if self.bot is None:
            return None

        text, markup = render_today_fallback_message(plan)
        if same_target and state.get("mode") == "fallback":
            try:
                await self.bot.edit_message_text(
                    text=text,
                    chat_id=chat_id,
                    message_id=int(existing_msg_id),
                    reply_markup=markup,
                    parse_mode="HTML",
                )
                return int(existing_msg_id)
            except TelegramAPIError as exc:
                logger.debug("Fallback edit failed, sending fresh message: %s", exc)

        msg = await self.bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=markup,
            parse_mode="HTML",
        )
        await self._save_widget_state(chat_id, msg.message_id, "fallback", plan.day)
        return msg.message_id

    async def _save_widget_state(
        self,
        chat_id: int,
        message_id: int,
        mode: str,
        day: date,
    ) -> None:
        async with self.session_factory() as session:
            repo = LifeOSRepository(session)
            await repo.set_sync_state(
                _WIDGET_STATE_KEY,
                {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "mode": mode,
                    "day": day.isoformat(),
                },
            )
            await session.commit()

    async def handle_widget_query(self, query: WidgetQueryUpdate) -> bool:
        """Process a 1-tap `widget_query` update with strict ACL and widget refresh."""
        if not is_authorized_user(query.from_user_id, self.settings.telegram_allowed_user_id):
            logger.warning(
                "Unauthorized widget_query rejected from user_id=%s",
                query.from_user_id,
            )
            return False

        parsed = _parse_action_payload(query.data)
        if parsed is None:
            try:
                await self.widget_api.answer_widget_query(query.id, data="invalid")
            except WidgetAPIError as exc:
                logger.warning("answerWidgetQuery failed: %s", exc)
            return False

        action, item_id = parsed
        status_msg = await self._apply_action(action, item_id)

        try:
            await self.widget_api.answer_widget_query(query.id, data=status_msg)
        except WidgetAPIError as exc:
            logger.warning("answerWidgetQuery failed: %s", exc)

        target_chat_id = query.chat_id or self.settings.telegram_allowed_user_id
        if target_chat_id > 0:
            plan = await self.build_today_plan()
            await self.send_or_update_today_widget(target_chat_id, plan)

        return True
