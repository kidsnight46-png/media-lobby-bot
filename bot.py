import asyncio
import html
import logging
import os
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
BOT_TOKEN = "9194225666:AAGEB4uV2T9pOGq0cvMmEXgTpPqEIbA8rGM"
DB_FILE = "/data/lobby.db"
ADMIN_ID = "8633391285"
ADMIN_CHANNEL_ID = --1004358547046  # <-- YAHAN APNE ADMIN CHANNEL KI ID LAGAYEIN
WELCOME_IMAGE = "https://files.catbox.moe/z5a53h.jpg"
REQUIRED_VERIFY_MEDIA = 5

NAME_EMOJIS = ["🦊", "🐯", "🐶", "🐼", "🦁", "🐰", "🦄", "🐉", "🦉", "🐙", "🦅", "🐺"]

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()

broadcast_semaphore = asyncio.Semaphore(25)
ALBUMS = {}

# ================== ULTRA-FAST ASYNC SQLITE (WAL MODE) ==================
async def init_db():
    os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
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
                            last_active REAL,
                            streak_count INTEGER DEFAULT 0,
                            last_active_date TEXT DEFAULT '',
                            approved INTEGER DEFAULT 0
                        )''')
        # Purane users ke liye automatic migration (sab purane users approved rahenge)
        try:
            await db.execute("ALTER TABLE users ADD COLUMN approved INTEGER DEFAULT 1")
        except Exception:
            pass

        # Admin ko hamesha approved rakhein
        await db.execute("UPDATE users SET approved=1 WHERE user_id=?", (ADMIN_ID,))

        # Pending verification media table
        await db.execute('''CREATE TABLE IF NOT EXISTS pending_media (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            user_id TEXT,
                            file_id TEXT,
                            media_type TEXT,
                            caption TEXT
                        )''')
        await db.commit()
    logging.info("Async SQLite Database (WAL Mode) Initialized.")

def get_honor(level: int) -> str:
    ranks = {1: "Beginner 🔰", 2: "Pro 🌟", 3: "Veteran 🏆", 4: "Master 👑"}
    return ranks.get(level, "Legend 💎")

async def ensure_user(user_id_str: str, first_name: str):
    safe_name = html.escape(first_name or "Anonymous")
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT alias, media_count, level, honor, banned, streak_count, last_active_date, approved FROM users WHERE user_id=?",
            (user_id_str,)
        ) as cursor:
            user = await cursor.fetchone()

        if not user:
            alias = f"{random.choice(NAME_EMOJIS)} {safe_name}"
            current_time = time.time()
            is_approved = 1 if user_id_str == ADMIN_ID else 0
            await db.execute(
                "INSERT INTO users (user_id, alias, media_count, level, honor, banned, last_active, approved) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id_str, alias, 0, 1, "Beginner 🔰", 0, current_time, is_approved)
            )
            await db.commit()
            user = (alias, 0, 1, "Beginner 🔰", 0, 0, "", is_approved)
            logging.info(f"New User Registered: {alias} (Approved: {is_approved})")

    return {
        "alias": html.escape(user[0]), "media_count": user[1], "level": user[2],
        "honor": user[3], "banned": bool(user[4]),
        "streak_count": user[5], "last_active_date": user[6],
        "approved": bool(user[7])
    }

async def update_user_activity(user_id_str: str, increment: int = 1):
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT media_count, level, streak_count, last_active_date FROM users WHERE user_id=?",
            (user_id_str,)
        ) as cursor:
            user = await cursor.fetchone()

        if not user:
            return False, 1

        old_count = user[0]
        new_count = old_count + increment
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
        if (new_count // 100) > (old_count // 100):
            new_level += 1
            level_up = True

        new_honor = get_honor(new_level)
        await db.execute(
            "UPDATE users SET media_count=?, level=?, honor=?, last_active=?, streak_count=?, last_active_date=? WHERE user_id=?",
            (new_count, new_level, new_honor, time.time(), streak_count, today_str, user_id_str)
        )
        await db.commit()
        return level_up, new_level

async def get_stats():
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as c:
            total = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM users WHERE banned=0 AND approved=1") as c:
            active = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM users WHERE banned=0 AND approved=0") as c:
            pending = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM users WHERE banned=1") as c:
            banned = (await c.fetchone())[0]
    return total, active, pending, banned

async def admin_action(user_id_str: str, action: str):
    async with aiosqlite.connect(DB_FILE) as db:
        if action == "ban":
            await db.execute("UPDATE users SET banned=1 WHERE user_id=?", (user_id_str,))
        elif action == "unban":
            await db.execute("UPDATE users SET banned=0, last_active=? WHERE user_id=?", (time.time(), user_id_str))
        elif action == "unbanall":
            await db.execute("UPDATE users SET banned=0, last_active=?", (time.time(),))
        await db.commit()

# ================== ULTRA-FAST ASYNC BROADCASTER (NO AUTO-BAN) ==================
async def safe_send(chat_id: int, func, **kwargs):
    async with broadcast_semaphore:
        for _ in range(3):
            try:
                await func(chat_id=chat_id, **kwargs)
                break
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)
            except (TelegramForbiddenError, TelegramBadRequest):
                break
            except Exception as e:
                logging.error(f"Send Error ({chat_id}): {e}")
                await asyncio.sleep(1)

async def broadcast_to_lobby(sender_id_str: str, func, **kwargs):
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT user_id FROM users WHERE banned=0 AND approved=1") as cursor:
            users_list = await cursor.fetchall()

    tasks = []
    for (u_id,) in users_list:
        if str(u_id) == sender_id_str:
            continue

        tasks.append(safe_send(int(u_id), func, **kwargs))

        if len(tasks) >= 25:
            await asyncio.gather(*tasks)
            tasks = []
            await asyncio.sleep(1)

    if tasks:
        await asyncio.gather(*tasks)

async def broadcast_album(group_id: str, sender_id_str: str, header_text: str):
    await asyncio.sleep(2.0)
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
@dp.message(Command("users", "ban", "unban", "unbanall", "broadcast", "pin"))
async def admin_controls(message: Message):
    if str(message.from_user.id) != ADMIN_ID:
        return

    parts = message.text.split(" ", 1)
    command = parts[0].lower()

    if command == "/users":
        t, a, p, b = await get_stats()
        text = (
            f"📊 <b>BOT STATISTICS</b>\n━━━━━━━━━━━━━━━━━━\n"
            f"👥 Total Users: {t}\n✅ Active (Approved): {a}\n⏳ Pending: {p}\n🚫 Banned: {b}\n━━━━━━━━━━━━━━━━━━"
        )
        return await message.reply(text)

    if command == "/unbanall":
        await admin_action("", "unbanall")
        return await message.reply("✅ All banned users have been unbanned!")

    if command in ["/ban", "/unban"]:
        if len(parts) < 2:
            return await message.reply(f"⚠️ Usage: {command} USER_ID")
        target_id = parts[1].strip()

        if command == "/ban":
            await admin_action(target_id, "ban")
            await message.reply(f"🚫 User {target_id} Banned.")
        else:
            await admin_action(target_id, "unban")
            await message.reply(f"✅ User {target_id} Unbanned.")

    elif command == "/broadcast":
        if len(parts) < 2:
            return
        asyncio.create_task(broadcast_to_lobby(
            ADMIN_ID, bot.send_message,
            text=f"📢 <b>ADMIN ANNOUNCEMENT</b>\n━━━━━━━━━━━━━━━━━━\n{html.escape(parts[1])}"
        ))
        await message.reply("✅ Broadcast started in background.")

    elif command == "/pin":
        if len(parts) < 2:
            return
        async def send_and_pin(chat_id: int, text: str):
            msg = await bot.send_message(chat_id, f"📌 <b>PINNED LINK</b>\n━━━━━━━━━━━━━━━━━━\n{html.escape(text)}")
            await bot.pin_chat_message(chat_id, msg.message_id)

        asyncio.create_task(broadcast_to_lobby(ADMIN_ID, send_and_pin, text=parts[1]))
        await message.reply("✅ Pinning message across lobby.")

# ================== START COMMAND ==================
@dp.message(Command("start"))
async def start_info(message: Message):
    user_id_str = str(message.from_user.id)
    u_data = await ensure_user(user_id_str, message.from_user.first_name or "Anonymous")

    if u_data["banned"]:
        return await message.answer("❌ You are blocked from this lobby.")

    if not u_data["approved"]:
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute("SELECT COUNT(*) FROM pending_media WHERE user_id=?", (user_id_str,)) as c:
                count = (await c.fetchone())[0]

        remaining = max(0, REQUIRED_VERIFY_MEDIA - count)
        if remaining == 0:
            welcome_text = (
                f"👋 <b>Welcome, {u_data['alias']}!</b>\n\n"
                f"⏳ Aapki <b>5 photos/videos</b> Admin review ke liye ja chuki hain.\n"
                f"Approve hote hi aapko Lobby mein add kar diya jayega."
            )
        else:
            welcome_text = (
                f"👋 <b>Welcome to the Media Lobby, {u_data['alias']}!</b>\n\n"
                f"🔐 <b>Verification Required:</b>\n"
                f"Lobby join karne ke liye pehle <b>{remaining} photos ya videos</b> yahan bhejein.\n"
                f"Admin approve karte hi aap Lobby mein shamil ho jayenge!"
            )
        try:
            await message.answer_photo(WELCOME_IMAGE, caption=welcome_text)
        except Exception:
            await message.answer(welcome_text)
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 My Profile", callback_data="my_profile"),
         InlineKeyboardButton(text="🌐 Lobby Stats", callback_data="lobby_stats")]
    ])
    welcome_text = (
        f"👋 <b>Welcome to the Media Lobby, {u_data['alias']}!</b>\n\n"
        f"Drop Text, Photo, or Video to broadcast instantly."
    )
    try:
        await message.answer_photo(WELCOME_IMAGE, caption=welcome_text, reply_markup=kb)
    except Exception:
        await message.answer(welcome_text, reply_markup=kb)

# ================== INLINE BUTTONS (ADMIN CHANNEL + LOBBY) ==================
@dp.callback_query()
async def button_handler(call: CallbackQuery):
    # --- ADMIN CHANNEL APPROVE / REJECT ---
    if call.data.startswith("approve_") or call.data.startswith("reject_"):
        action, target_id = call.data.split("_", 1)

        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute("SELECT alias, honor, level, streak_count FROM users WHERE user_id=?", (target_id,)) as c:
                user_row = await c.fetchone()
            async with db.execute("SELECT file_id, media_type, caption FROM pending_media WHERE user_id=? ORDER BY id ASC", (target_id,)) as c:
                media_rows = await c.fetchall()

            if action == "approve":
                await db.execute("UPDATE users SET approved=1, last_active=? WHERE user_id=?", (time.time(), target_id))
                await db.execute("DELETE FROM pending_media WHERE user_id=?", (target_id,))
                await db.commit()

                # User ki 5 files lobby mein broadcast karein
                if media_rows and user_row:
                    alias, honor, level, streak = user_row
                    await update_user_activity(target_id, increment=len(media_rows))
                    header_text = f"<b>{html.escape(alias)}</b>\n🎖 <i>{honor} (Lvl {level})</i>\n"

                    media_group = []
                    for i, (f_id, m_type, cap) in enumerate(media_rows[:10]):
                        caption = f"{header_text}\n{cap}" if (i == 0 and cap) else (header_text if i == 0 else "")
                        if m_type == "photo":
                            media_group.append(InputMediaPhoto(media=f_id, caption=caption))
                        else:
                            media_group.append(InputMediaVideo(media=f_id, caption=caption))

                    asyncio.create_task(broadcast_to_lobby(target_id, bot.send_media_group, media=media_group))

                try:
                    await bot.send_message(
                        int(target_id),
                        "🎉 <b>Mubarak ho! Admin ne aapko APPROVE kar diya hai.</b>\n\n"
                        "Aapki bheji hui files Lobby mein share ho gayi hain aur ab aap Lobby ka hissa hain! "
                        "Ab aap koi bhi Text, Photo ya Video bhej sakte hain."
                    )
                except Exception:
                    pass

                await call.message.edit_text(f"{call.message.html_text}\n\n✅ <b>STATUS: APPROVED & BROADCASTED</b>")
                return await call.answer("✅ User Approved!", show_alert=True)

            elif action == "reject":
                await db.execute("DELETE FROM pending_media WHERE user_id=?", (target_id,))
                await db.commit()

                try:
                    await bot.send_message(
                        int(target_id),
                        "❌ <b>Aapki verification REJECT ho gayi hai.</b>\n\n"
                        "Dobara <b>5 sahi photos ya videos</b> bhej kar try karein."
                    )
                except Exception:
                    pass

                await call.message.edit_text(f"{call.message.html_text}\n\n❌ <b>STATUS: REJECTED</b>")
                return await call.answer("❌ User Rejected!", show_alert=True)

    # --- NORMAL USER BUTTONS ---
    user_id_str = str(call.from_user.id)
    u_data = await ensure_user(user_id_str, call.from_user.first_name or "Anonymous")
    if u_data["banned"]:
        return await call.answer("❌ You are blocked.", show_alert=True)

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
        if call.message.photo:
            await call.message.edit_caption(caption=text, reply_markup=back_kb)
        else:
            await call.message.edit_text(text, reply_markup=back_kb)
    elif call.data == "lobby_stats":
        t, a, p, b = await get_stats()
        text = f"🌐 <b>Live Lobby Stats</b>\n━━━━━━━━━━━━━━━━━━\n👥 <b>Active Users:</b> {a}\n━━━━━━━━━━━━━━━━━━"
        if call.message.photo:
            await call.message.edit_caption(caption=text, reply_markup=back_kb)
        else:
            await call.message.edit_text(text, reply_markup=back_kb)
    elif call.data == "back_start":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📊 My Profile", callback_data="my_profile"),
             InlineKeyboardButton(text="🌐 Lobby Stats", callback_data="lobby_stats")]
        ])
        text = f"👋 <b>Welcome back, {u_data['alias']}!</b>\n\nChoose an option below."
        if call.message.photo:
            await call.message.edit_caption(caption=text, reply_markup=kb)
        else:
            await call.message.edit_text(text, reply_markup=kb)

    await call.answer()

# ================== MAIN LOBBY HANDLER ==================
@dp.message(F.content_type.in_({"text", "photo", "video"}))
async def handle_media(message: Message):
    if message.chat.type != "private":
        return
    if message.text and message.text.startswith('/'):
        return

    user_id_str = str(message.from_user.id)
    first_name = message.from_user.first_name or "Anonymous"
    user = await ensure_user(user_id_str, first_name)
    if user["banned"]:
        return

    # --- UNAPPROVED NEW USER VERIFICATION FLOW ---
    if not user["approved"]:
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute("SELECT COUNT(*) FROM pending_media WHERE user_id=?", (user_id_str,)) as c:
                current_count = (await c.fetchone())[0]

            if current_count >= REQUIRED_VERIFY_MEDIA:
                return await message.reply("⏳ Aapki 5 files pehle hi Admin review ke liye ja chuki hain. Approval ka intezar karein.")

            if message.text:
                rem = REQUIRED_VERIFY_MEDIA - current_count
                return await message.reply(f"⚠️ Verification ke liye sirf <b>Photos ya Videos</b> bhejein ({rem} baqi hain).")

            is_photo = message.photo is not None
            file_id = message.photo[-1].file_id if is_photo else message.video.file_id
            m_type = "photo" if is_photo else "video"
            raw_caption = html.escape(message.caption or "")

            await db.execute(
                "INSERT INTO pending_media (user_id, file_id, media_type, caption) VALUES (?, ?, ?, ?)",
                (user_id_str, file_id, m_type, raw_caption)
            )
            await db.commit()
            new_count = current_count + 1

            if new_count < REQUIRED_VERIFY_MEDIA:
                return await message.reply(f"✅ <b>{new_count}/{REQUIRED_VERIFY_MEDIA}</b> received! Baqi <b>{REQUIRED_VERIFY_MEDIA - new_count}</b> photos ya videos bhejein.")

            # 5 files complete -> Admin Channel mein bhejein
            async with db.execute("SELECT file_id, media_type, caption FROM pending_media WHERE user_id=? ORDER BY id ASC", (user_id_str,)) as c:
                all_media = await c.fetchall()

        media_group = []
        for i, (f_id, mt, cap) in enumerate(all_media):
            if mt == "photo":
                media_group.append(InputMediaPhoto(media=f_id, caption=cap if i == 0 else ""))
            else:
                media_group.append(InputMediaVideo(media=f_id, caption=cap if i == 0 else ""))

        review_kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Approve", callback_data=f"approve_{user_id_str}"),
                InlineKeyboardButton(text="❌ Reject", callback_data=f"reject_{user_id_str}")
            ]
        ])

        try:
            await bot.send_media_group(chat_id=ADMIN_CHANNEL_ID, media=media_group)
            await bot.send_message(
                chat_id=ADMIN_CHANNEL_ID,
                text=(
                    f"🆕 <b>NEW USER VERIFICATION</b>\n━━━━━━━━━━━━━━━━━━\n"
                    f"👤 <b>Alias:</b> {user['alias']}\n"
                    f"🆔 <b>User ID:</b> <code>{user_id_str}</code>\n"
                    f"📦 <b>Files:</b> {len(all_media)} Media\n━━━━━━━━━━━━━━━━━━\n"
                    f"Approve ya Reject select karein:"
                ),
                reply_markup=review_kb
            )
            await message.reply("✅ <b>5/5 files mil gayi hain!</b>\nAapki request Admin review ke liye bhej di gayi hai. Approve hote hi aap Lobby mein shamil ho jayenge.")
        except Exception as e:
            logging.error(f"Admin Channel Send Error: {e}")
            await message.reply("⚠️ Admin channel mein bhejne mein masla aaya. Thori der baad try karein.")
        return

    # --- APPROVED USER NORMAL LOBBY FLOW ---
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
        full_text = f"{header_text}━━━━━━━━━━\n{html.escape(message.text)}"
        asyncio.create_task(broadcast_to_lobby(user_id_str, bot.send_message, text=full_text))
        return

    # --- 2. PHOTO / VIDEO / ALBUM ---
    is_photo = message.photo is not None
    file_id = message.photo[-1].file_id if is_photo else message.video.file_id
    raw_caption = html.escape(message.caption or "")
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

# ================== MAIN STARTUP (RAILWAY POLLING) ==================
async def main():
    await init_db()
    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("🚀 Ultra-Fast Media Lobby Bot is LIVE on Railway!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
