from aiogram import types
from aiogram.dispatcher.event.bases import SkipHandler

import database as db


UPDATE_DEFAULT_TEXT = (
    "В боте сейчас идёт полное обновление. Меняется вся логика и система.\n"
    "Просим прощения за неудобства, мы оповестим, когда обновление закончится!"
)

# Команды, которые блокируются при включённом режиме обновления
BLOCKED_COMMANDS = {"/ref", "/help", "/feedback"}


async def _is_staff(user: dict | None) -> bool:
    if not user:
        return False
    return bool(user.get("is_admin") or user.get("is_moderator"))


async def should_block(event_obj) -> bool:
    """
    Возвращает True, если нужно прервать обработку из-за режима обновления.
    Ничего не отправляем пользователю (уведомление — отдельной рассылкой).
    """
    tg_id = getattr(getattr(event_obj, "from_user", None), "id", None)
    if tg_id is None:
        return False

    state = await db.get_update_mode_state()
    if not state.get("update_enabled"):
        return False

    user = await db.get_user_by_tg_id(tg_id)
    if _is_staff(user):
        return False

    # Регистрация /start позволена — не блокируем её
    if isinstance(event_obj, types.Message) and event_obj.text and event_obj.text.startswith("/start"):
        return False
    if isinstance(event_obj, types.CallbackQuery) and (event_obj.data or "").startswith("auth:"):
        return False

    # Блокируем команды /ref /help /feedback
    if isinstance(event_obj, types.Message) and event_obj.text:
        cmd = event_obj.text.strip().split()[0].lower()
        if cmd in BLOCKED_COMMANDS:
            raise SkipHandler

    # Блокируем дальнейшую обработку
    raise SkipHandler


async def send_notice_once(message_obj: types.Message | types.CallbackQuery | None) -> None:
    """
    Отправляет уведомление об обновлении один раз на версию (без блокировки).
    Использовать после регистрации (/start), чтобы показать текст и продолжить работу.
    """
    if message_obj is None:
        return
    tg_id = getattr(getattr(message_obj, "from_user", None), "id", None)
    if tg_id is None:
        return

    state = await db.get_update_mode_state()
    if not state.get("update_enabled"):
        return

    user = await db.get_user_by_tg_id(tg_id)
    if _is_staff(user):
        return

    notice_ver = int(state.get("update_notice_ver") or 0)
    seen_ver = await db.get_user_update_notice_ver(tg_id)
    if seen_ver >= notice_ver:
        return

    text = state.get("update_notice_text") or UPDATE_DEFAULT_TEXT
    try:
        if isinstance(message_obj, types.Message):
            await message_obj.answer(text, disable_notification=True)
        elif isinstance(message_obj, types.CallbackQuery):
            await message_obj.message.answer(text, disable_notification=True)
    except Exception:
        pass
    try:
        await db.set_user_update_notice_ver(tg_id, notice_ver)
    except Exception:
        pass
