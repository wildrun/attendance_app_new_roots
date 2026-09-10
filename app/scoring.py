"""Parsing the Zoom report and turning intervals into minutes.

The whole correctness story of this app lives here: Zoom's own "Duration
(Minutes)" column cannot be used directly, because it counts time outside the
session window and double counts a Fellow who is connected from two devices.
We recompute from the join/leave timestamps instead.
"""
import csv
import io
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

from .models import SessionWindow, ZoomRow

# Zoom exports vary a little between accounts; accept the common spellings.
NAME_KEYS = ("name (original name)", "name", "user name", "participant")
EMAIL_KEYS = ("user email", "email", "email address")
JOIN_KEYS = ("join time", "join_time", "joined")
LEAVE_KEYS = ("leave time", "leave_time", "left")
DURATION_KEYS = ("duration (minutes)", "duration", "duration(minutes)")

TIME_FORMATS = (
    "%m/%d/%Y %I:%M:%S %p",
    "%m/%d/%Y %I:%M %p",
    "%m/%d/%Y %H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%b %d, %Y %I:%M:%S %p",
)


class ParseError(Exception):
    """Raised when the uploaded file is not a Zoom participant report."""


def _pick(header: Sequence[str], candidates: Sequence[str]) -> Optional[str]:
    lowered = {h.strip().lower(): h for h in header if h}
    for want in candidates:
        if want in lowered:
            return lowered[want]
    return None


def parse_timestamp(raw: str) -> datetime:
    text = re.sub(r"\s+", " ", (raw or "").strip())
    for fmt in TIME_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise ValueError("unrecognised timestamp: %r" % raw)


def parse_zoom_csv(text: str) -> Tuple[List[ZoomRow], List[str]]:
    """Return (rows, warnings). Bad lines are skipped, never fatal."""
    warnings: List[str] = []

    # Zoom sometimes prefixes a meeting-summary block before the real header.
    # Find the first line that looks like a participant header.
    lines = text.splitlines()
    start_index = 0
    for i, line in enumerate(lines[:40]):
        low = line.lower()
        if any(k in low for k in JOIN_KEYS) and any(k in low for k in NAME_KEYS + EMAIL_KEYS):
            start_index = i
            break
    body = "\n".join(lines[start_index:])

    reader = csv.DictReader(io.StringIO(body))
    header = reader.fieldnames or []
    name_key = _pick(header, NAME_KEYS)
    email_key = _pick(header, EMAIL_KEYS)
    join_key = _pick(header, JOIN_KEYS)
    leave_key = _pick(header, LEAVE_KEYS)
    duration_key = _pick(header, DURATION_KEYS)

    if not join_key or not leave_key or not (name_key or email_key):
        raise ParseError(
            "This file does not look like a Zoom participant report. Expected "
            "columns for name, join time and leave time; found: %s"
            % (", ".join(header) if header else "no columns")
        )

    rows: List[ZoomRow] = []
    for offset, raw in enumerate(reader, start=start_index + 2):
        name = (raw.get(name_key) or "").strip() if name_key else ""
        email = (raw.get(email_key) or "").strip() if email_key else ""
        if not name and not email:
            continue
        try:
            join = parse_timestamp(raw.get(join_key) or "")
            leave = parse_timestamp(raw.get(leave_key) or "")
        except ValueError as exc:
            warnings.append("Line %d (%s): %s - row skipped." % (offset, name or email, exc))
            continue
        if leave < join:
            warnings.append(
                "Line %d (%s): leave time is before join time - row skipped."
                % (offset, name or email)
            )
            continue

        duration = None
        if duration_key:
            try:
                duration = int(float(raw.get(duration_key) or ""))
            except (TypeError, ValueError):
                duration = None

        rows.append(
            ZoomRow(
                display_name=name,
                email=email,
                join=join,
                leave=leave,
                reported_duration=duration,
                line_number=offset,
            )
        )

    if not rows:
        raise ParseError("No usable attendance rows were found in this file.")
    return rows, warnings


def dominant_date(rows: Sequence[ZoomRow]):
    """The calendar date most rows belong to - used to prefill the form."""
    counts: Dict = {}
    for row in rows:
        counts[row.join.date()] = counts.get(row.join.date(), 0) + 1
    return max(counts.items(), key=lambda kv: kv[1])[0] if counts else None


def minutes_in_window(intervals: Sequence[Tuple[datetime, datetime]], window: SessionWindow) -> float:
    """Total minutes covered by the union of `intervals`, clipped to `window`.

    Union (not sum) is what makes a Fellow connected from a phone and a laptop
    at the same time count once, and what makes the gaps between six rejoins
    count as missed time.
    """
    clipped = []
    for start, end in intervals:
        lo = max(start, window.start)
        hi = min(end, window.end)
        if hi > lo:
            clipped.append((lo, hi))
    if not clipped:
        return 0.0

    clipped.sort()
    total = timedelta()
    cur_start, cur_end = clipped[0]
    for start, end in clipped[1:]:
        if start <= cur_end:                 # overlapping or touching
            cur_end = max(cur_end, end)
        else:                                 # a real gap: bank the run
            total += cur_end - cur_start
            cur_start, cur_end = start, end
    total += cur_end - cur_start
    return round(total.total_seconds() / 60.0, 1)
