import gradio as gr
import pandas as pd
import re
from datetime import datetime
from dateutil.relativedelta import relativedelta
import os

class ScrabbleSelectionEngine:
    def __init__(self):
        self.tournament_data = []
        self.players_db = {}
        self.config = {}
        self.quad_ranges = []

    def configure_selection(self, mode, intl_date_str):
        try:
            # Parse date and set selection parameters
            intl_date = datetime.strptime(intl_date_str, "%d.%m.%Y")
            offset = 6 if mode == "WSC" else 3
            cutoff_date = intl_date - relativedelta(months=offset)
            
            self.config = {
                "mode": mode, "intl_date": intl_date, "cutoff_date": cutoff_date,
                "req_games": 80 if mode == "WSC" else 50,
                "req_tours": 5 if mode == "WSC" else 3,
                "req_recent": 2 if mode == "WSC" else 1
            }

            # Generate 5 Quadrimesters (4 months each)
            self.quad_ranges = []
            curr_end = cutoff_date
            for i in range(5, 0, -1):
                start = curr_end - relativedelta(months=4)
                weight = 1.0 + (i-1)*0.25
                self.quad_ranges.append({"quad": i, "start": start, "end": curr_end, "weight": weight})
                curr_end = start
            
            quad_df = pd.DataFrame([
                {"Quad": q['quad'], "Weight": q['weight'], "From": q['start'].strftime('%Y-%m-%d'), "To": q['end'].strftime('%Y-%m-%d')}
                for q in self.quad_ranges
            ])
            return f"System configured for {mode}. Selection window: {self.quad_ranges[-1]['start'].strftime('%b %Y')} to {cutoff_date.strftime('%b %Y')}", quad_df
        except Exception as e:
            return f"Error in date format: {str(e)}", None

    def parse_txt_file(self, content):
        lines = content.splitlines()
        
        # 1. Improved Date Extraction: Search first 5 lines for a date
        t_date = None
        t_name = "Unknown Tournament"
        for line in lines[:5]:
            date_match = re.search(r'(\d{2}\.\d{2}\.\d{4})', line)
            if date_match:
                t_date = datetime.strptime(date_match.group(1), "%d.%m.%Y")
                t_name = line.split(date_match.group(1))[-1].strip()
                break
        
        if not t_date: return None
        
        is_18_rnd = "18 games" in content.lower()
        players_found = []
        current_section_games = 0

        for line in lines:
            line = line.strip()
            if not line: continue

            # Track game count sections (e.g., "18 games")
            game_header = re.search(r'^(\d+)\s+games', line.lower())
            if game_header:
                current_section_games = int(game_header.group(1))
                continue

            # Identify Player Rows (Start with rank, e.g., "1  ", "12 ")
            if re.match(r'^\d+\s+', line):
                # Use regex to find all numbers/ratings, ignoring special chars like * or &
                # This handles "( 892)", "+109", and standard "1158"
                numeric_blocks = re.findall(r'\(?\s*[\d\-+]+\s*\)?', line)
                if len(numeric_blocks) < 3: continue 
                
                # The New Rating is ALWAYS the very last numeric block on the line
                raw_rating = numeric_blocks[-1].replace('(', '').replace(')', '').strip()
                try:
                    new_rating = int(raw_rating)
                except: continue

                # Extract Name: It's between the 3rd numeric block (Margin) and the ratings at the end
                # We identify it by stripping away the known prefix and suffix patterns
                name_clean = line
                name_clean = re.sub(r'^\d+\s+[\d\-+]+\s+[\d\-+*&]+\s+', '', name_clean) # Remove rank/wins/margin
                name_clean = re.sub(r'[\d\-+*&\(\)\s]+$', '', name_clean).strip() # Remove ratings/change
                
                if name_clean:
                    players_found.append({"name": name_clean, "rating": new_rating, "games": current_section_games})

        return {"name": t_name, "date": t_date, "is_18_rnd": is_18_rnd, "players": players_found}

    def process_files(self, files):
        if not self.quad_ranges: return "❌ Error: Please complete Step 1 (Configure Selection) first!"
        
        valid_tours = []
        for f in files:
            with open(f.name, 'r', encoding='utf-8', errors='ignore') as file_in:
                data = self.parse_txt_file(file_in.read())
                if data: valid_tours.append(data)
        
        self.tournament_data = sorted(valid_tours, key=lambda x: x['date'])
        self.players_db = {}
        processed_count = 0

        for tour in self.tournament_data:
            q_num, q_weight = None, 0
            for q in self.quad_ranges:
                if q['start'] <= tour['date'] < q['end']:
                    q_num, q_weight = q['quad'], q['weight']
                    break
            
            # Skip tournaments outside the 20-month window
            if q_num is None: continue 
            processed_count += 1

            for p in tour['players']:
                name = p['name']
                if name not in self.players_db:
                    self.players_db[name] = {"history": [], "total_games": 0, "tournaments": 0, "quads": set(), "major_count": 0, "recent_count": 0}
                
                db = self.players_db[name]
                db["history"].append({"Date": tour['date'].strftime('%Y-%m-%d'), "Tournament": tour['name'], "Quad": q_num, "Weight": q_weight, "Rating": p['rating'], "W*R": p['rating'] * q_weight, "Games": p['games']})
                db["total_games"] += p['games']
                db["tournaments"] += 1
                db["quads"].add(q_num)
                if tour['is_18_rnd'] and p['games'] >= 18: db["major_count"] += 1
                if q_num >= 4: db["recent_count"] += 1

        if processed_count == 0:
            return "⚠️ Warning: Processed 0 tournaments. Your uploaded files are likely OLDER than the 20-month selection window. Check your dates in Step 1."
        return f"✅ Successfully processed {processed_count} tournaments within the selection window."

    def get_leaderboard(self):
        rows = []
        if not self.players_db:
            return pd.DataFrame(columns=["Rank", "Player Name", "WAR", "Status"])
            
        for name, data in self.players_db.items():
            total_wr = sum(h['W*R'] for h in data['history'])
            total_w = sum(h['Weight'] for h in data['history'])
            war = round(total_wr / total_w) if total_w > 0 else 0
            
            eligible = (war >= 800 and data['total_games'] >= self.config['req_games'] and data['tournaments'] >= self.config['req_tours'] and 
                        len(data['quads']) >= 3 and data['major_count'] >= 1 and data['recent_count'] >= self.config['req_recent'])
            
            rows.append({"Player Name": name, "WAR": war, "Quads": len(data['quads']), "Tours": data['tournaments'], "Games": data['total_games'], "18-Rnd": data['major_count'], "Recent (Q4/5)": data['recent_count'], "Qualification": "✅ SUCCESS" if eligible else "❌ FAIL"})
        
        df = pd.DataFrame(rows)
        if not df.empty:
            df = df.sort_values(by="WAR", ascending=False)
            df.insert(0, "Rank", range(1, len(df) + 1))
        return df

engine = ScrabbleSelectionEngine()

# --- UI CODE ---
with gr.Blocks(theme=gr.themes.Soft(), title="SL Scrabble Portal") as demo:
    gr.Markdown("# 🏆 Sri Lankan Scrabble Selection Portal")
    
    with gr.Tabs():
        with gr.TabItem("1. Configure Selection"):
            with gr.Row():
                with gr.Column():
                    mode = gr.Dropdown(["WSC", "WYSC"], label="Tournament Type", value="WSC")
                    intl_date = gr.Textbox(label="Date of International Tournament (DD.MM.YYYY)", value="15.10.2025")
                    btn_c = gr.Button("Calculate Selection Dates", variant="primary")
                with gr.Column():
                    cutoff_lbl = gr.Label(label="Window Status")
                    quad_tbl = gr.Dataframe(label="Quadrimester Periods")

        with gr.TabItem("2. Upload & Process"):
            files = gr.File(label="Select Result Files (.txt)", file_count="multiple")
            btn_p = gr.Button("Step 2: Process Files", variant="primary")
            status = gr.Textbox(label="Processing Result")
            btn_l = gr.Button("Step 3: Generate Leaderboard", variant="stop")

        with gr.TabItem("3. Leaderboard"):
            leaderboard = gr.Dataframe()
            export_btn = gr.Button("Export to Excel")
            export_file = gr.File(label="Download Excel")

        with gr.TabItem("4. Player Details"):
            player_opt = gr.Dropdown(label="Select Player", choices=[])
            re_btn = gr.Button("Update List")
            history = gr.Dataframe(label="History Breakdown")

    # Bindings
    btn_c.click(engine.configure_selection, inputs=[mode, intl_date], outputs=[cutoff_lbl, quad_tbl])
    btn_p.click(engine.process_files, inputs=[files], outputs=[status])
    btn_l.click(engine.get_leaderboard, outputs=[leaderboard])
    re_btn.click(lambda: gr.Dropdown(choices=sorted(list(engine.players_db.keys()))), outputs=[player_opt])
    player_opt.change(lambda n: pd.DataFrame(engine.players_db[n]["history"]), inputs=[player_opt], outputs=[history])
    export_btn.click(lambda: engine.get_leaderboard().to_excel("Leaderboard.xlsx", index=False) or "Leaderboard.xlsx", outputs=[export_file])

if __name__ == "__main__":
    demo.launch()