"""Google Sheets access via a service account.

A service account is used rather than end-user OAuth so that staff never see a
consent screen or a token expiry. Setup is one manual step: share the roster
Sheet with the service account's email address as an Editor.
"""
import json
import os
from typing import List, Optional, Sequence, Tuple

import gspread
from google.oauth2.service_account import Credentials

from .models import Fellow, Result, UnmatchedRow

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

NAME_HEADERS = ("fellow name", "name", "full name", "fellow")
EMAIL_HEADERS = ("email", "email address", "fellow email")
COHORT_HEADERS = ("cohort",)
STATUS_HEADERS = ("enrollment status", "status", "enrolment status")

RESULT_HEADERS = [
    "Fellow Name", "Email", "Enrollment Status", "Attendance Status",
    "Minutes Present", "Minutes Missed", "Matched By", "Needs Review", "Notes",
]
UNMATCHED_HEADERS = [
    "Zoom Display Name", "Zoom Email", "Minutes In Session", "Why Unmatched",
    "Suggestion",
]


class SheetsError(Exception):
    """Something went wrong talking to Google, phrased for a non-technical reader."""


def _credentials() -> Credentials:
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if raw:
        try:
            info = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SheetsError("The Google credentials setting is not valid JSON.") from exc
        return Credentials.from_service_account_info(info, scopes=SCOPES)

    path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
    if path and os.path.exists(path):
        return Credentials.from_service_account_file(path, scopes=SCOPES)

    raise SheetsError(
        "No Google credentials are configured. Set GOOGLE_SERVICE_ACCOUNT_JSON "
        "on the server."
    )


def service_account_email() -> str:
    """Shown in the UI so staff know which address to share the Sheet with."""
    try:
        creds = _credentials()
        return getattr(creds, "service_account_email", "") or ""
    except SheetsError:
        return ""


def _client() -> gspread.Client:
    return gspread.authorize(_credentials())


def _open(spreadsheet_id: str) -> gspread.Spreadsheet:
    try:
        return _client().open_by_key(spreadsheet_id)
    except gspread.SpreadsheetNotFound:
        raise SheetsError(
            "That Google Sheet could not be opened. Check the link, and make "
            "sure the Sheet is shared with %s as an Editor."
            % (service_account_email() or "the service account")
        )
    except gspread.exceptions.APIError as exc:
        raise SheetsError("Google refused the request: %s" % exc) from exc


def _index_of(header: Sequence[str], candidates: Sequence[str]) -> Optional[int]:
    lowered = [(h or "").strip().lower() for h in header]
    for want in candidates:
        if want in lowered:
            return lowered.index(want)
    return None


def read_roster(spreadsheet_id: str, worksheet_name: str = "") -> Tuple[List[Fellow], str]:
    """Return (fellows, worksheet_title). Reads the first tab unless named."""
    book = _open(spreadsheet_id)
    sheet = book.worksheet(worksheet_name) if worksheet_name else book.get_worksheet(0)
    if sheet is None:
        raise SheetsError("That Google Sheet has no tabs to read.")

    values = sheet.get_all_values()
    if not values:
        raise SheetsError("The roster tab '%s' is empty." % sheet.title)

    header = values[0]
    name_i = _index_of(header, NAME_HEADERS)
    email_i = _index_of(header, EMAIL_HEADERS)
    if name_i is None or email_i is None:
        raise SheetsError(
            "The roster tab '%s' needs a name column and an email column. "
            "Found: %s" % (sheet.title, ", ".join(h for h in header if h) or "nothing")
        )
    cohort_i = _index_of(header, COHORT_HEADERS)
    status_i = _index_of(header, STATUS_HEADERS)

    def cell(row, i):
        return (row[i].strip() if i is not None and i < len(row) else "")

    fellows: List[Fellow] = []
    for line_no, row in enumerate(values[1:], start=2):
        name = cell(row, name_i)
        email = cell(row, email_i)
        if not name and not email:
            continue
        fellows.append(
            Fellow(
                name=name,
                email=email,
                cohort=cell(row, cohort_i),
                enrollment_status=cell(row, status_i),
                row_number=line_no,
            )
        )
    if not fellows:
        raise SheetsError("No Fellows were found on the roster tab '%s'." % sheet.title)
    return fellows, sheet.title


def _resize(sheet, rows: int, cols: int) -> None:
    if sheet.row_count < rows or sheet.col_count < cols:
        sheet.resize(rows=max(rows, sheet.row_count), cols=max(cols, sheet.col_count))


def write_results(
    spreadsheet_id: str,
    tab_name: str,
    results: Sequence[Result],
    unmatched: Sequence[UnmatchedRow],
    summary_lines: Sequence[str],
) -> Tuple[str, str]:
    """Write results to their own tab. Returns (tab title, direct URL).

    The roster tab is never modified. Re-running the same session clears and
    rewrites its tab rather than appending, so uploading twice is harmless.
    """
    book = _open(spreadsheet_id)

    body = [RESULT_HEADERS]
    for r in results:
        body.append([
            r.name, r.email, r.enrollment_status, r.status, r.minutes_present,
            r.minutes_missed, r.match_method, "Yes" if r.needs_review else "",
            r.note_text,
        ])

    body.append([])
    body.append(["Run summary"])
    for line in summary_lines:
        body.append([line])

    if unmatched:
        body.append([])
        body.append(["Not on the roster - please review by hand"])
        body.append(UNMATCHED_HEADERS)
        for u in unmatched:
            body.append([
                u.display_name, u.email, u.minutes_present, u.reason, u.suggestion,
            ])

    width = max(len(row) for row in body)
    body = [row + [""] * (width - len(row)) for row in body]

    try:
        sheet = book.worksheet(tab_name)
        sheet.clear()
        _resize(sheet, len(body) + 10, width)
    except gspread.WorksheetNotFound:
        sheet = book.add_worksheet(title=tab_name, rows=len(body) + 10, cols=width)

    # RAW keeps Google from evaluating a display name such as "=IMPORTXML(...)"
    # as a formula. Participant-supplied text is data, never a live cell.
    sheet.update(values=body, range_name="A1", value_input_option="RAW")
    sheet.freeze(rows=1)
    sheet.format("A1:%s1" % chr(ord("A") + width - 1), {"textFormat": {"bold": True}})

    url = "https://docs.google.com/spreadsheets/d/%s/edit#gid=%s" % (
        spreadsheet_id, sheet.id,
    )
    return sheet.title, url
