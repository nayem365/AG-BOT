import os
import re
import asyncio
from datetime import datetime
from typing import Optional, Dict, Any

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.mongo import MongoStorage
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, 
    InlineKeyboardButton, ReplyKeyboardRemove, FSInputFile
)
from aiogram.utils.markdown import hbold

from motor.motor_asyncio import AsyncIOMotorClient
import aiohttp
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
admin_chat_collection = db.admin_chat  # For reply threads

# Bot and dispatcher
bot = Bot(token=BOT_TOKEN)
storage = MongoStorage.from_url(MONGO_URI, db_name="mobicash_bot_fsm")
dp = Dispatcher(storage=storage)

# -------------------- FSM States --------------------
class RegisterStates(StatesGroup):
    start = State()
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
    registered = State()  # pending approval

class AdminReplyStates(StatesGroup):
    waiting_for_rejection = State()

# -------------------- Helper Functions --------------------
def validate_name(name: str) -> bool:
    """Validate name: 2-4 words, allowed chars (A-Z a-z А-Я а-я French), no all caps, max 40 chars."""
    name = name.strip()
    if len(name) > 40:
        return False
    # Allow letters (English, Russian, French diacritics), hyphens, apostrophes, periods, spaces
    # French letters: àâäéèêëîïôöùûüÿç - we'll use Unicode category L + basic punctuation
    pattern = r"^[A-Za-zÀ-ÿА-Яа-я'\-\.]+(?:\s+[A-Za-zÀ-ÿА-Яа-я'\-\.]+){1,3}$"
    if not re.match(pattern, name):
        return False
    # Check not all uppercase (allow normal case)
    if name.isupper():
        return False
    # Count words (split by space)
    words = name.split()
    if not (2 <= len(words) <= 4):
        return False
    return True

def validate_gaming_id(gid: str) -> bool:
    """Numeric, 9-11 digits."""
    return bool(re.fullmatch(r"\d{9,11}", gid.strip()))

async def get_country_from_coords(lat: float, lon: float) -> Optional[str]:
    """Reverse geocoding using Nominatim."""
    async with aiohttp.ClientSession() as session:
        url = "https://nominatim.openstreetmap.org/reverse"
        params = {
            "lat": lat,
            "lon": lon,
            "format": "json",
            "accept-language": "en"
        }
        headers = {"User-Agent": "MobicashBot/1.0"}
        try:
            async with session.get(url, params=params, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    country = data.get("address", {}).get("country")
                    return country
        except:
            return None
    return None

def get_currency_for_country(country: str) -> str:
    """Simple mapping for demo (add more as needed)."""
    mapping = {
        "United States": "USD",
        "Canada": "CAD",
        "United Kingdom": "GBP",
        "European Union": "EUR",
        "Germany": "EUR",
        "France": "EUR",
        "Russia": "RUB",
        "India": "INR",
    }
    return mapping.get(country, "USD")  # fallback to USD

async def save_user_data(user_id: int, data: dict):
    """Save or update user data in MongoDB."""
    await users_collection.update_one(
        {"user_id": user_id},
        {"$set": {"data": data, "updated_at": datetime.utcnow()}},
        upsert=True
    )

async def get_user_data(user_id: int) -> Optional[dict]:
    doc = await users_collection.find_one({"user_id": user_id})
    return doc.get("data") if doc else None

async def set_user_status(user_id: int, status: str, rejection_msg: str = None):
    """status: pending, approved, rejected"""
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
    # Check if already registered (pending, approved, rejected)
    status = await get_user_status(user.id)
    if status in ["pending", "approved"]:
        await message.answer(
            f"Welcome back, {user.first_name}!\n"
            f"Your registration is {status.upper()}. Use the button below to check status.",
            reply_markup=get_main_menu_keyboard()
        )
        return
    elif status == "rejected":
        await message.answer(
            "Your registration was rejected. Please contact admin if you think this is a mistake.\n"
            "Use /start to begin a new registration."
        )
        return

    # New user -> show agreement
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
    await callback.message.edit_text(
        "✅ You have agreed to the terms.\n\n"
        "📍 Please share your current location using the button below.\n"
        "*(Only works on smartphones)*"
    )
    await callback.message.answer(
        "Tap the button to share location:",
        reply_markup=get_location_keyboard()
    )

@dp.message(RegisterStates.location, F.location)
async def location_received(message: types.Message, state: FSMContext):
    lat = message.location.latitude
    lon = message.location.longitude
    # Store raw coordinates
    await state.update_data(location={"lat": lat, "lon": lon})
    # Reverse geocode to get country
    country = await get_country_from_coords(lat, lon)
    if not country:
        country = "Unknown"
    await state.update_data(country=country)
    await message.answer(
        f"📍 Location received. Your country: {country}\n\n"
        "📞 Next step: share your phone number.",
        reply_markup=get_phone_keyboard()
    )
    await state.set_state(RegisterStates.phone)

@dp.message(RegisterStates.location)
async def location_missing(message: types.Message, state: FSMContext):
    await message.answer("Please share your location using the button below.", reply_markup=get_location_keyboard())

@dp.message(RegisterStates.phone, F.contact)
async def phone_received(message: types.Message, state: FSMContext):
    contact = message.contact
    phone = contact.phone_number
    await state.update_data(phone=phone)
    await message.answer(
        f"✅ Phone number saved: {phone}\n\n"
        "✏️ Now, enter your real first and last name as in your ID/passport.\n"
        "Requirements:\n"
        "• 2-4 words\n"
        "• English, Russian, or French letters (no numbers, not all caps)\n"
        "• Hyphens, apostrophes, periods allowed\n"
        "• Max 40 characters\n\n"
        "Example: John Doe, Jean-Pierre Dupont, Иван Петров",
        reply_markup=ReplyKeyboardRemove()
    )
    await state.set_state(RegisterStates.name)

@dp.message(RegisterStates.phone)
async def phone_missing(message: types.Message, state: FSMContext):
    await message.answer("Please share your phone number using the button below.", reply_markup=get_phone_keyboard())

@dp.message(RegisterStates.name)
async def name_received(message: types.Message, state: FSMContext):
    name = message.text.strip()
    if not validate_name(name):
        await message.answer(
            "❌ Invalid name.\n"
            "Make sure:\n"
            "• 2-4 words\n"
            "• Only letters (English/Russian/French), hyphens, apostrophes, periods\n"
            "• Not all uppercase\n"
            "• Max 40 characters\n\n"
            "Please try again:"
        )
        return
    await state.update_data(full_name=name)
    # Now ask for currency - but we need country from location (already in state)
    data = await state.get_data()
    country = data.get("country", "Unknown")
    local_currency = get_currency_for_country(country)
    await state.update_data(local_currency=local_currency)
    await message.answer(
        f"✅ Name accepted: {name}\n\n"
        f"🌍 Your country: {country}\n"
        f"💰 Select your preferred currency (only one):",
        reply_markup=get_currency_keyboard(local_currency)
    )
    await state.set_state(RegisterStates.currency)

@dp.callback_query(RegisterStates.currency, F.data.startswith("currency_"))
async def currency_selected(callback: types.CallbackQuery, state: FSMContext):
    currency = callback.data.split("_")[1].upper()
    await state.update_data(selected_currency=currency)
    await callback.answer(f"Currency: {currency}")
    await callback.message.edit_text(
        f"💱 Currency set to: {currency}\n\n"
        "📄 Now, please send a photo of your **Identity Document** (Passport / ID Card / Driving License)."
    )
    await state.set_state(RegisterStates.id_photo)

@dp.message(RegisterStates.id_photo, F.photo)
async def id_photo_received(message: types.Message, state: FSMContext):
    photo = message.photo[-1]  # largest size
    file_id = photo.file_id
    await state.update_data(id_photo_file_id=file_id)
    await message.answer(
        "✅ First document received.\n\n"
        "📸 Now, please send **another photo** (e.g., selfie with document or second ID)."
    )
    await state.set_state(RegisterStates.second_photo)

@dp.message(RegisterStates.id_photo)
async def id_photo_missing(message: types.Message, state: FSMContext):
    await message.answer("Please send a photo of your identity document (Passport/ID/License).")

@dp.message(RegisterStates.second_photo, F.photo)
async def second_photo_received(message: types.Message, state: FSMContext):
    photo = message.photo[-1]
    file_id = photo.file_id
    await state.update_data(second_photo_file_id=file_id)
    # Show example image (you need to upload an example photo to Telegram first or send a URL)
    example_path = "experience_example.jpg"  # Place an example image in your bot directory
    if os.path.exists(example_path):
        await message.answer_photo(
            FSInputFile(example_path),
            caption="ℹ️ Example: Yes/No selection\n\n"
                    "Do you have experience working with the MobCash mobile app?"
        )
    else:
        await message.answer(
            "Do you have experience working with the MobCash mobile app?\n"
            "Select one option:",
            reply_markup=get_experience_keyboard()
        )
        await state.set_state(RegisterStates.experience)
        return

    await message.answer(
        "Do you have experience working with the MobCash mobile app?",
        reply_markup=get_experience_keyboard()
    )
    await state.set_state(RegisterStates.experience)

@dp.message(RegisterStates.second_photo)
async def second_photo_missing(message: types.Message, state: FSMContext):
    await message.answer("Please send the second photo (e.g., selfie or second document).")

@dp.callback_query(RegisterStates.experience, F.data.startswith("exp_"))
async def experience_selected(callback: types.CallbackQuery, state: FSMContext):
    exp = callback.data.split("_")[1]  # "yes" or "no"
    await state.update_data(experience=exp)
    await callback.answer()
    await callback.message.edit_text(
        f"✅ Experience: {'Yes' if exp == 'yes' else 'No'}\n\n"
        "🏠 Please enter your **street name** (only the name, not full address).\n"
        "It will be visible to players picking a cashier for withdrawals."
    )
    await state.set_state(RegisterStates.street)

@dp.message(RegisterStates.street)
async def street_received(message: types.Message, state: FSMContext):
    street = message.text.strip()
    if len(street) < 2:
        await message.answer("Please enter a valid street name (at least 2 characters).")
        return
    await state.update_data(street=street)
    # Show top-up method example photo
    example_path = "topup_example.jpg"
    if os.path.exists(example_path):
        await message.answer_photo(
            FSInputFile(example_path),
            caption="How would you like to top up your account to register as a mobile cash agent?\n"
                    "Choose one method:"
        )
    else:
        await message.answer(
            "How would you like to top up your account to register as a mobile cash agent?\n"
            "Choose one method (you can only use one):",
            reply_markup=get_topup_keyboard()
        )
        await state.set_state(RegisterStates.topup_method)
        return

    await message.answer(
        "How would you like to top up your account?",
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
        "🎮 Now, send your **gaming ID from your 7starswin profile**.\n"
        "It's a numeric ID (9-11 digits). You can copy it from the app or your profile."
    )
    await state.set_state(RegisterStates.gaming_id)

@dp.message(RegisterStates.gaming_id)
async def gaming_id_received(message: types.Message, state: FSMContext):
    gid = message.text.strip()
    if not validate_gaming_id(gid):
        await message.answer(
            "❌ Invalid Gaming ID.\n"
            "It must be a **numeric ID** with **9 to 11 digits**.\n"
            "Please re-check and send the correct number:"
        )
        return
    await state.update_data(gaming_id=gid)
    # All data collected. Save to DB and set status pending
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
                f"🆕 New registration pending!\n"
                f"User: {message.from_user.full_name} (@{message.from_user.username})\n"
                f"ID: {message.from_user.id}\n"
                f"Use /approve {message.from_user.id} or /reject {message.from_user.id}"
            )
        except:
            pass

    await message.answer(
        "✅ **Registration complete!**\n\n"
        "Your application is now pending admin approval.\n"
        "You will be notified once approved or rejected.\n"
        "Use the button below to check your status.",
        reply_markup=get_main_menu_keyboard(),
        parse_mode="Markdown"
    )
    await state.set_state(RegisterStates.registered)

# -------------------- User Status Check --------------------
@dp.message(F.text == "📋 Request Status")
async def check_status(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    status = await get_user_status(user_id)
    if status == "pending":
        await message.answer("⏳ Your registration is **under review**. Please wait for admin approval.")
    elif status == "approved":
        await message.answer("✅ **Approved!** You are now a registered Mobicash agent. Thank you.")
    elif status == "rejected":
        data = await users_collection.find_one({"user_id": user_id})
        rejection_msg = data.get("rejection_msg", "No reason provided.") if data else "No reason provided."
        await message.answer(f"❌ **Rejected**.\nReason: {rejection_msg}\n\nContact admin if you have questions.")
    else:
        await message.answer("You have not started registration. Use /start to begin.")

# -------------------- Admin Commands --------------------
@dp.message(Command("listpending"))
async def list_pending(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("Unauthorized.")
        return
    pending = await users_collection.find({"status": "pending"}).to_list(None)
    if not pending:
        await message.answer("No pending registrations.")
        return
    text = "📋 **Pending users:**\n"
    for user in pending:
        uid = user["user_id"]
        username = user["data"].get("username", "N/A")
        text += f"- ID: `{uid}` (@{username})\n"
    await message.answer(text, parse_mode="Markdown")

@dp.message(Command("approve"))
async def approve_user(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("Unauthorized.")
        return
    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("Usage: /approve <user_id>")
        return
    user_id = int(parts[1])
    user_data = await users_collection.find_one({"user_id": user_id})
    if not user_data:
        await message.answer("User not found.")
        return
    await set_user_status(user_id, "approved")
    await bot.send_message(user_id, "🎉 Congratulations! Your registration has been **approved**. You can now use the Mobicash agent services.")
    await message.answer(f"User {user_id} approved successfully.")

@dp.message(Command("reject"))
async def reject_start(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("Unauthorized.")
        return
    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("Usage: /reject <user_id>\nThen send the rejection reason (text, photo, or video).")
        return
    user_id = int(parts[1])
    # Verify user exists
    user_data = await users_collection.find_one({"user_id": user_id})
    if not user_data:
        await message.answer("User not found.")
        return
    await state.update_data(reject_user_id=user_id)
    await state.set_state(AdminReplyStates.waiting_for_rejection)
    await message.answer(f"Now send the rejection message for user {user_id} (text, photo, or video). This will be forwarded to the user.")

@dp.message(AdminReplyStates.waiting_for_rejection, F.text | F.photo | F.video)
async def reject_send(message: types.Message, state: FSMContext):
    data = await state.get_data()
    user_id = data["reject_user_id"]
    # Save rejection message content
    rejection_content = {}
    if message.text:
        rejection_content["type"] = "text"
        rejection_content["text"] = message.text
        await bot.send_message(user_id, f"❌ Your registration has been rejected.\nReason: {message.text}")
    elif message.photo:
        rejection_content["type"] = "photo"
        rejection_content["file_id"] = message.photo[-1].file_id
        await bot.send_photo(user_id, message.photo[-1].file_id, caption="❌ Your registration has been rejected.")
    elif message.video:
        rejection_content["type"] = "video"
        rejection_content["file_id"] = message.video.file_id
        await bot.send_video(user_id, message.video.file_id, caption="❌ Your registration has been rejected.")
    # Update status and store rejection message
    await set_user_status(user_id, "rejected", rejection_msg=rejection_content.get("text", "Rejected by admin"))
    await message.answer(f"User {user_id} rejected. Notification sent.")
    await state.clear()

@dp.message(AdminReplyStates.waiting_for_rejection)
async def reject_invalid(message: types.Message):
    await message.answer("Please send a text, photo, or video as rejection reason.")

# -------------------- User Replies to Admin Messages --------------------
# When a user replies to a message from admin, forward to admin
@dp.message(F.reply_to_message)
async def handle_user_reply(message: types.Message):
    # Check if the replied message was sent by bot and is a rejection/forwarded admin message
    # Simpler approach: store admin-user conversation mapping in DB? For now, if user replies to any bot message, forward to all admins
    if message.from_user.id in ADMIN_IDS:
        return  # avoid loops
    # Only if user has pending/rejected/approved status? Actually any reply to bot should go to admins
    if message.reply_to_message.from_user.id == bot.id:
        # Forward to all admins
        for admin_id in ADMIN_IDS:
            try:
                await bot.forward_message(admin_id, message.chat.id, message.message_id)
                await bot.send_message(admin_id, f"Reply from user {message.from_user.id} (@{message.from_user.username})")
            except:
                pass

# -------------------- Error Handling & Startup --------------------
@dp.startup()
async def on_startup():
    await bot.send_message(ADMIN_IDS[0], "🤖 Bot started!")

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
