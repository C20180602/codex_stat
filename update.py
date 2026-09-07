#!/usr/bin/env python3
"""增量更新 Codex session 的单轮 token 用量数据。

本脚本依赖 build.py 创建的 ``codex_usage_data`` 及其中两个索引。新增的
JSONL 会从头读取；修改过的 JSONL 会从 ``jsonl_offset_index.json`` 保存的
字节位置继续读取。新增 token 事件按用户、时间戳排序后追加到用户 TXT。
"""

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

from build import (
    DATA_DIR_NAME,
    DEFAULT_BASE_DIRS,
    OFFSET_INDEX_NAME,
    TIME_INDEX_NAME,
    index_key,
    rollout_files,
    token_events,
)


def read_index(path):
    """读取并校验一个 JSON 对象索引。"""
    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except FileNotFoundError:
        raise RuntimeError(f"找不到索引 {path}；请先运行 python3 build.py。")
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"无法读取索引 {path}: {error}") from error
    if not isinstance(data, dict):
        raise RuntimeError(f"索引 {path} 的根节点必须是 JSON 对象。")
    return data


def write_json(path, data):
    """以可读、稳定的格式保存索引。"""
    with path.open("w", encoding="utf-8", newline="\n") as file:
        json.dump(data, file, ensure_ascii=False, indent=2, sort_keys=True)
        file.write("\n")


def append_user_events(output_dir, events_by_user):
    """将每位用户的本次新增事件排序后追加到其 TXT 文件。"""
    users_updated = 0
    events_appended = 0
    for username, events in events_by_user.items():
        if not events:
            continue
        # ISO 8601 timestamp 的字典序即时间序；相同时间保留 JSONL 扫描顺序。
        events.sort(key=lambda event: event[0])
        user_file = output_dir / f"{username}.txt"
        with user_file.open("a", encoding="utf-8", newline="\n") as file:
            for timestamp, total_tokens in events:
                file.write(f"{timestamp}\t{total_tokens}\n")
        users_updated += 1
        events_appended += len(events)
    return users_updated, events_appended


def update(base_dirs, output_dir):
    """处理新增或有修改的 JSONL，并更新数据文件与索引。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    time_path = output_dir / TIME_INDEX_NAME
    offset_path = output_dir / OFFSET_INDEX_NAME
    time_index = read_index(time_path)
    offset_index = read_index(offset_path)

    new_events_by_user = defaultdict(list)
    index_updates = 0
    new_files = 0
    changed_files = 0
    skipped_files = []

    for username, rollout_path in rollout_files(base_dirs):
        key = index_key(username, rollout_path)
        try:
            stat = rollout_path.stat()
        except OSError as error:
            print(f"无法读取 {rollout_path} 的状态: {error}", file=sys.stderr)
            continue

        if key not in time_index:
            events, offset = token_events(rollout_path)
            new_events_by_user[username].extend(events)
            time_index[key] = stat.st_mtime
            offset_index[key] = offset
            new_files += 1
            index_updates += 1
            continue

        if time_index[key] == stat.st_mtime:
            continue

        previous_offset = offset_index.get(key)
        if (
            isinstance(previous_offset, bool)
            or not isinstance(previous_offset, int)
            or previous_offset < 0
        ):
            skipped_files.append(
                f"{rollout_path}（索引偏移量无效：{previous_offset!r}）"
            )
            continue
        if stat.st_size < previous_offset:
            # 文件被截断或被同名内容替换时，历史 TXT 无法仅靠追加正确修复。
            skipped_files.append(
                f"{rollout_path}（文件大小 {stat.st_size} 小于已记录偏移量 {previous_offset}）"
            )
            continue

        events, offset = token_events(rollout_path, start_offset=previous_offset)
        new_events_by_user[username].extend(events)
        time_index[key] = stat.st_mtime
        offset_index[key] = offset
        changed_files += 1
        index_updates += 1

    users_updated, events_appended = append_user_events(output_dir, new_events_by_user)
    write_json(time_path, time_index)
    write_json(offset_path, offset_index)

    return {
        "new_files": new_files,
        "changed_files": changed_files,
        "index_updates": index_updates,
        "users_updated": users_updated,
        "events_appended": events_appended,
        "skipped_files": skipped_files,
    }


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
        help="build.py 输出的 codex_usage_data 目录。",
    )
    args = parser.parse_args()
    try:
        result = update(args.base_dirs or DEFAULT_BASE_DIRS, args.output_dir)
    except RuntimeError as error:
        parser.exit(1, f"错误：{error}\n")

    print(
        "更新完成："
        f"新增 JSONL {result['new_files']} 个，"
        f"增量 JSONL {result['changed_files']} 个，"
        f"追加 {result['events_appended']} 条事件到 {result['users_updated']} 个用户。"
    )
    if result["skipped_files"]:
        print("以下文件未处理（请在重新构建前确认文件没有被截断或替换）：", file=sys.stderr)
        for item in result["skipped_files"]:
            print(f"- {item}", file=sys.stderr)


if __name__ == "__main__":
    main()
