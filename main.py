import streamlit as st
import pandas as pd
import re
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
    
    def calculate_configuration(self, mode, intl_date_str, tournament_dates=[]):
        try:
            intl_date = datetime.strptime(intl_date_str, "%d.%m.%Y")
            offset = 6 if mode == "WSC" else 3
            cutoff_date = intl_date - relativedelta(months=offset)
            
            q5_start, q5_end = self.get_season_bounds(cutoff_date)

            tours_in_q5 = [d for d in tournament_dates if q5_start <= d <= cutoff_date]

            is_first_month = (cutoff_date.month in [1, 5, 9])

            if len(tours_in_q5) == 0 or (is_first_month and len(tours_in_q5) == 1):

                actual_q5_end = self.get_prev_season_end(q5_start)
            else:
                actual_q5_end = q5_end

            quads = []
            curr_end = actual_q5_end
            weights = [2.0, 1.75, 1.50, 1.25, 1.0] # PDF page 3
            
            for i in range(5, 0, -1):
                q_start, q_end = self.get_season_bounds(curr_end)
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
                "min_war": 800
            }
            
            return config, quads
        except Exception as e:
            st.error(f"Configuration Error: {str(e)}")
            return None, None

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
        is_major_tournament = False 

        for line in lines:
            raw_line = line
            line = line.strip()
            if not line: continue

            game_header = re.search(r'(\d+)\s+games', line.lower())
            if game_header:
                current_section_games = int(game_header.group(1))
                if current_section_games >= 18:
                    is_major_tournament = True
                continue

            if re.match(r'^\d+\s+', line):
                numeric_blocks = re.findall(r'\(?\s*[\d\-+.]+\s*\)?', line)
                if len(numeric_blocks) < 2: continue 
                
                try:
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
                            "games": current_section_games
                        })
                except: continue

        return {
            "name": t_name, 
            "date": t_date, 
            "is_major": is_major_tournament, 
            "players": players_found
        }

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
    st.session_state.processed_files = False
    st.session_state.sorted_leaderboard_names = []

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
                tournament_dates=all_tour_dates
            )
            
            if config:
                st.session_state.config = config
                st.session_state.quad_ranges = quads

                db = {}
                for data in parsed_tournament_objects:

                    q_info = next((q for q in st.session_state.quad_ranges 
                                 if q['start'] <= data['date'] <= q['end']), None)
                    
                    if q_info:

                        file_summary = {}
                        for p in data['players']:
                            name = p['name']
                            if name not in file_summary:
                                file_summary[name] = {"games": 0, "old": p['old_rating'], "new": p['new_rating']}
                            
                            file_summary[name]["games"] += p['games']
                            file_summary[name]["new"] = p['new_rating'] 

                        for name, p_file_data in file_summary.items():
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
                                
                            if p_file_data['games'] >= 18 or data['is_major']: 
                                db[name]["major_count"] += 1
                            
                            if q_info['quad'] >= 4: 
                                db[name]["recent_count"] += 1
                
                if db:
                    st.session_state.players_db = db
                    st.session_state.processed_files = True
                    st.success("Calculated WAR using Seasonal Calendar Weights")
                    st.rerun()
            
            if db:
                st.session_state.players_db = db
                st.session_state.processed_files = True
                st.rerun()

# Main
st.title("National Scrabble Selections - WAR Calculator")
st.caption("Official Administrative System for Weighted Average Rating (WAR) Calculation")

tabs = st.tabs(["Selection Overview", "National Leaderboard", "Individual Player Audit", "Policy & Criteria", "AI Assistant"])

# Overview
with tabs[0]:
    if 'config' in st.session_state:
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
                "Player Name": name, 
                "WAR": war, 
                "Current Rating": data['current_rating'],
                "WAR Precise": round(war_precise, 2), 
                "Quads": len(data['quads']), 
                "Tournaments": data['tournaments'], 
                "Total Games": data['total_games'], 
                "Majors": data['major_count'],
                "Recent": data['recent_count'],
                "Status": "QUALIFIED" if eligible else "INELIGIBLE"
            })

        if rows:
            df = pd.DataFrame(rows).sort_values(by=["WAR", "Current Rating", "WAR Precise"], ascending=False).reset_index(drop=True)
            st.session_state.sorted_leaderboard_names = df["Player Name"].tolist()
            df.index = range(1, len(df) + 1)
            
            def color_status(val):
                color = '#28a745' if val == "QUALIFIED" else '#dc3545'
                return f'color: {color}; font-weight: bold;'

            st.dataframe(df.style.map(color_status, subset=['Status']), use_container_width=True)
            
            csv = df.to_csv(index=False).encode('utf-8')
            st.download_button("Export National Rankings (CSV)", data=csv, file_name="national_rankings.csv", mime='text/csv')
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
            calc_war = round(total_wv / total_w) if total_w > 0 else 0
            
            st.markdown("### Player Record Summary")
            summary_df = pd.DataFrame({
                "Metric": ["Distinct Quadrimesters", "Aggregate Weight", "Total Weighted Value", "Cumulative Games", "Calculated WAR"],
                "Value": [len(p_data['quads']), f"{total_w:.2f}", f"{total_wv:,.2f}", p_data['total_games'], calc_war]
            })

            summary_df['Value'] = summary_df['Value'].astype(str)

            st.markdown('<div class="summary-container">', unsafe_allow_html=True)
            st.table(summary_df)
            st.markdown('</div>', unsafe_allow_html=True)
                        
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
            
            st.markdown("---")

            if st.button("Generate Master WAR Breakdown"):
                master_buffer = io.StringIO()
                for name in st.session_state.sorted_leaderboard_names:
                    data = st.session_state.players_db[name]
                    master_buffer.write(f"Player Name: {name}\n")

                    h_df = pd.DataFrame(data["history"])
                    h_df.to_csv(master_buffer, index=False)

                    m_w = sum(h['Weight'] for h in data['history'])
                    m_wv = sum(h['WeightedVal'] for h in data['history'])
                    m_g = data['total_games']
                    m_war = round(m_wv / m_w) if m_w > 0 else 0
                    
                    master_buffer.write(f"SUMMARY,,,Total Weight,Total Weighted Val,Total Games,Calculated WAR\n")
                    master_buffer.write(f",,,{m_w:.2f},{m_wv:.2f},{m_g},{m_war}\n")
                    
                    master_buffer.write("\n\n\n\n") 
                
                st.download_button(
                    label="Download Master WAR Breakdown (CSV)",
                    data=master_buffer.getvalue().encode('utf-8'),
                    file_name="WAR_breakdown_master.csv",
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