"""Canonical venue names.

Parses the vendored ``data/strings.bib`` @string table (journals /
conferences / workshops) and maps venue strings returned by DBLP, Semantic
Scholar, Google Scholar, CrossRef, Unpaywall, etc. onto the canonical names.
"""

import re
from dataclasses import dataclass
from importlib import resources

from .normalize import fold_ascii

CATEGORY_HEADER = re.compile(r"%{2,}\s*(Journals|Conferences|Workshops)", re.I)
STRING_DEF = re.compile(r'@string\{(\w+)\s*=\s*"([^"]+)"\}', re.I)

# Entries whose section in strings.bib does not reflect how they are cited.
CATEGORY_OVERRIDES = {
    "RSS": "conference",  # listed among journals
    "TMLR": "journal",  # listed among conferences
    "SIGGRAPH": "journal",  # cited as ACM TOG articles
    "SIGGRAPHAsia": "journal",
}

# Tokens dropped on BOTH sides before comparing venue names.
DROP_TOKENS = frozenset(
    "ieee cvf acm rsj the annual proceedings proc of on in".split()
)

# Hand-written aliases (normalized form -> macro) for spellings the automatic
# alias generation cannot derive, e.g. DBLP's abbreviated journal names.
EXTRA_ALIASES = {
    "neural information processing systems": "NeurIPS",
    "nips": "NIPS",
    "trans pattern anal mach intell": "TPAMI",
    "pattern analysis and machine intelligence": "TPAMI",
    "j mach learn res": "JMLR",
    "int j comput vis": "IJCV",
    "trans mach learn res": "TMLR",
    "transactions machine learning research": "TMLR",
    "robotics autom lett": "RAL",
    "robotics and automation letters": "RAL",
    "computer vision and pattern recognition": "CVPR",
    "winter applications computer vision": "WACV",
    "aaai artificial intelligence": "AAAI",
    "national aaai": "AAAI",
    "aaai": "AAAI",
    "acl": "ACL",
    "emnlp": "EMNLP",
    "naacl": "NAACL",
    "naacl hlt": "NAACL",
    "north american chapter association for computational linguistics": "NAACL",
    "empirical methods natural language processing": "EMNLP",
    "association for computational linguistics": "ACL",
    "robotics science and systems": "RSS",
    "3dv": "TDV",
    "colt": "COLT92",
    "computational learning theory": "COLT92",
    "knowledge discovery and data mining": "KDD",
    "int j robotics res": "IJRR",
    "j field robotics": "IJRR",
}


@dataclass(frozen=True)
class Venue:
    macro: str
    name: str  # canonical full name (LaTeX escapes removed at parse time? kept)
    category: str  # "journal" | "conference" | "workshop"

    @property
    def is_journal(self) -> bool:
        return self.category == "journal"

    @property
    def bib_field(self) -> str:
        return "journal" if self.is_journal else "booktitle"

    @property
    def entry_type(self) -> str:
        return "article" if self.is_journal else "inproceedings"


def _norm(s: str) -> str:
    """Normalize a venue string for comparison."""
    s = fold_ascii(s).lower()
    s = re.sub(r"\\(.)", r"\1", s)  # \& -> &
    s = s.replace("&", " and ")
    s = re.sub(r"\(.*?\)", " ", s)  # drop parentheticals (acronyms handled separately)
    tokens = re.split(r"[^a-z0-9]+", s)
    out = []
    for t in tokens:
        if not t or t in DROP_TOKENS:
            continue
        if t.isdigit() or re.fullmatch(r"\d+(st|nd|rd|th)", t):
            continue
        out.append(t)
    return " ".join(out)


def _acronyms(s: str) -> list[str]:
    """Candidate acronyms in a raw venue string: parenthesized chunks and
    standalone ALL-CAPS tokens, mini-hashed."""
    cands = re.findall(r"\(([^()]+)\)", s)
    cands += [t for t in re.split(r"[\s,.:]+", s) if len(t) >= 2 and t.isupper()]
    out = []
    for c in cands:
        c = re.sub(r"[^a-z0-9]", "", fold_ascii(c).lower())
        if c and c not in out:
            out.append(c)
    return out


class VenueTable:
    def __init__(self, text: str):
        self.venues: dict[str, Venue] = {}
        self._exact: dict[str, str] = {}  # normalized string -> macro
        self._acr: dict[str, str] = {}  # minihashed acronym -> macro
        self._containment: list[tuple[str, str]] = []  # (normalized full, macro)
        self._parse(text)
        self._build_aliases()

    def _parse(self, text: str):
        category = "journal"
        for line in text.splitlines():
            m = CATEGORY_HEADER.search(line)
            if m:
                category = m.group(1).lower().rstrip("s")  # journal/conference/workshop
                continue
            m = STRING_DEF.search(line)
            if not m:
                continue
            macro, name = m.group(1), m.group(2)
            cat = CATEGORY_OVERRIDES.get(macro, category)
            self.venues[macro] = Venue(macro=macro, name=name, category=cat)

    def _build_aliases(self):
        for macro, v in self.venues.items():
            mini = re.sub(r"[^a-z0-9]", "", macro.lower())
            self._acr.setdefault(mini, macro)
            for acr in _acronyms(v.name):
                self._acr.setdefault(acr, macro)
                # Workshop variants: "ICCV Workshops" style aliases.
                if v.category == "workshop" and acr.endswith("w"):
                    self._exact.setdefault(f"{acr[:-1]} workshops", macro)
            n = _norm(v.name)
            if n:
                self._exact.setdefault(n, macro)
                if len(n.split()) >= 3:
                    self._containment.append((n, macro))
        for alias, macro in EXTRA_ALIASES.items():
            self._exact[alias] = macro
        # Longest names first so e.g. ICCVW ("... computer vision workshops")
        # wins over ICCV on containment.
        self._containment.sort(key=lambda p: -len(p[0]))

    # ------------------------------------------------------------------
    def canonicalize(self, venue: str, year: int | str | None = None) -> Venue | None:
        """Map an arbitrary venue string to a canonical Venue, or None."""
        if not venue or not venue.strip():
            return None
        raw = venue.strip()
        n = _norm(raw)
        lowered = re.sub(r"[^a-z0-9]", "", fold_ascii(raw).lower())
        is_workshoppy = "workshop" in n

        macro = None
        # 1. exact normalized-name / alias match
        if n in self._exact:
            macro = self._exact[n]
        # 2. the whole string is an acronym (e.g. DBLP venue "CVPR")
        if macro is None and lowered in self._acr:
            macro = self._acr[lowered]
        # 3. embedded acronyms — prefer workshop variants when applicable
        if macro is None:
            for acr in _acronyms(raw):
                if is_workshoppy and acr + "w" in self._acr:
                    macro = self._acr[acr + "w"]
                    break
                if acr in self._acr:
                    macro = self._acr[acr]
                    break
        # 4. containment of the canonical full name in the venue string
        if macro is None:
            for full, m in self._containment:
                if full in n:
                    macro = m
                    break
        if macro is None:
            return None
        macro = self._apply_year_rules(macro, year)
        return self.venues[macro]

    @staticmethod
    def _year_int(year) -> int | None:
        try:
            return int(str(year)[:4])
        except (TypeError, ValueError):
            return None

    def _apply_year_rules(self, macro: str, year) -> str:
        y = self._year_int(year)
        if macro in ("NIPS", "NeurIPS"):
            if y is None:
                return "NeurIPS"
            return "NeurIPS" if y >= 2018 else "NIPS"
        if macro in ("WACV", "WACV_until_2016"):
            if y is not None and y <= 2016:
                return "WACV_until_2016"
            return "WACV"
        return macro


_table: VenueTable | None = None


def _strings_text() -> str:
    """The @string table, overridable so other people can use their own:
    $BIBCITE_STRINGS, then ~/.config/bibcite/strings.bib, then the vendored
    default."""
    import os
    from pathlib import Path

    env = os.environ.get("BIBCITE_STRINGS")
    if env:
        return Path(env).read_text()
    user = Path.home() / ".config" / "bibcite" / "strings.bib"
    if user.exists():
        return user.read_text()
    return (resources.files("bibcite") / "data" / "strings.bib").read_text()


def get_table() -> VenueTable:
    global _table
    if _table is None:
        _table = VenueTable(_strings_text())
    return _table


def canonicalize(venue: str, year=None) -> Venue | None:
    return get_table().canonicalize(venue, year)
