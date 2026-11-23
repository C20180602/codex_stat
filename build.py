#!/usr/bin/env python3
"""从 Codex session JSONL 构建按用户保存的单轮 token 用量数据。

对每个 ``event_msg / token_count`` 事件，只提取
``payload.info.last_token_usage.total_tokens``。结果保存到工作目录下的
``codex_usage_data``：每个用户一个 TXT，格式为 ``时间戳<TAB>total_tokens``，
每行一条记录。另会写入 JSONL 文件的修改时间和已读取字节偏移量索引。
"""

import argparse
import fcntl
import json
import os
import stat
import sys
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory


DEFAULT_BASE_DIRS = (Path("/d"), Path("/e"))
DATA_DIR_NAME = "codex_usage_data"
TIME_INDEX_NAME = "jsonl_time_index.json"
OFFSET_INDEX_NAME = "jsonl_offset_index.json"
WHITELIST_CONFIG_PATH = Path(__file__).with_name("config") / "codex_usage_whitelist.json"


@contextmanager
def lock_output_dir(output_dir):
    """串行执行同一输出目录的全量构建和增量更新。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(output_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def write_index(path, data):
    """写完索引再原子替换，使读者始终看到一份完整的 JSON。"""
    with TemporaryDirectory(prefix=f".{path.name}.", dir=path.parent) as directory:
        temporary_path = Path(directory) / path.name
        with temporary_path.open("x", encoding="utf-8", newline="\n") as file:
            json.dump(data, file, ensure_ascii=False, indent=2, sort_keys=True)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except FileNotFoundError:
            pass
        else:
            temporary_path.chmod(mode)
        temporary_path.replace(path)


def load_whitelisted_users(path=WHITELIST_CONFIG_PATH):
    """读取不采集、不封禁的用户名白名单配置。"""
    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except FileNotFoundError:
        raise RuntimeError(f"找不到白名单配置 {path}。")
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"无法读取白名单配置 {path}: {error}") from error
    users = data.get("whitelisted_users") if isinstance(data, dict) else None
    if not isinstance(users, list) or any(not isinstance(user, str) or not user for user in users):
        raise RuntimeError(f"白名单配置 {path} 必须包含字符串数组 whitelisted_users。")
    return frozenset(users)


def rollout_files(base_dirs):
    """迭代 /d、/e 各用户 Codex sessions 下的 JSONL 文件。"""
    whitelisted_users = load_whitelisted_users()
    for base_dir in base_dirs:
        if not base_dir.is_dir():
            continue
        for user_home in base_dir.iterdir():
            if not user_home.is_dir():
                continue
            if user_home.name in whitelisted_users:
                continue
            # Codex 当前目录名为 sessions；也兼容可能存在的旧版 session。
            for directory_name in ("sessions", "session"):
                sessions_dir = user_home / ".codex" / directory_name
                if not sessions_dir.is_dir():
                    continue
                for rollout_path in sessions_dir.rglob("*.jsonl"):
                    if rollout_path.is_file():
                        yield user_home.name, rollout_path


def index_key(username, rollout_path):
    """创建稳定且不会混淆不同用户的 JSONL 索引键。"""
    return f"{username}:{rollout_path.name}"


def token_events(rollout_path, start_offset=0):
    """从 ``start_offset`` 读取 JSONL，返回事件序列和下次读取位置。"""
    events = []
    offset = start_offset
    # 使用二进制模式，使 tell() 明确表示字节偏移量；JSONL 是 UTF-8。
    with rollout_path.open("rb") as file:
        if start_offset:
            file.seek(start_offset - 1)
            previous_byte = file.read(1)
            if not previous_byte:
                raise ValueError(f"偏移量 {start_offset} 超过文件末尾")
            # 已读取的最后一条完整记录可能在下一次追加时才补换行。
            if previous_byte != b"\n" and file.readline().strip():
                raise ValueError(f"偏移量 {start_offset} 落在 JSONL 行中间")
        file.seek(start_offset)
        for raw_line in file:
            try:
                record = json.loads(raw_line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                if not raw_line.endswith(b"\n"):
                    # 写入方可能仍在追加这行，下次从行首重新读取。
                    break
                record = None

            offset = file.tell()
            if not isinstance(record, dict) or record.get("type") != "event_msg":
                continue
            payload = record.get("payload")
            if not isinstance(payload, dict) or payload.get("type") != "token_count":
                continue
            timestamp = record.get("timestamp")
            info = payload.get("info")
            usage = info.get("last_token_usage") if isinstance(info, dict) else None
            total_tokens = usage.get("total_tokens") if isinstance(usage, dict) else None
            # bool 在 Python 是 int 的子类，不应被当成 token 数。
            if not isinstance(timestamp, str) or isinstance(total_tokens, bool) or not isinstance(total_tokens, int):
                continue
            events.append((timestamp, total_tokens))

    return events, offset


def build(base_dirs, output_dir):
    """扫描全部 rollout，写入用户序列及 JSONL 修改时间、偏移量索引。"""
    with lock_output_dir(output_dir):
        return _build(base_dirs, output_dir)


def _build(base_dirs, output_dir):
    events_by_user = defaultdict(list)
    time_index = {}
    offset_index = {}
    files_processed = 0

    for username, rollout_path in rollout_files(base_dirs):
        try:
            modified_time = rollout_path.stat().st_mtime
            events, offset = token_events(rollout_path)
        except OSError as error:
            print(f"无法读取 {rollout_path}: {error}", file=sys.stderr)
            continue

        files_processed += 1
        events_by_user[username].extend(events)
        key = index_key(username, rollout_path)
        time_index[key] = modified_time
        offset_index[key] = offset

    for username, events in events_by_user.items():
        # ISO 8601 timestamp 可直接按字典序得到时间顺序；相同时间保留读取顺序。
        events.sort(key=lambda event: event[0])
        user_file = output_dir / f"{username}.txt"
        with user_file.open("w", encoding="utf-8", newline="\n") as file:
            for timestamp, total_tokens in events:
                file.write(f"{timestamp}\t{total_tokens}\n")

    write_index(output_dir / OFFSET_INDEX_NAME, offset_index)
    write_index(output_dir / TIME_INDEX_NAME, time_index)

    return files_processed, sum(len(events) for events in events_by_user.values()), len(events_by_user)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-dir",
        action="append",
        type=Path,
        dest="base_dirs",
        help="用户家目录的父目录；可重复指定。默认扫描 /d 和 /e。",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path.cwd() / DATA_DIR_NAME,
        help="输出目录，默认为当前目录下的 codex_usage_data。",
    )
    args = parser.parse_args()
    files, events, users = build(args.base_dirs or DEFAULT_BASE_DIRS, args.output_dir)
    print(f"已处理 {files} 个 JSONL，写入 {users} 个用户、{events} 条 token 事件：{args.output_dir}")


if __name__ == "__main__":
    main()
