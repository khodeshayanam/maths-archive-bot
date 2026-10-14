#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
پاک کردن کامل جداول آرشیو (دروس، ویدیوها، جزوات).
فقط با --yes اجرا می‌شود.

  python tools/reset_database.py --yes
"""

from __future__ import annotations

import argparse
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


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--yes", action="store_true", help="تأیید حذف کامل")
    args = p.parse_args()
    if not args.yes:
        print("برای پاک کردن کامل دیتابیس بنویس:")
        print("  python tools/reset_database.py --yes")
        return 1

    db = os.getenv("DATABASE_URL")
    if not db:
        print("❌ DATABASE_URL نیست.")
        return 1

    with psycopg.connect(db) as conn:
        with conn.cursor() as c:
            c.execute(
                """
                TRUNCATE TABLE
                    materials,
                    videos,
                    courses
                RESTART IDENTITY CASCADE
                """
            )
        conn.commit()

    print("✅ دیتابیس خالی شد (courses / videos / materials).")
    print("قدم بعد: دوباره Excel را import کن، بعد /autolink و فوروارد.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
