# Global Trade Copier — Claude Code Briefing

Paste the relevant phase section into a Claude Code session to brief it before starting work.

---

## Project context

Self-hosted trade copier. MT4 EAs publish trade signals over ZeroMQ; a Python hub receives them and copies to broker slave accounts (Schwab initially, IBKR later). Full design completed in Cowork — build follows the phases below.

**Stack:** Python 3.11 · FastAPI · asyncio · pyzmq · SQLite · React + Vite  
**Working directory:** `C:\Users\c_mie\OneDrive\Documents\Claude\Projects\Global Trade Copier`  
**Repo structure:** `/hub` (Python) · `/ui` (React) · `/bridge_ea` (MQL4)

**To run locally:**
- Hub: `cd hub && python -u run.py` (use `-u` flag — output is buffered otherwise)
- UI: `cd ui && npm run dev` → `http://localhost:5173`
- EA simulator: `cd hub && python scripts/simulate_ea.py`

---

## Phase 1 — Project scaffold + ZMQ foundation

**Goal:** Get signals flowing end-to-end with no broker execution.

Tasks:
1. Repo structure: `/hub`, `/ui`, `/bridge_ea` folders + `requirements.txt`
2. FastAPI app skeleton with SQLite init
3. ZMQ Receiver — asyncio task, SUB socket :5555, PULL socket :5557 (heartbeat)
4. Signal Bus — parses/validates JSON, deduplicates by ticket+ts, fan-out to per-slave `asyncio.Queue()`
5. Trade Registry — SQLite schema: `trades`, `slaves`, `config` tables
6. Bridge EA stub — MQL4 that polls `OrdersTotal()`, diffs state, publishes a hardcoded test JSON message over ZMQ PUB :5555

**Exit criterion:** Bridge EA publishes a signal, Python hub receives and logs it.

---

## Phase 2 — MT4 slave adapter + mirror mode

**Goal:** First working copy — MT4 master → MT4 slave.

Tasks:
1. `BrokerAdapter` ABC — 6-method abstract base class + shared error types
2. `MT4Adapter` — ZMQ REQ/REP, sends JSON commands, waits for ACK
3. Copy Engine (mirror mode) — copies absolute SL/TP, follows MODIFY + CLOSE signals
4. Trade Registry writes — master_ticket → slave_ticket mapping on open/close
5. Slave-side bridge EA — REP socket receives commands, executes OrderSend/Modify/Close

**Exit criterion:** Trade opens on master MT4, modifies SL, closes — all reflected on slave MT4.

---

## Phase 3 — Schwab adapter + autonomous mode

**Goal:** Cross-asset copying — MT4 master → Schwab futures + ETFs.

Tasks:
1. Schwab OAuth2 flow — client credentials, token refresh, account ID routing (futures + brokerage are separate account IDs)
2. `SchwabAdapter` — REST API: place, modify, close orders; get positions
3. Symbol mapper — per-slave config: XAUUSD→MGC, US30→MYM, XAUUSD→GLD etc.
4. Copy Engine (autonomous mode) — receives ATR params, computes slave SL/TP via slave instrument ATR; own BE + trailing logic; ignores MODIFY signals
5. Sizing engine — fixed contracts, lot multiplier, % risk (with capital base), $ notional
6. Contract spec per futures symbol — tick size, tick value, point value, min size; skip trade if computed size < 1

**Capital base options (% risk mode, per slave):**
- `balance` — cash balance, excludes floating P&L
- `equity` — balance + open P&L
- `balance_plus_fixed` — balance + user-defined offset
- `fixed_amount` — always uses this $ amount as base

**Exit criterion:** XAUUSD BUY on master → MGC and GLD open on Schwab with autonomous SL/TP.

---

## Phase 4 — Dashboard + UI

**Goal:** React frontend wired to live hub data.

Tasks:
1. FastAPI endpoints — `/slaves`, `/trades`, `/config`, `/status`
2. WebSocket feed — pushes live OPEN/MODIFY/CLOSE events to UI
3. React scaffold — Vite + React, routes: dashboard / settings
4. Dashboard view — master panel, slave cards with live P&L, activity feed, per-slave pause toggle
5. Connection wizard — 5-step add-slave flow (broker → credentials → copy mode + symbol map → sizing → review)
6. Settings panel — General, Master (ZMQ + ATR config table), Slaves, Risk, Notifications, Logs, Export

**Exit criterion:** Dashboard shows live trades, pause toggle works, new slave can be added through UI.

---

## Phase 5 — Hardening

**Goal:** Survive real-world conditions.

Tasks:
1. Retry + reconnect logic — per-adapter retries, ZMQ auto-reconnect on heartbeat loss
2. Startup reconciliation — on restart, compare Trade Registry against live positions, resolve gaps
3. Notifications — email + webhook on trade close, drawdown alert, disconnect
4. Structured logging — log to file + DB, surfaced in Settings → Logs
5. Risk controls — global kill switch, trading hours filter (no per-slave max trades or daily DD)

**Exit criterion:** Hub survives a broker disconnect and reconnects cleanly; all alerts fire correctly.

---

## Phase 6 — Deployment

**Goal:** Running on VPS, always-on.

Tasks:
1. VPS provisioning — Ubuntu, Python 3.11, Node, nginx reverse proxy for UI
2. systemd service — hub runs on boot, auto-restarts on crash
3. Config file — secrets (API keys, OAuth tokens) in `.env`, never in code
4. MT4 → VPS ZMQ — firewall rules, ZMQ over TCP from local MT4 to remote hub
5. IBKR slot — dormant adapter wired in, config ready for migration from Schwab

**Exit criterion:** Hub running on VPS, MT4 on local machine publishing signals to it reliably.

---

## Known issues / cleanup backlog

- Settings → Risk tab renders raw JSON instead of a proper UI — needs a form-based view built out

---

## Key design decisions (do not change without reviewing in Cowork)

- No per-slave max open trades or max daily drawdown
- Capital base is a per-slave setting, not global
- ATR back-calculation (Option 1) until fxDreema EAs are updated to use `GlobalVariableSet()`
- Micro contracts are the default futures preset (MGC, MES, MNQ, MYM, M2K, MCL)
- If computed futures position size < 1 contract, log and skip — never place a 0-lot order
- Swapping Schwab → IBKR = config change only; Copy Engine does not change
- IBKR adapter slot exists from Phase 3 onwards, dormant until configured
