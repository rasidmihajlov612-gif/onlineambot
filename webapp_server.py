import hashlib
import hmac
import json
import time
from pathlib import Path
from urllib.parse import parse_qsl

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiohttp import web

import db
from config_loader import ADMISSION, WEBAPP, get_step, step_progress

WEBAPP_DIR = Path(__file__).parent / "webapp"
_INIT_DATA_MAX_AGE = 86400  # Telegram recommends treating older initData as stale


def _validate_init_data(init_data: str, bot_token: str):
    if not init_data:
        return None
    try:
        parsed = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None
    received_hash = parsed.pop("hash", None)
    if not received_hash:
        return None
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(computed_hash, received_hash):
        return None
    if time.time() - int(parsed.get("auth_date", 0)) > _INIT_DATA_MAX_AGE:
        return None
    user_raw = parsed.get("user")
    if not user_raw:
        return None
    return json.loads(user_raw)


def _authenticate(request, body=None):
    init_data = (body or {}).get("initData") or request.query.get("initData")
    user = _validate_init_data(init_data, request.app["bot_token"])
    if not user:
        raise web.HTTPUnauthorized(text="invalid initData")
    return user


async def handle_index(request):
    return web.FileResponse(WEBAPP_DIR / "index.html")


async def handle_config(request):
    return web.json_response({
        "checklist": WEBAPP.get("checklist", []),
        "support_info": WEBAPP.get("support_info", ""),
        "contact": ADMISSION.get("rashid_contact", ""),
        "extra_videos": WEBAPP.get("extra_videos", []),
    })


async def handle_progress(request):
    user = _authenticate(request)
    cand = db.get_candidate(user["id"])
    if not cand or cand["current_step"] in ("new", ""):
        return web.json_response({"started": False})

    if cand["current_step"] == "done" or cand["status"] == "passed":
        return web.json_response({
            "started": True, "done": True, "percent": 100,
            "step_label": "Обучение пройдено",
        })

    step_num, total = step_progress(cand["current_step"])
    try:
        step_title = get_step(cand["current_step"])["title"]
    except KeyError:
        step_title = cand["current_step"]

    return web.json_response({
        "started": True,
        "done": False,
        "percent": round(step_num / total * 100) if total else 0,
        "step_num": step_num,
        "total_steps": total,
        "step_label": step_title,
    })


async def handle_counts(request):
    user = _authenticate(request)
    counts = db.count_objects_by_status(user["id"])
    return web.json_response({
        "pending": counts.get("pending", 0),
        "in_progress": counts.get("in_progress", 0),
    })


_OBJECT_FIELDS = [
    "owner_name", "owner_phone", "address", "price",
    "deposit", "showing_time", "tenant_criteria", "notes",
]

_OBJECT_LABELS = {
    "owner_name": "Собственник",
    "owner_phone": "Телефон",
    "address": "Адрес",
    "price": "Цена",
    "deposit": "Залог",
    "showing_time": "Показ",
    "tenant_criteria": "Кого рассматривает",
    "notes": "Комментарий",
}


async def handle_submit_object(request):
    body = await request.json()
    user = _authenticate(request, body)

    fields = {key: (body.get(key) or "").strip() for key in _OBJECT_FIELDS}
    if not fields["owner_name"] or not fields["address"]:
        raise web.HTTPBadRequest(text="owner_name and address are required")

    object_id = db.create_object(user["id"], **fields)

    agent_label = f"@{user['username']}" if user.get("username") else user.get("first_name", "агент")
    lines = [f"🆕 Новый объект от {agent_label} (id {user['id']})", ""]
    lines += [f"{_OBJECT_LABELS[key]}: {fields[key] or '—'}" for key in _OBJECT_FIELDS]
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ В работу", callback_data=f"obj_approve:{object_id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"obj_reject:{object_id}"),
    ]])
    await request.app["bot"].send_message(
        ADMISSION["admin_chat_id"], "\n".join(lines), reply_markup=kb
    )

    return web.json_response({"ok": True, "id": object_id})


async def handle_start_training(request):
    body = await request.json()
    user = _authenticate(request, body)
    await request.app["start_training_cb"](user)
    return web.json_response({"ok": True})


def create_app(bot, bot_token: str, start_training_cb) -> web.Application:
    app = web.Application()
    app["bot"] = bot
    app["bot_token"] = bot_token
    app["start_training_cb"] = start_training_cb
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/config", handle_config)
    app.router.add_get("/api/counts", handle_counts)
    app.router.add_get("/api/progress", handle_progress)
    app.router.add_post("/api/submit-object", handle_submit_object)
    app.router.add_post("/api/start-training", handle_start_training)
    app.router.add_static("/static/", WEBAPP_DIR / "static")
    return app
