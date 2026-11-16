"""How one entry's size is cut into the orders that go out.

Separate from the adapter because it is arithmetic, not broker work, and
because getting it wrong is expensive in a way that is easy to miss: a
slice of 8.75 contracts is refused outright, and slices that do not sum
to the whole leave the position smaller than it was sized to be, which
nothing downstream would notice.

The slices are placed over a window, not all at once -- the reason for
splitting at all -- and they collapse into ONE tracked position. See
IBKRAdapter.open().
"""
from typing import List


def split_sizes(total: float, slices: int) -> List[float]:
    """`total` in `slices` parts, summing to exactly `total`.

    Cut in whole units of the size's own granularity. An order for 35
    contracts splits into contracts -- 9, 9, 9, 8 -- never into 8.75 of
    one, which IBKR refuses. An order written in lots (0.13) splits in
    hundredths, because that is the step MT accounts trade in.

    The LARGER slices go first. If the window is cut short -- the master
    closes, the session ends, the hub restarts -- more of the intended
    size is already working than if the remainder had been left to last.

    A total too small to go round comes back as FEWER, larger slices
    rather than as slices of zero: 3 contracts asked to be split five
    ways is three orders of one, because a zero-quantity order is a
    rejection and splitting is not worth a rejection.
    """
    total = float(total)
    if slices <= 1 or total <= 0:
        return [total]

    # The granularity to cut in. An integral size is a contract or share
    # count; anything else is a lot size, which MT quotes to hundredths.
    step = 1.0 if total.is_integer() else 0.01
    units = int(round(total / step))
    slices = min(int(slices), units)
    if slices <= 1:
        return [total]

    base, extra = divmod(units, slices)
    sizes = [round((base + (1 if i < extra else 0)) * step, 6)
             for i in range(slices)]
    # Converting back out of units can drift in the last decimal. The
    # last slice absorbs it, so the parts still add up to the size that
    # was risk-sized -- not to something a hair under it.
    sizes[-1] = round(total - sum(sizes[:-1]), 6)
    return sizes


def resolve_split(route_slices, route_interval, slave_slices, slave_interval):
    """The split in force for one entry, as (slices, interval).

    A route's own setting wins, the slave's is the fallback, and off is
    the default -- the same shape as tif and entry_timeout, so a slave
    with nothing configured behaves exactly as it did before any of this
    existed.

    `(1, _)` and `(0, _)` are both off. So is any number of slices with
    no interval: slices placed with no time between them are one order
    sent in pieces, which costs commission per piece and achieves
    nothing, and it is far more likely to be an unfinished config than an
    intention.
    """
    slices = route_slices if route_slices is not None else slave_slices
    interval = route_interval if route_interval is not None else slave_interval
    slices = int(slices or 0)
    interval = float(interval or 0.0)
    if slices <= 1 or interval <= 0:
        return 0, 0.0
    return slices, interval
