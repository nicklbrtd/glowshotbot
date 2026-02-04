from __future__ import annotations

from typing import Any

from datetime import datetime, timedelta

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InputMediaPhoto, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

from keyboards.common import build_back_to_menu_kb, ensure_section_reply_kb
from utils.i18n import t
from utils.time import get_moscow_now, get_moscow_today

from database_results import (
    PERIOD_DAY,
    PERIOD_ALL_TIME,
    SCOPE_GLOBAL,
    SCOPE_CITY,
    SCOPE_COUNTRY,
    KIND_TOP_PHOTOS,
    ALL_TIME_MIN_VOTES,
    get_results_items,
    get_all_time_top,
    update_hall_of_fame_from_top,
    get_hof_items,
    refresh_hof_statuses,
)

from services.results_engine import recalc_day_global, get_day_eligibility
from database import get_user_by_tg_id


try:
    from services.results_engine import recalc_day_city, recalc_day_country
except Exception:  # pragma: no cover
    recalc_day_city = None  # type: ignore
    recalc_day_country = None  # type: ignore



router = Router()

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

def build_results_menu_kb(lang: str = "ru") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=t("results.btn.day", lang), callback_data="results:day"),
                InlineKeyboardButton(text=t("results.btn.me", lang), callback_data="results:me"),
                InlineKeyboardButton(text="🏆 За всё время", callback_data="results:alltime"),
            ],
            [InlineKeyboardButton(text="👑 Зал славы", callback_data="results:hof")],
            [
                InlineKeyboardButton(text=t("common.menu", lang), callback_data="menu:back"),
            ],
        ]
    )


def build_back_to_results_kb(lang: str = "ru") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t("results.btn.back_results", lang), callback_data="results:menu")],
        ]
    )


def build_alltime_menu_kb(mode: str, lang: str = "ru") -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if mode != "top10":
        kb.button(text="📜 Топ 10", callback_data="results:alltime:top10")
    if mode != "top50":
        kb.button(text="📜 Топ 50", callback_data="results:alltime:top50")
    kb.button(text="👑 Зал славы", callback_data="results:hof")
    kb.button(text=t("results.btn.back_results", lang), callback_data="results:menu")
    kb.adjust(1)
    return kb.as_markup()


def build_alltime_paged_kb(mode: str, page: int, max_page: int, lang: str = "ru") -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if page > 0:
        kb.button(text="⬅️", callback_data=f"results:{mode}:{page-1}")
    if page < max_page:
        kb.button(text="➡️", callback_data=f"results:{mode}:{page+1}")
    kb.button(text=t("results.btn.back_results", lang), callback_data="results:alltime")
    buttons = kb.export()
    kb.adjust(len(buttons) if buttons else 1)
    return kb.as_markup()


def build_day_nav_kb(day_key: str, step: int, lang: str = "ru") -> InlineKeyboardMarkup:
    """
    step 0: заставка — «Вперёд», «В меню»
    step 1–3: 3/2/1 места — «Назад», «Вперёд»
    step 4: топ-10 — «Назад», «В меню»
    """
    if step <= 0:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text=t("common.menu", lang), callback_data="menu:back"),
                    InlineKeyboardButton(text=t("results.btn.forward", lang), callback_data=f"results:day:{day_key}:1"),
                ]
            ]
        )

    if 1 <= step <= 3:
        prev_step = step - 1
        next_step = step + 1
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text=t("results.btn.back", lang), callback_data=f"results:day:{day_key}:{prev_step}"),
                    InlineKeyboardButton(text=t("results.btn.forward", lang), callback_data=f"results:day:{day_key}:{next_step}"),
                ]
            ]
        )

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=t("results.btn.back", lang), callback_data=f"results:day:{day_key}:3"),
                InlineKeyboardButton(text=t("common.menu", lang), callback_data="menu:back"),
            ]
        ]
    )


async def _show_text(callback: CallbackQuery, text: str, kb: InlineKeyboardMarkup) -> None:
    msg = callback.message
    try:
        if msg.photo:
            await msg.edit_caption(caption=text, reply_markup=kb, parse_mode="HTML")
        else:
            await msg.edit_text(text, reply_markup=kb, parse_mode="HTML")
        return
    except Exception:
        pass

    try:
        await msg.delete()
    except Exception:
        pass

    try:
        await msg.bot.send_message(
            chat_id=msg.chat.id,
            text=text,
            reply_markup=kb,
            parse_mode="HTML",
            disable_notification=True,
        )
    except Exception:
        try:
            await msg.answer(text, reply_markup=kb, parse_mode="HTML")
        except Exception:
            pass


async def _show_photo(callback: CallbackQuery, file_id: str, caption: str, kb: InlineKeyboardMarkup) -> None:
    try:
        await callback.message.edit_media(
            media=InputMediaPhoto(media=file_id, caption=caption, parse_mode="HTML"),
            reply_markup=kb,
        )
        return
    except Exception:
        pass

    try:
        await callback.message.delete()
    except Exception:
        pass

    try:
        await callback.message.bot.send_photo(
            chat_id=callback.message.chat.id,
            photo=file_id,
            caption=caption,
            reply_markup=kb,
            parse_mode="HTML",
            disable_notification=True,
        )
    except Exception:
        await _show_text(callback, caption, kb)


def _label_for_day(day_key: str) -> str:
    now = get_moscow_now()
    today = get_moscow_today()
    try:
        today_key = today.isoformat()
    except Exception:
        today_key = str(today)

    yesterday = (now.date() - timedelta(days=1)).isoformat()

    if day_key == today_key:
        return "сегодняшнего дня"
    if day_key == yesterday:
        return "вчерашнего дня"
    return f"дня {day_key}"


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


async def _render_alltime_top(callback: CallbackQuery, limit: int = 3, page: int = 0) -> None:
    user = await get_user_by_tg_id(int(callback.from_user.id)) if callback.from_user else None
    lang = _lang(user)
    # Refresh HoF lazily
    try:
        top_items = await get_all_time_top(limit=max(50, limit), min_votes=ALL_TIME_MIN_VOTES)
        await update_hall_of_fame_from_top(top_items)
    except Exception:
        top_items = []

    if not top_items:
        text = "🏆 За всё время\n\nПока недостаточно данных для рейтинга."
        await _show_text(callback, text, build_back_to_results_kb(lang))
        return

    if limit == 3:
        items = top_items[:3]
        lines = ["🏆 За всё время", ""]
        for idx, it in enumerate(items, start=1):
            title = (it.get("title") or "Без названия").strip()
            author = (it.get("author_name") or it.get("username") or "").strip()
            score = _fmt_score(it.get("bayes_score"))
            votes = int(it.get("ratings_count") or 0)
            pub = _fmt_date_str(it.get("created_at"))
            line = f"{idx}. \"{title}\" — {author or 'Автор'}\nРейтинг: {score} • v={votes}"
            if pub:
                line += f" • {pub}"
            lines.append(line)
            lines.append("")

        kb = build_alltime_menu_kb("top3", lang)
        await _show_text(callback, "\n".join(lines).strip(), kb)
        return

    # paged top10/50 list
    items = top_items[:limit]
    per_page = 10
    max_page = max((len(items) - 1) // per_page, 0)
    page = max(0, min(page, max_page))
    chunk = items[page * per_page : (page + 1) * per_page]

    lines = ["🏆 За всё время", ""]
    for idx, it in enumerate(chunk, start=page * per_page + 1):
        title = (it.get("title") or "Без названия").strip()
        author = (it.get("author_name") or it.get("username") or "").strip()
        score = _fmt_score(it.get("bayes_score"))
        votes = int(it.get("ratings_count") or 0)
        line = f"{idx}. \"{title}\" — {author or 'Автор'} — {score} — v={votes}"
        lines.append(line)

    kb = build_alltime_paged_kb("alltime:top10" if limit == 10 else "alltime:top50", page, max_page, lang)
    await _show_text(callback, "\n".join(lines), kb)


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
                "avg_rating": payload.get("avg_rating"),
                "ratings_count": payload.get("ratings_count"),
                "user_name": payload.get("user_name"),
                "user_username": payload.get("user_username"),
                "rated_users": payload.get("rated_users"),
                "comments_count": payload.get("comments_count"),
                "super_count": payload.get("super_count"),
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
async def results_menu(callback: CallbackQuery, state: FSMContext):
    user = await get_user_by_tg_id(int(callback.from_user.id))
    lang = _lang(user)
    await ensure_section_reply_kb(
        bot=callback.message.bot,
        chat_id=callback.message.chat.id,
        state=state,
        lang=lang,
    )
    kb = build_results_menu_kb(lang)
    text = (
        "🏁 <b>Итоги</b>\n\n"
        "Доступны:\n"
        "• 📅 Итоги дня\n"
        "• 🏆 За всё время\n"
        "• 👑 Зал славы\n"
        "• 👤 Мои итоги (soon)\n\n"
        "Остальные разделы подключим позже."
    )
    try:
        sent = await callback.message.bot.send_message(
            chat_id=callback.message.chat.id,
            text=text,
            reply_markup=kb,
            parse_mode="HTML",
            disable_notification=True,
        )
    except Exception:
        sent = await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")

    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer()


@router.callback_query(F.data == "results:alltime")
async def results_alltime(callback: CallbackQuery):
    await _render_alltime_top(callback, limit=3)


@router.callback_query(F.data.startswith("results:alltime:top10"))
async def results_alltime_top10(callback: CallbackQuery):
    parts = (callback.data or "").split(":")
    page = 0
    if len(parts) == 4:
        try:
            page = int(parts[3])
        except Exception:
            page = 0
    await _render_alltime_top(callback, limit=10, page=page)


@router.callback_query(F.data.startswith("results:alltime:top50"))
async def results_alltime_top50(callback: CallbackQuery):
    parts = (callback.data or "").split(":")
    page = 0
    if len(parts) == 4:
        try:
            page = int(parts[3])
        except Exception:
            page = 0
    await _render_alltime_top(callback, limit=50, page=page)


@router.callback_query(F.data == "results:hof")
async def results_hof(callback: CallbackQuery):
    await _render_hof(callback, page=0)


@router.callback_query(F.data.startswith("results:hof:"))
async def results_hof_nav(callback: CallbackQuery):
    parts = (callback.data or "").split(":")
    page = 0
    if len(parts) == 3:
        try:
            page = int(parts[2])
        except Exception:
            page = 0
    await _render_hof(callback, page=page)


# =========================
# Day results (GLOBAL, v2)
# =========================


async def _ensure_day_global_cached(day_key: str) -> None:
    # Try read cache first
    items = await _get_top_cached_day(day_key, SCOPE_GLOBAL, "global", limit=10)
    if items:
        return

    # No cache -> compute now (fast enough for MVP)
    await recalc_day_global(day_key=str(day_key), limit=10)


async def _ensure_day_city_cached(day_key: str, city: str) -> None:
    items = await _get_top_cached_day(day_key, SCOPE_CITY, city, limit=10)
    if items:
        return
    if recalc_day_city is None:
        raise RuntimeError("City results engine is not implemented (recalc_day_city missing)")
    await recalc_day_city(day_key=str(day_key), city=str(city), limit=10)


async def _ensure_day_country_cached(day_key: str, country: str) -> None:
    items = await _get_top_cached_day(day_key, SCOPE_COUNTRY, country, limit=10)
    if items:
        return
    if recalc_day_country is None:
        raise RuntimeError("Country results engine is not implemented (recalc_day_country missing)")
    await recalc_day_country(day_key=str(day_key), country=str(country), limit=10)


async def _render_results_day(callback: CallbackQuery, day_key: str, step: int) -> None:
    label = _label_for_day(day_key)
    user = await get_user_by_tg_id(int(callback.from_user.id))
    lang = _lang(user)
    kb_back_menu = build_back_to_results_kb(lang)

    # Ensure cache exists (global)
    try:
        await _ensure_day_global_cached(day_key)
    except Exception:
        text = (
            "🔥 <b>Итоги дня</b>\n\n"
            "Пока не могу загрузить итоги. Попробуй ещё раз через минуту.\n"
            "<i>Если повторяется — значит движок/кэш упали, глянь логи.</i>"
        )
        await _show_text(callback, text, kb_back_menu)
        await callback.answer()
        return

    top = await _get_top_cached_day(day_key, SCOPE_GLOBAL, "global", limit=10)
    if not top:
        text = (
            f"📭 За {label} пока нет ни одной фотографии, прошедшей порог участия.\n\n"
            "Итоги появятся, когда работам поставят достаточно оценок."
        )
        await _show_text(callback, text, kb_back_menu)
        await callback.answer()
        return

    nav_kb = build_day_nav_kb(day_key, step, lang)

    # step 0: intro screen
    if step <= 0:
        try:
            day_dt = datetime.fromisoformat(day_key)
            day_str = day_dt.strftime("%d.%m.%Y")
        except Exception:
            day_str = day_key

        today_str = get_moscow_now().date().strftime("%d.%m.%Y")

        text = (
            f"📅 <b>Итоги дня ({day_str})</b>\n"
            f"Сегодня: {today_str}\n\n"
            "Нажимай «Вперёд», чтобы увидеть:\n"
            "• 🥉 3 место дня\n"
            "• 🥈 2 место дня\n"
            "• 🥇 1 место дня\n"
            "• 📊 Топ-10 фотографий дня"
        )
        await _show_text(callback, text, nav_kb)
        await callback.answer()
        return

    total = len(top)

    # steps 1–3: 3rd/2nd/1st places
    if step in (1, 2, 3):
        if step == 1:
            place_num = 3
            if total < 3:
                await _show_text(callback, f"ℹ️ За {label} недостаточно работ для 3 места.", nav_kb)
                await callback.answer()
                return
            item = top[2]
        elif step == 2:
            place_num = 2
            if total < 2:
                await _show_text(callback, f"ℹ️ За {label} недостаточно работ для 2 места.", nav_kb)
                await callback.answer()
                return
            item = top[1]
        else:
            place_num = 1
            item = top[0]

        author_name = (item.get("user_name") or "").strip()
        username = item.get("user_username")

        if username:
            link_text = author_name or f"@{username}"
            author_display = f'<a href="https://t.me/{username}">{link_text}</a>'
        elif author_name:
            author_display = author_name
        else:
            author_display = "Неизвестный автор"

        avg = item.get("avg_rating")
        if avg is not None:
            try:
                avg_str = f"{float(avg):.2f}".rstrip("0").rstrip(".")
            except Exception:
                avg_str = str(avg)
        else:
            avg_str = "—"

        medal_map = {1: "🥇", 2: "🥈", 3: "🥉"}
        medal = medal_map.get(place_num, "🏅")

        caption = "\n".join(
            [
                f"{medal} <b>{place_num} место {label}</b>",
                "",
                f"<code>\"{item.get('title') or 'Без названия'}\"</code>",
                f"Автор: {author_display}",
                "",
                f"Рейтинг: <b>{avg_str}</b>",
            ]
        )

        await _show_photo(callback, file_id=str(item["file_id"]), caption=caption, kb=nav_kb)
        await callback.answer()
        return

    # step 4: top-10 text
    lines: list[str] = [f"📊 <b>Топ-10 фотографий {label}</b>", ""]
    for i, item in enumerate(top, start=1):
        medal = "🥇" if i == 1 else ("🥈" if i == 2 else ("🥉" if i == 3 else "▪️"))

        avg = item.get("avg_rating")
        if avg is not None:
            try:
                avg_str = f"{float(avg):.2f}".rstrip("0").rstrip(".")
            except Exception:
                avg_str = str(avg)
        else:
            avg_str = "—"

        title = item.get("title") or "Без названия"
        if i <= 3:
            lines.append(f"{medal} {i} место — <b>\"{title}\"</b>")
        else:
            lines.append(f"{medal} {i} место — <b>\"{title}\"</b>")
            lines.append(f"    рейтинг: <b>{avg_str}</b>")

        if i == 3 and len(top) > 3:
            lines.append("")

    nav = build_day_nav_kb(day_key, step=4, lang=lang)
    await _show_text(callback, "\n".join(lines), nav)
    await callback.answer()


@router.callback_query(F.data == "results:day")
async def results_day(callback: CallbackQuery):
    """
    Итоги дня показываем за вчерашний календарный день по Москве,
    и показываем только после 07:00 МСК (как у тебя было).
    """
    # Если пользователь ещё не допущен — показываем чеклист.
    user = await get_user_by_tg_id(int(callback.from_user.id))
    lang = _lang(user)
    elig = await get_day_eligibility(int(callback.from_user.id))
    if not elig.get("eligible"):
        kb = build_back_to_results_kb(lang)
        lines = ["🔥 <b>Итоги дня</b>", "", "Чтобы участвовать, выполни условия:"]
        for c in elig.get("checks", []):
            mark = "✅" if c.get("ok") else "❌"
            extra = f" ({c.get('value')} из 2)" if c.get("value") is not None else ""
            lines.append(f"{mark} {c.get('title')}{extra}")
        note = elig.get("note_best_photo")
        if note:
            lines.append("")
            lines.append(note)
        await _show_text(callback, "\n".join(lines), kb)
        await callback.answer()
        return

    now = get_moscow_now()

    if now.hour < 7:
        kb = build_back_to_menu_kb()
        text = (
            "⏰ Итоги дня появляются каждый день после <b>07:00 по МСК</b>.\n\n"
            f"Сейчас: <b>{now.strftime('%H:%M')}</b>.\n"
            "Загляни чуть позже — мы подсчитаем все оценки за вчера."
        )
        await _show_text(callback, text, build_back_to_results_kb(lang))
        await callback.answer()
        return

    day_key = (now.date() - timedelta(days=1)).isoformat()
    await _render_results_day(callback, day_key, step=0)


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

    await _render_results_day(callback, day_key, step)


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
    kb = build_results_menu_kb()
    text = (
        "👤 <b>Мои итоги</b>\n\n"
        "Сделаем на новой системе красиво:\n"
        "• моё лучшее фото вчера\n"
        "• место среди всех/города/страны\n"
        "• прогресс и ранги\n\n"
        "<i>Пока этот раздел в разработке.</i>"
    )
    await _show_text(callback, text, kb)
    await callback.answer()



@router.callback_query(F.data == "results:city")
async def results_city(callback: CallbackQuery):
    now = get_moscow_now()

    if now.hour < 7:
        kb = build_back_to_menu_kb()
        text = (
            "⏰ Итоги дня появляются каждый день после <b>07:00 по МСК</b>.\n\n"
            f"Сейчас: <b>{now.strftime('%H:%M')}</b>.\n"
            "Загляни чуть позже — мы подсчитаем все оценки за вчера."
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

    day_key = (now.date() - timedelta(days=1)).isoformat()

    try:
        await _ensure_day_city_cached(day_key, city)
    except Exception:
        kb = build_back_to_menu_kb()
        text = (
            "🏙 <b>Итоги города</b>\n\n"
            "Пока не могу загрузить итоги города. Попробуй ещё раз через минуту."
        )
        await _show_text(callback, text, kb)
        await callback.answer()
        return

    top = await _get_top_cached_day(day_key, SCOPE_CITY, city, limit=10)

    if not top:
        kb = build_back_to_menu_kb()
        text = (
            f"🏙 <b>Итоги города: {city}</b>\n\n"
            "Пока итоги города недоступны.\n"
            "Условия: в городе должно быть <b>минимум 5 активных авторов</b> и\n"
            "каждая работа должна набрать <b>10+ оценок</b>."
        )
        await _show_text(callback, text, kb)
        await callback.answer()
        return

    label = _label_for_day(day_key)
    lines: list[str] = [f"🏙 <b>Топ-10 города {city} за {label}</b>", ""]
    for i, item in enumerate(top, start=1):
        medal = "🥇" if i == 1 else ("🥈" if i == 2 else ("🥉" if i == 3 else "▪️"))
        title = item.get("title") or "Без названия"
        avg = item.get("avg_rating")
        if avg is not None:
            try:
                avg_str = f"{float(avg):.2f}".rstrip("0").rstrip(".")
            except Exception:
                avg_str = str(avg)
        else:
            avg_str = "—"
        lines.append(f"{medal} {i} — <b>\"{title}\"</b> (рейтинг: <b>{avg_str}</b>)")

    kb = build_back_to_menu_kb()
    await _show_text(callback, "\n".join(lines), kb)
    await callback.answer()



@router.callback_query(F.data == "results:country")
async def results_country(callback: CallbackQuery):
    now = get_moscow_now()

    if now.hour < 7:
        kb = build_back_to_menu_kb()
        text = (
            "⏰ Итоги дня появляются каждый день после <b>07:00 по МСК</b>.\n\n"
            f"Сейчас: <b>{now.strftime('%H:%M')}</b>.\n"
            "Загляни чуть позже — мы подсчитаем все оценки за вчера."
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

    day_key = (now.date() - timedelta(days=1)).isoformat()

    try:
        await _ensure_day_country_cached(day_key, country)
    except Exception:
        kb = build_back_to_menu_kb()
        text = (
            "🌍 <b>Итоги страны</b>\n\n"
            "Пока не могу загрузить итоги страны. Попробуй ещё раз через минуту."
        )
        await _show_text(callback, text, kb)
        await callback.answer()
        return

    top = await _get_top_cached_day(day_key, SCOPE_COUNTRY, country, limit=10)

    if not top:
        kb = build_back_to_menu_kb()
        text = (
            f"🌍 <b>Итоги страны: {country}</b>\n\n"
            "Пока итоги страны недоступны.\n"
            "Условия: в стране должно быть <b>минимум 100 активных авторов</b> и\n"
            "каждая работа должна набрать <b>25+ оценок</b>."
        )
        await _show_text(callback, text, kb)
        await callback.answer()
        return

    label = _label_for_day(day_key)
    lines: list[str] = [f"🌍 <b>Топ-10 страны {country} за {label}</b>", ""]
    for i, item in enumerate(top, start=1):
        medal = "🥇" if i == 1 else ("🥈" if i == 2 else ("🥉" if i == 3 else "▪️"))
        title = item.get("title") or "Без названия"
        avg = item.get("avg_rating")
        if avg is not None:
            try:
                avg_str = f"{float(avg):.2f}".rstrip("0").rstrip(".")
            except Exception:
                avg_str = str(avg)
        else:
            avg_str = "—"
        lines.append(f"{medal} {i} — <b>\"{title}\"</b> (рейтинг: <b>{avg_str}</b>)")

    kb = build_back_to_menu_kb()
    await _show_text(callback, "\n".join(lines), kb)
    await callback.answer()
