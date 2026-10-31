# Bridge EAs (Phase 6)

**Versions:** each EA carries `#property version` and `BRIDGE_VERSION`
(same value), printed in the Experts log on start-up, with a version history
in its header comment. Current: `TradeCopierBridge` 1.02, `SlaveBridge` 1.03.
Bump both lines in the EA with every change.

Two EAs, one per role:

- `TradeCopierBridge.mq4` -- runs on the **master** account. Snapshots open
  market orders every `PollIntervalMs`, diffs against the previous
  snapshot, and publishes real OPEN / MODIFY (SL or TP changed) / CLOSE
  signals over ZMQ PUB, plus a periodic heartbeat. (Phase 1 only sent a
  hardcoded test payload; Phase 2 replaced that with real order-state
  diffing.) Phase 3 adds the instrument's own `iATR()` value to every
  signal as an `"atr"` field, so autonomous-mode slaves (e.g. Schwab) can
  back-calculate what multiple of that ATR the SL/TP represent and reapply
  the same multiple to their own instrument's ATR. This is "Option 1"
  back-calculation -- a stand-in until the fxDreema EAs that actually set
  SL/TP are updated to publish their own ATR multiple directly via
  `GlobalVariableSet()`.
- `SlaveBridge.mq4` -- runs on a **slave** account. Binds a ZMQ REP socket
  and executes whatever the hub's `MT4Adapter` sends it: `OPEN` ->
  `OrderSend`, `MODIFY` -> `OrderModify`, `CLOSE` -> `OrderClose`, `PING` ->
  a bare ACK for health checks, and `POSITIONS` -> every open market
  position on the account. Replies with a JSON ACK or error for every
  command.

  `POSITIONS` is what lets `MT4Adapter` implement `get_open_positions()`,
  which in turn enables Trade Registry reconciliation for MT4 slaves and
  live P&L on the dashboard. It reports *all* positions, not just
  copier-placed ones, because reconciliation specifically needs to notice
  untracked positions so it can warn about them instead of assuming they're
  the copier's to touch.

  `CLOSE` and `MODIFY` are idempotent against an already-closed ticket: if
  `OrderCloseTime()` is non-zero the EA replies
  `{"status":"ok","already_closed":true}` rather than letting `OrderClose`
  fail with 4108. A leg closed by hand (or by its own SL/TP) before the
  master's CLOSE arrived has reached the state CLOSE was asking for, and
  reporting it as an error used to strand the registry row at `OPEN`.

## Dependencies

Both EAs need the [mql-zmq](https://github.com/dingmaotu/mql-zmq) binding,
since MQL4 has no native ZeroMQ support:

1. Download mql-zmq (https://github.com/dingmaotu/mql-zmq). MT4 is always
   32-bit, so you want `Library/MT4`, not `Library/MT5`.
2. Copy **both** include trees:
   - `Include/Zmq/*.mqh` -> `MQL4/Include/Zmq/`
   - `Include/Mql/Lang/*.mqh` -> `MQL4/Include/Mql/Lang/`

   The second one is not optional and is easy to miss. `Zmq.mqh` includes
   `Mql/Lang/Native.mqh`; without it MetaEditor fails with
   `error 106: can't open "Mql/Lang/Native.mqh"` and then cascades into
   dozens of bogus "undeclared identifier" errors from that one cause.
3. Copy `libzmq.dll` **and** `libsodium.dll` from `Library/MT4` into
   `MQL4/Libraries/`.
4. Install the **Visual C++ 2015 redistributable** if it isn't already
   present -- without it the DLLs fail to load with an unhelpful error
   rather than a clear one.
5. In MetaTrader: Tools -> Options -> Expert Advisors -> check
   "Allow DLL imports". The slave terminal also needs **AutoTrading** on,
   or `OrderSend` fails with 4109 (`trade not allowed`) while everything
   else looks healthy.
6. Attach the relevant EA to a chart on each terminal and check the Experts
   log for `TradeCopierBridge connected: ...` (master) or `SlaveBridge
   listening on ...` (slave).

`Include/Mql/Lang/` ships inside mql-zmq as a subset of mql4-lib -- you
don't need to install mql4-lib separately.

### Compiling from the command line

`metaeditor.exe /compile:"<path to .mq4>" /log:"<logfile>"` compiles without
opening the GUI, which is the quickest way to see real errors. **If the
terminal is a portable instance, pass `/portable` as well** -- otherwise
MetaEditor creates a fresh roaming data folder, looks for the includes
there, and fails with `error 106` even though they're installed correctly
next to the exe. The log is UTF-16.

### Restarting cleanly

mql-zmq's `Context` is a *named, shared* handle, and MT4 does not unload the
DLL when an EA is removed -- so the ZMQ context and its I/O thread outlive
the EA. Sockets stay bound or stay retrying with nothing servicing them,
which looks like a port that accepts connections but never answers.

**Removing or re-attaching the EA does not give you clean ZMQ state. Only
restarting the terminal does.** `SlaveBridge` checks `bind()`'s return value
and will say so explicitly if the port is still held.

Note also that MT4 only reloads a recompiled EA once the terminal processes
the file-change event, which generally needs the terminal window focused.

## TradeCopierBridge.mq4 (master) inputs

| Input | Default | Purpose |
|---|---|---|
| `HubHost` | `127.0.0.1` | Address of the Python hub |
| `HubPubPort` | `5555` | Hub's SUB socket -- EA connects here as PUB |
| `HubPushPort` | `5557` | Hub's PULL socket -- EA connects here as PUSH (heartbeat) |
| `PollIntervalMs` | `500` | How often to diff open orders |
| `HeartbeatIntervalMs` | `5000` | How often to send a heartbeat |
| `SlTpEpsilon` | `0.00001` | Ignore SL/TP deltas smaller than this (float noise) |
| `AtrPeriod` | `14` | `iATR()` period used for the `"atr"` field |
| `AtrTimeframe` | `PERIOD_CURRENT` | `iATR()` timeframe used for the `"atr"` field |

## SlaveBridge.mq4 (slave) inputs

| Input | Default | Purpose |
|---|---|---|
| `HubCommandPort` | `5560` | REP port this EA binds -- must match the `port` set for this slave in `hub/config/slaves.json` |
| `PollIntervalMs` | `100` | How often to poll for a command |
| `Slippage` | `5` | Slippage (points) passed to `OrderSend`/`OrderClose` |

## Connection direction

The hub binds its SUB (`:5555`) and PULL (`:5557`) sockets, so the master
EA is the one that calls `connect()` -- multiple master EAs (or the same EA
reconnecting) can join without port conflicts. For the slave side it's
reversed: the hub's `MT4Adapter` is a ZMQ REQ client that `connect()`s out
to each slave's address, so `SlaveBridge.mq4` is the one that `bind()`s its
REP socket -- one bound port per slave account.

## Testing without a real slave terminal

`hub/scripts/simulate_slave.py` stands in for `SlaveBridge.mq4` during
development: it binds the same REP port and answers OPEN/MODIFY/CLOSE/PING
from an in-memory ticket book, so the hub side (adapters, Copy Engine, Trade
Registry) can be exercised without MT4 running at all. See
`hub/README.md` for the full walkthrough.

## Symbol names must match on both terminals

Mirror mode passes the master's symbol string straight through -- there is
no translation. If the master trades `XAUUSD` and the slave broker calls it
`GOLD` or `XAUUSD.raw`, `OrderSend` fails with 4106 (`unknown symbol`).

The symbol also has to be **in the slave's Market Watch**, not merely
offered by the broker: an unselected symbol makes `MarketInfo()` return 0
and the order is rejected. You can check without opening the GUI by looking
for the symbol in `<data folder>/history/<server>/symbols.sel`.

## Schwab slaves (Phase 3) don't use a bridge EA at all

There's no MQL4 counterpart for Schwab -- `hub/app/adapters/schwab.py`
talks to Schwab's REST API directly over the internet, using the master's
`"atr"` field (added above) to compute autonomous SL/TP rather than
following the master's own SL/TP values. See `hub/README.md` for the
Schwab OAuth setup and the `hub/scripts/simulate_schwab.py` fake-server
test flow.

## Pointing at a remote hub (Phase 6: VPS deployment)

Once the hub is running on a VPS instead of your own machine, set
`HubHost` (master EA) to the VPS's public IP or domain -- everything else
is unchanged, since the EA already connects out over plain TCP regardless
of whether the hub is next to it or across the internet.

`SlaveBridge.mq4` doesn't need any changes at all for a remote hub: it
binds locally and the hub's `MT4Adapter` connects *out* to it, so the
slave terminal needs a reachable address (a static IP, or a dynamic-DNS
hostname, or a port forward on its router) rather than the other way
around -- see `hub/config/slaves.json`'s `host`/`port` for that slave.

ZMQ has no built-in authentication or encryption -- anyone who can reach
`HubPubPort`/`HubPushPort` can publish fake signals or read real ones in
plaintext. `deploy/ufw-rules.sh` restricts those ports on the VPS side to
your MT4 machine's IP; see that script's header comment for the VPN
alternative if your IP isn't static.
