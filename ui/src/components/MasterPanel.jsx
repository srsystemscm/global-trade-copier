/**
 * Masters first, because a master is the thing a signal comes FROM.
 *
 * This used to be one strip of hub statistics with the heartbeats squeezed
 * into the last cell. That reads fine with one master and stops reading at
 * two, which is the direction this is going: fewer masters than slaves, but
 * more than one. A master now gets a row of its own, with the two facts
 * that say whether it is working -- when it last sent a heartbeat, and when
 * it last sent a signal. They are different questions: a terminal can be
 * alive and flat for hours.
 */
export default function MasterPanel({ status, trades }) {
  if (!status) {
    return <div className="panel">Loading master status...</div>;
  }

  const now = Date.now() / 1000;
  const heartbeats = status.master.last_heartbeat_by_account || {};

  // Last signal per master, folded out of the trade feed the page already
  // holds. A master with a heartbeat and no signals all session is a
  // different problem from one that has gone quiet entirely.
  const lastSignal = {};
  for (const t of trades || []) {
    const acct = t.master_account;
    if (!acct) continue;
    if (!lastSignal[acct] || t.received_at > lastSignal[acct].received_at) {
      lastSignal[acct] = t;
    }
  }

  // Every account we know of from either source. A master that has sent
  // signals but no heartbeat still belongs on this page.
  const accounts = Array.from(
    new Set([...Object.keys(heartbeats), ...Object.keys(lastSignal)]),
  ).sort();

  return (
    <section className="masters">
      {/* The section head carries what a master's signals arrive
          THROUGH -- how long this hub has been listening, and on which
          two ports. It sits here rather than on each card because it is
          one hub serving every master: printed per card it would repeat,
          and with four masters a repeated figure reads as four
          different ones. It was a panel of its own above the masters
          until 2026-11-17 and a strip in the page header until
          2026-11-18. */}
      <div className="section-head masters-head">
        <h2>Masters</h2>
        <div className="hub-state">
          <div className="hub-state-cell">
            <span className="hub-state-label">Hub uptime</span>
            <span className="hub-state-value">{formatUptime(status.uptime_seconds)}</span>
          </div>
          <div className="hub-state-cell">
            <span className="hub-state-label">Signal ports</span>
            <span className="hub-state-value">
              SUB :{status.master.sub_port} / PULL :{status.master.pull_port}
            </span>
          </div>
        </div>
      </div>
      {accounts.length === 0 && (
        <p className="muted">
          No master has reported yet. The bridge EA publishes a heartbeat as soon as it
          loads, so nothing here means nothing is attached.
        </p>
      )}

      <div className="master-grid">
        {accounts.map((account) => {
          const hb = heartbeats[account];
          const sig = lastSignal[account];
          const hbAge = hb == null ? null : now - hb;
          // 30s is the bridge's own heartbeat interval, so one missed beat
          // is a warning and two is a problem.
          const hbClass =
            hbAge == null ? "badge-error" : hbAge < 60 ? "badge-ok" : "badge-warn";

          return (
            <div className="card master-card" key={account}>
              <div className="master-card-head">
                <span className="master-account">{account}</span>
                <span className={`badge ${hbClass}`}>
                  {hbAge == null ? "no heartbeat" : `heartbeat ${formatAge(hbAge)}`}
                </span>
              </div>
              <div className="master-card-row">
                <span className="label">Last signal</span>
                <span className="value">
                  {sig ? (
                    <>
                      {sig.symbol} {sig.action}{" "}
                      <span className="muted">{formatAge(now - sig.received_at)}</span>
                    </>
                  ) : (
                    <span className="muted">none this session</span>
                  )}
                </span>
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
}

function formatAge(seconds) {
  const s = Math.max(0, Math.round(seconds));
  if (s < 90) return `${s}s ago`;
  const m = Math.round(s / 60);
  if (m < 90) return `${m}m ago`;
  const h = Math.round(m / 60);
  if (h < 48) return `${h}h ago`;
  return `${Math.round(h / 24)}d ago`;
}

function formatUptime(seconds) {
  const s = Math.floor(seconds || 0);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return `${h}h ${m}m ${s % 60}s`;
}
