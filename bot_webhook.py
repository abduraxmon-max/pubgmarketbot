import asyncio
import logging
import os
from datetime import datetime
from typing import Iterable

import aiosqlite
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (Message, CallbackQuery, InlineKeyboardMarkup,
                           InlineKeyboardButton, ReplyKeyboardMarkup,
                           KeyboardButton)
from dotenv import load_dotenv
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
DB_PATH = os.getenv("DATABASE_PATH", "marketplace.sqlite3").strip() or "marketplace.sqlite3"

# Render Web Service webhook settings.
RUN_MODE = os.getenv("RUN_MODE", "webhook").strip().lower()
BASE_WEBHOOK_URL = (
    os.getenv("WEBHOOK_URL", os.getenv("RENDER_EXTERNAL_URL", "")).strip().rstrip("/")
)
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/telegram/webhook").strip()
if not WEBHOOK_PATH.startswith("/"):
    WEBHOOK_PATH = "/" + WEBHOOK_PATH
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
if not WEBHOOK_SECRET:
    import secrets
    WEBHOOK_SECRET = secrets.token_urlsafe(32)
WEB_SERVER_HOST = "0.0.0.0"
WEB_SERVER_PORT = int(os.getenv("PORT", "10000"))

if not TOKEN:
    raise RuntimeError("BOT_TOKEN .env faylida ko'rsatilmagan")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("pubg_marketplace")

MENU = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="🛍 E'lonlar"), KeyboardButton(text="➕ E'lon joylash")],
    [KeyboardButton(text="📦 Mening e'lonlarim"), KeyboardButton(text="⭐ Saqlanganlar")],
    [KeyboardButton(text="ℹ️ Qoidalar"), KeyboardButton(text="🆘 Yordam")],
], resize_keyboard=True)

class ListingForm(StatesGroup):
    title = State()
    price = State()
    level = State()
    rank = State()
    description = State()
    report_reason = State()


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

async def db_execute(query: str, params: tuple = ()):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(query, params)
        await db.commit()

async def db_fetchone(query: str, params: tuple = ()):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, params) as cur:
            return await cur.fetchone()

async def db_fetchall(query: str, params: tuple = ()):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, params) as cur:
            return await cur.fetchall()

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript('''
        PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS listings(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            price INTEGER NOT NULL,
            level TEXT NOT NULL,
            rank TEXT NOT NULL,
            description TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            FOREIGN KEY(seller_id) REFERENCES users(user_id)
        );
        CREATE TABLE IF NOT EXISTS favorites(
            user_id INTEGER NOT NULL, listing_id INTEGER NOT NULL,
            PRIMARY KEY(user_id, listing_id),
            FOREIGN KEY(user_id) REFERENCES users(user_id),
            FOREIGN KEY(listing_id) REFERENCES listings(id)
        );
        CREATE TABLE IF NOT EXISTS inquiries(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            listing_id INTEGER NOT NULL, buyer_id INTEGER NOT NULL,
            created_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'new',
            FOREIGN KEY(listing_id) REFERENCES listings(id),
            FOREIGN KEY(buyer_id) REFERENCES users(user_id)
        );
        CREATE TABLE IF NOT EXISTS reports(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            listing_id INTEGER, reporter_id INTEGER NOT NULL,
            reason TEXT NOT NULL, created_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'new'
        );
        ''')
        await db.commit()

async def notify_admins(bot: Bot, text: str, reply_markup=None):
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text, reply_markup=reply_markup)
        except Exception as e:
            log.warning("Admin %s ga xabar yuborilmadi: %s", admin_id, e)

async def register_user(message: Message, bot: Bot):
    u = message.from_user
    old = await db_fetchone("SELECT user_id FROM users WHERE user_id=?", (u.id,))
    await db_execute("INSERT INTO users(user_id,username,full_name,created_at) VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, full_name=excluded.full_name",
                     (u.id, u.username or "", u.full_name, datetime.utcnow().isoformat(timespec="seconds")))
    if old is None:
        await notify_admins(bot, f"👤 <b>Yangi foydalanuvchi</b>\nIsm: {u.full_name}\nID: <code>{u.id}</code>\nUsername: @{u.username or 'yo‘q'}")

async def main_menu(message: Message):
    await message.answer("🎮 <b>PUBG Marketplace</b>\nE'lonlarni ko'ring yoki moderatsiyaga yangi e'lon yuboring.", reply_markup=MENU)

async def start(message: Message, bot: Bot):
    await register_user(message, bot)
    await main_menu(message)

async def begin_listing(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(ListingForm.title)
    await message.answer("E'lon sarlavhasini yozing. Masalan: PUBG Mobile profil — 3 ta mythic skin.\n\n/parastop — bekor qilish")

async def cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Jarayon bekor qilindi.", reply_markup=MENU)

async def title_step(message: Message, state: FSMContext):
    title = (message.text or "").strip()
    if len(title) < 5 or len(title) > 90:
        return await message.answer("Sarlavha 5–90 belgidan iborat bo'lsin.")
    await state.update_data(title=title)
    await state.set_state(ListingForm.price)
    await message.answer("Narxini so'mda kiriting (faqat raqam).")

async def price_step(message: Message, state: FSMContext):
    raw = (message.text or "").replace(" ", "").replace(",", "")
    if not raw.isdigit() or not (1 <= int(raw) <= 1_000_000_000):
        return await message.answer("Narxni to'g'ri kiriting. Masalan: 250000")
    await state.update_data(price=int(raw))
    await state.set_state(ListingForm.level)
    await message.answer("Akkaunt levelini kiriting (masalan: 55).")

async def level_step(message: Message, state: FSMContext):
    level = (message.text or "").strip()
    if len(level) > 20 or not level:
        return await message.answer("Levelni qisqa yozing.")
    await state.update_data(level=level)
    await state.set_state(ListingForm.rank)
    await message.answer("Rankini kiriting (masalan: Ace, Crown, Conqueror yoki noma'lum).")

async def rank_step(message: Message, state: FSMContext):
    rank = (message.text or "").strip()
    if len(rank) > 40 or not rank:
        return await message.answer("Rankni qayta kiriting.")
    await state.update_data(rank=rank)
    await state.set_state(ListingForm.description)
    await message.answer("Qo'shimcha tavsif yozing. <b>Login, parol, email yoki kod yubormang.</b>")

async def description_step(message: Message, state: FSMContext, bot: Bot):
    desc = (message.text or "").strip()
    if len(desc) < 10 or len(desc) > 800:
        return await message.answer("Tavsif 10–800 belgidan iborat bo'lsin.")
    data = await state.get_data()
    await state.clear()
    now = datetime.utcnow().isoformat(timespec="seconds")
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("INSERT INTO listings(seller_id,title,price,level,rank,description,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (message.from_user.id, data['title'], data['price'], data['level'], data['rank'], desc, 'pending', now))
        listing_id = cur.lastrowid
        await db.commit()
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Tasdiqlash", callback_data=f"approve:{listing_id}"),
        InlineKeyboardButton(text="❌ Rad etish", callback_data=f"reject:{listing_id}")
    ]])
    admin_text = (f"🆕 <b>Yangi e'lon moderatsiyada</b>\nID: #{listing_id}\nSarlavha: {data['title']}\n"
                  f"Narx: {data['price']:,} so'm\nLevel: {data['level']}\nRank: {data['rank']}\n"
                  f"Sotuvchi ID: <code>{message.from_user.id}</code>\nTavsif: {desc}")
    await notify_admins(bot, admin_text, kb)
    await message.answer(f"✅ E'loningiz #{listing_id} admin ko'rigiga yuborildi. Tasdiqlansa katalogda ko'rinadi.", reply_markup=MENU)

async def show_catalog(message: Message):
    rows = await db_fetchall("SELECT * FROM listings WHERE status='approved' ORDER BY id DESC LIMIT 20")
    if not rows:
        return await message.answer("Hozircha tasdiqlangan e'lon yo'q.", reply_markup=MENU)
    await message.answer("🛍 <b>So'nggi e'lonlar</b>")
    for r in rows:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔎 Batafsil", callback_data=f"view:{r['id']}"),
            InlineKeyboardButton(text="⭐ Saqlash", callback_data=f"fav:{r['id']}")
        ]])
        await message.answer(f"<b>#{r['id']} {r['title']}</b>\n💰 {r['price']:,} so'm\n🎚 Level: {r['level']}\n🏆 Rank: {r['rank']}", reply_markup=kb)

async def view_listing(call: CallbackQuery, bot: Bot):
    listing_id = int(call.data.split(':')[1])
    r = await db_fetchone("SELECT * FROM listings WHERE id=? AND status='approved'", (listing_id,))
    if not r:
        return await call.answer("E'lon topilmadi yoki faol emas.", show_alert=True)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📩 Sotuvchiga so'rov", callback_data=f"buy:{listing_id}")],
        [InlineKeyboardButton(text="🚩 Shikoyat", callback_data=f"report:{listing_id}")]
    ])
    await call.message.answer(f"<b>{r['title']}</b>\n💰 Narx: {r['price']:,} so'm\n🎚 Level: {r['level']}\n🏆 Rank: {r['rank']}\n\n{r['description']}\n\nSotuvchi ID: <code>{r['seller_id']}</code>", reply_markup=kb)
    await call.answer()

async def favorite(call: CallbackQuery):
    listing_id = int(call.data.split(':')[1])
    exists = await db_fetchone("SELECT 1 FROM favorites WHERE user_id=? AND listing_id=?", (call.from_user.id, listing_id))
    if exists:
        await db_execute("DELETE FROM favorites WHERE user_id=? AND listing_id=?", (call.from_user.id, listing_id))
        await call.answer("Sevimlilardan olib tashlandi.")
    else:
        await db_execute("INSERT OR IGNORE INTO favorites(user_id,listing_id) VALUES(?,?)", (call.from_user.id, listing_id))
        await call.answer("Sevimlilarga saqlandi.")

async def show_favorites(message: Message):
    rows = await db_fetchall("SELECT l.* FROM listings l JOIN favorites f ON f.listing_id=l.id WHERE f.user_id=? AND l.status='approved' ORDER BY l.id DESC", (message.from_user.id,))
    if not rows:
        return await message.answer("Sevimlilaringiz hozircha bo'sh.")
    for r in rows:
        await message.answer(f"⭐ <b>#{r['id']} {r['title']}</b>\n💰 {r['price']:,} so'm\n🎚 {r['level']} | 🏆 {r['rank']}")

async def create_inquiry(call: CallbackQuery, bot: Bot):
    listing_id = int(call.data.split(':')[1])
    r = await db_fetchone("SELECT * FROM listings WHERE id=? AND status='approved'", (listing_id,))
    if not r:
        return await call.answer("E'lon topilmadi.", show_alert=True)
    if r['seller_id'] == call.from_user.id:
        return await call.answer("O'zingizning e'loningizga so'rov yubora olmaysiz.", show_alert=True)
    now = datetime.utcnow().isoformat(timespec="seconds")
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("INSERT INTO inquiries(listing_id,buyer_id,created_at) VALUES(?,?,?)", (listing_id, call.from_user.id, now))
        inquiry_id = cur.lastrowid
        await db.commit()
    buyer = await db_fetchone("SELECT username,full_name FROM users WHERE user_id=?", (call.from_user.id,))
    msg = (f"📩 <b>Yangi xaridor so'rovi</b>\nSo'rov: #{inquiry_id}\nE'lon: #{listing_id} — {r['title']}\n"
           f"Xaridor: {buyer['full_name'] if buyer else call.from_user.full_name}\n"
           f"Xaridor ID: <code>{call.from_user.id}</code>\nUsername: @{(buyer['username'] if buyer else '') or 'yo‘q'}")
    await notify_admins(bot, msg)
    try:
        await bot.send_message(r['seller_id'], f"📩 E'loningizga qiziqish bildirildi: <b>#{listing_id} {r['title']}</b>\nXaridor ID: <code>{call.from_user.id}</code>. Bot ichida parol/login yubormang; xavfsiz muloqot qiling.")
    except Exception:
        pass
    await call.answer("So'rovingiz yuborildi. Adminlarga ham xabar berildi.", show_alert=True)

async def report_start(call: CallbackQuery, state: FSMContext):
    listing_id = int(call.data.split(':')[1])
    await state.update_data(report_listing_id=listing_id)
    await state.set_state(ListingForm.report_reason)
    await call.message.answer("Shikoyat sababini yozing:")
    await call.answer()

async def report_reason(message: Message, state: FSMContext, bot: Bot):
    reason = (message.text or '').strip()
    if len(reason) < 5 or len(reason) > 500:
        return await message.answer("Sabab 5–500 belgi bo'lsin.")
    data = await state.get_data(); await state.clear()
    now = datetime.utcnow().isoformat(timespec="seconds")
    await db_execute("INSERT INTO reports(listing_id,reporter_id,reason,created_at) VALUES(?,?,?,?)",
                     (data.get('report_listing_id'), message.from_user.id, reason, now))
    await notify_admins(bot, f"🚩 <b>Yangi shikoyat</b>\nE'lon: #{data.get('report_listing_id')}\nFoydalanuvchi: <code>{message.from_user.id}</code>\nSabab: {reason}")
    await message.answer("Shikoyat adminlarga yuborildi. Rahmat.", reply_markup=MENU)

async def my_listings(message: Message):
    rows = await db_fetchall("SELECT id,title,price,status FROM listings WHERE seller_id=? ORDER BY id DESC LIMIT 20", (message.from_user.id,))
    if not rows:
        return await message.answer("Sizda hali e'lon yo'q.")
    text = "📦 <b>Mening e'lonlarim</b>\n\n" + "\n".join(f"#{r['id']} {r['title']} — {r['price']:,} so'm ({r['status']})" for r in rows)
    await message.answer(text)

async def rules(message: Message):
    await message.answer("ℹ️ <b>Qoidalar</b>\n1. Login, parol, email va tasdiqlash kodlarini botga yubormang.\n2. Noto'g'ri yoki chalg'ituvchi e'lon joylamang.\n3. Shikoyatlar admin tomonidan ko'rib chiqiladi.\n4. PUBG akkauntini sotish/o'tkazish o'yin qoidalariga zid bo'lishi mumkin; akkauntingiz xavfsizligini xavf ostiga qo'ymang.\n5. Bu bot to'lov yoki akkaunt topshirishni amalga oshirmaydi.")

async def help_msg(message: Message):
    await message.answer("Yordam: e'lonlar va shikoyatlar adminlar tomonidan ko'rib chiqiladi. Savol uchun admin bilan bog'laning.")

async def admin_panel(message: Message):
    if not is_admin(message.from_user.id):
        return await message.answer("Bu bo'lim faqat adminlar uchun.")
    users = await db_fetchone("SELECT COUNT(*) n FROM users")
    pending = await db_fetchone("SELECT COUNT(*) n FROM listings WHERE status='pending'")
    approved = await db_fetchone("SELECT COUNT(*) n FROM listings WHERE status='approved'")
    reports = await db_fetchone("SELECT COUNT(*) n FROM reports WHERE status='new'")
    await message.answer(f"🛠 <b>Admin panel</b>\nFoydalanuvchilar: {users['n']}\nKutilayotgan e'lonlar: {pending['n']}\nTasdiqlangan e'lonlar: {approved['n']}\nYangi shikoyatlar: {reports['n']}\n\n/pending — kutilayotgan e'lonlar")

async def pending_listings(message: Message):
    if not is_admin(message.from_user.id): return
    rows = await db_fetchall("SELECT * FROM listings WHERE status='pending' ORDER BY id ASC LIMIT 20")
    if not rows: return await message.answer("Kutilayotgan e'lon yo'q.")
    for r in rows:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Tasdiqlash", callback_data=f"approve:{r['id']}"),
            InlineKeyboardButton(text="❌ Rad etish", callback_data=f"reject:{r['id']}")
        ]])
        await message.answer(f"#{r['id']} {r['title']}\nNarx: {r['price']:,}\nSotuvchi: {r['seller_id']}\n{r['description']}", reply_markup=kb)

async def moderate(call: CallbackQuery, bot: Bot):
    if not is_admin(call.from_user.id): return await call.answer("Ruxsat yo'q.", show_alert=True)
    action, sid = call.data.split(':'); listing_id = int(sid)
    new_status = 'approved' if action == 'approve' else 'rejected'
    r = await db_fetchone("SELECT seller_id,title FROM listings WHERE id=?", (listing_id,))
    if not r: return await call.answer("E'lon topilmadi.", show_alert=True)
    await db_execute("UPDATE listings SET status=? WHERE id=?", (new_status, listing_id))
    try:
        label = 'tasdiqlandi ✅' if new_status == 'approved' else 'rad etildi ❌'
        await bot.send_message(r['seller_id'], f"E'loningiz #{listing_id} {label}: {r['title']}")
    except Exception: pass
    await call.message.edit_reply_markup(reply_markup=None)
    await call.answer("Holat yangilandi.")

async def myid(message: Message):
    await message.answer(f"Sizning Telegram ID: <code>{message.from_user.id}</code>")

def build_dispatcher() -> Dispatcher:
    """Create the dispatcher and register all bot handlers."""
    dp = Dispatcher(storage=MemoryStorage())

    dp.message.register(start, CommandStart())
    dp.message.register(myid, Command("myid"))
    dp.message.register(admin_panel, Command("admin"))
    dp.message.register(pending_listings, Command("pending"))
    dp.message.register(cancel, Command("cancel"))
    dp.message.register(begin_listing, F.text == "➕ E'lon joylash")
    dp.message.register(show_catalog, F.text == "🛍 E'lonlar")
    dp.message.register(my_listings, F.text == "📦 Mening e'lonlarim")
    dp.message.register(show_favorites, F.text == "⭐ Saqlanganlar")
    dp.message.register(rules, F.text == "ℹ️ Qoidalar")
    dp.message.register(help_msg, F.text == "🆘 Yordam")
    dp.message.register(title_step, ListingForm.title)
    dp.message.register(price_step, ListingForm.price)
    dp.message.register(level_step, ListingForm.level)
    dp.message.register(rank_step, ListingForm.rank)
    dp.message.register(description_step, ListingForm.description)
    dp.message.register(report_reason, ListingForm.report_reason)

    dp.callback_query.register(view_listing, F.data.startswith("view:"))
    dp.callback_query.register(favorite, F.data.startswith("fav:"))
    dp.callback_query.register(create_inquiry, F.data.startswith("buy:"))
    dp.callback_query.register(report_start, F.data.startswith("report:"))
    dp.callback_query.register(
        moderate,
        F.data.startswith("approve:") | F.data.startswith("reject:")
    )
    return dp


async def health_check(_: web.Request) -> web.Response:
    """Simple endpoint for Render health checks."""
    return web.json_response({"status": "ok"})


async def run_polling(bot: Bot, dp: Dispatcher) -> None:
    """Optional local development mode. Do not use this on Render Web Service."""
    await init_db()
    log.info("Bot polling rejimida ishga tushmoqda. Adminlar: %s", len(ADMIN_IDS))
    await dp.start_polling(bot)


def main() -> None:
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = build_dispatcher()

    if RUN_MODE == "polling":
        asyncio.run(run_polling(bot, dp))
        return

    if RUN_MODE != "webhook":
        raise RuntimeError("RUN_MODE 'webhook' yoki 'polling' bo'lishi kerak.")

    if not BASE_WEBHOOK_URL:
        raise RuntimeError(
            "Webhook URL topilmadi. Render Web Service'da RENDER_EXTERNAL_URL mavjud "
            "bo'lishi kerak yoki WEBHOOK_URL environment variable kiriting."
        )

    async def on_startup(bot: Bot) -> None:
        await init_db()
        webhook_url = f"{BASE_WEBHOOK_URL}{WEBHOOK_PATH}"
        await bot.set_webhook(
            url=webhook_url,
            secret_token=WEBHOOK_SECRET,
            allowed_updates=dp.resolve_used_update_types(),
        )
        log.info("Webhook o'rnatildi: %s", webhook_url)
        log.info("Bot ishga tushdi. Adminlar: %s", len(ADMIN_IDS))

    dp.startup.register(on_startup)

    app = web.Application()
    app.router.add_get("/", health_check)
    app.router.add_get("/health", health_check)

    webhook_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=WEBHOOK_SECRET,
    )
    webhook_handler.register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    log.info("HTTP server %s:%s manzilda ishga tushmoqda.", WEB_SERVER_HOST, WEB_SERVER_PORT)
    web.run_app(app, host=WEB_SERVER_HOST, port=WEB_SERVER_PORT)


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, SystemExit):
        log.info("Bot to'xtatildi")
