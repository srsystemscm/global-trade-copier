export default function MasterPanel({ status, wsConnected }) {
  if (!status) {
    return <div className="panel">Loading master status...</div>;
  }

  const heartbeats = Object.entries(status.master.last_heartbeat_by_account || {});
  const now = Date.now() / 1000;

  return (
    <div className="panel master-panel">
      <div className="master-stat">
        <span className="label">Hub uptime</span>
        <span className="value">{formatUptime(status.uptime_seconds)}</span>
      </div>
      <div className="master-stat">
        <span className="label">Signal ports</span>
        <span className="value">
          SUB :{status.master.sub_port} / PULL :{status.master.pull_port}
        </span>
      </div>
      <div className="master-stat">
        <span className="label">Live feed</span>
        <span className={`badge ${wsConnected ? "badge-ok" : "badge-error"}`}>
          {wsConnected ? "connected" : "disconnected"}
        </span>
      </div>
      <div className="master-stat">
        <span className="label">Master heartbeats</span>
        <span className="value">
          {heartbeats.length === 0 && <span className="muted">none yet</span>}
          {heartbeats.map(([account, ts]) => (
            <span key={account} className={`badge ${now - ts < 30 ? "badge-ok" : "badge-warn"}`}>
              {account} ({Math.round(now - ts)}s ago)
            </span>
          ))}
        </span>
      </div>
    </div>
  );
}

function formatUptime(seconds) {
  const s = Math.floor(seconds);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  return `${h}h ${m}m ${sec}s`;
}
