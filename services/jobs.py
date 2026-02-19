import asyncio
from datetime import datetime, time, timedelta, date
from typing import Callable

from aiogram import Bot

from utils.time import get_bot_now
from database import (
    finalize_party,
    publish_daily_results,
    grant_daily_credits_once,
    fetch_pending_notifications,
    mark_notification_done,
)


def _next_run(at_time: time) -> datetime:
    """Return the next datetime in bot TZ at given wall-clock time."""
    now = get_bot_now()
    target = now.replace(hour=at_time.hour, minute=at_time.minute, second=0, microsecond=0)
    if target <= now:
        target = target + timedelta(days=1)
    return target


async def _sleep_until(dt: datetime) -> None:
    now = get_bot_now()
    delay = (dt - now).total_seconds()
    if delay > 0:
        await asyncio.sleep(delay)


async def finalize_party_job(bot: Bot) -> None:
    """Ежедневно 00:05 — закрываем все истёкшие партии (по expires_at)."""
    while True:
        target = _next_run(time(0, 5))
        await _sleep_until(target)
        try:
            await finalize_party(None)
        except Exception:
            # не прерываем цикл; логирование возьмёт Errors middleware
            pass


async def daily_credits_grant_job(bot: Bot) -> None:
    """Ежедневно — выдача daily credits (idempotent)."""
    while True:
        target = _next_run(time(0, 6))
        await _sleep_until(target)
        try:
            await grant_daily_credits_once(get_bot_now().date(), batch_size=1000)
        except Exception:
            pass


async def daily_results_publish_job(bot: Bot) -> None:
    """Ежедневно 08:00 — публикация итогов за позавчера (без пользовательских DM)."""
    while True:
        target = _next_run(time(8, 0))
        await _sleep_until(target)
        try:
            submit_day = get_bot_now().date() - timedelta(days=2)
            await publish_daily_results(submit_day, limit=10)
        except Exception:
            pass


async def daily_recap_job(bot: Bot) -> None:
    """Deprecated wrapper: kept for backward compatibility."""
    await daily_results_publish_job(bot)


async def notifications_worker(bot: Bot, send_fn: Callable[[int, dict], asyncio.Future] | None = None) -> None:
    """
    Постоянный воркер: достаёт pending уведомления партиями и отправляет.
    send_fn: кастомная функция доставки; если None — noop (очередь просто очищается).
    """
    while True:
        try:
            batch = await fetch_pending_notifications(limit=50)
            if not batch:
                await asyncio.sleep(60)
                continue
            for item in batch:
                nid = int(item["id"])
                status = "sent"
                backoff = None
                if send_fn:
                    try:
                        await send_fn(nid, item)
                    except Exception as e:
                        status = "failed"
                        attempts = int(item.get("attempts") or 0)
                        backoff = min(900, max(30, attempts * 60))
                        await mark_notification_done(nid, status=status, error=str(e), backoff_seconds=backoff)
                        await asyncio.sleep(1.0)
                        continue
                await mark_notification_done(nid, status=status)
                await asyncio.sleep(0.3)  # мягкий rate limit
        except Exception:
            await asyncio.sleep(10)
            continue
