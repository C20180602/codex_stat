import { createReadStream } from "node:fs";
import { stat } from "node:fs/promises";
import { createServer } from "node:http";
import { extname, join, normalize, resolve } from "node:path";
import { loadUsageData } from "./usage-data.js";

const root = process.cwd();
const distDirectory = resolve(root, "dist");
const MIME_TYPES = { ".css": "text/css", ".html": "text/html", ".js": "text/javascript", ".json": "application/json", ".svg": "image/svg+xml" };

export async function usageData() {
  return loadUsageData();
}

export function startServer(port = process.env.PORT || 4173, host = "0.0.0.0") {
  return createServer(async (request, response) => {
  if (request.url === "/api/usage") {
    try {
      response.writeHead(200, { "Content-Type": "application/json; charset=utf-8" });
      response.end(JSON.stringify(await usageData()));
    } catch (error) {
      response.writeHead(500, { "Content-Type": "application/json; charset=utf-8" });
      response.end(JSON.stringify({ error: error.message }));
    }
    return;
  }
  const requestPath = request.url === "/" ? "/index.html" : request.url.split("?")[0];
  const target = normalize(join(distDirectory, requestPath));
  if (!target.startsWith(distDirectory)) { response.writeHead(403).end(); return; }
  try {
    const file = await stat(target);
    if (!file.isFile()) throw new Error("not a file");
    response.writeHead(200, { "Content-Type": `${MIME_TYPES[extname(target)] || "application/octet-stream"}; charset=utf-8` });
    createReadStream(target).pipe(response);
  } catch {
    response.writeHead(404, { "Content-Type": "text/plain; charset=utf-8" });
    response.end("请先执行 npm run build，再启动服务。");
  }
  }).listen(port, host, () => console.log(`Codex 用量看板：http://${host}:${port}`));
}

if (process.env.RUN_CODEX_USAGE_SERVER === "1") startServer();
