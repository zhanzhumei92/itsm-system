-- ============================================================================
-- 亿网通IT运维监控管理平台 —— 数据库建表脚本（MySQL 版）
-- 适用：设备规模 > 500 台或采集周期 < 30s 时迁移
-- 迁移方式：仅修改 .env 中 DATABASE_URL 为
--           mysql+pymysql://user:pass@host:3306/itsm?charset=utf8mb4
--           业务代码与 SQLAlchemy 模型零改动
-- 与 SQLite 版的差异仅在类型与注释语法，表/字段/索引完全一致
-- ============================================================================

SET NAMES utf8mb4;

-- ----------------------------------------------------------------------------
-- 1. sys_user 系统用户表（模块5：用户与角色权限）
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sys_user (
    id              BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
    username        VARCHAR(64)  NOT NULL COMMENT '登录账号',
    password_hash   VARCHAR(255) NOT NULL COMMENT 'bcrypt 哈希，禁止明文',
    real_name       VARCHAR(64)  COMMENT '姓名',
    role            VARCHAR(32)  NOT NULL DEFAULT 'viewer' COMMENT 'admin=管理员 / viewer=运维查看员',
    email           VARCHAR(128) COMMENT '接收邮件告警',
    phone           VARCHAR(32),
    status          TINYINT(1)   NOT NULL DEFAULT 1 COMMENT '1启用 0禁用',
    must_change_pwd TINYINT(1)   NOT NULL DEFAULT 0 COMMENT '首次登录强制改密',
    last_login_at   DATETIME,
    last_login_ip   VARCHAR(64),
    created_at      DATETIME,
    updated_at      DATETIME,
    remark          VARCHAR(255),
    PRIMARY KEY (id),
    UNIQUE KEY uk_user_username (username),
    KEY idx_user_role (role)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='系统用户表';

-- ----------------------------------------------------------------------------
-- 2. sys_config 系统配置表（模块5：告警密钥与全局采集参数入库）
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sys_config (
    id           BIGINT       NOT NULL AUTO_INCREMENT,
    config_key   VARCHAR(100) NOT NULL COMMENT '配置键，如 mail.smtp_host',
    config_value TEXT         COMMENT '配置值（JSON 或纯文本）',
    config_group VARCHAR(32)  NOT NULL COMMENT 'mail/wecom/dingtalk/wechat/collect/system',
    value_type   VARCHAR(16)  NOT NULL DEFAULT 'string' COMMENT 'string/int/bool/json',
    is_secret    TINYINT(1)   NOT NULL DEFAULT 0 COMMENT '1=敏感项，API 返回脱敏为 ***',
    description  VARCHAR(255),
    updated_at   DATETIME,
    PRIMARY KEY (id),
    UNIQUE KEY uk_config_key (config_key),
    KEY idx_config_group (config_group)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='系统配置表';

-- ----------------------------------------------------------------------------
-- 3. sys_log 系统日志表
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sys_log (
    id         BIGINT       NOT NULL AUTO_INCREMENT,
    level      VARCHAR(16)  NOT NULL COMMENT 'INFO/WARNING/ERROR',
    module     VARCHAR(64)  COMMENT '来源模块',
    action     VARCHAR(64)  COMMENT '动作',
    message    TEXT,
    user_id    BIGINT,
    username   VARCHAR(64),
    ip         VARCHAR(64),
    created_at DATETIME,
    PRIMARY KEY (id),
    KEY idx_syslog_time (created_at),
    KEY idx_syslog_level (level)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='系统日志表';

-- ----------------------------------------------------------------------------
-- 4. mon_device 监控设备主表（模块1：设备接入）
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mon_device (
    id               BIGINT       NOT NULL AUTO_INCREMENT,
    name             VARCHAR(128) NOT NULL COMMENT '设备名称',
    ip               VARCHAR(64)  NOT NULL COMMENT '管理 IP',
    device_type      VARCHAR(32)  NOT NULL COMMENT 'switch/server/firewall/router/storage',
    vendor           VARCHAR(32)  COMMENT 'huawei/h3c/cisco/windows/linux/kylin/uos/other',
    os_type          VARCHAR(64)  COMMENT '操作系统明细，信创主机区分麒麟/统信',
    protocol         VARCHAR(16)  NOT NULL COMMENT 'snmp/ssh/winrm/icmp',
    model            VARCHAR(64)  COMMENT '设备型号',
    location         VARCHAR(128) COMMENT '物理位置',
    asset_id         BIGINT       COMMENT '关联资产',
    collect_interval INT          NOT NULL DEFAULT 60 COMMENT '单台独立采集周期（秒）',
    timeout          INT          NOT NULL DEFAULT 10 COMMENT '单次采集超时（秒）',
    enabled          TINYINT(1)   NOT NULL DEFAULT 1 COMMENT '1启用 0停用',
    online_status    VARCHAR(16)  NOT NULL DEFAULT 'unknown' COMMENT 'online/offline/unknown',
    fail_count       INT          NOT NULL DEFAULT 0 COMMENT '连续失败次数',
    last_collect_at  DATETIME     COMMENT '上次采集时间',
    last_online_at   DATETIME,
    created_at       DATETIME,
    updated_at       DATETIME,
    remark           VARCHAR(255),
    PRIMARY KEY (id),
    KEY idx_device_ip (ip),
    KEY idx_device_enabled_protocol (enabled, protocol),
    KEY idx_device_online (online_status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='监控设备主表';

-- ----------------------------------------------------------------------------
-- 5. mon_device_credential 设备协议凭据表（四类协议合一）
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mon_device_credential (
    id                 BIGINT       NOT NULL AUTO_INCREMENT,
    device_id          BIGINT       NOT NULL COMMENT '一台设备一套凭据',
    protocol           VARCHAR(16)  NOT NULL COMMENT 'snmp/ssh/winrm/icmp',
    snmp_version       VARCHAR(8)   DEFAULT 'v2c' COMMENT 'v2c/v3',
    snmp_community     VARCHAR(128) COMMENT 'v2c 团体字（加密存储）',
    snmp_port          INT          DEFAULT 161,
    snmp_username      VARCHAR(64)  COMMENT 'v3 安全名',
    snmp_auth_proto    VARCHAR(16)  COMMENT 'v3 认证协议 MD5/SHA/SHA256',
    snmp_auth_key_enc  VARCHAR(255) COMMENT 'v3 认证口令（加密）',
    snmp_priv_proto    VARCHAR(16)  COMMENT 'v3 加密协议 DES/AES/AES256',
    snmp_priv_key_enc  VARCHAR(255) COMMENT 'v3 加密口令（加密）',
    snmp_timeout       INT          DEFAULT 5,
    snmp_retries       INT          DEFAULT 2,
    ssh_port           INT          DEFAULT 22,
    ssh_username       VARCHAR(64),
    ssh_password_enc   VARCHAR(255) COMMENT 'SSH 口令（加密）',
    ssh_key_path       VARCHAR(255) COMMENT '私钥路径，优先于口令',
    ssh_device_type    VARCHAR(32)  COMMENT 'huawei/hp_comware(华三)/linux/autodetect',
    winrm_port         INT          DEFAULT 5985 COMMENT '5985 http / 5986 https',
    winrm_username     VARCHAR(64),
    winrm_password_enc VARCHAR(255) COMMENT 'WinRM 口令（加密）',
    winrm_transport    VARCHAR(16)  DEFAULT 'ntlm' COMMENT 'ntlm/basic/ssl',
    winrm_scheme       VARCHAR(8)   DEFAULT 'http',
    ping_count         INT          DEFAULT 5 COMMENT '每轮发包数',
    ping_interval      INT          DEFAULT 1 COMMENT '发包间隔（秒）',
    ping_timeout       INT          DEFAULT 2 COMMENT '单包超时（秒）',
    ping_size          INT          DEFAULT 56 COMMENT '包大小（字节）',
    created_at         DATETIME,
    updated_at         DATETIME,
    PRIMARY KEY (id),
    UNIQUE KEY uk_cred_device (device_id),
    KEY idx_cred_device (device_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='设备协议凭据表';

-- ----------------------------------------------------------------------------
-- 6. asset_info IT资产台账表（模块2）
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS asset_info (
    id             BIGINT        NOT NULL AUTO_INCREMENT,
    asset_code     VARCHAR(64)   NOT NULL COMMENT '资产编号，导入去重依据',
    name           VARCHAR(128)  NOT NULL COMMENT '设备名称',
    device_type    VARCHAR(32)   COMMENT 'switch/server/firewall/pc/other',
    vendor         VARCHAR(64)   COMMENT '厂商',
    model          VARCHAR(64)   COMMENT '型号',
    serial_no      VARCHAR(128)  COMMENT '序列号',
    ip             VARCHAR(64)   COMMENT 'IP 地址',
    mac            VARCHAR(64)   COMMENT 'MAC 地址',
    room_location  VARCHAR(128)  COMMENT '机房位置',
    rack_position  VARCHAR(64)   COMMENT '机柜/机位',
    online_at      DATETIME      COMMENT '上架时间',
    warranty_start DATETIME      COMMENT '维保起始',
    warranty_end   DATETIME      COMMENT '维保到期（大屏预警依据）',
    owner          VARCHAR(64)   COMMENT '责任人',
    owner_phone    VARCHAR(32),
    supplier       VARCHAR(128)  COMMENT '供应商',
    purchase_at    DATETIME      COMMENT '采购日期',
    purchase_price DECIMAL(12,2) COMMENT '采购金额',
    status         VARCHAR(16)   DEFAULT 'in_use' COMMENT 'in_use/idle/repair/scrapped',
    device_id      BIGINT        COMMENT '绑定的监控设备 ID',
    created_at     DATETIME,
    updated_at     DATETIME,
    remark         TEXT,
    PRIMARY KEY (id),
    UNIQUE KEY uk_asset_code (asset_code),
    KEY idx_asset_warranty (warranty_end),
    KEY idx_asset_device (device_id),
    KEY idx_asset_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='IT资产台账表';

-- ----------------------------------------------------------------------------
-- 7. asset_maintain_remind 资产维保到期提醒表
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS asset_maintain_remind (
    id            BIGINT      NOT NULL AUTO_INCREMENT,
    asset_id      BIGINT      NOT NULL,
    asset_code    VARCHAR(64),
    asset_name    VARCHAR(128),
    warranty_end  DATETIME    NOT NULL,
    remind_days   INT         NOT NULL DEFAULT 30 COMMENT '提前提醒天数',
    remind_level  VARCHAR(16) COMMENT 'expired/urgent(<=30天)/soon(<=90天)',
    remind_status VARCHAR(16) NOT NULL DEFAULT 'pending' COMMENT 'pending/sent/ignored',
    sent_at       DATETIME,
    created_at    DATETIME,
    updated_at    DATETIME,
    PRIMARY KEY (id),
    KEY idx_remind_status (remind_status),
    KEY idx_remind_end (warranty_end)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='资产维保到期提醒表';

-- ----------------------------------------------------------------------------
-- 8. metric_catalog 指标字典表（新增指标只加数据，不改表结构）
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS metric_catalog (
    id          BIGINT      NOT NULL AUTO_INCREMENT,
    metric_key  VARCHAR(64) NOT NULL COMMENT '指标键，如 cpu_usage',
    metric_name VARCHAR(64) NOT NULL COMMENT '中文名',
    unit        VARCHAR(16) COMMENT '%/Mbps/ms/℃/KB',
    metric_type VARCHAR(16) DEFAULT 'gauge' COMMENT 'gauge/counter/state',
    value_type  VARCHAR(16) DEFAULT 'float' COMMENT 'float/int/string',
    protocol    VARCHAR(16) COMMENT 'snmp/ssh/winrm/icmp/any',
    category    VARCHAR(32) COMMENT 'cpu/memory/disk/interface/ping/system',
    is_builtin  TINYINT(1)  NOT NULL DEFAULT 1 COMMENT '内置指标不可删除',
    enabled     TINYINT(1)  NOT NULL DEFAULT 1,
    sort_order  INT         DEFAULT 0,
    created_at  DATETIME,
    updated_at  DATETIME COMMENT '更新时间（与 ORM TimestampMixin 对应）',
    remark      VARCHAR(255),
    PRIMARY KEY (id),
    UNIQUE KEY uk_metric_key (metric_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='指标字典表';

-- ----------------------------------------------------------------------------
-- 9. metric_latest 最新指标表（热表，大屏实时读）
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS metric_latest (
    id           BIGINT         NOT NULL AUTO_INCREMENT,
    device_id    BIGINT         NOT NULL,
    metric_key   VARCHAR(64)    NOT NULL,
    metric_value DECIMAL(18,4)  COMMENT '数值型指标',
    str_value    VARCHAR(255)   COMMENT '字符串型指标（版本、端口 up/down）',
    unit         VARCHAR(16),
    collect_time DATETIME       NOT NULL,
    created_at   DATETIME,
    updated_at   DATETIME,
    PRIMARY KEY (id),
    UNIQUE KEY uk_latest_device_metric (device_id, metric_key),
    KEY idx_latest_metric (metric_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='最新指标热表';

-- ----------------------------------------------------------------------------
-- 10. metric_sample 历史指标明细表（冷表，降采样清理）
--     数据量到千万级后可对 collect_time 做 RANGE 分区
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS metric_sample (
    id           BIGINT        NOT NULL AUTO_INCREMENT,
    device_id    BIGINT        NOT NULL,
    metric_key   VARCHAR(64)   NOT NULL,
    metric_value DECIMAL(18,4),
    str_value    VARCHAR(255),
    unit         VARCHAR(16),
    collect_time DATETIME      NOT NULL,
    PRIMARY KEY (id),
    KEY idx_sample_device_metric_time (device_id, metric_key, collect_time),
    KEY idx_sample_time (collect_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='历史指标冷表';

-- ----------------------------------------------------------------------------
-- 11. alert_rule 告警规则表（模块3：自定义阈值）
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS alert_rule (
    id              BIGINT       NOT NULL AUTO_INCREMENT,
    name            VARCHAR(128) NOT NULL COMMENT '规则名称',
    device_id       BIGINT       COMMENT 'NULL=全局规则',
    metric_key      VARCHAR(64)  NOT NULL COMMENT '指标键，offline 为设备离线特殊键',
    operator        VARCHAR(8)   NOT NULL DEFAULT 'gt' COMMENT 'gt/lt/ge/le/eq/ne',
    threshold       DECIMAL(18,4) NOT NULL COMMENT '阈值',
    duration_n      INT          NOT NULL DEFAULT 3 COMMENT '连续 N 次超标才触发',
    recovery_n      INT          NOT NULL DEFAULT 3 COMMENT '连续 M 次正常才恢复（滞回）',
    level           VARCHAR(16)  NOT NULL DEFAULT 'major' COMMENT 'critical/major/minor/info',
    converge_window INT          NOT NULL DEFAULT 300 COMMENT '收敛窗口（秒）',
    converge_max    INT          NOT NULL DEFAULT 3 COMMENT '窗口内最大推送条数',
    notify_channels VARCHAR(255) DEFAULT '[]' COMMENT 'JSON 数组 ["email","wecom",...]',
    silence_until   DATETIME     COMMENT '静默截止时间',
    enabled         TINYINT(1)   NOT NULL DEFAULT 1,
    created_at      DATETIME,
    updated_at      DATETIME,
    remark          VARCHAR(255),
    PRIMARY KEY (id),
    KEY idx_rule_device_metric (device_id, metric_key),
    KEY idx_rule_enabled (enabled)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='告警规则表';

-- ----------------------------------------------------------------------------
-- 12. alert_rule_state 规则运行状态表（连续计数器落库，防重启重复告警）
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS alert_rule_state (
    id               BIGINT      NOT NULL AUTO_INCREMENT,
    rule_id          BIGINT      NOT NULL,
    device_id        BIGINT      NOT NULL,
    breach_count     INT         NOT NULL DEFAULT 0 COMMENT '连续超标次数',
    normal_count     INT         NOT NULL DEFAULT 0 COMMENT '连续正常次数',
    status           VARCHAR(16) NOT NULL DEFAULT 'normal' COMMENT 'normal/firing/acked/recovered',
    last_value       DECIMAL(18,4),
    last_eval_time   DATETIME,
    last_fire_time   DATETIME,
    current_event_id BIGINT      COMMENT '当前未关闭的告警事件',
    updated_at       DATETIME,
    PRIMARY KEY (id),
    UNIQUE KEY uk_rule_state (rule_id, device_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='告警规则运行状态表';

-- ----------------------------------------------------------------------------
-- 13. alert_event 告警事件表（告警中心主表）
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS alert_event (
    id             BIGINT       NOT NULL AUTO_INCREMENT,
    event_no       VARCHAR(64)  COMMENT '事件编号',
    rule_id        BIGINT,
    device_id      BIGINT       NOT NULL,
    device_name    VARCHAR(128),
    device_ip      VARCHAR(64),
    metric_key     VARCHAR(64),
    metric_name    VARCHAR(64),
    metric_value   DECIMAL(18,4) COMMENT '触发时指标值',
    threshold      DECIMAL(18,4),
    operator       VARCHAR(8),
    level          VARCHAR(16)  NOT NULL COMMENT 'critical/major/minor/info',
    title          VARCHAR(255),
    content        TEXT,
    fingerprint    VARCHAR(64)  NOT NULL COMMENT 'md5(device_id+rule_id)，收敛查重',
    status         VARCHAR(16)  NOT NULL DEFAULT 'firing' COMMENT 'firing/acked/recovered/closed',
    occur_count    INT          NOT NULL DEFAULT 1 COMMENT '同指纹累计触发次数',
    first_occur_at DATETIME,
    last_occur_at  DATETIME,
    recover_at     DATETIME,
    ack_at         DATETIME,
    ack_user       VARCHAR(64),
    ack_remark     VARCHAR(255),
    created_at     DATETIME,
    updated_at     DATETIME,
    PRIMARY KEY (id),
    KEY idx_event_fingerprint (fingerprint),
    KEY idx_event_status (status),
    KEY idx_event_level (level),
    KEY idx_event_last_occur (last_occur_at),
    KEY idx_event_device (device_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='告警事件表';

-- ----------------------------------------------------------------------------
-- 14. alert_notify_record 告警通知记录表（每渠道一条，失败可重发）
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS alert_notify_record (
    id           BIGINT      NOT NULL AUTO_INCREMENT,
    event_id     BIGINT      NOT NULL,
    channel      VARCHAR(16) NOT NULL COMMENT 'email/wecom/dingtalk/wechat',
    target       VARCHAR(255) COMMENT '收件人 / 机器人标识',
    status       VARCHAR(16) NOT NULL DEFAULT 'pending' COMMENT 'pending/success/fail',
    retry_count  INT         NOT NULL DEFAULT 0,
    response_msg TEXT,
    sent_at      DATETIME,
    created_at   DATETIME,
    PRIMARY KEY (id),
    KEY idx_notify_event (event_id),
    KEY idx_notify_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='告警通知记录表';

-- ----------------------------------------------------------------------------
-- 15. collect_log 采集日志表
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS collect_log (
    id           BIGINT      NOT NULL AUTO_INCREMENT,
    device_id    BIGINT      NOT NULL,
    device_name  VARCHAR(128),
    device_ip    VARCHAR(64),
    protocol     VARCHAR(16),
    status       VARCHAR(16) NOT NULL COMMENT 'success/fail/timeout',
    duration_ms  INT         COMMENT '采集耗时（毫秒）',
    error_msg    TEXT,
    metric_count INT         DEFAULT 0,
    collect_time DATETIME,
    PRIMARY KEY (id),
    KEY idx_collectlog_time (collect_time),
    KEY idx_collectlog_device (device_id),
    KEY idx_collectlog_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='采集日志表';

-- ----------------------------------------------------------------------------
-- 16. alarm_log 告警处理日志表
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS alarm_log (
    id         BIGINT      NOT NULL AUTO_INCREMENT,
    event_id   BIGINT,
    device_id  BIGINT,
    action     VARCHAR(16) NOT NULL COMMENT 'trigger/recover/ack/notify/close/converge',
    channel    VARCHAR(16) COMMENT '通知渠道',
    level      VARCHAR(16),
    message    TEXT,
    operator   VARCHAR(64) COMMENT '操作人',
    created_at DATETIME,
    PRIMARY KEY (id),
    KEY idx_alarmlog_time (created_at),
    KEY idx_alarmlog_event (event_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='告警处理日志表';

-- ============================================================================
-- 建表完成。执行 sql/init_data.sql 灌入初始化数据
-- ============================================================================

-- ----------------------------------------------------------------------------
-- N. sys_scheduled_task 自定义定时任务表（系统配置页"定时任务"面板）
--    动作取值受后端 ACTION_REGISTRY 约束；period 二选一：
--    trigger_type=interval 用 interval_seconds，cron 用 cron_expr（5 段）。
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sys_scheduled_task (
    id               BIGINT       NOT NULL AUTO_INCREMENT,
    name             VARCHAR(100) NOT NULL COMMENT '任务名称，全平台唯一',
    action           VARCHAR(64)  NOT NULL COMMENT '动作标识，见 app/scheduler/custom_tasks.py',
    trigger_type     VARCHAR(16)  NOT NULL DEFAULT 'interval' COMMENT 'interval / cron',
    interval_seconds INT          COMMENT '固定间隔秒数（interval 类型必填，最小 10）',
    cron_expr        VARCHAR(64)  COMMENT '5 段 cron 表达式（cron 类型必填）',
    enabled          TINYINT(1)   NOT NULL DEFAULT 1,
    remark           VARCHAR(255),
    last_run_at      DATETIME     COMMENT '最近一次执行时间',
    last_run_ok      TINYINT(1)   COMMENT '最近一次执行是否成功（NULL=从未执行）',
    last_run_message TEXT         COMMENT '最近一次执行结果摘要/错误信息',
    created_at       DATETIME,
    updated_at       DATETIME,
    PRIMARY KEY (id),
    UNIQUE KEY uk_task_name (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='自定义定时任务表';
