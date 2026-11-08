#!/usr/bin/env python3
"""core/work_state.py — 跨会话「未完成事项/工作线状态」追加式补丁层。

数据文件每行一个 JSON 补丁:
    {"ts": <epoch float>, "list": "<列表名>", "fields": {...}}

补丁按 ts (同 ts 按文件行序) 回放:
    * fields 含非空 task -> 作用于该列表下的一个任务, 其余键是任务字段;
    * 否则作用于列表自身状态;
    * 同目标同键最新值获胜, null 删除该键;
    * 任务 status in {done, dropped} 或列表 status=done -> 隐藏不渲染。

本模块只读写 MEMORYCORE_WORK_STATE_PATH 指定的一个 JSONL 文件,
不调 LLM、不碰冷层、不写热层。
"""
import fcntl
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_MAX_FIELDS_JSON_CHARS = 2000
_DEFAULT_MAX_CHARS = 600


def _state_path() -> Path:
    """返回状态文件路径; 每次调用读取环境, 便于测试隔离。"""
    env = os.environ.get("MEMORYCORE_WORK_STATE_PATH", "")
    if env:
        return Path(env).expanduser()
    return Path(os.path.expanduser("~/.hermes/memorycore/work_state.jsonl"))


def _read_entries() -> List[Tuple[int, Dict[str, Any]]]:
    """读取可解析补丁行 (行号, dict); 坏 JSON / 非 dict 行跳过。"""
    path = _state_path()
    entries: List[Tuple[int, Dict[str, Any]]] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for idx, raw in enumerate(fh):
                if not raw.strip():
                    continue
                try:
                    obj = json.loads(raw)
                except Exception:
                    continue
                if not isinstance(obj, dict):
                    continue
                entries.append((idx, obj))
    except FileNotFoundError:
        return []
    except Exception:
        return []
    return entries


def _valid_entry(obj: Dict[str, Any]) -> bool:
    """仅回放结构完整的补丁; 畸形行按容错要求跳过且不抛异常。"""
    try:
        if not isinstance(obj.get("ts"), (int, float)):
            return False
        if isinstance(obj.get("ts"), bool):  # bool 是 int 子类, 明确排除
            return False
        if not isinstance(obj.get("list"), str) or not obj["list"]:
            return False
        if not isinstance(obj.get("fields"), dict):
            return False
        return True
    except Exception:
        return False


def _task_name(value: Any) -> Optional[str]:
    """task 字段的稳定任务名; 仅非空标量视为任务 (空/null 不算)。"""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return str(value) or None


def fold() -> Dict[str, Any]:
    """回放补丁, 返回当前开放的工作线/任务视图。

    返回结构 (仅供 render/server 消费, 语义以任务书为准):
        {
          "lists": {
            list_name: {
              "updated_ts": 列表自身最近补丁 ts,
              "activity_ts": 列表下最近活动 ts (含任务),
              "fields": {列表自身开放字段},
              "tasks": {
                task_name: {"updated_ts": ts, "fields": {任务开放字段}}
              }
            }
          }
        }
    """
    states: Dict[Any, Dict[str, Any]] = {}
    list_names: Dict[str, float] = {}
    entries = _read_entries()
    entries = [(idx, obj) for idx, obj in entries if _valid_entry(obj)]
    entries.sort(key=lambda pair: (pair[1].get("ts"), pair[0]))

    for _idx, obj in entries:
        list_name = obj["list"]
        ts = float(obj["ts"])
        fields = obj["fields"]
        task = _task_name(fields.get("task"))

        if task is not None:
            # 任务补丁: 除 task 保留键外全部作为任务字段回放。
            target = ("task", list_name, task)
            patch = {k: v for k, v in fields.items() if k != "task"}
            list_names[list_name] = max(list_names.get(list_name, 0.0), ts)
        else:
            target = ("list", list_name)
            patch = {k: v for k, v in fields.items() if k != "task"}
            list_names[list_name] = max(list_names.get(list_name, 0.0), ts)

        state = states.setdefault(target, {"fields": {}, "updated_ts": 0.0})
        state["updated_ts"] = ts
        for key, value in patch.items():
            if value is None:
                state["fields"].pop(key, None)
            else:
                state["fields"][key] = value

    # 开放列表: 列表自身 status != done; 任务 status not in {done, dropped}。
    out: Dict[str, Any] = {"lists": {}}
    # 先收集列表自身 patch 出现的所有列表; 仅含任务也会被 list_names 收录。
    for list_name in list_names:
        list_state = states.get(("list", list_name))
        list_fields = dict(list_state["fields"]) if list_state else {}
        if _status_matches(list_fields.get("status"), ("done",)):
            continue
        tasks: Dict[str, Any] = {}
        for target_key, tstate in states.items():
            if (not isinstance(target_key, tuple) or len(target_key) != 3
                    or target_key[0] != "task" or target_key[1] != list_name):
                continue
            tname = target_key[2]
            tfields = dict(tstate["fields"])
            if _status_matches(tfields.get("status"), ("done", "dropped")):
                continue
            tasks[tname] = {
                "updated_ts": float(tstate["updated_ts"]),
                "fields": tfields,
            }
        if not list_fields and not tasks:
            # 列表自身 patch 被清空后且无开放任务 -> 不是开放条目。
            continue
        activity_ts = list_names.get(list_name, 0.0)
        if list_state is not None:
            activity_ts = max(activity_ts, float(list_state["updated_ts"]))
        for t in tasks.values():
            activity_ts = max(activity_ts, float(t["updated_ts"]))
        out["lists"][list_name] = {
            "updated_ts": float(list_state["updated_ts"]) if list_state else 0.0,
            "activity_ts": float(activity_ts),
            "has_list_patch": list_state is not None,
            "fields": list_fields,
            "tasks": tasks,
        }
    return out


def _status_matches(value: Any, wanted: Tuple[str, ...]) -> bool:
    if value is None:
        return False
    try:
        return str(value).strip().lower() in wanted
    except Exception:
        return False


def _fmt_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return str(value)


def _age_text(seconds: float) -> str:
    seconds = max(0.0, float(seconds or 0.0))
    if seconds < 60:
        return "刚刚"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} 分钟前"
    hours = int(seconds // 3600)
    if hours < 24:
        return f"{hours} 小时前"
    days = int(seconds // 86400)
    return f"{days} 天前"


def _field_tail(fields: Dict[str, Any], excluded: Tuple[str, ...] = ()) -> str:
    parts = []
    for key, value in fields.items():
        if key in excluded:
            continue
        if value is None:
            continue
        parts.append(f"{key}={_fmt_value(value)}")
    return ", ".join(parts)


def _render_lines(open_view: Dict[str, Any], now_ts: Optional[float]) -> List[str]:
    now = float(time.time() if now_ts is None else now_ts)
    lists = open_view.get("lists") or {}
    items = []
    for lname, ldata in lists.items():
        # 每行按自己的 updated_ts 降序: 列表用自身状态补丁时间;
        # 从未写过列表自身状态的隐式工作线, 才退回其任务最近活动时间。
        if ldata.get("has_list_patch"):
            sort_ts = ldata.get("updated_ts", 0.0)
        else:
            sort_ts = ldata.get("activity_ts", 0.0)
        items.append((float(sort_ts or 0.0), lname, ldata))
    items.sort(key=lambda pair: (float(pair[0] or 0.0), pair[1]), reverse=True)

    lines: List[str] = []
    for _activity, lname, ldata in items:
        lfields = ldata.get("fields") or {}
        lts = float(ldata.get("updated_ts") or ldata.get("activity_ts") or now)
        tail = _field_tail(lfields)
        if tail:
            line = f"- {lname}: {tail}, {_age_text(now - lts)}更新"
        else:
            line = f"- {lname}, {_age_text(now - lts)}更新"
        lines.append(line)

        tasks = ldata.get("tasks") or {}
        task_items = []
        for tname, tdata in tasks.items():
            task_items.append((float(tdata.get("updated_ts") or 0.0), tname, tdata))
        task_items.sort(key=lambda pair: (float(pair[0] or 0.0), pair[1]),
                        reverse=True)
        for _tts, tname, tdata in task_items:
            tfields = tdata.get("fields") or {}
            tts = float(tdata.get("updated_ts") or now)
            status = tfields.get("status")
            tail = _field_tail(tfields, excluded=("status",))
            if status is not None:
                prefix = f"  - [{_fmt_value(status)}] {tname}"
            else:
                prefix = f"  - {tname}"
            parts = [prefix]
            if tail:
                parts.append(tail)
            parts.append(f"{_age_text(now - tts)}更新")
            lines.append(", ".join(parts))
    return lines


def _truncate_lines(lines: List[str], max_chars: int) -> str:
    """按整行截断; 末尾追加 (另有 N 项...), 提示自身计入预算。"""
    if not lines:
        return ""
    raw = "\n".join(lines)
    if len(raw) <= max_chars:
        return raw
    hint = "(另有 N 项, 详见 memorycore_work_state)"
    kept: List[str] = []
    for line in lines:
        omitted = len(lines) - len(kept) - 1
        candidate_hint = hint.replace("N", str(max(0, omitted)))
        parts = kept + [line]
        text = "\n".join(parts)
        candidate = f"{text}\n{candidate_hint}" if text else candidate_hint
        if len(candidate) <= max_chars:
            kept.append(line)
            continue
        # 当前行放不下: 用提示行替代之后的所有行; 若连提示都放不下则返回空。
        omitted = len(lines) - len(kept)
        final_hint = hint.replace("N", str(max(0, omitted)))
        if kept:
            final = "\n".join(kept) + "\n" + final_hint
        else:
            final = final_hint
        if len(final) <= max_chars:
            return final
        return ""
    return raw


def render(now_ts: Optional[float] = None, max_chars: Optional[int] = _DEFAULT_MAX_CHARS) -> str:
    """渲染当前开放条目为中文短行, 按 updated_ts 降序, 超预算整行截断。"""
    try:
        open_view = fold()
        lines = _render_lines(open_view, now_ts)
        if not lines:
            return ""
        if max_chars is None:
            return "\n".join(lines)
        max_chars = int(max_chars)
        if max_chars <= 0:
            return ""
        return _truncate_lines(lines, max_chars)
    except Exception:
        # 容错: 文件异常/意外数据不得抛出, 也不写文件。
        return ""


def append(list_name: str, fields: Dict[str, Any]) -> Dict[str, Any]:
    """原子追加一条补丁。

    * list_name 必须是非空 str, fields 必须是 dict, 否则 ValueError;
    * fields 序列化后 >2000 字符 -> ValueError 且不落盘;
    * 追加时 fcntl 排他锁 + 单次 O_APPEND 写入 + fsync。
    """
    if not isinstance(list_name, str) or not list_name.strip():
        raise ValueError("list_name 必须是非空字符串")
    if not isinstance(fields, dict):
        raise ValueError("fields 必须是 dict")

    try:
        payload = json.dumps(fields, ensure_ascii=False, separators=(",", ":"))
    except Exception as e:
        raise ValueError(f"fields 无法序列化: {e}") from e
    if len(payload) > _MAX_FIELDS_JSON_CHARS:
        raise ValueError(
            f"fields 序列化后 {len(payload)} 字符, 超过 {_MAX_FIELDS_JSON_CHARS} 上限")

    entry = {
        "ts": time.time(),
        "list": list_name,
        "fields": fields,
    }
    line = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
    path = _state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass  # 已存在 / 无权限等后续 open 会给出真实错误
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        data = line.encode("utf-8")
        written = 0
        while written < len(data):
            n = os.write(fd, data[written:])
            if n <= 0:
                raise OSError("work_state append 写入失败")
            written += n
        os.fsync(fd)
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
    return entry


def _clear_for_tests() -> None:
    """测试辅助: 删除当前路径指向的状态文件 (仅测试使用, 不参与生产语义)。"""
    try:
        _state_path().unlink()
    except FileNotFoundError:
        pass
