import os
import hashlib
from fastapi import FastAPI, Request, Response

app = FastAPI()

TB_PASSWORD = os.getenv("TB_PASSWORD", "")
TB_TERMINAL_KEY = os.getenv("TB_TERMINAL_KEY", "")

def calc_token(body: dict) -> str:
    data = {}
    for k, v in body.items():
        if k == "Token":
            continue
        # вложенные объекты не участвуют
        if isinstance(v, (dict, list)):
            continue
        data[str(k)] = "" if v is None else str(v)

    data["Password"] = TB_PASSWORD
    s = "".join(data[k] for k in sorted(data.keys()))
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

@app.post("/tbank/notify")
async def tbank_notify(req: Request):
    body = await req.json()

    # optional: защита по terminal key
    if TB_TERMINAL_KEY and str(body.get("TerminalKey", "")) != TB_TERMINAL_KEY:
        return Response("bad terminal", status_code=400)

    if not body.get("Token") or calc_token(body) != body["Token"]:
        return Response("bad token", status_code=400)

    status = str(body.get("Status", ""))
    success = str(body.get("Success", "")).lower() == "true"

    # IMPORTANT: премиум выдавать на CONFIRMED
    if success and status == "CONFIRMED":
        # TODO: тут будет выдача премиума по OrderId
        pass

    return Response(content="OK", media_type="text/plain", status_code=200)