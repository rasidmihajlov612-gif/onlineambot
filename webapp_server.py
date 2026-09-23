import hashlib
import hmac
import json
import logging
import time
from pathlib import Path
from urllib.parse import parse_qsl

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiohttp import web

import db
import llm
from config_loader import ADMISSION, PAYMENTS, TRAINER, WEBAPP, get_step, step_progress

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


# ============================================================
# Тренажёр звонка
# ============================================================

def _persona(persona_id):
    for p in TRAINER.get("personas", []):
        if p["id"] == persona_id:
            return p
    return None


def _public_persona(p):
    return {"id": p["id"], "name": p["name"], "desc": p["desc"]}


def _rubric_for_prompt():
    return "\n".join(
        f"- {r['id']} (до {r['weight']} баллов): {r['label']} — {r['hint']}"
        for r in TRAINER.get("rubric", [])
    )


def _dialogue_for_prompt(messages):
    who = {"agent": "Агент", "owner": "Собственник"}
    return "\n".join(f"{who.get(m['role'], m['role'])}: {m['text']}" for m in messages)


def _roleplay_messages(persona, messages):
    """История для модели: системный промпт с характером + реплики, где
    собственник — assistant, агент — user."""
    system = TRAINER["roleplay_prompt"].format(
        name=persona["name"], behavior=persona["behavior"]
    )
    out = [{"role": "system", "content": system}]
    for m in messages:
        out.append({
            "role": "assistant" if m["role"] == "owner" else "user",
            "content": m["text"],
        })
    return out


def _offline():
    raise web.HTTPServiceUnavailable(
        text=TRAINER.get("offline_notice", "Тренажёр сейчас недоступен."),
        content_type="text/plain",
    )


async def _review(client, messages):
    """Разбор диалога. Баллы режем по весам критериев: модель периодически
    выдаёт 30 там, где максимум 20."""
    prompt = TRAINER["review_prompt"].format(
        rubric=_rubric_for_prompt(), dialogue=_dialogue_for_prompt(messages)
    )
    raw = await client.chat(
        [{"role": "user", "content": prompt}], temperature=0.2, max_tokens=700
    )
    data = llm.parse_json_reply(raw)

    scores, total = {}, 0
    for r in TRAINER.get("rubric", []):
        try:
            value = int(data.get("scores", {}).get(r["id"], 0))
        except (TypeError, ValueError):
            value = 0
        value = max(0, min(value, r["weight"]))
        scores[r["id"]] = value
        total += value

    advice = data.get("advice") or []
    if isinstance(advice, str):
        advice = [advice]
    return total, scores, str(data.get("verdict", ""))[:500], [str(a)[:500] for a in advice[:3]]


def _session_or_404(request, user_id, body):
    session = db.get_trainer_session(int(body["session_id"]))
    if not session or session["agent_user_id"] != user_id:
        raise web.HTTPNotFound(text="session not found")
    return session


async def handle_trainer_config(request):
    user = _authenticate(request)
    used = db.count_trainer_sessions_today(user["id"])
    limit = TRAINER.get("daily_limit", 3)
    active = db.get_active_trainer_session(user["id"])

    payload = {
        "intro": TRAINER.get("intro", ""),
        "personas": [_public_persona(p) for p in TRAINER.get("personas", [])],
        "rubric": [{"id": r["id"], "label": r["label"], "weight": r["weight"]}
                   for r in TRAINER.get("rubric", [])],
        "max_turns": TRAINER.get("max_turns", 10),
        "left_today": max(0, limit - used),
        "daily_limit": limit,
        "available": request.app.get("llm") is not None,
        "offline_notice": TRAINER.get("offline_notice", ""),
        "active": None,
    }
    if active:
        persona = _persona(active["persona_id"])
        payload["active"] = {
            "session_id": active["id"],
            "persona": _public_persona(persona) if persona else None,
            "messages": json.loads(active["messages"]),
            "turns": active["turns"],
        }
    return web.json_response(payload)


async def handle_trainer_start(request):
    body = await request.json()
    user = _authenticate(request, body)
    if not request.app.get("llm"):
        _offline()

    persona = _persona(body.get("persona_id"))
    if not persona:
        raise web.HTTPBadRequest(text="unknown persona")

    if db.count_trainer_sessions_today(user["id"]) >= TRAINER.get("daily_limit", 3):
        raise web.HTTPTooManyRequests(text="daily limit reached")

    # Незакрытый диалог помечаем брошенным, чтобы не копились активные.
    previous = db.get_active_trainer_session(user["id"])
    if previous:
        db.abandon_trainer_session(previous["id"])

    session_id = db.create_trainer_session(user["id"], persona["id"])
    messages = [{"role": "owner", "text": persona["opening"]}]
    db.save_trainer_messages(session_id, messages, 0)
    db.touch_active(user["id"])

    return web.json_response({
        "session_id": session_id,
        "persona": _public_persona(persona),
        "messages": messages,
    })


async def handle_trainer_reply(request):
    body = await request.json()
    user = _authenticate(request, body)
    client = request.app.get("llm")
    if not client:
        _offline()

    session = _session_or_404(request, user["id"], body)
    if session["status"] != "active":
        raise web.HTTPConflict(text="session is finished")

    text = (body.get("text") or "").strip()
    if not text:
        raise web.HTTPBadRequest(text="empty reply")

    persona = _persona(session["persona_id"])
    messages = json.loads(session["messages"])
    messages.append({"role": "agent", "text": text[:1000]})
    turns = session["turns"] + 1
    db.save_trainer_messages(session["id"], messages, turns)
    db.touch_active(user["id"])

    if turns >= TRAINER.get("max_turns", 10):
        return await _finish_session(client, session["id"], messages)

    try:
        reply = await client.chat(_roleplay_messages(persona, messages), max_tokens=200)
    except llm.LLMUnavailable as e:
        logging.warning("trainer roleplay failed: %s", e)
        _offline()

    messages.append({"role": "owner", "text": reply})
    db.save_trainer_messages(session["id"], messages, turns)
    return web.json_response({"reply": reply, "turns": turns, "finished": False})


async def _finish_session(client, session_id, messages):
    try:
        score, scores, verdict, advice = await _review(client, messages)
    except llm.LLMUnavailable as e:
        logging.warning("trainer review failed: %s", e)
        _offline()

    db.finish_trainer_session(session_id, score, scores, verdict, advice)
    return web.json_response({
        "finished": True,
        "score": score,
        "scores": scores,
        "verdict": verdict,
        "advice": advice,
    })


async def handle_trainer_finish(request):
    body = await request.json()
    user = _authenticate(request, body)
    client = request.app.get("llm")
    if not client:
        _offline()

    session = _session_or_404(request, user["id"], body)
    if session["status"] != "active":
        raise web.HTTPConflict(text="session is finished")

    messages = json.loads(session["messages"])
    if not any(m["role"] == "agent" for m in messages):
        # Разговора не было — оценивать нечего, просто закрываем.
        db.abandon_trainer_session(session["id"])
        return web.json_response({"finished": True, "skipped": True})

    return await _finish_session(client, session["id"], messages)


async def handle_trainer_leaderboard(request):
    user = _authenticate(request)
    rows = db.trainer_leaderboard(min_sessions=TRAINER.get("min_sessions_for_board", 3))
    board = [
        {
            "place": i,
            "user_id": r["agent_user_id"],
            "full_name": r["full_name"],
            "username": r["username"],
            "sessions": r["sessions"],
            "avg_score": int(r["avg_score"] or 0),
            "best_score": r["best_score"],
            "me": r["agent_user_id"] == user["id"],
        }
        for i, r in enumerate(rows, 1)
    ]
    return web.json_response({
        "board": board,
        "min_sessions": TRAINER.get("min_sessions_for_board", 3),
        "history": [
            {
                "id": h["id"],
                "score": h["score"],
                "persona": (_persona(h["persona_id"]) or {}).get("name", h["persona_id"]),
                "finished_at": h["finished_at"],
            }
            for h in db.list_trainer_sessions_for_agent(user["id"], limit=10)
        ],
    })


async def handle_admin_trainings(request):
    _authenticate_admin(request)
    sessions = [
        {
            "id": t["id"],
            "agent_name": t["agent_name"],
            "agent_username": t["agent_username"],
            "persona": (_persona(t["persona_id"]) or {}).get("name", t["persona_id"]),
            "score": t["score"],
            "scores": json.loads(t["scores"] or "{}"),
            "verdict": t["verdict"],
            "advice": json.loads(t["advice"] or "[]"),
            "messages": json.loads(t["messages"] or "[]"),
            "finished_at": t["finished_at"],
        }
        for t in db.list_all_trainer_sessions()
    ]
    return web.json_response({
        "sessions": sessions,
        "rubric": [{"id": r["id"], "label": r["label"], "weight": r["weight"]}
                   for r in TRAINER.get("rubric", [])],
    })


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
               restore_agent_cb=None, llm_client=None) -> web.Application:
    app = web.Application()
    app["bot"] = bot
    app["bot_token"] = bot_token
    app["start_training_cb"] = start_training_cb
    app["admin_pin"] = admin_pin
    app["restore_agent_cb"] = restore_agent_cb
    # None, если ключа модели нет — тренажёр тогда честно говорит, что
    # собеседник недоступен, остальные вкладки работают как работали.
    app["llm"] = llm_client if llm_client is not None else llm.build_client()
    app.router.add_get("/", handle_index)
    app.router.add_get("/admin", handle_admin_page)
    app.router.add_post("/api/admin/verify", handle_admin_verify)
    app.router.add_get("/api/admin/money", handle_admin_money)
    app.router.add_post("/api/admin/payout", handle_admin_payout)
    app.router.add_get("/api/admin/banned", handle_admin_banned)
    app.router.add_post("/api/admin/unban", handle_admin_unban)
    app.router.add_get("/api/admin/objects", handle_admin_objects)
    app.router.add_get("/api/admin/trainings", handle_admin_trainings)
    app.router.add_get("/api/trainer/config", handle_trainer_config)
    app.router.add_get("/api/trainer/leaderboard", handle_trainer_leaderboard)
    app.router.add_post("/api/trainer/start", handle_trainer_start)
    app.router.add_post("/api/trainer/reply", handle_trainer_reply)
    app.router.add_post("/api/trainer/finish", handle_trainer_finish)
    app.router.add_get("/api/config", handle_config)
    app.router.add_get("/api/counts", handle_counts)
    app.router.add_get("/api/finances", handle_finances)
    app.router.add_get("/api/progress", handle_progress)
    app.router.add_post("/api/submit-object", handle_submit_object)
    app.router.add_post("/api/start-training", handle_start_training)
    app.router.add_static("/static/", WEBAPP_DIR / "static")
    return app
