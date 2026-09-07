import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import { loadUsageData } from "./usage-data.js";

function usageApi() {
  return {
    name: "codex-usage-api",
    configureServer(server) {
      server.middlewares.use("/api/usage", async (_request, response) => {
        try {
          const data = await loadUsageData();
          response.setHeader("Content-Type", "application/json; charset=utf-8");
          response.end(JSON.stringify(data));
        } catch (error) {
          response.statusCode = 500;
          response.setHeader("Content-Type", "application/json; charset=utf-8");
          response.end(JSON.stringify({ error: `无法读取 codex_usage_data：${error.message}` }));
        }
      });
    },
  };
}

export default defineConfig({ plugins: [vue(), usageApi()] });
