from typing import Iterable
from aiogram.fsm.context import FSMContext

from database import get_user_ui_state, set_user_screen_msg_id


async def _safe_delete(bot, chat_id: int, message_id: int | None) -> None:
    if not message_id:
        return
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass


async def cleanup_previous_screen(
    bot,
    chat_id: int,
    user_id: int,
    state: FSMContext | None = None,
    exclude_ids: Iterable[int] | None = None,
) -> None:
    """
    Удаляет последнее экранное сообщение (screen_msg_id), если оно ещё висит.
    Не трогает идентификаторы из exclude_ids (например, текущее сообщение колбэка).
    """
    safe_exclude = set(exclude_ids or [])

    msg_id = None
    if state is not None:
        try:
            data = await state.get_data()
            msg_id = data.get("screen_msg_id") or data.get("menu_msg_id")
        except Exception:
            msg_id = None

    if msg_id is None:
        try:
            ui = await get_user_ui_state(user_id)
            msg_id = ui.get("screen_msg_id")
        except Exception:
            msg_id = None

    if msg_id and msg_id not in safe_exclude:
        await _safe_delete(bot, chat_id, msg_id)
        if state is not None:
            try:
                data = await state.get_data()
                if data.get("screen_msg_id") == msg_id:
                    data["screen_msg_id"] = None
                    await state.set_data(data)
            except Exception:
                pass
        try:
            await set_user_screen_msg_id(user_id, None)
        except Exception:
            pass


async def remember_screen(
    user_id: int,
    message_id: int,
    state: FSMContext | None = None,
) -> None:
    """
    Запоминает текущий экран, чтобы его можно было удалить при переходе.
    """
    if state is not None:
        try:
            data = await state.get_data()
            data["screen_msg_id"] = message_id
            await state.set_data(data)
        except Exception:
            pass

    try:
        await set_user_screen_msg_id(user_id, message_id)
    except Exception:
        pass
