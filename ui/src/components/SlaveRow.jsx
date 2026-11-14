/**
 * One slave as a row, for when there are more of them than fit as tiles.
 *
 * The tile shows positions, which is most of its height and is also on the
 * Copier tab. A row shows the things that decide what a slave DOES -- how
 * it sizes, what it accepts, whether it is connected -- and keeps positions
 * behind a toggle. Six slaves already scroll; the plan is more.
 */
import { useState } from "react";

export default function SlaveRow({ slave, onTogglePause, onRemove }) {
  const [open, setOpen] = useState(false);

  const statusBadge = slave.paused ? "paused" : slave.running ? "running" : "stopped";
  const statusClass = slave.paused ? "badge-warn" : slave.running ? "badge-ok" : "badge-error";
  const connBadge =
    slave.connected === true ? "connected" : slave.connected === false ? "disconnected" : "--";
  const connClass =
    slave.connected === true ? "badge-ok" : slave.connected === false ? "badge-error" : "badge-warn";

  const positions = slave.positions;
  const scale = slave.risk_scale ?? 1;
  const byMagic = Object.keys(slave.risk_scale_by_magic || {}).length;

  return (
    <>
      <tr className="slave-row">
        <td>
          <div className="slave-name">{slave.name}</div>
          <div className="slave-meta">
            {slave.broker_type} &middot; {slave.mode}
          </div>
        </td>
        <td>
          <span className={`badge ${statusClass}`}>{statusBadge}</span>
        </td>
        <td>
          {slave.running ? <span className={`badge ${connClass}`}>{connBadge}</span> : <span className="muted">--</span>}
        </td>
        <td className="slave-row-masters">{slave.master_filter_summary}</td>
        <td className="slave-row-sizing">{slave.sizing_summary}</td>
        <td>
          {scale === 1 && byMagic === 0 ? (
            <span className="muted">full</span>
          ) : (
            <>
              {scale !== 1 && <span>x{scale}</span>}
              {byMagic > 0 && (
                <span className="badge" title="Strategies with their own scale on this slave">
                  {byMagic} per-EA
                </span>
              )}
            </>
          )}
        </td>
        <td>{slave.magic_filter_summary}</td>
        <td>
          {/* An error is worth a column of its own: on a tile it sits at
              the bottom where it is easy to miss. */}
          {slave.positions_error ? (
            <span className="badge badge-warn" title={slave.positions_error}>
              no answer
            </span>
          ) : positions ? (
            <button className="btn btn-small" onClick={() => setOpen((v) => !v)}>
              {positions.length} open
            </button>
          ) : (
            <span className="muted">--</span>
          )}
        </td>
        <td className="slave-row-actions">
          <button className="btn btn-small" onClick={onTogglePause}>
            {slave.paused ? "Resume" : "Pause"}
          </button>
          <button className="btn btn-small btn-danger" onClick={onRemove}>
            Remove
          </button>
        </td>
      </tr>

      {open && positions && positions.length > 0 && (
        <tr className="slave-row-detail">
          <td colSpan={9}>
            <table className="positions-table">
              <thead>
                <tr>
                  <th>Symbol</th>
                  <th>Qty</th>
                  <th>Avg</th>
                  <th>P&amp;L</th>
                </tr>
              </thead>
              <tbody>
                {positions.map((p) => (
                  <tr key={p.symbol}>
                    <td>{p.symbol}</td>
                    <td>{p.quantity}</td>
                    <td>{fmt(p.average_price)}</td>
                    <td className={p.unrealized_pnl >= 0 ? "pnl-positive" : "pnl-negative"}>
                      {p.unrealized_pnl != null ? fmt(p.unrealized_pnl) : "--"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </td>
        </tr>
      )}
      {open && positions && positions.length === 0 && (
        <tr className="slave-row-detail">
          <td colSpan={9}>
            <span className="muted small">No open positions</span>
          </td>
        </tr>
      )}
    </>
  );
}

function fmt(n) {
  return typeof n === "number" ? n.toFixed(2) : n;
}
