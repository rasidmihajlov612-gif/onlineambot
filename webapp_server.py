import hashlib
import hmac
import json
import time
from pathlib import Path
from urllib.parse import parse_qsl

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiohttp import web

import db
from config_loader import ADMISSION, PAYMENTS, WEBAPP, get_step, step_progress

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


def _authenticate_admin(request, body=None):
    """Двухфакторка для /admin: настоящая личность в Telegram (initData,
    как везде) + PIN, известный только куратору. Обе проверки — на каждый
    запрос, не только на входе, PIN нигде не сохраняется на сервере."""
    user = _authenticate(request, body)
    if user["id"] != ADMISSION["admin_chat_id"]:
        raise web.HTTPForbidden(text="not admin")
    admin_pin = request.app.get("admin_pin")
    pin = (body or {}).get("pin") or request.query.get("pin")
    if not admin_pin or pin != admin_pin:
        raise web.HTTPUnauthorized(text="invalid pin")
    return user


async def handle_index(request):
    return web.FileResponse(WEBAPP_DIR / "index.html")


async def handle_admin_page(request):
    return web.FileResponse(WEBAPP_DIR / "admin.html")


async def handle_admin_verify(request):
    body = await request.json()
    _authenticate_admin(request, body)
    return web.json_response({"ok": True})


_PAYMENT_KIND_LABELS = {"accepted": "принята", "closed": "сдана"}


async def handle_admin_money(request):
    _authenticate_admin(request)
    agents = db.list_agents_with_unpaid()
    result = []
    grand_total = 0
    for a in agents:
        payments = db.list_unpaid_payments_for_agent(a["user_id"])
        result.append({
            "user_id": a["user_id"],
            "full_name": a["full_name"],
            "username": a["username"],
            "unpaid_total": a["unpaid_total"],
            "items": [
                {
                    "address": p["address"],
                    "kind_label": _PAYMENT_KIND_LABELS.get(p["kind"], p["kind"]),
                    "amount": p["amount"],
                }
                for p in payments
            ],
        })
        grand_total += a["unpaid_total"]
    return web.json_response({"agents": result, "grand_total": grand_total})


async def handle_admin_payout(request):
    body = await request.json()
    _authenticate_admin(request, body)
    agent_id = int(body["agent_id"])
    amount = db.mark_agent_paid(agent_id)
    if amount:
        try:
            await request.app["bot"].send_message(agent_id, f"💰 Вам выплачено {amount}₽.")
        except Exception:
            pass
    return web.json_response({"paid": amount})


_OBJECT_STATUS_LABELS = {
    "pending": "на проверке",
    "in_progress": "в работе",
    "closed": "сдана",
    "rejected": "отклонена",
    "failed": "сорвалась",
}


async def handle_admin_banned(request):
    _authenticate_admin(request)
    agents = [
        {
            "user_id": c["user_id"],
            "full_name": c["full_name"],
            "username": c["username"],
            "objects_count": c["objects_count"],
            "removed_at": c["updated_at"],
            "last_active_at": c["last_active_at"],
        }
        for c in db.list_removed_candidates()
    ]
    return web.json_response({"agents": agents})


async def handle_admin_unban(request):
    body = await request.json()
    _authenticate_admin(request, body)
    agent_id = int(body["agent_id"])
    restore_cb = request.app.get("restore_agent_cb")
    if not restore_cb:
        raise web.HTTPServiceUnavailable(text="restore callback is not wired")
    # Восстановление живёт в bot.py (статус + новые инвайты в чаты), сюда
    # приходит колбэком — импортировать bot.py отсюда нельзя, он сам
    # импортирует этот модуль.
    cand = await restore_cb(agent_id)
    if not cand:
        raise web.HTTPNotFound(text="nothing to restore")
    return web.json_response({"ok": True})


async def handle_admin_objects(request):
    _authenticate_admin(request)
    status = request.query.get("status") or None
    if status and status not in _OBJECT_STATUS_LABELS:
        raise web.HTTPBadRequest(text="unknown status")

    objects = [
        {
            "id": o["id"],
            "status": o["status"],
            "status_label": _OBJECT_STATUS_LABELS.get(o["status"], o["status"]),
            "address": o["address"],
            "price": o["price"],
            "deposit": o["deposit"],
            "owner_name": o["owner_name"],
            "owner_phone": o["owner_phone"],
            "showing_time": o["showing_time"],
            "tenant_criteria": o["tenant_criteria"],
            "notes": o["notes"],
            "created_at": o["created_at"],
            "agent_user_id": o["agent_user_id"],
            "agent_name": o["agent_name"],
            "agent_username": o["agent_username"],
        }
        for o in db.list_all_objects(status)
    ]
    counts = db.count_all_objects_by_status()
    counts["all"] = sum(counts.values())
    return web.json_response({"objects": objects, "counts": counts})


async def handle_config(request):
    return web.json_response({
        "checklist": WEBAPP.get("checklist", []),
        "support_info": WEBAPP.get("support_info", ""),
        "contact": ADMISSION.get("rashid_contact", ""),
        "extra_videos": WEBAPP.get("extra_videos", []),
        "self_study": WEBAPP.get("self_study", {}),
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


async def handle_finances(request):
    user = _authenticate(request)
    agent_id = user["id"]
    counts = db.count_objects_by_status(agent_id)
    in_progress_count = counts.get("in_progress", 0)
    return web.json_response({
        "upcoming": db.get_unpaid_total(agent_id),
        "in_progress_count": in_progress_count,
        "in_progress_potential": in_progress_count * PAYMENTS["closed_rate"],
        "totals": db.get_payment_totals(agent_id),
        "weekly": db.get_weekly_earnings(agent_id),
        "history": db.get_payout_history(agent_id),
    })


async def handle_counts(request):
    user = _authenticate(request)
    counts = db.count_objects_by_status(user["id"])
    pending = counts.get("pending", 0)
    in_progress = counts.get("in_progress", 0)
    rejected = counts.get("rejected", 0)
    failed = counts.get("failed", 0)
    return web.json_response({
        "pending": pending,
        "in_progress": in_progress,
        "rejected": rejected,
        "failed": failed,
        "total": pending + in_progress + rejected + failed,
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
    db.touch_active(user["id"])

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


def create_app(bot, bot_token: str, start_training_cb, admin_pin: str = None,
               restore_agent_cb=None) -> web.Application:
    app = web.Application()
    app["bot"] = bot
    app["bot_token"] = bot_token
    app["start_training_cb"] = start_training_cb
    app["admin_pin"] = admin_pin
    app["restore_agent_cb"] = restore_agent_cb
    app.router.add_get("/", handle_index)
    app.router.add_get("/admin", handle_admin_page)
    app.router.add_post("/api/admin/verify", handle_admin_verify)
    app.router.add_get("/api/admin/money", handle_admin_money)
    app.router.add_post("/api/admin/payout", handle_admin_payout)
    app.router.add_get("/api/admin/banned", handle_admin_banned)
    app.router.add_post("/api/admin/unban", handle_admin_unban)
    app.router.add_get("/api/admin/objects", handle_admin_objects)
    app.router.add_get("/api/config", handle_config)
    app.router.add_get("/api/counts", handle_counts)
    app.router.add_get("/api/finances", handle_finances)
    app.router.add_get("/api/progress", handle_progress)
    app.router.add_post("/api/submit-object", handle_submit_object)
    app.router.add_post("/api/start-training", handle_start_training)
    app.router.add_static("/static/", WEBAPP_DIR / "static")
    return app
