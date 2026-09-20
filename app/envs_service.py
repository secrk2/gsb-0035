"""织云系统 - 环境管理 / 发布窗口 / 应用健康 领域服务。

设计要点：
- 环境是「应用级实体」（app_environments）：开发/预发/生产不再全局写死，
  业务线可在单个应用下自行增删、改名；env_key 稳定（内置 dev/test/staging/prod，
  自定义环境生成 c<n>），配置项、版本、实例、窗口全部挂 env_key。
- 发布窗口 = 周计划（deploy_windows：周几 + 时段，可多条）+ 节假日封网
  （deploy_window_blocks：某天单独关掉，优先级最高）。周计划为空表示不设限，
  此时仅封网日生效。窗口判定在服务端强制执行，窗口外发布被拒并给出下一次可发布时间，
  不只靠前端置灰按钮。
- 实例健康（app_instances + instance_events）：存活/掉线、累计与近 24h 重启次数、
  最后一次重启时间；近 24h 重启达到阈值判定为「反复重启」，与正常重启分级区分。
- 窗口改动、封网、环境增删、发布拦截、实例掉线/重启/恢复统一写 ops_event_logs，
  与配置逐键流水（config_audit_logs）平行，均可按应用与时间窗查询。
"""
import time

from .db import (
    BUILTIN_ENVIRONMENTS, BUILTIN_ENV_LABELS, get_conn, query, query_one,
)

WEEKDAY_LABELS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

# 健康分级阈值（近 24 小时重启次数）
CRASH_LOOP_RESTARTS = 5     # ≥5 次：反复重启（高危，作战台红色告警）
FREQUENT_RESTARTS = 3       # 3~4 次：重启偏多（黄色关注）
HEALTH_WINDOW_SECONDS = 24 * 3600
# 向前扫描下一次窗口的最长天数
WINDOW_SCAN_DAYS = 14
LABEL_MAX_LEN = 16

# 运维留痕动作字典
OPS_ACTION_LABELS = {
    "env_create": "新建环境",
    "env_rename": "环境改名",
    "env_delete": "删除环境",
    "window_update": "修改发布窗口",
    "block_add": "新增封网日",
    "block_remove": "解除封网日",
    "deploy_ok": "窗口内发布",
    "deploy_blocked": "窗口外发布被拦截",
    "register": "实例注册",
    "restart": "实例重启",
    "offline": "实例掉线",
    "recover": "实例恢复",
}
OPS_CATEGORY_LABELS = {
    "env": "环境管理",
    "window": "发布窗口",
    "deploy": "发布",
    "instance": "实例健康",
}


# ---------------------------------------------------------------- 基础工具

def now_ts() -> int:
    return int(time.time())


def env_label(app_id: int, env_key: str) -> str:
    """环境显示名：优先应用级自定义名，其次内置名，最后回落到 key。"""
    row = query_one("SELECT label FROM app_environments WHERE app_id = ? AND env_key = ?",
                    (app_id, env_key))
    if row:
        return row["label"]
    return BUILTIN_ENV_LABELS.get(env_key, env_key)


def list_app_envs(app_id: int) -> list[dict]:
    return [dict(r) for r in query(
        "SELECT * FROM app_environments WHERE app_id = ? ORDER BY sort_no, id", (app_id,))]


def get_env(app_id: int, env_key: str):
    return query_one("SELECT * FROM app_environments WHERE app_id = ? AND env_key = ?",
                     (app_id, env_key))


def require_env(app_id: int, env_key: str) -> dict:
    row = get_env(app_id, env_key)
    if not row:
        raise LookupError(f"该应用下不存在环境「{env_key}」，可能已被删除；请刷新环境列表")
    return dict(row)


def _parse_hm(value: str) -> int:
    """HH:MM → 当日分钟数；24:00 = 1440。"""
    parts = value.split(":")
    if len(parts) != 2:
        raise ValueError(f"时间格式应为 HH:MM：{value}")
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h <= 24 and 0 <= m <= 59) or (h == 24 and m != 0):
        raise ValueError(f"时间超出范围：{value}")
    return h * 60 + m


def validate_hm(value: str, allow_2400: bool = False) -> str:
    s = str(value).strip()
    minutes = _parse_hm(s)
    if minutes == 1440 and not allow_2400:
        raise ValueError("结束时间最晚为 24:00（当日结束）")
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def log_ops(app_id, business_line_id, env_key, category, action, target, detail,
            actor_id) -> None:
    get_conn().execute(
        """INSERT INTO ops_event_logs
           (app_id, business_line_id, env_key, category, action, target, detail, actor_id, created_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (app_id, business_line_id, env_key, category, action, target, detail,
         actor_id, now_ts()),
    )
    get_conn().commit()


# ---------------------------------------------------------------- 环境增删改

def create_environment(app_row: dict, label: str, user: dict) -> dict:
    label = label.strip()
    if not label:
        raise ValueError("环境名称不能为空")
    if len(label) > LABEL_MAX_LEN:
        raise ValueError(f"环境名称不能超过 {LABEL_MAX_LEN} 个字")
    if any(r["label"] == label for r in list_app_envs(app_row["id"])):
        raise ValueError(f"该应用下已存在同名环境「{label}」，环境名称在同一应用内不能重复")
    existing_keys = {r["env_key"] for r in list_app_envs(app_row["id"])}
    n = 1
    while f"c{n}" in existing_keys:
        n += 1
    env_key = f"c{n}"
    sort_no = 100 + n
    ts = now_ts()
    cur = get_conn().execute(
        """INSERT INTO app_environments (app_id, env_key, label, is_builtin, sort_no, created_at)
           VALUES (?,?,?,0,?,?)""",
        (app_row["id"], env_key, label, sort_no, ts),
    )
    get_conn().commit()
    log_ops(app_row["id"], app_row["business_line_id"], env_key, "env", "env_create",
            label, f"新建自定义环境「{label}」（标识 {env_key}）", user["id"])
    return dict(get_env(app_row["id"], env_key))


def rename_environment(app_row: dict, env_key: str, label: str, user: dict) -> dict:
    env = require_env(app_row["id"], env_key)
    label = label.strip()
    if not label:
        raise ValueError("环境名称不能为空")
    if len(label) > LABEL_MAX_LEN:
        raise ValueError(f"环境名称不能超过 {LABEL_MAX_LEN} 个字")
    dup = query_one(
        "SELECT id FROM app_environments WHERE app_id = ? AND label = ? AND env_key != ?",
        (app_row["id"], label, env_key),
    )
    if dup:
        raise ValueError(f"该应用下已存在同名环境「{label}」，环境名称在同一应用内不能重复")
    if label == env["label"]:
        raise ValueError("环境名称没有变化")
    get_conn().execute("UPDATE app_environments SET label = ? WHERE id = ?",
                       (label, env["id"]))
    get_conn().commit()
    log_ops(app_row["id"], app_row["business_line_id"], env_key, "env", "env_rename",
            label, f"环境改名：{env['label']} → {label}", user["id"])
    return dict(get_env(app_row["id"], env_key))


def environment_blockers(app_id: int, env_key: str) -> list[dict]:
    """删除前的占用检查：配置项/历史版本/实例还挂在该环境上时禁止删除。

    发布窗口与封网日属于环境自身的设置，随环境一并清除（不视为外部占用）。
    """
    blockers: list[dict] = []
    cfg_count = query_one(
        "SELECT COUNT(*) AS c FROM config_items WHERE app_id = ? AND environment = ?",
        (app_id, env_key),
    )["c"]
    if cfg_count:
        keys = [r["key"] for r in query(
            "SELECT key FROM config_items WHERE app_id = ? AND environment = ? ORDER BY key LIMIT 5",
            (app_id, env_key),
        )]
        ver_count = query_one(
            "SELECT COUNT(*) AS c FROM config_versions WHERE app_id = ? AND environment = ?",
            (app_id, env_key),
        )["c"]
        blockers.append({
            "type": "config",
            "count": cfg_count,
            "detail": f"配置档案挂着 {cfg_count} 个配置项（如 {('、'.join(keys))} 等）、"
                      f"{ver_count} 个历史版本；请先清空或迁移该环境配置后再删除",
        })
    inst_rows = query(
        "SELECT id, node_name, status FROM app_instances WHERE app_id = ? AND env_key = ? ORDER BY node_name",
        (app_id, env_key),
    )
    if inst_rows:
        running = [r for r in inst_rows if r["status"] == "running"]
        stopped = [r for r in inst_rows if r["status"] == "stopped"]
        parts = []
        if running:
            parts.append(f"运行中实例 {len(running)} 个：{('、'.join(r['node_name'] for r in running[:5]))}"
                         + (" 等" if len(running) > 5 else ""))
        if stopped:
            parts.append(f"已掉线实例 {len(stopped)} 个：{('、'.join(r['node_name'] for r in stopped[:5]))}"
                         + (" 等" if len(stopped) > 5 else ""))
        blockers.append({
            "type": "instance",
            "count": len(inst_rows),
            "detail": "实例仍挂在该环境上——" + "；".join(parts) + "。请先下线/摘除实例后再删除环境",
        })
    return blockers


def delete_environment(app_row: dict, env_key: str, user: dict) -> dict:
    env = require_env(app_row["id"], env_key)
    blockers = environment_blockers(app_row["id"], env_key)
    if blockers:
        msg = "环境「%s」不能删除，仍有资源挂在它上面：\n" % env["label"]
        msg += "\n".join(f"· {b['detail']}" for b in blockers)
        msg += "\n（环境下的配置与实例属于业务资产，系统不允许连带删除）"
        raise ValueError(msg)
    conn = get_conn()
    win_count = conn.execute(
        "SELECT COUNT(*) AS c FROM deploy_windows WHERE app_id = ? AND env_key = ?",
        (app_row["id"], env_key),
    ).fetchone()["c"]
    block_count = conn.execute(
        "SELECT COUNT(*) AS c FROM deploy_window_blocks WHERE app_id = ? AND env_key = ?",
        (app_row["id"], env_key),
    ).fetchone()["c"]
    conn.execute("DELETE FROM deploy_windows WHERE app_id = ? AND env_key = ?",
                 (app_row["id"], env_key))
    conn.execute("DELETE FROM deploy_window_blocks WHERE app_id = ? AND env_key = ?",
                 (app_row["id"], env_key))
    conn.execute("DELETE FROM app_environments WHERE id = ?", (env["id"],))
    conn.commit()
    extra = []
    if win_count:
        extra.append(f"{win_count} 条发布窗口规则")
    if block_count:
        extra.append(f"{block_count} 个封网日")
    extra_txt = f"，随环境一并清除{'、'.join(extra)}" if extra else ""
    log_ops(app_row["id"], app_row["business_line_id"], env_key, "env", "env_delete",
            env["label"],
            f"删除环境「{env['label']}」（标识 {env_key}，{'内置' if env['is_builtin'] else '自定义'}环境）{extra_txt}",
            user["id"])
    return {"ok": True, "label": env["label"]}


# ---------------------------------------------------------------- 发布窗口

def get_window_rules(app_id: int, env_key: str) -> list[dict]:
    return [dict(r) for r in query(
        "SELECT * FROM deploy_windows WHERE app_id = ? AND env_key = ? ORDER BY weekday, start_time",
        (app_id, env_key))]


def get_blocks(app_id: int, env_key: str) -> list[dict]:
    return [dict(r) for r in query(
        """SELECT k.*, u.name AS created_by_name
           FROM deploy_window_blocks k LEFT JOIN users u ON u.id = k.created_by
           WHERE k.app_id = ? AND k.env_key = ? ORDER BY k.block_date""",
        (app_id, env_key))]


def save_window_rules(app_row: dict, env_key: str, rules: list[dict], user: dict) -> dict:
    """整体替换周计划。rules: [{weekday:0-6, start_time:'HH:MM', end_time:'HH:MM'}]"""
    env = require_env(app_row["id"], env_key)
    cleaned = []
    seen: set[tuple] = set()
    for raw in rules or []:
        weekday = int(raw.get("weekday"))
        if not 0 <= weekday <= 6:
            raise ValueError("星期几必须在 0~6 之间（0=周一）")
        start = validate_hm(raw.get("start_time", ""))
        end = validate_hm(raw.get("end_time", ""), allow_2400=True)
        if _parse_hm(end) <= _parse_hm(start):
            raise ValueError(f"{WEEKDAY_LABELS[weekday]} 的窗口结束时间必须晚于开始时间（{start}~{end}）")
        key = (weekday, start)
        if key in seen:
            raise ValueError(f"{WEEKDAY_LABELS[weekday]} {start} 开始的窗口重复配置")
        seen.add(key)
        cleaned.append((weekday, start, end))
    conn = get_conn()
    conn.execute("DELETE FROM deploy_windows WHERE app_id = ? AND env_key = ?",
                 (app_row["id"], env_key))
    ts = now_ts()
    conn.executemany(
        """INSERT INTO deploy_windows (app_id, env_key, weekday, start_time, end_time, created_at)
           VALUES (?,?,?,?,?,?)""",
        [(app_row["id"], env_key, wd, st, et, ts) for wd, st, et in cleaned],
    )
    conn.commit()
    if cleaned:
        by_day: dict[int, list[tuple]] = {}
        for wd, st, et in cleaned:
            by_day.setdefault(wd, []).append((st, et))
        plan_txt = "；".join(
            f"{WEEKDAY_LABELS[wd]} " + "、".join(f"{st}~{et}" for st, et in sorted(segs))
            for wd, segs in sorted(by_day.items())
        )
        detail = f"设置发布窗口：{plan_txt}（窗口外发布将被拦截）"
    else:
        detail = "清空周计划窗口：该环境不再限制星期/时段（节假日封网仍生效）"
    log_ops(app_row["id"], app_row["business_line_id"], env_key, "window", "window_update",
            env["label"], detail, user["id"])
    return {"ok": True, "rules": len(cleaned)}


def add_block_day(app_row: dict, env_key: str, block_date: str, reason: str, user: dict) -> dict:
    env = require_env(app_row["id"], env_key)
    block_date = block_date.strip()
    import re
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", block_date):
        raise ValueError("日期格式应为 YYYY-MM-DD")
    try:
        time.strptime(block_date, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"非法日期：{block_date}")
    reason = reason.strip()
    if not reason:
        raise ValueError("封网日必须填写原因（如：国庆假期 / 大促封网），便于留痕追溯")
    existing = query_one(
        "SELECT id FROM deploy_window_blocks WHERE app_id = ? AND env_key = ? AND block_date = ?",
        (app_row["id"], env_key, block_date),
    )
    if existing:
        raise ValueError(f"{block_date} 已经是封网日，请勿重复添加")
    get_conn().execute(
        """INSERT INTO deploy_window_blocks (app_id, env_key, block_date, reason, created_by, created_at)
           VALUES (?,?,?,?,?,?)""",
        (app_row["id"], env_key, block_date, reason, user["id"], now_ts()),
    )
    get_conn().commit()
    log_ops(app_row["id"], app_row["business_line_id"], env_key, "window", "block_add",
            env["label"], f"新增封网日：{block_date} 全天禁止发布（原因：{reason}）", user["id"])
    return {"ok": True}


def remove_block_day(app_row: dict, env_key: str, block_date: str, user: dict) -> dict:
    env = require_env(app_row["id"], env_key)
    row = query_one(
        "SELECT id FROM deploy_window_blocks WHERE app_id = ? AND env_key = ? AND block_date = ?",
        (app_row["id"], env_key, block_date),
    )
    if not row:
        raise ValueError(f"{block_date} 不是封网日，无需解除")
    get_conn().execute("DELETE FROM deploy_window_blocks WHERE id = ?", (row["id"],))
    get_conn().commit()
    log_ops(app_row["id"], app_row["business_line_id"], env_key, "window", "block_remove",
            env["label"], f"解除封网日：{block_date}（该日恢复按周计划窗口发布）", user["id"])
    return {"ok": True}


# ---------------------------------------------------------------- 窗口判定

def _fmt_candidate(epoch: int) -> str:
    lt = time.localtime(epoch)
    return f"{lt.tm_mon}月{lt.tm_mday}日（{WEEKDAY_LABELS[lt.tm_wday]}）{lt.tm_hour:02d}:{lt.tm_min:02d}"


def window_status(app_id: int, env_key: str, at: int | None = None) -> dict:
    """判定某时刻是否在发布窗口内，并算出下一次可发布时间。

    规则：封网日优先级最高（全天关闭）；当天周计划窗口内即开放；
    未配置任何周计划窗口时视为全天开放（仍受封网日约束）。
    返回 open / closed_by / current_rule / next_open_at / next_open_text / message。
    """
    t = int(at if at is not None else time.time())
    lt = time.localtime(t)
    # 当天 00:00 的 epoch，作为逐日扫描基点（按本地日历，不做时区换算）
    midnight = t - (lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec)
    today_str = time.strftime("%Y-%m-%d", lt)
    cur_min = lt.tm_hour * 60 + lt.tm_min

    rules = get_window_rules(app_id, env_key)
    block_rows = query(
        "SELECT block_date, reason FROM deploy_window_blocks WHERE app_id = ? AND env_key = ?",
        (app_id, env_key),
    )
    blocks = {r["block_date"]: r["reason"] for r in block_rows}

    by_day: dict[int, list[dict]] = {}
    for r in rules:
        by_day.setdefault(r["weekday"], []).append(r)

    label = env_label(app_id, env_key)
    today_block_reason = blocks.get(today_str)
    current_rule = None
    if today_block_reason is None:
        for r in by_day.get(lt.tm_wday, []):
            if _parse_hm(r["start_time"]) <= cur_min < _parse_hm(r["end_time"]):
                current_rule = r
                break
        # 未配置任何周计划窗口：非封网日全天开放（仅节假日封网生效）
        if current_rule is None and not rules:
            return {
                "open": True,
                "closed_by": None,
                "rule": None,
                "current_window_end_at": midnight + 86400 - 1,
                "next_open_at": None,
                "next_open_text": None,
                "message": f"「{label}」未限定每周发布时段，当前全天可发布；节假日封网日仍会关闭",
            }

    if current_rule is not None:
        until = midnight + _parse_hm(current_rule["end_time"]) * 60
        return {
            "open": True,
            "closed_by": None,
            "rule": {"weekday": current_rule["weekday"],
                     "start_time": current_rule["start_time"],
                     "end_time": current_rule["end_time"]},
            "current_window_end_at": until,
            "next_open_at": None,
            "next_open_text": None,
            "message": f"当前在「{label}」发布窗口内（{current_rule['start_time']}–{current_rule['end_time']}），"
                       f"{time.strftime('%H:%M', time.localtime(until))} 后窗口关闭",
        }

    # 关闭态：从今天起逐日扫描下一个可用窗口
    next_open_epoch = None
    next_rule = None
    for d in range(WINDOW_SCAN_DAYS + 1):
        day_epoch = midnight + d * 86400
        day_lt = time.localtime(day_epoch)
        day_str = time.strftime("%Y-%m-%d", day_lt)
        if day_str in blocks:
            continue
        day_rules = by_day.get(day_lt.tm_wday, [])
        if not day_rules:
            if not rules:
                # 根本没配周计划：任何非封网时间都开放
                next_open_epoch, next_rule = (day_epoch if day_epoch > t else t), None
                break
            continue
        for r in sorted(day_rules, key=lambda x: x["start_time"]):
            candidate = day_epoch + _parse_hm(r["start_time"]) * 60
            if candidate >= t:
                next_open_epoch, next_rule = candidate, r
                break
        if next_open_epoch is not None:
            break

    # 组装说人话的关闭原因
    if today_block_reason is not None:
        closed_by = "block"
        why = f"今天（{today_str}）是「{label}」的封网日（原因：{today_block_reason}），全天禁止发布"
    elif not rules:
        closed_by = "block_only"
        why = f"「{label}」未配置周计划窗口，但今天被封网日关闭"
    else:
        closed_by = "window"
        today_rules = sorted(by_day.get(lt.tm_wday, []), key=lambda x: x["start_time"])
        if today_rules:
            why = (f"当前不在「{label}」的发布窗口内：今天（{WEEKDAY_LABELS[lt.tm_wday]}）窗口为 "
                   + "、".join(f"{r['start_time']}–{r['end_time']}" for r in today_rules))
        else:
            why = f"当前不在「{label}」的发布窗口内：今天（{WEEKDAY_LABELS[lt.tm_wday]}）没有开放窗口"
    if next_open_epoch is not None:
        if next_rule is not None:
            when = f"{_fmt_candidate(next_open_epoch)}（{next_rule['start_time']}–{next_rule['end_time']}）"
        else:
            when = _fmt_candidate(next_open_epoch)
        message = f"{why}；下一次可发布时间：{when}。紧急发布请先走封网审批，由有权限人员调整窗口。"
    else:
        when = None
        message = (f"{why}；未来 {WINDOW_SCAN_DAYS} 天内没有可用发布窗口"
                   "（可能被封网日连续覆盖），请联系业务线负责人调整发布安排")
    return {
        "open": False,
        "closed_by": closed_by,
        "rule": None,
        "current_window_end_at": None,
        "next_open_at": next_open_epoch,
        "next_open_text": when,
        "message": message,
    }


# ---------------------------------------------------------------- 实例健康

def _restart_counts_24h(app_id: int | None = None, now: int | None = None) -> dict[int, int]:
    """各实例近 24h restart 事件数。"""
    ts = (now or now_ts()) - HEALTH_WINDOW_SECONDS
    sql = """SELECT instance_id, COUNT(*) AS c FROM instance_events
             WHERE event_type = 'restart' AND created_at >= ?"""
    params: list = [ts]
    if app_id is not None:
        sql += " AND app_id = ?"
        params.append(app_id)
    sql += " GROUP BY instance_id"
    return {r["instance_id"]: r["c"] for r in query(sql, tuple(params))}


def _last_events(instance_ids: list[int]) -> dict[int, dict]:
    """每个实例最近一次 offline / recover 事件（用于显示掉线/恢复时间）。"""
    if not instance_ids:
        return {}
    result = {}
    for iid in instance_ids:
        row = query_one(
            """SELECT event_type, created_at, note FROM instance_events
               WHERE instance_id = ? AND event_type IN ('offline','recover')
               ORDER BY created_at DESC, id DESC LIMIT 1""",
            (iid,),
        )
        if row:
            result[iid] = dict(row)
    return result


def health_level(status: str, restarts_24h: int) -> str:
    if status == "stopped":
        return "offline"
    if restarts_24h >= CRASH_LOOP_RESTARTS:
        return "crash_loop"
    if restarts_24h >= FREQUENT_RESTARTS:
        return "frequent"
    return "healthy"


HEALTH_LEVEL_LABELS = {
    "healthy": "正常",
    "frequent": "重启偏多",
    "crash_loop": "反复重启",
    "offline": "已掉线",
}


def instance_to_dict(row, restarts_24h: int, last_state_event: dict | None = None,
                     app_name: str | None = None, bl_name: str | None = None,
                     env_label_text: str | None = None) -> dict:
    level = health_level(row["status"], restarts_24h)
    d = {
        "id": row["id"],
        "app_id": row["app_id"],
        "env_key": row["env_key"],
        "node_name": row["node_name"],
        "status": row["status"],
        "alive": row["status"] == "running",
        "restart_count": row["restart_count"],
        "restarts_24h": restarts_24h,
        "last_restart_at": row["last_restart_at"],
        "level": level,
        "level_label": HEALTH_LEVEL_LABELS[level],
        "offline_since": None,
        "last_state_note": None,
    }
    if last_state_event and last_state_event["event_type"] == "offline" and row["status"] == "stopped":
        d["offline_since"] = last_state_event["created_at"]
        d["last_state_note"] = last_state_event["note"]
    if app_name is not None:
        d["app_name"] = app_name
        d["business_line_name"] = bl_name
        d["environment_label"] = env_label_text
    return d


def env_health(app_id: int, env_key: str, now: int | None = None) -> dict:
    ts = now or now_ts()
    rows = query(
        "SELECT * FROM app_instances WHERE app_id = ? AND env_key = ? ORDER BY node_name",
        (app_id, env_key),
    )
    counts = _restart_counts_24h(app_id, ts)
    lasts = _last_events([r["id"] for r in rows])
    instances = [instance_to_dict(r, counts.get(r["id"], 0), lasts.get(r["id"])) for r in rows]
    total = len(instances)
    alive = sum(1 for i in instances if i["alive"])
    stopped = total - alive
    crash = sum(1 for i in instances if i["level"] == "crash_loop")
    frequent = sum(1 for i in instances if i["level"] == "frequent")
    restarts_24h = sum(i["restarts_24h"] for i in instances)
    return {
        "total": total,
        "alive": alive,
        "stopped": stopped,
        "crash_loop_count": crash,
        "frequent_count": frequent,
        "restarts_24h": restarts_24h,
        "instances": instances,
    }


def _record_instance_event(instance, event_type, actor_id, note) -> None:
    conn = get_conn()
    conn.execute(
        """INSERT INTO instance_events
           (instance_id, app_id, env_key, event_type, actor_id, note, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (instance["id"], instance["app_id"], instance["env_key"], event_type,
         actor_id, note, now_ts()),
    )
    conn.commit()


def register_instance(app_row: dict, env_key: str, node_name: str, user: dict | None,
                      note: str = "") -> dict:
    require_env(app_row["id"], env_key)
    node_name = node_name.strip()
    if not node_name:
        raise ValueError("实例名（节点标识）不能为空")
    if len(node_name) > 64:
        raise ValueError("实例名不能超过 64 个字符")
    row = query_one(
        "SELECT * FROM app_instances WHERE app_id = ? AND env_key = ? AND node_name = ?",
        (app_row["id"], env_key, node_name),
    )
    actor_id = user["id"] if user else None
    if row:
        if row["status"] == "running":
            raise ValueError(f"实例「{node_name}」已存在且处于运行状态，无需重复注册")
        # 已登记但掉线：重新注册视为恢复上线
        get_conn().execute(
            "UPDATE app_instances SET status = 'running' WHERE id = ?", (row["id"],))
        get_conn().commit()
        _record_instance_event(dict(row), "recover", actor_id, note or "实例重新注册，自动恢复")
        log_ops(app_row["id"], app_row["business_line_id"], env_key, "instance", "recover",
                node_name, f"实例恢复上线（重新注册）" + (f"：{note}" if note else ""), actor_id)
        return dict(query_one("SELECT * FROM app_instances WHERE id = ?", (row["id"],)))
    ts = now_ts()
    cur = get_conn().execute(
        """INSERT INTO app_instances
           (app_id, env_key, node_name, status, restart_count, last_restart_at, created_at)
           VALUES (?,?,?, 'running', 0, NULL, ?)""",
        (app_row["id"], env_key, node_name, ts),
    )
    get_conn().commit()
    inst = dict(query_one("SELECT * FROM app_instances WHERE id = ?", (cur.lastrowid,)))
    _record_instance_event(inst, "register", actor_id, note)
    log_ops(app_row["id"], app_row["business_line_id"], env_key, "instance", "register",
            node_name, "新实例注册并上线" + (f"：{note}" if note else ""), actor_id)
    return inst


def restart_instance(app_row: dict, instance_id: int, user: dict, note: str = "") -> dict:
    inst = _owned_instance(app_row, instance_id)
    if inst["status"] != "running":
        raise ValueError(f"实例「{inst['node_name']}」当前已掉线，不能按重启处理；请先恢复上线")
    ts = now_ts()
    get_conn().execute(
        "UPDATE app_instances SET restart_count = restart_count + 1, last_restart_at = ? WHERE id = ?",
        (ts, inst["id"]),
    )
    get_conn().commit()
    _record_instance_event(inst, "restart", user["id"], note)
    log_ops(app_row["id"], app_row["business_line_id"], inst["env_key"], "instance", "restart",
            inst["node_name"], "人工/系统重启实例" + (f"：{note}" if note else ""), user["id"])
    return dict(query_one("SELECT * FROM app_instances WHERE id = ?", (inst["id"],)))


def mark_instance_offline(app_row: dict, instance_id: int, user: dict | None,
                          note: str = "") -> dict:
    inst = _owned_instance(app_row, instance_id)
    if inst["status"] == "stopped":
        raise ValueError(f"实例「{inst['node_name']}」已经是掉线状态")
    get_conn().execute("UPDATE app_instances SET status = 'stopped' WHERE id = ?", (inst["id"],))
    get_conn().commit()
    actor_id = user["id"] if user else None
    _record_instance_event(inst, "offline", actor_id, note)
    log_ops(app_row["id"], app_row["business_line_id"], inst["env_key"], "instance", "offline",
            inst["node_name"], "实例掉线（监测发现/人工摘除）" + (f"：{note}" if note else ""), actor_id)
    return dict(query_one("SELECT * FROM app_instances WHERE id = ?", (inst["id"],)))


def recover_instance(app_row: dict, instance_id: int, user: dict, note: str = "") -> dict:
    inst = _owned_instance(app_row, instance_id)
    if inst["status"] == "running":
        raise ValueError(f"实例「{inst['node_name']}」当前运行正常，无需恢复")
    get_conn().execute("UPDATE app_instances SET status = 'running' WHERE id = ?", (inst["id"],))
    get_conn().commit()
    _record_instance_event(inst, "recover", user["id"], note)
    log_ops(app_row["id"], app_row["business_line_id"], inst["env_key"], "instance", "recover",
            inst["node_name"], "实例恢复上线" + (f"：{note}" if note else ""), user["id"])
    return dict(query_one("SELECT * FROM app_instances WHERE id = ?", (inst["id"],)))


def _owned_instance(app_row: dict, instance_id: int):
    row = query_one("SELECT * FROM app_instances WHERE id = ?", (instance_id,))
    if not row or row["app_id"] != app_row["id"]:
        raise LookupError(f"实例 #{instance_id} 不存在或不属于该应用")
    return dict(row)


def recent_instance_events(app_id: int, env_key: str | None = None, limit: int = 20) -> list[dict]:
    sql = """SELECT e.*, i.node_name, u.name AS actor_name
             FROM instance_events e
             JOIN app_instances i ON i.id = e.instance_id
             LEFT JOIN users u ON u.id = e.actor_id
             WHERE e.app_id = ?"""
    params: list = [app_id]
    if env_key:
        sql += " AND e.env_key = ?"
        params.append(env_key)
    sql += " ORDER BY e.created_at DESC, e.id DESC LIMIT ?"
    params.append(limit)
    rows = query(sql, tuple(params))
    event_labels = {"register": "注册", "restart": "重启", "offline": "掉线", "recover": "恢复"}
    return [{
        "id": r["id"], "node_name": r["node_name"], "env_key": r["env_key"],
        "event_type": r["event_type"], "event_label": event_labels.get(r["event_type"], r["event_type"]),
        "actor_name": r["actor_name"] or "系统监测", "note": r["note"], "created_at": r["created_at"],
    } for r in rows]


# ---------------------------------------------------------------- 环境汇总（列表用）

def env_summary(app_id: int, at: int | None = None) -> list[dict]:
    """应用下每个环境的一站式视图：占用计数 + 窗口状态 + 健康汇总。"""
    ts = at if at is not None else now_ts()
    restart_map = _restart_counts_24h(app_id, ts)
    result = []
    for env in list_app_envs(app_id):
        cfg_count = query_one(
            "SELECT COUNT(*) AS c FROM config_items WHERE app_id = ? AND environment = ?",
            (app_id, env["env_key"]),
        )["c"]
        ver_row = query_one(
            "SELECT MAX(version) AS v, COUNT(*) AS c FROM config_versions WHERE app_id = ? AND environment = ?",
            (app_id, env["env_key"]),
        )
        inst_rows = query(
            "SELECT * FROM app_instances WHERE app_id = ? AND env_key = ?",
            (app_id, env["env_key"]),
        )
        levels = [health_level(r["status"], restart_map.get(r["id"], 0)) for r in inst_rows]
        win = window_status(app_id, env["env_key"], at=ts)
        result.append({
            "id": env["id"],
            "env_key": env["env_key"],
            "label": env["label"],
            "is_builtin": bool(env["is_builtin"]),
            "sort_no": env["sort_no"],
            "config_count": cfg_count,
            "version_count": ver_row["c"],
            "latest_version": ver_row["v"] or 0,
            "instance_total": len(inst_rows),
            "instance_alive": sum(1 for r in inst_rows if r["status"] == "running"),
            "instance_stopped": sum(1 for r in inst_rows if r["status"] == "stopped"),
            "crash_loop_count": levels.count("crash_loop"),
            "frequent_count": levels.count("frequent"),
            "restarts_24h": sum(restart_map.get(r["id"], 0) for r in inst_rows),
            "window_open": win["open"],
            "window_message": win["message"],
            "next_open_at": win["next_open_at"],
            "next_open_text": win["next_open_text"],
        })
    return result


# ---------------------------------------------------------------- 作战台告警

def health_alerts_rows(scope_sql: str, scope_params: list, now: int | None = None) -> list[dict]:
    """跨应用的健康告警：掉线实例 + 反复重启实例，按可见范围 SQL 收窄。

    返回的行带 业务线/应用/环境 标识，直接在资产作战台冒出来，
    不用逐个应用点进去翻。
    """
    ts = now or now_ts()
    rows = query(
        f"""SELECT i.*, a.name AS app_name, a.business_line_id,
                   b.name AS business_line_name, e.label AS environment_label
            FROM app_instances i
            JOIN applications a ON a.id = i.app_id
            JOIN business_lines b ON b.id = a.business_line_id
            JOIN app_environments e ON e.app_id = i.app_id AND e.env_key = i.env_key
            WHERE {scope_sql}
            ORDER BY CASE i.status WHEN 'stopped' THEN 0 ELSE 1 END, i.id""",
        tuple(scope_params),
    )
    counts = _restart_counts_24h(now=ts)
    alerts = []
    for r in rows:
        r24 = counts.get(r["id"], 0)
        level = health_level(r["status"], r24)
        if level not in ("offline", "crash_loop"):
            continue
        d = instance_to_dict(r, r24, app_name=r["app_name"], bl_name=r["business_line_name"],
                             env_label_text=r["environment_label"])
        d["level_label"] = HEALTH_LEVEL_LABELS[level]
        alerts.append(d)
    alerts.sort(key=lambda x: (x["level"] != "offline",
                               -(x["offline_since"] or x["last_restart_at"] or 0)))
    return alerts


def health_overview(scope_sql: str, scope_params: list, now: int | None = None) -> dict:
    ts = now or now_ts()
    rows = query(
        f"""SELECT i.*, a.name AS app_name, b.name AS business_line_name,
                   e.label AS environment_label
            FROM app_instances i
            JOIN applications a ON a.id = i.app_id
            JOIN business_lines b ON b.id = a.business_line_id
            JOIN app_environments e ON e.app_id = i.app_id AND e.env_key = i.env_key
            WHERE {scope_sql}""",
        tuple(scope_params),
    )
    counts = _restart_counts_24h(now=ts)
    total = alive = stopped = crash = frequent = 0
    env_with_alerts: set[tuple] = set()
    for r in rows:
        total += 1
        if r["status"] == "running":
            alive += 1
        else:
            stopped += 1
        level = health_level(r["status"], counts.get(r["id"], 0))
        if level == "crash_loop":
            crash += 1
            env_with_alerts.add((r["app_id"], r["env_key"]))
        elif level == "frequent":
            frequent += 1
        if level == "offline":
            env_with_alerts.add((r["app_id"], r["env_key"]))
    return {
        "instance_total": total,
        "instance_alive": alive,
        "instance_stopped": stopped,
        "crash_loop_instances": crash,
        "frequent_instances": frequent,
        "alert_env_count": len(env_with_alerts),
    }


# ---------------------------------------------------------------- 运维留痕查询

def query_ops(user, *, app_id=None, business_line_id=None, env_key=None,
              category=None, start=None, end=None) -> tuple[str, list]:
    """返回 (sql, params)，强制按可见范围收窄，与配置留痕同一套口径。"""
    import re
    from . import permissions as perms
    sql = """SELECT l.id, l.app_id, l.env_key, l.category, l.action, l.target, l.detail,
                    l.created_at, a.name AS app_name, a.owner_id,
                    b.id AS business_line_id, b.name AS business_line_name,
                    u.name AS user_name
             FROM ops_event_logs l
             LEFT JOIN applications a ON a.id = l.app_id
             LEFT JOIN business_lines b ON b.id = a.business_line_id
             LEFT JOIN users u ON u.id = l.actor_id
             WHERE 1=1"""
    params: list = []
    if perms.is_admin(user):
        if business_line_id:
            sql += " AND a.business_line_id = ?"
            params.append(business_line_id)
    else:
        # 历史留痕的应用可能已被删除（a.* 为 NULL）：这类行仅管理员可见，避免借留痕绕过范围
        sql += " AND a.id IS NOT NULL"
        cond, cond_params = perms.scope_condition(user, "a.business_line_id", "l.env_key", "a.owner_id")
        sql += f" AND {cond}"
        params.extend(cond_params)
        if business_line_id:
            sql += " AND a.business_line_id = ?"
            params.append(business_line_id)
    if app_id:
        sql += " AND l.app_id = ?"
        params.append(app_id)
    if env_key:
        sql += " AND l.env_key = ?"
        params.append(env_key)
    if category:
        sql += " AND l.category = ?"
        params.append(category)
    if start and re.fullmatch(r"\d{4}-\d{2}-\d{2}", start.strip()):
        t = time.strptime(start.strip() + " 00:00:00", "%Y-%m-%d %H:%M:%S")
        sql += " AND l.created_at >= ?"
        params.append(int(time.mktime(t)))
    if end and re.fullmatch(r"\d{4}-\d{2}-\d{2}", end.strip()):
        t = time.strptime(end.strip() + " 23:59:59", "%Y-%m-%d %H:%M:%S")
        sql += " AND l.created_at <= ?"
        params.append(int(time.mktime(t)))
    sql += " ORDER BY l.created_at DESC, l.id DESC LIMIT 500"
    return sql, params


def ops_row_to_dict(r) -> dict:
    env_key = r["env_key"]
    env_label_text = env_label(r["app_id"], env_key) if r["app_id"] else env_key
    return {
        "id": r["id"],
        "app_id": r["app_id"],
        "app_name": r["app_name"] or "（应用已删除）",
        "business_line_name": r["business_line_name"] or "—",
        "env_key": env_key,
        "environment_label": env_label_text,
        "category": r["category"],
        "category_label": OPS_CATEGORY_LABELS.get(r["category"], r["category"]),
        "action": r["action"],
        "action_label": OPS_ACTION_LABELS.get(r["action"], r["action"]),
        "target": r["target"],
        "detail": r["detail"],
        "user_name": r["user_name"] or "系统监测",
        "created_at": r["created_at"],
    }
