import { readFile, readdir } from "node:fs/promises";
import { join, resolve } from "node:path";

const KNOWN_METRICS = {
  total_tokens: "总 Token 用量",
  input_tokens: "输入 Token 用量",
  output_tokens: "输出 Token 用量",
};

function metricLabel(key) {
  return KNOWN_METRICS[key] || key.replaceAll("_", " ");
}

function parseMetrics(fields) {
  // 兼容现有格式：timestamp<TAB>total_tokens
  if (fields.length === 1 && Number.isFinite(Number(fields[0]))) {
    return { total_tokens: Number(fields[0]) };
  }
  // 后续可使用 JSON：timestamp<TAB>{"input_tokens":12,"output_tokens":34}
  if (fields.length === 1 && fields[0].trim().startsWith("{")) {
    try {
      const parsed = JSON.parse(fields[0]);
      return Object.fromEntries(Object.entries(parsed).filter(([, value]) => Number.isFinite(Number(value))).map(([key, value]) => [key, Number(value)]));
    } catch { return {}; }
  }
  // 或键值格式：timestamp<TAB>input_tokens=12<TAB>output_tokens=34
  return Object.fromEntries(fields.map((field) => field.split("=", 2)).filter(([key, value]) => key && Number.isFinite(Number(value))).map(([key, value]) => [key, Number(value)]));
}

export function parseUsageFile(content, username) {
  return content.split(/\r?\n/).flatMap((line) => {
    if (!line.trim()) return [];
    const [timestamp, ...fields] = line.split("\t");
    const metrics = parseMetrics(fields);
    return timestamp && Object.keys(metrics).length ? [{ timestamp, username, metrics }] : [];
  });
}

export async function loadUsageData(dataDirectory = resolve(process.cwd(), "codex_usage_data")) {
  const entries = await readdir(dataDirectory, { withFileTypes: true });
  const batches = await Promise.all(entries.filter((item) => item.isFile() && item.name.endsWith(".txt")).map(async (item) =>
    parseUsageFile(await readFile(join(dataDirectory, item.name), "utf8"), item.name.slice(0, -4)),
  ));
  const records = batches.flat().sort((a, b) => a.timestamp.localeCompare(b.timestamp));
  const keys = [...new Set(records.flatMap((record) => Object.keys(record.metrics)))];
  return { metrics: keys.map((key) => ({ key, label: metricLabel(key), unit: "tokens" })), records };
}
