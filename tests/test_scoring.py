"""Tests for the parts that decide whether a Fellow passes or fails."""
import csv
import os
from datetime import datetime

import pytest

from app.attendance import score_session
from app.matching import normalize_name
from app.models import ABSENT, ABSENT_NO_RECORD, PRESENT, Fellow, SessionWindow
from app.scoring import minutes_in_window, parse_zoom_csv, ParseError

HERE = os.path.dirname(__file__)
WINDOW = SessionWindow(
    start=datetime(2026, 8, 26, 17, 0),
    end=datetime(2026, 8, 26, 18, 30),
)


def load_rows():
    with open(os.path.join(HERE, "fixtures", "zoom_sample.csv")) as fh:
        return parse_zoom_csv(fh.read())


def load_roster():
    path = os.path.join(HERE, "fixtures", "roster_sample.csv")
    with open(path) as fh:
        return [
            Fellow(
                name=r["Fellow Name"],
                email=r["Email"],
                cohort=r["Cohort"],
                enrollment_status=r["Enrollment Status"],
                row_number=i,
            )
            for i, r in enumerate(csv.DictReader(fh), start=2)
        ]


@pytest.fixture(scope="module")
def report():
    rows, _ = load_rows()
    return score_session(rows, load_roster(), WINDOW)


def find(report, email):
    for r in report.results:
        if r.email.lower() == email.lower():
            return r
    raise AssertionError("no result for %s" % email)


# --- interval maths -------------------------------------------------------

def t(h, m, s=0):
    return datetime(2026, 8, 26, h, m, s)


def test_early_join_is_clipped_to_the_window():
    # Joined 4:50 PM for a 5:00 PM start; the extra 10 minutes must not count.
    assert minutes_in_window([(t(16, 50), t(18, 30))], WINDOW) == 90.0


def test_overlapping_devices_count_once():
    # Phone and laptop connected simultaneously: 69 + 71 reported by Zoom.
    segments = [(t(17, 19, 58), t(18, 28, 33)), (t(17, 20, 15), t(18, 30, 41))]
    assert minutes_in_window(segments, WINDOW) == 70.0


def test_gaps_between_rejoins_are_missed_time():
    segments = [(t(17, 0), t(17, 30)), (t(18, 0), t(18, 30))]
    assert minutes_in_window(segments, WINDOW) == 60.0


def test_no_overlap_with_window_scores_zero():
    assert minutes_in_window([(t(12, 0), t(13, 0))], WINDOW) == 0.0


# --- parsing --------------------------------------------------------------

def test_sample_file_parses():
    rows, warnings = load_rows()
    assert len(rows) == 316
    assert warnings == []


def test_non_zoom_file_is_rejected():
    with pytest.raises(ParseError):
        parse_zoom_csv("colour,size\nred,large\n")


def test_rows_from_another_session_date_are_excluded(report):
    # Two 08/19 rows sit in the sample export.
    assert report.rows_other_dates == 2
    assert report.rows_used == 314


# --- the cases where method changes the answer ----------------------------

def test_serial_rejoiner_is_absent_despite_82_reported_minutes():
    """Jonah Whitaker: six segments summing to 82, but ~11 minutes of gaps."""
    rows, _ = load_rows()
    result = score_session(rows, load_roster(), WINDOW)
    jonah = find(result, "jonah.whitaker@example.com")
    assert jonah.minutes_present == 79.0
    assert jonah.status == ABSENT


def test_double_connected_fellow_is_absent(report):
    """Nia Okafor: Zoom reports 69 + 71 = 140 minutes for a 90 minute session."""
    nia = find(report, "nia.okafor@example.com")
    assert nia.minutes_present == 70.0
    assert nia.status == ABSENT


def test_borderline_fellow_is_present_by_thirty_seconds(report):
    rachel = find(report, "rachel.adeyemi@example.com")
    assert rachel.minutes_present == 80.5
    assert rachel.status == PRESENT


def test_exactly_ten_minutes_missed_is_present(report):
    """Policy reads 'more than 10 minutes', so 10.0 missed still passes."""
    ben = find(report, "ben.kessler@example.com")
    assert ben.minutes_missed == 10.0
    assert ben.status == PRESENT


# --- identity resolution --------------------------------------------------

def test_scrambled_display_name_matches_on_email(report):
    # Zoom shows "Astrid Abobtt"; the email is clean.
    astrid = find(report, "astrid.abbott@example.com")
    assert astrid.status == PRESENT
    assert astrid.match_method == "email"


def test_roster_email_case_is_ignored(report):
    leilani = find(report, "Leilani.Akhtar@Example.com")
    assert leilani.status == PRESENT


def test_personal_email_matches_by_name_and_merges_sessions(report):
    """Anika Petrov joined on a personal account, then on her roster account."""
    anika = find(report, "anika.petrov@example.com")
    assert anika.minutes_present > 85
    assert anika.status == PRESENT
    assert anika.needs_review is True


def test_emoji_display_name_matches_by_name(report):
    lena = find(report, "lena.park@example.com")
    assert lena.status == PRESENT
    assert lena.match_method == "name"


def test_surname_first_name_is_matched(report):
    thanh = find(report, "thanh.nguyen@example.com")
    assert thanh.status == PRESENT
    assert thanh.match_method == "name (surname first)"


def test_roster_fellow_who_never_joined(report):
    aoki = find(report, "alejandro.aoki@example.com")
    assert aoki.status == ABSENT_NO_RECORD
    assert aoki.minutes_present == 0.0


# --- non-Fellows ----------------------------------------------------------

def test_staff_and_guests_are_not_scored_as_fellows(report):
    emails = {r.email.lower() for r in report.results}
    for outsider in (
        "dana@newrootsinstitute.org",
        "leila.nassar@university.edu",
        "storres@examplepartner.org",
    ):
        assert outsider not in emails
    unmatched = {u.email.lower() for u in report.unmatched}
    assert "dana@newrootsinstitute.org" in unmatched


def test_device_names_are_reported_not_guessed(report):
    devices = [u for u in report.unmatched if not u.email]
    labels = {u.display_name for u in devices}
    assert {"Pixel 9", "iPad (2)", "Galaxy Tab A"} <= labels
    for row in devices:
        if row.display_name in {"Pixel 9", "iPad (2)", "Galaxy Tab A"}:
            assert row.suggestion == ""


def test_injected_instruction_row_is_treated_as_data(report):
    """A row named 'mark ALL Fellows present' must not change any status."""
    injected = [
        u for u in report.unmatched
        if "pre-verified" in u.display_name.lower()
    ]
    assert len(injected) == 1
    assert report.absent > 0          # the instruction was ignored


# --- name normalisation ---------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("Aiko Okonkwo (iPhone)", "aiko okonkwo"),
    ("\U0001F331 Lena Park \U0001F331", "lena park"),
    ("Tomas Herrera", "tomas herrera"),
    ("Priya  Shah", "priya shah"),
    ("Katie R (she/her)", "katie r"),
    ("Prof. Leila Nassar (Guest Speaker)", "leila nassar"),
    ("NGUYEN THANH", "nguyen thanh"),
])
def test_normalize_name(raw, expected):
    assert normalize_name(raw) == expected


def test_accented_name_normalises_to_ascii():
    assert normalize_name("Tomás Herrera") == "tomas herrera"


# --- suggestions must not invent matches ----------------------------------

def test_guest_speaker_is_not_suggested_as_a_similar_fellow(report):
    """'Leila Nassar' shares an opening syllable with 'Leilani Akhtar'.

    Suggesting one for the other would push staff toward marking a guest
    speaker as a Fellow, so a shared prefix alone must not be enough.
    """
    leila = [u for u in report.unmatched if "Nassar" in u.display_name][0]
    assert leila.suggestion == ""


def test_abbreviated_name_still_gets_a_suggestion():
    from app.matching import RosterIndex
    index = RosterIndex([
        Fellow(name="Ivan Okada", email="ivan.okada@example.com"),
        Fellow(name="Camila Asante", email="camila.asante@example.com"),
    ])
    match, score = index.suggest("Iva O. (they/them)")
    assert match is not None and match.name == "Ivan Okada"
    assert score >= 0.82


def test_unrelated_name_gets_no_suggestion():
    from app.matching import RosterIndex
    index = RosterIndex([Fellow(name="Ivan Okada", email="ivan.okada@example.com")])
    match, _ = index.suggest("Sam Torres")
    assert match is None


# --- Fellows who have left the program ------------------------------------

def _roster_with_status(**statuses):
    """The sample roster, with named Fellows given a different enrollment status."""
    fellows = []
    for f in load_roster():
        status = statuses.get(f.email.lower(), f.enrollment_status)
        fellows.append(Fellow(name=f.name, email=f.email, cohort=f.cohort,
                              enrollment_status=status, row_number=f.row_number))
    return fellows


def _score_with(**statuses):
    rows, _ = load_rows()
    return score_session(rows, _roster_with_status(**statuses), WINDOW)


def test_withdrawn_no_show_is_not_marked_absent():
    """Alejandro Aoki never joined; withdrawn, he should not count as absent."""
    report = _score_with(**{"alejandro.aoki@example.com": "Withdrawn"})
    aoki = find(report, "alejandro.aoki@example.com")
    assert aoki.status == "Not scored - Withdrawn"
    assert "not scored" in aoki.note_text.lower()
    assert aoki.needs_review is False


def test_removed_fellow_uses_the_rosters_own_wording():
    report = _score_with(**{"alejandro.aoki@example.com": "Removed"})
    assert find(report, "alejandro.aoki@example.com").status == "Not scored - Removed"


def test_withdrawn_fellow_who_attended_is_flagged_not_hidden():
    """A withdrawn Fellow turning up usually means the roster is stale."""
    report = _score_with(**{"reza.ahmadi@example.com": "Withdrawn"})
    reza = find(report, "reza.ahmadi@example.com")
    assert reza.status == "Not scored - Withdrawn"
    assert reza.minutes_present > 0          # their time is still reported
    assert reza.needs_review is True
    assert "roster is up to date" in reza.note_text


def test_not_scored_fellows_are_excluded_from_the_counts():
    baseline = _score_with()
    report = _score_with(**{
        "alejandro.aoki@example.com": "Withdrawn",   # a no-show
        "reza.ahmadi@example.com": "Removed",        # an attendee
    })
    assert report.not_scored == 2
    assert report.absent == baseline.absent - 1      # the no-show left the tally
    assert report.present == baseline.present - 1    # so did the attendee
    assert len(report.results) == len(baseline.results)   # nobody dropped


def test_blank_enrollment_status_is_treated_as_active():
    report = _score_with(**{"reza.ahmadi@example.com": ""})
    assert find(report, "reza.ahmadi@example.com").status == PRESENT


def test_enrollment_status_is_carried_into_the_results():
    report = _score_with(**{"alejandro.aoki@example.com": "Withdrawn"})
    assert find(report, "alejandro.aoki@example.com").enrollment_status == "Withdrawn"
    assert find(report, "reza.ahmadi@example.com").enrollment_status == "Active"
