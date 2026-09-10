"""Tying a Zoom participant to a Fellow on the roster.

Order matters. Email is authoritative because Zoom captures it from the signed
in account, while the display name is whatever the participant typed - in this
data set eight Fellows have scrambled display names ("Astrid Abobtt") but
perfectly good emails. Name matching is therefore a narrow fallback for rows
with no email or a personal email, not a general strategy.
"""
import re
import unicodedata
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Sequence, Tuple

from .models import Fellow

# Device labels Zoom uses when someone dials in from hardware with no account.
DEVICE_PATTERNS = (
    r"^ipad\b", r"^iphone\b", r"^ipod\b", r"^pixel\b", r"^galaxy\b",
    r"^samsung\b", r"^android\b", r"^huawei\b", r"^oneplus\b", r"^xiaomi\b",
    r"^s\d+\s*(ultra|plus|pro)?$", r"^sm-[a-z0-9]+$", r"^moto\b",
    r"^windows\b", r"^macbook\b", r"^surface\b", r"^chromebook\b",
    r"^zoom\b", r"^user\b", r"^guest\b", r"^caller\b", r"^\+?\d[\d\s\-().]*$",
)

PRONOUNS = {
    "she", "her", "hers", "he", "him", "his", "they", "them", "theirs",
    "ze", "hir", "xe", "ey", "per", "she/her", "he/him", "they/them",
}

TITLES = {"prof", "professor", "dr", "mr", "mrs", "ms", "mx", "rev", "sr", "jr"}


def normalize_email(raw: str) -> str:
    return (raw or "").strip().lower()


def strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def normalize_name(raw: str) -> str:
    """Reduce a Zoom display name to comparable 'first last' form.

    Removes emoji, parenthetical device/affiliation/pronoun tags, bare pronoun
    tokens, honorifics and stray punctuation, then collapses whitespace.
    """
    text = strip_accents(raw or "")
    text = re.sub(r"\([^)]*\)", " ", text)          # "(iPhone)", "(she/her)"
    text = re.sub(r"\[[^\]]*\]", " ", text)
    text = "".join(c for c in text if c.isalpha() or c in " .-'")
    text = text.replace(".", " ").replace("-", " ").replace("'", "")
    tokens = [t for t in text.split() if t]
    kept = [
        t for t in tokens
        if t.lower() not in PRONOUNS and t.lower().strip(".") not in TITLES
    ]
    return " ".join(kept).lower().strip()


def _is_device(display_name: str) -> bool:
    probe = normalize_name(display_name) or (display_name or "").strip().lower()
    return any(re.search(p, probe) for p in DEVICE_PATTERNS)


class RosterIndex:
    """Lookup structures built once per run."""

    def __init__(self, fellows: Sequence[Fellow]):
        self.fellows = list(fellows)
        self.by_email: Dict[str, Fellow] = {}
        self.by_name: Dict[str, List[Fellow]] = {}
        for fellow in self.fellows:
            email = normalize_email(fellow.email)
            if email:
                self.by_email.setdefault(email, fellow)
            name = normalize_name(fellow.name)
            if name:
                self.by_name.setdefault(name, []).append(fellow)

    def match_email(self, email: str) -> Optional[Fellow]:
        return self.by_email.get(normalize_email(email))

    def match_name(self, display_name: str) -> Tuple[Optional[Fellow], bool]:
        """Return (fellow, is_ambiguous). Exact normalized-name hit only."""
        candidates = self.by_name.get(normalize_name(display_name), [])
        if len(candidates) == 1:
            return candidates[0], False
        if len(candidates) > 1:
            return None, True                     # two Fellows share a name
        return None, False

    def match_reversed_name(self, display_name: str) -> Optional[Fellow]:
        """Handle surname-first entries such as 'NGUYEN THANH'."""
        tokens = normalize_name(display_name).split()
        if len(tokens) != 2:
            return None
        flipped = "%s %s" % (tokens[1], tokens[0])
        candidates = self.by_name.get(flipped, [])
        return candidates[0] if len(candidates) == 1 else None

    def suggest(self, display_name: str, threshold: float = 0.82) -> Tuple[Optional[Fellow], float]:
        """Closest roster name, for review suggestions only - never auto-applied."""
        probe = normalize_name(display_name)
        if not probe:
            return None, 0.0
        probe_tokens = probe.split()

        # An abbreviated display name such as "Iva O." or "Cam A." scores badly
        # on raw similarity but is still recognisable: the first token is a
        # prefix of the given name and the rest are initials of the surname.
        # Only treat a name as abbreviated when it really does end in initials,
        # otherwise two unrelated people who merely share an opening syllable
        # ("Leila Nassar" / "Leilani Akhtar") get suggested for each other.
        abbreviated = (
            len(probe_tokens) >= 2
            and len(probe_tokens[0]) >= 3
            and all(len(t) == 1 for t in probe_tokens[1:])
        )

        best: Optional[Fellow] = None
        best_score = 0.0
        for name, fellows in self.by_name.items():
            name_tokens = name.split()
            score = SequenceMatcher(None, probe, name).ratio()
            if abbreviated and len(name_tokens) == len(probe_tokens):
                first_ok = name_tokens[0].startswith(probe_tokens[0])
                rest_ok = all(
                    full.startswith(initial)
                    for full, initial in zip(name_tokens[1:], probe_tokens[1:])
                )
                if first_ok and rest_ok:
                    score = max(score, 0.9)
            if score > best_score:
                best, best_score = fellows[0], score
        if best_score >= threshold:
            return best, best_score
        return None, best_score


# How a Fellow was identified. Surfaced in the Sheet so staff can audit.
BY_EMAIL = "email"
BY_NAME = "name"
BY_NAME_REVERSED = "name (surname first)"
UNMATCHED = "unmatched"
