from aiogram import Bot

from database import get_user_ui_state, set_user_banner_msg_id


async def ensure_giraffe_banner(bot: Bot, chat_id: int, tg_id: int, *, text: str = "🦒") -> int | None:
    """Ensure a single giraffe banner exists above section content."""
    banner_id = None
    try:
        ui_state = await get_user_ui_state(int(tg_id))
        banner_id = ui_state.get("banner_msg_id")
    except Exception:
        banner_id = None

    if banner_id:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=int(banner_id),
                text=text,
            )
            return int(banner_id)
        except Exception:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=int(banner_id))
            except Exception:
                pass

    try:
        sent = await bot.send_message(
            chat_id=chat_id,
            text=text,
            disable_notification=True,
        )
    except Exception:
        return None

    try:
        await set_user_banner_msg_id(int(tg_id), sent.message_id)
    except Exception:
        pass

    return sent.message_id
