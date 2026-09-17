import streamlit as st
import pandas as pd
import re
import math
import csv
from datetime import datetime
from dateutil.relativedelta import relativedelta
import os
import io
import base64
import difflib
import requests

class SelectionsEngine:
    def __init__(self):
        self.quad_ranges = []
        self.config = {}

    def get_season_bounds(self, date):
        """Returns the fixed (start, end) of the season a date falls into."""
        year = date.year
        if 1 <= date.month <= 4:
            return datetime(year, 1, 1), datetime(year, 4, 30)
        elif 5 <= date.month <= 8:
            return datetime(year, 5, 1), datetime(year, 8, 31)
        else:
            return datetime(year, 9, 1), datetime(year, 12, 31)

    def get_prev_season_end(self, start_date):
        """Returns the end date of the previous fixed season."""
        return start_date - relativedelta(days=1)
    
    def calculate_configuration(self, mode, intl_date_str, tournament_dates=[], ignore_q5_push=False):
        try:
            intl_date = datetime.strptime(intl_date_str, "%d.%m.%Y")
            offset = 6 if mode == "WSC" else 3
            cutoff_date = intl_date - relativedelta(months=offset)

            q5_start, q5_end = self.get_season_bounds(cutoff_date)

            tours_in_q5 = [d for d in tournament_dates if q5_start <= d <= cutoff_date]

            is_first_month = (cutoff_date.month in [1, 5, 9])
            # PDF p.3 exception: exactly one tournament in the first month of the quadrimester
            # gets MERGED into the previous quadrimester (not dropped).
            merge_stray_tournament = is_first_month and len(tours_in_q5) == 1

            if ignore_q5_push:
                # Live-view override: skip the p.3 "no tournament yet -> push back" and
                # "stray tournament -> merge" rules entirely, and just anchor Q5 to the
                # cutoff date's natural season. Lets an admin see today's live WAR as
                # results trickle in, without the official-selection pushback logic
                # making Q5 jump back a whole quadrimester while it's still empty.
                merge_stray_tournament = False
                actual_q5_end = q5_end
            elif len(tours_in_q5) == 0 or merge_stray_tournament:
                actual_q5_end = self.get_prev_season_end(q5_start)
            else:
                actual_q5_end = q5_end

            quads = []
            curr_end = actual_q5_end
            weights = [2.0, 1.75, 1.50, 1.25, 1.0] # PDF page 3

            for i in range(5, 0, -1):
                q_start, q_end = self.get_season_bounds(curr_end)
                if i == 5 and merge_stray_tournament:
                    # Widen Q5's end past its natural season boundary to cutoff_date so the
                    # lone stray tournament's date still falls inside Q5's matching range,
                    # instead of matching no quadrimester and being silently dropped.
                    q_end = cutoff_date
                quads.append({
                    "quad": i,
                    "start": q_start,
                    "end": q_end,
                    "weight": weights[5-i]
                })
                curr_end = self.get_prev_season_end(q_start)

            config = {
                "mode": mode,
                "intl_date": intl_date,
                "cutoff_date": cutoff_date,
                "req_games": 80 if mode == "WSC" else 50,
                "req_tours": 5 if mode == "WSC" else 3,
                "req_recent": 2 if mode == "WSC" else 1,
                "min_quads": 3,
                "min_war": 800,
                "ignore_q5_push": ignore_q5_push
            }
            
            return config, quads
        except Exception as e:
            st.error(f"Configuration Error: {str(e)}")
            return None, None

    def detect_inactivity(self, history, cutoff_date):
        """PDF p.6: a player idle for more than a year is 'inactive' and ineligible.
        On return, their rating is restored but they need 50 rated games since
        resumption before being reconsidered. `history` is every detectable
        appearance for this player across ALL uploaded files (any date, provisional
        or not) - not just the ones inside the current WAR window - since we need
        their full timeline to spot the gap."""
        if not history:
            return {"status": "no_data", "remark": "", "override_ineligible": False, "games_since_resumption": None}

        sorted_h = sorted(history, key=lambda x: x['date'])

        resumption_idx = 0
        for i in range(1, len(sorted_h)):
            gap_days = (sorted_h[i]['date'] - sorted_h[i-1]['date']).days
            if gap_days > 365:
                resumption_idx = i

        last_played = sorted_h[-1]['date']
        trailing_gap = (cutoff_date - last_played).days > 365

        if trailing_gap:
            return {
                "status": "inactive",
                "remark": f"Inactive - no tournaments played since {last_played:%Y-%m-%d} (>1 year). Ineligible for selection.",
                "override_ineligible": True,
                "games_since_resumption": 0,
            }

        had_gap = resumption_idx > 0
        if had_gap:
            resumption_date = sorted_h[resumption_idx]['date']
            games_since = sum(h['games'] for h in sorted_h[resumption_idx:] if not h.get('provisional'))
            if games_since < 50:
                remaining = 50 - games_since
                return {
                    "status": "resuming",
                    "remark": (f"Inactive for more than a year before {resumption_date:%Y-%m-%d}. "
                               f"WAR considered only after 50 games played since activeness "
                               f"({games_since}/50 played, {remaining} more needed). Ineligible until then."),
                    "override_ineligible": True,
                    "games_since_resumption": games_since,
                }
            return {
                "status": "cleared",
                "remark": f"Previously inactive (gap ending {resumption_date:%Y-%m-%d}); {games_since} games played since resumption - restriction cleared.",
                "override_ineligible": False,
                "games_since_resumption": games_since,
            }

        return {"status": "active", "remark": "", "override_ineligible": False, "games_since_resumption": None}

    def parse_tournament_file(self, content):
        lines = content.splitlines()
        t_date, t_name = None, "Unknown Tournament"
        
        for line in lines[:5]:
            date_match = re.search(r'(\d{2}\.\d{2}\.\d{4})', line)
            if date_match:
                t_date = datetime.strptime(date_match.group(1), "%d.%m.%Y")
                t_name = line.split(date_match.group(1))[-1].strip()
                break
        
        if not t_date: return None

        players_found = []
        warnings = []
        current_section_games = 0

        for line_no, line in enumerate(lines, start=1):
            raw_line = line
            line = line.strip()
            if not line: continue

            game_header = re.search(r'(\d+)\s+games', line.lower())
            if game_header:
                current_section_games = int(game_header.group(1))
                continue

            if re.match(r'^\d+\s+', line):
                numeric_blocks = re.findall(r'\(?\s*[\d\-+.]+\s*\)?', line)
                if len(numeric_blocks) < 2:
                    warnings.append(f"Line {line_no}: looked like a player row but only found "
                                     f"{len(numeric_blocks)} numeric field(s) - skipped: \"{line}\"")
                    continue

                try:
                    # A rating shown in parentheses, e.g. "( 900)", is the standard notation
                    # for a provisional (not-yet-fully-rated) result. PDF p.2 excludes these
                    # from WAR entirely.
                    is_provisional = '(' in numeric_blocks[-1] or ')' in numeric_blocks[-1]
                    new_rating = int(float(numeric_blocks[-1].replace('(', '').replace(')', '').strip()))

                    old_rating = 0
                    if len(numeric_blocks) >= 5:
                         try:
                            old_rating = int(float(numeric_blocks[-3].replace('(', '').replace(')', '').strip()))
                         except: pass

                    name_part = re.sub(r'^\s*\d+\s+[\d\-+.]+\s+[\d\-+*&.]+', '', raw_line)
                    name_part = re.sub(r'[\d\-+*&\(\)\s.]+$', '', name_part)
                    name_part = name_part.strip().strip('*&').strip()

                    if name_part:
                        players_found.append({
                            "name": name_part,
                            "old_rating": old_rating,
                            "new_rating": new_rating,
                            "games": current_section_games,
                            "provisional": is_provisional
                        })
                    else:
                        warnings.append(f"Line {line_no}: parsed ratings but no player name remained "
                                         f"- skipped: \"{line}\"")
                except Exception as e:
                    warnings.append(f"Line {line_no}: could not parse ({e}) - skipped: \"{line}\"")
                    continue

        return {
            "name": t_name,
            "date": t_date,
            "players": players_found,
            "warnings": warnings
        }


def round_half_up(x):
    """PDF: 'Weighted averages will be rounded off to the nearest integer.' Python's
    built-in round() uses banker's rounding (round-half-to-even), which can disagree
    with plain round-half-up exactly on .5 boundaries."""
    return int(math.floor(x + 0.5))


def compute_war(history):
    total_weight = sum(h['Weight'] for h in history)
    total_weighted = sum(h['WeightedVal'] for h in history)
    war_precise = total_weighted / total_weight if total_weight > 0 else 0
    return round_half_up(war_precise), war_precise


def threshold_headers(conf):
    return {
        "Total Games": f"Total Games (Req ≥{conf['req_games']})",
        "Tournaments": f"Tournaments (Req ≥{conf['req_tours']})",
        "Quads": f"Quadrimesters (Req ≥{conf['min_quads']})",
        "Majors": "Major Tournaments (Req ≥1)",
        "Recent": f"Recent Activity (Req ≥{conf['req_recent']})",
    }


def build_leaderboard_rows(players_db, full_history_db, inactivity_map, conf):
    """One row per detectable player (players_db ∪ full_history_db), so a player who
    is inactive / has no in-window non-provisional results still shows up with a
    Remarks explanation instead of silently vanishing from the report."""
    rows = []
    all_names = set(players_db.keys()) | set(full_history_db.keys())

    for name in all_names:
        data = players_db.get(name)
        inact = inactivity_map.get(name, {"override_ineligible": False, "remark": ""})

        if data:
            war, war_precise = compute_war(data['history'])
            current_rating = data['current_rating']
            total_games = data['total_games']
            tournaments = data['tournaments']
            quads_count = len(data['quads'])
            majors = data['major_count']
            recent = data['recent_count']
        else:
            war, war_precise = 0, 0.0
            current_rating, total_games, tournaments, quads_count, majors, recent = 0, 0, 0, 0, 0, 0

        base_eligible = (war >= conf['min_war'] and
                          total_games >= conf['req_games'] and
                          tournaments >= conf['req_tours'] and
                          quads_count >= conf['min_quads'] and
                          majors >= 1 and
                          recent >= conf['req_recent'])
        eligible = base_eligible and not inact.get("override_ineligible", False)

        remark = inact.get("remark", "")
        if not data and not remark:
            remark = "No qualifying (non-provisional, in-window) tournament results found."

        rows.append({
            "Player Name": name,
            "WAR": war,
            "Current Rating": current_rating,
            "WAR Precise": round(war_precise, 2),
            "Quads": quads_count,
            "Tournaments": tournaments,
            "Total Games": total_games,
            "Majors": majors,
            "Recent": recent,
            "Status": "QUALIFIED" if eligible else "INELIGIBLE",
            "Remarks": remark
        })

    rows.sort(key=lambda r: (r["WAR"], r["Current Rating"], r["WAR Precise"]), reverse=True)
    return rows


def find_similar_player_names(names, ratio_threshold=0.85):
    """Flags likely-duplicate player names so an admin can catch a typo or inconsistent
    spelling before it silently fragments one person's results across two 'players' -
    each with their own (wrong) WAR. Names identical except for case/spacing are flagged
    as an exact match; anything else above ratio_threshold is flagged as similar spelling
    for manual review. Returns a list of (name_a, name_b, kind) tuples."""
    distinct_names = sorted(set(names))
    normalized = {n: re.sub(r'\s+', ' ', n).strip().lower() for n in distinct_names}

    pairs = []
    for i in range(len(distinct_names)):
        for j in range(i + 1, len(distinct_names)):
            a, b = distinct_names[i], distinct_names[j]
            if normalized[a] == normalized[b]:
                pairs.append((a, b, "Exact match (case/spacing only)"))
            else:
                ratio = difflib.SequenceMatcher(None, normalized[a], normalized[b]).ratio()
                if ratio >= ratio_threshold:
                    pairs.append((a, b, "Similar spelling"))
    return pairs


TOURNAMENT_ARCHIVE_DIR = "tournament files"
ARCHIVE_PASSWORD = st.secrets.get("ARCHIVE_PASSWORD")


def _read_archive_file(fpath):
    try:
        with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except OSError:
        return None


GITHUB_REPO = "imethdesilva/WAR_Calculator"
GITHUB_BRANCH = "main"


def push_archive_file_to_github(fpath, commit_message):
    """Commits fpath's current on-disk content straight to GITHUB_REPO via GitHub's
    Contents API (not the git CLI), so it works identically whether this app is run
    locally or in a hosted container that has no git identity or push credentials
    configured (e.g. a Streamlit Community Cloud deploy only has read access to the
    repo checkout). Requires GITHUB_TOKEN in st.secrets - a token with write access to
    this repo's contents. Returns False if the file already matches what's on GitHub
    (nothing to commit), True if a new commit was made."""
    token = st.secrets["GITHUB_TOKEN"]
    repo_path = fpath.replace(os.sep, "/").lstrip("/")

    with open(fpath, "rb") as f:
        encoded_content = base64.b64encode(f.read()).decode("ascii")

    api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{repo_path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "WAR-Calculator-App",
    }

    get_resp = requests.get(api_url, headers=headers, params={"ref": GITHUB_BRANCH}, timeout=30)
    existing_sha = None
    if get_resp.status_code == 200:
        existing = get_resp.json()
        existing_sha = existing.get("sha")
        if existing.get("content", "").replace("\n", "") == encoded_content:
            return False
    elif get_resp.status_code != 404:
        get_resp.raise_for_status()

    payload = {"message": commit_message, "content": encoded_content, "branch": GITHUB_BRANCH}
    if existing_sha:
        payload["sha"] = existing_sha

    put_resp = requests.put(api_url, headers=headers, json=payload, timeout=30)
    put_resp.raise_for_status()
    return True


def get_archive_structure(engine, base_dir=TOURNAMENT_ARCHIVE_DIR):
    """Returns an ordered list of (year, [filenames]) tuples for whatever year
    subfolders exist under base_dir - years newest-first, and files within each year
    sorted by their parsed tournament date, latest first (undated files sort last,
    alphabetically). This folder holds real tournament results; on a checkout that
    doesn't have it, this returns an empty list."""
    structure = []
    if not os.path.isdir(base_dir):
        return structure

    year_dirs = [e for e in os.listdir(base_dir) if os.path.isdir(os.path.join(base_dir, e))]
    for year in sorted(year_dirs, reverse=True):
        year_path = os.path.join(base_dir, year)
        dated_files = []
        for fname in os.listdir(year_path):
            if not fname.lower().endswith(".txt"):
                continue
            content = _read_archive_file(os.path.join(year_path, fname))
            data = engine.parse_tournament_file(content) if content is not None else None
            dated_files.append((fname, data['date'] if data else None))

        dated_files.sort(key=lambda fd: (fd[1] is None, -fd[1].toordinal() if fd[1] else 0, fd[0]))
        structure.append((year, [fname for fname, _ in dated_files]))

    return structure


def build_tournament_history(engine, base_dir=TOURNAMENT_ARCHIVE_DIR):
    """Parses every file in the archive and returns one summary row per tournament,
    sorted with the latest tournament first (so later years/dates appear first)."""
    rows = []
    for year, files in get_archive_structure(engine, base_dir):
        for fname in files:
            content = _read_archive_file(os.path.join(base_dir, year, fname))
            if content is None:
                continue

            data = engine.parse_tournament_file(content)
            if not data:
                continue

            distinct_players = {p['name'] for p in data['players']}
            game_counts = sorted({p['games'] for p in data['players']})

            rows.append({
                "Date": data['date'],
                "Tournament Name": data['name'],
                "Year Folder": year,
                "Players": len(distinct_players),
                "Division Sizes (games)": ", ".join(str(g) for g in game_counts) if game_counts else "-",
                "Source File": fname,
            })

    rows.sort(key=lambda r: r["Date"], reverse=True)
    return rows


def rename_player_across_archive(engine, old_names, new_name, base_dir=TOURNAMENT_ARCHIVE_DIR):
    """Merges two (or more) name spellings into one canonical name by rewriting every
    archived tournament file that contains any of old_names, so future uploads treat
    them as a single player instead of silently splitting their WAR. Matches whole
    names only (not as a substring of some other name) via word-boundary-style
    lookaround. Returns a list of (year, fname, occurrences_replaced) for files that
    were actually changed; writes those files to disk immediately."""
    changed = []
    patterns = [re.compile(r'(?<![A-Za-z])' + re.escape(old) + r'(?![A-Za-z])')
                for old in old_names if old != new_name]
    if not patterns:
        return changed

    for year, files in get_archive_structure(engine, base_dir):
        for fname in files:
            fpath = os.path.join(base_dir, year, fname)
            content = _read_archive_file(fpath)
            if content is None:
                continue

            new_content = content
            total_count = 0
            for pattern in patterns:
                new_content, count = pattern.subn(new_name, new_content)
                total_count += count

            if total_count > 0:
                with open(fpath, "w", encoding="utf-8") as f:
                    f.write(new_content)
                changed.append((year, fname, total_count))

    return changed


def generate_full_report_csv(rows_sorted, players_db, conf, mode_label):
    buf = io.StringIO()
    headers_map = threshold_headers(conf)

    buf.write(f"NATIONAL SELECTIONS REPORT - {mode_label}\n")
    buf.write(f"Cutoff Date,{conf['cutoff_date'].strftime('%Y-%m-%d')}\n")
    buf.write(f"International Event Date,{conf['intl_date'].strftime('%Y-%m-%d')}\n")
    buf.write(f"Min WAR,{conf['min_war']}\n\n")

    writer = csv.writer(buf, lineterminator='\n')

    buf.write("SELECTION LEADERBOARD\n")
    lb_df = pd.DataFrame(rows_sorted)
    lb_df.insert(0, "Rank", range(1, len(lb_df) + 1))
    lb_df = lb_df.rename(columns=headers_map)
    # lineterminator='\n' avoids pandas' default '\r\n' colliding with the plain '\n'
    # used elsewhere in this buffer and producing malformed '\r\r\n' blank rows.
    lb_df.to_csv(buf, index=False, lineterminator='\n')
    buf.write("\n\n")

    buf.write("INDIVIDUAL PLAYER BREAKDOWN\n\n")
    for row in rows_sorted:
        name = row["Player Name"]
        writer.writerow(["Player", name])
        data = players_db.get(name)
        if data:
            h_df = pd.DataFrame(data["history"])
            h_df = h_df.sort_values(by="Date")
            h_df.to_csv(buf, index=False, lineterminator='\n')
            war, war_precise = compute_war(data['history'])
            tw = sum(h['Weight'] for h in data['history'])
            twr = sum(h['WeightedVal'] for h in data['history'])
            buf.write(f"SUMMARY: Total Weight={tw:.2f}, Total Weighted Value={twr:.2f}, "
                      f"Total Games={data['total_games']}, Calculated WAR={war}\n")
        else:
            buf.write("No qualifying (non-provisional, in-window) tournament history found.\n")
        if row["Remarks"]:
            # Remarks can contain commas, so this must go through csv.writer (not an
            # f-string) or an unescaped comma would silently shift/break the row.
            writer.writerow(["Remark", row["Remarks"]])
        buf.write("\n\n")

    return buf.getvalue()


PYTHONANYWHERE_STATIC_CSV_PATH = "/mysite/static/csv"


def push_csv_to_pythonanywhere(csv_text, filename):
    """Uploads csv_text to <PYTHONANYWHERE_STATIC_CSV_PATH>/<filename> in the user's
    PythonAnywhere account via the Files API, overwriting whatever is already
    published there. Requires PYTHONANYWHERE_USERNAME and PYTHONANYWHERE_API_TOKEN
    in st.secrets (Account > API Token on PythonAnywhere)."""
    username = st.secrets["PYTHONANYWHERE_USERNAME"]
    token = st.secrets["PYTHONANYWHERE_API_TOKEN"]

    dest_path = f"/home/{username}{PYTHONANYWHERE_STATIC_CSV_PATH}/{filename}"
    url = f"https://www.pythonanywhere.com/api/v0/user/{username}/files/path{dest_path}"

    response = requests.post(
        url,
        headers={"Authorization": f"Token {token}"},
        files={"content": (filename, csv_text.encode('utf-8'), "text/csv")},
        timeout=30,
    )
    response.raise_for_status()


# UI
st.set_page_config(page_title="National Selections Dashboard", layout="wide")

st.markdown("""
    <style>
    /* 1. Metric Card Styling: Professional contrast for Dark and Light modes */
    div[data-testid="stMetric"] {
        background-color: var(--secondary-background-color);
        border: 1px solid var(--border-color);
        padding: 15px;
        border-radius: 10px;
        box-shadow: 0 2px 4px rgba(0,0,0,0.1);
    }
    div[data-testid="stMetricValue"] > div { color: var(--text-color) !important; font-weight: 700; }
    div[data-testid="stMetricLabel"] > div { color: var(--text-color); opacity: 0.8; font-weight: 600; }

    /* 2. Professional Buttons and Tabs */
    div.stButton > button:first-child { 
        background-color: #004a99; 
        color: white; 
        border-radius: 5px; 
        width: 100%; 
        font-weight: bold; 
        border: none; 
    }
    .stTabs [data-baseweb="tab-list"] { gap: 24px; }
    .stTabs [data-baseweb="tab"] { font-weight: 600; }

    /* 3. Global Centering: Applied to standard Tables and modern DataFrames */
    [data-testid="stTable"] th, 
    [data-testid="stTable"] td,
    [data-testid="stDataFrame"] th,
    [data-testid="stDataFrame"] [data-testid="styled-table-cell"] {
        text-align: center !important;
    }

    /* 4. Individual Player Audit Summary Box: Short width and Left-aligned content */
    .summary-container {
        width: 400px;
    }
    
    /* Overrides global centering specifically inside the summary box */
    .summary-container [data-testid="stTable"] td {
        text-align: left !important;
    }
    </style>
    """, unsafe_allow_html=True)

if 'engine' not in st.session_state:
    st.session_state.engine = SelectionsEngine()
    st.session_state.players_db = {}
    st.session_state.full_history_db = {}
    st.session_state.inactivity_map = {}
    st.session_state.processed_files = False
    st.session_state.sorted_leaderboard_names = []
    st.session_state.uploaded_tournament_dates = []
    st.session_state.upload_warnings = {}
    st.session_state.archive_unlocked = False

with st.sidebar:
    st.title("Administrative Panel")
    selected_mode = st.selectbox("Tournament Classification", ["WSC", "WYSC"])
    event_date = st.text_input("International Event Date (DD.MM.YYYY)", value="15.10.2025")
    ignore_q5_push = st.toggle(
        "Ignore Q5 Push (Live View)",
        value=False,
        help="Official selection rules push Q5 back a full quadrimester when it's still empty "
             "(PDF p.3). Turn this on to instead always anchor Q5 to the cutoff date's natural "
             "quadrimester, so you can watch live WAR update as new results come in, ahead of "
             "the official cutoff determination."
    )

    if st.button("Initialize Selection Window"):
        # Reuse whatever tournament dates are already known (from a prior file
        # upload) so this preview doesn't wrongly assume "no tournaments yet"
        # and push Q5 back a full quadrimester when real data says otherwise.
        config, quads = st.session_state.engine.calculate_configuration(
            selected_mode, event_date,
            tournament_dates=st.session_state.uploaded_tournament_dates,
            ignore_q5_push=ignore_q5_push
        )
        if config:
            st.session_state.config = config
            st.session_state.quad_ranges = quads
            if ignore_q5_push:
                st.warning("Live View active: Q5 push-back rule is disabled. This is for monitoring "
                           "current-form WAR only, not for official selection determinations.")
            elif not st.session_state.uploaded_tournament_dates:
                st.info("Preview only (no tournament files uploaded yet) - Q5 is assumed empty per the PDF's "
                        "'no tournament held' rule until you upload and process real results.")
            st.success("Configuration Validated")

    st.markdown("---")
    st.subheader("Data Ingestion")
    uploaded_files = st.file_uploader("Upload Tournament Files (.txt)", accept_multiple_files=True)

    if uploaded_files:
        if st.button("Process Tournament Results"):

            all_tour_dates = []
            parsed_tournament_objects = []
            upload_warnings = {}

            for f in uploaded_files:
                content = f.read().decode('utf-8', errors='ignore')
                data = st.session_state.engine.parse_tournament_file(content)
                if data:
                    all_tour_dates.append(data['date'])
                    parsed_tournament_objects.append(data)
                    if data.get('warnings'):
                        upload_warnings[f.name] = data['warnings']

            if not all_tour_dates:
                st.error("No valid tournament data found in uploaded files.")
                st.stop()

            config, quads = st.session_state.engine.calculate_configuration(
                selected_mode,
                event_date,
                tournament_dates=all_tour_dates,
                ignore_q5_push=ignore_q5_push
            )

            if config:
                st.session_state.config = config
                st.session_state.quad_ranges = quads

                db = {}
                # Every detectable player across ALL uploaded files, any date, provisional
                # or not - used only to detect >1yr inactivity gaps (PDF p.6), never for WAR math.
                full_history = {}

                for data in parsed_tournament_objects:

                    q_info = next((q for q in st.session_state.quad_ranges
                                 if q['start'] <= data['date'] <= q['end']), None)

                    file_summary = {}
                    for p in data['players']:
                        name = p['name']
                        if name not in file_summary:
                            file_summary[name] = {
                                "games": 0, "old": p['old_rating'], "new": p['new_rating'],
                                "provisional": p.get('provisional', False)
                            }
                        file_summary[name]["games"] += p['games']
                        file_summary[name]["new"] = p['new_rating']
                        file_summary[name]["provisional"] = p.get('provisional', False)

                    for name, p_file_data in file_summary.items():
                        full_history.setdefault(name, []).append({
                            "date": data['date'],
                            "games": p_file_data['games'],
                            "provisional": p_file_data['provisional']
                        })

                        # PDF p.2: provisional-rated results are excluded from WAR entirely.
                        if p_file_data['provisional']:
                            continue
                        if not q_info:
                            continue

                        if name not in db:
                            db[name] = {
                                "history": [], "total_games": 0, "tournaments": 0,
                                "quads": set(), "major_count": 0, "recent_count": 0,
                                "current_rating": 0, "latest_rating_date": datetime(1900, 1, 1)
                            }

                        db[name]["history"].append({
                            "Date": data['date'].strftime('%Y-%m-%d'),
                            "Tournament": data['name'],
                            "Quad": q_info['quad'],
                            "Weight": q_info['weight'],
                            "Old Rating": p_file_data['old'],
                            "New Rating": p_file_data['new'],
                            "WeightedVal": p_file_data['new'] * q_info['weight'],
                            "Games": p_file_data['games']
                        })

                        db[name]["total_games"] += p_file_data['games']
                        db[name]["tournaments"] += 1
                        db[name]["quads"].add(q_info['quad'])

                        if data['date'] >= db[name]["latest_rating_date"]:
                            db[name]["latest_rating_date"] = data['date']
                            db[name]["current_rating"] = p_file_data['new']

                        # PDF p.5: the candidate must have personally played the full 18
                        # rounds - a file-wide "this tournament had an 18-round division
                        # somewhere" flag is not enough if the player was in a shorter one.
                        if p_file_data['games'] >= 18:
                            db[name]["major_count"] += 1

                        if q_info['quad'] >= 4:
                            db[name]["recent_count"] += 1

                inactivity_map = {
                    name: st.session_state.engine.detect_inactivity(hist, config['cutoff_date'])
                    for name, hist in full_history.items()
                }

                st.session_state.players_db = db
                st.session_state.full_history_db = full_history
                st.session_state.inactivity_map = inactivity_map
                st.session_state.uploaded_tournament_dates = all_tour_dates
                st.session_state.upload_warnings = upload_warnings
                st.session_state.processed_files = True
                st.success("Calculated WAR using Seasonal Calendar Weights")
                st.rerun()

# Main
st.title("National Scrabble Selections - WAR Calculator")
st.caption("Official Administrative System for Weighted Average Rating (WAR) Calculation")

tabs = st.tabs(["Selection Overview", "National Leaderboard", "Individual Player Audit", "Policy & Criteria",
                "Tournament Archive", "Tournament History"])

# Overview
with tabs[0]:
    if 'config' in st.session_state:
        if st.session_state.config.get('ignore_q5_push'):
            st.warning("LIVE VIEW - Q5 push-back rule is disabled. Quadrimesters are anchored to "
                       "the cutoff date's natural season regardless of whether it has any tournaments "
                       "yet. Use this to monitor current-form WAR only; turn it off for the official "
                       "selection calculation.")

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Tournament", st.session_state.config['mode'])
        c2.metric("Cutoff Date", st.session_state.config['cutoff_date'].strftime('%d %b %Y'))
        c3.metric("Min Games Req", st.session_state.config['req_games'])
        c4.metric("Recent Activity", f"{st.session_state.config['req_recent']} Tournaments")
        
        st.subheader("Quadrimester Weighting Schedule")
        q_df = pd.DataFrame(st.session_state.quad_ranges)
        q_df['start'] = q_df['start'].dt.strftime('%Y-%m-%d')
        q_df['end'] = q_df['end'].dt.strftime('%Y-%m-%d')
        st.table(q_df[['quad', 'weight', 'start', 'end']].rename(columns={'quad': 'Period', 'weight': 'Weight Factor'}))
    else:
        st.info("Awaiting Configuration. Please initialize the selection window in the sidebar.")

# Leaderboard
with tabs[1]:
    if st.session_state.processed_files:
        conf = st.session_state.config
        rows = build_leaderboard_rows(
            st.session_state.players_db,
            st.session_state.full_history_db,
            st.session_state.inactivity_map,
            conf
        )

        if rows:
            if st.session_state.upload_warnings:
                total_warnings = sum(len(w) for w in st.session_state.upload_warnings.values())
                with st.expander(f"{total_warnings} line(s) across "
                                  f"{len(st.session_state.upload_warnings)} file(s) could not be parsed "
                                  f"during the last upload - review before trusting these results",
                                  expanded=False):
                    for fname, warns in st.session_state.upload_warnings.items():
                        st.markdown(f"**{fname}**")
                        for w in warns:
                            st.caption(w)

            duplicate_pairs = find_similar_player_names([r["Player Name"] for r in rows])
            if duplicate_pairs:
                with st.expander(f"{len(duplicate_pairs)} possible duplicate player name pair(s) found - "
                                  f"a spelling mismatch silently splits one player's WAR across two rows",
                                  expanded=False):
                    dup_df = pd.DataFrame(duplicate_pairs, columns=["Name A", "Name B", "Match Type"])
                    st.dataframe(dup_df, use_container_width=True, hide_index=True)

                    st.markdown("---")
                    st.caption("Merging rewrites the chosen name in every archived tournament file on "
                               "disk (under 'tournament files/') so both spellings become one person "
                               "going forward. After merging, re-upload/reprocess the files to refresh "
                               "this leaderboard, and push the changed files to GitHub from the "
                               "Tournament Archive tab.")

                    for name_a, name_b, kind in duplicate_pairs:
                        merge_result_key = f"merge_result_{name_a}_{name_b}"

                        st.markdown(f"**{name_a}**  vs  **{name_b}**  -  {kind}")
                        merge_col1, merge_col2 = st.columns([3, 1])
                        with merge_col1:
                            canonical = st.text_input(
                                "Correct name to use for both",
                                value=name_a,
                                key=f"merge_canonical_{name_a}_{name_b}"
                            )
                        with merge_col2:
                            st.markdown("&nbsp;", unsafe_allow_html=True)
                            if st.button("Merge Names", key=f"merge_btn_{name_a}_{name_b}"):
                                canonical_name = canonical.strip()
                                if not canonical_name:
                                    st.error("Enter the correct name before merging.")
                                else:
                                    changed = rename_player_across_archive(
                                        st.session_state.engine, [name_a, name_b], canonical_name
                                    )
                                    if changed:
                                        st.session_state[merge_result_key] = {
                                            "canonical": canonical_name, "changed": changed
                                        }
                                        st.rerun()
                                    else:
                                        st.info("No occurrences of either name were found in the "
                                                "archive files.")

                        merge_result = st.session_state.get(merge_result_key)
                        if merge_result:
                            details = "; ".join(
                                f"{fname} ({year}, {count}x)" for year, fname, count in merge_result["changed"]
                            )
                            st.success(f"Merged into \"{merge_result['canonical']}\" across "
                                       f"{len(merge_result['changed'])} file(s): {details}.")
                            st.caption("Optional: push these files straight to GitHub (origin/main) now, "
                                       "instead of reviewing/pushing each one individually from the "
                                       "Tournament Archive tab.")
                            push_col, dismiss_col = st.columns(2)
                            with push_col:
                                if st.button("Push These Changes to GitHub Now",
                                             key=f"merge_push_{name_a}_{name_b}"):
                                    push_errors = []
                                    pushed_count = 0
                                    for year, fname, count in merge_result["changed"]:
                                        fpath = os.path.join(TOURNAMENT_ARCHIVE_DIR, year, fname)
                                        try:
                                            if push_archive_file_to_github(
                                                fpath,
                                                f"Merge player name to \"{merge_result['canonical']}\" "
                                                f"in {fname} ({year})"
                                            ):
                                                pushed_count += 1
                                        except (OSError, KeyError, requests.exceptions.RequestException) as e:
                                            push_errors.append(f"{fname}: {e}")
                                    if push_errors:
                                        st.error("Some files failed to push: " + "; ".join(push_errors))
                                    else:
                                        st.success(f"Pushed {pushed_count} file(s) to GitHub (origin/main).")
                                        st.session_state[merge_result_key] = None
                                        st.rerun()
                            with dismiss_col:
                                if st.button("Dismiss", key=f"merge_dismiss_{name_a}_{name_b}"):
                                    st.session_state[merge_result_key] = None
                                    st.rerun()
                        st.markdown("---")

            filter_col1, filter_col2 = st.columns(2)
            with filter_col1:
                hide_zero_war = st.checkbox("Hide players with WAR = 0", value=False)
            with filter_col2:
                hide_inactive = st.checkbox(
                    "Hide inactive players (no tournaments in >1 year)",
                    value=False,
                    help="Excludes players flagged as 'Inactive' in the Remarks column - "
                         "i.e. no tournaments played since more than a year before the cutoff "
                         "date (PDF p.6). Does not exclude players who resumed after a past gap "
                         "but haven't yet played 50 games since resumption."
                )

            omitted_names = []
            if conf['mode'] == 'WYSC':
                with st.expander("Omit players from WYSC report (age not auto-verified)", expanded=False):
                    st.caption("Age eligibility for youth events can't be verified automatically - there's "
                               "no date-of-birth data. Manually omit players here after checking their age; "
                               "they'll be dropped from both the leaderboard and their individual audit in "
                               "the exported report.")
                    omitted_names = st.multiselect(
                        "Players to omit",
                        options=[r["Player Name"] for r in rows]
                    )

            filtered_rows = rows
            if hide_zero_war:
                filtered_rows = [r for r in filtered_rows if r["WAR"] != 0]
            if hide_inactive:
                filtered_rows = [
                    r for r in filtered_rows
                    if st.session_state.inactivity_map.get(r["Player Name"], {}).get("status") != "inactive"
                ]
            if omitted_names:
                filtered_rows = [r for r in filtered_rows if r["Player Name"] not in omitted_names]

            st.session_state.sorted_leaderboard_names = [r["Player Name"] for r in filtered_rows]

            if not filtered_rows:
                st.info("No players remain after the selected filters.")
            else:
                df = pd.DataFrame(filtered_rows)
                df.insert(0, "Rank", range(1, len(df) + 1))
                df = df.rename(columns=threshold_headers(conf))
                df.index = range(1, len(df) + 1)

                def color_status(val):
                    color = '#28a745' if val == "QUALIFIED" else '#dc3545'
                    return f'color: {color}; font-weight: bold;'

                st.dataframe(df.style.map(color_status, subset=['Status']), use_container_width=True)

                report_csv = generate_full_report_csv(filtered_rows, st.session_state.players_db, conf, conf['mode'])

                export_col, push_col = st.columns(2)
                with export_col:
                    st.download_button(
                        "Export Full Selection Report (CSV)",
                        data=report_csv.encode('utf-8'),
                        file_name=f"{conf['mode']}_selection_report.csv",
                        mime='text/csv'
                    )
                with push_col:
                    if st.button("Push WAR Updates to Web"):
                        push_filename = f"{conf['mode'].lower()}.csv"
                        try:
                            push_csv_to_pythonanywhere(report_csv, push_filename)
                            st.success(f"Pushed {push_filename} live to the PythonAnywhere site.")
                        except KeyError:
                            st.error("Missing PYTHONANYWHERE_USERNAME / PYTHONANYWHERE_API_TOKEN in "
                                      ".streamlit/secrets.toml - add them (Account > API Token on "
                                      "PythonAnywhere) and restart the app.")
                        except requests.exceptions.RequestException as e:
                            st.error(f"Upload failed: {e}")
    else:
        st.warning("Upload result files in the sidebar to generate rankings.")

# Player Breakdown
with tabs[2]:
    if st.session_state.processed_files:
        player_select = st.selectbox("Search Player for Audit", sorted(st.session_state.players_db.keys()))
        if player_select:
            p_data = st.session_state.players_db[player_select]
            st.subheader(f"Participation History: {player_select}")

            p_df = pd.DataFrame(p_data["history"])
            p_df['Date_dt'] = pd.to_datetime(p_df['Date'])
            p_df = p_df.sort_values(by="Date_dt", ascending=False).drop(columns=['Date_dt'])
            p_df.index = range(1, len(p_df) + 1)
            
            st.dataframe(p_df, use_container_width=True)
            
            # Calculations for Summary
            total_w = sum(h['Weight'] for h in p_data['history'])
            total_wv = sum(h['WeightedVal'] for h in p_data['history'])
            total_g = p_data['total_games']
            calc_war, _ = compute_war(p_data['history'])

            st.markdown("### Player Record Summary")
            summary_df = pd.DataFrame({
                "Metric": ["Distinct Quadrimesters", "Aggregate Weight", "Total Weighted Value", "Cumulative Games", "Calculated WAR"],
                "Value": [len(p_data['quads']), f"{total_w:.2f}", f"{total_wv:,.2f}", p_data['total_games'], calc_war]
            })

            summary_df['Value'] = summary_df['Value'].astype(str)

            st.markdown('<div class="summary-container">', unsafe_allow_html=True)
            st.table(summary_df)
            st.markdown('</div>', unsafe_allow_html=True)

            inact = st.session_state.inactivity_map.get(player_select, {})
            if inact.get("remark"):
                if inact.get("override_ineligible"):
                    st.error(inact['remark'])
                else:
                    st.info(inact['remark'])

            # Individual Export
            indiv_buffer = io.StringIO()
            indiv_buffer.write(f"Player Name: {player_select}\n\n")
            p_df.to_csv(indiv_buffer, index=False)
            st.download_button(
                label=f"Export {player_select} Results",
                data=indiv_buffer.getvalue().encode('utf-8'),
                file_name=f"{player_select.replace(' ', '_')}_WAR.csv",
                mime='text/csv'
            )
    else:
        st.info("Awaiting data processing.")

# Info
with tabs[3]:
    st.header("National Selection Policy Summary")
    
    st.subheader("World Scrabble Championship (WSC) Selection Criteria")
    st.write("""
    Candidates seeking selection for the World Scrabble Championship (WSC) must demonstrate consistent performance and activity within 
    a 20-month evaluation window. Eligibility is predicated on completing a minimum of 80 rated games across at least five tournaments 
    spanning no fewer than three distinct quadrimesters. This participation must include at least one 18-round 'Major' event. 
    Furthermore, candidates must demonstrate current form by participating in at least two rated tournaments during the most recent 
    eight-month period (Quadrimesters 4 and 5), following a mandatory six-month buffer period prior to the international event.
    """)

    st.subheader("World Youth Scrabble Championship (WYSC) Selection Criteria")
    st.write("""
    Candidates for the World Youth Scrabble Championship (WYSC) and associated youth international events must complete a minimum of 
    50 rated games within the 20-month selection window. Eligibility requires participation in a minimum of three tournaments 
    held across at least three different quadrimesters, including one 18-round major event. To validate recent competitive standing, 
    at least one tournament must fall within the final two quadrimesters (Q4/Q5) of the window, following a three-month buffer period.
    """)
    
    st.markdown("---")
    st.subheader("Official Documentation")
    pdf_path = "Selections Criteria 2024.pdf"
    if os.path.exists(pdf_path):
        with open(pdf_path, "rb") as f:
            st.download_button("Download Official Criteria PDF", data=f, file_name="Selections_Criteria_2024.pdf")
    
    st.link_button("Read Technical Documentation on Medium", "https://medium.com/@imethdesilva/technical-documentation-nss-war-calculator-4c7641c9875d")
    st.link_button("Read about the National Scrabble Selections Process on Medium", "https://medium.com/@imethdesilva/weighted-ratings-and-the-national-scrabble-selections-process-567231d9c486")

# Tournament Archive
with tabs[4]:
    st.header("Tournament File Archive")

    if not ARCHIVE_PASSWORD:
        st.error("ARCHIVE_PASSWORD is not set in .streamlit/secrets.toml - add it to enable this section.")
    elif not st.session_state.archive_unlocked:
        st.info("This section is password protected. Enter the password to browse the archived tournament files.")
        archive_pwd = st.text_input("Password", type="password", key="archive_pwd_input")
        if st.button("Unlock Archive"):
            if archive_pwd == ARCHIVE_PASSWORD:
                st.session_state.archive_unlocked = True
                st.rerun()
            else:
                st.error("Incorrect password.")
    else:
        top_col, refresh_col, lock_col = st.columns([4, 1, 1])
        with top_col:
            st.caption(f"Browsing '{TOURNAMENT_ARCHIVE_DIR}/' - years newest-first, "
                       "tournaments within each year sorted latest-first.")
        with refresh_col:
            if st.button("Refresh"):
                st.rerun()
        with lock_col:
            if st.button("Lock"):
                st.session_state.archive_unlocked = False
                st.rerun()

        structure = get_archive_structure(st.session_state.engine)
        if not structure:
            st.warning(f"No archive found. Expected year subfolders (e.g. '2024', '2025') under "
                       f"'{TOURNAMENT_ARCHIVE_DIR}/' next to main.py.")
        else:
            year_tabs = st.tabs([year for year, _ in structure])
            for year_tab, (year, files) in zip(year_tabs, structure):
                with year_tab:
                    if not files:
                        st.caption("No files in this folder.")
                        continue
                    for fname in files:
                        fpath = os.path.join(TOURNAMENT_ARCHIVE_DIR, year, fname)
                        content = _read_archive_file(fpath)
                        if content is None:
                            st.error(f"Could not read {fname}.")
                            continue

                        data = st.session_state.engine.parse_tournament_file(content)
                        with st.expander(fname):
                            if data:
                                info_col1, info_col2, info_col3 = st.columns(3)
                                info_col1.metric("Tournament", data['name'] or "Unknown")
                                info_col2.metric("Date", data['date'].strftime('%Y-%m-%d'))
                                info_col3.metric("Players", len({p['name'] for p in data['players']}))
                                if data.get('warnings'):
                                    with st.expander(f"{len(data['warnings'])} line(s) in this file "
                                                      f"could not be parsed"):
                                        for w in data['warnings']:
                                            st.caption(w)
                            else:
                                st.caption("Could not parse tournament metadata from this file.")

                            text_key = f"archive_view_{year}_{fname}"
                            st.text_area(
                                "File Contents (edit player names or any other text directly, then Save)",
                                content, height=200, key=text_key
                            )

                            edited_text = st.session_state[text_key]
                            has_unsaved_edits = edited_text != content
                            if has_unsaved_edits:
                                edited_data = st.session_state.engine.parse_tournament_file(edited_text)
                                if edited_data is None:
                                    st.error("Parse preview: this edit no longer has a readable date in "
                                              "the first 5 lines - saving it would make the file unusable "
                                              "by the calculator.")
                                else:
                                    orig_players = len({p['name'] for p in data['players']}) if data else 0
                                    new_players = len({p['name'] for p in edited_data['players']})
                                    new_warns = len(edited_data.get('warnings') or [])
                                    if new_players < orig_players or new_warns:
                                        st.warning(f"Parse preview of your edit: {new_players} player(s) "
                                                    f"detected (was {orig_players}), {new_warns} line(s) "
                                                    f"unparseable. Review before saving/pushing.")
                                    else:
                                        st.caption(f"Parse preview of your edit: {new_players} player(s) "
                                                    f"detected - looks OK.")

                            confirm_key = f"archive_push_confirm_{year}_{fname}"

                            action_col1, action_col2, action_col3 = st.columns(3)
                            with action_col1:
                                if st.button("Save Changes", key=f"archive_save_{year}_{fname}"):
                                    try:
                                        with open(fpath, "w", encoding="utf-8") as f:
                                            f.write(edited_text)
                                        st.success(f"Saved changes to {fname} on this machine.")
                                        st.rerun()
                                    except OSError as e:
                                        st.error(f"Could not save {fname}: {e}")
                            with action_col2:
                                if st.button("Push Changes to GitHub", key=f"archive_push_{year}_{fname}"):
                                    st.session_state[confirm_key] = True
                                    st.rerun()
                            with action_col3:
                                st.download_button(
                                    "Download",
                                    data=content.encode('utf-8'),
                                    file_name=fname,
                                    mime='text/plain',
                                    key=f"archive_dl_{year}_{fname}"
                                )

                            if st.session_state.get(confirm_key):
                                st.warning("This pushes directly to the public GitHub repo (origin/main) "
                                           "with no review step. Confirm you want to publish this edit.")
                                diff_lines = list(difflib.unified_diff(
                                    content.splitlines(), edited_text.splitlines(),
                                    fromfile="on disk", tofile="your edit", lineterm=""
                                ))
                                if diff_lines:
                                    st.code("\n".join(diff_lines), language="diff")
                                else:
                                    st.caption("No textual changes detected - this will push the file as-is.")

                                confirm_col, cancel_col = st.columns(2)
                                with confirm_col:
                                    if st.button("Confirm & Push", key=f"archive_push_confirm_btn_{year}_{fname}"):
                                        try:
                                            with open(fpath, "w", encoding="utf-8") as f:
                                                f.write(edited_text)
                                            pushed = push_archive_file_to_github(
                                                fpath, f"Edit {fname} ({year}) via WAR Calculator dashboard"
                                            )
                                            st.session_state[confirm_key] = False
                                            if pushed:
                                                st.success(f"Pushed {fname} to the GitHub repo (origin/main).")
                                            else:
                                                st.info(f"{fname} already matches what's on GitHub - "
                                                        f"nothing to push.")
                                            st.rerun()
                                        except (OSError, KeyError, requests.exceptions.RequestException) as e:
                                            st.error(f"Could not push {fname} to GitHub: {e}")
                                with cancel_col:
                                    if st.button("Cancel", key=f"archive_push_cancel_{year}_{fname}"):
                                        st.session_state[confirm_key] = False
                                        st.rerun()

# Tournament History
with tabs[5]:
    st.header("Tournament History")
    st.caption(f"Every tournament found under '{TOURNAMENT_ARCHIVE_DIR}/', latest first.")

    history_rows = build_tournament_history(st.session_state.engine)
    if not history_rows:
        st.warning(f"No tournament files found under '{TOURNAMENT_ARCHIVE_DIR}/'.")
    else:
        hist_df = pd.DataFrame(history_rows)
        hist_df['Date'] = hist_df['Date'].dt.strftime('%Y-%m-%d')
        hist_df.insert(0, "No.", range(1, len(hist_df) + 1))
        st.dataframe(hist_df, use_container_width=True, hide_index=True)
        year_count = len(get_archive_structure(st.session_state.engine))
        st.caption(f"{len(history_rows)} tournaments found across {year_count} year folder(s).")