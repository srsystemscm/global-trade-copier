import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api.js";
import { useEvents } from "../useEvents.js";

/** Local midnight for a yyyy-mm-dd string, as epoch seconds. */
function dayBounds(isoDate) {
  const [y, m, d] = isoDate.split("-").map(Number);
  const start = new Date(y, m - 1, d, 0, 0, 0, 0).getTime() / 1000;
  return { start, end: start + 86400 };
}

function todayIso() {
  const now = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
}

function shiftIso(isoDate, days) {
  const [y, m, d] = isoDate.split("-").map(Number);
  const dt = new Date(y, m - 1, d + days);
  const pad = (n) => String(n).padStart(2, "0");
  return `${dt.getFullYear()}-${pad(dt.getMonth() + 1)}-${pad(dt.getDate())}`;
}

function time(ts) {
  return ts ? new Date(ts * 1000).toLocaleTimeString() : "";
}

function qty(v) {
  return v === null || v === undefined ? "--" : Number(v).toFixed(2);
}

/**
 * A price, at the precision it actually carries.
 *
 * One fixed width cannot serve this table: the same column holds XAUUSD at
 * 4029.32, XAGUSD at 48.2 and US30 at 44120, and padding them all to five
 * decimals turns an index level into 44120.00000.
 */
function px(v) {
  if (v === null || v === undefined) return "--";
  return Number(v).toFixed(5).replace(/\.?0+$/, "");
}

/**
 * One row per master order rather than one per signal.
 *
 * A single order arrives as several signals -- an OPEN, any number of
 * MODIFYs as the EA walks its stop, then a CLOSE -- and listing those
 * separately is the activity feed's job, not a summary's. Keyed by master
 * account as well as ticket: MT4 ticket numbers are per-account, so two
 * masters legitimately share ticket numbers and folding them together would
 * merge two different trades into one row.
 */
function toOrders(trades, { start, end }) {
  const byOrder = new Map();

  const inDay = trades.filter((t) => t.received_at >= start && t.received_at < end);
  // oldest first, so OPEN lands before the MODIFYs that follow it
  const ordered = [...inDay].sort((a, b) => a.received_at - b.received_at);

  for (const t of ordered) {
    const key = `${t.master_account}\u0000${t.master_ticket}`;
    let o = byOrder.get(key);
    if (!o) {
      o = {
        key,
        account: t.master_account,
        ticket: t.master_ticket,
        symbol: t.symbol,
        direction: null,
        lots: null,
        price: null,
        sl: null,
        tp: null,
        openedAt: null,
        closedAt: null,
        modifies: 0,
        copies: [],
      };
      byOrder.set(key, o);
    }

    if (t.action === "OPEN") {
      o.direction = t.direction;
      o.lots = t.lots;
      o.price = t.price;
      o.openedAt = t.received_at;
    }
    if (t.action === "MODIFY") o.modifies += 1;
    if (t.action === "CLOSE") o.closedAt = t.received_at;

    // The latest levels the master asked for. Taken from OPEN and MODIFY
    // only: a CLOSE carries whatever happened to be on the order and must
    // not overwrite them.
    //
    // null from a MODIFY is meaningful and must be kept -- that is how the
    // master says it REMOVED a level. Skipping nulls here left a removed
    // take-profit still showing in the column, so the view claimed a target
    // the position no longer had.
    if (t.action !== "CLOSE") {
      o.sl = t.sl ?? null;
      o.tp = t.tp ?? null;
    }
    if (t.slave_copies?.length) o.copies = t.slave_copies;
  }

  return [...byOrder.values()].sort(
    (a, b) => (b.openedAt ?? b.closedAt ?? 0) - (a.openedAt ?? a.closedAt ?? 0)
  );
}

/**
 * What the copy cost, signed so that positive always means worse.
 *
 * A BUY filled above the master paid for the hop; a SELL filled below it
 * did too. Without the sign flip a sample of these averages to roughly
 * nothing and the cost disappears.
 *
 * Only meaningful when both sides traded the same instrument, so it is not
 * shown for an autonomous leg -- a gold CFD price against a GC futures
 * price is a difference between contracts, not slippage.
 */
function slip(order, copy) {
  if (copy.master_price == null || copy.slave_price == null) return null;
  if (copy.slave_symbol !== order.symbol) return null;
  const raw = copy.slave_price - copy.master_price;
  return order.direction === "SELL" ? -raw : raw;
}

/**
 * Milliseconds is the right unit for a copy, which should land well inside
 * a second. Anything past ten seconds is a different kind of event -- a
 * stalled session, a retry storm -- and six digits of ms reads as noise
 * exactly when it most needs to be legible.
 */
function fmtLatency(ms) {
  return ms >= 10000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`;
}

/** Signal received by the hub to broker fill, in ms. */
function latency(order, copy) {
  if (copy.filled_at == null || order.openedAt == null) return null;
  return Math.round((copy.filled_at - order.openedAt) * 1000);
}

function OrderRow({ order }) {
  // An OPEN that produced no slave leg is the visible symptom of a refusal:
  // no route matched, the slave was paused, or its session was down. Worth
  // surfacing, because nothing else in this view would show it.
  const uncopied = order.openedAt !== null && order.copies.length === 0;

  return (
    <>
      <tr className={uncopied ? "copier-row copier-row-uncopied" : "copier-row"}>
        <td>{time(order.openedAt ?? order.closedAt)}</td>
        <td className="mono">{order.ticket}</td>
        <td>{order.symbol}</td>
        <td>{order.direction ?? "--"}</td>
        <td className="num">{qty(order.lots)}</td>
        <td className="num">{px(order.price)}</td>
        <td className="num">{px(order.sl)}</td>
        <td className="num">{px(order.tp)}</td>
        <td className="num">{order.modifies || ""}</td>
        <td>
          {order.closedAt ? (
            <span className="badge">closed</span>
          ) : order.openedAt ? (
            <span className="badge badge-ok">open</span>
          ) : (
            <span className="badge badge-warn">no open seen</span>
          )}
        </td>
        <td>
          {uncopied ? (
            <span className="badge badge-error">not copied</span>
          ) : (
            <div className="copier-legs">
              {order.copies.map((c) => (
                <div key={`${c.slave_id}-${c.slave_symbol}-${c.slave_ticket}`} className="copier-leg">
                  <span className="copier-leg-slave">{c.slave_id}</span>
                  <span className="copier-leg-sym">{c.slave_symbol}</span>
                  <span className="mono copier-leg-ticket">#{c.slave_ticket ?? "--"}</span>
                  <span className={c.status === "OPEN" ? "badge badge-ok" : "badge"}>
                    {c.status?.toLowerCase()}
                  </span>
                  {slip(order, c) !== null && (
                    <span
                      className={slip(order, c) > 0 ? "copier-slip pnl-negative" : "copier-slip pnl-positive"}
                      title="Fill vs the master's price, signed so positive means it cost you"
                    >
                      {slip(order, c) > 0 ? "+" : ""}
                      {px(slip(order, c))}
                    </span>
                  )}
                  {latency(order, c) !== null && (
                    <span className="copier-lat muted" title="Hub received the signal to broker fill">
                      {fmtLatency(latency(order, c))}
                    </span>
                  )}
                </div>
              ))}
            </div>
          )}
        </td>
      </tr>
    </>
  );
}

export default function Copier() {
  const [date, setDate] = useState(todayIso());
  const [trades, setTrades] = useState([]);
  const [error, setError] = useState(null);
  const { events } = useEvents();

  const bounds = useMemo(() => dayBounds(date), [date]);

  const refresh = useCallback(async () => {
    try {
      // Ask the hub for the day rather than a row count: a busy day across
      // several masters would silently fall off the end of a fixed limit.
      const res = await api.getTrades(2000, bounds.start);
      setTrades(res.trades);
      setError(null);
    } catch (err) {
      setError(err.message);
    }
  }, [bounds.start]);

  useEffect(() => {
    refresh();
    const interval = setInterval(refresh, 5000);
    return () => clearInterval(interval);
  }, [refresh]);

  useEffect(() => {
    if (events.length > 0) refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [events.length]);

  const orders = useMemo(() => toOrders(trades, bounds), [trades, bounds]);

  const byMaster = useMemo(() => {
    const groups = new Map();
    for (const o of orders) {
      if (!groups.has(o.account)) groups.set(o.account, []);
      groups.get(o.account).push(o);
    }
    return [...groups.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  }, [orders]);

  const totals = useMemo(() => {
    const legs = orders.reduce((n, o) => n + o.copies.length, 0);
    const uncopied = orders.filter((o) => o.openedAt !== null && o.copies.length === 0).length;
    const openNow = orders.filter((o) => o.openedAt !== null && !o.closedAt).length;
    return { orders: orders.length, legs, uncopied, openNow };
  }, [orders]);

  const isToday = date === todayIso();

  return (
    <div className="dashboard">
      <div className="dashboard-header">
        <h1>Copier</h1>
        <div className="copier-daypick">
          <button className="btn btn-small" onClick={() => setDate(shiftIso(date, -1))}>
            &lsaquo; Prev
          </button>
          <input type="date" value={date} onChange={(e) => setDate(e.target.value)} />
          <button
            className="btn btn-small"
            onClick={() => setDate(shiftIso(date, 1))}
            disabled={isToday}
          >
            Next &rsaquo;
          </button>
          {!isToday && (
            <button className="btn btn-small btn-primary" onClick={() => setDate(todayIso())}>
              Today
            </button>
          )}
        </div>
      </div>

      {error && <div className="banner banner-error">{error}</div>}

      <section className="panel copier-summary">
        <div className="master-stat">
          <span className="copier-stat-n">{totals.orders}</span>
          <span className="muted small">master orders</span>
        </div>
        <div className="master-stat">
          <span className="copier-stat-n">{totals.legs}</span>
          <span className="muted small">slave legs</span>
        </div>
        <div className="master-stat">
          <span className="copier-stat-n">{totals.openNow}</span>
          <span className="muted small">still open</span>
        </div>
        <div className="master-stat">
          <span className={totals.uncopied ? "copier-stat-n pnl-negative" : "copier-stat-n"}>
            {totals.uncopied}
          </span>
          <span className="muted small">not copied</span>
        </div>
      </section>

      {byMaster.length === 0 && (
        <p className="muted">
          No master orders on {date}
          {isToday ? " yet." : "."}
        </p>
      )}

      {byMaster.map(([account, group]) => (
        <section key={account || "(unattributed)"}>
          <h2>
            {account || <span className="muted">unattributed master</span>}{" "}
            <span className="muted small">
              {group.length} order{group.length === 1 ? "" : "s"} &rarr;{" "}
              {group.reduce((n, o) => n + o.copies.length, 0)} leg
              {group.reduce((n, o) => n + o.copies.length, 0) === 1 ? "" : "s"}
            </span>
          </h2>
          <div className="panel copier-table-wrap">
            <table className="positions-table copier-table">
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Ticket</th>
                  <th>Symbol</th>
                  <th>Dir</th>
                  <th className="num">Lots</th>
                  <th className="num">Price</th>
                  <th className="num">SL</th>
                  <th className="num">TP</th>
                  <th className="num" title="MODIFY signals from the master">
                    Mods
                  </th>
                  <th>State</th>
                  <th>Slave legs</th>
                </tr>
              </thead>
              <tbody>
                {group.map((o) => (
                  <OrderRow key={o.key} order={o} />
                ))}
              </tbody>
            </table>
          </div>
        </section>
      ))}
    </div>
  );
}
