# Which strategies reach which slave

One master terminal can run thirty or forty origin EAs. Each slave is meant to
receive a *selection* of them. This is how that selection is configured, where
it lives, and why it lives there.

## Where the decision belongs

**On the slave, in the hub — not in the EA.**

A master EA publishes down one PUB socket and has no idea which slaves exist.
It must not: putting the selection in the EA would mean editing forty charts
every time a slave changed, and which strategies a slave takes is the slave's
business, not the master's.

**In the Signal Bus's fan-out — not in the engines.** Two reasons:

- `signal_received.targets` is reported to the Operations hub as "how many
  slaves this signal fanned to", and is what makes *received* comparable with
  *copied*. Filtered in the engines, that number would count every registered
  slave and be wrong for every signal.
- A signal a slave is not configured to take should never reach its queue.
  Dropped later, it is indistinguishable in a log from one the engine refused
  for a reason that matters.

Every action is filtered, not only `OPEN`. `TradeCopierBridge.mq4` 1.02+ puts
`OrderMagicNumber()` on `OPEN`, `MODIFY` and `CLOSE` alike, so this is safe —
and filtering only opens would send a slave the exits of trades it never
entered.

## Configuration

In the slave's own config (`config/slaves.json`, or `PATCH /slaves/{id}`):

```jsonc
// The common case: an allow-list.
"magic_filter": [1001, 1002, 1003]
```

```jsonc
// Long form. `deny` is applied after `allow`, so a block always wins --
// that way one magic can be taken out of service without editing the
// list it belongs to.
"magic_filter": {
  "allow": [1001, 1002, 1003],
  "deny":  [1002]
}
```

A slave with **no** `magic_filter` takes every strategy, which is what every
slave configured today does. The key is purely additive.

A deny-list on its own takes everything else.

Changes take effect without a hub restart: `PATCH /slaves/{id}` validates the
selection *before* writing it and then updates the bus. A selection that
cannot be read is answered with a 400 and never persisted — persisted, it
would stop the slave on the next restart rather than on the request that broke
it, and by then the connection between the two is a day old.

### Two deliberate refusals

**A selection that cannot be read stops the slave.** A typo in an allow-list
fails *open* — the slave would copy a strategy nobody put on its list — and
that is the one direction this must not fail. `start_engines()` stops the one
slave loudly and keeps every other one running.

**`{"allow": []}` is refused outright.** An empty allow-list and no
allow-list are opposites, and read as a boolean they are identical; that is how
a filter comes to pass every signal it was written to stop. Remove the key to
take everything, or disable the slave.

### Magic `0` and no magic at all

`OrderMagicNumber()` is `0` for a trade placed by hand, so `0` is a real value.
List it to copy manual trades; deny it to exclude them. (Everything here
compares against `None` rather than reading truthiness, for exactly this
reason.)

A signal carrying **no** magic — a bridge EA older than 1.02 — is refused by a
slave with an allow-list, and accepted by one with only a deny-list. An
allow-list is an explicit statement of which strategies the slave takes, and
"cannot tell" is not one of them. This is the same reasoning as strict
`routes`: an unrecognised strategy landing on a known strategy's contract is
the worst available outcome.

## `magic_filter` versus `routes`

Both key on magic and they do different jobs:

| | decides | applies to |
|---|---|---|
| `magic_filter` | whether the slave **hears** the signal | every slave, mirror and autonomous |
| `routes` | which **instrument** a `(symbol, magic)` becomes | autonomous slaves only |

A mirror slave has no routes and still needs a selection, which is why these
are separate. On an autonomous slave they compose: the filter decides what
arrives, `routes` decides where it lands, and a signal that arrives and
matches no route is still refused by `routes` as it always was.

`routes` entries now accept a **list** of magics:

```jsonc
"routes": [
  {"symbol": "US2000", "magic": [1001, 1002, 1003], "target": "M2K"}
]
```

Several strategies routinely want the same instrument on one slave, and
writing a near-identical route for each is how one of them comes to differ by
a typo nobody notices. A route built from a list still records the magic that
actually matched, so a copy is attributed to the strategy that caused it.

## Importing a selection from the portfolio manager

The Operations hub's **registry** is where a magic number is given a strategy
name and a portfolio, many-to-many, with CSV import and export. That makes it
the authority on "which magics are the trend-following book". A selection
maintained by hand here is a second copy of an answer that already exists
somewhere better.

So an entry may be an **object** carrying whatever the registry knows. The
copier reads `magic`, keeps any name it finds under `strategy` / `name` /
`label`, and ignores everything else — it has no business failing on a field
the portfolio manager added for its own reasons:

```jsonc
"magic_filter": {
  "allow": [
    {"magic": 1001, "strategy": "US2000 trend", "portfolio": "Trend"},
    {"magic": 1002, "strategy": "GC breakout",  "portfolio": "Trend"}
  ],
  "source": {
    "kind": "ops_hub_registry",
    "portfolio": "Trend",
    "imported_at": "2026-11-12T20:05:02Z",
    "revision": 7
  }
}
```

An export can therefore be pasted in verbatim, and the names come with it:
they appear in the log line that says what a slave takes, and in
`snapshot.slaves[].magic_filter` that the copier reports — so the hub's Copier
tab can show *its own* strategy names beside each slave without the copier
resolving anything.

### `source` is recorded and never acted on

The copier does not fetch it. It has no idea what a portfolio is, and it must
not:

- the reporter is **push only**. The hub never calls the copier, which is what
  makes the copier work behind a VPS with no inbound route;
- a copier that pulled its own risk configuration from a web service would
  have the market open while a failed GET decided which strategies it was
  copying.

Resolving a portfolio to a set of magics is therefore an **offline step** that
writes the resolved list through `PATCH /slaves/{id}`, which already takes
effect without a restart. `source` is there so an imported selection can be
told from a hand-written one and refreshed later — the shape is settled now so
that whatever does the resolving does not have to change this schema.

A `source` with no `allow` list resolves to nothing and is therefore not a
filter: the slave still takes everything, rather than silently taking nothing
because a fetch never happened.
