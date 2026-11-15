# Order timing per route

How long an entry lives at the broker, and how long the copier waits for
it, are set **per route**. Both are needed together.

## The problem this solves

A CFD signal arrives at 18:00 ET and should open an ETF leg. Before
2026-11-15 it could not, for two reasons that each defeated the other's
fix:

| baked in where | value | what it did |
| --- | --- | --- |
| `IBKRAdapter.entry_tif` | `DAY` | the order expires at that session's close, so one placed at 18:00 ET can never reach the next morning |
| `IBKRAdapter.entry_timeout` | `900` | the copier cancels the unfilled entry after 15 minutes and pulls its protective legs with it |

Setting only the TIF leaves the copier cancelling the order a quarter of
an hour in. Setting only the wait leaves the copier watching an order
the broker has already expired. **Set both or neither.**

## The two fields

```json
{
  "symbol": "SP500",
  "target": "SPXL",
  "direction": "BUY",
  "order_type": "limit",
  "outside_rth": true,
  "limit_offset_pct": 0.01,
  "tif": "GTC",
  "entry_timeout": 93600
}
```

- **`tif`** — `DAY`, `GTC`, `GTD` or `IOC`. Omitted means the adapter's
  own default (`DAY` for entries). A value outside that set is refused
  when the order is built rather than sent, because an order IBKR rejects
  for a bad TIF is a position that silently never opens.
- **`entry_timeout`** — seconds the copier will wait for a **limit**
  entry before abandoning it. Omitted means the adapter's default (900).
  Ignored for market orders: one that has not filled in ten seconds has
  something wrong with it whatever the route says.

`order_type`, `outside_rth` and `limit_offset_pct` were already per-route
and are unchanged. All five are read together.

## Choosing a wait

IBKR US equity and ETF sessions, Eastern Time:

| | |
| --- | --- |
| pre-market | 04:00 – 09:30 |
| regular | 09:30 – 16:00 |
| post-market | 16:00 – 20:00 |

| signal lands | next chance to fill | wait needed |
| --- | --- | --- |
| weekday 18:00 | same evening, until 20:00 | `7200` (2h) |
| Sunday 18:00 | Monday 04:00 pre-market | `36000` (10h) |
| Sunday 18:00, resting all Monday | Monday 20:00 close | `93600` (26h) |

A Sunday evening order has no session to fill in at all — there is no
equity trading until Monday's pre-market. That is the case `DAY` could
never serve.

## What a market order cannot do

Outside regular hours IBKR refuses a market order outright. An evening
ETF leg needs `order_type: "limit"` **and** `outside_rth: true` — a limit
without `outside_rth` waits for the 09:30 open, and `outside_rth` on a
market order is rejected. The TIF and the wait are the third and fourth
parts of the same arrangement, not alternatives to it.

## Where it shows

The Routes table on the Slaves tab prints the TIF as a badge and the wait
in minutes. A route with a non-`DAY` TIF and no `entry_timeout` is
flagged **no wait set**, because that is the combination that looks
configured and changes nothing.

## Not covered

- **`bracket_tif`** is still per-slave (`GTC` by default). The ETF routes
  set `broker_brackets: false` and rest no protective orders at all, so
  it does not arise for them.
- **Contract rolls** build their own order and deliberately carry neither
  field: a roll is a market order on a futures contract, where the
  session an ETF leg must outlive does not apply.
