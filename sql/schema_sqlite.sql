-- ============================================================================
-- 亿网通IT运维监控管理平台 —— 数据库建表脚本（SQLite 版）
-- 适用：初期部署 / 开发环境，直接 sqlite3 执行即可
-- 迁移 MySQL：使用 sql/schema_mysql.sql，业务代码零改动（ORM 层已屏蔽方言差异）
-- 约定：
--   1. 不使用物理外键，关联完整性由 Service 层维护（便于 SQLite <-> MySQL 迁移）
--   2. 时间统一 TEXT 存 'YYYY-MM-DD HH:MM:SS'，由 ORM DateTime 处理
--   3. 布尔统一 INTEGER 0/1
--   4. 密码/密钥类字段以 _enc 结尾，Fernet 对称加密后存储
-- ============================================================================

PRAGMA journal_mode = WAL;      -- 写并发关键：WAL 模式允许多线程读不阻塞写
PRAGMA busy_timeout = 5000;      -- 写锁冲突最多等 5 秒，避免采集线程报错
PRAGMA foreign_keys = OFF;       -- 本设计不使用物理外键

-- ----------------------------------------------------------------------------
-- 1. sys_user 系统用户表（模块5：用户与角色权限）
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sys_user (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT    NOT NULL UNIQUE,          -- 登录账号
    password_hash   TEXT    NOT NULL,                 -- bcrypt 哈希，禁止明文
    real_name       TEXT,                             -- 姓名
    role            TEXT    NOT NULL DEFAULT 'viewer',-- admin=管理员 / viewer=运维查看员
    email           TEXT,                             -- 接收邮件告警
    phone           TEXT,
    status          INTEGER NOT NULL DEFAULT 1,       -- 1启用 0禁用
    must_change_pwd INTEGER NOT NULL DEFAULT 0,       -- 首次登录强制改密
    last_login_at   TEXT,
    last_login_ip   TEXT,
    created_at      TEXT,
    updated_at      TEXT,
    remark          TEXT
);
CREATE INDEX IF NOT EXISTS idx_user_role ON sys_user(role);

-- ----------------------------------------------------------------------------
-- 2. sys_config 系统配置表（模块5：系统配置，告警密钥/全局采集参数全部入库）
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sys_config (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    config_key   TEXT    NOT NULL UNIQUE,   -- 配置键，如 mail.smtp_host
    config_value TEXT,                      -- 配置值（JSON 或纯文本）
    config_group TEXT    NOT NULL,          -- mail / wecom / dingtalk / wechat / collect / system
    value_type   TEXT    NOT NULL DEFAULT 'string', -- string / int / bool / json
    is_secret    INTEGER NOT NULL DEFAULT 0,-- 1=敏感项，API 返回时脱敏为 ***
    description  TEXT,
    updated_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_config_group ON sys_config(config_group);

-- ----------------------------------------------------------------------------
-- 3. sys_log 系统日志表（非功能需求：系统日志落地可查）
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sys_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    level      TEXT    NOT NULL,   -- INFO / WARNING / ERROR
    module     TEXT,               -- 来源模块，如 api.device / scheduler
    action     TEXT,               -- 动作，如 login / create_device
    message    TEXT,
    user_id    INTEGER,
    username   TEXT,
    ip         TEXT,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_syslog_time ON sys_log(created_at);
CREATE INDEX IF NOT EXISTS idx_syslog_level ON sys_log(level);

-- ----------------------------------------------------------------------------
-- 4. mon_device 监控设备主表（模块1：设备接入）
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mon_device (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT    NOT NULL,             -- 设备名称
    ip                TEXT    NOT NULL,             -- 管理 IP
    device_type       TEXT    NOT NULL,             -- switch / server / firewall / router / storage
    vendor            TEXT,                         -- huawei / h3c / cisco / windows / linux / kylin / uos / other
    os_type           TEXT,                         -- 操作系统明细，信创主机区分麒麟/统信
    protocol          TEXT    NOT NULL,             -- snmp / ssh / winrm / icmp
    model             TEXT,                         -- 设备型号
    location          TEXT,                         -- 物理位置（机房/楼层/机柜）
    asset_id          INTEGER,                      -- 关联资产（资产绑定监控设备）
    collect_interval  INTEGER NOT NULL DEFAULT 60,  -- 单台独立采集周期（秒）
    timeout           INTEGER NOT NULL DEFAULT 10,  -- 单次采集超时（秒）
    enabled           INTEGER NOT NULL DEFAULT 1,   -- 1启用 0停用采集任务
    online_status     TEXT    NOT NULL DEFAULT 'unknown', -- online / offline / unknown
    fail_count        INTEGER NOT NULL DEFAULT 0,   -- 连续失败次数，用于判定离线
    last_collect_at   TEXT,                         -- 上次采集时间（调度器判是否到点）
    last_online_at    TEXT,
    created_at        TEXT,
    updated_at        TEXT,
    remark            TEXT
);
CREATE INDEX IF NOT EXISTS idx_device_ip ON mon_device(ip);
CREATE INDEX IF NOT EXISTS idx_device_enabled_protocol ON mon_device(enabled, protocol);
CREATE INDEX IF NOT EXISTS idx_device_online ON mon_device(online_status);

-- ----------------------------------------------------------------------------
-- 5. mon_device_credential 设备协议凭据表
--    一张表容纳四类协议凭据，按 protocol 各取所需字段，避免拆成四张小表
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mon_device_credential (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id         INTEGER NOT NULL UNIQUE,       -- 一台设备一套凭据
    protocol          TEXT    NOT NULL,              -- snmp / ssh / winrm / icmp

    -- SNMP v2c / v3
    snmp_version      TEXT    DEFAULT 'v2c',         -- v2c / v3
    snmp_community    TEXT,                          -- v2c 团体字（加密存储）
    snmp_port         INTEGER DEFAULT 161,
    snmp_username     TEXT,                          -- v3 安全名
    snmp_auth_proto   TEXT,                          -- v3 认证协议 MD5 / SHA / SHA224 / SHA256
    snmp_auth_key_enc TEXT,                          -- v3 认证口令（加密）
    snmp_priv_proto   TEXT,                          -- v3 加密协议 DES / AES / AES256
    snmp_priv_key_enc TEXT,                          -- v3 加密口令（加密）
    snmp_timeout      INTEGER DEFAULT 5,
    snmp_retries      INTEGER DEFAULT 2,

    -- SSH（netmiko 适配华为/华三/Linux/信创）
    ssh_port          INTEGER DEFAULT 22,
    ssh_username      TEXT,
    ssh_password_enc  TEXT,                          -- SSH 口令（加密）
    ssh_key_path      TEXT,                          -- 私钥路径，优先于口令
    ssh_device_type   TEXT,                          -- huawei / hp_comware(华三) / linux / autodetect

    -- WinRM（Windows Server）
    winrm_port        INTEGER DEFAULT 5985,          -- 5985 http / 5986 https
    winrm_username    TEXT,
    winrm_password_enc TEXT,                         -- WinRM 口令（加密）
    winrm_transport   TEXT    DEFAULT 'ntlm',        -- ntlm / basic / ssl
    winrm_scheme      TEXT    DEFAULT 'http',        -- http / https

    -- ICMP 长 Ping 参数
    ping_count        INTEGER DEFAULT 5,             -- 每轮发包数
    ping_interval     INTEGER DEFAULT 1,             -- 发包间隔（秒）
    ping_timeout      INTEGER DEFAULT 2,             -- 单包超时（秒）
    ping_size         INTEGER DEFAULT 56,            -- 包大小（字节）

    created_at        TEXT,
    updated_at        TEXT
);
CREATE INDEX IF NOT EXISTS idx_cred_device ON mon_device_credential(device_id);

-- ----------------------------------------------------------------------------
-- 6. asset_info IT资产台账表（模块2）
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS asset_info (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_code      TEXT    NOT NULL UNIQUE,   -- 资产编号，导入时用于去重
    name            TEXT    NOT NULL,          -- 设备名称
    device_type     TEXT,                      -- switch / server / firewall / pc / other
    vendor          TEXT,                      -- 厂商
    model           TEXT,                      -- 型号
    serial_no       TEXT,                      -- 序列号
    ip              TEXT,                      -- IP 地址
    mac             TEXT,                      -- MAC 地址
    room_location   TEXT,                      -- 机房位置
    rack_position   TEXT,                      -- 机柜/机位
    online_at       TEXT,                      -- 上架时间
    warranty_start  TEXT,                      -- 维保起始
    warranty_end    TEXT,                      -- 维保到期（大屏维保预警依据）
    owner           TEXT,                      -- 责任人
    owner_phone     TEXT,
    supplier        TEXT,                      -- 供应商
    purchase_at     TEXT,                      -- 采购日期
    purchase_price  REAL,                      -- 采购金额
    status          TEXT    DEFAULT 'in_use',  -- in_use / idle / repair / scrapped
    device_id       INTEGER,                   -- 绑定的监控设备 ID（绑定后可看实时数据）
    created_at      TEXT,
    updated_at      TEXT,
    remark          TEXT
);
CREATE INDEX IF NOT EXISTS idx_asset_warranty ON asset_info(warranty_end);
CREATE INDEX IF NOT EXISTS idx_asset_device ON asset_info(device_id);
CREATE INDEX IF NOT EXISTS idx_asset_status ON asset_info(status);

-- ----------------------------------------------------------------------------
-- 7. asset_maintain_remind 资产维保到期提醒表（大屏容量视图数据源）
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS asset_maintain_remind (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id      INTEGER NOT NULL,
    asset_code    TEXT,
    asset_name    TEXT,
    warranty_end  TEXT    NOT NULL,
    remind_days   INTEGER NOT NULL DEFAULT 30,  -- 提前提醒天数
    remind_level  TEXT,                         -- expired / urgent(<=30天) / soon(<=90天)
    remind_status TEXT    NOT NULL DEFAULT 'pending', -- pending / sent / ignored
    sent_at       TEXT,
    created_at    TEXT,
    updated_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_remind_status ON asset_maintain_remind(remind_status);

-- ----------------------------------------------------------------------------
-- 8. metric_catalog 指标字典表
--    新增指标 = 插入一行 + 写一个解析函数，不改表结构（扩展性需求）
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS metric_catalog (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    metric_key   TEXT    NOT NULL UNIQUE,   -- 指标键，如 cpu_usage / if_in_rate
    metric_name  TEXT    NOT NULL,          -- 中文名
    unit         TEXT,                      -- % / Mbps / ms / ℃ / KB
    metric_type  TEXT    DEFAULT 'gauge',   -- gauge(瞬时值) / counter(累计值) / state(状态值)
    value_type   TEXT    DEFAULT 'float',   -- float / int / string
    protocol     TEXT,                      -- 来源协议 snmp / ssh / winrm / icmp / any
    category     TEXT,                      -- cpu / memory / disk / interface / ping / system
    is_builtin   INTEGER NOT NULL DEFAULT 1,-- 内置指标不可删除
    enabled      INTEGER NOT NULL DEFAULT 1,
    sort_order   INTEGER DEFAULT 0,
    created_at   TEXT,
    updated_at   TEXT,   -- 与 ORM 的 TimestampMixin 保持一致，否则查询会报 no such column
    remark       TEXT
);

-- ----------------------------------------------------------------------------
-- 9. metric_latest 最新指标表（热表）
--    大屏实时数据全读这里；行数 = 设备数 x 指标数，恒定可控
--    唯一键支撑 UPSERT，避免每轮采集无限膨胀
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS metric_latest (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id    INTEGER NOT NULL,
    metric_key   TEXT    NOT NULL,
    metric_value REAL,                      -- 数值型指标
    str_value    TEXT,                      -- 字符串型指标（版本、端口状态 up/down）
    unit         TEXT,
    collect_time TEXT    NOT NULL,          -- 采集时间
    created_at   TEXT,
    updated_at   TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uk_latest_device_metric ON metric_latest(device_id, metric_key);
CREATE INDEX IF NOT EXISTS idx_latest_metric ON metric_latest(metric_key);

-- ----------------------------------------------------------------------------
-- 10. metric_sample 历史指标明细表（冷表）
--     只 INSERT 不 UPDATE；靠定时任务降采样清理（7天原始 / 30天5分钟 / 90天小时）
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS metric_sample (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id    INTEGER NOT NULL,
    metric_key   TEXT    NOT NULL,
    metric_value REAL,
    str_value    TEXT,
    unit         TEXT,
    collect_time TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sample_device_metric_time ON metric_sample(device_id, metric_key, collect_time);
CREATE INDEX IF NOT EXISTS idx_sample_time ON metric_sample(collect_time);

-- ----------------------------------------------------------------------------
-- 11. alert_rule 告警规则表（模块3：自定义阈值）
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS alert_rule (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT    NOT NULL,           -- 规则名称
    device_id        INTEGER,                    -- NULL = 全局规则，对同类设备批量生效
    metric_key       TEXT    NOT NULL,           -- 绑定指标键，offline 为特殊键（设备离线）
    operator         TEXT    NOT NULL DEFAULT 'gt', -- gt / lt / ge / le / eq / ne
    threshold        REAL    NOT NULL,           -- 阈值
    duration_n       INTEGER NOT NULL DEFAULT 3, -- 连续 N 次超标才触发（防抖动误报）
    recovery_n       INTEGER NOT NULL DEFAULT 3, -- 连续 M 次正常才恢复（滞回）
    level            TEXT    NOT NULL DEFAULT 'major', -- critical紧急/major重要/minor一般/info提示
    converge_window  INTEGER NOT NULL DEFAULT 300,     -- 收敛窗口（秒）
    converge_max     INTEGER NOT NULL DEFAULT 3,       -- 窗口内最大推送条数（防轰炸）
    notify_channels  TEXT    DEFAULT '[]',       -- JSON 数组 ["email","wecom","dingtalk","wechat"]
    silence_until    TEXT,                       -- 静默截止时间（维护窗口不发告警）
    enabled          INTEGER NOT NULL DEFAULT 1,
    created_at       TEXT,
    updated_at       TEXT,
    remark           TEXT
);
CREATE INDEX IF NOT EXISTS idx_rule_device_metric ON alert_rule(device_id, metric_key);
CREATE INDEX IF NOT EXISTS idx_rule_enabled ON alert_rule(enabled);

-- ----------------------------------------------------------------------------
-- 12. alert_rule_state 规则运行状态表（连续计数器）
--     落库而非仅内存：平台重启后计数不丢失，避免重启瞬间产生一批重复告警
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS alert_rule_state (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id          INTEGER NOT NULL,
    device_id        INTEGER NOT NULL,
    breach_count     INTEGER NOT NULL DEFAULT 0,  -- 连续超标次数
    normal_count     INTEGER NOT NULL DEFAULT 0,  -- 连续正常次数
    status           TEXT    NOT NULL DEFAULT 'normal', -- normal / firing / acked / recovered
    last_value       REAL,
    last_eval_time   TEXT,
    last_fire_time   TEXT,
    current_event_id INTEGER,                     -- 当前未关闭的告警事件
    updated_at       TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uk_rule_state ON alert_rule_state(rule_id, device_id);

-- ----------------------------------------------------------------------------
-- 13. alert_event 告警事件表（告警中心主表）
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS alert_event (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    event_no       TEXT,                       -- 事件编号，便于运维口头沟通
    rule_id        INTEGER,
    device_id      INTEGER NOT NULL,
    device_name    TEXT,
    device_ip      TEXT,
    metric_key     TEXT,
    metric_name    TEXT,
    metric_value   REAL,                       -- 触发时指标值
    threshold      REAL,
    operator       TEXT,
    level          TEXT    NOT NULL,           -- critical / major / minor / info
    title          TEXT,
    content        TEXT,
    fingerprint    TEXT    NOT NULL,           -- md5(device_id + rule_id)，收敛查重用
    status         TEXT    NOT NULL DEFAULT 'firing', -- firing / acked / recovered / closed
    occur_count    INTEGER NOT NULL DEFAULT 1, -- 同指纹累计触发次数
    first_occur_at TEXT,
    last_occur_at  TEXT,
    recover_at     TEXT,                       -- 恢复时间
    ack_at         TEXT,                       -- 人工确认时间
    ack_user       TEXT,
    ack_remark     TEXT,
    created_at     TEXT,
    updated_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_event_fingerprint ON alert_event(fingerprint);
CREATE INDEX IF NOT EXISTS idx_event_status ON alert_event(status);
CREATE INDEX IF NOT EXISTS idx_event_level ON alert_event(level);
CREATE INDEX IF NOT EXISTS idx_event_last_occur ON alert_event(last_occur_at);
CREATE INDEX IF NOT EXISTS idx_event_device ON alert_event(device_id);

-- ----------------------------------------------------------------------------
-- 14. alert_notify_record 告警通知记录表（每渠道一条，失败可重发）
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS alert_notify_record (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id     INTEGER NOT NULL,
    channel      TEXT    NOT NULL,           -- email / wecom / dingtalk / wechat
    target       TEXT,                       -- 收件人 / 机器人标识
    status       TEXT    NOT NULL DEFAULT 'pending', -- pending / success / fail
    retry_count  INTEGER NOT NULL DEFAULT 0,
    response_msg TEXT,
    sent_at      TEXT,
    created_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_notify_event ON alert_notify_record(event_id);
CREATE INDEX IF NOT EXISTS idx_notify_status ON alert_notify_record(status);

-- ----------------------------------------------------------------------------
-- 15. collect_log 采集日志表（非功能需求：采集日志落地，失败不影响其他设备）
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS collect_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id    INTEGER NOT NULL,
    device_name  TEXT,
    device_ip    TEXT,
    protocol     TEXT,
    status       TEXT    NOT NULL,           -- success / fail / timeout
    duration_ms  INTEGER,                    -- 采集耗时（毫秒），用于慢设备排查
    error_msg    TEXT,                       -- 异常信息
    metric_count INTEGER DEFAULT 0,          -- 本轮采集到的指标条数
    collect_time TEXT
);
CREATE INDEX IF NOT EXISTS idx_collectlog_time ON collect_log(collect_time);
CREATE INDEX IF NOT EXISTS idx_collectlog_device ON collect_log(device_id);
CREATE INDEX IF NOT EXISTS idx_collectlog_status ON collect_log(status);

-- ----------------------------------------------------------------------------
-- 16. alarm_log 告警处理日志表（触发/恢复/确认/通知全链路留痕）
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS alarm_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id   INTEGER,
    device_id  INTEGER,
    action     TEXT    NOT NULL,             -- trigger / recover / ack / notify / close / converge
    channel    TEXT,                         -- 通知渠道（action=notify 时有值）
    level      TEXT,
    message    TEXT,
    operator   TEXT,                         -- 操作人（ack 时）
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_alarmlog_time ON alarm_log(created_at);
CREATE INDEX IF NOT EXISTS idx_alarmlog_event ON alarm_log(event_id);

-- ============================================================================
-- 建表完成。执行 sql/init_data.sql 灌入初始化数据（管理员、指标字典、默认规则）
-- ============================================================================

-- ----------------------------------------------------------------------------
-- N. sys_scheduled_task 自定义定时任务表（系统配置页"定时任务"面板）
--    动作取值受后端 ACTION_REGISTRY 约束；period 二选一：
--    trigger_type=interval 用 interval_seconds，cron 用 cron_expr（5 段）。
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sys_scheduled_task (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT    NOT NULL UNIQUE,  -- 任务名称，全平台唯一
    action           TEXT    NOT NULL,         -- 动作标识，见 app/scheduler/custom_tasks.py
    trigger_type     TEXT    NOT NULL DEFAULT 'interval', -- interval / cron
    interval_seconds INTEGER,                  -- 固定间隔秒数（interval 类型必填，最小 10）
    cron_expr        TEXT,                     -- 5 段 cron 表达式（cron 类型必填）
    enabled          INTEGER NOT NULL DEFAULT 1,
    remark           TEXT,
    last_run_at      TEXT,                     -- 最近一次执行时间
    last_run_ok      INTEGER,                  -- 最近一次执行是否成功（NULL=从未执行）
    last_run_message TEXT,                     -- 最近一次执行结果摘要/错误信息
    created_at       TEXT,
    updated_at       TEXT
);
