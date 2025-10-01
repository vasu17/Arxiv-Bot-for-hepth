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


BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

STATE_DIR = Path(__file__).with_name(".state")
STATE_PATH = STATE_DIR / "posted.json"
MAX_TRACKED_IDS = 2000


def _load_state() -> Tuple[set[str], Optional[str]]:
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
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    ids = sorted(posted_ids)
    if len(ids) > MAX_TRACKED_IDS:
        ids = ids[-MAX_TRACKED_IDS:]
    STATE_PATH.write_text(
        json.dumps({"posted_ids": ids, "last_run_iso": last_run_iso}, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def send_message(chat_id: str, text: str, parse_mode: str = "HTML") -> dict:
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
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getChat"
    resp = requests.get(url, params={"chat_id": chat_id}, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _iter_new_submission_nodes(soup):
    # Find the H3 header for New submissions, then yield the sequence of
    # alternating <dt>/<dd> nodes up to (but not including) the next <h3>.
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
    # Build (dt, dd) pairs from the stream of nodes after the H3 header.
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
    # Use an exactauthor search on Inspire HEP.
    # Example: https://inspirehep.net/authors?search=exactauthor%3A%22First%20Last%22
    q = quote(f'"{name}"')
    return f"https://inspirehep.net/authors?sort=bestmatch&size=25&page=1&q={q}"


def format_entry_html(entry: dict) -> str:
    # Build the message using HTML parse_mode for hyperlinks
    title = escape(entry.get("title", "")).strip()
    comments = escape(entry.get("comments", "")).strip()
    abstract = escape(entry.get("abstract", "")).strip()

    # Authors with Inspire links
    linked_authors = []
    for name in entry.get("authors", []):
        url = _inspire_author_link(name)
        linked_authors.append(f'<a href="{url}">{escape(name)}</a>')
    authors_html = ", ".join(linked_authors) if linked_authors else ""

    parts = []
    if title:
        parts.append(f"Title:- {title}")
    if authors_html:
        parts.append(f"Author:- {authors_html}")
    if comments:
        parts.append(f"Comment:- {comments}")
    if abstract:
        parts.append(f"Abstract:- {abstract}")
    # Add arXiv HTML/PDF links when available
    abs_url = entry.get("abs_url") or ""
    pdf_url = entry.get("pdf_url") or ""
    link_lines = []
    if abs_url:
        link_lines.append(f'HTML:- <a href="{abs_url}">arXiv</a>')
    if pdf_url:
        link_lines.append(f'PDF:- <a href="{pdf_url}">PDF</a>')
    if link_lines:
        parts.extend(link_lines)

    text = "\n".join(parts)

    # Telegram message limit is 4096 characters
    if len(text) > 4000:
        text = text[:3950] + "..."
    return text


def scrape_hep_th_new() -> list:
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
    candidate = entry.get("id") or ""
    candidate = candidate.strip()
    if candidate:
        return candidate
    abs_url = entry.get("abs_url") or ""
    if "/abs/" in abs_url:
        return abs_url.rsplit("/abs/", 1)[-1]
    return None


def run_once_and_post(chat_id: str) -> None:
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
    # CET/CEST handling: derive 8am in Europe/Berlin local time
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
