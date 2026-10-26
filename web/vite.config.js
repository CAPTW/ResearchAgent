import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
function normalizeBasePath(value) {
    const resolved = (value ?? "/app").trim() || "/app";
    const withLeadingSlash = resolved.startsWith("/") ? resolved : `/${resolved}`;
    return withLeadingSlash.endsWith("/") ? withLeadingSlash : `${withLeadingSlash}/`;
}
export default defineConfig({
    base: normalizeBasePath(process.env.WEB_APP_BASE_PATH),
    plugins: [react()],
    server: {
        port: 5173,
    },
});
