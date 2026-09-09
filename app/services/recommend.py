"""Similar-content recommendations (v4.0).

Given a cover post, find the 3-5 most similar OTHER covers in the database:
  Stage 1 — title match: covers whose title shares meaningful tokens with the
            requested one (exact/series matches first).
  Stage 2 — tag similarity: Jaccard-style weighted overlap over the caption's
            hashtags (Tags/Parodies/Characters/Artists/Groups lines). Heavier
            weights for Parodies/Characters (more specific) than generic Tags.

Returns rows with .post_number + .code so the caller can build deep-link
buttons straight into the bot.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from . import repo
from ..utils import clean_caption

log = logging.getLogger("recommend")

# How many buttons to show.
MAX_RESULTS = 5
MIN_RESULTS = 3

# Hashtag sections in the caption and their similarity weight.
_SECTION_WEIGHTS = {
    "parodies": 3.0,
    "characters": 3.0,
    "artists": 2.0,
    "groups": 2.0,
    "tags": 1.0,
    "languages": 0.0,   # excluded — 'english/translated' would match everything
    "categories": 0.0,  # excluded — everything is 'doujinshi'
}

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_TAG_RE = re.compile(r"#([A-Za-z0-9_]+)")
_SECTION_RE = re.compile(r"➤\s*(\w+):\s*(.*)", re.IGNORECASE)

# Words too generic to be useful for title matching.
_STOPWORDS = {"the", "a", "an", "no", "ni", "wa", "o", "to", "ga", "de", "of",
              "in", "on", "and", "or", "my", "your", "his", "her", "its"}


def parse_tags(caption: Optional[str]) -> dict:
    """Extract weighted tag sets from a cover caption.

    Returns {"tags": {...}, "parodies": {...}, ...} — each a set of lowercase
    tag strings. Missing sections simply don't appear."""
    out: dict = {}
    if not caption:
        return out
    text = clean_caption(caption)
    for line in text.splitlines():
        m = _SECTION_RE.search(line)
        if not m:
            continue
        section = m.group(1).lower()
        if _SECTION_WEIGHTS.get(section, 0.0) <= 0.0:
            continue
        tags = {t.lower() for t in _TAG_RE.findall(m.group(2))}
        if tags:
            out.setdefault(section, set()).update(tags)
    return out


def title_tokens(title: Optional[str]) -> set:
    """Meaningful lowercase word tokens from a title, minus stopwords."""
    if not title:
        return set()
    toks = set(_TOKEN_RE.findall(title.lower()))
    return {t for t in toks if t not in _STOPWORDS and len(t) > 2}


def _title_of(caption: Optional[str]) -> str:
    if not caption:
        return ""
    for line in clean_caption(caption).splitlines():
        s = line.strip().lstrip("*_").rstrip("*_").strip()
        if s and not s.startswith("➤"):
            return s
    return ""


def tag_similarity(a: dict, b: dict) -> float:
    """Weighted overlap score between two parsed-tag dicts."""
    score = 0.0
    for section, w in _SECTION_WEIGHTS.items():
        if w <= 0:
            continue
        sa, sb = a.get(section, set()), b.get(section, set())
        if not sa or not sb:
            continue
        inter = len(sa & sb)
        union = len(sa | sb)
        if union:
            score += w * (inter / union)
    return score


def title_similarity(a_toks: set, b_toks: set) -> float:
    if not a_toks or not b_toks:
        return 0.0
    inter = len(a_toks & b_toks)
    return inter / len(a_toks | b_toks)


async def similar_covers(cover: dict, limit: int = MAX_RESULTS) -> list:
    """Return up to `limit` similar published covers (dicts with post_number,
    code, caption) excluding the requested one. Empty list if none found."""
    base_id = int(cover.get("id") or cover.get("_id") or 0)
    base_caption = cover.get("caption") or ""
    base_tags = parse_tags(base_caption)
    base_toks = title_tokens(_title_of(base_caption))

    # Candidate pool: recent published covers. Bounded for speed.
    pool = await repo.recent_published_covers(limit=400, exclude_id=base_id)
    if not pool:
        return []

    scored = []
    for c in pool:
        c_cap = c.get("caption") or ""
        c_toks = title_tokens(_title_of(c_cap))
        tscore = title_similarity(base_toks, c_toks)
        gscore = tag_similarity(base_tags, parse_tags(c_cap))
        # title dominates when it matches; tags fill in / break ties
        total = (tscore * 10.0) + gscore
        if total > 0.05:  # ignore zero-relevance noise
            scored.append((total, tscore, gscore, c))

    scored.sort(key=lambda x: (-x[0], -x[1], -x[2]))
    return [c for _, _, _, c in scored[:int(limit)]]
