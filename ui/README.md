# Trade Copier Dashboard (Phase 6)

Vite + React frontend for the hub. Talks to the FastAPI backend in `/hub`
over REST + a WebSocket, never anything else -- there's no separate state
store; the dashboard is a thin, mostly-live view over `/status`, `/trades`,
and `/ws/events`.

## Setup

```
npm install
npm run dev
```

Open the URL Vite prints (default `http://localhost:5173`). The dev server
proxies `/slaves`, `/trades`, `/config`, `/status`, `/logs`, `/schwab`,
`/health`, and `/ws` straight through to the hub on `http://127.0.0.1:8000`
(see `vite.config.js`) -- if the hub is on a different port, run with
`VITE_HUB_TARGET=http://127.0.0.1:<port> npm run dev`.

## Structure

- `src/api.js` -- thin fetch wrapper for the REST endpoints.
- `src/useEvents.js` -- subscribes to `/ws/events`, keeps the last N events
  in state, auto-reconnects on disconnect.
- `src/pages/Dashboard.jsx` -- master panel (uptime, ZMQ ports, master
  heartbeats, live-feed connection status), slave cards (pause/resume,
  remove, live positions/P&L where the adapter supports it), and the
  activity feed (live WebSocket events merged with `/trades` history).
- `src/components/ConnectionWizard.jsx` -- the 5-step add-slave flow
  (broker -> credentials -> copy mode + symbol map -> sizing -> review),
  posts to `POST /slaves` on completion. The new slave starts copying
  immediately -- no hub restart needed (see `hub/app/engine_manager.py`).
  Broker choices are MT4, Schwab, and (Phase 6) Interactive Brokers --
  picking IBKR asks for the TWS/Gateway host/port/client ID instead of
  Schwab's OAuth account IDs, and says plainly that the slave will be
  created but can't connect until `ib_insync` is installed on the hub and
  a real TWS/Gateway is running.
- `src/pages/Settings.jsx` -- General (free-form KV config), Master (ZMQ
  ports + per-slave ATR periods), Slaves (list/pause/remove/add), Risk
  (kill switch toggle + trading hours window, wired to `GET/PATCH /risk`;
  plus a read-only view of each autonomous slave's sizing + breakeven/
  trailing config), Notifications (email/webhook targets -- delivery is
  real as of Phase 5), Logs (now backed by the `logs` DB table via
  `GET /logs`, not a raw file tail), Export (client-side CSV export of
  loaded trades).

## What "live" actually means here

- Slave cards' pause toggle calls `PATCH /slaves/{id}` and the engine
  reacts immediately (paused engines keep running, just drop signals).
- The activity feed is genuinely push-based via `/ws/events` -- opening a
  master trade shows up without a page refresh. A 5s poll of `/status` +
  `/trades` covers anything the socket might have missed (e.g. a page that
  was open before the hub restarted).
- Live P&L on slave cards depends on the broker adapter implementing
  `get_open_positions()`. Schwab does; MT4 mirror slaves show open
  position count only, with a note why P&L isn't available -- MT4 doesn't
  report live P&L back over the existing ZMQ command protocol.

## Verified

**Phase 4 pass** -- driving an actual Chromium browser through the rendered
app: dashboard renders live (heartbeat badge + activity feed update off
`/ws/events` with no page refresh), Pause/Resume on a live slave card
actually stops/resumes copying, the full 5-step wizard was clicked through
end to end and the newly created slave copied a live signal immediately
with no hub restart, and the General/Slaves settings tabs render and
function correctly.

**Phase 5 pass** -- the new Risk tab controls (kill switch toggle, trading
hours form) and the DB-backed Logs tab were verified against the hub's
`/risk` and `/logs` endpoints directly (curl/PowerShell) and via
`npm run build`, which compiles cleanly with no JSX/import errors. No
browser automation tool was reachable in this session to click through the
new Risk tab specifically -- it follows the exact same component pattern
(fetch-on-mount, controlled inputs, save-on-change) as the General/
Notifications tabs that *were* browser-verified in Phase 4, so risk here is
low, but it hasn't been re-confirmed rendering pixel-for-pixel. Worth a
quick manual click-through before relying on it.

**Phase 6 pass** -- the wizard's new IBKR branch (`StepCredentials`,
`StepBroker`'s third option, `buildConfig`'s ibkr case) was reviewed by
reading the full file and cross-checked field-for-field against
`create_adapter()`'s expectations (`host`/`port`/`client_id`) -- consistent
end to end. `npm run build` still compiles cleanly with the addition. Not
clicked through in a browser this pass (no automation tool reachable);
it's new branching logic, not a copy-pasted pattern like the Phase 5 Risk
tab was, so treat it as slightly higher-risk than that one until someone
actually walks through creating an IBKR slave in the UI.

Not covered by any pass: Export tab, MT4 credential validation against a
real MT4 terminal, and the Schwab OAuth flow's UI (the redirect step still
needs a real Schwab developer app).
