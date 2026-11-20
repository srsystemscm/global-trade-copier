"""The go-live gate's unit list, read out of the research registry.

The gate (FxdGateTest.mq4) fires a test entry for every live strategy at
once, to prove that what was delivered is what is running: the right
magic on the right symbol, the right side, copied to the right slaves at
the right size. It needs to know which units those are, and nothing in
MT4 can ask the registry.

So this writes the list, and the gate reads it. Run it, drop the file in
the master terminal's MQL4/Files, run the gate.

WHAT IS AND IS NOT ALLOWED TO FIRE. A magic is permanent in the registry
(`unit_magic_is_permanent`) and unique, so a number identifies one
strategy forever -- including the numbers belonging to units nobody
wants traded. `void` is "reservation abandoned, the number stays
consumed", and `rejected` and `retired` are decisions not to run
something. Those three can never be listed, whatever is asked for:
firing one would be testing a strategy that is not meant to exist.
`approved` is the default. `candidate` can be added by hand for a
rehearsal before launch.

THE SIDE COMES FROM THE REGISTRY, not from the gate. A unit is long,
short, or both, and that is a property of the strategy -- so the list
says which, and the gate fires what each unit says rather than firing
two legs at everything. A `both` unit is what gives the gate its
opposing pair, which is also how the copier's refusal of opposing legs
gets exercised.

Read-only, always: this opens the registry with mode=ro and writes
nothing to it.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import sqlite3
import sys

FORMAT = 1

DEFAULT_DB = r"C:\Users\c_mie\Downloads\research_registry\data\registry.db"

# Never listed, whatever --status asks for. See the module docstring.
NEVER = ("void", "rejected", "retired")
DEFAULT_STATUSES = ("approved",)


def broker_symbol(con: sqlite3.Connection) -> dict[str, str]:
    """Registry code -> the name a terminal trades it under.

    `symbol_alias` maps every alias to its code, and one of those aliases
    IS the code (XAG -> XAG). The other is the tradable name (XAGUSD ->
    XAG). Where a code has no second alias, the code is the name --
    HK50 is listed once and traded as HK50.

    A broker suffix (XAUUSD.r, ETHUSD-ECN) is NOT resolved here and
    cannot be: it differs per terminal. The gate resolves the real name
    against Market Watch at run time and says so when it cannot.
    """
    out: dict[str, str] = {}
    extra: dict[str, list[str]] = {}
    for alias, code in con.execute("SELECT alias, symbol FROM symbol_alias"):
        if alias == code:
            out.setdefault(code, code)
        else:
            extra.setdefault(code, []).append(alias)
    for code, aliases in extra.items():
        if len(aliases) > 1:
            # Said out loud rather than guessed. Picking by length or
            # alphabetically would be a coin toss that looks like a rule.
            print(f"  ! {code} has several tradable aliases {sorted(aliases)}; "
                  f"using {sorted(aliases)[0]} -- edit the file if that is wrong",
                  file=sys.stderr)
        out[code] = sorted(aliases)[0]
    return out


def live_set_risk(con: sqlite3.Connection) -> dict[str, tuple]:
    """Each unit's recorded live .set risk, where one is recorded.

    The point of the gate is to test the EA **and the .set file it was
    delivered with**, so where the registry knows what risk that file
    carries, the gate sizes from it rather than from its own default.
    Retired records are ignored: they are what the EA used to run.

    Empty today -- no live .set has been recorded for any unit yet -- so
    every row falls back to the gate's own inputs and says so. It gets
    truer as they are recorded, rather than waiting for them.
    """
    out: dict[str, tuple] = {}
    try:
        rows = con.execute(
            "SELECT unit_id, risk_mode, risk_json FROM live_set "
            "WHERE retired_at IS NULL")
    except sqlite3.OperationalError:
        return out                      # a registry older than 0.10.0
    for unit_id, risk_mode, risk_json in rows:
        out[unit_id] = (risk_mode, risk_json)
    return out


def rows_for(db: str, statuses: tuple[str, ...],
             only_codes: tuple[str, ...] = ()) -> list[dict]:
    """The units the gate is allowed to fire, and a count of the ones it
    had to leave out.

    THE COUNT IS NOT DECORATION. A gate list that comes up short without
    saying so is a gate that passes vacuously -- every leg in it fires,
    nothing fails, and the strategy that was missing is the one nobody
    checked. A unit with no magic cannot be fired, because the magic is
    the only thing the copier routes on, so it is skipped either way; the
    difference is whether the skip is visible.
    """
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        symbols = broker_symbol(con)
        risks = live_set_risk(con)
        out = []
        #--- Counted separately because the row filter below is in SQL.
        no_magic = [
            code for code, status in con.execute(
                "SELECT symbol, status FROM v_unit WHERE magic IS NULL")
            if status not in NEVER and status in statuses
            #--- Scoped like the rows are. Warning about a symbol the
            #--- caller excluded on purpose is noise, and noise in a
            #--- go-live gate is what makes a real warning easy to miss.
            and (not only_codes or code.upper() in only_codes)
        ]
        for unit_id, magic, code, tf, side, status in con.execute(
                "SELECT id, magic, symbol, timeframe, side, status FROM v_unit "
                "WHERE magic IS NOT NULL ORDER BY magic"):
            if status in NEVER or status not in statuses:
                continue
            #--- Matched on the REGISTRY code (ETH), not the broker symbol
            #--- (ETHUSD), because the code is what the registry and the
            #--- strategy names agree on and the broker symbol is a per-
            #--- account alias.
            #--- `symbols` above is the ALIAS MAP, not this filter. Naming
            #--- this one `symbols` too made the test read `code not in
            #--- <alias dict>`, which is False for every code the registry
            #--- knows -- so the filter silently passed everything.
            if only_codes and code.upper() not in only_codes:
                continue
            mode, risk_json = risks.get(unit_id, (None, None))
            out.append({
                "magic": int(magic),
                "symbol": symbols.get(code, code),
                # `both` is what gives the gate an opposing pair; a
                # one-sided unit fires one leg, because that is all the
                # strategy has.
                "side": side or "both",
                "timeframe": tf,
                "status": status,
                "risk_mode": "" if mode is None else mode,
                "risk_json": risk_json or "",
                "source": "live_set" if mode is not None else "gate_defaults",
            })
        for code in no_magic:
            print(f"WARNING: {code} is {'/'.join(statuses)} but has no magic -- "
                  f"not in the list, so the gate will not test it",
                  file=sys.stderr)
        return out
    finally:
        con.close()


def write(path: str, rows: list[dict], db: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(f"# gtc-gate-list/{FORMAT}\n")
        fh.write(f"# generated {dt.datetime.now(dt.timezone.utc):%Y-%m-%dT%H:%M:%SZ} "
                 f"from {os.path.basename(db)}\n")
        fh.write("# magic,symbol,side,timeframe,status,risk_mode,source\n")
        writer = csv.writer(fh)
        for r in rows:
            writer.writerow([r["magic"], r["symbol"], r["side"], r["timeframe"],
                             r["status"], r["risk_mode"], r["source"]])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--out", default="gtc-gate-list.csv",
                    help="write here; copy it to the master terminal's MQL4/Files")
    ap.add_argument("--symbol", action="append", default=None, metavar="CODE",
                    help="limit to these registry symbol codes (ETH, XAG ...); "
                         "repeatable. Default: every symbol.")
    ap.add_argument("--status", action="append", default=None,
                    help=f"statuses to include (default {DEFAULT_STATUSES[0]}); "
                         f"{', '.join(NEVER)} are never included")
    args = ap.parse_args()

    statuses = tuple(args.status) if args.status else DEFAULT_STATUSES
    refused = [s for s in statuses if s in NEVER]
    if refused:
        print(f"ERROR: {', '.join(refused)} cannot be fired by the gate -- "
              f"those units are not meant to trade", file=sys.stderr)
        return 2

    if not os.path.isfile(args.db):
        print(f"ERROR: no registry at {args.db}", file=sys.stderr)
        return 2

    rows = rows_for(args.db, statuses,
                    tuple(sym.upper() for sym in args.symbol or ()))
    if not rows:
        print(f"ERROR: no units with status {', '.join(statuses)} and a magic "
              f"number -- nothing for the gate to fire", file=sys.stderr)
        return 1

    write(args.out, rows, args.db)
    print(f"{len(rows)} unit(s) -> {args.out}")
    legs = sum(2 if r["side"] == "both" else 1 for r in rows)
    print(f"  {legs} leg(s) will be sent: "
          f"{sum(1 for r in rows if r['side'] == 'both')} both-sided, "
          f"{sum(1 for r in rows if r['side'] != 'both')} one-sided")
    for r in rows:
        print(f"  {r['magic']}  {r['symbol']:<10} {r['side']:<6} {r['timeframe']:<3} "
              f"{r['status']:<10} {r['source']}")
    missing = [r for r in rows if r["source"] != "live_set"]
    if missing:
        print(f"  ! {len(missing)} of {len(rows)} have no live .set recorded, so the "
              f"gate sizes them from its own inputs rather than from the file the "
              f"EA was delivered with")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
