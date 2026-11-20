import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import { useHub } from "./hub.jsx";
import Copier from "./pages/Copier.jsx";
import Dashboard from "./pages/Dashboard.jsx";
import Settings from "./pages/Settings.jsx";

export default function App() {
  return (
    <div className="app-shell">
      {/* Brand and tabs to the left, hub state hard right. The three hub
          facts used to sit in a panel of their own at the top of the
          dashboard, where they cost a full band of vertical space on the
          one page that most needs it -- and were invisible from the
          Copier and Settings tabs, which is where someone is standing
          when the feed drops. */}
      <nav className="top-nav">
        <div className="nav-left">
          <div className="brand">
            <span className="brand-mark">GTC</span>
            <span className="brand-full">Global Trade Copier</span>
          </div>
          <div className="nav-links">
            <NavLink to="/dashboard" className={({ isActive }) => (isActive ? "active" : "")}>
              Dashboard
            </NavLink>
            <NavLink to="/copier" className={({ isActive }) => (isActive ? "active" : "")}>
              Copier
            </NavLink>
            <NavLink to="/settings" className={({ isActive }) => (isActive ? "active" : "")}>
              Settings
            </NavLink>
          </div>
        </div>
        <HubState />
      </nav>
      <main className="main-content">
        <Routes>
          <Route path="/" element={<Navigate to="/dashboard" replace />} />
          <Route path="/dashboard" element={<Dashboard />} />
          <Route path="/copier" element={<Copier />} />
          <Route path="/settings" element={<Settings />} />
        </Routes>
      </main>
    </div>
  );
}

/** Uptime, the ports signals arrive on, and whether this page is still
 *  being told about them. Read at a glance from any tab. */
function HubState() {
  const { status, connected } = useHub();

  return (
    <div className="hub-state">
      {/* UPTIME AND THE PORTS MOVED TO THE MASTERS SECTION on
          2026-11-18. They describe the path a signal takes from a master
          to this hub, so beside the masters they are about something;
          up here they were three hub facts in a row with nothing to
          attach to. The live feed stays: it is about THIS PAGE's
          connection, which is true wherever you are standing. */}
      <div className="hub-state-cell">
        <span className="hub-state-label">Live feed</span>
        <span className={`badge ${connected ? "badge-ok" : "badge-error"}`}>
          {connected ? "connected" : "disconnected"}
        </span>
      </div>
    </div>
  );
}


