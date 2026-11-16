# Telegram Job Saver

Watches the job channels/groups you choose, matches posts against your keywords,
and re-sends every match to **your Saved Messages** so you can review and apply
later — without scrolling through dozens of channels.

```
  channels / groups  ──►  keyword filter  ──►  Saved Messages
   (config.yaml)         (include/exclude)      (review later)
```

Runs as **your own user account** (not a bot), so it can read any channel or
group you are already a member of — including private ones.

---

## 1. Install

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux / macOS
source .venv/bin/activate

pip install -r requirements.txt
```

## 2. Get your Telegram API credentials

1. Open <https://my.telegram.org> and log in with your phone number.
2. Go to **API development tools** → create an app (any name is fine).
3. Copy **api_id** (a number) and **api_hash** (32 characters).

Put them in `config.yaml`:

```yaml
telegram:
  api_id: 1234567
  api_hash: "0123456789abcdef0123456789abcdef"
  phone: "+251912345678"
```

> Keep `config.yaml` private — it is already in `.gitignore`.

## 3. Log in once

```bash
python main.py --login
```

Enter the code Telegram sends you (and your 2FA password if you have one). This
creates `sessions/jobsaver.session`; you won't be asked again.

## 4. Find your channel IDs

```bash
python main.py --chats
```

```
                    ID  TYPE     USERNAME                     TITLE
----------------------------------------------------------------------
          -1001234567890  channel  python_jobs                  Python Jobs
          -1009876543210  group    —                            Addis Tech Jobs
```

Copy the ones you want into `config.yaml` → `sources`. `@username` also works
for public channels; raw `-100…` ids work for private ones.

You do not have to edit the file by hand — the same lists can be managed from
the terminal (see [Managing sources & keywords](#managing-sources--keywords-from-the-terminal)).

## 5. Write your filters

```yaml
filters:
  keywords:                 # at least ONE must match  (OR)
    - python
    - django
    - "backend developer"
  must_include: []          # ALL must match           (AND)
  exclude:                  # if ANY matches → skipped
    - unpaid
    - internship
```

Every entry is either:

| form | meaning | example |
|---|---|---|
| `python` | plain substring, case-insensitive by default | matches "Python dev" |
| `"data engineer"` | multi-word substring | matches "Data Engineer" |
| `'re:\bsenior\b'` | regular expression (prefix `re:`) | matches "senior" only |

Set `whole_word: true` to make plain keywords word-bounded (`python` no longer
matches `pythonista`), and `case_sensitive: true` if you want exact case.

### Fuzzy matching (misspelling tolerance)

Set `filters.fuzzy: true` so plain keywords also match via approximate string
matching (rapidfuzz `partial_ratio`). This catches typos like "pythn",
"djang0", "backend devloper", etc.

```yaml
filters:
  fuzzy: true
  fuzzy_threshold: 70          # 0-100; lower = more lenient
```

| setting | default | meaning |
|---|---|---|
| `filters.fuzzy` | `false` | enable approximate keyword matching |
| `filters.fuzzy_threshold` | `70` | minimum fuzzy score (0-100) |

Only plain keywords get fuzzy matching — `re:` regex patterns, `must_include`,
and `exclude` stay exact.


## 6. Test, then run

```bash
python main.py --dry-run --once   # shows what would be saved, saves nothing
python main.py --once             # one real pass
python main.py                    # keep polling every 5 minutes
```

Press `Ctrl+C` to stop.

---

## Managing sources & keywords from the terminal

Both lists in `config.yaml` can be edited without opening the file. The commands
write the file in place and **keep all of its comments**, then check the result
still parses before saving.

```bash
python main.py --sources            # show what is watched (same as "list")
python main.py --keywords           # show the current keywords
```

| command | effect |
|---|---|
| `--sources add REF…` | append channels (`@name`, `-100…`, `t.me/…`); duplicates are skipped |
| `--sources set REF…` | replace the whole watched list |
| `--sources remove REF…` | drop exact entries |
| `--keywords add WORD…` | append keywords |
| `--keywords set WORD…` | replace all keywords |
| `--keywords remove WORD…` | drop exact keywords |

```bash
python main.py --sources add "@hahujobs" -1001212256595 "https://t.me/geezjobs_ethiopia"
python main.py --sources set "@hahujobs" "@sabijobs" "@shegarjob"
python main.py --sources remove "@example_jobs_channel"

python main.py --keywords add "backend developer" django fastapi
python main.py --keywords set python django "backend developer"
python main.py --keywords remove internship
```

Notes:

- Duplicates are compared case-insensitively (`@HaHuJobs` == `@hahujobs`).
- **In PowerShell, always quote `@username`.** A bare `@name` is parsed as
  splatting and silently dropped, so `--sources add "@hahujobs"` works but
  `--sources add @hahujobs` does nothing. `-100…` ids are fine unquoted.
- Use quotes for anything with spaces, for regex keywords
  (`'re:\b(?:django|fastapi)\b'`) and for `-100…` ids if your shell complains.
- Add `--once` / `--dry-run` to the same command to scan immediately after the
  edit: `python main.py --sources add "@hahujobs" --dry-run --once`.
- Without a run flag the command only edits the file and exits.

---

## Date windows, checkpoints & read state

### Pick a window

```bash
# everything posted between Nov 1 and Nov 16 (inclusive), then exit
python main.py --from "2026-11-01" --to "2026-11-16 23:59" --once

# only a single day
python main.py --from "2026-11-14" --to "2026-11-15" --once
```

Accepted formats: `2026-11-14`, `2026-11-14 15:34:45`, `2026-11-14T15:34:45+03:00`,
`14.11.2026 15:34`, `now`, `yesterday`. Dates without an offset are read in
`scan.timezone` (default `UTC`).

The same can be set permanently in `config.yaml`:

```yaml
scan:
  from_date: "2026-11-01"
  to_date: null            # null = now
```

`--from` / `--to` on the command line override the config for that run.

### Resume from where it stopped

Every pass stores a **per-chat checkpoint** (the timestamp of the newest message
it finished) in `jobsaver.sqlite3`. If the process stops — Ctrl+C, crash,
reboot, `--once` — the next run continues at that exact second:

```bash
python main.py --once      # stops at 2026-11-14 15:34:45
python main.py --once      # resumes there, nothing is re-saved
```

```bash
python main.py --checkpoint    # inspect the stored progress
```

```
               CHAT ID  SCANNED UP TO                  LAST MSG  TITLE
----------------------------------------------------------------------
        -1001234567890  2026-11-14 15:34:45 UTC            4211  Python Jobs
```

Order of precedence, per chat:

| priority | condition | starts at |
|---|---|---|
| 1 | `--from` / `scan.from_date` set | that date — **overrides the checkpoint** |
| 2 | `scan.resume: true` and a checkpoint exists | the checkpoint |
| 3 | otherwise | only posts newer than the last one seen |

`scan.resume: false` turns the timestamp checkpoint off and falls back to the
message-id cursor (priority 3).

To start over (re-read history, keep the local record):

```bash
python main.py --reset-checkpoint --once
```

Posts already sent to Saved Messages are never sent twice even after a reset —
the `(chat_id, message_id)` table blocks them.

### Mark as read, or leave unread

Reading a message through the API does **not** mark it as read by itself, so
the default is completely non-intrusive:

| setting | effect |
|---|---|
| `scan.mark_as_read: false` *(default)* / `--keep-unread` | content is read, chats stay **unread** in Telegram |
| `scan.mark_as_read: true` / `--mark-read` | every scanned chat is marked as read up to the last scanned message |

```bash
python main.py --from "2026-11-01" --to "2026-11-16" --mark-read --once
```

A date window that ends in the past only marks messages up to that point, so
anything newer stays unread. `--dry-run` never changes read state.

### Replace already-saved posts

By default, posts already in `saved_messages` are skipped — even inside an
explicit `--from` window — so re-running never duplicates them in Saved Messages.

To force a re-send (for example, after you fixed the keywords and want the same
window re-evaluated):

```bash
python main.py --from "2026-11-01" --to "2026-11-16" --replace-saved --once
```

This deletes the old `(chat_id, message_id)` record and writes a fresh one with a
new `saved_at` timestamp. Combine with `--dry-run` to preview what would be
re-sent without touching Saved Messages:

```bash
python main.py --from "2026-11-01" --replace-saved --dry-run --once
```

Permanent config: `runtime.replace_saved: true` (CLI `--replace-saved` wins
over the file for that run).

### Windows bigger than one pass

`scan.max_history_messages` (default 2000) caps how much history one pass reads
per chat, to stay gentle on the API. If a window is larger, run it continuously
(omit `--once`) — each pass continues where the previous one ended, so nothing
is skipped:

```bash
python main.py --from "2026-01-01" --to "2026-11-16"
```

With `--once`, raise `scan.max_history_messages` instead.

---

## How it behaves

**No duplicates.** Three guards: a per-chat checkpoint (timestamp + message id)
so a restart continues cleanly, a unique `(chat_id, message_id)` record for
every saved post, and a `was_saved()` check before sending. Restarting is always
safe.

**First run vs. later runs.** With no `from_date` and no checkpoint yet, the
first scan looks at the last `initial_scan_limit` posts (default 200) per chat,
ignoring anything older than `max_age_days` (default 14). After that it resumes
from the checkpoint.

**Saving modes** (`saving.mode`):

| mode | what lands in Saved Messages |
|---|---|
| `auto` *(default)* | the forwarded post; falls back to a text copy if the channel blocks forwarding |
| `forward` | always a forward — keeps images, formatting, "Forwarded from …" header |
| `copy` | your own message: header + post text + `t.me` link |

With `append_note: true` a short `🔎 matched: python, django` line follows each
saved post, so you know *why* it was kept.

**Local history.** Every save is also recorded in `jobsaver.sqlite3`:

```bash
python main.py --stats
```

---

## Configuration reference

| Key | Default | Meaning |
|---|---|---|
| `telegram.api_id` / `api_hash` / `phone` | — | credentials from my.telegram.org |
| `telegram.session_dir` | `sessions` | where the login session is stored |
| `sources` | — | list of channels/groups (`@name`, `-100…`, `t.me/…`) — edit with `--sources add/set/remove` |
| `filters.keywords` | — | OR list — at least one must match — edit with `--keywords add/set/remove` |
| `filters.must_include` | `[]` | AND list — all must match |
| `filters.exclude` | `[]` | any match → post skipped |
| `filters.case_sensitive` | `false` | exact case matching |
| `filters.whole_word` | `false` | word boundaries for plain keywords |
| `filters.fuzzy` | `false` | approximate keyword matching (misspelling tolerance) |
| `filters.fuzzy_threshold` | `70` | minimum fuzzy score, 0-100 |
| `saving.mode` | `auto` | `auto` / `forward` / `copy` |
| `saving.destination` | `me` | `me` = Saved Messages |
| `saving.include_link` | `true` | add link in copy mode |
| `saving.append_note` | `false` | append the "matched: …" line (forward mode) |
| `saving.header` | `🎯 {channel} · {date}` | copy-mode header template |
| `scan.from_date` | `null` | start of the window (overrides the checkpoint) |
| `scan.to_date` | `null` | end of the window, inclusive (`null` = now) |
| `scan.resume` | `true` | continue from the stored per-chat checkpoint |
| `scan.timezone` | `UTC` | zone for dates without an offset (IANA name) |
| `scan.mark_as_read` | `false` | `false` leaves scanned chats unread |
| `scan.max_history_messages` | `2000` | cap per chat per pass in window mode |
| `runtime.poll_interval_seconds` | `300` | polling frequency |
| `runtime.initial_scan_limit` | `200` | posts examined per chat on the first run |
| `runtime.max_messages_per_poll` | `100` | posts handled per chat per cycle |
| `runtime.max_age_days` | `14` | ignore older posts (`0` = no limit) |
| `runtime.dry_run` | `false` | never save, only print |
| `runtime.replace_saved` | `false` | re-send posts already in Saved Messages |
| `storage.database` | `jobsaver.sqlite3` | dedupe/history database |
| `storage.log_file` | `logs/jobsaver.log` | rotating log file |

Any string in the config can reference an environment variable:
`api_hash: "${TG_API_HASH}"`.

Relative paths (`session_dir`, `database`, `log_file`) are resolved against the
directory containing the config file, so the program behaves the same no matter
where you launch it from.

---

## Keeping it running (Windows)

Option A — Task Scheduler:
1. Create Task → *Run whether user is logged on or not*
2. Program: `C:\...\Ai_parser_telegram_jobs\.venv\Scripts\python.exe`
3. Arguments: `main.py`
4. Start in: `C:\...\Ai_parser_telegram_jobs`
5. Trigger: *At startup*, and tick "Restart the task if it fails".

Option B — `run.bat` (double-click and leave the window open):

```bat
@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (.venv\Scripts\python.exe main.py %*) else (python main.py %*)
pause
```

Linux/macOS: `./run.sh`, or a `systemd --user` service / `tmux` session.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Cannot resolve source '…'` | You're not a member of that chat, or the id is wrong. Run `python main.py --chats` and copy the **ID** column exactly. |
| Nothing is saved, no errors | Run `python main.py --dry-run -v` — it prints every post it looks at and why each one was skipped (`excluded by …`, `missing:…`). Also check `max_age_days` and `initial_scan_limit`. |
| `FloodWaitError` | The client sleeps automatically; long waits are logged. Raise `poll_interval_seconds`. |
| Forwarding fails | The channel protects its content. `mode: auto` falls back to a text copy; use `mode: copy` to always copy. |
| Session lost / `AuthKeyUnregistered` | Delete `sessions/jobsaver.session` and run `python main.py --login` again. |
| Want to re-check old posts | `python main.py --reset-checkpoint --once`, or give an explicit `--from` date. |
| Scan stops immediately | A past `scan.to_date` (or `--to`) finishes and exits by design. Set `null` / drop `--to` for continuous polling. |
| Wrong hours in checkpoints | Dates without an offset use `scan.timezone`. Set it to your zone (e.g. `Africa/Addis_Ababa`). |
| `needs the 'tzdata' package` | `pip install tzdata` (required on Windows for non-UTC zones). |

### Notes

* Never commit `config.yaml`, `*.session`, or `jobsaver.sqlite3` — they contain
  your credentials and message history.
* This uses your personal account. Extremely aggressive polling is not needed
  (300s is plenty for job channels) and can trigger Telegram rate limits.
* Run one instance per session file; a second instance will conflict on the
  SQLite database and the session.
