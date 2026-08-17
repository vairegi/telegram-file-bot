"""Sync engine: captures posts from Database Channel(s).

Rules (per your spec):
- The Database Channel contains cover posts followed by 1..N PDFs. The next
  cover marks the boundary of the previous cover's PDF group.
- We assign #N (post_number) ONLY when a cover post is captured. All PDFs
  belonging to that cover share the cover's #N via parent_source_message_id.
- If a PDF arrives before any cover exists above it (or before the current
  cursor), we still store it (kind='pdf', parent NULL). When a cover is later
  captured, orphan PDFs between (cover_msg_id, later_msg_id] are back-attached.
- The cursor is per (db_chat_id[, main_chat_id]) so a future multi-main setup
  can independently resume.
"""
from __future__ import annotations

from typing import Optional

from . import repo


def classify_message(msg) -> tuple[str, str, Optional[str], Optional[str]]:
    """Return (kind, media_kind, file_id, file_name).

    kind: 'cover' | 'pdf'
    - PDF documents -> 'pdf'
    - Everything else (photo / video / text / non-PDF document / audio) -> 'cover'
    """
    doc = getattr(msg, "document", None)
    photo = getattr(msg, "photo", None)
    video = getattr(msg, "video", None)
    audio = getattr(msg, "audio", None)

    if doc is not None:
        name = (getattr(doc, "file_name", "") or "").lower()
        mime = (getattr(doc, "mime_type", "") or "").lower()
        is_pdf = name.endswith(".pdf") or "pdf" in mime
        if is_pdf:
            return ("pdf", "document", doc.file_id, getattr(doc, "file_name", None))
        return ("cover", "document", doc.file_id, getattr(doc, "file_name", None))
    if photo:
        # Aiogram returns list of PhotoSize; take the largest (last)
        biggest = photo[-1] if isinstance(photo, list) else photo
        return ("cover", "photo", getattr(biggest, "file_id", None), None)
    if video is not None:
        return ("cover", "video", video.file_id, getattr(video, "file_name", None))
    if audio is not None:
        return ("cover", "audio", audio.file_id, getattr(audio, "file_name", None))
    return ("cover", "text", None, None)


async def handle_channel_post(msg) -> Optional[dict]:
    """Ingest one channel post. Returns the stored post record, or None if skipped."""
    chat = getattr(msg, "chat", None)
    if not chat:
        return None
    chat_id = chat.id
    msg_id = msg.message_id

    # Only sync from registered database channels
    dbs = {c["chat_id"] for c in repo.get_database_channels()}
    if chat_id not in dbs:
        return None

    # Resume cursor: ignore everything <= cursor
    cursor = repo.get_cursor(chat_id)
    if cursor and msg_id <= cursor:
        return None

    # Skip duplicates
    if repo.post_exists(chat_id, msg_id):
        return None

    kind, media_kind, file_id, file_name = classify_message(msg)
    caption = getattr(msg, "caption", None) or getattr(msg, "text", None)

    if kind == "cover":
        pid, number, code = repo.insert_cover(
            source_chat_id=chat_id, source_message_id=msg_id,
            caption=caption, media_kind=media_kind,
            file_id=file_id, file_name=file_name,
            raw={"message_id": msg_id})
        # Back-attach any orphan PDFs that arrived between the previous cover
        # (or the cursor) and this new cover -> they belong to the PREVIOUS
        # cover. But covers grow forward in time, so orphans in the window
        # (previous_cover_msg_id, this_cover_msg_id) belong to previous_cover.
        prev_cover = _previous_cover(chat_id, msg_id)
        if prev_cover is not None:
            for op in repo.orphan_pdfs_between(chat_id, prev_cover["source_message_id"], msg_id):
                repo.attach_pdf_to_cover(op["id"], prev_cover["source_message_id"])
        result = {"kind": "cover", "id": pid, "number": number, "code": code}
    else:
        # PDF: parent = most recent cover in this chat (may be None)
        parent_cover = repo.find_cover_before(chat_id, msg_id)
        parent_msg_id = parent_cover["source_message_id"] if parent_cover else None
        pid = repo.insert_pdf(
            source_chat_id=chat_id, source_message_id=msg_id,
            parent_msg_id=parent_msg_id, caption=caption,
            media_kind=media_kind, file_id=file_id, file_name=file_name,
            raw={"message_id": msg_id})
        result = {"kind": "pdf", "id": pid, "parent_msg_id": parent_msg_id}

    repo.set_cursor(chat_id, msg_id)
    return result


def _previous_cover(chat_id: int, this_msg_id: int) -> Optional[dict]:
    from ..db import query_one
    return query_one(
        "SELECT * FROM posts WHERE kind='cover' AND source_chat_id=? AND source_message_id<? "
        "ORDER BY source_message_id DESC LIMIT 1", (chat_id, this_msg_id))


async def ensure_cursor_seeded() -> None:
    """If START_MESSAGE_ID env is set and no cursor yet, seed once per DB channel."""
    from ..config import settings as cfg
    start = getattr(cfg, "start_message_id", 0) or 0
    if not start:
        return
    for c in repo.get_database_channels():
        if repo.get_cursor(c["chat_id"]) == 0:
            repo.set_cursor(c["chat_id"], int(start) - 1)


async def set_cursor_from_link(db_chat_id: int, msg_id: int, main_chat_id: Optional[int] = None) -> dict:
    """Implement /setcursor. Behavior:

    - Linked message is INCLUSIVE (start FROM this post).
    - If the linked message is a known PDF, walk UP to the nearest cover in
      that DB channel and start from there.
    - Cursor is set to (start_msg_id - 1) so the next capture is start_msg_id.
    """
    known = repo.get_post_by_source(db_chat_id, msg_id)
    start_msg = msg_id
    note = "linked-inclusive"
    if known and known.get("kind") == "pdf":
        cover = repo.find_cover_before(db_chat_id, msg_id)
        if cover is not None:
            start_msg = cover["source_message_id"]
            note = f"pdf-link → rewound to cover #{cover.get('post_number')}"
    repo.set_cursor(db_chat_id, start_msg - 1, main_chat_id)
    return {"db_chat_id": db_chat_id, "main_chat_id": main_chat_id,
            "cursor": start_msg - 1, "next": start_msg, "note": note}
