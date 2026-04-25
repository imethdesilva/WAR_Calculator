import streamlit as st
import pandas as pd
import re
from datetime import datetime
from dateutil.relativedelta import relativedelta
import os

# --- CORE ENGINE LOGIC ---
class SelectionsEngine:
    def __init__(self):
        self.quad_ranges = []
        self.config = {}

    def calculate_configuration(self, mode, intl_date_str):
        try:
            intl_date = datetime.strptime(intl_date_str, "%d.%m.%Y")
            offset = 6 if mode == "WSC" else 3
            cutoff_date = intl_date - relativedelta(months=offset)
            
            config = {
                "mode": mode,
                "intl_date": intl_date,
                "cutoff_date": cutoff_date,
                "req_games": 80 if mode == "WSC" else 50,
                "req_tours": 5 if mode == "WSC" else 3,
                "req_recent": 2 if mode == "WSC" else 1,
                "min_quads": 3
            }

            quads = []
            curr_end = cutoff_date
            for i in range(5, 0, -1):
                start = curr_end - relativedelta(months=4)
                weight = 1.0 + (i-1)*0.25
                quads.append({"quad": i, "start": start, "end": curr_end, "weight": weight})
                curr_end = start
            
            return config, quads
        except Exception as e:
            st.error(f"Configuration Error: {str(e)}")
            return None, None

    def parse_tournament_file(self, content):
        lines = content.splitlines()
        t_date, t_name = None, "Unknown"
        
        for line in lines[:5]:
            date_match = re.search(r'(\d{2}\.\d{2}\.\d{4})', line)
            if date_match:
                t_date = datetime.strptime(date_match.group(1), "%d.%m.%Y")
                t_name = line.split(date_match.group(1))[-1].strip()
                break
        
        if not t_date: return None
        
        is_major = "18 games" in content.lower()
        players = []
        current_games = 0

        for line in lines:
            line = line.strip()
            if not line: continue
            game_header = re.search(r'^(\d+)\s+games', line.lower())
            if game_header:
                current_games = int(game_header.group(1))
                continue

            if re.match(r'^\d+\s+', line):
                numeric_blocks = re.findall(r'\(?\s*[\d\-+]+\s*\)?', line)
                if len(numeric_blocks) < 3: continue 
                
                try:
                    new_rating = int(numeric_blocks[-1].replace('(', '').replace(')', '').strip())
                    name_clean = re.sub(r'^\d+\s+[\d\-+]+\s+[\d\-+*&]+\s+', '', line)
                    name_clean = re.sub(r'[\d\-+*&\(\)\s]+$', '', name_clean).strip()
                    if name_clean:
                        players.append({"name": name_clean, "rating": new_rating, "games": current_games})
                except: continue

        return {"name": t_name, "date": t_date, "is_major": is_major, "players": players}

# --- STREAMLIT UI SETUP ---
st.set_page_config(page_title="National Selections Dashboard", layout="wide")

# Custom CSS for a professional look
st.markdown("""
    <style>
    .main { background-color: #f8f9fa; }
    .stMetric { background-color: #ffffff; border: 1px solid #e9ecef; padding: 15px; border-radius: 5px; }
    div.stButton > button:first-child { background-color: #004a99; color: white; border-radius: 0px; width: 100%; }
    .stTabs [data-baseweb="tab-list"] { gap: 24px; }
    .stTabs [data-baseweb="tab"] { font-weight: 600; color: #495057; }
    </style>
    """, unsafe_allow_html=True)

if 'engine' not in st.session_state:
    st.session_state.engine = SelectionsEngine()
    st.session_state.players_db = {}
    st.session_state.processed_files = False

# --- SIDEBAR: CONTROLS ---
with st.sidebar:
    st.title("Administrative Settings")
    st.markdown("---")
    selected_mode = st.selectbox("Tournament Classification", ["WSC", "WYSC"])
    event_date = st.text_input("International Event Date (DD.MM.YYYY)", value="15.10.2025")
    
    if st.button("Initialize Selection Window"):
        config, quads = st.session_state.engine.calculate_configuration(selected_mode, event_date)
        if config:
            st.session_state.config = config
            st.session_state.quad_ranges = quads
            st.success("Configuration Validated")

    st.markdown("---")
    st.markdown("### Data Ingestion")
    uploaded_files = st.file_uploader("Upload Tournament Results (.txt)", accept_multiple_files=True)
    
    if uploaded_files and 'config' in st.session_state:
        if st.button("Process Tournament Data"):
            db = {}
            for f in uploaded_files:
                content = f.read().decode('utf-8', errors='ignore')
                data = st.session_state.engine.parse_tournament_file(content)
                
                if data:
                    q_info = next((q for q in st.session_state.quad_ranges if q['start'] <= data['date'] < data['end']), None)
                    if q_info:
                        for p in data['players']:
                            name = p['name']
                            if name not in db:
                                db[name] = {"history": [], "total_games": 0, "tournaments": 0, "quads": set(), "major_count": 0, "recent_count": 0}
                            
                            db[name]["history"].append({
                                "Date": data['date'].strftime('%Y-%m-%d'),
                                "Tournament": data['name'],
                                "Quad": q_info['quad'],
                                "Weight": q_info['weight'],
                                "Rating": p['rating'],
                                "WeightedValue": p['rating'] * q_info['weight'],
                                "Games": p['games']
                            })
                            db[name]["total_games"] += p['games']
                            db[name]["tournaments"] += 1
                            db[name]["quads"].add(q_info['quad'])
                            if data['is_major'] and p['games'] >= 18: db[name]["major_count"] += 1
                            if q_info['quad'] >= 4: db[name]["recent_count"] += 1
            
            st.session_state.players_db = db
            st.session_state.processed_files = True
            st.rerun()

# --- MAIN CONTENT ---
st.title("National Scrabble Selections Management Portal")
st.caption("Official System for Weighted Average Rating (WAR) Calculation and Eligibility Auditing")

tabs = st.tabs(["Selection Overview", "National Leaderboard", "Individual Player Audit", "Policy & Criteria"])

# TAB 1: OVERVIEW
with tabs[0]:
    if 'config' in st.session_state:
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Target Event", st.session_state.config['mode'])
        col2.metric("Cutoff Date", st.session_state.config['cutoff_date'].strftime('%d %b %Y'))
        col3.metric("Required Games", st.session_state.config['req_games'])
        col4.metric("Recent Quads Req", st.session_state.config['req_recent'])
        
        st.subheader("Quadrimester Weighting Schedule")
        q_df = pd.DataFrame(st.session_state.quad_ranges)
        q_df['start'] = q_df['start'].dt.strftime('%Y-%m-%d')
        q_df['end'] = q_df['end'].dt.strftime('%Y-%m-%d')
        st.table(q_df[['quad', 'weight', 'start', 'end']].rename(columns={'quad': 'Period', 'weight': 'Multiplication Factor'}))
    else:
        st.info("Please initialize the Selection Window from the sidebar to begin.")

# TAB 2: LEADERBOARD
with tabs[1]:
    if st.session_state.processed_files:
        rows = []
        conf = st.session_state.config
        for name, data in st.session_state.players_db.items():
            tw = sum(h['Weight'] for h in data['history'])
            twr = sum(h['WeightedValue'] for h in data['history'])
            war = round(twr / tw) if tw > 0 else 0
            
            eligible = (war >= 800 and 
                        data['total_games'] >= conf['req_games'] and 
                        data['tournaments'] >= conf['req_tours'] and 
                        len(data['quads']) >= conf['min_quads'] and 
                        data['major_count'] >= 1 and 
                        data['recent_count'] >= conf['req_recent'])
            
            rows.append({
                "Player Name": name, "WAR": war, "Quads": len(data['quads']), 
                "Tours": data['tournaments'], "Total Games": data['total_games'], 
                "Majors": data['major_count'], "Recent Activities": data['recent_count'],
                "Status": "QUALIFIED" if eligible else "INELIGIBLE"
            })
        
        df = pd.DataFrame(rows).sort_values(by="WAR", ascending=False).reset_index(drop=True)
        df.index += 1
        st.dataframe(df, use_container_width=True)
        
        csv = df.to_csv(index=False).encode('utf-8')
        st.download_button("Export Results to CSV", data=csv, file_name="national_rankings.csv", mime='text/csv')
    else:
        st.warning("No tournament data processed. Use the sidebar to upload results.")

# TAB 3: PLAYER AUDIT
with tabs[2]:
    if st.session_state.processed_files:
        player_select = st.selectbox("Search Player for Detailed Audit", sorted(st.session_state.players_db.keys()))
        if player_select:
            p_data = st.session_state.players_db[player_select]
            st.subheader(f"Participation History: {player_select}")
            st.table(pd.DataFrame(p_data["history"]))
    else:
        st.info("Awaiting data processing.")

# TAB 4: POLICY & CRITERIA
with tabs[3]:
    st.header("National Selection Policy Summary")
    
    col_a, col_b = st.columns(2)
    with col_a:
        st.subheader("WSC Selection (Senior)")
        st.markdown("""
        - **Buffer Period:** 6 Months prior to event.
        - **Game Requirement:** Minimum 80 rated games.
        - **Tournaments:** Minimum 5 tournaments within 20 months.
        - **Continuity:** Must span at least 3 Quadrimesters.
        - **Recency:** 2 tournaments within the last 8 months.
        - **Major:** Must include at least one 18-round event.
        """)
        
    with col_b:
        st.subheader("WYSC Selection (Youth)")
        st.markdown("""
        - **Buffer Period:** 3 Months prior to event.
        - **Game Requirement:** Minimum 50 rated games.
        - **Tournaments:** Minimum 3 tournaments within 20 months.
        - **Continuity:** Must span at least 3 Quadrimesters.
        - **Recency:** 1 tournament within the last 8 months.
        - **Major:** Must include at least one 18-round event.
        """)
    
    st.markdown("---")
    st.subheader("Official Documentation")
    
    # PDF Download
    if os.path.exists("Selections Criteria 2024.pdf"):
        with open("Selections Criteria 2024.pdf", "rb") as f:
            st.download_button(
                label="Download Full Selection Criteria (PDF)",
                data=f,
                file_name="Selections_Criteria_2024.pdf",
                mime="application/pdf"
            )
    else:
        st.error("Criteria PDF not found in the local directory.")

    st.markdown("### Technical Methodology")
    st.write("For a detailed breakdown of the Weighted Average Rating (WAR) mathematical model, refer to the following technical documentation:")
    st.link_button("Read Technical Article on Medium", "https://medium.com/@imethdesilva/weighted-ratings-and-the-national-scrabble-selections-process-567231d9c486")