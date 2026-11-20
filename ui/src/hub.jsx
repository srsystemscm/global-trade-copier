/**
 * One poller and one socket for the whole app.
 *
 * /status is not a cheap read: it asks every running adapter for its open
 * positions, which is a round trip to IBKR and to each MT5 terminal. The
 * header now shows hub uptime, the signal ports and the live feed on every
 * page, and a second component polling for them would have doubled that
 * traffic against a hub that is copying real money. The page that needs
 * status reads it from here instead.
 *
 * The event socket is shared for the same reason: useEvents() opens a
 * WebSocket per caller, and with the header subscribing there would have
 * been one open on every page as well as the page's own.
 */
import { createContext, useCallback, useContext, useEffect, useState } from "react";
import { api } from "./api.js";
import { useEvents } from "./useEvents.js";

const HubContext = createContext(null);

export function HubProvider({ children }) {
  const [status, setStatus] = useState(null);
  const [error, setError] = useState(null);
  const { events, connected } = useEvents();

  const refresh = useCallback(async () => {
    try {
      setStatus(await api.getStatus());
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

  // A live event means something changed -- a leg filled, a slave errored
  // -- so the polled view is stale a moment after it arrives rather than
  // five seconds later.
  useEffect(() => {
    if (events.length > 0) refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [events.length]);

  return (
    <HubContext.Provider value={{ status, error, refresh, events, connected }}>
      {children}
    </HubContext.Provider>
  );
}

export function useHub() {
  const value = useContext(HubContext);
  if (!value) throw new Error("useHub() outside HubProvider");
  return value;
}
