from __future__ import annotations

from typing import Any

from datetime import datetime, timedelta, date
import time
import html

from aiogram import Router, F, Bot
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InputMediaPhoto, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder

import io
from PIL import Image, ImageDraw, ImageFont  # type: ignore[import]

from keyboards.common import HOME, RESULTS, RESULTS_ARCHIVE, build_back_to_menu_kb
from utils.i18n import t
from utils.banner import sync_giraffe_section_nav
from utils.time import (
    get_moscow_now,
    get_moscow_today,
    format_party_label,
    format_day_short,
)
from utils.ui import cleanup_previous_screen, remember_screen

from database_results import (
    PERIOD_DAY,
    SCOPE_CITY,
    SCOPE_COUNTRY,
    KIND_TOP_PHOTOS,
    ALL_TIME_MIN_VOTES,
    ALLTIME_CACHE_KIND_TOP3,
    get_results_items,
    get_all_time_top,
    get_all_time_user_rank,
    update_hall_of_fame_from_top,
    get_hof_items,
    refresh_hof_statuses,
    get_alltime_cache,
    upsert_alltime_cache,
    refresh_alltime_cache_payload,
)

from database import (
    get_user_by_tg_id,
    set_user_screen_msg_id,
    get_latest_daily_results_cache,
    get_daily_results_cache,
    get_daily_results_top_live,
    list_daily_results_days,
    publish_daily_results,
    is_section_blocked,
    get_tech_mode_state,
)
from utils.registration_guard import require_user_name



router = Router()
SECTION_BLOCKED_TEXT = (
    "Пока что вход в этот раздел запрещен. Возможно ведутся улучшения или исправления багов. "
    "Подождите пожалуйста!"
)


async def _blocked_section_text() -> str:
    try:
        tech = await get_tech_mode_state()
        custom = str(tech.get("tech_notice_text") or "").strip()
        if custom:
            return custom
    except Exception:
        pass
    return SECTION_BLOCKED_TEXT


@router.callback_query(F.data.startswith("results:"))
async def _results_access_guard(callback: CallbackQuery, state: FSMContext | None = None):
    try:
        blocked = await is_section_blocked("results")
    except Exception:
        blocked = False
    if not blocked:
        raise SkipHandler

    sent_id = await _show_text(callback, await _blocked_section_text(), build_back_to_menu_kb())
    if sent_id is not None and state is not None:
        try:
            await remember_screen(callback.from_user.id, int(sent_id), state=state)
        except Exception:
            pass
    await callback.answer()

def _lang(user: dict | None) -> str:
    try:
        raw = (user or {}).get("lang") or (user or {}).get("language") or (user or {}).get("language_code")
        if raw:
            return str(raw).split("-")[0].lower()
    except Exception:
        pass
    return "ru"

# =========================
# DB helpers (users.city/users.country)
# =========================

try:
    from database import _assert_pool
except Exception:  # pragma: no cover
    _assert_pool = None  # type: ignore


def _pool() -> Any:
    if _assert_pool is None:
        raise RuntimeError("DB pool is not available: cannot import _assert_pool from database.py")
    return _assert_pool()


async def _get_user_place(user_tg_id: int) -> tuple[str, str]:
    p = _pool()
    async with p.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT COALESCE(city,'' ) AS city, COALESCE(country,'') AS country FROM users WHERE tg_id=$1",
            int(user_tg_id),
        )
        if not row:
            return "", ""
        return str(row["city"] or "").strip(), str(row["country"] or "").strip()

# =========================
# UI helpers
# =========================

PODIUM_MIN_PARTICIPANTS = 3
TOP10_DEFAULT = 10
_DAY_CACHE_REBUILD_COOLDOWN_SEC = 60.0
_DAY_CACHE_REBUILD_TS: dict[str, float] = {}


def _parse_day_key(day_key: str) -> date | None:
    try:
        return date.fromisoformat(str(day_key or "").strip())
    except Exception:
        return None


async def _get_day_cache_ready(day_key: str) -> dict | None:
    """
    Ensure daily cache is present and not stale-empty for podium/top views.

    If cache is missing or has participants>=3 but empty top, try to rebuild once per cooldown.
    """
    cache = await get_daily_results_cache(day_key)
    should_rebuild = False
    if not cache:
        should_rebuild = True
    else:
        payload = cache.get("payload") if isinstance(cache.get("payload"), dict) else {}
        participants = _daily_int(payload.get("participants_count") or cache.get("participants_count") or 0, 0)
        top = payload.get("top") if isinstance(payload.get("top"), list) else []
        if participants >= PODIUM_MIN_PARTICIPANTS and not top:
            should_rebuild = True

    if should_rebuild:
        now_ts = time.monotonic()
        last_ts = float(_DAY_CACHE_REBUILD_TS.get(day_key) or 0.0)
        if now_ts - last_ts >= _DAY_CACHE_REBUILD_COOLDOWN_SEC:
            _DAY_CACHE_REBUILD_TS[day_key] = now_ts
            day = _parse_day_key(day_key)
            if day is not None:
                try:
                    await publish_daily_results(day, limit=10)
                except Exception:
                    pass
            cache = await get_daily_results_cache(day_key)

    # Fallback: read directly from result_ranks if cache payload is stale-empty.
    payload = cache.get("payload") if cache and isinstance(cache.get("payload"), dict) else {}
    participants = _daily_int((payload or {}).get("participants_count") or (cache or {}).get("participants_count") or 0, 0)
    top = (payload or {}).get("top") if isinstance((payload or {}).get("top"), list) else []
    if (cache is None) or (participants >= PODIUM_MIN_PARTICIPANTS and not top):
        try:
            live = await get_daily_results_top_live(day_key, limit=10)
        except Exception:
            live = {}
        live_participants = _daily_int(live.get("participants_count") or 0, 0)
        live_top = live.get("top") if isinstance(live.get("top"), list) else []
        if cache is None and (live_participants > 0 or live_top):
            cache = {
                "submit_day": str(live.get("submit_day") or day_key),
                "participants_count": int(live_participants),
                "top_threshold": _daily_int(live.get("top_threshold") or 0, 0),
                "published_at": None,
                "payload": {
                    "submit_day": str(live.get("submit_day") or day_key),
                    "participants_count": int(live_participants),
                    "top_threshold": _daily_int(live.get("top_threshold") or 0, 0),
                    "top": [dict(x) for x in live_top if isinstance(x, dict)],
                },
            }
        elif cache is not None:
            merged_payload = dict(payload or {})
            merged_payload["submit_day"] = str(live.get("submit_day") or cache.get("submit_day") or day_key)
            merged_payload["participants_count"] = int(live_participants)
            merged_payload["top_threshold"] = _daily_int(live.get("top_threshold") or cache.get("top_threshold") or 0, 0)
            merged_payload["top"] = [dict(x) for x in live_top if isinstance(x, dict)]
            cache = dict(cache)
            cache["participants_count"] = int(live_participants)
            cache["top_threshold"] = _daily_int(live.get("top_threshold") or cache.get("top_threshold") or 0, 0)
            cache["payload"] = merged_payload

    return cache


def build_results_menu_kb(lang: str = "ru") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🏆 Итоги дня", callback_data="results:day")],
            [
                InlineKeyboardButton(text="👤 Мои итоги", callback_data="results:me"),
                InlineKeyboardButton(text=RESULTS_ARCHIVE, callback_data="results:archive:0"),
            ],
            [InlineKeyboardButton(text=HOME, callback_data="menu:back")],
        ]
    )


def build_back_to_results_kb(lang: str = "ru") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🏁 Итоги", callback_data="results:latest"),
                InlineKeyboardButton(text=HOME, callback_data="menu:back"),
            ]
        ]
    )


def _build_results_archive_kb(
    *, day_buttons: list[tuple[str, str]]
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for text, cb in day_buttons:
        kb.row(InlineKeyboardButton(text=text, callback_data=cb))
    kb.row(
        InlineKeyboardButton(text=HOME, callback_data="menu:back"),
        InlineKeyboardButton(text="⬅️ Назад", callback_data="results:latest"),
    )
    return kb.as_markup()


def _page_token(page: int | None) -> str:
    if page is None or page < 0:
        return "-1"
    return str(int(page))


def _parse_page_token(token: str | None) -> int | None:
    try:
        v = int(str(token or "-1"))
    except Exception:
        return None
    if v < 0:
        return None
    return v


def _archive_cb_from_page_token(page_token: str | None) -> str:
    page = _parse_page_token(page_token)
    return f"results:archive:{page or 0}"


def _hub_cb(day_key: str, page_token: str | None) -> str:
    return f"results:hub:{day_key}:{_page_token(_parse_page_token(page_token))}"


def _party_back_cb(page_token: str | None) -> str:
    page = _parse_page_token(page_token)
    if page is None:
        return "results:latest"
    return f"results:archive:{page}"


def _build_cache_wait_kb(*, refresh_cb: str, archive_cb: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="🔄 Обновить", callback_data=refresh_cb))
    kb.row(InlineKeyboardButton(text="📅 Архив", callback_data=archive_cb))
    kb.row(InlineKeyboardButton(text=HOME, callback_data="menu:back"))
    return kb.as_markup()


def _build_results_hub_kb(*, day_key: str, page_token: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="🏆 Итоги дня", callback_data=f"results:podium:{day_key}:1:{page_token}"))
    kb.row(InlineKeyboardButton(text="👤 Мои итоги", callback_data="results:me"))
    kb.row(InlineKeyboardButton(text="📅 Архив", callback_data=_archive_cb_from_page_token(page_token)))
    kb.row(InlineKeyboardButton(text=HOME, callback_data="menu:back"))
    return kb.as_markup()


def _build_podium_nav_kb(*, day_key: str, step: int, page_token: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if step == 1:
        kb.row(InlineKeyboardButton(text="➡️", callback_data=f"results:podium:{day_key}:2:{page_token}"))
    elif step == 2:
        kb.row(
            InlineKeyboardButton(text="⬅️", callback_data=f"results:podium:{day_key}:1:{page_token}"),
            InlineKeyboardButton(text="➡️", callback_data=f"results:podium:{day_key}:3:{page_token}"),
        )
    else:
        kb.row(
            InlineKeyboardButton(text="⬅️", callback_data=f"results:podium:{day_key}:2:{page_token}"),
            InlineKeyboardButton(text="➡️", callback_data=f"results:top10:{day_key}:{page_token}"),
        )
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=_party_back_cb(page_token)))
    return kb.as_markup()


def _build_top10_kb(*, day_key: str, page_token: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="🏆 Подиум", callback_data=f"results:podium:{day_key}:1:{page_token}"))
    kb.row(InlineKeyboardButton(text="👤 Мои итоги", callback_data="results:me"))
    kb.row(
        InlineKeyboardButton(text=HOME, callback_data="menu:back"),
        InlineKeyboardButton(text="⬅️ Назад", callback_data=_party_back_cb(page_token)),
    )
    return kb.as_markup()


def _fmt_daily_bayes(item: dict) -> str:
    value = item.get("bayes_score")
    if value is None:
        value = item.get("avg_score")
    if value is None:
        value = item.get("avg_rating")
    if value is None:
        value = item.get("score")
    try:
        return f"{float(value):.2f}"
    except Exception:
        return "—"


def _daily_votes(item: dict) -> int:
    return int(item.get("votes_count") or item.get("ratings_count") or 0)


def _daily_author(item: dict) -> str:
    return str(
        item.get("author_name")
        or item.get("user_name")
        or item.get("author")
        or "Автор"
    ).strip() or "Автор"


def _daily_title(item: dict) -> str:
    return str(item.get("title") or "Без названия").strip() or "Без названия"


def _daily_file_id(item: dict) -> str:
    return str(item.get("file_id") or "").strip()


def _daily_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _daily_int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _daily_created_at_key(item: dict) -> str:
    raw = str(item.get("created_at") or "").strip()
    if not raw:
        return "9999-12-31T23:59:59.999999"
    try:
        return datetime.fromisoformat(raw).isoformat()
    except Exception:
        return raw


def _daily_photo_id(item: dict) -> int:
    return _daily_int(item.get("photo_id") or item.get("id") or 10**18, 10**18)


def _daily_sort_key(item: dict) -> tuple[float, int, int, str, int]:
    bayes = _daily_float(
        item.get("bayes_score")
        if item.get("bayes_score") is not None
        else item.get("avg_score")
        if item.get("avg_score") is not None
        else item.get("avg_rating")
        if item.get("avg_rating") is not None
        else item.get("score"),
        0.0,
    )
    votes = _daily_int(item.get("votes_count") or item.get("ratings_count") or 0, 0)
    views = _daily_int(item.get("views_count") or item.get("views") or 0, 0)
    created_at = _daily_created_at_key(item)
    photo_id = _daily_photo_id(item)
    # 1) bayes DESC, 2) votes DESC, 3) views ASC, 4) created_at ASC, 5) photo_id ASC
    return (-bayes, -votes, views, created_at, photo_id)


def _daily_top(payload: dict) -> list[dict]:
    top = payload.get("top") or []
    if not isinstance(top, list):
        return []
    items = [dict(x) for x in top if isinstance(x, dict)]
    items.sort(key=_daily_sort_key)
    return items


def _render_results_hub_text(*, payload: dict, lang: str, from_archive: bool) -> str:
    submit_day = str(payload.get("submit_day") or "")
    participants = int(payload.get("participants_count") or 0)
    party_label = html.escape(format_party_label(submit_day, lang=lang, mode="full"), quote=False)
    if from_archive:
        title = f"🏁 <b>Итоги за {html.escape(format_day_short(submit_day, lang=lang), quote=False)}</b>"
    else:
        title = "🏁 <b>Итоги</b>"
    lines = [
        title,
        "",
        f"📅 <b>{party_label}</b>",
        f"👥 Участников: <b>{participants}</b>",
    ]
    if participants <= 0:
        lines.append("😶 Сегодня партия не набралась.")
    elif participants < PODIUM_MIN_PARTICIPANTS:
        lines.append("⚠️ Маленькая партия: показываем участников без мест.")
    elif participants < TOP10_DEFAULT:
        lines.append(f"✅ Мини-итоги: подиум + топ-{participants} (N = кол-во работ).")
    else:
        lines.append("🏆 Полные итоги: подиум + топ-10.")
    return "\n".join(lines)


async def _render_day_hub_screen(
    callback: CallbackQuery,
    *,
    day_key: str,
    cache: dict,
    page_token: str,
    from_archive: bool,
) -> None:
    user = await get_user_by_tg_id(int(callback.from_user.id))
    lang = _lang(user)
    payload = cache.get("payload") if isinstance(cache.get("payload"), dict) else {}
    merged_payload = dict(payload or {})
    merged_payload.setdefault("submit_day", cache.get("submit_day"))
    merged_payload.setdefault("participants_count", cache.get("participants_count"))
    text = _render_results_hub_text(payload=merged_payload, lang=lang, from_archive=from_archive)
    kb = _build_results_hub_kb(day_key=day_key, page_token=page_token)
    await _show_text(callback, text, kb)


async def _render_day_podium_screen(
    callback: CallbackQuery,
    *,
    day_key: str,
    cache: dict,
    step: int,
    page_token: str,
) -> None:
    payload = cache.get("payload") if isinstance(cache.get("payload"), dict) else {}
    merged_payload = dict(payload or {})
    merged_payload.setdefault("submit_day", cache.get("submit_day"))
    merged_payload.setdefault("participants_count", cache.get("participants_count"))
    participants = _daily_int(merged_payload.get("participants_count") or 0, 0)
    top = _daily_top(merged_payload)
    kb = _build_podium_nav_kb(day_key=day_key, step=step, page_token=page_token)
    if participants < PODIUM_MIN_PARTICIPANTS:
        await _show_text(
            callback,
            (
                f"⚠️ В этой партии всего {participants} работ — подиум не формируется.\n"
                "Открой Топ, чтобы посмотреть участников."
            ),
            kb,
        )
        return
    if not top:
        await _show_text(callback, "⏳ Топ партии пока формируется.", kb)
        return

    idx_map = {1: 2, 2: 1, 3: 0}
    place_map = {1: 3, 2: 2, 3: 1}
    item_idx = idx_map.get(step, 2)
    place = place_map.get(step, 3)
    if item_idx >= len(top):
        await _show_text(callback, "Пока недостаточно работ для этого места.", kb)
        return

    item = top[item_idx]
    title = html.escape(_daily_title(item), quote=False)
    author = html.escape(_daily_author(item), quote=False)
    party_short = html.escape(format_party_label(day_key, mode="short"), quote=False)
    votes = _daily_votes(item)
    bayes = _fmt_daily_bayes(item)
    medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(place, "🏅")
    lines = [
        f"{medal} <b>{place} место</b> · {party_short}",
        f"<code>\"{title}\"</code>",
        f"👤 Автор: <b>{author}</b>",
        f"⭐: <b>{bayes}</b> · 🗳: <b>{votes}</b>",
    ]
    views = item.get("views") or item.get("views_count")
    try:
        views_int = int(views or 0)
    except Exception:
        views_int = 0
    if views_int > 0:
        lines.append(f"👁: <b>{views_int}</b>")
    caption = "\n".join(lines)
    file_id = _daily_file_id(item)
    if file_id:
        await _show_photo(callback, file_id=file_id, caption=caption, kb=kb)
    else:
        await _show_text(callback, caption, kb)


async def _render_day_top10_screen(
    callback: CallbackQuery,
    *,
    day_key: str,
    cache: dict,
    page_token: str,
) -> None:
    payload = cache.get("payload") if isinstance(cache.get("payload"), dict) else {}
    merged_payload = dict(payload or {})
    merged_payload.setdefault("submit_day", cache.get("submit_day"))
    merged_payload.setdefault("participants_count", cache.get("participants_count"))
    participants = _daily_int(merged_payload.get("participants_count") or 0, 0)
    all_items = _daily_top(merged_payload)
    if participants <= 0:
        limit_n = 0
        title_text = "📊 <b>Топ</b>"
    elif participants < PODIUM_MIN_PARTICIPANTS:
        limit_n = participants
        title_text = "📊 <b>Участники</b>"
    elif participants < TOP10_DEFAULT:
        limit_n = participants
        title_text = f"📊 <b>Топ-{participants}</b>"
    else:
        limit_n = TOP10_DEFAULT
        title_text = "📊 <b>Топ-10</b>"
    top = all_items[: max(0, min(limit_n, len(all_items)))]
    party_short = html.escape(format_party_label(day_key, mode="short"), quote=False)
    lines: list[str] = [f"{title_text} · {party_short}"]
    if participants == 0:
        lines.extend(["<i>В этой партии нет работ.</i>", ""])
    elif participants < PODIUM_MIN_PARTICIPANTS:
        lines.append("")
    else:
        lines.extend(["<i>Рейтинг зафиксирован после окончания партии.</i>", ""])
    medal = {1: "🥇", 2: "🥈", 3: "🥉"}
    if participants < PODIUM_MIN_PARTICIPANTS:
        for item in top:
            title = html.escape(_daily_title(item), quote=False)
            bayes = _fmt_daily_bayes(item)
            votes = _daily_votes(item)
            lines.append(f"• <code>\"{title}\"</code> — ⭐ {bayes} · 🗳 {votes}")
        lines.append("")
        lines.append("<i>Партия маленькая, результаты менее стабильны.</i>")
    else:
        for i, item in enumerate(top, start=1):
            icon = medal.get(i, "•")
            title = html.escape(_daily_title(item), quote=False)
            bayes = _fmt_daily_bayes(item)
            votes = _daily_votes(item)
            lines.append(f"{icon} {i}. <code>\"{title}\"</code> — ⭐ {bayes} · 🗳 {votes}")
    lines.extend(
        [
            "",
            "💡 Хочешь больше оценок? Оценивай других → credits → твои фото покажут чаще.",
            "⚖️ Если рейтинги совпали, выше работа с большим числом оценок; дальше — по времени публикации.",
        ]
    )
    kb = _build_top10_kb(day_key=day_key, page_token=page_token)
    await _show_text(callback, "\n".join(lines), kb)


def build_alltime_menu_kb(mode: str, lang: str = "ru") -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📃 Топ 10", callback_data="results:alltime:top10")
    kb.button(text="📍 Где я?", callback_data="results:alltime:me")
    kb.button(text=RESULTS, callback_data="results:latest")
    kb.button(text=HOME, callback_data="menu:back")
    kb.adjust(1, 1, 2)
    return kb.as_markup()


def build_alltime_paged_kb(mode: str, page: int, max_page: int, lang: str = "ru") -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if page > 0:
        kb.button(text="⬅️", callback_data=f"results:{mode}:{page-1}")
    if page < max_page:
        kb.button(text="➡️", callback_data=f"results:{mode}:{page+1}")
    buttons = kb.export()
    if buttons:
        kb.adjust(len(buttons))
    kb.row(
        InlineKeyboardButton(text="🏁 Итоги", callback_data="results:latest"),
        InlineKeyboardButton(text=HOME, callback_data="menu:back"),
    )
    return kb.as_markup()


async def _show_text(callback: CallbackQuery, text: str, kb: InlineKeyboardMarkup) -> int | None:
    msg = callback.message
    try:
        if msg.photo:
            await msg.edit_caption(caption=text, reply_markup=kb, parse_mode="HTML")
        else:
            await msg.edit_text(text, reply_markup=kb, parse_mode="HTML")
        return int(msg.message_id)
    except Exception:
        pass

    try:
        await msg.delete()
    except Exception:
        pass

    try:
        sent = await msg.bot.send_message(
            chat_id=msg.chat.id,
            text=text,
            reply_markup=kb,
            parse_mode="HTML",
            disable_notification=True,
        )
        return int(sent.message_id)
    except Exception:
        try:
            sent = await msg.answer(text, reply_markup=kb, parse_mode="HTML")
            return int(sent.message_id)
        except Exception:
            return None


async def _show_photo(callback: CallbackQuery, file_id: str, caption: str, kb: InlineKeyboardMarkup) -> int | None:
    try:
        await callback.message.edit_media(
            media=InputMediaPhoto(media=file_id, caption=caption, parse_mode="HTML"),
            reply_markup=kb,
        )
        return int(callback.message.message_id)
    except Exception:
        pass

    try:
        await callback.message.delete()
    except Exception:
        pass

    try:
        sent = await callback.message.bot.send_photo(
            chat_id=callback.message.chat.id,
            photo=file_id,
            caption=caption,
            reply_markup=kb,
            parse_mode="HTML",
            disable_notification=True,
        )
        return int(sent.message_id)
    except Exception:
        return await _show_text(callback, caption, kb)


# ===== ALL-TIME =====


def _fmt_score(v) -> str:
    try:
        return f"{float(v):.2f}".rstrip("0").rstrip(".")
    except Exception:
        return str(v)


def _fmt_date_str(dt_str: str | None) -> str:
    if not dt_str:
        return ""
    try:
        dt = datetime.fromisoformat(str(dt_str).replace("Z", "+00:00"))
        return dt.strftime("%d.%m.%Y")
    except Exception:
        try:
            return str(dt_str)[:10]
        except Exception:
            return ""


def _safe_text(value: str | None) -> str:
    return html.escape(str(value or "").strip(), quote=False)


def _plain_text(value: str | None) -> str:
    return str(value or "").strip()


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    try:
        return ImageFont.truetype("DejaVuSans.ttf", size=size)
    except Exception:
        try:
            return ImageFont.truetype("arial.ttf", size=size)
        except Exception:
            return ImageFont.load_default()


def _calc_text_size(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> tuple[int, int]:
    try:
        bbox = draw.textbbox((0, 0), text, font=font)
        return bbox[2] - bbox[0], bbox[3] - bbox[1]
    except Exception:
        w, h = draw.textsize(text, font=font)
        return int(w), int(h)


def _truncate_to_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_w: int) -> str:
    if not text:
        return text
    if _calc_text_size(draw, text, font)[0] <= max_w:
        return text
    suffix = "…"
    trimmed = text
    while trimmed:
        trimmed = trimmed[:-1]
        candidate = trimmed + suffix
        if _calc_text_size(draw, candidate, font)[0] <= max_w:
            return candidate
    return suffix


def _fit_contain(img: Image.Image, max_w: int, max_h: int) -> Image.Image:
    w, h = img.size
    if w <= 0 or h <= 0:
        return img.resize((max_w, max_h))
    scale = min(float(max_w) / float(w), float(max_h) / float(h))
    new_w = max(1, int(w * scale))
    new_h = max(1, int(h * scale))
    return img.resize((new_w, new_h), Image.Resampling.LANCZOS)


async def _download_photo_bytes(bot: Bot, file_id: str) -> bytes | None:
    try:
        tg_file = await bot.get_file(file_id)
        buff = io.BytesIO()
        await bot.download_file(tg_file.file_path, destination=buff)
        return buff.getvalue()
    except Exception:
        return None


def _compose_alltime_podium_image(items: list[dict], images: list[bytes]) -> bytes | None:
    if len(items) < 3 or len(images) < 3:
        return None

    canvas_w, canvas_h = 1280, 720
    bg = Image.new("RGB", (canvas_w, canvas_h), "#000000")
    draw = ImageDraw.Draw(bg)

    # Bigger photos + tighter spacing (but keep the trio centered as a group)
    main_box = (560, 410)
    side_box = (420, 320)
    gap = 28
    margin_x = 48

    top_main = 44
    top_side = 168

    # Order items as 2nd, 1st, 3rd to build podium layout
    podium_images = [images[1], images[0], images[2]]

    frame_colors = {
        1: (212, 175, 55),   # gold
        2: (192, 192, 192),  # silver
        3: (205, 127, 50),   # bronze
    }
    frame_thickness = 8

    # First pass: decode + resize, so we can compute exact x positions and avoid hugging edges
    resized_imgs: dict[int, Image.Image] = {}
    sizes: dict[int, tuple[int, int]] = {}

    for place, (box, img_bytes) in zip([2, 1, 3], [(side_box, podium_images[0]), (main_box, podium_images[1]), (side_box, podium_images[2])]):
        try:
            img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        except Exception:
            continue
        max_w, max_h = box
        resized = _fit_contain(img, max_w, max_h)
        resized_imgs[place] = resized
        sizes[place] = resized.size

    # If something failed to decode, bail out
    if len(resized_imgs) < 3:
        return None

    w2, h2 = sizes[2]
    w1, h1 = sizes[1]
    w3, h3 = sizes[3]

    # Center the whole trio as a group. If it doesn't fit within margins, shrink the gap.
    available = (canvas_w - 2 * margin_x) - (w2 + w1 + w3)
    if available < 2 * gap:
        gap = max(10, available // 2)

    total_w = w2 + w1 + w3 + 2 * gap
    start_x = int((canvas_w - total_w) / 2)
    start_x = max(margin_x, start_x)

    # Exact left edges for each photo
    x2 = start_x
    x1 = x2 + w2 + gap
    x3 = x1 + w1 + gap

    positions = [
        {"left": x2, "top": top_side, "w": w2, "h": h2, "center": x2 + w2 / 2},  # 2nd
        {"left": x1, "top": top_main, "w": w1, "h": h1, "center": x1 + w1 / 2},  # 1st
        {"left": x3, "top": top_side, "w": w3, "h": h3, "center": x3 + w3 / 2},  # 3rd
    ]

    # Paste + frame
    for place, pos in zip([2, 1, 3], positions):
        resized = resized_imgs[place]
        x = int(pos["left"])
        y = int(pos["top"])
        bg.paste(resized, (x, y))

        color = frame_colors.get(place, (255, 255, 255))
        x1f, y1f = x, y
        x2f, y2f = x + resized.size[0], y + resized.size[1]
        for i in range(frame_thickness):
            draw.rectangle(
                (
                    max(0, x1f - i),
                    max(0, y1f - i),
                    min(canvas_w - 1, x2f + i),
                    min(canvas_h - 1, y2f + i),
                ),
                outline=color,
                width=1,
            )

    title_font = _load_font(26)
    author_font = _load_font(22)

    text_color = (255, 255, 255)
    author_color = (200, 200, 200)

    for place, pos in zip([2, 1, 3], positions):
        item = items[place - 1]
        title = _plain_text(item.get("title") or "Без названия")
        author = _plain_text(item.get("author") or item.get("author_name") or item.get("username") or "Автор")

        size = sizes.get(place)
        if size is None:
            # Fallback to stored w/h in positions if somehow missing
            box_w = int(pos.get("w") or 0)
            box_h = int(pos.get("h") or 0)
        else:
            box_w, box_h = int(size[0]), int(size[1])
        max_w = int(box_w + 40)
        line1 = f'"{title}"'
        line1 = _truncate_to_width(draw, line1, title_font, max_w)
        line2 = f"- {author}"
        line2 = _truncate_to_width(draw, line2, author_font, max_w)

        y_text = int(pos["top"] + box_h + 18)
        w1, h1 = _calc_text_size(draw, line1, title_font)
        w2, h2 = _calc_text_size(draw, line2, author_font)
        x1 = int(pos["center"] - w1 / 2)
        x2 = int(pos["center"] - w2 / 2)

        draw.text((x1, y_text), line1, font=title_font, fill=text_color)
        draw.text((x2, y_text + h1 + 6), line2, font=author_font, fill=author_color)

    out = io.BytesIO()
    bg.save(out, format="JPEG", quality=92, subsampling=0, optimize=True)
    return out.getvalue()


async def _build_alltime_podium_bytes(bot: Bot, items: list[dict]) -> bytes | None:
    if len(items) < 3:
        return None
    images: list[bytes] = []
    for it in items[:3]:
        file_id = it.get("file_id")
        if not file_id:
            return None
        data = await _download_photo_bytes(bot, str(file_id))
        if not data:
            return None
        images.append(data)
    return _compose_alltime_podium_image(items[:3], images)


def _alltime_payload_from_items(items: list[dict]) -> dict:
    payload_items: list[dict] = []
    for idx, it in enumerate(items[:3], start=1):
        payload_items.append(
            {
                "place": idx,
                "photo_id": it.get("photo_id"),
                "file_id": it.get("file_id"),
                "title": it.get("title"),
                "author": it.get("author_name") or it.get("username"),
                "author_name": it.get("author_name"),
                "username": it.get("username"),
                "bayes_score": it.get("bayes_score"),
                "ratings_count": it.get("ratings_count"),
            }
        )
    top10_items: list[dict] = []
    for idx, it in enumerate(items[:10], start=1):
        top10_items.append(
            {
                "place": idx,
                "photo_id": it.get("photo_id"),
                "file_id": it.get("file_id"),
                "title": it.get("title"),
                "author": it.get("author_name") or it.get("username"),
                "author_name": it.get("author_name"),
                "username": it.get("username"),
                "bayes_score": it.get("bayes_score"),
                "ratings_count": it.get("ratings_count"),
            }
        )
    return {"v": 2, "items": payload_items, "top10": top10_items}


def _format_alltime_item_lines(place: int, item: dict) -> list[str]:
    medal_map = {1: "🥇", 2: "🥈", 3: "🥉"}
    medal = medal_map.get(place, "🏅")
    title = _safe_text(item.get("title") or "Без названия")
    author = _safe_text(item.get("author") or item.get("author_name") or item.get("username") or "Автор")
    score = _fmt_score(item.get("bayes_score"))
    votes = int(item.get("ratings_count") or 0)
    return [
        f'{medal} "{title}" - {author}',
        f"-- 🔸 {score} · ⭐️ {votes}",
    ]


def _is_alltime_payload_current(payload: dict | None) -> bool:
    try:
        return int((payload or {}).get("v") or 0) >= 2
    except Exception:
        return False


async def _send_alltime_podium(
    callback: CallbackQuery,
    *,
    file_id: str | None,
    image_bytes: bytes | None,
    caption: str,
    kb: InlineKeyboardMarkup,
) -> str | None:
    msg = callback.message
    if msg is None:
        return None

    if file_id:
        try:
            await msg.edit_media(
                media=InputMediaPhoto(media=file_id, caption=caption, parse_mode="HTML"),
                reply_markup=kb,
            )
            return file_id
        except Exception:
            pass

        try:
            await msg.delete()
        except Exception:
            pass

        try:
            sent = await msg.bot.send_photo(
                chat_id=msg.chat.id,
                photo=file_id,
                caption=caption,
                reply_markup=kb,
                parse_mode="HTML",
                disable_notification=True,
            )
            if sent and sent.photo:
                return sent.photo[-1].file_id
        except Exception:
            pass

    if image_bytes:
        media = InputMediaPhoto(
            media=BufferedInputFile(image_bytes, filename="alltime_top3.jpg"),
            caption=caption,
            parse_mode="HTML",
        )
        try:
            sent = await msg.edit_media(media=media, reply_markup=kb)
            try:
                return sent.photo[-1].file_id  # type: ignore[union-attr]
            except Exception:
                return None
        except Exception:
            pass

        try:
            await msg.delete()
        except Exception:
            pass

        try:
            sent = await msg.bot.send_photo(
                chat_id=msg.chat.id,
                photo=BufferedInputFile(image_bytes, filename="alltime_top3.jpg"),
                caption=caption,
                reply_markup=kb,
                parse_mode="HTML",
                disable_notification=True,
            )
            if sent and sent.photo:
                return sent.photo[-1].file_id
        except Exception:
            pass

    await _show_text(callback, caption, kb)
    return None


async def _render_alltime_top(callback: CallbackQuery, limit: int = 3, page: int = 0) -> None:
    user = await get_user_by_tg_id(int(callback.from_user.id)) if callback.from_user else None
    lang = _lang(user)
    if limit == 3:
        today = get_moscow_today()
        try:
            day_key = today.isoformat()
        except Exception:
            day_key = str(today)

        cached = None
        try:
            cached = await get_alltime_cache(day_key, ALLTIME_CACHE_KIND_TOP3)
        except Exception:
            cached = None

        items: list[dict] = []
        cached_file_id: str | None = None
        payload = None
        if cached:
            cached_file_id = cached.get("file_id")
            payload = cached.get("payload") or {}
            if not _is_alltime_payload_current(payload):
                payload = None
                cached_file_id = None
                items = []
            else:
                items = payload.get("items") or []

        if len(items) < 3:
            try:
                payload = await refresh_alltime_cache_payload(day_key=day_key)
            except Exception:
                payload = None
            if payload:
                items = payload.get("items") or []
                cached = await get_alltime_cache(day_key, ALLTIME_CACHE_KIND_TOP3)
                if cached:
                    cached_file_id = cached.get("file_id")

        if len(items) < 3:
            try:
                top_items = await get_all_time_top(limit=50, min_votes=ALL_TIME_MIN_VOTES)
                await update_hall_of_fame_from_top(top_items)
            except Exception:
                top_items = []

            if not top_items:
                text = "🏆 За всё время\n\nПока недостаточно данных для рейтинга."
                await _show_text(callback, text, build_back_to_results_kb(lang))
                return

            payload = _alltime_payload_from_items(top_items)
            items = payload.get("items") or []
            cached_file_id = None

        if len(items) < 3:
            text = "🏆 За всё время\n\nПока недостаточно данных для рейтинга."
            await _show_text(callback, text, build_back_to_results_kb(lang))
            return

        lines: list[str] = ["🏆 За всё время", ""]
        for idx, it in enumerate(items[:3], start=1):
            lines.extend(_format_alltime_item_lines(idx, it))
        caption = "\n".join(lines)

        image_bytes = None
        if not cached_file_id and callback.message:
            image_bytes = await _build_alltime_podium_bytes(callback.message.bot, items)

        sent_file_id = await _send_alltime_podium(
            callback,
            file_id=cached_file_id,
            image_bytes=image_bytes,
            caption=caption,
            kb=build_alltime_menu_kb("top3", lang),
        )

        if not sent_file_id and cached_file_id and image_bytes is None and callback.message:
            image_bytes = await _build_alltime_podium_bytes(callback.message.bot, items)
            if image_bytes:
                sent_file_id = await _send_alltime_podium(
                    callback,
                    file_id=None,
                    image_bytes=image_bytes,
                    caption=caption,
                    kb=build_alltime_menu_kb("top3", lang),
                )

        try:
            await upsert_alltime_cache(
                day_key=day_key,
                kind=ALLTIME_CACHE_KIND_TOP3,
                file_id=sent_file_id or cached_file_id,
                payload=payload or {"items": items},
            )
        except Exception:
            pass
        return

    # Top-10 list
    today = get_moscow_today()
    try:
        day_key = today.isoformat()
    except Exception:
        day_key = str(today)

    cached = None
    try:
        cached = await get_alltime_cache(day_key, ALLTIME_CACHE_KIND_TOP3)
    except Exception:
        cached = None

    cached_file_id: str | None = None
    items_top3: list[dict] = []
    items_top10: list[dict] = []
    payload = None
    if cached:
        cached_file_id = cached.get("file_id")
        payload = cached.get("payload") or {}
        if not _is_alltime_payload_current(payload):
            payload = None
            cached_file_id = None
            items_top3 = []
            items_top10 = []
        else:
            items_top3 = payload.get("items") or []
            items_top10 = payload.get("top10") or []

    if not items_top10:
        try:
            payload = await refresh_alltime_cache_payload(day_key=day_key)
        except Exception:
            payload = None

        if payload:
            items_top3 = payload.get("items") or []
            items_top10 = payload.get("top10") or []
            cached = await get_alltime_cache(day_key, ALLTIME_CACHE_KIND_TOP3)
            if cached:
                cached_file_id = cached.get("file_id")
        else:
            try:
                top_items = await get_all_time_top(limit=10, min_votes=ALL_TIME_MIN_VOTES)
            except Exception:
                top_items = []

            if not top_items:
                text = "🏆 За всё время\n\nПока недостаточно данных для рейтинга."
                await _show_text(callback, text, build_back_to_results_kb(lang))
                return

            payload = _alltime_payload_from_items(top_items)
            items_top3 = payload.get("items") or []
            items_top10 = payload.get("top10") or []

            try:
                await upsert_alltime_cache(
                    day_key=day_key,
                    kind=ALLTIME_CACHE_KIND_TOP3,
                    file_id=cached_file_id,
                    payload=payload,
                )
            except Exception:
                pass

    if len(items_top3) < 3:
        text = "🏆 За всё время\n\nПока недостаточно данных для рейтинга."
        await _show_text(callback, text, build_back_to_results_kb(lang))
        return

    lines: list[str] = ["🏆 За всё время", ""]
    for idx, it in enumerate(items_top3[:3], start=1):
        lines.extend(_format_alltime_item_lines(idx, it))

    if len(items_top10) > 3:
        lines.append("")
    for idx, it in enumerate(items_top10[3:10], start=4):
        title = _safe_text(it.get("title") or "Без названия")
        author = _safe_text(it.get("author") or it.get("author_name") or it.get("username") or "Автор")
        lines.append(f'{idx}. "{title}" - {author}')

    image_bytes = None
    if not cached_file_id and callback.message:
        image_bytes = await _build_alltime_podium_bytes(callback.message.bot, items_top3)

    sent_file_id = await _send_alltime_podium(
        callback,
        file_id=cached_file_id,
        image_bytes=image_bytes,
        caption="\n".join(lines),
        kb=build_alltime_menu_kb("top10", lang),
    )

    if not sent_file_id and cached_file_id and image_bytes is None and callback.message:
        image_bytes = await _build_alltime_podium_bytes(callback.message.bot, items_top3)
        if image_bytes:
            sent_file_id = await _send_alltime_podium(
                callback,
                file_id=None,
                image_bytes=image_bytes,
                caption="\n".join(lines),
                kb=build_alltime_menu_kb("top10", lang),
            )

    try:
        await upsert_alltime_cache(
            day_key=day_key,
            kind=ALLTIME_CACHE_KIND_TOP3,
            file_id=sent_file_id or cached_file_id,
            payload=payload or {"items": items_top3, "top10": items_top10},
        )
    except Exception:
        pass


async def _render_hof(callback: CallbackQuery, page: int = 0) -> None:
    user = await get_user_by_tg_id(int(callback.from_user.id)) if callback.from_user else None
    lang = _lang(user)
    try:
        await refresh_hof_statuses()
        items = await get_hof_items(limit=50)
    except Exception:
        items = []

    if not items:
        await _show_text(callback, "👑 Зал славы\n\nПока пусто.", build_back_to_results_kb(lang))
        return

    per_page = 10
    max_page = max((len(items) - 1) // per_page, 0)
    page = max(0, min(page, max_page))
    chunk = items[page * per_page : (page + 1) * per_page]

    lines = ["👑 Зал славы", ""]
    for it in chunk:
        rank = int(it.get("best_rank") or 0)
        title = (it.get("title_snapshot") or "Без названия").strip()
        author = (it.get("author_snapshot") or "Автор").strip()
        score = _fmt_score(it.get("best_score"))
        votes = int(it.get("votes_at_best") or 0)
        achieved = _fmt_date_str(it.get("achieved_at"))
        status = str(it.get("status") or "active")
        status_line = ""
        if status == "deleted_by_author":
            status_line = " (фото удалено автором)"
        elif status == "hidden":
            status_line = " (фото скрыто)"
        elif status == "moderated":
            status_line = " (модерация)"
        lines.append(f"🏅 #{rank} — \"{title}\" — {author}{status_line}")
        lines.append(f"Рейтинг: {score} • Оценок: {votes} • Дата: {achieved}")
        lines.append("")

    kb = build_alltime_paged_kb("hof", page, max_page, lang)
    await _show_text(callback, "\n".join(lines).strip(), kb)


async def _render_alltime_me(callback: CallbackQuery) -> None:
    user = await get_user_by_tg_id(int(callback.from_user.id)) if callback.from_user else None
    lang = _lang(user)

    today = get_moscow_today()
    try:
        day_key = today.isoformat()
    except Exception:
        day_key = str(today)

    items: list[dict] = []
    cached_file_id: str | None = None
    payload = None
    try:
        cached = await get_alltime_cache(day_key, ALLTIME_CACHE_KIND_TOP3)
    except Exception:
        cached = None

    if cached:
        cached_file_id = cached.get("file_id")
        payload = cached.get("payload") or {}
        if not _is_alltime_payload_current(payload):
            payload = None
            cached_file_id = None
            items = []
        else:
            items = payload.get("items") or []

    if len(items) < 3:
        try:
            payload = await refresh_alltime_cache_payload(day_key=day_key)
        except Exception:
            payload = None

        if payload:
            items = payload.get("items") or []
            try:
                cached = await get_alltime_cache(day_key, ALLTIME_CACHE_KIND_TOP3)
            except Exception:
                cached = None
            if cached:
                cached_file_id = cached.get("file_id")
        else:
            try:
                top_items = await get_all_time_top(limit=50, min_votes=ALL_TIME_MIN_VOTES)
                await update_hall_of_fame_from_top(top_items)
            except Exception:
                top_items = []

            if top_items:
                payload = _alltime_payload_from_items(top_items)
                items = payload.get("items") or []
                try:
                    await upsert_alltime_cache(
                        day_key=day_key,
                        kind=ALLTIME_CACHE_KIND_TOP3,
                        file_id=cached_file_id,
                        payload=payload,
                    )
                except Exception:
                    pass

    if len(items) < 3:
        text = "🏆 За всё время\n\nПока недостаточно данных для рейтинга."
        await _show_text(callback, text, build_back_to_results_kb(lang))
        return

    lines: list[str] = ["🏆 За всё время", ""]
    for idx, it in enumerate(items[:3], start=1):
        lines.extend(_format_alltime_item_lines(idx, it))

    lines.append("")
    lines.append("Твои результаты:")

    user_rank = None
    user_id = None
    if user:
        try:
            user_id = int(user.get("id"))
        except Exception:
            user_id = None

    if user_id is not None:
        try:
            user_rank = await get_all_time_user_rank(user_id, min_votes=ALL_TIME_MIN_VOTES)
        except Exception:
            user_rank = None

    if not user_rank:
        lines.append("📍 Пока нет места в рейтинге.")
    else:
        title = _safe_text(user_rank.get("title") or "Без названия")
        rank_num = int(user_rank.get("rank_global") or 0)
        score = _fmt_score(user_rank.get("bayes_score"))
        votes = int(user_rank.get("ratings_count") or 0)
        lines.append(f'📍 "{title}"')
        lines.append(f"Место: {rank_num}")
        lines.append(f"-- 🔸 {score} · ⭐️ {votes}")

    kb = build_alltime_menu_kb("me", lang)
    await _show_text(callback, "\n".join(lines), kb)


async def _get_top_cached_day(day_key: str, scope_type: str, scope_key: str, limit: int = 10) -> list[dict]:
    items = await get_results_items(
        period=PERIOD_DAY,
        period_key=str(day_key),
        scope_type=str(scope_type),
        scope_key=str(scope_key),
        kind=KIND_TOP_PHOTOS,
        limit=int(limit),
    )

    # Convert results_v2 rows -> UI-friendly dicts
    out: list[dict] = []
    for it in items:
        payload = it.get("payload") or {}
        out.append(
            {
                "id": payload.get("photo_id") or it.get("photo_id"),
                "file_id": payload.get("file_id"),
                "title": payload.get("title") or "Без названия",
                "bayes_score": payload.get("bayes_score") or payload.get("avg_rating") or it.get("score"),
                "avg_rating": payload.get("avg_rating"),
                "ratings_count": payload.get("ratings_count") or payload.get("votes_count"),
                "votes_count": payload.get("votes_count") or payload.get("ratings_count"),
                "author_name": payload.get("author_name") or payload.get("user_name"),
                "user_name": payload.get("user_name") or payload.get("author_name"),
                "user_username": payload.get("user_username"),
                "rated_users": payload.get("rated_users"),
                "comments_count": payload.get("comments_count"),
                "super_count": payload.get("super_count"),
                "views_count": payload.get("views_count") or payload.get("views"),
                "score": it.get("score"),
            }
        )

    # If engine didn't fill payload yet (no file_id), treat as empty
    out = [x for x in out if x.get("file_id")]
    return out


# =========================
# Main menu entrypoint
# =========================

@router.callback_query(F.data == "results:menu")
async def results_menu(callback: CallbackQuery, state: FSMContext | None = None):
    try:
        blocked = await is_section_blocked("results")
    except Exception:
        blocked = False
    if blocked:
        sent_id = await _show_text(callback, await _blocked_section_text(), build_back_to_menu_kb())
        if sent_id is not None and state is not None:
            try:
                await remember_screen(callback.from_user.id, int(sent_id), state=state)
            except Exception:
                pass
        await callback.answer()
        return

    if not await require_user_name(callback):
        return
    await cleanup_previous_screen(
        callback.message.bot,
        callback.message.chat.id,
        callback.from_user.id,
        state=state,
        exclude_ids={callback.message.message_id},
    )
    user = await get_user_by_tg_id(int(callback.from_user.id))
    lang = _lang(user)
    try:
        await sync_giraffe_section_nav(
            callback.message.bot,
            callback.message.chat.id,
            callback.from_user.id,
            section="results",
            lang=lang,
        )
    except Exception:
        pass
    text = (
        "🏁 <b>Итоги</b>\n\n"
        "Здесь публикуются ежедневные результаты партии.\n"
        "Открой последние итоги или архив партий."
    )
    sent_id = await _show_text(callback, text, build_results_menu_kb(lang))
    if sent_id is None:
        await callback.answer()
        return
    try:
        await remember_screen(callback.from_user.id, int(sent_id), state=state)
    except Exception:
        pass
    await callback.answer()


@router.callback_query(F.data == "results:latest")
async def results_latest(callback: CallbackQuery):
    user = await get_user_by_tg_id(int(callback.from_user.id))
    lang = _lang(user)
    text = (
        "🏁 <b>Итоги</b>\n\n"
        "Выбери раздел ниже."
    )
    await _show_text(callback, text, build_results_menu_kb(lang))
    await callback.answer()


@router.callback_query(F.data.regexp(r"^results:archive:(\d+)$"))
async def results_archive(callback: CallbackQuery):
    rows = await list_daily_results_days(limit=6, offset=0)
    page_rows = rows[:6]

    lines: list[str] = ["📅 <b>Архив итогов</b>", ""]
    day_buttons: list[tuple[str, str]] = []
    if not page_rows:
        lines.append("В архиве пока нет дат.")
    else:
        for row in page_rows:
            day = str(row.get("submit_day") or "")
            participants = int(row.get("participants_count") or 0)
            short_label = html.escape(format_party_label(day, mode="short"), quote=False)
            day_short = html.escape(format_day_short(day), quote=False)
            lines.append(f"· Партия {short_label} · {day_short} - 👥 {participants}")
            day_buttons.append(
                (
                    f"{short_label}",
                    f"results:daycache:{day}",
                )
            )

    kb = _build_results_archive_kb(day_buttons=day_buttons)
    await _show_text(callback, "\n".join(lines), kb)
    await callback.answer()


@router.callback_query(F.data.regexp(r"^results:daycache:(\d{4}-\d{2}-\d{2})$"))
async def results_archive_day_simple(callback: CallbackQuery):
    parts = (callback.data or "").split(":")
    if len(parts) < 3:
        await callback.answer()
        return
    day = str(parts[2] or "")
    cache = await _get_day_cache_ready(day)
    if not cache:
        kb = _build_cache_wait_kb(
            refresh_cb=f"results:daycache:{day}",
            archive_cb="results:archive:0",
        )
        await _show_text(callback, "⏳ Итоги ещё готовятся или недоступны.\nПопробуй через минуту.", kb)
        await callback.answer()
        return
    await _render_day_podium_screen(
        callback,
        day_key=day,
        cache=cache,
        step=1,
        page_token="0",
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^results:daycache:(\d{4}-\d{2}-\d{2}):(\d+)$"))
async def results_archive_day(callback: CallbackQuery):
    parts = (callback.data or "").split(":")
    if len(parts) < 4:
        await callback.answer()
        return
    day = str(parts[2] or "")
    try:
        page = max(0, int(parts[3]))
    except Exception:
        page = 0

    cache = await _get_day_cache_ready(day)
    if not cache:
        kb = _build_cache_wait_kb(
            refresh_cb=f"results:daycache:{day}:{page}",
            archive_cb=f"results:archive:{page}",
        )
        await _show_text(callback, "⏳ Итоги ещё готовятся или недоступны.\nПопробуй через минуту.", kb)
        await callback.answer()
        return

    await _render_day_podium_screen(
        callback,
        day_key=day,
        cache=cache,
        step=1,
        page_token=_page_token(page),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^results:hub:(\d{4}-\d{2}-\d{2}):(-?\d+)$"))
async def results_hub_day(callback: CallbackQuery):
    parts = (callback.data or "").split(":")
    if len(parts) < 4:
        await callback.answer()
        return
    day = str(parts[2] or "")
    page_token = str(parts[3] or "-1")
    page = _parse_page_token(page_token)
    cache = await _get_day_cache_ready(day)
    if not cache:
        kb = _build_cache_wait_kb(
            refresh_cb=f"results:hub:{day}:{page_token}",
            archive_cb=f"results:archive:{page or 0}",
        )
        await _show_text(callback, "⏳ Итоги ещё готовятся или недоступны.\nПопробуй через минуту.", kb)
        await callback.answer()
        return
    await _render_day_hub_screen(
        callback,
        day_key=day,
        cache=cache,
        page_token=page_token,
        from_archive=(page is not None),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^results:podium:(\d{4}-\d{2}-\d{2}):([1-3]):(-?\d+)$"))
async def results_podium(callback: CallbackQuery):
    parts = (callback.data or "").split(":")
    if len(parts) < 5:
        await callback.answer()
        return
    day = str(parts[2] or "")
    try:
        step = int(parts[3])
    except Exception:
        step = 1
    page_token = str(parts[4] or "-1")
    page = _parse_page_token(page_token)
    cache = await _get_day_cache_ready(day)
    if not cache:
        kb = _build_cache_wait_kb(
            refresh_cb=f"results:podium:{day}:{step}:{page_token}",
            archive_cb=f"results:archive:{page or 0}",
        )
        await _show_text(callback, "⏳ Итоги ещё готовятся или недоступны.\nПопробуй через минуту.", kb)
        await callback.answer()
        return
    await _render_day_podium_screen(
        callback,
        day_key=day,
        cache=cache,
        step=step,
        page_token=page_token,
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^results:top10:(\d{4}-\d{2}-\d{2}):(-?\d+)$"))
async def results_top10_day(callback: CallbackQuery):
    parts = (callback.data or "").split(":")
    if len(parts) < 4:
        await callback.answer()
        return
    day = str(parts[2] or "")
    page_token = str(parts[3] or "-1")
    page = _parse_page_token(page_token)

    cache = await _get_day_cache_ready(day)
    if not cache:
        kb = _build_cache_wait_kb(
            refresh_cb=f"results:top10:{day}:{page_token}",
            archive_cb=f"results:archive:{page or 0}",
        )
        await _show_text(callback, "⏳ Итоги ещё готовятся или недоступны.\nПопробуй через минуту.", kb)
        await callback.answer()
        return

    await _render_day_top10_screen(
        callback,
        day_key=day,
        cache=cache,
        page_token=page_token,
    )
    await callback.answer()


@router.callback_query(F.data == "results:alltime")
async def results_alltime(callback: CallbackQuery):
    await _render_alltime_top(callback, limit=3)


@router.callback_query(F.data.startswith("results:alltime:top10"))
async def results_alltime_top10(callback: CallbackQuery):
    await _render_alltime_top(callback, limit=10)


@router.callback_query(F.data == "results:alltime:me")
async def results_alltime_me(callback: CallbackQuery):
    await _render_alltime_me(callback)


@router.callback_query(F.data == "results:hof")
async def results_hof(callback: CallbackQuery):
    await results_latest(callback)


@router.callback_query(F.data.startswith("results:hof:"))
async def results_hof_nav(callback: CallbackQuery):
    await results_latest(callback)


# =========================
# Day results (GLOBAL, v2)
# =========================


@router.callback_query(F.data == "results:day")
async def results_day(callback: CallbackQuery):
    user = await get_user_by_tg_id(int(callback.from_user.id))
    lang = _lang(user)
    now = get_moscow_now()
    if now.hour < 8:
        text = (
            "⏰ Итоги дня появляются каждый день после <b>08:00 по МСК</b>.\n\n"
            f"Сейчас: <b>{now.strftime('%H:%M')}</b>.\n"
            "Загляни чуть позже — мы публикуем итоги за позавчера."
        )
        await _show_text(callback, text, build_back_to_results_kb(lang))
        await callback.answer()
        return
    day_key = (now.date() - timedelta(days=2)).isoformat()
    cache = await _get_day_cache_ready(day_key)
    if not cache:
        kb = _build_cache_wait_kb(refresh_cb="results:day", archive_cb="results:archive:0")
        await _show_text(callback, "⏳ Итоги ещё готовятся или недоступны.\nПопробуй через минуту.", kb)
        await callback.answer()
        return
    await _render_day_podium_screen(
        callback,
        day_key=day_key,
        cache=cache,
        step=1,
        page_token="-1",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("results:day:"))
async def results_day_nav(callback: CallbackQuery):
    try:
        _, _, day_key, step_str = callback.data.split(":", 3)
        step = int(step_str)
    except Exception:
        await callback.answer()
        return

    if step < 0:
        step = 0
    if step > 4:
        step = 4
    if step == 0:
        cache = await _get_day_cache_ready(day_key)
        if not cache:
            kb = _build_cache_wait_kb(refresh_cb="results:day", archive_cb="results:archive:0")
            await _show_text(callback, "⏳ Итоги ещё готовятся или недоступны.\nПопробуй через минуту.", kb)
            await callback.answer()
            return
        await _render_day_hub_screen(
            callback,
            day_key=day_key,
            cache=cache,
            page_token="-1",
            from_archive=False,
        )
        await callback.answer()
        return
    if step in (1, 2, 3):
        cache = await _get_day_cache_ready(day_key)
        if not cache:
            kb = _build_cache_wait_kb(refresh_cb="results:day", archive_cb="results:archive:0")
            await _show_text(callback, "⏳ Итоги ещё готовятся или недоступны.\nПопробуй через минуту.", kb)
            await callback.answer()
            return
        await _render_day_podium_screen(
            callback,
            day_key=day_key,
            cache=cache,
            step=step,
            page_token="-1",
        )
        await callback.answer()
        return
    cache = await _get_day_cache_ready(day_key)
    if not cache:
        kb = _build_cache_wait_kb(refresh_cb="results:day", archive_cb="results:archive:0")
        await _show_text(callback, "⏳ Итоги ещё готовятся или недоступны.\nПопробуй через минуту.", kb)
        await callback.answer()
        return
    await _render_day_top10_screen(
        callback,
        day_key=day_key,
        cache=cache,
        page_token="-1",
    )
    await callback.answer()


# =========================
# Week / Me / City / Country — placeholders for v2
# (We'll add engines later: recalc_week_*, recalc_day_city, recalc_day_country, etc.)
# =========================

@router.callback_query(F.data == "results:week")
async def results_week(callback: CallbackQuery):
    kb = build_back_to_menu_kb()
    text = (
        "🗓 <b>Итоги недели</b>\n\n"
        "Этот раздел уже будет на новой системе (кэш + движок), но движок недели ещё не подключён.\n"
        "Скоро сделаем: топ недели, лучший фотограф недели, лучшие по городам/странам."
    )
    await _show_text(callback, text, kb)
    await callback.answer()


@router.callback_query(F.data == "results:me")
async def results_me(callback: CallbackQuery):
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=HOME, callback_data="menu:back"),
                InlineKeyboardButton(text="⬅️ Назад", callback_data="results:latest"),
            ]
        ]
    )
    text = (
        "👤 <b>Мои итоги</b>\n\n"
        "<i>Раздел в разработке.</i>"
    )
    await _show_text(callback, text, kb)
    await callback.answer()



@router.callback_query(F.data == "results:city")
async def results_city(callback: CallbackQuery):
    now = get_moscow_now()

    if now.hour < 8:
        kb = build_back_to_menu_kb()
        text = (
            "⏰ Итоги дня появляются каждый день после <b>08:00 по МСК</b>.\n\n"
            f"Сейчас: <b>{now.strftime('%H:%M')}</b>.\n"
            "Загляни чуть позже — публикуем итоги за позавчера."
        )
        await _show_text(callback, text, kb)
        await callback.answer()
        return

    tg_id = int(callback.from_user.id)
    city, _country = await _get_user_place(tg_id)

    if not city:
        kb = build_results_menu_kb()
        text = (
            "🏙 <b>Итоги города</b>\n\n"
            "У тебя не указан город в профиле.\n"
            "Зайди в профиль и укажи город — тогда откроются итоги города."
        )
        await _show_text(callback, text, kb)
        await callback.answer()
        return

    day_key = (now.date() - timedelta(days=2)).isoformat()

    top = await _get_top_cached_day(day_key, SCOPE_CITY, city, limit=10)

    if not top:
        kb = build_back_to_menu_kb()
        text = (
            f"🏙 <b>Итоги города: {city}</b>\n\n"
            "⏳ Итоги ещё готовятся или недоступны.\n"
            "Попробуй через минуту."
        )
        await _show_text(callback, text, kb)
        await callback.answer()
        return

    party = html.escape(format_party_label(day_key, mode="full"), quote=False)
    lines: list[str] = [f"🏙 <b>Топ-10 города {city}</b>", f"<b>{party}</b>", ""]
    for i, item in enumerate(top, start=1):
        medal = "🥇" if i == 1 else ("🥈" if i == 2 else ("🥉" if i == 3 else "▪️"))
        title = html.escape(str(item.get("title") or "Без названия"), quote=False)
        bayes = _fmt_daily_bayes(item)
        votes = _daily_votes(item)
        lines.append(f"{medal} {i}. <code>\"{title}\"</code> — ⭐ {bayes} · 🗳 {votes}")

    kb = build_back_to_menu_kb()
    await _show_text(callback, "\n".join(lines), kb)
    await callback.answer()



@router.callback_query(F.data == "results:country")
async def results_country(callback: CallbackQuery):
    now = get_moscow_now()

    if now.hour < 8:
        kb = build_back_to_menu_kb()
        text = (
            "⏰ Итоги дня появляются каждый день после <b>08:00 по МСК</b>.\n\n"
            f"Сейчас: <b>{now.strftime('%H:%M')}</b>.\n"
            "Загляни чуть позже — публикуем итоги за позавчера."
        )
        await _show_text(callback, text, kb)
        await callback.answer()
        return

    tg_id = int(callback.from_user.id)
    _city, country = await _get_user_place(tg_id)

    if not country:
        kb = build_results_menu_kb()
        text = (
            "🌍 <b>Итоги страны</b>\n\n"
            "У тебя не указана страна в профиле.\n"
            "Укажи город в профиле — страна подтянется автоматически."
        )
        await _show_text(callback, text, kb)
        await callback.answer()
        return

    day_key = (now.date() - timedelta(days=2)).isoformat()

    top = await _get_top_cached_day(day_key, SCOPE_COUNTRY, country, limit=10)

    if not top:
        kb = build_back_to_menu_kb()
        text = (
            f"🌍 <b>Итоги страны: {country}</b>\n\n"
            "⏳ Итоги ещё готовятся или недоступны.\n"
            "Попробуй через минуту."
        )
        await _show_text(callback, text, kb)
        await callback.answer()
        return

    party = html.escape(format_party_label(day_key, mode="full"), quote=False)
    lines: list[str] = [f"🌍 <b>Топ-10 страны {country}</b>", f"<b>{party}</b>", ""]
    for i, item in enumerate(top, start=1):
        medal = "🥇" if i == 1 else ("🥈" if i == 2 else ("🥉" if i == 3 else "▪️"))
        title = html.escape(str(item.get("title") or "Без названия"), quote=False)
        bayes = _fmt_daily_bayes(item)
        votes = _daily_votes(item)
        lines.append(f"{medal} {i}. <code>\"{title}\"</code> — ⭐ {bayes} · 🗳 {votes}")

    kb = build_back_to_menu_kb()
    await _show_text(callback, "\n".join(lines), kb)
    await callback.answer()
