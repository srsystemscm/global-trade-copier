import { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";
import ActivityFeed from "../components/ActivityFeed.jsx";
import ConnectionWizard from "../components/ConnectionWizard.jsx";
import MasterPanel from "../components/MasterPanel.jsx";
import SlaveCard from "../components/SlaveCard.jsx";
import { useEvents } from "../useEvents.js";

export default function Dashboard() {
  const [status, setStatus] = useState(null);
  const [trades, setTrades] = useState([]);
  const [wizardOpen, setWizardOpen] = useState(false);
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

      <MasterPanel status={status} wsConnected={connected} />

      <section>
        <h2>Slaves</h2>
        <div className="slave-grid">
          {status?.slaves.map((slave) => (
            <SlaveCard
              key={slave.id}
              slave={slave}
              onTogglePause={() => togglePause(slave)}
              onRemove={() => removeSlave(slave)}
            />
          ))}
          {status && status.slaves.length === 0 && <p className="muted">No slaves configured yet.</p>}
        </div>
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
