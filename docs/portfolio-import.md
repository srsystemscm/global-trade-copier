# Importing a portfolio from the Portfolio Manager

A slave takes a selection of the master's strategies, and each one carries a
share of the risk the EA signalled. Both are keyed by magic number, and both
were typed in by hand.

That is the step this removes. Chris, 2026-11-18:

> I dont want to have to (if possible) introduce a manual, error prone step
> like adding magic numbers and their risk scale by hand.

The Portfolio Manager already owns the answer: it is where a magic number is
given a strategy name and a portfolio. This is the file it hands over, and
what the copier does with it.

## The file

`application/json`, one portfolio per file. Schema `1.0`.

```jsonc
{
  "schema": "gtc.portfolio-import/1.0",

  "portfolio": {
    // PM's own id for this portfolio. RECORDED AND NEVER RESOLVED -- see
    // "What the copier will not do". It is here so a selection on a slave
    // can be matched back to the book it came from, and so a later export
    // of the same portfolio can be recognised as a newer copy of the same
    // thing rather than as a different one.
    "id": "pf_7f3a9c",
    "name": "Trend",
    "revision": 7,
    "exported_at": "2026-11-18T09:12:03Z"
  },

  "strategies": [
    {"magic": 1001, "name": "US2000 trend", "risk_scale": 0.5,
     "account": "44964870"},
    {"magic": 1002, "name": "GC breakout",  "risk_scale": 1.0,
     "account": "44964870"}
  ]
}
```

| field | required | meaning |
|---|---|---|
| `schema` | yes | `gtc.portfolio-import/<major>.<minor>`. A major this copier does not know is refused. |
| `portfolio.id` | yes | PM's identifier. Opaque to the copier. |
| `portfolio.name` | no | Shown on screen where there is one. |
| `portfolio.revision` | no | Any JSON scalar. Recorded, compared only for equality. |
| `portfolio.exported_at` | no | RFC3339. Recorded. |
| `strategies[].magic` | yes | Integer. `0` is a real value: it is a trade placed by hand. |
| `strategies[].name` | no | The strategy's name. Also read from `strategy` or `label`. |
| `strategies[].risk_scale` | yes | The share of what this EA signalled that the slave takes. |
| `strategies[].account` | no | Which master this magic belongs to. |

**Unknown fields are ignored**, at every level. PM adds fields for its own
reasons and the copier has no business failing on one.

## What it writes on the slave

Two keys, both of which already exist and are already read by the running
engine, so an import needs no restart and no new concept:

```jsonc
"magic_filter": {
  "allow": [
    {"magic": 1001, "strategy": "US2000 trend", "portfolio": "Trend"},
    {"magic": 1002, "strategy": "GC breakout",  "portfolio": "Trend"}
  ],
  "source": {
    // The word docs/hub-reporter.md already settled for this. The hub's
    // registry IS its portfolio manager, and the reporter sends this
    // block on to it -- a second name for one thing would have the hub
    // read every imported selection as hand-written.
    "kind": "ops_hub_registry",
    "portfolio": "Trend",
    "portfolio_id": "pf_7f3a9c",
    "revision": 7,
    "exported_at": "2026-11-18T09:12:03Z",
    "imported_at": "2026-11-18T14:02:11Z"
  }
},

"risk_scale_by_magic": {"1001": 0.5, "1002": 1.0}
```

**The ops hub is told, without anything further being built.** The reporter
already sends `source` verbatim inside `snapshot.slaves[].magic_filter`
(`docs/hub-reporter.md`), so a slave reports the portfolio it is running on
the next snapshot after an import -- which portfolio, at which revision,
applied when. That is what lets the hub's Copier tab say "this slave takes
the trend-following book", and show one that has drifted since. The copier
still resolves nothing and still never calls out.

`magic_filter` decides whether a signal reaches the slave at all;
`risk_scale_by_magic` multiplies what it stakes when it does. The shape of
the first is the one already settled in `docs/magic-selection.md` -- this
file is what produces it, rather than a second answer to the same question.

**Everything else on the slave is untouched.** `PATCH /slaves/{id}` replaces
`config_json` wholesale, so the import is a read-modify-write over the live
config: sizing, routes, masters, contract roll, the splitter, the chase and
every other key are carried across exactly as they were.

**`risk_scale` of `0` is kept, not dropped.** It means "do not trade this
strategy here", which is a decision; dropping the magic from the list instead
would say "this slave was never told about it", and the two look identical
afterwards.

## Deliberate refusals

The import is refused whole, and nothing is written, when:

- **The schema's major version is unknown.** A file written for a later
  copier may mean something different by a field this one recognises.
- **`strategies` is empty.** An empty selection and no selection are
  opposites that read identically as a boolean -- the same reasoning that
  already refuses `{"allow": []}`. An empty file is what an export looks like
  when something went wrong producing it.
- **A magic appears twice.** Which `risk_scale` wins is not a thing to guess.
- **`risk_scale` is negative**, or missing on an entry. Negative risk is not
  a smaller position, it is an inverted one.
- **`risk_scale` is above 10.** Ten times what the EA staked is not a
  calibration; it is a typo or a unit mistake (50 meaning 50%). A genuine
  figure above 10 can still be set by hand, where there is a human looking
  at that one number rather than at a file of forty.
- **Two entries give the same magic different `account`s.** `magic_filter`
  and `risk_scale_by_magic` are keyed by magic ALONE: they have no master
  dimension, so a hub following two masters that both use magic 1001 cannot
  tell them apart. `routes` already solved this with its own `account` field;
  these two have not. Refusing is the only honest answer until they do, and
  it is better found here than the first evening both masters signal at once.

## What the copier will not do

**It does not fetch.** The copier never calls the Portfolio Manager, and
there is no URL in this document. The reporter is push-only -- that is what
lets the copier run behind a VPS with no inbound route -- and a copier that
pulled its own risk configuration from a web service would have the market
open while a failed GET decided which strategies it was copying.

An import is therefore a file, handed over, applied by someone who is there.

**It does not resolve `portfolio.id`.** The id is recorded so the two sides
can talk about the same book. The copier has no idea what a portfolio is and
must not acquire one.

**It does not reconcile.** An import states the selection in full and
replaces it in full. It does not merge with what was there: a magic the file
does not mention is one the slave stops taking, which is the only reading of
"this is the portfolio" that cannot silently leave a strategy running after
it was removed from the book.

## Applying one

```
POST /slaves/{id}/import/portfolio?dry_run=true
```

The body is the file. With `dry_run=true` -- the default -- nothing is
written and the reply is the plan:

```jsonc
{
  "portfolio": {"id": "pf_7f3a9c", "name": "Trend", "revision": 7},
  "takes_now": "every magic",
  "takes_after": "allow [1001 (US2000 trend), 1002 (GC breakout)]",
  "magics_added":   [1002],
  "magics_removed": [1003],
  "scales_changed": [{"magic": 1001, "from": 1.0, "to": 0.5}],
  "unchanged": 4,
  "replaces_import": {"portfolio_id": "pf_7f3a9c", "revision": 6}
}
```

`replaces_import` is present when the slave already carries a selection from
PM, and says which one -- so re-importing the same portfolio at a newer
revision is visibly that, and importing a *different* portfolio over an
existing one is visibly that too.

With `dry_run=false` the same plan is returned, and the config is written
through the same path a hand edit takes: validated first, refused as a whole
if the selection cannot be read, and live without a restart.

## Version history

**1.0** (2026-11-18) -- first. Magics, names and risk scales, with a
portfolio id. Carries `account` per strategy, which is recorded and checked
for collisions but not yet used as a key: see the refusal above.
