ArXiv hep-th Telegram bot

Overview
- Posts the daily “New submissions” from arXiv hep-th to a Telegram chat/channel.
- Runs via GitHub Actions at 08:00 Europe/Berlin and politely rates limits messages.
- Skips weekends and avoids reposting by remembering which arXiv IDs were already published.

What’s New (AI-authored changes)
- Weekend skip: The workflow and the bot both exit on Saturday/Sunday (Europe/Berlin) so no weekend posts.
- No reposts after holidays: The bot caches previously posted arXiv IDs in `.state/posted.json` (restored via GitHub Actions cache). Any submission already seen is skipped, even across weekends/holidays.
- Manual override for tests: `workflow_dispatch` runs set `FORCE_POST=1`, bypassing only the weekend guard so you can rerun the workflow without waiting for the next morning.
- These changes were written with the help of an AI coding assistant.

How It Works
- Workflow schedule: `.github/workflows/Scheduler.yml` triggers daily at 06:00 UTC.
- Weekend guard (workflow): The workflow detects Europe/Berlin day-of-week and exits on Saturday/Sunday.
- Weekend guard (bot): The Python script also checks Europe/Berlin day-of-week and returns early if run on weekends (belt-and-suspenders for manual runs).
- Stateful dedupe: `actions/cache` restores `.state/posted.json`; the bot adds newly posted IDs and saves it back. If nothing new is found, it logs “No new submissions to post.”

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
- Dedupe cache:
  - File: `Arxiv-Bot-for-hepth/Arxiv_bot.py`
  - Functions: `_load_state()`, `_save_state()`, and `_extract_entry_id()`
  - The cache records posted arXiv IDs. It works for any category without modification once the scrape URL is updated.

Notes
- The Inspire author link building is generic and does not depend on the arXiv category.
- No extra Python dependencies beyond `requests` and `beautifulsoup4`.
- Timezone logic uses Europe/Berlin to align the run with the original target audience/time.

Repository Structure (key files)
- Bot code: `Arxiv-Bot-for-hepth/Arxiv_bot.py`
- Requirements: `Arxiv-Bot-for-hepth/requirements.txt`
- GitHub Actions workflow: `.github/workflows/Scheduler.yml`
- Helper scripts: `Arxiv-Bot-for-hepth/run_once.sh`, `Arxiv-Bot-for-hepth/run_daily.sh`
