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
import zipfile
from xml.sax.saxutils import escape as xml_escape
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, HRFlowable, KeepTogether
)

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
                "remark": (f"Inactive from {last_played:%Y-%m-%d} to the cutoff date "
                           f"{cutoff_date:%Y-%m-%d} - no tournaments played in that span (>1 year). "
                           f"Ineligible for selection."),
                "override_ineligible": True,
                "games_since_resumption": 0,
            }

        had_gap = resumption_idx > 0
        if had_gap:
            gap_start = sorted_h[resumption_idx - 1]['date']
            resumption_date = sorted_h[resumption_idx]['date']
            games_since = sum(h['games'] for h in sorted_h[resumption_idx:] if not h.get('provisional'))
            if games_since < 50:
                remaining = 50 - games_since
                return {
                    "status": "resuming",
                    "remark": (f"Was inactive from {gap_start:%Y-%m-%d} to {resumption_date:%Y-%m-%d} "
                               f"(>1 year gap). WAR considered only after 50 games played since "
                               f"resumption ({games_since}/50 played, {remaining} more needed). "
                               f"Ineligible until then."),
                    "override_ineligible": True,
                    "games_since_resumption": games_since,
                }
            return {
                "status": "cleared",
                "remark": (f"Was inactive from {gap_start:%Y-%m-%d} to {resumption_date:%Y-%m-%d} "
                           f"(>1 year gap); {games_since} games played since resumption - "
                           f"restriction cleared, normal eligibility criteria apply."),
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
                    if len(numeric_blocks) >= 4:
                        try:
                            second_last = numeric_blocks[-2]
                            if '(' in second_last or ')' in second_last:
                                # The old rating itself was provisional, so this row has no
                                # separate ratings-change field - "(Old) New" instead of the
                                # usual "Old Change New" - meaning the old rating is one field
                                # back from the new rating, not two.
                                old_rating = int(float(second_last.replace('(', '').replace(')', '').strip()))
                            elif len(numeric_blocks) >= 5:
                                old_rating = int(float(
                                    numeric_blocks[-3].replace('(', '').replace(')', '').strip()
                                ))
                        except Exception:
                            pass

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

        # Whether this player's most recent known result (any date, in full_history_db)
        # was provisional - i.e. their current rating status right now, independent of
        # whether that result fell inside this mode's WAR window.
        hist_all = full_history_db.get(name, [])
        latest_entry = max(hist_all, key=lambda h: h['date']) if hist_all else None
        is_provisional = bool(latest_entry and latest_entry.get('provisional'))

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
            "Provisional": "Prov." if is_provisional else "",
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


def build_eligibility_reasons(row, conf):
    """Explains, in plain language, exactly why a player qualified or fell short -
    every threshold that wasn't met, by how much, plus the inactivity remark (if
    any) from detect_inactivity. Used in the all-players PDF audit so a reader
    doesn't have to reverse-engineer the numbers themselves."""
    if row["Total Games"] == 0 and row["Tournaments"] == 0:
        return [row["Remarks"] or "No qualifying (non-provisional, in-window) tournament results found."]

    reasons = []
    if row["Remarks"]:
        reasons.append(row["Remarks"])

    shortfalls = []
    if row["WAR"] < conf['min_war']:
        shortfalls.append(f"WAR of {row['WAR']} is below the minimum required {conf['min_war']}.")
    if row["Total Games"] < conf['req_games']:
        shortfalls.append(f"Played {row['Total Games']} rated game(s), short of the required "
                           f"{conf['req_games']}+ by {conf['req_games'] - row['Total Games']}.")
    if row["Tournaments"] < conf['req_tours']:
        shortfalls.append(f"Played {row['Tournaments']} tournament(s), short of the required "
                           f"{conf['req_tours']}+ by {conf['req_tours'] - row['Tournaments']}.")
    if row["Quads"] < conf['min_quads']:
        shortfalls.append(f"Played in {row['Quads']} distinct quadrimester(s), short of the "
                           f"required {conf['min_quads']}+ by {conf['min_quads'] - row['Quads']}.")
    if row["Majors"] < 1:
        shortfalls.append("Did not play a qualifying 18-round major tournament.")
    if row["Recent"] < conf['req_recent']:
        shortfalls.append(f"Played {row['Recent']} tournament(s) in the most recent quadrimester(s) "
                           f"(Q4/Q5), short of the required {conf['req_recent']}+ by "
                           f"{conf['req_recent'] - row['Recent']}.")

    if shortfalls:
        reasons.extend(shortfalls)
    else:
        suffix = " (otherwise)." if reasons else "."
        reasons.append("Meets all WAR, games, tournament, quadrimester, major, and "
                        "recent-activity requirements" + suffix)

    return reasons


def build_considered_tournaments_table(players_db):
    """Lists every distinct tournament actually counted toward WAR for this mode -
    i.e. one that matched a quadrimester and had at least one non-provisional result.
    Derived from players_db history entries (already filtered to in-window,
    non-provisional results) and deduped, since every player who played it repeats
    the same tournament/date/quad/weight."""
    seen = {}
    for pdata in players_db.values():
        for h in pdata['history']:
            key = (h['Tournament'], h['Date'], h['Quad'])
            if key not in seen:
                seen[key] = {
                    "Tournament": h['Tournament'],
                    "Date": h['Date'],
                    "Quadrimester": h['Quad'],
                    "Weight Factor": h['Weight'],
                    "Players Considered": 0,
                }
            seen[key]["Players Considered"] += 1

    rows = list(seen.values())
    rows.sort(key=lambda r: r["Date"], reverse=True)
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
DEFAULT_WSC_EVENT_DATE = "01.09.2027"
DEFAULT_WYSC_EVENT_DATE = "27.08.2027"


def _read_archive_file(fpath):
    try:
        with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except OSError:
        return None


GITHUB_REPO = "imethdesilva/WAR_Calculator"
GITHUB_BRANCH = "main"


def admin_tag():
    """The name/initials the current admin entered when unlocking the archive, formatted
    for appending to a commit message - since ARCHIVE_PASSWORD is one shared secret,
    this is the only thing that ties a given push/delete back to the person who made
    it, so every commit message built for a password-gated action should include it."""
    name = st.session_state.get("archive_admin_name", "").strip()
    return f" [by {name}]" if name else " [by unspecified admin]"


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


def delete_archive_file_from_github(repo_path, commit_message):
    """Deletes repo_path from GITHUB_REPO via the Contents API. Used when a local
    rename needs to remove the old filename on GitHub too - the Contents API has no
    atomic rename, so a rename there is represented as a delete-old + create-new pair
    of commits. Returns False if the path doesn't exist on GitHub (nothing to
    delete), True if it was deleted."""
    token = st.secrets["GITHUB_TOKEN"]
    repo_path = repo_path.replace(os.sep, "/").lstrip("/")

    api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{repo_path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "WAR-Calculator-App",
    }

    get_resp = requests.get(api_url, headers=headers, params={"ref": GITHUB_BRANCH}, timeout=30)
    if get_resp.status_code == 404:
        return False
    get_resp.raise_for_status()
    sha = get_resp.json()["sha"]

    payload = {"message": commit_message, "sha": sha, "branch": GITHUB_BRANCH}
    del_resp = requests.delete(api_url, headers=headers, json=payload, timeout=30)
    del_resp.raise_for_status()
    return True


@st.dialog("Add Tournament File")
def add_tournament_file_dialog():
    """Modal opened from the 'All Tournaments' tab's '+' button: upload a .txt file,
    the year folder is auto-detected from the date parsed out of it, saved locally
    for review, and only pushed to GitHub once explicitly confirmed - mirroring the
    Confirm & Push step used when editing an existing archive file, so a bad or
    fraudulent upload can't reach GitHub without a second, deliberate click."""
    confirm_key = "add_file_pending_push"
    pending = st.session_state.get(confirm_key)

    if pending:
        st.success(f"Detected: **{pending['tournament_name']}** on {pending['date']} - "
                   f"{pending['player_count']} player(s) parsed"
                   + (f", {pending['warn_count']} line(s) unparseable" if pending['warn_count'] else "")
                   + f" - saved locally in '{pending['year']}/'.")
        if pending["warnings"]:
            with st.expander(f"{pending['warn_count']} parse warning(s) - review before pushing"):
                for w in pending["warnings"]:
                    st.caption(f"- {w}")
        st.warning("Review the details above, then confirm to push this new file to GitHub "
                   "(origin/main), or cancel to discard it.")
        confirm_col, cancel_col, _spacer = st.columns([2, 1, 6])
        with confirm_col:
            if st.button("Confirm & Push to GitHub", key="add_file_confirm_push_btn"):
                try:
                    push_archive_file_to_github(
                        pending["path"],
                        f"Add {pending['fname']} ({pending['year']}) via WAR Calculator dashboard"
                        f"{admin_tag()}"
                    )
                    st.success(f"Added '{pending['fname']}' to '{pending['year']}/' and pushed to GitHub.")
                except (KeyError, requests.exceptions.RequestException) as e:
                    st.warning(f"Saved locally, but the GitHub push failed: {e}. Push it from the "
                               f"'{pending['year']}' tab once resolved.")
                st.session_state[confirm_key] = None
                st.rerun()
        with cancel_col:
            if st.button("Cancel", key="add_file_cancel_push_btn"):
                try:
                    os.remove(pending["path"])
                except OSError:
                    pass
                st.session_state[confirm_key] = None
                st.rerun()
        return

    st.caption("Upload a .txt file - the year folder is detected automatically from the date "
               "in the file. It's saved locally for review first, and only pushed to GitHub "
               "once you confirm.")
    uploaded_new_file = st.file_uploader(
        "Upload a tournament .txt file", type=["txt"], key="add_file_uploader_dialog"
    )
    if uploaded_new_file is None:
        return

    uploaded_content = uploaded_new_file.read().decode('utf-8', errors='ignore')
    uploaded_data = st.session_state.engine.parse_tournament_file(uploaded_content)
    if not uploaded_data:
        st.error("Could not detect a tournament date in the first 5 lines of this file - it "
                 "doesn't look like a valid results file.")
        return

    detected_year = str(uploaded_data['date'].year)
    player_count = len({p['name'] for p in uploaded_data['players']})
    warnings = uploaded_data.get('warnings') or []
    st.success(f"Detected: **{uploaded_data['name'] or 'Unknown tournament'}** on "
               f"{uploaded_data['date']:%Y-%m-%d} - {player_count} player(s) parsed"
               + (f", {len(warnings)} line(s) unparseable" if warnings else "")
               + f" - will be added to '{detected_year}/'.")
    if warnings:
        with st.expander(f"{len(warnings)} parse warning(s) - review before adding"):
            for w in warnings:
                st.caption(f"- {w}")

    target_dir = os.path.join(TOURNAMENT_ARCHIVE_DIR, detected_year)
    target_path = os.path.join(target_dir, uploaded_new_file.name)
    if os.path.exists(target_path):
        st.error(f"'{uploaded_new_file.name}' already exists in '{detected_year}/' - open it "
                 f"from the archive list to edit it instead.")
        return

    if st.button("Add File", key="add_file_upload_btn_dialog"):
        os.makedirs(target_dir, exist_ok=True)
        with open(target_path, "w", encoding="utf-8") as f:
            f.write(uploaded_content)
        st.session_state[confirm_key] = {
            "path": target_path,
            "fname": uploaded_new_file.name,
            "year": detected_year,
            "tournament_name": uploaded_data['name'] or 'Unknown tournament',
            "date": uploaded_data['date'].strftime('%Y-%m-%d'),
            "player_count": player_count,
            "warn_count": len(warnings),
            "warnings": warnings,
        }
        st.rerun()


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
            game_counts = [p['games'] for p in data['players']]

            rows.append({
                "Date": data['date'],
                "Tournament Name": data['name'],
                "Year Folder": year,
                "Players": len(distinct_players),
                "Division Size (games)": max(game_counts) if game_counts else "-",
                "Source File": fname,
            })

    rows.sort(key=lambda r: r["Date"], reverse=True)
    return rows


def build_archive_zip(base_dir=TOURNAMENT_ARCHIVE_DIR):
    """Zips every year folder and tournament .txt file under base_dir into a single
    in-memory archive, preserving the 'year/filename.txt' structure, so the whole
    tournament archive can be downloaded in one click."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if os.path.isdir(base_dir):
            for year in sorted(os.listdir(base_dir)):
                year_path = os.path.join(base_dir, year)
                if not os.path.isdir(year_path):
                    continue
                for fname in sorted(os.listdir(year_path)):
                    if fname.lower().endswith(".txt"):
                        zf.write(os.path.join(year_path, fname), arcname=f"{year}/{fname}")
    buf.seek(0)
    return buf.getvalue()


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


def process_tournament_data(engine, mode, event_date_str, tournament_objects, ignore_q5_push=False):
    """Runs the full WAR pipeline (quad/cutoff calculation, per-player history build,
    inactivity detection) for one mode over a pool of already-parsed tournament objects,
    exactly like the Archive tab's manual upload flow. Shared so both that flow and the
    archive-wide 'Run WAR for WYSC and WSC' button use identical logic instead of two
    copies drifting apart. Returns a result bundle dict, or None if tournament_objects
    is empty or the configuration failed (calculate_configuration already st.error's on
    a bad date)."""
    all_tour_dates = [t['date'] for t in tournament_objects]
    if not all_tour_dates:
        return None

    upload_warnings = {}
    for t in tournament_objects:
        if t.get('warnings'):
            upload_warnings[t.get('source_filename', t['name'])] = t['warnings']

    config, quads = engine.calculate_configuration(
        mode, event_date_str, tournament_dates=all_tour_dates, ignore_q5_push=ignore_q5_push
    )
    if not config:
        return None

    db = {}
    # Every detectable player across ALL files, any date, provisional or not - used
    # only to detect >1yr inactivity gaps (PDF p.6), never for WAR math.
    full_history = {}

    for data in tournament_objects:
        q_info = next((q for q in quads if q['start'] <= data['date'] <= q['end']), None)

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
            # Outside every quadrimester's date range (e.g. older than the 20-month
            # window, or in a gap the p.3 push-back/merge rules excluded) - omitted.
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

            # PDF p.5: the candidate must have personally played the full 18 rounds -
            # a file-wide "this tournament had an 18-round division somewhere" flag
            # is not enough if the player was in a shorter one.
            if p_file_data['games'] >= 18:
                db[name]["major_count"] += 1

            if q_info['quad'] >= 4:
                db[name]["recent_count"] += 1

    inactivity_map = {
        name: engine.detect_inactivity(hist, config['cutoff_date'])
        for name, hist in full_history.items()
    }

    return {
        "config": config,
        "quad_ranges": quads,
        "players_db": db,
        "full_history_db": full_history,
        "inactivity_map": inactivity_map,
        "uploaded_tournament_dates": all_tour_dates,
        "upload_warnings": upload_warnings,
    }


def load_all_archive_tournament_objects(engine, base_dir=TOURNAMENT_ARCHIVE_DIR):
    """Parses every .txt file across every year folder in the archive into a flat pool
    of tournament objects, tagged with their source filename for warning attribution.
    This is the pool the 'Run WAR for WYSC and WSC' button feeds into
    process_tournament_data() for each mode - the same quad/date matching in that
    function then naturally omits whichever files fall outside a given mode's window."""
    objects = []
    for year, files in get_archive_structure(engine, base_dir):
        for fname in files:
            content = _read_archive_file(os.path.join(base_dir, year, fname))
            if content is None:
                continue
            data = engine.parse_tournament_file(content)
            if data:
                data['source_filename'] = f"{fname} ({year})"
                objects.append(data)
    return objects


def sync_active_dataset(mode):
    """Copies a cached result bundle from st.session_state.results[mode] into the flat
    session-state variables the Overview/Leaderboard/Player Audit tabs read, so
    switching between a cached WSC and WYSC run doesn't require recomputation."""
    bundle = st.session_state.results.get(mode)
    if not bundle:
        return
    st.session_state.config = bundle["config"]
    st.session_state.quad_ranges = bundle["quad_ranges"]
    st.session_state.players_db = bundle["players_db"]
    st.session_state.full_history_db = bundle["full_history_db"]
    st.session_state.inactivity_map = bundle["inactivity_map"]
    st.session_state.uploaded_tournament_dates = bundle["uploaded_tournament_dates"]
    st.session_state.upload_warnings = bundle["upload_warnings"]
    st.session_state.processed_files = True
    st.session_state.active_mode = mode


def reset_dataset():
    """Clears every computed WAR result so the app goes back to its initial,
    unconfigured state - used by the Archive tab's 'Reset Dataset' button. Does not
    touch the archive files themselves, only the in-memory results."""
    st.session_state.results = {}
    st.session_state.active_mode = None
    st.session_state.processed_files = False
    st.session_state.players_db = {}
    st.session_state.full_history_db = {}
    st.session_state.inactivity_map = {}
    st.session_state.uploaded_tournament_dates = []
    st.session_state.upload_warnings = {}
    st.session_state.pop('config', None)
    st.session_state.pop('quad_ranges', None)


def sanitize_csv_cell(value):
    """Neutralizes spreadsheet formula injection: player/tournament names come
    straight from uploaded tournament files with no character restrictions, so a
    name starting with =, +, -, @, tab or CR would be evaluated as a live formula
    by Excel/Sheets when someone opens the exported report. Prefixing with a
    single quote forces it to be read as literal text instead."""
    s = str(value)
    if s and s[0] in ('=', '+', '-', '@', '\t', '\r'):
        return "'" + s
    return value


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
    lb_df["Player Name"] = lb_df["Player Name"].map(sanitize_csv_cell)
    lb_df["Remarks"] = lb_df["Remarks"].map(sanitize_csv_cell)
    lb_df.insert(0, "Rank", range(1, len(lb_df) + 1))
    lb_df = lb_df.rename(columns=headers_map)
    # lineterminator='\n' avoids pandas' default '\r\n' colliding with the plain '\n'
    # used elsewhere in this buffer and producing malformed '\r\r\n' blank rows.
    lb_df.to_csv(buf, index=False, lineterminator='\n')
    buf.write("\n\n")

    buf.write("INDIVIDUAL PLAYER BREAKDOWN\n\n")
    for row in rows_sorted:
        name = row["Player Name"]
        writer.writerow(["Player", sanitize_csv_cell(name)])
        data = players_db.get(name)
        if data:
            h_df = pd.DataFrame(data["history"])
            h_df["Tournament"] = h_df["Tournament"].map(sanitize_csv_cell)
            h_df = h_df.sort_values(by="Date", ascending=False)
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
            writer.writerow(["Remark", sanitize_csv_cell(row["Remarks"])])
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


PDF_HEADER_BG = colors.HexColor("#1a1a1a")
PDF_LIGHT_ROW = colors.HexColor("#f0f0f0")
PDF_BORDER = colors.HexColor("#555555")
PDF_GREY_TEXT = colors.HexColor("#555555")

_PDF_CELL_STYLE = ParagraphStyle(
    "PdfCell", fontName="Helvetica", fontSize=8, leading=10, alignment=TA_CENTER
)


def _pdf_cell(text):
    """Wraps a table cell's text in a Paragraph so long strings (e.g. a long
    tournament name) wrap within the column instead of overflowing into the next
    cell - a plain string in a reportlab Table never wraps, no matter how narrow
    the column is."""
    return Paragraph(xml_escape(str(text)), _PDF_CELL_STYLE)


def _pdf_styles():
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(
        name="ReportTitle", parent=styles["Title"], textColor=colors.black, fontSize=19, spaceAfter=4
    ))
    styles.add(ParagraphStyle(
        name="ReportSubtitle", parent=styles["Normal"], textColor=PDF_GREY_TEXT, fontSize=9, spaceAfter=12
    ))
    styles.add(ParagraphStyle(
        name="SectionHeading", parent=styles["Heading2"], textColor=colors.black, fontSize=13,
        spaceBefore=14, spaceAfter=6
    ))
    styles.add(ParagraphStyle(
        name="PlayerHeading", parent=styles["Heading3"], textColor=colors.black, fontSize=11.5,
        spaceBefore=10, spaceAfter=2
    ))
    styles.add(ParagraphStyle(name="BodySmall", parent=styles["Normal"], fontSize=8.5, leading=11))
    styles.add(ParagraphStyle(
        name="ReasonLine", parent=styles["Normal"], fontSize=8.5, leading=11, leftIndent=10,
        bulletIndent=0
    ))
    return styles


def _pdf_table(data, col_widths=None):
    """Shared 'house style' for every data table in both reports: black header row,
    alternating light-grey body rows, thin grey gridlines, centered small text -
    plain black-and-white throughout, no colour."""
    table = Table(data, colWidths=col_widths, repeatRows=1)
    style = [
        ('BACKGROUND', (0, 0), (-1, 0), PDF_HEADER_BG),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'),
        ('FONTSIZE', (0, 0), (-1, -1), 8),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('GRID', (0, 0), (-1, -1), 0.5, PDF_BORDER),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]
    for row_idx in range(2, len(data), 2):
        style.append(('BACKGROUND', (0, row_idx), (-1, row_idx), PDF_LIGHT_ROW))
    table.setStyle(TableStyle(style))
    return table


def _pdf_header(story, styles, title, subtitle):
    story.append(Paragraph(xml_escape(title), styles["ReportTitle"]))
    story.append(Paragraph(xml_escape(subtitle), styles["ReportSubtitle"]))
    story.append(HRFlowable(width="100%", color=colors.black, thickness=1.2, spaceAfter=12))


def _pdf_calculation_details_table(conf, players_assessed, players_qualified):
    """The 'how this window was derived' table - international event date, cut-off
    date and the eligibility thresholds - shared by the calculation report and the
    player audit report(s) so every PDF that shows player results also explains
    where those results came from."""
    details_data = [
        ["Tournament Classification", conf['mode']],
        ["International Event Date", conf['intl_date'].strftime('%d %b %Y')],
        ["Cut-off Date", conf['cutoff_date'].strftime('%d %b %Y')],
        ["Minimum WAR Required", str(conf['min_war'])],
        ["Minimum Games Required", str(conf['req_games'])],
        ["Minimum Tournaments Required", str(conf['req_tours'])],
        ["Minimum Quadrimesters Required", str(conf['min_quads'])],
        ["Recent Activity Requirement", f"{conf['req_recent']} tournament(s) in Q4/Q5"],
        ["Players Assessed", str(players_assessed)],
        ["Players Qualified", str(players_qualified)],
    ]
    detail_table = Table(details_data, colWidths=[7*cm, 8.2*cm])
    detail_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
        ('FONTNAME', (1, 0), (1, -1), 'Helvetica'),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('GRID', (0, 0), (-1, -1), 0.4, PDF_BORDER),
        ('BACKGROUND', (0, 0), (0, -1), PDF_LIGHT_ROW),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('LEFTPADDING', (0, 0), (-1, -1), 8),
    ]))
    return detail_table


def _pdf_quad_schedule_table(quad_ranges):
    """The fixed quadrimester date ranges and weights actually used for this
    calculation - shared by the calculation report and the player audit report(s)."""
    quad_data = [["Period", "Weight Factor", "Start", "End"]]
    for q in quad_ranges:
        quad_data.append([
            f"Q{q['quad']}", f"{q['weight']:.2f}",
            q['start'].strftime('%Y-%m-%d'), q['end'].strftime('%Y-%m-%d')
        ])
    return _pdf_table(quad_data, col_widths=[3*cm, 3.5*cm, 4.4*cm, 4.4*cm])


def generate_calculation_report_pdf(conf, quad_ranges, rows, considered_tournaments):
    """Builds the 'Generate Report' PDF: calculation specifics (cutoff date, event
    date, thresholds), the quadrimester weighting schedule, a qualification summary,
    and the full list of tournaments considered for WAR (latest first) - everything
    needed to audit how this mode's results were derived. Per-player detail lives in
    generate_all_players_audit_pdf() instead, to keep this one short and scannable."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4, leftMargin=1.8*cm, rightMargin=1.8*cm, topMargin=1.6*cm, bottomMargin=1.6*cm,
        title=f"{conf['mode']} Selections Report"
    )
    styles = _pdf_styles()
    story = []

    _pdf_header(
        story, styles, f"National Selections Report - {conf['mode']}",
        f"Generated {datetime.now().strftime('%d %B %Y, %H:%M')} - "
        f"Scrabble Federation of Sri Lanka - WAR Calculator"
    )

    story.append(Paragraph("Calculation Details", styles["SectionHeading"]))
    qualified_count = sum(1 for r in rows if r["Status"] == "QUALIFIED")
    story.append(_pdf_calculation_details_table(conf, len(rows), qualified_count))

    story.append(Paragraph("Quadrimester Weighting Schedule", styles["SectionHeading"]))
    story.append(_pdf_quad_schedule_table(quad_ranges))

    story.append(Paragraph("Tournaments Considered for WAR (Latest First)", styles["SectionHeading"]))
    if not considered_tournaments:
        story.append(Paragraph("No tournaments contributed to this calculation.", styles["BodySmall"]))
    else:
        t_data = [["#", "Tournament", "Date", "Quad", "Weight", "Players"]]
        for i, t in enumerate(considered_tournaments, start=1):
            t_data.append([
                str(i), _pdf_cell(t["Tournament"]), t["Date"], f"Q{t['Quadrimester']}",
                f"{t['Weight Factor']:.2f}", str(t["Players Considered"])
            ])
        story.append(_pdf_table(t_data, col_widths=[0.9*cm, 7.5*cm, 2.4*cm, 1.5*cm, 1.8*cm, 1.6*cm]))

    doc.build(story)
    return buf.getvalue()


def generate_all_players_audit_pdf(rows_sorted, players_db, conf, quad_ranges,
                                    label="Individual Player Audit Report"):
    """Builds a per-player audit PDF - one section per player, in the same
    highest-WAR-first order as the leaderboard, each with a summary table and
    their full tournament history (latest first). Used for the all-players,
    qualified-players-only, and single-player reports; `label` distinguishes them
    in the PDF's own title so it's self-describing once downloaded. Opens with the
    same Calculation Details / Quadrimester Weighting Schedule sections as the
    calculation report, so a player audit is self-contained proof of how the
    international event date and cut-off date drove every result inside it."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4, leftMargin=1.8*cm, rightMargin=1.8*cm, topMargin=1.6*cm, bottomMargin=1.6*cm,
        title=f"{conf['mode']} {label}"
    )
    styles = _pdf_styles()
    status_style = ParagraphStyle(
        "StatusLine", parent=styles["BodySmall"], fontName="Helvetica-Bold", spaceAfter=4
    )
    reason_label_style = ParagraphStyle(
        "ReasonLabel", parent=styles["BodySmall"], fontName="Helvetica-Bold", spaceBefore=3, spaceAfter=1
    )
    story = []

    _pdf_header(
        story, styles, f"{label} - {conf['mode']}",
        f"Generated {datetime.now().strftime('%d %B %Y, %H:%M')} - {len(rows_sorted)} player(s), "
        f"ranked by highest WAR first - Scrabble Federation of Sri Lanka - WAR Calculator"
    )

    story.append(Paragraph("Calculation Details", styles["SectionHeading"]))
    qualified_count = sum(1 for r in rows_sorted if r["Status"] == "QUALIFIED")
    story.append(_pdf_calculation_details_table(conf, len(rows_sorted), qualified_count))

    story.append(Paragraph("Quadrimester Weighting Schedule", styles["SectionHeading"]))
    story.append(_pdf_quad_schedule_table(quad_ranges))
    story.append(Spacer(1, 6))
    story.append(HRFlowable(width="100%", color=colors.black, thickness=1, spaceAfter=10))

    for idx, row in enumerate(rows_sorted, start=1):
        name = row["Player Name"]

        prov_badge = "  [PROVISIONAL]" if row.get("Provisional") else ""
        story.append(Paragraph(f"{idx}. {xml_escape(name)}{prov_badge}", styles["PlayerHeading"]))
        story.append(Paragraph(f"WAR: {row['WAR']}  |  Status: {row['Status']}", status_style))

        summary_data = [
            ["Current Rating", str(row["Current Rating"]), "WAR (precise)", f"{row['WAR Precise']:.2f}"],
            ["Quadrimesters", str(row["Quads"]), "Tournaments", str(row["Tournaments"])],
            ["Total Games", str(row["Total Games"]), "Major Tournaments", str(row["Majors"])],
            ["Recent Activity", str(row["Recent"]), "", ""],
        ]
        summary_table = Table(summary_data, colWidths=[3.3*cm, 3*cm, 3.6*cm, 3*cm])
        summary_table.setStyle(TableStyle([
            ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
            ('FONTNAME', (2, 0), (2, -1), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 8),
            ('GRID', (0, 0), (-1, -1), 0.3, PDF_BORDER),
            ('BACKGROUND', (0, 0), (0, -1), PDF_LIGHT_ROW),
            ('BACKGROUND', (2, 0), (2, -1), PDF_LIGHT_ROW),
            ('TOPPADDING', (0, 0), (-1, -1), 3),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
        ]))
        story.append(summary_table)
        story.append(Spacer(1, 4))

        data = players_db.get(name)
        if data and data["history"]:
            history_sorted = sorted(data["history"], key=lambda h: h["Date"], reverse=True)
            hist_data = [["Date", "Tournament", "Quad", "Weight", "Old", "New", "Games", "Wtd. Value"]]
            for h in history_sorted:
                hist_data.append([
                    h["Date"], _pdf_cell(h["Tournament"]), f"Q{h['Quad']}", f"{h['Weight']:.2f}",
                    str(h["Old Rating"]), str(h["New Rating"]), str(h["Games"]),
                    f"{h['WeightedVal']:.2f}"
                ])
            story.append(_pdf_table(
                hist_data,
                col_widths=[1.7*cm, 5.4*cm, 1.0*cm, 1.3*cm, 1.2*cm, 1.2*cm, 1.1*cm, 1.9*cm]
            ))
            story.append(Paragraph(
                "Weighted Value = Weight &#215; New Rating (rating after that tournament).",
                styles["BodySmall"]
            ))
            story.append(Spacer(1, 4))

            total_weight = sum(h["Weight"] for h in history_sorted)
            total_weighted_val = sum(h["WeightedVal"] for h in history_sorted)
            calc_war, calc_war_precise = compute_war(history_sorted)
            breakdown_data = [
                ["Total Weight (Sum of Weight column)", f"{total_weight:.2f}"],
                ["Total Weighted Value (Sum of Wtd. Value column)", f"{total_weighted_val:.2f}"],
                ["Final WAR = Total Weighted Value / Total Weight",
                 f"{total_weighted_val:.2f} / {total_weight:.2f} = {calc_war_precise:.2f} -> {calc_war}"],
            ]
            breakdown_table = Table(breakdown_data, colWidths=[7.5*cm, 8.5*cm])
            breakdown_table.setStyle(TableStyle([
                ('FONTNAME', (0, 0), (0, -1), 'Helvetica'),
                ('FONTNAME', (1, 0), (1, -1), 'Helvetica-Bold'),
                ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, -1), 8),
                ('GRID', (0, 0), (-1, -1), 0.3, PDF_BORDER),
                ('BACKGROUND', (0, -1), (-1, -1), PDF_LIGHT_ROW),
                ('TOPPADDING', (0, 0), (-1, -1), 3),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
            ]))
            story.append(KeepTogether([breakdown_table]))
        else:
            story.append(Paragraph(
                "No qualifying (non-provisional, in-window) tournament history found.", styles["BodySmall"]
            ))

        reasons = build_eligibility_reasons(row, conf)
        story.append(Paragraph("Reasons:", reason_label_style))
        for reason in reasons:
            story.append(Paragraph(f"- {xml_escape(reason)}", styles["ReasonLine"]))

        story.append(Spacer(1, 10))
        story.append(HRFlowable(width="100%", color=PDF_BORDER, thickness=0.5, spaceAfter=8))

    doc.build(story)
    return buf.getvalue()


# UI
st.set_page_config(page_title="National Selections Dashboard", layout="wide")

st.markdown("""
    <style>
    /* 0. Trim the default top whitespace above the header */
    .block-container {
        padding-top: 2rem;
    }

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

    /* 2b. Tighten the gap between columns so button rows sit close together
       instead of spreading edge-to-edge - paired with narrow column ratios
       (plus a spacer column) wherever multiple buttons sit in one row. */
    div[data-testid="stHorizontalBlock"] {
        gap: 0.5rem;
    }

    /* 2c. Any column that holds a button/download-button shrinks to fit that
       button instead of stretching to its flex ratio's full share of the row -
       this is what actually removes the dead space between buttons that sit
       in the same row. A column with no button (used purely as a spacer) is
       left alone, so it keeps growing and absorbs the rest of the row width,
       pushing the compacted button group to whichever side the spacer isn't on. */
    div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"]:has(div[data-testid="stButton"]),
    div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"]:has(div[data-testid="stDownloadButton"]) {
        flex: 0 0 auto !important;
        width: auto !important;
        min-width: 0 !important;
    }
    div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"]:has(div[data-testid="stButton"]) div.stButton > button,
    div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"]:has(div[data-testid="stDownloadButton"]) div[data-testid="stDownloadButton"] > button {
        width: auto !important;
        white-space: nowrap;
        padding-left: 1.1rem;
        padding-right: 1.1rem;
    }

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
    st.session_state.archive_admin_name = ""
    st.session_state.archive_renames = {}
    st.session_state.results = {}
    st.session_state.active_mode = None

# Main
st.title("National Scrabble Selections - WAR Calculator")
st.caption("Official Administrative System for Weighted Average Rating (WAR) Calculation")

if st.session_state.active_mode and len(st.session_state.results) > 1:
    cached_modes = list(st.session_state.results.keys())
    current_idx = cached_modes.index(st.session_state.active_mode) if st.session_state.active_mode in cached_modes else 0
    viewing_mode = st.radio(
        "Viewing dataset", cached_modes, index=current_idx, horizontal=True,
        help="Both WSC and WYSC results are cached from the last run - switch between "
             "them instantly without reprocessing."
    )
    if viewing_mode != st.session_state.active_mode:
        sync_active_dataset(viewing_mode)
        st.rerun()
elif st.session_state.active_mode:
    st.caption(f"Viewing dataset: **{st.session_state.active_mode}**")

tabs = st.tabs(["Tournament Archive", "Selection Overview", "National Leaderboard",
                "Individual Player Audit", "Policy & Criteria"])

# Overview
with tabs[1]:
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

        st.subheader(f"Tournaments Considered for WAR ({st.session_state.config['mode']})")
        considered = build_considered_tournaments_table(st.session_state.players_db)
        if not considered:
            st.info("No tournaments have been included yet - run WAR from the Tournament "
                    "Archive tab first.")
        else:
            considered_df = pd.DataFrame(considered)
            considered_df.insert(0, "No.", range(1, len(considered_df) + 1))
            st.dataframe(considered_df, use_container_width=True, hide_index=True)
            st.caption(f"{len(considered)} tournament(s) fell within a quadrimester and contributed "
                       f"to WAR for {st.session_state.config['mode']}. Tournaments outside the 20-month "
                       f"window, or with only provisional results, are omitted from this list.")

        overview_rows = build_leaderboard_rows(
            st.session_state.players_db, st.session_state.full_history_db,
            st.session_state.inactivity_map, st.session_state.config
        )
        calc_report_pdf = generate_calculation_report_pdf(
            st.session_state.config, st.session_state.quad_ranges, overview_rows, considered
        )
        st.download_button(
            "Generate Report (PDF)",
            data=calc_report_pdf,
            file_name=f"{st.session_state.config['mode']}_calculation_report.pdf",
            mime="application/pdf"
        )
    else:
        st.info("Awaiting configuration. Run WAR from the Tournament Archive tab to get started.")

# Leaderboard
with tabs[2]:
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
                            push_col, dismiss_col, _spacer = st.columns([3, 1, 5])
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
                                                f"in {fname} ({year}){admin_tag()}"
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
                hide_zero_war = st.checkbox("Hide players with WAR = 0", value=True)
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

                export_col, push_col, _spacer1 = st.columns([2, 2, 4])
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

                all_players_pdf = generate_all_players_audit_pdf(
                    rows, st.session_state.players_db, conf, st.session_state.quad_ranges,
                    label="All Players Audit Report"
                )
                qualified_rows = [r for r in rows if r["Status"] == "QUALIFIED"]
                qualified_pdf = generate_all_players_audit_pdf(
                    qualified_rows, st.session_state.players_db, conf, st.session_state.quad_ranges,
                    label="Qualified Players Audit Report"
                )

                all_pdf_col, qualified_pdf_col, _spacer2 = st.columns([2, 2, 4])
                with all_pdf_col:
                    st.download_button(
                        "Download All Players Audit Report (PDF)",
                        data=all_players_pdf,
                        file_name=f"{conf['mode']}_all_players_audit_report.pdf",
                        mime="application/pdf",
                        key="dl_all_players_pdf"
                    )
                with qualified_pdf_col:
                    st.download_button(
                        "Download Qualified Players Audit Report (PDF)",
                        data=qualified_pdf,
                        file_name=f"{conf['mode']}_qualified_players_audit_report.pdf",
                        mime="application/pdf",
                        key="dl_qualified_players_pdf"
                    )
    else:
        st.warning("Run WAR from the Tournament Archive tab to generate rankings.")

# Player Breakdown
with tabs[3]:
    if st.session_state.processed_files:
        player_select = st.selectbox("Search Player for Audit", sorted(st.session_state.players_db.keys()))
        if player_select:
            p_data = st.session_state.players_db[player_select]
            hist_all = st.session_state.full_history_db.get(player_select, [])
            latest_entry = max(hist_all, key=lambda h: h['date']) if hist_all else None
            is_provisional = bool(latest_entry and latest_entry.get('provisional'))
            prov_suffix = "  **[PROVISIONAL]**" if is_provisional else ""
            st.subheader(f"Participation History: {player_select}{prov_suffix}")

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
            all_rows_for_player = build_leaderboard_rows(
                st.session_state.players_db, st.session_state.full_history_db,
                st.session_state.inactivity_map, st.session_state.config
            )
            player_row = next(
                (r for r in all_rows_for_player if r["Player Name"] == player_select), None
            )
            if player_row:
                indiv_pdf = generate_all_players_audit_pdf(
                    [player_row], st.session_state.players_db, st.session_state.config,
                    st.session_state.quad_ranges,
                    label=f"{player_select} Individual Audit Report"
                )
                st.download_button(
                    label=f"Export {player_select} Results (PDF)",
                    data=indiv_pdf,
                    file_name=f"{player_select.replace(' ', '_')}_WAR.pdf",
                    mime='application/pdf'
                )
    else:
        st.info("Awaiting data processing.")

# Info
with tabs[4]:
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
with tabs[0]:
    archive_ready = ARCHIVE_PASSWORD and st.session_state.archive_unlocked

    if archive_ready:
        header_col, reset_col, refresh_col, lock_col = st.columns(
            [5, 1, 1, 1], gap="small", vertical_alignment="center"
        )
        with header_col:
            st.header("Tournament File Archive")
        with reset_col:
            if st.button("Reset Dataset", help="Clears the currently computed WAR results and "
                                                "returns the dashboard to its initial state. "
                                                "Doesn't touch any archive files."):
                reset_dataset()
                st.rerun()
        with refresh_col:
            if st.button("Refresh"):
                st.rerun()
        with lock_col:
            if st.button("Lock"):
                st.session_state.archive_unlocked = False
                st.session_state.archive_admin_name = ""
                st.rerun()
    else:
        st.header("Tournament File Archive")

    if not ARCHIVE_PASSWORD:
        st.error("ARCHIVE_PASSWORD is not set in .streamlit/secrets.toml - add it to enable this section.")
    elif not st.session_state.archive_unlocked:
        st.info("This section is password protected. Enter the password to browse the archived tournament files.")
        archive_pwd = st.text_input("Password", type="password", key="archive_pwd_input")
        archive_name_input = st.text_input(
            "Your name / initials", key="archive_name_input",
            help="The password is shared, so this is recorded on every change you push or delete "
                 "from this point on - it's what tells one admin's edits apart from another's."
        )
        if st.button("Unlock Archive"):
            if archive_pwd != ARCHIVE_PASSWORD:
                st.error("Incorrect password.")
            elif not archive_name_input.strip():
                st.error("Enter your name or initials - required so changes you make can be traced "
                          "back to you.")
            else:
                st.session_state.archive_unlocked = True
                st.session_state.archive_admin_name = archive_name_input.strip()
                st.rerun()
    else:
        if not st.session_state.active_mode:
            st.info("**Getting started:** click **Run WAR for WYSC and WSC** below. Once that "
                    "finishes, results appear in Selection Overview and National Leaderboard.")

        st.markdown("---")
        st.subheader("Run WAR for WYSC and WSC")
        st.caption("Runs the full WAR calculation for both classifications using every file "
                   "in this archive across all years - files outside a classification's "
                   "quadrimester window are automatically excluded, exactly like a normal "
                   "run. Results are cached so you can switch between them from the "
                   "'Viewing dataset' control at the top of the page without reprocessing.")

        if st.button("Run WAR for WYSC and WSC", key="dual_run_trigger"):
            st.session_state.show_dual_run_form = True
            st.rerun()

        if st.session_state.get("show_dual_run_form"):
            wysc_date = st.text_input(
                "WYSC International Event Date (DD.MM.YYYY)", value=DEFAULT_WYSC_EVENT_DATE,
                key="dual_run_wysc_date"
            )
            wsc_date = st.text_input(
                "WSC International Event Date (DD.MM.YYYY)", value=DEFAULT_WSC_EVENT_DATE,
                key="dual_run_wsc_date"
            )
            dual_run_ignore_q5 = st.checkbox(
                "Ignore Q5 Push (Live View)", value=False, key="dual_run_ignore_q5",
                help="Skips the PDF p.3 push-back/merge "
                     "rules and anchors Q5 to each cutoff date's natural quadrimester. Leave off "
                     "for the official selection calculation."
            )
            run_col, cancel_col, _spacer = st.columns([2, 1, 6])
            with run_col:
                if st.button("Confirm & Run", key="dual_run_confirm"):
                    tournament_objects = load_all_archive_tournament_objects(st.session_state.engine)
                    if not tournament_objects:
                        st.error("No parsable tournament files found in the archive.")
                    else:
                        wysc_bundle = process_tournament_data(
                            st.session_state.engine, "WYSC", wysc_date, tournament_objects,
                            ignore_q5_push=dual_run_ignore_q5
                        )
                        wsc_bundle = process_tournament_data(
                            st.session_state.engine, "WSC", wsc_date, tournament_objects,
                            ignore_q5_push=dual_run_ignore_q5
                        )
                        if wysc_bundle:
                            st.session_state.results["WYSC"] = wysc_bundle
                        if wsc_bundle:
                            st.session_state.results["WSC"] = wsc_bundle

                        if wysc_bundle or wsc_bundle:
                            sync_active_dataset("WSC" if wsc_bundle else "WYSC")
                            st.session_state.show_dual_run_form = False
                            st.session_state.dual_run_done_message = (
                                f"WSC: {'calculated' if wsc_bundle else 'FAILED - check the WSC date'}. "
                                f"WYSC: {'calculated' if wysc_bundle else 'FAILED - check the WYSC date'}. "
                                f"View results in the National Leaderboard and Individual Player Audit tabs."
                            )
                            st.toast("WAR calculations generated for WSC and WYSC.")
                            st.rerun()
                        else:
                            st.error("Both configurations failed - check the event dates entered.")
            with cancel_col:
                if st.button("Cancel", key="dual_run_cancel"):
                    st.session_state.show_dual_run_form = False
                    st.rerun()

        if st.session_state.get("dual_run_done_message"):
            st.success(st.session_state.dual_run_done_message)
            if st.button("Dismiss", key="dual_run_dismiss"):
                st.session_state.dual_run_done_message = None
                st.rerun()

        st.markdown("---")

        structure = get_archive_structure(st.session_state.engine)
        if not structure:
            st.warning(f"No archive found. Expected year subfolders (e.g. '2024', '2025') under "
                       f"'{TOURNAMENT_ARCHIVE_DIR}/' next to main.py.")
        else:
            quality_objects = load_all_archive_tournament_objects(st.session_state.engine)
            quality_warnings = {
                obj['source_filename']: obj['warnings']
                for obj in quality_objects if obj.get('warnings')
            }
            total_quality_warnings = sum(len(w) for w in quality_warnings.values())
            if total_quality_warnings:
                with st.expander(
                    f"⚠ Data Quality: {total_quality_warnings} parse warning(s) across "
                    f"{len(quality_warnings)} file(s)", expanded=False
                ):
                    for fname, warns in quality_warnings.items():
                        st.markdown(f"**{fname}**")
                        for w in warns:
                            st.caption(f"- {w}")
            else:
                st.caption(f"✓ Data Quality: no parse warnings across "
                           f"{len(quality_objects)} tournament file(s).")

            year_tabs = st.tabs([year for year, _ in structure] + ["All Tournaments"])
            for year_tab, (year, files) in zip(year_tabs[:-1], structure):
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

                            fname_key = f"archive_fname_{year}_{fname}"
                            st.text_input("File name", value=fname, key=fname_key)

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
                            delete_confirm_key = f"archive_delete_confirm_{year}_{fname}"
                            new_fname = st.session_state[fname_key].strip()
                            # The name GitHub still has this file under, if a rename hasn't been
                            # pushed yet - tracked across saves so a rename never gets "lost" and
                            # leaves the old filename duplicated on GitHub after a later push.
                            original_github_name = st.session_state.archive_renames.get((year, fname), fname)

                            action_col1, action_col2, action_col3, action_col4, _spacer = st.columns(
                                [1.3, 2, 1, 1, 3], gap="small"
                            )
                            with action_col1:
                                if st.button("Save Changes", key=f"archive_save_{year}_{fname}"):
                                    if not new_fname:
                                        st.error("File name can't be empty.")
                                    elif not new_fname.lower().endswith(".txt"):
                                        st.error("File name must end in .txt")
                                    else:
                                        new_fpath = os.path.join(TOURNAMENT_ARCHIVE_DIR, year, new_fname)
                                        if new_fname != fname and os.path.exists(new_fpath):
                                            st.error(f"'{new_fname}' already exists in '{year}/'.")
                                        else:
                                            try:
                                                with open(new_fpath, "w", encoding="utf-8") as f:
                                                    f.write(edited_text)
                                                if new_fname != fname:
                                                    os.remove(fpath)
                                                    # Carry forward the true original name (not just
                                                    # the immediately-previous one) so a chain of local
                                                    # renames still resolves to a single GitHub delete
                                                    # once pushed.
                                                    st.session_state.archive_renames.pop((year, fname), None)
                                                    if new_fname != original_github_name:
                                                        st.session_state.archive_renames[(year, new_fname)] = (
                                                            original_github_name
                                                        )
                                                    st.success(f"Saved as '{new_fname}' (renamed from "
                                                               f"'{fname}') on this machine. Push to "
                                                               f"GitHub to rename it there too.")
                                                else:
                                                    st.success(f"Saved changes to {fname} on this machine.")
                                                st.rerun()
                                            except OSError as e:
                                                st.error(f"Could not save {new_fname}: {e}")
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
                            with action_col4:
                                if st.button("Delete", key=f"archive_delete_{year}_{fname}"):
                                    st.session_state[delete_confirm_key] = True
                                    st.rerun()

                            if st.session_state.get(delete_confirm_key):
                                st.warning(f"This permanently deletes '{fname}' on this machine AND "
                                           f"pushes that deletion to GitHub (origin/main) - it will "
                                           f"stop appearing in 'All Tournaments' and be left out of "
                                           f"any WAR calculation you run afterwards. Re-enter the "
                                           f"archive password to confirm.")
                                del_pwd = st.text_input(
                                    "Password", type="password", key=f"archive_delete_pwd_{year}_{fname}"
                                )
                                del_confirm_col, del_cancel_col, _spacer = st.columns([2, 1, 6])
                                with del_confirm_col:
                                    if st.button("Confirm Delete",
                                                 key=f"archive_delete_confirm_btn_{year}_{fname}"):
                                        if del_pwd != ARCHIVE_PASSWORD:
                                            st.error("Incorrect password.")
                                        else:
                                            try:
                                                os.remove(fpath)
                                                st.session_state.archive_renames.pop((year, fname), None)
                                                try:
                                                    delete_archive_file_from_github(
                                                        os.path.join(TOURNAMENT_ARCHIVE_DIR, year,
                                                                     original_github_name),
                                                        f"Delete {original_github_name} ({year}) via "
                                                        f"WAR Calculator dashboard{admin_tag()}"
                                                    )
                                                    st.session_state[delete_confirm_key] = False
                                                    st.success(f"Deleted '{fname}' on this machine and "
                                                               f"on GitHub.")
                                                except (KeyError, requests.exceptions.RequestException) as e:
                                                    st.session_state[delete_confirm_key] = False
                                                    st.warning(f"Deleted '{fname}' on this machine, but "
                                                               f"the GitHub deletion failed: {e}. Remove "
                                                               f"it from GitHub manually if needed.")
                                                st.rerun()
                                            except OSError as e:
                                                st.error(f"Could not delete {fname}: {e}")
                                with del_cancel_col:
                                    if st.button("Cancel", key=f"archive_delete_cancel_{year}_{fname}"):
                                        st.session_state[delete_confirm_key] = False
                                        st.rerun()

                            if st.session_state.get(confirm_key):
                                is_rename = bool(new_fname) and new_fname != original_github_name
                                if is_rename:
                                    st.warning(f"This renames '{original_github_name}' to '{new_fname}' on "
                                               f"GitHub (origin/main) - pushes the new file and removes the "
                                               f"old one, in two commits, with no review step. Confirm you "
                                               f"want to publish this.")
                                else:
                                    st.warning("This pushes directly to the public GitHub repo (origin/main) "
                                               "with no review step. Confirm you want to publish this edit.")
                                diff_lines = list(difflib.unified_diff(
                                    content.splitlines(), edited_text.splitlines(),
                                    fromfile="on disk", tofile="your edit", lineterm=""
                                ))
                                if diff_lines:
                                    st.code("\n".join(diff_lines), language="diff")
                                elif not is_rename:
                                    st.caption("No textual changes detected - this will push the file as-is.")

                                confirm_col, cancel_col, _spacer = st.columns([2, 1, 6])
                                with confirm_col:
                                    if st.button("Confirm & Push", key=f"archive_push_confirm_btn_{year}_{fname}"):
                                        if not new_fname:
                                            st.error("File name can't be empty.")
                                        elif not new_fname.lower().endswith(".txt"):
                                            st.error("File name must end in .txt")
                                        else:
                                            new_fpath = os.path.join(TOURNAMENT_ARCHIVE_DIR, year, new_fname)
                                            try:
                                                with open(new_fpath, "w", encoding="utf-8") as f:
                                                    f.write(edited_text)
                                                if new_fname != fname and os.path.exists(fpath):
                                                    os.remove(fpath)

                                                commit_msg = (
                                                    f"Rename {original_github_name} to {new_fname} "
                                                    f"({year}) via WAR Calculator dashboard" if is_rename else
                                                    f"Edit {fname} ({year}) via WAR Calculator dashboard"
                                                ) + admin_tag()
                                                pushed = push_archive_file_to_github(new_fpath, commit_msg)

                                                removed_old = False
                                                if is_rename:
                                                    old_repo_path = os.path.join(
                                                        TOURNAMENT_ARCHIVE_DIR, year, original_github_name
                                                    )
                                                    removed_old = delete_archive_file_from_github(
                                                        old_repo_path,
                                                        f"Remove {original_github_name} ({year}) - renamed "
                                                        f"to {new_fname} via WAR Calculator dashboard"
                                                        f"{admin_tag()}"
                                                    )

                                                # GitHub and local now agree under new_fname - drop any
                                                # rename tracking for both the old and new names.
                                                st.session_state.archive_renames.pop((year, fname), None)
                                                st.session_state.archive_renames.pop((year, new_fname), None)

                                                st.session_state[confirm_key] = False
                                                if is_rename:
                                                    st.success(
                                                        f"Renamed '{original_github_name}' to '{new_fname}' "
                                                        f"on GitHub"
                                                        + (" and removed the old file there."
                                                           if removed_old else
                                                           " (old file wasn't on GitHub, nothing to "
                                                           "remove there).")
                                                    )
                                                elif pushed:
                                                    st.success(f"Pushed {new_fname} to the GitHub repo (origin/main).")
                                                else:
                                                    st.info(f"{new_fname} already matches what's on GitHub - "
                                                            f"nothing to push.")
                                                st.rerun()
                                            except (OSError, KeyError, requests.exceptions.RequestException) as e:
                                                st.error(f"Could not push {new_fname} to GitHub: {e}")
                                with cancel_col:
                                    if st.button("Cancel", key=f"archive_push_cancel_{year}_{fname}"):
                                        st.session_state[confirm_key] = False
                                        st.rerun()

            with year_tabs[-1]:
                title_col, add_col = st.columns([6, 1], vertical_alignment="center")
                with title_col:
                    st.subheader("All Tournaments (Latest First)")
                with add_col:
                    if st.button("+ Add Tournament File", key="open_add_file_dialog"):
                        add_tournament_file_dialog()
                history_rows = build_tournament_history(st.session_state.engine)
                if not history_rows:
                    st.warning(f"No tournament files found under '{TOURNAMENT_ARCHIVE_DIR}/'.")
                else:
                    hist_df = pd.DataFrame(history_rows)
                    hist_df['Date'] = hist_df['Date'].dt.strftime('%Y-%m-%d')
                    hist_df.insert(0, "No.", range(1, len(hist_df) + 1))
                    st.dataframe(
                        hist_df,
                        use_container_width=True,
                        hide_index=True,
                        column_config={
                            "No.": st.column_config.NumberColumn("No.", width="small"),
                            "Source File": st.column_config.TextColumn("Source File", width="large"),
                        },
                    )
                    st.caption(f"{len(history_rows)} tournaments found across {len(structure)} year folder(s).")
                    st.download_button(
                        "Download ZIP of All Tournaments",
                        data=build_archive_zip(),
                        file_name="tournament_files_archive.zip",
                        mime="application/zip",
                        key="dl_full_archive_zip",
                    )
