#!/usr/bin/env python3
"""待办清单（todos）:微信提醒/待办的本地存储。

设计说明（只写自己的代码，不抄参考实现）:
- 存储是 data/todos.json,一条 JSON 数组,每条:
    {id, text, due, created_at, status, cron_id}
  其中 due 是 "YYYY-MM-DDTHH:MM" 形式的本地(北京时间)墙钟时间,
  或 null(纯待办,没有到期时间);status 为 pending / done / cancelled;
  cron_id 是提醒 cron 的任务 id(带提醒的待办才有)。
- 写盘是原子的:先写同一目录下的临时文件,fsync 后 os.replace 覆盖。
  读改写全程持有 todos.lock 的 fcntl 排他锁,worker 与提醒 cron 并发
  时不会互相覆盖(提醒 cron 到时调 todo-done,worker 可能同时在写)。
- 本模块不发任何微信消息、不建 cron、不碰网络;它只负责记账。
  发提醒是提醒 cron 的职责(见 AGENTS.md 新增段落),投递走
  `ilink.py proactive`。
- 时间解析 parse_due 只接受明确的时间:相对时间("半小时后/10 分钟后/
  明天早上 8 点/今晚 9 点")和绝对时间("2026-11-13 08:00")。含糊的
  ("9 点",没说上午下午)、过去的时间、不存在的日期一律抛错,由调用
  方(worker)向用户澄清或拒绝,绝不猜一个时间落库。
"""

import contextlib
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9,本桥要求 3.10+,理论上走不到
    ZoneInfo = None

LOCAL_TZ_NAME = "Asia/Shanghai"

# 状态常量
STATUS_PENDING = "pending"
STATUS_DONE = "done"
STATUS_CANCELLED = "cancelled"
STATUSES = (STATUS_PENDING, STATUS_DONE, STATUS_CANCELLED)

# 相对时间上限:超过 7 天的提醒拒绝(与参考实现的上限一致,避免误建
# 一个月后的 cron 还占着提醒位)。
MAX_RELATIVE_DAYS = 7
# 绝对时间上限:超过 365 天拒绝。
MAX_FUTURE_DAYS = 365


def local_tz():
    """北京时间时区;系统缺 tzdata 时退回固定 +08:00。"""
    if ZoneInfo is not None:
        try:
            return ZoneInfo(LOCAL_TZ_NAME)
        except Exception:
            pass
    from datetime import timezone

    return timezone(timedelta(hours=8))


def data_dir():
    """待办数据目录:与 ilink.py 同一套 ILINK_DATA_DIR 规则。"""
    env = os.environ.get("ILINK_DATA_DIR")
    if env:
        return env
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def todos_path():
    return os.path.join(data_dir(), "todos.json")


def _lock_path():
    return os.path.join(data_dir(), "todos.lock")


@contextlib.contextmanager
def _locked(exclusive=True):
    """持 todos.lock 的文件锁(有 fcntl 才生效,无则退化为不加锁)。"""
    os.makedirs(data_dir(), exist_ok=True)
    try:
        import fcntl
    except ImportError:
        fcntl = None
    if fcntl is None:
        yield
        return
    fd = os.open(_lock_path(), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _load_unlocked():
    """读 todos.json;文件不存在或为空视为空列表,损坏则抛错不猜。"""
    path = todos_path()
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    if not raw.strip():
        return []
    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError(f"todos file corrupt: top level is not a list: {path}")
    return data


def _save_unlocked(items):
    """原子写盘:同目录临时文件 + fsync + os.replace,再 fsync 目录。"""
    os.makedirs(data_dir(), exist_ok=True)
    path = todos_path()
    fd, tmp = tempfile.mkstemp(prefix=".todos.", suffix=".tmp",
                               dir=data_dir())
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        tmp = None
        dir_fd = os.open(data_dir(), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if tmp is not None and os.path.exists(tmp):
            os.unlink(tmp)


def _next_id(items):
    return max((int(i.get("id", 0)) for i in items), default=0) + 1


def _now():
    return datetime.now(local_tz())


# ---------------------------------------------------------------- 时间解析

class AmbiguousTimeError(ValueError):
    """时间含糊(例如裸 "9 点" 没说上午/下午),需要向用户澄清。"""


class PastTimeError(ValueError):
    """时间已过去,拒绝落库。"""


_CN_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _num(s):
    """解析阿拉伯数字或一位中文数字。"""
    s = s.strip()
    if s.isdigit():
        return int(s)
    if s in _CN_NUM:
        return _CN_NUM[s]
    raise ValueError(f"not a number: {s!r}")


def _apply_period(hour, period):
    """按 上午/下午/晚上/中午 调整 12 小时制小时;返回(小时, 是否含糊)。

    规则(与参考实现一致):
    - 无时段词且 1<=hour<=12 -> 含糊,需要澄清。
    - 下午/晚上 + hour<12 -> +12;上午 + hour==12 -> 0(凌晨 0 点)。
    - 下午/晚上 + hour==12 -> 含糊(12 点是中午还是半夜说不清)。
    - 上午 + hour>12 -> 非法。
    """
    if period is None:
        if 1 <= hour <= 12:
            return hour, True
        return hour, False
    if period in ("下午", "晚上"):
        if hour == 12:
            return hour, True
        if hour == 0:
            raise ValueError("下午/晚上不能是 0 点")
        if hour < 12:
            return hour + 12, False
        return hour, False
    if period == "上午":
        if hour > 12:
            raise ValueError("上午不能超过 12 点")
        if hour == 12:
            return 0, False
        return hour, False
    if period == "中午":
        if hour != 12:
            raise ValueError("中午只能是 12 点")
        return 12, False
    if period in ("早上", "凌晨"):
        if hour > 12:
            raise ValueError("早上/凌晨不能超过 12 点")
        if hour == 12:
            return 0, False
        return hour, False
    raise ValueError(f"unknown period: {period!r}")


def _check_future(dt, now):
    if dt <= now:
        raise PastTimeError(f"时间已过去: {dt.strftime('%Y-%m-%d %H:%M')}")
    if dt - now > timedelta(days=MAX_FUTURE_DAYS):
        raise ValueError("时间太远(超过 365 天),请换一个近期的时间")
    return dt


def parse_due(text, now=None):
    """把用户说的时间换算成北京时间 datetime(带时区)。

    接受的形式:
    - ISO: "2026-11-13 08:00" / "2026-11-13T08:00" / "2026/11/13 08:00"
    - 相对: "半小时后" / "10 分钟后" / "2 小时后" / "3 天后"
    - 今天/明天/后天 + 时段 + 时间:
      "今晚 9 点" / "明天早上 8 点" / "明天下午 3 点半" /
      "今天 21:00" / "明天 9 点 30 分"
    - 裸时钟 "21:00"/"21 点"(24 小时制,无歧义)视为今天。
    含糊、过去、不存在的日期抛 AmbiguousTimeError / PastTimeError /
    ValueError,调用方负责向用户澄清或拒绝,不要在这里猜。
    """
    if text is None:
        return None
    s = str(text).strip()
    if not s or s in ("-", "none", "None", "无"):
        return None
    now = now or _now()
    tz = local_tz()

    # -- ISO 绝对时间(按北京时间解释) --
    m = re.match(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})[ T](\d{1,2}):(\d{2})$", s)
    if m:
        y, mo, d, h, mi = (int(g) for g in m.groups())
        try:
            dt = datetime(y, mo, d, h, mi, tzinfo=tz)
        except ValueError:
            raise ValueError(f"不存在的日期时间: {s!r}")
        if h > 23 or mi > 59:
            raise ValueError(f"不存在的时间: {s!r}")
        return _check_future(dt, now)

    # -- 相对时间:半小时后 / N 分钟后 / N 小时后 / N 天后 --
    if s in ("半小时后", "半个小时后"):
        return _check_future(now + timedelta(minutes=30), now)
    m = re.match(r"^(\d+)\s*分钟后$", s)
    if m:
        mins = int(m.group(1))
        if mins <= 0:
            raise ValueError("分钟数必须是正数")
        if mins > MAX_RELATIVE_DAYS * 24 * 60:
            raise ValueError("相对时间超过 7 天,请用具体日期")
        return _check_future(now + timedelta(minutes=mins), now)
    m = re.match(r"^(\d+)\s*小时后$", s)
    if m:
        hours = int(m.group(1))
        if hours <= 0:
            raise ValueError("小时数必须是正数")
        if hours > MAX_RELATIVE_DAYS * 24:
            raise ValueError("相对时间超过 7 天,请用具体日期")
        return _check_future(now + timedelta(hours=hours), now)
    m = re.match(r"^([一二两三四五六七九十\d]+)\s*小时后$", s)
    if m:
        try:
            hours = _num(m.group(1))
        except ValueError:
            hours = None
        if hours:
            return _check_future(now + timedelta(hours=hours), now)
    m = re.match(r"^(\d+)\s*天后$", s)
    if m:
        days = int(m.group(1))
        if days <= 0 or days > MAX_RELATIVE_DAYS:
            raise ValueError("天数必须在 1-7 之间")
        return _check_future(now + timedelta(days=days), now)

    # -- 今天/明天/后天/今晚/明晚 + 时段 + 时间 --
    m = re.match(
        r"^(今天|明天|后天|今晚|明晚)?\s*(早上|上午|中午|下午|晚上|凌晨)?\s*"
        r"(\d{1,2})\s*点(?:\s*半|(?:\s*(\d{1,2})\s*分?))?$", s)
    if m:
        day_word, period, hour_s, minute_s = m.groups()
        hour = int(hour_s)
        minute = 30 if ("半" in s and minute_s is None) else (
            int(minute_s) if minute_s is not None else 0)
        if hour > 23 or minute > 59:
            raise ValueError(f"不存在的时间: {s!r}")
        if day_word in ("今晚", "明晚"):
            period = period or "晚上"
            day_word = "今天" if day_word == "今晚" else "明天"
        day_offset = {"今天": 0, "明天": 1, "后天": 2}.get(day_word, 0)
        hour, ambiguous = _apply_period(hour, period)
        if ambiguous:
            raise AmbiguousTimeError(
                f"{s!r} 没说上午还是下午,请明确说例如\"明天下午 3 点\"")
        base = now + timedelta(days=day_offset)
        dt = datetime(base.year, base.month, base.day, hour, minute,
                      tzinfo=tz)
        return _check_future(dt, now)

    # -- 今天/明天/后天 + H:MM --
    m = re.match(r"^(今天|明天|后天)?\s*(\d{1,2}):(\d{2})$", s)
    if m:
        day_word, hour_s, minute_s = m.groups()
        hour, minute = int(hour_s), int(minute_s)
        if hour > 23 or minute > 59:
            raise ValueError(f"不存在的时间: {s!r}")
        day_offset = {"今天": 0, "明天": 1, "后天": 2}.get(day_word, 0)
        base = now + timedelta(days=day_offset)
        dt = datetime(base.year, base.month, base.day, hour, minute,
                      tzinfo=tz)
        return _check_future(dt, now)

    raise ValueError(
        f"没理解时间 {s!r},请换个说法,例如\"10 分钟后\"或\"明天下午 3 点\"")


def format_due(dt):
    """datetime -> todos.json 里存的 due 字符串(本地墙钟,无秒无时区)。"""
    if dt is None:
        return None
    return dt.astimezone(local_tz()).strftime("%Y-%m-%dT%H:%M")


def parse_stored_due(due):
    """把存的 due 字符串解析回带时区的 datetime;due 为空返回 None。"""
    if not due:
        return None
    return datetime.strptime(due, "%Y-%m-%dT%H:%M").replace(tzinfo=local_tz())



# ---------------------------------------------------------------- 记账 API

def add(text, due=None, cron_id=None):
    """新增一条待办,返回该条 dict。

    text:待办内容(必填,去首尾空白后不能为空)。
    due:到期时间,可以是 datetime、parse_due 接受的字符串、已格式化的
      "YYYY-MM-DDTHH:MM" 字符串,或 None(纯待办)。
    cron_id:提醒 cron 的任务 id(可选,创建提醒时由 worker 回填)。
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("待办内容不能为空")
    if isinstance(due, datetime):
        due_str = format_due(due)
    elif isinstance(due, str) and due.strip():
        due_str = format_due(parse_due(due))
    elif due is None:
        due_str = None
    else:
        raise ValueError(f"非法的 due: {due!r}")
    with _locked(exclusive=True):
        items = _load_unlocked()
        item = {
            "id": _next_id(items),
            "text": text,
            "due": due_str,
            "created_at": _now().strftime("%Y-%m-%dT%H:%M:%S"),
            "status": STATUS_PENDING,
            "cron_id": cron_id,
        }
        items.append(item)
        _save_unlocked(items)
    return item


def get(todo_id):
    """按 id 取一条,找不到返回 None。"""
    with _locked(exclusive=False):
        for item in _load_unlocked():
            if int(item.get("id", -1)) == int(todo_id):
                return item
    return None


def list_todos(include_done=False, include_cancelled=False):
    """列出待办:默认只列 pending;按 due 升序(无 due 的排最后),再按 id。

    include_done=True 时同时列 done;include_cancelled=True 时同时列
    cancelled(查询清单一般不需要 cancelled,除非显式要看)。
    """
    with _locked(exclusive=False):
        items = _load_unlocked()
    wanted = {STATUS_PENDING}
    if include_done:
        wanted.add(STATUS_DONE)
    if include_cancelled:
        wanted.add(STATUS_CANCELLED)
    items = [i for i in items if i.get("status") in wanted]

    def sort_key(i):
        due = parse_stored_due(i.get("due"))
        return (due is None, due or datetime.max.replace(tzinfo=local_tz()),
                int(i.get("id", 0)))

    return sorted(items, key=sort_key)


def _set_status(todo_id, status):
    """把一条 pending 改成指定状态;成功 True,找不到或已非 pending 则 False。"""
    with _locked(exclusive=True):
        items = _load_unlocked()
        for item in items:
            if int(item.get("id", -1)) == int(todo_id):
                if item.get("status") != STATUS_PENDING:
                    return False
                item["status"] = status
                if status == STATUS_DONE:
                    item["completed_at"] = _now().strftime("%Y-%m-%dT%H:%M:%S")
                _save_unlocked(items)
                return True
    return False


def done(todo_id):
    """标记完成(提醒 cron 到时投递后调这个,防重复提醒)。"""
    return _set_status(todo_id, STATUS_DONE)


def cancel(todo_id):
    """取消一条待办(用户说"取消待办 N"时调这个;提醒 cron 被删时也要调)。"""
    return _set_status(todo_id, STATUS_CANCELLED)


def set_cron_id(todo_id, cron_id):
    """回填提醒 cron 的任务 id;找不到返回 False。"""
    with _locked(exclusive=True):
        items = _load_unlocked()
        for item in items:
            if int(item.get("id", -1)) == int(todo_id):
                item["cron_id"] = cron_id
                _save_unlocked(items)
                return True
    return False


def cleanup(days=30):
    """清理 done/cancelled 超过 days 天的旧条目,返回清理条数。

    pending 的条目永不清理(没完成的待办不能悄悄消失)。
    """
    cutoff = _now() - timedelta(days=days)
    removed = 0
    with _locked(exclusive=True):
        items = _load_unlocked()
        kept = []
        for item in items:
            if item.get("status") in (STATUS_DONE, STATUS_CANCELLED):
                stamp = item.get("completed_at") or item.get("created_at")
                try:
                    when = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S").replace(
                        tzinfo=local_tz())
                except (TypeError, ValueError):
                    when = None
                if when is not None and when < cutoff:
                    removed += 1
                    continue
            kept.append(item)
        if removed:
            _save_unlocked(kept)
    return removed


def format_list(items):
    """把 list_todos 的结果排成发给微信的一行行文本(供 worker 直接拼回复)。"""
    lines = []
    for i in items:
        due = i.get("due")
        due_s = due.replace("T", " ") if due else "无到期时间"
        mark = {"pending": "", "done": " [已完成]",
                "cancelled": " [已取消]"}.get(i.get("status"), "")
        lines.append(f"{i['id']}. {i['text']}({due_s}){mark}")
    return "\n".join(lines)


# ---------------------------------------------------------------- CLI 入口(供 ilink.py main 分发调用)

def cmd_todo_add(due, text):
    """ilink.py todo-add <due> <text> ;due 为 "-" 表示无到期时间。"""
    try:
        item = add(text, due=None if due == "-" else due)
    except (AmbiguousTimeError, PastTimeError, ValueError) as e:
        print(f"TODO_TIME_ERROR:{e}")
        return 1
    print(f"TODO_OK:{item['id']}")
    print(format_list([item]))
    return 0


def cmd_todo_list(include_done=False):
    """ilink.py todo-list [--all] ;只列 pending,除非 --all。"""
    items = list_todos(include_done=include_done)
    if not items:
        print("TODO_EMPTY")
        return 0
    print(format_list(items))
    return 0


def cmd_todo_done(todo_id):
    """ilink.py todo-done <id>。"""
    try:
        tid = int(todo_id)
    except (TypeError, ValueError):
        print("usage: ilink.py todo-done <id>", file=sys.stderr)
        return 2
    if done(tid):
        print(f"TODO_DONE:{tid}")
        return 0
    print(f"TODO_NOT_FOUND:{tid}")
    return 1


def cmd_todo_show(todo_id):
    """ilink.py todo-show <id>:打印一条的完整记录(含 cron_id,供 worker
    取提醒 cron 的任务 id 以便取消提醒时 cron.remove)。"""
    try:
        tid = int(todo_id)
    except (TypeError, ValueError):
        print("usage: ilink.py todo-show <id>", file=sys.stderr)
        return 2
    item = get(tid)
    if item is None:
        print(f"TODO_NOT_FOUND:{tid}")
        return 1
    print(json.dumps(item, ensure_ascii=False))
    return 0


def cmd_todo_set_cron(todo_id, cron_id):
    """ilink.py todo-set-cron <id> <cron_id>:回填提醒 cron 的任务 id。"""
    try:
        tid = int(todo_id)
    except (TypeError, ValueError):
        print("usage: ilink.py todo-set-cron <id> <cron_id>", file=sys.stderr)
        return 2
    if set_cron_id(tid, cron_id):
        print(f"TODO_CRON:{tid}:{cron_id}")
        return 0
    print(f"TODO_NOT_FOUND:{tid}")
    return 1


def cmd_todo_cancel(todo_id):
    """ilink.py todo-cancel <id>。"""
    try:
        tid = int(todo_id)
    except (TypeError, ValueError):
        print("usage: ilink.py todo-cancel <id>", file=sys.stderr)
        return 2
    if cancel(tid):
        print(f"TODO_CANCELLED:{tid}")
        return 0
    print(f"TODO_NOT_FOUND:{tid}")
    return 1


def cmd_todo_cleanup(days=30):
    """ilink.py todo-cleanup [--days N]。"""
    print(f"TODO_CLEANED:{cleanup(days)}")
    return 0


if __name__ == "__main__":
    # 独立自测入口:python3 todos.py add/list/done/cancel/cleanup
    # (生产入口是 ilink.py 的 todo-add/todo-list/todo-done 分发。)
    argv = sys.argv[1:]
    if not argv:
        print("usage: todos.py add <due|-> <text> | list [--all] | "
              "done <id> | cancel <id> | cleanup [--days N]")
        sys.exit(2)
    verb = argv[0]
    if verb == "add" and len(argv) >= 3:
        sys.exit(cmd_todo_add(argv[1], " ".join(argv[2:])))
    if verb == "list":
        sys.exit(cmd_todo_list(include_done="--all" in argv[1:]))
    if verb == "done" and len(argv) == 2:
        sys.exit(cmd_todo_done(argv[1]))
    if verb == "show" and len(argv) == 2:
        sys.exit(cmd_todo_show(argv[1]))
    if verb == "set-cron" and len(argv) == 3:
        sys.exit(cmd_todo_set_cron(argv[1], argv[2]))
    if verb == "cancel" and len(argv) == 2:
        sys.exit(cmd_todo_cancel(argv[1]))
    if verb == "cleanup":
        days = 30
        if "--days" in argv and argv.index("--days") + 1 < len(argv):
            days = int(argv[argv.index("--days") + 1])
        sys.exit(cmd_todo_cleanup(days))
    print("usage: todos.py add <due|-> <text> | list [--all] | "
          "done <id> | cancel <id> | cleanup [--days N]", file=sys.stderr)
    sys.exit(2)
