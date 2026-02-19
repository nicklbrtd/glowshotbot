from __future__ import annotations

from datetime import datetime, timedelta
from html import escape
import re

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import (
    CREDIT_SHOWS_BASE,
    CREDIT_SHOWS_HAPPY,
    HAPPY_HOUR_END,
    HAPPY_HOUR_START,
    MIN_VOTES_FOR_NORMAL_FEED,
    TAIL_PROBABILITY,
)
from database import (
    admin_reset_archives_only,
    admin_reset_results_and_archives,
    admin_reset_results_only,
    apply_access_preset,
    get_effective_ads_settings,
    get_effective_economy_settings,
    get_effective_protection_settings,
    get_results_reset_preview_counts,
    get_section_access_state,
    get_tech_mode_state,
    get_update_mode_state,
    set_setting,
    set_settings_bulk,
    set_tech_mode_state,
    set_update_mode_state,
    toggle_section_blocked,
)
from utils.time import get_moscow_now
from utils.update_guard import UPDATE_DEFAULT_TEXT

from .common import _ensure_admin, edit_or_answer


router = Router(name="admin_settings")

SETTINGS_PREFIX = "admin_settings"
SETTINGS_TAB_KEY = "admin_settings_tab"
DEFAULT_TAB = "modes"

TAB_TITLES: dict[str, str] = {
    "modes": "📟 Режимы",
    "access": "🧱 Доступ",
    "economy": "💳 Экономика",
    "ads": "📢 Реклама",
    "protection": "🛡 Защита",
    "reset": "🧹 Сброс",
}
TAB_ORDER = ["modes", "access", "economy", "ads", "protection", "reset"]

DEFAULT_SECTION_BLOCKED_TEXT = (
    "Пока что вход в этот раздел запрещен. Возможно ведутся улучшения или исправления багов. "
    "Подождите пожалуйста!"
)

ALLOWED_TECH_DELAY_MINUTES = {0, 5, 15, 30, 60}


class AdminSettingsInput(StatesGroup):
    waiting_tech_notice_text = State()
    waiting_update_notice_text = State()
    waiting_number_value = State()


NUMERIC_EDIT_FIELDS: dict[str, dict] = {
    "eco_daily_normal": {
        "tab": "economy",
        "db_key": "economy.daily_free_credits_normal",
        "value_key": "daily_free_credits_normal",
        "label": "daily_free_credits_normal",
        "kind": "int",
        "min": 0,
        "max": 100,
    },
    "eco_daily_premium": {
        "tab": "economy",
        "db_key": "economy.daily_free_credits_premium",
        "value_key": "daily_free_credits_premium",
        "label": "daily_free_credits_premium",
        "kind": "int",
        "min": 0,
        "max": 100,
    },
    "eco_daily_author": {
        "tab": "economy",
        "db_key": "economy.daily_free_credits_author",
        "value_key": "daily_free_credits_author",
        "label": "daily_free_credits_author",
        "kind": "int",
        "min": 0,
        "max": 100,
    },
    "eco_publish_bonus": {
        "tab": "economy",
        "db_key": "economy.publish_bonus_credits",
        "value_key": "publish_bonus_credits",
        "label": "publish_bonus_credits",
        "kind": "int",
        "min": 0,
        "max": 100,
    },
    "eco_credit_normal": {
        "tab": "economy",
        "db_key": "economy.credit_to_shows_normal",
        "value_key": "credit_to_shows_normal",
        "label": "credit_to_shows_normal",
        "kind": "int",
        "min": 1,
        "max": 100,
    },
    "eco_credit_hh": {
        "tab": "economy",
        "db_key": "economy.credit_to_shows_happyhour",
        "value_key": "credit_to_shows_happyhour",
        "label": "credit_to_shows_happyhour",
        "kind": "int",
        "min": 1,
        "max": 100,
    },
    "eco_hh_start": {
        "tab": "economy",
        "db_key": "economy.happy_hour_start_hour",
        "value_key": "happy_hour_start_hour",
        "label": "happy_hour_start_hour",
        "kind": "int",
        "min": 0,
        "max": 23,
    },
    "eco_hh_duration": {
        "tab": "economy",
        "db_key": "economy.happy_hour_duration_minutes",
        "value_key": "happy_hour_duration_minutes",
        "label": "happy_hour_duration_minutes",
        "kind": "int",
        "min": 1,
        "max": 1440,
    },
    "eco_tail": {
        "tab": "economy",
        "db_key": "economy.tail_probability",
        "value_key": "tail_probability",
        "label": "tail_probability",
        "kind": "float",
        "min": 0.0,
        "max": 1.0,
    },
    "eco_votes": {
        "tab": "economy",
        "db_key": "economy.min_votes_for_normal_feed",
        "value_key": "min_votes_for_normal_feed",
        "label": "min_votes_for_normal_feed",
        "kind": "int",
        "min": 0,
        "max": 500,
    },
    "eco_max_normal": {
        "tab": "economy",
        "db_key": "economy.max_active_photos_normal",
        "value_key": "max_active_photos_normal",
        "label": "max_active_photos_normal",
        "kind": "int",
        "min": 1,
        "max": 20,
    },
    "eco_max_author": {
        "tab": "economy",
        "db_key": "economy.max_active_photos_author",
        "value_key": "max_active_photos_author",
        "label": "max_active_photos_author",
        "kind": "int",
        "min": 1,
        "max": 20,
    },
    "eco_max_premium": {
        "tab": "economy",
        "db_key": "economy.max_active_photos_premium",
        "value_key": "max_active_photos_premium",
        "label": "max_active_photos_premium",
        "kind": "int",
        "min": 1,
        "max": 20,
    },
    "ads_frequency": {
        "tab": "ads",
        "db_key": "ads.frequency_n",
        "value_key": "frequency_n",
        "label": "ads_frequency_n",
        "kind": "int",
        "min": 1,
        "max": 1000,
    },
    "prot_callbacks": {
        "tab": "protection",
        "db_key": "protection.max_callbacks_per_minute_per_user",
        "value_key": "max_callbacks_per_minute_per_user",
        "label": "max_callbacks_per_minute_per_user",
        "kind": "int",
        "min": 10,
        "max": 2000,
    },
    "prot_actions": {
        "tab": "protection",
        "db_key": "protection.max_actions_per_10s",
        "value_key": "max_actions_per_10s",
        "label": "max_actions_per_10s",
        "kind": "int",
        "min": 1,
        "max": 500,
    },
    "prot_cooldown": {
        "tab": "protection",
        "db_key": "protection.cooldown_on_spike_seconds",
        "value_key": "cooldown_on_spike_seconds",
        "label": "cooldown_on_spike_seconds",
        "kind": "int",
        "min": 0,
        "max": 300,
    },
    "prot_threshold": {
        "tab": "protection",
        "db_key": "protection.spam_suspect_threshold",
        "value_key": "spam_suspect_threshold",
        "label": "spam_suspect_threshold",
        "kind": "int",
        "min": 1,
        "max": 5000,
    },
}


def _settings_header() -> str:
    now_hhmm = get_moscow_now().strftime("%H:%M")
    return (
        "⚙️ <b>Настройки</b> · <i>админ панель</i>\n"
        f"🕒 Сейчас: <b>{now_hhmm} МСК</b>"
    )


def _tab_button_name(tab: str, current_tab: str) -> str:
    title = TAB_TITLES.get(tab, tab)
    return f"• {title}" if tab == current_tab else title


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _new_tab_keyboard(current_tab: str) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.row(*[_btn(_tab_button_name(t, current_tab), f"admin:settings:tab:{t}") for t in TAB_ORDER[:3]])
    kb.row(*[_btn(_tab_button_name(t, current_tab), f"admin:settings:tab:{t}") for t in TAB_ORDER[3:]])
    kb.row(_btn("⬅️ В админ-меню", "admin:menu"))
    return kb


def _parse_dt(raw: object) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw))
    except Exception:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=get_moscow_now().tzinfo)
    return dt


def _tech_countdown_minutes(state: dict) -> int | None:
    if not bool(state.get("tech_enabled")):
        return None
    dt = _parse_dt(state.get("tech_start_at"))
    if dt is None:
        return None
    now = get_moscow_now()
    if now >= dt:
        return 0
    sec = max(0, int((dt - now).total_seconds()))
    return max(1, (sec + 59) // 60)


def _format_tech_status(state: dict) -> tuple[str, str]:
    if not bool(state.get("tech_enabled")):
        return "выключен", ""
    dt = _parse_dt(state.get("tech_start_at"))
    now = get_moscow_now()
    if dt is not None and now < dt:
        left = _tech_countdown_minutes(state) or 0
        return "запланирован", f"До старта: <b>{left} мин</b>"
    if dt is not None:
        return "включен", f"Старт: <b>{dt.astimezone(get_moscow_now().tzinfo).strftime('%H:%M')}</b>"
    return "включен", ""


def _format_bool(value: object) -> str:
    return "вкл" if bool(value) else "выкл"


def _format_num(value: object, kind: str) -> str:
    if kind == "float":
        try:
            return f"{float(value):.2f}"
        except Exception:
            return "0.00"
    try:
        return str(int(value))
    except Exception:
        return "0"


def _clip_text(value: object, limit: int = 200) -> str:
    text = str(value or "").strip()
    if not text:
        return "—"
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _safe_notice_text(raw: str) -> str:
    text = str(raw or "").strip()
    if len(text) > 1500:
        text = text[:1500]
    return escape(text, quote=False)


def _happy_hour_default_duration_minutes() -> int:
    start_total = int(HAPPY_HOUR_START.hour) * 60 + int(HAPPY_HOUR_START.minute)
    end_total = int(HAPPY_HOUR_END.hour) * 60 + int(HAPPY_HOUR_END.minute)
    diff = end_total - start_total
    if diff <= 0:
        diff += 24 * 60
    return max(1, diff)


def _economy_defaults_payload() -> dict[str, object]:
    values = {
        "daily_free_credits_normal": 2,
        "daily_free_credits_premium": 3,
        "daily_free_credits_author": 3,
        "publish_bonus_credits": 2,
        "credit_to_shows_normal": int(CREDIT_SHOWS_BASE),
        "credit_to_shows_happyhour": int(CREDIT_SHOWS_HAPPY),
        "happy_hour_enabled": True,
        "happy_hour_start_hour": int(HAPPY_HOUR_START.hour),
        "happy_hour_duration_minutes": _happy_hour_default_duration_minutes(),
        "tail_probability": float(TAIL_PROBABILITY),
        "min_votes_for_normal_feed": int(MIN_VOTES_FOR_NORMAL_FEED),
        "max_active_photos_normal": 2,
        "max_active_photos_author": 2,
        "max_active_photos_premium": 2,
    }
    return {f"economy.{k}": v for k, v in values.items()}


def _detect_access_preset(access_state: dict) -> str:
    combo = (
        bool(access_state.get("upload_blocked")),
        bool(access_state.get("rating_blocked")),
        bool(access_state.get("results_blocked")),
        bool(access_state.get("profile_blocked")),
    )
    mapping = {
        (False, False, False, False): "NORMAL",
        (True, False, False, False): "UPLOAD OFF",
        (False, True, False, False): "RATE OFF",
        (True, True, False, False): "READ-ONLY",
        (True, True, True, True): "FULL LOCK",
    }
    return mapping.get(combo, "CUSTOM")


def render_modes_text(tech_state: dict, update_state: dict, *, preview_update: bool = False, notice: str | None = None) -> str:
    tech_status, tech_extra = _format_tech_status(tech_state)
    tech_notice = tech_state.get("tech_notice_text") or DEFAULT_SECTION_BLOCKED_TEXT
    update_text = update_state.get("update_notice_text") or UPDATE_DEFAULT_TEXT

    lines = [
        _settings_header(),
        "",
        "📟 <b>Режимы</b>",
        "Техрежим и update-уведомления без хвостов.",
        "",
        f"Техрежим: <b>{tech_status}</b>",
    ]
    if tech_extra:
        lines.append(tech_extra)
    lines.extend(
        [
            f"📝 Текст техработ: {_clip_text(tech_notice, 200)}",
            "",
            f"Update mode: <b>{'включен' if update_state.get('update_enabled') else 'выключен'}</b>",
            f"Версия уведомления: <b>{int(update_state.get('update_notice_ver') or 0)}</b>",
            f"📝 Текст обновления: {_clip_text(update_text, 200)}",
        ]
    )
    if preview_update:
        lines.extend(["", "👁 <b>Предпросмотр текста обновления:</b>", update_text])
    if notice:
        lines.extend(["", notice])
    return "\n".join(lines)


def render_access_text(access_state: dict, *, notice: str | None = None) -> str:
    preset = _detect_access_preset(access_state)
    lines = [
        _settings_header(),
        "",
        "🧱 <b>Доступ</b>",
        "Пресеты + ручные тумблеры разделов.",
        "",
        f"Активный пресет: <b>{preset}</b>",
        f"• upload: <b>{'запрещено' if access_state.get('upload_blocked') else 'разрешено'}</b>",
        f"• rate: <b>{'запрещено' if access_state.get('rating_blocked') else 'разрешено'}</b>",
        f"• results: <b>{'запрещено' if access_state.get('results_blocked') else 'разрешено'}</b>",
        f"• profile: <b>{'запрещено' if access_state.get('profile_blocked') else 'разрешено'}</b>",
    ]
    if notice:
        lines.extend(["", notice])
    return "\n".join(lines)


def render_economy_text(economy: dict, *, notice: str | None = None) -> str:
    lines = [
        _settings_header(),
        "",
        "💳 <b>Экономика</b>",
        "Настройки кредитов, HH, tail и лимитов.",
        "",
        f"daily_free_credits_normal: <b>{_format_num(economy.get('daily_free_credits_normal'), 'int')}</b>",
        f"daily_free_credits_premium: <b>{_format_num(economy.get('daily_free_credits_premium'), 'int')}</b>",
        f"daily_free_credits_author: <b>{_format_num(economy.get('daily_free_credits_author'), 'int')}</b>",
        f"publish_bonus_credits: <b>{_format_num(economy.get('publish_bonus_credits'), 'int')}</b>",
        f"credit_to_shows_normal: <b>{_format_num(economy.get('credit_to_shows_normal'), 'int')}</b>",
        f"credit_to_shows_happyhour: <b>{_format_num(economy.get('credit_to_shows_happyhour'), 'int')}</b>",
        f"happy_hour_enabled: <b>{_format_bool(economy.get('happy_hour_enabled'))}</b>",
        f"happy_hour_start_hour: <b>{_format_num(economy.get('happy_hour_start_hour'), 'int')}</b>",
        f"happy_hour_duration_minutes: <b>{_format_num(economy.get('happy_hour_duration_minutes'), 'int')}</b>",
        f"tail_probability: <b>{_format_num(economy.get('tail_probability'), 'float')}</b>",
        f"min_votes_for_normal_feed: <b>{_format_num(economy.get('min_votes_for_normal_feed'), 'int')}</b>",
        f"max_active_photos_normal: <b>{_format_num(economy.get('max_active_photos_normal'), 'int')}</b>",
        f"max_active_photos_author: <b>{_format_num(economy.get('max_active_photos_author'), 'int')}</b>",
        f"max_active_photos_premium: <b>{_format_num(economy.get('max_active_photos_premium'), 'int')}</b>",
    ]
    if notice:
        lines.extend(["", notice])
    return "\n".join(lines)


def render_ads_text(ads: dict, *, preview: bool = False, notice: str | None = None) -> str:
    lines = [
        _settings_header(),
        "",
        "📢 <b>Реклама</b>",
        "Глобальные тумблеры рекламного блока.",
        "",
        f"ads_enabled: <b>{_format_bool(ads.get('enabled'))}</b>",
        f"ads_frequency_n: <b>{_format_num(ads.get('frequency_n'), 'int')}</b>",
        f"ads_only_nonpremium: <b>{_format_bool(ads.get('only_nonpremium'))}</b>",
    ]
    if preview:
        lines.extend(["", "👁 <b>Предпросмотр рекламного блока:</b>", "──────────────", "📣 Пример рекламного текста"])
    if notice:
        lines.extend(["", notice])
    return "\n".join(lines)


def render_protection_text(protection: dict, *, notice: str | None = None) -> str:
    lines = [
        _settings_header(),
        "",
        "🛡 <b>Защита</b>",
        "Пороги антиспама и защитный режим.",
        "",
        f"protection_mode_enabled: <b>{_format_bool(protection.get('mode_enabled'))}</b>",
        f"max_callbacks_per_minute_per_user: <b>{_format_num(protection.get('max_callbacks_per_minute_per_user'), 'int')}</b>",
        f"max_actions_per_10s: <b>{_format_num(protection.get('max_actions_per_10s'), 'int')}</b>",
        f"cooldown_on_spike_seconds: <b>{_format_num(protection.get('cooldown_on_spike_seconds'), 'int')}</b>",
        f"spam_suspect_threshold: <b>{_format_num(protection.get('spam_suspect_threshold'), 'int')}</b>",
        "",
        "При включенной защите бот может чаще ограничивать тяжелые экраны и кнопку «Еще».",
    ]
    if notice:
        lines.extend(["", notice])
    return "\n".join(lines)


def render_reset_text(counts: dict, *, notice: str | None = None) -> str:
    lines = [
        _settings_header(),
        "",
        "🧹 <b>Сброс</b>",
        "Опасные операции c предварительным просмотром.",
        "",
        f"Архивных фото сейчас: <b>{int(counts.get('archived_photos_count') or 0)}</b>",
        f"Строк итогов/кэшей: <b>{int(counts.get('results_rows_count') or 0)}</b>",
        f"Партий/дней результатов: <b>{int(counts.get('parties_days_count') or 0)}</b>",
    ]
    if notice:
        lines.extend(["", notice])
    return "\n".join(lines)


def _kb_settings(tab: str) -> object:
    current = tab if tab in TAB_TITLES else DEFAULT_TAB
    kb = _new_tab_keyboard(current)

    if current == "modes":
        kb.row(_btn("🟢 Включить сейчас", "admin:settings:tech:on:0"), _btn("🔴 Выключить", "admin:settings:tech:off"))
        kb.row(_btn("⏱ Через 5 мин", "admin:settings:tech:on:5"), _btn("⏱ Через 15 мин", "admin:settings:tech:on:15"))
        kb.row(_btn("⏱ Через 30 мин", "admin:settings:tech:on:30"), _btn("⏱ Через 60 мин", "admin:settings:tech:on:60"))
        kb.row(_btn("📝 Текст техработ", "admin:settings:tech:text"), _btn("♻️ Сбросить текст", "admin:settings:tech:text:reset"))
        kb.row(_btn("📝 Текст обновления", "admin:settings:update:text"), _btn("👁 Предпросмотр", "admin:settings:update:preview"))
        kb.row(_btn("🟢 Включить обновление", "admin:settings:update:on"), _btn("🟠 Выключить обновление", "admin:settings:update:off"))
    elif current == "access":
        kb.row(
            _btn("🟢 NORMAL", "admin:settings:access:preset:normal"),
            _btn("🟠 UPLOAD OFF", "admin:settings:access:preset:upload_off"),
            _btn("🟠 RATE OFF", "admin:settings:access:preset:rate_off"),
        )
        kb.row(
            _btn("🔴 READ-ONLY", "admin:settings:access:preset:read_only"),
            _btn("🔴 FULL LOCK", "admin:settings:access:preset:full_lock"),
        )
        kb.row(_btn("Публикация фото", "admin:settings:lock:upload"), _btn("Оценивание", "admin:settings:lock:rate"))
        kb.row(_btn("Вход в итоги", "admin:settings:lock:results"), _btn("Вход в профиль", "admin:settings:lock:profile"))
    elif current == "economy":
        kb.row(_btn("✏️ daily normal", "admin:settings:edit:eco_daily_normal"), _btn("✏️ daily premium", "admin:settings:edit:eco_daily_premium"))
        kb.row(_btn("✏️ daily author", "admin:settings:edit:eco_daily_author"), _btn("✏️ publish bonus", "admin:settings:edit:eco_publish_bonus"))
        kb.row(_btn("✏️ credit normal", "admin:settings:edit:eco_credit_normal"), _btn("✏️ credit HH", "admin:settings:edit:eco_credit_hh"))
        kb.row(_btn("happy hour вкл/выкл", "admin:settings:econ:hh:toggle"))
        kb.row(_btn("✏️ HH start", "admin:settings:edit:eco_hh_start"), _btn("✏️ HH duration", "admin:settings:edit:eco_hh_duration"))
        kb.row(_btn("✏️ tail_probability", "admin:settings:edit:eco_tail"), _btn("✏️ min_votes", "admin:settings:edit:eco_votes"))
        kb.row(_btn("✏️ max normal", "admin:settings:edit:eco_max_normal"), _btn("✏️ max author", "admin:settings:edit:eco_max_author"))
        kb.row(_btn("✏️ max premium", "admin:settings:edit:eco_max_premium"))
        kb.row(_btn("↩️ Сбросить к дефолту", "admin:settings:econ:reset_defaults"))
    elif current == "ads":
        kb.row(_btn("ads_enabled вкл/выкл", "admin:settings:ads:toggle_enabled"))
        kb.row(_btn("ads_only_nonpremium вкл/выкл", "admin:settings:ads:toggle_only_nonpremium"))
        kb.row(_btn("✏️ ads_frequency_n", "admin:settings:edit:ads_frequency"))
        kb.row(_btn("👁 Предпросмотр рекламного блока", "admin:settings:ads:preview"))
    elif current == "protection":
        kb.row(_btn("protection_mode вкл/выкл", "admin:settings:protection:toggle"))
        kb.row(_btn("✏️ callbacks/min", "admin:settings:edit:prot_callbacks"), _btn("✏️ actions/10s", "admin:settings:edit:prot_actions"))
        kb.row(_btn("✏️ cooldown", "admin:settings:edit:prot_cooldown"), _btn("✏️ threshold", "admin:settings:edit:prot_threshold"))
    elif current == "reset":
        kb.row(_btn("Удалить только итоги/кэши", "admin:settings:reset:ask:results"))
        kb.row(_btn("Удалить только архивные фото", "admin:settings:reset:ask:archives"))
        kb.row(_btn("Полный сброс", "admin:settings:reset:ask:full"))
        kb.row(_btn("🔄 Обновить счетчики", "admin:settings:tab:reset"))

    return kb.as_markup()


def _kb_reset_confirm_step1(mode: str) -> object:
    kb = InlineKeyboardBuilder()
    kb.row(_btn("Продолжить", f"admin:settings:reset:confirm1:{mode}"), _btn("Отмена", "admin:settings:tab:reset"))
    return kb.as_markup()


def _kb_reset_confirm_step2(mode: str) -> object:
    kb = InlineKeyboardBuilder()
    kb.row(_btn("✅ Удалить", f"admin:settings:reset:do:{mode}"), _btn("❌ Отмена", "admin:settings:tab:reset"))
    return kb.as_markup()


async def _reset_input_mode(state: FSMContext) -> None:
    await state.set_state(None)
    await state.update_data(admin_settings_edit_field=None)


async def _get_current_tab(state: FSMContext) -> str:
    data = await state.get_data()
    tab = str(data.get(SETTINGS_TAB_KEY) or DEFAULT_TAB)
    if tab not in TAB_TITLES:
        tab = DEFAULT_TAB
    return tab


async def _set_current_tab(state: FSMContext, tab: str) -> str:
    current = tab if tab in TAB_TITLES else DEFAULT_TAB
    await state.update_data(**{SETTINGS_TAB_KEY: current})
    return current


async def _render_settings(
    message: Message,
    state: FSMContext,
    *,
    tab: str | None = None,
    notice: str | None = None,
    preview_update: bool = False,
    preview_ad: bool = False,
) -> None:
    current = await _set_current_tab(state, tab or await _get_current_tab(state))
    if current == "modes":
        tech_state = await get_tech_mode_state()
        update_state = await get_update_mode_state()
        text = render_modes_text(tech_state, update_state, preview_update=preview_update, notice=notice)
    elif current == "access":
        access_state = await get_section_access_state()
        text = render_access_text(access_state, notice=notice)
    elif current == "economy":
        economy = await get_effective_economy_settings()
        text = render_economy_text(economy, notice=notice)
    elif current == "ads":
        ads = await get_effective_ads_settings()
        text = render_ads_text(ads, preview=preview_ad, notice=notice)
    elif current == "protection":
        protection = await get_effective_protection_settings()
        text = render_protection_text(protection, notice=notice)
    else:
        counts = await get_results_reset_preview_counts()
        text = render_reset_text(counts, notice=notice)

    await edit_or_answer(
        message,
        state,
        prefix=SETTINGS_PREFIX,
        text=text,
        reply_markup=_kb_settings(current),
    )


def _parse_number(raw_text: str, kind: str) -> int | float:
    raw = str(raw_text or "").strip().replace(" ", "")
    if kind == "int":
        if not re.fullmatch(r"[+-]?\d+", raw):
            raise ValueError("expected_int")
        return int(raw)
    safe = raw.replace(",", ".")
    if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d+)?|\.\d+)", safe):
        raise ValueError("expected_float")
    return float(safe)


def _reset_mode_title(mode: str) -> str:
    names = {
        "results": "Удалить только итоги/кэши",
        "archives": "Удалить только архивные фото",
        "full": "Полный сброс",
    }
    return names.get(mode, "Сброс")


def _render_reset_confirm_text(mode: str, counts: dict, *, final: bool = False) -> str:
    archived = int(counts.get("archived_photos_count") or 0)
    results = int(counts.get("results_rows_count") or 0)
    days = int(counts.get("parties_days_count") or 0)

    lines = [
        _settings_header(),
        "",
        "🧹 <b>Сброс</b>",
        f"Режим: <b>{_reset_mode_title(mode)}</b>",
        "",
        f"Архивных фото сейчас: <b>{archived}</b>",
        f"Строк итогов/кэшей: <b>{results}</b>",
        f"Партий/дней результатов: <b>{days}</b>",
        "",
    ]
    if final:
        lines.append("⚠️ Последнее подтверждение: удалить данные?")
    else:
        lines.append("⚠️ Это удалит данные без возможности отката. Продолжить?")
    return "\n".join(lines)


def _render_reset_done(mode: str, result: dict) -> str:
    return (
        f"✅ <b>{_reset_mode_title(mode)}: выполнено</b>\n"
        f"Архивных фото удалено: <b>{int(result.get('archived_photos_deleted') or 0)}</b>\n"
        f"Строк итогов/кэшей удалено: <b>{int(result.get('results_rows_deleted') or 0)}</b>\n"
        f"Всего затронуто строк: <b>{int(result.get('total_rows_affected') or 0)}</b>"
    )


@router.callback_query(F.data == "admin:settings")
async def admin_settings_open(callback: CallbackQuery, state: FSMContext):
    user = await _ensure_admin(callback)
    if user is None:
        return

    current_tab = await _get_current_tab(state)
    await _reset_input_mode(state)
    await _render_settings(callback.message, state, tab=current_tab)
    await callback.answer()


@router.callback_query(F.data.startswith("admin:settings:tab:"))
async def admin_settings_switch_tab(callback: CallbackQuery, state: FSMContext):
    user = await _ensure_admin(callback)
    if user is None:
        return

    tab = str((callback.data or "").split(":")[-1]).strip().lower()
    if tab not in TAB_TITLES:
        await callback.answer("Неизвестная вкладка", show_alert=True)
        return

    await _reset_input_mode(state)
    await _render_settings(callback.message, state, tab=tab)
    await callback.answer()


@router.callback_query(F.data == "admin:settings:tech:on")
async def admin_settings_tech_on_legacy(callback: CallbackQuery, state: FSMContext):
    user = await _ensure_admin(callback)
    if user is None:
        return
    start_at = (get_moscow_now() + timedelta(minutes=5)).isoformat()
    await set_tech_mode_state(enabled=True, start_at=start_at)
    await _render_settings(callback.message, state, tab="modes", notice="✅ Техрежим запланирован через 5 мин")
    await callback.answer()


@router.callback_query(F.data.startswith("admin:settings:tech:on:"))
async def admin_settings_tech_on(callback: CallbackQuery, state: FSMContext):
    user = await _ensure_admin(callback)
    if user is None:
        return

    parts = str(callback.data or "").split(":")
    try:
        minutes = int(parts[-1])
    except Exception:
        minutes = 0
    if minutes not in ALLOWED_TECH_DELAY_MINUTES:
        await callback.answer("Недопустимая задержка", show_alert=True)
        return

    start_at = (get_moscow_now() + timedelta(minutes=minutes)).isoformat()
    await set_tech_mode_state(enabled=True, start_at=start_at)

    await _render_settings(
        callback.message,
        state,
        tab="modes",
        notice=("✅ Техрежим включен сейчас" if minutes == 0 else f"✅ Техрежим запланирован через {minutes} мин"),
    )
    await callback.answer()


@router.callback_query(F.data == "admin:settings:tech:off")
async def admin_settings_tech_off(callback: CallbackQuery, state: FSMContext):
    user = await _ensure_admin(callback)
    if user is None:
        return

    await set_tech_mode_state(enabled=False, start_at=None)
    await _render_settings(callback.message, state, tab="modes", notice="✅ Техрежим выключен")
    await callback.answer()


@router.callback_query(F.data == "admin:settings:tech:text")
async def admin_settings_tech_text_start(callback: CallbackQuery, state: FSMContext):
    user = await _ensure_admin(callback)
    if user is None:
        return

    await state.set_state(AdminSettingsInput.waiting_tech_notice_text)
    await _set_current_tab(state, "modes")
    await _render_settings(
        callback.message,
        state,
        tab="modes",
        notice="✍️ Отправь новый текст техработ (до 1500 символов).",
    )
    await callback.answer()


@router.callback_query(F.data == "admin:settings:tech:text:reset")
async def admin_settings_tech_text_reset(callback: CallbackQuery, state: FSMContext):
    user = await _ensure_admin(callback)
    if user is None:
        return

    tech = await get_tech_mode_state()
    await set_tech_mode_state(
        enabled=bool(tech.get("tech_enabled")),
        start_at=tech.get("tech_start_at"),
        notice_text=None,
    )
    await _reset_input_mode(state)
    await _render_settings(callback.message, state, tab="modes", notice="✅ Текст техработ сброшен")
    await callback.answer()


@router.message(AdminSettingsInput.waiting_tech_notice_text)
async def admin_settings_tech_text_save(message: Message, state: FSMContext):
    user = await _ensure_admin(message)
    if user is None:
        return

    raw_text = str(message.text or message.caption or "").strip()
    if not raw_text:
        await _render_settings(message, state, tab="modes", notice="❌ Нужен текст (до 1500 символов).")
        return
    if len(raw_text) > 1500:
        await _render_settings(message, state, tab="modes", notice="❌ Слишком длинно. Максимум 1500 символов.")
        return

    tech = await get_tech_mode_state()
    await set_tech_mode_state(
        enabled=bool(tech.get("tech_enabled")),
        start_at=tech.get("tech_start_at"),
        notice_text=_safe_notice_text(raw_text),
    )

    await _reset_input_mode(state)
    await _render_settings(message, state, tab="modes", notice="✅ Текст техработ сохранен")
    try:
        await message.delete()
    except Exception:
        pass


@router.callback_query(F.data == "admin:settings:update:text")
async def admin_settings_update_text_start(callback: CallbackQuery, state: FSMContext):
    user = await _ensure_admin(callback)
    if user is None:
        return

    await state.set_state(AdminSettingsInput.waiting_update_notice_text)
    await _set_current_tab(state, "modes")
    await _render_settings(
        callback.message,
        state,
        tab="modes",
        notice="✍️ Отправь текст обновления (до 1500 символов).",
    )
    await callback.answer()


@router.message(AdminSettingsInput.waiting_update_notice_text)
async def admin_settings_update_text_save(message: Message, state: FSMContext):
    user = await _ensure_admin(message)
    if user is None:
        return

    raw_text = str(message.text or message.caption or "").strip()
    if not raw_text:
        await _render_settings(message, state, tab="modes", notice="❌ Нужен текст (до 1500 символов).")
        return
    if len(raw_text) > 1500:
        await _render_settings(message, state, tab="modes", notice="❌ Слишком длинно. Максимум 1500 символов.")
        return

    update_state = await get_update_mode_state()
    await set_update_mode_state(
        enabled=bool(update_state.get("update_enabled")),
        notice_text=_safe_notice_text(raw_text),
        bump_version=False,
    )

    await _reset_input_mode(state)
    await _render_settings(message, state, tab="modes", notice="✅ Текст обновления сохранен")
    try:
        await message.delete()
    except Exception:
        pass


@router.callback_query(F.data == "admin:settings:update:preview")
async def admin_settings_update_preview(callback: CallbackQuery, state: FSMContext):
    user = await _ensure_admin(callback)
    if user is None:
        return

    await _reset_input_mode(state)
    await _render_settings(callback.message, state, tab="modes", preview_update=True)
    await callback.answer()


@router.callback_query(F.data == "admin:settings:update:on")
async def admin_settings_update_on(callback: CallbackQuery, state: FSMContext):
    user = await _ensure_admin(callback)
    if user is None:
        return

    await set_update_mode_state(enabled=True, notice_text=None, bump_version=True)
    await _reset_input_mode(state)
    await _render_settings(
        callback.message,
        state,
        tab="modes",
        notice="✅ Update mode включен. Пользователи увидят уведомление один раз.",
    )
    await callback.answer()


@router.callback_query(F.data == "admin:settings:update:off")
async def admin_settings_update_off(callback: CallbackQuery, state: FSMContext):
    user = await _ensure_admin(callback)
    if user is None:
        return

    await set_update_mode_state(enabled=False, notice_text=None, bump_version=False)
    await _reset_input_mode(state)
    await _render_settings(callback.message, state, tab="modes", notice="✅ Update mode выключен")
    await callback.answer()


@router.callback_query(F.data.startswith("admin:settings:access:preset:"))
async def admin_settings_access_preset(callback: CallbackQuery, state: FSMContext):
    user = await _ensure_admin(callback)
    if user is None:
        return

    preset = str((callback.data or "").split(":")[-1]).strip().lower()
    try:
        await apply_access_preset(preset)
    except ValueError:
        await callback.answer("Неизвестный пресет", show_alert=True)
        return

    await _reset_input_mode(state)
    await _render_settings(callback.message, state, tab="access", notice=f"✅ Применен пресет: {preset}")
    await callback.answer()


@router.callback_query(F.data.startswith("admin:settings:lock:"))
async def admin_settings_toggle_section_lock(callback: CallbackQuery, state: FSMContext):
    user = await _ensure_admin(callback)
    if user is None:
        return

    section = (callback.data or "").split(":")[-1].strip().lower()
    if section not in {"upload", "rate", "results", "profile"}:
        await callback.answer("Неизвестная настройка", show_alert=True)
        return

    access_state = await toggle_section_blocked(section)
    key = f"{'rating' if section == 'rate' else section}_blocked"
    now_blocked = bool(access_state.get(key))

    await _reset_input_mode(state)
    await _render_settings(
        callback.message,
        state,
        tab="access",
        notice=(
            f"✅ {section}: {'запрещено' if now_blocked else 'разрешено'}"
        ),
    )
    await callback.answer()


@router.callback_query(F.data == "admin:settings:econ:hh:toggle")
async def admin_settings_toggle_happy_hour(callback: CallbackQuery, state: FSMContext):
    user = await _ensure_admin(callback)
    if user is None:
        return

    economy = await get_effective_economy_settings()
    current = bool(economy.get("happy_hour_enabled"))
    await set_setting("economy.happy_hour_enabled", not current)

    await _reset_input_mode(state)
    await _render_settings(
        callback.message,
        state,
        tab="economy",
        notice=f"✅ happy_hour_enabled: {'вкл' if not current else 'выкл'}",
    )
    await callback.answer()


@router.callback_query(F.data == "admin:settings:econ:reset_defaults")
async def admin_settings_economy_reset_defaults(callback: CallbackQuery, state: FSMContext):
    user = await _ensure_admin(callback)
    if user is None:
        return

    await set_settings_bulk(_economy_defaults_payload())
    await _reset_input_mode(state)
    await _render_settings(callback.message, state, tab="economy", notice="✅ Экономика сброшена к дефолту")
    await callback.answer()


@router.callback_query(F.data == "admin:settings:ads:toggle_enabled")
async def admin_settings_ads_toggle_enabled(callback: CallbackQuery, state: FSMContext):
    user = await _ensure_admin(callback)
    if user is None:
        return

    ads = await get_effective_ads_settings()
    current = bool(ads.get("enabled"))
    await set_setting("ads.enabled", not current)

    await _reset_input_mode(state)
    await _render_settings(
        callback.message,
        state,
        tab="ads",
        notice=f"✅ ads_enabled: {'вкл' if not current else 'выкл'}",
    )
    await callback.answer()


@router.callback_query(F.data == "admin:settings:ads:toggle_only_nonpremium")
async def admin_settings_ads_toggle_only_nonpremium(callback: CallbackQuery, state: FSMContext):
    user = await _ensure_admin(callback)
    if user is None:
        return

    ads = await get_effective_ads_settings()
    current = bool(ads.get("only_nonpremium"))
    await set_setting("ads.only_nonpremium", not current)

    await _reset_input_mode(state)
    await _render_settings(
        callback.message,
        state,
        tab="ads",
        notice=f"✅ ads_only_nonpremium: {'вкл' if not current else 'выкл'}",
    )
    await callback.answer()


@router.callback_query(F.data == "admin:settings:ads:preview")
async def admin_settings_ads_preview(callback: CallbackQuery, state: FSMContext):
    user = await _ensure_admin(callback)
    if user is None:
        return

    await _reset_input_mode(state)
    await _render_settings(callback.message, state, tab="ads", preview_ad=True)
    await callback.answer()


@router.callback_query(F.data == "admin:settings:protection:toggle")
async def admin_settings_protection_toggle(callback: CallbackQuery, state: FSMContext):
    user = await _ensure_admin(callback)
    if user is None:
        return

    protection = await get_effective_protection_settings()
    current = bool(protection.get("mode_enabled"))
    await set_setting("protection.mode_enabled", not current)

    await _reset_input_mode(state)
    await _render_settings(
        callback.message,
        state,
        tab="protection",
        notice=f"✅ protection_mode_enabled: {'вкл' if not current else 'выкл'}",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:settings:edit:"))
async def admin_settings_edit_number_start(callback: CallbackQuery, state: FSMContext):
    user = await _ensure_admin(callback)
    if user is None:
        return

    code = str((callback.data or "").split(":")[-1]).strip()
    meta = NUMERIC_EDIT_FIELDS.get(code)
    if not meta:
        await callback.answer("Параметр не найден", show_alert=True)
        return

    await state.update_data(admin_settings_edit_field=code)
    await state.set_state(AdminSettingsInput.waiting_number_value)
    await _set_current_tab(state, str(meta.get("tab") or DEFAULT_TAB))
    await _render_settings(
        callback.message,
        state,
        tab=str(meta.get("tab") or DEFAULT_TAB),
        notice=(
            f"✍️ Введите {meta.get('label')} ({meta.get('kind')}, "
            f"{meta.get('min')}..{meta.get('max')})."
        ),
    )
    await callback.answer()


@router.message(AdminSettingsInput.waiting_number_value)
async def admin_settings_edit_number_save(message: Message, state: FSMContext):
    user = await _ensure_admin(message)
    if user is None:
        return

    data = await state.get_data()
    code = str(data.get("admin_settings_edit_field") or "")
    meta = NUMERIC_EDIT_FIELDS.get(code)
    if not meta:
        await _reset_input_mode(state)
        await _render_settings(message, state, tab=DEFAULT_TAB, notice="❌ Потерян контекст редактирования")
        return

    tab = str(meta.get("tab") or DEFAULT_TAB)
    raw = str(message.text or message.caption or "").strip()
    try:
        value = _parse_number(raw, str(meta.get("kind") or "int"))
    except Exception:
        await _render_settings(
            message,
            state,
            tab=tab,
            notice=(
                f"❌ Неверный формат. Нужен {meta.get('kind')} в диапазоне "
                f"{meta.get('min')}..{meta.get('max')}."
            ),
        )
        return

    min_v = meta.get("min")
    max_v = meta.get("max")
    if min_v is not None and value < min_v:
        await _render_settings(message, state, tab=tab, notice=f"❌ Значение ниже минимума ({min_v}).")
        return
    if max_v is not None and value > max_v:
        await _render_settings(message, state, tab=tab, notice=f"❌ Значение выше максимума ({max_v}).")
        return

    store_value: int | float
    if str(meta.get("kind")) == "int":
        store_value = int(value)
    else:
        store_value = float(value)

    await set_setting(str(meta.get("db_key")), store_value)
    await _reset_input_mode(state)
    await _render_settings(
        message,
        state,
        tab=tab,
        notice=f"✅ {meta.get('label')} = {_format_num(store_value, str(meta.get('kind') or 'int'))}",
    )
    try:
        await message.delete()
    except Exception:
        pass


@router.callback_query(F.data == "admin:settings:results_reset:ask")
async def admin_settings_results_reset_ask_legacy(callback: CallbackQuery, state: FSMContext):
    user = await _ensure_admin(callback)
    if user is None:
        return
    await _reset_input_mode(state)
    counts = await get_results_reset_preview_counts()
    await edit_or_answer(
        callback.message,
        state,
        prefix=SETTINGS_PREFIX,
        text=_render_reset_confirm_text("full", counts, final=False),
        reply_markup=_kb_reset_confirm_step1("full"),
    )
    await callback.answer()


@router.callback_query(F.data == "admin:settings:results_reset:do")
async def admin_settings_results_reset_do_legacy(callback: CallbackQuery, state: FSMContext):
    user = await _ensure_admin(callback)
    if user is None:
        return
    result = await admin_reset_results_and_archives()
    await _reset_input_mode(state)
    await _render_settings(
        callback.message,
        state,
        tab="reset",
        notice=_render_reset_done("full", result),
    )
    await callback.answer("Сброс выполнен", show_alert=True)


@router.callback_query(F.data.startswith("admin:settings:reset:ask:"))
async def admin_settings_reset_ask(callback: CallbackQuery, state: FSMContext):
    user = await _ensure_admin(callback)
    if user is None:
        return

    mode = str((callback.data or "").split(":")[-1]).strip().lower()
    if mode not in {"results", "archives", "full"}:
        await callback.answer("Неизвестный режим сброса", show_alert=True)
        return

    await _reset_input_mode(state)
    counts = await get_results_reset_preview_counts()
    await edit_or_answer(
        callback.message,
        state,
        prefix=SETTINGS_PREFIX,
        text=_render_reset_confirm_text(mode, counts, final=False),
        reply_markup=_kb_reset_confirm_step1(mode),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:settings:reset:confirm1:"))
async def admin_settings_reset_confirm1(callback: CallbackQuery, state: FSMContext):
    user = await _ensure_admin(callback)
    if user is None:
        return

    mode = str((callback.data or "").split(":")[-1]).strip().lower()
    if mode not in {"results", "archives", "full"}:
        await callback.answer("Неизвестный режим сброса", show_alert=True)
        return

    counts = await get_results_reset_preview_counts()
    await edit_or_answer(
        callback.message,
        state,
        prefix=SETTINGS_PREFIX,
        text=_render_reset_confirm_text(mode, counts, final=True),
        reply_markup=_kb_reset_confirm_step2(mode),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:settings:reset:do:"))
async def admin_settings_reset_do(callback: CallbackQuery, state: FSMContext):
    user = await _ensure_admin(callback)
    if user is None:
        return

    mode = str((callback.data or "").split(":")[-1]).strip().lower()
    if mode == "results":
        result = await admin_reset_results_only()
    elif mode == "archives":
        result = await admin_reset_archives_only()
    elif mode == "full":
        result = await admin_reset_results_and_archives()
    else:
        await callback.answer("Неизвестный режим сброса", show_alert=True)
        return

    await _reset_input_mode(state)
    await _render_settings(
        callback.message,
        state,
        tab="reset",
        notice=_render_reset_done(mode, result),
    )
    await callback.answer("Сброс выполнен", show_alert=True)
