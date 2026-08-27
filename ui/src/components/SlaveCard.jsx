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
