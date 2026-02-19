from __future__ import annotations

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from database import admin_delete_all_active_photos, admin_delete_all_archived_photos
from .common import _ensure_admin, edit_or_answer

router = Router(name="admin_photos")


def _photos_menu_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🧨 Удалить все активные", callback_data="admin:photos:active:ask")
    kb.button(text="🗃 Удалить архивы", callback_data="admin:photos:archive:ask")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(1, 1, 1)
    return kb.as_markup()


def _confirm_kb(do_cb: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Подтвердить", callback_data=do_cb)
    kb.button(text="❌ Отмена", callback_data="admin:photos")
    kb.adjust(1, 1)
    return kb.as_markup()


@router.callback_query(F.data == "admin:photos")
async def admin_photos_menu(callback: CallbackQuery, state: FSMContext):
    admin = await _ensure_admin(callback)
    if not admin:
        return
    try:
        await state.set_state(None)
    except Exception:
        pass
    text = (
        "🖼 <b>Фотографии</b>\n\n"
        "• Удалить все активные — скроет все активные фото у всех пользователей.\n"
        "• Удалить архивы — скроет все архивные фото у всех пользователей.\n\n"
        "Действие массовое. Используй с осторожностью."
    )
    await edit_or_answer(
        callback.message,
        state,
        prefix="admin_photos",
        text=text,
        reply_markup=_photos_menu_kb(),
    )
    await callback.answer()


@router.callback_query(F.data == "admin:photos:active:ask")
async def admin_photos_active_ask(callback: CallbackQuery, state: FSMContext):
    admin = await _ensure_admin(callback)
    if not admin:
        return
    await edit_or_answer(
        callback.message,
        state,
        prefix="admin_photos",
        text="⚠️ Подтвердить удаление всех активных фотографий у всех пользователей?",
        reply_markup=_confirm_kb("admin:photos:active:do"),
    )
    await callback.answer()


@router.callback_query(F.data == "admin:photos:archive:ask")
async def admin_photos_archive_ask(callback: CallbackQuery, state: FSMContext):
    admin = await _ensure_admin(callback)
    if not admin:
        return
    await edit_or_answer(
        callback.message,
        state,
        prefix="admin_photos",
        text="⚠️ Подтвердить удаление всех архивных фотографий у всех пользователей?",
        reply_markup=_confirm_kb("admin:photos:archive:do"),
    )
    await callback.answer()


@router.callback_query(F.data == "admin:photos:active:do")
async def admin_photos_active_do(callback: CallbackQuery, state: FSMContext):
    admin = await _ensure_admin(callback)
    if not admin:
        return
    affected = await admin_delete_all_active_photos()
    await edit_or_answer(
        callback.message,
        state,
        prefix="admin_photos",
        text=f"✅ Активные фото удалены: <b>{affected}</b>.",
        reply_markup=_photos_menu_kb(),
    )
    await callback.answer("Готово")


@router.callback_query(F.data == "admin:photos:archive:do")
async def admin_photos_archive_do(callback: CallbackQuery, state: FSMContext):
    admin = await _ensure_admin(callback)
    if not admin:
        return
    affected = await admin_delete_all_archived_photos()
    await edit_or_answer(
        callback.message,
        state,
        prefix="admin_photos",
        text=f"✅ Архивные фото удалены: <b>{affected}</b>.",
        reply_markup=_photos_menu_kb(),
    )
    await callback.answer("Готово")

