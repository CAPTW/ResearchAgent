import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";

import { App } from "./App";
import { AppearanceProvider } from "./v2/AppearanceProvider";
import "./v2/v2.css";

function normalizeBasePath(raw: string | undefined) {
  const value = (raw ?? "/").trim() || "/";
  const normalized = value.startsWith("/") ? value : `/${value}`;
  return normalized.endsWith("/") ? normalized.slice(0, -1) : normalized;
}

const baseFromVite = normalizeBasePath(import.meta.env.BASE_URL);
const currentPath = typeof window !== "undefined" && window.location.pathname ? window.location.pathname : "";
const derivedBasePath = currentPath.startsWith("/app") ? "/app" : baseFromVite;
const finalBasePath = baseFromVite === "/" ? derivedBasePath : baseFromVite;

if (typeof window !== "undefined") {
  const current = window.location.pathname || "/";
  const normalized = current.replace(/\/+$/, "") || "/";
  const defaultedBasePath = finalBasePath === "/" ? "" : finalBasePath;
  const redirectTarget = defaultedBasePath ? `${defaultedBasePath}/` : "/";

  if (normalized === "/" || normalized === finalBasePath) {
    if (normalized !== redirectTarget) {
      window.history.replaceState(null, "", redirectTarget);
    }
  }
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <BrowserRouter basename={finalBasePath}>
      <AppearanceProvider>
        <App />
      </AppearanceProvider>
    </BrowserRouter>
  </React.StrictMode>,
);
