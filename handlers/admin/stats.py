

from __future__ import annotations

# =============================================================
# ==== АДМИНКА: СТАТИСТИКА ====================================
# =============================================================

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from database import (
    get_total_users,
    get_users_sample,
    get_active_users_last_24h,
    get_online_users_recent,
    get_total_activity_events,
    get_new_users_last_days,
    get_premium_stats,
    get_premium_users,
    get_top_users_by_activity_events,
)

from .common import _ensure_admin, _safe_int

router = Router()


# =============================================================
# ==== СВОДНАЯ СТАТИСТИКА ======================================
# =============================================================


@router.callback_query(F.data == "admin:stats")
async def admin_stats(callback: CallbackQuery, state: FSMContext):
    """Сводная статистика (быстро, без сложных фильтров)."""
    user = await _ensure_admin(callback)
    if user is None:
        return

    total_users = active_24h = online_recent = total_events = new_7d = premium_total = 0

    try:
        total_users = _safe_int(await get_total_users())
    except Exception:
        pass

    # эти функции возвращают (total, rows)
    try:
        active_total, _ = await get_active_users_last_24h(limit=1, offset=0)
        active_24h = _safe_int(active_total)
    except Exception:
        pass

    try:
        online_total, _ = await get_online_users_recent(window_minutes=5, limit=1, offset=0)
        online_recent = _safe_int(online_total)
    except Exception:
        pass

    try:
        total_events = _safe_int(await get_total_activity_events())
    except Exception:
        pass

    try:
        new_total, _ = await get_new_users_last_days(7, limit=1, offset=0)
        new_7d = _safe_int(new_total)
    except Exception:
        pass

    try:
        prem = await get_premium_stats()
        if isinstance(prem, dict):
            premium_total = _safe_int(prem.get("total") or prem.get("premium_total") or prem.get("count"))
        else:
            premium_total = _safe_int(prem)
    except Exception:
        pass

    text = (
        "📊 <b>Статистика</b>\n\n"
        f"👥 Всего пользователей: <b>{total_users}</b>\n"
        f"⚡ Активных за 24ч: <b>{active_24h}</b>\n"
        f"🟢 Онлайн (recent): <b>{online_recent}</b>\n"
        f"🧠 Всего событий активности: <b>{total_events}</b>\n"
        f"🆕 Новых за 7 дней: <b>{new_7d}</b>\n"
        f"🌟 Премиум (всего): <b>{premium_total}</b>\n"
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="👥 Всего пользователей — список", callback_data="admin:stats:list:total:1")
    kb.button(text="⚡ Активные 24ч — список", callback_data="admin:stats:list:active24:1")
    kb.button(text="🟢 Онлайн (recent) — список", callback_data="admin:stats:list:online:1")
    kb.button(text="🧠 События активности — список", callback_data="admin:stats:list:events:1")
    kb.button(text="🆕 Новые за 7 дней — список", callback_data="admin:stats:list:new7:1")
    kb.button(text="🌟 Премиум — список", callback_data="admin:stats:list:premium:1")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(1)

    try:
        await callback.message.edit_text(text, reply_markup=kb.as_markup())
    except Exception:
        await callback.message.answer(text, reply_markup=kb.as_markup())

    await callback.answer()


# =============================================================
# ==== СТАТИСТИКА: СПИСКИ =====================================
# =============================================================

_STATS_PAGE_LIMIT = 20


def _fmt_user_short(u: dict) -> str:
    tg_id = u.get("tg_id")
    username = (u.get("username") or "").strip()
    name = (u.get("name") or "").strip()
    uname = f"@{username}" if username else "—"
    nm = name if name else "Без имени"
    return f"{uname} · {nm} · <code>{tg_id if tg_id is not None else '—'}</code>"


def _stats_list_title(kind: str) -> str:
    return {
        "total": "👥 Все пользователи",
        "active24": "⚡ Активные за 24ч",
        "online": "🟢 Онлайн (recent)",
        "events": "🧠 Топ по событиям активности",
        "new7": "🆕 Новые за 7 дней",
        "premium": "🌟 Премиум пользователи",
    }.get(kind, "📋 Список")


@router.callback_query(F.data.startswith("admin:stats:list:"))
async def admin_stats_list(callback: CallbackQuery, state: FSMContext):
    admin_user = await _ensure_admin(callback)
    if admin_user is None:
        return

    # admin:stats:list:<kind>:<page>
    parts = (callback.data or "").split(":")
    if len(parts) < 5:
        await callback.answer("Некорректная команда.", show_alert=True)
        return

    kind = parts[3]
    try:
        page = int(parts[4])
    except Exception:
        page = 1

    page = max(1, page)
    offset = (page - 1) * _STATS_PAGE_LIMIT

    total = 0
    rows: list[dict] = []

    try:
        if kind == "total":
            total = _safe_int(await get_total_users())
            rows = await get_users_sample(limit=_STATS_PAGE_LIMIT, offset=offset, only_active=True)

        elif kind == "active24":
            total, rows = await get_active_users_last_24h(limit=_STATS_PAGE_LIMIT, offset=offset)

        elif kind == "online":
            total, rows = await get_online_users_recent(window_minutes=5, limit=_STATS_PAGE_LIMIT, offset=offset)

        elif kind == "new7":
            total, rows = await get_new_users_last_days(7, limit=_STATS_PAGE_LIMIT, offset=offset)

        elif kind == "premium":
            prem = await get_premium_stats()
            if isinstance(prem, dict):
                total = _safe_int(prem.get("total") or prem.get("premium_total") or prem.get("count"))
            else:
                total = _safe_int(prem)
            rows = await get_premium_users(limit=_STATS_PAGE_LIMIT, offset=offset)

        elif kind == "events":
            total, rows = await get_top_users_by_activity_events(limit=_STATS_PAGE_LIMIT, offset=offset)

        else:
            await callback.answer("Неизвестный список.", show_alert=True)
            return

    except Exception:
        await callback.answer("Не удалось получить список (ошибка БД).", show_alert=True)
        return

    total_pages = max(1, (int(total or 0) + _STATS_PAGE_LIMIT - 1) // _STATS_PAGE_LIMIT)
    if page > total_pages:
        page = total_pages

    lines: list[str] = [
        f"{_stats_list_title(kind)}",
        f"Всего: <b>{int(total or 0)}</b>",
        f"Страница: <b>{page}</b>/<b>{total_pages}</b>",
        "",
    ]

    if not rows:
        lines.append("Пусто.")
    else:
        for i, u in enumerate(rows, start=offset + 1):
            if kind == "events" and (u.get("events_count") is not None):
                lines.append(f"{i}. {_fmt_user_short(u)} · событий: <b>{int(u.get('events_count') or 0)}</b>")
            else:
                lines.append(f"{i}. {_fmt_user_short(u)}")

    text = "\n".join(lines)

    kb = InlineKeyboardBuilder()
    if page > 1:
        kb.button(text="⬅️", callback_data=f"admin:stats:list:{kind}:{page-1}")
    if page < total_pages:
        kb.button(text="➡️", callback_data=f"admin:stats:list:{kind}:{page+1}")

    kb.button(text="⬅️ Назад к статистике", callback_data="admin:stats")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")

    if page > 1 or page < total_pages:
        kb.adjust(2, 1, 1)
    else:
        kb.adjust(1, 1)

    try:
        await callback.message.edit_text(text, reply_markup=kb.as_markup())
    except Exception:
        await callback.message.answer(text, reply_markup=kb.as_markup())

    await callback.answer()