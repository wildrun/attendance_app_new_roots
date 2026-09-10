"""FastAPI front end: upload a Zoom CSV, preview the scoring, write to Sheets."""
import csv
import io
import os
import re
import time
import uuid
from datetime import datetime, timedelta
from typing import Dict, Optional

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from .attendance import GRACE_MINUTES, score_session
from .models import SessionWindow
from .scoring import ParseError, dominant_date, parse_zoom_csv
from .sheets import SheetsError, read_roster, service_account_email, write_results

app = FastAPI(title="New Roots Attendance Tracker")
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))

DEFAULT_START = "17:00"
DEFAULT_END = "18:30"
DEFAULT_SHEET_URL = os.environ.get("DEFAULT_SHEET_URL", "")

# Uploads held briefly so the preview -> write step does not need a re-upload.
_UPLOADS: Dict[str, Dict] = {}
_UPLOAD_TTL_SECONDS = 60 * 60
_MAX_UPLOADS = 40
MAX_UPLOAD_BYTES = 5 * 1024 * 1024

SHEET_ID_RE = re.compile(r"/spreadsheets/d/([a-zA-Z0-9-_]+)")


def _sweep() -> None:
    cutoff = time.time() - _UPLOAD_TTL_SECONDS
    for key in [k for k, v in _UPLOADS.items() if v["at"] < cutoff]:
        _UPLOADS.pop(key, None)
    while len(_UPLOADS) > _MAX_UPLOADS:
        _UPLOADS.pop(min(_UPLOADS, key=lambda k: _UPLOADS[k]["at"]), None)


def extract_sheet_id(value: str) -> str:
    value = (value or "").strip()
    match = SHEET_ID_RE.search(value)
    if match:
        return match.group(1)
    # Allow a bare ID to be pasted too.
    if value and "/" not in value and " " not in value:
        return value
    return ""


def build_window(date_text: str, start_text: str, end_text: str) -> SessionWindow:
    day = datetime.strptime(date_text, "%Y-%m-%d").date()
    start_t = datetime.strptime(start_text, "%H:%M").time()
    end_t = datetime.strptime(end_text, "%H:%M").time()
    start = datetime.combine(day, start_t)
    end = datetime.combine(day, end_t)
    if end <= start:                       # session crossing midnight
        end += timedelta(days=1)
    return SessionWindow(start=start, end=end)


def _summary_lines(report, window, grace) -> list:
    return [
        "Session: %s, %s to %s (%d minutes)." % (
            window.start.strftime("%A %d %B %Y"),
            window.start.strftime("%I:%M %p").lstrip("0"),
            window.end.strftime("%I:%M %p").lstrip("0"),
            round(window.minutes),
        ),
        "Policy: absent if more than %g minutes were missed." % grace,
        "Present: %d. Absent: %d. Needs review: %d." % (
            report.present, report.absent, report.needs_review,
        ),
        "Zoom rows used: %d. Rows outside this session: %d. Not on roster: %d." % (
            report.rows_used, report.rows_other_dates, len(report.unmatched),
        ),
        "Written by the Attendance Tracker on %s." % datetime.now().strftime("%d %b %Y, %H:%M"),
    ]


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "upload.html", {
        "sheet_url": DEFAULT_SHEET_URL,
        "start": DEFAULT_START,
        "end": DEFAULT_END,
        "grace": GRACE_MINUTES,
        "service_account": service_account_email(),
        "error": None,
    })


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.post("/preview", response_class=HTMLResponse)
async def preview(
    request: Request,
    sheet_url: str = Form(""),
    start: str = Form(DEFAULT_START),
    end: str = Form(DEFAULT_END),
    grace: float = Form(GRACE_MINUTES),
    session_date: str = Form(""),
    token: str = Form(""),
    file: Optional[UploadFile] = File(None),
):
    _sweep()

    def fail(message: str):
        return templates.TemplateResponse(request, "upload.html", {
            "sheet_url": sheet_url, "start": start, "end": end,
            "grace": grace, "service_account": service_account_email(),
            "error": message,
        }, status_code=400)

    # Either a fresh upload, or a recalculation of one we already hold.
    if file is not None and file.filename:
        raw = await file.read()
        if len(raw) > MAX_UPLOAD_BYTES:
            return fail("That file is larger than 5 MB. Please upload the Zoom participant report.")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")
        token = uuid.uuid4().hex
        _UPLOADS[token] = {"text": text, "at": time.time(), "filename": file.filename}
    elif token and token in _UPLOADS:
        text = _UPLOADS[token]["text"]
    else:
        return fail("Please choose the Zoom attendance CSV to upload.")

    sheet_id = extract_sheet_id(sheet_url)
    if not sheet_id:
        return fail("Please paste the full link to your roster Google Sheet.")

    try:
        rows, parse_warnings = parse_zoom_csv(text)
    except ParseError as exc:
        return fail(str(exc))

    if not session_date:
        detected = dominant_date(rows)
        session_date = detected.isoformat() if detected else datetime.now().date().isoformat()

    try:
        window = build_window(session_date, start, end)
    except ValueError:
        return fail("The session date or times were not understood. Use the date and time pickers.")

    try:
        fellows, roster_tab = read_roster(sheet_id)
    except SheetsError as exc:
        return fail(str(exc))

    report = score_session(rows, fellows, window, grace_minutes=grace)
    report.warnings.extend(parse_warnings)
    _UPLOADS[token]["report_meta"] = {
        "sheet_id": sheet_id, "session_date": session_date,
        "start": start, "end": end, "grace": grace,
    }

    return templates.TemplateResponse(request, "preview.html", {
        "report": report,
        "window": window,
        "grace": grace,
        "token": token,
        "sheet_url": sheet_url,
        "sheet_id": sheet_id,
        "roster_tab": roster_tab,
        "session_date": session_date,
        "start": start,
        "end": end,
        "tab_name": "Attendance %s" % session_date,
        "filename": _UPLOADS[token].get("filename", "attendance.csv"),
    })


@app.post("/write", response_class=HTMLResponse)
def write(
    request: Request,
    token: str = Form(...),
    sheet_id: str = Form(...),
    session_date: str = Form(...),
    start: str = Form(...),
    end: str = Form(...),
    grace: float = Form(GRACE_MINUTES),
    sheet_url: str = Form(""),
):
    _sweep()
    entry = _UPLOADS.get(token)
    if not entry:
        return templates.TemplateResponse(request, "upload.html", {
            "sheet_url": sheet_url, "start": start, "end": end,
            "grace": grace, "service_account": service_account_email(),
            "error": "That upload has expired. Please upload the CSV again.",
        }, status_code=400)

    rows, _ = parse_zoom_csv(entry["text"])
    window = build_window(session_date, start, end)

    try:
        fellows, _tab = read_roster(sheet_id)
        report = score_session(rows, fellows, window, grace_minutes=grace)
        tab_name, tab_url = write_results(
            sheet_id,
            "Attendance %s" % session_date,
            report.results,
            report.unmatched,
            _summary_lines(report, window, grace),
        )
    except SheetsError as exc:
        return templates.TemplateResponse(request, "upload.html", {
            "sheet_url": sheet_url, "start": start, "end": end,
            "grace": grace, "service_account": service_account_email(),
            "error": str(exc),
        }, status_code=502)

    return templates.TemplateResponse(request, "done.html", {
        "report": report,
        "tab_name": tab_name,
        "tab_url": tab_url,
        "token": token,
        "window": window,
    })


@app.get("/download/{token}")
def download(token: str, session_date: str = "", start: str = DEFAULT_START,
             end: str = DEFAULT_END, grace: float = GRACE_MINUTES,
             sheet_id: str = ""):
    """Always-available fallback: the same results as a CSV."""
    entry = _UPLOADS.get(token)
    if not entry:
        return RedirectResponse("/", status_code=303)

    rows, _ = parse_zoom_csv(entry["text"])
    if not session_date:
        detected = dominant_date(rows)
        session_date = detected.isoformat() if detected else datetime.now().date().isoformat()
    window = build_window(session_date, start, end)

    fellows, _tab = read_roster(sheet_id) if sheet_id else ([], "")
    report = score_session(rows, fellows, window, grace_minutes=grace)

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([
        "Fellow Name", "Email", "Attendance Status", "Minutes Present",
        "Minutes Missed", "Matched By", "Needs Review", "Notes",
    ])
    for r in report.results:
        writer.writerow([
            r.name, r.email, r.status, r.minutes_present, r.minutes_missed,
            r.match_method, "Yes" if r.needs_review else "", r.note_text,
        ])
    if report.unmatched:
        writer.writerow([])
        writer.writerow(["Not on the roster - please review by hand"])
        writer.writerow([
            "Zoom Display Name", "Zoom Email", "Minutes In Session",
            "Why Unmatched", "Suggestion",
        ])
        for u in report.unmatched:
            writer.writerow([
                u.display_name, u.email, u.minutes_present, u.reason, u.suggestion,
            ])

    buffer.seek(0)
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="attendance-%s.csv"' % session_date,
        },
    )
