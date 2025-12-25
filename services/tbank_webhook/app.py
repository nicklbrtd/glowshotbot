import os
import hashlib
import logging
from typing import Optional, Tuple

from fastapi import FastAPI, Request, Response

import aiohttp

from database import init_db, apply_tbank_payment_confirmed

app = FastAPI()
log = logging.getLogger("tbank_webhook")
logging.basicConfig(level=logging.INFO)


@app.on_event("startup")
async def _startup():
    # Webhook runs as a separate process from the bot,
    # so it must initialize DB connections itself.
    await init_db()


def _env(name: str) -> str:
    return (os.getenv(name, "") or "").strip()


# --- Telegram notification helpers ---
async def _tg_send_message(tg_id: int, text: str) -> None:
    """Send a one-off Telegram message from the bot (webhook runs without aiogram)."""
    token = _env("BOT_TOKEN") or _env("TELEGRAM_BOT_TOKEN") or _env("TG_BOT_TOKEN")
    if not token:
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": int(tg_id),
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=10) as resp:
                # ignore failures (user may have blocked the bot etc.)
                await resp.read()
    except Exception:
        return


def _plan_to_human(plan: str) -> str:
    p = (plan or "").strip().lower()
    if p == "w":
        return "Неделя"
    if p == "m":
        return "Месяц"
    if p == "q":
        return "3 месяца"
    return p


def calc_token(body: dict, password: str) -> str:
    data = {}
    for k, v in body.items():
        if k == "Token":
            continue
        # вложенные объекты не участвуют
        if isinstance(v, (dict, list)):
            continue
        data[str(k)] = "" if v is None else str(v)

    data["Password"] = password
    s = "".join(data[k] for k in sorted(data.keys()))
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def parse_order_id(order_id: str) -> Optional[Tuple[int, str]]:
    """Parse OrderId in format: GS_<tgid>_<plan>_<ts>

    plan: w/m/q (week/month/3months)
    example: GS_123456789_m_170343
    """
    try:
        parts = (order_id or "").split("_")
        if len(parts) < 4 or parts[0] != "GS":
            return None
        tgid = int(parts[1])
        plan = str(parts[2])
        return tgid, plan
    except Exception:
        return None


@app.post("/tbank/notify")
async def tbank_notify(req: Request):
    tb_password = _env("TB_PASSWORD")
    tb_terminal = _env("TB_TERMINAL_KEY")

    # If keys are missing — service is up, but not ready for real notifications
    if not tb_password or not tb_terminal:
        return Response(content="not configured", media_type="text/plain", status_code=503)

    try:
        body = await req.json()
        if not isinstance(body, dict):
            raise ValueError("json is not object")
    except Exception:
        return Response(content="bad json", media_type="text/plain", status_code=400)

    # Optional: ensure the notification belongs to our terminal
    if str(body.get("TerminalKey", "")).strip() != tb_terminal:
        return Response(content="bad terminal", media_type="text/plain", status_code=400)

    token = body.get("Token")
    if not token or calc_token(body, tb_password) != str(token):
        return Response(content="bad token", media_type="text/plain", status_code=400)

    status = str(body.get("Status", ""))
    success = str(body.get("Success", "")).lower() == "true"
    order_id = str(body.get("OrderId", ""))
    payment_id = str(body.get("PaymentId", ""))

    amount_rub: Optional[int] = None
    try:
        if body.get("Amount") is not None:
            amount_rub = int(body.get("Amount")) // 100  # Amount usually in kopecks
    except Exception:
        amount_rub = None

    log.info(
        "notify: status=%s success=%s order_id=%s payment_id=%s amount_rub=%s",
        status,
        success,
        order_id,
        payment_id,
        amount_rub,
    )

    # We grant premium only on CONFIRMED
    if not (success and status == "CONFIRMED"):
        return Response(content="OK", media_type="text/plain", status_code=200)

    parsed = parse_order_id(order_id)
    if not parsed:
        log.warning("Bad OrderId format: %s", order_id)
        return Response(content="OK", media_type="text/plain", status_code=200)

    tgid, plan = parsed

    # Apply payment idempotently (TBank can send the same notify multiple times)
    try:
        changed = await apply_tbank_payment_confirmed(
            tg_id=int(tgid),
            plan=str(plan),
            order_id=str(order_id),
            payment_id=str(payment_id),
            amount_rub=amount_rub,
        )
        if changed:
            log.info("tbank confirmed -> premium extended: tg_id=%s plan=%s order_id=%s", tgid, plan, order_id)
            await _tg_send_message(
                int(tgid),
                (
                    "✅ <b>Оплата подтверждена!</b>\n"
                    f"Премиум активирован/продлён: <b>{_plan_to_human(plan)}</b>.\n\n"
                    "Вернись в бота — меню обновится автоматически."
                ),
            )
        else:
            log.info("tbank confirmed -> already processed: order_id=%s", order_id)
        return Response(content="OK", media_type="text/plain", status_code=200)
    except Exception:
        # If DB apply fails, return 500 so TBank retries the notification
        log.exception("tbank confirmed -> failed to apply payment: order_id=%s", order_id)
        return Response(content="internal error", media_type="text/plain", status_code=500)