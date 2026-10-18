"""Text normalization shared by keyword matching, dedupe and district detection.

Bangla text arrives in many encodings of the "same" string: precomposed vs.
decomposed nukta letters (য় / য় ), stray zero-width joiners, Bangla vs. ASCII
digits. Everything that is matched goes through `normalize` first so that keyword
lists only need one spelling.
"""

from __future__ import annotations

import re
import unicodedata

_BN_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")
_INVISIBLE = re.compile("[​‌‍⁠﻿­]")
_PUNCT_MAP = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"',
                            "–": "-", "—": "-", " ": " "})
_WS = re.compile(r"\s+")

BANGLA_RANGE = "ঀ-৿"
_BANGLA_CHAR = re.compile(f"[{BANGLA_RANGE}]")


def normalize_unicode(text: str) -> str:
    """NFC + remove invisible characters + unify digits/punctuation. Case is kept."""
    if not text:
        return ""
    text = unicodedata.normalize("NFC", text)
    text = _INVISIBLE.sub("", text)
    return text.translate(_BN_DIGITS).translate(_PUNCT_MAP)


def normalize(text: str | None) -> str:
    """Normalized, lower-cased, whitespace-collapsed text used for matching."""
    if not text:
        return ""
    return _WS.sub(" ", normalize_unicode(text).lower()).strip()


def has_bangla(text: str) -> bool:
    return bool(_BANGLA_CHAR.search(text))


def detect_language(text: str) -> str:
    """'bn' when a meaningful share of letters are Bangla, else 'en'."""
    sample = text[:2000]
    bangla = len(_BANGLA_CHAR.findall(sample))
    letters = sum(1 for c in sample if c.isalpha()) or 1
    return "bn" if bangla / letters > 0.3 else "en"
