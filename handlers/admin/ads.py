from __future__ import annotations

from aiogram import Router, F
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext

from .common import _ensure_admin, edit_or_answer, AdminStates
from keyboards.common import build_back_kb

try:
    from database import create_ad, list_ads, set_ad_active, delete_ad
except Exception:  # pragma: no cover
    create_ad = list_ads = set_ad_active = delete_ad = None  # type: ignore


router = Router()


from aiogram.fsm.state import StatesGroup, State


class AdsStates(StatesGroup):
    waiting_title = State()
    waiting_body = State()


async def _reset_ads_state_only(state: FSMContext) -> None:
    try:
        await state.set_state(None)
    except Exception:
        pass


@router.callback_query(F.data == "admin:ads")
async def admin_ads_menu(callback: CallbackQuery, state: FSMContext):
    user = await _ensure_admin(callback)
    if user is None:
        return

    await _render_ads_list(callback.message, state)
    await callback.answer()


def _ads_list_keyboard(ads: list[dict]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []

    for ad in ads[:10]:
        ad_id = int(ad.get("id"))
        active = bool(ad.get("is_active"))
        status = "🟢" if active else "⚪️"
        rows.append(
            [
                InlineKeyboardButton(text=f"{status} {ad.get('title')[:30]}", callback_data=f"admin:ads:toggle:{ad_id}"),
                InlineKeyboardButton(text="🗑", callback_data=f"admin:ads:delete:{ad_id}"),
            ]
        )

    rows.append([InlineKeyboardButton(text="➕ Добавить", callback_data="admin:ads:add")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:menu")])

    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _render_ads_list(message, state: FSMContext):
    if list_ads is None:
        await edit_or_answer(message, state, prefix="admin", text="Блок рекламы недоступен (нет функций в DB)")
        return

    ads = await list_ads(limit=50)
    lines = ["📣 <b>Реклама</b>", "", "Активные объявления участвуют в показах при оценке."]
    if not ads:
        lines.append("Пока нет объявлений.")
    else:
        for ad in ads[:10]:
            status = "🟢" if ad.get("is_active") else "⚪️"
            lines.append(f"{status} <b>{ad.get('title')}</b>")

        if len(ads) > 10:
            lines.append(f"… и ещё {len(ads)-10}")

    kb = _ads_list_keyboard(ads)
    await edit_or_answer(message, state, prefix="admin", text="\n".join(lines), reply_markup=kb)


@router.callback_query(F.data == "admin:ads:add")
async def admin_ads_add(callback: CallbackQuery, state: FSMContext):
    user = await _ensure_admin(callback)
    if user is None:
        return

    await _reset_ads_state_only(state)
    await state.set_state(AdsStates.waiting_title)
    await state.update_data(admin_chat_id=callback.message.chat.id, admin_msg_id=callback.message.message_id)
    await edit_or_answer(
        callback.message,
        state,
        prefix="admin",
        text="🆕 <b>Новая реклама</b>\n\nОтправь заголовок (до 100 символов).\nОн виден только в админ-панели.",
        reply_markup=build_back_kb(callback_data="admin:ads", text="⬅️ Назад"),
    )
    await callback.answer()


@router.message(AdsStates.waiting_title)
async def admin_ads_title(message: Message, state: FSMContext):
    data = await state.get_data()

    title = (message.text or "").strip()
    await message.delete()

    if not title:
        await edit_or_answer(
            message,
            state,
            prefix="admin",
            text="Заголовок не может быть пустым. Отправь ещё раз.",
            reply_markup=build_back_kb(callback_data="admin:ads", text="⬅️ Назад"),
        )
        return

    await state.update_data(ads_new_title=title)
    await state.set_state(AdsStates.waiting_body)

    await edit_or_answer(
        message,
        state,
        prefix="admin",
        text="Теперь отправь текст объявления (этот текст увидят пользователи).",
        reply_markup=build_back_kb(callback_data="admin:ads", text="⬅️ Назад"),
    )


@router.message(AdsStates.waiting_body)
async def admin_ads_body(message: Message, state: FSMContext):
    data = await state.get_data()
    if not data.get("ads_new_title"):
        return
    body = (message.text or "").strip()
    await message.delete()

    if not body:
        await edit_or_answer(
            message,
            state,
            prefix="admin",
            text="Текст не может быть пустым. Пришли ещё раз.",
            reply_markup=build_back_kb(callback_data="admin:ads", text="⬅️ Назад"),
        )
        return

    if create_ad is not None:
        try:
            await create_ad(str(data.get("ads_new_title")), body, True)
        except Exception:
            pass

    # сбрасываем
    await _reset_ads_state_only(state)
    await _render_ads_list(message, state)


@router.callback_query(F.data.startswith("admin:ads:toggle:"))
async def admin_ads_toggle(callback: CallbackQuery, state: FSMContext):
    user = await _ensure_admin(callback)
    if user is None:
        return
    parts = callback.data.split(":")
    ad_id = int(parts[-1])
    if set_ad_active is not None:
        ads = await list_ads(limit=100)
        current = next((a for a in ads if int(a.get("id")) == ad_id), None)
        if current:
            new_state = not bool(current.get("is_active"))
            await set_ad_active(ad_id, new_state)
    await _render_ads_list(callback.message, state)
    await callback.answer()


@router.callback_query(F.data.startswith("admin:ads:delete:"))
async def admin_ads_delete(callback: CallbackQuery, state: FSMContext):
    user = await _ensure_admin(callback)
    if user is None:
        return
    ad_id = int(callback.data.split(":")[-1])
    if delete_ad is not None:
        try:
            await delete_ad(ad_id)
        except Exception:
            pass
    await _render_ads_list(callback.message, state)
    await callback.answer("Удалено")
