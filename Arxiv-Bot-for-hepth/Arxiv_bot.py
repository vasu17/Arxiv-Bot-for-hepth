import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from urllib.parse import quote

import requests
from typing import Optional, Tuple


# ─── Configuration ───────────────────────────────────────────────────────────
# Telegram bot token, injected via environment variable by the GitHub Actions workflow.
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

# Persistent state: tracks which arXiv IDs have already been posted to avoid duplicates.
# Stored as a JSON file alongside this script in a .state/ directory.
STATE_DIR = Path(__file__).with_name(".state")
STATE_PATH = STATE_DIR / "posted.json"
MAX_TRACKED_IDS = 2000  # Cap to prevent the state file from growing indefinitely


def _load_state() -> Tuple[set[str], Optional[str]]:
    """Load the persisted deduplication state from disk.

    Returns:
        A tuple of (posted_ids, last_run_iso) where:
        - posted_ids: set of arXiv IDs that have already been sent to Telegram.
        - last_run_iso: ISO-8601 timestamp of the last successful run, or None.

    If the state file is missing or corrupt, returns empty defaults so the bot
    can still run (it will just re-post everything visible on the "new" page).
    """
    if not STATE_PATH.exists():
        return set(), None
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        posted = set(data.get("posted_ids", []))
        last_run = data.get("last_run_iso")
        return posted, last_run
    except Exception:
        return set(), None


def _save_state(posted_ids: set[str], last_run_iso: str) -> None:
    """Persist the deduplication state to disk.

    Args:
        posted_ids: The full set of arXiv IDs that have been posted so far.
        last_run_iso: ISO-8601 timestamp to record as the last successful run.

    The set is sorted and trimmed to MAX_TRACKED_IDS (oldest removed first)
    to prevent unbounded growth of the state file over months of operation.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    ids = sorted(posted_ids)
    if len(ids) > MAX_TRACKED_IDS:
        ids = ids[-MAX_TRACKED_IDS:]
    STATE_PATH.write_text(
        json.dumps({"posted_ids": ids, "last_run_iso": last_run_iso}, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def send_message(chat_id: str, text: str, parse_mode: str = "HTML") -> dict:
    """Send a message to a Telegram chat via the Bot API.

    Args:
        chat_id: Telegram chat/channel identifier (numeric ID or @username).
        text: Message body (may contain HTML tags when parse_mode is HTML).
        parse_mode: Telegram parse mode — 'HTML' or 'Markdown'.

    Returns:
        The Telegram API JSON response dict.

    Implements exponential back-off retry (up to 6 attempts) when the API
    returns HTTP 429 (rate-limited). Web page previews are disabled to keep
    messages compact.
    """
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    backoff = 1.0
    for _ in range(6):
        resp = requests.post(url, json=payload, timeout=30)
        if resp.status_code == 429:
            try:
                retry_after = resp.json().get("parameters", {}).get("retry_after", None)
            except Exception:
                retry_after = None
            time.sleep((retry_after or backoff))
            backoff = min(backoff * 2, 8)
            continue
        resp.raise_for_status()
        return resp.json()
    resp.raise_for_status()
    return resp.json()


def get_chat(chat_id: str) -> dict:
    """Retrieve chat metadata from Telegram (used by --check-chat).

    Args:
        chat_id: Telegram chat/channel identifier.

    Returns:
        The Telegram API JSON response containing chat title, type, etc.
    """
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getChat"
    resp = requests.get(url, params={"chat_id": chat_id}, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _iter_new_submission_nodes(soup):
    """Yield <dt> and <dd> DOM nodes from the 'New submissions' section.

    Walks the arXiv listing page HTML: finds the <h3> containing
    'New submissions', then yields every sibling <dt>/<dd> node until
    the next <h3> (which marks the start of Cross-listings or Replacements).

    Args:
        soup: A BeautifulSoup object of the arXiv /list/hep-th/new page.

    Yields:
        BeautifulSoup Tag objects (only <dt> and <dd> elements).
    """
    for h3 in soup.find_all("h3"):
        if "New submissions" in h3.get_text(strip=True):
            node = h3.next_sibling
            while node is not None:
                name = getattr(node, "name", None)
                if name == "h3":
                    break
                if name in ("dt", "dd"):
                    yield node
                node = node.next_sibling
            break


def _extract_entries_after_header(soup):
    """Parse all new arXiv submissions into structured dicts.

    Pairs up <dt> (metadata links) and <dd> (title/authors/abstract) nodes
    from _iter_new_submission_nodes, then extracts:
      - arXiv ID, abs URL, PDF URL
      - Title, author names, comments, abstract text

    Args:
        soup: A BeautifulSoup object of the arXiv /list/hep-th/new page.

    Returns:
        A list of dicts, each with keys: id, title, authors, comments,
        abstract, abs_url, pdf_url.
    """
    entries = []
    pending_dt = None
    for node in _iter_new_submission_nodes(soup):
        if node.name == "dt":
            pending_dt = node
        elif node.name == "dd" and pending_dt is not None:
            dt, dd = pending_dt, node
            pending_dt = None
            # Extract arXiv abs/pdf from the dt block
            abs_a = dt.find("a", href=lambda h: h and h.startswith("/abs/"))
            pdf_a = dt.find("a", href=lambda h: h and h.startswith("/pdf/"))
            abs_url = f"https://arxiv.org{abs_a['href']}" if abs_a and abs_a.has_attr('href') else ""
            pdf_url = (
                f"https://arxiv.org{pdf_a['href']}" if pdf_a and pdf_a.has_attr('href') else ""
            )
            # If pdf link missing but we have abs, derive pdf URL
            if not pdf_url and abs_url:
                try:
                    aid = abs_url.rsplit("/abs/", 1)[1]
                    pdf_url = f"https://arxiv.org/pdf/{aid}.pdf"
                except Exception:
                    pass
            title_div = dd.find("div", class_=lambda c: c and "list-title" in c)
            title = (
                title_div.get_text(" ", strip=True).replace("Title:", "").strip()
                if title_div
                else ""
            )

            authors_div = dd.find("div", class_=lambda c: c and "list-authors" in c)
            author_links = authors_div.find_all("a") if authors_div else []
            authors = [a.get_text(strip=True) for a in author_links]

            comments_div = dd.find("div", class_=lambda c: c and "list-comments" in c)
            comments = (
                comments_div.get_text(" ", strip=True).replace("Comments:", "").strip()
                if comments_div
                else ""
            )

            # Abstract may be present as <p class="mathjax"> or hidden span with 'abstract-full'
            abstract_span = dd.find(["span", "p"], class_=lambda c: c and "abstract" in c)
            if not abstract_span:
                abstract_span = dd.find("p", class_=lambda c: c and "mathjax" in c)
            abstract = abstract_span.get_text(" ", strip=True) if abstract_span else ""

            entries.append(
                {
                    "id": abs_url.rsplit("/abs/", 1)[1] if abs_url and "/abs/" in abs_url else "",
                    "title": title,
                    "authors": authors,
                    "comments": comments,
                    "abstract": abstract,
                    "abs_url": abs_url,
                    "pdf_url": pdf_url,
                }
            )
    return entries


def _inspire_author_link(name: str) -> str:
    """Build an INSPIRE HEP search URL for a given author name.

    Args:
        name: Author's full name (e.g. "Edward Witten").

    Returns:
        A URL string linking to an INSPIRE exactauthor search for that name.
    """
    q = quote(f'"{name}"')
    return f"https://inspirehep.net/authors?sort=bestmatch&size=25&page=1&q={q}"


def format_entry_html(entry: dict) -> str:
    """Format a single arXiv entry dict into an HTML Telegram message.

    Constructs a message with Title, Author (linked to INSPIRE), Comment,
    Abstract, and arXiv/PDF links.  Telegram messages are capped at ~4096
    characters, so this function uses a budget system:
      1. Compute the length of fixed fields (title, comments, links).
      2. Allocate remaining space to the abstract (truncated with '...' if needed).
      3. Fill remaining budget with author links, falling back to 'et al.'
         when the list would exceed the limit.

    Args:
        entry: A dict with keys from _extract_entries_after_header.

    Returns:
        An HTML-formatted string ready for Telegram's sendMessage API.
    """
    title = escape(entry.get("title", "")).strip()
    comments = escape(entry.get("comments", "")).strip()
    raw_abstract = entry.get("abstract", "").strip()
    
    abs_url = entry.get("abs_url") or ""
    pdf_url = entry.get("pdf_url") or ""
    link_lines = []
    if abs_url:
        link_lines.append(f'HTML:- <a href="{abs_url}">arXiv</a>')
    if pdf_url:
        link_lines.append(f'PDF:- <a href="{pdf_url}">PDF</a>')

    base_parts = []
    if title:
        base_parts.append(f"Title:- {title}")
    if comments:
        base_parts.append(f"Comment:- {comments}")
    if link_lines:
        base_parts.extend(link_lines)
    
    base_text_len = sum(len(p) for p in base_parts) + len(base_parts)
    budget = 3950 - base_text_len
    
    abstract_html = escape(raw_abstract)
    if len(abstract_html) > budget - 30:
        safe_cut = max(0, budget - 30)
        if safe_cut > 3:
            raw_abstract = raw_abstract[:safe_cut - 3] + "..."
            abstract_html = escape(raw_abstract)
            if len(abstract_html) > budget - 30:
                abstract_html = abstract_html[:budget - 30]
    
    remaining_budget = budget - len(f"Abstract:- {abstract_html}") - 1
    
    authors_list = entry.get("authors", [])
    linked_authors = []
    
    current_authors_len = 9 # Length roughly to account for "Author:- " prefix
    for i, name in enumerate(authors_list):
        url = _inspire_author_link(name)
        author_html = f'<a href="{url}">{escape(name)}</a>'
        
        future_len = current_authors_len + len(author_html)
        if i < len(authors_list) - 1:
            if future_len + 10 > remaining_budget:
                linked_authors.append("et al.")
                break
            else:
                linked_authors.append(author_html)
                current_authors_len += len(author_html) + 2 # For ", "
        else:
            if future_len > remaining_budget:
                linked_authors.append("et al.")
            else:
                linked_authors.append(author_html)

    authors_html = ", ".join(linked_authors) if linked_authors else ""

    parts = []
    if title:
        parts.append(f"Title:- {title}")
    if authors_html:
        parts.append(f"Author:- {authors_html}")
    if comments:
        parts.append(f"Comment:- {comments}")
    if abstract_html:
        parts.append(f"Abstract:- {abstract_html}")
    if link_lines:
        parts.extend(link_lines)

    text = "\n".join(parts)
    return text


def scrape_hep_th_new() -> list:
    """Scrape today's new hep-th submissions from the arXiv website.

    Fetches https://arxiv.org/list/hep-th/new, parses the HTML with
    BeautifulSoup, and returns structured entry dicts.

    Returns:
        A list of entry dicts (see _extract_entries_after_header).

    Raises:
        RuntimeError: If beautifulsoup4 is not installed.
        requests.HTTPError: If the arXiv page returns a non-200 status.
    """
    url = "https://arxiv.org/list/hep-th/new"
    # Lazy import to avoid requiring bs4 for --test path
    try:
        from bs4 import BeautifulSoup  # type: ignore
    except Exception as e:
        raise RuntimeError("beautifulsoup4 is required for scraping. Install bs4.") from e

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
    }
    r = requests.get(url, timeout=30, headers=headers)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    entries = _extract_entries_after_header(soup)
    return entries


def _extract_entry_id(entry: dict) -> Optional[str]:
    """Extract the arXiv ID from an entry dict.

    Tries the 'id' field first; if empty, falls back to parsing the abs_url.

    Args:
        entry: A parsed arXiv entry dict.

    Returns:
        The arXiv ID string (e.g. '2405.12345'), or None if not found.
    """
    candidate = entry.get("id") or ""
    candidate = candidate.strip()
    if candidate:
        return candidate
    abs_url = entry.get("abs_url") or ""
    if "/abs/" in abs_url:
        return abs_url.rsplit("/abs/", 1)[-1]
    return None


def run_once_and_post(chat_id: str) -> None:
    """Scrape arXiv hep-th/new and post all *unseen* entries to Telegram.

    This is the core bot loop for a single run:
      1. Scrape today's new submissions.
      2. Load the set of previously-posted IDs from the state file.
      3. For each entry not in the posted set, format and send it.
      4. Save the updated state (with the new IDs) to disk.

    The posted_ids dedup mechanism is the primary protection against sending
    the same paper twice — even if the GitHub Actions scheduler fires
    multiple times in one day.

    Args:
        chat_id: Telegram chat/channel to post messages to.
    """
    entries = scrape_hep_th_new()
    posted_ids, _ = _load_state()
    newly_posted: list[str] = []

    for entry in entries:
        entry_id = _extract_entry_id(entry)
        if entry_id and entry_id in posted_ids:
            continue

        msg = format_entry_html(entry)
        if not msg:
            continue

        send_message(chat_id, msg, parse_mode="HTML")
        newly_posted.append(entry_id or "")
        if entry_id:
            posted_ids.add(entry_id)

        # be nice to Telegram API: ~1 msg/sec to a single chat
        time.sleep(1.2)

    if newly_posted:
        _save_state(posted_ids, datetime.now(timezone.utc).isoformat())
        print(f"Posted {len([i for i in newly_posted if i])} new submissions.")
    else:
        print("No new submissions to post.")


def _is_weekend_berlin(now: Optional[datetime] = None) -> bool:
    """Check whether the current day is Saturday or Sunday in Europe/Berlin.

    Used as a belt-and-suspenders guard alongside the cron schedule
    (which already targets Mon-Fri) to ensure arXiv is not scraped on
    weekends when no new papers are posted.

    Args:
        now: Optional datetime for testing; defaults to current UTC time.

    Returns:
        True if it is Saturday or Sunday in Europe/Berlin.
    """
    try:
        from zoneinfo import ZoneInfo
    except Exception:  # pragma: no cover
        ZoneInfo = None  # type: ignore

    if now is None:
        now = datetime.now(timezone.utc)

    if ZoneInfo is not None:
        berlin = ZoneInfo("Europe/Berlin")
        dow = now.astimezone(berlin).weekday()
    else:
        dow = now.weekday()  # fallback UTC
    return dow >= 5  # 5=Sat, 6=Sun


def seconds_until_next_8am_cet(now_utc: datetime | None = None) -> int:
    """Calculate seconds until the next 08:00 in Europe/Berlin.

    Used by the daemon loop to sleep until the next posting window.
    Handles CET/CEST transitions via zoneinfo; falls back to a fixed
    UTC+1 offset if zoneinfo is unavailable.

    Args:
        now_utc: Optional current UTC datetime for testing.

    Returns:
        Number of seconds (int) to wait.
    """
    try:
        from zoneinfo import ZoneInfo  # Python 3.9+
    except Exception:  # pragma: no cover
        ZoneInfo = None

    if now_utc is None:
        now_utc = datetime.utcnow()

    if ZoneInfo is None:
        # Fallback: assume CET fixed offset (+1) without DST (approximation)
        local_now = now_utc + timedelta(hours=1)
        target_local = local_now.replace(hour=8, minute=0, second=0, microsecond=0)
        if local_now >= target_local:
            target_local += timedelta(days=1)
        delta = target_local - local_now
        return int(delta.total_seconds())

    berlin = ZoneInfo("Europe/Berlin")
    # compute now in Berlin time
    now_local = datetime.now(tz=berlin)
    target = now_local.replace(hour=8, minute=0, second=0, microsecond=0)
    if now_local >= target:
        target += timedelta(days=1)
    delta = target - now_local
    return int(delta.total_seconds())


def run_daemon(chat_id: str) -> None:
    """Run the bot as a long-lived daemon process (--daemon mode).

    Sleeps until 08:00 Europe/Berlin, posts new submissions, then
    loops forever.  On error, attempts to send the traceback to the
    Telegram chat for remote debugging.

    Note: This mode is NOT used by the GitHub Actions scheduler — the
    workflow uses --once instead.  This is for self-hosted deployments.

    Args:
        chat_id: Telegram chat/channel to post to.
    """
    while True:
        try:
            delay = seconds_until_next_8am_cet()
        except Exception:
            # In case of any timezone calc issues, wait 1 hour
            delay = 3600
        time.sleep(max(1, delay))
        try:
            run_once_and_post(chat_id)
        except Exception as e:
            # Post error to chat to aid debugging (optional)
            try:
                send_message(chat_id, f"Bot error: {escape(str(e))}")
            except Exception:
                pass
        # small pause to avoid tight loop in rare cases
        time.sleep(2)


def main(argv=None):
    """CLI entry point — parse arguments and dispatch to the appropriate mode.

    Modes:
        --test        Send a test message and exit.
        --check-chat  Print chat metadata and exit.
        --once        Scrape & post once, then exit (used by GitHub Actions).
        --daemon      Run forever, posting daily at 08:00 CET.
        (default)     Send a configuration confirmation message.

    Environment variables:
        TELEGRAM_BOT_TOKEN  (required) Bot API token.
        TELEGRAM_CHAT_ID    Fallback chat ID if --chat is not provided.
        FORCE_POST          Set to '1' to bypass weekend/holiday guards.

    Args:
        argv: Optional argument list for testing; defaults to sys.argv.
    """
    parser = argparse.ArgumentParser(description="ArXiv hep-th bot")
    parser.add_argument(
        "--chat",
        dest="chat_id",
        help="Chat identifier (e.g. @publicchannel or -1001234567890)",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Send a test message to the chat and exit",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Scrape and post once, then exit",
    )
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="Run continuously and post daily at 08:00 CET",
    )
    parser.add_argument(
        "--check-chat",
        action="store_true",
        help="Check chat accessibility and print numeric chat id",
    )

    args = parser.parse_args(argv)

    if not BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN not set", file=sys.stderr)
        sys.exit(2)

    chat_id = args.chat_id or os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not chat_id:
        print(
            "ERROR: chat id missing. Provide --chat or set TELEGRAM_CHAT_ID.",
            file=sys.stderr,
        )
        print(
            "Note: invite links like https://t.me/+xxxx cannot be used as chat_id.",
            file=sys.stderr,
        )
        sys.exit(2)

    if args.test:
        send_message(chat_id, "Test: ArXiv hep-th bot can send messages ✅")
        print("Test message sent.")
        return

    if args.check_chat:
        info = get_chat(chat_id)
        print(info)
        return

    force_post = os.getenv("FORCE_POST", "").strip().lower() in {"1", "true", "yes", "on"}
    if force_post:
        print("FORCE_POST enabled — bypassing weekend/no-update guards.")

    # Weekend guard (belt-and-suspenders with workflow-level guard)
    if not force_post and _is_weekend_berlin():
        print("Weekend detected (Europe/Berlin). Skipping run.")
        return

    # Skip if arXiv hasn't updated since last successful run (avoid reposting after holidays)
    last_success = os.getenv("LAST_SUCCESS_AT", "").strip()
    try:
        if not force_post and _should_skip_for_no_update_since(last_success):
            print(
                f"No arXiv updates since last success ({last_success}). Skipping run."
            )
            return
    except Exception:
        if not force_post:
            # Non-fatal
            pass

    if args.once:
        if force_post:
            print("FORCE_POST enabled — weekend guard bypassed but duplicates still suppressed.")
        run_once_and_post(chat_id)
        print("Posted current new submissions.")
        return

    if args.daemon:
        print("Running daemon. Will post daily at 08:00 CET.")
        run_daemon(chat_id)
        return

    # Default to test to avoid surprises
    send_message(chat_id, "ArXiv hep-th bot is configured. Use --once or --daemon.")
    print("Configuration message sent.")


if __name__ == "__main__":
    main()
