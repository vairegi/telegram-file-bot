"""Single classifier — used by BOTH live sync AND MTProto backfill.

Contract:
    classify(msg_like) -> (kind, media_kind, file_id_or_None, file_name_or_None,
                           mime_type_or_None)

    kind ∈ {'cover', 'file', 'skip'}
    media_kind ∈ {'photo', 'document', 'sticker', 'video', 'audio', 'text', 'other'}

Rules (per approved v2 spec):
    * Photo message                             → cover / photo
    * Document w/ image MIME OR image extension → cover / photo   (spoiler-able)
    * Document w/ video MIME OR video extension → cover / video   (spoiler-able)
    * Document w/ file ext (.pdf .cbz .cbr .cbt .cb7 .zip .rar .7z .epub)
      OR matching MIME (pdf/cbz/cbr/…)          → file / document
    * Sticker (any form)                        → file / sticker  (only kept if
                                                                    a cover exists above it — enforced by
                                                                    caller, not here)
    * Divider / emoji-only text (≤40 chars)     → skip
    * Everything else                           → skip
"""
from __future__ import annotations

import re
from typing import Optional, Tuple

# ---- Extensions ----
FILE_EXTS  = (".pdf", ".cbz", ".cbr", ".cbt", ".cb7", ".zip", ".rar", ".7z", ".epub")
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")
VIDEO_EXTS = (".mp4", ".mov", ".webm", ".mkv")

# ---- MIME fragments (any substring match) ----
FILE_MIMES = ("pdf", "cbz", "cbr", "cbt", "epub", "zip", "rar", "7z",
              "comicbook", "x-cbz", "x-cbr", "x-cbt")

_EMOJI_RE = re.compile(
    r"[\U0001F300-\U0001FAFF\U0001F000-\U0001F9FF"
    r"\u2600-\u27BF\u2300-\u23FF\u2B00-\u2BFF"
    r"\u25A0-\u25FF\u2700-\u27BF\u3000-\u303F"
    r"\uFE00-\uFE0F\u200B-\u200D\uFF00-\uFFEF]+"
)
_PUNCT_RE = re.compile(
    r"[\s\-\—\–\_\=\.\,\!\?\|\/\\*#@~`\^\(\)\[\]{}<>\+•▪▫◾◽◼◻■□●○★☆♦♢♥♡♠♣➤]+"
)


def _is_divider_text(text: str) -> bool:
    if not text:
        return False
    t = text.strip()
    if len(t) > 40:
        return False
    stripped = _PUNCT_RE.sub("", _EMOJI_RE.sub("", t))
    return len(stripped) == 0


def _is_image_doc(name: str, mime: str) -> bool:
    n = (name or "").lower()
    m = (mime or "").lower()
    if m.startswith("image/") and m != "image/webp":  # webp handled explicitly below
        return True
    if any(n.endswith(e) for e in IMAGE_EXTS):
        return True
    if m == "image/webp" and n and not n.endswith(".tgs"):
        # webp with a real filename → probably a legit image cover, not a sticker
        return True
    return False


def _is_video_doc(name: str, mime: str) -> bool:
    n = (name or "").lower()
    m = (mime or "").lower()
    if m.startswith("video/"):
        return True
    if any(n.endswith(e) for e in VIDEO_EXTS):
        return True
    return False


def _is_file_doc(name: str, mime: str) -> bool:
    n = (name or "").lower()
    m = (mime or "").lower()
    if _is_image_doc(n, m) or _is_video_doc(n, m):
        return False
    if any(n.endswith(e) for e in FILE_EXTS):
        return True
    if any(k in m for k in FILE_MIMES):
        return True
    return False


def _has_sticker_attribute(doc) -> bool:
    """Telethon: DocumentAttributeSticker. Bot API: no such thing, msg.sticker
    is the tell-tale field."""
    for attr in getattr(doc, "attributes", None) or []:
        if type(attr).__name__ == "DocumentAttributeSticker":
            return True
    return False


def _fname_of_doc(doc) -> Optional[str]:
    """Bot API: doc.file_name. Telethon: DocumentAttributeFilename."""
    n = getattr(doc, "file_name", None)
    if n:
        return n
    for attr in getattr(doc, "attributes", None) or []:
        n = getattr(attr, "file_name", None)
        if n:
            return n
    return None


def classify(msg) -> Tuple[str, str, Optional[str], Optional[str], Optional[str]]:
    """Return (kind, media_kind, file_id, file_name, mime_type).

    Accepts BOTH aiogram Message AND Telethon Message — the shape overlap
    (photo, document, video, audio, sticker, text/message/caption) is what
    matters.
    """
    # ----- Sticker (native — aiogram) or via Telethon (msg.sticker set) -----
    sticker = getattr(msg, "sticker", None)
    if sticker is not None:
        # ALWAYS classified as 'file' with media_kind='sticker'.
        # The CALLER decides whether to store it (only if there's a cover above).
        return ("file", "sticker",
                getattr(sticker, "file_id", None),
                None,
                (getattr(sticker, "mime_type", "") or "").lower() or None)

    # ----- Document (may itself be a sticker via attributes / webp+no-name) -
    doc = getattr(msg, "document", None)
    if doc is not None:
        name = _fname_of_doc(doc) or ""
        mime = (getattr(doc, "mime_type", "") or "").lower()

        # Sticker-as-document (webp / tgs, no filename) OR sticker attribute:
        if _has_sticker_attribute(doc):
            return ("file", "sticker", getattr(doc, "file_id", None), name or None, mime or None)
        if mime in ("image/webp", "application/x-tgsticker") and not name:
            return ("file", "sticker", getattr(doc, "file_id", None), None, mime or None)

        if _is_file_doc(name, mime):
            return ("file", "document",
                    getattr(doc, "file_id", None),
                    name or None,
                    mime or None)
        if _is_image_doc(name, mime):
            return ("cover", "photo",
                    getattr(doc, "file_id", None),
                    name or None,
                    mime or None)
        if _is_video_doc(name, mime):
            return ("cover", "video",
                    getattr(doc, "file_id", None),
                    name or None,
                    mime or None)
        # Non-image / non-video / non-file document — skip (audio scripts, etc.)
        return ("skip", "document", None, name or None, mime or None)

    # ----- Native photo -----
    photo = getattr(msg, "photo", None)
    if photo:
        biggest = photo[-1] if isinstance(photo, list) else photo
        return ("cover", "photo",
                getattr(biggest, "file_id", None),
                None, "image/jpeg")

    # ----- Native video / audio -----
    video = getattr(msg, "video", None)
    if video is not None:
        return ("cover", "video",
                getattr(video, "file_id", None),
                getattr(video, "file_name", None),
                (getattr(video, "mime_type", "") or "video/mp4").lower())
    audio = getattr(msg, "audio", None)
    if audio is not None:
        # Audio is not a cover in v2 spec (photos + image docs only per Q1).
        return ("skip", "audio",
                getattr(audio, "file_id", None),
                getattr(audio, "file_name", None),
                (getattr(audio, "mime_type", "") or "").lower() or None)

    # ----- Text-only -----
    text = (getattr(msg, "caption", None)
            or getattr(msg, "text", None)
            or getattr(msg, "message", None))
    if text and _is_divider_text(text):
        return ("skip", "text", None, None, None)

    # Anything else (service msgs, empty messages, contact/location, ...)
    return ("skip", "other", None, None, None)


def caption_of(msg) -> Optional[str]:
    raw = (getattr(msg, "caption", None)
           or getattr(msg, "text", None)
           or getattr(msg, "message", None))
    if not raw:
        return raw
    # Strip literal markdown markers so main-channel reposts show clean text.
    import re as _re
    return _re.sub(r"(\*\*|__|~~|\|\|)", "", raw)
