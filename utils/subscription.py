# utils/subscription.py

from __future__ import annotations

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from config import REQUIRED_CHANNEL_ID

CHANNEL_ID = REQUIRED_CHANNEL_ID


async def is_subscribed(bot: Bot, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
    except TelegramBadRequest:
        return False

    return member.status in ("member", "administrator", "creator")
