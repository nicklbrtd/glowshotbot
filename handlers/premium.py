from __future__ import annotations

from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.exceptions import TelegramBadRequest
from datetime import datetime, timedelta

from utils.time import get_moscow_now
from database import (
    get_premium_news_since,
    get_user_by_tg_id,
    get_premium_benefits,
)
from database import get_user_premium_status, is_user_premium_active
from utils.i18n import t
from aiogram.utils.keyboard import InlineKeyboardBuilder
import html
from utils.ui import remember_screen

router = Router(name="premium")

_PREMIUM_EXPIRY_CACHE: dict[int, str] = {}


async def _safe_edit_or_send(
    callback: CallbackQuery,
    *,
    text: str,
    reply_markup: InlineKeyboardMarkup,
    parse_mode: str = "HTML",
) -> None:
    """Edit current message; if it no longer exists, send a fresh screen."""
    try:
        await callback.message.edit_text(
            text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )
        return
    except TelegramBadRequest as e:
        msg = str(e).lower()
        if "message is not modified" in msg:
            return
        recoverable = (
            "message to edit not found" in msg
            or "message can't be edited" in msg
            or "there is no text in the message to edit" in msg
        )
        if not recoverable:
            raise
    except Exception:
        pass

    sent = await callback.message.bot.send_message(
        chat_id=callback.message.chat.id,
        text=text,
        reply_markup=reply_markup,
        parse_mode=parse_mode,
        disable_notification=True,
    )
    try:
        await remember_screen(callback.from_user.id, sent.message_id)
    except Exception:
        pass


def _get_lang(user: object | None) -> str:
    """Return "ru" or "en".

    Works with dicts AND asyncpg.Record-like objects.
    Accepts values like "en-US" / "ru-RU" and normalizes to "en"/"ru".
    """
    if not user:
        return "ru"

    keys = ("lang", "language", "language_code", "locale")

    def _read(u: object, k: str):
        # dict-style
        try:
            if isinstance(u, dict):
                return u.get(k)
        except Exception:
            pass
        # mapping/Record-style: `k in u` and `u[k]`
        try:
            if k in u:  # type: ignore[operator]
                return u[k]  # type: ignore[index]
        except Exception:
            pass
        # attribute-style
        try:
            return getattr(u, k)
        except Exception:
            return None

    raw = None
    for k in keys:
        v = _read(user, k)
        if v:
            raw = v
            break

    if not raw:
        # try coercing to dict (asyncpg.Record supports dict(record))
        try:
            d = dict(user)  # type: ignore[arg-type]
            for k in keys:
                if d.get(k):
                    raw = d.get(k)
                    break
        except Exception:
            pass

    if not raw:
        return "ru"

    try:
        s = str(raw).strip().lower()
        s = s.split("-")[0]
        return s if s in ("ru", "en") else "ru"
    except Exception:
        return "ru"


async def build_premium_benefits_text(lang: str) -> str:
    benefits = await get_premium_benefits()
    if not benefits:
        return t("premium.benefits.title", lang) + "\n\n" + t("premium.benefits.footer", lang)

    lines: list[str] = [t("premium.benefits.title", lang), ""]
    for b in benefits:
        title = html.escape(str(b.get("title") or ""), quote=False)
        desc = html.escape(str(b.get("description") or ""), quote=False)
        lines.append(f"<b>{title}</b>")
        if desc:
            lines.append(f"<i>{desc}</i>")
        lines.append("")

    return "\n".join(lines).strip()


def _format_until_and_days_left(until_iso: str | None, lang: str) -> tuple[str, str]:
    """Returns (human_until, days_left_text)."""
    if not until_iso:
        return ("—", "")
    try:
        dt = datetime.fromisoformat(until_iso)
        human = dt.strftime("%d.%m.%Y")
        now = get_moscow_now()
        days_left = (dt.date() - now.date()).days
        if days_left < 0:
            days_left = 0
        if lang == "en":
            return (human, f"({days_left} days)")
        return (human, f"({days_left} дней)")
    except Exception:
        return (str(until_iso), "")


def _build_expiry_warning_text(lang: str) -> str:
    if lang == "en":
        return (
            "💎 Your Premium will expire in 2 days.\n"
            "To keep Premium, tap “Renew subscription”."
        )
    return (
        "💎 Ваша премиум подписка закончится через 2 дня.\n"
        "Чтобы оставаться с премиум, нажмите «Продлить подписку»."
    )


def _build_expiry_warning_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Преимущества", callback_data="premium:expiry:benefits")
    kb.button(text="🔁 Продлить подписку", callback_data="premium:plans")
    kb.button(text="✖️ Понятно", callback_data="premium:expiry:dismiss")
    kb.adjust(1)
    return kb.as_markup()


def _build_expiry_benefits_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data="premium:expiry:back")
    kb.button(text="🔁 Продлить подписку", callback_data="premium:plans")
    kb.button(text="✖️ Понятно", callback_data="premium:expiry:dismiss")
    kb.adjust(1)
    return kb.as_markup()


async def maybe_send_premium_expiry_warning(bot, tg_id: int, chat_id: int, lang: str):
    """
    Best-effort напоминание за 2 дня до окончания премиума.
    Шлёт одно сообщение в сутки (кэш внутри процесса).
    """
    status = await get_user_premium_status(tg_id)
    until_iso = (status or {}).get("premium_until")
    if not until_iso:
        return
    try:
        until_dt = datetime.fromisoformat(until_iso)
    except Exception:
        return

    now = get_moscow_now()
    delta_days = (until_dt.date() - now.date()).days
    if delta_days != 2:
        return

    cache_key = f"{tg_id}:{now.date().isoformat()}"
    if _PREMIUM_EXPIRY_CACHE.get(tg_id) == cache_key:
        return
    _PREMIUM_EXPIRY_CACHE[tg_id] = cache_key

    try:
        await bot.send_message(
            chat_id=chat_id,
            text=_build_expiry_warning_text(lang),
            reply_markup=_build_expiry_warning_kb(),
            parse_mode="HTML",
            disable_notification=True,
        )
    except Exception:
        pass


@router.callback_query(F.data == "premium:expiry:benefits")
async def premium_expiry_benefits(callback: CallbackQuery):
    user = await get_user_by_tg_id(callback.from_user.id)
    lang = _get_lang(user)
    text = await build_premium_benefits_text(lang)
    try:
        await _safe_edit_or_send(
            callback,
            text=text,
            reply_markup=_build_expiry_benefits_kb(),
            parse_mode="HTML",
        )
    except Exception:
        pass
    await callback.answer()


@router.callback_query(F.data == "premium:expiry:back")
async def premium_expiry_back(callback: CallbackQuery):
    user = await get_user_by_tg_id(callback.from_user.id)
    lang = _get_lang(user)
    try:
        await _safe_edit_or_send(
            callback,
            text=_build_expiry_warning_text(lang),
            reply_markup=_build_expiry_warning_kb(),
            parse_mode="HTML",
        )
    except Exception:
        pass
    await callback.answer()


@router.callback_query(F.data == "premium:expiry:dismiss")
async def premium_expiry_dismiss(callback: CallbackQuery):
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer()


async def _render_premium_menu(callback: CallbackQuery, back_cb: str = "menu:profile", *, from_menu: bool = False):
    """Premium screen (info + buy/extend entrypoint).

    Payment flow and invoices live in `handlers/payments.py`.
    Here we only show the UX and route users to the payment handlers.
    """

    tg_id = callback.from_user.id

    user = await get_user_by_tg_id(tg_id)
    lang = _get_lang(user)

    status = await get_user_premium_status(tg_id)
    is_active = await is_user_premium_active(tg_id)

    until = (status or {}).get("premium_until")

    kb = InlineKeyboardBuilder()

    is_admin = bool(user and (user.get("is_admin") or user.get("is_moderator")))

    if is_active:
        # --- Active premium scenario ---
        human_until, days_left_text = _format_until_and_days_left(until, lang)
        status_block = (
            t("premium.title", lang)
            + "\n"
            + t("premium.status.active_until", lang, until=human_until, days_left=days_left_text)
            + "\n"
        )

        # News for last 7 days
        since = (get_moscow_now() - timedelta(days=7)).isoformat()
        news_items = await get_premium_news_since(since, limit=10)
        if news_items:
            news_lines = [t("premium.news.header", lang)]
            for it in news_items:
                news_lines.append(f"• {it}")
            news_block = "\n".join(news_lines)
        else:
            news_block = t("premium.news.header", lang) + "\n" + t("premium.news.empty", lang)

        text = status_block + "\n" + news_block

        kb.button(text=t("premium.btn.benefits", lang), callback_data=f"premium:benefits:{back_cb}")
        kb.button(text=t("premium.btn.extend", lang), callback_data="premium:plans")
        if is_admin:
            kb.button(
                text=f"🔧 Премиум: {'ВКЛ' if is_active else 'ВЫКЛ'}",
                callback_data="profile:premium_toggle_admin",
            )
        kb.button(text=t("premium.btn.back", lang), callback_data=back_cb)
        kb.adjust(1)

    else:
        # --- Inactive premium scenario ---
        text = await build_premium_benefits_text(lang)

        kb.button(text=t("premium.plan.7d", lang), callback_data="premium:plan:7d")
        kb.button(text=t("premium.plan.30d", lang), callback_data="premium:plan:30d")
        kb.button(text=t("premium.plan.90d", lang), callback_data="premium:plan:90d")
        if is_admin:
            kb.button(
                text=f"🔧 Премиум: {'ВКЛ' if is_active else 'ВЫКЛ'}",
                callback_data="profile:premium_toggle_admin",
            )
        kb.button(text=t("premium.btn.back", lang), callback_data=back_cb)
        kb.adjust(1)

    if from_menu:
        sent = await callback.message.bot.send_message(
            chat_id=callback.message.chat.id,
            text=text,
            reply_markup=kb.as_markup(),
            parse_mode="HTML",
            disable_notification=True,
        )
        try:
            await remember_screen(callback.from_user.id, sent.message_id)
        except Exception:
            pass
        try:
            await callback.message.delete()
        except Exception:
            pass
    else:
        await _safe_edit_or_send(
            callback,
            text=text,
            reply_markup=kb.as_markup(),
            parse_mode="HTML",
        )
    await callback.answer()


@router.callback_query(F.data == "profile:premium")
async def profile_premium_menu(callback: CallbackQuery):
    await _render_premium_menu(callback, back_cb="menu:profile")


@router.callback_query(F.data.regexp(r"^premium:open(?::(menu|profile))?$"))
async def premium_open(callback: CallbackQuery):
    parts = (callback.data or "").split(":")
    # ожидаем premium:open or premium:open:menu/profile
    source = parts[2] if len(parts) > 2 else (parts[1] if len(parts) > 1 else None)
    back_cb = "menu:profile" if source == "profile" else "menu:back"
    await _render_premium_menu(callback, back_cb=back_cb, from_menu=(source == "menu"))


@router.callback_query(F.data.regexp(r"^premium:benefits(?::(.+))?$"))
async def premium_benefits(callback: CallbackQuery):
    tg_id = callback.from_user.id
    user = await get_user_by_tg_id(tg_id)
    lang = _get_lang(user)

    parts = (callback.data or "").split(":", 2)
    back_cb = parts[2] if len(parts) >= 3 and parts[2] else "menu:profile"

    kb = InlineKeyboardBuilder()
    kb.button(text=t("premium.btn.back", lang), callback_data=f"premium:open:{'menu' if back_cb=='menu:back' else 'profile'}")
    kb.adjust(1)

    await _safe_edit_or_send(
        callback,
        text=await build_premium_benefits_text(lang),
        reply_markup=kb.as_markup(),
        parse_mode="HTML",
    )
    await callback.answer()
