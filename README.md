# Attendance Tracker

Turns a Zoom participant report into scored attendance on the Fellow roster
Google Sheet, so nobody has to compare two lists by hand.

- **Live app:https://attendance-tracker-ccv2.onrender.com/**
- **Roster Sheet:https://docs.google.com/spreadsheets/d/1JTHk-3pfDmLto0Vmw77uYw1M1ONF2t0YBdcXh3DfNfg/edit?pli=1&gid=2110501583#gid=2110501583**

---

## Part 1 — For program staff

### What it does

You upload the attendance file Zoom gives you. The app works out how long each
Fellow was actually in the session, compares that to the roster, and writes a
new tab into your Google Sheet with everyone's status.

### Every session
1. **Get the attendance file from Zoom.** In Zoom, go to **Reports → Usage**, find your
   meeting, click the number in the **Participants** column, then **Export**.
   You'll get a `.csv` file.
2. **Open the web app** and upload attendance file and roster sheets.
3. **Check the session times.** They're pre-filled for a 5:00–6:30 PM session
   and the date is read from the file. Change them if this session was
   different.
5. **Click "Check attendance."** Nothing is saved yet.
   - *If your roster has more than one cohort*, you'll be asked which one this
     session was for. Pick it and continue. Rosters with a single cohort skip
     this step entirely.
6. **Look over the results.** The page shows how many Fellows were present and
   absent, flags anything worth a second look, and lists anyone in the Zoom file
   who isn't on your roster.
7. **Click "Save to Google Sheet."** A tab called `Attendance 2026-08-26` (with
   your session's date) appears in your Sheet.

If you'd rather not save to the Sheet, **Download as CSV** gives you the same
results as a file.

### Reading the results

| Column | What it means |
|---|---|
| **Attendance Status** | `Present`, `Absent`, `Absent - no record` (on the roster but never appeared in the Zoom file), or `Not scored - Withdrawn` / `Not scored - Removed` (no longer on the program) |
| **Enrollment Status** | Copied straight from your roster, so you can sort or filter by it |
| **Minutes Present** | Time actually in the session, ignoring anything before the start or after the end |
| **Minutes Missed** | Session length minus the above |
| **Matched By** | `email` if we recognised their Zoom email, `name` if we had to fall back to their display name |
| **Needs Review** | `Yes` means we made a judgement call — worth a quick look |
| **Notes** | Plain-English explanation of any judgement call |

**Fellows who have left the program** are never marked absent. If your roster
says `Withdrawn` or `Removed`, they're listed with that status instead, and they
don't count towards your present or absent totals. If one of them *did* turn up,
their minutes are still shown and they're flagged for review — that usually
means the roster needs updating.

---

## Part 2 — Architecture and decisions

### How it's put together

```
Browser ──upload CSV──> FastAPI ──reads roster──> Google Sheets API
                           │                       (service account)
                           ├── parse + score  (pure Python, no I/O)
                           ├── preview page   (nothing written yet)
                           └── on confirm ──writes results tab──> Sheet
```

| Piece | Choice | Why |
|---|---|---|
| Web framework | FastAPI + Jinja templates, server-rendered | No build step, no JavaScript framework; the whole UI is three pages |
| Hosting | Render free tier | Free public URL, deploys from a repo, `render.yaml` included |
| Google access | Service account, not end-user OAuth | Staff never see a consent screen and there's no token to refresh. The cost is one manual share step, done once during render.com setup |
| Scoring | Pure functions in `app/scoring.py` + `app/attendance.py` | No network or credentials needed to test the part that decides pass/fail — 62 tests run in under a second |

```
app/
  models.py      dataclasses shared everywhere (no I/O)
  scoring.py     Zoom CSV parsing; interval union clipped to the session window
  matching.py    name normalisation; roster lookup ladder
  attendance.py  orchestration: rows + roster + window -> results
  sheets.py      the only module that talks to Google
  main.py        HTTP routes
  templates/     four pages
tests/           62 tests, Google stubbed out
sample_output/   what the sample data produces
```

### The decision that matters most: how minutes are counted

**Zoom's own `Duration (Minutes)` column is not usable as-is,** for three
reasons, all present in the sample data:

1. **It counts time outside the session.** Most Fellows joined around 4:50 PM
   for a 5:00 PM start, so Zoom credits them ~10 minutes that don't count.
2. **It double-counts people on two devices.** Nia Okafor appears twice —
   phone and laptop, overlapping — for 69 + 71 = **140 minutes of a 90-minute
   session**.
3. **It ignores the gaps between rejoins.** Jonah Whitaker has six rows totalling
   82 minutes, which looks like a pass. But the five gaps between them add up to
   ~11 minutes, so he actually missed more than the allowance.

So instead: **clip each join/leave pair to the session window, merge overlapping
intervals, and total the union.** One operation handles all three problems.

### Edge cases found in the sample data, and what was done

**Identity**

| What we found | Decision | Why |
|---|---|---|
| Eight Fellows have scrambled display names but correct emails (`Astrid Abobtt`, `Rebecca Daltno`, `Gabriela Corbtet`, `Lin Noavk`, `Adaeze Fjuimoto`, `Jesica Delgado`, `Maya Fosrythe`, `Paulo Ganonn`) | **Match on email first; treat the display name as decoration** | Zoom takes the email from the signed-in account; the display name is whatever the person typed. Email-first resolves all eight with no fuzzy matching at all |
| Roster stores one email in mixed case (`Leilani.Akhtar@Example.com`) | Compare emails case-insensitively | Email local parts are technically case-sensitive, but no real roster means it that way |
| `Anika Petrov` joined on a personal account (`anika.p.music@gmail.com`) for the first half, then on her roster account for the second | Match the personal-email row **by name**, then merge both stretches | Scored separately she has 42 and 49 minutes and fails twice. Merged she has 89.1 and passes. Flagged for review |
| `🌱 Lena Park 🌱` joined from `lenapark94@gmail.com`, not on the roster | Strip emoji, match by name, flag for review | Same situation as above |
| Device names with no email: `Pixel 9`, `iPad (2)`, `Galaxy Tab A` | **Don't guess.** List them separately with their minutes | There is genuinely no information tying a tablet to a person. A wrong guess is worse than an honest "you need to look at this" |
| People with no email but a real name: `Mike Sandoval`, `Rebeca Kowalsky`, `Jose Ramirez`, `Maria Vasquez` | Match by normalised name, flag for review | Maria Vasquez also has a separate emailed row; the two stretches merge to a full 90 minutes |
| `NGUYEN THANH` — all caps, surname first | Try the reversed word order too | Common in Vietnamese name order; a plain match would have marked a present Fellow absent |
| Display-name noise: `(iPhone)`, `(she/her)`, `Prof.`, `Tomás` (accent), `Priya  Shah` (double space) | Normalise: strip parentheticals, pronouns, titles, accents, punctuation; collapse whitespace | All of these have valid emails anyway, so this only matters as a fallback |
| Abbreviated names: `Iva O.`, `Cam A.`, `Katie R`, `Kai U.` | Suggest a match **for a human to confirm**, never auto-apply | All four have valid emails, so this path rarely fires — but when it does, the app proposes rather than decides |
| Staff and guests: `Dana Whitfield (New Roots)`, `Prof. Leila Nassar (Guest Speaker)`, `Sam Torres (ACLU - guest)` | Not scored as Fellows; listed separately | They're not on the roster, which is the correct signal |

**Fellows who have left the program**

The roster's `Enrollment Status` column carries `Active`, `Withdrawn` and
`Removed`. Scoring all three identically would mark a withdrawn Fellow **absent
for a session they were never expected at** — a quiet error that inflates the
absence count and could follow someone into a program record.

| Roster status | Attended? | Result | Why |
|---|---|---|---|
| `Active` | yes | `Present` / `Absent` | Normal scoring |
| `Active` | no | `Absent - no record` | Expected, didn't come |
| `Withdrawn` / `Removed` | no | `Not scored - Withdrawn` | Not expected; excluded from both totals |
| `Withdrawn` / `Removed` | **yes** | `Not scored - Removed`, **flagged**, minutes still shown | Someone attending after leaving usually means the roster is stale — worth surfacing, not hiding |

**Cohorts**

A roster can hold several cohorts, and a session normally belongs to one of
them. Scoring the whole roster every time would mark an entire cohort absent
for a session they were never invited to. Hence the app reads the distinct values in the `Cohort` column and, **only when there
is more than one**, asks which cohort the session was for before scoring
anything. 

### Results on the sample data

Scored against a 270-Fellow roster reconstructed from the sample CSV
(`sample_output/roster_used_for_sample_output.csv` — the real roster lives in
your Google Sheet):

- **230 present**, 40 absent (4 of those never joined)
- **7 flagged for review** — the name-matched Fellows described above
- **7 not on the roster** — 3 staff/guests, 4 unidentifiable devices
- 314 of 316 rows used; 2 excluded as belonging to another session
- **0 not scored** — every Fellow in this fixture is `Active`. Marking four of
  them `Withdrawn`/`Removed` moves the totals to 228 present, 38 absent,
  4 not scored, with the two who attended flagged for review
- **One cohort**, so the cohort question never appears. Splitting the fixture
  into two cohorts (159 and 111 active Fellows) makes it appear and scopes the
  results to the chosen half

### Assumptions
1. **A session belongs to one cohort.** When a roster holds several cohorts the
   app asks which one, and scores only that group. A session genuinely shared
   between two cohorts is handled by the "Every cohort" option, but there's no
   way to select *two of three* — that would need checkboxes rather than a
   single choice, and no such session exists yet.
2. **The roster is the first tab** of the Sheet, with a header row containing a
   name column and an email column. Other column names are tolerated.
3. **Emails identify people.** Two Fellows sharing an email would collide; the
   sample data has none.
4. **The session window is the same for everyone.** No per-Fellow excused lateness.
5. **Zoom's export format.** Several column spellings are accepted, and a
   meeting-summary preamble is skipped, but a heavily reformatted export would
   need adjusting.

---

## Part 3 — Running and deploying

### Locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export GOOGLE_SERVICE_ACCOUNT_JSON='{"type":"service_account",...}'
uvicorn app.main:app --reload
```

Then open http://127.0.0.1:8000.

### Tests

```bash
pip install pytest
python -m pytest tests/ -q      # 62 tests, no network or credentials needed
```

### Creating the Google service account

1. In the [Google Cloud console](https://console.cloud.google.com), create a
   project.
2. **APIs & Services → Library →** enable the **Google Sheets API**.
3. **APIs & Services → Credentials → Create credentials → Service account.**
4. Open the new service account → **Keys → Add key → JSON**. A file downloads.
5. Share your roster Sheet with the `client_email` from that file, as **Editor**.

### Deploying to Render

1. Push this directory to a Git repository.
2. In Render, **New → Web Service**, point it at the repo. `render.yaml`
   supplies the build and start commands.
3. Under **Environment**, add `GOOGLE_SERVICE_ACCOUNT_JSON` and paste the entire
   contents of the JSON key file as the value.
4. Optionally add `DEFAULT_SHEET_URL` to pre-fill the Sheet link on the form.
5. Deploy. Render gives you a public `.onrender.com` URL.

The free tier sleeps after ~15 minutes idle, so the first request afterwards
takes up to a minute.
