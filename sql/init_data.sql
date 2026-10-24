-- ============================================================================
-- 亿网通IT运维监控管理平台 —— 初始化数据脚本（SQLite / MySQL 通用）
-- 执行顺序：schema_sqlite.sql 或 schema_mysql.sql → 本文件
-- 内容：默认账号、指标字典、系统配置、默认告警规则
-- 说明：时间使用固定字符串以保证两套数据库通用；重复执行用 INSERT 前先 DELETE 保证幂等
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 一、默认账号（bcrypt 哈希，首次登录请立即修改密码）
--   admin  / admin123   系统管理员（全权限）
--   viewer / viewer123  运维查看员（只读 + 告警确认）
-- ----------------------------------------------------------------------------
DELETE FROM sys_user WHERE username IN ('admin', 'viewer');
INSERT INTO sys_user (username, password_hash, real_name, role, email, phone, status, must_change_pwd, created_at, updated_at, remark) VALUES
('admin',  '$2b$12$UfdR/Qw6hIIjU8VzCVVPDeZjsVgfmOmnxaIH9Mn5dW84kNRKY1GOa', '系统管理员', 'admin',  'admin@enetop.com',  '18025081188', 1, 0, '2026-09-21 14:00:00', '2026-09-21 14:00:00', '默认管理员账号'),
('viewer', '$2b$12$iGNcbHTKMwvUMIQpRXK0sOu/AASe5LLFNMk774uvWhZ2p52QMLWcq', '运维查看员', 'viewer', 'viewer@enetop.com', '',             1, 0, '2026-09-21 14:00:00', '2026-09-21 14:00:00', '默认只读账号');

-- ----------------------------------------------------------------------------
-- 二、指标字典（metric_catalog）
--   新增采集指标只需在此插入一行 + 在采集器实现一个解析函数，无需改表结构
-- ----------------------------------------------------------------------------
DELETE FROM metric_catalog;
INSERT INTO metric_catalog (metric_key, metric_name, unit, metric_type, value_type, protocol, category, is_builtin, enabled, sort_order, created_at, remark) VALUES
('cpu_usage',      'CPU使用率',     '%',    'gauge',   'float',  'any',    'cpu',       1, 1, 10, '2026-09-21 14:00:00', 'SNMP hrProcessorLoad / WMI / top'),
('mem_usage',      '内存使用率',     '%',    'gauge',   'float',  'any',    'memory',    1, 1, 11, '2026-09-21 14:00:00', '内存已用/总量'),
('mem_total',      '内存总量',       'MB',   'gauge',   'float',  'any',    'memory',    1, 1, 12, '2026-09-21 14:00:00', ''),
('mem_used',       '内存已用',       'MB',   'gauge',   'float',  'any',    'memory',    1, 1, 13, '2026-09-21 14:00:00', ''),
('disk_usage',     '磁盘使用率',     '%',    'gauge',   'float',  'any',    'disk',      1, 1, 20, '2026-09-21 14:00:00', '整体磁盘占用百分比'),
('disk_total',     '磁盘总容量',     'GB',   'gauge',   'float',  'any',    'disk',      1, 1, 21, '2026-09-21 14:00:00', '大屏容量饼图数据源'),
('disk_used',      '磁盘已用',       'GB',   'gauge',   'float',  'any',    'disk',      1, 1, 22, '2026-09-21 14:00:00', ''),
('disk_free',      '磁盘剩余',       'GB',   'gauge',   'float',  'any',    'disk',      1, 1, 23, '2026-09-21 14:00:00', ''),
('if_in_rate',     '端口入向速率',   'Mbps', 'gauge',   'float',  'snmp',   'interface', 1, 1, 30, '2026-09-21 14:00:00', '由 ifHCInOctets 差值计算'),
('if_out_rate',    '端口出向速率',   'Mbps', 'gauge',   'float',  'snmp',   'interface', 1, 1, 31, '2026-09-21 14:00:00', '由 ifHCOutOctets 差值计算'),
('if_in_octets',   '端口入向流量',   'Byte', 'counter', 'float',  'snmp',   'interface', 1, 1, 32, '2026-09-21 14:00:00', '累计值，用于算速率'),
('if_out_octets',  '端口出向流量',   'Byte', 'counter', 'float',  'snmp',   'interface', 1, 1, 33, '2026-09-21 14:00:00', '累计值，用于算速率'),
('if_status',      '端口状态',       '',     'state',   'string', 'snmp',   'interface', 1, 1, 34, '2026-09-21 14:00:00', 'up / down / testing'),
('if_bandwidth',   '端口带宽',       'Mbps', 'gauge',   'float',  'snmp',   'interface', 1, 1, 35, '2026-09-21 14:00:00', 'ifHighSpeed'),
('ping_latency',   'Ping平均延迟',   'ms',   'gauge',   'float',  'icmp',   'ping',      1, 1, 40, '2026-09-21 14:00:00', '长Ping平均往返时延'),
('ping_loss',      '丢包率',         '%',    'gauge',   'float',  'icmp',   'ping',      1, 1, 41, '2026-09-21 14:00:00', ''),
('ping_max',       'Ping最大延迟',   'ms',   'gauge',   'float',  'icmp',   'ping',      1, 1, 42, '2026-09-21 14:00:00', ''),
('ping_jitter',    'Ping抖动',       'ms',   'gauge',   'float',  'icmp',   'ping',      1, 1, 43, '2026-09-21 14:00:00', ''),
('online_status',  '在线状态',       '',     'state',   'string', 'icmp',   'ping',      1, 1, 50, '2026-09-21 14:00:00', 'online / offline，离线告警专用键'),
('device_uptime',  '运行时间',       '小时', 'gauge',   'float',  'any',    'system',    1, 1, 51, '2026-09-21 14:00:00', 'sysUpTime'),
('device_temp',    '设备温度',       '℃',   'gauge',   'float',  'snmp',   'system',    1, 1, 52, '2026-09-21 14:00:00', '部分交换机支持'),
('device_version', '设备版本',       '',     'state',   'string', 'ssh',    'system',    1, 1, 53, '2026-09-21 14:00:00', 'SSH 低频采集'),
('device_serial',  '设备序列号',     '',     'state',   'string', 'ssh',    'system',    1, 1, 54, '2026-09-21 14:00:00', 'SSH 低频采集'),
('process_count',  '进程数',         '个',   'gauge',   'int',    'any',    'system',    1, 1, 60, '2026-09-21 14:00:00', 'WinRM / Linux'),
('service_status', '关键服务状态',   '',     'state',   'string', 'winrm',  'system',    1, 1, 61, '2026-09-21 14:00:00', 'Windows 服务运行状态');

-- ----------------------------------------------------------------------------
-- 三、系统配置（sys_config）
--   is_secret=1 的项 API 返回时脱敏为 ***，前端只显示"已配置"
-- ----------------------------------------------------------------------------
DELETE FROM sys_config;
INSERT INTO sys_config (config_key, config_value, config_group, value_type, is_secret, description, updated_at) VALUES
-- 全局采集参数
('collect.thread_pool_size',   '60',              'collect', 'int',    0, '采集线程池并发数，200台设备建议 60',   '2026-09-21 14:00:00'),
('collect.default_interval',   '60',              'collect', 'int',    0, '默认采集周期（秒）',                   '2026-09-21 14:00:00'),
('collect.default_timeout',    '10',              'collect', 'int',    0, '单台设备采集超时（秒）',               '2026-09-21 14:00:00'),
('collect.ssh_slow_interval',  '600',             'collect', 'int',    0, 'SSH 低频采集周期（秒）',               '2026-09-21 14:00:00'),
('collect.offline_threshold',  '3',               'collect', 'int',    0, '连续失败多少次判定设备离线',           '2026-09-21 14:00:00'),
-- 指标保留策略
('metric.keep_raw_days',       '7',               'collect', 'int',    0, '原始采样保留天数',                     '2026-09-21 14:00:00'),
('metric.keep_5min_days',      '30',              'collect', 'int',    0, '5分钟粒度保留天数',                    '2026-09-21 14:00:00'),
('metric.keep_hour_days',      '90',              'collect', 'int',    0, '1小时粒度保留天数',                    '2026-09-21 14:00:00'),
-- 邮件告警
('mail.enabled',               '0',               'mail',    'bool',   0, '是否启用邮件告警',                     '2026-09-21 14:00:00'),
('mail.smtp_host',             'smtp.exmail.qq.com','mail',   'string', 0, 'SMTP 服务器地址',                     '2026-09-21 14:00:00'),
('mail.smtp_port',             '465',             'mail',    'int',    0, 'SMTP 端口（465 SSL / 25 明文）',      '2026-09-21 14:00:00'),
('mail.smtp_user',             '',                'mail',    'string', 0, 'SMTP 账号',                            '2026-09-21 14:00:00'),
('mail.smtp_password',         '',                'mail',    'string', 1, 'SMTP 密码/授权码',                     '2026-09-21 14:00:00'),
('mail.sender',                '',                'mail',    'string', 0, '发件人地址',                           '2026-09-21 14:00:00'),
('mail.use_ssl',               '1',               'mail',    'bool',   0, '是否启用 SSL',                         '2026-09-21 14:00:00'),
('mail.receivers',             '[]',              'mail',    'json',   0, '收件人列表 JSON 数组',                 '2026-09-21 14:00:00'),
-- 企业微信机器人
('wecom.enabled',              '0',               'wecom',   'bool',   0, '是否启用企业微信机器人',               '2026-09-21 14:00:00'),
('wecom.webhook',              '',                'wecom',   'string', 1, '企业微信群机器人 Webhook 地址',        '2026-09-21 14:00:00'),
('wecom.msg_type',             'markdown',        'wecom',   'string', 0, '消息类型 text / markdown',             '2026-09-21 14:00:00'),
-- 钉钉机器人
('dingtalk.enabled',           '0',               'dingtalk','bool',   0, '是否启用钉钉机器人',                   '2026-09-21 14:00:00'),
('dingtalk.webhook',           '',                'dingtalk','string', 1, '钉钉机器人 Webhook 地址',              '2026-09-21 14:00:00'),
('dingtalk.secret',            '',                'dingtalk','string', 1, '钉钉加签密钥（SEC 开头）',             '2026-09-21 14:00:00'),
-- 微信推送（企业号/服务号接口）
('wechat.enabled',             '0',               'wechat',  'bool',   0, '是否启用微信消息推送',                 '2026-09-21 14:00:00'),
('wechat.corp_id',             '',                'wechat',  'string', 1, '企业微信 CorpID',                      '2026-09-21 14:00:00'),
('wechat.corp_secret',         '',                'wechat',  'string', 1, '企业微信应用 Secret',                  '2026-09-21 14:00:00'),
('wechat.agent_id',            '',                'wechat',  'string', 0, '应用 AgentId',                         '2026-09-21 14:00:00'),
('wechat.to_user',             '@all',            'wechat',  'string', 0, '接收人，@all 表示全员',                '2026-09-21 14:00:00'),
-- 系统
('system.platform_name',       '亿网通IT运维监控管理平台', 'system','string',0, '平台名称（大屏标题）',             '2026-09-21 14:00:00'),
('system.dashboard_refresh',   '15',              'system',  'int',    0, '大屏自动刷新间隔（秒）',               '2026-09-21 14:00:00'),
('system.platform_url',        '',                'system',  'string', 0, '平台访问地址，用于告警消息里的“前往处理”链接（留空则不显示链接）', '2026-09-21 14:00:00'),
('system.company_name',        '广东亿网通科技有限公司',   'system','string',0, '公司名称（大屏页脚）',            '2026-09-21 14:00:00');

-- ----------------------------------------------------------------------------
-- 四、默认全局告警规则（device_id 为 NULL 表示对全部设备生效）
--   duration_n = 连续 N 次超标才触发（防抖动）
--   recovery_n = 连续 M 次正常才恢复（滞回）
-- ----------------------------------------------------------------------------
DELETE FROM alert_rule;
INSERT INTO alert_rule (name, device_id, metric_key, operator, threshold, duration_n, recovery_n, level, converge_window, converge_max, notify_channels, enabled, created_at, updated_at, remark) VALUES
('CPU使用率过高',   NULL, 'cpu_usage',     'gt', 85,  3, 3, 'major',    300, 3, '["email","wecom"]', 1, '2026-09-21 14:00:00', '2026-09-21 14:00:00', '连续3次采集超过85%触发'),
('内存使用率过高', NULL, 'mem_usage',     'gt', 90,  3, 3, 'major',    300, 3, '["email","wecom"]', 1, '2026-09-21 14:00:00', '2026-09-21 14:00:00', '连续3次采集超过90%触发'),
('磁盘使用率过高', NULL, 'disk_usage',    'gt', 90,  2, 2, 'critical', 600, 2, '["email","wecom"]', 1, '2026-09-21 14:00:00', '2026-09-21 14:00:00', '连续2次采集超过90%触发，紧急'),
('Ping延迟过大',   NULL, 'ping_latency',  'gt', 100, 3, 3, 'minor',    300, 3, '["wecom"]',          1, '2026-09-21 14:00:00', '2026-09-21 14:00:00', '连续3次平均延迟超100ms'),
('网络丢包率过高', NULL, 'ping_loss',     'gt', 5,   2, 3, 'major',    300, 3, '["email","wecom"]', 1, '2026-09-21 14:00:00', '2026-09-21 14:00:00', '连续2次丢包率超5%'),
('设备离线',       NULL, 'online_status', 'eq', 0,   1, 1, 'critical', 60,  1, '["email","wecom","dingtalk"]', 1, '2026-09-21 14:00:00', '2026-09-21 14:00:00', '离线立即告警，收敛窗口60秒');

-- ============================================================================
-- 初始化完成。
-- 提示：生产环境请务必修改默认账号密码，并配置 mail/wecom/dingtalk/wechat 密钥。
-- ============================================================================
