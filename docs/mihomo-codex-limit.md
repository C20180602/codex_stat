# Codex 超限 UID 封禁（mihomo）

`enforce_codex_limit.py` 是唯一的定时任务脚本：每次运行读取 mihomo 当前活跃连接的 UID 与域名、增量采集 Codex session、计算滚动近 24 小时用量、维护当前封禁表，并热更新 mihomo 规则。它不会安装 crontab，也不会重启 mihomo。

## mihomo 一次性配置

主配置必须启用 Linux UID 查找，并声明一个文件型 rule-provider。`RULE-SET` 必须置于其他 OpenAI/Codex 匹配规则之前。`path` 必须位于 mihomo HomeDir（本服务器为 `/root/clash-for-linux/runtime`）或已加入 `SAFE_PATHS`。

```yaml
find-process-mode: always

rule-providers:
  codex-over-limit:
    type: file
    behavior: classical
    format: yaml
    path: /path/to/codex_stat/codex_usage_data/codex_over_limit.yaml

rules:
  - RULE-SET,codex-over-limit,REJECT
  # 其余已有 rules
```

生成的每条规则为：

```text
AND,((UID,1001),(DOMAIN,api.openai.com))
```

它只拒绝该 Linux UID 对配置域名的连接，不影响其他 UID 或其他域名。mihomo 的 `classical` rule-provider 支持 `UID` 和逻辑 `AND`；`UID` 仅适用于同一 Linux 主机上的进程，远端客户端必须改用入站认证账号而非 UID。

## 运行与表格

首次需要已有用量数据（未初始化时先运行 `python3 build.py`）。此前的 `codex_usage_data/mihomo_uid_scan.json` 会在首次运行时导入 UID 映射；以后不再需要独立扫描脚本。

```bash
python3 enforce_codex_limit.py \
  --mihomo-controller-url http://127.0.0.1:9090 \
  --rules-file /root/clash-for-linux/runtime/codex_over_limit.yaml
```

如 mihomo 配置了 `secret`，将其放入执行环境的 `MIHOMO_SECRET`，不要写进命令行、cron 或仓库。

封禁表固定写入 `codex_usage_data/codex_block_index.json`，包含：

- `known_users`：用户名→UID、最后发现时间、域名和进程；
- `active_connections`：本轮的无 IP 连接详情；
- `blocked_users`：当前超限且已生成 `UID + DOMAIN` 规则的用户；
- `unresolved_over_limit_users`：超限但暂未发现 UID 的用户，不会误封。

用量严格大于 150,000,000 才封禁；低于或等于上限即从 `blocked_users` 和 provider 中移除。规则文件变化时，脚本会向 Controller 调用 `PUT /providers/rules/codex-over-limit`，成功（HTTP 204）后才将表标记为 `active: true`。该操作只热更新 rule-provider，不调用 `/restart` 或全配置 reload。

可先审查而不写入：

```bash
python3 enforce_codex_limit.py \
  --mihomo-controller-url http://127.0.0.1:9090 \
  --rules-file /root/clash-for-linux/runtime/codex_over_limit.yaml \
  --dry-run
```

## cron（暂不安装）

管理员审查完成后可每小时调用一次；仓库和脚本不会自行添加此条目。

```cron
0 * * * * cd /path/to/codex_stat && /usr/bin/python3 enforce_codex_limit.py --mihomo-controller-url http://127.0.0.1:9090 --rules-file /root/clash-for-linux/runtime/codex_over_limit.yaml >> /var/log/codex-stat/enforce.log 2>&1
```
