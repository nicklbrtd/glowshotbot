from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse, RedirectResponse
import hashlib
from datetime import datetime, timedelta

from config import ROBOKASSA_PASSWORD2
from database import set_user_premium_status, get_user_premium_status
from utils.time import get_moscow_now  # если у тебя есть, как в payments.py

app = FastAPI()

TARIFFS_RUB = {
    "7d":  "79.00",
    "30d": "239.00",
    "90d": "569.00",
}
TARIFFS_DAYS = {"7d": 7, "30d": 30, "90d": 90}

def md5_hex(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()

def build_result_signature(out_sum: str, inv_id: str, shp: dict[str, str]) -> str:
    # ResultURL: OutSum:InvId:Password#2:[user params]   [oai_citation:5‡Robokassa Documentation](https://docs.robokassa.ru/payment/)
    base = f"{out_sum}:{inv_id}:{ROBOKASSA_PASSWORD2}"
    # user params (Shp_*) — лучше сортировать стабильно
    for k in sorted(shp.keys()):
        base += f":{k}={shp[k]}"
    return md5_hex(base).lower()

@app.post("/robokassa/result")
async def robokassa_result(request: Request):
    data = await request.form()

    out_sum = (data.get("OutSum") or "").strip()
    inv_id  = (data.get("InvId") or "").strip()
    sig     = (data.get("SignatureValue") or "").strip().lower()

    # кастомные параметры
    shp_tg_id  = (data.get("Shp_tg_id") or "").strip()
    shp_period = (data.get("Shp_period") or "").strip()  # "7d" / "30d" / "90d"

    if not out_sum or not inv_id or not sig:
        raise HTTPException(status_code=400, detail="Missing required fields")

    shp = {}
    if shp_tg_id:
        shp["Shp_tg_id"] = shp_tg_id
    if shp_period:
        shp["Shp_period"] = shp_period

    check = build_result_signature(out_sum, inv_id, shp)
    if sig != check:
        raise HTTPException(status_code=403, detail="Invalid signature")

    # ✅ защита от “подмены тарифа”
    if not shp_tg_id.isdigit():
        raise HTTPException(status_code=400, detail="Missing Shp_tg_id")

    if shp_period not in TARIFFS_RUB:
        raise HTTPException(status_code=400, detail="Unknown period")

    expected = TARIFFS_RUB[shp_period]

    # out_sum в бою часто 6 знаков после точки — сравниваем по числу, аккуратно
    # (Но сам out_sum для подписи НЕ ТРОГАЕМ!)  [oai_citation:6‡Robokassa Documentation](https://docs.robokassa.ru/payment/)
    def norm_money(s: str) -> str:
        # "79.000000" -> "79.00"
        if "." not in s:
            return f"{s}.00"
        a, b = s.split(".", 1)
        b = (b + "00")[:2]
        return f"{a}.{b}"

    if norm_money(out_sum) != expected:
        raise HTTPException(status_code=403, detail="Amount mismatch")

    tg_id = int(shp_tg_id)
    days = TARIFFS_DAYS[shp_period]
    now = get_moscow_now()

    # ✅ Продление премиума: добавляем дни к текущему сроку, если он ещё не истёк.
    base_dt = now
    try:
        current = await get_user_premium_status(tg_id)
        current_until = (current or {}).get("premium_until")
        if current_until:
            try:
                cur_dt = datetime.fromisoformat(current_until)
                if cur_dt > base_dt:
                    base_dt = cur_dt
            except Exception:
                pass
    except Exception:
        pass

    until_dt = base_dt + timedelta(days=days)
    premium_until_iso = until_dt.isoformat(timespec="seconds")

    await set_user_premium_status(tg_id, True, premium_until=premium_until_iso)

    # Robokassa ждёт "OK{InvId}"  [oai_citation:7‡Robokassa Documentation](https://docs.robokassa.ru/payment/)
    return PlainTextResponse(f"OK{inv_id}")

@app.get("/pay/success")
async def pay_success():
    return RedirectResponse("https://t.me/glowshotbot?start=payment_success")

@app.get("/pay/fail")
async def pay_fail():
    return RedirectResponse("https://t.me/glowshotbot?start=payment_fail")