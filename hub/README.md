# Trade Copier Hub (Phase 6)

FastAPI + asyncio hub that receives trade signals from MT4 bridge EAs over
ZeroMQ, deduplicates and persists them, and fans them out to per-slave
queues. Phase 3 added cross-asset autonomous copying, Phase 4 added the
REST/WebSocket API and React dashboard, Phase 5 hardened the hub against
real-world conditions. Phase 6 is deployment: running always-on on a VPS,
plus a dormant IBKR adapter so migrating off Schwab later is a config
change, not a rewrite.

## Architecture (Phase 6 additions: deployment + IBKR)

- `app/adapters/ibkr.py` -- `IBKRAdapter`, talking to Interactive Brokers
  via TWS/IB Gateway through `ib_insync`. **Dormant**: registered in the
  adapter factory (`broker_type: "ibkr"`) and structurally complete to the
  same standard as `SchwabAdapter`, but `ib_insync` is lazy-imported so it
  isn't a hard dependency until you actually configure an IBKR slave --
  `pip install ib_insync` on the hub when you're ready. Per the project's
  design decision, Copy Engines only ever talk through the `BrokerAdapter`
  interface, so swapping Schwab -> IBKR means adding a new slave with
  `broker_type: "ibkr"` (host/port/client_id instead of Schwab's OAuth +
  account IDs) -- no engine code changes. See the docstring in
  `ibkr.py` for one known gap (futures contract month resolution) to
  resolve before trading real size with it.
- `deploy/` -- VPS deployment artifacts: `provision.sh` (Ubuntu setup:
  Python 3.11, Node, nginx, systemd), `tradecopier-hub.service` (systemd
  unit, auto-restart on crash, starts on boot), `nginx.conf` (reverse
  proxy: serves the built dashboard, proxies the API + WebSocket to the
  hub), `ufw-rules.sh` (firewall: SSH, 80/443, and the ZMQ ports
  IP-restricted to wherever MT4 actually runs). None of this was run
  against a real VPS in this environment -- there wasn't one available --
  see "Deployment" below for what was and wasn't verified.

## Architecture (Phase 5 additions: hardening)

- `app/retry.py` -- `retry_async()`, generic exponential-backoff retry.
  Wrapped around `MT4Adapter`/`SchwabAdapter`'s request methods: MT4 retries
  a timed-out command after rebuilding its REQ socket; Schwab retries
  connection errors and 5xx. Neither retries a hard rejection (bad order,
  expired/missing OAuth token, 4xx) -- retrying those just wastes time
  before failing anyway.
- `app/watchdog.py` -- `SlaveWatchdog` (per slave: polls `get_status()` for
  connectivity, and where supported, `get_account_summary()` for a simple
  peak-equity drawdown alert) and `MasterWatchdog` (polls
  `ZmqReceiver.last_heartbeat` freshness per master account). Both emit
  connect/disconnect events and fire notifications on every transition.
  The hub's own sockets are bound/server-side, so there's nothing for the
  hub itself to "reconnect" -- this is the detect-and-alert half; the
  client side (MT4 EA, Schwab HTTP client) reconnects on its own.
- `app/reconciliation.py` -- on every engine start (hub startup or a slave
  added live), compares `slave_trades` OPEN rows against the broker's
  `get_open_positions()` (skipped gracefully for MT4, which doesn't support
  it). If the registry says OPEN but the broker doesn't have it, the
  registry is corrected to CLOSED. If the broker has a position the
  registry doesn't know about, it's logged and left alone -- can't safely
  assume an untracked position is the copier's to touch.
- `app/notifier.py` -- `Notifier.notify()` delivers to whatever's set in
  Settings -> Notifications: webhook (POST) and/or email (stdlib
  `smtplib`, off the event loop via `asyncio.to_thread`). Fires on trade
  close, drawdown alerts, and slave/master disconnect-reconnect. Delivery
  failures are logged, never raised. Email is silently skipped if
  `TC_SMTP_HOST` isn't set.
- `app/risk_controls.py` -- `is_kill_switch_active()` / `is_within_trading_hours()`,
  backed by the `config` KV store (so they're editable live from Settings ->
  Risk, no restart). The kill switch is checked in `SignalBus` before
  fan-out -- it blocks everything, but signals are still persisted for
  audit. Trading hours (a UTC HH:MM window, handles midnight wraparound) is
  checked per-engine and only gates new OPENs; MODIFY/CLOSE always go
  through so existing positions can always be managed.
- `app/logging_config.py` -- a `SQLiteLogHandler` persists `app.*` log
  records (third-party noise like httpx's per-request lines is filtered
  out) into a new `logs` table. `GET /logs` now reads from there instead of
  tailing the raw file, with `level`/`lines` filters.

### Schema change from Phase 4

New `logs` table (id, ts, level, logger, message). No migration -- delete
`hub/data/tradecopier.db` if you have one from before Phase 5.

## Architecture (Phase 4 additions: API + dashboard)

- `app/api_routes.py` -- `GET/POST /slaves`, `PATCH /slaves/{id}` (pause/
  resume, live config updates for autonomous engines), `DELETE /slaves/{id}`,
  `GET /trades` (joined with per-slave copy status), `GET/PATCH /config`
  (generic KV store), `GET /status` (uptime, master heartbeats, per-slave
  summary with best-effort live positions/P&L), `GET /logs` (tails
  `hub.log`).
- `app/engine_manager.py` -- `start_engine_for_slave()` / `stop_engine()`,
  shared by hub startup and `POST/DELETE /slaves` so a slave added or
  removed through the UI takes effect immediately, no hub restart.
- `app/events.py` -- `EventBus`, a small asyncio pub-sub. `SignalBus` emits
  a `signal` event on every validated master signal; `CopyEngine` /
  `AutonomousCopyEngine` emit `slave_open` / `slave_modify` / `slave_close`
  / `slave_error` as they act on it.
- `app/ws_routes.py` -- `GET /ws/events`, a WebSocket that subscribes to
  the EventBus and forwards every event to the connected client. This is
  what the dashboard's live activity feed consumes.
- Live pause/resume -- `slaves.paused` (DB) + `CopyEngine.paused` /
  `AutonomousCopyEngine.paused` (in-memory): a paused engine still runs and
  keeps its adapter connected, it just drops signals off its queue instead
  of acting on them. Autonomous mode's breakeven/trailing monitor loop
  keeps managing already-open positions regardless of pause state.
- `app/adapters/base.py` gained a 10th method, `get_open_positions()`
  (+ a `Position` dataclass), for the dashboard's live P&L. Implemented in
  `SchwabAdapter`; `MT4Adapter` raises `NotImplementedError` (mirror-mode
  slave cards show position count without live P&L).
- `/ui` -- Vite + React (see `ui/README.md`).

### Schema change from Phase 3

`slaves` gained a `paused` column. No migration -- delete
`hub/data/tradecopier.db` if you have one from before Phase 4.

## Architecture (Phase 2: MT4 mirror mode)

- `app/adapters/base.py` -- `BrokerAdapter` ABC + shared error types
  (`AdapterError`, `AdapterConnectionError`, `AdapterTimeoutError`,
  `AdapterRejectedError`). Copy Engines only ever talk to this interface, so
  swapping brokers (e.g. Schwab -> IBKR in a later phase) is a config
  change, not a Copy Engine change. Grew from 6 methods in Phase 2 to 9 in
  Phase 3 (autonomous mode also needs broker market data + account
  balances): `connect`, `disconnect`, `open`, `modify`, `close`,
  `get_status`, `get_account_summary`, `get_price_history`, `get_quote`.
- `app/adapters/mt4.py` -- `MT4Adapter`: one ZMQ REQ socket per slave,
  sends `{"cmd": "OPEN"|"MODIFY"|"CLOSE"|"PING", ...}` and waits for a JSON
  ACK. REQ/REP allows only one request in flight; a timeout rebuilds the
  socket since a REQ socket that never got its reply is stuck. Doesn't
  implement the Phase 3 market-data/account methods (mirror mode never
  calls them).
- `app/copy_engine.py` -- `CopyEngine`: one per slave, mirror mode. Copies
  OPEN with the master's absolute SL/TP, and follows MODIFY/CLOSE via the
  master_ticket -> slave_ticket mapping.
- `app/trade_registry.py` + the `slave_trades` table -- the
  master_ticket -> (slave_symbol, slave_ticket) mapping, written on
  open/close. One master_ticket can map to *several* slave symbols (see
  Phase 3 below), so lookups return a list.
- `app/slaves.py` + `config/slaves.json` -- slaves are seeded from this
  JSON file into the `slaves` table on first run (an empty database only);
  after that, add/edit/remove slaves through the dashboard's connection
  wizard or the `/slaves` API instead of editing this file.

## Architecture (Phase 3 additions: Schwab + autonomous mode)

- `app/schwab_auth.py` -- `SchwabAuth`: Schwab's OAuth2 is
  authorization-code, not client-credentials. A one-time manual step (visit
  `GET /schwab/authorize?slave_id=...`, log in, then hit
  `GET /schwab/callback?slave_id=...&code=...` with the code Schwab's
  redirect gives you) gets the first refresh_token; after that the
  ~30-minute access_token auto-refreshes. The refresh_token itself expires
  after 7 days and needs that manual step repeated -- Schwab has no fully
  unattended flow. The refresh_token is persisted in the `config` table.
- `app/adapters/schwab.py` -- `SchwabAdapter`: places orders as a
  TRIGGER-entry + OCO(STOP,LIMIT) bracket, routes each order to
  `futures_account_id` or `brokerage_account_id` depending on whether the
  symbol has a futures contract spec, and reads back the bracket's child
  order IDs (needed to cancel them individually when the stop trails).
  Structurally follows Schwab's documented (ex-TD Ameritrade) API shape but
  has not been exercised against the live API -- verify against a real
  sandbox response before trading real size.
- `app/symbol_mapper.py` -- per-slave `symbol_map` config; one master
  symbol can fan out to several slave targets (`"XAUUSD": ["MGC", "GLD"]`).
- `app/contract_specs.py` -- tick size/value, point value, min size for the
  micro-futures preset (MGC, MES, MNQ, MYM, M2K, MCL, the project's
  default). Symbols not in this table are treated as equities/ETFs.
- `app/sizing.py` -- `compute_size()`: `fixed_contracts`, `lot_multiplier`,
  `pct_risk` (capital base: `balance` / `equity` / `balance_plus_fixed` /
  `fixed_amount`), `dollar_notional`. Always floors to a whole unit and
  skips the trade (never places a <1-unit order) if that floors to zero.
  Sizing can be shared across a slave's targets or overridden per symbol.
- `app/atr.py` -- `compute_atr()`: a plain moving average of true range
  (not Wilder's smoothed version -- simpler, close enough for sizing/risk).
- `app/autonomous_copy_engine.py` -- `AutonomousCopyEngine`: on OPEN,
  back-calculates the ATR multiple the master's own SL/TP represent from
  `signal.atr` (see the master EA note below -- "Option 1" back-calculation
  until fxDreema can publish its own ATR multiple directly), reapplies that
  multiple to each mapped slave symbol's own ATR, sizes via `sizing.py`
  using the adapter's own account balance/equity, then opens. **Ignores
  MODIFY** -- a background loop manages its own breakeven + trailing stop
  per position instead. Still follows CLOSE (all mapped legs close when the
  master closes).

### Schema change from Phase 2

`slave_trades` gained a `slave_symbol` column and the unique constraint is
now `(slave_id, master_ticket, slave_symbol)` instead of
`(slave_id, master_ticket)`, since one master_ticket can now map to several
slave symbols. There's no migration -- delete `hub/data/tradecopier.db` if
you have one from before Phase 3.

## Setup

```
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
cp .env.example .env          # then fill in whatever secrets you're using
```

Secrets (Schwab OAuth client ID/secret, SMTP credentials) only ever come
from `.env` / environment variables -- never hardcoded, never committed.
`.gitignore` excludes `.env` (but not `.env.example`, which has no real
values). See `.env.example` for the full list; everything in it is
optional with sane defaults.

## Run

```
python run.py
```

Don't launch with the bare `uvicorn app.main:app` CLI on Windows -- pyzmq's
asyncio integration requires `add_reader()`/`add_writer()`, which Windows'
`ProactorEventLoop` doesn't support (ZMQ `recv()` just hangs forever,
silently, with no error, while the rest of the app looks fine). Setting the
asyncio event loop *policy* isn't enough either: uvicorn's own loop factory
(`uvicorn/loops/asyncio.py`) hardcodes `ProactorEventLoop` on win32 whenever
it isn't running in subprocess/reload mode, regardless of the active policy.
`run.py` sidesteps this by building a `SelectorEventLoop` itself and driving
`uvicorn.Server.serve()` on it directly. On Linux/macOS this isn't an issue
either way.

- `GET /health` -- basic liveness check
- SUB socket binds on `tcp://*:5555` (trade signals)
- PULL socket binds on `tcp://*:5557` (heartbeats)

SQLite DB is created at `hub/data/tradecopier.db` on startup (`trades`,
`slaves`, `config`, `slave_trades` tables). Logs go to stdout and
`hub/logs/hub.log`. On first run, `config/slaves.json` is seeded into the
`slaves` table if it's empty -- edit that file (or the DB row's
`config_json`) to point `mt4-demo-1` at your real slave EA's host/port, and
`schwab-demo-1` at your real Schwab account IDs (it defaults to
`mt4-demo-1: enabled=false` and `schwab-demo-1: enabled=true`, pointed at
`scripts/simulate_schwab.py`, so the demo config runs against test doubles
out of the box).

Run the pure-math unit tests (ATR, contract specs, symbol mapping, sizing,
retry backoff, kill switch/trading hours) with:

```
python -m pytest tests/ -v
```

Phase 5 hardening settings, all optional (`.env` or `TC_*` env vars, see
`app/config.py` for defaults):

| Setting | Purpose |
|---|---|
| `TC_ADAPTER_MAX_RETRIES` / `TC_ADAPTER_RETRY_BASE_DELAY` | Per-adapter retry count/backoff base (seconds) |
| `TC_WATCHDOG_POLL_INTERVAL_SECONDS` | How often watchdogs check connectivity/drawdown |
| `TC_WATCHDOG_STALE_HEARTBEAT_SECONDS` | How long since the last master heartbeat before it's "disconnected" |
| `TC_DRAWDOWN_ALERT_PCT` | Peak-equity drawdown fraction (0.05 = 5%) that triggers a `drawdown_alert` |
| `TC_SMTP_HOST` / `_PORT` / `_USER` / `_PASSWORD` / `_FROM` | Email delivery for notifications -- unset means email is silently skipped, only the webhook fires |

Notification targets themselves (`notify_email`, `notify_webhook_url`) and
risk controls (kill switch, trading hours) are runtime config, set via the
Settings UI or `PATCH /config` / `PATCH /risk` -- no restart needed.

## Running the dashboard (Phase 4)

The UI is a separate Vite dev server that proxies API + WebSocket calls to
this hub. With the hub running on its default port 8000:

```
cd ../ui
npm install   # first time only
npm run dev
```

Then open the URL Vite prints (typically `http://localhost:5173`). If the
hub isn't on port 8000, override the proxy target: set `VITE_HUB_TARGET`
(e.g. `VITE_HUB_TARGET=http://127.0.0.1:8010 npm run dev`) -- see
`ui/vite.config.js`. See `ui/README.md` for the dashboard/settings/wizard
walkthrough.

## Deployment (Phase 6): running on a VPS, always-on

Everything under `deploy/` targets Ubuntu 22.04/24.04. **None of it has
been run against a real VPS** -- there wasn't one available while building
this -- it's written to documented apt/systemd/nginx/ufw conventions and
reviewed carefully (`bash -n` syntax-checked; no `nginx -t` or
`systemd-analyze verify` since neither nginx nor systemd exist on this dev
machine). Treat the first real run as a first real run.

1. **Provision**: `git clone`/`rsync` this repo onto the VPS, then from
   inside it run `bash deploy/provision.sh`. Installs Python 3.11 (via the
   deadsnakes PPA if your Ubuntu's default isn't 3.11), Node LTS, nginx;
   creates a dedicated `tradecopier` service user; syncs the repo to
   `/opt/tradecopier`; sets up the hub's venv; builds the dashboard;
   installs the systemd unit and nginx site. Idempotent -- re-run after a
   pull to redeploy.
2. **Secrets**: the script seeds `/opt/tradecopier/hub/.env` from
   `.env.example` if one doesn't already exist. Edit it (Schwab OAuth,
   SMTP) before starting the service -- see "Setup" above.
3. **Firewall**: `bash deploy/ufw-rules.sh <your-MT4-machine's-public-IP>`.
   Opens SSH (rate-limited), 80/443, and the ZMQ ports 5555/5557 --
   restricted to that one IP, not the world. ZMQ has no built-in auth, so
   this allowlisting *is* the access control; see the script's header
   comment for the VPN alternative if that IP isn't static.
4. **systemd**: `deploy/tradecopier-hub.service` runs `run.py` as the
   `tradecopier` user, restarts on crash (`Restart=on-failure`, backed off
   so a persistently broken deploy doesn't spin), and starts on boot
   (`WantedBy=multi-user.target`). `sudo systemctl start tradecopier-hub`,
   then `sudo systemctl status tradecopier-hub` / `journalctl -u
   tradecopier-hub -f` to check it.
5. **nginx**: `deploy/nginx.conf` serves the built dashboard as static
   files and reverse-proxies the API + WebSocket to the hub on
   `127.0.0.1:8000`. Set up HTTPS afterward with `sudo certbot --nginx -d
   your-domain` once you have a domain pointed at the VPS -- needed for
   Schwab's OAuth redirect_uri and for `wss://` to work from a public
   dashboard URL.
6. **MT4 -> VPS**: point the master EA's `HubHost` input at the VPS's
   public IP (see `bridge_ea/README.md`). Slave-side MT4 terminals don't
   change -- the hub connects *out* to them, so a slave terminal needs a
   reachable address, not the other way around.
7. **IBKR migration**: when you're ready to move a slave off Schwab,
   `pip install ib_insync` in the hub's venv, then add a slave with
   `broker_type: "ibkr"` (host/port/client_id pointing at a running
   TWS/Gateway) through the dashboard wizard or `POST /slaves` -- no code
   or Copy Engine changes. See `app/adapters/ibkr.py`'s docstring for one
   known gap (futures contract-month resolution) to close before trading
   real size with it.

## Verifying the Phase 1 exit criterion (signal path only, no slave)

`scripts/simulate_ea.py` stands in for the master bridge EA -- it connects a
PUB socket to the hub's SUB port and a PUSH socket to the hub's PULL port,
then publishes an OPEN -> MODIFY -> CLOSE sequence for one ticket plus a
heartbeat (mirroring `bridge_ea/TradeCopierBridge.mq4`).

```
# terminal 1
python run.py

# terminal 2
python scripts/simulate_ea.py
```

Expect log lines in terminal 1 like:

```
signal received: ticket=123456 symbol=XAUUSD action=OPEN direction=BUY lots=0.1 price=2385.5 sl=2375.0 tp=2405.0
signal received: ticket=123456 symbol=XAUUSD action=MODIFY direction=BUY lots=0.1 price=2385.5 sl=2380.0 tp=2405.0
signal received: ticket=123456 symbol=XAUUSD action=CLOSE direction=BUY lots=0.1 price=2385.5 sl=2380.0 tp=2405.0
```

## Verifying the Phase 2 exit criterion without real MT4 terminals

`scripts/simulate_slave.py` stands in for `bridge_ea/SlaveBridge.mq4` -- it
binds a REP socket on the port configured for `mt4-demo-1` in
`config/slaves.json` (`5560` by default) and answers OPEN/MODIFY/CLOSE/PING
with an in-memory ticket book, so you can exercise the full mirror-mode path
with no MT4 terminal running at all. `mt4-demo-1` defaults to
`"enabled": false` since Phase 3 -- flip it to `true` (and disable
`schwab-demo-1` if you don't want both running at once) before this
walkthrough.

```
# terminal 1: fake slave EA
python scripts/simulate_slave.py

# terminal 2: hub (connects MT4Adapter to the fake slave, starts the Copy Engine)
python run.py

# terminal 3: fake master EA (OPEN -> MODIFY -> CLOSE)
python scripts/simulate_ea.py
```

Expect terminal 2 to log the full round trip:

```
signal received: ticket=123456 ... action=OPEN ...
slave=mt4-demo-1 opened master_ticket=123456 as slave_ticket=900001
signal received: ticket=123456 ... action=MODIFY ...
slave=mt4-demo-1 modified slave_ticket=900001
signal received: ticket=123456 ... action=CLOSE ...
slave=mt4-demo-1 closed slave_ticket=900001
```

Check the `slave_trades` table for the master_ticket -> slave_ticket mapping,
which should show `status='CLOSED'` with both `opened_at` and `closed_at`
set once the sequence finishes:

```python
import sqlite3
conn = sqlite3.connect("data/tradecopier.db")
conn.row_factory = sqlite3.Row
for row in conn.execute("SELECT * FROM slave_trades"):
    print(dict(row))
```

Run `scripts/simulate_ea.py` twice in a row -- since each run uses the same
hardcoded `ticket=123456`, the dedup key is `ticket:ts`, and `ts` is
regenerated every run, so both runs deliver; to see dedup fire, replay the
exact same JSON payload twice (e.g. via a raw ZMQ REPL) and confirm no
second row appears in the `trades` table.

## Verifying the Phase 3 exit criterion without a real Schwab account

`scripts/simulate_schwab.py` is a fake Schwab REST server implementing just
enough of the OAuth + trading + market-data surface to exercise the whole
autonomous pipeline: token exchange/refresh, order place/replace/cancel
(including reading back bracket child order IDs), account balances, price
history, and quotes. `config/slaves.json`'s `schwab-demo-1` already points
at it (`http://127.0.0.1:8801`).

```
# terminal 1: fake Schwab
python scripts/simulate_schwab.py

# terminal 2: hub
python run.py
```

Complete the one-time OAuth exchange (the fake server doesn't validate the
code, so any value works):

```
curl "http://127.0.0.1:8000/schwab/callback?slave_id=schwab-demo-1&code=anything"
```

Then publish a signal:

```
# terminal 3
python scripts/simulate_ea.py
```

Expect terminal 2 to log both legs opening with independently-computed
ATR-based SL/TP, the MODIFY being ignored, and both legs closing together:

```
slave=schwab-demo-1 opened MGC master_ticket=123456 -> slave_ticket=700004 size=1.0 sl=2369.79 tp=2413.25
slave=schwab-demo-1 opened GLD master_ticket=123456 -> slave_ticket=700008 size=20.0 sl=236.98 tp=241.33
slave=schwab-demo-1 closed GLD slave_ticket=700008 (master closed)
slave=schwab-demo-1 closed MGC slave_ticket=700004 (master closed)
```

`SELECT * FROM slave_trades` should show two rows for `master_ticket=123456`
-- one per mapped symbol -- both `status='CLOSED'`. Note there's no log line
for the MODIFY at all: autonomous mode owns its own risk management once a
position is open, so MODIFY signals are silently ignored by design (see
`app/autonomous_copy_engine.py`).

The breakeven + trailing monitor loop (`risk_management.breakeven_trigger_atr`
/ `trailing_atr` / `poll_interval_seconds` in a slave's config) only moves
the stop when price actually moves, which the fake server's static quotes
don't do -- it's exercised with a scripted rallying price in an isolated
test rather than through the full simulate_schwab.py flow.

## Verifying the Phase 5 exit criterion (disconnect/reconnect + alerts)

`scripts/simulate_webhook.py` is a fake webhook receiver -- logs every POST
body it gets and exposes them at `GET /received`, so notification delivery
can be checked without a real email/webhook target.

```
# terminal 1: fake MT4 slave
python scripts/simulate_slave.py

# terminal 2: fake webhook receiver
python scripts/simulate_webhook.py

# terminal 3: hub -- shorter intervals make the demo faster to watch
$env:TC_WATCHDOG_POLL_INTERVAL_SECONDS=3; $env:TC_WATCHDOG_STALE_HEARTBEAT_SECONDS=8; python run.py
```

Point notifications at the fake receiver, then publish a baseline signal:

```
curl -X PATCH http://127.0.0.1:8000/config -H "Content-Type: application/json" -d "{\"notify_webhook_url\": \"http://127.0.0.1:8901/webhook\"}"
python scripts/simulate_ea.py
curl http://127.0.0.1:8901/received   # should show a trade_close event
```

Now kill terminal 1 (the fake slave) and publish another signal -- expect
the hub to retry (`retry 1/2`, `retry 2/2` in its log) and then fail that
one signal gracefully rather than crash, followed within a few poll
intervals by a `slave_disconnected` notification. Restart
`simulate_slave.py`; within a few more poll intervals expect
`slave_reconnected`, and a subsequent `simulate_ea.py` run should copy
successfully again -- **no hub restart involved anywhere in this
sequence**, which is the actual exit criterion.

Kill switch and trading hours:

```
curl -X PATCH http://127.0.0.1:8000/risk -H "Content-Type: application/json" -d "{\"kill_switch_enabled\": true}"
python scripts/simulate_ea.py   # hub logs "kill switch active, not fanning out" -- signal persisted, never copied
curl -X PATCH http://127.0.0.1:8000/risk -H "Content-Type: application/json" -d "{\"kill_switch_enabled\": false}"

curl -X PATCH http://127.0.0.1:8000/risk -H "Content-Type: application/json" -d "{\"trading_hours_enabled\": true, \"trading_hours_start\": \"01:00\", \"trading_hours_end\": \"02:00\"}"
python scripts/simulate_ea.py   # hub logs "skipping OPEN ... outside configured trading hours" -- MODIFY/CLOSE for an already-open ticket still go through
```

Reconciliation (needs a broker that supports `get_open_positions()` --
Schwab; MT4 skips reconciliation gracefully and logs why): insert a stale
OPEN row that the broker won't confirm, then restart the hub --

```python
from app.db import db_cursor
import time
with db_cursor() as cur:
    cur.execute(
        "INSERT INTO slave_trades (slave_id, master_ticket, slave_symbol, slave_ticket, status, opened_at) VALUES (?, ?, ?, ?, 'OPEN', ?)",
        ("schwab-demo-1", 999999, "MGC", 800001, time.time()),
    )
```

Expect the hub's startup log to show `... was OPEN in the registry but the
broker doesn't have it open -- marked CLOSED`, and the row's `status` to
now read `CLOSED` in the DB. `scripts/simulate_schwab.py` also exposes
`POST /test/positions/{account_id}` to declare a position the broker
*does* have open but the registry doesn't know about -- expect a `left
as-is` warning instead of any correction.

Finally, `GET /logs` should reflect the `logs` table (not the raw file) --
confirm with `python -m pytest tests/` (retry + trading-hours/kill-switch
logic) or by querying `app.db.list_logs()` directly.
