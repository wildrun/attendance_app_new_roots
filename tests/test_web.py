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
    assert rows[0][:3] == ["Fellow Name", "Email", "Attendance Status"]
    jonah = [r for r in rows if len(r) > 2 and r[1] == "jonah.whitaker@example.com"][0]
    assert jonah[2] == "Absent"


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
