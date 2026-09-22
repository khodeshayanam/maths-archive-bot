import os
import re
import sqlite3
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

load_dotenv()

# ==================== تنظیمات ====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

# وضعیت‌های مکالمه ادمین
(
    ADMIN_MENU,
    ADD_COURSE_NAME,
    ADD_VIDEO_COURSE,
    ADD_VIDEO_TITLE,
    ADD_VIDEO_URL,
    DELETE_CONFIRM,
) = range(6)

DB_PATH = "database.db"


# ==================== دیتابیس ====================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS courses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            teacher TEXT,
            created_at TEXT
        )
        """
    )

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            course_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            episode TEXT,
            namasha_url TEXT NOT NULL UNIQUE,
            created_at TEXT,
            FOREIGN KEY (course_id) REFERENCES courses (id)
        )
        """
    )

    conn.commit()
    conn.close()


def get_connection():
    return sqlite3.connect(DB_PATH)


def add_course(name: str, teacher: str = None) -> bool:
    try:
        conn = get_connection()
        c = conn.cursor()
        c.execute(
            "INSERT INTO courses (name, teacher, created_at) VALUES (?, ?, ?)",
            (name.strip(), teacher, datetime.now().isoformat()),
        )
        conn.commit()
        conn.close()
        return True
    except sqlite3.IntegrityError:
        return False


def get_all_courses():
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT id, name, teacher FROM courses ORDER BY name")
    rows = c.fetchall()
    conn.close()
    return rows


def get_course_by_id(course_id: int):
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT id, name, teacher FROM courses WHERE id = ?", (course_id,))
    row = c.fetchone()
    conn.close()
    return row


def add_video(course_id: int, title: str, namasha_url: str, episode: str = None) -> bool:
    try:
        conn = get_connection()
        c = conn.cursor()
        c.execute(
            """
            INSERT INTO videos (course_id, title, episode, namasha_url, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (course_id, title.strip(), episode, namasha_url.strip(), datetime.now().isoformat()),
        )
        conn.commit()
        conn.close()
        return True
    except sqlite3.IntegrityError:
        return False


def get_videos_by_course(course_id: int):
    conn = get_connection()
    c = conn.cursor()
    c.execute(
        """
        SELECT id, title, episode, namasha_url
        FROM videos
        WHERE course_id = ?
        ORDER BY CAST(episode AS INTEGER), title
        """,
        (course_id,),
    )
    rows = c.fetchall()
    conn.close()
    return rows


def search_videos(query: str):
    conn = get_connection()
    c = conn.cursor()
    like = f"%{query}%"
    c.execute(
        """
        SELECT v.id, v.title, v.episode, v.namasha_url, c.name
        FROM videos v
        JOIN courses c ON v.course_id = c.id
        WHERE v.title LIKE ? OR c.name LIKE ? OR v.episode LIKE ?
        ORDER BY c.name, CAST(v.episode AS INTEGER)
        LIMIT 30
        """,
        (like, like, like),
    )
    rows = c.fetchall()
    conn.close()
    return rows


def get_video_by_id(video_id: int):
    conn = get_connection()
    c = conn.cursor()
    c.execute(
        """
        SELECT v.id, v.title, v.episode, v.namasha_url, c.name
        FROM videos v
        JOIN courses c ON v.course_id = c.id
        WHERE v.id = ?
        """,
        (video_id,),
    )
    row = c.fetchone()
    conn.close()
    return row


def delete_video(video_id: int):
    conn = get_connection()
    c = conn.cursor()
    c.execute("DELETE FROM videos WHERE id = ?", (video_id,))
    conn.commit()
    conn.close()


def count_stats():
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM courses")
    courses = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM videos")
    videos = c.fetchone()[0]
    conn.close()
    return courses, videos


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# ==================== کیبوردها ====================
def main_keyboard(is_admin_user: bool = False):
    buttons = [
        [KeyboardButton("📚 لیست دروس"), KeyboardButton("🔍 جستجو")],
        [KeyboardButton("🆕 آخرین ویدیوها"), KeyboardButton("📖 راهنما")],
    ]
    if is_admin_user:
        buttons.append([KeyboardButton("⚙️ پنل مدیریت")])
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)


def admin_keyboard():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("➕ افزودن درس"), KeyboardButton("🎬 افزودن ویدیو")],
            [KeyboardButton("📊 آمار"), KeyboardButton("🔙 بازگشت به منوی اصلی")],
        ],
        resize_keyboard=True,
    )


# ==================== هندلرها ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = (
        f"سلام {user.first_name} 👋\n\n"
        "به ربات **آرشیو ویدیوهای دانشکده ریاضی** خوش آمدید.\n\n"
        "از منوی زیر می‌توانید دروس را مشاهده کنید یا جستجو کنید."
    )
    await update.message.reply_text(
        text,
        reply_markup=main_keyboard(is_admin(user.id)),
        parse_mode="Markdown",
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 **راهنمای استفاده**\n\n"
        "• روی **📚 لیست دروس** بزنید تا دروس را ببینید.\n"
        "• بعد از انتخاب درس، قسمت‌ها را مشاهده کنید.\n"
        "• با زدن روی هر قسمت، لینک نماشا برایتان ارسال می‌شود.\n"
        "• می‌توانید با **🔍 جستجو** نام درس یا قسمت را پیدا کنید.\n\n"
        "ویدیوها روی سایت نماشا میزبانی می‌شوند."
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def show_courses(update: Update, context: ContextTypes.DEFAULT_TYPE):
    courses = get_all_courses()
    if not courses:
        await update.message.reply_text(
            "هنوز هیچ درسی اضافه نشده است.\n"
            "اگر ادمین هستید، از پنل مدیریت درس اضافه کنید."
        )
        return

    buttons = []
    for course_id, name, teacher in courses:
        label = f"📘 {name}"
        if teacher:
            label += f" ({teacher})"
        buttons.append([InlineKeyboardButton(label, callback_data=f"course_{course_id}")])

    await update.message.reply_text(
        "📚 **لیست دروس**\nیکی را انتخاب کنید:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )


async def course_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    course_id = int(query.data.split("_")[1])
    course = get_course_by_id(course_id)
    if not course:
        await query.edit_message_text("درس پیدا نشد.")
        return

    _, name, teacher = course
    videos = get_videos_by_course(course_id)

    if not videos:
        text = f"📘 **{name}**\n\nهنوز ویدیویی برای این درس ثبت نشده است."
        await query.edit_message_text(text, parse_mode="Markdown")
        return

    text = f"📘 **{name}**"
    if teacher:
        text += f"\n👨‍🏫 {teacher}"
    text += f"\n\nتعداد قسمت‌ها: {len(videos)}\nیکی را انتخاب کنید:"

    buttons = []
    for vid_id, title, episode, url in videos:
        label = f"🎬 {title}"
        if episode:
            label = f"🎬 قسمت {episode} — {title}" if title != f"قسمت {episode}" else f"🎬 قسمت {episode}"
        buttons.append([InlineKeyboardButton(label[:60], callback_data=f"video_{vid_id}")])

    buttons.append([InlineKeyboardButton("🔙 بازگشت به دروس", callback_data="back_courses")])

    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )


async def video_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    video_id = int(query.data.split("_")[1])
    video = get_video_by_id(video_id)
    if not video:
        await query.edit_message_text("ویدیو پیدا نشد.")
        return

    _, title, episode, url, course_name = video

    text = f"🎬 **{title}**\n\n"
    text += f"📘 درس: {course_name}\n"
    if episode:
        text += f"📌 قسمت: {episode}\n"
    text += f"\n🔗 لینک نماشا:\n{url}"

    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("▶️ مشاهده در نماشا", url=url)],
            [InlineKeyboardButton("🔙 بازگشت", callback_data=f"course_{get_course_id_from_video(video_id)}")],
        ]
    )

    await query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")


def get_course_id_from_video(video_id: int) -> int:
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT course_id FROM videos WHERE id = ?", (video_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else 0


async def back_to_courses(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    courses = get_all_courses()
    if not courses:
        await query.edit_message_text("هیچ درسی وجود ندارد.")
        return

    buttons = []
    for course_id, name, teacher in courses:
        label = f"📘 {name}"
        if teacher:
            label += f" ({teacher})"
        buttons.append([InlineKeyboardButton(label, callback_data=f"course_{course_id}")])

    await query.edit_message_text(
        "📚 **لیست دروس**\nیکی را انتخاب کنید:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )


async def search_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔍 عبارت مورد نظر را بنویسید (نام درس، استاد یا قسمت):",
        reply_markup=ReplyKeyboardRemove(),
    )
    context.user_data["waiting_search"] = True


async def handle_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("waiting_search"):
        return

    query = update.message.text.strip()
    context.user_data["waiting_search"] = False

    if len(query) < 2:
        await update.message.reply_text(
            "عبارت خیلی کوتاه است.",
            reply_markup=main_keyboard(is_admin(update.effective_user.id)),
        )
        return

    results = search_videos(query)

    if not results:
        await update.message.reply_text(
            f"نتیجه‌ای برای «{query}» پیدا نشد.",
            reply_markup=main_keyboard(is_admin(update.effective_user.id)),
        )
        return

    text = f"🔎 نتایج جستجو برای «{query}»:\n\n"
    buttons = []
    for vid_id, title, episode, url, course_name in results[:20]:
        label = f"{course_name} — {title}"
        if episode:
            label = f"{course_name} | قسمت {episode}"
        buttons.append([InlineKeyboardButton(label[:60], callback_data=f"video_{vid_id}")])

    await update.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    await update.message.reply_text(
        "از منوی زیر ادامه دهید:",
        reply_markup=main_keyboard(is_admin(update.effective_user.id)),
    )


async def latest_videos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = get_connection()
    c = conn.cursor()
    c.execute(
        """
        SELECT v.id, v.title, v.episode, c.name
        FROM videos v
        JOIN courses c ON v.course_id = c.id
        ORDER BY v.id DESC
        LIMIT 15
        """
    )
    rows = c.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("هنوز ویدیویی ثبت نشده است.")
        return

    buttons = []
    for vid_id, title, episode, course_name in rows:
        label = f"{course_name} — {title}"
        if episode:
            label = f"{course_name} | قسمت {episode}"
        buttons.append([InlineKeyboardButton(label[:60], callback_data=f"video_{vid_id}")])

    await update.message.reply_text(
        "🆕 **آخرین ویدیوهای اضافه‌شده:**",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )


# ==================== پنل مدیریت ====================
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("شما دسترسی ادمین ندارید.")
        return

    courses, videos = count_stats()
    text = (
        "⚙️ **پنل مدیریت**\n\n"
        f"تعداد دروس: {courses}\n"
        f"تعداد ویدیوها: {videos}\n\n"
        "یکی از گزینه‌ها را انتخاب کنید:"
    )
    await update.message.reply_text(text, reply_markup=admin_keyboard(), parse_mode="Markdown")


async def admin_add_course_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END

    await update.message.reply_text(
        "نام درس را وارد کنید (مثال: نظریه آمار ۱):",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ADD_COURSE_NAME


async def admin_add_course_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    if len(name) < 2:
        await update.message.reply_text("نام درس خیلی کوتاه است. دوباره وارد کنید:")
        return ADD_COURSE_NAME

    success = add_course(name)
    if success:
        await update.message.reply_text(
            f"✅ درس «{name}» با موفقیت اضافه شد.",
            reply_markup=admin_keyboard(),
        )
    else:
        await update.message.reply_text(
            f"⚠️ این درس از قبل وجود دارد.",
            reply_markup=admin_keyboard(),
        )
    return ConversationHandler.END


async def admin_add_video_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END

    courses = get_all_courses()
    if not courses:
        await update.message.reply_text(
            "ابتدا باید حداقل یک درس اضافه کنید.",
            reply_markup=admin_keyboard(),
        )
        return ConversationHandler.END

    buttons = []
    for course_id, name, _ in courses:
        buttons.append([InlineKeyboardButton(name, callback_data=f"addvid_course_{course_id}")])

    await update.message.reply_text(
        "درس مربوط به ویدیو را انتخاب کنید:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return ADD_VIDEO_COURSE


async def admin_add_video_course(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    course_id = int(query.data.split("_")[2])
    context.user_data["add_video_course_id"] = course_id

    course = get_course_by_id(course_id)
    await query.edit_message_text(
        f"درس انتخاب‌شده: **{course[1]}**\n\nحالا عنوان ویدیو را بفرستید (مثال: قسمت ۲ یا نظریه آمار ۱ - قسمت ۲):",
        parse_mode="Markdown",
    )
    return ADD_VIDEO_TITLE


async def admin_add_video_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    title = update.message.text.strip()
    context.user_data["add_video_title"] = title

    # تلاش برای استخراج شماره قسمت
    episode_match = re.search(r"قسمت\s*(\d+)", title)
    episode = episode_match.group(1) if episode_match else None
    context.user_data["add_video_episode"] = episode

    await update.message.reply_text(
        "حالا لینک نماشا را بفرستید (مثال:\nhttps://www.namasha.com/v/xxxxxx )"
    )
    return ADD_VIDEO_URL


async def admin_add_video_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()

    if "namasha.com" not in url:
        await update.message.reply_text(
            "لینک معتبر نیست. لینک باید از سایت namasha.com باشد. دوباره بفرستید:"
        )
        return ADD_VIDEO_URL

    course_id = context.user_data.get("add_video_course_id")
    title = context.user_data.get("add_video_title")
    episode = context.user_data.get("add_video_episode")

    success = add_video(course_id, title, url, episode)

    if success:
        await update.message.reply_text(
            f"✅ ویدیو با موفقیت اضافه شد.\n\nعنوان: {title}\nلینک: {url}",
            reply_markup=admin_keyboard(),
        )
    else:
        await update.message.reply_text(
            "⚠️ این لینک قبلاً ثبت شده است.",
            reply_markup=admin_keyboard(),
        )

    # پاک کردن داده‌های موقت
    context.user_data.pop("add_video_course_id", None)
    context.user_data.pop("add_video_title", None)
    context.user_data.pop("add_video_episode", None)

    return ConversationHandler.END


async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    courses, videos = count_stats()
    await update.message.reply_text(
        f"📊 **آمار آرشیو**\n\n"
        f"تعداد دروس: {courses}\n"
        f"تعداد ویدیوها: {videos}",
        parse_mode="Markdown",
        reply_markup=admin_keyboard(),
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "عملیات لغو شد.",
        reply_markup=main_keyboard(is_admin(update.effective_user.id)),
    )
    return ConversationHandler.END


async def back_to_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "بازگشت به منوی اصلی",
        reply_markup=main_keyboard(is_admin(update.effective_user.id)),
    )


async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """مسیریابی پیام‌های متنی منو"""
    text = update.message.text
    user_id = update.effective_user.id

    if context.user_data.get("waiting_search"):
        await handle_search(update, context)
        return

    if text == "📚 لیست دروس":
        await show_courses(update, context)
    elif text == "🔍 جستجو":
        await search_start(update, context)
    elif text == "🆕 آخرین ویدیوها":
        await latest_videos(update, context)
    elif text == "📖 راهنما":
        await help_command(update, context)
    elif text == "⚙️ پنل مدیریت":
        await admin_panel(update, context)
    elif text == "📊 آمار":
        await admin_stats(update, context)
    elif text == "🔙 بازگشت به منوی اصلی":
        await back_to_main(update, context)
    elif text == "➕ افزودن درس":
        # این توسط ConversationHandler مدیریت می‌شود
        pass
    elif text == "🎬 افزودن ویدیو":
        pass
    else:
        await update.message.reply_text(
            "دستور نامعتبر است. از منو استفاده کنید.",
            reply_markup=main_keyboard(is_admin(user_id)),
        )


# ==================== اجرای ربات ====================
def main():
    if not BOT_TOKEN:
        print("❌ خطا: BOT_TOKEN تنظیم نشده است.")
        print("فایل .env را بسازید و توکن را داخل آن قرار دهید.")
        return

    if not ADMIN_IDS:
        print("⚠️ هشدار: هیچ ادمینی تعریف نشده است. ADMIN_IDS را در فایل .env تنظیم کنید.")

    init_db()
    print("✅ دیتابیس آماده است.")

    # افزایش تایم‌اوت برای اتصال‌های ضعیف (مخصوصاً ایران)
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(30.0)
        .read_timeout(30.0)
        .write_timeout(30.0)
        .pool_timeout(30.0)
        .get_updates_connect_timeout(30.0)
        .get_updates_read_timeout(30.0)
        .get_updates_pool_timeout(30.0)
        .build()
    )

    # ConversationHandler برای افزودن درس
    add_course_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^➕ افزودن درس$"), admin_add_course_start)],
        states={
            ADD_COURSE_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_course_name)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    # ConversationHandler برای افزودن ویدیو
    add_video_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^🎬 افزودن ویدیو$"), admin_add_video_start)],
        states={
            ADD_VIDEO_COURSE: [CallbackQueryHandler(admin_add_video_course, pattern=r"^addvid_course_")],
            ADD_VIDEO_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_video_title)],
            ADD_VIDEO_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_video_url)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(add_course_conv)
    app.add_handler(add_video_conv)
    app.add_handler(CallbackQueryHandler(course_selected, pattern=r"^course_"))
    app.add_handler(CallbackQueryHandler(video_selected, pattern=r"^video_"))
    app.add_handler(CallbackQueryHandler(back_to_courses, pattern=r"^back_courses$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    print("🤖 ربات در حال اجرا است...")
    print("اگر خطا گرفتی، حتماً VPN را روشن کن و دوباره امتحان کن.")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
