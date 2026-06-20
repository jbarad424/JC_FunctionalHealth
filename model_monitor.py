#!/usr/bin/env python3
"""
Claude model monitor.

Watches Anthropic's model list and sends a Telegram alert whenever a *new*
model appears that wasn't there before -- e.g. a future "Fable 5.1",
"Mythos 5" going generally available, or a brand-new model family nobody has
seen yet. (Optionally it can also alert on Pushover.)

How it works
------------
On the first run it snapshots the set of currently-available model IDs as a
baseline ("known models") and does NOT alert -- everything that already exists
is, by definition, not new. On every later run it diffs the current set against
that baseline; any ID that wasn't in the baseline is treated as new and triggers
a notification, after which it's folded into the baseline so you're alerted
exactly once per model.

Detection sources
-----------------
  1. Anthropic Models API  (GET /v1/models)  -- requires ANTHROPIC_API_KEY.
     Authoritative, and the ONLY source that can reveal genuinely new/unknown
     model families. Strongly recommended.
  2. Public model docs page -- no key needed, but (to avoid matching unrelated
     doc URLs) it only recognises known model families. Best-effort fallback.

Optional narrowing
-------------------
Set WATCH_FILTER (e.g. "mythos") to only be alerted about new models whose ID
contains that text. Leave it empty to be alerted about ANY new model.

Standard library only -- no pip install required.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# --------------------------------------------------------------------------- #
# Configuration (all via environment variables)
# --------------------------------------------------------------------------- #

# --- Detection ---
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_VERSION = os.environ.get("ANTHROPIC_VERSION", "2023-06-01").strip()
MODELS_API_URL = os.environ.get("MODELS_API_URL", "https://api.anthropic.com/v1/models?limit=1000")

PUBLIC_MODELS_URL = os.environ.get(
    "PUBLIC_MODELS_URL",
    "https://platform.claude.com/docs/en/about-claude/models/overview",
)
CHECK_PUBLIC_PAGE = os.environ.get("CHECK_PUBLIC_PAGE", "1").strip() not in ("0", "false", "False", "")

# Known model families -- used ONLY to keep the public-page scrape from picking
# up unrelated doc slugs (e.g. "claude-in-amazon-bedrock"). The API path does
# not use this list, so genuinely new families are still caught there.
MODEL_FAMILIES = {
    f.strip().lower()
    for f in os.environ.get("MODEL_FAMILIES", "opus,sonnet,haiku,fable,mythos").split(",")
    if f.strip()
}

# Optional: only alert for new models whose ID contains this (normalized) text.
WATCH_FILTER = os.environ.get("WATCH_FILTER", "").strip()

# Alert with the full model list on the very first (baseline) run?
SEED_NOTIFY = os.environ.get("SEED_NOTIFY", "0").strip() not in ("0", "false", "False", "")

# --- Telegram (primary channel) ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# --- Pushover (optional secondary channel) ---
PUSHOVER_TOKEN = os.environ.get("PUSHOVER_TOKEN", "").strip()
PUSHOVER_USER = os.environ.get("PUSHOVER_USER", "").strip()
PUSHOVER_PRIORITY = os.environ.get("PUSHOVER_PRIORITY", "1").strip()

# --- State / misc ---
STATE_FILE = os.environ.get("STATE_FILE", "state/known_models.json")
HTTP_TIMEOUT = int(os.environ.get("HTTP_TIMEOUT", "20"))

DOCS_URL = "https://platform.claude.com/docs/en/about-claude/models/overview"
MODEL_ID_RE = re.compile(r"claude-[a-z0-9]+(?:-[a-z0-9]+)*")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg: str) -> None:
    print(f"[{now_iso()}] {msg}", flush=True)


def normalize(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def http_get(url: str, headers: dict | None = None) -> tuple[int, str]:
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return resp.status, resp.read().decode("utf-8", errors="replace")


def http_post_form(url: str, fields: dict) -> tuple[int, str]:
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"content-type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return resp.status, resp.read().decode("utf-8", errors="replace")


# --------------------------------------------------------------------------- #
# Detection -- collect the set of currently-available model IDs
# --------------------------------------------------------------------------- #

def models_from_api() -> tuple[set[str], str | None]:
    if not ANTHROPIC_API_KEY:
        return set(), "no ANTHROPIC_API_KEY set (API source skipped)"
    try:
        status, body = http_get(
            MODELS_API_URL,
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": ANTHROPIC_VERSION,
                "accept": "application/json",
            },
        )
        if status != 200:
            return set(), f"HTTP {status}"
        data = json.loads(body).get("data", [])
        return {m.get("id", "") for m in data if m.get("id")}, None
    except urllib.error.HTTPError as e:
        return set(), f"HTTP {e.code}: {e.reason}"
    except Exception as e:  # noqa: BLE001
        return set(), f"{type(e).__name__}: {e}"


def models_from_page() -> tuple[set[str], str | None]:
    if not CHECK_PUBLIC_PAGE:
        return set(), "public page source disabled"
    try:
        status, body = http_get(PUBLIC_MODELS_URL, headers={"user-agent": "claude-model-monitor/1.0"})
        if status != 200:
            return set(), f"HTTP {status}"
        found = set()
        for m in MODEL_ID_RE.findall(body.lower()):
            parts = m.split("-")
            family = parts[1] if len(parts) > 1 else ""
            # Only trust IDs whose family we recognise, so doc-URL slugs like
            # "claude-in-amazon-bedrock" don't masquerade as models.
            if family not in MODEL_FAMILIES:
                continue
            # Drop section-heading slugs ("...-and-...") and Bedrock id
            # fragments ("...-v1") that aren't real Claude API model IDs.
            if "and" in parts or parts[-1] in ("v1", "v1:0"):
                continue
            found.add(m)
        return found, None
    except urllib.error.HTTPError as e:
        return set(), f"HTTP {e.code}: {e.reason}"
    except Exception as e:  # noqa: BLE001
        return set(), f"{type(e).__name__}: {e}"


def current_models() -> tuple[set[str], dict, bool]:
    """Return (ids, source_report, any_source_succeeded)."""
    api_ids, api_err = models_from_api()
    page_ids, page_err = models_from_page()

    report = {
        "anthropic_api": {"count": len(api_ids), "error": api_err},
        "public_page": {"count": len(page_ids), "error": page_err},
    }
    api_ok = api_err is None
    page_ok = page_err is None
    log(f"signal[anthropic_api]: {len(api_ids)} models" + (f" — {api_err}" if api_err else ""))
    log(f"signal[public_page]:   {len(page_ids)} models" + (f" — {page_err}" if page_err else ""))
    return api_ids | page_ids, report, (api_ok or page_ok)


# --------------------------------------------------------------------------- #
# Notification
# --------------------------------------------------------------------------- #

def send_telegram(text: str) -> bool | None:
    """True/False if attempted; None if not configured."""
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return None
    try:
        status, body = http_post_form(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            {"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": "true"},
        )
        ok = status == 200 and json.loads(body).get("ok") is True
        log("Telegram: sent." if ok else f"Telegram: FAILED — {body}")
        return ok
    except urllib.error.HTTPError as e:
        log(f"Telegram: HTTP {e.code} — {e.read().decode('utf-8', errors='replace')}")
        return False
    except Exception as e:  # noqa: BLE001
        log(f"Telegram: failed — {type(e).__name__}: {e}")
        return False


def send_pushover(title: str, message: str) -> bool | None:
    if not (PUSHOVER_TOKEN and PUSHOVER_USER):
        return None
    try:
        status, body = http_post_form(
            "https://api.pushover.net/1/messages.json",
            {"token": PUSHOVER_TOKEN, "user": PUSHOVER_USER, "title": title,
             "message": message, "priority": PUSHOVER_PRIORITY, "url": DOCS_URL},
        )
        ok = status == 200 and json.loads(body).get("status") == 1
        log("Pushover: sent." if ok else f"Pushover: FAILED — {body}")
        return ok
    except Exception as e:  # noqa: BLE001
        log(f"Pushover: failed — {type(e).__name__}: {e}")
        return False


def notify(title: str, message: str) -> bool:
    """Send to every configured channel. Returns True if at least one channel
    was configured and all configured channels succeeded."""
    full = f"{title}\n\n{message}"
    results = [send_telegram(full), send_pushover(title, message)]
    attempted = [r for r in results if r is not None]
    if not attempted:
        log("ERROR: no notification channel configured "
            "(set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID, and/or PUSHOVER_TOKEN + PUSHOVER_USER).")
        return False
    return all(attempted)


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #

def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:  # noqa: BLE001
        log(f"WARN: could not read state ({e}); treating as empty.")
        return {}


def save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")
    log(f"State written to {STATE_FILE} ({len(state.get('known', []))} known models).")


# --------------------------------------------------------------------------- #
# Run modes
# --------------------------------------------------------------------------- #

def _passes_filter(model_id: str) -> bool:
    return (not WATCH_FILTER) or (normalize(WATCH_FILTER) in normalize(model_id))


def run_once() -> int:
    ids, _report, any_ok = current_models()
    if not any_ok:
        log("ERROR: no detection source succeeded; skipping this cycle without changing state.")
        return 0
    if not ids:
        log("WARN: zero models returned; skipping to avoid a bogus empty baseline.")
        return 0

    state = load_state()
    known = set(state.get("known", []))

    # First run -> establish the baseline silently (existing models aren't "new").
    if not known:
        new_state = {
            "known": sorted(ids),
            "seeded_at": now_iso(),
            "history": [],
            "watch_filter": WATCH_FILTER,
        }
        save_state(new_state)
        log(f"Baseline seeded with {len(ids)} models. Future new models will alert.")
        if SEED_NOTIFY:
            notify("📋 Model monitor baseline set",
                   f"Now watching {len(ids)} models. You'll be alerted when a new one appears.\n"
                   + "\n".join(f"• {m}" for m in sorted(ids)))
        return 0

    new_ids = sorted(ids - known)
    if not new_ids:
        log("No new models. State left untouched.")
        return 0

    alert_ids = [m for m in new_ids if _passes_filter(m)]
    log(f"New model id(s) detected: {new_ids}"
        + (f"; matching filter '{WATCH_FILTER}': {alert_ids}" if WATCH_FILTER else ""))

    exit_code = 0
    if alert_ids:
        title = "🚀 New Claude model detected!"
        message = (
            ("A new Claude model is available:\n" if len(alert_ids) > 1 else "A new Claude model is available:\n")
            + "\n".join(f"• {m}" for m in alert_ids)
            + f"\n\nTime: {now_iso()}\nDocs: {DOCS_URL}"
        )
        if not notify(title, message):
            exit_code = 2  # surface send failure to the scheduler

    # Fold everything we now see into the baseline so each model alerts once.
    history = state.get("history", [])
    history.append({"detected_at": now_iso(), "new_ids": new_ids, "alerted_ids": alert_ids})
    save_state({
        "known": sorted(known | ids),
        "seeded_at": state.get("seeded_at"),
        "history": history[-50:],
        "watch_filter": WATCH_FILTER,
    })
    return exit_code


def run_list() -> int:
    ids, _report, any_ok = current_models()
    if not any_ok:
        log("Could not reach any detection source.")
        return 1
    print("\nCurrently detected models:")
    for m in sorted(ids):
        print(f"  • {m}")
    print(f"\n{len(ids)} total.")
    return 0


def run_test() -> int:
    log("Sending a test notification to all configured channels...")
    ok = notify("✅ Model monitor test",
                f"Your notification setup works.\nWatching for new Claude models"
                + (f" matching '{WATCH_FILTER}'" if WATCH_FILTER else " (any new model)")
                + f".\nTime: {now_iso()}")
    return 0 if ok else 2


def run_watch(interval: int) -> int:
    log(f"Watch mode: checking every {interval}s. Ctrl-C to stop.")
    while True:
        try:
            run_once()
        except Exception as e:  # noqa: BLE001
            log(f"ERROR during cycle: {type(e).__name__}: {e}")
        time.sleep(interval)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="Alert (via Telegram/Pushover) when a new Claude model appears.")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="Single check (default; ideal for cron/CI).")
    mode.add_argument("--watch", action="store_true", help="Loop forever every --interval seconds.")
    mode.add_argument("--test", action="store_true", help="Send a test notification and exit.")
    mode.add_argument("--list", action="store_true", help="Print currently-detected models and exit.")
    p.add_argument("--interval", type=int, default=int(os.environ.get("INTERVAL", "60")),
                   help="Seconds between checks in --watch mode (default 60).")
    args = p.parse_args(argv)

    if args.test:
        return run_test()
    if args.list:
        return run_list()
    if args.watch:
        return run_watch(args.interval)
    return run_once()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
