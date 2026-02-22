"""Spotlighting helpers to defend against indirect prompt injection.

Implements the *data marking* technique described in the Microsoft
research paper (arXiv:2403.14720).  Untrusted text is transformed so
that LLMs can clearly distinguish it from trusted system instructions.

Two strategies are provided:

* **Data marking** -- replaces whitespace with a sentinel token so the
  model sees a visually distinct block of text.
* **Delimiting** -- wraps the text in unique boundary tokens.

Both approaches are lightweight (no external API calls) and can be
applied before feeding untrusted content into any LLM prompt.
"""

from __future__ import annotations

import re

# Default sentinel used for data-marking.  The caret (^) is recommended
# by Microsoft's documentation because it rarely appears in natural text
# and does not conflict with common markup languages.
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

    Parameters
    ----------
    text:
        The untrusted content to transform.
    method:
        ``"datamark"`` (default) or ``"delimit"``.
    marker:
        Sentinel token for data-marking (default ``^``).
    tag:
        Boundary tag for delimiting.

    Returns
    -------
    str
        The transformed text.

    Raises
    ------
    ValueError
        If *method* is not recognized.
    """
    if method == "datamark":
        return datamark(text, marker=marker)
    if method == "delimit":
        return delimit(text, tag=tag)
    raise ValueError(f"Unknown spotlight method: {method!r}")
