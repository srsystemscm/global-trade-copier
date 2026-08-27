# Bridge EAs (Phase 6)

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
  a bare ACK for health checks. Replies with a JSON ACK or error for every
  command.

## Dependencies

Both EAs need the [mql-zmq](https://github.com/dingmaotu/mql-zmq) binding,
since MQL4 has no native ZeroMQ support:

1. Download the mql-zmq release matching your MT4 build (32-bit).
2. Copy `Zmq.mqh` and its dependency headers into `MQL4/Include/Zmq/`.
3. Copy `libzmq.dll` (and `libsodium.dll` if required by your build) into
   `MQL4/Libraries/`.
4. In MetaTrader: Tools -> Options -> Expert Advisors -> check
   "Allow DLL imports".
5. Attach the relevant EA to a chart on each terminal and check the Experts
   log for `TradeCopierBridge connected: ...` (master) or `SlaveBridge
   listening on ...` (slave).

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
