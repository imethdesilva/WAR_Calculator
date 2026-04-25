# National Scrabble Selections - WAR Calculator

An automated administrative dashboard for the **Scrabble Federation of Sri Lanka**, designed to streamline national team selections through precise **Weighted Average Rating (WAR)** calculations.

---

## Overview
This application transforms a manual process into a instant and streamlined operation. By digitizing the WAR calculations, the portal allows the administrator to upload tournament data and instantly generate rankings for the World Scrabble Championship (WSC) and World Youth Scrabble Championship (WYSC)

### What is WAR?
The Weighted Average Rating (WAR) system rewards consistency and recent form. It evaluates performance over a 20-month window, divided into five 4-month "Quadrimesters," where more recent games carry higher mathematical weight.

🔗 **[Read the Full Technical Rationale on Medium](https://medium.com/@imethdesilva/weighted-ratings-and-the-national-scrabble-selections-process-567231d9c486)**

---

## Key Features

*   **Temporal Logic Engine:** Automatically calculates selection windows and five 4-month "Quadrimesters" with weights ranging from 1.0 to 2.0.
*   **Deterministic Rating Logic:** Identifies a player's official "Current Rating" by chronologically processing tournament dates, ensuring accuracy regardless of file upload order.
*   **Intelligent Parser:** Seamlessly extracts data (Names, Old/New Ratings, Game Counts) from standard tournament `.txt` result files.
*   **Eligibility:** Automatically verifies players against official criteria (Min games, Min tournaments, Recency, and Major requirements).
*   **Administrative Dashboard:** A professional UI built with Streamlit, featuring centering, tabulated summaries, and full **Dark Mode** support.
*   **Master Data:** Generates a **Master WAR Breakdown CSV** containing stacked histories of all ranked players with exactly 4-line spacing for easy archival.

---

## 🛠️ Installation & Setup

### Prerequisites
*   **Python 3.12** is recommended.
*   Avoid Python 3.14 (experimental) as key dependencies may not be stable.

### Step-by-Step Setup
1. **Clone the repository:**
   ```bash
   git clone https://github.com/imethdesilva/WAR_Calculator.git
   cd WAR_Calculator

2. **Create a virtual environment:**
   ```bash
   python -m venv .venv
   
3. **Activate the environment:**
   
* Windows PowerShell: .venv\Scripts\activate
* Git Bash/Linux/Mac: source .venv/bin/activate

4. **Install dependencies:**
  ```bash
pip install streamlit pandas openpyxl python-dateutil
```
---
## Usage Instructions

* Launch the App: Run streamlit run main.py in your terminal.
* Step 1 (Configure): In the sidebar, select the tournament classification (WSC or WYSC) and enter the international event date.
* Step 2 (Ingest): Upload all relevant tournament .txt files. The engine will sort them chronologically and calculate WAR values automatically.
* Navigate to the National Leaderboard to view official rankings and qualification status.
* Use the Individual Player Audit to view a specific player's full history (sorted latest first) and tabulated record summaries.
* Step 4 (Export): Download the Master WAR Breakdown CSV for official records.

---

## Documentation & Contact
* Author: Imeth de Silva
* imethdesilva@gmail.com

