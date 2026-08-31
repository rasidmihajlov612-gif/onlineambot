import asyncio
import logging
import os
import socket

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from dotenv import load_dotenv

import db
from config_loader import ADMISSION, first_step_id, get_step, next_step_id

load_dotenv()
logging.basicConfig(level=logging.INFO)

# Some ISPs reset connections to Telegram's default IP for api.telegram.org.
# Pin DNS for that host to a known-working IP from Telegram's range as a workaround.
_TELEGRAM_IP_OVERRIDE = "149.154.167.220"
_original_getaddrinfo = socket.getaddrinfo


def _patched_getaddrinfo(host, *args, **kwargs):
    if host == "api.telegram.org":
        host = _TELEGRAM_IP_OVERRIDE
    return _original_getaddrinfo(host, *args, **kwargs)


socket.getaddrinfo = _patched_getaddrinfo

router = Router()


def _quiz_of(step):
    return step if step["type"] == "quiz" else step.get("quiz")


def _questions_of(step):
    quiz = _quiz_of(step)
    return quiz["questions"] if quiz else []


def _passes(step, correct, total):
    quiz = _quiz_of(step) or {}
    if "min_correct" in quiz:
        return correct >= quiz["min_correct"]
    pass_rule = quiz.get("pass_rule", "all_correct")
    if isinstance(pass_rule, str) and pass_rule.endswith("%"):
        return (correct / total * 100) >= int(pass_rule.rstrip("%"))
    return correct == total


async def send_step(bot: Bot, chat_id: int, user_id: int, step_id: str):
    step = get_step(step_id)
    db.update_candidate(user_id, current_step=step_id, quiz_question_index=-1, quiz_correct_count=0)

    if step["type"] == "content":
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=step["confirm_button"], callback_data=f"confirm_step:{step_id}")
        ]])
        if step.get("file_id"):
            # send_document for everything (video included): Telegram re-compresses
            # video sent via send_video (client-side), which broke aspect ratio on some
            # source files. Documents are delivered byte-for-byte, no re-encoding.
            await bot.send_document(chat_id, document=step["file_id"], caption=step["title"], reply_markup=kb)
        else:
            text = f"🎬 {step['title']}\n\n{step.get('url', '(материал ещё не загружен)')}"
            await bot.send_message(chat_id, text, reply_markup=kb)

    elif step["type"] == "confirm":
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=step.get("yes_button", "Да, готов(а)"), callback_data=f"confirm_gate:{step_id}:yes"),
            InlineKeyboardButton(text=step.get("no_button", "Ещё не готов(а)"), callback_data=f"confirm_gate:{step_id}:no"),
        ]])
        await bot.send_message(chat_id, step["title"], reply_markup=kb)

    elif step["type"] == "quiz":
        db.update_candidate(user_id, quiz_question_index=0, quiz_correct_count=0)
        await send_quiz_question(bot, chat_id, step_id, 0)


async def send_quiz_question(bot: Bot, chat_id: int, step_id: str, q_index: int):
    step = get_step(step_id)
    questions = _questions_of(step)
    q = questions[q_index]
    text = q.get("question") or q.get("objection")
    buttons = [
        [InlineKeyboardButton(text=opt, callback_data=f"quiz_answer:{step_id}:{q_index}:{i}")]
        for i, opt in enumerate(q["options"])
    ]
    await bot.send_message(
        chat_id,
        f"❓ Вопрос {q_index + 1}/{len(questions)}\n\n{text}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


async def advance(bot: Bot, chat_id: int, user_id: int, step_id: str):
    nxt = next_step_id(step_id)
    if nxt is None:
        await finish_admission(bot, chat_id, user_id)
    else:
        await send_step(bot, chat_id, user_id, nxt)


async def finish_admission(bot: Bot, chat_id: int, user_id: int):
    cand = db.get_candidate(user_id)
    db.update_candidate(user_id, status="passed", current_step="done")

    lines = []
    for chat in ADMISSION["chats"]:
        try:
            link = await bot.create_chat_invite_link(
                chat_id=chat["chat_id"], member_limit=1, name=f"candidate_{user_id}"
            )
            lines.append(f"• {chat['name']}: {link.invite_link}")
        except Exception as e:
            lines.append(f"• {chat['name']}: не удалось создать ссылку ({e}) — добавьте вручную")

    await bot.send_message(
        chat_id,
        "🎉 Поздравляем! Вы прошли обучение и готовы приступать к звонкам.\n\n"
        "Вступите в чаты:\n" + "\n".join(lines) + "\n\n"
        f"По всем вопросам пишите/звоните: {ADMISSION['rashid_contact']}",
    )

    await bot.send_message(
        ADMISSION["admin_chat_id"],
        f"✅ Кандидат прошёл обучение: {cand['full_name']} (@{cand['username']}, id {user_id})\n"
        f"Результаты тестов: {cand['quiz_results']}",
    )


@router.message(Command("start"))
async def cmd_start(message: Message):
    user = message.from_user
    db.create_candidate(user.id, user.username, user.full_name)
    db.update_candidate(
        user.id,
        current_step="new",
        status="new",
        quiz_question_index=-1,
        quiz_correct_count=0,
        quiz_results="{}",
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Начать обучение", callback_data="training_start")
    ]])
    await message.answer(
        "Здравствуйте! 👋 Это бот обучения агентов агентства недвижимости.\n\nПриступим к обучению?",
        reply_markup=kb,
    )


@router.message(F.video)
async def on_video_uploaded(message: Message):
    if message.from_user.id != ADMISSION["admin_chat_id"]:
        return
    await message.answer(
        "⚠️ Это видео отправлено со сжатием клиентом Telegram — пропорции кадра могли "
        "испортиться. Пришлите его ещё раз как файл (без сжатия), тогда бот подхватит "
        "нужный file_id автоматически.\n\n"
        f"file_id этого (возможно испорченного) видео: <code>{message.video.file_id}</code>",
        parse_mode="HTML",
    )


@router.message(F.document)
async def on_document_uploaded(message: Message):
    if message.from_user.id != ADMISSION["admin_chat_id"]:
        return
    await message.answer(
        f"file_id для training.yaml (поле file_id):\n\n<code>{message.document.file_id}</code>",
        parse_mode="HTML",
    )


@router.my_chat_member()
async def on_bot_added_to_chat(event: ChatMemberUpdated):
    old_status = event.old_chat_member.status
    new_status = event.new_chat_member.status
    if old_status in ("left", "kicked") and new_status in ("member", "administrator"):
        logging.info("Bot added to chat %r, chat_id=%s", event.chat.title, event.chat.id)
        await event.bot.send_message(
            ADMISSION["admin_chat_id"],
            f"🤖 Бота добавили в чат «{event.chat.title}»\n"
            f"chat_id: <code>{event.chat.id}</code>\n\n"
            "Вставьте это значение в training.yaml в нужный chat_id под admission.chats.",
            parse_mode="HTML",
        )


@router.message(Command("ready"))
async def cmd_ready(message: Message):
    cand = db.get_candidate(message.from_user.id)
    if not cand or cand["current_step"] in ("new", "done"):
        await message.answer("Сначала начните обучение: /start")
        return
    await send_step(message.bot, message.chat.id, message.from_user.id, cand["current_step"])


@router.callback_query(F.data == "training_start")
async def on_training_start(callback: CallbackQuery):
    await callback.answer()
    await send_step(callback.bot, callback.message.chat.id, callback.from_user.id, first_step_id())


@router.callback_query(F.data.startswith("confirm_step:"))
async def on_confirm_step(callback: CallbackQuery):
    await callback.answer()
    _, step_id = callback.data.split(":", 1)
    step = get_step(step_id)
    user_id, chat_id = callback.from_user.id, callback.message.chat.id
    if step.get("quiz"):
        db.update_candidate(user_id, quiz_question_index=0, quiz_correct_count=0)
        await send_quiz_question(callback.bot, chat_id, step_id, 0)
    else:
        await advance(callback.bot, chat_id, user_id, step_id)


@router.callback_query(F.data.startswith("confirm_gate:"))
async def on_confirm_gate(callback: CallbackQuery):
    await callback.answer()
    _, step_id, answer = callback.data.split(":")
    step = get_step(step_id)
    user_id, chat_id = callback.from_user.id, callback.message.chat.id

    if answer == "yes":
        await advance(callback.bot, chat_id, user_id, step_id)
        return

    if step.get("on_no") == "restart":
        db.update_candidate(user_id, status="in_training", quiz_results="{}")
        await callback.bot.send_message(chat_id, "Хорошо, давайте пройдём материалы ещё раз.")
        await send_step(callback.bot, chat_id, user_id, first_step_id())
    else:
        await callback.bot.send_message(chat_id, "Хорошо, не торопитесь. Когда будете готовы — напишите /ready.")


@router.callback_query(F.data.startswith("quiz_answer:"))
async def on_quiz_answer(callback: CallbackQuery):
    await callback.answer()
    _, step_id, q_index, opt_index = callback.data.split(":")
    q_index, opt_index = int(q_index), int(opt_index)

    step = get_step(step_id)
    questions = _questions_of(step)
    q = questions[q_index]
    correct = opt_index == q["correct_index"]

    user_id, chat_id = callback.from_user.id, callback.message.chat.id
    cand = db.get_candidate(user_id)
    new_correct = cand["quiz_correct_count"] + (1 if correct else 0)

    feedback = "✅ Верно!" if correct else "❌ Неверно."
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.bot.send_message(chat_id, feedback)

    next_q_index = q_index + 1
    if next_q_index < len(questions):
        db.update_candidate(user_id, quiz_question_index=next_q_index, quiz_correct_count=new_correct)
        await send_quiz_question(callback.bot, chat_id, step_id, next_q_index)
        return

    total = len(questions)
    db.save_quiz_result(user_id, step_id, new_correct, total)

    if _passes(step, new_correct, total):
        db.update_candidate(user_id, quiz_question_index=-1, quiz_correct_count=0)
        await advance(callback.bot, chat_id, user_id, step_id)
    else:
        db.update_candidate(user_id, quiz_question_index=0, quiz_correct_count=0)
        await callback.bot.send_message(chat_id, f"Результат: {new_correct}/{total}. Попробуем ещё раз.")
        await send_quiz_question(callback.bot, chat_id, step_id, 0)


async def main():
    db.init_db()
    bot = Bot(token=os.environ["BOT_TOKEN"])
    dp = Dispatcher()
    dp.include_router(router)

    while True:
        try:
            await dp.start_polling(bot)
        except Exception:
            logging.exception("Polling crashed, retrying in 10s")
            await asyncio.sleep(10)


if __name__ == "__main__":
    asyncio.run(main())
