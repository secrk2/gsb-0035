"""织云系统 - 环境管理 / 发布窗口 / 应用健康 API。

- 环境在「应用」下增删改名：开发/预发/生产不写死，业务线可自建（灰度、压测……）；
- 每个环境可设发布窗口（周几 + 时段）与节假日封网；窗口外发布服务端强制拦截，
  返回下一次可发布时间，而不是只给一个灰色按钮；
- 删除环境前清点挂在它上面的配置项/版本/实例，有占用一律拒绝并说明占用内容；
- 每个环境下管理实例：存活/掉线、重启次数与最后重启时间，反复重启单独分级；
- 窗口、封网、环境增删、发布拦截、实例掉线/重启全部写运维留痕，可按应用与时间窗查询。
"""
import csv
import io
import time
from urllib.parse import quote

from fastapi import APIRouter
from fastapi.responses import Response
from pydantic import BaseModel, Field

from .. import envs_service as svc
from .. import permissions as perms
from ..auth import User, err, get_app_checked, get_app_or_404, get_app_writable
from ..db import TERMINAL_STATUS, query

router = APIRouter()


# ---------------------------------------------------------------- 请求模型

class EnvCreateIn(BaseModel):
    label: str = Field(min_length=1, max_length=16)


class EnvRenameIn(BaseModel):
    label: str = Field(min_length=1, max_length=16)


class WindowRuleIn(BaseModel):
    weekday: int = Field(ge=0, le=6)
    start_time: str
    end_time: str


class WindowIn(BaseModel):
    rules: list[WindowRuleIn] = []


class BlockIn(BaseModel):
    block_date: str
    reason: str = Field(default="", max_length=100)


class DeployIn(BaseModel):
    note: str = Field(default="", max_length=200)


class InstanceIn(BaseModel):
    node_name: str = Field(min_length=1, max_length=64)
    note: str = Field(default="", max_length=200)


class InstanceActionIn(BaseModel):
    note: str = Field(default="", max_length=200)


# ---------------------------------------------------------------- 辅助

def _env_or_404(app_id: int, env_key: str) -> dict:
    try:
        return svc.require_env(app_id, env_key)
    except LookupError as e:
        raise err(404, str(e))


def _env_dict(env_row: dict, can_manage: bool) -> dict:
    return {
        "env_key": env_row["env_key"],
        "label": env_row["label"],
        "is_builtin": bool(env_row["is_builtin"]),
        "sort_no": env_row["sort_no"],
        "can_manage": can_manage,
    }


# ---------------------------------------------------------------- 环境列表/详情

@router.get("/api/apps/{app_id}/environments")
def list_environments(app_id: int, user: dict = User):
    """应用下的环境一览：每环境带配置/实例占用计数、窗口状态与健康分级。

    只有应用级可见（管理员/业务线负责人/应用负责人）才返回全部环境明细；
    仅被授予某几个环境的账号，其他环境只返回存在性（visible=false），
    不泄露窗口设置与实例健康数据。
    """
    app_row = get_app_checked(user, app_id)
    can_manage = perms.can_manage_app(user, app_row)
    full_visibility = (perms.is_admin(user) or perms.is_bl_owner(user, app_row["business_line_id"])
                       or app_id in user.get("_owned_apps", set()))
    items = svc.env_summary(app_id)
    out = []
    for it in items:
        env_visible = full_visibility or bool(perms._grant_match(
            perms.grants_of(user), app_row["business_line_id"], it["env_key"], "view"))
        it["can_manage"] = can_manage
        it["visible"] = env_visible
        if not env_visible:
            # 只保留存在性，剥掉占用/窗口/健康明细
            for k in ("config_count", "version_count", "latest_version", "instance_total",
                      "instance_alive", "instance_stopped", "crash_loop_count",
                      "frequent_count", "restarts_24h", "window_open", "window_message",
                      "next_open_at", "next_open_text"):
                it.pop(k, None)
        out.append(it)
    return {
        "app_id": app_id,
        "app_name": app_row["name"],
        "read_only": app_row["status"] == TERMINAL_STATUS,
        "can_manage": can_manage,
        "environments": out,
    }


@router.get("/api/apps/{app_id}/environments/{env_key}")
def environment_detail(app_id: int, env_key: str, user: dict = User):
    app_row = get_app_checked(user, app_id)
    env = _env_or_404(app_id, env_key)
    # 可见性按"具体环境"收窄：只有应用所属环境的授权不代表能看该应用其他环境的窗口/实例
    perms.ensure_config_perm(user, app_row, env_key, "view")
    can_manage = perms.can_manage_app(user, app_row)
    win = svc.window_status(app_id, env_key)
    rules = svc.get_window_rules(app_id, env_key)
    blocks = svc.get_blocks(app_id, env_key)
    health = svc.env_health(app_id, env_key)
    events = svc.recent_instance_events(app_id, env_key, limit=15)
    blockers = svc.environment_blockers(app_id, env_key) if can_manage else []
    return {
        "app_id": app_id,
        "app_name": app_row["name"],
        "business_line_id": app_row["business_line_id"],
        "read_only": app_row["status"] == TERMINAL_STATUS,
        "can_manage": can_manage,
        "environment": _env_dict(env, can_manage),
        "window": win,
        "rules": [{"id": r["id"], "weekday": r["weekday"],
                   "weekday_label": svc.WEEKDAY_LABELS[r["weekday"]],
                   "start_time": r["start_time"], "end_time": r["end_time"]} for r in rules],
        "blocks": [{"block_date": b["block_date"], "reason": b["reason"],
                    "created_by_name": b["created_by_name"] or "系统",
                    "created_at": b["created_at"]} for b in blocks],
        "health": health,
        "recent_events": events,
        "delete_blockers": [b["detail"] for b in blockers],
        "weekday_labels": svc.WEEKDAY_LABELS,
    }


# ---------------------------------------------------------------- 环境增删改

@router.post("/api/apps/{app_id}/environments", status_code=201)
def create_environment(app_id: int, body: EnvCreateIn, user: dict = User):
    app_row = get_app_writable(user, app_id, "环境管理")
    if app_row["status"] == TERMINAL_STATUS:
        raise err(400, "应用已下线（终态），环境结构只读，不能新增环境")
    try:
        env = svc.create_environment(app_row, body.label, user)
    except ValueError as e:
        raise err(400, str(e))
    return {"ok": True, "environment": {"env_key": env["env_key"], "label": env["label"]}}


@router.patch("/api/apps/{app_id}/environments/{env_key}")
def rename_environment(app_id: int, env_key: str, body: EnvRenameIn, user: dict = User):
    app_row = get_app_writable(user, app_id, "环境管理")
    if app_row["status"] == TERMINAL_STATUS:
        raise err(400, "应用已下线（终态），环境结构只读，不能改名")
    _env_or_404(app_id, env_key)
    try:
        env = svc.rename_environment(app_row, env_key, body.label, user)
    except ValueError as e:
        raise err(400, str(e))
    return {"ok": True, "environment": {"env_key": env["env_key"], "label": env["label"]}}


@router.delete("/api/apps/{app_id}/environments/{env_key}")
def delete_environment(app_id: int, env_key: str, user: dict = User):
    app_row = get_app_writable(user, app_id, "环境管理")
    if app_row["status"] == TERMINAL_STATUS:
        raise err(400, "应用已下线（终态），环境结构只读，不能删除环境")
    _env_or_404(app_id, env_key)
    try:
        result = svc.delete_environment(app_row, env_key, user)
    except ValueError as e:
        # 有配置/实例挂着：409 + 占用明细，让前端把"挂在什么上"讲清楚
        blockers = svc.environment_blockers(app_id, env_key)
        raise err(409, str(e), {"code": "env_not_empty", "blockers": blockers})
    return result


# ---------------------------------------------------------------- 发布窗口 / 封网日

@router.put("/api/apps/{app_id}/environments/{env_key}/window")
def save_window(app_id: int, env_key: str, body: WindowIn, user: dict = User):
    app_row = get_app_writable(user, app_id, "发布窗口")
    if app_row["status"] == TERMINAL_STATUS:
        raise err(400, "应用已下线（终态），发布窗口只读")
    _env_or_404(app_id, env_key)
    try:
        result = svc.save_window_rules(
            app_row, env_key, [r.model_dump() for r in body.rules], user)
    except ValueError as e:
        raise err(400, str(e))
    return result


@router.post("/api/apps/{app_id}/environments/{env_key}/blocks", status_code=201)
def add_block(app_id: int, env_key: str, body: BlockIn, user: dict = User):
    app_row = get_app_writable(user, app_id, "节假日封网")
    if app_row["status"] == TERMINAL_STATUS:
        raise err(400, "应用已下线（终态），封网设置只读")
    _env_or_404(app_id, env_key)
    try:
        return svc.add_block_day(app_row, env_key, body.block_date, body.reason, user)
    except ValueError as e:
        raise err(400, str(e))


@router.delete("/api/apps/{app_id}/environments/{env_key}/blocks/{block_date}")
def remove_block(app_id: int, env_key: str, block_date: str, user: dict = User):
    app_row = get_app_writable(user, app_id, "节假日封网")
    if app_row["status"] == TERMINAL_STATUS:
        raise err(400, "应用已下线（终态），封网设置只读")
    _env_or_404(app_id, env_key)
    try:
        return svc.remove_block_day(app_row, env_key, block_date, user)
    except ValueError as e:
        raise err(400, str(e))


# ---------------------------------------------------------------- 发布（窗口拦截）

@router.post("/api/apps/{app_id}/environments/{env_key}/deploy")
def deploy(app_id: int, env_key: str, body: DeployIn, user: dict = User):
    app_row = get_app_writable(user, app_id, "发布上线")
    if app_row["status"] == TERMINAL_STATUS:
        raise err(400, "应用已下线（终态），不能再发布")
    env = _env_or_404(app_id, env_key)
    status = svc.window_status(app_id, env_key)
    note = body.note.strip()
    if not status["open"]:
        # 拦截本身也要留痕：谁在窗口外尝试上线
        svc.log_ops(app_id, app_row["business_line_id"], env_key, "deploy", "deploy_blocked",
                    env["label"],
                    f"窗口外发布被拦截：{status['message']}" + (f"；发布说明：{note}" if note else ""),
                    user["id"])
        raise err(409, status["message"], {
            "code": "deploy_window_closed",
            "closed_by": status["closed_by"],
            "next_open_at": status["next_open_at"],
            "next_open_text": status["next_open_text"],
        })
    ts = svc.now_ts()
    if status["rule"]:
        win_txt = f"窗口内发布上线（{status['rule']['start_time']}–{status['rule']['end_time']}）"
    else:
        win_txt = "发布上线（该环境未限定发布时段，全天可发布）"
    svc.log_ops(app_id, app_row["business_line_id"], env_key, "deploy", "deploy_ok",
                env["label"], win_txt + (f"：{note}" if note else ""),
                user["id"])
    from ..db import execute
    execute(
        "INSERT INTO change_logs (app_id, user_id, action, detail, created_at) VALUES (?,?,?,?,?)",
        (app_id, user["id"], "发布上线",
         f"在「{env['label']}」环境" + ("窗口内发布" if status["rule"] else "发布（未限窗口）")
         + (f"：{note}" if note else ""), ts),
    )
    return {
        "ok": True,
        "deployed_at": ts,
        "message": f"发布成功：{env['label']} 环境已在窗口内完成上线",
        "window_end_at": status["current_window_end_at"],
    }


# ---------------------------------------------------------------- 实例管理

@router.post("/api/apps/{app_id}/environments/{env_key}/instances", status_code=201)
def register_instance(app_id: int, env_key: str, body: InstanceIn, user: dict = User):
    app_row = get_app_writable(user, app_id, "实例管理")
    if app_row["status"] == TERMINAL_STATUS:
        raise err(400, "应用已下线（终态），不能登记实例")
    _env_or_404(app_id, env_key)
    try:
        svc.register_instance(app_row, env_key, body.node_name, user, body.note)
    except ValueError as e:
        raise err(400, str(e))
    return {"ok": True, "health": svc.env_health(app_id, env_key)}


@router.post("/api/apps/{app_id}/instances/{instance_id}/restart")
def restart_instance(app_id: int, instance_id: int, body: InstanceActionIn, user: dict = User):
    app_row = get_app_writable(user, app_id, "实例管理")
    if app_row["status"] == TERMINAL_STATUS:
        raise err(400, "应用已下线（终态），实例操作只读")
    try:
        svc.restart_instance(app_row, instance_id, user, body.note)
    except LookupError as e:
        raise err(404, str(e))
    except ValueError as e:
        raise err(400, str(e))
    return {"ok": True}


@router.post("/api/apps/{app_id}/instances/{instance_id}/offline")
def mark_offline(app_id: int, instance_id: int, body: InstanceActionIn, user: dict = User):
    """人工标记掉线（演示环境模拟监测发现；真实系统由监测回调触发）。"""
    app_row = get_app_writable(user, app_id, "实例管理")
    if app_row["status"] == TERMINAL_STATUS:
        raise err(400, "应用已下线（终态），实例操作只读")
    try:
        svc.mark_instance_offline(app_row, instance_id, user, body.note)
    except LookupError as e:
        raise err(404, str(e))
    except ValueError as e:
        raise err(400, str(e))
    return {"ok": True}


@router.post("/api/apps/{app_id}/instances/{instance_id}/recover")
def recover_instance(app_id: int, instance_id: int, body: InstanceActionIn, user: dict = User):
    app_row = get_app_writable(user, app_id, "实例管理")
    if app_row["status"] == TERMINAL_STATUS:
        raise err(400, "应用已下线（终态），实例操作只读")
    try:
        svc.recover_instance(app_row, instance_id, user, body.note)
    except LookupError as e:
        raise err(404, str(e))
    except ValueError as e:
        raise err(400, str(e))
    return {"ok": True}


# ---------------------------------------------------------------- 作战台健康告警

def _scope(user: dict) -> tuple[str, list]:
    if perms.is_admin(user):
        return "1=1", []
    return perms.scope_condition(user, "a.business_line_id", "i.env_key", "a.owner_id")


@router.get("/api/ops/alerts")
def ops_alerts(user: dict = User):
    """掉线 / 反复重启实例，跨应用直接冒出，带业务线与环境标识。"""
    scope_sql, scope_params = _scope(user)
    alerts = svc.health_alerts_rows(scope_sql, scope_params)
    return {
        "alerts": alerts,
        "totals": {
            "offline": sum(1 for a in alerts if a["level"] == "offline"),
            "crash_loop": sum(1 for a in alerts if a["level"] == "crash_loop"),
        },
    }


# ---------------------------------------------------------------- 运维留痕

def _ops_rows(user, app_id, business_line_id, env_key, category, start, end):
    if category and category not in svc.OPS_CATEGORY_LABELS:
        raise err(400, f"非法留痕类别：{category}")
    if app_id:
        app_row = get_app_or_404(app_id)
        perms.ensure_app_visible(user, app_row)
        if env_key:
            try:
                svc.require_env(app_id, env_key)
            except LookupError as e:
                raise err(404, str(e))
            perms.ensure_config_perm(user, app_row, env_key, "view")
    elif business_line_id:
        from ..auth import ensure_bl_visible
        ensure_bl_visible(user, business_line_id)
    sql, params = svc.query_ops(
        user, app_id=app_id, business_line_id=business_line_id, env_key=env_key or None,
        category=category, start=start, end=end,
    )
    return query(sql, tuple(params))


@router.get("/api/ops/audit")
def ops_audit(user: dict = User,
              app_id: int | None = None,
              business_line_id: int | None = None,
              env_key: str | None = None,
              category: str | None = None,
              start: str | None = None,
              end: str | None = None):
    rows = _ops_rows(user, app_id, business_line_id, env_key, category, start, end)
    return [svc.ops_row_to_dict(r) for r in rows]


OPS_CSV_HEADER = ["时间", "业务线", "应用", "环境", "类别", "动作", "对象", "详情", "操作人"]


@router.get("/api/ops/audit/export.csv")
def ops_audit_export(user: dict = User,
                     app_id: int | None = None,
                     business_line_id: int | None = None,
                     env_key: str | None = None,
                     category: str | None = None,
                     start: str | None = None,
                     end: str | None = None):
    rows = _ops_rows(user, app_id, business_line_id, env_key, category, start, end)
    buf = io.StringIO()
    buf.write("﻿")  # UTF-8 BOM
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(OPS_CSV_HEADER)
    for r in rows:
        d = svc.ops_row_to_dict(r)
        writer.writerow([
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(d["created_at"])),
            d["business_line_name"], d["app_name"], d["environment_label"],
            d["category_label"], d["action_label"], d["target"], d["detail"], d["user_name"],
        ])
    filename = f"ops-changelog-{time.strftime('%Y%m%d-%H%M%S')}.csv"
    return Response(
        content=buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )
