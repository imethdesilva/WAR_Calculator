import streamlit as st
import pandas as pd
import re
import math
import csv
from datetime import datetime
from dateutil.relativedelta import relativedelta
import os
import io
import google.generativeai as genai
import PyPDF2

genai.configure(api_key=st.secrets["GEMINI_KEY"])
model = genai.GenerativeModel('gemini-2.5-flash')

def load_pdf_text(filename):
    if not os.path.exists(filename):
        return ""
    try:
        text = ""
        with open(filename, "rb") as f:
            reader = PyPDF2.PdfReader(f)
            for page in reader.pages:
                content = page.extract_text()
                if content:
                    text += content + "\n"
        return text
    except Exception as e:
        return f"Error reading PDF: {e}"

SYSTEM_INSTRUCTION = """
# IDENTITY
You are MUUMUUS, the intelligent AI Consultant for the Scrabble Federation of Sri Lanka. 

# BEHAVIOR GUIDELINES
- DO NOT introduce yourself or state your name in every response. Only state your name if the user specifically asks who you are or at the very beginning of a new session.
- Maintain a natural, fluid conversation. Refer back to previous things the user said when appropriate (e.g., "As we discussed earlier regarding your game count...").
- Be professional, warm, and helpful. You are a consultant, not a robot.

# CITATION PROTOCOL
- You must always cite the official selection criteria PDF. Example: "According to Page 6, Section 2..."
- Use the provided context to answer questions accurately.

# CORE RULES
- WAR window: 20 months.
- Quads: Jan-Apr, May-Aug, Sep-Dec.
- WSC: 80 games / 5 tours.
- WYSC: 50 games / 3 tours.
- Tie-break: 1st Current Rating, 2nd WAR (2 decimals).
"""

PDF_CONTENT = load_pdf_text("Selections Criteria 2024.pdf")

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
        current_section_games = 0

        for line in lines:
            raw_line = line
            line = line.strip()
            if not line: continue

            game_header = re.search(r'(\d+)\s+games', line.lower())
            if game_header:
                current_section_games = int(game_header.group(1))
                continue

            if re.match(r'^\d+\s+', line):
                numeric_blocks = re.findall(r'\(?\s*[\d\-+.]+\s*\)?', line)
                if len(numeric_blocks) < 2: continue 
                
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
                except: continue

        return {
            "name": t_name,
            "date": t_date,
            "players": players_found
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

            for f in uploaded_files:
                content = f.read().decode('utf-8', errors='ignore')
                data = st.session_state.engine.parse_tournament_file(content)
                if data:
                    all_tour_dates.append(data['date'])
                    parsed_tournament_objects.append(data)

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
                st.session_state.processed_files = True
                st.success("Calculated WAR using Seasonal Calendar Weights")
                st.rerun()

# Main
st.title("National Scrabble Selections - WAR Calculator")
st.caption("Official Administrative System for Weighted Average Rating (WAR) Calculation")

tabs = st.tabs(["Selection Overview", "National Leaderboard", "Individual Player Audit", "Policy & Criteria", "AI Assistant"])

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
            hide_zero_war = st.checkbox("Hide players with WAR = 0", value=False)

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
                st.download_button(
                    "Export Full Selection Report (CSV)",
                    data=report_csv.encode('utf-8'),
                    file_name=f"{conf['mode']}_selection_report.csv",
                    mime='text/csv'
                )
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
                    st.error(f"⚠️ {inact['remark']}")
                else:
                    st.info(f"ℹ️ {inact['remark']}")

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

with tabs[4]:
    st.header("AI Selection Assistant")

    if PDF_CONTENT:
        st.success(" MUUMUUS is online and has read the Selection Criteria.")
    else:
        st.warning("MUUMUUS is online but the Criteria PDF was not found.")

    if "messages" not in st.session_state:
        st.session_state.messages = []

    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])

    if prompt := st.chat_input("Ask MUUMUUS about WSC/WYSC rules..."):
        st.chat_message("user").markdown(prompt)
        st.session_state.messages.append({"role": "user", "content": prompt})

        context_for_ai = f"""
        {SYSTEM_INSTRUCTION}
        
        REFERENCE DOCUMENT CONTENT:
        {PDF_CONTENT}
        
        CONVERSATION HISTORY:
        """
        for msg in st.session_state.messages[-6:]:
            role_name = "Player" if msg["role"] == "user" else "MUUMUUS"
            context_for_ai += f"{role_name}: {msg['content']}\n"

        context_for_ai += f"\nMUUMUUS, please respond to the player's latest request while citing the PDF accurately."

        try:
           
            response = model.generate_content(context_for_ai)
            answer = response.text
            
            with st.chat_message("assistant"):
                st.markdown(answer)
            
            st.session_state.messages.append({"role": "assistant", "content": answer})
            
        except Exception as e:
            st.error(f"MUUMUUS encountered an error: {e}")
            st.info("Check the Developer Panel below to verify your API Key and Model status.")

    st.markdown("---")
    with st.expander("🛠️ Developer Panel"):
        st.write("Use these tools to manage the AI session.")
        
        col1, col2 = st.columns(2)
        
        with col1:
            if st.button("List Available Models"):
                try:
                    available_models = []
                    for m in genai.list_models():
                        if 'generateContent' in m.supported_generation_methods:
                            available_models.append(m.name)
                    st.json(available_models)
                    
                    current_model_name = "models/gemini-1.5-flash-latest"
                    if any(current_model_name in m for m in available_models):
                        st.success(f"Confirmed: {current_model_name} is active.")
                except Exception as e:
                    st.error(f"Error: {e}")
        
        with col2:
            if st.button("Clear History & Reset MUUMUUS"):
                st.session_state.messages = []
                st.rerun()