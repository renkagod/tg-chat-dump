# tg-chat-dump

[![PyPI](https://img.shields.io/pypi/v/tg-chat-dump.svg)](https://pypi.org/project/tg-chat-dump/)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)
[![Telethon](https://img.shields.io/badge/built%20with-Telethon-26A5E4.svg)](https://github.com/LonamiWebs/Telethon)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

<p align="center"><img src="assets/speed.svg" width="100%" alt="Over 100,000 messages per minute: ~115,000 with tg-chat-dump, ~29,000 with Telegram Desktop's export, ~6,000 through the regular API"></p>

<p align="center"><img src="assets/demo.gif" width="100%" alt="Interactive mode: find a chat with live suggestions, then dump 421,393 messages in 3m 47s"><br><sub>A 421,393-message group dumped in 3m 47s with two accounts (the download part is sped up 12×; account names are blurred).</sub></p>

Dump an entire Telegram chat into SQLite and a folder per forum topic at **over 100,000 messages a minute**. It runs Telegram's own data-export mode with parallel workers on several accounts, so a chat of several hundred thousand messages is done in minutes: about 4× faster than Telegram Desktop's own export, and a whole forum in one pass instead of topic by topic.

```
out/1234567890_My_Chat/
├── 1_General/
│   ├── messages.jsonl
│   └── messages.txt
├── 12_Off-topic/
│   ├── messages.jsonl
│   └── messages.txt
└── 37_Announcements/
    ├── messages.jsonl
    └── messages.txt
```

## Features

- Over 100,000 messages a minute with Telegram's export mode and two accounts: about 4× Telegram Desktop's export and 10× the regular API (see [Performance](#performance)).
- Run it without arguments for the interactive mode: add or remove accounts, find a chat as you type, watch a progress bar with ETA.
- A whole forum in one pass. Messages are sorted into topics as they arrive.
- The chat is split into message-id ranges that several workers download at once. Telegram limits each account separately, so every extra account adds about as much speed as the first.
- Progress is committed to SQLite every 100 messages. An interrupted run resumes where it stopped, and later runs fetch only new messages and append them to the files, so a repeat run of a large chat takes seconds. When a forum topic is renamed, its folder is renamed too.
- Optional extras, each a toggle: reactions and views, poll results, formatting with hidden links, comments under channel posts, and Telegram's export mode (see [Extras](#extras)).
- Filters by date range, author or message type, such as links or documents.
- On `FLOOD_WAIT` the run sleeps and then continues on its own.
- SOCKS5 and HTTP proxies, for networks where Telegram is blocked.
- `messages.txt` for reading, `messages.jsonl` for scripts, and the SQLite database for queries.

## Quick start

1. Create an app at [my.telegram.org](https://my.telegram.org) → *API development tools* and note its `api_id` and `api_hash`.
2. Install and run:

   ```sh
   pipx install tg-chat-dump
   tg-chat-dump
   ```

   `uv tool install tg-chat-dump` or `pip install tg-chat-dump` work too. Installed this way, settings and sessions are kept in `~/.tg-chat-dump` and dumps go to `~/tg-chat-dump`.

   From source, with [uv](https://docs.astral.sh/uv/), everything stays inside the checkout (`.env`, `data/`, `out/`):

   ```sh
   git clone https://github.com/renkagod/tg-chat-dump.git
   cd tg-chat-dump
   uv run dump.py
   ```

The first start asks for `api_id` and `api_hash` and saves them to `.env`. On the first dump Telegram sends a "Data export request" message to the account: that is the fast export mode, allow it there (see [Extras](#extras)). After that it works like this:

```
Accounts:
  1) Alice @alice
  2) Bob
Output folder: D:\Telegram dumps
Extras: takeout, meta, polls
[a] add account  [d N] remove  [f] output folder  [x] extras  [Enter] continue >
Loading your chats… 312 found.

Search chat (name, @username or id; Tab completes, Enter lists all): forum
   1) Forum Club  @forumclub  [forum]  2/2 accounts
   2) Forum News  @forumnews  [channel]  1/2 accounts
Number, [p] search public chats, or Enter to search again > 1

Forum Club  [forum]  id -1001234567890
~152,310 messages
Dump it with 2 accounts, extras: takeout, meta, polls? [Y/n, f = filters]
[████████░░░░░░░░░░░░]  41%  62,104 saved  112,400/min  ETA 48s
```

- Matching chats pop up under the cursor as you type; Tab fills in the highlighted one and Enter picks it.
- If nothing in your chats matches the search, public groups and channels are searched too. Public chats can be dumped without joining.
- Every account that can see the chat is used. A private chat or a small (non-super) group is the exception: each account has its own copy of it, so you see how many messages each one has and pick one (Enter takes the biggest).
- `x` toggles the extras, `f` sets the output folder. Both are saved. By default the output goes to `~/tg-chat-dump`, or `out/` in a source checkout.
- Answering `f` instead of `Y` asks for filters for this one dump.
- **Ctrl+C** stops the dump; the next run resumes it.
- If Telegram is blocked in your network, set `TG_PROXY` in `.env`, for example `TG_PROXY=socks5://127.0.0.1:1080`.
- The interactive mode is colored in a terminal; set `NO_COLOR=1` to turn colors off.

## Extras

`takeout` is on by default, the others are off. Switch them with `x` in the interactive mode, `--with` on the command line (it replaces the saved list), or `TG_OPTIONS` in `.env`; `TG_OPTIONS=none` turns all of them off.

| Extra | Adds | Cost |
|---|---|---|
| `meta` | `reactions` (`{"👍": 12}`), `views`, `forwards`, `replies` | none, it is already in every message |
| `polls` | poll question, answers, votes; checklist items and which are done | none |
| `markdown` | `text_md`: the text as Markdown, with bold, links behind words, mentions | none |
| `comments` | for a channel: the comments under its posts, from the linked discussion group, in `comments/`, grouped by post | a second dump of the discussion group |
| `takeout` | runs the dump in Telegram's data-export mode, the one Telegram Desktop uses; it skips the usual rate limits, about 10× faster (see [Performance](#performance)) | the first time, Telegram asks you to allow the export from a phone logged in to that account, otherwise after 24 hours; until then the dump runs at normal speed |

Turning an extra on later does not touch messages that are already saved. A new run adds it to new messages only.

## Filters

```sh
uv run dump.py --chat @somegroup --since 2026-01-01 --until 2026-03-31   # a date range, inclusive
uv run dump.py --chat @somegroup --from @alice                         # one author
uv run dump.py --chat @somegroup --type links                          # links, docs, photos, videos, voice, pinned, ...
```

A filtered dump gets its own database and folder, for example `out/1234567890_My_Chat_since-2026-01-01_links/`, so it never mixes with the full dump. Filters can be combined. They work with whole-chat dumps, not with `--topics`.

## Command line

With arguments it runs without questions, for scripts and cron. Settings come from `.env` (see `.env.example`). The examples use the source checkout; with the installed package, replace `uv run dump.py` with `tg-chat-dump`:

```sh
uv run dump.py --chat @somegroup                     # whole chat (or only new messages on repeat runs)
uv run dump.py --chat @somegroup --topics 12,37      # only these forum topics
uv run dump.py --chat @somegroup --with meta,polls   # with extras
uv run dump.py --chat @somegroup --export-only       # rebuild the folders from the database, no network
uv run dump.py --login acc2                          # add another account (see below)
uv run dump.py --chat @somegroup --workers 2         # workers per account, default 3
uv run dump.py --chat @friend --account acc2         # only this account: session name, @username or id
uv run dump.py --chat @somegroup --out "D:\Telegram dumps"   # output folder, remembered in .env
```

### More accounts, more speed

Every extra account that is a member of the chat adds throughput. Add one with `a` in the interactive mode, or:

```sh
uv run dump.py --login acc2   # log in once, saved to data/acc2.session
```

Each `data/*.session` file is picked up automatically. Accounts that are not logged in or are not members of the chat are skipped with a warning. Private chats and small groups are numbered separately in every account, so they are always dumped by one account; on the command line it is the one with the most messages, unless `--account` names another.

### Performance

Measured on a large forum supergroup (several hundred thousand messages):

| Accounts | Speed | Full dump |
|---|---|---|
| 1 | ~6,000–7,000 messages/min | ~1h 45m |
| 2 | ~11,500 messages/min | ~1h |
| 2, with `takeout` | ~115,000 messages/min | ~6m |
| Telegram Desktop export, for comparison | ~29,000 messages/min | ~25m (extrapolated) |

The Telegram Desktop row comes from a separate side-by-side test on one 420,912-message public group, text only: Telegram Desktop exported ~29,000 messages/min and tg-chat-dump with `takeout` ~110,000. Its full-dump time in the table is extrapolated from that rate.

Telegram's per-account rate limit sets the ceiling: about 6,000 messages/min without takeout and about 60,000 with it. More than 3 workers per account does not help (3, 6 and 12 measured the same), and with many more Telegram adds flood waits. Turning on `takeout` gives the biggest jump; after that, more speed comes only from more accounts.

## Output

Each topic folder contains:

**`messages.txt`**: one line per message:

```
[2026-03-14 09:01:12] #1042 Alice: hi everyone  [👍 3 · 120 views]
[2026-03-14 09:02:40] #1043 Bob (reply to #1042): <MessageMediaPhoto> look at this
[2026-03-14 09:05:00] #1044 Carol: <poll> Lunch? | Pizza (4) | Sushi (2) | 6 votes
```

**`messages.jsonl`**: one JSON object per message:

| Field | Meaning |
|---|---|
| `id` | message id |
| `date`, `edit_date` | ISO 8601, UTC |
| `topic_id` | forum topic (`1` = General, `null` in non-forum chats) |
| `sender_id`, `sender` | author id and display name |
| `text` | message text |
| `reply_to`, `reply_top` | replied-to message and thread root |
| `fwd_from` | original author of a forwarded message |
| `media` | media type (`MessageMediaPhoto`, `MessageMediaDocument`, ...) |
| `action` | service message type (`MessageActionTopicCreate`, ...) |
| `grouped_id` | album id |
| `text_md`, `views`, `forwards`, `replies`, `reactions`, `extra` | only with the matching [extras](#extras) |

The raw data is in `data/<chat id>.sqlite`, with tables `messages`, `topics`, `tasks` (download progress) and `exports` (what was last written to each folder, so a repeat run only appends). While a dump runs, `.sqlite-wal` and `.sqlite-shm` files sit next to the database; they are part of it and disappear when the run ends, so copy the database only after that.

## Limitations

- Downloads text and metadata only. Media files are recorded by type and not downloaded.
- The account only sees what Telegram shows it: if the chat hides history from new members, older messages are not available.
- `--topics` cannot fetch the General topic on its own; the full-chat mode covers it.

## Responsible use

Using your own account through the API falls under Telegram's [API Terms of Service](https://core.telegram.org/api/terms). Dumps contain other people's messages, so keep them private and comply with your local data-protection laws. `data/*.session` files are full logins to your accounts: never share or commit them.

## Development

```sh
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

The code is in `tgdump/`: `fetch` downloads, `store` turns messages into database rows, `export` writes the folders, `cli` and `interactive` are the two front ends.

Issues and pull requests are welcome. Please run the tests and linters before opening a PR.

## License

[MIT](LICENSE)
