import { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";
import ActivityFeed from "../components/ActivityFeed.jsx";
import ConnectionWizard from "../components/ConnectionWizard.jsx";
import MasterPanel from "../components/MasterPanel.jsx";
import SlaveCard from "../components/SlaveCard.jsx";
import SlaveRow from "../components/SlaveRow.jsx";
import { useEvents } from "../useEvents.js";

export default function Dashboard() {
  const [status, setStatus] = useState(null);
  const [trades, setTrades] = useState([]);
  const [wizardOpen, setWizardOpen] = useState(false);
  // Remembered per browser. Which view suits depends on how many slaves
  // someone runs, which is a property of the person, not of the data.
  const [view, setView] = useState(() => {
    try {
      return localStorage.getItem("gtc.slaveView") || "tiles";
    } catch {
      return "tiles";
    }
  });
  const [error, setError] = useState(null);
  const { events, connected } = useEvents();

  const refresh = useCallback(async () => {
    try {
      const [statusRes, tradesRes] = await Promise.all([api.getStatus(), api.getTrades(100)]);
      setStatus(statusRes);
      setTrades(tradesRes.trades);
      setError(null);
    } catch (err) {
      setError(err.message);
    }
  }, []);

  useEffect(() => {
    refresh();
    const interval = setInterval(refresh, 5000);
    return () => clearInterval(interval);
  }, [refresh]);

  // live events imply state changed (a trade landed, a slave errored) --
  // refresh the polled data so positions/activity stay in sync
  useEffect(() => {
    if (events.length > 0) refresh();
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
    refresh();
  }

  async function removeSlave(slave) {
    if (!window.confirm(`Remove slave "${slave.name}"?`)) return;
    await api.deleteSlave(slave.id);
    refresh();
  }

  return (
    <div className="dashboard">
      <div className="dashboard-header">
        <h1>Dashboard</h1>
        <button className="btn btn-primary" onClick={() => setWizardOpen(true)}>
          + Add Slave
        </button>
      </div>

      {error && <div className="banner banner-error">{error}</div>}

      <MasterPanel status={status} trades={trades} wsConnected={connected} />

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
                    <th>Risk scale</th>
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

      {wizardOpen && (
        <ConnectionWizard
          onClose={() => setWizardOpen(false)}
          onCreated={() => {
            setWizardOpen(false);
            refresh();
          }}
        />
      )}
    </div>
  );
}
