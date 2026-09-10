"""Orchestration: Zoom rows + roster + window -> scored results."""
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Sequence, Tuple

from .matching import (
    BY_EMAIL, BY_NAME, BY_NAME_REVERSED, RosterIndex, _is_device, normalize_email,
)
from .models import (
    ABSENT, ABSENT_NO_RECORD, NOT_SCORED, PRESENT, Fellow, Result,
    SessionWindow, UnmatchedRow, ZoomRow, is_active, not_scored_status,
)
from .scoring import minutes_in_window

# Program policy: miss more than this many minutes and you are absent.
GRACE_MINUTES = 10.0

# Form value meaning "score every cohort on the roster".
ALL_COHORTS = "*"


def available_cohorts(fellows: Sequence[Fellow]) -> List[Tuple[str, int, int]]:
    """Distinct cohorts as (name, total Fellows, active Fellows), commonest first.

    Fellows whose cohort cell is blank are grouped under "" so they are still
    reachable rather than silently unselectable.
    """
    totals: Dict[str, int] = {}
    actives: Dict[str, int] = {}
    for fellow in fellows:
        name = (fellow.cohort or "").strip()
        totals[name] = totals.get(name, 0) + 1
        if is_active(fellow.enrollment_status):
            actives[name] = actives.get(name, 0) + 1
    return sorted(
        ((name, count, actives.get(name, 0)) for name, count in totals.items()),
        key=lambda item: (-item[1], item[0]),
    )


def filter_by_cohort(fellows: Sequence[Fellow], cohort: str) -> List[Fellow]:
    """`ALL_COHORTS` (or an empty choice) keeps everyone."""
    if not cohort or cohort == ALL_COHORTS:
        return list(fellows)
    wanted = cohort.strip().lower()
    return [f for f in fellows if (f.cohort or "").strip().lower() == wanted]


@dataclass
class Report:
    results: List[Result] = field(default_factory=list)
    unmatched: List[UnmatchedRow] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    rows_used: int = 0
    rows_other_dates: int = 0

    @property
    def present(self) -> int:
        return sum(1 for r in self.results if r.status == PRESENT)

    @property
    def absent(self) -> int:
        """Counts only Fellows who were expected at the session."""
        return sum(
            1 for r in self.results
            if r.status not in (PRESENT,) and not r.status.startswith(NOT_SCORED)
        )

    @property
    def not_scored(self) -> int:
        return sum(1 for r in self.results if r.status.startswith(NOT_SCORED))

    @property
    def needs_review(self) -> int:
        return sum(1 for r in self.results if r.needs_review)


def score_session(
    rows: Sequence[ZoomRow],
    fellows: Sequence[Fellow],
    window: SessionWindow,
    grace_minutes: float = GRACE_MINUTES,
) -> Report:
    report = Report()
    index = RosterIndex(fellows)

    # 1. Keep only rows that overlap the session window at all. This is what
    #    drops the two 08/19 rows left over from the previous week's export.
    in_window: List[ZoomRow] = []
    for row in rows:
        if row.leave > window.start and row.join < window.end:
            in_window.append(row)
        else:
            report.rows_other_dates += 1
    report.rows_used = len(in_window)

    # 2. Resolve each row to a Fellow.
    intervals: Dict[str, List[Tuple]] = defaultdict(list)
    methods: Dict[str, str] = {}
    review_notes: Dict[str, List[str]] = defaultdict(list)
    leftovers: List[ZoomRow] = []

    for row in in_window:
        fellow = index.match_email(row.email) if row.email else None
        method = BY_EMAIL if fellow else ""

        if not fellow:
            fellow, ambiguous = index.match_name(row.display_name)
            if fellow:
                method = BY_NAME
            elif ambiguous:
                leftovers.append(row)
                continue

        if not fellow:
            fellow = index.match_reversed_name(row.display_name)
            method = BY_NAME_REVERSED if fellow else method

        if not fellow:
            leftovers.append(row)
            continue

        key = normalize_email(fellow.email) or fellow.name.lower()
        intervals[key].append((row.join, row.leave))
        # Email beats name if a Fellow was found both ways across rows.
        if methods.get(key) != BY_EMAIL:
            methods[key] = method
        if method != BY_EMAIL and row.email:
            review_notes[key].append(
                "Matched by name; Zoom email %s is not on the roster." % row.email
            )
        elif method != BY_EMAIL:
            review_notes[key].append("Matched by display name; no email in the Zoom report.")

    # 3. Score every Fellow on the roster, including those who never joined.
    for fellow in fellows:
        key = normalize_email(fellow.email) or fellow.name.lower()
        notes = list(dict.fromkeys(review_notes.get(key, [])))

        if not is_active(fellow.enrollment_status):
            # Not expected at this session. Still show any time they spent in
            # it: a withdrawn Fellow who turns up usually means the roster is
            # out of date, and that is worth surfacing rather than hiding.
            attended = minutes_in_window(intervals.get(key, []), window)
            if attended > 0:
                notes.append(
                    "Roster says %s, but they attended %.1f minutes. "
                    "Check whether the roster is up to date."
                    % (fellow.enrollment_status.strip(), attended)
                )
            else:
                notes.append(
                    "Roster says %s, so this session was not scored for them."
                    % fellow.enrollment_status.strip()
                )
            report.results.append(
                Result(
                    name=fellow.name,
                    email=fellow.email,
                    enrollment_status=fellow.enrollment_status,
                    status=not_scored_status(fellow.enrollment_status),
                    minutes_present=attended,
                    minutes_missed=0.0,
                    match_method=methods.get(key, "none"),
                    needs_review=attended > 0,
                    notes=notes,
                )
            )
            continue

        if key not in intervals:
            report.results.append(
                Result(
                    name=fellow.name,
                    email=fellow.email,
                    enrollment_status=fellow.enrollment_status,
                    status=ABSENT_NO_RECORD,
                    minutes_present=0.0,
                    minutes_missed=round(window.minutes, 1),
                    match_method="none",
                    needs_review=False,
                    notes=["No matching row in the Zoom report."],
                )
            )
            continue

        segments = intervals[key]
        present = minutes_in_window(segments, window)
        missed = round(window.minutes - present, 1)
        if len(segments) > 1:
            notes.append("%d connection segments merged." % len(segments))

        report.results.append(
            Result(
                name=fellow.name,
                email=fellow.email,
                enrollment_status=fellow.enrollment_status,
                status=PRESENT if missed <= grace_minutes else ABSENT,
                minutes_present=present,
                minutes_missed=missed,
                match_method=methods.get(key, BY_EMAIL),
                needs_review=bool(review_notes.get(key)),
                notes=notes,
            )
        )

    report.results.sort(key=lambda r: r.name.lower())

    # 4. Everything we could not place, grouped so one guest is one line.
    grouped: Dict[str, List[ZoomRow]] = defaultdict(list)
    for row in leftovers:
        grouped[normalize_email(row.email) or row.display_name.strip().lower()].append(row)

    for group in grouped.values():
        first = group[0]
        present = minutes_in_window([(r.join, r.leave) for r in group], window)
        if _is_device(first.display_name) and not first.email:
            reason = "No email, and the display name is a device or app name rather than a person."
            suggestion = ""
        else:
            match, score = index.suggest(first.display_name)
            reason = "Not found on the roster."
            suggestion = ""
            if match:
                suggestion = "Did you mean %s (%s)? %.0f%% name similarity." % (
                    match.name, match.email, score * 100,
                )
        report.unmatched.append(
            UnmatchedRow(
                display_name=first.display_name,
                email=first.email,
                minutes_present=present,
                reason=reason,
                suggestion=suggestion,
            )
        )

    report.unmatched.sort(key=lambda u: -u.minutes_present)
    return report
