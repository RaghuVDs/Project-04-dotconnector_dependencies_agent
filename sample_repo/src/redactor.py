"""Tiny PII redaction utility -- the 'Enterprise Redaction Utility' under test.

Deliberately dependency-free so the test suite runs in milliseconds. The
vulnerable packages live in requirements.txt for the agent to fix; the code
itself doesn't import them, which keeps the validation gate fast.
"""
from __future__ import annotations

import re

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_CARD = re.compile(r"\b(?:\d[ -]?){13,16}\b")


def redact(text: str) -> str:
    """Mask emails, US SSNs, and card-like numbers in `text`."""
    text = _EMAIL.sub("[EMAIL]", text)
    text = _SSN.sub("[SSN]", text)
    text = _CARD.sub("[CARD]", text)
    return text


def redaction_count(text: str) -> int:
    """How many PII tokens would be redacted."""
    return (len(_EMAIL.findall(text))
            + len(_SSN.findall(text))
            + len(_CARD.findall(text)))
