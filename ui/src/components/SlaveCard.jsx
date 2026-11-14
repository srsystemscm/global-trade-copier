export default function SlaveCard({ slave, onTogglePause, onRemove }) {
  const statusBadge = slave.paused ? "paused" : slave.running ? "running" : "stopped";
  const statusClass = slave.paused ? "badge-warn" : slave.running ? "badge-ok" : "badge-error";

  const connBadge = slave.connected === true ? "connected" : slave.connected === false ? "disconnected" : "checking...";
  const connClass = slave.connected === true ? "badge-ok" : slave.connected === false ? "badge-error" : "badge-warn";

  return (
    <div className="card slave-card">
      <div className="slave-card-header">
        <div>
          <div className="slave-name">{slave.name}</div>
          <div className="slave-meta">
            {slave.broker_type} &middot; {slave.mode}
          </div>
        </div>
        <span className={`badge ${statusClass}`}>{statusBadge}</span>
      </div>

      {slave.running && (
        <div style={{ marginBottom: 8 }}>
          <span className={`badge ${connClass}`}>{connBadge}</span>
        </div>
      )}

      {/* What this slave stakes per signal. It decides every size on the
          card below, and used to appear on no screen at all for a mirror
          slave -- which is most of them. */}
      {slave.sizing_summary && (
        <div className="slave-sizing" title="Risk per signal">
          {slave.sizing_summary}
        </div>
      )}
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

function formatNumber(n) {
  return typeof n === "number" ? n.toFixed(2) : n;
}
