# 数据维护说明

## 数据来源与隐私

`build.py` 会扫描 `/d` 和 `/e` 下用户目录内的 `.codex/sessions/`（也兼容旧的 `.codex/session/`）JSONL 记录，提取每轮事件的时间戳和 `total_tokens`。

生成的 `codex_usage_data/` 可能包含个人用量信息，因此默认不纳入 Git。若需要共享示例数据，请使用经过脱敏的独立样本，而非直接提交此目录。

## 初始化与更新

首次使用时执行完整采集：

```bash
python3 build.py
```

日常更新使用增量脚本：

```bash
python3 update.py
```

可以重复传入 `--base-dir` 指定其他待扫描的用户目录父路径；使用 `--output-dir` 可将结果写入其他目录。

```bash
python3 build.py --base-dir /d --output-dir /path/to/data
```

## 生成文件格式

每位用户对应 `codex_usage_data/<用户名>.txt`，每行格式为：

```text
ISO-8601 时间戳<TAB>total_tokens
```

目录中还包含两份增量更新索引：

- `jsonl_time_index.json`：已扫描源文件的修改时间。
- `jsonl_offset_index.json`：已读取的字节偏移量。

不要手动编辑索引。若索引损坏、源文件被截断，或需完全重算，请重新执行 `python3 build.py`。

## 定时更新

部署完成后，可按运维策略将 `update.py` 加入任务调度。以 cron 为例：

```cron
*/15 * * * * cd /path/to/codex_stat && /usr/bin/python3 update.py >> logs/update.log 2>&1
```

请先创建 `logs/`，并按实际环境调整项目路径与 Python 路径。
