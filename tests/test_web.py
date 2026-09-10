"""End-to-end tests of the browser flow, with Google Sheets stubbed out."""
import csv
import os

import pytest
from fastapi.testclient import TestClient

from app import main
from app.models import Fellow

HERE = os.path.dirname(__file__)
SHEET_URL = "https://docs.google.com/spreadsheets/d/1AbCdEf_TestSheetId123/edit#gid=0"
WRITTEN = {}


def fake_roster(spreadsheet_id, worksheet_name=""):
    with open(os.path.join(HERE, "fixtures", "roster_sample.csv")) as fh:
        fellows = [
            Fellow(name=r["Fellow Name"], email=r["Email"], cohort=r["Cohort"],
                   enrollment_status=r["Enrollment Status"], row_number=i)
            for i, r in enumerate(csv.DictReader(fh), start=2)
        ]
    return fellows, "Roster"


def fake_write(spreadsheet_id, tab_name, results, unmatched, summary_lines):
    WRITTEN.update({
        "id": spreadsheet_id, "tab": tab_name, "results": list(results),
        "unmatched": list(unmatched), "summary": list(summary_lines),
    })
    return tab_name, "https://docs.google.com/spreadsheets/d/%s/edit#gid=99" % spreadsheet_id


@pytest.fixture(autouse=True)
def stub_sheets(monkeypatch):
    monkeypatch.setattr(main, "read_roster", fake_roster)
    monkeypatch.setattr(main, "write_results", fake_write)
    monkeypatch.setattr(main, "service_account_email", lambda: "tracker@example.iam.gserviceaccount.com")
    WRITTEN.clear()


@pytest.fixture
def client():
    return TestClient(main.app)


def upload(client, **overrides):
    with open(os.path.join(HERE, "fixtures", "zoom_sample.csv"), "rb") as fh:
        data = {"sheet_url": SHEET_URL, "start": "17:00", "end": "18:30", "grace": "10"}
        data.update(overrides)
        return client.post(
            "/preview", data=data,
            files={"file": ("zoom.csv", fh.read(), "text/csv")},
        )


def token_from(html):
    marker = 'name="token" value="'
    return html.split(marker, 1)[1].split('"', 1)[0]


def test_home_page_shows_the_service_account_to_share_with(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "tracker@example.iam.gserviceaccount.com" in page.text


def test_healthz(client):
    assert client.get("/healthz").json() == {"ok": True}


def test_preview_detects_the_session_date_from_the_file(client):
    page = upload(client)
    assert page.status_code == 200
    assert "26 August 2026" in page.text
    assert 'value="2026-08-26"' in page.text


def test_preview_does_not_write_anything(client):
    upload(client)
    assert WRITTEN == {}


def test_preview_reports_the_out_of_window_rows(client):
    page = upload(client)
    assert "2 row(s) were left out" in page.text


def test_missing_file_is_a_friendly_error(client):
    page = client.post("/preview", data={"sheet_url": SHEET_URL, "start": "17:00", "end": "18:30", "grace": "10"})
    assert page.status_code == 400
    assert "choose the Zoom attendance CSV" in page.text


def test_bad_sheet_link_is_a_friendly_error(client):
    page = upload(client, sheet_url="not a link")
    assert page.status_code == 400
    assert "roster Google Sheet" in page.text


def test_non_zoom_csv_is_a_friendly_error(client):
    page = client.post(
        "/preview",
        data={"sheet_url": SHEET_URL, "start": "17:00", "end": "18:30", "grace": "10"},
        files={"file": ("shopping.csv", b"colour,size\nred,large\n", "text/csv")},
    )
    assert page.status_code == 400
    assert "does not look like a Zoom participant report" in page.text


def test_write_creates_a_dated_tab_and_leaves_roster_alone(client):
    token = token_from(upload(client).text)
    page = client.post("/write", data={
        "token": token, "sheet_id": "1AbCdEf_TestSheetId123",
        "session_date": "2026-08-26", "start": "17:00", "end": "18:30",
        "grace": "10", "sheet_url": SHEET_URL,
    })
    assert page.status_code == 200
    assert WRITTEN["tab"] == "Attendance 2026-08-26"
    assert WRITTEN["tab"] != "Roster"
    assert "Saved to your Google Sheet" in page.text
    assert any("Policy: absent if more than 10 minutes" in s for s in WRITTEN["summary"])


def test_expired_token_asks_for_a_re_upload(client):
    page = client.post("/write", data={
        "token": "nope", "sheet_id": "x", "session_date": "2026-08-26",
        "start": "17:00", "end": "18:30", "grace": "10", "sheet_url": SHEET_URL,
    })
    assert page.status_code == 400
    assert "expired" in page.text


def test_csv_download_matches_the_preview(client):
    token = token_from(upload(client).text)
    page = client.get("/download/%s" % token, params={
        "session_date": "2026-08-26", "start": "17:00", "end": "18:30",
        "grace": "10", "sheet_id": "1AbCdEf_TestSheetId123",
    })
    assert page.status_code == 200
    assert "attachment" in page.headers["content-disposition"]
    rows = list(csv.reader(page.text.splitlines()))
    assert rows[0][:4] == ["Fellow Name", "Email", "Enrollment Status", "Attendance Status"]
    jonah = [r for r in rows if len(r) > 3 and r[1] == "jonah.whitaker@example.com"][0]
    assert jonah[3] == "Absent"


def test_changing_the_window_changes_the_scores(client):
    token = token_from(upload(client).text)
    # A one hour session: far more Fellows clear the bar.
    page = client.post("/preview", data={
        "token": token, "sheet_url": SHEET_URL, "session_date": "2026-08-26",
        "start": "17:00", "end": "18:00", "grace": "10",
    })
    assert page.status_code == 200
    assert "60 minutes" in page.text


def test_injected_instruction_does_not_mark_everyone_present(client):
    token = token_from(upload(client).text)
    client.post("/write", data={
        "token": token, "sheet_id": "1AbCdEf_TestSheetId123",
        "session_date": "2026-08-26", "start": "17:00", "end": "18:30",
        "grace": "10", "sheet_url": SHEET_URL,
    })
    statuses = {r.status for r in WRITTEN["results"]}
    assert "Absent" in statuses
    injected = [u for u in WRITTEN["unmatched"] if "pre-verified" in u.display_name.lower()]
    assert len(injected) == 1


def test_preview_explains_fellows_who_left_the_program(client, monkeypatch):
    """The banner and the enrollment column only appear when they apply."""
    def roster_with_leavers(spreadsheet_id, worksheet_name=""):
        fellows, title = fake_roster(spreadsheet_id, worksheet_name)
        changed = []
        for f in fellows:
            status = f.enrollment_status
            if f.email.lower() == "alejandro.aoki@example.com":
                status = "Withdrawn"
            elif f.email.lower() == "reza.ahmadi@example.com":
                status = "Removed"
            changed.append(Fellow(name=f.name, email=f.email, cohort=f.cohort,
                                  enrollment_status=status, row_number=f.row_number))
        return changed, title

    monkeypatch.setattr(main, "read_roster", roster_with_leavers)
    page = upload(client)
    assert page.status_code == 200
    assert "2 Fellow(s) are no longer on the program" in page.text
    assert "Not scored - Withdrawn" in page.text
    assert "Not scored - Removed" in page.text
    assert "pill inactive" in page.text


def test_banner_is_absent_when_every_fellow_is_active(client):
    page = upload(client)
    assert "no longer on the program" not in page.text


# --- cohorts --------------------------------------------------------------

def two_cohort_roster(spreadsheet_id, worksheet_name=""):
    """Split the sample roster: A-M into Fall 2026, N-Z into Spring 2027."""
    fellows, title = fake_roster(spreadsheet_id, worksheet_name)
    split = []
    for f in fellows:
        surname = f.name.split()[-1].upper()
        cohort = "Fall 2026" if surname[0] <= "M" else "Spring 2027"
        split.append(Fellow(name=f.name, email=f.email, cohort=cohort,
                            enrollment_status=f.enrollment_status,
                            row_number=f.row_number))
    return split, title


def test_single_cohort_roster_never_asks(client):
    """The sample roster is all Fall 2026, so the extra step must not appear."""
    page = upload(client)
    assert "Which cohort was this session for?" not in page.text
    assert "Check the results before saving" in page.text


def test_multi_cohort_roster_asks_before_scoring(client, monkeypatch):
    monkeypatch.setattr(main, "read_roster", two_cohort_roster)
    page = upload(client)
    assert page.status_code == 200
    assert "Which cohort was this session for?" in page.text
    assert "Fall 2026" in page.text and "Spring 2027" in page.text
    assert "Every cohort" in page.text
    # No scoring has happened yet.
    assert "Check the results before saving" not in page.text


def test_choosing_a_cohort_scores_only_that_cohort(client, monkeypatch):
    monkeypatch.setattr(main, "read_roster", two_cohort_roster)
    token = token_from(upload(client).text)
    page = client.post("/preview", data={
        "token": token, "sheet_url": SHEET_URL, "session_date": "2026-08-26",
        "start": "17:00", "end": "18:30", "grace": "10", "cohort": "Fall 2026",
    })
    assert page.status_code == 200
    assert "Check the results before saving" in page.text
    # Surnames beyond M belong to the other cohort and must be absent entirely.
    assert "astrid.abbott@example.com" in page.text        # Abbott -> Fall
    assert "alejandro.aoki@example.com" in page.text       # Aoki   -> Fall
    assert "lena.park@example.com" not in page.text        # Park   -> Spring


def test_every_cohort_option_scores_the_whole_roster(client, monkeypatch):
    monkeypatch.setattr(main, "read_roster", two_cohort_roster)
    token = token_from(upload(client).text)
    page = client.post("/preview", data={
        "token": token, "sheet_url": SHEET_URL, "session_date": "2026-08-26",
        "start": "17:00", "end": "18:30", "grace": "10", "cohort": "*",
    })
    assert "astrid.abbott@example.com" in page.text
    assert "lena.park@example.com" in page.text


def test_cohort_appears_in_the_tab_name_so_two_cohorts_do_not_collide(client, monkeypatch):
    monkeypatch.setattr(main, "read_roster", two_cohort_roster)
    token = token_from(upload(client).text)
    client.post("/write", data={
        "token": token, "sheet_id": "1AbCdEf_TestSheetId123",
        "session_date": "2026-08-26", "start": "17:00", "end": "18:30",
        "grace": "10", "cohort": "Spring 2027", "sheet_url": SHEET_URL,
    })
    assert WRITTEN["tab"] == "Attendance 2026-08-26 - Spring 2027"
    assert any("Cohort scored: Spring 2027." in s for s in WRITTEN["summary"])


def test_single_cohort_tab_name_stays_plain(client):
    token = token_from(upload(client).text)
    client.post("/write", data={
        "token": token, "sheet_id": "1AbCdEf_TestSheetId123",
        "session_date": "2026-08-26", "start": "17:00", "end": "18:30",
        "grace": "10", "cohort": "", "sheet_url": SHEET_URL,
    })
    assert WRITTEN["tab"] == "Attendance 2026-08-26"


def test_unknown_cohort_is_a_friendly_error(client, monkeypatch):
    monkeypatch.setattr(main, "read_roster", two_cohort_roster)
    token = token_from(upload(client).text)
    page = client.post("/preview", data={
        "token": token, "sheet_url": SHEET_URL, "session_date": "2026-08-26",
        "start": "17:00", "end": "18:30", "grace": "10", "cohort": "Winter 1999",
    })
    assert page.status_code == 400
    assert "No Fellows on the roster are in the cohort" in page.text
