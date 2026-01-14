from __future__ import annotations

from aiogram import Router, F
from aiogram.types import CallbackQuery
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

router = Router(name="premium")


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


@router.callback_query(F.data == "profile:premium")
async def profile_premium_menu(callback: CallbackQuery):
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

        kb.button(text=t("premium.btn.benefits", lang), callback_data="premium:benefits")
        kb.button(text=t("premium.btn.extend", lang), callback_data="premium:plans")
        kb.button(text=t("premium.btn.back", lang), callback_data="menu:profile")
        kb.adjust(1)

    else:
        # --- Inactive premium scenario ---
        text = await build_premium_benefits_text(lang)

        kb.button(text=t("premium.plan.7d", lang), callback_data="premium:plan:7d")
        kb.button(text=t("premium.plan.30d", lang), callback_data="premium:plan:30d")
        kb.button(text=t("premium.plan.90d", lang), callback_data="premium:plan:90d")
        kb.button(text=t("premium.btn.back", lang), callback_data="menu:profile")
        kb.adjust(1)

    await callback.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "premium:benefits")
async def premium_benefits(callback: CallbackQuery):
    tg_id = callback.from_user.id
    user = await get_user_by_tg_id(tg_id)
    lang = _get_lang(user)

    kb = InlineKeyboardBuilder()
    kb.button(text=t("premium.btn.back", lang), callback_data="profile:premium")
    kb.adjust(1)

    await callback.message.edit_text(
        await build_premium_benefits_text(lang),
        reply_markup=kb.as_markup(),
        parse_mode="HTML",
    )
    await callback.answer()
