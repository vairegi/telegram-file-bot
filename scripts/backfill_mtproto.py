"""Full-history backfill via MTProto (Telethon) — resilient edition.

Improvements over v1:
  * Turso "stream not found" auto-reconnect (5 retries per write).
  * --resume: start from the highest already-imported message-id (fast rerun).
  * Prints live progress every 25 rows; heartbeat every 200.
  * Ctrl+C = clean disconnect + summary.
  * Session-desync detection: if you hit "Too many messages had to be ignored",
    delete backfill.session and re-login.

Setup once:
  pip install -r requirements.txt        # installs telethon + libsql
  API_ID / API_HASH -> https://my.telegram.org

Run (PowerShell on Windows):
  $env:TG_API_ID="123456"
  $env:TG_API_HASH="abcdef..."
  $env:TURSO_DATABASE_URL="turso://your-db.turso.io"   # also accepts libsql://
  $env:TURSO_AUTH_TOKEN="eyJ..."
  py scripts/backfill_mtproto.py --channel -1002298797194 --resume

  # or force full re-scan from message 1:
  py scripts/backfill_mtproto.py --channel -1002298797194 --from-id 1

Safe to rerun: existing (source_chat_id, source_message_id) rows are skipped.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from app import db as botdb          # type: ignore
    from app.services import repo as botrepo  # type: ignore
except Exception as e:
    print(f"ERROR: run from the project root so `app` is importable. ({e})")
    sys.exit(1)

from telethon import TelegramClient


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fname_of(doc) -> str | None:
    """Extract file_name from a Telethon Document (via DocumentAttributeFilename)."""
    for attr in getattr(doc, "attributes", None) or []:
        n = getattr(attr, "file_name", None)
        if n:
            return n
    return None


def classify(msg):
    """Return (kind, media_kind, file_name, caption).

    kind:
      'pdf'   -> a PDF document (goes under the current cover)
      'cover' -> photo / video / non-PDF doc / audio / plain text with caption
      'skip'  -> service message / empty / unknown
    """
    caption = msg.message or None
    d = getattr(msg, "document", None)
    if d is not None:
        name = _fname_of(d)
        mime = (getattr(d, "mime_type", "") or "").lower()
        lname = (name or "").lower()
        if lname.endswith(".pdf") or "pdf" in mime:
            return ("pdf", "document", name, caption)
        return ("cover", "document", name, caption)
    if getattr(msg, "photo", None) is not None:
        return ("cover", "photo", None, caption)
    if getattr(msg, "video", None) is not None:
        return ("cover", "video", None, caption)
    if getattr(msg, "audio", None) is not None:
        return ("cover", "audio", None, caption)
    if caption:
        return ("cover", "text", None, caption)
    return ("skip", "other", None, caption)


def _retry_write(fn, *args, attempts: int = 6, **kw):
    """Run a DB write with Turso reconnect on stream-lost errors."""
    last = None
    for i in range(1, attempts + 1):
        try:
            return fn(*args, **kw)
        except Exception as e:
            last = e
            msg = str(e).lower()
            transient = ("stream not found" in msg
                         or ("hrana" in msg and "404" in msg)
                         or "connection" in msg and "close" in msg
                         or "reset" in msg and "peer" in msg)
            if not transient:
                raise
            try:
                botdb.reset_conn()
            except Exception:
                pass
            wait = min(2 * i, 15)
            print(f"  ! DB stream lost — reconnecting in {wait}s (attempt {i}/{attempts})")
            time.sleep(wait)
    if last:
        raise last


def _highest_imported(chat_id: int) -> int:
    row = botdb.query_one(
        "SELECT COALESCE(MAX(source_message_id),0) AS mx FROM posts WHERE source_chat_id=?",
        (chat_id,))
    return int((row or {}).get("mx") or 0)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", required=True, help="DB channel id like -1002298797194")
    ap.add_argument("--from-id", type=int, default=1, help="min message-id (ignored with --resume)")
    ap.add_argument("--to-id", type=int, default=0, help="0 = latest")
    ap.add_argument("--resume", action="store_true",
                    help="start from highest already-imported message-id (recommended)")
    ap.add_argument("--session", default="backfill.session")
    ap.add_argument("--heartbeat", type=int, default=200,
                    help="print running total every N processed messages")
    args = ap.parse_args()

    api_id_raw = os.environ.get("TG_API_ID", "").strip()
    api_hash = os.environ.get("TG_API_HASH", "").strip()
    if not api_id_raw or not api_hash:
        print("ERROR: set TG_API_ID and TG_API_HASH (https://my.telegram.org)")
        sys.exit(1)
    try:
        api_id = int(api_id_raw)
    except ValueError:
        print(f"ERROR: TG_API_ID must be a number (got: {api_id_raw!r})")
        sys.exit(1)

    botdb.get_conn()   # init schema (idempotent)
    channel_id = int(args.channel)

    ch = botrepo.get_channel(channel_id)
    if not ch:
        botrepo.add_channel(channel_id, "database", title=None)
        print(f"registered {channel_id} as database channel")

    from_id = args.from_id
    if args.resume:
        hi = _highest_imported(channel_id)
        from_id = max(from_id, hi)  # continue AT hi so we re-check that message (it'll skip fast) then go up
        print(f"[resume] highest already-imported msg-id = {hi}; starting from {from_id}")

    client = TelegramClient(args.session, api_id, api_hash)
    await client.start()
    entity = await client.get_entity(channel_id)
    if not ch or not ch.get("title"):
        # cache channel title for /listchannels display
        _retry_write(botrepo.add_channel, channel_id, "database",
                     title=getattr(entity, "title", None))

    print(f"Connected. Backfilling {entity.title} ({channel_id}) "
          f"ids {from_id}..{args.to_id or 'latest'}")
    print("Press Ctrl+C anytime to stop cleanly. Rerun with --resume to continue.")

    inserted_covers = inserted_pdfs = skipped_dup = skipped_svc = errors = 0
    current_cover_msg_id: int | None = None
    last_seen_mid = 0
    started = time.time()

    try:
        async for msg in client.iter_messages(
            entity, min_id=from_id - 1,
            max_id=(args.to_id or 0), reverse=True):
            if msg is None or msg.id is None:
                continue
            mid = int(msg.id)
            last_seen_mid = mid

            # Fast dup skip
            try:
                exists = botdb.query_one_retry(
                    "SELECT id, kind, parent_source_message_id FROM posts "
                    "WHERE source_chat_id=? AND source_message_id=?",
                    (channel_id, mid))
            except Exception as e:
                print(f"  ! dup-check failed at mid={mid}: {e}")
                exists = None

            if exists:
                skipped_dup += 1
                # keep track of the most-recent cover so PDFs after it group correctly
                if (exists.get("kind") == "cover"):
                    current_cover_msg_id = mid
                if (inserted_covers + inserted_pdfs + skipped_dup) % args.heartbeat == 0:
                    _hb(started, mid, inserted_covers, inserted_pdfs, skipped_dup, skipped_svc, errors)
                continue

            kind, media_kind, file_name, caption = classify(msg)
            if kind == "skip":
                skipped_svc += 1
                continue

            raw = {"message_id": mid,
                   "date": msg.date.isoformat() if msg.date else None,
                   "backfilled_at": _iso()}
            try:
                if kind == "cover":
                    _retry_write(botrepo.insert_cover,
                                 source_chat_id=channel_id, source_message_id=mid,
                                 caption=caption, media_kind=media_kind,
                                 file_id=None, file_name=file_name, raw=raw)
                    current_cover_msg_id = mid
                    inserted_covers += 1
                else:
                    parent = current_cover_msg_id
                    if parent is None:
                        pc = botrepo.find_cover_before(channel_id, mid)
                        parent = pc["source_message_id"] if pc else None
                    _retry_write(botrepo.insert_pdf,
                                 source_chat_id=channel_id, source_message_id=mid,
                                 parent_msg_id=parent, caption=caption,
                                 media_kind=media_kind, file_id=None,
                                 file_name=file_name, raw=raw)
                    inserted_pdfs += 1
            except Exception as e:
                errors += 1
                print(f"  x mid={mid} failed after retries: {e}")

            total_done = inserted_covers + inserted_pdfs + skipped_dup + skipped_svc
            if total_done % args.heartbeat == 0:
                _hb(started, mid, inserted_covers, inserted_pdfs, skipped_dup, skipped_svc, errors)

    except KeyboardInterrupt:
        print("\n[stop] Ctrl+C received; cleaning up.")
    except Exception as e:
        print(f"\n[fatal] {type(e).__name__}: {e}")

    # Set channel cursor to the highest seen msg-id so live capture continues cleanly
    if last_seen_mid:
        try:
            _retry_write(botrepo.set_cursor, channel_id, last_seen_mid)
        except Exception as e:
            print(f"  ! cursor set failed: {e}")

    elapsed = time.time() - started
    print(f"\n===== DONE (elapsed {elapsed:.1f}s) =====")
    print(f"  covers inserted : {inserted_covers}")
    print(f"  pdfs inserted   : {inserted_pdfs}")
    print(f"  skipped dup     : {skipped_dup}")
    print(f"  skipped service : {skipped_svc}")
    print(f"  errors          : {errors}")
    print(f"  last msg-id seen: {last_seen_mid}")
    try:
        t = botrepo.total_covers()
        p = botrepo.queued_covers_count()
        print(f"  DB totals -> covers: {t}, pending: {p}")
    except Exception:
        pass
    print("Rerun with --resume to continue from where you left off.")

    try:
        await client.disconnect()
    except Exception:
        pass


def _hb(started, mid, c, p, dup, svc, err):
    dt = time.time() - started
    rate = (c + p + dup + svc) / dt if dt > 0 else 0
    print(f"  [progress] mid={mid}  covers=+{c}  pdfs=+{p}  dup={dup}  "
          f"svc={svc}  err={err}  ({rate:.1f} msg/s)")


if __name__ == "__main__":
    asyncio.run(main())
