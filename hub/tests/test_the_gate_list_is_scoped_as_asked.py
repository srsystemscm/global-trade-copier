"""The go-live gate's unit list: what it includes, and what it says it left out.

A gate list is the one file that decides what gets fired at a live
account, so the two ways it can be wrong are not symmetrical. Too many
rows fires a strategy nobody meant to test. TOO FEW passes vacuously --
every leg in a short list fills, nothing fails, and the strategy that was
missing is the one nobody checked.

The symbol filter shipped inert, and that is why this file exists. It
read

    symbols = broker_symbol(con)          # the ALIAS MAP, set at the top
    ...
    if symbols and code.upper() not in symbols:

so the filter tested the code against the alias dictionary -- which
contains every code the registry knows -- and skipped nothing. It looked
exactly like a filter, the output was a plausible list, and only counting
the rows showed it had done nothing at all.
"""
import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

GATE = Path(__file__).resolve().parents[2] / "tools" / "make_gate_list.py"


@pytest.fixture(scope="module")
def gate():
    spec = importlib.util.spec_from_file_location("make_gate_list", GATE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["make_gate_list"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def db(tmp_path):
    """A registry with just the columns the gate reads.

    v_unit is a view in the real registry; here it is a table, because
    what is under test is the gate's filtering and not the registry's
    own schema.
    """
    path = tmp_path / "registry.db"
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE v_unit (id TEXT, magic INTEGER, symbol TEXT,
                             timeframe TEXT, side TEXT, status TEXT);
        CREATE TABLE symbol_alias (alias TEXT, symbol TEXT);
        CREATE TABLE live_set (unit_id TEXT, risk_mode INTEGER,
                               risk_json TEXT, retired_at TEXT);
        """)
    con.executemany("INSERT INTO v_unit VALUES (?,?,?,?,?,?)", [
        ("u-eth-a", 120002, "ETH", "D", "both", "approved"),
        ("u-eth-c", 120001, "ETH", "D", "both", "candidate"),
        ("u-eth-s", 121002, "ETH", "D", "short", "candidate"),
        ("u-xag-a", 110001, "XAG", "D", "both", "approved"),
        ("u-xag-v", 110009, "XAG", "D", "both", "void"),
        # Approved, and unfireable: a magic is the only thing the copier
        # routes on. It must be REPORTED, not merely dropped.
        ("u-xag-nm", None, "XAG", "D", "both", "approved"),
    ])
    con.executemany("INSERT INTO symbol_alias VALUES (?,?)", [
        ("ETH", "ETH"), ("ETHUSD", "ETH"), ("XAG", "XAG"), ("XAGUSD", "XAG"),
    ])
    con.execute("INSERT INTO live_set VALUES ('u-eth-a', 1, '{\"RISK_LONG_A\": \"2.0\"}', NULL)")
    con.commit()
    con.close()
    return str(path)


def magics(rows):
    return sorted(r["magic"] for r in rows)


# ---------------- the symbol filter actually filters -------------------

def test_a_symbol_filter_excludes_the_other_symbols(gate, db):
    """The inert-filter regression. Asserted by COUNT, because the broken
    version returned a list that looked right."""
    rows = gate.rows_for(db, ("approved",), ("ETH",))
    assert magics(rows) == [120002]


def test_no_symbol_filter_still_means_every_symbol(gate, db):
    rows = gate.rows_for(db, ("approved",))
    assert magics(rows) == [110001, 120002]


def test_the_filter_is_case_insensitive_on_the_code(gate, db):
    assert magics(gate.rows_for(db, ("approved",), ("eth",))) == []
    # main() upper-cases what the user typed; rows_for compares upper to
    # what it was given, so a lower-case code here is correctly no match.
    assert magics(gate.rows_for(db, ("approved",), ("ETH",))) == [120002]


def test_several_symbols_may_be_asked_for(gate, db):
    rows = gate.rows_for(db, ("approved",), ("ETH", "XAG"))
    assert magics(rows) == [110001, 120002]


# ---------------- status, which the symbol filter must not disturb -----

def test_candidates_are_out_unless_asked_for(gate, db):
    assert magics(gate.rows_for(db, ("approved",), ("ETH",))) == [120002]
    assert magics(gate.rows_for(db, ("approved", "candidate"), ("ETH",))) == \
        [120001, 120002, 121002]


def test_a_void_unit_never_fires_however_it_is_asked_for(gate, db):
    """NEVER is not a default that a wider --status can widen past."""
    rows = gate.rows_for(db, ("approved", "candidate", "void"), ("XAG",))
    assert 110009 not in magics(rows)


# ---------------- what it leaves out, it says -------------------------

def test_an_approved_unit_with_no_magic_is_reported(gate, db, capsys):
    gate.rows_for(db, ("approved",))
    assert "XAG" in capsys.readouterr().err


def test_but_not_when_that_symbol_was_excluded_on_purpose(gate, db, capsys):
    """Noise in a go-live gate is what makes a real warning easy to miss."""
    gate.rows_for(db, ("approved",), ("ETH",))
    assert "no magic" not in capsys.readouterr().err


# ---------------- the live set is what the gate should size from -------

def test_a_recorded_live_set_is_the_row_s_source(gate, db):
    row, = gate.rows_for(db, ("approved",), ("ETH",))
    assert row["source"] == "live_set"
    assert row["risk_mode"] == 1


def test_without_one_it_says_it_fell_back(gate, db):
    row, = gate.rows_for(db, ("approved",), ("XAG",))
    assert row["source"] == "gate_defaults"
    assert row["risk_mode"] == ""


def test_the_tradable_alias_is_used_not_the_registry_code(gate, db):
    """The EA trades ETHUSD; the registry calls it ETH. The gate list is
    read by MT4, so it carries the tradable name."""
    row, = gate.rows_for(db, ("approved",), ("ETH",))
    assert row["symbol"] == "ETHUSD"
