import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

// Self-hosted variable fonts (no external requests, no CSP exception).
import "@fontsource-variable/geist";
import "@fontsource-variable/geist-mono";
import "./index.css";

import App from "./App";

const container = document.getElementById("root");
if (!container) throw new Error("Missing #root container");

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
