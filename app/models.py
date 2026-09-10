"""Shared data structures.

Kept free of any I/O so the scoring path can be unit tested without a network
or a Google credential.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional


@dataclass(frozen=True)
class Fellow:
    """One row of the program roster."""
    name: str
    email: str
    cohort: str = ""
    enrollment_status: str = ""
    row_number: int = 0


@dataclass
class ZoomRow:
    """One line of the Zoom participant report."""
    display_name: str
    email: str
    join: datetime
    leave: datetime
    reported_duration: Optional[int]
    line_number: int


@dataclass
class SessionWindow:
    start: datetime
    end: datetime

    @property
    def minutes(self) -> float:
        return (self.end - self.start).total_seconds() / 60.0


@dataclass
class Result:
    """Scored attendance for one Fellow."""
    name: str
    email: str
    status: str
    minutes_present: float
    minutes_missed: float
    match_method: str
    needs_review: bool = False
    notes: List[str] = field(default_factory=list)

    @property
    def note_text(self) -> str:
        return "; ".join(self.notes)


@dataclass
class UnmatchedRow:
    """A Zoom participant we could not tie to a Fellow."""
    display_name: str
    email: str
    minutes_present: float
    reason: str
    suggestion: str = ""


# Status vocabulary, kept in one place so the UI and the Sheet agree.
PRESENT = "Present"
ABSENT = "Absent"
ABSENT_NO_RECORD = "Absent - no record"
