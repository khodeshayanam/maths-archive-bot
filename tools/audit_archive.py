#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""گزارش وضعیت آرشیو در دیتابیس در مقابل لیست واقعی کانال."""

from __future__ import annotations

import os
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    load_dotenv()
except ImportError:
    pass

import psycopg

# لیست درس‌های اعلام‌شده در کانال (نیمسال ۴۰۴۲ و ...)
EXPECTED_COURSES = [
    "مبانی نظریه محاسبه",
    "اصول سیستم های کامپیوتری",
    "آنالیز عددی",
    "روش تحقیق و مشاوره آماری",
    "روش های نمونه‌گیری",
    "روش های نمونه گیری",
    "داده کاوی پیشرفته دکتر فراهانی",
    "آمار بیزی",
    "آنالیز ریاضی دکتر احمدی",
    "آنالیز ریاضی دکتر منصف شکری",
    "آنالیز ریاضی دکتر منصف‌شکری",
    "برنامه سازی پیشرفته دکتر خردپیشه",
    "بینایی کامپیوتر",
    "داده کاوی دکتر دهقان",
    "مبانی علوم داده",
    "احتمال ۲",
    "احتمال2",
    "جبرخطی دکتر خجسته",
    "جبر خطی دکتر خجسته",
    "مبانی آمار ناپارامتری",
    "مبانی هندسه دیفرانسیل",
    "مبانی جبر دکتر سالمکار",
    "مبانی جبر دکتر طالب",
    "مبانی علوم ریاضی دکتر طوسی",
    "مبانی علوم ریاضی دکتر منیری",
    "مبانی ترکیبیات دکتر طهماسبی",
    "مبانی یادگیری ماشین",
    "مدل های آماری۲",
    "مدل های آماری 2",
    "مدل های مرگ و میر",
    "مدیریت ریسک",
    "محاسبات آماری دکتر قریشی",
    "نظریه آمار۱",
    "نظریه آمار 1",
    "نظریه اندازه",
    "شبکه دکتر راجی",
    "پردازش تصویر",
    "برنامه نویسی با زبان R",
    "برنامه نویسی با زبانR",
    "روش های چند متغیره گسسته",
    "روش های چند متغیره پیوسته",
    "روش های محاسبه ذخایر خسارت",
    "روش های آماری چند متغیره",
    "رگرسیون۱",
    "رگرسیون 1",
    "ریاضی عمومی۲",
    "ریاضی عمومی 2",
    "ریاضیات بیمه های غیر زندگی",
    "ریاضیات مالی",
    "تحلیل های چند متغیره دکتر قنبرزاده",
    "اصول طراحی نرم افزار",
    "توابع مختلط",
    "برنامه نویسی همروند",
    "کنترل کیفیت",
    "احتمال۱",
    "احتمال 1",
    "رگرسیون۲",
    "رگرسیون 2",
    "طرح های آزمایش",
    "فرایند های تصادفی",
    "فرایندهای تصادفی",
]


def norm(s: str) -> str:
    s = (s or "").replace("‌", " ").replace("ي", "ی").replace("ك", "ک")
    s = " ".join(s.split())
    return s


def main() -> int:
    db = os.getenv("DATABASE_URL")
    if not db:
        print("❌ DATABASE_URL تنظیم نشده. در PowerShell:")
        print('  $env:DATABASE_URL="postgresql://..."')
        return 1

    with psycopg.connect(db) as conn:
        c = conn.cursor()
        c.execute(
            """
            SELECT c.id, c.name,
                   COUNT(v.id) AS total,
                   COUNT(v.telegram_file_id) FILTER (
                       WHERE v.telegram_file_id IS NOT NULL AND v.telegram_file_id <> ''
                   ) AS with_tg,
                   COUNT(v.id) FILTER (
                       WHERE v.telegram_file_id IS NULL OR v.telegram_file_id = ''
                   ) AS without_tg
            FROM courses c
            LEFT JOIN videos v ON v.course_id = c.id
            GROUP BY c.id, c.name
            ORDER BY c.name
            """
        )
        rows = c.fetchall()

        c.execute("SELECT COUNT(*) FROM videos")
        total_v = c.fetchone()[0]
        c.execute(
            """
            SELECT COUNT(*) FROM videos
            WHERE telegram_file_id IS NOT NULL AND telegram_file_id <> ''
            """
        )
        with_tg = c.fetchone()[0]

    print("=" * 60)
    print(f"تعداد درس در دیتابیس: {len(rows)}")
    print(f"کل ویدیو: {total_v}")
    print(f"دارای فایل تلگرام: {with_tg}")
    print(f"بدون فایل تلگرام: {total_v - with_tg}")
    print("=" * 60)
    print(f"{'درس':<45} {'کل':>5} {'تلگرام':>6} {'بدون':>5}")
    print("-" * 60)
    for _id, name, total, wt, wo in rows:
        flag = ""
        if total and wo == total:
            flag = "  ← هیچ فایل تلگرامی ندارد"
        elif total and wo:
            flag = "  ← ناقص"
        print(f"{name[:45]:<45} {total:>5} {wt:>6} {wo:>5}{flag}")

    # تطبیق تقریبی با لیست کانال
    db_names = [norm(r[1]) for r in rows]
    print("\n" + "=" * 60)
    print("درس‌های لیست کانال که در دیتابیس شبیهشان کم است:")
    print("=" * 60)
    missing_like = []
    for exp in EXPECTED_COURSES:
        en = norm(exp)
        found = any(en in d or d in en for d in db_names if d)
        if not found:
            # جلوگیری از تکرار نزدیک
            if not any(en in norm(m) or norm(m) in en for m in missing_like):
                missing_like.append(exp)

    if not missing_like:
        print("تقریباً همه نام‌های لیست کانال در دیتابیس شبیه دارند.")
    else:
        for m in missing_like:
            print(f"  ? {m}")

    print("\nتمام.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
