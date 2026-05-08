import os
import re
from datetime import datetime
from typing import Optional

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.mongo import MongoStorage
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup,
    InlineKeyboardButton, ReplyKeyboardRemove, FSInputFile
)

from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv

load_dotenv()

# -------------------- Configuration --------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "").split(",")))

# MongoDB setup
mongo_client = AsyncIOMotorClient(MONGO_URI)
db = mongo_client.mobicash_bot
users_collection = db.users

# Bot and dispatcher
bot = Bot(token=BOT_TOKEN)
storage = MongoStorage.from_url(MONGO_URI, db_name="mobicash_bot_fsm")
dp = Dispatcher(storage=storage)

# -------------------- FSM States --------------------
class RegisterStates(StatesGroup):
    agree = State()
    location = State()
    phone = State()
    name = State()
    currency = State()
    id_photo = State()
    second_photo = State()
    experience = State()
    street = State()
    topup_method = State()
    gaming_id = State()
    registered = State()

class AdminReplyStates(StatesGroup):
    waiting_for_rejection = State()

# -------------------- Helper Functions --------------------
def validate_name(name: str) -> bool:
    """Validate name: 2-4 words, allowed chars, max 40 chars, not all caps."""
    name = name.strip()
    if len(name) > 40:
        return False
    pattern = r"^[A-Za-zÀ-ÿА-Яа-я'\-\.]+(?:\s+[A-Za-zÀ-ÿА-Яа-я'\-\.]+){1,3}$"
    if not re.match(pattern, name):
        return False
    if name.isupper():
        return False
    words = name.split()
    if not (2 <= len(words) <= 4):
        return False
    return True

def validate_gaming_id(gid: str) -> bool:
    return bool(re.fullmatch(r"\d{9,11}", gid.strip()))

async def save_user_data(user_id: int, data: dict):
    await users_collection.update_one(
        {"user_id": user_id},
        {"$set": {"data": data, "updated_at": datetime.utcnow()}},
        upsert=True
    )

async def set_user_status(user_id: int, status: str, rejection_msg: str = None):
    await users_collection.update_one(
        {"user_id": user_id},
        {"$set": {"status": status, "rejection_msg": rejection_msg, "updated_at": datetime.utcnow()}}
    )

async def get_user_status(user_id: int) -> str:
    doc = await users_collection.find_one({"user_id": user_id})
    return doc.get("status", "not_registered") if doc else "not_registered"

# -------------------- Reply Keyboards --------------------
def get_agree_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ I Agree", callback_data="agree_yes")]
    ])

def get_location_keyboard():
    button = KeyboardButton(text="📍 Share Location", request_location=True)
    return ReplyKeyboardMarkup(keyboard=[[button]], resize_keyboard=True, one_time_keyboard=True)

def get_phone_keyboard():
    button = KeyboardButton(text="📱 Share Phone Number", request_contact=True)
    return ReplyKeyboardMarkup(keyboard=[[button]], resize_keyboard=True, one_time_keyboard=True)

def get_currency_keyboard(local_currency: str):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💵 USD (Default)", callback_data="currency_usd")],
        [InlineKeyboardButton(text=f"💶 {local_currency} (Local)", callback_data=f"currency_{local_currency}")]
    ])
    return keyboard

def get_experience_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Yes", callback_data="exp_yes")],
        [InlineKeyboardButton(text="❌ No", callback_data="exp_no")]
    ])

def get_topup_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🪙 USDT (TRC20/ERC20)", callback_data="topup_usdt")],
        [InlineKeyboardButton(text="🔄 Other Cryptocurrency", callback_data="topup_other")]
    ])

def get_main_menu_keyboard():
    keyboard = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📋 Request Status")]],
        resize_keyboard=True
    )
    return keyboard

# -------------------- Handlers --------------------
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    user = message.from_user
    status = await get_user_status(user.id)
    if status in ["pending", "approved"]:
        await message.answer(
            f"Welcome back, {user.first_name}!\nYour registration is {status.upper()}. Use the button below to check status.",
            reply_markup=get_main_menu_keyboard()
        )
        return
    elif status == "rejected":
        await message.answer(
            "Your registration was rejected. Use /start to begin a new registration."
        )
        return

    await state.set_state(RegisterStates.agree)
    await message.answer(
        f"Hello, {user.full_name}!\n\n"
        "📜 **User Agreement**\n"
        "The User Agreement covers the processing of personal data, prohibits copying any bot functions, "
        "and requires non-disclosure of confidential information obtained through the use of both proprietary software "
        "and free distributions that include proprietary elements.\n\n"
        "Do you agree?",
        reply_markup=get_agree_keyboard(),
        parse_mode="Markdown"
    )

@dp.callback_query(F.data == "agree_yes", StateFilter(RegisterStates.agree))
async def agree_callback(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(RegisterStates.location)
    await callback.message.edit_text("✅ You agreed to the terms.\n\n📍 Please share your current location using the button below.")
    await callback.message.answer("Tap the button to share location:", reply_markup=get_location_keyboard())

@dp.message(RegisterStates.location, F.location)
async def location_received(message: types.Message, state: FSMContext):
    lat = message.location.latitude
    lon = message.location.longitude
    await state.update_data(location={"lat": lat, "lon": lon}, country="Unknown")  # No external API
    await message.answer(
        f"📍 Location received.\n\n📞 Next step: share your phone number.",
        reply_markup=get_phone_keyboard()
    )
    await state.set_state(RegisterStates.phone)

@dp.message(RegisterStates.location)
async def location_missing(message: types.Message):
    await message.answer("Please share your location using the button below.", reply_markup=get_location_keyboard())

@dp.message(RegisterStates.phone, F.contact)
async def phone_received(message: types.Message, state: FSMContext):
    phone = message.contact.phone_number
    await state.update_data(phone=phone)
    await message.answer(
        f"✅ Phone saved: {phone}\n\n"
        "✏️ Enter your real first and last name (as in ID/passport).\n"
        "Requirements:\n• 2-4 words\n• English/Russian/French letters, hyphens, apostrophes, periods\n• Max 40 chars\n\n"
        "Example: John Doe or Jean-Pierre Dupont",
        reply_markup=ReplyKeyboardRemove()
    )
    await state.set_state(RegisterStates.name)

@dp.message(RegisterStates.phone)
async def phone_missing(message: types.Message):
    await message.answer("Please share your phone number using the button below.", reply_markup=get_phone_keyboard())

@dp.message(RegisterStates.name)
async def name_received(message: types.Message, state: FSMContext):
    name = message.text.strip()
    if not validate_name(name):
        await message.answer("❌ Invalid name. Please follow the rules and try again.")
        return
    await state.update_data(full_name=name)
    local_currency = "EUR"  # Default fallback, user can select USD or local (but we don't know country)
    # Instead of auto country detection, ask user to select from list? For simplicity, we show USD + a generic local.
    await state.update_data(local_currency=local_currency)
    await message.answer(
        f"✅ Name: {name}\n\n💰 Select your preferred currency:",
        reply_markup=get_currency_keyboard(local_currency)
    )
    await state.set_state(RegisterStates.currency)

@dp.callback_query(RegisterStates.currency, F.data.startswith("currency_"))
async def currency_selected(callback: types.CallbackQuery, state: FSMContext):
    currency = callback.data.split("_")[1].upper()
    await state.update_data(selected_currency=currency)
    await callback.answer(f"Currency: {currency}")
    await callback.message.edit_text(f"💱 Currency set: {currency}\n\n📄 Now send a photo of your **Identity Document** (Passport/ID/License).")
    await state.set_state(RegisterStates.id_photo)

@dp.message(RegisterStates.id_photo, F.photo)
async def id_photo_received(message: types.Message, state: FSMContext):
    file_id = message.photo[-1].file_id
    await state.update_data(id_photo_file_id=file_id)
    await message.answer("✅ First document received.\n\n📸 Now send **another photo** (e.g., selfie with document).")
    await state.set_state(RegisterStates.second_photo)

@dp.message(RegisterStates.id_photo)
async def id_photo_missing(message: types.Message):
    await message.answer("Please send a photo of your identity document.")

@dp.message(RegisterStates.second_photo, F.photo)
async def second_photo_received(message: types.Message, state: FSMContext):
    file_id = message.photo[-1].file_id
    await state.update_data(second_photo_file_id=file_id)
    await message.answer(
        "Do you have experience working with the MobCash mobile app?",
        reply_markup=get_experience_keyboard()
    )
    await state.set_state(RegisterStates.experience)

@dp.message(RegisterStates.second_photo)
async def second_photo_missing(message: types.Message):
    await message.answer("Please send the second photo.")

@dp.callback_query(RegisterStates.experience, F.data.startswith("exp_"))
async def experience_selected(callback: types.CallbackQuery, state: FSMContext):
    exp = callback.data.split("_")[1]
    await state.update_data(experience=exp)
    await callback.answer()
    await callback.message.edit_text(f"✅ Experience: {'Yes' if exp == 'yes' else 'No'}\n\n🏠 Please enter your **street name** (only the name, not full address).")
    await state.set_state(RegisterStates.street)

@dp.message(RegisterStates.street)
async def street_received(message: types.Message, state: FSMContext):
    street = message.text.strip()
    if len(street) < 2:
        await message.answer("Please enter a valid street name (≥2 chars).")
        return
    await state.update_data(street=street)
    await message.answer(
        "How would you like to top up your account? (choose one)",
        reply_markup=get_topup_keyboard()
    )
    await state.set_state(RegisterStates.topup_method)

@dp.callback_query(RegisterStates.topup_method, F.data.startswith("topup_"))
async def topup_selected(callback: types.CallbackQuery, state: FSMContext):
    method = callback.data.split("_")[1]
    await state.update_data(topup_method=method)
    await callback.answer()
    await callback.message.edit_text(
        f"✅ Top-up method: {'USDT' if method == 'usdt' else 'Other Crypto'}\n\n"
        "🎮 Send your **gaming ID from 7starswin profile** (9-11 digits, numbers only)."
    )
    await state.set_state(RegisterStates.gaming_id)

@dp.message(RegisterStates.gaming_id)
async def gaming_id_received(message: types.Message, state: FSMContext):
    gid = message.text.strip()
    if not validate_gaming_id(gid):
        await message.answer("❌ Invalid Gaming ID. Must be 9-11 digits. Try again.")
        return
    await state.update_data(gaming_id=gid)
    user_data = await state.get_data()
    user_data["user_id"] = message.from_user.id
    user_data["username"] = message.from_user.username
    user_data["first_name"] = message.from_user.first_name
    user_data["last_name"] = message.from_user.last_name
    user_data["registered_at"] = datetime.utcnow()

    await save_user_data(message.from_user.id, user_data)
    await set_user_status(message.from_user.id, "pending")

    # Notify admins
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"🆕 New registration pending!\nUser: {message.from_user.full_name} (@{message.from_user.username})\nID: {message.from_user.id}\nUse /approve {message.from_user.id} or /reject {message.from_user.id}"
            )
        except:
            pass

    await message.answer(
        "✅ Registration complete! Your application is pending admin approval.\nUse the button below to check status.",
        reply_markup=get_main_menu_keyboard()
    )
    await state.set_state(RegisterStates.registered)

# -------------------- User Status Check --------------------
@dp.message(F.text == "📋 Request Status")
async def check_status(message: types.Message):
    status = await get_user_status(message.from_user.id)
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

# -------------------- Admin Commands --------------------
@dp.message(Command("listpending"))
async def list_pending(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    pending = await users_collection.find({"status": "pending"}).to_list(None)
    if not pending:
        await message.answer("No pending registrations.")
        return
    text = "📋 Pending users:\n"
    for user in pending:
        uid = user["user_id"]
        username = user["data"].get("username", "N/A")
        text += f"- ID: `{uid}` (@{username})\n"
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
    await set_user_status(user_id, "approved")
    await bot.send_message(user_id, "🎉 Congratulations! Your registration has been approved.")
    await message.answer(f"User {user_id} approved.")

@dp.message(Command("reject"))
async def reject_start(message: types.Message, state: FSMContext):
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
    await state.set_state(AdminReplyStates.waiting_for_rejection)
    await message.answer(f"Send rejection message for user {user_id} (text, photo, or video).")

@dp.message(AdminReplyStates.waiting_for_rejection, F.text | F.photo | F.video)
async def reject_send(message: types.Message, state: FSMContext):
    data = await state.get_data()
    user_id = data["reject_user_id"]
    rejection_text = "Your registration has been rejected."
    if message.text:
        rejection_text = message.text
        await bot.send_message(user_id, f"❌ {rejection_text}")
    elif message.photo:
        await bot.send_photo(user_id, message.photo[-1].file_id, caption="❌ Your registration has been rejected.")
        rejection_text = "Rejected with photo."
    elif message.video:
        await bot.send_video(user_id, message.video.file_id, caption="❌ Your registration has been rejected.")
        rejection_text = "Rejected with video."
    await set_user_status(user_id, "rejected", rejection_msg=rejection_text)
    await message.answer(f"User {user_id} rejected.")
    await state.clear()

# -------------------- User Replies to Admin --------------------
@dp.message(F.reply_to_message)
async def forward_user_reply_to_admin(message: types.Message):
    if message.from_user.id in ADMIN_IDS:
        return
    if message.reply_to_message and message.reply_to_message.from_user.id == bot.id:
        for admin_id in ADMIN_IDS:
            try:
                await bot.forward_message(admin_id, message.chat.id, message.message_id)
                await bot.send_message(admin_id, f"Reply from user {message.from_user.id} (@{message.from_user.username})")
            except:
                pass

# -------------------- Start --------------------
async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
