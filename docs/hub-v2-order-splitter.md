# For the hub: the order splitter, and a stall the hub should watch for

Written for the Operations Hub v2 thread. Self-contained — it assumes
only `docs/hub-reporter.md`, which it extends.

Everything here is **additive**. A copier that has not been updated sends
`null` or omits these fields, and the existing tab keeps working
untouched. Nothing that already had a meaning has changed its meaning,
with one exception called out under *What changed that you may already be
rendering*.

---

## 1. What the copier now does

A route can place ONE entry as **N orders spread over a window** instead
of one print. For an entry too large for the book to absorb at once:

```
21:49:24  opened BUY MGC x11.0 -> ticket=2165 -- slice 1/3 of an intended 33.0,
          the rest 5s apart (11.0, 11.0, 11.0)
21:49:29  slice 2/3: BUY MGC x11.0 joined slave_ticket=2165 (22.0 of 33.0 placed)
21:49:35  slice 3/3: BUY MGC x11.0 joined slave_ticket=2165 (33.0 of 33.0 placed)
21:49:36  split entry 2165 filled 33.0 across 3 slice(s) at 3871.036363
```

**They collapse into ONE position.** One leg row, one size, one entry
price, one pair of protective legs, one exit, one thing to roll. That is
the whole design, and it is deliberate: the alternative — N positions —
would mean N rows for one decision, a MODIFY arriving against whichever
one a lookup happened to return, and a contract roll that has to find all
of them.

So the splitter does not change the shape of anything the hub already
renders. What it changes is that the hub previously could not tell a
33-lot print from 33 lots assembled over eleven seconds, because the leg
looks identical either way.

---

## 2. `legs[].entry_slices` — integer or null

How many broker orders this one position's entry went out in.

```jsonc
{
  "slave_id": "ibkr-paper-1", "slave_symbol": "MGC", "slave_ticket": "2165",
  "size": 33.0, "size_unit": "contracts",
  "fill_price": 3871.036363,
  "entry_slices": 3,
  ...
}
```

- `1` — an ordinary entry, one order.
- `n > 1` — a split entry.
- `null` — unknown. Either a copier that predates this, or a leg row
  written before the column existed. **Treat null as "not known", not as
  1**: the tab should show nothing rather than asserting it was a single
  print.

It is **what actually happened**, not what was configured. A master CLOSE
arriving mid-window calls off the slices still to come, so a route set to
three can legitimately produce `entry_slices: 1`. See §6.

---

## 3. `legs[].slices[]` — array, or null/absent

What each of those orders was and what it filled, in the order they went
out. Present only where `entry_slices > 1`.

```jsonc
"slices": [
  {"n": 1, "size": 11.0, "filled": 11.0, "fill_price": 3870.763636, "placed_at": 1794883764.5},
  {"n": 2, "size": 11.0, "filled": 11.0, "fill_price": 3871.563636, "placed_at": 1794883769.6},
  {"n": 3, "size": 11.0, "filled": 11.0, "fill_price": 3870.781818, "placed_at": 1794883775.7}
]
```

| field | type | meaning |
|---|---|---|
| `n` | integer | 1-based, in the order the orders went out. Contiguous. |
| `size` | number | what the slice was **placed** for |
| `filled` | number | what it **filled**. May be less than `size`, including `0`. |
| `fill_price` | number or null | `null` when `filled` is 0 — a price with no fill is not a price |
| `placed_at` | epoch seconds | when the order went to the broker |

`size` is in the slave broker's own unit, the same `size_unit` the leg
carries. Nothing is ever converted.

### THE RULE THAT MATTERS MOST

**The leg is the position. `slices` is a measurement.**

Do not assemble a position out of `slices`. Do not sum `filled` to get
the size, or average `fill_price` to get the entry. The leg's own `size`
and `fill_price` are authoritative and are what every other part of the
system trades, closes, rolls and reconciles from.

They *should* reconcile — both are written from the same read, at the
same moment:

```
#2165   (3870.763636 + 3871.563636 + 3870.781818) / 3  = 3871.036363   = leg.fill_price
#2150   (6(3870.666667) + 5(3869.3) + 5(3872.54)) / 16 = 3870.825000   = leg.fill_price
```

A mismatch means one of the two writes did not land. **That is worth
surfacing** — it is a cheap, strong integrity check the hub is well
placed to run, and the copier cannot run it on itself. If they disagree,
the leg wins and the slices are the suspect record.

---

## 4. What to render, and why it is worth rendering

The reason to spread a 33-lot entry over a window is **market impact**.
The position's single average price is the number the spreading was meant
to improve — so it cannot be the number that judges whether it worked.
Slice 1 against slice 3 can.

Three readings, in rough order of usefulness:

**Drift** — `last fill_price − first fill_price`, signed so that positive
means it cost you (a BUY filling higher later, a SELL filling lower).
This is the one that answers the question. Consistent positive drift
across many split entries means the later slices are paying for the
earlier ones, and the window is too long or the slices too large.

**Dispersion** — the spread between best and worst slice. Large
dispersion with no drift is a thin or jumpy book rather than impact.

**Completion** — `sum(filled) / leg.size`, and how many slices have
`filled: 0`. A split that routinely does not complete is a window longer
than the trades it is being used on. See §6.

Tonight's three runs, as an illustration of what the data looks like and
why one number is not enough:

| leg | size | slices | fills | drift |
|---|---|---|---|---|
| 2131 | 6 | 2/2/2 | 3871.20, 3871.40, 3871.50 | +0.30, monotonic |
| 2150 | 16 | 6/5/5 | 3870.67, 3869.30, 3872.54 | +1.87, not monotonic |
| 2165 | 33 | 11/11/11 | 3870.76, 3871.56, 3870.78 | +0.02 |

The largest order has the least drift, which is the opposite of impact.
**A caveat the hub should carry too:** these are IBKR *paper* fills
against delayed market data — the same simulator that filled an ETF exit
at exactly 38.71 three separate times in one hour. The measurement
machinery is proven; what it says about impact will only mean something
on live data. Do not let the tab present a conclusion the data cannot
support yet.

### Suggested treatment

- An affordance on the leg where `entry_slices > 1` — the copier's own
  dashboard uses a quiet `3×`, deliberately styled as information rather
  than as a warning. Nothing is wrong with a split entry.
- The per-slice fills on expansion, not inline by default: they are
  detail, and the position is the headline.
- Nothing at all for `entry_slices` of `null` or `1`. A one-element list
  under every leg on the page reads as though something were going on.

---

## 5. `status.slaves[].consume` — the one the hub should ALERT on

Not part of the splitter, and more important than it.

```jsonc
"consume": {
  "queue_depth": 0,
  "last_consumed_at": "2026-11-16T21:49:36Z",
  "seconds_since_consumed": 12.4,
  "alive": true,
  "stalled": false
}
```

On 2026-11-16 four copier engines stopped draining their signal queues.
An exception that was not an `AdapterError` escaped the consume loop and
ended the task. For 37 minutes `running`, `connected` and `open_legs` all
stayed exactly as they are when everything is fine, while eight signals
reached one slave and never reached another. The only symptom was an
absence, and an absence looks exactly like a quiet master.

- **`alive: false` is terminal.** The loop does not come back. Every
  signal for that slave is being dropped until the copier is restarted,
  whatever `running` and `connected` say. This deserves the loudest alert
  the hub has.
- **`stalled: true` with `alive: true`** means the loop is running but
  has a queue it has not touched for a minute — a handler wedged on a
  broker call. Also actionable, less terminal.
- `null` for a slave with no engine running.

The hub is the thing watching when nobody is looking at the copier's own
dashboard. This is the field that makes that watch worth anything.

---

## 6. `status.slaves[].order_split` — what was asked for

```jsonc
"order_split": {
  "slices": null,
  "interval_s": null,
  "by_target": {"GC": {"slices": 3, "interval_s": 5.0}}
}
```

`null` when nothing is configured, which is most slaves. `slices` /
`interval_s` are the slave-wide default; `by_target` lists only routes
that differ from it. A target named by a preference list (`["GC","MGC"]`
is **one** position, first tradeable wins) appears under its first
candidate.

This is the **configuration**. What any single entry actually did is
`legs[].entry_slices` and `legs[].slices[]`. They differ routinely and
both are correct:

> A route configured `3 over 5s` is a ten-second window. If the master
> closes the trade eight seconds in, the copier calls off the slices
> still to come and the position is whatever had gone out — commonly
> `entry_slices: 1`. That is the splitter working, not failing.

A tab that renders the config as though it described the trades will show
"3 slices" against positions that were placed in one order.

---

## 7. What changed that you may already be rendering

**`slave_open` event, `legs` field.** Previously hardcoded to `1` with a
comment anticipating this case. It is now the number of broker orders the
entry is being assembled from — `3` for a split entry.

It is the **planned** count, known at placement. `legs[].entry_slices` is
what happened. When they differ, the window was cut short, and the
difference is itself informative.

`size` on that event remains the POSITION's size, not the first slice's.
`size` and `legs` describe one thing: *"33 contracts in 3 orders"*.

Nothing else changed meaning.

---

## 8. Upsert and ordering

Unchanged from `docs/hub-reporter.md`: legs are upserted on
`(master_account, master_ticket, slave_id)` and sent in full whenever
they change.

- `slices` travels **inside the leg**, so it replaces wholesale on each
  upsert. Do not merge slice arrays across reports; the newest leg row
  carries the complete current picture.
- A leg's slices can arrive **before** the leg's own `fill_price`
  settles, and can be corrected afterwards — a slice re-reported after a
  reconnect updates in place on `n` rather than duplicating. Render from
  whatever the latest leg says.
- Slices are always sent in `n` order, contiguous from 1.

---

## 9. Quick reference

| field | where | type | null means |
|---|---|---|---|
| `entry_slices` | `legs[]` | int | not known — show nothing |
| `slices[]` | `legs[]` | array | not split, or not known |
| `slices[].fill_price` | | number | that slice filled nothing |
| `consume` | `status.slaves[]` | object | no engine running |
| `consume.alive` | | bool | — `false` is terminal, alert |
| `order_split` | `status.slaves[]` | object | nothing configured |
| `legs` | `slave_open` event | int | — now N, was always 1 |

Copier-side source, if anything here is ambiguous:

- `hub/app/order_split.py` — how a size is cut into slices
- `hub/app/adapters/ibkr.py` — `_schedule_slices`, `_place_slice`,
  `_stamp_slice_fills`, `_await_split_fill`
- `hub/app/trade_registry.py` — `record_entry_slices`, `entry_slices_for`
- `hub/app/hub_reporter/rows.py` — `_leg`, `_slices_for`
- `hub/app/hub_reporter/snapshot.py` — `_consume`, `_split`
- `docs/hub-reporter.md` — the full contract
