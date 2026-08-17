"""Full-history backfill via MTProto (Telethon).

USE THIS for large channels (1000+ posts). It reads the ENTIRE channel history
directly as YOUR user account and writes cover/pdf rows into the SAME Turso DB
the bot uses. Bot API limits don't apply.

Setup (one time):
  1) pip install telethon libsql==0.1.11
  2) Go to https://my.telegram.org → API development tools → get API_ID + API_HASH
  3) cp ../.env.example ../.env and fill TURSO_DATABASE_URL + TURSO_AUTH_TOKEN
     (or set DATABASE_PATH to the bot's local SQLite file)

Run:
  TG_API_ID=123456 TG_API_HASH=abcdef... python scripts/backfill_mtproto.py \
      --channel -1002298797194 --from-id 1 --to-id 4000

Notes:
  - Your phone login is required the FIRST time (code sent by Telegram).
  - Session is cached in ./backfill.session so reruns don't need the code.
  - Safe to re-run: existing (source_chat_id, source_message_id) rows are skipped.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone

# --- reuse the bot's DB layer if importable, else fall back to direct libsql ---
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from app import db as botdb          # type: ignore
    from app.services import repo as botrepo  # type: ignore
    HAVE_BOT = True
except Exception:
    HAVE_BOT = False

from telethon import TelegramClient


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def classify(msg) -> tuple[str, str, str | None, str | None, str | None]:
    """(kind, media_kind, file_id, file_name, caption)"""
    caption = msg.message or None
    d = getattr(msg, "document", None)
    if d is not None:
        name = (getattr(d, "attributes", None) and next(
            (a.file_name for a in d.attributes if getattr(a, "file_name", None)), None)) \
            or (getattr(d, "file_name", None))
        mime = getattr(d, "mime_type", "") or ""
        lname = (name or "").lower()
        if lname.endswith(".pdf") or "pdf" in mime.lower():
            return ("pdf", "document", None, name, caption)
        return ("cover", "document", None, name, caption)
    if getattr(msg, "photo", None) is not None:
        return ("cover", "photo", None, None, caption)
    if getattr(msg, "video", None) is not None:
        return ("cover", "video", None, None, caption)
    if getattr(msg, "audio", None) is not None:
        return ("cover", "audio", None, None, caption)
    if caption:
        return ("cover", "text", None, None, caption)
    return ("skip", "other", None, None, caption)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", required=True, help="DB channel id like -1002298797194")
    ap.add_argument("--from-id", type=int, default=1)
    ap.add_argument("--to-id", type=int, default=0, help="0 = latest")
    ap.add_argument("--session", default="backfill.session")
    ap.add_argument("--batch", type=int, default=100)
    args = ap.parse_args()

    api_id = int(os.environ.get("TG_API_ID", "0"))
    api_hash = os.environ.get("TG_API_HASH", "")
    if not api_id or not api_hash:
        print("ERROR: set TG_API_ID and TG_API_HASH (from https://my.telegram.org)")
        sys.exit(1)

    if not HAVE_BOT:
        print("ERROR: run from the project root so `app` is importable.")
        sys.exit(1)

    botdb.get_conn()  # init schema (idempotent)
    channel_id = int(args.channel)

    # Ensure channel is registered
    ch = botrepo.get_channel(channel_id)
    if not ch:
        botrepo.add_channel(channel_id, "database", title=None)
        print(f"registered {channel_id} as database channel")

    client = TelegramClient(args.session, api_id, api_hash)
    await client.start()  # will prompt for phone + code on first run

    entity = await client.get_entity(channel_id)
    print(f"Connected. Backfilling {entity.title} ({channel_id}) ids {args.from_id}..{args.to_id or 'latest'}")

    inserted_covers = inserted_pdfs = skipped = 0
    current_cover_msg_id: int | None = None
    seen_pdf_ids: set[int] = set()

    # Telethon iterates from newest to oldest; we want oldest->newest to
    # preserve cover->pdf grouping, so use reverse=True.
    async for msg in client.iter_messages(
        entity, min_id=args.from_id - 1,
        max_id=(args.to_id or 0), reverse=True):
        if msg is None or msg.id is None:
            continue
        mid = int(msg.id)
        if botrepo.post_exists(channel_id, mid):
            skipped += 1
            continue
        kind, media_kind, file_id, file_name, caption = classify(msg)
        if kind == "skip":
            skipped += 1
            continue
        raw = {"message_id": mid, "date": msg.date.isoformat() if msg.date else None}
        if kind == "cover":
            pid, _num, code = botrepo.insert_cover(
                source_chat_id=channel_id, source_message_id=mid,
                caption=caption, media_kind=media_kind,
                file_id=file_id, file_name=file_name, raw=raw)
            current_cover_msg_id = mid
            inserted_covers += 1
        else:
            # attach to most recent cover seen in this run; else to whatever
            # cover is already in the DB before this message
            parent = current_cover_msg_id
            if parent is None:
                pc = botrepo.find_cover_before(channel_id, mid)
                parent = pc["source_message_id"] if pc else None
            botrepo.insert_pdf(
                source_chat_id=channel_id, source_message_id=mid,
                parent_msg_id=parent, caption=caption, media_kind=media_kind,
                file_id=file_id, file_name=file_name, raw=raw)
            inserted_pdfs += 1

        if (inserted_covers + inserted_pdfs) % args.batch == 0:
            print(f"  ... +{inserted_covers} covers / +{inserted_pdfs} pdfs "
                  f"(skipped {skipped}), at msg {mid}")

    # After pass, set the channel cursor to the highest seen message-id so
    # the bot won't re-capture old posts from live channel_post updates.
    if args.to_id:
        botrepo.set_cursor(channel_id, args.to_id)
    print(f"\nDONE. covers={inserted_covers} pdfs={inserted_pdfs} skipped={skipped}")
    print(f"covers total={botrepo.total_covers()} pending={botrepo.queued_covers_count()}")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
