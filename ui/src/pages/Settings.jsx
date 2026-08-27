import { useEffect, useState } from "react";
import { api } from "../api.js";
import ConnectionWizard from "../components/ConnectionWizard.jsx";

const TABS = ["General", "Master", "Slaves", "Risk", "Notifications", "Logs", "Export"];

export default function Settings() {
  const [tab, setTab] = useState("General");
  const [status, setStatus] = useState(null);
  const [config, setConfig] = useState({});
  const [error, setError] = useState(null);

  async function refresh() {
    try {
      const [statusRes, configRes] = await Promise.all([api.getStatus(), api.getConfig()]);
      setStatus(statusRes);
      setConfig(configRes.config);
      setError(null);
    } catch (err) {
      setError(err.message);
    }
  }

  useEffect(() => {
    refresh();
  }, []);

  return (
    <div className="settings">
      <h1>Settings</h1>
      {error && <div className="banner banner-error">{error}</div>}

      <div className="settings-tabs">
        {TABS.map((t) => (
          <button key={t} className={`settings-tab ${tab === t ? "active" : ""}`} onClick={() => setTab(t)}>
            {t}
          </button>
        ))}
      </div>

      {tab === "General" && <GeneralTab config={config} onChanged={refresh} />}
      {tab === "Master" && <MasterTab status={status} />}
      {tab === "Slaves" && <SlavesTab status={status} onChanged={refresh} />}
      {tab === "Risk" && <RiskTab status={status} />}
      {tab === "Notifications" && <NotificationsTab config={config} onChanged={refresh} />}
      {tab === "Logs" && <LogsTab />}
      {tab === "Export" && <ExportTab />}
    </div>
  );
}

function GeneralTab({ config, onChanged }) {
  const [key, setKey] = useState("");
  const [value, setValue] = useState("");
  const [saving, setSaving] = useState(false);

  async function save() {
    if (!key.trim()) return;
    setSaving(true);
    try {
      await api.patchConfig({ [key]: value });
      setKey("");
      setValue("");
      onChanged();
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="panel">
      <p className="muted small">
        General hub configuration is stored as free-form key/value pairs. This is the same store the hub reads
        for things like notification targets -- most sizing/risk settings live on the slave itself instead (see
        the Slaves and Risk tabs).
      </p>
      <table className="data-table">
        <thead>
          <tr>
            <th>Key</th>
            <th>Value</th>
          </tr>
        </thead>
        <tbody>
          {Object.entries(config).map(([k, v]) => (
            <tr key={k}>
              <td>{k}</td>
              <td>{v}</td>
            </tr>
          ))}
          {Object.keys(config).length === 0 && (
            <tr>
              <td colSpan={2} className="muted">
                No config values set yet.
              </td>
            </tr>
          )}
        </tbody>
      </table>

      <div className="form-grid" style={{ marginTop: 16, maxWidth: 360 }}>
        <label>
          Key
          <input value={key} onChange={(e) => setKey(e.target.value)} placeholder="e.g. hub_display_name" />
        </label>
        <label>
          Value
          <input value={value} onChange={(e) => setValue(e.target.value)} />
        </label>
        <button className="btn btn-primary" onClick={save} disabled={saving || !key.trim()}>
          {saving ? "Saving..." : "Save"}
        </button>
      </div>
    </div>
  );
}

function MasterTab({ status }) {
  if (!status) return <div className="panel">Loading...</div>;
  const autonomousSlaves = status.slaves.filter((s) => s.mode === "autonomous");

  return (
    <div className="panel">
      <h2 style={{ marginTop: 0 }}>ZMQ</h2>
      <table className="data-table">
        <tbody>
          <tr>
            <td>SUB port (trade signals)</td>
            <td>{status.master.sub_port}</td>
          </tr>
          <tr>
            <td>PULL port (heartbeats)</td>
            <td>{status.master.pull_port}</td>
          </tr>
        </tbody>
      </table>

      <h2>ATR config by slave</h2>
      <table className="data-table">
        <thead>
          <tr>
            <th>Slave</th>
            <th>ATR period</th>
          </tr>
        </thead>
        <tbody>
          {autonomousSlaves.map((s) => (
            <tr key={s.id}>
              <td>{s.name}</td>
              <td>{s.atr_period ?? "--"}</td>
            </tr>
          ))}
          {autonomousSlaves.length === 0 && (
            <tr>
              <td colSpan={2} className="muted">
                No autonomous-mode slaves configured.
              </td>
            </tr>
          )}
        </tbody>
      </table>
      <p className="muted small">
        ATR period is set per-slave (autonomous mode only) -- edit it from the Slaves tab or the Add Slave wizard.
      </p>
    </div>
  );
}

function SlavesTab({ status, onChanged }) {
  const [wizardOpen, setWizardOpen] = useState(false);

  async function togglePause(slave) {
    await api.patchSlave(slave.id, { paused: !slave.paused });
    onChanged();
  }

  async function removeSlave(slave) {
    if (!window.confirm(`Remove slave "${slave.name}"?`)) return;
    await api.deleteSlave(slave.id);
    onChanged();
  }

  return (
    <div className="panel">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <h2 style={{ marginTop: 0 }}>Slaves</h2>
        <button className="btn btn-primary btn-small" onClick={() => setWizardOpen(true)}>
          + Add Slave
        </button>
      </div>
      <table className="data-table">
        <thead>
          <tr>
            <th>Name</th>
            <th>Broker</th>
            <th>Mode</th>
            <th>Status</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {status?.slaves.map((s) => (
            <tr key={s.id}>
              <td>{s.name}</td>
              <td>{s.broker_type}</td>
              <td>{s.mode}</td>
              <td>{s.paused ? "paused" : s.running ? "running" : "stopped"}</td>
              <td>
                <button className="btn btn-small" onClick={() => togglePause(s)}>
                  {s.paused ? "Resume" : "Pause"}
                </button>{" "}
                <button className="btn btn-small btn-danger" onClick={() => removeSlave(s)}>
                  Remove
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {wizardOpen && (
        <ConnectionWizard
          onClose={() => setWizardOpen(false)}
          onCreated={() => {
            setWizardOpen(false);
            onChanged();
          }}
        />
      )}
    </div>
  );
}

function RiskTab({ status }) {
  const [risk, setRisk] = useState(null);
  const [error, setError] = useState(null);
  const [saving, setSaving] = useState(false);
  const autonomousSlaves = status?.slaves.filter((s) => s.mode === "autonomous") ?? [];

  async function load() {
    try {
      setRisk(await api.getRisk());
      setError(null);
    } catch (err) {
      setError(err.message);
    }
  }

  useEffect(() => {
    load();
  }, []);

  async function save(patch) {
    setSaving(true);
    try {
      setRisk(await api.patchRisk(patch));
      setError(null);
    } catch (err) {
      setError(err.message);
    } finally {
      setSaving(false);
    }
  }

  if (!risk) return <div className="panel">Loading...</div>;

  return (
    <>
      <div className="panel" style={{ marginBottom: 16 }}>
        <h2 style={{ marginTop: 0 }}>Kill switch</h2>
        <p className="muted small">
          Stops every slave from copying immediately. Master signals are still logged for audit, just never fanned
          out. Existing open positions are untouched -- this blocks new activity, it doesn't close anything.
        </p>
        {error && <div className="banner banner-error">{error}</div>}
        <button
          className={`btn ${risk.kill_switch_enabled ? "btn-danger" : "btn-primary"}`}
          disabled={saving}
          onClick={() => save({ kill_switch_enabled: !risk.kill_switch_enabled })}
        >
          {risk.kill_switch_enabled ? "Kill switch ON -- click to disable" : "Kill switch off -- click to enable"}
        </button>
      </div>

      <div className="panel" style={{ marginBottom: 16 }}>
        <h2 style={{ marginTop: 0 }}>Trading hours</h2>
        <p className="muted small">
          UTC window during which new positions may be opened. Existing positions can always be modified/closed
          regardless of this window.
        </p>
        <label style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 12 }}>
          <input
            type="checkbox"
            checked={risk.trading_hours_enabled}
            onChange={(e) => save({ trading_hours_enabled: e.target.checked })}
          />
          Enabled
        </label>
        <div className="form-grid" style={{ maxWidth: 300 }}>
          <label>
            Start (UTC)
            <input
              type="time"
              value={risk.trading_hours_start}
              onChange={(e) => setRisk({ ...risk, trading_hours_start: e.target.value })}
              onBlur={(e) => save({ trading_hours_start: e.target.value })}
            />
          </label>
          <label>
            End (UTC)
            <input
              type="time"
              value={risk.trading_hours_end}
              onChange={(e) => setRisk({ ...risk, trading_hours_end: e.target.value })}
              onBlur={(e) => save({ trading_hours_end: e.target.value })}
            />
          </label>
        </div>
      </div>

      <div className="panel">
        <h2 style={{ marginTop: 0 }}>Per-slave sizing &amp; risk management</h2>
        <p className="muted small">
          Autonomous mode only. Edit from the Slaves tab or the Add Slave wizard; this view is read-only.
        </p>
        {autonomousSlaves.length === 0 && <p className="muted">No autonomous-mode slaves configured.</p>}
        {autonomousSlaves.map((s) => (
          <div key={s.id} style={{ marginBottom: 16 }}>
            <strong>{s.name}</strong>
            <pre className="review-json">{JSON.stringify({ sizing: s.sizing, risk_management: s.risk_management }, null, 2)}</pre>
          </div>
        ))}
      </div>
    </>
  );
}

function NotificationsTab({ config, onChanged }) {
  const [email, setEmail] = useState(config.notify_email || "");
  const [webhook, setWebhook] = useState(config.notify_webhook_url || "");
  const [saving, setSaving] = useState(false);

  async function save() {
    setSaving(true);
    try {
      await api.patchConfig({ notify_email: email, notify_webhook_url: webhook });
      onChanged();
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="panel">
      <p className="muted small">
        Notifications fire on trade close, drawdown alerts, and slave/master disconnect-reconnect. Email requires
        SMTP settings configured via environment variables (TC_SMTP_HOST etc, see hub/README.md); if unset, email
        is silently skipped and only the webhook fires.
      </p>
      <div className="form-grid" style={{ maxWidth: 420 }}>
        <label>
          Notification email
          <input value={email} onChange={(e) => setEmail(e.target.value)} placeholder="you@example.com" />
        </label>
        <label>
          Webhook URL
          <input value={webhook} onChange={(e) => setWebhook(e.target.value)} placeholder="https://..." />
        </label>
        <button className="btn btn-primary" onClick={save} disabled={saving}>
          {saving ? "Saving..." : "Save"}
        </button>
      </div>
    </div>
  );
}

function LogsTab() {
  const [lines, setLines] = useState([]);
  const [error, setError] = useState(null);

  async function load() {
    try {
      const res = await api.getLogs(300);
      setLines(res.lines);
      setError(null);
    } catch (err) {
      setError(err.message);
    }
  }

  useEffect(() => {
    load();
    const interval = setInterval(load, 5000);
    return () => clearInterval(interval);
  }, []);

  return (
    <div className="panel">
      {error && <div className="banner banner-error">{error}</div>}
      <pre className="review-json" style={{ maxHeight: 480 }}>
        {lines.length === 0 ? "No log lines yet." : lines.join("\n")}
      </pre>
    </div>
  );
}

function ExportTab() {
  const [limit, setLimit] = useState(1000);
  const [busy, setBusy] = useState(false);

  async function exportCsv() {
    setBusy(true);
    try {
      const { trades } = await api.getTrades(limit);
      const header = ["id", "master_ticket", "symbol", "action", "direction", "lots", "price", "sl", "tp", "signal_ts", "received_at"];
      const rows = trades.map((t) => header.map((h) => t[h] ?? "").join(","));
      const csv = [header.join(","), ...rows].join("\n");
      const blob = new Blob([csv], { type: "text/csv" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "trades.csv";
      a.click();
      URL.revokeObjectURL(url);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="panel">
      <p className="muted small">Exports the master trade log (not per-slave copies) as a CSV file.</p>
      <div className="form-grid" style={{ maxWidth: 260 }}>
        <label>
          Max rows
          <input type="number" value={limit} onChange={(e) => setLimit(e.target.value)} />
        </label>
        <button className="btn btn-primary" onClick={exportCsv} disabled={busy}>
          {busy ? "Exporting..." : "Export CSV"}
        </button>
      </div>
    </div>
  );
}
