"""Text normalization for matching event titles and outcome names.

Deterministic and dependency-free on purpose: the matcher proposes, a human
decides, so the goal is high recall with explainable scores — not cleverness.

Numbers are identity. "Map 1" and "Map 2" are different events and "Over 2.5"
and "Over 3.5" are different outcomes, and in each case the only token that
says so is one character long. So digits survive the single-character filter
that drops "a" and "x", a decimal point between digits survives folding (so
"2.5" is one token, not the "2" and "5" that "5.2" would also make), and
:func:`name_similarity` treats two names that disagree on a number as
different however much else they share.
"""

from __future__ import annotations

import re
import unicodedata

# Words that carry no identity across venues ("Ohio Senate winner?" vs
# "Ohio Senate Election Winner").
TITLE_STOPWORDS = frozenset(
    """
    the a an of in on for to and or by at be will who what which win winner
    wins election elections midterm midterms champion champions championship
    general party race 2026 2027 us
    """.split()
)

# Venue shorthand expanded to a common vocabulary before tokenizing.
TITLE_SYNONYMS: tuple[tuple[str, str], ...] = (
    (r"\bu\.?s\.?\b", "us"),
    (r"\bnl\b", "national league"),
    (r"\bal\b", "american league"),
    (r"\bmls\b", "major league soccer"),
    (r"\broty\b", "rookie of the year"),
    (r"\bgov\b", "governor"),
    (r"\bsen\b", "senate"),
    (r"\bpres\b", "presidential"),
    (r"\bmvp\b", "mvp"),
)

NAME_STOPWORDS = frozenset("the fc sc cf afc party".split())
# "(D)", "(R)", "(Ind)", "(I)", "Jr." etc. are venue decoration, not identity.
_PAREN = re.compile(r"\([^)]*\)")
_SUFFIX = re.compile(r"\b(jr|sr|ii|iii|iv)\.?$")
# "2.5" stays one token and "1,000" becomes "1000"; every other run of
# punctuation becomes a space. The NUL is a placeholder no title contains.
_DECIMAL_POINT = re.compile(r"(?<=\d)\.(?=\d)")
_THOUSANDS_COMMA = re.compile(r"(?<=\d),(?=\d{3}\b)")
_NON_TOKEN = re.compile(r"[^a-z0-9\x00]+")


def fold(text: str) -> str:
    """Lowercase, accent-fold, punctuation -> space (a decimal point stays)."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    text = _THOUSANDS_COMMA.sub("", text)
    text = _DECIMAL_POINT.sub("\x00", text)
    return _NON_TOKEN.sub(" ", text).replace("\x00", ".").strip()


def numeric_tokens(tokens: frozenset[str]) -> frozenset[str]:
    """The tokens that are numbers — "2", "2.5", "1000" — but not "g2" or "f1"."""
    return frozenset(t for t in tokens if t[0].isdigit())


def numbers_conflict(a: frozenset[str], b: frozenset[str]) -> bool:
    """Both sides name a number and neither side's numbers contain the other's.

    "Map 1" / "Map 2" conflict. "Game 1 of 7" / "Game 1" do not (a subset is
    the same thing said with less), and nor does a number on one side only.
    """
    na, nb = numeric_tokens(a), numeric_tokens(b)
    return bool(na and nb) and not (na <= nb or nb <= na)


def title_tokens(text: str) -> frozenset[str]:
    text = text.lower()
    for pattern, repl in TITLE_SYNONYMS:
        text = re.sub(pattern, repl, text)
    return frozenset(
        t for t in fold(text).split() if t not in TITLE_STOPWORDS and (len(t) > 1 or t.isdigit())
    )


def name_tokens(text: str) -> frozenset[str]:
    text = _PAREN.sub(" ", text)
    text = fold(text)
    text = _SUFFIX.sub("", text).strip()
    return frozenset(t for t in text.split() if t not in NAME_STOPWORDS)


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def name_similarity(a: frozenset[str], b: frozenset[str]) -> float:
    """Jaccard, but containment counts: "Dodgers" in "Los Angeles Dodgers".

    Except that numbers are identity, not vocabulary: "Over 2.5" and "Over
    3.5" are different outcomes however much else they share, so names that
    disagree on a number score zero.
    """
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if inter == 0 or numbers_conflict(a, b):
        return 0.0
    containment = inter / min(len(a), len(b))
    # A single shared token is only decisive when one side *is* that token.
    if inter == 1 and min(len(a), len(b)) > 1:
        return len(a & b) / len(a | b)
    return max(containment, jaccard(a, b))
