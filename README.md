ArXiv hep-th Telegram bot

Overview
- Posts the daily “New submissions” from arXiv hep-th to a Telegram chat/channel.
- Runs via GitHub Actions at 08:00 Europe/Berlin and politely rates limits messages.
- Skips weekends and avoids reposting after holidays by checking if arXiv has actually updated since the last successful run.

What’s New (AI-authored changes)
- Weekend skip: The workflow and the bot both exit on Saturday/Sunday (Europe/Berlin) so no weekend posts.
- No reposts after holidays: Before posting, the bot queries arXiv’s API for the most recent update in hep-th and compares it to the timestamp of the last successful GitHub Actions run. If arXiv hasn’t updated since then, the bot exits without posting.
- These changes were written with the help of an AI coding assistant.

How It Works
- Workflow schedule: `.github/workflows/Scheduler.yml` triggers daily at 06:00 UTC
- Weekend guard (workflow): The workflow detects Europe/Berlin day-of-week and exits on Saturday/Sunday.
- Weekend guard (bot): The Python script also checks Europe/Berlin day-of-week and returns early if run on weekends (belt-and-suspenders for manual runs).
- Update check: The workflow fetches the last successful run time and sets `LAST_SUCCESS_AT`. The bot then calls arXiv’s API for `hep-th`, finds the latest updated timestamp, and skips posting if it isn’t newer than `LAST_SUCCESS_AT`.

Configuration
- Environment variables (required):
  - `TELEGRAM_BOT_TOKEN`: Telegram bot token.
  - `TELEGRAM_CHAT_ID`: Target chat/channel (e.g., `@your_channel` or a numeric ID).
- GitHub Actions secrets: Set both `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in the repository secrets.

Local Usage
- One-off run:
  - `Arxiv-Bot-for-hepth/run_once.sh` requires both env vars set, creates a venv, installs deps, and posts the current “New submissions”.
- Daemon (self-hosted):
  - `Arxiv-Bot-for-hepth/run_daily.sh` runs a simple daily scheduler at 08:00 CET/CEST. For GitHub Actions, the workflow already handles scheduling.

Adapting To Other arXiv Categories
If you want to use this bot for other arXiv categories or pages, change two places:
- Scrape source (HTML list page):
  - File: `Arxiv-Bot-for-hepth/Arxiv_bot.py`
  - Function: `scrape_hep_th_new()`
  - Change `url = "https://arxiv.org/list/hep-th/new"` to a different list path, e.g. `https://arxiv.org/list/cs.CL/new` or any other category `.../list/<category>/new`.
- Update detection (API query):
  - File: `Arxiv-Bot-for-hepth/Arxiv_bot.py`
  - Function: `_arxiv_latest_updated_iso()`
  - Change the query string `search_query=cat:hep-th` to your category, e.g. `search_query=cat:cs.CL`.

Notes
- The Inspire author link building is generic and does not depend on the arXiv category.
- No extra Python dependencies were added; the arXiv “updated” check uses a minimal XML substring parse to avoid adding a feed parser.
- Timezone logic uses Europe/Berlin to align the run with the original target audience/time.

Repository Structure (key files)
- Bot code: `Arxiv-Bot-for-hepth/Arxiv_bot.py`
- Requirements: `Arxiv-Bot-for-hepth/requirements.txt`
- GitHub Actions workflow: `.github/workflows/Scheduler.yml`
- Helper scripts: `Arxiv-Bot-for-hepth/run_once.sh`, `Arxiv-Bot-for-hepth/run_daily.sh`
