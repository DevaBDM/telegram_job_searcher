# Telegram Job Saver

Watches the job channels/groups you choose, matches posts against your ordered rules,
and re-sends every match to **your Saved Messages** so you can review and apply
later — without scrolling through dozens of channels.

```
  channels / groups  ──►  rule filter  ──►  Saved Messages
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
the terminal (see [Managing sources & rules](#managing-sources--rules-from-the-terminal)).

## 5. Write your rules

```yaml
filters:
  rules:                    # ordered: index 0 wins
    - "lead architect"      # high-priority include
    - "!unpaid"             # exclude
    - "!internship"
    - "python"              # ordinary include
    - "django"
    - "w:IT"                # whole word only (never fuzzy)
  must_include: []          # ALL must match (AND)
```

**Position is priority.** The first matching rule decides the outcome, so a
high-priority inclusion protects a post from a lower exclusion, and a
high-priority exclusion beats a lower inclusion.

| form | meaning | example |
|---|---|---|
| `python` | substring, case-insensitive by default | matches "Python dev" |
| `"data engineer"` | multi-word substring | matches "Data Engineer" |
| `!internship` | exclusion (a match drops the post) | drops "unpaid internship" |
| `w:IT` | whole word only, **never fuzzy** | matches "IT support", not "with" |
| `!w:IT` | whole-word exclusion | drops "IT admin", keeps "edit" |
| `'re:\bsenior\b'` | regular expression, **never fuzzy** | matches "senior" only |
| `'!re:\bintern\b'` | regex exclusion | drops "intern", keeps "internal" |

`w:` exists for short rules like `IT`. A plain substring match would hit
"w**it**h", "pos**it**ion" and "qual**it**y"; `w:IT` only matches the standalone
word. Both `w:` and `re:` rules are exact on purpose — fuzzy matching is skipped
for them.

Set `case_sensitive: true` if you want exact case.

### Fuzzy matching (misspelling tolerance)

Set `filters.fuzzy: true` so plain substring rules also match via approximate
string matching (rapidfuzz `partial_ratio`). This catches typos like "pythn",
"djang0", "backend devloper", etc.

```yaml
filters:
  fuzzy: true
  fuzzy_threshold: 70          # 0-100; lower = more lenient
```

| setting | default | meaning |
|---|---|---|
| `filters.fuzzy` | `false` | enable approximate matching for substring rules |
| `filters.fuzzy_threshold` | `70` | minimum fuzzy score (0-100) |

Only plain substring rules get fuzzy matching — `w:`, `re:` and `must_include`
stay exact.


## 6. Test, then run

```bash
python main.py --dry-run --once   # preview matches as a table, save nothing
python main.py --once             # one real pass
python main.py                    # keep polling every 5 minutes
```

`--dry-run` prints one row per match, so the matched rule is easy to spot:

```
    #  CHANNEL                       MATCHED RULE              LINK
  ---  ----------------------------  ------------------------  ----
    1  GeezJobs - Ethiopia Jobs      software                  https://t.me/geezjobs_ethiopia/26123
    2  Info                          w:IT                      https://t.me/newinfo3/5076
```

Long channel names and long rule lists are ellipsized to keep the columns
aligned; the link is always last and never truncated.

Press `Ctrl+C` to stop.

---

## Managing sources & rules from the terminal

Both lists in `config.yaml` can be edited without opening the file. The commands
write the file in place and **keep all of its comments**, then check the result
still parses before saving.

```bash
python main.py --sources            # show what is watched (same as "list")
python main.py --rules              # show the ordered rules
```

| command | effect |
|---|---|
| `--sources add REF…` | append channels (`@name`, `-100…`, `t.me/…`); duplicates are skipped |
| `--sources set REF…` | replace the whole watched list |
| `--sources remove REF…` | drop exact entries |
| `--rules add RULE…` | append rules at the **end** (lowest priority) |
| `--rules set RULE…` | replace the whole rule list in the given order |
| `--rules remove RULE…` | drop exact rules |

```bash
python main.py --sources add "@hahujobs" -1001212256595 "https://t.me/geezjobs_ethiopia"
python main.py --sources set "@hahujobs" "@sabijobs" "@shegarjob"
python main.py --sources remove "@example_jobs_channel"

python main.py --rules add "backend developer" "w:IT" "!internship"
python main.py --rules set "lead architect" "!unpaid" python django
python main.py --rules remove "!internship"
```

Notes:

- Duplicates are compared case-insensitively (`@HaHuJobs` == `@hahujobs`).
- **In PowerShell, always quote `@username`.** A bare `@name` is parsed as
  splatting and silently dropped, so `--sources add "@hahujobs"` works but
  `--sources add @hahujobs` does nothing. `-100…` ids are fine unquoted.
- Use quotes for anything with spaces, for regex rules
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
anything newer stays unread. `--dry-run` never changes read state and never
advances the per-chat checkpoint — a preview cannot consume posts that a later
real run still needs to see.

### Replace already-saved posts

By default, posts already in `saved_messages` are skipped — even inside an
explicit `--from` window — so re-running never duplicates them in Saved Messages.

To force a re-send (for example, after you fixed the rules and want the same
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

### Review saved matches in deadline order

`python main.py --review` walks every saved post, soonest deadline first (posts
without a detected deadline come last). Each post is opened directly in
Telegram Desktop through a `tg://` deep link — nothing is re-sent to Saved
Messages during review.

```bash
python main.py --review

# start with posts whose deadline could not be extracted
python main.py --review --null-first
```

`--null-first` puts the unknown-deadline posts at the front, ordered by the date
the job was posted (oldest first), followed by the dated posts in deadline
order. Each heading names the rule that matched instead of the Telegram message
id, so an unknown deadline still has the posting date next to it:

```
[1/395] id=17 Shegerjobs.com matched=Computer Science deadline=unknown posted=2026-11-16
  https://t.me/shegerjobs/18067
```

`matched=` is the rule that put the post in the list (`w:IT`, `Computer
Science`, …); `id=` is the row id to use with `--tailor` / `--note`.

| key | action |
|---|---|
| `Enter` / `o` | open the current post in Telegram Desktop |
| `n` | next post (nothing opened, nothing deleted) |
| `>` | skip this post but open the next one in Telegram Desktop |
| `x` | delete the current post and open the next one |
| `d` | delete the current post |
| `q` | quit |
| `a` `l` `j` `s` `p` | apply / letter-only / job note / show text / paste a post — see [From review to a tailored CV and a job note](#from-review-to-a-tailored-cv-and-a-job-note) |

If the Telegram app is not registered for `tg://` links, the post opens in your
browser instead. `--no-save` records matches locally without sending anything
to Saved Messages, so `--review` is the natural companion to it.

---

## From review to a tailored CV and a job note

`--review` is also the hand-off to the two local agents that already live in the
vault. Nothing about them is re-implemented here: this program writes the post
text to a file and runs their normal one-shot entry points, streaming their
output back into the review window. There is no second window to close — the
agent runs where you are standing and the review prompt comes back on its own.

```
--review ──► a / l ──► paste the FULL posting (EOF)
                            │
                            ▼
                 _pipeline/inbox/<stamp>-<slug>.md  (full text + link/channel/posted)
                            │
                            ▼
            agent_tailer/run.py --posting <file>
                  (CV + cover letter, or letter only)
                            │
                            ▼
           a / r / s / n ──► agent_jobnote/run.py --posting <file>
                  (applied, or research-only)
```

**The Telegram post is only a teaser.** The saved row holds a short summary that
links to the real listing, and the database deliberately stores metadata only
(no post text, no schema change, no re-saving). So the apply flow asks you to
open the link, copy the listing, and paste it into the terminal. That pasted
text is what both agents receive; the link, channel and posted date still come
from the saved Telegram row.

```
a ─► paste the FULL posting (open the link), then a line with only  EOF
      (type EOF straight away to use the short Telegram text instead)
        │
        ▼
   tailor the CV + cover letter? [Y/n] ─► run agent_tailer inline, then come back
                                        └─ n ─► straight to the next question
        ▼
   record it?  a = applied · r = researching · n = no record
```

| key | action |
|---|---|
| `a` | Apply: paste the full posting, ask whether to tailor, then ask how to record it. |
| `l` | Same, cover letter only (`agent_tailer --letter-only`): no CV, no PDF. |
| `j` | Write the job note for the current post without tailoring (still asks for the full text). |
| `t` | Alias for `a` (kept from the first version of this section). |
| `s` | Show the current post's text (first 60 lines) without leaving the review. |
| `p` | Paste a posting that is not in the list; finish with a line containing `EOF`. Use it for a multi-line paste. |
| *(free text)* | A long single-line paste at the prompt is treated as `p`, as long as it is longer than one command word. |

Paste a multi-line listing and finish it with a line containing only `EOF`
(`:END`, `###END` and `---END---` work too). If you paste nothing at all, the
short Telegram text is used as a fallback and you are told so — good enough for
a quick research note, rarely good enough for a CV.

| choice | result |
|---|---|
| `a` | `agent_jobnote --cover-letter`: the letter that was just written is copied into the note, `status: applied`, submission date set. If no letter was tailored in this run, you are asked once before the *current* master letter is copied. |
| `r` | Research note: the description block is filled, the letter block stays empty. |
| `n` | Nothing is written to the vault. |

If `agent_tailer` fails (model server down, unusable output, non-zero exit),
nothing is recorded in the vault and the review simply continues.

The same flow without prompts — the full text now has to come from you here as
well, as a file or on stdin, because the saved post is only the teaser:

```bash
python main.py --pipeline-check                            # agent paths + model server status
python main.py --tailor 42 --posting-file full-posting.txt # id= from a --review line
python main.py --tailor 42 --posting-file -                # pipe the listing in
python main.py --tailor 42 --posting-file - --letter-only  # letter only
python main.py --tailor 42 --posting-file - --note-mode none  # tailer only
python main.py --note 42 --posting-file -                  # note only
python main.py --note 42 --posting-file - --note-mode applied # note + the current letter
python main.py --tailor 42 --from-telegram                 # short text on purpose
```

Without `--posting-file` or `--from-telegram` these commands stop and show the
options instead of quietly tailoring against a teaser.

Files this bridge writes:

| path | content |
|---|---|
| `_pipeline/inbox/<stamp>-<slug>.md` | the exact post text handed to both agents, plus `Posting:` / `Channel:` / `Posted:` lines |
| `_pipeline/state.json` | which posts were tailored/noted, and whether they were marked applied |

`_pipeline/` is git-ignored. The two agents keep their own artifacts and their
own write sandboxes: `agent_tailer/out/`, `agent_jobnote/out/`, and the vault
note itself.

Both agents need `llama-server` on `http://127.0.0.1:1234/v1` (or
`LLAMA_SERVER_URL`, which both agents and `--pipeline-check` read). The review
list itself only needs Telegram.

Running from WSL against a server started in Windows? `127.0.0.1` inside WSL is
the Linux guest, not Windows, so point at the host instead:

```bash
export LLAMA_SERVER_URL="http://$(cat /etc/hostname).local:1234/v1"
```

`--pipeline-check` prints which endpoint it probed, so this shows up immediately.

Moved a folder? Point the bridge at it instead of editing the file:

```bash
PIPELINE_TAILER_DIR="D:\...\agent_tailer"
PIPELINE_JOBNOTE_DIR="D:\...\agent_jobnote"
PIPELINE_PYTHON="C:\...\python.exe"
```

---

## Configuration reference

| Key | Default | Meaning |
|---|---|---|
| `telegram.api_id` / `api_hash` / `phone` | — | credentials from my.telegram.org |
| `telegram.session_dir` | `sessions` | where the login session is stored |
| `sources` | — | list of channels/groups (`@name`, `-100…`, `t.me/…`) — edit with `--sources add/set/remove` |
| `filters.rules` | — | ordered rules, index 0 wins — edit with `--rules add/set/remove` |
| `filters.must_include` | `[]` | AND list — all must appear literally |
| `filters.exclude_symbol` | `!` | prefix marking an exclusion rule |
| `filters.case_sensitive` | `false` | exact case matching |
| `filters.fuzzy` | `false` | approximate matching for substring rules (misspelling tolerance) |
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
