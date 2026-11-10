import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import path from "path";

export default defineConfig({
    plugins: [react(), tailwindcss()],
    resolve: {
        alias: { "@": path.resolve(__dirname, "src") },
        extensions: [".mjs", ".mts", ".ts", ".tsx", ".js", ".jsx", ".json"],
    },
    server: {
        host: "0.0.0.0",
        port: 5173,
        // Dev server behind a reverse proxy: both only apply
        // when the variables are set, so plain local `pnpm dev` is unchanged.
        allowedHosts: process.env.VITE_ALLOWED_HOSTS ? process.env.VITE_ALLOWED_HOSTS.split(",") : undefined,
        hmr: process.env.VITE_HMR_CLIENT_PORT ? { clientPort: Number(process.env.VITE_HMR_CLIENT_PORT) } : undefined,
        proxy: {
            "/api": {
                target: "http://127.0.0.1:1241",
                changeOrigin: true,
            },
            "/mcp": {
                target: "http://127.0.0.1:1241",
                changeOrigin: true,
            },
            "/agent-installation-guide.md": {
                target: "http://127.0.0.1:1241",
                changeOrigin: true,
            },
        },
    },
    build: {
        outDir: "dist",
        emptyOutDir: true,
    },
});
