"""Spotlighting helpers to defend against indirect prompt injection."""

from __future__ import annotations

import re

_DEFAULT_MARKER = "^"


def datamark(text: str, marker: str = _DEFAULT_MARKER) -> str:
    """Apply data-marking to *text* by replacing whitespace with *marker*.

    >>> datamark("hello world")
    'hello^world'
    >>> datamark("  a  b  ")
    'a^b'
    """
    return re.sub(r"\s+", marker, text.strip())


def delimit(text: str, tag: str = "UNTRUSTED_CONTENT") -> str:
    """Wrap *text* in unique delimiter tags.

    >>> delimit("some input", tag="DOC")
    '<<<DOC>>>\\nsome input\\n<<</DOC>>>'
    """
    return f"<<<{tag}>>>\n{text}\n<<</{tag}>>>"


def spotlight(
    text: str,
    *,
    method: str = "datamark",
    marker: str = _DEFAULT_MARKER,
    tag: str = "UNTRUSTED_CONTENT",
) -> str:
    """Apply a spotlighting transformation to untrusted *text*.

    *method* is ``"datamark"`` (default) or ``"delimit"``.
    """
    if method == "datamark":
        return datamark(text, marker=marker)
    if method == "delimit":
        return delimit(text, tag=tag)
    raise ValueError(f"Unknown spotlight method: {method!r}")
