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
            <th>How it copies</th>
            <th>Status</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {status?.slaves.map((s) => (
            <tr key={s.id}>
              <td>{s.name}</td>
              <td>{s.broker_type}</td>
              <td>
                <ModeCell slave={s} onChanged={onChanged} />
              </td>
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
      <p className="muted small">
        How a slave copies selects the engine, so switching it stops one and starts the other.
        Only possible while the slave is flat: the incoming engine would otherwise adopt open
        positions and protect them under rules they were never opened under -- mirror aims at
        the master&apos;s own stop, autonomous trails its own from ATR.
      </p>

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
      {/* Two hub-wide switches on one row. Both are "stop trading"
          controls and both are read at a glance rather than configured
          often, so a panel each was mostly whitespace. The explanations
          move to title text -- they matter when you reach for the
          control, not every time you pass it. */}
      <div className="panel hub-risk-row">
        {error && <div className="banner banner-error">{error}</div>}
        <div className="hub-risk-cell">
          <span className="muted small">Kill switch</span>
          <button
            className={`btn btn-small ${risk.kill_switch_enabled ? "btn-danger" : ""}`}
            disabled={saving}
            title="Stops every slave copying immediately. Signals are still logged. Open positions are untouched -- this blocks new activity, it does not close anything."
            onClick={() => save({ kill_switch_enabled: !risk.kill_switch_enabled })}
          >
            {risk.kill_switch_enabled ? "ON -- click to disable" : "off -- click to enable"}
          </button>
        </div>

        <div className="hub-risk-cell">
          <span
            className="muted small"
            title="UTC window in which new positions may be opened. Existing positions can always be modified or closed regardless."
          >
            Trading hours (UTC)
          </span>
          <div className="hub-hours">
            <input
              type="checkbox"
              checked={risk.trading_hours_enabled}
              onChange={(e) => save({ trading_hours_enabled: e.target.checked })}
              title="Enable the window"
            />
            <input
              type="time"
              value={risk.trading_hours_start}
              disabled={!risk.trading_hours_enabled}
              onChange={(e) => setRisk({ ...risk, trading_hours_start: e.target.value })}
              onBlur={(e) => save({ trading_hours_start: e.target.value })}
            />
            <span className="muted">to</span>
            <input
              type="time"
              value={risk.trading_hours_end}
              disabled={!risk.trading_hours_enabled}
              onChange={(e) => setRisk({ ...risk, trading_hours_end: e.target.value })}
              onBlur={(e) => save({ trading_hours_end: e.target.value })}
            />
          </div>
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
  // Folded by default. Six slaves of open detail is a page nobody reads;
  // the summary line is what answers "is this one set up as I think".
  // Remembered per slave so a card someone is working in stays open
  // across the refresh that follows every save.
  const [open, setOpen] = useState(() => {
    try {
      return localStorage.getItem(`gtc.riskCard.${slave.id}`) === "open";
    } catch {
      return false;
    }
  });
  const [editing, setEditing] = useState(false);
  const [editingScale, setEditingScale] = useState(false);
  const [editingBasis, setEditingBasis] = useState(false);
  const [editingRoutes, setEditingRoutes] = useState(false);
  const [editingMagic, setEditingMagic] = useState(false);
  const [editingMasters, setEditingMasters] = useState(false);
  const [editingRoll, setEditingRoll] = useState(false);
  const [editingSplit, setEditingSplit] = useState(false);
  const [showRoutes, setShowRoutes] = useState(false);

  function toggle() {
    const next = !open;
    setOpen(next);
    try {
      localStorage.setItem(`gtc.riskCard.${slave.id}`, next ? "open" : "shut");
    } catch {
      // A browser refusing storage still folds, it just forgets.
    }
  }

  return (
    <div className={`slave-risk ${open ? "is-open" : ""}`}>
      <div className="slave-risk-head" onClick={toggle} role="button" tabIndex={0}
           onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") toggle(); }}>
        <div>
          <span className="fold-caret">{open ? "\u25be" : "\u25b8"}</span>{" "}
          <strong>{slave.name}</strong>{" "}
          <span className="muted small">{slave.broker_type}</span>
          {/* The one line worth seeing folded: what it stakes and what
              it stakes it against. */}
          <div className="slave-risk-sizing">
            {describeHeadline(slave)}
            {describeBasis(slave) !== "the broker's own balance" && (
              <span className="muted"> &middot; {describeBasis(slave)}</span>
            )}
          </div>
        </div>
        {/* NO LONGER GATED ON sizing_per_route. This form holds the leg
            cap, the combined-risk cap, opposing legs and the margin
            ceiling -- none of which are sizing -- so hiding it on a
            route-sized slave made four limits unreachable on
            ibkr-etf-1. Chris, twice: "max lex and refusal % arent
            editable again". The SIZING half hides itself below. */}
        {open && (
          <button
            className="btn btn-small"
            onClick={(e) => { e.stopPropagation(); setEditing((v) => !v); }}
          >
            {editing ? "Cancel" : "Edit"}
          </button>
        )}
      </div>

      {!open ? null : (
      <>

      {/* The mode was PRINTED in the head above and changeable only from the
          Slaves tab, which is a different screen. Chris switched a slave to
          mirror while testing and found no way back, because this is the
          card the work happens on. Shown where it is read. */}
      <div className="slave-risk-row">
        <div>
          <span className="muted small">How it copies</span>
          <div className="slave-risk-sizing">
            {slave.mode_label || slave.mode}
          </div>
        </div>
        <ModeCell slave={slave} onChanged={onSaved} compact />
      </div>

      {/* The folded headline reads "risk scale x1.00 of what the master
          staked" and there was NOWHERE on this card to change it. The only
          field for it sat inside Strategy selection, labelled "All
          strategies on this slave", and only appeared once a selection mode
          other than Accept none was chosen -- so the number the card leads
          with had no visible control at all. Chris, twice: "there is no
          field to adjust the slave level risk scale. there is text claiming
          whats staked, but no adjustment field still."

          Shown unconditionally, INCLUDING for a route-sized slave. Risk
          scale is read off the slave config even when every route carries
          its own sizing -- resolve_risk_scale() takes slave_config -- so
          "sizing is per route, nothing to edit here" was never true of
          this one value. ibkr-etf-1 is exactly that slave. */}
      <div className="slave-risk-row">
        <div>
          <span className="muted small">Risk calibration</span>
          <div className="slave-risk-sizing">{describeScale(slave)}</div>
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

      {/* THE SAME DEFECT, found by generalising the test rather than by
          anyone hitting it: RiskBasisForm was written in full and never
          mounted either, while describeBasis() has been printing
          "+100,000 adjustment, never below 250,000" in the head all along.
          Text claiming a number, no field for it -- which is the complaint
          that started this, one row up.

          It edits balance_adjustment and min_risk_capital, and both change
          every size this slave calculates, so being settable only by a
          PATCH against the API was not a small gap. */}
      <div className="slave-risk-row">
        <div>
          <span className="muted small">Capital basis</span>
          <div className="slave-risk-sizing">{describeBasis(slave)}</div>
        </div>
        <button className="btn btn-small" onClick={() => setEditingBasis((v) => !v)}>
          {editingBasis ? "Cancel" : "Edit"}
        </button>
      </div>
      {editingBasis && (
        <RiskBasisForm
          slave={slave}
          onDone={() => { setEditingBasis(false); onSaved && onSaved(); }}
        />
      )}

      <div className="slave-risk-flags">
        {/* The refusal ceiling sits with the other limits, not in the
            sizing line. It is not how much this slave stakes -- it is the
            point at which it declines to stake at all. */}
        {describeCeiling(slave) != null && (
          <span
            className="badge badge-warn"
            title="A signal whose risk works out above this share of capital is refused outright rather than trimmed"
          >
            refuse above {(describeCeiling(slave) * 100).toFixed(2)}%
          </span>
        )}
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
        {/* The RESOLVED ceiling. This read margin.max_pct_of_free, one of
            the two keys that mean this -- so the two IBKR slaves, which
            carry netting.max_margin_pct, showed no ceiling at all while
            being capped at 40%, and the Darwinex slave showed 25% while
            its engine read the other key and capped nothing. */}
        {slave.margin_cap_pct != null && (
          <span className="badge" title="Initial margin may not exceed this share of free margin">
            margin ceiling {(slave.margin_cap_pct * 100).toFixed(0)}% of free
          </span>
        )}
      </div>

      {/* A ROUTE-SIZED SLAVE HAD NO WAY TO CHANGE ITS CAP AT ALL.
          ibkr-etf-1 carries no slave-level sizing block, so the Edit
          button above is correctly hidden -- a slave-level value would
          save cleanly and never be read. But its ceiling is real and
          lives on each of twenty routes, and nothing in this UI could
          touch it: the routes are listed read-only further down.
          Chris: "I cant adjust the max cap. that initial edit tab, top
          right, is gone on that slave."
          So the editor writes to every route at once, which is what a
          slave-wide figure means here. */}
      {slave.sizing_per_route && (
        <>
          <p className="muted small">
            Risk <em>sizing</em> is set on each route individually rather than on the slave, so a
            slave-level block would save cleanly and never be read. Risk <em>calibration</em>{" "}
            above is the exception -- that one is read off the slave either way.
          </p>
          <div className="slave-risk-row">
            <div>
              <span className="muted small">Sizing on every route</span>
              <div className="slave-risk-sizing">{slave.sizing_summary}</div>
            </div>
            <button className="btn btn-small" onClick={() => setEditingRoutes((v) => !v)}>
              {editingRoutes ? "Cancel" : "Edit"}
            </button>
          </div>
          {editingRoutes && (
            <RouteSizingForm
              slave={slave}
              onDone={() => { setEditingRoutes(false); onSaved && onSaved(); }}
            />
          )}
        </>
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
      {/* ONE row, because a strategy and the risk it carries here are a
          single decision. They were two, and a scale a section away from
          the magic it belongs to is read wrong. */}
      <div className="slave-risk-row">
        <div>
          <span className="muted small">Strategy selection</span>
          <div className="slave-risk-sizing">{describeSelection(slave)}</div>
        </div>
        <button className="btn btn-small" onClick={() => setEditingMagic((v) => !v)}>
          {editingMagic ? "Cancel" : "Edit"}
        </button>
      </div>
      {editingMagic && (
        <StrategyForm
          slave={slave}
          onDone={() => { setEditingMagic(false); onSaved && onSaved(); }}
        />
      )}

      {/* Rolling is two market orders per position when it fires, and it
          fires on its own schedule. Running invisibly is the problem. */}
      {/* Only where something EXPIRES. A share of UGL has no front
          month, so this row offered to schedule two market orders for
          an event that cannot occur. An existing contract_roll block
          still shows, so a slave configured by hand is never hidden
          from its own setting. */}
      {(slave.contract_roll
        || (slave.mode === "autonomous" && slave.rolls_contracts !== false)) && (
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

      {/* HOW an entry reaches the market, which is not how big it is --
          so it sits apart from sizing. Shown on every slave, including
          the ones that cannot do it: "off" and "this broker places one
          order per entry" are different answers and the page should not
          give the first when it means the second. */}
      <div className="slave-risk-row">
        <div>
          <span className="muted small">Order splitting</span>
          <div className="slave-risk-sizing">
            {slave.supports_order_split === false
              ? "not available -- this broker places one order per entry"
              : slave.split_summary || "off -- each entry goes out as one order"}
          </div>
        </div>
        {slave.supports_order_split !== false && (
          <button className="btn btn-small" onClick={() => setEditingSplit((v) => !v)}>
            {editingSplit ? "Cancel" : "Edit"}
          </button>
        )}
      </div>
      {editingSplit && (
        <OrderSplitForm
          slave={slave}
          onDone={() => { setEditingSplit(false); onSaved && onSaved(); }}
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
      </>
      )}
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
            <th>Split</th>
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
                  <span className="muted small"> (next one on capacity)</span>
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
                {/* The pair that decides whether an evening entry can still
                    be open at the next session. One without the other is
                    the setting that looks right and does nothing, so both
                    are shown and a lone TIF is flagged. */}
                {r.tif && <span className="badge">{r.tif}</span>}
                {r.tif && r.tif !== "DAY" && !r.entry_timeout && (
                  <span
                    className="badge badge-warn"
                    title="This engine still cancels the unfilled entry on its own default clock, so the longer TIF changes nothing. Set entry_timeout too."
                  >
                    no wait set
                  </span>
                )}
                {r.entry_timeout && (
                  <span className="muted small">
                    {" "}rests {Math.round(r.entry_timeout / 60)}m
                  </span>
                )}
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
              {/* This route's own splitter, which wins over the
                  slave's. It was invisible: the gold route carried
                  "3 over 5s" while the slave card said nothing, so
                  clearing the slave-level setting left a split running
                  that no screen would own up to. */}
              <td>
                {r.split_slices > 1 && r.split_interval > 0 ? (
                  <span
                    title={`One entry goes out as ${r.split_slices} orders ${r.split_interval}s apart -- a ${(r.split_slices - 1) * r.split_interval}s window -- filling into one position`}
                  >
                    {r.split_slices}&times; / {r.split_interval}s
                  </span>
                ) : r.split_slices === 0 ? (
                  <span className="muted" title="This route opts out of the slave-wide split">
                    opted out
                  </span>
                ) : (
                  <span className="muted">--</span>
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

function ModeCell({ slave, onChanged, compact }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const raw = slave.mode === "autonomous" ? "autonomous" : "mirror";
  const other = raw === "autonomous" ? "mirror" : "autonomous";

  async function switchTo(next) {
    if (next === raw) return;
    setBusy(true);
    setErr(null);
    try {
      // Only the mode travels. Sending back a config this component had
      // been holding would make a read-modify-write out of what is meant
      // to be one decision, and anything edited elsewhere since the page
      // loaded would be undone. ModeCell always read fresh here -- the
      // other six forms did not, which is what this now matches.
      const config = await currentConfig(slave.id);
      await api.patchSlave(slave.id, { config: { ...config, mode: next } });
      onChanged && onChanged();
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mode-cell">
      {!compact && <div>{slave.mode_label || slave.mode}</div>}
      <select
        value={raw}
        disabled={busy || slave.mode_editable === false}
        onChange={(e) => switchTo(e.target.value)}
      >
        <option value="mirror">mirror</option>
        <option value="autonomous">autonomous</option>
      </select>
      {slave.mode_editable === false && (
        <div className="muted small">
          {slave.mode_locked_reason
            ? `locked -- ${slave.mode_locked_reason}`
            : "locked"}
        </div>
      )}
      {busy && <div className="muted small">restarting the engine...</div>}
      {err && <div className="banner banner-error">{err}</div>}
      {!busy && !err && slave.mode_editable !== false && (
        <div className="muted small">switching restarts as {other}</div>
      )}
    </div>
  );
}

function describeHeadline(slave) {
  // The old line read "1.00% of balance" -- a percentage that is no
  // longer edited anywhere, because sizing comes from what the master
  // actually staked. Saying it invites someone to go looking for a field
  // that is not there.
  const sizing = slave.sizing_summary || "";
  // "10 routes, all mirrors the master's lots" is what ibkr-paper-1 read
  // after its sizing block was deleted -- true of the config and alarming
  // to find on a futures slave, which is exactly when it needs saying
  // loudly rather than as a tally.
  if (/mirrors the master's lots/.test(sizing)) {
    return "NO RISK SIZING -- sends the master's lot count unchanged";
  }
  // "staked risk" is the pct_risk case as the hub now words it. The old
  // test was /\d+\.\d+% of/, matching "1.00% of balance" -- and that
  // phrasing went away on 2026-11-16 precisely because the percentage in
  // it is overridden by what the master staked. Left as it was, this
  // would have fallen through and printed the summary instead of the
  // calibration line, quietly undoing the row.
  if (!/staked risk/.test(sizing) && !/\d+\.\d+% of/.test(sizing)) {
    return sizing;   // lots, or something new
  }

  // RISK SCALE LEADS, and no percentage appears here at all.
  //
  // The percentages are internal calibration: risk_pct is overridden by
  // whatever the master's order actually staked, and max_risk_pct is a
  // refusal threshold. Neither is the dial anybody turns. Risk scale is --
  // it is the calibration between what the EA signalled and what this
  // slave takes of it -- so it is the thing the folded line has to show,
  // and it shows even at 1.00 because "unscaled" is a setting too.
  //
  // The ceiling has not been dropped: it moved to a badge beside the leg
  // and aggregate limits, which is what it is.
  const scale = slave.risk_scale ?? 1;
  const byMagic = Object.keys(slave.risk_scale_by_magic || {}).length;
  const base = sizing.includes("equity") ? "equity" : "balance";

  // "Risk calibration", Chris's term and the right one: this is not
  // a size, it is the ratio between what the EA signalled and what
  // this slave takes of it.
  let head = `risk calibration x${Number(scale).toFixed(2)}`;
  head += ` of what the master staked, against this ${base}`;
  if (byMagic) {
    // "override" is the right word for the screen even though the
    // dials multiply: what the reader needs to know from the folded
    // line is that some strategies are not on the headline figure.
    // The product itself is spelled out on the Risk calibration row.
    head += ` \u00b7 ${byMagic} strategy override${byMagic === 1 ? "" : "s"}`;
  }
  return head;
}

/** The refusal ceiling, as a limit rather than as sizing. */
function describeCeiling(slave) {
  const direct = (slave.sizing || {}).max_risk_pct;
  if (typeof direct === "number") return direct;
  // A route-sized slave carries no slave-level block, so read it off the
  // summary the hub already built rather than re-deriving it here.
  const found = /capped ([\d.]+)%/.exec(slave.sizing_summary || "");
  return found ? Number(found[1]) / 100 : null;
}

function describeBasis(slave) {
  const adj = slave.balance_adjustment;
  const floor = slave.min_risk_capital;
  if (!adj && !floor) return "the broker's own balance";
  const parts = [];
  if (adj) parts.push(`${adj > 0 ? "+" : ""}${money(adj)} adjustment`);
  if (floor) parts.push(`never below ${money(floor)}`);
  return parts.join(", ");
}

function money(n) {
  const v = Number(n);
  if (!isFinite(v)) return String(n);
  return v.toLocaleString(undefined, { maximumFractionDigits: 0 });
}

function RiskBasisForm({ slave, onDone }) {
  const [full, setFull] = useState(null);
  const [adjustment, setAdjustment] = useState("");
  const [floor, setFloor] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  useEffect(() => {
    (async () => {
      try {
        const got = await api.getSlave(slave.id);
        setFull(got);
        setAdjustment(got.config.balance_adjustment != null ? String(got.config.balance_adjustment) : "");
        setFloor(got.config.min_risk_capital != null ? String(got.config.min_risk_capital) : "");
      } catch (e) {
        setErr(e.message);
      }
    })();
  }, [slave.id]);

  async function save() {
    setBusy(true);
    setErr(null);
    try {
      const config = await currentConfig(slave.id);

      const adj = adjustment.trim();
      if (adj === "" || Number(adj) === 0) delete config.balance_adjustment;
      else {
        if (Number.isNaN(Number(adj))) throw new Error("the adjustment must be a number");
        config.balance_adjustment = Number(adj);
      }

      const flr = floor.trim();
      if (flr === "" || Number(flr) === 0) delete config.min_risk_capital;
      else {
        const n = Number(flr);
        if (Number.isNaN(n) || n < 0) throw new Error("the floor must be 0 or more");
        config.min_risk_capital = n;
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

      <label>
        Balance adjustment <span className="muted small">account currency, + or -</span>
        <input
          type="number"
          step="1000"
          value={adjustment}
          placeholder="0"
          onChange={(e) => setAdjustment(e.target.value)}
        />
      </label>
      <p className="muted small">
        Corrects a figure that is wrong. A 250k account pays 50k in tax and the broker now
        reports 200k; an adjustment of +50,000 keeps the strategies risking as if it were
        still 250k, because a withdrawal is not a drawdown. Applied whichever capital base
        is in force.
      </p>

      <label>
        Never size below <span className="muted small">account currency, blank or 0 for no floor</span>
        <input
          type="number"
          step="1000"
          min="0"
          value={floor}
          placeholder="0"
          onChange={(e) => setFloor(e.target.value)}
        />
      </label>
      <p className="muted small">
        Overrides a figure that is right. If that account then draws down to 175k, sizing
        carries on at the floor instead.
      </p>
      <p className="muted small">
        <strong>This is not free.</strong> Below the floor you are risking a percentage of
        money that is not there: 1% of a 250,000 floor is 2,500, which on an account
        actually holding 175,000 is 1.43% of it -- and the gap widens as the drawdown does.
        That is the point of the setting, and it is also how a drawdown accelerates.
      </p>
      <p className="muted small">
        Margin is never measured against either of these. The broker holds margin against
        real equity, and a cap measured against an inflated figure would believe it had room
        it does not have.
      </p>

      <div className="slave-actions">
        <button className="btn btn-small" onClick={save} disabled={busy}>
          {busy ? "Saving..." : "Save"}
        </button>
      </div>
    </div>
  );
}

/**
 * The slave's CURRENT config, re-read at the moment of saving.
 *
 * Every form here PATCHes the whole config, and PATCH /slaves/{id}
 * replaces rather than merges -- so a form saving the snapshot it loaded
 * on open silently restores every value changed since. That is not
 * hypothetical: risk_scale 0.50 was set on ibkr-paper-1 and wiped twice
 * in twenty minutes by exactly this, from forms that had been open the
 * whole time.
 *
 * Re-reading shrinks the window from "as long as the form was open" to
 * one round trip. It is not real concurrency control -- two saves inside
 * that window still lose one -- and the proper fix is for each form to
 * send only the keys it owns against a merging endpoint.
 */
async function currentConfig(slaveId) {
  const got = await api.getSlave(slaveId);
  return { ...got.config };
}

function describeScale(slave) {
  const byMagic = slave.risk_scale_by_magic || {};
  const entries = Object.entries(byMagic);
  const base = slave.risk_scale ?? 1;
  // The dials MULTIPLY: intake x strategy. So the per-strategy figures
  // are shown with the product they actually produce -- "2002 x0.50 (=
  // x0.25)" -- because that product is the number being asked about, and
  // multiplying two decimals in your head is how a position comes out
  // four times the size intended.
  const baseText =
    base === 1
      ? "every signal taken in full"
      : `x${Number(base).toFixed(2)} of signal at intake`;
  if (entries.length === 0) return baseText;
  const listed = entries
    .map(([magic, scale]) => {
      const each = `${magic} x${Number(scale).toFixed(2)}`;
      const net = Number(base) * Number(scale);
      return base === 1 ? each : `${each} (= x${net.toFixed(2)})`;
    })
    .join(", ");
  return `${baseText}; then ${listed}`;
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

      const config = await currentConfig(slave.id);
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
    <div className="slave-risk-form with-notes">
      {err && <div className="banner banner-error">{err}</div>}

      {/* The number the folded headline reads out, labelled in the same
          words so the line and its control are recognisably one thing.

          This is the INTAKE figure: how much of any incoming signal this
          venue takes, which is a property of the venue's leverage rather
          than of any strategy. The per-strategy figures below multiply
          it, so this one applies to every signal whether or not the
          strategy has a row of its own. */}
      <label>
        Risk calibration &mdash; this slave takes this much of every signal &times;
        <input
          type="number"
          step="0.05"
          min="0"
          value={base}
          onChange={(e) => setBase(e.target.value)}
        />
      </label>

      {/* Magic and its scale on ONE row, which is the whole point: the
          question is always "what is strategy 1001 set to", and a value
          a column away from its label is read wrong. */}
      <div className="scale-rows">
        <div className="scale-head">
          <span>Strategy (magic)</span>
          <span>&times; on top of intake</span>
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

      <aside className="side-note">
        <p>
          <strong>1.0</strong> takes the whole of what the master staked on that strategy.
          The same share on a futures or ETF slave is usually too much -- far less leverage
          than the CFD account it was sized for. <strong>0</strong> means do not trade this
          strategy here at all.
        </p>
        <p>
          A strategy listed here <strong>multiplies</strong> the intake figure above &mdash; it
          does not replace it. An EA signalling 2% of balance, into a slave calibrated 0.50, with
          this strategy at 0.50, trades <strong>0.5% &mdash; a quarter of the original signal</strong>.
        </p>
        <p>
          <strong>1.00 means no further adjustment</strong>, which is what most strategies want:
          they simply inherit the slave's intake figure. It does not mean full size.
        </p>
        <p>A route with its own figure multiplies in turn, on top of both.</p>
      </aside>

      <div className="slave-actions">
        <button className="btn btn-small" onClick={save} disabled={busy}>
          {busy ? "Saving..." : "Save"}
        </button>
      </div>
    </div>
  );
}

/**
 * Sizing for EVERY route on a slave that sizes per route.
 *
 * Not a slave-level block -- those are ignored when routes size
 * themselves, which is exactly why the card hides the ordinary Edit
 * button here. This writes the same numbers into each route's own
 * sizing, because "the cap on this slave" is what the twenty identical
 * blocks on ibkr-etf-1 actually mean.
 *
 * Routes that carry NO sizing block are left alone: adding one would
 * change a route that was deliberately inheriting, and the summary line
 * says how many were skipped.
 */
function RouteSizingForm({ slave, onDone }) {
  const [full, setFull] = useState(null);
  const [form, setForm] = useState(null);
  const [mixed, setMixed] = useState(false);
  const [count, setCount] = useState(0);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  useEffect(() => {
    (async () => {
      try {
        const got = await api.getSlave(slave.id);
        setFull(got);
        const routes = (got.config.routes || []).filter((r) => r.sizing);
        setCount(routes.length);

        // Uniform or not is the whole question: one editor over twenty
        // identical blocks is honest, over twenty different ones it is
        // a bulk overwrite and has to say so.
        const shapes = new Set(routes.map((r) => JSON.stringify(r.sizing)));
        setMixed(shapes.size > 1);

        const first = routes.length ? routes[0].sizing : {};
        setForm({
          // risk_pct is carried, not shown. See the note above the save:
          // the master's measured fraction replaces it on every signal.
          risk_pct: first.risk_pct,
          max_risk_pct:
            first.max_risk_pct != null ? +(first.max_risk_pct * 100).toFixed(4) : "",
          capital_base: first.capital_base || "balance",
          rounding: first.rounding || "nearest_min1",
          round_band: first.round_band != null ? first.round_band : "",
          max_risk_error:
            first.max_risk_error != null ? +(first.max_risk_error * 100).toFixed(4) : "",
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
      const cap = String(form.max_risk_pct).trim();
      if (cap !== "" && (Number.isNaN(Number(cap)) || Number(cap) <= 0)) {
        throw new Error("the ceiling must be a percentage above zero, or blank for none");
      }
      const band = String(form.round_band).trim();
      if (band !== "") {
        const b = Number(band);
        if (Number.isNaN(b) || b < 0 || b >= 0.5) {
          // 0.5 would make every size "near a whole contract" from both
          // sides at once, so the step-down could never fire.
          throw new Error("the step-down band must be between 0 and 0.5");
        }
      }
      const tol = String(form.max_risk_error).trim();
      if (tol !== "") {
        const t = Number(tol);
        if (Number.isNaN(t) || t < 0 || t >= 100) {
          throw new Error("the risk tolerance must be a percentage from 0 to under 100");
        }
      }

      // Re-read at save time. These forms PATCH the whole config, and a
      // snapshot taken when the form opened silently restores anything
      // changed since -- the bug that wiped risk_scale twice.
      const config = await currentConfig(slave.id);
      let touched = 0;
      config.routes = (config.routes || []).map((r) => {
        if (!r.sizing) return r;
        touched += 1;
        const sizing = { ...r.sizing };
        // risk_pct is left exactly as found. The master's own figure
        // replaces it on every signal, so there is no field for it --
        // but a slave fed by a bridge too old to publish one still
        // falls back to it, and dropping it would resize those silently.
        if (band === "") delete sizing.round_band;
        else sizing.round_band = Number(band);
        if (tol === "") delete sizing.max_risk_error;
        else sizing.max_risk_error = Number(tol) / 100;
        if (cap === "") delete sizing.max_risk_pct;
        else sizing.max_risk_pct = Number(cap) / 100;
        sizing.capital_base = form.capital_base;
        sizing.rounding = form.rounding;
        return { ...r, sizing };
      });
      if (!touched) throw new Error("no route on this slave carries a sizing block");

      await api.patchSlave(slave.id, { config });
      onDone();
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  }

  if (err && !full) return <div className="banner banner-error">{err}</div>;
  if (!full || !form) return <p className="muted small">Loading...</p>;

  return (
    <div className="slave-risk-form">
      {err && <div className="banner banner-error">{err}</div>}

      {mixed && (
        <div className="banner banner-error">
          These routes do not all size the same way today. Saving gives all {count} of
          them the values below.
        </div>
      )}

      <p className="muted small">
        Sizing lives on each route here rather than on the slave, so this is what a
        slave-wide figure means: the same values written to all <strong>{count}</strong> of
        them. The share each signal stakes comes from what the master&apos;s order actually
        risked, not from a percentage set here; risk calibration above decides how much of it
        this slave takes.
      </p>

      <div className="field-row">
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
          Refuse above (%)
          <input
            type="number"
            step="0.05"
            min="0"
            value={form.max_risk_pct}
            placeholder="none"
            onChange={(e) => setForm({ ...form, max_risk_pct: e.target.value })}
          />
        </label>
      </div>
      <p className="muted small">
        The smallest tradeable size can already risk more than the target -- a leveraged ETF on
        a wide stop does. Over the ceiling the leg is refused rather than sized down, so a
        ceiling close to the target means that instrument simply will not trade.
      </p>

      <div className="field-row">
        <label>
          Rounding
          <select
            value={form.rounding}
            onChange={(e) => setForm({ ...form, rounding: e.target.value })}
          >
            <option value="nearest_min1">Nearest, never below one</option>
            <option value="floor">Floor -- skip rather than round up</option>
          </select>
        </label>
        <label>
          Step down over (% risk off)
          <input
            type="number"
            step="1"
            min="0"
            max="99"
            value={form.max_risk_error}
            placeholder="off"
            onChange={(e) => setForm({ ...form, max_risk_error: e.target.value })}
          />
        </label>
        {/* VISIBLY inert when the tolerance is set, not just described
            as inert in a paragraph below. Two editable fields answering
            one question is how a config comes to hold two answers -- the
            margin ceiling did exactly that, and showed 25%% on a slave
            that was uncapped. */}
        <label className={form.max_risk_error !== "" ? "muted" : undefined}>
          Step down outside (band){form.max_risk_error !== "" ? " -- ignored" : ""}
          <input
            type="number"
            step="0.05"
            min="0"
            max="0.49"
            value={form.round_band}
            placeholder="off"
            disabled={form.max_risk_error !== ""}
            title={form.max_risk_error !== ""
              ? "The risk tolerance decides while it is set. Clear it to use the band again."
              : undefined}
            onChange={(e) => setForm({ ...form, round_band: e.target.value })}
          />
        </label>
      </div>
      <p className="muted small">
        <strong>Floor</strong> skips a signal that does not reach one whole unit rather than
        rounding it up to one.
      </p>
      {/* Both answer "take the bigger contract, or step down to the
          smaller one?" and they ask it differently. The newer one is
          listed first and wins, because a config carrying a stale band
          should not look like it decides anything. */}
      <p className="muted small">
        <strong>% risk off</strong> rounds to the nearest whole contract and steps down to the
        smaller one only when that rounding is further off the intended risk than this. At
        15%, 3.35 contracts becomes 3 (10% off) and 6.65 becomes 7 (5% off), while 1.66 steps
        down to the micros rather than take 2 at 21% over.
      </p>
      <p className="muted small">
        <strong>Band</strong> is the older rule and is ignored while the one above is set. It
        measures distance to a whole contract instead of cost, which cannot tell those cases
        apart: the same 0.65 of a contract is 21% of the intended risk at 1 and 5% at 6, and no
        band width takes the second while refusing the first.
      </p>

      <div className="slave-actions">
        <button className="btn btn-small" onClick={save} disabled={busy}>
          {busy ? "Saving..." : `Save to all ${count} routes`}
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

      const config = await currentConfig(slave.id);
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
        per-strategy calibration resizes both. Naming the masters is what makes the strategy
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

function describeSelection(slave) {
  const mf = slave.magic_filter;
  const scales = slave.risk_scale_by_magic || {};

  if (mf && mf.accept === "none") return "no strategies -- nothing is copied";
  if (!mf || (mf.allow == null && !(mf.deny || []).length)) {
    // WHICH strategies, not how much they stake. This read "every
    // strategy x0.5", echoing the slave's intake calibration into a row
    // about selection -- two rows showing one number, with this one
    // implying the selection screen is where it is set. It is not; the
    // Risk calibration row above owns it.
    return "every strategy";
  }
  const allow = mf.allow || [];
  if (!allow.length) return "every strategy except those denied";
  return allow
    .map((m) => {
      const sc = scales[String(m)] ?? scales[m];
      return sc != null && sc !== 1 ? `${m} x${sc}` : String(m);
    })
    .join(", ");
}

function StrategyForm({ slave, onDone }) {
  const [full, setFull] = useState(null);
  const [mode, setMode] = useState("none");
  const [rows, setRows] = useState([]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  useEffect(() => {
    (async () => {
      try {
        const got = await api.getSlave(slave.id);
        setFull(got);
        const mf = got.config.magic_filter;
        const scales = got.config.risk_scale_by_magic || {};

        let listed = [];
        let m = "none";
        if (mf == null) m = "all";
        else if (Array.isArray(mf)) { m = "listed"; listed = mf.map(String); }
        else if (mf.accept === "none") m = "none";
        else if (mf.accept === "all") m = "all";
        else if (mf.allow && mf.allow.length) { m = "listed"; listed = mf.allow.map(String); }
        else if (mf.allow == null) m = "all";

        setMode(m);
        setRows(listed.map((magic) => ({
          magic,
          scale: String(scales[magic] ?? scales[Number(magic)] ?? 1),
        })));
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
      const config = await currentConfig(slave.id);

      if (mode === "all") {
        delete config.magic_filter;
      } else if (mode === "none") {
        config.magic_filter = { accept: "none" };
      } else {
        const allow = [];
        const scales = {};
        for (const r of rows) {
          const magic = String(r.magic).trim();
          if (!magic) continue;
          if (!/^\d+$/.test(magic)) throw new Error(`"${magic}" is not a magic number`);
          if (allow.includes(Number(magic))) throw new Error(`magic ${magic} is listed twice`);
          allow.push(Number(magic));

          const sc = String(r.scale).trim();
          if (sc === "") continue;
          const n = Number(sc);
          // 0 is a real instruction -- do not trade this strategy here.
          if (Number.isNaN(n) || n < 0) throw new Error(`scale for ${magic} must be 0 or more`);
          if (n !== 1) scales[magic] = n;
        }
        if (!allow.length) {
          throw new Error("add at least one magic, or choose accept none");
        }
        config.magic_filter = { allow };
        if (Object.keys(scales).length) config.risk_scale_by_magic = scales;
        else delete config.risk_scale_by_magic;
      }

      // risk_scale is deliberately NOT written here any more. This form
      // patches the whole config from the snapshot it loaded on open, so
      // while it also owned the slave-wide scale, saving a strategy
      // selection could quietly put back a scale changed on the Risk scale
      // row in the meantime.

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
    <div className="slave-risk-form with-notes">
      {err && <div className="banner banner-error">{err}</div>}

      <div className="mode-row">
        {[
          ["none", "Accept none"],
          ["listed", "Accept listed"],
          ["all", "Accept all"],
        ].map(([value, label]) => (
          <label key={value} className="mode-choice">
            <input
              type="radio"
              name={`sel-${slave.id}`}
              checked={mode === value}
              onChange={() => setMode(value)}
            />
            {label}
          </label>
        ))}
      </div>

      {mode === "listed" && (
        <div className="scale-rows">
          <div className="scale-head">
            <span>Strategy (magic)</span>
            <span>Calibration</span>
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
            + Add magic
          </button>
        </div>
      )}

      {/* The slave-wide scale USED TO BE EDITED HERE, and it was the only
          place it could be. That is how it came to be invisible: the card
          leads with "risk scale x1.00" and the field for it was two
          sections down, inside a form about which strategies are accepted,
          and hidden entirely while the mode was Accept none.

          It now lives on the Risk scale row, where the text is. Two
          editors of one JSON key is also a clobbering hazard -- each
          patches the whole config from its own snapshot -- so this one is
          gone rather than duplicated. */}
      <p className="muted small">
        The default, and per-strategy figures that replace it, are on the{" "}
        <strong>Risk calibration</strong> row above.
      </p>

      <aside className="side-note">
        <p>
          <strong>Accept none</strong> is where a slave starts: a master running every strategy
          should not port all of them here the moment it connects. Opt each one in.
        </p>
        <p>
          <strong>Risk calibration</strong> is how much of what the master staked this slave
          takes, and the figure here multiplies it rather than replacing it.
          1.0 is all of it; the same share on a futures or ETF leg is usually too much, with far
          less leverage than the CFD account it was sized for. <strong>0</strong> means do not
          trade that strategy here.
        </p>
        <p>A route with its own scale wins over this; this wins over the slave-wide value.</p>
      </aside>

      <div className="slave-actions">
        <button className="btn btn-small" onClick={save} disabled={busy}>
          {busy ? "Saving..." : "Save"}
        </button>
      </div>
    </div>
  );
}

/* MagicList went with MagicFilterForm, its only caller. AccountList
   below is the one MasterFilterForm still uses. */
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
      const config = await currentConfig(slave.id);
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

/**
 * The slave-wide default for the order splitter: how many orders one
 * entry goes out in, and how long apart.
 *
 * A ROUTE may set its own and wins over this. There is no field for that
 * here on purpose -- a route's split belongs with the rest of its
 * settings, and a slave-level control that silently loses to a route is
 * the setting-that-saves-cleanly-and-does-nothing this page has had to
 * go back and fix twice.
 */
function OrderSplitForm({ slave, onDone }) {
  const [form, setForm] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  useEffect(() => {
    (async () => {
      try {
        const got = await api.getSlave(slave.id);
        setForm({
          slices: got.config.entry_split_slices ?? "",
          interval: got.config.entry_split_interval ?? "",
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
      const slices = String(form.slices).trim();
      const interval = String(form.interval).trim();

      // Off is BOTH cleared, not one of them. Slices with no interval is
      // one order sent in pieces -- commission per piece and nothing
      // gained -- and the engine reads it as off, so a config that says
      // "4" while nothing splits is the worst of both.
      const off = (slices === "" || Number(slices) <= 1) &&
                  (interval === "" || Number(interval) <= 0);

      let n = null;
      let secs = null;
      if (!off) {
        n = Number(slices);
        secs = Number(interval);
        if (!Number.isInteger(n) || n < 2) {
          throw new Error("slices must be a whole number of 2 or more -- 1 is not a split");
        }
        if (Number.isNaN(secs) || secs <= 0) {
          throw new Error("the delay between slices must be more than 0 seconds");
        }
      }

      // Read-modify-write: PATCH replaces config_json whole, so routes,
      // mirror_map, host and ports travel back untouched or they are
      // deleted by omission.
      const config = await currentConfig(slave.id);
      if (off) {
        delete config.entry_split_slices;
        delete config.entry_split_interval;
      } else {
        config.entry_split_slices = n;
        config.entry_split_interval = secs;
      }

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

  const n = Number(form.slices);
  const secs = Number(form.interval);
  const window = n > 1 && secs > 0 ? (n - 1) * secs : null;

  return (
    <div className="slave-risk-form">
      {err && <div className="banner banner-error">{err}</div>}

      <label>
        Slices
        <input
          type="number"
          min="0"
          step="1"
          value={form.slices}
          placeholder="off"
          onChange={(e) => setForm({ ...form, slices: e.target.value })}
        />
      </label>
      <label>
        Seconds between them
        <input
          type="number"
          min="0"
          step="1"
          value={form.interval}
          placeholder="off"
          onChange={(e) => setForm({ ...form, interval: e.target.value })}
        />
      </label>

      {window != null && (
        <p className="muted small">
          One entry goes out as <strong>{n} orders</strong> over{" "}
          <strong>{window}s</strong>, filling into <strong>one position</strong> -- one
          row, one stop, one thing to roll. The trade has to outlive that window: a
          master CLOSE arriving part-way calls off the orders still to come, and the
          position is whatever had gone out.
        </p>
      )}
      {window == null && (
        <p className="muted small">
          Off. Each entry goes to the broker as a single order. Set both fields to
          split one -- for a size the book cannot absorb in one print.
        </p>
      )}

      <p className="muted small">
        This is the slave-wide default. A route may set its own{" "}
        <code>split_slices</code> / <code>split_interval</code>, which wins over this,
        and <code>split_slices: 0</code> on a route opts it out entirely.
      </p>

      <div className="form-actions">
        <button className="btn btn-primary" onClick={save} disabled={busy}>
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
          // Kept even though there is no field for it: a slave configured
          // before the master published its own risk still has one, and
          // saving must not silently delete the fallback that sizes every
          // signal from a bridge too old to say.
          risk_pct: sz.risk_pct,
          capital_base: sz.capital_base || "balance",
          max_risk_pct: sz.max_risk_pct != null ? +(sz.max_risk_pct * 100).toFixed(4) : "",
          allow_opposing: !!(got.config.netting || {}).allow_opposing,
          max_legs: (got.config.netting || {}).max_legs ?? "",
          max_aggregate_risk_pct:
            (got.config.netting || {}).max_aggregate_risk_pct != null
              ? +((got.config.netting || {}).max_aggregate_risk_pct * 100).toFixed(4)
              : "",
          // Either key, since both have meant this. Saving writes the
          // canonical one and clears the legacy block, so a slave cannot
          // keep two answers to one question.
          max_margin_pct: (() => {
            const n = got.config.netting || {};
            const m = got.config.margin || {};
            const v = n.max_margin_pct != null ? n.max_margin_pct : m.max_pct_of_free;
            return v != null ? +(v * 100).toFixed(4) : "";
          })(),
          balance_adjustment: got.config.balance_adjustment != null ? String(got.config.balance_adjustment) : "",
          min_risk_capital: got.config.min_risk_capital != null ? String(got.config.min_risk_capital) : "",
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
      const config = await currentConfig(slave.id);
      // A route-sized slave gets its LIMITS written and its sizing left
      // alone. Writing a slave-level block is what the hidden button was
      // protecting against: it saves cleanly and is never read.
      const sizingIsEditable = !slave.sizing_per_route;

      if (form.mode === "mirror_lots") {
        if (sizingIsEditable) delete config.sizing;
      } else {
        const sizing = { ...(config.sizing || {}), mode: "pct_risk" };
        // Untouched. There is no field for it any more -- the master's own
        // risk is used -- but it remains the fallback for a bridge that
        // cannot report one, and deleting it here would silently stop
        // those signals sizing at all.
        if (form.risk_pct != null) sizing.risk_pct = form.risk_pct;
        sizing.capital_base = form.capital_base;
        if (form.max_risk_pct === "" || form.max_risk_pct === null) delete sizing.max_risk_pct;
        else sizing.max_risk_pct = Number(form.max_risk_pct) / 100;
        if (sizingIsEditable) config.sizing = sizing;
      }

      const adj = String(form.balance_adjustment).trim();
      if (adj === "" || Number(adj) === 0) delete config.balance_adjustment;
      else {
        if (Number.isNaN(Number(adj))) throw new Error("the balance adjustment must be a number");
        config.balance_adjustment = Number(adj);
      }

      const flr = String(form.min_risk_capital).trim();
      if (flr === "" || Number(flr) === 0) delete config.min_risk_capital;
      else {
        const n = Number(flr);
        if (Number.isNaN(n) || n < 0) throw new Error("the floor must be 0 or more");
        config.min_risk_capital = n;
      }

      const netting = { ...(config.netting || {}), allow_opposing: !!form.allow_opposing };
      const legs = String(form.max_legs).trim();
      if (legs === "") delete netting.max_legs;
      else {
        const n = Number(legs);
        if (!Number.isInteger(n) || n < 1) throw new Error("max legs must be a whole number of 1 or more");
        netting.max_legs = n;
      }
      const marg = String(form.max_margin_pct).trim();
      if (marg === "") {
        delete netting.max_margin_pct;
      } else {
        const n = Number(marg);
        if (Number.isNaN(n) || n <= 0 || n > 100) {
          throw new Error("the margin ceiling must be a percentage above 0 and at most 100");
        }
        netting.max_margin_pct = n / 100;
      }
      // The legacy key is cleared whatever happens. Two keys for one
      // ceiling is how a slave came to show 25% while being uncapped.
      if (config.margin && config.margin.max_pct_of_free != null) {
        const rest = { ...config.margin };
        delete rest.max_pct_of_free;
        if (Object.keys(rest).length) config.margin = rest;
        else delete config.margin;
      }

      const agg = String(form.max_aggregate_risk_pct).trim();
      if (agg === "") delete netting.max_aggregate_risk_pct;
      else {
        const n = Number(agg);
        if (Number.isNaN(n) || n <= 0) throw new Error("the combined risk cap must be above 0");
        netting.max_aggregate_risk_pct = n / 100;
      }
      config.netting = netting;

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

      {/* "Mirror the master's lots" was read as the ENGINE mode, because
          the card said "autonomous"/"mirror" above it with no control. It
          is a different axis -- HOW MUCH, not which engine -- and picking
          it deletes the sizing block. So the word "mirror" is gone from
          here, and the consequence is stated rather than implied. */}
      {!slave.sizing_per_route && (
      <label>
        How much
        <select value={form.mode} onChange={(e) => setForm({ ...form, mode: e.target.value })}>
          <option value="pct_risk">Risk what the master risked</option>
          <option value="mirror_lots">Copy the master&apos;s lot count as-is</option>
        </select>
      </label>
      )}
      {!slave.sizing_per_route && form.mode === "mirror_lots" && (
        <p className="muted small">
          <strong>This is not the engine mode.</strong> It removes this slave&apos;s risk
          sizing: the master&apos;s lot count is sent to the broker unchanged, so a CFD lot
          becomes that many futures contracts or shares. Use <em>How it copies</em> above to
          change engines.
        </p>
      )}

      {slave.sizing_per_route && (
        <p className="muted small">
          Sizing is set on each route on this slave, so there is none to edit here -- a
          slave-level block would save cleanly and never be read. Use{" "}
          <strong>Sizing on every route</strong> above for that. The limits below are not
          sizing and apply either way.
        </p>
      )}

      {!slave.sizing_per_route && form.mode === "pct_risk" && (
        <>
          <p className="muted small">
            The share the master staked on each signal, taken from what its order actually risked
            rather than from a percentage typed here. Per-strategy scale decides how much of it this
            slave takes.
          </p>

          <div className="field-row">
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
              Refuse above (%)
              <input
                type="number"
                step="0.05"
                min="0"
                value={form.max_risk_pct}
                placeholder="none"
                onChange={(e) => setForm({ ...form, max_risk_pct: e.target.value })}
              />
            </label>
          </div>
          <p className="muted small">
            The smallest tradeable size can already risk more than the target -- one gold future on a
            wide stop does. Over the ceiling the leg is refused rather than sized up, so a ceiling
            close to the target means that instrument simply will not trade.
          </p>

          {/* Folded in from its own row: this is what the share is a
              share OF, and the two are only meaningful together. */}
          <div className="field-row">
            <label>
              Balance adjustment
              <input
                type="number"
                step="1000"
                value={form.balance_adjustment}
                placeholder="0"
                onChange={(e) => setForm({ ...form, balance_adjustment: e.target.value })}
              />
            </label>
            <label>
              Never size below
              <input
                type="number"
                step="1000"
                min="0"
                value={form.min_risk_capital}
                placeholder="no floor"
                onChange={(e) => setForm({ ...form, min_risk_capital: e.target.value })}
              />
            </label>
          </div>
          <p className="muted small">
            <strong>Adjustment</strong> corrects a figure that is wrong: 250k pays 50k in tax, the
            broker reports 200k, +50,000 keeps sizing as if it were still 250k, because a withdrawal
            is not a drawdown. <strong>Floor</strong> overrides a figure that is right: that account
            draws to 175k and sizing carries on at the floor.
          </p>
          <p className="muted small">
            A floor is not free -- 1% of a 250,000 floor is 2,500, which on an account holding
            175,000 is 1.43% of it, and the gap widens as the drawdown does. Margin is never measured
            against either; the broker holds it against real equity.
          </p>
        </>
      )}

      {/* These decide whether a signal becomes a trade at all, and were
          reachable only by editing config. Same form as sizing because
          they are read together: how much, and how much is too much. */}
      <div className="field-row">
        <label>
          Max legs per symbol
          <input
            type="number"
            step="1"
            min="1"
            value={form.max_legs}
            placeholder="no limit"
            onChange={(e) => setForm({ ...form, max_legs: e.target.value })}
          />
        </label>
        <label>
          Max combined risk per symbol (%)
          <input
            type="number"
            step="0.5"
            min="0"
            value={form.max_aggregate_risk_pct}
            placeholder="no limit"
            onChange={(e) => setForm({ ...form, max_aggregate_risk_pct: e.target.value })}
          />
        </label>
      </div>

      {/* NO FIELD EXISTED for this, on either form, while both IBKR
          slaves ran with a 40% ceiling set through the API. It is the
          cap that refused a 17,408-share UGL leg at 80% of the account,
          so it is not a setting anybody should have to reach for curl to
          change. Written to netting.max_margin_pct, the canonical key. */}
      <div className="field-row">
        <label>
          Margin ceiling (% of free)
          <input
            type="number"
            step="5"
            min="0"
            max="100"
            value={form.max_margin_pct}
            placeholder="no limit"
            onChange={(e) => setForm({ ...form, max_margin_pct: e.target.value })}
          />
        </label>
        <span />
      </div>
      <p className="muted small">
        Risk and margin are different limits and either can bind first. A tight stop buys a
        large position for the same risk, so a leg can sit well inside its risk ceiling and
        still need most of the account to hold -- 17,408 UGL shares risked 1% and wanted 80%
        of the balance. An autonomous slave refuses the leg and tries the next instrument on
        the route; a mirror slave trims the size to fit.
      </p>
      <p className="muted small">
        Several strategies on one symbol are a single correlated bet. The leg cap counts
        same-side positions; the combined cap adds up what they risk together and refuses the
        one that would cross it. A leg with no stop cannot be measured, so the combined cap
        refuses rather than guesses.
      </p>

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
