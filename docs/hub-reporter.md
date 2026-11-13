# Copier → Operations hub reporter, schema 1.0

The contract between this copier and the Operations hub's Copier tab. The
copier's side is built here; the hub's storage, ingest route, API and page are
built in the hub thread against this document. Keep them in step: a field added
on one side only is a field nobody can rely on.

The closest existing model is `ops_hub/docs/payload-v1.md`, the Desk Manager
wire format. The identity / ack / queue semantics here are deliberately the
same, so the hub has one pattern to implement rather than two.

## The hand-over model: push

The copier reports to the hub. The hub never calls the copier. That matches
every other feed the hub has (Desk Manager EAs, VPS heartbeats), it works when
the copier's Windows VPS is not reachable from outside, and the hub keeps a
history of what happened rather than only what is true now.

**Reporting must never slow or block copying.** The sender runs off the signal
path in its own task, with a short timeout, and fails quietly with a logged
warning. A dead or slow hub must not cost one copy. Everything the reporter
needs is written to a local queue by the thing that produced it; the sender is
the only part that talks to the network, and nothing waits for it.

## Transport

```
POST <hub_url>/api/v1/ingest/copier
Authorization: Bearer <key>          # issued by the hub; X-Api-Key also accepted
Content-Type: application/json
```

The exact route is settled with the hub thread; `ingest/copier` is the
proposal, matching `ingest/desk-manager` and `ingest/host-heartbeat`.

**Versioning.** `schema_version` is `"1.0"`. Unknown fields are ignored, so a
minor version may add fields freely; a different major version is rejected with
422. Both sides note a schema change in their CHANGELOG.

**No secrets, ever.** No broker passwords, no IBKR credentials, no API keys of
any kind in the payload. Account numbers are fine and are what identifies an
account. The hub key itself is read from the environment or a file and never
logged.

## Identity

| field | meaning |
|---|---|
| `copier_id` | stable name for this installation, e.g. `"gtc-vps2"`. Configured, not derived, so moving the copier to another host does not look like a new copier. |
| `session_id` | new on every copier start. Lets the hub see a restart. |
| `seq` | increments per report within a session. A gap means a report was lost. |

## Snapshot vs events

**Snapshot** is what is true now. It is sent on every report, is never queued,
and is never acked — a stale snapshot is worthless, so a lost one is simply
replaced by the next.

**Events** are what happened. They are queued to disk, sent until the hub acks
them, and only then dropped. Nothing is lost across a copier restart or a hub
outage, and nothing is double-counted.

### event_id

```
{copier_id}:{session_id}:{n}        # n monotonic within the session
```

Assigned once, when the event is queued, and stored with it — so the id
survives a restart along with the event it belongs to. An event is never
regenerated, so the same occurrence never gets two ids.

**The hub must deduplicate on `event_id`.** A reply that is lost in transit
causes a resend of events the hub already stored, and that has to be harmless.

### Ack semantics

The reply carries `acked_event_ids`. The copier then:

- removes exactly those events from the queue;
- keeps everything else queued, on disk, and sends it again next cycle;
- never retries within a cycle;
- never queues a snapshot.

A reply that is missing, malformed, or non-200 acks nothing. The queue grows
and the next cycle tries again with back-off.

## Cadence

- **At once** when something happens — a signal, a fill, a refusal, a session
  change — debounced by ~1s so one signal's legs travel together.
- **Otherwise every 15s**, snapshot only, so the tab can tell "quiet" from
  "dead". The Desk Manager does the same (ops_hub DECISIONS D41).
- A report carries at most **200 events**, oldest first. More than that and the
  next cycle takes the rest.

## Queue and retry

- The queue is a table in the copier's own SQLite database, not a file: an
  append that is interrupted mid-write must not corrupt the backlog.
- Back-off on failure: 1s, 2s, 4s … to a 60s ceiling, then steady. The snapshot
  rides along with every attempt, so a hub that comes back is current
  immediately.
- **Bounded**, default 10,000 events. At the ceiling the oldest are dropped and
  `queue.dropped_total` counts them in the snapshot, so loss is visible in the
  tab rather than silent. Dropping the oldest is the right way round: recent
  events are the ones being looked at.
- `queue.depth` and `queue.oldest_event_at` are reported too. A depth that
  climbs is the symptom of a hub that is accepting reports but not acking.

## Units and times

- All times are **UTC, ISO-8601, `Z`** (`"2026-11-12T21:10:05Z"`). The copier
  works in UTC throughout; broker server time never leaves the copier.
- Latencies are **integer milliseconds**.
- Sizes are in the slave broker's own unit, with `size_unit` naming it
  (`"lots"`, `"contracts"`, `"shares"`). The copier does not convert; see
  DECISIONS D39 in the hub for why the hub keeps the broker's unit too.
- Money is in the account's currency, with `currency` beside it.

## Snapshot

```jsonc
"snapshot": {
  "copier": {
    "version": "2.1.0",
    "started_at": "2026-11-12T18:29:13Z",
    "uptime_s": 9412,
    "http_port": 8010
  },
  "masters": [
    {
      "account": "11558127",
      "last_signal_at": "2026-11-12T20:05:02Z",
      "last_heartbeat_at": "2026-11-12T21:09:58Z"
    }
  ],
  "slaves": [
    {
      "id": "ibkr-paper-1",
      "name": "IBKR Paper (Autonomous)",
      "broker_type": "ibkr",
      "mode": "autonomous",
      "enabled": true,
      "paused": false,
      "running": true,
      "connected": true,
      "open_legs": 0,
      "account": "DUR314653",
      "session_state": "connected",
      "blocked_targets": {},
      "preflight": {"targets": 12, "resolved": 12, "at": "2026-11-12T20:29:22Z"},
      "last_error": null,
      "last_error_at": null
    }
  ],
  "risk": {
    "kill_switch_enabled": false,
    "balance_adjustments": {"ibkr-paper-1": 0.0}
  },
  "counters": {
    "today": {"received": 7, "copied": 19, "refused": 2, "failed": 0},
    "since_start": {"received": 23, "copied": 61, "refused": 5, "failed": 1}
  },
  "queue": {"depth": 0, "oldest_event_at": null, "dropped_total": 0}
}
```

### Why `masters[]` reports last-seen and not a link state

A ZMQ SUB socket cannot tell a dead publisher from a quiet one: the hub binds
SUB on :5555 and a master that exits looks exactly like a master with nothing
to say. So there is no honest "connected" flag for a master. `last_signal_at`
is the truth, and `last_heartbeat_at` — from the PULL socket on :5557 — is the
only positive liveness signal, and only for masters that send one. A tab that
wants "is the master alive" should threshold the heartbeat age and say which
threshold it used.

`slaves[].connected` **is** honest, because every slave adapter is
request/response: the copier asks and either gets an answer or does not.

## Events

One event **per slave leg**, keyed by `(master_ticket, slave_id)` — the same
key the Trade Registry uses. A fan-out that partly succeeds then reports
exactly what each slave did, and a leg that completes much later (a deferred
open can fill forty minutes after the signal) is a new event rather than an
amendment to an old one. The hub appends; it never has to update an event in
place.

Every event carries:

```jsonc
{
  "event_id": "gtc-vps2:a1b2c3:417",
  "type": "slave_open",
  "at": "2026-11-12T20:05:04Z",       // when it happened, not when it was sent
  "master_account": "11558127",
  "master_ticket": 999031902,
  "magic": 1001,                       // the master EA's magic; null when absent
  "master_symbol": "US2000",
  "slave_id": "ibkr-paper-1",
  "detail": { }                        // per-type, below
}
```

`magic` is first-class deliberately. It is how a signal is attributed to the
strategy that produced it, and the production (V3) master EAs are being put in
place to set it on signals and their copies.

### Event catalogue

`alert` is the recommendation to the hub thread for which events deserve
attention. `notice` means show it, don't alert: it is policy working as
intended. `info` is for the stream and the counters.

| type | class | `detail` |
|---|---|---|
| `signal_received` | info | `action`, `direction`, `lots`, `price`, `sl`, `tp`, `atr`, `targets` (how many slaves it fanned to) |
| `slave_open` | info | `slave_symbol`, `slave_ticket`, `size`, `size_unit`, `sizing` (the human-readable explanation), `requested_price`, `fill_price`, `sl`, `tp`, `legs` (how many broker orders the size became), `latency_ms`, `signal_to_fill_ms` |
| `slave_modify` | info | `slave_ticket`, `sl`, `tp`, `legs_moved` |
| `slave_close` | info | `slave_ticket`, `fill_price`, `size`, `latency_ms` |
| `margin_reduced` | notice | `slave_symbol`, `asked`, `sent`, `margin_required`, `margin_free`, `ceiling_pct`, `risk_fraction_carried` |
| `netting_blocked` | notice | `slave_symbol`, `net_held`, `direction`, `reason` |
| `margin_blocked` | notice | `slave_symbol`, `reason` |
| `route_blocked` | notice | `target`, `reason` |
| `route_unresolved` | notice | `target`, `reason` |
| `signal_unrouted` | notice | `reason` |
| `trading_hours_blocked` | notice | — |
| `duplicate_open_ignored` | notice | `existing_slave_ticket` |
| `kill_switch_blocked` | alert | — |
| `signal_refused` | notice | `reason` |
| `open_deferred` / `close_deferred` | notice | `slave_symbol`, `reason`, `retry_every_s` |
| `deferred_open_filled` / `deferred_close_filled` | info | `slave_ticket`, `fill_price`, `waited_s` |
| `deferred_open_cancelled` | notice | `reason`, `waited_s` |
| `deferred_open_abandoned` / `deferred_close_abandoned` | **alert** | `reason`, `waited_s` — volume may be live and untracked |
| `entry_unfilled` | **alert** | `slave_symbol`, `limit_price`, `waited_s` |
| `slave_error` | **alert** | `action`, `message` |
| `slave_start_failed` | **alert** | `message` |
| `slave_recovery_failed` | **alert** | `message`, `attempts` |
| `slave_disconnected` / `slave_reconnected` / `slave_session_recovered` | info, **never alert** | `message` — IBKR's Gateway restarts daily and this is what it looks like |
| `master_disconnected` / `master_reconnected` | notice | `account`, `silent_for_s` |
| `reconciliation` | alert **only on mismatch** | `checked`, `matched`, `untracked[]`, `stuck[]`, `at` |

### Three sources that do not exist yet

The copier must grow these for the catalogue above to be complete. They are in
the spec because the hub builds against this document, and a field added later
means revisiting both sides.

1. **`reconciliation`** — `app/reconciliation.py` runs but emits no summary, so
   "open legs versus the registry, untracked or stuck rows" has nothing to
   report. Needs an event carrying what it checked and what did not match.
2. **`signal_received`** — the signal bus logs arrivals but emits nothing, so
   received cannot be reconciled against copied. Without it a signal that
   routes nowhere appears only as `signal_unrouted`.
3. **`magic` on copy events** — `Signal` carries it; `_emit` does not pass it
   through. Every copy event needs it.

## A full report

```json
{
  "schema_version": "1.0",
  "copier_id": "gtc-vps2",
  "session_id": "a1b2c3",
  "seq": 418,
  "sent_at": "2026-11-12T20:05:06Z",
  "snapshot": {
    "copier": {"version": "2.1.0", "started_at": "2026-11-12T18:29:13Z",
               "uptime_s": 5753, "http_port": 8010},
    "masters": [{"account": "11558127", "last_signal_at": "2026-11-12T20:05:02Z",
                 "last_heartbeat_at": "2026-11-12T20:05:04Z"}],
    "slaves": [
      {"id": "ibkr-paper-1", "name": "IBKR Paper (Autonomous)", "broker_type": "ibkr",
       "mode": "autonomous", "enabled": true, "paused": false, "running": true,
       "connected": true, "open_legs": 1, "account": "DUR314653",
       "session_state": "connected", "blocked_targets": {},
       "preflight": {"targets": 12, "resolved": 12, "at": "2026-11-12T20:29:22Z"},
       "last_error": null, "last_error_at": null},
      {"id": "mt5-icm-1", "name": "MT5 ICM Mirror", "broker_type": "mt4",
       "mode": "mirror", "enabled": true, "paused": false, "running": true,
       "connected": true, "open_legs": 1, "account": "53116707",
       "session_state": "connected", "blocked_targets": {},
       "preflight": null, "last_error": null, "last_error_at": null}
    ],
    "risk": {"kill_switch_enabled": false, "balance_adjustments": {}},
    "counters": {"today": {"received": 7, "copied": 19, "refused": 2, "failed": 0},
                 "since_start": {"received": 23, "copied": 61, "refused": 5, "failed": 1}},
    "queue": {"depth": 2, "oldest_event_at": "2026-11-12T20:05:03Z", "dropped_total": 0}
  },
  "events": [
    {
      "event_id": "gtc-vps2:a1b2c3:416",
      "type": "signal_received",
      "at": "2026-11-12T20:05:02Z",
      "master_account": "11558127",
      "master_ticket": 999031902,
      "magic": 1001,
      "master_symbol": "US2000",
      "slave_id": null,
      "detail": {"action": "OPEN", "direction": "BUY", "lots": 1.0, "price": 2819.0,
                 "sl": 2684.0, "tp": null, "atr": 45.0, "targets": 3}
    },
    {
      "event_id": "gtc-vps2:a1b2c3:417",
      "type": "slave_open",
      "at": "2026-11-12T20:05:04Z",
      "master_account": "11558127",
      "master_ticket": 999031902,
      "magic": 1001,
      "master_symbol": "US2000",
      "slave_id": "ibkr-paper-1",
      "detail": {
        "slave_symbol": "M2K", "slave_ticket": 392, "size": 24.0,
        "size_unit": "contracts",
        "sizing": "pct_risk on M2K: a 4.790% stop (134.6 away at 2808.8) risks 673.00/lot of 1012480.00 (equity) -> 24 lots",
        "requested_price": 2808.8, "fill_price": 2808.8,
        "sl": 2674.2, "tp": null, "legs": 1,
        "latency_ms": 1180, "signal_to_fill_ms": 2140
      }
    },
    {
      "event_id": "gtc-vps2:a1b2c3:418",
      "type": "margin_reduced",
      "at": "2026-11-12T20:05:05Z",
      "master_account": "11558127",
      "master_ticket": 999031902,
      "magic": 1001,
      "master_symbol": "BTCUSD",
      "slave_id": "mt5-dwx-futures-1",
      "detail": {
        "slave_symbol": "MBT_X", "asked": 429.0, "sent": 91.0,
        "margin_required": 1458600.0, "margin_free": 1250000.0,
        "ceiling_pct": 0.25, "risk_fraction_carried": 0.212
      }
    }
  ]
}
```

### The reply

```json
{"ok": true, "schema_version": "1.0", "server_time_utc": "2026-11-12T20:05:06Z",
 "acked_event_ids": ["gtc-vps2:a1b2c3:416", "gtc-vps2:a1b2c3:417",
                     "gtc-vps2:a1b2c3:418"]}
```

## What the hub needs that is not obvious from the fields

- **Dedupe on `event_id`.** A lost reply means a resend of stored events.
- **Keep a snapshot history**, not just the latest. "The ETF slave was
  disconnected for six minutes this morning" is a question about the past, and
  events alone will not answer it — a slave can be disconnected without
  emitting anything.
- **`slave_disconnected` must not alert.** IBKR's Gateway restarts daily; this
  is the normal shape of that. `slave_recovery_failed` is the one that matters.
- **A `seq` gap means a lost report**, which costs a snapshot but no events —
  worth showing as a data-quality note rather than an alert.
- **Sizes are not comparable across slaves.** 24 M2K contracts and 1.0 US2000
  lots are the same signal; `size_unit` is why the tab must not sum them.

## Configuration

```jsonc
{
  "hub_reporter": {
    "enabled": false,                  // off until a key exists
    "url": "https://srsystems.duckdns.org",
    "copier_id": "gtc-vps2",
    "key_file": "config/hub_key.txt",  // or ODH_COPIER_KEY in the environment
    "snapshot_interval_s": 15,
    "timeout_s": 5,
    "queue_max": 10000,
    "max_events_per_report": 200
  }
}
```

Off by default. A copier with no key configured must behave exactly as one
built before the reporter existed.
