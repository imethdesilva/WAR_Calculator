# National Scrabble Selections - WAR Calculator

An automated administrative dashboard for the **Scrabble Federation of Sri Lanka**, used to run national team selections for the World Scrabble Championship (WSC) and World Youth Scrabble Championship (WYSC) through precise **Weighted Average Rating (WAR)** calculations, straight from a GitHub-backed tournament archive.

---

## Overview
The app maintains a persistent archive of tournament result files (organized by year, synced to this GitHub repo), and computes WAR for both WSC and WYSC in one run. Every result - the leaderboard, individual player audits, and the underlying calculation itself - is fully explainable: each report shows the international event date, the cut-off date, the exact quadrimester schedule used, and a line-by-line arithmetic breakdown of how each player's WAR was derived, so a result can always be traced back to the official selection criteria it was computed against.

### What is WAR?
The Weighted Average Rating (WAR) system rewards consistency and recent form. It evaluates performance over a 20-month window, divided into five 4-month "Quadrimesters," where more recent games carry higher mathematical weight (1.00 up to 2.00).

🔗 **[Read the Full Technical Rationale on Medium](https://medium.com/@imethdesilva/weighted-ratings-and-the-national-scrabble-selections-process-567231d9c486)**

---

## Key Features

*   **GitHub-Backed Tournament Archive:** Tournament files live under `tournament files/<year>/*.txt` and are pushed to/deleted from this repo directly from the app via the GitHub Contents API - browse, edit, rename, delete, or add a new file (with a review step before anything is pushed) without touching git yourself.
*   **Password-Protected Admin Access + Audit Trail:** The archive is gated behind a shared password. Unlocking it also requires a name/initials field, which is stamped onto every push/delete commit message from that point on - so even with one shared password, every change is traceable to whoever made it.
*   **Dual WAR Run:** One button calculates WAR for both WSC and WYSC together across the entire archive, correctly applying each classification's own cut-off offset (6 months for WSC, 3 for WYSC), then caches both so you can flip between them instantly.
*   **Temporal Logic Engine:** Automatically derives the five fixed 4-month quadrimesters for a given cut-off date, including the official push-back rule (an empty or single-tournament "stray" quadrimester merges into the previous one) - plus a "Live View" toggle to preview current-form WAR before a quadrimester is officially locked in.
*   **Full Eligibility Engine:** Automatically checks every player against the official criteria - minimum games, minimum tournaments, minimum quadrimesters, recent-activity (Q4/Q5) requirement, the 18-round "major" tournament requirement, the >1-year inactivity rule (with 50-games-after-resumption reinstatement), and the official WAR → current rating → WAR-to-2dp tie-break chain.
*   **Data Quality & Provisional Flags:** A Data Quality summary on the archive tab surfaces any unparseable lines across the whole archive up front, and players whose most recent result is still provisional are flagged with a `[PROVISIONAL]` badge everywhere they appear.
*   **Explainable PDF Reports:** Black-and-white, audit-ready PDFs for the overall calculation, all players, qualified players only, or a single player - each opens with the calculation details and quadrimester schedule used, and every player's history table includes the Weight × Rating = Weighted Value arithmetic plus the final WAR division spelled out.
*   **CSV & Web Export:** A full selection report (leaderboard + per-player breakdown) as CSV, sanitized against spreadsheet formula injection, with an optional one-click push to a live PythonAnywhere-hosted page.
*   **Archive ZIP Download:** Download every year folder and tournament file in the archive as a single zip.
*   **Full Dark Mode Support:** Professional Streamlit UI with compact, consistently-aligned button rows throughout.

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
   ```
2. **Create a virtual environment:**
   ```bash
   python -m venv .venv
   ```
3. **Activate the environment:**
   * Windows PowerShell: `.venv\Scripts\activate`
   * Git Bash/Linux/Mac: `source .venv/bin/activate`
4. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```
5. **Configure secrets** - create `.streamlit/secrets.toml` (already git-ignored, never commit this file):
   ```toml
   ARCHIVE_PASSWORD = "choose-a-shared-admin-password"
   GITHUB_TOKEN = "a-token-with-write-access-to-this-repo"

   # Optional - only needed for "Push WAR Updates to Web"
   PYTHONANYWHERE_USERNAME = "your-pythonanywhere-username"
   PYTHONANYWHERE_API_TOKEN = "your-pythonanywhere-api-token"
   ```
   `GITHUB_TOKEN` needs write access to this repo's contents (a fine-grained PAT scoped to just this repo is enough) - it's how the app pushes archive edits back to GitHub, and works the same whether the repo is public or private.

---

## Usage Instructions

1. **Launch the app:** `streamlit run main.py`
2. **Unlock the Tournament Archive:** enter the shared `ARCHIVE_PASSWORD` plus your name/initials (recorded on every change you make from then on).
3. **Build the archive:** files already in `tournament files/<year>/` show up automatically. Add more via the **+ Add Tournament File** button on the "All Tournaments" sub-tab - the year folder is detected from the date in the file, and nothing is pushed to GitHub until you review the parsed summary and confirm.
4. **Run WAR for WYSC and WSC:** enter each classification's international event date and confirm - both get calculated and cached together.
5. **Review results:**
   - **Selection Overview** - the cut-off date, quadrimester schedule, and every tournament that contributed to WAR for the currently-viewed classification.
   - **National Leaderboard** - the full ranked list with qualification status, filters, and CSV/PDF export.
   - **Individual Player Audit** - one player's full history, record summary, and a PDF export with the complete WAR arithmetic breakdown.
   - **Policy & Criteria** - the official selection criteria summary and the source criteria PDF.
6. **Export:** download the CSV selection report, the calculation/all-players/qualified-players/individual audit PDFs, or a zip of the whole archive, as needed.

---

## Documentation & Contact
* Author: Imeth de Silva
* imethdesilva@gmail.com
