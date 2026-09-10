#!/usr/bin/env python3
"""扫描 mihomo UID、检查 Codex 近 24 小时用量并热更新封禁规则。

供每小时的单次 cron 任务调用。脚本会扫描当前连接、合并旧扫描快照的 UID
映射、增量采集 session，用滚动近 24 小时 total_tokens 判断 150M 上限，并
用 mihomo rule-provider API 热更新 ``UID + DOMAIN`` 封禁规则。不会重启 mihomo。
"""

import argparse
import json
import os
import pwd
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from build import DATA_DIR_NAME, DEFAULT_BASE_DIRS
from update import update


DEFAULT_LIMIT = 150_000_000
DEFAULT_DOMAINS = ("chatgpt.com", "api.openai.com")
BLOCK_INDEX_NAME = "codex_block_index.json"
RULE_FILE_NAME = "codex_over_limit.yaml"
LEGACY_SCAN_NAME = "mihomo_uid_scan.json"


def read_json_object(path, missing=None):
    try:
        with path.open("r", encoding="utf-8") as file:
            value = json.load(file)
    except FileNotFoundError:
        return dict(missing or {})
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"无法读取 JSON 文件 {path}: {error}") from error
    if not isinstance(value, dict):
        raise RuntimeError(f"JSON 文件 {path} 的根节点必须是对象。")
    return value


def atomic_write(path, content):
    """在同一目录原子替换文件，避免 mihomo 热更新读到半个 provider。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, text=True)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def write_json(path, value):
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def parse_event_timestamp(timestamp):
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def recent_totals(data_dir, now):
    """返回滚动近 24 小时（含边界）的用户名→total_tokens。"""
    if now.tzinfo is None:
        raise ValueError("now 必须包含时区")
    window_end = now.astimezone(timezone.utc)
    window_start = window_end - timedelta(hours=24)
    totals = {}
    try:
        files = sorted(path for path in data_dir.iterdir() if path.is_file() and path.suffix == ".txt")
    except FileNotFoundError:
        raise RuntimeError(f"找不到用量目录 {data_dir}；请先运行 python3 build.py。")
    except OSError as error:
        raise RuntimeError(f"无法列出用量目录 {data_dir}: {error}") from error

    for path in files:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as error:
            print(f"警告：无法读取 {path}: {error}", file=sys.stderr)
            continue
        total = 0
        for line in lines:
            timestamp, separator, value = line.partition("\t")
            if not separator or "\t" in value:
                continue
            event_time = parse_event_timestamp(timestamp)
            try:
                tokens = int(value)
            except ValueError:
                continue
            if event_time is not None and tokens >= 0 and window_start <= event_time <= window_end:
                total += tokens
        if total:
            totals[path.stem] = total
    return window_start, window_end, totals


def get_connections(controller_url, secret, timeout):
    request = Request(f"{controller_url.rstrip('/')}/connections", method="GET")
    if secret:
        request.add_header("Authorization", f"Bearer {secret}")
    try:
        with urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                raise RuntimeError(f"mihomo /connections 返回 HTTP {response.status}（预期 200）")
            payload = json.load(response)
    except HTTPError as error:
        raise RuntimeError(f"读取 mihomo /connections 失败：HTTP {error.code} {error.reason}") from error
    except URLError as error:
        raise RuntimeError(f"无法连接 mihomo controller：{error.reason}") from error
    except json.JSONDecodeError as error:
        raise RuntimeError(f"mihomo /connections 返回无效 JSON：{error}") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("connections"), list):
        raise RuntimeError("mihomo /connections 响应缺少 connections 数组。")
    return payload["connections"]


def username_for_uid(uid):
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return None


def observed_host(metadata):
    for key in ("host", "sniffHost"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower().rstrip(".")
    return None


def normalize_known_users(raw_users):
    if not isinstance(raw_users, dict):
        return {}
    users = {}
    for username, entry in raw_users.items():
        if not isinstance(username, str) or not username or not isinstance(entry, dict):
            continue
        uid = entry.get("uid")
        if isinstance(uid, bool) or not isinstance(uid, int) or uid < 0:
            continue
        users[username] = {
            "uid": uid,
            "last_seen": entry.get("last_seen") if isinstance(entry.get("last_seen"), str) else None,
            "domains": sorted({item for item in entry.get("domains", []) if isinstance(item, str)}),
            "processes": sorted({item for item in entry.get("processes", []) if isinstance(item, str)}),
        }
    return users


def legacy_scan_users(path):
    """迁移已有 mihomo_uid_scan.json，之后只需运行本脚本。"""
    raw = read_json_object(path, {})
    users = {}
    raw_users = raw.get("users", {})
    if not isinstance(raw_users, dict):
        return users
    for entry in raw_users.values():
        if not isinstance(entry, dict):
            continue
        username, uid = entry.get("username"), entry.get("uid")
        if not isinstance(username, str) or not username or isinstance(uid, bool) or not isinstance(uid, int) or uid < 0:
            continue
        users[username] = {
            "uid": uid,
            "last_seen": raw.get("scanned_at") if isinstance(raw.get("scanned_at"), str) else None,
            "domains": sorted({item for item in entry.get("domains", []) if isinstance(item, str)}),
            "processes": sorted({item for item in entry.get("processes", []) if isinstance(item, str)}),
        }
    return users


def scan_known_users(connections, now):
    """从活跃连接生成用户名→UID 映射；不读取或保存 IP 字段。"""
    discovered = {}
    details = []
    ignored = {"missing_uid": 0, "unknown_username": 0, "missing_host": 0}
    for connection in connections:
        metadata = connection.get("metadata") if isinstance(connection, dict) else None
        if not isinstance(metadata, dict):
            continue
        uid = metadata.get("uid")
        if isinstance(uid, bool) or not isinstance(uid, int) or uid < 0:
            ignored["missing_uid"] += 1
            continue
        username = username_for_uid(uid)
        if not username:
            ignored["unknown_username"] += 1
            continue
        host = observed_host(metadata)
        if not host:
            ignored["missing_host"] += 1
            continue
        entry = discovered.setdefault(username, {"uid": uid, "last_seen": now, "domains": set(), "processes": set()})
        chains = connection.get("chains") if isinstance(connection, dict) else None
        details.append({
            "id": connection.get("id") if isinstance(connection.get("id"), str) else None,
            "uid": uid,
            "username": username,
            "host": host,
            "process": metadata.get("process") if isinstance(metadata.get("process"), str) else None,
            "inbound_name": metadata.get("inboundName") if isinstance(metadata.get("inboundName"), str) else None,
            "rule": connection.get("rule") if isinstance(connection.get("rule"), str) else None,
            "chains": [chain for chain in chains if isinstance(chain, str)] if isinstance(chains, list) else [],
            "start": connection.get("start") if isinstance(connection.get("start"), str) else None,
            "upload_bytes": connection.get("upload") if isinstance(connection.get("upload"), int) else None,
            "download_bytes": connection.get("download") if isinstance(connection.get("download"), int) else None,
        })
        entry["domains"].add(host)
        process = metadata.get("process")
        if isinstance(process, str) and process:
            entry["processes"].add(process)
    users = {
        username: {"uid": entry["uid"], "last_seen": entry["last_seen"], "domains": sorted(entry["domains"]), "processes": sorted(entry["processes"])}
        for username, entry in discovered.items()
    }
    details.sort(key=lambda detail: (detail["uid"], detail["id"] or ""))
    return users, ignored, details


def merge_known_users(existing, migrated, discovered):
    """当前扫描的 UID 优先；历史映射可在用户暂时没有连接时继续用于封禁。"""
    merged = normalize_known_users(existing)
    for username, entry in migrated.items():
        merged.setdefault(username, entry)
    for username, entry in discovered.items():
        merged[username] = entry
    return dict(sorted(merged.items()))


def rules_yaml(blocked, domains):
    lines = [
        "# 由 enforce_codex_limit.py 生成；请勿手工编辑。",
        "# classical provider: Linux UID + Codex domain only",
        "payload:",
    ]
    for entry in blocked:
        for domain in domains:
            lines.append(f"  - 'AND,((UID,{entry['uid']}),(DOMAIN,{domain}))'")
    return "\n".join(lines) + "\n"


def refresh_rule_provider(controller_url, provider_name, secret, timeout):
    """仅热更新规则集合；不会调用 /restart 或 /configs。"""
    url = f"{controller_url.rstrip('/')}/providers/rules/{quote(provider_name, safe='')}"
    request = Request(url, data=b"", method="PUT")
    if secret:
        request.add_header("Authorization", f"Bearer {secret}")
    try:
        with urlopen(request, timeout=timeout) as response:
            if response.status != 204:
                raise RuntimeError(f"mihomo rule-provider 热更新返回 HTTP {response.status}（预期 204）")
    except HTTPError as error:
        raise RuntimeError(f"mihomo rule-provider 热更新失败：HTTP {error.code} {error.reason}") from error
    except URLError as error:
        raise RuntimeError(f"无法连接 mihomo controller：{error.reason}") from error


def state_document(blocked, unresolved, users, connections, window_start, window_end, limit, now, active, scan_ignored):
    return {
        "version": 2,
        "updated_at": now,
        "active": active,
        "limit_tokens": limit,
        "usage_window_start": window_start.isoformat().replace("+00:00", "Z"),
        "usage_window_end": window_end.isoformat().replace("+00:00", "Z"),
        "known_users": users,
        "active_connections": connections,
        "scan_ignored_connections": scan_ignored,
        "unresolved_over_limit_users": unresolved,
        "blocked_users": blocked,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mihomo-controller-url", required=True, help="例如 http://127.0.0.1:9090。")
    parser.add_argument("--mihomo-rule-provider", default="codex-over-limit", help="mihomo 主配置中的 rule-provider 名称。")
    parser.add_argument("--mihomo-secret-env", default="MIHOMO_SECRET", help="mihomo secret 所在环境变量名；设为空以关闭。")
    parser.add_argument("--timeout", type=float, default=10, help="Controller HTTP 超时秒数。")
    parser.add_argument("--base-dir", action="append", type=Path, dest="base_dirs", help="用户家目录父路径；可重复指定。")
    parser.add_argument("--data-dir", type=Path, default=Path.cwd() / DATA_DIR_NAME, help="用量和封禁表目录。")
    parser.add_argument("--rules-file", type=Path, help="生成的 mihomo rule-provider 文件。")
    parser.add_argument("--domain", action="append", default=[], help="拦截的精确 Codex 域名；重复指定以覆盖默认列表。")
    parser.add_argument("--limit-tokens", type=int, default=DEFAULT_LIMIT, help="滚动 24 小时上限，默认 150000000。")
    parser.add_argument("--dry-run", action="store_true", help="扫描并计算，但不写表、不更新 provider。")
    parser.add_argument("--skip-update", action="store_true", help="不先执行 update.py 的增量采集（仅排障）。")
    args = parser.parse_args()

    if not args.mihomo_controller_url.startswith(("http://", "https://")):
        parser.error("--mihomo-controller-url 必须是 http:// 或 https:// URL")
    if not args.mihomo_rule_provider or "/" in args.mihomo_rule_provider:
        parser.error("--mihomo-rule-provider 不能为空且不能含 /")
    if args.timeout <= 0 or args.limit_tokens < 0:
        parser.error("--timeout 必须大于 0，--limit-tokens 必须不小于 0")
    domains = tuple(dict.fromkeys(value.strip().lower() for value in (args.domain or DEFAULT_DOMAINS) if value.strip()))
    if not domains or any("/" in domain or any(char.isspace() for char in domain) for domain in domains):
        parser.error("至少需要一个有效的精确域名")

    data_dir = args.data_dir
    state_path = data_dir / BLOCK_INDEX_NAME
    rules_path = args.rules_file or data_dir / RULE_FILE_NAME
    now_datetime = datetime.now(timezone.utc)
    now = now_datetime.isoformat().replace("+00:00", "Z")
    secret = os.environ.get(args.mihomo_secret_env) if args.mihomo_secret_env else None

    try:
        previous_state = read_json_object(state_path, {"version": 2, "known_users": {}, "blocked_users": []})
        connections = get_connections(args.mihomo_controller_url, secret, args.timeout)
        discovered, scan_ignored, connection_details = scan_known_users(connections, now)
        known_users = merge_known_users(previous_state.get("known_users", {}), legacy_scan_users(data_dir / LEGACY_SCAN_NAME), discovered)
        if not args.skip_update:
            result = update(args.base_dirs or DEFAULT_BASE_DIRS, data_dir)
            print(f"用量增量更新：新增 {result['new_files']}，变更 {result['changed_files']}，追加 {result['events_appended']} 条。")
        window_start, window_end, totals = recent_totals(data_dir, now_datetime)
        over_limit = {username: total for username, total in totals.items() if total > args.limit_tokens}
        blocked, unresolved = [], []
        for username in sorted(over_limit):
            user = known_users.get(username)
            if user is None:
                unresolved.append({"username": username, "total_tokens": over_limit[username], "reason": "未发现 UID"})
            else:
                blocked.append({"username": username, "uid": user["uid"], "total_tokens": over_limit[username]})
        provider = rules_yaml(blocked, domains)
        pending_state = state_document(blocked, unresolved, known_users, connection_details, window_start, window_end, args.limit_tokens, now, False, scan_ignored)
        applied_state = state_document(blocked, unresolved, known_users, connection_details, window_start, window_end, args.limit_tokens, now, True, scan_ignored)
        previous_provider = rules_path.read_text(encoding="utf-8") if rules_path.exists() else None
        needs_update = previous_provider != provider or previous_state.get("blocked_users") != blocked or previous_state.get("active") is not True

        print(f"扫描到 {len(discovered)} 个当前 UID；24 小时超限：{', '.join(over_limit) or '无'}；将封禁：{', '.join(item['username'] for item in blocked) or '无'}。")
        for number, detail in enumerate(connection_details, 1):
            print(f"连接 {number}：UID={detail['uid']} 用户={detail['username']} 域名={detail['host']} 进程={detail['process'] or '未知'} 入站={detail['inbound_name'] or '未知'} 规则={detail['rule'] or '未知'} 链路={' → '.join(detail['chains']) or '无'} 上传/下载={detail['upload_bytes'] if detail['upload_bytes'] is not None else '未知'}/{detail['download_bytes'] if detail['download_bytes'] is not None else '未知'} bytes")
        if unresolved:
            print("未封禁（缺少 UID）：" + ", ".join(item["username"] for item in unresolved), file=sys.stderr)
        if args.dry_run:
            print("dry-run：未写入封禁表、规则文件，也未热更新 mihomo。")
            return
        if previous_provider != provider:
            write_json(state_path, pending_state)
            atomic_write(rules_path, provider)
        if needs_update:
            refresh_rule_provider(args.mihomo_controller_url, args.mihomo_rule_provider, secret, args.timeout)
            write_json(state_path, applied_state)
            print("mihomo rule-provider 已热更新；未重启 mihomo。")
        elif previous_state != applied_state:
            write_json(state_path, applied_state)
        print(f"当前封禁表：{state_path}（封禁 {len(blocked)} 位用户）。")
    except RuntimeError as error:
        parser.exit(1, f"错误：{error}\n")


if __name__ == "__main__":
    main()
