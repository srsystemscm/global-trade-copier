import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import App from "./App.jsx";
import { HubProvider } from "./hub.jsx";
import "./styles.css";

ReactDOM.createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <BrowserRouter>
      {/* Above the router: the header carries hub status on every page,
          and the provider is what keeps that to one poll and one socket. */}
      <HubProvider>
        <App />
      </HubProvider>
    </BrowserRouter>
  </React.StrictMode>
);
