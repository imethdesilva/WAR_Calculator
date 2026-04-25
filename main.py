import streamlit as st
import pandas as pd
import re
from datetime import datetime
from dateutil.relativedelta import relativedelta
import os

# --- CORE SELECTIONS ENGINE ---
class SelectionsEngine:
    def __init__(self):
        self.quad_ranges = []
        self.config = {}

    def calculate_configuration(self, mode, intl_date_str):
        try:
            # Parse target date and set selection parameters
            intl_date = datetime.strptime(intl_date_str, "%d.%m.%Y")
            # WSC has a 6-month buffer; WYSC/others have 3 months
            offset = 6 if mode == "WSC" else 3
            cutoff_date = intl_date - relativedelta(months=offset)
            
            config = {
                "mode": mode,
                "intl_date": intl_date,
                "cutoff_date": cutoff_date,
                "req_games": 80 if mode == "WSC" else 50,
                "req_tours": 5 if mode == "WSC" else 3,
                "req_recent": 2 if mode == "WSC" else 1,
                "min_quads": 3,
                "min_war": 800
            }

            # Generate 5 Quadrimesters (4-month blocks) ending at cutoff
            quads = []
            curr_end = cutoff_date
            for i in range(5, 0, -1):
                start = curr_end - relativedelta(months=4)
                # Weights: 1.0, 1.25, 1.5, 1.75, 2.0
                weight = 1.0 + (i-1)*0.25
                quads.append({"quad": i, "start": start, "end": curr_end, "weight": weight})
                curr_end = start
            
            return config, quads
        except Exception as e:
            st.error(f"Configuration Error: {str(e)}")
            return None, None

    def parse_tournament_file(self, content):
        lines = content.splitlines()
        t_date, t_name = None, "Unknown Tournament"
        
        # Capture Date (DD.MM.YYYY) and Name from the first line
        for line in lines[:2]:
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

            # Track game count sections (e.g., "18 games")
            game_header = re.search(r'^(\d+)\s+games', line.lower())
            if game_header:
                current_section_games = int(game_header.group(1))
                continue

            # Identify Player Rows
            if re.match(r'^\d+\s+', line):
                numeric_blocks = re.findall(r'\(?\s*[\d\-+]+\s*\)?', line)
                if len(numeric_blocks) < 2: continue 
                
                try:
                    # New Rating is the final numeric block
                    new_rating = int(numeric_blocks[-1].replace('(', '').replace(')', '').strip())

                    # Old Rating
                    old_rating = 0
                    if len(numeric_blocks) >= 3:
                        try:
                            old_rating = int(numeric_blocks[-3].replace('(', '').replace(')', '').strip())
                        except: pass

                    # Name Extraction Logic
                    name_part = re.sub(r'^\s*\d+\s+[\d\-+]+\s+[\d\-+*&]+\s+', '', raw_line)
                    name_part = re.sub(r'[\d\-+*&\(\)\s]+$', '', name_part).strip()
                    name_part = name_part.lstrip('*') 
                    
                    if name_part:
                        players_found.append({
                            "name": name_part, 
                            "old_rating": old_rating,
                            "new_rating": new_rating, 
                            "games": current_section_games
                        })
                except: continue

        return {
            "name": t_name, 
            "date": t_date, 
            "is_major": (current_section_games >= 18), 
            "players": players_found
        }

# --- UI CONFIGURATION ---
st.set_page_config(page_title="National Selections Dashboard", layout="wide")

# Theme-aware CSS (Ensures visibility in both Light and Dark modes)
st.markdown("""
    <style>
    /* Metric Card Styling */
    div[data-testid="stMetric"] {
        background-color: var(--secondary-background-color);
        border: 1px solid var(--border-color);
        padding: 15px;
        border-radius: 10px;
        box-shadow: 0 2px 4px rgba(0,0,0,0.1);
    }
    
    /* Dynamic Text Colors based on Streamlit Theme */
    div[data-testid="stMetricValue"] > div {
        color: var(--text-color) !important;
        font-weight: 700;
    }

    div[data-testid="stMetricLabel"] > div {
        color: var(--text-color);
        opacity: 0.8;
        font-weight: 600;
    }

    /* Button Styling */
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
    </style>
    """, unsafe_allow_html=True)

if 'engine' not in st.session_state:
    st.session_state.engine = SelectionsEngine()
    st.session_state.players_db = {}
    st.session_state.processed_files = False

# --- SIDEBAR ---
with st.sidebar:
    st.title("Administrative Panel")
    selected_mode = st.selectbox("Tournament Classification", ["WSC", "WYSC"])
    event_date = st.text_input("International Event Date (DD.MM.YYYY)", value="15.10.2025")
    
    if st.button("Initialize Selection Window"):
        config, quads = st.session_state.engine.calculate_configuration(selected_mode, event_date)
        if config:
            st.session_state.config = config
            st.session_state.quad_ranges = quads
            st.success("Configuration Validated")

    st.markdown("---")
    st.subheader("Data Ingestion")
    uploaded_files = st.file_uploader("Upload Tournament Files (.txt)", accept_multiple_files=True)
    
    if uploaded_files and 'config' in st.session_state:
        if st.button("Process Tournament Results"):
            db = {}
            for f in uploaded_files:
                content = f.read().decode('utf-8', errors='ignore')
                data = st.session_state.engine.parse_tournament_file(content)
                
                if data:
                    # Logic: Check if tournament date falls within any defined Quadrimester period
                    q_info = next((q for q in st.session_state.quad_ranges if q['start'] <= data['date'] < q['end']), None)
                    if q_info:
                        for p in data['players']:
                            name = p['name']
                            if name not in db:
                                db[name] = {"history": [], "total_games": 0, "tournaments": 0, "quads": set(), "major_count": 0, "recent_count": 0, "current_rating": 0}
                            
                            db[name]["history"].append({
                                "Date": data['date'].strftime('%Y-%m-%d'),
                                "Tournament": data['name'],
                                "Quad": q_info['quad'],
                                "Weight": q_info['weight'],
                                "Old Rating": p['old_rating'],
                                "New Rating": p['new_rating'],
                                "WeightedVal": p['new_rating'] * q_info['weight'],
                                "Games": p['games']
                            })
                            db[name]["total_games"] += p['games']
                            db[name]["tournaments"] += 1
                            db[name]["quads"].add(q_info['quad'])
                            db[name]["current_rating"] = p['new_rating']
                            if data['is_major']: db[name]["major_count"] += 1
                            if q_info['quad'] >= 4: db[name]["recent_count"] += 1
            
            if db:
                st.session_state.players_db = db
                st.session_state.processed_files = True
                st.rerun()

# --- MAIN DASHBOARD ---
st.title("National Scrabble Selections Management Portal")
st.caption("Official Administrative System for Weighted Average Rating (WAR) Calculation")

tabs = st.tabs(["Selection Overview", "National Leaderboard", "Individual Player Audit", "Policy & Criteria"])

# TAB 1: OVERVIEW
with tabs[0]:
    if 'config' in st.session_state:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Tournament", st.session_state.config['mode'])
        c2.metric("Cutoff Date", st.session_state.config['cutoff_date'].strftime('%d %b %Y'))
        c3.metric("Min Games Req", st.session_state.config['req_games'])
        c4.metric("Recent Activity", f"{st.session_state.config['req_recent']} Tours")
        
        st.subheader("Quadrimester Weighting Schedule")
        q_df = pd.DataFrame(st.session_state.quad_ranges)
        q_df['start'] = q_df['start'].dt.strftime('%Y-%m-%d')
        q_df['end'] = q_df['end'].dt.strftime('%Y-%m-%d')
        st.table(q_df[['quad', 'weight', 'start', 'end']].rename(columns={'quad': 'Period', 'weight': 'Weight Factor'}))
    else:
        st.info("Awaiting Configuration. Please initialize the selection window in the sidebar.")

# TAB 2: LEADERBOARD
with tabs[1]:
    if st.session_state.processed_files:
        rows = []
        conf = st.session_state.config
        for name, data in st.session_state.players_db.items():
            tw = sum(h['Weight'] for h in data['history'])
            twr = sum(h['WeightedVal'] for h in data['history'])
            
            war_precise = twr / tw if tw > 0 else 0
            war = round(war_precise)
            
            eligible = (war >= conf['min_war'] and 
                        data['total_games'] >= conf['req_games'] and 
                        data['tournaments'] >= conf['req_tours'] and 
                        len(data['quads']) >= 3 and 
                        data['major_count'] >= 1 and 
                        data['recent_count'] >= conf['req_recent'])
            
            rows.append({
                "Player Name": name, "WAR": war, 
                "Current Rating": data['current_rating'], # PDF Page 6: Tie Breaker 1
                "WAR Precise": round(war_precise, 2),    # PDF Page 6: Tie Breaker 2
                "Quads": len(data['quads']), 
                "Tours": data['tournaments'], "Total Games": data['total_games'], 
                "Status": "QUALIFIED" if eligible else "INELIGIBLE"
            })
        
        if rows:
            df = pd.DataFrame(rows).sort_values(by=["WAR", "Current Rating", "WAR Precise"], ascending=False).reset_index(drop=True)
            df.index += 1
            
            def color_status(val):
                color = '#28a745' if val == "QUALIFIED" else '#dc3545'
                return f'color: {color}; font-weight: bold;'

            # FIX: Updated applymap to map for Pandas 2.1+ compatibility
            st.dataframe(df.style.map(color_status, subset=['Status']), use_container_width=True)
            
            csv = df.to_csv(index=False).encode('utf-8')
            st.download_button("Export Results (CSV)", data=csv, file_name="rankings.csv", mime='text/csv')
    else:
        st.warning("Upload result files in the sidebar to generate rankings.")

# TAB 3: AUDIT
with tabs[2]:
    if st.session_state.processed_files:
        player_select = st.selectbox("Search Player for Audit", sorted(st.session_state.players_db.keys()))
        if player_select:
            p_data = st.session_state.players_db[player_select]
            st.subheader(f"Participation History: {player_select}")
            st.dataframe(pd.DataFrame(p_data["history"]), use_container_width=True)
    else:
        st.info("Awaiting data processing.")

# TAB 4: POLICY
with tabs[3]:
    st.header("National Selection Policy Summary")
    col_a, col_b = st.columns(2)
    with col_a:
        st.subheader("WSC Selection")
        st.markdown("- **Window:** 20 Months\n- **Games:** 80 Min\n- **Recency:** 2 tours in Q4/Q5\n- **Buffer:** 6 Months")
    with col_b:
        st.subheader("WYSC Selection")
        st.markdown("- **Window:** 20 Months\n- **Games:** 50 Min\n- **Recency:** 1 tour in Q4/Q5\n- **Buffer:** 3 Months")
    
    st.markdown("---")
    st.subheader("Official Documentation")
    pdf_path = "Selections Criteria 2024.pdf"
    if os.path.exists(pdf_path):
        with open(pdf_path, "rb") as f:
            st.download_button("Download Official Criteria PDF", data=f, file_name="Selections_Criteria_2024.pdf")
    
    st.link_button("Read Technical Documentation on Medium", "https://medium.com/@imethdesilva/weighted-ratings-and-the-national-scrabble-selections-process-567231d9c486")