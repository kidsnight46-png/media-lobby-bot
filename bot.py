import asyncio
import logging
import random
import time
import datetime
import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton,
    CallbackQuery, InputMediaPhoto, InputMediaVideo
)
from aiogram.exceptions import TelegramRetryAfter, TelegramForbiddenError, TelegramBadRequest

# ================== LOGGING SETUP ==================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# ================== CONFIGURATION ==================
BOT_TOKEN = "9257042993:AAH3U-vStUBysdrGVixFakT0AN5cCcXvLTE"
DB_FILE = "lobby.db"
ADMIN_ID = "7651976653"
WELCOME_IMAGE = "https://placehold.co/600x400/png?text=Welcome+to+Media+Lobby"

NAME_EMOJIS = ["🦊", "🐯", "🐶", "🐼", "🦁", "🐰", "🦄", "🐉", "🦉", "🐙", "🦅", "🐺"]
LURKER_LIMIT_SECONDS = 172800  # 48 hours

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()

# Telegram ki 30 msg/sec limit ke andar ultra-fast parallel sending
broadcast_semaphore = asyncio.Semaphore(25)

# ================== RAM STORAGE ==================
ALBUMS = {}
PENDING_MEDIA = {}  # Naye users ki 5 photos/videos hold karne ke liye

# ================== ULTRA-FAST ASYNC SQLITE (WAL MODE) ==================
async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("PRAGMA synchronous=NORMAL;")
        await db.execute('''CREATE TABLE IF NOT EXISTS users (
                            user_id TEXT PRIMARY KEY,
                            alias TEXT,
                            media_count INTEGER,
                            level INTEGER,
                            honor TEXT,
                            banned INTEGER DEFAULT 0,
                            is_approved INTEGER DEFAULT 0,
                            last_active REAL,
                            warned INTEGER DEFAULT 0,
                            streak_count INTEGER DEFAULT 0,
                            last_active_date TEXT DEFAULT '',
                            lurker_warned INTEGER DEFAULT 0
                        )''')
        await db.commit()
    logging.info("Async SQLite Database (WAL Mode) Initialized.")

def get_honor(level: int) -> str:
    ranks = {1: "Beginner 🔰", 2: "Pro 🌟", 3: "Veteran 🏆", 4: "Master 👑"}
    return ranks.get(level, "Legend 💎")

async def ensure_user(user_id_str: str, first_name: str):
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT alias, media_count, level, honor, banned, is_approved, streak_count, last_active_date, lurker_warned FROM users WHERE user_id=?",
            (user_id_str,)
        ) as cursor:
            user = await cursor.fetchone()

        if not user:
            alias = f"{random.choice(NAME_EMOJIS)} {first_name}"
            is_admin = 1 if user_id_str == ADMIN_ID else 0
            current_time = time.time()
            await db.execute(
                "INSERT INTO users (user_id, alias, media_count, level, honor, is_approved, last_active) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id_str, alias, 0, 1, "Beginner 🔰", is_admin, current_time)
            )
            await db.commit()
            user = (alias, 0, 1, "Beginner 🔰", 0, is_admin, 0, "", 0)
            logging.info(f"New User Registered: {alias}")

    return {
        "alias": user[0], "media_count": user[1], "level": user[2],
        "honor": user[3], "banned": bool(user[4]), "is_approved": bool(user[5]),
        "streak_count": user[6], "last_active_date": user[7], "lurker_warned": bool(user[8])
    }

async def update_user_activity(user_id_str: str):
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT media_count, level, streak_count, last_active_date FROM users WHERE user_id=?",
            (user_id_str,)
        ) as cursor:
            user = await cursor.fetchone()

        if not user:
            return False, 1

        new_count = user[0] + 1
        new_level = user[1]
        streak_count = user[2]
        last_date_str = user[3]

        today_str = datetime.date.today().strftime("%Y-%m-%d")
        yesterday_str = (datetime.date.today() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")

        if last_date_str == yesterday_str:
            streak_count += 1
        elif last_date_str != today_str:
            streak_count = 1

        level_up = False
        if new_count % 100 == 0:
            new_level += 1
            level_up = True

        new_honor = get_honor(new_level)
        await db.execute(
            "UPDATE users SET media_count=?, level=?, honor=?, last_active=?, warned=0, streak_count=?, last_active_date=?, lurker_warned=0 WHERE user_id=?",
            (new_count, new_level, new_honor, time.time(), streak_count, today_str, user_id_str)
        )
        await db.commit()
        return level_up, new_level

async def get_stats():
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as c:
            total = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM users WHERE is_approved=1 AND banned=0") as c:
            approved = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM users WHERE is_approved=0 AND banned=0") as c:
            pending = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM users WHERE banned=1") as c:
            banned = (await c.fetchone())[0]
    return total, approved, pending, banned

async def admin_action(user_id_str: str, action: str):
    async with aiosqlite.connect(DB_FILE) as db:
        if action == "approve":
            await db.execute("UPDATE users SET is_approved=1, banned=0 WHERE user_id=?", (user_id_str,))
        elif action == "ban":
            await db.execute("UPDATE users SET banned=1 WHERE user_id=?", (user_id_str,))
        elif action == "unban":
            await db.execute("UPDATE users SET banned=0 WHERE user_id=?", (user_id_str,))
        await db.commit()

# ================== ULTRA-FAST ASYNC BROADCASTER ==================
async def safe_send(chat_id: int, func, **kwargs):
    async with broadcast_semaphore:
        for _ in range(3):
            try:
                await func(chat_id=chat_id, **kwargs)
                break
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)
            except (TelegramForbiddenError, TelegramBadRequest):
                # User ne bot block kar diya -> mark banned
                async with aiosqlite.connect(DB_FILE) as db:
                    await db.execute("UPDATE users SET banned=1 WHERE user_id=?", (str(chat_id),))
                    await db.commit()
                break
            except Exception as e:
                logging.error(f"Send Error ({chat_id}): {e}")
                await asyncio.sleep(1)

async def broadcast_to_lobby(sender_id_str: str, func, **kwargs):
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT user_id, last_active, lurker_warned FROM users WHERE is_approved=1 AND banned=0") as cursor:
            users_list = await cursor.fetchall()

    current_time = time.time()
    tasks = []
    lurkers_to_mark = []

    for u_id, last_active, lurker_warned in users_list:
        if str(u_id) == sender_id_str:
            continue

        # 48H LURKER CHECK
        if last_active and (current_time - last_active) > LURKER_LIMIT_SECONDS:
            if not bool(lurker_warned):
                tasks.append(safe_send(
                    int(u_id), bot.send_message,
                    text="⏸️ <b>Connection Paused!</b>\nYou haven't shared anything in 48 hours. Send media to unpause!"
                ))
                lurkers_to_mark.append((u_id,))
            continue

        tasks.append(safe_send(int(u_id), func, **kwargs))

        # Har 25 messages ek sath fire honge + 1s safe gap
        if len(tasks) >= 25:
            await asyncio.gather(*tasks)
            tasks = []
            await asyncio.sleep(1)

    if tasks:
        await asyncio.gather(*tasks)

    if lurkers_to_mark:
        async with aiosqlite.connect(DB_FILE) as db:
            await db.executemany("UPDATE users SET lurker_warned=1 WHERE user_id=?", lurkers_to_mark)
            await db.commit()

async def broadcast_album(group_id: str, sender_id_str: str, header_text: str):
    await asyncio.sleep(2.0)  # Faster album collection
    album_items = ALBUMS.pop(group_id, [])
    if not album_items:
        return

    media_group = []
    for i, item in enumerate(album_items):
        caption = f"{header_text}\n{item['caption']}" if (i == 0 and item["caption"]) else (header_text if i == 0 else "")
        if item["type"] == "photo":
            media_group.append(InputMediaPhoto(media=item["file_id"], caption=caption))
        else:
            media_group.append(InputMediaVideo(media=item["file_id"], caption=caption))

    await broadcast_to_lobby(sender_id_str, bot.send_media_group, media=media_group)

# ================== ADMIN PANEL COMMANDS ==================
@dp.message(Command("users", "ban", "unban", "approve", "reject", "broadcast", "pin"))
async def admin_controls(message: Message):
    if str(message.from_user.id) != ADMIN_ID:
        return

    parts = message.text.split(" ", 1)
    command = parts[0].lower()

    if command == "/users":
        t, a, p, b = await get_stats()
        text = (
            f"📊 <b>BOT STATISTICS</b>\n━━━━━━━━━━━━━━━━━━\n"
            f"👥 Total Users: {t}\n✅ Approved: {a}\n⏳ Pending: {p}\n🚫 Banned: {b}\n━━━━━━━━━━━━━━━━━━"
        )
        return await message.reply(text)

    if command in ["/ban", "/unban", "/approve", "/reject"]:
        if len(parts) < 2:
            return await message.reply(f"⚠️ Usage: {command} USER_ID")
        target_id = parts[1].strip()

        if command in ["/ban", "/reject"]:
            await admin_action(target_id, "ban")
            await message.reply(f"🚫 User {target_id} Banned/Rejected.")
        else:
            await admin_action(target_id, "approve")
            if command == "/approve":
                try:
                    await bot.send_message(int(target_id), "🎉 <b>Approved by Admin!</b> You can now chat.")
                except Exception:
                    pass
            await message.reply(f"✅ User {target_id} Approved/Unbanned.")

    elif command == "/broadcast":
        if len(parts) < 2:
            return
        asyncio.create_task(broadcast_to_lobby(
            ADMIN_ID, bot.send_message,
            text=f"📢 <b>ADMIN ANNOUNCEMENT</b>\n━━━━━━━━━━━━━━━━━━\n{parts[1]}"
        ))
        await message.reply("✅ Broadcast started in background.")

    elif command == "/pin":
        if len(parts) < 2:
            return
        async def send_and_pin(chat_id: int, text: str):
            msg = await bot.send_message(chat_id, f"📌 <b>PINNED LINK</b>\n━━━━━━━━━━━━━━━━━━\n{text}")
            await bot.pin_chat_message(chat_id, msg.message_id)

        asyncio.create_task(broadcast_to_lobby(ADMIN_ID, send_and_pin, text=parts[1]))
        await message.reply("✅ Pinning message across lobby.")

# ================== START COMMAND ==================
@dp.message(Command("start"))
async def start_info(message: Message):
    user_id_str = str(message.from_user.id)
    u_data = await ensure_user(user_id_str, message.from_user.first_name or "Anonymous")

    if u_data["banned"]:
        return await message.answer("❌ You are banned from this lobby.")

    if not u_data["is_approved"]:
        rem = 5 - len(PENDING_MEDIA.get(user_id_str, []))
        warning = (
            f"👋 <b>Welcome, {u_data['alias']}!</b>\n\n"
            f"🔒 <b>Verification Required</b>\nPlease send <b>{rem} Photos or Videos</b> to get Admin approval."
        )
        try:
            await message.answer_photo(WELCOME_IMAGE, caption=warning)
        except Exception:
            await message.answer(warning)
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 My Profile", callback_data="my_profile"),
         InlineKeyboardButton(text="🌐 Lobby Stats", callback_data="lobby_stats")]
    ])
    await message.answer(
        f"👋 <b>Welcome to the Media Lobby, {u_data['alias']}!</b>\n\nDrop Text, Photo, or Video to broadcast instantly.",
        reply_markup=kb
    )

# ================== INLINE BUTTONS ==================
@dp.callback_query()
async def button_handler(call: CallbackQuery):
    user_id_str = str(call.from_user.id)

    if call.data.startswith("approve_") or call.data.startswith("reject_"):
        if user_id_str != ADMIN_ID:
            return await call.answer("❌ You are not Admin!", show_alert=True)
        action, target_id = call.data.split("_", 1)

        if action == "approve":
            await admin_action(target_id, "approve")
            await call.message.edit_text(f"✅ User <code>{target_id}</code> Approved.")
            try:
                await bot.send_message(int(target_id), "🎉 <b>Congratulations!</b> APPROVED by Admin.")
            except Exception:
                pass
        else:
            await admin_action(target_id, "ban")
            await call.message.edit_text(f"❌ User <code>{target_id}</code> Rejected.")
        return await call.answer()

    u_data = await ensure_user(user_id_str, call.from_user.first_name or "Anonymous")
    back_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="back_start")]])

    if call.data == "my_profile":
        text = (
            f"👤 <b>Your Profile</b>\n━━━━━━━━━━━━━━━━━━\n"
            f"<b>Alias:</b> {u_data['alias']}\n"
            f"<b>Honor:</b> {u_data['honor']}\n"
            f"<b>Level:</b> {u_data['level']}\n"
            f"<b>Streak:</b> 🔥 {u_data['streak_count']} Days\n"
            f"<b>Sent:</b> {u_data['media_count']}\n━━━━━━━━━━━━━━━━━━"
        )
        await call.message.edit_text(text, reply_markup=back_kb)
    elif call.data == "lobby_stats":
        t, a, p, b = await get_stats()
        text = f"🌐 <b>Live Lobby Stats</b>\n━━━━━━━━━━━━━━━━━━\n👥 <b>Active Users:</b> {a}\n━━━━━━━━━━━━━━━━━━"
        await call.message.edit_text(text, reply_markup=back_kb)
    elif call.data == "back_start":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📊 My Profile", callback_data="my_profile"),
             InlineKeyboardButton(text="🌐 Lobby Stats", callback_data="lobby_stats")]
        ])
        await call.message.edit_text("👋 <b>Welcome back!</b>\n\nChoose an option below.", reply_markup=kb)

    await call.answer()

# ================== MAIN LOBBY HANDLER ==================
@dp.message(F.content_type.in_({"text", "photo", "video"}))
async def handle_media(message: Message):
    if message.text and message.text.startswith('/'):
        return

    user_id_str = str(message.from_user.id)
    first_name = message.from_user.first_name or "Anonymous"
    user = await ensure_user(user_id_str, first_name)
    if user["banned"]:
        return

    # 🛑 5-Media Gatekeeper
    if not user["is_approved"]:
        if message.text:
            return await message.reply("⚠️ Send 5 Photos/Videos first for verification.")

        is_photo = message.photo is not None
        file_id = message.photo[-1].file_id if is_photo else message.video.file_id

        PENDING_MEDIA.setdefault(user_id_str, []).append({
            "type": "photo" if is_photo else "video",
            "file_id": file_id
        })
        count = len(PENDING_MEDIA[user_id_str])

        if count < 5:
            await message.reply(f"✅ Received {count}/5. Send {5 - count} more.")
        else:
            await message.reply("⏳ <b>All 5 files received!</b> Pending Admin approval.")
            media_group = [
                InputMediaPhoto(media=item["file_id"]) if item["type"] == "photo"
                else InputMediaVideo(media=item["file_id"])
                for item in PENDING_MEDIA[user_id_str]
            ]
            try:
                await bot.send_media_group(int(ADMIN_ID), media=media_group)
                kb = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="✅ Approve", callback_data=f"approve_{user_id_str}"),
                    InlineKeyboardButton(text="❌ Reject", callback_data=f"reject_{user_id_str}")
                ]])
                await bot.send_message(
                    int(ADMIN_ID),
                    f"🆕 <b>New Request</b>\n<b>User:</b> {user['alias']}\n<b>ID:</b> <code>{user_id_str}</code>",
                    reply_markup=kb
                )
                del PENDING_MEDIA[user_id_str]
            except Exception as e:
                logging.error(f"Gatekeeper error: {e}")
        return

    # Lurker Restore Notification
    if user["lurker_warned"]:
        try:
            await message.reply("▶️ <b>Connection Restored!</b>")
        except Exception:
            pass

    # --- Update DB & Gamification ---
    level_up, new_level = await update_user_activity(user_id_str)
    if level_up:
        try:
            await message.answer(f"🎉 <b>LEVEL UP!</b>\n🔼 Level: {new_level}")
        except Exception:
            pass

    fresh_user = await ensure_user(user_id_str, first_name)
    streak_display = f" 🔥 {fresh_user['streak_count']}" if fresh_user['streak_count'] > 0 else ""
    header_text = f"<b>{fresh_user['alias']}</b>{streak_display}\n🎖 <i>{fresh_user['honor']} (Lvl {fresh_user['level']})</i>\n"

    # --- 1. TEXT ---
    if message.text:
        await message.reply("✅ <i>Broadcasted.</i>")
        full_text = f"{header_text}━━━━━━━━━━\n{message.text}"
        asyncio.create_task(broadcast_to_lobby(user_id_str, bot.send_message, text=full_text))
        return

    # --- 2. PHOTO / VIDEO / ALBUM ---
    is_photo = message.photo is not None
    file_id = message.photo[-1].file_id if is_photo else message.video.file_id
    raw_caption = message.caption or ""
    group_id = message.media_group_id

    if not group_id:
        await message.reply("✅ <i>Broadcasted instantly.</i>")
        caption = f"{header_text}\n{raw_caption}" if raw_caption else header_text
        if is_photo:
            asyncio.create_task(broadcast_to_lobby(user_id_str, bot.send_photo, photo=file_id, caption=caption))
        else:
            asyncio.create_task(broadcast_to_lobby(user_id_str, bot.send_video, video=file_id, caption=caption))
    else:
        if group_id not in ALBUMS:
            ALBUMS[group_id] = []
            await message.reply("✅ <i>Album Broadcasted!</i>")
            asyncio.create_task(broadcast_album(group_id, user_id_str, header_text))

        ALBUMS[group_id].append({
            "type": "photo" if is_photo else "video",
            "file_id": file_id,
            "caption": raw_caption
        })

# ================== 24H INACTIVITY CHECKER ==================
async def check_inactivity():
    while True:
        await asyncio.sleep(3600)  # Har 1 ghante baad check
        limit = time.time() - 86400  # 24 Hours
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute(
                "SELECT user_id FROM users WHERE is_approved=1 AND banned=0 AND warned=0 AND last_active < ?",
                (limit,)
            ) as cursor:
                lazy_users = await cursor.fetchall()

            for (u_id,) in lazy_users:
                try:
                    await bot.send_message(
                        int(u_id),
                        "⚠️ <b>We miss you!</b>\nYou haven't sent anything in 24 hours. Drop a message!"
                    )
                    await db.execute("UPDATE users SET warned=1 WHERE user_id=?", (u_id,))
                except Exception:
                    pass
                await asyncio.sleep(0.1)
            await db.commit()

# ================== MAIN STARTUP (RAILWAY POLLING) ==================
async def main():
    await init_db()
    asyncio.create_task(check_inactivity())
    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("🚀 Ultra-Fast Media Lobby Bot is LIVE on Railway!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
