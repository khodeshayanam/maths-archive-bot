#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
اتصال file_id تلگرام به ویدیوهای *موجود* در دیتابیس ربات.

مهم:
  file_id اکانت شخصی با file_id ربات یکی نیست.
  این اسکریپت ویدیوها را از کانال خصوصی به ربات *فوروارد* می‌کند؛
  ربات در حالت /autolink آن‌ها را به ردیف موجود وصل می‌کند.

پیش‌نیاز:
  1) در ربات (به‌عنوان ادمین): /autolink
  2) pip install telethon python-dotenv psycopg[binary] openpyxl
  3) در .env:
       TELEGRAM_API_ID=...
       TELEGRAM_API_HASH=...
       CHANNEL_INVITE=https://t.me/+xxxx  یا  https://t.me/joinchat/xxxx
       DATABASE_URL=...   (فقط برای گزارش تطبیق؛ آپدیت را ربات انجام می‌دهد)
       BOT_USERNAME=YourBot   (بدون @)

اجرا:
  python tools/sync_channel_file_ids.py --dry-run
  python tools/sync_channel_file_ids.py --limit 20
  python tools/sync_channel_file_ids.py
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    load_dotenv()
except ImportError:
    pass

from telethon import TelegramClient
from telethon.tl.functions.messages import CheckChatInviteRequest, ImportChatInviteRequest
from telethon.tl.types import MessageMediaDocument, MessageMediaPhoto

EXCLUDE = ("داده کاوی دکتر دهقان", "داده کاوی")
PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")


def clean_caption(caption: str) -> str:
    text = caption or ""
    text = re.sub(r"#\S+", " ", text)
    text = text.translate(PERSIAN_DIGITS)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def invite_hash(link: str) -> str:
    link = (link or "").strip()
    if "joinchat/" in link:
        return link.split("joinchat/")[-1].split("?")[0].strip("/")
    if "/+" in link:
        return link.split("/+")[-1].split("?")[0].strip("/")
    if link.startswith("+"):
        return link[1:]
    return link


async def resolve_channel(client: TelegramClient, link: str):
    link = (link or "").strip()

    # لینک عمومی مثل https://t.me/ChannelName یا @ChannelName
    if "t.me/+" not in link and "joinchat/" not in link:
        username = link
        if "t.me/" in username:
            username = username.split("t.me/")[-1].split("?")[0].strip("/")
        if username.startswith("@"):
            username = username[1:]
        if username and not username.startswith("+"):
            try:
                return await client.get_entity(username)
            except Exception as e:
                print(f"⚠️ با یوزرنیم پیدا نشد ({e})؛ لینک دعوت را امتحان می‌کنم...")

    # لینک دعوت خصوصی
    h = invite_hash(link)
    try:
        updates = await client(ImportChatInviteRequest(h))
        if getattr(updates, "chats", None):
            return updates.chats[0]
    except Exception as e:
        err = str(e).lower()
        if "already" in err or "user already participant" in err:
            pass
        else:
            try:
                info = await client(CheckChatInviteRequest(h))
                if hasattr(info, "chat"):
                    return info.chat
            except Exception:
                pass

    # جستجو در دیالوگ‌های باز
    async for dialog in client.iter_dialogs():
        if dialog.is_channel:
            ent = dialog.entity
            uname = getattr(ent, "username", None) or ""
            title = getattr(ent, "title", "") or ""
            if "Math_faculty" in uname or "آرشیو" in title or "Archive" in title:
                return ent

    raise RuntimeError(
        "کانال پیدا نشد. عضو کانال باش و CHANNEL_INVITE را درست بگذار "
        "(یوزرنیم یا لینک دعوت)."
    )


async def main_async(args) -> int:
    api_id = int(os.getenv("TELEGRAM_API_ID") or "0")
    api_hash = os.getenv("TELEGRAM_API_HASH") or ""
    invite = os.getenv("CHANNEL_INVITE") or ""
    bot_username = (os.getenv("BOT_USERNAME") or "").lstrip("@")

    if not api_id or not api_hash:
        print("❌ TELEGRAM_API_ID / TELEGRAM_API_HASH در .env نیست.")
        return 1
    if not invite:
        print("❌ CHANNEL_INVITE در .env نیست (لینک دعوت کانال).")
        return 1
    if not bot_username and not args.dry_run:
        print("❌ BOT_USERNAME در .env نیست (مثلاً MyArchiveBot).")
        return 1

    session_path = Path(__file__).resolve().parent / "user_session"
    client = TelegramClient(str(session_path), api_id, api_hash)
    await client.start()
    print("✅ وارد تلگرام شدی.")

    entity = await resolve_channel(client, invite)
    title = getattr(entity, "title", entity)
    print(f"کانال: {title}")

    # فقط آمار و فوروارد؛ تطبیق نهایی در ربات است
    count = 0
    forwarded = 0
    skipped_exclude = 0
    skipped_no_caption = 0

    async for msg in client.iter_messages(entity, limit=args.limit):
        if not msg.media:
            continue
        if not (msg.video or (msg.document and getattr(msg.document, "mime_type", "") or "").startswith("video")):
            # بعضی آرشیوها به‌صورت document ویدیو می‌گذارند
            if not msg.document:
                continue
            mime = getattr(msg.document, "mime_type", "") or ""
            name = ""
            for a in getattr(msg.document, "attributes", []) or []:
                if getattr(a, "file_name", None):
                    name = a.file_name
            if not (mime.startswith("video/") or name.lower().endswith((".mp4", ".mkv", ".webm"))):
                continue

        cap = clean_caption(msg.message or "")
        if not cap:
            skipped_no_caption += 1
            continue
        if any(b in cap.replace("‌", " ") for b in EXCLUDE):
            skipped_exclude += 1
            continue

        count += 1
        print(f"  [{count}] {cap[:80]}")

        if args.dry_run:
            continue

        try:
            await client.forward_messages(bot_username, msg)
            forwarded += 1
            await asyncio.sleep(args.delay)
        except Exception as e:
            print(f"    ⚠️ فوروارد نشد: {e}")

    print("\n--- خلاصه ---")
    print(f"ویدیو با کپشن (غیر داده کاوی): {count}")
    print(f"رد شده (داده کاوی): {skipped_exclude}")
    print(f"بدون کپشن: {skipped_no_caption}")
    if args.dry_run:
        print("dry-run: چیزی فوروارد نشد.")
    else:
        print(f"فوروارد به ربات: {forwarded}")
        print("در ربات باید /autolink روشن باشد تا file_id ذخیره شود.")

    await client.disconnect()
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true", help="فقط لیست؛ فوروارد نکن")
    p.add_argument("--limit", type=int, default=None, help="حداکثر تعداد پیام کانال")
    p.add_argument("--delay", type=float, default=1.2, help="ثانیه بین هر فوروارد")
    args = p.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
