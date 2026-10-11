"""Title/author normalization and citation-key generation.

Ported from PaperMemory's miniHash / firstNonStopLowercase logic so that
matching behaves identically to the battle-tested browser extension.
"""

import re
import unicodedata

# NLTK-style English stop words (same purpose as PaperMemory's englishStopWords).
ENGLISH_STOPWORDS = frozenset(
    """i me my myself we our ours ourselves you your yours yourself yourselves he
    him his himself she her hers herself it its itself they them their theirs
    themselves what which who whom this that these those am is are was were be
    been being have has had having do does did doing a an the and but if or
    because as until while of at by for with about against between into through
    during before after above below to from up down in out on off over under
    again further then once here there when where why how all any both each few
    more most other some such no nor not only own same so than too very s t can
    will just don should now""".split()
)


def fold_ascii(s: str) -> str:
    """Fold accents/unicode to plain ASCII (é -> e)."""
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


def mini_hash(s: str, replace: str = "") -> str:
    """PaperMemory's miniHash: lowercase, non-alphanumeric replaced.

    When ``replace`` is non-empty, each non-word char maps to one replacement
    char so string positions are preserved.
    """
    if replace:
        return re.sub(r"[^a-z0-9_]", replace, s.lower())
    return re.sub(r"[^a-z0-9_]", "", fold_ascii(s).lower())


def norm_title(s: str) -> str:
    """Normalized form used to decide two titles are the same paper."""
    return mini_hash(s)


def clean_title(s: str) -> str:
    """Human-readable cleanup: collapse whitespace, strip braces artifacts and
    a single trailing period (DBLP titles end with '.')."""
    t = re.sub(r"\s+", " ", s).strip()
    if t.endswith(".") and not t.endswith("..."):
        t = t[:-1]
    return t


def first_significant_word(title: str) -> str:
    """First non-stop word of a title, lowercased and alphanumeric-only."""
    words = [mini_hash(w) for w in title.lower().split()]
    words = [w for w in words if w]
    meaningful = [w for w in words if w not in ENGLISH_STOPWORDS]
    if meaningful:
        return meaningful[0]
    return words[0] if words else "paper"


def first_author_last_name(author_field: str) -> str:
    """Last name of the first author from a BibTeX author field.

    Handles both "First Last and ..." and "Last, First and ..." forms.
    """
    first = re.split(r"\s+and\s+", author_field.strip(), flags=re.I)[0].strip()
    first = first.strip("{}")
    if "," in first:
        last = first.split(",")[0]
    else:
        last = first.split()[-1] if first.split() else "anon"
    return mini_hash(last) or "anon"


def sig_tokens(title: str) -> set[str]:
    """Significant title tokens: folded, alphanumeric, stopwords removed."""
    tokens = re.split(r"[^a-z0-9]+", fold_ascii(title).lower())
    return {t for t in tokens if len(t) > 2 and t not in ENGLISH_STOPWORDS}


def titles_similar(a: str, b: str, threshold: float = 0.75) -> bool:
    """Token-overlap similarity — catches preprint→camera-ready title drift
    ("Information-Theoretic Perspective" vs "Information Theory Perspective")
    without matching genuinely different papers.

    Uses the overlap coefficient (|∩| / min) rather than Jaccard so one
    changed word in a shortish title still matches; very short titles
    (<=3 significant tokens, e.g. "Deep Learning") must match exactly
    because a single shared word would otherwise dominate.

    This is a *local* heuristic: flagging near-duplicates in a file, or asking
    whether a replacement still describes the same paper. Deciding that a
    remote record may overwrite a local entry needs :func:`same_paper_title`,
    which does not accept a mere overlap ratio.
    """
    ta, tb = sig_tokens(a), sig_tokens(b)
    if not ta or not tb:
        return False
    smaller = min(len(ta), len(tb))
    if smaller <= 3:
        return ta == tb
    return len(ta & tb) / smaller >= threshold


# A paper's self-given short name, "GMT" in "GMT: General Motion Tracking for
# Humanoid Whole-Body Control". A colon also introduces an ordinary subtitle,
# which is not a name, so only a head of at most three words counts.
_TITLE_HEAD = re.compile(r"^\s*([^:]{1,60}?)\s*:\s*\S", re.S)


def title_acronym(title: str) -> str:
    """The short name a title gives itself before the colon, or ""."""
    m = _TITLE_HEAD.match(title)
    if not m:
        return ""
    head = m.group(1).strip()
    return mini_hash(head) if 1 <= len(head.split()) <= 3 else ""


def _title_description(title: str) -> str:
    """The title without its short name — one side of a preprint/camera-ready
    pair may have added or dropped it."""
    if not title_acronym(title):
        return title
    return title.split(":", 1)[1]


def same_paper_title(a: str, b: str) -> bool:
    """Whether two titles name the SAME paper, tolerating only the wording
    drift a camera-ready version introduces.

    Deliberately stricter than :func:`titles_similar`, because a false
    positive here silently swaps one paper's citation for another's. Papers in
    one subfield share nearly all of their descriptive words: "GMT: General
    Motion Tracking for Humanoid Whole-Body Control" and "SONIC: Supersizing
    Motion Tracking for Natural Humanoid Whole-Body Control" share six of the
    smaller title's eight significant tokens, so no overlap ratio loose enough
    to accept real drift can also separate those two. Identity has to come
    from the short name and from a bound on how many words may differ at all.
    """
    if not a.strip() or not b.strip():
        return False
    if norm_title(a) == norm_title(b):
        return True
    # Different self-given names are different papers, however alike the
    # description that follows them.
    acronym_a, acronym_b = title_acronym(a), title_acronym(b)
    if acronym_a and acronym_b and acronym_a != acronym_b:
        return False
    ta = sig_tokens(_title_description(a))
    tb = sig_tokens(_title_description(b))
    if not ta or not tb:
        return False
    # Short descriptions carry too little signal for any tolerance at all.
    if min(len(ta), len(tb)) <= 3:
        return ta == tb
    # Camera-ready drift rewords: "Information-Theoretic" -> "Information
    # Theory" is one word out on each side. Two independent papers never are.
    return len(ta - tb) <= 1 and len(tb - ta) <= 1


def fix_author_caps(author_field: str) -> str:
    """Normalize ALL-CAPS author names (old CrossRef records store e.g.
    "EPPS, T. W. and PULLEY, LAWRENCE B."). A word is re-cased only when it
    is fully uppercase and longer than 2 letters, so initials ("T.", "W.")
    and legitimately-capitalized short names survive."""

    def fix_word(w: str) -> str:
        core = re.sub(r"[^A-Za-z]", "", w)
        if len(core) > 2 and core.isupper():
            return w.capitalize()
        return w

    def fix_name(name: str) -> str:
        letters = re.sub(r"[^A-Za-z]", "", name)
        if not letters.isupper():
            return name  # mixed case already — leave it alone
        return " ".join(fix_word(w) for w in name.split())

    names = re.split(r"\s+and\s+", author_field)
    return " and ".join(fix_name(n) for n in names)


def fix_pages(pages: str) -> str:
    """BibTeX page ranges use `--`; CrossRef emits en-dashes (411–430) and
    some sources a single hyphen. Collapse any dash run to `--`."""
    return re.sub(r"\s*[-‐-―]+\s*", "--", pages.strip())


def make_key(author_field: str, year: str | int, title: str) -> str:
    """Deterministic citation key: <lastname><year><firstword>.

    Same scheme as PaperMemory (e.g. vaswani2017attention). Note that when
    bibtex-tidy runs with --generate-keys it takes precedence; this is the
    fallback/default key.
    """
    return f"{first_author_last_name(author_field)}{year}{first_significant_word(title)}"
