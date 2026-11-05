import os
import asyncio
import logging
import sqlite3
from datetime import datetime
from decimal import Decimal, InvalidOperation

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
PAYOUT_GROUP_ID = int(os.getenv("PAYOUT_GROUP_ID", "0") or 0)
PAYOUT_TOPIC_ID = int(os.getenv("PAYOUT_TOPIC_ID", "0") or 0)
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "@support")
DB_PATH = os.getenv("DB_PATH", "gret_team.sqlite3")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")
if not ADMIN_ID:
    raise RuntimeError("ADMIN_ID is missing")

logging.basicConfig(level=logging.INFO)
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()


def connect():
    global DB_PATH
    try:
        db_dir = os.path.dirname(os.path.abspath(DB_PATH))
        os.makedirs(db_dir, exist_ok=True)
        con = sqlite3.connect(DB_PATH)
    except (OSError, sqlite3.OperationalError) as exc:
        fallback = "/tmp/gret_team.sqlite3"
        logging.warning("Cannot open DB_PATH=%r (%s). Using temporary database %s", DB_PATH, exc, fallback)
        DB_PATH = fallback
        con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with connect() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, username TEXT NOT NULL DEFAULT '',
            wallet TEXT NOT NULL DEFAULT '', balance TEXT NOT NULL DEFAULT '0',
            total_withdrawn TEXT NOT NULL DEFAULT '0', withdraw_count INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            username TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL,
            amount TEXT NOT NULL DEFAULT '', wallet TEXT NOT NULL DEFAULT '',
            photos TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL
        )""")
        user_columns = {row["name"] for row in con.execute("PRAGMA table_info(users)")}
        if "balance" not in user_columns:
            con.execute("ALTER TABLE users ADD COLUMN balance TEXT NOT NULL DEFAULT '0'")
        if "total_withdrawn" not in user_columns:
            con.execute("ALTER TABLE users ADD COLUMN total_withdrawn TEXT NOT NULL DEFAULT '0'")
        if "withdraw_count" not in user_columns:
            con.execute("ALTER TABLE users ADD COLUMN withdraw_count INTEGER NOT NULL DEFAULT 0")
        columns = {row["name"] for row in con.execute("PRAGMA table_info(requests)")}
        if "payout_amount" not in columns:
            con.execute("ALTER TABLE requests ADD COLUMN payout_amount TEXT NOT NULL DEFAULT ''")


def stamp():
    return datetime.now().astimezone().strftime("%d.%m.%Y %H:%M:%S %Z")


def save_user(user):
    with connect() as con:
        con.execute(
            """INSERT INTO users(user_id, username, updated_at) VALUES(?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,
            updated_at=excluded.updated_at""",
            (user.id, user.username or "", stamp())
        )


def menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="💸 Выплаты"), KeyboardButton(text="👤 Профиль")],
            [KeyboardButton(text="🛟 Поддержка"), KeyboardButton(text="ℹ️ О нас")],
            [KeyboardButton(text="👛 Кошелёк"), KeyboardButton(text="📤 Вывод")]
        ],
        resize_keyboard=True
    )


def cancel_menu(extra=None):
    rows = []
    if extra:
        rows.append([KeyboardButton(text=extra)])
    rows.append([KeyboardButton(text="❌ Отменить")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def decision_buttons(request_id):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Одобрить", callback_data=f"decision:approve:{request_id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"decision:reject:{request_id}")
    ]])


def display_user(user_id, username):
    return f"@{username}" if username else f'<a href="tg://user?id={user_id}">пользователь</a>'


class Form(StatesGroup):
    payout_photos = State()
    wallet = State()
    withdraw_amount = State()
    admin_payout_amount = State()


@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    save_user(message.from_user)
    await message.answer(
        "👋 <b>Добро пожаловать в GRET TEAM!</b>\n\n"
        "Здесь вы можете отправить заявку на выплату, привязать TON-кошелёк "
        "и оформить вывод средств. Выберите нужный раздел в меню ниже.",
        reply_markup=menu()
    )


@dp.message(F.text == "❌ Отменить")
async def cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=menu())


@dp.message(F.text == "💸 Выплаты")
async def payout_begin(message: Message, state: FSMContext):
    save_user(message.from_user)
    await state.clear()
    await state.set_state(Form.payout_photos)
    await state.update_data(photos=[])
    await message.answer(
        "📸 <b>Заявка на выплату</b>\n"
        "Пришлите от 1 до 2 фотографий. После этого нажмите "
        "«📨 Отправить на выплату». Если передумали — нажмите «❌ Отменить».",
        reply_markup=cancel_menu("📨 Отправить на выплату")
    )


@dp.message(Form.payout_photos, F.photo)
async def payout_add_photo(message: Message, state: FSMContext):
    data = await state.get_data()
    photos = data.get("photos", [])
    if len(photos) >= 2:
        await message.answer("Можно отправить максимум 2 фотографии.")
        return
    photos.append(message.photo[-1].file_id)
    await state.update_data(photos=photos)
    await message.answer(f"Фото принято: {len(photos)}/2.")


@dp.message(Form.payout_photos, F.text == "📨 Отправить на выплату")
async def payout_submit(message: Message, state: FSMContext):
    photos = (await state.get_data()).get("photos", [])
    if not photos:
        await message.answer("Сначала пришлите хотя бы одну фотографию.")
        return

    user = message.from_user
    created = stamp()
    with connect() as con:
        cur = con.execute(
            """INSERT INTO requests(user_id,username,kind,photos,status,created_at)
            VALUES(?,?, 'payout', ?, 'pending', ?)""",
            (user.id, user.username or "", ",".join(photos), created)
        )
        request_id = cur.lastrowid

    caption = (
        f"🧾 <b>Заявка на выплату №{request_id}</b>\n"
        f"Пользователь: {display_user(user.id, user.username or '')}\n"
        f"Telegram ID: <code>{user.id}</code>\n"
        f"Фото: {len(photos)}\nВремя: {created}"
    )
    admin_sent = False

    # Keep the admin's private notification and decision buttons.
    try:
        if len(photos) == 1:
            await bot.send_photo(
                chat_id=ADMIN_ID,
                photo=photos[0],
                caption=caption,
                reply_markup=decision_buttons(request_id)
            )
        else:
            await bot.send_media_group(
                chat_id=ADMIN_ID,
                media=[
                    InputMediaPhoto(media=photos[0]),
                    InputMediaPhoto(media=photos[1])
                ]
            )
            await bot.send_message(
                chat_id=ADMIN_ID,
                text=caption,
                reply_markup=decision_buttons(request_id)
            )
        admin_sent = True
    except Exception:
        logging.exception("Failed to deliver payout application to admin")

    if admin_sent:
        await message.answer(
            "✅ Заявка отправлена администратору.",
            reply_markup=menu()
        )
    else:
        await message.answer(
            "Заявка сохранена, но не удалось отправить её администратору. Попробуйте позже.",
            reply_markup=menu()
        )
    await state.clear()


@dp.message(Form.payout_photos)
async def payout_other(message: Message):
    await message.answer("Отправьте фотографию или нажмите «📨 Отправить на выплату».")


@dp.message(F.text == "👤 Профиль")
async def profile(message: Message, state: FSMContext):
    save_user(message.from_user)
    await state.clear()
    with connect() as con:
        row = con.execute(
            "SELECT balance,total_withdrawn,withdraw_count FROM users WHERE user_id=?",
            (message.from_user.id,)
        ).fetchone()
    balance = Decimal(row["balance"] or "0")
    total = Decimal(row["total_withdrawn"] or "0")
    await message.answer(
        "👤 <b>Ваш профиль GRET TEAM</b>\\n\\n"
        f"🆔 ID: <code>{message.from_user.id}</code>\\n"
        f"💰 Баланс: <b>{format(balance.normalize(), 'f')} TON</b>\\n"
        f"📤 Всего выведено: <b>{format(total.normalize(), 'f')} TON</b>\\n"
        f"✅ Успешных выводов: <b>{row['withdraw_count']}</b>",
        reply_markup=menu()
    )


@dp.message(F.text == "👛 Кошелёк")
async def wallet_begin(message: Message, state: FSMContext):
    save_user(message.from_user)
    await state.clear()
    with connect() as con:
        row = con.execute("SELECT wallet FROM users WHERE user_id=?", (message.from_user.id,)).fetchone()
    current = row["wallet"] if row and row["wallet"] else "не привязан"
    await state.set_state(Form.wallet)
    await message.answer(
        f"👛 Ваш текущий TON-кошелёк: <code>{current}</code>\n\n"
        "Отправьте адрес кошелька одним сообщением.",
        reply_markup=cancel_menu()
    )


@dp.message(Form.wallet, F.text)
async def wallet_store(message: Message, state: FSMContext):
    wallet = message.text.strip()
    if not (20 <= len(wallet) <= 120) or any(ch.isspace() for ch in wallet):
        await message.answer("Адрес выглядит некорректно. Проверьте его и отправьте ещё раз.")
        return
    with connect() as con:
        con.execute(
            """INSERT INTO users(user_id,username,wallet,updated_at) VALUES(?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,
            wallet=excluded.wallet, updated_at=excluded.updated_at""",
            (message.from_user.id, message.from_user.username or "", wallet, stamp())
        )
    await state.clear()
    await message.answer("✅ TON-кошелёк сохранён.", reply_markup=menu())


@dp.message(F.text == "📤 Вывод")
async def withdraw_begin(message: Message, state: FSMContext):
    save_user(message.from_user)
    await state.clear()
    with connect() as con:
        row = con.execute("SELECT wallet FROM users WHERE user_id=?", (message.from_user.id,)).fetchone()
    if not row or not row["wallet"]:
        await message.answer("Сначала привяжите TON-кошелёк в разделе «👛 Кошелёк».", reply_markup=menu())
        return
    await state.set_state(Form.withdraw_amount)
    await message.answer(
        "📤 <b>Заявка на вывод</b>\nВведите сумму в TON, которую хотите вывести "
        "(например, 4.59).",
        reply_markup=cancel_menu()
    )


@dp.message(Form.withdraw_amount, F.text)
async def withdraw_submit(message: Message, state: FSMContext):
    try:
        amount = Decimal(message.text.strip().replace(",", "."))
        if not amount.is_finite() or amount <= 0 or amount > Decimal("1000000000"):
            raise InvalidOperation
        amount_text = format(amount.normalize(), "f")
    except (InvalidOperation, ValueError):
        await message.answer("Введите положительную сумму числом, например 4.59.")
        return

    user = message.from_user
    created = stamp()
    with connect() as con:
        row = con.execute("SELECT wallet,balance FROM users WHERE user_id=?", (user.id,)).fetchone()
        if not row or not row["wallet"]:
            await state.clear()
            await message.answer("Кошелёк не найден. Сначала привяжите его.", reply_markup=menu())
            return
        wallet = row["wallet"]
        balance = Decimal(row["balance"] or "0")
        pending = con.execute(
            "SELECT amount FROM requests WHERE user_id=? AND kind='withdraw' AND status='pending'",
            (user.id,)
        ).fetchall()
        reserved = sum((Decimal(item["amount"] or "0") for item in pending), Decimal("0"))
        if amount > balance - reserved:
            available = max(balance - reserved, Decimal("0"))
            await message.answer(
                f"Недостаточно средств. Доступно: <b>{format(available.normalize(), 'f')} TON</b>.",
                reply_markup=menu()
            )
            await state.clear()
            return
        cur = con.execute(
            """INSERT INTO requests(user_id,username,kind,amount,wallet,status,created_at)
            VALUES(?,?, 'withdraw', ?, ?, 'pending', ?)""",
            (user.id, user.username or "", amount_text, wallet, created)
        )
        request_id = cur.lastrowid

    post = (
        f"📤 <b>Заявка на вывод №{request_id}</b>\n"
        f"👤 Воркер: {display_user(user.id, user.username or '')}\n"
        f"💰 Сумма: <b>{amount_text} TON</b>\n"
        f"👛 Кошелёк: <code>{wallet}</code>\n"
        f"🕒 Время: {created}\n"
        f"Статус: ⏳ Ожидает обработки"
    )
    try:
        await bot.send_message(ADMIN_ID, post, reply_markup=decision_buttons(request_id))
        await message.answer("✅ Заявка на вывод отправлена.", reply_markup=menu())
    except Exception:
        logging.exception("Failed to publish withdrawal request")
        await message.answer("Заявка сохранена, но уведомление не отправилось. Обратитесь в поддержку.")
    await state.clear()


@dp.message(Form.withdraw_amount)
async def withdraw_wrong(message: Message):
    await message.answer("Введите сумму числом, например 4.59.")


@dp.message(F.text == "🛟 Поддержка")
async def support(message: Message):
    handle = SUPPORT_USERNAME if SUPPORT_USERNAME.startswith("@") else f"@{SUPPORT_USERNAME}"
    await message.answer(f"🛟 Поддержка GRET TEAM: {handle}", reply_markup=menu())


@dp.message(F.text == "ℹ️ О нас")
async def about(message: Message):
    await message.answer(
        "ℹ️ <b>GRET TEAM</b>\n\n"
        "Панель для подачи заявок на выплаты и оформления вывода TON. "
        "Все заявки проверяются администрацией.",
        reply_markup=menu()
    )


@dp.callback_query(F.data.startswith("decision:"))
async def decide(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Эта кнопка доступна только администратору.", show_alert=True)
        return

    _, action, raw_id = callback.data.split(":")
    request_id = int(raw_id)

    with connect() as con:
        row = con.execute(
            "SELECT user_id,status,kind,amount FROM requests WHERE id=?", (request_id,)
        ).fetchone()
        if not row:
            await callback.answer("Заявка не найдена.", show_alert=True)
            return
        if row["status"] != "pending":
            await callback.answer("Заявка уже обработана.", show_alert=True)
            return

        if action == "approve" and row["kind"] == "payout":
            await state.set_state(Form.admin_payout_amount)
            await state.update_data(admin_request_id=request_id)
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
            await bot.send_message(
                callback.from_user.id,
                f"Заявка на начисление №{request_id}. Введите сумму TON для зачисления на баланс, например <code>4.45</code>."
            )
            await callback.answer("Запросил сумму начисления в личном чате.")
            return

        if action == "approve" and row["kind"] == "withdraw":
            amount = Decimal(row["amount"] or "0")
            user_row = con.execute(
                "SELECT balance,total_withdrawn FROM users WHERE user_id=?", (row["user_id"],)
            ).fetchone()
            if not user_row or Decimal(user_row["balance"] or "0") < amount:
                await callback.answer("Недостаточно средств на балансе пользователя.", show_alert=True)
                return
            new_balance = Decimal(user_row["balance"] or "0") - amount
            new_total = Decimal(user_row["total_withdrawn"] or "0") + amount
            con.execute(
                """UPDATE users SET balance=?,total_withdrawn=?,withdraw_count=withdraw_count+1,
                updated_at=? WHERE user_id=?""",
                (format(new_balance.normalize(), "f"), format(new_total.normalize(), "f"), stamp(), row["user_id"])
            )
            con.execute(
                "UPDATE requests SET status='approved',payout_amount=? WHERE id=?",
                (format(amount.normalize(), "f"), request_id)
            )
            approved_amount = format(amount.normalize(), "f")
            status = "approved"
        elif action == "reject":
            status = "rejected"
            con.execute("UPDATE requests SET status=? WHERE id=?", (status, request_id))
        else:
            await callback.answer("Неизвестное действие.", show_alert=True)
            return

    outcome = "одобрена ✅" if status == "approved" else "отклонена ❌"
    try:
        if status == "approved" and row["kind"] == "withdraw":
            await bot.send_message(row["user_id"], f"Вывод №{request_id} одобрен. Списано с баланса: <b>{approved_amount} TON</b>.")
        else:
            await bot.send_message(row["user_id"], f"Ваша заявка №{request_id} {outcome}.")
    except Exception:
        logging.info("User notification could not be delivered: %s", row["user_id"])

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.answer("Решение сохранено.")


@dp.message(Form.admin_payout_amount, F.text)
async def admin_set_payout_amount(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        amount = Decimal(message.text.strip().replace(",", "."))
        if not amount.is_finite() or amount <= 0 or amount > Decimal("1000000000"):
            raise InvalidOperation
        amount_text = format(amount.normalize(), "f")
    except (InvalidOperation, ValueError):
        await message.answer("Введите положительную сумму числом, например 4.45.")
        return

    data = await state.get_data()
    request_id = int(data.get("admin_request_id", 0))
    with connect() as con:
        row = con.execute(
            "SELECT user_id,status,kind FROM requests WHERE id=?",
            (request_id,)
        ).fetchone()
        if not row or row["status"] != "pending" or row["kind"] != "payout":
            await state.clear()
            await message.answer("Заявка не найдена или уже обработана.")
            return
        user_row = con.execute("SELECT username,balance FROM users WHERE user_id=?", (row["user_id"],)).fetchone()
        old_balance = Decimal(user_row["balance"] or "0") if user_row else Decimal("0")
        new_balance = old_balance + amount
        con.execute(
            """INSERT INTO users(user_id,username,balance,updated_at) VALUES(?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET balance=excluded.balance,updated_at=excluded.updated_at""",
            (row["user_id"], user_row["username"] if user_row else "", format(new_balance.normalize(), "f"), stamp())
        )
        con.execute(
            "UPDATE requests SET status='approved', payout_amount=? WHERE id=?",
            (amount_text, request_id)
        )

    await state.clear()

    try:
        await bot.send_message(
            row["user_id"],
            f"Ваша заявка на выплату №{request_id} одобрена.\n"
            f"💰 <b>Начислено на баланс:</b> {amount_text} TON"
        )
    except Exception:
        logging.exception("Could not notify user about approved withdrawal %s", request_id)
    await message.answer(
        f"Заявка №{request_id} одобрена.\n"
        f"💰 Начислено на баланс пользователя: <b>{amount_text} TON</b>"
    )


@dp.message(Form.admin_payout_amount)
async def admin_payout_amount_wrong(message: Message):
    if message.from_user.id == ADMIN_ID:
        await message.answer("Введите сумму выплаты числом, например 4.45.")


async def main():
    init_db()
    logging.info("GRET TEAM bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
