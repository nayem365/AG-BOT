import os
import re
import sys
import asyncio
import traceback
from datetime import datetime

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup,
    InlineKeyboardButton, ReplyKeyboardRemove
)
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv

load_dotenv()  # loads .env only if present (local development)

# ---------- Configuration ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

# Validate required variables
if not BOT_TOKEN:
    print("ERROR: BOT_TOKEN environment variable not set.", flush=True)
    sys.exit(1)
if not MONGO_URI:
    print("ERROR: MONGO_URI environment variable not set.", flush=True)
    sys.exit(1)
if not ADMIN_IDS:
    print("ERROR: ADMIN_IDS environment variable not set.", flush=True)
    sys.exit(1)

print(f"✅ BOT_TOKEN loaded (first 5 chars: {BOT_TOKEN[:5]}...)", flush=True)
print(f"✅ MONGO_URI loaded (first 10 chars: {MONGO_URI[:10]}...)", flush=True)
print(f"✅ ADMIN_IDS loaded: {ADMIN_IDS}", flush=True)

# ---------- MongoDB client ----------
mongo_client = AsyncIOMotorClient(MONGO_URI)
db = mongo_client.mobicash_bot
users_collection = db.users

# ---------- FSM storage (MongoDB with fallback) ----------
try:
    from aiogram.fsm.storage.mongo import MongoStorage
    storage = MongoStorage.from_url(MONGO_URI, db_name="mobicash_bot_fsm")
    print("✅ Using MongoStorage for FSM", flush=True)
except Exception as e:
    from aiogram.fsm.storage.memory import MemoryStorage
    storage = MemoryStorage()
    print(f"⚠️ MongoStorage failed, using MemoryStorage: {e}", flush=True)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=storage)

# ---------- FSM States ----------
class Form(StatesGroup):
    agree = State()
    location = State()
    phone = State()
    name = State()
    currency = State()
    id_photo = State()
    second_photo = State()
    experience = State()
    street = State()
    topup = State()
    gaming_id = State()
    done = State()

class AdminState(StatesGroup):
    waiting_reject = State()

# ---------- Validators ----------
def validate_name(name: str) -> bool:
    name = name.strip()
    if len(name) > 40:
        return False
    # English, Russian, French letters + hyphens, apostrophes, periods
    pattern = r"^[A-Za-zÀ-ÿА-Яа-я'\-\.]+(?:\s+[A-Za-zÀ-ÿА-Яа-я'\-\.]+){1,3}$"
    if not re.match(pattern, name):
        return False
    if name.isupper():
        return False
    words = name.split()
    return 2 <= len(words) <= 4

def validate_gaming_id(gid: str) -> bool:
    return bool(re.fullmatch(r"\d{9,11}", gid.strip()))

# ---------- Database helpers ----------
async def save_user_data(user_id: int, data: dict):
    await users_collection.update_one(
        {"user_id": user_id},
        {"$set": {"data": data, "updated_at": datetime.utcnow()}},
        upsert=True
    )

async def set_status(user_id: int, status: str, reject_msg: str = None):
    await users_collection.update_one(
        {"user_id": user_id},
        {"$set": {"status": status, "rejection_msg": reject_msg}}
    )

async def get_status(user_id: int) -> str:
    doc = await users_collection.find_one({"user_id": user_id})
    return doc.get("status", "not_started") if doc else "not_started"

# ---------- Keyboards ----------
def agree_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ I Agree", callback_data="agree_yes")]
    ])

def location_kb():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📍 Share Location", request_location=True)]],
        resize_keyboard=True, one_time_keyboard=True
    )

def phone_kb():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Share Phone Number", request_contact=True)]],
        resize_keyboard=True, one_time_keyboard=True
    )

def currency_kb(local_currency: str = "EUR"):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💵 USD (Default)", callback_data="cur_USD")],
        [InlineKeyboardButton(text=f"💶 {local_currency} (Local)", callback_data=f"cur_{local_currency}")]
    ])

def exp_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Yes", callback_data="exp_yes")],
        [InlineKeyboardButton(text="❌ No", callback_data="exp_no")]
    ])

def topup_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🪙 USDT (TRC20/ERC20)", callback_data="topup_usdt")],
        [InlineKeyboardButton(text="🔄 Other Cryptocurrency", callback_data="topup_other")]
    ])

def main_menu():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📋 Request Status")]],
        resize_keyboard=True
    )

# ---------- Handlers ----------
@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    status = await get_status(uid)
    if status == "pending":
        await message.answer("⏳ Your registration is pending approval. Use 'Request Status' button.", reply_markup=main_menu())
        return
    if status == "approved":
        await message.answer("✅ You are already approved. Thank you!", reply_markup=main_menu())
        return
    if status == "rejected":
        await message.answer("❌ Your registration was rejected. Use /start to begin again.")
        return

    await state.set_state(Form.agree)
    await message.answer(
        f"Hello, {message.from_user.full_name}!\n\n"
        "📜 **User Agreement**\n"
        "The User Agreement covers the processing of personal data, prohibits copying any bot functions, "
        "and requires non-disclosure of confidential information obtained through the use of both proprietary "
        "software and free distributions that include proprietary elements.\n\n"
        "Do you agree?",
        reply_markup=agree_kb(),
        parse_mode="Markdown"
    )

@dp.callback_query(F.data == "agree_yes", StateFilter(Form.agree))
async def agree(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(Form.location)
    await callback.message.edit_text("✅ You agreed.\n\n📍 Share your location (smartphone only) via the button below.")
    await callback.message.answer("Tap the button:", reply_markup=location_kb())

@dp.message(Form.location, F.location)
async def got_location(message: types.Message, state: FSMContext):
    await state.update_data(location={"lat": message.location.latitude, "lon": message.location.longitude})
    await message.answer("📍 Location saved.\n\n📞 Share your phone number:", reply_markup=phone_kb())
    await state.set_state(Form.phone)

@dp.message(Form.location)
async def location_missing(message: types.Message):
    await message.answer("Please use the button to share your location.", reply_markup=location_kb())

@dp.message(Form.phone, F.contact)
async def got_phone(message: types.Message, state: FSMContext):
    phone = message.contact.phone_number
    await state.update_data(phone=phone)
    await message.answer(
        f"✅ Phone: {phone}\n\n"
        "✏️ Enter your real first and last name (as in ID/passport).\n"
        "Rules:\n• 2‑4 words\n• English/Russian/French letters, hyphens, apostrophes, periods\n• Max 40 chars\n"
        "Example: John Doe or Jean-Pierre Dupont",
        reply_markup=ReplyKeyboardRemove()
    )
    await state.set_state(Form.name)

@dp.message(Form.phone)
async def phone_missing(message: types.Message):
    await message.answer("Please share your phone number using the button.", reply_markup=phone_kb())

@dp.message(Form.name)
async def got_name(message: types.Message, state: FSMContext):
    name = message.text.strip()
    if not validate_name(name):
        await message.answer("❌ Invalid name. Please follow the rules and try again.")
        return
    await state.update_data(full_name=name)
    await state.update_data(local_currency="EUR")
    await message.answer(f"✅ Name: {name}\n\n💰 Choose currency:", reply_markup=currency_kb("EUR"))
    await state.set_state(Form.currency)

@dp.callback_query(StateFilter(Form.currency), F.data.startswith("cur_"))
async def currency_chosen(callback: types.CallbackQuery, state: FSMContext):
    cur = callback.data.split("_")[1].upper()
    await state.update_data(selected_currency=cur)
    await callback.answer(f"Currency: {cur}")
    await callback.message.edit_text(f"💱 Currency set: {cur}\n\n📄 Send a photo of your **Identity Document** (Passport/ID/License).")
    await state.set_state(Form.id_photo)

@dp.message(Form.id_photo, F.photo)
async def id_photo1(message: types.Message, state: FSMContext):
    await state.update_data(id_photo=message.photo[-1].file_id)
    await message.answer("✅ First document received.\n\n📸 Now send **another photo** (e.g., selfie with document).")
    await state.set_state(Form.second_photo)

@dp.message(Form.id_photo)
async def id_photo1_missing(message: types.Message):
    await message.answer("Please send a photo of your identity document.")

@dp.message(Form.second_photo, F.photo)
async def id_photo2(message: types.Message, state: FSMContext):
    await state.update_data(second_photo=message.photo[-1].file_id)
    await message.answer("Do you have experience working with the MobCash mobile app?", reply_markup=exp_kb())
    await state.set_state(Form.experience)

@dp.message(Form.second_photo)
async def id_photo2_missing(message: types.Message):
    await message.answer("Please send the second photo.")

@dp.callback_query(StateFilter(Form.experience), F.data.startswith("exp_"))
async def experience_chosen(callback: types.CallbackQuery, state: FSMContext):
    exp = callback.data.split("_")[1]
    await state.update_data(experience=exp)
    await callback.answer()
    await callback.message.edit_text(f"✅ Experience: {'Yes' if exp=='yes' else 'No'}\n\n🏠 Enter your street name (only the name, not full address).")
    await state.set_state(Form.street)

@dp.message(Form.street)
async def got_street(message: types.Message, state: FSMContext):
    street = message.text.strip()
    if len(street) < 2:
        await message.answer("Please enter a valid street name (≥2 chars).")
        return
    await state.update_data(street=street)
    await message.answer("How would you like to top up your account? (choose one)", reply_markup=topup_kb())
    await state.set_state(Form.topup)

@dp.callback_query(StateFilter(Form.topup), F.data.startswith("topup_"))
async def topup_chosen(callback: types.CallbackQuery, state: FSMContext):
    method = callback.data.split("_")[1]
    await state.update_data(topup_method=method)
    await callback.answer()
    await callback.message.edit_text(
        f"✅ Top‑up: {'USDT' if method=='usdt' else 'Other Crypto'}\n\n"
        "🎮 Send your **gaming ID from 7starswin profile** (numeric, 9‑11 digits)."
    )
    await state.set_state(Form.gaming_id)

@dp.message(Form.gaming_id)
async def got_gaming_id(message: types.Message, state: FSMContext):
    gid = message.text.strip()
    if not validate_gaming_id(gid):
        await message.answer("❌ Invalid Gaming ID. Must be 9‑11 digits. Try again.")
        return
    await state.update_data(gaming_id=gid)

    # Save all data
    data = await state.get_data()
    data["user_id"] = message.from_user.id
    data["username"] = message.from_user.username
    data["first_name"] = message.from_user.first_name
    data["last_name"] = message.from_user.last_name
    data["registered_at"] = datetime.utcnow()

    await save_user_data(message.from_user.id, data)
    await set_status(message.from_user.id, "pending")

    # Notify admins
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"🆕 New registration pending!\nUser: {message.from_user.full_name} (@{message.from_user.username})\nID: {message.from_user.id}\n"
                f"Commands:\n/approve {message.from_user.id}\n/reject {message.from_user.id}"
            )
        except:
            pass

    await message.answer(
        "✅ Registration complete! Pending admin approval.\nUse the button below to check status.",
        reply_markup=main_menu()
    )
    await state.set_state(Form.done)

# ---------- User status check ----------
@dp.message(F.text == "📋 Request Status")
async def status_check(message: types.Message):
    status = await get_status(message.from_user.id)
    if status == "pending":
        await message.answer("⏳ Your registration is under review.")
    elif status == "approved":
        await message.answer("✅ Approved! You are now a registered Mobicash agent.")
    elif status == "rejected":
        doc = await users_collection.find_one({"user_id": message.from_user.id})
        reason = doc.get("rejection_msg", "No reason provided.") if doc else "No reason."
        await message.answer(f"❌ Rejected.\nReason: {reason}")
    else:
        await message.answer("Use /start to begin registration.")

# ---------- Admin commands ----------
@dp.message(Command("listpending"))
async def list_pending(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    pending = await users_collection.find({"status": "pending"}).to_list(None)
    if not pending:
        await message.answer("No pending registrations.")
        return
    text = "📋 Pending users:\n"
    for u in pending:
        uid = u["user_id"]
        uname = u["data"].get("username", "N/A")
        text += f"- ID: `{uid}` (@{uname})\n"
    await message.answer(text, parse_mode="Markdown")

@dp.message(Command("approve"))
async def approve_user(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("Usage: /approve <user_id>")
        return
    user_id = int(parts[1])
    user = await users_collection.find_one({"user_id": user_id})
    if not user:
        await message.answer("User not found.")
        return
    await set_status(user_id, "approved")
    await bot.send_message(user_id, "🎉 Congratulations! Your registration has been approved.")
    await message.answer(f"User {user_id} approved.")

@dp.message(Command("reject"))
async def reject_cmd(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("Usage: /reject <user_id>")
        return
    user_id = int(parts[1])
    user = await users_collection.find_one({"user_id": user_id})
    if not user:
        await message.answer("User not found.")
        return
    await state.update_data(reject_user_id=user_id)
    await state.set_state(AdminState.waiting_reject)
    await message.answer(f"Send rejection message for user {user_id} (text, photo, or video).")

@dp.message(AdminState.waiting_reject, F.text | F.photo | F.video)
async def reject_send(message: types.Message, state: FSMContext):
    data = await state.get_data()
    user_id = data["reject_user_id"]
    if message.text:
        await bot.send_message(user_id, f"❌ Your registration has been rejected.\nReason: {message.text}")
        reject_text = message.text
    elif message.photo:
        await bot.send_photo(user_id, message.photo[-1].file_id, caption="❌ Your registration has been rejected.")
        reject_text = "Rejected with photo."
    elif message.video:
        await bot.send_video(user_id, message.video.file_id, caption="❌ Your registration has been rejected.")
        reject_text = "Rejected with video."
    else:
        await message.answer("Unsupported media. Send text, photo, or video.")
        return
    await set_status(user_id, "rejected", reject_msg=reject_text)
    await message.answer(f"User {user_id} rejected.")
    await state.clear()

# ---------- Forward user replies to admins ----------
@dp.message(F.reply_to_message)
async def forward_reply(message: types.Message):
    if message.from_user.id in ADMIN_IDS:
        return
    if message.reply_to_message and message.reply_to_message.from_user.id == bot.id:
        for admin_id in ADMIN_IDS:
            try:
                await bot.forward_message(admin_id, message.chat.id, message.message_id)
                await bot.send_message(admin_id, f"Reply from user {message.from_user.id} (@{message.from_user.username})")
            except:
                pass

# ---------- Start bot with error logging ----------
async def main():
    print("== BOT STARTING ==", flush=True)
    print("Deleting webhook...", flush=True)
    await bot.delete_webhook(drop_pending_updates=True)
    print("Webhook deleted. Starting polling...", flush=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print("FATAL UNHANDLED EXCEPTION:", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)
