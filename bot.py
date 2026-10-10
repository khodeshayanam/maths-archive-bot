import os
import re
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime
from typing import Optional, List, Tuple
from contextlib import contextmanager

import psycopg
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
DATABASE_URL = os.getenv("DATABASE_URL")

PAGE_SIZE = 10

(
    ADD_COURSE_NAME,
    ADD_VIDEO_COURSE,
    ADD_VIDEO_TITLE,
    ADD_VIDEO_URL,
    ADD_VIDEO_DL,
    ADD_VIDEO_TG,
    EDIT_VIDEO_SELECT,
    EDIT_VIDEO_FIELD,
    EDIT_VIDEO_VALUE,
    DELETE_VIDEO_SELECT,
    DELETE_VIDEO_CONFIRM,
    EDIT_COURSE_SELECT,
    EDIT_COURSE_NAME,
    DELETE_COURSE_SELECT,
    DELETE_COURSE_CONFIRM,
    MAT_COURSE,
    MAT_SECTION,
    MAT_CATEGORY,
    MAT_CAT_NAME,
    MAT_TITLE,
    MAT_FILES,
    DEL_MAT_COURSE,
    DEL_MAT_SECTION,
    DEL_MAT_ITEM,
    DEL_MAT_CONFIRM,
) = range(25)

PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")

SECTION_MATERIALS = "materials"
SECTION_EXAMS = "exams"
SECTION_LABELS = {
    SECTION_MATERIALS: "📄 جزوات و کتاب‌ها",
    SECTION_EXAMS: "📝 نمونه سوالات و امتحانات",
}

import hashlib

SECTION_CODE = {SECTION_MATERIALS: "m", SECTION_EXAMS: "e"}
CODE_TO_SECTION = {"m": SECTION_MATERIALS, "e": SECTION_EXAMS}


def pack_category_callback(prefix: str, course_id: int, section: str, category: str) -> str:
    """callback_data حداکثر ۶۴ بایت؛ نام دسته یا هش پایدار."""
    sec = SECTION_CODE.get(section, "x")
    raw = f"{prefix}_{course_id}_{sec}_{category}"
    if len(raw.encode("utf-8")) <= 64:
        return raw
    h = hashlib.sha1(f"{course_id}:{section}:{category}".encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{course_id}_{sec}_H{h}"


def resolve_category_from_callback(data: str, prefix: str) -> tuple:
    """برمی‌گرداند (course_id, section, category) یا (None, None, None)."""
    if not data.startswith(prefix + "_"):
        return None, None, None
    rest = data[len(prefix) + 1 :]
    parts = rest.split("_", 2)
    if len(parts) < 3:
        return None, None, None
    try:
        course_id = int(parts[0])
    except ValueError:
        return None, None, None
    sec_code = parts[1]
    section = CODE_TO_SECTION.get(sec_code)
    if not section:
        return None, None, None
    cat_part = parts[2]
    if cat_part.startswith("H") and len(cat_part) == 17:
        target = cat_part[1:]
        for cat in get_material_categories(course_id, section):
            h = hashlib.sha1(f"{course_id}:{section}:{cat}".encode("utf-8")).hexdigest()[:16]
            if h == target:
                return course_id, section, cat
        return None, None, None
    return course_id, section, cat_part


def normalize_digits(text: str) -> str:
    """ارقام فارسی + یکسان‌سازی ي/ك عربی + حذف اعراب و کشیده."""
    if not text:
        return text
    letters = str.maketrans({"ي": "ی", "ى": "ی", "ك": "ک", "ة": "ه"})
    text = text.translate(PERSIAN_DIGITS).translate(letters)
    text = re.sub(r"[\u064B-\u065F\u0670\u0640]", "", text)  # اعراب و ـ
    return text


def safe_text(text: str) -> str:
    if not text:
        return ""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def is_valid_namasha_url(url: str) -> bool:
    if not url:
        return False
    return bool(re.match(r"^https?://(www\.)?namasha\.com/", url.strip(), re.IGNORECASE))


def is_valid_download_url(url: str) -> bool:
    if not url:
        return False
    u = url.strip()
    if u in ("-", "ندارد", "skip", "بدون", "خیر"):
        return True  # skip marker
    return bool(re.match(r"^https?://", u, re.IGNORECASE))


def extract_episode(title: str) -> Optional[str]:
    title = normalize_digits(title)
    match = re.search(
        r"(?:قسمت|جلسه|part|episode)\s*([0-9]+(?:[.-][0-9]+)?)",
        title,
        re.IGNORECASE,
    )
    if match:
        return match.group(1)
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


def _db_url() -> str:
    """برای Supabase در صورت نیاز sslmode=require اضافه می‌شود."""
    url = DATABASE_URL or ""
    if "sslmode=" not in url and ("supabase.com" in url or "pooler.supabase" in url):
        join = "&" if "?" in url else "?"
        url = f"{url}{join}sslmode=require"
    return url


@contextmanager
def get_connection():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL تنظیم نشده است.")
    conn = psycopg.connect(_db_url())
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS courses (
                id SERIAL PRIMARY KEY,
                name TEXT UNIQUE NOT NULL,
                teacher TEXT,
                created_at TEXT
            )
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS videos (
                id SERIAL PRIMARY KEY,
                course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                episode TEXT,
                namasha_url TEXT NOT NULL,
                download_url TEXT,
                telegram_file_id TEXT,
                created_at TEXT
            )
            """
        )
        # migrate older DBs that had UNIQUE on namasha_url only
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS materials (
                id SERIAL PRIMARY KEY,
                course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
                section TEXT NOT NULL,
                category TEXT NOT NULL,
                title TEXT,
                file_id TEXT NOT NULL,
                file_type TEXT NOT NULL,
                caption TEXT,
                created_at TEXT
            )
            """
        )
        # add columns if upgrading from older videos schema
        for col, typ in [
            ("download_url", "TEXT"),
            ("telegram_file_id", "TEXT"),
        ]:
            try:
                c.execute(f"ALTER TABLE videos ADD COLUMN IF NOT EXISTS {col} {typ}")
            except Exception:
                pass


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# ---------- courses ----------
def add_course(name: str, teacher: str = None) -> bool:
    try:
        with get_connection() as conn:
            c = conn.cursor()
            c.execute(
                "INSERT INTO courses (name, teacher, created_at) VALUES (%s, %s, %s)",
                (name.strip(), teacher, datetime.now().isoformat()),
            )
        return True
    except psycopg.errors.IntegrityError:
        return False


def update_course(course_id: int, name: str) -> bool:
    try:
        with get_connection() as conn:
            c = conn.cursor()
            c.execute(
                "UPDATE courses SET name = %s WHERE id = %s",
                (normalize_digits(name.strip()), course_id),
            )
            return c.rowcount > 0
    except psycopg.errors.IntegrityError:
        return False


def delete_course(course_id: int) -> bool:
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM materials WHERE course_id = %s", (course_id,))
        c.execute("DELETE FROM videos WHERE course_id = %s", (course_id,))
        c.execute("DELETE FROM courses WHERE id = %s", (course_id,))
        return c.rowcount > 0


def get_all_courses() -> List[Tuple]:
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT id, name, teacher FROM courses ORDER BY name")
        return c.fetchall()


def get_course_by_id(course_id: int):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT id, name, teacher FROM courses WHERE id = %s", (course_id,))
        return c.fetchone()


def count_videos_in_course(course_id: int) -> int:
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM videos WHERE course_id = %s", (course_id,))
        return c.fetchone()[0]


# ---------- videos ----------
def add_video(
    course_id: int,
    title: str,
    namasha_url: str,
    episode: str = None,
    download_url: str = None,
    telegram_file_id: str = None,
) -> bool:
    try:
        title = normalize_digits(title.strip())
        episode = normalize_digits(episode) if episode else extract_episode(title)
        dl = None
        if download_url and download_url.strip() not in ("-", "ندارد", "skip", "بدون", "خیر", ""):
            dl = download_url.strip()
        with get_connection() as conn:
            c = conn.cursor()
            c.execute(
                """
                INSERT INTO videos
                (course_id, title, episode, namasha_url, download_url, telegram_file_id, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    course_id,
                    title,
                    episode,
                    namasha_url.strip(),
                    dl,
                    telegram_file_id,
                    datetime.now().isoformat(),
                ),
            )
        return True
    except psycopg.errors.IntegrityError:
        return False


def update_video(
    video_id: int,
    title: str = None,
    episode: str = None,
    namasha_url: str = None,
    download_url: str = None,
    telegram_file_id: str = None,
) -> bool:
    fields = []
    values = []
    if title is not None:
        title = normalize_digits(title.strip())
        fields.append("title = %s")
        values.append(title)
        if episode is None:
            episode = extract_episode(title)
    if episode is not None:
        fields.append("episode = %s")
        values.append(normalize_digits(str(episode)))
    if namasha_url is not None:
        fields.append("namasha_url = %s")
        values.append(namasha_url.strip())
    if download_url is not None:
        dl = download_url.strip()
        if dl in ("-", "ندارد", "skip", "بدون", "خیر", ""):
            dl = None
        fields.append("download_url = %s")
        values.append(dl)
    if telegram_file_id is not None:
        fields.append("telegram_file_id = %s")
        values.append(telegram_file_id if telegram_file_id else None)
    if not fields:
        return False
    values.append(video_id)
    try:
        with get_connection() as conn:
            c = conn.cursor()
            c.execute(f"UPDATE videos SET {', '.join(fields)} WHERE id = %s", values)
            return c.rowcount > 0
    except psycopg.errors.IntegrityError:
        return False


def get_videos_by_course(course_id: int) -> List[Tuple]:
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            """
            SELECT id, title, episode, namasha_url, download_url, telegram_file_id
            FROM videos WHERE course_id = %s
            """,
            (course_id,),
        )
        rows = c.fetchall()
    return sorted(rows, key=lambda r: episode_sort_key(r[2]))


def get_video_by_id(video_id: int):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            """
            SELECT v.id, v.title, v.episode, v.namasha_url, v.download_url,
                   v.telegram_file_id, c.name, v.course_id
            FROM videos v
            JOIN courses c ON v.course_id = c.id
            WHERE v.id = %s
            """,
            (video_id,),
        )
        return c.fetchone()


def delete_video(video_id: int) -> bool:
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM videos WHERE id = %s", (video_id,))
        return c.rowcount > 0


def clean_caption_for_match(caption: str) -> str:
    text = caption or ""
    text = re.sub(r"#\S+", " ", text)
    # تاریخ‌هایی مثل 15/01/1405
    text = re.sub(r"\b\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}\b", " ", text)
    text = normalize_digits(text)
    text = text.replace("‌", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _extract_episode_num(text: str):
    """شماره قسمت از کپشن/عنوان (پشتیبانی 23-2)."""
    matches = re.findall(
        r"(?:قسمت|جلسه)\s*([0-9]+(?:\s*[-–./]\s*[0-9]+)?)",
        text or "",
    )
    if not matches:
        return None
    ep = normalize_digits(matches[-1])
    ep = re.sub(r"\s+", "", ep)
    ep = ep.replace("–", "-")
    return ep


def find_video_for_autolink(caption: str):
    """
    تطبیق: نام درس در کپشن + شماره قسمت.
    امتیازدهی سخت‌گیرانه تا درس‌های شبیه (روحانی / ناپارامتری) قاطی نشوند.
    """
    text = clean_caption_for_match(caption)
    if not text or len(text) < 5:
        return None

    ep = _extract_episode_num(text)
    if not ep:
        return None

    STOP = {
        "دکتر", "درس", "مبانی", "با", "در", "و", "از", "به", "برای",
        "های", "ها", "یک", "۱", "2", "۲", "قسمت", "جلسه",
    }

    def toks(name: str):
        n = normalize_digits((name or "").replace("‌", " "))
        n = re.sub(r"\s+", " ", n).strip()
        return [w for w in re.split(r"[\s\-_:/]+", n) if len(w) >= 2 and w not in STOP]

    def score_course(name: str) -> float:
        """چقدر از نام درس واقعاً داخل کپشن است (۰ تا ۱+ طول)."""
        n = normalize_digits((name or "").replace("‌", " "))
        n = re.sub(r"\s+", " ", n).strip()
        if len(n) < 4:
            return 0.0
        # اگر کل نام داخل کپشن باشد بهترین حالت
        if n in text:
            return 1000.0 + len(n)
        words = toks(name)
        if not words:
            return 0.0
        hit = [w for w in words if w in text]
        if not hit:
            return 0.0
        ratio = len(hit) / len(words)
        # حداقل ۶۰٪ توکن‌های معنادار باید در کپشن باشند
        if ratio < 0.6:
            return 0.0
        # توکن‌های خاص‌تر (بلندتر) وزن بیشتر
        weight = sum(len(w) for w in hit)
        return weight * ratio + len(hit) * 0.1

    with get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT id, name FROM courses")
        courses = c.fetchall()

        scored = []
        for cid, name in courses:
            s = score_course(name)
            if s > 0:
                scored.append((s, cid, name))

        if not scored:
            return None

        scored.sort(key=lambda x: -x[0])
        best_score = scored[0][0]
        # فقط درس‌هایی که امتیازشان نزدیک بهترین است
        top = [x for x in scored if x[0] >= best_score * 0.85]
        # اگر چند تا ماند، آن که توکن‌های خاص‌تری در کپشن دارد
        course_ids = [x[1] for x in top]

        c.execute(
            """
            SELECT v.id, v.title, v.episode, c.name, v.telegram_file_id, v.course_id
            FROM videos v
            JOIN courses c ON v.course_id = c.id
            WHERE v.course_id = ANY(%s)
            """,
            (course_ids,),
        )
        rows = c.fetchall()

        def ep_norm(x: str) -> str:
            x = normalize_digits(str(x or ""))
            x = re.sub(r"\s+", "", x).replace("–", "-").replace("/", "-").replace(".", "-")
            return x

        target = ep_norm(ep)
        exact = [r for r in rows if ep_norm(r[2]) == target]
        if not exact:
            exact = [
                r
                for r in rows
                if ep_norm(_extract_episode_num(r[1] or "") or "") == target
            ]
        if not exact:
            return None

        # بین ویدیوهای هم‌قسمت، درسی با بالاترین score
        score_by_id = {cid: s for s, cid, _ in scored}

        def row_score(r):
            return score_by_id.get(r[5], 0)

        exact.sort(key=row_score, reverse=True)
        # اگر دو درس امتیاز نزدیک دارند ولی یکی کلمهٔ متمایز در کپشن دارد
        best = exact[0]
        if len(exact) > 1 and row_score(exact[0]) == row_score(exact[1]):
            # ترجیح نامی که همهٔ توکن‌هایش در کپشن است
            def full_fit(r):
                return 1 if score_course(r[3]) >= 1000 else 0

            exact.sort(key=lambda r: (full_fit(r), row_score(r)), reverse=True)
            best = exact[0]
        elif len(top) > 1:
            # اختلاف امتیاز باید واضح باشد؛ وگرنه فقط اگر full name match
            if best_score < 1000 and scored[0][0] - scored[1][0] < 3:
                # نیاز به تمایز بیشتر: توکن‌هایی که فقط در یک درس هستند
                unique_hits = []
                for s, cid, name in top:
                    other_words = set()
                    for s2, cid2, name2 in top:
                        if cid2 != cid:
                            other_words |= set(toks(name2))
                    mine = set(toks(name)) - other_words
                    unique_in_caption = [w for w in mine if w in text]
                    unique_hits.append((len(unique_in_caption), s, cid, name))
                unique_hits.sort(reverse=True)
                if unique_hits and unique_hits[0][0] > 0:
                    prefer_cid = unique_hits[0][2]
                    preferred = [r for r in exact if r[5] == prefer_cid]
                    if preferred:
                        return preferred[0]
        return best



def search_all(query: str) -> List[Tuple]:
    """Search videos: returns (vid_id, title, episode, course_name)"""
    q = normalize_digits(query.strip())
    like = f"%{q}%"
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            """
            SELECT v.id, v.title, v.episode, c.name
            FROM videos v
            JOIN courses c ON v.course_id = c.id
            WHERE v.title ILIKE %s OR c.name ILIKE %s
               OR COALESCE(v.episode,'') ILIKE %s OR COALESCE(c.teacher,'') ILIKE %s
            ORDER BY c.name
            LIMIT 40
            """,
            (like, like, like, like),
        )
        rows = c.fetchall()
    return sorted(rows, key=lambda r: (r[3], episode_sort_key(r[2])))


def get_latest_videos(limit: int = 15):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            """
            SELECT v.id, v.title, v.episode, c.name
            FROM videos v
            JOIN courses c ON v.course_id = c.id
            ORDER BY v.id DESC
            LIMIT %s
            """,
            (limit,),
        )
        return c.fetchall()


def count_stats():
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM courses")
        courses = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM videos")
        videos = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM materials")
        mats = c.fetchone()[0]
    return courses, videos, mats


# ---------- materials ----------
def add_material(
    course_id: int,
    section: str,
    category: str,
    title: str,
    file_id: str,
    file_type: str,
    caption: str = None,
) -> bool:
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            """
            INSERT INTO materials
            (course_id, section, category, title, file_id, file_type, caption, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                course_id,
                section,
                category.strip(),
                (title or "").strip() or None,
                file_id,
                file_type,
                caption,
                datetime.now().isoformat(),
            ),
        )
    return True


def get_material_categories(course_id: int, section: str) -> List[str]:
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            """
            SELECT DISTINCT category FROM materials
            WHERE course_id = %s AND section = %s
            ORDER BY category
            """,
            (course_id, section),
        )
        return [r[0] for r in c.fetchall()]


def get_materials(course_id: int, section: str, category: str) -> List[Tuple]:
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            """
            SELECT id, title, file_id, file_type, caption
            FROM materials
            WHERE course_id = %s AND section = %s AND category = %s
            ORDER BY id
            """,
            (course_id, section, category),
        )
        return c.fetchall()


def get_material_by_id(mat_id: int):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            """
            SELECT m.id, m.title, m.file_id, m.file_type, m.caption,
                   m.course_id, m.section, m.category, c.name
            FROM materials m
            JOIN courses c ON m.course_id = c.id
            WHERE m.id = %s
            """,
            (mat_id,),
        )
        return c.fetchone()


def delete_material(mat_id: int) -> bool:
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM materials WHERE id = %s", (mat_id,))
        return c.rowcount > 0


def list_materials_for_course(course_id: int, section: str) -> List[Tuple]:
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            """
            SELECT id, category, title, file_type
            FROM materials
            WHERE course_id = %s AND section = %s
            ORDER BY category, id
            """,
            (course_id, section),
        )
        return c.fetchall()


# ---------- keyboards ----------
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
            [KeyboardButton("📎 افزودن جزوه/سوال"), KeyboardButton("🗑 حذف جزوه/سوال")],
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
    logging.error("Exception while handling an update", exc_info=context.error)
    err = context.error
    msg = f"⚠️ {type(err).__name__}: {err}"[:3500]
    for aid in ADMIN_IDS:
        try:
            await context.bot.send_message(chat_id=aid, text=msg)
        except Exception:
            pass
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ مشکلی پیش آمد. لطفاً دوباره امتحان کنید."
            )
        except Exception:
            pass



async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.effective_message.reply_text(
        "عملیات لغو شد.",
        reply_markup=main_keyboard(is_admin(update.effective_user.id)),
    )
    return ConversationHandler.END


# دکمه‌های منو/پنل که نباید به‌عنوان ورودی مراحل (مثلاً لینک نماشا) تفسیر شوند
MENU_BUTTON_PATTERN = (
    r"^(🏠 منوی اصلی|🔙 بازگشت به منوی اصلی|"
    r"📚 لیست دروس|🔍 جستجو|🆕 آخرین ویدیوها|📖 راهنما|"
    r"⚙️ پنل مدیریت|📊 آمار|"
    r"➕ افزودن درس|✏️ ویرایش درس|🗑 حذف درس|"
    r"🎬 افزودن ویدیو|✏️ ویرایش ویدیو|🗑 حذف ویدیو|"
    r"📎 افزودن جزوه/سوال|🗑 حذف جزوه/سوال)$"
)


def text_input_filter():
    """متن کاربر، به‌جز دکمه‌های منو و دستورات."""
    return filters.TEXT & ~filters.COMMAND & ~filters.Regex(MENU_BUTTON_PATTERN)


async def cancel_and_route(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """خروج از Conversation و اجرای همان دکمهٔ منو."""
    context.user_data.clear()
    text = (update.message.text or "").strip() if update.message else ""
    uid = update.effective_user.id

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
        if is_admin(uid):
            await admin_stats(update, context)
        else:
            await update.effective_message.reply_text(
                "دسترسی ندارید.",
                reply_markup=main_keyboard(False),
            )
    else:
        # دکمه‌های دیگر پنل ادمین: فقط لغو؛ کاربر دوباره همان دکمه را بزند
        kb = admin_keyboard() if is_admin(uid) else main_keyboard(False)
        await update.effective_message.reply_text(
            "عملیات قبلی لغو شد. دوباره دکمهٔ مورد نظر را بزنید.",
            reply_markup=kb,
        )
    return ConversationHandler.END


# ---------- user handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = (
        f"سلام {safe_text(user.first_name)} 👋\n\n"
        "به ربات <b>آرشیو دانشکده ریاضی</b> خوش آمدید.\n\n"
        "از منوی زیر دروس را ببینید؛ برای هر درس می‌توانید\n"
        "ویدیو، جزوه و نمونه سوال را دریافت کنید."
    )
    await update.message.reply_text(
        text,
        reply_markup=main_keyboard(is_admin(user.id)),
        parse_mode=ParseMode.HTML,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 <b>راهنما</b>\n\n"
        "• <b>📚 لیست دروس</b> — انتخاب درس\n"
        "• زیر هر درس: ویدیو / جزوات و کتاب‌ها / نمونه سوالات\n"
        "• ویدیوها: تماشا، دانلود، و در صورت موجود بودن ارسال در ربات\n"
        "• جزوات و سوالات: فایل PDF یا عکس مستقیم در ربات\n"
        "• <b>🔍 جستجو</b> — نام درس یا قسمت\n"
        "• <b>🏠 منوی اصلی</b> — بازگشت\n"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def show_courses_page(update, context, page=0, edit=False):
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
        "یک درس را انتخاب کنید:"
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
    await show_courses_page(update, context, 0, False)


async def courses_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    page = int(q.data.split("_")[-1])
    await show_courses_page(update, context, page, True)


async def show_course_hub(update, context, course_id: int, edit: bool = True):
    course = get_course_by_id(course_id)
    if not course:
        msg = "درس پیدا نشد."
        if edit and update.callback_query:
            await update.callback_query.edit_message_text(msg)
        else:
            await update.effective_message.reply_text(msg)
        return
    _, name, teacher = course
    text = f"📘 <b>{safe_text(name)}</b>"
    if teacher:
        text += f"\n👨‍🏫 {safe_text(teacher)}"
    text += "\n\nچه چیزی می‌خواهید؟"
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🎬 ویدیوها", callback_data=f"hub_vid_{course_id}")],
            [InlineKeyboardButton("📄 جزوات و کتاب‌ها", callback_data=f"hub_mat_{course_id}")],
            [InlineKeyboardButton("📝 نمونه سوالات و امتحانات", callback_data=f"hub_exam_{course_id}")],
            [InlineKeyboardButton("🔙 بازگشت به لیست دروس", callback_data="back_courses")],
        ]
    )
    if edit and update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                text, reply_markup=keyboard, parse_mode=ParseMode.HTML
            )
        except BadRequest:
            await update.callback_query.answer()
    else:
        await update.effective_message.reply_text(
            text, reply_markup=keyboard, parse_mode=ParseMode.HTML
        )


async def course_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    course_id = int(q.data.split("_")[1])
    await show_course_hub(update, context, course_id, True)


async def back_to_courses(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await show_courses_page(update, context, 0, True)


async def hub_videos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    course_id = int(q.data.split("_")[2])
    await show_course_videos_page(update, context, course_id, 0, True)


async def show_course_videos_page(update, context, course_id, page=0, edit=False):
    course = get_course_by_id(course_id)
    if not course:
        return
    _, name, _ = course
    videos = get_videos_by_course(course_id)
    if not videos:
        text = f"🎬 <b>{safe_text(name)}</b>\n\nهنوز ویدیویی ثبت نشده است."
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔙 بازگشت", callback_data=f"course_{course_id}")]]
        )
        if edit and update.callback_query:
            await update.callback_query.edit_message_text(
                text, reply_markup=kb, parse_mode=ParseMode.HTML
            )
        return
    items = []
    for vid_id, title, episode, url, dl, tg in videos:
        title_s = (title or "").strip()
        ep_s = str(episode or "").strip()
        if ep_s and (title_s.startswith("قسمت") or title_s == f"قسمت {ep_s}"):
            label = f"🎬 {title_s}" if title_s else f"🎬 قسمت {ep_s}"
        elif ep_s:
            label = f"🎬 قسمت {ep_s} — {title_s}" if title_s else f"🎬 قسمت {ep_s}"
        else:
            label = f"🎬 {title_s}"
        items.append([InlineKeyboardButton(label[:60], callback_data=f"video_{vid_id}")])
    markup, total, start, end = paginate_buttons(
        items, page, PAGE_SIZE, f"cv_{course_id}", back_data=f"course_{course_id}"
    )
    text = (
        f"🎬 <b>ویدیوهای {safe_text(name)}</b>\n"
        f"نمایش {start+1} تا {min(end, total)} از {total}"
    )
    if edit and update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                text, reply_markup=markup, parse_mode=ParseMode.HTML
            )
        except BadRequest:
            await update.callback_query.answer()


async def course_videos_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    parts = q.data.split("_")
    course_id = int(parts[1])
    page = int(parts[-1])
    await show_course_videos_page(update, context, course_id, page, True)


async def video_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    video_id = int(q.data.split("_")[1])
    video = get_video_by_id(video_id)
    if not video:
        await q.edit_message_text("ویدیو پیدا نشد.")
        return
    _, title, episode, url, dl, tg_file, course_name, course_id = video
    text = f"🎬 <b>{safe_text(title)}</b>\n\n📘 {safe_text(course_name)}\n"
    if episode:
        text += f"📌 قسمت: {safe_text(str(episode))}\n"
    rows = []
    if url and str(url).startswith(("http://", "https://")):
        rows.append([InlineKeyboardButton("▶️ تماشا در نماشا", url=url)])
    if dl and str(dl).startswith(("http://", "https://")):
        rows.append([InlineKeyboardButton("⬇️ دانلود مستقیم", url=dl)])
    if tg_file:
        rows.append([InlineKeyboardButton("📤 دریافت در ربات", callback_data=f"sendvid_{video_id}")])
    if not rows:
        text += "\n\nℹ️ این قسمت فقط از طریق فایل تلگرام در دسترس است."
    rows.append([InlineKeyboardButton("🔙 بازگشت به قسمت‌ها", callback_data=f"hub_vid_{course_id}")])
    rows.append([InlineKeyboardButton("📘 صفحه درس", callback_data=f"course_{course_id}")])
    try:
        await q.edit_message_text(
            text, reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML
        )
    except BadRequest:
        try:
            await q.edit_message_text(
                f"🎬 {title}\n📘 {course_name}",
                reply_markup=InlineKeyboardMarkup(rows),
            )
        except BadRequest:
            await q.answer("به‌روز شد.", show_alert=False)


async def send_video_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("در حال ارسال...")
    video_id = int(q.data.split("_")[1])
    video = get_video_by_id(video_id)
    if not video or not video[5]:
        await q.message.reply_text("فایل ویدیو در ربات ثبت نشده است.")
        return
    _, title, episode, _, _, tg_file, course_name, _ = video
    caption = f"🎬 {title}\n📘 {course_name}"
    if episode:
        caption += f"\n📌 قسمت {episode}"
    try:
        await context.bot.send_video(
            chat_id=q.message.chat_id,
            video=tg_file,
            caption=caption,
            supports_streaming=True,
        )
    except Exception:
        try:
            await context.bot.send_document(
                chat_id=q.message.chat_id,
                document=tg_file,
                caption=caption,
            )
        except Exception as e:
            logger.error("send video failed: %s", e)
            await q.message.reply_text(
                "ارسال فایل ممکن نشد. از لینک تماشا یا دانلود استفاده کنید."
            )


async def hub_materials(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    course_id = int(q.data.split("_")[2])
    await show_material_categories(update, context, course_id, SECTION_MATERIALS)


async def hub_exams(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    course_id = int(q.data.split("_")[2])
    await show_material_categories(update, context, course_id, SECTION_EXAMS)


async def show_material_categories(update, context, course_id, section):
    course = get_course_by_id(course_id)
    if not course:
        return
    cats = get_material_categories(course_id, section)
    label = SECTION_LABELS.get(section, section)
    if not cats:
        text = f"{label}\n\n📘 {safe_text(course[1])}\n\nهنوز موردی ثبت نشده است."
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔙 بازگشت", callback_data=f"course_{course_id}")]]
        )
        await update.callback_query.edit_message_text(
            text, reply_markup=kb, parse_mode=ParseMode.HTML
        )
        return
    buttons = [
        [
            InlineKeyboardButton(
                f"📁 {cat}",
                callback_data=pack_category_callback("mcat", course_id, section, cat),
            )
        ]
        for cat in cats
    ]
    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data=f"course_{course_id}")])
    text = f"{label}\n\n📘 <b>{safe_text(course[1])}</b>\n\nیک دسته را انتخاب کنید:"
    await update.callback_query.edit_message_text(
        text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode=ParseMode.HTML
    )


async def material_category_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    course_id, section, category = resolve_category_from_callback(q.data, "mcat")
    if course_id is None or not category:
        await q.edit_message_text("دسته پیدا نشد. دوباره از لیست درس انتخاب کنید.")
        return
    await show_material_list(update, context, course_id, section, category)


async def show_material_list(update, context, course_id, section, category):
    course = get_course_by_id(course_id)
    items = get_materials(course_id, section, category)
    label = SECTION_LABELS.get(section, section)
    if not items:
        text = f"موردی در «{safe_text(category)}» نیست."
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔙 بازگشت", callback_data=f"hub_mat_{course_id}" if section == SECTION_MATERIALS else f"hub_exam_{course_id}")]]
        )
        await update.callback_query.edit_message_text(text, reply_markup=kb)
        return
    buttons = []
    for mid, title, file_id, file_type, caption in items:
        if title:
            name = title
        elif caption:
            name = caption[:40]
        else:
            name = "📷 عکس" if file_type == "photo" else "📄 فایل"
        icon = "🖼" if file_type == "photo" else "📄"
        buttons.append(
            [InlineKeyboardButton(f"{icon} {name}"[:60], callback_data=f"mfile_{mid}")]
        )
    back = f"hub_mat_{course_id}" if section == SECTION_MATERIALS else f"hub_exam_{course_id}"
    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data=back)])
    text = (
        f"{label}\n"
        f"📁 <b>{safe_text(category)}</b>\n"
        f"📘 {safe_text(course[1])}\n\n"
        "یک مورد را انتخاب کنید:"
    )
    await update.callback_query.edit_message_text(
        text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode=ParseMode.HTML
    )


async def send_material_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("در حال ارسال...")
    mat_id = int(q.data.split("_")[1])
    mat = get_material_by_id(mat_id)
    if not mat:
        await q.message.reply_text("فایل پیدا نشد.")
        return
    _, title, file_id, file_type, caption, course_id, section, category, course_name = mat
    parts = []
    if title:
        parts.append(title)
    if caption:
        parts.append(caption)
    if not parts:
        parts.append(f"{category} — {course_name}")
    cap = "\n".join(parts)[:1024]
    try:
        if file_type == "photo":
            await context.bot.send_photo(
                chat_id=q.message.chat_id, photo=file_id, caption=cap
            )
        else:
            await context.bot.send_document(
                chat_id=q.message.chat_id, document=file_id, caption=cap
            )
    except Exception as e:
        logger.error("send material failed: %s", e)
        await q.message.reply_text("ارسال فایل ممکن نشد. دوباره تلاش کنید.")


async def search_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔍 عبارت مورد نظر را بنویسید (نام درس یا قسمت):",
        reply_markup=main_keyboard(is_admin(update.effective_user.id)),
    )
    context.user_data["waiting_search"] = True


async def handle_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("waiting_search"):
        return False
    query = update.message.text.strip()
    menu = {
        "🏠 منوی اصلی",
        "📚 لیست دروس",
        "🔍 جستجو",
        "🆕 آخرین ویدیوها",
        "📖 راهنما",
        "⚙️ پنل مدیریت",
    }
    if query in menu:
        context.user_data["waiting_search"] = False
        return False
    context.user_data["waiting_search"] = False
    if len(query) < 2:
        await update.message.reply_text("عبارت خیلی کوتاه است.")
        return True
    results = search_all(query)
    if not results:
        await update.message.reply_text(f"نتیجه‌ای برای «{query}» پیدا نشد.")
        return True
    buttons = []
    for vid_id, title, episode, course_name in results[:20]:
        if episode:
            label = f"{course_name} | قسمت {episode}"
        else:
            label = f"{course_name} — {title}"
        buttons.append([InlineKeyboardButton(label[:60], callback_data=f"video_{vid_id}")])
    await update.message.reply_text(
        f"🔎 نتایج «{safe_text(query)}»:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode=ParseMode.HTML,
    )
    return True


async def latest_videos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = get_latest_videos(15)
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
        "🆕 <b>آخرین ویدیوها</b>",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode=ParseMode.HTML,
    )


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("شما دسترسی ادمین ندارید.")
        return
    courses, videos, mats = count_stats()
    text = (
        "⚙️ <b>پنل مدیریت</b>\n\n"
        f"تعداد دروس: {courses}\n"
        f"تعداد ویدیوها: {videos}\n"
        f"تعداد فایل‌های جزوه/سوال: {mats}"
    )
    await update.message.reply_text(text, reply_markup=admin_keyboard(), parse_mode=ParseMode.HTML)


async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    courses, videos, mats = count_stats()
    await update.message.reply_text(
        f"📊 <b>آمار</b>\n\nدروس: {courses}\nویدیو: {videos}\nجزوه/سوال: {mats}",
        parse_mode=ParseMode.HTML,
        reply_markup=admin_keyboard(),
    )


async def back_to_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "🏠 منوی اصلی",
        reply_markup=main_keyboard(is_admin(update.effective_user.id)),
    )


# ---------- admin: courses ----------
async def admin_add_course_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    await update.message.reply_text(
        "نام درس را وارد کنید:\n\nبرای لغو: /cancel یا «🏠 منوی اصلی»",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ADD_COURSE_NAME


async def admin_add_course_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = normalize_digits(update.message.text.strip())
    if name in ("🏠 منوی اصلی", "/cancel"):
        return await cancel(update, context)
    if len(name) < 2:
        await update.message.reply_text("نام خیلی کوتاه است.")
        return ADD_COURSE_NAME
    if add_course(name):
        await update.message.reply_text(f"✅ درس «{name}» اضافه شد.", reply_markup=admin_keyboard())
    else:
        await update.message.reply_text("⚠️ این درس از قبل وجود دارد.", reply_markup=admin_keyboard())
    return ConversationHandler.END


async def admin_edit_course_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    courses = get_all_courses()
    if not courses:
        await update.message.reply_text("درسی وجود ندارد.", reply_markup=admin_keyboard())
        return ConversationHandler.END
    buttons = [[InlineKeyboardButton(n, callback_data=f"editcourse_{i}")] for i, n, _ in courses]
    await update.message.reply_text(
        "درس را برای ویرایش انتخاب کنید:\nبرای لغو: /cancel یا «🏠 منوی اصلی»",
        reply_markup=ReplyKeyboardRemove(),
    )
    await update.message.reply_text("دروس:", reply_markup=InlineKeyboardMarkup(buttons))
    return EDIT_COURSE_SELECT


async def admin_edit_course_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    course_id = int(q.data.split("_")[1])
    course = get_course_by_id(course_id)
    if not course:
        await q.edit_message_text("پیدا نشد.")
        return ConversationHandler.END
    context.user_data["edit_course_id"] = course_id
    await q.edit_message_text(
        f"نام فعلی: <b>{safe_text(course[1])}</b>\n\nنام جدید را بفرستید:",
        parse_mode=ParseMode.HTML,
    )
    return EDIT_COURSE_NAME


async def admin_edit_course_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = normalize_digits(update.message.text.strip())
    if name in ("🏠 منوی اصلی", "/cancel"):
        return await cancel(update, context)
    course_id = context.user_data.get("edit_course_id")
    if not course_id or len(name) < 2:
        await update.message.reply_text("نامعتبر.", reply_markup=admin_keyboard())
        return ConversationHandler.END
    if update_course(course_id, name):
        await update.message.reply_text(f"✅ به «{name}» تغییر کرد.", reply_markup=admin_keyboard())
    else:
        await update.message.reply_text("⚠️ ویرایش نشد.", reply_markup=admin_keyboard())
    return ConversationHandler.END


async def admin_delete_course_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    courses = get_all_courses()
    if not courses:
        await update.message.reply_text("درسی نیست.", reply_markup=admin_keyboard())
        return ConversationHandler.END
    buttons = [[InlineKeyboardButton(f"🗑 {n}", callback_data=f"delcourse_{i}")] for i, n, _ in courses]
    await update.message.reply_text(
        "درس برای حذف:\n⚠️ ویدیوها و جزوات هم پاک می‌شوند.\nلغو: /cancel",
        reply_markup=ReplyKeyboardRemove(),
    )
    await update.message.reply_text("انتخاب:", reply_markup=InlineKeyboardMarkup(buttons))
    return DELETE_COURSE_SELECT


async def admin_delete_course_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    course_id = int(q.data.split("_")[1])
    course = get_course_by_id(course_id)
    if not course:
        await q.edit_message_text("پیدا نشد.")
        return ConversationHandler.END
    context.user_data["delete_course_id"] = course_id
    n = count_videos_in_course(course_id)
    buttons = [[
        InlineKeyboardButton("✅ بله", callback_data="delcourse_yes"),
        InlineKeyboardButton("❌ خیر", callback_data="delcourse_no"),
    ]]
    await q.edit_message_text(
        f"حذف «<b>{safe_text(course[1])}</b>»؟\nویدیوها: {n}",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode=ParseMode.HTML,
    )
    return DELETE_COURSE_CONFIRM


async def admin_delete_course_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "delcourse_no":
        await q.edit_message_text("لغو شد.")
        await context.bot.send_message(q.message.chat_id, "پنل:", reply_markup=admin_keyboard())
        return ConversationHandler.END
    cid = context.user_data.get("delete_course_id")
    if cid and delete_course(cid):
        await q.edit_message_text("✅ حذف شد.")
    else:
        await q.edit_message_text("⚠️ انجام نشد.")
    await context.bot.send_message(q.message.chat_id, "پنل:", reply_markup=admin_keyboard())
    return ConversationHandler.END


# ---------- admin: videos ----------
async def admin_add_video_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    courses = get_all_courses()
    if not courses:
        await update.message.reply_text("اول درس اضافه کنید.", reply_markup=admin_keyboard())
        return ConversationHandler.END
    buttons = [[InlineKeyboardButton(n, callback_data=f"addvid_course_{i}")] for i, n, _ in courses]
    await update.message.reply_text(
        "درس ویدیو را انتخاب کنید:\nلغو: /cancel یا «🏠 منوی اصلی»",
        reply_markup=ReplyKeyboardRemove(),
    )
    await update.message.reply_text("دروس:", reply_markup=InlineKeyboardMarkup(buttons))
    return ADD_VIDEO_COURSE


async def admin_add_video_course(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    course_id = int(q.data.split("_")[2])
    context.user_data["add_video_course_id"] = course_id
    course = get_course_by_id(course_id)
    await q.edit_message_text(
        f"درس: <b>{safe_text(course[1])}</b>\n\nعنوان ویدیو را بفرستید (مثال: قسمت ۲):",
        parse_mode=ParseMode.HTML,
    )
    return ADD_VIDEO_TITLE


async def admin_add_video_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    title = normalize_digits(update.message.text.strip())
    if title in ("🏠 منوی اصلی", "/cancel"):
        return await cancel(update, context)
    context.user_data["add_video_title"] = title
    context.user_data["add_video_episode"] = extract_episode(title)
    await update.message.reply_text(
        "لینک تماشا در نماشا را بفرستید:\nhttps://www.namasha.com/v/..."
    )
    return ADD_VIDEO_URL


async def admin_add_video_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    if url in ("🏠 منوی اصلی", "/cancel"):
        return await cancel(update, context)
    if not is_valid_namasha_url(url):
        await update.message.reply_text("لینک نماشا معتبر نیست. دوباره بفرستید:")
        return ADD_VIDEO_URL
    context.user_data["add_video_url"] = url
    await update.message.reply_text(
        "لینک دانلود مستقیم را بفرستید.\n"
        "اگر ندارید بنویسید: ندارد"
    )
    return ADD_VIDEO_DL


async def admin_add_video_dl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.text:
        await update.message.reply_text(
            "لطفاً لینک دانلود را به‌صورت متن بفرستید، یا بنویسید: ندارد"
        )
        return ADD_VIDEO_DL
    text = update.message.text.strip()
    if text in ("🏠 منوی اصلی", "/cancel"):
        return await cancel(update, context)
    if not is_valid_download_url(text):
        await update.message.reply_text("لینک نامعتبر. یا لینک http بفرستید یا «ندارد»:")
        return ADD_VIDEO_DL
    context.user_data["add_video_dl"] = text
    await update.message.reply_text(
        "اگر می‌خواهید ویدیو مستقیم در ربات ارسال شود، همین حالا فایل ویدیو را بفرستید.\n"
        "در غیر این صورت بنویسید: رد\n\n"
        "(می‌توانید از چت‌های دیگر Forward کنید)"
    )
    return ADD_VIDEO_TG


async def admin_add_video_tg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text:
        t = update.message.text.strip()
        if t in ("🏠 منوی اصلی", "/cancel"):
            return await cancel(update, context)
        tg_id = None
        if t not in ("رد", "ندارد", "skip", "-"):
            await update.message.reply_text("یا ویدیو بفرستید یا بنویسید: رد")
            return ADD_VIDEO_TG
    else:
        tg_id = None
        if update.message.video:
            tg_id = update.message.video.file_id
        elif update.message.document:
            tg_id = update.message.document.file_id
        else:
            await update.message.reply_text("فایل ویدیو یا «رد» بفرستید.")
            return ADD_VIDEO_TG

    ok = add_video(
        context.user_data["add_video_course_id"],
        context.user_data["add_video_title"],
        context.user_data["add_video_url"],
        context.user_data.get("add_video_episode"),
        context.user_data.get("add_video_dl"),
        tg_id,
    )
    if ok:
        extra = " + فایل تلگرام" if tg_id else ""
        await update.message.reply_text(
            f"✅ ویدیو ثبت شد{extra}.", reply_markup=admin_keyboard()
        )
    else:
        await update.message.reply_text("⚠️ ثبت نشد.", reply_markup=admin_keyboard())
    for k in list(context.user_data.keys()):
        if k.startswith("add_video"):
            context.user_data.pop(k, None)
    return ConversationHandler.END


async def admin_delete_video_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    courses = get_all_courses()
    if not courses:
        await update.message.reply_text("درسی نیست.", reply_markup=admin_keyboard())
        return ConversationHandler.END
    buttons = [[InlineKeyboardButton(n, callback_data=f"delvid_course_{i}")] for i, n, _ in courses]
    await update.message.reply_text(
        "درس را انتخاب کنید:\nلغو: /cancel",
        reply_markup=ReplyKeyboardRemove(),
    )
    await update.message.reply_text("دروس:", reply_markup=InlineKeyboardMarkup(buttons))
    return DELETE_VIDEO_SELECT


async def admin_delete_video_course(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data.startswith("delvid_course_"):
        course_id = int(q.data.split("_")[2])
        videos = get_videos_by_course(course_id)
        if not videos:
            await q.edit_message_text("ویدیویی نیست.")
            return ConversationHandler.END
        buttons = []
        for vid_id, title, episode, *_ in videos:
            label = title if (title or "").startswith("قسمت") else (f"قسمت {episode} — {title}" if episode else (title or "—"))
            buttons.append(
                [InlineKeyboardButton(f"🗑 {label}"[:50], callback_data=f"delvid_id_{vid_id}")]
            )
        await q.edit_message_text("ویدیو:", reply_markup=InlineKeyboardMarkup(buttons))
        return DELETE_VIDEO_SELECT
    if q.data.startswith("delvid_id_"):
        video_id = int(q.data.split("_")[2])
        video = get_video_by_id(video_id)
        if not video:
            await q.edit_message_text("پیدا نشد.")
            return ConversationHandler.END
        context.user_data["delete_video_id"] = video_id
        buttons = [[
            InlineKeyboardButton("✅ حذف", callback_data="delvid_confirm_yes"),
            InlineKeyboardButton("❌ لغو", callback_data="delvid_confirm_no"),
        ]]
        await q.edit_message_text(
            f"حذف «{safe_text(video[1])}»؟",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode=ParseMode.HTML,
        )
        return DELETE_VIDEO_CONFIRM
    return DELETE_VIDEO_SELECT


async def admin_delete_video_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "delvid_confirm_no":
        await q.edit_message_text("لغو شد.")
        await context.bot.send_message(q.message.chat_id, "پنل:", reply_markup=admin_keyboard())
        return ConversationHandler.END
    vid = context.user_data.get("delete_video_id")
    if vid and delete_video(vid):
        await q.edit_message_text("✅ حذف شد.")
    else:
        await q.edit_message_text("⚠️ نشد.")
    await context.bot.send_message(q.message.chat_id, "پنل:", reply_markup=admin_keyboard())
    return ConversationHandler.END


async def admin_edit_video_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    courses = get_all_courses()
    if not courses:
        await update.message.reply_text("درسی نیست.", reply_markup=admin_keyboard())
        return ConversationHandler.END
    buttons = [[InlineKeyboardButton(n, callback_data=f"editvid_course_{i}")] for i, n, _ in courses]
    await update.message.reply_text(
        "درس را انتخاب کنید:\nلغو: /cancel",
        reply_markup=ReplyKeyboardRemove(),
    )
    await update.message.reply_text("دروس:", reply_markup=InlineKeyboardMarkup(buttons))
    return EDIT_VIDEO_SELECT


async def admin_edit_video_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data.startswith("editvid_course_"):
        course_id = int(q.data.split("_")[2])
        videos = get_videos_by_course(course_id)
        if not videos:
            await q.edit_message_text("ویدیویی نیست.")
            return ConversationHandler.END
        buttons = []
        for vid_id, title, episode, *_ in videos:
            label = title if (title or "").startswith("قسمت") else (f"قسمت {episode} — {title}" if episode else (title or "—"))
            buttons.append(
                [InlineKeyboardButton(f"✏️ {label}"[:50], callback_data=f"editvid_id_{vid_id}")]
            )
        await q.edit_message_text("ویدیو:", reply_markup=InlineKeyboardMarkup(buttons))
        return EDIT_VIDEO_SELECT
    if q.data.startswith("editvid_id_"):
        video_id = int(q.data.split("_")[2])
        video = get_video_by_id(video_id)
        if not video:
            await q.edit_message_text("پیدا نشد.")
            return ConversationHandler.END
        context.user_data["edit_video_id"] = video_id
        buttons = [
            [InlineKeyboardButton("📝 عنوان", callback_data="editvid_field_title")],
            [InlineKeyboardButton("📌 قسمت", callback_data="editvid_field_episode")],
            [InlineKeyboardButton("🔗 لینک نماشا", callback_data="editvid_field_url")],
            [InlineKeyboardButton("⬇️ لینک دانلود", callback_data="editvid_field_dl")],
            [InlineKeyboardButton("📤 فایل تلگرام (بفرستید)", callback_data="editvid_field_tg")],
            [InlineKeyboardButton("❌ انصراف", callback_data="editvid_field_cancel")],
        ]
        await q.edit_message_text(
            f"ویرایش: <b>{safe_text(video[1])}</b>\nچه چیزی؟",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode=ParseMode.HTML,
        )
        return EDIT_VIDEO_FIELD
    return EDIT_VIDEO_SELECT


async def admin_edit_video_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "editvid_field_cancel":
        await q.edit_message_text("لغو شد.")
        await context.bot.send_message(q.message.chat_id, "پنل:", reply_markup=admin_keyboard())
        return ConversationHandler.END
    field = q.data.replace("editvid_field_", "")
    context.user_data["edit_video_field"] = field
    prompts = {
        "title": "عنوان جدید:",
        "episode": "شماره قسمت:",
        "url": "لینک نماشا:",
        "dl": "لینک دانلود (یا ندارد):",
        "tg": "فایل ویدیو را بفرستید (یا بنویسید حذف برای پاک کردن فایل):",
    }
    await q.edit_message_text(prompts.get(field, "مقدار:"))
    return EDIT_VIDEO_VALUE


async def admin_edit_video_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    video_id = context.user_data.get("edit_video_id")
    field = context.user_data.get("edit_video_field")
    if not video_id or not field:
        await update.message.reply_text("خطا.", reply_markup=admin_keyboard())
        return ConversationHandler.END

    # برای فیلدهای متنی، فایل/عکس قبول نیست
    if field != "tg" and not (update.message and update.message.text):
        await update.message.reply_text(
            "لطفاً فقط پیام متنی بفرستید (نه عکس یا فایل)."
        )
        return EDIT_VIDEO_VALUE

    if field == "tg":
        if update.message.text:
            t = update.message.text.strip()
            if t in ("🏠 منوی اصلی", "/cancel"):
                return await cancel(update, context)
            if t in ("حذف", "پاک", "delete"):
                update_video(video_id, telegram_file_id="")
                await update.message.reply_text("✅ فایل تلگرام حذف شد.", reply_markup=admin_keyboard())
                return ConversationHandler.END
            await update.message.reply_text("ویدیو بفرستید یا «حذف».")
            return EDIT_VIDEO_VALUE
        tg_id = None
        if update.message.video:
            tg_id = update.message.video.file_id
        elif update.message.document:
            tg_id = update.message.document.file_id
        if not tg_id:
            await update.message.reply_text("فایل معتبر نیست.")
            return EDIT_VIDEO_VALUE
        update_video(video_id, telegram_file_id=tg_id)
        await update.message.reply_text("✅ فایل تلگرام ذخیره شد.", reply_markup=admin_keyboard())
        return ConversationHandler.END

    value = normalize_digits(update.message.text.strip())
    if value in ("🏠 منوی اصلی", "/cancel"):
        return await cancel(update, context)
    kwargs = {}
    if field == "title":
        kwargs["title"] = value
    elif field == "episode":
        kwargs["episode"] = value
    elif field == "url":
        if not is_valid_namasha_url(value):
            await update.message.reply_text("لینک نماشا نامعتبر.")
            return EDIT_VIDEO_VALUE
        kwargs["namasha_url"] = value
    elif field == "dl":
        if not is_valid_download_url(value):
            await update.message.reply_text("لینک نامعتبر.")
            return EDIT_VIDEO_VALUE
        kwargs["download_url"] = value
    if update_video(video_id, **kwargs):
        await update.message.reply_text("✅ ذخیره شد.", reply_markup=admin_keyboard())
    else:
        await update.message.reply_text("⚠️ نشد.", reply_markup=admin_keyboard())
    return ConversationHandler.END


# ---------- admin: materials ----------
async def admin_add_mat_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    courses = get_all_courses()
    if not courses:
        await update.message.reply_text("اول درس اضافه کنید.", reply_markup=admin_keyboard())
        return ConversationHandler.END
    buttons = [[InlineKeyboardButton(n, callback_data=f"mat_course_{i}")] for i, n, _ in courses]
    await update.message.reply_text(
        "درس را انتخاب کنید:\nلغو: /cancel یا «🏠 منوی اصلی»",
        reply_markup=ReplyKeyboardRemove(),
    )
    await update.message.reply_text("دروس:", reply_markup=InlineKeyboardMarkup(buttons))
    return MAT_COURSE


async def admin_mat_course(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    course_id = int(q.data.split("_")[2])
    context.user_data["mat_course_id"] = course_id
    buttons = [
        [InlineKeyboardButton("📄 جزوات و کتاب‌ها", callback_data="mat_sec_materials")],
        [InlineKeyboardButton("📝 نمونه سوالات و امتحانات", callback_data="mat_sec_exams")],
    ]
    await q.edit_message_text("نوع محتوا:", reply_markup=InlineKeyboardMarkup(buttons))
    return MAT_SECTION


async def admin_mat_section(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    section = q.data.replace("mat_sec_", "")
    context.user_data["mat_section"] = section
    course_id = context.user_data["mat_course_id"]
    cats = get_material_categories(course_id, section)
    buttons = [
        [
            InlineKeyboardButton(
                f"📁 {c}",
                callback_data=pack_category_callback("amat", course_id, section, c),
            )
        ]
        for c in cats
    ]
    buttons.append([InlineKeyboardButton("➕ دسته جدید", callback_data="mat_cat_new")])
    await q.edit_message_text("دسته را انتخاب یا بسازید:", reply_markup=InlineKeyboardMarkup(buttons))
    return MAT_CATEGORY


async def admin_mat_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "mat_cat_new":
        await q.edit_message_text(
            "نام دسته جدید را بنویسید:\n"
            "مثال: کتاب مرجع / جزوه تایپی / میان‌ترم ۱۴۰۲"
        )
        return MAT_CAT_NAME
    _cid, _sec, category = resolve_category_from_callback(q.data, "amat")
    if not category:
        await q.edit_message_text("دسته نامعتبر. دوباره تلاش کنید.")
        return ConversationHandler.END
    context.user_data["mat_category"] = category
    await q.edit_message_text(
        f"دسته: <b>{safe_text(category)}</b>\n\n"
        "عنوان این فایل را بنویسید (اختیاری — می‌توانید «-» بفرستید):",
        parse_mode=ParseMode.HTML,
    )
    return MAT_TITLE


async def admin_mat_cat_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = normalize_digits(update.message.text.strip())
    if name in ("🏠 منوی اصلی", "/cancel"):
        return await cancel(update, context)
    if len(name) < 1:
        await update.message.reply_text("نام دسته را بنویسید:")
        return MAT_CAT_NAME
    context.user_data["mat_category"] = name
    await update.message.reply_text(
        f"دسته «{name}»\n\nعنوان این فایل (اختیاری — یا «-»):"
    )
    return MAT_TITLE


async def admin_mat_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t in ("🏠 منوی اصلی", "/cancel"):
        return await cancel(update, context)
    context.user_data["mat_title"] = "" if t in ("-", "ندارد") else normalize_digits(t)
    context.user_data["mat_count"] = 0
    await update.message.reply_text(
        "حالا فایل‌ها را بفرستید (PDF یا عکس).\n"
        "می‌توانید چند فایل پشت‌سرهم بفرستید.\n"
        "کپشن هر فایل (اگر داشته باشد) ذخیره می‌شود.\n\n"
        "وقتی تمام شد بنویسید: تمام"
    )
    return MAT_FILES


async def admin_mat_files(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text:
        t = update.message.text.strip()
        if t in ("🏠 منوی اصلی", "/cancel"):
            return await cancel(update, context)
        if t in ("تمام", "تمام شد", "done", "پایان"):
            n = context.user_data.get("mat_count", 0)
            await update.message.reply_text(
                f"✅ ثبت شد. تعداد فایل: {n}",
                reply_markup=admin_keyboard(),
            )
            for k in list(context.user_data.keys()):
                if k.startswith("mat_"):
                    context.user_data.pop(k, None)
            return ConversationHandler.END
        await update.message.reply_text("فایل بفرستید یا بنویسید: تمام")
        return MAT_FILES

    file_id = None
    file_type = None
    caption = update.message.caption

    if update.message.document:
        file_id = update.message.document.file_id
        file_type = "document"
    elif update.message.photo:
        file_id = update.message.photo[-1].file_id
        file_type = "photo"
    else:
        await update.message.reply_text("فقط PDF/فایل یا عکس. یا «تمام».")
        return MAT_FILES

    add_material(
        context.user_data["mat_course_id"],
        context.user_data["mat_section"],
        context.user_data["mat_category"],
        context.user_data.get("mat_title") or "",
        file_id,
        file_type,
        caption,
    )
    context.user_data["mat_count"] = context.user_data.get("mat_count", 0) + 1
    await update.message.reply_text(
        f"✔️ فایل {context.user_data['mat_count']} ذخیره شد.\n"
        "فایل بعدی یا «تمام»."
    )
    return MAT_FILES


async def admin_del_mat_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    courses = get_all_courses()
    if not courses:
        await update.message.reply_text("درسی نیست.", reply_markup=admin_keyboard())
        return ConversationHandler.END
    buttons = [[InlineKeyboardButton(n, callback_data=f"dmat_c_{i}")] for i, n, _ in courses]
    await update.message.reply_text(
        "درس را انتخاب کنید:\nلغو: /cancel",
        reply_markup=ReplyKeyboardRemove(),
    )
    await update.message.reply_text("دروس:", reply_markup=InlineKeyboardMarkup(buttons))
    return DEL_MAT_COURSE


async def admin_del_mat_course(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    course_id = int(q.data.split("_")[2])
    context.user_data["dmat_course"] = course_id
    buttons = [
        [InlineKeyboardButton("📄 جزوات و کتاب‌ها", callback_data="dmat_s_materials")],
        [InlineKeyboardButton("📝 نمونه سوالات", callback_data="dmat_s_exams")],
    ]
    await q.edit_message_text("بخش:", reply_markup=InlineKeyboardMarkup(buttons))
    return DEL_MAT_SECTION


async def _show_del_mat_page(update, context, course_id, section, page=0, edit=True):
    rows = list_materials_for_course(course_id, section)
    if not rows:
        if edit and update.callback_query:
            await update.callback_query.edit_message_text("موردی نیست.")
            await context.bot.send_message(
                update.callback_query.message.chat_id, "پنل:", reply_markup=admin_keyboard()
            )
        return ConversationHandler.END
    items = []
    for mid, cat, title, ftype in rows:
        label = f"{cat} — {title or ftype}"
        items.append(
            [InlineKeyboardButton(f"🗑 {label}"[:50], callback_data=f"dmat_i_{mid}")]
        )
    markup, total, start, end = paginate_buttons(
        items, page, PAGE_SIZE, f"dmatp_{course_id}_{section[:1]}"
    )
    # note: paginate prefix pattern handled below
    text = f"برای حذف (نمایش {start+1} تا {min(end, total)} از {total}):"
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=markup)
    return DEL_MAT_ITEM


async def admin_del_mat_section(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    section = q.data.replace("dmat_s_", "")
    course_id = context.user_data["dmat_course"]
    context.user_data["dmat_section"] = section
    return await _show_del_mat_page(update, context, course_id, section, page=0)


async def admin_del_mat_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """صفحه‌بندی لیست حذف جزوه: dmatp_{course_id}_{s}_page_{n}"""
    q = update.callback_query
    await q.answer()
    # dmatp_12_m_page_0
    body = q.data[len("dmatp_"):]
    parts = body.rsplit("_page_", 1)
    if len(parts) != 2:
        return DEL_MAT_ITEM
    head, page_s = parts
    page = int(page_s)
    # head = {course_id}_{s}
    cid_s, sec_code = head.rsplit("_", 1)
    course_id = int(cid_s)
    section = CODE_TO_SECTION.get(sec_code, context.user_data.get("dmat_section", SECTION_MATERIALS))
    context.user_data["dmat_course"] = course_id
    context.user_data["dmat_section"] = section
    return await _show_del_mat_page(update, context, course_id, section, page=page)


async def admin_del_mat_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    mid = int(q.data.split("_")[2])
    context.user_data["dmat_id"] = mid
    mat = get_material_by_id(mid)
    label = mat[1] or mat[7] if mat else mid
    buttons = [[
        InlineKeyboardButton("✅ حذف", callback_data="dmat_yes"),
        InlineKeyboardButton("❌ لغو", callback_data="dmat_no"),
    ]]
    await q.edit_message_text(
        f"حذف «{safe_text(str(label))}»؟",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode=ParseMode.HTML,
    )
    return DEL_MAT_CONFIRM


async def admin_del_mat_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "dmat_no":
        await q.edit_message_text("لغو شد.")
        await context.bot.send_message(q.message.chat_id, "پنل:", reply_markup=admin_keyboard())
        return ConversationHandler.END
    mid = context.user_data.get("dmat_id")
    if mid and delete_material(mid):
        await q.edit_message_text("✅ حذف شد.")
    else:
        await q.edit_message_text("⚠️ نشد.")
    await context.bot.send_message(q.message.chat_id, "پنل:", reply_markup=admin_keyboard())
    return ConversationHandler.END


async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = update.effective_user.id
    if context.user_data.get("waiting_search"):
        if await handle_search(update, context):
            return
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
            "از منو استفاده کنید.",
            reply_markup=main_keyboard(is_admin(user_id)),
        )


def _fallbacks():
    return [
        CommandHandler("cancel", cancel),
        MessageHandler(filters.Regex(MENU_BUTTON_PATTERN), cancel_and_route),
    ]


class _HealthCheckHandler(BaseHTTPRequestHandler):
    """هندلر خیلی ساده فقط برای جواب دادن به health check سرویس‌هایی مثل Render/UptimeRobot."""

    def do_GET(self):
        body = b"Bot is running."
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        # UptimeRobot اغلب با HEAD چک می‌کند؛ بدون این متد خطای 501 می‌دهد
        body = b"Bot is running."
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()

    def log_message(self, format, *args):
        # جلوگیری از شلوغ شدن لاگ‌ها با درخواست‌های health check
        pass


def _start_fake_webserver():
    """
    بعضی سرویس‌های هاست (مثل Render در حالت Web Service) انتظار دارن
    برنامه روی یک پورت گوش بده، وگرنه سرویس رو ناسالم/تایم‌اوت در نظر می‌گیرن.
    این ربات فقط polling می‌کنه و پورتی باز نمی‌کنه، پس یک سرور HTTP خیلی
    سبک و ساختگی توی یک ترد جدا بالا می‌آوریم که فقط به health check جواب بده.
    اگر متغیر محیطی PORT ست نشده باشه (مثلاً روی Railway یا اجرای لوکال)، این
    سرور اصلاً راه‌اندازی نمی‌شود.
    """
    port_str = os.getenv("PORT")
    if not port_str:
        return
    try:
        port = int(port_str)
    except ValueError:
        print(f"⚠️ مقدار PORT نامعتبر است: {port_str}")
        return

    def _run():
        server = HTTPServer(("0.0.0.0", port), _HealthCheckHandler)
        server.serve_forever()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    print(f"🌐 وب‌سرور سبک برای health check روی پورت {port} راه‌اندازی شد.")



def parse_course_and_episode_from_caption(caption: str):
    """از کپشن کانال: (نام_درس، شماره_قسمت، عنوان_کوتاه)."""
    text = clean_caption_for_match(caption)
    ep = _extract_episode_num(text)
    if not ep:
        return None, None, None
    # حذف قسمت از انتها
    course = re.sub(
        r"\s*[-–—]?\s*(?:قسمت|جلسه)\s*[0-9۰-۹]+(?:\s*[-–./]\s*[0-9۰-۹]+)?\s*$",
        "",
        text,
    ).strip(" -–—|")
    course = re.sub(r"\s+", " ", course).strip()
    # عنوان کوتاه مثل Excel تمیز
    if re.match(r"^\d+[-–./]\d+$", ep.replace(" ", "")):
        main, part = re.split(r"[-–./]", ep)
        ordinals = {
            "1": "اول", "2": "دوم", "3": "سوم", "4": "چهارم", "5": "پنجم",
            "6": "ششم", "7": "هفتم", "8": "هشتم", "9": "نهم", "10": "دهم",
        }
        part_fa = ordinals.get(part, part)
        title = f"قسمت {main} (بخش {part_fa})"
    else:
        title = f"قسمت {ep}"
    return course or None, ep, title


def resolve_or_create_course(course_name: str) -> tuple:
    """برمی‌گرداند (course_id, course_name_final)."""
    text = normalize_digits(course_name.replace("‌", " "))
    text = re.sub(r"\s+", " ", text).strip()
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT id, name FROM courses")
        courses = c.fetchall()
    best = None
    best_score = 0.0
    for cid, name in courses:
        n = normalize_digits((name or "").replace("‌", " "))
        n = re.sub(r"\s+", " ", n).strip()
        if not n:
            continue
        if n == text or n in text:
            score = 1000 + len(n)
        elif text in n and len(text) >= max(8, int(len(n) * 0.8)):
            # فقط اگر متن کپشن تقریباً کل نام درس را پوشش دهد
            score = 500 + len(text)
        else:
            STOP = {"دکتر", "درس", "مبانی", "با", "در", "و", "از", "به", "برای", "های", "ها"}
            words = [w for w in re.split(r"[\s\-_:/]+", n) if len(w) >= 2 and w not in STOP]
            if not words:
                continue
            hit = [w for w in words if w in text]
            ratio = len(hit) / len(words) if words else 0
            if ratio < 0.7:
                continue
            score = sum(len(w) for w in hit) * ratio
        if score > best_score:
            best_score = score
            best = (cid, name)
    if best and best_score >= 5:
        return best
    # ساخت درس جدید
    ok = add_course(text)
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT id, name FROM courses WHERE name = %s", (text,))
        row = c.fetchone()
    if row:
        return row
    # ممکن است normalize متفاوت باشد
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT id, name FROM courses WHERE name ILIKE %s ORDER BY id DESC LIMIT 1", (text,))
        row = c.fetchone()
    return row if row else (None, text)


def create_video_from_telegram_caption(caption: str, telegram_file_id: str):
    """
    وقتی در DB نبود: درس/قسمت از کپشن ساخته می‌شود.
    namasha_url با پیشوند telegram-only پر می‌شود (اجباری در اسکیما).
    """
    course_name, ep, title = parse_course_and_episode_from_caption(caption)
    if not course_name or not ep:
        return None
    resolved = resolve_or_create_course(course_name)
    if not resolved or not resolved[0]:
        return None
    course_id, final_name = resolved
    digest = hashlib.sha1((caption or "").encode("utf-8")).hexdigest()[:10]
    placeholder = f"telegram-only:{course_id}:{ep}:{digest}"
    ok = add_video(
        course_id=course_id,
        title=title,
        namasha_url=placeholder,
        episode=ep,
        download_url=None,
        telegram_file_id=telegram_file_id,
    )
    if not ok:
        return None
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            """
            SELECT v.id, v.title, v.episode, c.name, v.telegram_file_id
            FROM videos v JOIN courses c ON v.course_id = c.id
            WHERE v.namasha_url = %s
            """,
            (placeholder,),
        )
        return c.fetchone()



async def autolink_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """حالت اتصال فایل تلگرام به ویدیوهای موجود (فقط ادمین)."""
    if not is_admin(update.effective_user.id):
        return
    context.user_data["autolink"] = True
    await update.message.reply_text(
        "🔗 حالت اتصال فایل روشن شد.\n\n"
        "ویدیوها را از کانال آرشیو فوروارد کن.\n"
        "• اگر ردیف از قبل باشد → فقط فایل تلگرام وصل می‌شود.\n"
        "• اگر در نماشا/دیتابیس نباشد → از روی کپشن درس و قسمت ساخته می‌شود.\n\n"
        "برای خاموش کردن: /autolink_off",
        parse_mode=None,
    )


async def autolink_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    context.user_data["autolink"] = False
    await update.message.reply_text("🔗 حالت اتصال فایل خاموش شد.")


async def autolink_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """وقتی autolink روشن است، ویدیو/فایل فورواردشده را به ردیف موجود وصل می‌کند."""
    if not is_admin(update.effective_user.id):
        return
    if not context.user_data.get("autolink"):
        return
    msg = update.message
    if not msg:
        return

    tg_id = None
    if msg.video:
        tg_id = msg.video.file_id
    elif msg.document and (
        (msg.document.mime_type or "").startswith("video/")
        or (msg.document.file_name or "").lower().endswith((".mp4", ".mkv", ".webm"))
    ):
        tg_id = msg.document.file_id
    if not tg_id:
        return

    caption = msg.caption or msg.text or ""
    if not caption.strip():
        await msg.reply_text("⚠️ کپشن خالی است؛ رد شد.")
        return

    cleaned = clean_caption_for_match(caption)

    try:
        row = find_video_for_autolink(caption)
    except Exception as e:
        await msg.reply_text(f"⚠️ خطا در تطبیق: {type(e).__name__}: {e}")
        return

    created_new = False
    if not row:
        try:
            row = create_video_from_telegram_caption(caption, tg_id)
            created_new = bool(row)
        except Exception as e:
            await msg.reply_text(f"⚠️ خطا در ساخت ردیف جدید: {type(e).__name__}: {e}")
            return
        if not row:
            await msg.reply_text(
                f"❌ از کپشن درس/قسمت خوانده نشد:\n{cleaned[:120]}"
            )
            return

    # ممکن است ۶ ستون برگردد (با course_id)
    vid_id = row[0]
    title = row[1]
    episode = row[2]
    course_name = row[3]
    old_tg = row[4]

    if not created_new:
        try:
            update_video(vid_id, telegram_file_id=tg_id)
        except Exception as e:
            await msg.reply_text(f"⚠️ خطا در ذخیره: {type(e).__name__}: {e}")
            return

    if created_new:
        status = "اضافه و وصل شد (فقط تلگرام — در نماشا نبود)"
    elif old_tg:
        status = "جایگزین شد"
    else:
        status = "وصل شد"
    await msg.reply_text(
        f"✅ {status}\n"
        f"درس: {course_name}\n"
        f"عنوان: {title}\n"
        f"قسمت: {episode or '—'}"
    )


def main():
    if not BOT_TOKEN:
        print("❌ BOT_TOKEN تنظیم نشده.")
        return
    if not DATABASE_URL:
        print("❌ DATABASE_URL تنظیم نشده.")
        return
    init_db()
    print("✅ PostgreSQL آماده است.")

    _start_fake_webserver()

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

    fb = _fallbacks()

    add_course_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^➕ افزودن درس$"), admin_add_course_start)],
        states={ADD_COURSE_NAME: [MessageHandler(text_input_filter(), admin_add_course_name)]},
        fallbacks=fb,
        allow_reentry=True,
    )
    edit_course_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^✏️ ویرایش درس$"), admin_edit_course_start)],
        states={
            EDIT_COURSE_SELECT: [CallbackQueryHandler(admin_edit_course_select, pattern=r"^editcourse_")],
            EDIT_COURSE_NAME: [MessageHandler(text_input_filter(), admin_edit_course_name)],
        },
        fallbacks=fb,
        allow_reentry=True,
    )
    delete_course_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^🗑 حذف درس$"), admin_delete_course_start)],
        states={
            DELETE_COURSE_SELECT: [CallbackQueryHandler(admin_delete_course_select, pattern=r"^delcourse_\d+$")],
            DELETE_COURSE_CONFIRM: [CallbackQueryHandler(admin_delete_course_confirm, pattern=r"^delcourse_(yes|no)$")],
        },
        fallbacks=fb,
        allow_reentry=True,
    )
    add_video_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^🎬 افزودن ویدیو$"), admin_add_video_start)],
        states={
            ADD_VIDEO_COURSE: [CallbackQueryHandler(admin_add_video_course, pattern=r"^addvid_course_")],
            ADD_VIDEO_TITLE: [MessageHandler(text_input_filter(), admin_add_video_title)],
            ADD_VIDEO_URL: [MessageHandler(text_input_filter(), admin_add_video_url)],
            ADD_VIDEO_DL: [
                MessageHandler(text_input_filter(), admin_add_video_dl),
                MessageHandler(~filters.TEXT & ~filters.COMMAND, admin_add_video_dl),
            ],
            ADD_VIDEO_TG: [
                MessageHandler(filters.VIDEO | filters.Document.ALL, admin_add_video_tg),
                MessageHandler(text_input_filter(), admin_add_video_tg),
            ],
        },
        fallbacks=fb,
        allow_reentry=True,
    )
    delete_video_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^🗑 حذف ویدیو$"), admin_delete_video_start)],
        states={
            DELETE_VIDEO_SELECT: [CallbackQueryHandler(admin_delete_video_course, pattern=r"^delvid_")],
            DELETE_VIDEO_CONFIRM: [CallbackQueryHandler(admin_delete_video_confirm, pattern=r"^delvid_confirm_")],
        },
        fallbacks=fb,
        allow_reentry=True,
    )
    edit_video_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^✏️ ویرایش ویدیو$"), admin_edit_video_start)],
        states={
            EDIT_VIDEO_SELECT: [CallbackQueryHandler(admin_edit_video_select, pattern=r"^editvid_")],
            EDIT_VIDEO_FIELD: [CallbackQueryHandler(admin_edit_video_field, pattern=r"^editvid_field_")],
            EDIT_VIDEO_VALUE: [
                MessageHandler(filters.VIDEO | filters.Document.ALL, admin_edit_video_value),
                MessageHandler(text_input_filter(), admin_edit_video_value),
            ],
        },
        fallbacks=fb,
        allow_reentry=True,
    )
    add_mat_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^📎 افزودن جزوه/سوال$"), admin_add_mat_start)],
        states={
            MAT_COURSE: [CallbackQueryHandler(admin_mat_course, pattern=r"^mat_course_")],
            MAT_SECTION: [CallbackQueryHandler(admin_mat_section, pattern=r"^mat_sec_")],
            MAT_CATEGORY: [
                CallbackQueryHandler(admin_mat_category, pattern=r"^(mat_cat_new|amat_)"),
            ],
            MAT_CAT_NAME: [MessageHandler(text_input_filter(), admin_mat_cat_name)],
            MAT_TITLE: [MessageHandler(text_input_filter(), admin_mat_title)],
            MAT_FILES: [
                MessageHandler(filters.PHOTO | filters.Document.ALL, admin_mat_files),
                MessageHandler(text_input_filter(), admin_mat_files),
            ],
        },
        fallbacks=fb,
        allow_reentry=True,
    )
    del_mat_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^🗑 حذف جزوه/سوال$"), admin_del_mat_start)],
        states={
            DEL_MAT_COURSE: [CallbackQueryHandler(admin_del_mat_course, pattern=r"^dmat_c_")],
            DEL_MAT_SECTION: [CallbackQueryHandler(admin_del_mat_section, pattern=r"^dmat_s_")],
            DEL_MAT_ITEM: [
                CallbackQueryHandler(admin_del_mat_item, pattern=r"^dmat_i_"),
                CallbackQueryHandler(admin_del_mat_page, pattern=r"^dmatp_"),
            ],
            DEL_MAT_CONFIRM: [CallbackQueryHandler(admin_del_mat_confirm, pattern=r"^dmat_(yes|no)$")],
        },
        fallbacks=fb,
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("autolink", autolink_on))
    app.add_handler(CommandHandler("autolink_off", autolink_off))
    app.add_handler(add_course_conv)
    app.add_handler(edit_course_conv)
    app.add_handler(delete_course_conv)
    app.add_handler(add_video_conv)
    app.add_handler(delete_video_conv)
    app.add_handler(edit_video_conv)
    app.add_handler(add_mat_conv)
    app.add_handler(del_mat_conv)
    # بعد از ConversationHandlerها در group=0 تا وسط ویرایش، autolink نگیرد
    app.add_handler(
        MessageHandler(
            (filters.VIDEO | filters.Document.ALL) & filters.User(list(ADMIN_IDS) or [0]),
            autolink_media,
        )
    )
    app.add_handler(CallbackQueryHandler(course_selected, pattern=r"^course_\d+$"))
    app.add_handler(CallbackQueryHandler(courses_page_callback, pattern=r"^courses_page_\d+$"))
    app.add_handler(CallbackQueryHandler(hub_videos, pattern=r"^hub_vid_\d+$"))
    app.add_handler(CallbackQueryHandler(hub_materials, pattern=r"^hub_mat_\d+$"))
    app.add_handler(CallbackQueryHandler(hub_exams, pattern=r"^hub_exam_\d+$"))
    app.add_handler(CallbackQueryHandler(course_videos_page_callback, pattern=r"^cv_\d+_page_\d+$"))
    app.add_handler(CallbackQueryHandler(video_selected, pattern=r"^video_\d+$"))
    app.add_handler(CallbackQueryHandler(send_video_file, pattern=r"^sendvid_\d+$"))
    app.add_handler(CallbackQueryHandler(material_category_selected, pattern=r"^mcat_"))
    app.add_handler(CallbackQueryHandler(send_material_file, pattern=r"^mfile_\d+$"))
    app.add_handler(CallbackQueryHandler(back_to_courses, pattern=r"^back_courses$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
    app.add_error_handler(error_handler)

    print("🤖 ربات آرشیو کامل در حال اجرا...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
