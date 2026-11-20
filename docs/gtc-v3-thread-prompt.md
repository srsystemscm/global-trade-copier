# GTC v3 — thread prompt

Paste this whole file into a new Claude Code thread to brief it.

---

## What this is

A self-hosted trade copier. fxDreema EAs on an MT4 **master** publish trade signals
over ZeroMQ; a Python **hub** receives them and copies each signal to broker **slave**
accounts — IBKR futures, IBKR ETFs, and MT4/MT5 CFD accounts — resizing every position
for the slave's own instrument, volatility and capital.

**Stack:** Python 3.11 · FastAPI · asyncio · pyzmq · SQLite · React + Vite
**Layout:** `/hub` (Python) · `/ui` (React) · `/bridge_ea` (MQL4/MQL5) · `/tools` (test scripts)
**Ports:** ZMQ SUB 5555, PULL 5557 · HTTP 8010

Four live slaves, all paper/demo:

| slave | broker | mode | trades |
|---|---|---|---|
| `ibkr-paper-1` | IBKR | autonomous | futures, 10 routes |
| `ibkr-etf-1` | IBKR | autonomous | leveraged ETFs, 20 routes, sizing per route |
| `mt5-icm-1` | IC Markets MT5 | autonomous | CFDs, same symbols |
| `mt5-dwx-futures-1` | Darwinex MT5 | autonomous | paused; account is also traded by hand |

Both IBKR slaves share **one** account (`DUR314653`). That matters more than it sounds —
see "Things that have bitten".

---

## How a signal becomes a position

1. The master EA (`bridge_ea/TradeCopierBridge.mq4`, **1.06**) publishes `{ticket, symbol,
   direction, lots, price, sl, atr, magic, account, risk...}` per OPEN / MODIFY / CLOSE.
2. The hub routes it per slave. A `routes` key means **strict** routing on `(symbol, magic)`;
   no `routes` means the master's own symbols.
3. A route names one target or a **candidate chain** — largest contract first, e.g.
   `["GC", "MGC"]`. The chain falls through on margin, netting or rounding cost.
4. Sizing is a chain of multipliers:
   `what the master staked × slave risk calibration × per-strategy scale × route scale`
5. The slave sets its own stop from **its own** ATR, measured on the timeframe the
   strategy's magic names.

**The magic's leading digit names the ATR timeframe:** `1` daily, `2` H4, `3` H1, `4` weekly.
`121003` is a daily system. Both the master EA and the hub apply this rule and must agree —
they are on opposite sides of the wire and a disagreement is silent.

---

## Where things actually live

Getting these wrong cost hours. They are not guessable.

```
live config + registry   C:/GTC/data/tradecopier.db      (TC_DB_PATH — NOT hub/data/)
logs                     C:/GTC/logs/hub.log             (rotates to .log.1, .log.2)
master EA                %APPDATA%\MetaQuotes\Terminal\7078BF63…\MQL4\Experts\TradeCopier\
test scripts             %APPDATA%\MetaQuotes\Terminal\7078BF63…\MQL4\Scripts\
restart                  scripts/restart-copier.ps1      (self-elevates; needs UAC accepted)
```

The master EA lives in an `Experts\TradeCopier\` **subfolder**. A non-recursive search finds
a stale copy elsewhere and misses it. Always verify the file you deployed is the one being
compiled, and confirm the version afterwards:

```bash
curl -s http://127.0.0.1:8010/status | python -c "import json,sys; print(json.load(sys.stdin)['master']['bridge_version_by_account'])"
```

---

## Verifying things

- **`/health` answering does not mean a restart happened.** A failed stop leaves the old
  process on the port, answering cheerfully. Check `uptime_seconds` is small.
- **`PATCH /slaves/{id}` replaces the config wholesale**, it does not merge. Always read the
  whole config back, modify, and send it all. Omitting a key deletes it.
- **Preflight validates contract specs against IBKR** at engine start:
  `all N route targets resolve, and every multiplier matches its spec`. That line is real
  verification, not decoration.
- **Tests:** `cd hub && .venv/Scripts/python.exe -m pytest -q` — 1,108 pass. The one failure,
  `test_hub_reporter.py::test_the_shipped_config_is_off`, is a pre-existing local edit to
  `hub/config/hub_reporter.json`. Leave it.
- **Driving a test:** `tools/FxdMaeEntryTest_{D,H4,H1}.mq4` place one trade each with magics
  `121003` / `221003` / `321003`, so a run exercises all three ATR timeframes. `CloseAfterMs`
  (default 300000) decides how long the position rests — 5000 for the signal path, 300000 for
  anything that has to rest, 0 to leave it open.

Log lines that answer specific questions:

```
followed MODIFY ... (3.0001 ATR of its own instrument)   should be ~3.0 on EVERY timeframe
chose MGC (atr .., stop .., size ..) over GC -- ...      why the chain stepped down
sized from the broker's own spec: tick .. contract_size  MT sizing using real contract data
UGL had marketPrice=-1; using close=39.16                the quote fallback firing
```

---

## State as of 2026-11-16

Branch `fix/mode-label-says-what-it-does`, 32 commits, PR #22. All verified live.

**Working:** ATR by magic on the closed bar · MT lots sized by broker contract size ·
candidate chains across every asset with full-size parents (`ES`/`NQ`/`RTY`/`YM`/`CL` added) ·
rounding band that steps down on *cost* rather than count · one margin ceiling read by both
engines and editable · one account snapshot per signal · GTD with session-boundary deadlines ·
quote fallback for instruments IBKR will trade but not quote.

**Open, in priority order:**

1. **The engine stalls after an unfilled entry.** An `AutonomousCopyEngine` stops consuming
   its queue permanently, no exception, adapter still connected, `/slaves` still reporting
   healthy. Four occurrences on 2026-11-16, every one immediately after abandoning an entry
   that never filled. Reproducible: run an ETF route after-hours with `CloseAfterMs` at
   default. **This is the most serious thing in the system** — it drops signals silently, in
   exactly the window the ETF routes exist for. Being worked in its own session.
2. **Order splitter.** Large orders repeatedly fail to fill in one go — 33 MGC, 422 MET,
   `230 of 2444` on UCO. N slices of one instrument over a window, collapsing into one
   tracked position.
3. **IBKR overnight routing.** Stocks route `SMART`, never `OVERNIGHT`, so 20:00–04:00 ET is
   unreachable. Needs the account's overnight permissions checked first.
4. **Magic-filtered reconciliation.** On an account also traded by hand, every manual position
   is reported untracked on each restart. The EA already stamps `req.magic` on its own orders;
   the `POSITIONS` reply just does not report `POSITION_MAGIC`.
5. **Leftovers.** `ibkr-paper-1` holds MGC/M2K/MBT/UGL/TQQQ from testing. Real, and nothing in
   the copier will close them.
6. **Margin caps are at `1.0`** on all three for testing. Put them back before anything runs
   unattended.

---

## Things that have bitten, worth not repeating

**Measure before diagnosing.** Three confident wrong causes in one session — "the micro's ATR
runs higher", "the stale close is the blocker", "SQLite contention" — each overturned by one
log line. Add the instrumentation first; it is cheap and it is usually decisive.

**A test built on a fake shaped like your mental model proves nothing.** Nine tests covered
`get_working_orders` and all nine passed while it called the ib_async *module* instead of the
client, because they drove a fake adapter carrying its own copy. The real method body never
ran. When a test and the live system disagree, the live system is right.

**Silence is not success.** The worst bugs this session were all silent: a dropped signal, a
100× position whose reported risk was internally consistent, a cap that saved cleanly and was
never read, a form field that was written and never rendered. If something is configured,
check that something reads it.

**Sizing errors look plausible.** 13 lots is not an absurd number. The dollar risk printed
alongside a wrong position will agree with that wrong position. Cross-check against a second
source — the master's own figure, or the other slave on the same account.
