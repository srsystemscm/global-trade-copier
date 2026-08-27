function describeEvent(e) {
  switch (e.type) {
    case "signal":
      return `${e.action} ${e.symbol} ticket=${e.ticket} ${e.direction ?? ""} ${e.lots ?? ""}`.trim();
    case "slave_open":
      return `slave ${e.slave_id} opened ${e.slave_symbol} (master ${e.master_ticket}) -> ticket ${e.slave_ticket}, size ${e.size}`;
    case "slave_modify":
      return `slave ${e.slave_id} modified ${e.slave_symbol} ticket ${e.slave_ticket} -> sl=${e.sl} tp=${e.tp}`;
    case "slave_close":
      return `slave ${e.slave_id} closed ${e.slave_symbol} ticket ${e.slave_ticket}`;
    case "slave_error":
      return `slave ${e.slave_id} error on ${e.action} ticket=${e.master_ticket}: ${e.message}`;
    default:
      return JSON.stringify(e);
  }
}

function eventKey(e, i) {
  return `${e.type}-${e.ticket ?? e.master_ticket}-${e.slave_id ?? ""}-${e.slave_ticket ?? ""}-${e.emitted_at ?? i}`;
}

function normalizeTradeToEvent(trade) {
  return {
    type: "signal",
    ticket: trade.master_ticket,
    symbol: trade.symbol,
    action: trade.action,
    direction: trade.direction,
    lots: trade.lots,
    price: trade.price,
    sl: trade.sl,
    tp: trade.tp,
    emitted_at: trade.received_at,
  };
}

function formatTime(ts) {
  if (!ts) return "";
  return new Date(ts * 1000).toLocaleTimeString();
}

export default function ActivityFeed({ liveEvents, trades }) {
  const combined = [...liveEvents, ...trades.map(normalizeTradeToEvent)];
  const seen = new Set();
  const deduped = [];
  for (const e of combined) {
    const key = eventKey(e, deduped.length);
    if (seen.has(key)) continue;
    seen.add(key);
    deduped.push(e);
  }
  deduped.sort((a, b) => (b.emitted_at ?? 0) - (a.emitted_at ?? 0));

  return (
    <div className="activity-feed panel">
      {deduped.length === 0 && <p className="muted">No activity yet.</p>}
      <ul>
        {deduped.slice(0, 150).map((e, i) => (
          <li key={eventKey(e, i)} className={`activity-row activity-${e.type}`}>
            <span className="activity-time">{formatTime(e.emitted_at)}</span>
            <span className="activity-desc">{describeEvent(e)}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
