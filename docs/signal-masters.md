# One terminal per timeframe: the shape the masters are taking

Chris's plan, stated 2026-11-18. Written down because it answers a question
three documents were holding open, and because two things the copier already
does turn out to depend on it being true.

## The topology

A **master**, as the copier means it, is an MT4 account *and* a terminal: one
account, one terminal, one PUB socket. That is what the bridge EA publishes
from, and `master_account` is how everything downstream tells one apart.

The plan is **one terminal per timeframe** — four of them: daily, H4, H1,
weekly. Each runs the same EA core as a base library, runs its strategies on
out-of-sample data, and splays the signals out to whichever slaves want them.

New strategies are not added by designing a portfolio. They are added by the
research side and deployed OOS onto the terminal for their timeframe, and
they turn up in the registry as units with a magic number.

**Which slaves a magic ends up on is the Portfolio Manager's job** —
assembling a book, setting each strategy's risk scale, and exporting it to
this copier as a portfolio the ops hub can track (`docs/portfolio-import.md`).

### The name

"Master" is the copier's word throughout, and it is the wrong half of the
relationship: these terminals are where signals come *from*, not something
that commands. **Signal Master** or **Signal Source** reads better and Chris
has raised both. Not settled here.

If it changes, change the words on screen and leave the wire alone:
`master_account` is in the signal payload, in four database tables' keys, in
`broker_order_ref` (`gtc:<master_account>:<master_ticket>:<symbol>`) and in
the ops hub's reporting contract. Renaming the field would be a migration of
every one of those to buy a better noun; renaming the label costs nothing.

## What this settles

### The magic collisions were never going to happen

Three things in a slave's config are keyed by magic number alone —
`magic_filter`, `risk_scale_by_magic`, `bar_size_by_magic` — where `routes`
carry their own `account` and so are master-aware. On 2026-11-18 that looked
like a gap waiting for a second master: two masters both using magic 1001 for
different strategies could not be told apart.

Under this topology it cannot occur:

- the registry's `unit.magic` is **`UNIQUE`**, and `unit_magic_is_permanent`
  refuses to change one once assigned, so no two registry units share a magic
  whatever terminal they run on;
- a strategy has one timeframe, so it is deployed to exactly **one** of the
  four terminals.

One magic, one strategy, one terminal. The keys are safe as they are, and the
copier's import refusal for "the same magic given two different accounts"
stays as the net it was meant to be — reachable only by an EA that is not a
registry unit, or by magic `0`, which is a hand-placed trade.

### The registry does not need to record which master an EA runs on

`docs/downstream-trade-copier.md` in the registry asked whether an `account`
row should mean the terminal whose EAs publish. Under this plan the link is
already recorded twice over: a unit carries its `timeframe`, and **the magic's
leading digit names that timeframe** (`DEFAULT_BAR_SIZE_BY_MAGIC_PREFIX`:
1 daily, 2 H4, 3 H1, 4 weekly). Terminal, timeframe and magic prefix are one
fact in three places.

So nothing has to be stored to answer "which terminal published this" — and
the copier never has to ask, because the signal carries the account.

### The ATR timeframe keeps working, unchanged

`resolve_bar_size()` reads the timeframe off the magic's leading digit, so a
strategy deployed today measures its stop against the right bars today,
without anyone listing it anywhere. That rule is **already** the rule this
topology implies, written before the topology was. Four terminals change
nothing about it.

## What it makes newly possible

**A misdeployed EA becomes detectable.** Once there are four terminals, the
signal's source account and its magic's leading digit are two independent
statements of the same timeframe. An H4 terminal publishing a magic that
begins `1` is a daily strategy loaded on the wrong chart — and today nothing
would notice: the copier would size it against daily ATR, correctly per the
magic, while the EA traded H4 bars.

That is one comparison per signal against a four-row map, and it is the kind
of thing that otherwise shows up as a position several times the intended
size with no screen saying why. Worth building when the second terminal
exists; pointless before.

**Masters stop needing to be named by hand.** `last_heartbeat_by_account`
already lists every terminal that has reported, and the dashboard renders one
card per account. With the prefix map, each card can say which timeframe that
terminal is for, from the magics it has actually published — rather than from
anything configured.
