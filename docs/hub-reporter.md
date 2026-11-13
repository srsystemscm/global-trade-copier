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

### Sources that did not exist yet

The copier had to grow these for the catalogue above to be complete. They are
in the spec because the hub builds against this document, and a field added
later means revisiting both sides. **All of these are now built** — see the
addendum at the end of this document for what each one turned out to be.

1. **`reconciliation`** — `app/reconciliation.py` ran but emitted no summary,
   so "open legs versus the registry, untracked or stuck rows" had nothing to
   report. It now emits one event per pass, drift or no drift.
2. **`signal_received`** — the signal bus logged arrivals and emitted nothing
   that said how far they got, so received could not be reconciled against
   copied. The arrival event now carries `targets`.
3. **`magic` on copy events** — `Signal` carries it; `_emit` did not pass it
   through.
4. **`master_account` and `master_symbol` on copy events** — not in the
   original list, and the larger of the two gaps. Every event was emitted with
   `master_ticket=signal.ticket` and nothing else, so three of the four
   identity fields this spec makes mandatory were absent from all of them. An
   MT4 ticket number is per account, so without `master_account` the key at
   the top of every event is ambiguous the moment a second master exists.
5. **A hub-side arrival time** — `signal_to_fill_ms` could only have been
   measured against the master EA's `TimeGMT()`, which is another machine's
   clock at whole-second resolution. `Signal.received_at` is the hub's own.

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

---

# Addendum: what the copier side found

Written while implementing the copier's half (`hub/app/hub_reporter/`). Every
item is something the hub thread needs, either because this document was
incomplete or because a decision it left open has now been taken. Nothing
above has had its **meaning** changed; the transport, identity, ack and queue
semantics are exactly as agreed.

## Decisions this document left open

**`reconciliation` row shapes.** The catalogue named `untracked[]` and
`stuck[]` without saying what a row is. They are:

```jsonc
"detail": {
  "checked": 3,            // registry rows OPEN for this slave
  "matched": 2,            // of those, ones the broker also holds
  "stuck": [               // registry said OPEN, broker does not hold it
    {"slave_symbol": "M2K", "master_account": "11558127",
     "master_ticket": 999031902, "slave_ticket": 392,
     "resolved": "marked_closed"}
  ],
  "untracked": [           // broker holds it, registry does not know it
    {"slave_symbol": "MGC", "quantity": 2.0}
  ],
  "at": "2026-11-12T20:05:04Z"
}
```

`checked - matched == len(stuck)` always. `resolved: "marked_closed"` is there
because the copier **fixes** a stuck row — it marks the registry CLOSED and
drops the durable position — so a tab must not present it as something
awaiting a human. `untracked` rows are left alone and genuinely do want a
look: an untracked position cannot safely be assumed to be the copier's.

`quantity` is signed and in the slave broker's own unit, so the `size_unit`
warning applies to it too.

**`size_unit` is derived, not configured.** Mirror mode speaks to an MT
terminal, so `"lots"`. Autonomous mode is `"contracts"` when the symbol has a
futures spec in `contract_specs.py` and `"shares"` when it does not, which is
how that module already distinguishes a future from an ETF. A slave may
override with a `size_unit` key in its config. Nothing is ever converted.

**`requested_price` means different things per mode, deliberately.** Mirror
mode aims at the master's own level — same instrument, or one mapped onto it
-- so it reports `signal.price`. Autonomous mode is trading a different
instrument on a different price scale, where the master's entry is context and
not a slippage reference, so it reports the limit price where a limit was
placed and otherwise the quote the stops were sized from. `fill_price` is the
broker's in both cases, and `null` where the broker has not reported one yet.

**`balance_adjustments` lists only the non-zero ones.** The snapshot example
above shows `{"ibkr-paper-1": 0.0}` and the full report shows `{}`; the
implementation sends the second. An adjustment of zero is the absence of one,
and a dictionary of zeros reads as though something were configured.

**`copier.version`** is `"5.1.0"`. The copier had no version string at all --
this introduced one, numbered from the README's "Phase 5 hardening" as the
state the reporter was added to.

## One snapshot field beyond the spec: `slaves[].magic_filter`

A master terminal running thirty or forty origin EAs sends every strategy
down one socket, and each slave takes a selection of them by magic number
(`docs/magic-selection.md`). The snapshot reports that selection:

```jsonc
"magic_filter": {
  "allow": [1001, 1002],
  "deny": [],
  "labels": {"1001": "US2000 trend", "1002": "GC breakout"},
  "source": {"kind": "ops_hub_registry", "portfolio": "Trend",
             "imported_at": "2026-11-12T20:05:02Z", "revision": 7}
}
```

`null` means the slave takes every strategy, which is NOT the same as an
empty selection; the tab has to tell those apart. `labels` and `source` are
present only where the selection was imported with them.

This is the field that closes the loop with the hub's own registry — the
portfolio manager is what names a magic number, so the tab can render
"this slave takes the trend-following book" rather than five integers,
using its own names and not the copier's. `source` says which portfolio a
selection was resolved from and at what revision, so a tab can show one
that has drifted from the registry since.

The copier never resolves a portfolio itself: the reporter is push-only,
and a copier that fetched its own risk configuration from a web service
would have the market open while a failed GET decided which strategies it
was copying. Resolving is an offline step that writes the resolved list
through the copier's own `PATCH /slaves/{id}`.

**`targets` on `signal_received` counts the slaves that actually took the
signal**, not the number of slaves registered. On a hub where each slave
takes a selection those differ for nearly every signal, and the smaller
number is the one that makes received comparable with copied.

## Two event types beyond the catalogue

The copier reports these, and the hub should store them like any other rather
than reject the report:

| type | class | `detail` |
|---|---|---|
| `contract_roll` | info | `slave_symbol`, `slave_ticket`, `from_contract`, `to_contract`, `sl`, `tp` |
| `drawdown_alert` | **alert** | `account_type`, `drawdown_pct`, `peak_equity`, `equity` |

Both are in the copier's vocabulary and neither was in the table. Dropping
them would leave the tab missing the two events most likely to explain a
surprise: a roll replaces a live position with a different contract, and a
drawdown alert is the account itself saying something.

**More generally: the hub must tolerate a `type` it does not know.** The
copier passes an unmapped bus event through with its own fields as `detail`,
so a new event added to an engine shows up in the tab as something unlabelled
rather than silently vanishing. Treat an unknown type as `info`.

## A third reconciliation event: `flex_reconciliation`

Same idea as `reconciliation`, against a different authority.
`reconciliation` compares the registry to what the broker says it **holds**;
this compares it to what the broker says it **executed**, from an IBKR
Activity Flex statement. It is the only number in a report that did not
come through the Gateway API, which is the whole value of it. Class is
`mismatch`: alert only when something disagrees.

```jsonc
"detail": {
  "account": "DUR314653",
  "period": "20261012 -> 20261110",
  "checked": 2,          // executions in the statement
  "orders": 1,           // distinct broker orders among them
  "matched": 1,          // orders with a registry row
  "protective": 0,       // orders that are a resting stop or target
  "unjoinable": 0,       // executions with no ibOrderID in the query
  "stored": 2, "already_had": 0,
  "mismatched": [
    {"ib_order_id": "900001", "slave_id": "ibkr-paper-1",
     "slave_symbol": "GCZ6", "executions": 2,
     "price": {"hub": 4040.70, "broker": 4038.95}}
  ],
  "untracked": [],
  "absence_checked": false
}
```

**`absence_checked` is always `false`, and the tab must respect it.** A
clean pass does NOT mean the broker executed everything the registry
expected. A Flex statement covers completed statement days only, so a
registry row with no execution in it is usually legitimate — and telling a
real gap from a legitimate one means comparing this hub's epoch timestamps
against statement dates in the account's configured report timezone, which
the file does not state. The copier reports only what the statement
positively says.

`mismatched` is the valuable one: the hub captures a fill price once, over
the API, at the moment it happens, and nothing other than the broker's own
statement can say it was captured wrongly. `untracked` means the broker
filled an order this copier has no record of; resting stop and target fills
are NOT untracked (their order ids are known), so an entry here is real.
`unjoinable` counts executions the Flex query did not report an `ibOrderID`
for — reported as a number so an empty reconciliation shows its cause
rather than looking like "nothing to check".

## Fields added beyond the catalogue's rows

- `signal_refused.action` — `"OPEN"`, `"MODIFY"` or `"CLOSE"`. A refused
  CLOSE leaves the slave holding a position the master has exited and needs a
  human; a refused OPEN is a missed trade. Nothing like each other.
- `master_disconnected` / `master_reconnected` now carry `silent_for_s`, which
  the spec asked for and the copier was not measuring.
- `open_deferred` / `close_deferred` carry `retry_every_s` as specified, and
  `close_deferred` also carries `slave_ticket`.
- `deferred_*_abandoned` carry `attempts` alongside `reason` and `waited_s`.

## Fields that are `null` today, and why

Each is a case where the copier does not hold the number at the point the
event is emitted. None is hard to add later; none was worth changing an
engine's internals for now.

| field | why |
|---|---|
| `deferred_open_filled.slave_ticket`, `.fill_price` | the retry runs the whole open path again and emits its own `slave_open`, which carries both. This event says only that the wait ended. |
| `entry_unfilled.limit_price`, `.waited_s` | the adapter cancels the order and reports the outcome; neither the original limit nor the elapsed wait is in hand at that point. |
| `netting_blocked.net_held` on an **autonomous** slave | the refusal is decided inside a per-candidate loop. Mirror slaves do report it, read back off the same positions the refusal was decided from rather than asked for again. |
| `slave_open.legs` | always `1`. Both engines place one broker order per leg; a size that had to be split would report the number it became. |
| `slaves[].preflight` | `null` until preflight has finished, and on any adapter that cannot resolve contracts at all. Not the same as "nothing resolved". |

## Counters are a convenience, not the record

`counters.today` and `counters.since_start` are kept in the copier's memory
and derived from the event stream. `since_start` means what it says.
**`today` resets when the copier restarts**, which the hub should not inherit:
the hub has the events themselves and can count a real UTC day from them. Use
these for a header figure, not for a daily total anyone relies on.

The mapping, so the hub's own counting can agree with the tab's header:

- **received** — `signal_received`
- **copied** — `slave_open`, `slave_modify`, `slave_close`,
  `deferred_open_filled`, `deferred_close_filled`
- **refused** — `trading_hours_blocked`, `netting_blocked`, `margin_blocked`,
  `route_blocked`, `route_unresolved`, `signal_unrouted`,
  `duplicate_open_ignored`, `signal_refused`, `kill_switch_blocked`,
  `deferred_open_cancelled`
- **failed** — `slave_error`, `slave_start_failed`, `slave_recovery_failed`,
  `entry_unfilled`, `deferred_open_abandoned`, `deferred_close_abandoned`

`margin_reduced` and the two `*_deferred` events feed none: the copy they
belong to is counted by its own event, and counting these as well would
double it.

Note also that the autonomous engine's own name for a netting refusal is
`netting_refused`; the reporter maps it onto the spec's `netting_blocked`, so
the hub sees one type for both engines.

## What is NOT deployed

`enabled` is `false` in `hub/config/hub_reporter.json`, there is no key, and
nothing has been pointed at production. The copier's tests run against a stub
HTTP server on a loopback port (`hub/tests/stub_hub.py`), and one of them
asserts that no test file can so much as name the production host.

Switching it on, once the hub has a route and has issued a key: put the key in
`ODH_COPIER_KEY` or in `hub/config/hub_key.txt` (gitignored), set `enabled` to
true, and restart the copier. With no key the reporter subscribes to nothing,
starts no task and writes no row.
