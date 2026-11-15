"""The two slave bridges answer one adapter, so they owe the same answers.

MT4Adapter serves both MT4 and MT5 slaves -- there is no MT5Adapter. It
sends the same commands either way, so a command one bridge answers and
the other drops is a slave that works or doesn't depending on which
terminal it happens to be in, with nothing in the config to say so.

That had been the state for a while: SlaveBridge.mq5 reached 1.06 with
ACCOUNT, SYMBOLS and MARGIN while SlaveBridge.mq4 sat at 1.05 with none
of them, so an MT4 slave could not size by risk at all. Both are 1.07
now and both answer BARS.

Checked against the SOURCE rather than a running terminal, because that
is what a developer can check. Whether the EA on a chart is current is a
deployment question, and the handshake's bridge_version answers it --
see test_bridge_version.py.
"""
import re
from pathlib import Path

import pytest

# Every command MT4Adapter sends. Named here rather than scraped so that
# adding one to the adapter and not to the bridges fails loudly.
COMMANDS = ["PING", "OPEN", "MODIFY", "CLOSE", "POSITIONS",
            "ACCOUNT", "SYMBOLS", "MARGIN", "BARS"]


def _bridge(name):
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "bridge_ea" / name
        if candidate.exists():
            return candidate.read_text(encoding="utf-8", errors="replace")
    pytest.skip(f"{name} not found")


@pytest.fixture(scope="module")
def mq4():
    return _bridge("SlaveBridge.mq4")


@pytest.fixture(scope="module")
def mq5():
    return _bridge("SlaveBridge.mq5")


@pytest.mark.parametrize("command", COMMANDS)
def test_both_bridges_dispatch_every_command(command, mq4, mq5):
    for label, src in (("mq4", mq4), ("mq5", mq5)):
        assert f'cmd == "{command}"' in src, (
            f"SlaveBridge.{label} does not dispatch {command}, so an MT slave's "
            f"behaviour depends on which terminal it is in"
        )


def test_the_adapter_sends_nothing_the_bridges_cannot_answer():
    """The other direction: a command in the adapter and in neither
    bridge is a call that always fails."""
    from app.adapters import mt4 as adapter_mod
    src = Path(adapter_mod.__file__).read_text(encoding="utf-8")
    sent = set(re.findall(r'"cmd":\s*"([A-Z_]+)"', src))
    assert sent, "no commands found in the adapter at all -- check the pattern"
    assert sent <= set(COMMANDS), (
        f"the adapter sends {sorted(sent - set(COMMANDS))}, which this test "
        f"does not know about and the bridges may not answer"
    )


def test_both_bridges_report_the_same_version():
    """The adapter's recompile hint maps a command to a version number
    (BARS -> 1.07). That only works if a number means the same capability
    on both -- which is why the mq4 skipped 1.06."""
    versions = {}
    for name in ("SlaveBridge.mq4", "SlaveBridge.mq5"):
        src = _bridge(name)
        found = re.search(r'#define\s+BRIDGE_VERSION\s+"([\d.]+)"', src)
        assert found, f"{name} declares no BRIDGE_VERSION"
        versions[name] = found.group(1)
    assert len(set(versions.values())) == 1, (
        f"the bridges disagree on version: {versions}. The adapter's "
        f"recompile hint names one number per command and cannot carry two "
        f"numbering schemes."
    )


def test_the_version_matches_the_property():
    """Two lines, and a mismatch means the terminal's Experts list and the
    handshake disagree. This has been wrong before."""
    for name in ("SlaveBridge.mq4", "SlaveBridge.mq5"):
        src = _bridge(name)
        prop = re.search(r'#property\s+version\s+"([\d.]+)"', src)
        define = re.search(r'#define\s+BRIDGE_VERSION\s+"([\d.]+)"', src)
        assert prop and define, name
        assert prop.group(1) == define.group(1), (
            f"{name}: #property says {prop.group(1)} and BRIDGE_VERSION says "
            f"{define.group(1)}"
        )


def test_the_version_the_adapter_names_for_bars_is_the_one_shipped():
    """The hint told operators to recompile for a version. If the bridges
    move on and the hint does not, it sends them to the wrong file."""
    from app.adapters import mt4 as adapter_mod
    src = Path(adapter_mod.__file__).read_text(encoding="utf-8")
    hint = re.search(r'"BARS":\s*"([\d.]+)"', src)
    assert hint, "the adapter no longer names a version for BARS"
    shipped = re.search(r'#define\s+BRIDGE_VERSION\s+"([\d.]+)"',
                        _bridge("SlaveBridge.mq5")).group(1)
    assert hint.group(1) == shipped, (
        f"the adapter tells operators to recompile for {hint.group(1)} but the "
        f"bridge in the repository is {shipped}"
    )


def test_no_raw_control_character_sits_inside_a_char_literal():
    """SkipWhitespace in the mq4 compared against RAW tab and newline
    bytes instead of escape sequences, and line-ending normalisation had
    already collapsed its CR case into a second LF -- so carriage return
    stopped matching, silently.

    It did not bite because the only caller is json.dumps(), which emits
    no bare CR. It makes the file unreadable to every tool that is not the
    MQL compiler, and any checkout or editor can change what those
    literals mean.
    """
    for name in ("SlaveBridge.mq4", "SlaveBridge.mq5"):
        src = _bridge(name)
        for char, label in ((chr(9), "tab"), (chr(10), "LF"), (chr(13), "CR")):
            needle = "'" + char + "'"
            assert needle not in src, (
                f"{name} has a raw {label} inside a char literal -- write it as "
                f"an escape sequence, which survives line-ending normalisation"
            )


def test_bars_answers_the_same_shape_from_both():
    """The adapter parses one reply shape. Oldest-first and skipping the
    forming bar are the two decisions that fail without failing, so both
    bridges have to make them the same way."""
    for name in ("SlaveBridge.mq4", "SlaveBridge.mq5"):
        src = _bridge(name)
        # To the DEFINITION of the next function, not to the first mention
        # of its name -- HandleBars calls TimeframeFromName, so slicing on
        # the bare name cut the body off at the call.
        start = src.index("string HandleBars")
        end = src.index("TimeframeFromName(const string name)", start)
        body = src[start:end]

        assert "ArraySetAsSeries(rates, false)" in body, f"{name}: not oldest-first"
        # start_pos 1 skips the bar still forming. The cast differs between
        # the two (MQL4 needs ENUM_TIMEFRAMES explicitly), so match on the
        # arguments after the timeframe rather than on the whole call.
        assert re.search(r"CopyRates\(symbol,[^;]*?,\s*1,\s*count,\s*rates\)", body), (
            f"{name}: CopyRates does not start at bar 1, so the incomplete "
            f"current bar is in the ATR"
        )
        assert all(k in body for k in ('\\"h\\"', '\\"l\\"', '\\"c\\"')), (
            f"{name}: the bar keys are not h/l/c"
        )
