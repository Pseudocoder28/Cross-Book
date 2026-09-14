"""Text normalization for matching event titles and outcome names.

Deterministic and dependency-free on purpose: the matcher proposes, a human
decides, so the goal is high recall with explainable scores — not cleverness.
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


def fold(text: str) -> str:
    """Lowercase, accent-fold, punctuation -> space."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def title_tokens(text: str) -> frozenset[str]:
    text = text.lower()
    for pattern, repl in TITLE_SYNONYMS:
        text = re.sub(pattern, repl, text)
    return frozenset(t for t in fold(text).split() if t not in TITLE_STOPWORDS and len(t) > 1)


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
    """Jaccard, but containment counts: "Dodgers" in "Los Angeles Dodgers"."""
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if inter == 0:
        return 0.0
    containment = inter / min(len(a), len(b))
    # A single shared token is only decisive when one side *is* that token.
    if inter == 1 and min(len(a), len(b)) > 1:
        return len(a & b) / len(a | b)
    return max(containment, jaccard(a, b))
