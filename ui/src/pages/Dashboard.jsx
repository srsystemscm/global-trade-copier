import { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";
import ActivityFeed from "../components/ActivityFeed.jsx";
import MasterPanel from "../components/MasterPanel.jsx";
import SlaveCard from "../components/SlaveCard.jsx";
import SlaveRow from "../components/SlaveRow.jsx";
import { useHub } from "../hub.jsx";

export default function Dashboard() {
  // Status and the event socket are shared with the header, which shows
  // hub state on every tab. See hub.jsx: /status asks every adapter for
  // its positions, so a second poller here would have doubled that.
  const { status, error: statusError, refresh: refreshStatus, events } = useHub();
  const [trades, setTrades] = useState([]);
  const [error, setError] = useState(null);
  // Remembered per browser. Which view suits depends on how many slaves
  // someone runs, which is a property of the person, not of the data.
  const [view, setView] = useState(() => {
    try {
      return localStorage.getItem("gtc.slaveView") || "tiles";
    } catch {
      return "tiles";
    }
  });

  const refreshTrades = useCallback(async () => {
    try {
      const res = await api.getTrades(100);
      setTrades(res.trades);
      setError(null);
    } catch (err) {
      setError(err.message);
    }
  }, []);

  useEffect(() => {
    refreshTrades();
    const interval = setInterval(refreshTrades, 5000);
    return () => clearInterval(interval);
  }, [refreshTrades]);

  // A live event means a trade landed or a slave errored, so the feed
  // below is stale the moment it arrives rather than five seconds later.
  useEffect(() => {
    if (events.length > 0) refreshTrades();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [events.length]);

  function chooseView(next) {
    setView(next);
    try {
      localStorage.setItem("gtc.slaveView", next);
    } catch {
      // A browser that refuses storage still gets the toggle, it just
      // forgets it on reload. Not a reason to fail.
    }
  }

  async function togglePause(slave) {
    await api.patchSlave(slave.id, { paused: !slave.paused });
    refreshStatus();
  }

  async function removeSlave(slave) {
    if (!window.confirm(`Remove slave "${slave.name}"?`)) return;
    await api.deleteSlave(slave.id);
    refreshStatus();
  }

  const shown = error || statusError;

  return (
    <div className="dashboard">
      {/* NO ADD-SLAVE BUTTON. Adding one is a Settings job done once per
          account, and it sat on the dashboard's title row costing a
          band of space on the page watched all day. The wizard is on
          Settings -> Slaves, which is where the rest of a slave's
          configuration already lives. */}
      <div className="dashboard-header">
        <h1>Dashboard</h1>
      </div>

      {shown && <div className="banner banner-error">{shown}</div>}

      <MasterPanel status={status} trades={trades} />

      <section>
        <div className="section-head">
          <h2>Slaves</h2>
          <div className="view-toggle">
            <button
              className={`btn btn-small ${view === "tiles" ? "active" : ""}`}
              onClick={() => chooseView("tiles")}
            >
              Tiles
            </button>
            <button
              className={`btn btn-small ${view === "list" ? "active" : ""}`}
              onClick={() => chooseView("list")}
            >
              List
            </button>
          </div>
        </div>

        {status && status.slaves.length === 0 && <p className="muted">No slaves configured yet.</p>}

        {view === "tiles" ? (
          <div className="slave-grid">
            {status?.slaves.map((slave) => (
              <SlaveCard
                key={slave.id}
                slave={slave}
                onTogglePause={() => togglePause(slave)}
                onRemove={() => removeSlave(slave)}
              />
            ))}
          </div>
        ) : (
          status?.slaves.length > 0 && (
            <div className="table-wrap">
              <table className="positions-table slave-table">
                <thead>
                  <tr>
                    <th>Slave</th>
                    <th>State</th>
                    <th>Link</th>
                    <th>Masters</th>
                    <th>Sizing</th>
                    {/* Risk calibration, named as Chris names it. It is
                        not a size -- it is the ratio between what the EA
                        signalled and what this slave takes of it. */}
                    <th title="Risk calibration: what this slave takes of what the EA signalled">RC</th>
                    <th>Signals</th>
                    <th>Positions</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {status.slaves.map((slave) => (
                    <SlaveRow
                      key={slave.id}
                      slave={slave}
                      onTogglePause={() => togglePause(slave)}
                      onRemove={() => removeSlave(slave)}
                    />
                  ))}
                </tbody>
              </table>
            </div>
          )
        )}
      </section>

      <section>
        <h2>Activity</h2>
        <ActivityFeed liveEvents={events} trades={trades} />
      </section>
    </div>
  );
}
