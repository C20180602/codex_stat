# Codex Token 用量看板

一个用于查看 Codex Token 使用量的 Vue/Vite 看板：包含用户排行榜、按用户查看的时间序列和日期范围汇总。

## 快速开始

前置条件：Node.js（建议 LTS）与 Python 3。

```bash
npm install
python3 build.py
npm run dev
```

开发服务器通常运行在 `http://localhost:5173`。`build.py` 会扫描默认的 `/d`、`/e` 目录下各用户的 Codex session 数据，并在本地生成看板数据。

## 常用命令

| 命令 | 用途 |
| --- | --- |
| `python3 build.py` | 完整重建本地用量数据 |
| `python3 update.py` | 基于索引增量更新数据 |
| `npm run dev` | 启动开发服务器 |
| `npm run build` | 构建生产静态文件至 `dist/` |
| `npm start` | 启动生产服务器（先执行构建） |

详情页中，选择不超过 5 天的范围按小时汇总；更长的范围按天汇总。前端请求数据时会重新读取本地数据文件，因此运行 `update.py` 后可直接使用页面的“刷新数据”。

## 项目结构

```text
src/              Vue 前端源码
build.py          全量采集 Codex session 数据
update.py         增量更新采集数据
usage-data.js     用量数据读取与解析
server.js         生产静态文件与 API 服务
docs/             维护和数据格式说明
```

`codex_usage_data/`、`node_modules/`、`dist/` 和 Python 缓存均为本地生成内容，已在 `.gitignore` 中排除，不应提交。数据文件格式、首次采集和定时更新方式见 [数据维护说明](docs/data-maintenance.md)。
