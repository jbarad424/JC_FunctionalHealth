# Claude model monitor

Get a **Telegram** alert the moment a **new Claude model** shows up — a future
`Fable 5.1`, `Mythos 5` going generally available, or a brand-new model family
nobody has seen yet. (Pushover is supported too, as an optional second channel.)

It works by snapshotting the set of model IDs that exist **right now** as a
baseline, then alerting whenever a model ID appears that wasn't in that baseline.

> **Heads up:** `claude-fable-5` is already generally available (GA on
> 2026‑06‑09), so it goes into the baseline silently and will **not** trigger an
> alert. You'll be pinged on the *next* new model. To specifically watch for
> something not yet GA, set `WATCH_FILTER` (e.g. `mythos`).

---

## How detection works

Two independent sources; a model counts as present if **either** sees it:

| Source | Needs a key? | Notes |
|---|---|---|
| **Anthropic Models API** (`GET /v1/models`) | `ANTHROPIC_API_KEY` | Authoritative. **The only source that can reveal genuinely new/unknown model families** — strongly recommended. |
| **Public model docs page** | No | Best-effort fallback. Only recognises known families (`opus`, `sonnet`, `haiku`, `fable`, `mythos`) so it doesn't match unrelated doc URLs. |

On the **first run** it seeds the baseline silently (everything that already
exists isn't "new"). On later runs it diffs and alerts on additions, then folds
them into the baseline so you're alerted **exactly once** per model.

### On / off / back-on

It also tracks availability transitions:

- a known model **disappears** → after `OFFLINE_CONFIRM` consecutive missing
  checks (default 2, to ignore blips) you get a **⚠️ offline** alert;
- it **comes back** → you get a **🔁 back online** alert (a fresh appearance is
  a **🚀 new** alert).

This up/down signal is only meaningful from the **Anthropic API** source (the
public docs page basically never drops a model), so set `ANTHROPIC_API_KEY` if
you care about "Fable 5 went down / came back on". Set `NOTIFY_OFFLINE=0` to get
only the "came back / new" alerts without the offline ones.

> **Reusing an existing Telegram bot:** you don't need a new bot. Use the same
> `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` from another project — one bot can
> message you from any number of repos. Just add those same two values as
> secrets here.

---

## Option A — Run on GitHub Actions (free, ~5 min, no machine of your own)

A scheduled workflow (`.github/workflows/model-monitor.yml`) runs every ~5
minutes. Cron can't go below ~5 minutes, so this is **not** a true 60s monitor —
see Option B for that.

### 1. Create a Telegram bot + get your chat ID
1. In Telegram, message **@BotFather** → `/newbot` → copy the **bot token**
   (looks like `123456789:AAE…`).
2. Send any message to your new bot (e.g. "hi").
3. Get your **chat ID**: message **@userinfobot**, or open
   `https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates` in a browser and
   read `result[].message.chat.id`.

### 2. Add repository secrets
**Settings → Secrets and variables → Actions → New repository secret:**

| Secret | Required | Value |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | From @BotFather |
| `TELEGRAM_CHAT_ID` | ✅ | Your chat ID |
| `ANTHROPIC_API_KEY` | recommended | An Anthropic API key (enables authoritative detection) |
| `PUSHOVER_TOKEN` / `PUSHOVER_USER` | optional | If you also want Pushover |

Optional repository **variable** (Settings → … → Variables):
`WATCH_FILTER` — only alert for model IDs containing this text (e.g. `mythos`).
Leave unset to alert on **any** new model.

### 3. Activate it
- `schedule:` triggers only run from the repo's **default branch**, so **merge
  this to `main`** for the cron to start.
- Verify your setup now: **Actions → Claude model monitor → Run workflow →
  mode = `test`** (sends a test Telegram message), or `list` (prints the models
  it currently sees).
- The first `check` run seeds the baseline and commits `state/known_models.json`
  back to the repo. That file is how the alert stays "once per model" across runs
  — leave it in the repo.

---

## Option B — Run anywhere for a true 60-second check

No dependencies beyond Python 3.9+ (standard library only).

```bash
export TELEGRAM_BOT_TOKEN="123456789:AAE…"
export TELEGRAM_CHAT_ID="987654321"
export ANTHROPIC_API_KEY="sk-ant-…"      # optional but recommended
# export WATCH_FILTER="mythos"           # optional: only this model family

python3 model_monitor.py --test          # confirm notifications work
python3 model_monitor.py --list          # see what it detects right now
python3 model_monitor.py --watch --interval 60   # check every 60 seconds, forever
```

Run it under `systemd`, `pm2`, `tmux`, a Raspberry Pi, or any always-on box.
A single `--once` run is also cron-friendly if you'd rather use your own cron.

---

## Configuration reference (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | — | Telegram channel (primary) |
| `PUSHOVER_TOKEN`, `PUSHOVER_USER` | — | Pushover channel (optional) |
| `PUSHOVER_PRIORITY` | `1` | Pushover priority (`-2`…`2`) |
| `ANTHROPIC_API_KEY` | — | Enables the authoritative Models-API source |
| `WATCH_FILTER` | _(empty)_ | Only alert for IDs containing this text; empty = any new model |
| `CHECK_PUBLIC_PAGE` | `1` | Set `0` to disable the docs-page fallback |
| `MODEL_FAMILIES` | `opus,sonnet,haiku,fable,mythos` | Families trusted from the docs page |
| `PUBLIC_MODELS_URL` | Anthropic models overview | Page scraped for the fallback source |
| `STATE_FILE` | `state/known_models.json` | Where the baseline is stored |
| `OFFLINE_CONFIRM` | `2` | Consecutive missing checks before a model counts as offline |
| `NOTIFY_OFFLINE` | `1` | Set `0` to skip "went offline" alerts (keep new/back-online) |
| `SEED_NOTIFY` | `0` | Set `1` to also send the full list on the first (baseline) run |
| `INTERVAL` | `60` | Default seconds between checks in `--watch` |

## CLI

```
python3 model_monitor.py --once     # one check (default) — for cron/CI
python3 model_monitor.py --watch    # loop every --interval seconds
python3 model_monitor.py --test     # send a test notification
python3 model_monitor.py --list     # print currently-detected models
```

No secrets are stored in this repo — everything sensitive comes from environment
variables / GitHub Actions secrets.
