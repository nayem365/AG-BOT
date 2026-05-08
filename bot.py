import asyncio
import logging
import os
import re
from typing import Dict, Optional, Tuple

import aiohttp
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
)
from motor.motor_asyncio import AsyncIOMotorClient

# ========== CONFIGURATION ==========
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable not set")

MONGO_URI = os.getenv("MONGO_URI")
if not MONGO_URI:
    raise ValueError("MONGO_URI environment variable not set")

ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
if not ADMIN_IDS:
    logging.warning("No ADMIN_IDS set – admin features disabled")

# ========== LOGGING ==========
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ========== DATABASE ==========
mongo_client = AsyncIOMotorClient(MONGO_URI)
db = mongo_client.mobicash_bot
users_collection = db.users

# ========== BOT & DISPATCHER ==========
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ========== REGEX VALIDATION ==========
NAME_REGEX = re.compile(
    r"^[A-Za-zÀ-ÿА-Яа-я'\-\.]+(?:\s+[A-Za-zÀ-ÿА-Яа-я'\-\.]+){1,3}$"
)
GAMING_ID_REGEX = re.compile(r"^\d{9,11}$")

# ========== CURRENCY MAPPING (country -> local currency) ==========
CURRENCY_MAP = {
    "US": "USD", "GB": "GBP", "JP": "JPY", "CH": "CHF", "CA": "CAD",
    "AU": "AUD", "RU": "RUB", "CN": "CNY", "IN": "INR", "BR": "BRL"
}
FALLBACK_CURRENCY = "EUR"

# ========== HELPER FUNCTIONS ==========
async def get_country_from_coords(lat: float, lon: float) -> Optional[str]:
    """Reverse geocode using Nominatim, return country code (e.g. 'US') or None."""
    url = "https://nominatim.openstreetmap.org/reverse"
    params = {
        "lat": lat,
        "lon": lon,
        "format": "json",
        "addressdetails": 1,
        "accept-language": "en"
    }
    headers = {"User-Agent": "MobicashBot/1.0 (your_email@example.com)"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, headers=headers, timeout=10) as resp:
                data = await resp.json()
                addr = data.get("address", {})
                country_code = addr.get("country_code", "").upper()
                return country_code if country_code else None
    except Exception as e:
        logger.error(f"Reverse geocoding failed: {e}")
        return None

async def get_local_currency(lat: float, lon: float) -> str:
    """Return local currency code based on coordinates, fallback to EUR."""
    country = await get_country_from_coords(lat, lon)
    if country and country in CURRENCY_MAP:
        return CURRENCY_MAP[country]
    return FALLBACK_CURRENCY

async def notify_admins(text: str, **kwargs):
    """Send a message to all admins."""
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text, **kwargs)
        except Exception as e:
            logger.error(f"Failed to notify admin {admin_id}: {e}")

async def forward_to_admins(message: types.Message):
    """Forward any user message that is a reply to the bot to all admins."""
    if message.reply_to_message and message.reply_to_message.from_user.id == bot.id:
        for admin_id in ADMIN_IDS:
            try:
                await message.forward(admin_id)
            except Exception as e:
                logger.error(f"Failed to forward to admin {admin_id}: {e}")

# ========== KEYBOARDS ==========
location_kb = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="📍 Share Location", request_location=True)]],
    resize_keyboard=True, one_time_keyboard=True
)
contact_kb = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="📱 Share Phone Number", request_contact=True)]],
    resize_keyboard=True, one_time_keyboard=True
)
status_button_kb = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="📋 Request Status")]],
    resize_keyboard=True
)

def currency_kb(local_currency: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💵 USD", callback_data="currency_USD")],
        [InlineKeyboardButton(text=f"🌍 Local ({local_currency})", callback_data=f"currency_{local_currency}")]
    ])

experience_kb = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="✅ Yes", callback_data="exp_yes")],
    [InlineKeyboardButton(text="❌ No", callback_data="exp_no")]
])
topup_kb = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="🪙 USDT", callback_data="topup_usdt")],
    [InlineKeyboardButton(text="🔄 Other Crypto", callback_data="topup_other")]
])

# ========== FSM STATES ==========
class RegState(StatesGroup):
    waiting_location = State()
    waiting_phone = State()
    waiting_name = State()
    waiting_currency = State()
    waiting_photo1 = State()
    waiting_photo2 = State()
    waiting_experience = State()
    waiting_street = State()
    waiting_topup = State()
    waiting_gaming_id = State()

class AdminRejectState(StatesGroup):
    waiting_reason = State()   # stores target_user_id in state data

# ========== START & AGREEMENT ==========
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    user = await users_collection.find_one({"user_id": user_id})
    if user:
        status = user.get("status")
        if status == "pending":
            await message.answer(
                "⏳ You already have a pending application.\nUse the button below to check status.",
                reply_markup=status_button_kb
            )
            return
        elif status == "approved":
            await message.answer(
                "✅ You are already registered as a Mobicash agent.\n\n"
                "Use the button below to check status.",
                reply_markup=status_button_kb
            )
            return
        elif status == "rejected":
            # Allow restart if previously rejected
            await users_collection.delete_one({"user_id": user_id})
            # fall through to new registration
    # Start agreement
    agree_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ I Agree", callback_data="agree")]
    ])
    await message.answer(
        f"Hello, {message.from_user.first_name}!\n"
        "The User Agreement covers the processing of personal data, prohibits copying any bot functions, "
        "and requires non-disclosure of confidential information obtained through the use of both proprietary "
        "software and free distributions that include proprietary elements.",
        reply_markup=agree_kb
    )

@dp.callback_query(F.data == "agree")
async def agree_callback(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.delete()
    await callback.message.answer(
        "Share your location (only works with smartphones) via the button below.",
        reply_markup=location_kb
    )
    await state.set_state(RegState.waiting_location)
    await callback.answer()

# ========== LOCATION ==========
@dp.message(RegState.waiting_location, F.location)
async def location_received(message: types.Message, state: FSMContext):
    lat = message.location.latitude
    lon = message.location.longitude
    await state.update_data(location=(lat, lon))
    await message.answer(
        "To register, enter your phone number - just click the button start.",
        reply_markup=contact_kb
    )
    await state.set_state(RegState.waiting_phone)

@dp.message(RegState.waiting_location)
async def location_invalid(message: types.Message):
    await message.answer("Please share your location using the button below.", reply_markup=location_kb)

# ========== PHONE ==========
@dp.message(RegState.waiting_phone, F.contact)
async def phone_received(message: types.Message, state: FSMContext):
    phone = message.contact.phone_number
    await state.update_data(phone=phone)
    await message.answer(
        "Now please enter your full name.\n"
        "Rules: 2‑4 words, English/Russian/French letters, punctuation: hyphen, apostrophe, period.\n"
        "Example: John Doe, Jean-Pierre Dupont, Иван Петров",
        reply_markup=ReplyKeyboardRemove()
    )
    await state.set_state(RegState.waiting_name)

@dp.message(RegState.waiting_phone)
async def phone_invalid(message: types.Message):
    await message.answer("Please share your phone number using the button below.", reply_markup=contact_kb)

# ========== NAME VALIDATION ==========
@dp.message(RegState.waiting_name)
async def name_received(message: types.Message, state: FSMContext):
    name = message.text.strip()
    if not NAME_REGEX.match(name) or name.isupper():
        await message.answer(
            "❌ Invalid name. Please use a real name (2‑4 words, letters and allowed punctuation, not all caps).\n"
            "Example: John Doe, Jean-Pierre Dupont, Иван Петров"
        )
        return
    if len(name) > 40:
        await message.answer("Name is too long (max 40 characters). Please shorten it.")
        return
    await state.update_data(full_name=name)
    # Get currency options – need coordinates from saved data
    data = await state.get_data()
    lat, lon = data["location"]
    local_currency = await get_local_currency(lat, lon)
    await state.update_data(local_currency=local_currency)
    await message.answer(
        "Select your preferred currency:",
        reply_markup=currency_kb(local_currency)
    )
    await state.set_state(RegState.waiting_currency)

# ========== CURRENCY ==========
@dp.callback_query(RegState.waiting_currency, F.data.startswith("currency_"))
async def currency_selected(callback: types.CallbackQuery, state: FSMContext):
    currency = callback.data.split("_")[1]
    await state.update_data(currency=currency)
    await callback.message.delete()
    await callback.message.answer(
        "Share with us Your Real Identity Document Photo (Passport/ID Document/Driving License)"
    )
    await state.set_state(RegState.waiting_photo1)
    await callback.answer()

# ========== PHOTO 1 & 2 ==========
@dp.message(RegState.waiting_photo1, F.photo)
async def photo1_received(message: types.Message, state: FSMContext):
    file_id = message.photo[-1].file_id
    await state.update_data(photo1_id=file_id)
    await message.answer("Send another photo also.")
    await state.set_state(RegState.waiting_photo2)

@dp.message(RegState.waiting_photo1)
async def photo1_invalid(message: types.Message):
    await message.answer("Please send a photo (an image file).")

@dp.message(RegState.waiting_photo2, F.photo)
async def photo2_received(message: types.Message, state: FSMContext):
    file_id = message.photo[-1].file_id
    await state.update_data(photo2_id=file_id)
    # Example photo placeholder (optional)
    await message.answer_photo(
        "https://via.placeholder.com/300?text=Example+ID+Photo",
        caption="Example of required document photo (just a placeholder)."
    )
    await message.answer("Do you have experience working with the MobCash mobile app?", reply_markup=experience_kb)
    await state.set_state(RegState.waiting_experience)

@dp.message(RegState.waiting_photo2)
async def photo2_invalid(message: types.Message):
    await message.answer("Please send a second photo.")

# ========== EXPERIENCE ==========
@dp.callback_query(RegState.waiting_experience, F.data.in_(["exp_yes", "exp_no"]))
async def experience_selected(callback: types.CallbackQuery, state: FSMContext):
    experience = (callback.data == "exp_yes")
    await state.update_data(experience=experience)
    await callback.message.delete()
    await callback.message.answer(
        "Please enter your street name:\n"
        "- It needs to be understandable and easily readable\n"
        "- Only enter the name, not the entire address\n"
        "- It will be visible to players picking a cashier to withdraw funds from"
    )
    await state.set_state(RegState.waiting_street)
    await callback.answer()

# ========== STREET ==========
@dp.message(RegState.waiting_street)
async def street_received(message: types.Message, state: FSMContext):
    street = message.text.strip()
    if len(street) < 2:
        await message.answer("Street name must be at least 2 characters. Try again.")
        return
    await state.update_data(street=street)
    # Show top-up options
    await message.answer(
        "How would you like to top up your account to register as a mobile cash register agent "
        "(you can only use one of the international methods listed below)?\n"
        "Choose the option that works best for you.",
        reply_markup=topup_kb
    )
    await state.set_state(RegState.waiting_topup)

# ========== TOP-UP METHOD ==========
@dp.callback_query(RegState.waiting_topup, F.data.in_(["topup_usdt", "topup_other"]))
async def topup_selected(callback: types.CallbackQuery, state: FSMContext):
    method = "USDT" if callback.data == "topup_usdt" else "Other Crypto"
    await state.update_data(topup_method=method)
    await callback.message.delete()
    await callback.message.answer(
        "Send your gaming ID from your 7starswin profile to the chat.\n"
        "(It's a number ID, you can copy it in the app or your 7starswin profile)"
    )
    await state.set_state(RegState.waiting_gaming_id)
    await callback.answer()

# ========== GAMING ID + REGISTRATION COMPLETE ==========
@dp.message(RegState.waiting_gaming_id)
async def gaming_id_received(message: types.Message, state: FSMContext):
    gid = message.text.strip()
    if not GAMING_ID_REGEX.match(gid):
        await message.answer("❌ Invalid Gaming ID. Must be 9‑11 digits. Try again.")
        return
    await state.update_data(gaming_id=gid)
    data = await state.get_data()
    # Prepare user document
    user_doc = {
        "user_id": message.from_user.id,
        "username": message.from_user.username,
        "first_name": message.from_user.first_name,
        "location": data["location"],
        "phone": data["phone"],
        "full_name": data["full_name"],
        "currency": data["currency"],
        "photo1_id": data["photo1_id"],
        "photo2_id": data["photo2_id"],
        "experience": data["experience"],
        "street": data["street"],
        "topup_method": data["topup_method"],
        "gaming_id": gid,
        "status": "pending",
        "rejection_reason": None,
        "created_at": message.date,
        "updated_at": message.date
    }
    await users_collection.update_one(
        {"user_id": message.from_user.id},
        {"$set": user_doc},
        upsert=True
    )
    # Notify admins
    admin_text = (
        f"🆕 New registration pending!\n"
        f"User: {message.from_user.full_name} (@{message.from_user.username})\n"
        f"User ID: {message.from_user.id}\n"
        f"Phone: {data['phone']}\n"
        f"Gaming ID: {gid}\n"
        f"Currency: {data['currency']}\n"
        f"Experience: {'Yes' if data['experience'] else 'No'}"
    )
    await notify_admins(admin_text)
    # Confirm to user
    await message.answer(
        "✅ Registration complete! Your application is pending admin approval.\n"
        "Use the button below to check status.",
        reply_markup=status_button_kb
    )
    await state.clear()

@dp.message(RegState.waiting_gaming_id)
async def gaming_id_invalid(message: types.Message):
    await message.answer("Please send a numeric gaming ID (9‑11 digits).")

# ========== STATUS BUTTON ==========
@dp.message(F.text == "📋 Request Status")
async def request_status(message: types.Message):
    user = await users_collection.find_one({"user_id": message.from_user.id})
    if not user:
        await message.answer("No registration found. Please start with /start")
        return
    status = user.get("status")
    if status == "pending":
        await message.answer("⏳ Your registration is under review.")
    elif status == "approved":
        await message.answer("✅ Approved! You are now a registered Mobicash agent.")
    elif status == "rejected":
        reason = user.get("rejection_reason", "No reason provided")
        await message.answer(f"❌ Rejected. Reason: {reason}")
    else:
        await message.answer("Unknown status. Please contact support.")

# ========== ADMIN COMMANDS ==========
def admin_only(handler):
    async def wrapper(message: types.Message, *args, **kwargs):
        if message.from_user.id not in ADMIN_IDS:
            await message.answer("⛔ Access denied.")
            return
        return await handler(message, *args, **kwargs)
    return wrapper

@dp.message(Command("listpending"))
@admin_only
async def list_pending(message: types.Message):
    pending_users = users_collection.find({"status": "pending"})
    pending_list = []
    async for user in pending_users:
        name = user.get("full_name", "Unknown")
        uid = user["user_id"]
        uname = user.get("username", "no_username")
        pending_list.append(f"• {name} (`{uid}`) @{uname}")
    if not pending_list:
        await message.answer("No pending registrations.")
    else:
        await message.answer(
            "Pending registrations:\n" + "\n".join(pending_list),
            parse_mode="Markdown"
        )

@dp.message(Command("approve"))
@admin_only
async def approve_user(message: types.Message):
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Usage: /approve <user_id>")
        return
    user_id = int(parts[1])
    result = await users_collection.update_one(
        {"user_id": user_id, "status": "pending"},
        {"$set": {"status": "approved", "updated_at": message.date}}
    )
    if result.modified_count == 0:
        await message.answer("User not found or not pending.")
        return
    # Notify user
    try:
        await bot.send_message(
            user_id,
            "✅ Your registration has been approved! You are now a registered Mobicash agent.\n"
            "Use the button below to check status.",
            reply_markup=status_button_kb
        )
    except Exception as e:
        logger.error(f"Could not notify user {user_id}: {e}")
    await message.answer(f"User {user_id} approved.")

@dp.message(Command("reject"))
@admin_only
async def reject_init(message: types.Message, state: FSMContext):
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Usage: /reject <user_id>")
        return
    user_id = int(parts[1])
    user = await users_collection.find_one({"user_id": user_id, "status": "pending"})
    if not user:
        await message.answer("User not found or not pending.")
        return
    await state.update_data(target_user_id=user_id)
    await state.set_state(AdminRejectState.waiting_reason)
    await message.answer(
        "Send the rejection reason as text, photo, or video.\n"
        "The content will be forwarded to the user."
    )

@dp.message(AdminRejectState.waiting_reason)
@admin_only
async def reject_reason(message: types.Message, state: FSMContext):
    data = await state.get_data()
    user_id = data.get("target_user_id")
    if not user_id:
        await state.clear()
        await message.answer("Session expired. Use /reject again.")
        return
    # Save rejection reason (text or media caption)
    rejection_text = ""
    if message.text:
        rejection_text = message.text
    elif message.caption:
        rejection_text = message.caption
    else:
        rejection_text = "Reason provided as media (see forwarded message)."
    # Update DB
    await users_collection.update_one(
        {"user_id": user_id, "status": "pending"},
        {"$set": {"status": "rejected", "rejection_reason": rejection_text, "updated_at": message.date}}
    )
    # Forward the rejection message to the user
    try:
        if message.text:
            await bot.send_message(user_id, f"❌ Your registration has been rejected.\nReason: {message.text}")
        else:
            # forward the media (photo/video) + caption if any
            forwarded = await message.forward(user_id)
            if forwarded and message.caption:
                await bot.send_message(user_id, f"❌ Rejection reason: {message.caption}")
            else:
                await bot.send_message(user_id, "❌ Your registration has been rejected. See above for reason.")
    except Exception as e:
        logger.error(f"Could not send rejection to user {user_id}: {e}")
    await message.answer(f"User {user_id} rejected and notified.")
    await state.clear()

# ========== FORWARD REPLIES TO ADMINS ==========
@dp.message(F.reply_to_message)
async def forward_reply(message: types.Message):
    await forward_to_admins(message)

# ========== START BOT ==========
async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Bot started")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
