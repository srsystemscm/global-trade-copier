/**
 * The four things a slave is read by, in the four shortest words for them.
 *
 *   MODE   where the SIZE comes from -- the master's staked risk, or the
 *          master's lot count carried across unchanged
 *   RISK   what a percentage is a percentage of
 *   CAP    the ceiling past which a signal is refused
 *   RC     risk calibration: what this slave takes of what the EA signalled
 *
 * The hub decides all four in _sizing_facts() and reports them on every
 * slave, so the tiles, the list and the Risk tab cannot answer
 * differently. They used to be folded out of `sizing_summary`, which is a
 * sentence -- the ceiling was read back out of it with a regex over
 * "capped 2.50%" -- and when the wording changed on 2026-11-16 the line
 * stopped matching and the screens printed the raw summary instead.
 */

/**
 * Read off the structured block, for a hub that has not restarted yet.
 *
 * `sizing_facts` arrived on 2026-11-17 and the hub reports it only once
 * it restarts, which is the user's call to make and not something a UI
 * change should force. Until then these values come from the same
 * fields the screens had: `sizing` where the slave carries one block,
 * and -- only for a slave whose sizing is per route, where there is no
 * structured block at the slave level at all -- the summary sentence.
 * The patterns below were measured against what /status returned for the
 * four live slaves on 2026-11-17, not guessed.
 */
function factsFromOlderHub(slave) {
  const sizing = slave.sizing && typeof slave.sizing === "object" ? slave.sizing : null;
  const summary = slave.sizing_summary || "";

  if (sizing && sizing.mode) {
    return {
      mode: sizing.mode === "pct_risk" ? "master" : sizing.mode,
      basis: sizing.mode === "pct_risk" ? sizing.capital_base || "balance" : null,
      cap_pct: typeof sizing.max_risk_pct === "number" ? sizing.max_risk_pct : null,
      risk_scale: Number(slave.risk_scale ?? 1),
      scale_overrides: Object.keys(slave.risk_scale_by_magic || {}).length,
    };
  }

  const capped = /capped ([\d.]+)%/.exec(summary);
  const staked = /staked risk/.test(summary);
  return {
    mode: staked ? "master" : /mirrors the master's lots/.test(summary) ? "mirror" : null,
    basis: staked ? (/against equity/.test(summary) ? "equity" : "balance") : null,
    cap_pct: capped ? Number(capped[1]) / 100 : null,
    risk_scale: Number(slave.risk_scale ?? 1),
    scale_overrides: Object.keys(slave.risk_scale_by_magic || {}).length,
  };
}

/** Display strings for the four. `null` where the slave does not say. */
export function slaveFacts(slave) {
  const facts = slave?.sizing_facts || factsFromOlderHub(slave || {});
  return {
    mode: word(facts.mode),
    risk: word(facts.basis),
    cap: pct(facts.cap_pct),
    rc: Number(facts.risk_scale ?? 1).toFixed(2),
    // Strategies on a figure of their own. Not a fifth row: it is a mark
    // on RC saying the headline is not the whole answer.
    overrides: facts.scale_overrides || 0,
  };
}

/**
 * What this slave trades ON, in two words. The line under the account
 * name used to carry describe_mode()'s full sentence -- "own stops from
 * ATR, 10 routed instruments" -- which is three lines of wrapped text on
 * a tile and says the mode twice, since the name already carries it.
 */
export function slaveRouting(slave) {
  const routes = slave.routes?.length || 0;
  if (routes) return `${routes} ROUTE${routes === 1 ? "" : "S"}`;
  // mt5-dwx-futures-1 has no `routes` key at all: resolve_routes falls
  // back to map_symbol, so it takes every signal on its own mapped
  // symbol. "0 routes" would read as a slave that trades nothing.
  return slave.mode === "autonomous" ? "MAPPED SYMBOLS" : "MASTER SYMBOLS";
}

function word(value) {
  if (value === null || value === undefined) return null;
  // "mixed" is the hub saying the blocks in force disagree, which on a
  // twenty-route slave is a real state and not a missing answer.
  return String(value).replace(/_/g, " ").toUpperCase();
}

function pct(value) {
  if (value === null || value === undefined) return null;
  // A RANGE, where the routes in force do not agree on a ceiling. The
  // percent sign is said once: "3.50-20.00%" reads as one figure with
  // two ends, where "3.50%-20.00%" reads as two figures.
  if (typeof value === "object") {
    const lo = (value.min * 100).toFixed(2);
    // Open-ended on purpose: a route with no max_risk_pct refuses
    // nothing, and naming a number there would report a ceiling that
    // does not exist.
    if (value.unbounded) return `${lo}%–none`;
    return `${lo}–${(value.max * 100).toFixed(2)}%`;
  }
  if (typeof value !== "number") return word(value);
  return `${(value * 100).toFixed(2)}%`;
}

/**
 * What drives this slave, named as what it is: an ADAPTER, not a broker.
 *
 * `broker_type` is one of mt4 / ibkr / schwab and it selects the adapter
 * class -- and the mt4 adapter drives MT4 and MT5 terminals alike,
 * through SlaveBridge.mq4 and SlaveBridge.mq5. So "mt4" beside Darwinex
 * MT5 Futures was not a wrong value, it was the adapter's name shown
 * where a reader takes it for the platform. Chris, 2026-11-18:
 * "Darwinex MT5 lists the broker as MT4."
 *
 * The hub genuinely does not know which terminal it is attached to: the
 * bridge's STATUS reply carries an account, a balance and a
 * bridge_version, and nothing that says MT4 or MT5. Saying "MT4/MT5" is
 * the whole of what is known. The fix that would let this be exact is
 * one line in each bridge -- a "platform" field on that reply -- which
 * means recompiling and redeploying the EA to every slave terminal.
 */
export function adapterLabel(brokerType) {
  if (brokerType === "mt4") return "MT4/MT5";
  return String(brokerType || "").toUpperCase();
}
