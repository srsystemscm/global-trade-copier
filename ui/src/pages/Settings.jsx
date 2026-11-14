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
      {tab === "Risk" && <RiskTab status={status} onChanged={refresh} />}
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

function RiskTab({ status, onChanged }) {
  const [risk, setRisk] = useState(null);
  const [error, setError] = useState(null);
  const [saving, setSaving] = useState(false);
  // Every slave, not only the autonomous ones. Mirror slaves had no
  // sizing of their own when this view was written; one with a sizing
  // block now risks a share of its OWN balance per signal, so leaving
  // them out hid the setting that decides what gets traded.
  const slaves = status?.slaves ?? [];
  // Every master the hub has heard from. Offered in the editor because
  // with four to eight of them, a typed account number is a typo waiting
  // to silently stop a slave trading.
  const knownAccounts = Object.keys(status?.master?.last_heartbeat_by_account || {});

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
        <h2 style={{ marginTop: 0 }}>Per-slave sizing &amp; risk</h2>
        <p className="muted small">
          How much each slave stakes per signal, and what would stop it trading at all.
          Every slave is listed -- mirror slaves size from their own balance too when they
          carry a sizing block.
        </p>
        {slaves.length === 0 && <p className="muted">No slaves configured.</p>}
        {slaves.map((s) => (
          <SlaveRisk
            key={s.id}
            slave={s}
            knownAccounts={knownAccounts}
            onSaved={onChanged}
          />
        ))}
      </div>
    </>
  );
}

function SlaveRisk({ slave, knownAccounts, onSaved }) {
  const [editing, setEditing] = useState(false);
  const [editingMagic, setEditingMagic] = useState(false);
  const [editingMasters, setEditingMasters] = useState(false);
  const [editingScale, setEditingScale] = useState(false);
  const [editingRoll, setEditingRoll] = useState(false);
  const [showRoutes, setShowRoutes] = useState(false);

  return (
    <div className="slave-risk">
      <div className="slave-risk-head">
        <div>
          <strong>{slave.name}</strong>{" "}
          <span className="muted small">
            {slave.broker_type} &middot; {slave.mode}
          </span>
          <div className="slave-risk-sizing">{slave.sizing_summary}</div>
        </div>
        {!slave.sizing_per_route && (
          <button className="btn btn-small" onClick={() => setEditing((v) => !v)}>
            {editing ? "Cancel" : "Edit"}
          </button>
        )}
      </div>

      <div className="slave-risk-flags">
        {slave.netting?.allow_opposing && (
          <span className="badge badge-warn" title="A signal opposing what this account already holds is copied rather than refused">
            opposing legs allowed
          </span>
        )}
        {slave.netting?.max_legs != null && (
          <span className="badge">max {slave.netting.max_legs} legs/symbol</span>
        )}
        {slave.netting?.max_aggregate_risk_pct != null && (
          <span className="badge">
            {(slave.netting.max_aggregate_risk_pct * 100).toFixed(1)}% aggregate/symbol
          </span>
        )}
        {slave.margin?.max_pct_of_free != null && (
          <span className="badge">
            margin ceiling {(slave.margin.max_pct_of_free * 100).toFixed(0)}% of free
          </span>
        )}
      </div>

      {slave.sizing_per_route && (
        <p className="muted small">
          Sizing is set on each of those individually, not on the slave as a whole, so there is
          nothing to edit here -- a slave-level value would save cleanly and never be read.
        </p>
      )}

      {editing && <SlaveRiskForm slave={slave} onDone={() => { setEditing(false); onSaved && onSaved(); }} />}

      {/* Asked before the magic, because that is the order the bus
          asks in: magic numbers are per-master and collide across them. */}
      <div className="slave-risk-row">
        <div>
          <span className="muted small">Masters followed</span>
          <div className="slave-risk-sizing">{slave.master_filter_summary}</div>
        </div>
        <button className="btn btn-small" onClick={() => setEditingMasters((v) => !v)}>
          {editingMasters ? "Cancel" : "Edit"}
        </button>
      </div>
      {editingMasters && (
        <MasterFilterForm
          slave={slave}
          knownAccounts={knownAccounts}
          onDone={() => { setEditingMasters(false); onSaved && onSaved(); }}
        />
      )}

      {/* Which signals reach this slave at all. A filter decides that
          before sizing ever runs, so it belongs above the routes. */}
      <div className="slave-risk-row">
        <div>
          <span className="muted small">Strategies accepted</span>
          <div className="slave-risk-sizing">{slave.magic_filter_summary}</div>
        </div>
        <button className="btn btn-small" onClick={() => setEditingMagic((v) => !v)}>
          {editingMagic ? "Cancel" : "Edit"}
        </button>
      </div>
      {editingMagic && (
        <MagicFilterForm
          slave={slave}
          onDone={() => { setEditingMagic(false); onSaved && onSaved(); }}
        />
      )}

      {/* Per-EA risk. The strategies are sized for a CFD account; the
          futures and ETF legs have far less leverage, so the same signal
          is a different amount of risk once it lands there. */}
      <div className="slave-risk-row">
        <div>
          <span className="muted small">Risk per strategy</span>
          <div className="slave-risk-sizing">
            {describeScale(slave)}
          </div>
        </div>
        <button className="btn btn-small" onClick={() => setEditingScale((v) => !v)}>
          {editingScale ? "Cancel" : "Edit"}
        </button>
      </div>
      {editingScale && (
        <RiskScaleForm
          slave={slave}
          onDone={() => { setEditingScale(false); onSaved && onSaved(); }}
        />
      )}

      {/* Rolling is two market orders per position when it fires, and it
          fires on its own schedule. Running invisibly is the problem. */}
      {(slave.contract_roll || slave.mode === "autonomous") && (
        <div className="slave-risk-row">
          <div>
            <span className="muted small">Contract roll</span>
            <div className="slave-risk-sizing">
              {slave.contract_roll?.enabled
                ? `on -- rolls when the next month out-trades the front by ${
                    slave.contract_roll.min_volume_ratio ?? 1
                  }x, checked every ${
                    (slave.contract_roll.check_interval_seconds ?? 3600) / 60
                  } min`
                : "off -- positions are never rolled to the next contract"}
            </div>
          </div>
          <button className="btn btn-small" onClick={() => setEditingRoll((v) => !v)}>
            {editingRoll ? "Cancel" : "Edit"}
          </button>
        </div>
      )}
      {editingRoll && (
        <ContractRollForm
          slave={slave}
          onDone={() => { setEditingRoll(false); onSaved && onSaved(); }}
        />
      )}

      {slave.routes?.length > 0 && (
        <div className="slave-risk-row">
          <button className="btn btn-small" onClick={() => setShowRoutes((v) => !v)}>
            {showRoutes ? "Hide" : "Show"} {slave.routes.length} route
            {slave.routes.length === 1 ? "" : "s"}
          </button>
        </div>
      )}
      {showRoutes && <RouteTable routes={slave.routes} />}
    </div>
  );
}

function RouteTable({ routes }) {
  return (
    <div className="table-wrap">
      <table className="positions-table route-table">
        <thead>
          <tr>
            <th>Master</th>
            <th>Magic</th>
            <th>Target</th>
            <th>Dir</th>
            <th>Order</th>
            <th>Month</th>
            <th>Stop held by</th>
            <th>Sizing</th>
          </tr>
        </thead>
        <tbody>
          {routes.map((r, i) => (
            <tr key={`${r.symbol}-${r.target.join("/")}-${i}`}>
              <td>{r.symbol}</td>
              <td>{r.magic ?? <span className="muted">any</span>}</td>
              <td>
                {r.target.join(" / ")}
                {r.target.length > 1 && (
                  <span className="muted small"> (first that resolves)</span>
                )}
              </td>
              <td>
                {r.direction ?? <span className="muted">both</span>}
                {r.target_direction && r.target_direction !== r.direction && (
                  <span className="muted small"> &rarr;{r.target_direction}</span>
                )}
              </td>
              <td>
                {r.order_type}
                {r.outside_rth && <span className="badge badge-warn">outside RTH</span>}
              </td>
              <td>{r.month_slot ?? <span className="muted">--</span>}</td>
              <td>
                {r.broker_brackets === false ? (
                  <span className="badge badge-warn" title="No resting stop at the broker -- the copier closes this leg itself, so it is unprotected while the hub is down">
                    the copier
                  </span>
                ) : (
                  "broker"
                )}
              </td>
              <td>{r.sizing}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="muted small">
        Read-only for now. These decide what a signal becomes -- which contract month, who holds
        the stop, whether it may work outside regular hours.
      </p>
    </div>
  );
}

function describeScale(slave) {
  const byMagic = slave.risk_scale_by_magic || {};
  const entries = Object.entries(byMagic);
  const base = slave.risk_scale ?? 1;
  const baseText =
    base === 1 ? "every strategy at full target risk" : `all strategies x${base}`;
  if (entries.length === 0) return baseText;
  const listed = entries
    .map(([magic, scale]) => `${magic} x${scale}`)
    .join(", ");
  return `${baseText}; ${listed}`;
}

function RiskScaleForm({ slave, onDone }) {
  const [full, setFull] = useState(null);
  const [base, setBase] = useState(1);
  const [rows, setRows] = useState([]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  useEffect(() => {
    (async () => {
      try {
        const got = await api.getSlave(slave.id);
        setFull(got);
        setBase(got.config.risk_scale ?? 1);
        const table = got.config.risk_scale_by_magic || {};
        setRows(
          Object.entries(table).map(([magic, scale]) => ({
            magic: String(magic),
            scale: String(scale),
          })),
        );
      } catch (e) {
        setErr(e.message);
      }
    })();
  }, [slave.id]);

  function setRow(i, patch) {
    setRows(rows.map((r, j) => (i === j ? { ...r, ...patch } : r)));
  }

  async function save() {
    setBusy(true);
    setErr(null);
    try {
      const table = {};
      for (const r of rows) {
        const magic = r.magic.trim();
        if (!magic) continue;
        if (!/^\d+$/.test(magic)) throw new Error(`"${magic}" is not a magic number`);
        const scale = Number(r.scale);
        // 0 is a real instruction -- do not trade this strategy here --
        // so it is allowed. Blank and negative are not.
        if (r.scale === "" || Number.isNaN(scale) || scale < 0) {
          throw new Error(`scale for magic ${magic} must be 0 or more`);
        }
        if (magic in table) throw new Error(`magic ${magic} is listed twice`);
        table[magic] = scale;
      }

      const config = { ...full.config };
      if (Number(base) === 1) delete config.risk_scale;
      else config.risk_scale = Number(base);
      if (Object.keys(table).length === 0) delete config.risk_scale_by_magic;
      else config.risk_scale_by_magic = table;

      await api.patchSlave(slave.id, { config });
      onDone();
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  }

  if (err && !full) return <div className="banner banner-error">{err}</div>;
  if (!full) return <p className="muted small">Loading...</p>;

  return (
    <div className="slave-risk-form">
      {err && <div className="banner banner-error">{err}</div>}

      <label>
        All strategies on this slave
        <input
          type="number"
          step="0.05"
          min="0"
          value={base}
          onChange={(e) => setBase(e.target.value)}
        />
      </label>

      <div className="scale-rows">
        <div className="scale-head">
          <span>Magic</span>
          <span>Scale</span>
          <span />
        </div>
        {rows.map((r, i) => (
          <div className="scale-row" key={i}>
            <input
              value={r.magic}
              placeholder="1001"
              onChange={(e) => setRow(i, { magic: e.target.value })}
            />
            <input
              type="number"
              step="0.05"
              min="0"
              value={r.scale}
              placeholder="1.0"
              onChange={(e) => setRow(i, { scale: e.target.value })}
            />
            <button
              className="btn btn-small btn-danger"
              onClick={() => setRows(rows.filter((_, j) => j !== i))}
            >
              Remove
            </button>
          </div>
        ))}
        <button
          className="btn btn-small"
          onClick={() => setRows([...rows, { magic: "", scale: "1" }])}
        >
          + Add strategy
        </button>
      </div>

      <p className="muted small">
        1.0 is the strategy&apos;s own target risk, unchanged. The origin EAs each risk a
        fixed share of the account they run on, so 1.0 on a futures or ETF slave means the
        same share of THAT account -- which is usually too much, because there is far less
        leverage there than on the CFD account the strategy was sized for. 0 means do not
        trade this strategy on this slave at all.
      </p>
      <p className="muted small">
        A route that sets its own scale wins over this table, and this table wins over the
        slave-wide value above it.
      </p>

      <div className="slave-actions">
        <button className="btn btn-small" onClick={save} disabled={busy}>
          {busy ? "Saving..." : "Save"}
        </button>
      </div>
    </div>
  );
}

function MasterFilterForm({ slave, knownAccounts, onDone }) {
  const [full, setFull] = useState(null);
  const [allowRows, setAllowRows] = useState([]);
  const [denyRows, setDenyRows] = useState([]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  useEffect(() => {
    (async () => {
      try {
        const got = await api.getSlave(slave.id);
        const mf = got.config.master_filter;
        setFull(got);
        const asText = (list) =>
          (list || []).map((e) => String(e && e.account != null ? e.account : e));
        if (Array.isArray(mf)) setAllowRows(asText(mf));
        else if (mf && typeof mf === "object") {
          setAllowRows(asText(mf.allow));
          setDenyRows(asText(mf.deny));
        }
      } catch (e) {
        setErr(e.message);
      }
    })();
  }, [slave.id]);

  function clean(rows, where) {
    const out = [];
    for (const raw of rows) {
      const t = String(raw).trim();
      if (!t) continue;
      if (out.includes(t)) throw new Error(`master ${t} is listed twice in ${where}`);
      out.push(t);
    }
    return out;
  }

  async function save() {
    setBusy(true);
    setErr(null);
    try {
      const allow = clean(allowRows, "the follow list");
      const deny = clean(denyRows, "the never-follow list");
      const clash = allow.filter((a) => deny.includes(a));
      if (clash.length) {
        throw new Error(
          `master ${clash.join(", ")} is in both lists -- deny wins, so it would never trade`,
        );
      }

      const config = { ...full.config };
      if (allow.length === 0 && deny.length === 0) {
        delete config.master_filter;
      } else {
        const next = {};
        // An empty allow-list is not "no allow-list": it would stop every
        // signal. Only send the key when it has members.
        if (allow.length) next.allow = allow;
        if (deny.length) next.deny = deny;
        config.master_filter = next;
      }
      await api.patchSlave(slave.id, { config });
      onDone();
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  }

  if (err && !full) return <div className="banner banner-error">{err}</div>;
  if (!full) return <p className="muted small">Loading...</p>;

  return (
    <div className="slave-risk-form">
      {err && <div className="banner banner-error">{err}</div>}
      <datalist id="known-masters">
        {(knownAccounts || []).map((a) => (
          <option key={a} value={a} />
        ))}
      </datalist>
      <AccountList
        label="Follow only these masters"
        hint="none listed = follow every master"
        values={allowRows}
        onChange={setAllowRows}
      />
      <AccountList
        label="Never follow these"
        hint="applied after the list above"
        values={denyRows}
        onChange={setDenyRows}
      />
      <p className="muted small">
        Magic numbers are per master and WILL collide: every fxDreema project numbers its
        strategies from 1001, so two masters each running their first EA both stamp 1001. A
        slave that follows both cannot tell them apart -- an accepted magic takes both, and a
        per-strategy risk scale resizes both. Naming the masters is what makes the strategy
        list mean something.
      </p>
      <div className="slave-actions">
        <button className="btn btn-small" onClick={save} disabled={busy}>
          {busy ? "Saving..." : "Save"}
        </button>
      </div>
    </div>
  );
}

function AccountList({ label, hint, values, onChange }) {
  return (
    <div className="magic-list">
      <span className="muted small">
        {label} <span className="muted small">-- {hint}</span>
      </span>
      {values.map((v, i) => (
        <div className="magic-row" key={i}>
          <input
            value={v}
            list="known-masters"
            placeholder="44964870"
            onChange={(e) => onChange(values.map((x, j) => (i === j ? e.target.value : x)))}
          />
          <button
            className="btn btn-small btn-danger"
            onClick={() => onChange(values.filter((_, j) => j !== i))}
          >
            Remove
          </button>
        </div>
      ))}
      <button className="btn btn-small" onClick={() => onChange([...values, ""])}>
        + Add master
      </button>
    </div>
  );
}

function MagicList({ label, hint, values, onChange }) {
  return (
    <div className="magic-list">
      <span className="muted small">
        {label} <span className="muted small">-- {hint}</span>
      </span>
      {values.map((v, i) => (
        <div className="magic-row" key={i}>
          <input
            value={v}
            placeholder="1001"
            onChange={(e) => onChange(values.map((x, j) => (i === j ? e.target.value : x)))}
          />
          <button
            className="btn btn-small btn-danger"
            onClick={() => onChange(values.filter((_, j) => j !== i))}
          >
            Remove
          </button>
        </div>
      ))}
      <button className="btn btn-small" onClick={() => onChange([...values, ""])}>
        + Add magic
      </button>
    </div>
  );
}

function MagicFilterForm({ slave, onDone }) {
  const [full, setFull] = useState(null);
  // One magic number per field. A comma-separated box reads fine until a
  // stray character turns the whole list into one unparseable string, and
  // an allow-list that fails to parse is a slave that stops trading.
  const [allowRows, setAllowRows] = useState([]);
  const [denyRows, setDenyRows] = useState([]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  useEffect(() => {
    (async () => {
      try {
        const got = await api.getSlave(slave.id);
        const mf = got.config.magic_filter;
        setFull(got);
        if (Array.isArray(mf)) setAllowRows(mf.map(String));
        else if (mf && typeof mf === "object") {
          setAllowRows((mf.allow || []).map(String));
          setDenyRows((mf.deny || []).map(String));
        }
      } catch (e) {
        setErr(e.message);
      }
    })();
  }, [slave.id]);

  function parseRows(rows, where) {
    const out = [];
    for (const raw of rows) {
      const t = String(raw).trim();
      if (!t) continue;
      if (!/^\d+$/.test(t)) throw new Error(`"${t}" in ${where} is not a magic number`);
      const n = Number(t);
      if (out.includes(n)) throw new Error(`magic ${n} is listed twice in ${where}`);
      out.push(n);
    }
    return out;
  }

  async function save() {
    setBusy(true);
    setErr(null);
    try {
      const allowList = parseRows(allowRows, "the accept list");
      const denyList = parseRows(denyRows, "the never-accept list");
      const clash = allowList.filter((n) => denyList.includes(n));
      if (clash.length) {
        throw new Error(
          `magic ${clash.join(", ")} is in both lists -- deny wins, so it would never trade`,
        );
      }
      const config = { ...full.config };
      if (allowList.length === 0 && denyList.length === 0) {
        delete config.magic_filter;
      } else {
        const next = {};
        // An EMPTY allow-list is not the same as no allow-list: it would
        // stop every signal. Only send the key when it has members.
        if (allowList.length) next.allow = allowList;
        if (denyList.length) next.deny = denyList;
        config.magic_filter = next;
      }
      await api.patchSlave(slave.id, { config });
      onDone();
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  }

  if (err && !full) return <div className="banner banner-error">{err}</div>;
  if (!full) return <p className="muted small">Loading...</p>;

  return (
    <div className="slave-risk-form">
      {err && <div className="banner banner-error">{err}</div>}
      <MagicList
        label="Accept only these"
        hint="none listed = accept every strategy"
        values={allowRows}
        onChange={setAllowRows}
      />
      <MagicList
        label="Never accept these"
        hint="applied after the list above"
        values={denyRows}
        onChange={setDenyRows}
      />
      <p className="muted small">
        One master terminal can run many EAs, each stamping its own magic number. This decides
        which of them this slave copies. Leaving both blank accepts every signal -- which is what
        every slave does today.
      </p>
      <div className="slave-actions">
        <button className="btn btn-small" onClick={save} disabled={busy}>
          {busy ? "Saving..." : "Save"}
        </button>
      </div>
    </div>
  );
}

function ContractRollForm({ slave, onDone }) {
  const [full, setFull] = useState(null);
  const [form, setForm] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  useEffect(() => {
    (async () => {
      try {
        const got = await api.getSlave(slave.id);
        const roll = got.config.contract_roll || {};
        setFull(got);
        setForm({
          enabled: !!roll.enabled,
          min_volume_ratio: roll.min_volume_ratio ?? 1,
          check_interval_minutes: (roll.check_interval_seconds ?? 3600) / 60,
        });
      } catch (e) {
        setErr(e.message);
      }
    })();
  }, [slave.id]);

  async function save() {
    setBusy(true);
    setErr(null);
    try {
      const config = { ...full.config };
      config.contract_roll = {
        ...(config.contract_roll || {}),
        enabled: !!form.enabled,
        min_volume_ratio: Number(form.min_volume_ratio),
        check_interval_seconds: Math.round(Number(form.check_interval_minutes) * 60),
      };
      await api.patchSlave(slave.id, { config });
      onDone();
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  }

  if (err && !form) return <div className="banner banner-error">{err}</div>;
  if (!form) return <p className="muted small">Loading...</p>;

  return (
    <div className="slave-risk-form">
      {err && <div className="banner banner-error">{err}</div>}
      <label className="checkbox-row">
        <input
          type="checkbox"
          checked={form.enabled}
          onChange={(e) => setForm({ ...form, enabled: e.target.checked })}
        />
        Roll positions to the next contract before expiry
      </label>
      <label>
        Roll once the next month out-trades the front by
        <input
          type="number"
          step="0.1"
          min="0"
          value={form.min_volume_ratio}
          onChange={(e) => setForm({ ...form, min_volume_ratio: e.target.value })}
        />
      </label>
      <label>
        Check every (minutes)
        <input
          type="number"
          step="5"
          min="1"
          value={form.check_interval_minutes}
          onChange={(e) => setForm({ ...form, check_interval_minutes: e.target.value })}
        />
      </label>
      <p className="muted small">
        Volume, not the calendar: the next month out-trading the front is the market saying the
        front is dying. A roll is two market orders per position, and a sweep runs outermost expiry
        first so a rolled position never lands on a month another strategy still holds.
      </p>
      <div className="slave-actions">
        <button className="btn btn-small" onClick={save} disabled={busy}>
          {busy ? "Saving..." : "Save"}
        </button>
      </div>
    </div>
  );
}

function SlaveRiskForm({ slave, onDone }) {
  const [full, setFull] = useState(null);
  const [form, setForm] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  useEffect(() => {
    (async () => {
      try {
        const got = await api.getSlave(slave.id);
        const sz = got.config.sizing || {};
        setFull(got);
        setForm({
          mode: sz.mode === "pct_risk" ? "pct_risk" : "mirror_lots",
          risk_pct: sz.risk_pct != null ? +(sz.risk_pct * 100).toFixed(4) : 1,
          capital_base: sz.capital_base || "balance",
          max_risk_pct: sz.max_risk_pct != null ? +(sz.max_risk_pct * 100).toFixed(4) : "",
          allow_opposing: !!(got.config.netting || {}).allow_opposing,
        });
      } catch (e) {
        setErr(e.message);
      }
    })();
  }, [slave.id]);

  async function save() {
    setBusy(true);
    setErr(null);
    try {
      // Read-modify-write, deliberately. PATCH replaces config_json whole,
      // so routes, mirror_map, host and ports have to travel back
      // untouched or they are deleted by omission.
      const config = { ...full.config };

      if (form.mode === "mirror_lots") {
        delete config.sizing;
      } else {
        const sizing = { ...(config.sizing || {}), mode: "pct_risk" };
        sizing.risk_pct = Number(form.risk_pct) / 100;
        sizing.capital_base = form.capital_base;
        if (form.max_risk_pct === "" || form.max_risk_pct === null) delete sizing.max_risk_pct;
        else sizing.max_risk_pct = Number(form.max_risk_pct) / 100;
        config.sizing = sizing;
      }

      config.netting = { ...(config.netting || {}), allow_opposing: !!form.allow_opposing };

      await api.patchSlave(slave.id, { config });
      onDone();
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  }

  if (err && !form) return <div className="banner banner-error">{err}</div>;
  if (!form) return <p className="muted small">Loading this slave&apos;s configuration...</p>;

  return (
    <div className="slave-risk-form">
      {err && <div className="banner banner-error">{err}</div>}

      <label>
        Sizing
        <select value={form.mode} onChange={(e) => setForm({ ...form, mode: e.target.value })}>
          <option value="mirror_lots">Mirror the master&apos;s lots</option>
          <option value="pct_risk">Risk a percentage of this account</option>
        </select>
      </label>

      {form.mode === "pct_risk" && (
        <>
          <label>
            Risk per signal (%)
            <input
              type="number"
              step="0.05"
              min="0"
              value={form.risk_pct}
              onChange={(e) => setForm({ ...form, risk_pct: e.target.value })}
            />
          </label>
          <label>
            Measured against
            <select
              value={form.capital_base}
              onChange={(e) => setForm({ ...form, capital_base: e.target.value })}
            >
              <option value="balance">Balance</option>
              <option value="equity">Equity</option>
            </select>
          </label>
          <label>
            Refuse above (%) <span className="muted small">blank for no ceiling</span>
            <input
              type="number"
              step="0.05"
              min="0"
              value={form.max_risk_pct}
              onChange={(e) => setForm({ ...form, max_risk_pct: e.target.value })}
            />
          </label>
          <p className="muted small">
            The smallest tradeable size can already risk more than the target -- one gold future on a
            wide stop does. When it exceeds the ceiling the leg is refused rather than sized up, so a
            ceiling too close to the target means that instrument simply will not trade.
          </p>
        </>
      )}

      <label className="checkbox-row">
        <input
          type="checkbox"
          checked={form.allow_opposing}
          onChange={(e) => setForm({ ...form, allow_opposing: e.target.checked })}
        />
        Copy a signal that opposes what this account already holds
      </label>
      <p className="muted small">
        Off, a BUY is refused while the account is short that symbol. On a hedging account both legs
        sit side by side, which is intended if you trade both ways yourself -- and wrong if these
        positions are not yours to add to.
      </p>

      <div className="slave-actions">
        <button className="btn btn-small" onClick={save} disabled={busy}>
          {busy ? "Saving..." : "Save"}
        </button>
      </div>
    </div>
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
