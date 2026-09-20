"""织云系统 - SQLite 数据层。

生命周期状态机（只能向前流转，下线为终态）：
    在研 developing -> 上线 online -> 维保 maintenance -> 下线 offline(终态)
"""
import os
import sqlite3
import threading

DB_PATH = os.environ.get(
    "DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "zhiyun.db")
)

ENVIRONMENTS = ["dev", "test", "staging", "prod"]
ENV_LABELS = {"dev": "开发", "test": "测试", "staging": "预发", "prod": "生产"}
# 内置环境 key 集合：新建应用默认补建这四个；业务线可在应用下自行增删自定义环境。
BUILTIN_ENVIRONMENTS = list(ENVIRONMENTS)
BUILTIN_ENV_LABELS = dict(ENV_LABELS)

STATUSES = ["developing", "online", "maintenance", "offline"]
STATUS_LABELS = {
    "developing": "在研",
    "online": "上线",
    "maintenance": "维保",
    "offline": "下线",
}
STATUS_ORDER = {s: i for i, s in enumerate(STATUSES)}
TERMINAL_STATUS = "offline"

CLUSTERS = ["华东1集群", "华北2集群", "华南1集群", "西南灾备集群"]

# 角色体系：平台管理员 / 业务线负责人 / 应用负责人 / 只读观察者
ROLES = ["admin", "bl_owner", "app_owner", "viewer"]
ROLE_LABELS = {
    "admin": "平台管理员",
    "bl_owner": "业务线负责人",
    "app_owner": "应用负责人",
    "viewer": "只读观察者",
}
# 授权范围中的"全部环境"哨兵值
ENV_SCOPE_ALL = "*"

# 配置档案（按 应用 + 环境 管理）
CONFIG_TYPES = ["string", "number", "boolean", "json"]
CONFIG_TYPE_LABELS = {"string": "字符串", "number": "数字", "boolean": "布尔", "json": "JSON"}
# 生效范围
CONFIG_SCOPES = ["global", "cluster", "canary"]
CONFIG_SCOPE_LABELS = {"global": "全局", "cluster": "集群", "canary": "灰度"}
# 布尔值归一化后的存储形态
BOOL_TRUE = {"true", "1", "yes", "on", "是", "开"}

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS business_lines (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    code TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS users (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    username         TEXT NOT NULL UNIQUE,
    name             TEXT NOT NULL,
    -- admin 平台管理员 / bl_owner 业务线负责人 / app_owner 应用负责人 / viewer 只读观察者
    role             TEXT NOT NULL DEFAULT 'viewer'
                     CHECK (role IN ('admin', 'bl_owner', 'app_owner', 'viewer')),
    business_line_id INTEGER REFERENCES business_lines(id),  -- 业务线负责人/应用负责人的所属业务线；管理员与观察者可为空
    token            TEXT NOT NULL UNIQUE
);

-- 可见范围授权：按 业务线 × 环境 两级收窄。
-- environment='*' 表示该业务线全部环境；内置环境 dev/test/staging/prod；
-- 环境改为应用级自定义后，这里也允许出现自定义环境 key（如 c1 灰度）。
-- 密文查看权 can_reveal 与 配置编辑权 can_edit 分开授予：能看明文不等于能改。
CREATE TABLE IF NOT EXISTS user_grants (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    business_line_id INTEGER NOT NULL REFERENCES business_lines(id) ON DELETE CASCADE,
    environment      TEXT NOT NULL DEFAULT '*',
    can_view_config  INTEGER NOT NULL DEFAULT 1 CHECK (can_view_config IN (0,1)),
    can_edit_config  INTEGER NOT NULL DEFAULT 0 CHECK (can_edit_config IN (0,1)),
    can_reveal       INTEGER NOT NULL DEFAULT 0 CHECK (can_reveal IN (0,1)),
    granted_by       INTEGER REFERENCES users(id),
    created_at       INTEGER NOT NULL,
    updated_at       INTEGER NOT NULL,
    UNIQUE (user_id, business_line_id, environment)
);

-- 应用归属交接留痕：交接的是应用归属与配置管理权限，配置项随应用一并移交
CREATE TABLE IF NOT EXISTS app_transfers (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id           INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    old_owner_id     INTEGER REFERENCES users(id),
    new_owner_id     INTEGER REFERENCES users(id),
    transfer_by_id   INTEGER REFERENCES users(id),   -- 发起/确认交接的人
    note             TEXT NOT NULL DEFAULT '',
    created_at       INTEGER NOT NULL
);

-- 权限与交接类留痕（授权/收权/角色调整等，不只属于单个应用，独立于 change_logs）
CREATE TABLE IF NOT EXISTS permission_logs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id         INTEGER REFERENCES users(id),       -- 操作人（谁改的权限）
    target_user_id   INTEGER REFERENCES users(id),       -- 被改权限的账号
    action           TEXT NOT NULL,                       -- grant/revoke/role_change/transfer
    scope_text       TEXT NOT NULL DEFAULT '',            -- 业务线/环境/应用的文字描述
    detail           TEXT NOT NULL DEFAULT '',            -- 具体变化（授了什么、收了什么）
    created_at       INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS applications (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT NOT NULL,
    business_line_id INTEGER NOT NULL REFERENCES business_lines(id),
    owner_id         INTEGER REFERENCES users(id),
    cluster          TEXT NOT NULL,
    environment      TEXT NOT NULL,   -- 应用级环境 key（内置 dev/test/staging/prod 或自定义 c<n>）
    status           TEXT NOT NULL DEFAULT 'developing'
                     CHECK (status IN ('developing','online','maintenance','offline')),
    description      TEXT NOT NULL DEFAULT '',
    created_at       INTEGER NOT NULL,
    updated_at       INTEGER NOT NULL,
    UNIQUE (business_line_id, name)
);

CREATE TABLE IF NOT EXISTS env_vars (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    key    TEXT NOT NULL,
    value  TEXT NOT NULL DEFAULT '',
    UNIQUE (app_id, key)
);

CREATE TABLE IF NOT EXISTS change_logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id     INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    user_id    INTEGER REFERENCES users(id),
    action     TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
);

-- 配置档案：配置项按 应用 + 环境 管理（键、值、类型、生效范围、是否密文）
CREATE TABLE IF NOT EXISTS config_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id      INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    environment TEXT NOT NULL,        -- app_environments.env_key（内置或自定义环境）
    key         TEXT NOT NULL,
    value       TEXT NOT NULL DEFAULT '',   -- 密文同样落库（内部系统演示，无外部 KMS），接口默认不回传明文
    value_type  TEXT NOT NULL DEFAULT 'string'
                CHECK (value_type IN ('string','number','boolean','json')),
    scope       TEXT NOT NULL DEFAULT 'global'
                CHECK (scope IN ('global','cluster','canary')),
    is_secret   INTEGER NOT NULL DEFAULT 0 CHECK (is_secret IN (0,1)),
    updated_by  INTEGER REFERENCES users(id),
    updated_at  INTEGER NOT NULL,
    UNIQUE (app_id, environment, key)
);

-- 配置版本：每次保存产生一个全量快照；回滚 = 追加新版本，绝不改写历史版本
CREATE TABLE IF NOT EXISTS config_versions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id      INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    environment TEXT NOT NULL,
    version     INTEGER NOT NULL,          -- 该 应用+环境 内自增
    snapshot    TEXT NOT NULL,             -- JSON 全量快照（密文存明文，仅回滚/版本对比内部使用）
    change_note TEXT NOT NULL DEFAULT '',
    created_by  INTEGER REFERENCES users(id),
    created_at  INTEGER NOT NULL,
    UNIQUE (app_id, environment, version)
);-- 配置留痕：逐键流水（改前/改后/操作人/理由），只追加，不更新不删除
CREATE TABLE IF NOT EXISTS config_audit_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id      INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    environment TEXT NOT NULL,
    version_id  INTEGER REFERENCES config_versions(id) ON DELETE SET NULL,
    user_id     INTEGER REFERENCES users(id),
    action      TEXT NOT NULL,             -- add/update/remove/rollback/reveal
    config_key  TEXT NOT NULL DEFAULT '',
    old_value   TEXT,
    new_value   TEXT,
    is_secret   INTEGER NOT NULL DEFAULT 0,
    reason      TEXT NOT NULL DEFAULT '',  -- reveal 强制填写；rollback 记录目标版本
    created_at  INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_apps_bl ON applications(business_line_id);
CREATE INDEX IF NOT EXISTS idx_apps_owner ON applications(owner_id);
CREATE INDEX IF NOT EXISTS idx_logs_app ON change_logs(app_id);
CREATE INDEX IF NOT EXISTS idx_logs_time ON change_logs(created_at);
CREATE INDEX IF NOT EXISTS idx_grants_user ON user_grants(user_id);
CREATE INDEX IF NOT EXISTS idx_grants_bl_env ON user_grants(business_line_id, environment);
CREATE INDEX IF NOT EXISTS idx_transfers_app ON app_transfers(app_id);
CREATE INDEX IF NOT EXISTS idx_permlogs_target ON permission_logs(target_user_id);
CREATE INDEX IF NOT EXISTS idx_permlogs_time ON permission_logs(created_at);
CREATE INDEX IF NOT EXISTS idx_cfg_app_env ON config_items(app_id, environment);
CREATE INDEX IF NOT EXISTS idx_ver_app_env ON config_versions(app_id, environment);
CREATE INDEX IF NOT EXISTS idx_audit_app ON config_audit_logs(app_id);
CREATE INDEX IF NOT EXISTS idx_audit_time ON config_audit_logs(created_at);
CREATE INDEX IF NOT EXISTS idx_audit_action ON config_audit_logs(action);

-- =====================================================================
-- 环境管理（应用级环境：开发/预发/生产不写死，业务线可自行增删改名）
-- =====================================================================

CREATE TABLE IF NOT EXISTS app_environments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id      INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    env_key     TEXT NOT NULL,                 -- 稳定标识，内置 dev/test/staging/prod；自定义环境由系统生成 c<n>
    label       TEXT NOT NULL,                 -- 显示名（业务线可改，如「灰度」「压测」）
    is_builtin  INTEGER NOT NULL DEFAULT 0 CHECK (is_builtin IN (0,1)),
    sort_no     INTEGER NOT NULL DEFAULT 100,  -- 内置 10/20/30/40，自定义追加在后
    created_at  INTEGER NOT NULL,
    UNIQUE (app_id, env_key)
);

-- 发布窗口（周计划）：一周里哪几天、每天允许发布的时段 [start_time, end_time)
-- 一个环境可有多条（工作日窗口 / 周末窗口分别配置）；
-- 表为空表示「不设周计划限制」，此时仅节假日封网生效。
CREATE TABLE IF NOT EXISTS deploy_windows (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id      INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    env_key     TEXT NOT NULL,
    weekday     INTEGER NOT NULL CHECK (weekday BETWEEN 0 AND 6),  -- 0=周一 … 6=周日（与 Python time.localtime().tm_wday 一致）
    start_time  TEXT NOT NULL,                 -- HH:MM
    end_time    TEXT NOT NULL,                 -- HH:MM；允许 "24:00" 表示当日结束
    created_at  INTEGER NOT NULL,
    UNIQUE (app_id, env_key, weekday, start_time)
);

-- 节假日封网：某天单独关掉发布（优先级高于周计划窗口）
CREATE TABLE IF NOT EXISTS deploy_window_blocks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id      INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    env_key     TEXT NOT NULL,
    block_date  TEXT NOT NULL,                 -- YYYY-MM-DD（本地日期）
    reason      TEXT NOT NULL DEFAULT '',
    created_by  INTEGER REFERENCES users(id),
    created_at  INTEGER NOT NULL,
    UNIQUE (app_id, env_key, block_date)
);

-- 实例：每个应用×环境下实际运行的进程/节点
CREATE TABLE IF NOT EXISTS app_instances (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id        INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    env_key       TEXT NOT NULL,
    node_name     TEXT NOT NULL,               -- 实例标识，如 pay-gw-prod-01
    status        TEXT NOT NULL DEFAULT 'running'
                  CHECK (status IN ('running','stopped')),
    restart_count INTEGER NOT NULL DEFAULT 0,  -- 生命周期内累计重启次数
    last_restart_at INTEGER,
    created_at    INTEGER NOT NULL,
    UNIQUE (app_id, env_key, node_name)
);

-- 实例事件流：注册/重启/掉线/恢复，只追加，供健康统计与运维留痕
CREATE TABLE IF NOT EXISTS instance_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id INTEGER NOT NULL REFERENCES app_instances(id) ON DELETE CASCADE,
    app_id      INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    env_key     TEXT NOT NULL,
    event_type  TEXT NOT NULL CHECK (event_type IN ('register','restart','offline','recover')),
    actor_id    INTEGER REFERENCES users(id),  -- 重启/恢复可由人工触发；掉线/注册为系统监测
    note        TEXT NOT NULL DEFAULT '',
    created_at  INTEGER NOT NULL
);

-- 运维留痕：发布窗口改动、节假日封网、环境增删、发布拦截、实例掉线/重启/恢复
-- 与 config_audit_logs（配置逐键流水）平行，统一供「变更留痕」按应用+时间窗查询
CREATE TABLE IF NOT EXISTS ops_event_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id      INTEGER REFERENCES applications(id) ON DELETE CASCADE,
    business_line_id INTEGER,                  -- 冗余，便于业务线级聚合（应用删除后仍可按线过滤）
    env_key     TEXT NOT NULL DEFAULT '',
    category    TEXT NOT NULL,                 -- window/env/deploy/instance
    action      TEXT NOT NULL,                 -- 具体动作 key
    target      TEXT NOT NULL DEFAULT '',      -- 操作对象文字（实例名/窗口规则/环境名）
    detail      TEXT NOT NULL DEFAULT '',
    actor_id    INTEGER REFERENCES users(id),
    created_at  INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_aenv_app ON app_environments(app_id);
CREATE INDEX IF NOT EXISTS idx_win_app_env ON deploy_windows(app_id, env_key);
CREATE INDEX IF NOT EXISTS idx_block_app_env ON deploy_window_blocks(app_id, env_key);
CREATE INDEX IF NOT EXISTS idx_inst_app_env ON app_instances(app_id, env_key);
CREATE INDEX IF NOT EXISTS idx_inst_status ON app_instances(status);
CREATE INDEX IF NOT EXISTS idx_iev_inst ON instance_events(instance_id);
CREATE INDEX IF NOT EXISTS idx_iev_app_time ON instance_events(app_id, created_at);
CREATE INDEX IF NOT EXISTS idx_ops_app ON ops_event_logs(app_id);
CREATE INDEX IF NOT EXISTS idx_ops_time ON ops_event_logs(created_at);
CREATE INDEX IF NOT EXISTS idx_ops_bl ON ops_event_logs(business_line_id);
CREATE INDEX IF NOT EXISTS idx_ops_cat ON ops_event_logs(category);
"""

_local = threading.local()


def get_conn() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        _local.conn = conn
    return conn


def init_db() -> None:
    conn = get_conn()
    conn.executescript(SCHEMA)
    _migrate_legacy(conn)
    _rebuild_env_check_tables(conn)
    _backfill_app_environments(conn)
    _repair_audit_environment(conn)
    conn.commit()


def _rebuild_env_check_tables(conn) -> None:
    """环境从全局写死改为应用级自定义后，放开三张表 environment 列的枚举 CHECK。

    SQLite 不支持 ALTER TABLE 删约束，只能 关外键 → 改名 → 建新表 → 搬数据 → 删旧表。
    user_grants 的 environment 放宽后可授予具体自定义环境（如灰度 c1），'*' 语义不变。
    """
    new_ddl = {
        "applications": """CREATE TABLE applications (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            name             TEXT NOT NULL,
            business_line_id INTEGER NOT NULL REFERENCES business_lines(id),
            owner_id         INTEGER REFERENCES users(id),
            cluster          TEXT NOT NULL,
            environment      TEXT NOT NULL,
            status           TEXT NOT NULL DEFAULT 'developing'
                             CHECK (status IN ('developing','online','maintenance','offline')),
            description      TEXT NOT NULL DEFAULT '',
            created_at       INTEGER NOT NULL,
            updated_at       INTEGER NOT NULL,
            UNIQUE (business_line_id, name)
        )""",
        "user_grants": """CREATE TABLE user_grants (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id          INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            business_line_id INTEGER NOT NULL REFERENCES business_lines(id) ON DELETE CASCADE,
            environment      TEXT NOT NULL DEFAULT '*',
            can_view_config  INTEGER NOT NULL DEFAULT 1 CHECK (can_view_config IN (0,1)),
            can_edit_config  INTEGER NOT NULL DEFAULT 0 CHECK (can_edit_config IN (0,1)),
            can_reveal       INTEGER NOT NULL DEFAULT 0 CHECK (can_reveal IN (0,1)),
            granted_by       INTEGER REFERENCES users(id),
            created_at       INTEGER NOT NULL,
            updated_at       INTEGER NOT NULL,
            UNIQUE (user_id, business_line_id, environment)
        )""",
        "config_items": """CREATE TABLE config_items (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            app_id      INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
            environment TEXT NOT NULL,
            key         TEXT NOT NULL,
            value       TEXT NOT NULL DEFAULT '',
            value_type  TEXT NOT NULL DEFAULT 'string'
                        CHECK (value_type IN ('string','number','boolean','json')),
            scope       TEXT NOT NULL DEFAULT 'global'
                        CHECK (scope IN ('global','cluster','canary')),
            is_secret   INTEGER NOT NULL DEFAULT 0 CHECK (is_secret IN (0,1)),
            updated_by  INTEGER REFERENCES users(id),
            updated_at  INTEGER NOT NULL,
            UNIQUE (app_id, environment, key)
        )""",
    }
    rebuilt = False
    for table, ddl in new_ddl.items():
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not row or "CHECK (environment IN" not in (row["sql"] or ""):
            continue
        # legacy_alter_table=ON：RENAME 只改表名，不把 env_vars/config_items 等
        # 引用方的外键定义改写指向 *_oldchk（否则 DROP 旧表后外键悬空、后续语句报错）
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("PRAGMA legacy_alter_table=ON")
        cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        col_list = ",".join(cols)
        conn.execute(f"ALTER TABLE {table} RENAME TO {table}_oldchk")
        conn.execute(ddl)
        conn.execute(f"INSERT INTO {table} ({col_list}) SELECT {col_list} FROM {table}_oldchk")
        conn.execute(f"DROP TABLE {table}_oldchk")
        conn.execute("PRAGMA legacy_alter_table=OFF")
        conn.execute("PRAGMA foreign_keys=ON")
        rebuilt = True
    if rebuilt:
        # 旧表上的索引随 DROP 一起消失，重跑建表脚本（全部 IF NOT EXISTS）把索引补回来
        conn.executescript(SCHEMA)


def _backfill_app_environments(conn) -> None:
    """存量库平滑迁移：为每个应用补建四个内置环境。

    环境从"全局写死"改为"应用级实体"后，老库的应用没有 app_environments 行；
    已存在的自定义环境行保留，只补缺的内置环境。
    """
    import time
    now = int(time.time())
    apps = conn.execute("SELECT id FROM applications").fetchall()
    sort_map = {"dev": 10, "test": 20, "staging": 30, "prod": 40}
    for app in apps:
        have = {r["env_key"] for r in conn.execute(
            "SELECT env_key FROM app_environments WHERE app_id = ?", (app["id"],)
        ).fetchall()}
        for key in BUILTIN_ENVIRONMENTS:
            if key not in have:
                conn.execute(
                    """INSERT INTO app_environments (app_id, env_key, label, is_builtin, sort_no, created_at)
                       VALUES (?,?,?,1,?,?)""",
                    (app["id"], key, BUILTIN_ENV_LABELS[key], sort_map[key], now),
                )


def _repair_audit_environment(conn) -> None:
    """修复历史脏数据：逐键改动流水必须与所属版本的环境一致。

    旧版保存逻辑在出现非 global（集群/灰度）键时，会把整批流水错挂到
    “应用所属环境”，导致预发等环境的改动串进生产留痕并绕过按环境的可见范围。
    这里以 config_versions.environment 为权威来源回填纠正；
    reveal 流水 version_id 为 NULL 且本就按实际环境记录，不在修复范围内。
    """
    conn.execute(
        """UPDATE config_audit_logs
           SET environment = (
               SELECT v.environment FROM config_versions v
               WHERE v.id = config_audit_logs.version_id)
           WHERE version_id IS NOT NULL
             AND action IN ('add','update','remove','rollback')
             AND environment <> (
               SELECT v.environment FROM config_versions v
               WHERE v.id = config_audit_logs.version_id)"""
    )


def _migrate_legacy(conn) -> None:
    """旧版库（users.role 仅 admin/member）平滑升级到四角色体系。

    SQL 的 CREATE TABLE IF NOT EXISTS 不会更新既有表约束，这里检测到旧表后
    手工重建 users，并为旧成员补一条"整条业务线全权"授权，保持迁移前能力。
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='users'"
    ).fetchone()
    if not row or "'member'" not in (row["sql"] or ""):
        return
    now = int(__import__("time").time())
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("ALTER TABLE users RENAME TO users_legacy")
    conn.execute(
        """CREATE TABLE users (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            username         TEXT NOT NULL UNIQUE,
            name             TEXT NOT NULL,
            role             TEXT NOT NULL DEFAULT 'viewer'
                             CHECK (role IN ('admin', 'bl_owner', 'app_owner', 'viewer')),
            business_line_id INTEGER REFERENCES business_lines(id),
            token            TEXT NOT NULL UNIQUE
        )"""
    )
    conn.execute(
        """INSERT INTO users (id, username, name, role, business_line_id, token)
           SELECT id, username, name,
                  CASE role WHEN 'admin' THEN 'admin' ELSE 'app_owner' END,
                  business_line_id, token
           FROM users_legacy"""
    )
    conn.execute(
        """INSERT INTO user_grants
               (user_id, business_line_id, environment,
                can_view_config, can_edit_config, can_reveal, granted_by, created_at, updated_at)
           SELECT id, business_line_id, '*', 1, 1, 1, id, ?, ?
           FROM users_legacy WHERE role <> 'admin' AND business_line_id IS NOT NULL""",
        (now, now),
    )
    conn.execute("DROP TABLE users_legacy")
    conn.execute("PRAGMA foreign_keys=ON")



def query(sql: str, params: tuple = ()) -> list:
    return get_conn().execute(sql, params).fetchall()


def query_one(sql: str, params: tuple = ()):
    return get_conn().execute(sql, params).fetchone()


def execute(sql: str, params: tuple = ()) -> sqlite3.Cursor:
    cur = get_conn().execute(sql, params)
    get_conn().commit()
    return cur
