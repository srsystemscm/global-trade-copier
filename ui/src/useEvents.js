import { useEffect, useRef, useState } from "react";

/** Subscribes to the hub's /ws/events feed, keeping the most recent
 * `maxEvents` in state. Reconnects automatically on disconnect.
 */
export function useEvents(maxEvents = 200) {
  const [events, setEvents] = useState([]);
  const [connected, setConnected] = useState(false);
  const socketRef = useRef(null);

  useEffect(() => {
    let cancelled = false;
    let reconnectTimer;

    function connect() {
      const proto = window.location.protocol === "https:" ? "wss" : "ws";
      const socket = new WebSocket(`${proto}://${window.location.host}/ws/events`);
      socketRef.current = socket;

      socket.onopen = () => !cancelled && setConnected(true);
      socket.onclose = () => {
        if (cancelled) return;
        setConnected(false);
        reconnectTimer = setTimeout(connect, 2000);
      };
      socket.onerror = () => socket.close();
      socket.onmessage = (msg) => {
        const event = JSON.parse(msg.data);
        setEvents((prev) => [event, ...prev].slice(0, maxEvents));
      };
    }

    connect();
    return () => {
      cancelled = true;
      clearTimeout(reconnectTimer);
      socketRef.current?.close();
    };
  }, [maxEvents]);

  return { events, connected };
}
