import os
import re
import sqlite3
import logging
from datetime import datetime
from typing import Optional, List, Tuple

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
from telegram.constants import ParseMode
from telegram.error import BadRequest

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

PAGE_SIZE = 10

(
    ADD_COURSE_NAME,
    ADD_VIDEO_COURSE,
    ADD_VIDEO_TITLE,
    ADD_VIDEO_URL,
    EDIT_VIDEO_SELECT,
    EDIT_VIDEO_FIELD,
    EDIT_VIDEO_VALUE,
    DELETE_VIDEO_SELECT,
    DELETE_VIDEO_CONFIRM,
    EDIT_COURSE_SELECT,
    EDIT_COURSE_NAME,
    DELETE_COURSE_SELECT,
    DELETE_COURSE_CONFIRM,
) = range(13)

DB_PATH = "database.db"
PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def normalize_digits(text: str) -> str:
    if not text:
        return text
    return text.translate(PERSIAN_DIGITS)


def safe_text(text: str) -> str:
    if not text:
        return ""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def extract_episode(title: str) -> Optional[str]:
    title = normalize_digits(title)
    match = re.search(
        r"(?:قسمت|جلسه|part|episode)\s*([0-9]+(?:[.-][0-9]+)?)",
        title,
        re.IGNORECASE,
    )
    if match:
        return match.group(1)
    match2 = re.search(r"\b([0-9]+)\s*$", title.strip())
    if match2:
        return match2.group(1)
    return None


def episode_sort_key(episode: Optional[str]) -> Tuple[int, int, str]:
    if not episode:
        return (10**9, 0, "")
    ep = normalize_digits(str(episode))
    parts = re.split(r"[.-]", ep)
    try:
        major = int(parts[0])
        minor = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        return (major, minor, ep)
    except ValueError:
        return (10**9, 0, ep)


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


def update_course(course_id: int, name: str) -> bool:
    try:
        conn = get_connection()
        c = conn.cursor()
        c.execute(
            "UPDATE courses SET name = ? WHERE id = ?",
            (normalize_digits(name.strip()), course_id),
        )
        conn.commit()
        ok = c.rowcount > 0
        conn.close()
        return ok
    except sqlite3.IntegrityError:
        return False


def delete_course(course_id: int) -> bool:
    """حذف درس به همراه تمام ویدیوهایش"""
    conn = get_connection()
    c = conn.cursor()
    c.execute("DELETE FROM videos WHERE course_id = ?", (course_id,))
    c.execute("DELETE FROM courses WHERE id = ?", (course_id,))
    conn.commit()
    ok = c.rowcount > 0
    conn.close()
    return ok


def count_videos_in_course(course_id: int) -> int:
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM videos WHERE course_id = ?", (course_id,))
    n = c.fetchone()[0]
    conn.close()
    return n


def get_all_courses() -> List[Tuple]:
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
        title = normalize_digits(title.strip())
        episode = normalize_digits(episode) if episode else extract_episode(title)
        conn = get_connection()
        c = conn.cursor()
        c.execute(
            """
            INSERT INTO videos (course_id, title, episode, namasha_url, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (course_id, title, episode, namasha_url.strip(), datetime.now().isoformat()),
        )
        conn.commit()
        conn.close()
        return True
    except sqlite3.IntegrityError:
        return False


def update_video(video_id: int, title: str = None, episode: str = None, namasha_url: str = None) -> bool:
    conn = get_connection()
    c = conn.cursor()
    fields = []
    values = []
    if title is not None:
        title = normalize_digits(title.strip())
        fields.append("title = ?")
        values.append(title)
        if episode is None:
            episode = extract_episode(title)
    if episode is not None:
        fields.append("episode = ?")
        values.append(normalize_digits(str(episode)))
    if namasha_url is not None:
        fields.append("namasha_url = ?")
        values.append(namasha_url.strip())
    if not fields:
        conn.close()
        return False
    values.append(video_id)
    try:
        c.execute(f"UPDATE videos SET {', '.join(fields)} WHERE id = ?", values)
        conn.commit()
        ok = c.rowcount > 0
        conn.close()
        return ok
    except sqlite3.IntegrityError:
        conn.close()
        return False


def get_videos_by_course(course_id: int) -> List[Tuple]:
    conn = get_connection()
    c = conn.cursor()
    c.execute(
        "SELECT id, title, episode, namasha_url FROM videos WHERE course_id = ?",
        (course_id,),
    )
    rows = c.fetchall()
    conn.close()
    return sorted(rows, key=lambda r: episode_sort_key(r[2]))


def search_videos(query: str) -> List[Tuple]:
    conn = get_connection()
    c = conn.cursor()
    q = normalize_digits(query.strip())
    like = f"%{q}%"
    c.execute(
        """
        SELECT v.id, v.title, v.episode, v.namasha_url, c.name
        FROM videos v
        JOIN courses c ON v.course_id = c.id
        WHERE v.title LIKE ? OR c.name LIKE ?
           OR IFNULL(v.episode,'') LIKE ? OR IFNULL(c.teacher,'') LIKE ?
        ORDER BY c.name
        LIMIT 50
        """,
        (like, like, like, like),
    )
    rows = c.fetchall()
    conn.close()
    return sorted(rows, key=lambda r: (r[4], episode_sort_key(r[2])))


def get_video_by_id(video_id: int):
    conn = get_connection()
    c = conn.cursor()
    c.execute(
        """
        SELECT v.id, v.title, v.episode, v.namasha_url, c.name, v.course_id
        FROM videos v
        JOIN courses c ON v.course_id = c.id
        WHERE v.id = ?
        """,
        (video_id,),
    )
    row = c.fetchone()
    conn.close()
    return row


def delete_video(video_id: int) -> bool:
    conn = get_connection()
    c = conn.cursor()
    c.execute("DELETE FROM videos WHERE id = ?", (video_id,))
    conn.commit()
    ok = c.rowcount > 0
    conn.close()
    return ok


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


def main_keyboard(is_admin_user: bool = False):
    buttons = [
        [KeyboardButton("📚 لیست دروس"), KeyboardButton("🔍 جستجو")],
        [KeyboardButton("🆕 آخرین ویدیوها"), KeyboardButton("📖 راهنما")],
        [KeyboardButton("🏠 منوی اصلی")],
    ]
    if is_admin_user:
        buttons.append([KeyboardButton("⚙️ پنل مدیریت")])
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)


def admin_keyboard():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("➕ افزودن درس"), KeyboardButton("🎬 افزودن ویدیو")],
            [KeyboardButton("✏️ ویرایش درس"), KeyboardButton("🗑 حذف درس")],
            [KeyboardButton("✏️ ویرایش ویدیو"), KeyboardButton("🗑 حذف ویدیو")],
            [KeyboardButton("📊 آمار"), KeyboardButton("🏠 منوی اصلی")],
        ],
        resize_keyboard=True,
    )


def paginate_buttons(items, page: int, page_size: int, prefix: str, back_data: str = None):
    total = len(items)
    start = page * page_size
    end = start + page_size
    page_items = items[start:end]
    buttons = list(page_items)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ قبلی", callback_data=f"{prefix}_page_{page-1}"))
    if end < total:
        nav.append(InlineKeyboardButton("بعدی ▶️", callback_data=f"{prefix}_page_{page+1}"))
    if nav:
        buttons.append(nav)
    if back_data:
        buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data=back_data)])
    return InlineKeyboardMarkup(buttons), total, start, end


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Exception while handling an update:", exc_info=context.error)
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text(
                "⚠️ مشکلی پیش آمد. لطفاً دوباره امتحان کنید."
            )
    except Exception:
        pass


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = (
        f"سلام {safe_text(user.first_name)} 👋\n\n"
        "به ربات <b>آرشیو ویدیوهای دانشکده ریاضی</b> خوش آمدید.\n\n"
        "از منوی زیر می‌توانید دروس را مشاهده کنید یا جستجو کنید."
    )
    await update.message.reply_text(
        text,
        reply_markup=main_keyboard(is_admin(user.id)),
        parse_mode=ParseMode.HTML,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 <b>راهنمای استفاده</b>\n\n"
        "• روی <b>📚 لیست دروس</b> بزنید تا دروس را ببینید.\n"
        "• بعد از انتخاب درس، قسمت‌ها را مشاهده کنید.\n"
        "• با زدن روی هر قسمت، لینک نماشا برایتان ارسال می‌شود.\n"
        "• با <b>🔍 جستجو</b> نام درس یا قسمت را پیدا کنید.\n"
        "• هر وقت خواستید به ابتدا برگردید، <b>🏠 منوی اصلی</b> را بزنید.\n\n"
        "ویدیوها روی سایت نماشا میزبانی می‌شوند."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def show_courses_page(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0, edit: bool = False):
    courses = get_all_courses()
    if not courses:
        msg = "هنوز هیچ درسی اضافه نشده است."
        if edit and update.callback_query:
            await update.callback_query.edit_message_text(msg)
        else:
            await update.effective_message.reply_text(msg)
        return

    items = []
    for course_id, name, teacher in courses:
        label = f"📘 {name}"
        if teacher:
            label += f" ({teacher})"
        items.append([InlineKeyboardButton(label[:60], callback_data=f"course_{course_id}")])

    markup, total, start, end = paginate_buttons(items, page, PAGE_SIZE, "courses")
    text = (
        f"📚 <b>لیست دروس</b>\n"
        f"نمایش {start+1} تا {min(end, total)} از {total}\n"
        "یکی را انتخاب کنید:"
    )

    if edit and update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                text, reply_markup=markup, parse_mode=ParseMode.HTML
            )
        except BadRequest:
            await update.callback_query.answer()
    else:
        await update.effective_message.reply_text(
            text, reply_markup=markup, parse_mode=ParseMode.HTML
        )


async def show_courses(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_courses_page(update, context, page=0, edit=False)


async def courses_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    page = int(query.data.split("_")[-1])
    await show_courses_page(update, context, page=page, edit=True)


async def show_course_videos_page(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    course_id: int,
    page: int = 0,
    edit: bool = False,
):
    course = get_course_by_id(course_id)
    if not course:
        msg = "درس پیدا نشد."
        if edit and update.callback_query:
            await update.callback_query.edit_message_text(msg)
        else:
            await update.effective_message.reply_text(msg)
        return

    _, name, teacher = course
    videos = get_videos_by_course(course_id)

    if not videos:
        text = f"📘 <b>{safe_text(name)}</b>\n\nهنوز ویدیویی برای این درس ثبت نشده است."
        buttons = [[InlineKeyboardButton("🔙 بازگشت به لیست دروس", callback_data="back_courses")]]
        markup = InlineKeyboardMarkup(buttons)
        if edit and update.callback_query:
            await update.callback_query.edit_message_text(
                text, reply_markup=markup, parse_mode=ParseMode.HTML
            )
        else:
            await update.effective_message.reply_text(
                text, reply_markup=markup, parse_mode=ParseMode.HTML
            )
        return

    items = []
    for vid_id, title, episode, url in videos:
        if episode:
            label = f"🎬 قسمت {episode} — {title}"
        else:
            label = f"🎬 {title}"
        items.append([InlineKeyboardButton(label[:60], callback_data=f"video_{vid_id}")])

    markup, total, start, end = paginate_buttons(
        items, page, PAGE_SIZE, f"cv_{course_id}", back_data="back_courses"
    )

    text = f"📘 <b>{safe_text(name)}</b>"
    if teacher:
        text += f"\n👨‍🏫 {safe_text(teacher)}"
    text += (
        f"\n\nتعداد قسمت‌ها: {total}\n"
        f"نمایش {start+1} تا {min(end, total)}\n"
        "یکی را انتخاب کنید:"
    )

    if edit and update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                text, reply_markup=markup, parse_mode=ParseMode.HTML
            )
        except BadRequest:
            await update.callback_query.answer()
    else:
        await update.effective_message.reply_text(
            text, reply_markup=markup, parse_mode=ParseMode.HTML
        )


async def course_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    course_id = int(query.data.split("_")[1])
    await show_course_videos_page(update, context, course_id=course_id, page=0, edit=True)


async def course_videos_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split("_")
    course_id = int(parts[1])
    page = int(parts[-1])
    await show_course_videos_page(update, context, course_id=course_id, page=page, edit=True)


async def video_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    video_id = int(query.data.split("_")[1])
    video = get_video_by_id(video_id)
    if not video:
        await query.edit_message_text("ویدیو پیدا نشد.")
        return

    _, title, episode, url, course_name, course_id = video
    text = f"🎬 <b>{safe_text(title)}</b>\n\n"
    text += f"📘 درس: {safe_text(course_name)}\n"
    if episode:
        text += f"📌 قسمت: {safe_text(str(episode))}\n"
    text += f"\n🔗 لینک نماشا:\n{safe_text(url)}"

    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("▶️ مشاهده در نماشا", url=url)],
            [InlineKeyboardButton("🔙 بازگشت به قسمت‌ها", callback_data=f"course_{course_id}")],
            [InlineKeyboardButton("📚 بازگشت به لیست دروس", callback_data="back_courses")],
        ]
    )
    try:
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
    except BadRequest:
        await query.edit_message_text(
            f"🎬 {title}\n\n📘 {course_name}\n🔗 {url}",
            reply_markup=keyboard,
        )


async def back_to_courses(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await show_courses_page(update, context, page=0, edit=True)


async def search_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔍 عبارت مورد نظر را بنویسید (نام درس، استاد یا قسمت):\n\n"
        "بعد از جستجو می‌توانید با «🏠 منوی اصلی» برگردید.",
        reply_markup=main_keyboard(is_admin(update.effective_user.id)),
    )
    context.user_data["waiting_search"] = True


async def handle_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("waiting_search"):
        return
    query = update.message.text.strip()
    # اگر کاربر منوی اصلی زد، جستجو را لغو کن
    if query in ("🏠 منوی اصلی", "📚 لیست دروس", "🔍 جستجو", "🆕 آخرین ویدیوها", "📖 راهنما", "⚙️ پنل مدیریت"):
        context.user_data["waiting_search"] = False
        return False  # اجازه بده text_router ادامه دهد

    context.user_data["waiting_search"] = False
    if len(query) < 2:
        await update.message.reply_text(
            "عبارت خیلی کوتاه است.",
            reply_markup=main_keyboard(is_admin(update.effective_user.id)),
        )
        return True

    results = search_videos(query)
    if not results:
        await update.message.reply_text(
            f"نتیجه‌ای برای «{query}» پیدا نشد.",
            reply_markup=main_keyboard(is_admin(update.effective_user.id)),
        )
        return True

    text = f"🔎 نتایج جستجو برای «{safe_text(query)}»:\n\n"
    buttons = []
    for vid_id, title, episode, url, course_name in results[:20]:
        if episode:
            label = f"{course_name} | قسمت {episode}"
        else:
            label = f"{course_name} — {title}"
        buttons.append([InlineKeyboardButton(label[:60], callback_data=f"video_{vid_id}")])

    await update.message.reply_text(
        text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode=ParseMode.HTML
    )
    await update.message.reply_text(
        "برای برگشت، «🏠 منوی اصلی» را بزنید.",
        reply_markup=main_keyboard(is_admin(update.effective_user.id)),
    )
    return True


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
        if episode:
            label = f"{course_name} | قسمت {episode}"
        else:
            label = f"{course_name} — {title}"
        buttons.append([InlineKeyboardButton(label[:60], callback_data=f"video_{vid_id}")])

    await update.message.reply_text(
        "🆕 <b>آخرین ویدیوهای اضافه‌شده:</b>",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode=ParseMode.HTML,
    )


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("شما دسترسی ادمین ندارید.")
        return
    courses, videos = count_stats()
    text = (
        "⚙️ <b>پنل مدیریت</b>\n\n"
        f"تعداد دروس: {courses}\n"
        f"تعداد ویدیوها: {videos}\n\n"
        "می‌توانید درس و ویدیو را اضافه، ویرایش یا حذف کنید."
    )
    await update.message.reply_text(text, reply_markup=admin_keyboard(), parse_mode=ParseMode.HTML)


async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    courses, videos = count_stats()
    await update.message.reply_text(
        f"📊 <b>آمار آرشیو</b>\n\nتعداد دروس: {courses}\nتعداد ویدیوها: {videos}",
        parse_mode=ParseMode.HTML,
        reply_markup=admin_keyboard(),
    )


async def back_to_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "🏠 منوی اصلی",
        reply_markup=main_keyboard(is_admin(update.effective_user.id)),
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "عملیات لغو شد.",
        reply_markup=main_keyboard(is_admin(update.effective_user.id)),
    )
    return ConversationHandler.END


# ----- افزودن درس -----
async def admin_add_course_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    await update.message.reply_text(
        "نام درس را وارد کنید (مثال: نظریه آمار ۱ دکتر روحانی):\n\n/cancel برای لغو",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ADD_COURSE_NAME


async def admin_add_course_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = normalize_digits(update.message.text.strip())
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
            "⚠️ این درس از قبل وجود دارد.",
            reply_markup=admin_keyboard(),
        )
    return ConversationHandler.END


# ----- ویرایش درس -----
async def admin_edit_course_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    courses = get_all_courses()
    if not courses:
        await update.message.reply_text("درسی وجود ندارد.", reply_markup=admin_keyboard())
        return ConversationHandler.END
    buttons = [
        [InlineKeyboardButton(name, callback_data=f"editcourse_{cid}")]
        for cid, name, _ in courses
    ]
    await update.message.reply_text(
        "درسی که می‌خواهید ویرایش کنید را انتخاب کنید:\n\n/cancel برای لغو",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return EDIT_COURSE_SELECT


async def admin_edit_course_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    course_id = int(query.data.split("_")[1])
    course = get_course_by_id(course_id)
    if not course:
        await query.edit_message_text("درس پیدا نشد.")
        return ConversationHandler.END
    context.user_data["edit_course_id"] = course_id
    await query.edit_message_text(
        f"نام فعلی: <b>{safe_text(course[1])}</b>\n\nنام جدید درس را بفرستید:",
        parse_mode=ParseMode.HTML,
    )
    return EDIT_COURSE_NAME


async def admin_edit_course_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    course_id = context.user_data.get("edit_course_id")
    name = normalize_digits(update.message.text.strip())
    if not course_id or len(name) < 2:
        await update.message.reply_text("نام نامعتبر است.", reply_markup=admin_keyboard())
        return ConversationHandler.END
    ok = update_course(course_id, name)
    if ok:
        await update.message.reply_text(
            f"✅ نام درس به «{name}» تغییر کرد.",
            reply_markup=admin_keyboard(),
        )
    else:
        await update.message.reply_text(
            "⚠️ ویرایش انجام نشد (شاید این نام قبلاً وجود دارد).",
            reply_markup=admin_keyboard(),
        )
    context.user_data.pop("edit_course_id", None)
    return ConversationHandler.END


# ----- حذف درس -----
async def admin_delete_course_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    courses = get_all_courses()
    if not courses:
        await update.message.reply_text("درسی وجود ندارد.", reply_markup=admin_keyboard())
        return ConversationHandler.END
    buttons = [
        [InlineKeyboardButton(f"🗑 {name}", callback_data=f"delcourse_{cid}")]
        for cid, name, _ in courses
    ]
    await update.message.reply_text(
        "درسی که می‌خواهید حذف کنید را انتخاب کنید:\n"
        "⚠️ با حذف درس، همه ویدیوهای آن هم پاک می‌شوند.\n\n/cancel برای لغو",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return DELETE_COURSE_SELECT


async def admin_delete_course_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    course_id = int(query.data.split("_")[1])
    course = get_course_by_id(course_id)
    if not course:
        await query.edit_message_text("درس پیدا نشد.")
        return ConversationHandler.END
    context.user_data["delete_course_id"] = course_id
    n = count_videos_in_course(course_id)
    text = (
        f"آیا از حذف درس «<b>{safe_text(course[1])}</b>» مطمئن هستید؟\n\n"
        f"تعداد ویدیوهایی که حذف می‌شوند: <b>{n}</b>\n"
        "این عمل قابل بازگشت نیست."
    )
    buttons = [
        [
            InlineKeyboardButton("✅ بله، حذف شود", callback_data="delcourse_yes"),
            InlineKeyboardButton("❌ انصراف", callback_data="delcourse_no"),
        ]
    ]
    await query.edit_message_text(
        text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode=ParseMode.HTML
    )
    return DELETE_COURSE_CONFIRM


async def admin_delete_course_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "delcourse_no":
        await query.edit_message_text("حذف درس لغو شد.")
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="پنل مدیریت:",
            reply_markup=admin_keyboard(),
        )
        context.user_data.pop("delete_course_id", None)
        return ConversationHandler.END

    course_id = context.user_data.get("delete_course_id")
    if course_id and delete_course(course_id):
        await query.edit_message_text("✅ درس و ویدیوهایش حذف شدند.")
    else:
        await query.edit_message_text("⚠️ حذف انجام نشد.")
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text="پنل مدیریت:",
        reply_markup=admin_keyboard(),
    )
    context.user_data.pop("delete_course_id", None)
    return ConversationHandler.END


# ----- افزودن ویدیو -----
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
    buttons = [
        [InlineKeyboardButton(name, callback_data=f"addvid_course_{cid}")]
        for cid, name, _ in courses
    ]
    await update.message.reply_text(
        "درس مربوط به ویدیو را انتخاب کنید:\n\n/cancel برای لغو",
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
        f"درس انتخاب‌شده: <b>{safe_text(course[1])}</b>\n\n"
        "عنوان ویدیو را بفرستید (مثال: قسمت ۲):",
        parse_mode=ParseMode.HTML,
    )
    return ADD_VIDEO_TITLE


async def admin_add_video_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    title = normalize_digits(update.message.text.strip())
    context.user_data["add_video_title"] = title
    context.user_data["add_video_episode"] = extract_episode(title)
    await update.message.reply_text(
        "حالا لینک نماشا را بفرستید:\nhttps://www.namasha.com/v/xxxxxx"
    )
    return ADD_VIDEO_URL


async def admin_add_video_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    if "namasha.com" not in url:
        await update.message.reply_text(
            "لینک معتبر نیست. لینک باید از namasha.com باشد. دوباره بفرستید:"
        )
        return ADD_VIDEO_URL

    course_id = context.user_data.get("add_video_course_id")
    title = context.user_data.get("add_video_title")
    episode = context.user_data.get("add_video_episode")
    success = add_video(course_id, title, url, episode)
    if success:
        ep_info = f"\nقسمت: {episode}" if episode else ""
        await update.message.reply_text(
            f"✅ ویدیو اضافه شد.\n\nعنوان: {title}{ep_info}\nلینک: {url}",
            reply_markup=admin_keyboard(),
        )
    else:
        await update.message.reply_text(
            "⚠️ این لینک قبلاً ثبت شده است.",
            reply_markup=admin_keyboard(),
        )
    for k in ("add_video_course_id", "add_video_title", "add_video_episode"):
        context.user_data.pop(k, None)
    return ConversationHandler.END


# ----- حذف ویدیو -----
async def admin_delete_video_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    courses = get_all_courses()
    if not courses:
        await update.message.reply_text("درسی وجود ندارد.", reply_markup=admin_keyboard())
        return ConversationHandler.END
    buttons = [
        [InlineKeyboardButton(name, callback_data=f"delvid_course_{cid}")]
        for cid, name, _ in courses
    ]
    await update.message.reply_text(
        "درس مورد نظر برای حذف ویدیو را انتخاب کنید:\n\n/cancel برای لغو",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return DELETE_VIDEO_SELECT


async def admin_delete_video_course(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data.startswith("delvid_course_"):
        course_id = int(query.data.split("_")[2])
        videos = get_videos_by_course(course_id)
        if not videos:
            await query.edit_message_text("این درس ویدیویی ندارد.")
            return ConversationHandler.END
        buttons = []
        for vid_id, title, episode, _ in videos:
            label = f"قسمت {episode} — {title}" if episode else title
            buttons.append(
                [InlineKeyboardButton(f"🗑 {label[:50]}", callback_data=f"delvid_id_{vid_id}")]
            )
        await query.edit_message_text(
            "ویدیوی مورد نظر برای حذف را انتخاب کنید:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return DELETE_VIDEO_SELECT

    if query.data.startswith("delvid_id_"):
        video_id = int(query.data.split("_")[2])
        video = get_video_by_id(video_id)
        if not video:
            await query.edit_message_text("ویدیو پیدا نشد.")
            return ConversationHandler.END
        context.user_data["delete_video_id"] = video_id
        _, title, episode, url, course_name, _ = video
        text = (
            f"آیا از حذف این ویدیو مطمئن هستید؟\n\n"
            f"📘 {safe_text(course_name)}\n"
            f"🎬 {safe_text(title)}\n"
        )
        if episode:
            text += f"📌 قسمت: {episode}\n"
        buttons = [
            [
                InlineKeyboardButton("✅ بله، حذف شود", callback_data="delvid_confirm_yes"),
                InlineKeyboardButton("❌ انصراف", callback_data="delvid_confirm_no"),
            ]
        ]
        await query.edit_message_text(
            text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode=ParseMode.HTML
        )
        return DELETE_VIDEO_CONFIRM

    return DELETE_VIDEO_SELECT


async def admin_delete_video_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "delvid_confirm_no":
        await query.edit_message_text("حذف لغو شد.")
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="پنل مدیریت:",
            reply_markup=admin_keyboard(),
        )
        context.user_data.pop("delete_video_id", None)
        return ConversationHandler.END

    video_id = context.user_data.get("delete_video_id")
    if video_id and delete_video(video_id):
        await query.edit_message_text("✅ ویدیو حذف شد.")
    else:
        await query.edit_message_text("⚠️ حذف انجام نشد.")
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text="پنل مدیریت:",
        reply_markup=admin_keyboard(),
    )
    context.user_data.pop("delete_video_id", None)
    return ConversationHandler.END


# ----- ویرایش ویدیو -----
async def admin_edit_video_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    courses = get_all_courses()
    if not courses:
        await update.message.reply_text("درسی وجود ندارد.", reply_markup=admin_keyboard())
        return ConversationHandler.END
    buttons = [
        [InlineKeyboardButton(name, callback_data=f"editvid_course_{cid}")]
        for cid, name, _ in courses
    ]
    await update.message.reply_text(
        "درس مربوط به ویدیو را انتخاب کنید:\n\n/cancel برای لغو",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return EDIT_VIDEO_SELECT


async def admin_edit_video_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data.startswith("editvid_course_"):
        course_id = int(query.data.split("_")[2])
        videos = get_videos_by_course(course_id)
        if not videos:
            await query.edit_message_text("این درس ویدیویی ندارد.")
            return ConversationHandler.END
        buttons = []
        for vid_id, title, episode, _ in videos:
            label = f"قسمت {episode} — {title}" if episode else title
            buttons.append(
                [InlineKeyboardButton(f"✏️ {label[:50]}", callback_data=f"editvid_id_{vid_id}")]
            )
        await query.edit_message_text(
            "ویدیوی مورد نظر را انتخاب کنید:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return EDIT_VIDEO_SELECT

    if query.data.startswith("editvid_id_"):
        video_id = int(query.data.split("_")[2])
        video = get_video_by_id(video_id)
        if not video:
            await query.edit_message_text("ویدیو پیدا نشد.")
            return ConversationHandler.END
        context.user_data["edit_video_id"] = video_id
        _, title, episode, url, course_name, _ = video
        text = (
            f"ویرایش ویدیو:\n\n"
            f"📘 {safe_text(course_name)}\n"
            f"🎬 عنوان: {safe_text(title)}\n"
            f"📌 قسمت: {safe_text(str(episode or '-'))}\n"
            f"🔗 {safe_text(url)}\n\n"
            "کدام مورد را می‌خواهید ویرایش کنید؟"
        )
        buttons = [
            [InlineKeyboardButton("📝 عنوان", callback_data="editvid_field_title")],
            [InlineKeyboardButton("📌 شماره قسمت", callback_data="editvid_field_episode")],
            [InlineKeyboardButton("🔗 لینک", callback_data="editvid_field_url")],
            [InlineKeyboardButton("❌ انصراف", callback_data="editvid_field_cancel")],
        ]
        await query.edit_message_text(
            text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode=ParseMode.HTML
        )
        return EDIT_VIDEO_FIELD

    return EDIT_VIDEO_SELECT


async def admin_edit_video_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "editvid_field_cancel":
        await query.edit_message_text("ویرایش لغو شد.")
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="پنل مدیریت:",
            reply_markup=admin_keyboard(),
        )
        context.user_data.pop("edit_video_id", None)
        return ConversationHandler.END

    field = query.data.replace("editvid_field_", "")
    context.user_data["edit_video_field"] = field
    prompts = {
        "title": "عنوان جدید را بفرستید:",
        "episode": "شماره قسمت جدید را بفرستید (مثال: 12 یا 12-1):",
        "url": "لینک جدید نماشا را بفرستید:",
    }
    await query.edit_message_text(prompts.get(field, "مقدار جدید را بفرستید:"))
    return EDIT_VIDEO_VALUE


async def admin_edit_video_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    video_id = context.user_data.get("edit_video_id")
    field = context.user_data.get("edit_video_field")
    value = normalize_digits(update.message.text.strip())
    if not video_id or not field:
        await update.message.reply_text("خطا در ویرایش.", reply_markup=admin_keyboard())
        return ConversationHandler.END
    if field == "url" and "namasha.com" not in value:
        await update.message.reply_text("لینک معتبر نیست. دوباره بفرستید:")
        return EDIT_VIDEO_VALUE

    kwargs = {}
    if field == "title":
        kwargs["title"] = value
    elif field == "episode":
        kwargs["episode"] = value
    elif field == "url":
        kwargs["namasha_url"] = value

    ok = update_video(video_id, **kwargs)
    if ok:
        await update.message.reply_text(
            "✅ ویدیو با موفقیت ویرایش شد.", reply_markup=admin_keyboard()
        )
    else:
        await update.message.reply_text(
            "⚠️ ویرایش انجام نشد (شاید لینک تکراری باشد).",
            reply_markup=admin_keyboard(),
        )
    context.user_data.pop("edit_video_id", None)
    context.user_data.pop("edit_video_field", None)
    return ConversationHandler.END


async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = update.effective_user.id

    if context.user_data.get("waiting_search"):
        handled = await handle_search(update, context)
        if handled:
            return
        # اگر False بود یعنی کاربر دکمه منو زده؛ ادامه بده

    if text in ("🏠 منوی اصلی", "🔙 بازگشت به منوی اصلی"):
        await back_to_main(update, context)
    elif text == "📚 لیست دروس":
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
    else:
        await update.message.reply_text(
            "دستور نامعتبر است. از منو استفاده کنید.",
            reply_markup=main_keyboard(is_admin(user_id)),
        )


def main():
    if not BOT_TOKEN:
        print("❌ خطا: BOT_TOKEN تنظیم نشده است.")
        return
    if not ADMIN_IDS:
        print("⚠️ هشدار: هیچ ادمینی تعریف نشده است.")

    init_db()
    print("✅ دیتابیس آماده است.")

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

    add_course_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^➕ افزودن درس$"), admin_add_course_start)],
        states={
            ADD_COURSE_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_course_name)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )
    edit_course_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^✏️ ویرایش درس$"), admin_edit_course_start)],
        states={
            EDIT_COURSE_SELECT: [
                CallbackQueryHandler(admin_edit_course_select, pattern=r"^editcourse_")
            ],
            EDIT_COURSE_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_edit_course_name)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )
    delete_course_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^🗑 حذف درس$"), admin_delete_course_start)],
        states={
            DELETE_COURSE_SELECT: [
                CallbackQueryHandler(admin_delete_course_select, pattern=r"^delcourse_\d+$")
            ],
            DELETE_COURSE_CONFIRM: [
                CallbackQueryHandler(admin_delete_course_confirm, pattern=r"^delcourse_(yes|no)$")
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )
    add_video_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^🎬 افزودن ویدیو$"), admin_add_video_start)],
        states={
            ADD_VIDEO_COURSE: [CallbackQueryHandler(admin_add_video_course, pattern=r"^addvid_course_")],
            ADD_VIDEO_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_video_title)],
            ADD_VIDEO_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_video_url)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )
    delete_video_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^🗑 حذف ویدیو$"), admin_delete_video_start)],
        states={
            DELETE_VIDEO_SELECT: [CallbackQueryHandler(admin_delete_video_course, pattern=r"^delvid_")],
            DELETE_VIDEO_CONFIRM: [
                CallbackQueryHandler(admin_delete_video_confirm, pattern=r"^delvid_confirm_")
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )
    edit_video_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^✏️ ویرایش ویدیو$"), admin_edit_video_start)],
        states={
            EDIT_VIDEO_SELECT: [CallbackQueryHandler(admin_edit_video_select, pattern=r"^editvid_")],
            EDIT_VIDEO_FIELD: [
                CallbackQueryHandler(admin_edit_video_field, pattern=r"^editvid_field_")
            ],
            EDIT_VIDEO_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_edit_video_value)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(add_course_conv)
    app.add_handler(edit_course_conv)
    app.add_handler(delete_course_conv)
    app.add_handler(add_video_conv)
    app.add_handler(delete_video_conv)
    app.add_handler(edit_video_conv)
    app.add_handler(CallbackQueryHandler(course_selected, pattern=r"^course_\d+$"))
    app.add_handler(CallbackQueryHandler(courses_page_callback, pattern=r"^courses_page_\d+$"))
    app.add_handler(CallbackQueryHandler(course_videos_page_callback, pattern=r"^cv_\d+_page_\d+$"))
    app.add_handler(CallbackQueryHandler(video_selected, pattern=r"^video_\d+$"))
    app.add_handler(CallbackQueryHandler(back_to_courses, pattern=r"^back_courses$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
    app.add_error_handler(error_handler)

    print("🤖 ربات در حال اجرا است...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
