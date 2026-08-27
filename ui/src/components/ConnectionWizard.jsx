import { useState } from "react";
import { api } from "../api.js";

const STEP_LABELS = ["Broker", "Credentials", "Copy Mode", "Sizing", "Review"];

const DEFAULT_FORM = {
  id: "",
  name: "",
  broker_type: "mt4",
  // mt4 credentials
  host: "127.0.0.1",
  port: 5560,
  timeout_ms: 5000,
  // schwab credentials
  futures_account_id: "",
  brokerage_account_id: "",
  // ibkr credentials
  ibkr_host: "127.0.0.1",
  ibkr_port: 7497,
  ibkr_client_id: 1,
  // copy mode
  mode: "mirror",
  symbol_map_text: "",
  atr_period: 14,
  // sizing (autonomous only)
  sizing_mode: "fixed_contracts",
  sizing_contracts: 1,
  sizing_multiplier: 1,
  sizing_notional: 1000,
  sizing_risk_pct: 0.01,
  sizing_capital_base: "balance",
  // risk management (autonomous only)
  breakeven_trigger_atr: 1.0,
  trailing_atr: 1.5,
  poll_interval_seconds: 15,
};

function buildConfig(form) {
  if (form.broker_type === "mt4") {
    return {
      host: form.host,
      port: Number(form.port),
      mode: form.mode,
      timeout_ms: Number(form.timeout_ms),
    };
  }

  let symbolMap = {};
  try {
    symbolMap = form.symbol_map_text ? JSON.parse(form.symbol_map_text) : {};
  } catch {
    symbolMap = {};
  }

  const sizing =
    form.sizing_mode === "fixed_contracts"
      ? { mode: "fixed_contracts", contracts: Number(form.sizing_contracts) }
      : form.sizing_mode === "lot_multiplier"
      ? { mode: "lot_multiplier", multiplier: Number(form.sizing_multiplier) }
      : form.sizing_mode === "dollar_notional"
      ? { mode: "dollar_notional", notional: Number(form.sizing_notional) }
      : { mode: "pct_risk", risk_pct: Number(form.sizing_risk_pct), capital_base: form.sizing_capital_base };

  const base = {
    mode: form.mode,
    symbol_map: symbolMap,
    atr_period: Number(form.atr_period),
    sizing,
    risk_management: {
      breakeven_trigger_atr: Number(form.breakeven_trigger_atr),
      trailing_atr: Number(form.trailing_atr),
      poll_interval_seconds: Number(form.poll_interval_seconds),
    },
  };

  if (form.broker_type === "ibkr") {
    return {
      ...base,
      host: form.ibkr_host,
      port: Number(form.ibkr_port),
      client_id: Number(form.ibkr_client_id),
    };
  }

  return {
    ...base,
    futures_account_id: form.futures_account_id,
    brokerage_account_id: form.brokerage_account_id,
  };
}

function canProceed(step, form) {
  if (step === 0) return form.id.trim() !== "" && form.name.trim() !== "";
  if (step === 1) {
    if (form.broker_type === "mt4") return form.host.trim() !== "" && String(form.port).trim() !== "";
    if (form.broker_type === "ibkr") return form.ibkr_host.trim() !== "" && String(form.ibkr_port).trim() !== "";
    return form.futures_account_id.trim() !== "" && form.brokerage_account_id.trim() !== "";
  }
  return true;
}

export default function ConnectionWizard({ onClose, onCreated }) {
  const [step, setStep] = useState(0);
  const [form, setForm] = useState(DEFAULT_FORM);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState(null);

  function update(field, value) {
    setForm((f) => ({ ...f, [field]: value }));
  }

  function next() {
    setStep((s) => Math.min(s + 1, STEP_LABELS.length - 1));
  }

  function back() {
    setStep((s) => Math.max(s - 1, 0));
  }

  async function handleSubmit() {
    setSubmitting(true);
    setSubmitError(null);
    try {
      const config = buildConfig(form);
      await api.createSlave({ id: form.id, name: form.name, broker_type: form.broker_type, config });
      onCreated();
    } catch (err) {
      setSubmitError(err.message);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h2>Add Slave</h2>
          <button className="btn-icon" onClick={onClose}>
            ×
          </button>
        </div>

        <div className="wizard-steps">
          {STEP_LABELS.map((label, i) => (
            <div key={label} className={`wizard-step ${i === step ? "active" : ""} ${i < step ? "done" : ""}`}>
              {i + 1}. {label}
            </div>
          ))}
        </div>

        <div className="wizard-body">
          {step === 0 && <StepBroker form={form} update={update} />}
          {step === 1 && <StepCredentials form={form} update={update} />}
          {step === 2 && <StepCopyMode form={form} update={update} />}
          {step === 3 && <StepSizing form={form} update={update} />}
          {step === 4 && <StepReview form={form} />}
        </div>

        {submitError && <div className="banner banner-error">{submitError}</div>}

        <div className="wizard-actions">
          <button className="btn" onClick={back} disabled={step === 0}>
            Back
          </button>
          {step < STEP_LABELS.length - 1 && (
            <button className="btn btn-primary" onClick={next} disabled={!canProceed(step, form)}>
              Next
            </button>
          )}
          {step === STEP_LABELS.length - 1 && (
            <button className="btn btn-primary" onClick={handleSubmit} disabled={submitting}>
              {submitting ? "Creating..." : "Create Slave"}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

function StepBroker({ form, update }) {
  return (
    <div className="form-grid">
      <label>
        Slave ID
        <input value={form.id} onChange={(e) => update("id", e.target.value)} placeholder="e.g. schwab-live-1" />
      </label>
      <label>
        Display name
        <input
          value={form.name}
          onChange={(e) => update("name", e.target.value)}
          placeholder="e.g. My Schwab Account"
        />
      </label>
      <label>
        Broker
        <select value={form.broker_type} onChange={(e) => update("broker_type", e.target.value)}>
          <option value="mt4">MT4</option>
          <option value="schwab">Schwab</option>
          <option value="ibkr">Interactive Brokers (dormant)</option>
        </select>
      </label>
    </div>
  );
}

function StepCredentials({ form, update }) {
  if (form.broker_type === "mt4") {
    return (
      <div className="form-grid">
        <label>
          Host
          <input value={form.host} onChange={(e) => update("host", e.target.value)} />
        </label>
        <label>
          Port
          <input type="number" value={form.port} onChange={(e) => update("port", e.target.value)} />
        </label>
        <label>
          Timeout (ms)
          <input type="number" value={form.timeout_ms} onChange={(e) => update("timeout_ms", e.target.value)} />
        </label>
        <p className="muted small">
          This must match the REP port <code>SlaveBridge.mq4</code> binds to on the slave terminal.
        </p>
      </div>
    );
  }
  if (form.broker_type === "ibkr") {
    return (
      <div className="form-grid">
        <label>
          TWS/Gateway host
          <input value={form.ibkr_host} onChange={(e) => update("ibkr_host", e.target.value)} />
        </label>
        <label>
          Port
          <input type="number" value={form.ibkr_port} onChange={(e) => update("ibkr_port", e.target.value)} />
        </label>
        <label>
          Client ID
          <input
            type="number"
            value={form.ibkr_client_id}
            onChange={(e) => update("ibkr_client_id", e.target.value)}
          />
        </label>
        <p className="muted small">
          Dormant: requires <code>pip install ib_insync</code> on the hub and a running TWS or IB Gateway
          (7497 = TWS paper, 7496 = TWS live, 4002 = Gateway paper, 4001 = Gateway live). This slave will be
          created but can't connect until that's set up -- see hub/README.md.
        </p>
      </div>
    );
  }
  return (
    <div className="form-grid">
      <label>
        Futures account ID
        <input value={form.futures_account_id} onChange={(e) => update("futures_account_id", e.target.value)} />
      </label>
      <label>
        Brokerage account ID
        <input
          value={form.brokerage_account_id}
          onChange={(e) => update("brokerage_account_id", e.target.value)}
        />
      </label>
      <p className="muted small">
        After creating this slave, complete the one-time Schwab OAuth exchange via{" "}
        <code>GET /schwab/authorize?slave_id={form.id || "<id>"}</code> before it can place orders.
      </p>
    </div>
  );
}

function StepCopyMode({ form, update }) {
  return (
    <div className="form-grid">
      <label>
        Copy mode
        <select value={form.mode} onChange={(e) => update("mode", e.target.value)}>
          <option value="mirror">Mirror (copy master SL/TP exactly)</option>
          <option value="autonomous">Autonomous (own ATR-based SL/TP + sizing)</option>
        </select>
      </label>
      {form.mode === "autonomous" && (
        <>
          <label>
            Symbol map (JSON)
            <textarea
              rows={4}
              value={form.symbol_map_text}
              onChange={(e) => update("symbol_map_text", e.target.value)}
              placeholder='{"XAUUSD": ["MGC", "GLD"], "US30": "MYM"}'
            />
          </label>
          <label>
            ATR period
            <input type="number" value={form.atr_period} onChange={(e) => update("atr_period", e.target.value)} />
          </label>
        </>
      )}
      {form.mode === "mirror" && (
        <p className="muted small">Mirror mode maps the master symbol to itself 1:1 -- no symbol map needed.</p>
      )}
    </div>
  );
}

function StepSizing({ form, update }) {
  if (form.mode !== "autonomous") {
    return <p className="muted">Mirror mode copies the master's lot size directly -- no sizing configuration needed.</p>;
  }
  return (
    <div className="form-grid">
      <label>
        Sizing mode
        <select value={form.sizing_mode} onChange={(e) => update("sizing_mode", e.target.value)}>
          <option value="fixed_contracts">Fixed contracts/shares</option>
          <option value="lot_multiplier">Lot multiplier</option>
          <option value="dollar_notional">$ notional</option>
          <option value="pct_risk">% risk</option>
        </select>
      </label>

      {form.sizing_mode === "fixed_contracts" && (
        <label>
          Contracts/shares
          <input
            type="number"
            value={form.sizing_contracts}
            onChange={(e) => update("sizing_contracts", e.target.value)}
          />
        </label>
      )}
      {form.sizing_mode === "lot_multiplier" && (
        <label>
          Multiplier
          <input
            type="number"
            step="0.1"
            value={form.sizing_multiplier}
            onChange={(e) => update("sizing_multiplier", e.target.value)}
          />
        </label>
      )}
      {form.sizing_mode === "dollar_notional" && (
        <label>
          Notional ($)
          <input
            type="number"
            value={form.sizing_notional}
            onChange={(e) => update("sizing_notional", e.target.value)}
          />
        </label>
      )}
      {form.sizing_mode === "pct_risk" && (
        <>
          <label>
            Risk % (0.01 = 1%)
            <input
              type="number"
              step="0.001"
              value={form.sizing_risk_pct}
              onChange={(e) => update("sizing_risk_pct", e.target.value)}
            />
          </label>
          <label>
            Capital base
            <select value={form.sizing_capital_base} onChange={(e) => update("sizing_capital_base", e.target.value)}>
              <option value="balance">Balance</option>
              <option value="equity">Equity</option>
              <option value="balance_plus_fixed">Balance + fixed offset</option>
              <option value="fixed_amount">Fixed amount</option>
            </select>
          </label>
        </>
      )}

      <label>
        Breakeven trigger (x ATR)
        <input
          type="number"
          step="0.1"
          value={form.breakeven_trigger_atr}
          onChange={(e) => update("breakeven_trigger_atr", e.target.value)}
        />
      </label>
      <label>
        Trailing distance (x ATR)
        <input
          type="number"
          step="0.1"
          value={form.trailing_atr}
          onChange={(e) => update("trailing_atr", e.target.value)}
        />
      </label>
      <label>
        Risk poll interval (s)
        <input
          type="number"
          value={form.poll_interval_seconds}
          onChange={(e) => update("poll_interval_seconds", e.target.value)}
        />
      </label>
    </div>
  );
}

function StepReview({ form }) {
  const config = buildConfig(form);
  return (
    <div>
      <p>
        <strong>{form.name || "(unnamed)"}</strong> ({form.id || "(no id)"}) -- {form.broker_type} / {form.mode}
      </p>
      <pre className="review-json">{JSON.stringify(config, null, 2)}</pre>
    </div>
  );
}
