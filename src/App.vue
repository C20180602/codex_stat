<script setup>
import { computed, onMounted, ref } from "vue";

const TIME_ZONE = "Asia/Shanghai";
const RANGE_OPTIONS = [
  [1, "近一天"], [3, "近三天"], [7, "近一周"], [30, "近一个月"],
  [90, "近三个月"], [183, "近半年"], [365, "近一年"], [null, "有史以来"],
];

const records = ref([]);
const metrics = ref([]);
const selectedMetric = ref("total_tokens");
const leaderboardRange = ref(7);
const selectedUser = ref(null);
const detailStart = ref("");
const detailEnd = ref("");
const loading = ref(true);
const error = ref("");

const numberFormatter = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 0 });
const calendarFormatter = new Intl.DateTimeFormat("en-CA", {
  timeZone: TIME_ZONE, year: "numeric", month: "2-digit", day: "2-digit",
});
const dateParts = calendarFormatter.formatToParts.bind(calendarFormatter);
const hourFormatter = new Intl.DateTimeFormat("en-CA", {
  timeZone: TIME_ZONE, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", hourCycle: "h23",
});
const hourParts = hourFormatter.formatToParts.bind(hourFormatter);

function calendarDate(timestamp) {
  const parts = Object.fromEntries(dateParts(new Date(timestamp)).map(({ type, value }) => [type, value]));
  return `${parts.year}-${parts.month}-${parts.day}`;
}

function calendarHour(timestamp) {
  const parts = Object.fromEntries(hourParts(new Date(timestamp)).map(({ type, value }) => [type, value]));
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}`;
}

function addDays(date, days) {
  const next = new Date(`${date}T12:00:00Z`);
  next.setUTCDate(next.getUTCDate() + days);
  return next.toISOString().slice(0, 10);
}

const availableDates = computed(() => records.value.map((record) => calendarDate(record.timestamp)).sort());
const lastDate = computed(() => availableDates.value.at(-1) || "");
const firstDate = computed(() => availableDates.value[0] || "");
const users = computed(() => [...new Set(records.value.map((record) => record.username))].sort());

function rangeFor(days) {
  const end = lastDate.value;
  return { start: days ? addDays(end, -(days - 1)) : firstDate.value, end };
}

function inRange(record, start, end) {
  const date = calendarDate(record.timestamp);
  return (!start || date >= start) && (!end || date <= end);
}

function sum(recordsToSum) {
  return recordsToSum.reduce((total, record) => total + (record.metrics[selectedMetric.value] || 0), 0);
}

const leaderboard = computed(() => {
  const { start, end } = rangeFor(leaderboardRange.value);
  return users.value.map((username) => ({
    username,
    total: sum(records.value.filter((record) => record.username === username && inRange(record, start, end))),
  })).sort((a, b) => b.total - a.total);
});

const detailRecords = computed(() => records.value.filter((record) =>
  record.username === selectedUser.value && inRange(record, detailStart.value, detailEnd.value),
));

const selectedDays = computed(() => {
  if (!detailStart.value || !detailEnd.value) return 0;
  return Math.round((new Date(`${detailEnd.value}T12:00:00Z`) - new Date(`${detailStart.value}T12:00:00Z`)) / 86400000) + 1;
});

const grouping = computed(() => {
  if (selectedDays.value <= 5) return "hour";
  return selectedDays.value > 60 ? "month" : "day";
});

function timeBuckets(start, end, grain) {
  if (!start || !end) return [];
  if (grain === "hour") {
    return Array.from({ length: selectedDays.value * 24 }, (_, index) => {
      const day = addDays(start, Math.floor(index / 24));
      return `${day} ${String(index % 24).padStart(2, "0")}`;
    });
  }
  if (grain === "day") return Array.from({ length: selectedDays.value }, (_, index) => addDays(start, index));

  const [startYear, startMonth] = start.split("-").map(Number);
  const [endYear, endMonth] = end.split("-").map(Number);
  const buckets = [];
  let year = startYear;
  let month = startMonth;
  while (year < endYear || (year === endYear && month <= endMonth)) {
    buckets.push(`${year}-${String(month).padStart(2, "0")}`);
    month += 1;
    if (month === 13) { year += 1; month = 1; }
  }
  return buckets;
}

function bucketKey(timestamp, grain) {
  if (grain === "hour") return calendarHour(timestamp);
  if (grain === "day") return calendarDate(timestamp);
  return calendarDate(timestamp).slice(0, 7);
}

function displayLabel(bucket, grain) {
  if (grain === "hour") return `${bucket.slice(5, 10)} ${bucket.slice(11)}时`;
  if (grain === "day") return bucket.slice(5);
  return bucket;
}

const chartData = computed(() => {
  const groups = new Map(timeBuckets(detailStart.value, detailEnd.value, grouping.value).map((bucket) => [bucket, 0]));
  for (const record of detailRecords.value) {
    const key = bucketKey(record.timestamp, grouping.value);
    groups.set(key, (groups.get(key) || 0) + (record.metrics[selectedMetric.value] || 0));
  }
  return [...groups].map(([bucket, value]) => ({ bucket, label: displayLabel(bucket, grouping.value), value }));
});

const chartMax = computed(() => Math.max(...chartData.value.map((item) => item.value), 1));
const metricLabel = computed(() => metrics.value.find((item) => item.key === selectedMetric.value)?.label || "Token 用量");
const detailTotal = computed(() => sum(detailRecords.value));

function chooseUser(username) {
  selectedUser.value = username;
  const range = rangeFor(7);
  detailStart.value = range.start;
  detailEnd.value = range.end;
}

function setDetailPreset(days) {
  const range = rangeFor(days);
  detailStart.value = range.start;
  detailEnd.value = range.end;
}

function ensureValidRange() {
  if (detailStart.value && detailEnd.value && detailStart.value > detailEnd.value) detailEnd.value = detailStart.value;
}

function formatNumber(value) { return numberFormatter.format(value); }

function formatToken(value) {
  const unit = [[1e12, "B"], [1e9, "G"], [1e6, "M"], [1e3, "K"]].find(([threshold]) => Math.abs(value) >= threshold);
  return unit ? `${(value / unit[0]).toFixed(1)}${unit[1]}` : formatNumber(value);
}

async function load() {
  try {
    const response = await fetch("/api/usage");
    if (!response.ok) throw new Error("接口请求失败");
    const data = await response.json();
    records.value = data.records;
    metrics.value = data.metrics;
    selectedMetric.value = data.metrics[0]?.key || "total_tokens";
  } catch (cause) {
    error.value = cause.message;
  } finally {
    loading.value = false;
  }
}

onMounted(load);
</script>

<template>
  <main class="page-shell">
    <header class="site-header">
      <div>
        <p class="eyebrow">CODEX USAGE</p>
        <h1>Token 用量看板</h1>
        <p class="subtle">数据源：codex_usage_data · 时间按中国标准时间（UTC+8）统计</p>
      </div>
      <button class="reload" type="button" @click="load">刷新数据</button>
    </header>

    <p v-if="loading" class="state">正在读取用量数据…</p>
    <p v-else-if="error" class="state error">{{ error }}</p>

    <template v-else>
      <section v-if="!selectedUser" class="overview">
        <div class="section-heading">
          <div>
            <p class="eyebrow">LEADERBOARD</p>
            <h2>用户 {{ metricLabel }} 排行</h2>
          </div>
          <label class="metric-select">指标
            <select v-model="selectedMetric">
              <option v-for="metric in metrics" :key="metric.key" :value="metric.key">{{ metric.label }}</option>
            </select>
          </label>
        </div>
        <div class="pill-row" aria-label="排行榜时间范围">
          <button v-for="([days, label]) in RANGE_OPTIONS" :key="label" type="button" :class="{ active: leaderboardRange === days }" @click="leaderboardRange = days">{{ label }}</button>
        </div>
        <div class="range-note">统计区间：{{ rangeFor(leaderboardRange).start }} 至 {{ rangeFor(leaderboardRange).end }}</div>
        <div class="ranking-card">
          <button v-for="(item, index) in leaderboard" :key="item.username" class="ranking-row" type="button" @click="chooseUser(item.username)">
            <span class="rank">{{ index + 1 }}</span>
            <span class="avatar">{{ item.username.slice(0, 1).toUpperCase() }}</span>
            <span class="username">{{ item.username }}</span>
            <span class="rank-bar"><i :style="{ width: `${(item.total / (leaderboard[0]?.total || 1)) * 100}%` }" /></span>
            <strong>{{ formatToken(item.total) }}</strong>
            <span class="arrow">→</span>
          </button>
        </div>
      </section>

      <section v-else class="detail">
        <button class="back" type="button" @click="selectedUser = null">← 返回排行榜</button>
        <div class="detail-title">
          <div><p class="eyebrow">USER DETAIL</p><h2>{{ selectedUser }} 的用量</h2></div>
          <label class="metric-select">指标
            <select v-model="selectedMetric"><option v-for="metric in metrics" :key="metric.key" :value="metric.key">{{ metric.label }}</option></select>
          </label>
        </div>
        <div class="filters">
          <div class="date-fields">
            <label>开始日期<input v-model="detailStart" type="date" :min="firstDate" :max="lastDate" @change="ensureValidRange" /></label>
            <span>至</span>
            <label>结束日期<input v-model="detailEnd" type="date" :min="firstDate" :max="lastDate" @change="ensureValidRange" /></label>
          </div>
          <div class="pill-row compact">
            <button v-for="([days, label]) in RANGE_OPTIONS" :key="label" type="button" @click="setDetailPreset(days)">{{ label }}</button>
          </div>
        </div>
        <div class="summary-grid">
          <article><span>所选总用量</span><strong>{{ formatToken(detailTotal) }}</strong><small>{{ metricLabel }}</small></article>
          <article><span>统计粒度</span><strong>{{ grouping === 'hour' ? '每小时' : grouping === 'day' ? '每天' : '每月' }}</strong><small>{{ selectedDays }} 天区间；5 天内按小时，60 天以上按月</small></article>
          <article><span>数据记录</span><strong>{{ formatNumber(detailRecords.length) }}</strong><small>原始对话轮次</small></article>
        </div>
        <article class="chart-card">
          <div class="chart-heading"><div><h3>{{ metricLabel }} 柱状统计</h3><p>{{ detailStart }} 至 {{ detailEnd }} · {{ grouping === 'hour' ? '按每小时' : grouping === 'day' ? '按每天' : '按每月' }}汇总</p></div><span>{{ formatToken(detailTotal) }}</span></div>
          <div v-if="chartData.length" class="bar-chart" role="img" :aria-label="`${selectedUser} 的 ${metricLabel} 柱状图`">
            <div v-for="item in chartData" :key="item.bucket" class="bar-column">
              <span class="bar-value">{{ formatToken(item.value) }}</span>
              <div class="bar-track"><i :style="{ height: `${(item.value / chartMax) * 100}%` }" :title="`${item.label}：${formatNumber(item.value)}`" /></div>
              <span class="bar-label">{{ item.label }}</span>
            </div>
          </div>
          <p v-else class="empty">该时间范围没有可展示的记录。</p>
        </article>
      </section>
    </template>
  </main>
</template>
