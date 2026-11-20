import { adapterLabel, slaveFacts, slaveRouting } from "../slaveFacts.js";

export default function SlaveCard({ slave, onTogglePause, onRemove }) {
  // "running" describes the engine OBJECT, and a stalled engine is still
  // an object. On 2026-11-16 four of them read running and connected
  // while consuming nothing at all, and this card said so for 37
  // minutes. `consume` is the only field that can tell the two apart, so
  // it OVERRIDES the badge rather than sitting quietly beside it.
  const consume = slave.consume;
  const stalled = slave.running && consume?.stalled;
  const statusBadge = stalled
    ? "NOT CONSUMING"
    : slave.paused ? "paused" : slave.running ? "running" : "stopped";
  const statusClass = stalled
    ? "badge-error"
    : slave.paused ? "badge-warn" : slave.running ? "badge-ok" : "badge-error";

  const connBadge = slave.connected === true ? "connected" : slave.connected === false ? "disconnected" : "checking...";
  const connClass = slave.connected === true ? "badge-ok" : slave.connected === false ? "badge-error" : "badge-warn";

  return (
    <div className="card slave-card">
      <div className="slave-card-header">
        <div>
          <div className="slave-name">{slave.name}</div>
          {/* The broker and what this slave trades on. It used to carry
              describe_mode()'s whole sentence -- "own stops from ATR, 10
              routed instruments" -- which wrapped to three lines on a
              tile and repeated the mode the name already gives. */}
          <div className="slave-meta" title="The adapter driving this slave. The MT4 adapter drives MT4 and MT5 terminals alike -- the bridge does not say which.">
            {adapterLabel(slave.broker_type)} &middot; {slaveRouting(slave)}
          </div>
        </div>
        <span className={`badge ${statusClass}`}>{statusBadge}</span>
      </div>

      {slave.running && (
        <div style={{ marginBottom: 8 }}>
          <span className={`badge ${connClass}`}>{connBadge}</span>
        </div>
      )}

      {/* Said in words, because the badge alone does not tell a reader
          that signals are being lost rather than merely delayed. */}
      {stalled && (
        <div className="slave-stalled" style={{ marginBottom: 8 }}>
          {consume.alive === false
            ? "This slave's copy loop has stopped and will not restart on its own. Every signal is being dropped. Restart the hub."
            : `${consume.queue_depth} signal(s) queued and nothing taken off the queue for ${consume.seconds_since_consumed ?? "?"}s.`}
        </div>
      )}

      {/* What this slave stakes per signal, as three facts rather than
          the sentence that used to run to four wrapped lines here. See
          slaveFacts.js -- the hub decides these, so the tile, the list
          and the Risk tab cannot drift apart. */}
      <SlaveFactRows slave={slave} />
      {/* Which masters this slave follows. With one master it reads
          "every master" and is noise; with four to eight it is the first
          thing worth knowing about a slave. */}
      {slave.master_filter_summary && (
        <div className="slave-masters" title="Masters this slave follows">
          {slave.master_filter_summary}
        </div>
      )}
      {slave.netting?.allow_opposing && (
        <div style={{ marginBottom: 6 }}>
          <span className="badge badge-warn" title="A signal opposing what this account holds is copied, not refused">
            opposing legs allowed
          </span>
        </div>
      )}

      <div className="slave-positions">
        {slave.positions_error && <div className="muted small">P&amp;L: {slave.positions_error}</div>}
        {slave.positions && slave.positions.length === 0 && (
          <div className="muted small">No open positions</div>
        )}
        {slave.positions && slave.positions.length > 0 && (
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
              {slave.positions.map((p) => (
                <tr key={p.symbol}>
                  <td>{p.symbol}</td>
                  <td>{p.quantity}</td>
                  <td>{formatNumber(p.average_price)}</td>
                  <td className={p.unrealized_pnl >= 0 ? "pnl-positive" : "pnl-negative"}>
                    {p.unrealized_pnl != null ? formatNumber(p.unrealized_pnl) : "--"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="slave-actions">
        <button className="btn btn-small" onClick={onTogglePause}>
          {slave.paused ? "Resume" : "Pause"}
        </button>
        <button className="btn btn-small btn-danger" onClick={onRemove}>
          Remove
        </button>
      </div>
    </div>
  );
}

/**
 * MODE / RISK / CAP, in that order, on three rows.
 *
 * Ordered the way the question is asked: where the size comes from, what
 * it is measured against, and where it stops. A value the hub does not
 * report reads "--" rather than being dropped, so a row that is missing
 * on one slave and present on another is visible as a difference.
 */
function SlaveFactRows({ slave }) {
  const facts = slaveFacts(slave);
  return (
    <div className="slave-facts" title="Where the size comes from, what it is measured against, and the refusal ceiling">
      <div className="slave-fact">
        <span className="slave-fact-k">Mode</span>
        <span className="slave-fact-v">{facts.mode || "--"}</span>
      </div>
      <div className="slave-fact">
        <span className="slave-fact-k">Risk</span>
        <span className="slave-fact-v">{facts.risk || "--"}</span>
      </div>
      <div className="slave-fact">
        <span className="slave-fact-k">Cap</span>
        <span className="slave-fact-v">{facts.cap || "--"}</span>
      </div>
    </div>
  );
}

function formatNumber(n) {
  return typeof n === "number" ? n.toFixed(2) : n;
}
