# Global Trade Copier on a VPS

Everything needed to stand the copier up on a fresh Windows VPS, and the
things that are deliberately not in this package.

## What has to live on the same machine

Every slave the copier talks to is on `127.0.0.1`:

| | |
|---|---|
| IB Gateway | `127.0.0.1:4002` |
| MT5 IC Markets (SlaveBridge) | `127.0.0.1:5561` |
| MT5 Darwinex (SlaveBridge) | `127.0.0.1:5562` |
| MT4 master (TradeCopierBridge) | publishes to the copier's `5555` / `5557` |

So the copier is not something you move on its own. It belongs on the box
that runs the terminals and IB Gateway, and that box must be Windows.

**The Operations hub does not belong here.** The whole point of the push
model is that the hub keeps working, and keeps history, when this VPS is
unreachable. Put both on one machine and a single failure takes out the
trading and the thing meant to tell you trading stopped.

## Install

1. Unzip anywhere. `C:\Apps\gtc` is a reasonable place; it must **not** be
   inside OneDrive, Dropbox or any other synced folder.
2. `.\Install-Copier.ps1`
   Creates `C:\GTC\{data,logs,config}`, builds the virtual environment,
   installs the pinned dependencies, and lists what it cannot create.
3. Put the three things it names in place by hand (below).
4. Start MT4/MT5 and IB Gateway, and let them finish connecting.
5. `.\restart-copier.ps1` — it waits for `/health` before claiming success.
6. `.\Register-CopierTask.ps1` — elevated, once.

## The three things not in this package

None of them belong in a zip, and the install script refuses to invent
them.

**`hub\.env`** — copy `hub\.env.example`. Holds the IBKR Flex token and
query id, and the paths that keep state off the application directory.

**`hub\config\hub_reporter.json`** — copy the `.example`, set `url` to the
hub, set `enabled` to `true`. `copier_id` is configured and never derived,
so moving the copier to another host does not look like a new copier.

**`C:\GTC\config\hub_key.txt`** — the hub's API key, one line, nothing
else. Issued by the hub.

> A reporter with no key logs `off` once and is then silent. A copier that
> starts cleanly, copies trades correctly and reports nothing to the hub is
> a plausible result of skipping this one file. The hub raises *copier
> silent*, and you will be looking at the copier.

## Moving a live registry

`C:\GTC\data\tradecopier.db` is the record of every open position. It is
**not** in the package, because overwriting a VPS's live registry with a
stale copy would lose whatever that copier currently believes it holds.

If you are migrating an existing copier rather than starting fresh:

1. Stop the old copier. Stop it, do not just close the window — a copy
   taken mid-write is a copy of a half-finished transaction.
2. Copy `tradecopier.db` across.
3. Verify on the target before starting:
   `python -c "import sqlite3;d=sqlite3.connect(r'C:\GTC\data\tradecopier.db');print(d.execute('PRAGMA integrity_check').fetchone())"`
4. Never run both copiers against the same accounts. Two copiers both
   acting on one master's signals will double every position.

## Running it

| | |
|---|---|
| restart | `.\restart-copier.ps1` |
| health | `http://127.0.0.1:8010/health` |
| slave state | `http://127.0.0.1:8010/slaves` |
| logs | `C:\GTC\logs\hub.log` (rotates, 5 MB x 3) |

The Scheduled Task starts at logon, after a 60-second delay so the
terminals and Gateway are up first. That means **auto-logon must be on and
the session must stay signed in** — disconnect from RDP, never sign out.
Signing out closes the terminals and the copier with them.

The task restarts the copier if it **crashes**. A copier that is running
but has stopped copying still looks healthy to Task Scheduler; that is
what `/health`, the slave `connected` flags and the hub's own alert are
for.

## What is not solved yet

- **No dashboard.** The hub serves its API on 8010 but does not serve the
  UI; the dashboard runs from a Vite dev server. On a VPS there is
  currently no web interface unless you run `npm run dev` in `ui/`.
- **The Flex token sits in `.env`** beside the application. Real
  environment variables on the scheduled task would be better: no file,
  nothing to copy, nothing to sync.
- **One Flex query for two consumers.** If the Operations hub pulls the
  same query id, the rate limit (error 1018) is per token and bites on
  frequency. Give the hub its own.
