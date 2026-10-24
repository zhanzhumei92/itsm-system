"""
第3步端到端验证脚本（回归测试用）

运行方式（项目根目录下）：
    python scripts/verify_api.py

状态码断言严格按本项目约定：
  业务规则不通过 → HTTP 200 + 业务码 1001/1004/1005
  参数错误 → 400 | 不存在 → 404 | 无权限 → 403 | 未登录 → 401

设计说明：
- 用 FastAPI TestClient 进程内直连，不依赖真实监听的端口，CI 里可直接跑。
- 所有用例自带「建→查→改→删」闭环，跑完自动清理，可反复执行不污染数据。
- **用例必须自给自足，不依赖残留的演示数据**：告警段会自备一条「告警中」事件。
  这条约束来自一次真实教训——「确认告警」会把 firing 改成 acked（产品设计上不可逆），
  于是同一套用例第二次运行时 firing 列表为空，整段 6 项被静默跳过，
  通过项从 95 悄悄降到 90 而失败数仍是 0。覆盖度随运行次数衰减的回归套件
  比没有套件更危险，它会让人误以为接口一直是好的。
- 末尾的「脏数据容错回归」是本步核心修复点的守护用例：手工把库里的枚举字段
  改成字典外的值，再访问列表接口，必须仍然 200（否则说明输出侧又混进了入参校验器）。
"""
import os
import sys
import warnings
import sqlite3

# 允许从任意工作目录以 `python scripts/verify_api.py` 方式执行：
# 把项目根目录加入模块搜索路径，否则 import app.* 会失败（脚本所在目录会被自动加入 path）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

warnings.filterwarnings("ignore")
from fastapi.testclient import TestClient
from app.main import create_app

client = TestClient(create_app())
R = {"ok": 0, "fail": 0}
FAILS = []

def purge_previous_run() -> int:
    """
    清理上一次运行留下的数据，让本脚本**可反复执行**。

    ⚠ **为什么必须有这个**
    脚本原本只在末尾清理，假设自己每次都能跑完。但现实是：网络抖动、手滑 Ctrl-C、
    某个断言抛异常……只要中途断一次，测试数据就会留在库里。下一步再跑，
    `新增用户` 会因为账号已存在而失败（code=1001），整个套件卡在第一вался一步。

    更糟的是这种失败会误导排查方向：看起来像"权限/校验逻辑回归了"，
    实际上只是上一轮的垃圾没清。**让脚本自己先把场地打扫干净**，
    比让每个人记住"先手工执行一段 SQL"可靠得多。

    清理范围与末尾的清理保持一致：只动测试专用前缀的数据，绝不碰演示数据。
    """
    from app.core.config import settings
    path = getattr(settings, "DATABASE_URL", "sqlite:///./data/itsm.db")
    if not path.startswith("sqlite"):
        return 0  # 非 SQLite 环境不自动清理，避免误伤现场库
    db_file = path.split("sqlite:///")[-1]
    if not os.path.exists(db_file):
        return 0

    conn = sqlite3.connect(db_file)
    total = 0
    try:
        # 顺序：先删依赖它的行，再删主表行
        dev_ids = [r[0] for r in conn.execute(
            "SELECT id FROM mon_device WHERE name LIKE '测试-%' OR name LIKE 'API测试-%'")]
        asset_ids = [r[0] for r in conn.execute(
            "SELECT id FROM asset_info WHERE asset_code LIKE 'TEST-%'")]
        user_ids = [r[0] for r in conn.execute(
            "SELECT id FROM sys_user WHERE username LIKE 'ops_t%' OR username LIKE 'test_%'")]
        rule_ids = [r[0] for r in conn.execute(
            "SELECT id FROM alert_rule WHERE name LIKE '%测试%'")]

        for tbl, col, ids in (("alarm_log", "event_id", None),
                              ("alert_notify_record", "event_id", None)):
            if dev_ids and col == "event_id":
                ids = [r[0] for r in conn.execute(
                    f"SELECT id FROM alert_event WHERE device_id IN ({','.join('?'*len(dev_ids))})", dev_ids)]
            if ids:
                total += conn.execute(
                    f"DELETE FROM {tbl} WHERE {col} IN ({','.join('?'*len(ids))})", ids).rowcount

        if rule_ids:
            total += conn.execute(
                f"DELETE FROM alert_rule_state WHERE rule_id IN ({','.join('?'*len(rule_ids))})", rule_ids).rowcount
        if dev_ids:
            q = ','.join('?' * len(dev_ids))
            total += conn.execute(f"DELETE FROM alert_event WHERE device_id IN ({q})", dev_ids).rowcount
            total += conn.execute(f"DELETE FROM alert_rule_state WHERE device_id IN ({q})", dev_ids).rowcount
            total += conn.execute(f"DELETE FROM alarm_log WHERE device_id IN ({q})", dev_ids).rowcount
            total += conn.execute(f"DELETE FROM collect_log WHERE device_id IN ({q})", dev_ids).rowcount
            total += conn.execute(f"DELETE FROM metric_latest WHERE device_id IN ({q})", dev_ids).rowcount
            total += conn.execute(f"DELETE FROM metric_sample WHERE device_id IN ({q})", dev_ids).rowcount
            total += conn.execute(f"DELETE FROM mon_device_credential WHERE device_id IN ({q})", dev_ids).rowcount
        if rule_ids:
            total += conn.execute(
                f"DELETE FROM alert_rule WHERE id IN ({','.join('?'*len(rule_ids))})", rule_ids).rowcount
        if asset_ids:
            total += conn.execute(
                f"DELETE FROM asset_info WHERE id IN ({','.join('?'*len(asset_ids))})", asset_ids).rowcount
        if dev_ids:
            total += conn.execute(
                f"DELETE FROM mon_device WHERE id IN ({','.join('?'*len(dev_ids))})", dev_ids).rowcount
        if user_ids:
            total += conn.execute(
                f"DELETE FROM sys_user WHERE id IN ({','.join('?'*len(user_ids))})", user_ids).rowcount
        conn.commit()
    finally:
        conn.close()
    return total

_purged = purge_previous_run()
if _purged:
    print(f"[清理] 检测到上一次运行的残留数据，已清理 {_purged} 行"
          f"（保证本脚本可反复执行，不受中断影响）\n")

def step(name, resp, http=200, code=0):
    ct = resp.headers.get("content-type", "")
    if not ct.startswith("application/json"):
        # 二进制响应（Excel 导出/模板下载）：只校验 HTTP 状态码 + 内容类型
        good = resp.status_code == http and len(resp.content) > 0
        R["ok" if good else "fail"] += 1
        if not good:
            FAILS.append((name, resp.status_code, None, "二进制响应异常"))
        print(f"  [{'OK ' if good else 'FAIL'}] {name:26s} HTTP {resp.status_code} 二进制 {len(resp.content)} 字节")
        return None
    body = resp.json()
    got = body.get("code")
    good = resp.status_code == http and got == code
    R["ok" if good else "fail"] += 1
    if not good:
        FAILS.append((name, resp.status_code, got, resp.text[:170]))
    print(f"  [{'OK ' if good else 'FAIL'}] {name:26s} HTTP {resp.status_code}" + (f" code={got}" if isinstance(got, int) else ""))
    return body.get("data")

r = client.post("/api/v1/auth/login", json={"username":"admin","password":"admin123"})
H = {"Authorization": f"Bearer {r.json()['data']['access_token']}"}

print("【认证】")
step("登录成功", r)
step("密码错误→401", client.post("/api/v1/auth/login", json={"username":"admin","password":"x"}), 401, 401)
step("无令牌→401", client.get("/api/v1/devices"), 401, 401)

print("\n【用户管理】")
u = step("新增用户", client.post("/api/v1/users", headers=H, json={"username":"ops_t","password":"Ops@Test2026","real_name":"测试","role":"viewer"}))
uid = u["id"]
step("账号重复→200/1001", client.post("/api/v1/users", headers=H, json={"username":"ops_t","password":"Ops@Test2026","real_name":"重复","role":"viewer"}), 200, 1001)
step("密码过短→400", client.post("/api/v1/users", headers=H, json={"username":"ops_t2","password":"123","real_name":"短","role":"viewer"}), 400, 400)
step("角色非法→400", client.post("/api/v1/users", headers=H, json={"username":"ops_t3","password":"Ops@Test2026","real_name":"x","role":"superman"}), 400, 400)
step("改admin角色→200/1004", client.put("/api/v1/users/1", headers=H, json={"role":"viewer"}), 200, 1004)
step("禁用admin→200/1004", client.put("/api/v1/users/1", headers=H, json={"status":False}), 200, 1004)
step("删admin→200/1004", client.delete("/api/v1/users/1", headers=H), 200, 1004)
step("用户不存在→404", client.get("/api/v1/users/99999", headers=H), 404, 404)
step("改用户", client.put(f"/api/v1/users/{uid}", headers=H, json={"real_name":"测试改"}))
step("重置密码", client.post(f"/api/v1/users/{uid}/reset-password", headers=H, json={"new_password":"NewPwd@2026","must_change_pwd":True}))
step("角色字典", client.get("/api/v1/users/roles", headers=H))

print("\n【设备管理】")
d = step("新增设备", client.post("/api/v1/devices", headers=H, json={
    "name":"测试-核心交换机","ip":"10.99.99.1","device_type":"switch","vendor":"huawei",
    "protocol":"snmp","location":"测试机房","collect_interval":60,"enabled":True,
    "credential":{"protocol":"snmp","snmp_version":"v2c","snmp_community":"public"}}))
did = d["id"]
print(f"          -> id={did} {d['name']} {d['protocol_text']}/{d['device_type_text']} 状态={d['online_text']}")
step("IP重复→200/1001", client.post("/api/v1/devices", headers=H, json={
    "name":"重复IP","ip":"10.99.99.1","device_type":"switch","protocol":"snmp","credential":{"protocol":"snmp","snmp_community":"x"}}), 200, 1001)
step("协议不一致→400", client.post("/api/v1/devices", headers=H, json={
    "name":"协议冲突","ip":"10.99.99.9","device_type":"switch","protocol":"snmp",
    "credential":{"protocol":"ssh","ssh_username":"a","ssh_password":"b"}}), 400, 400)
step("SNMP缺团体字→400", client.post("/api/v1/devices", headers=H, json={
    "name":"缺凭据","ip":"10.99.99.8","device_type":"switch","protocol":"snmp","credential":{"protocol":"snmp","snmp_version":"v2c"}}), 400, 400)
step("IP非法→400", client.post("/api/v1/devices", headers=H, json={
    "name":"坏IP","ip":"999.1.1.1","device_type":"switch","protocol":"icmp","credential":{"protocol":"icmp"}}), 400, 400)
step("设备类型非法→400", client.post("/api/v1/devices", headers=H, json={
    "name":"坏类型","ip":"10.99.99.7","device_type":"乱写","protocol":"icmp","credential":{"protocol":"icmp"}}), 400, 400)
step("设备不存在→404", client.get("/api/v1/devices/99999", headers=H), 404, 404)
det = step("设备详情(脱敏)", client.get(f"/api/v1/devices/{did}", headers=H))
print(f"          -> 凭据 has_community={det['credential']['has_snmp_community']} 明文泄露={'snmp_community' in det['credential']}")
step("改设备(凭据留空)", client.put(f"/api/v1/devices/{did}", headers=H, json={"name":"测试-核心交换机V2","credential":{"protocol":"snmp","snmp_version":"v2c"}}))
det2 = step("改后凭据仍在", client.get(f"/api/v1/devices/{did}", headers=H))
print(f"          -> has_community={det2['credential']['has_snmp_community']} 名称={det2['name']}（留空未覆盖）")
step("改设备(只改名)", client.put(f"/api/v1/devices/{did}", headers=H, json={"name":"测试-核心交换机V3"}))
step("改设备类型非法→400", client.put(f"/api/v1/devices/{did}", headers=H, json={"device_type":"乱写"}), 400, 400)
step("批量禁用", client.post("/api/v1/devices/batch-toggle?enabled=false", headers=H, json={"ids":[did]}))
step("协议字典", client.get("/api/v1/devices/protocols", headers=H))
devs = step("设备列表", client.get("/api/v1/devices", headers=H))
print(f"          -> total={devs['total']} 首条={devs['items'][0]['name']}({devs['items'][0]['device_type_text']})")
step("设备精简列表", client.get("/api/v1/devices/simple", headers=H))
step("采集日志", client.get(f"/api/v1/devices/{did}/collect-logs", headers=H))
step("采集统计", client.get(f"/api/v1/devices/{did}/collect-stat", headers=H))

print("\n【资产管理】")
a = step("新增资产", client.post("/api/v1/assets", headers=H, json={
    "asset_code":"TEST-001","name":"测试资产-服务器","device_type":"server","vendor":"华为",
    "model":"2288H V5","serial_no":"SN-T-001","ip":"10.99.99.1",
    "room_location":"测试机房","status":"in_use","owner":"测试责任人","device_id":did}))
aid = a["id"]
print(f"          -> id={aid} {a['name']} 状态={a['status_text']} 已绑定={a['is_monitored']}")
step("编号重复→200/1001", client.post("/api/v1/assets", headers=H, json={"asset_code":"TEST-001","name":"重复"}), 200, 1001)
step("状态非法→400", client.post("/api/v1/assets", headers=H, json={"asset_code":"TEST-002","name":"坏状态","status":"在用"}), 400, 400)
al = step("列表带在线状态", client.get("/api/v1/assets?keyword=TEST-001", headers=H))
if al["items"]:
    print(f"          -> online_status={al['items'][0].get('device_online_status')}（资产↔监控已打通）")
step("资产不存在→404", client.get("/api/v1/assets/99999", headers=H), 404, 404)
step("绑不存在设备→404", client.post(f"/api/v1/assets/{aid}/bind", headers=H, json={"device_id":99999}), 404, 404)
step("改资产", client.put(f"/api/v1/assets/{aid}", headers=H, json={"name":"测试资产V2","status":"repair"}))
step("改状态非法→400", client.put(f"/api/v1/assets/{aid}", headers=H, json={"status":"在用"}), 400, 400)
step("导出Excel", client.get("/api/v1/assets/export", headers=H))
step("模板下载", client.get("/api/v1/assets/template", headers=H))
step("维保提醒", client.get("/api/v1/assets/maintain-remind", headers=H))
step("导入非xlsx→400", client.post("/api/v1/assets/import", headers=H, files={"file":("t.txt",b"hi","text/plain")}), 400, 1006)
step("导入坏xlsx→400", client.post("/api/v1/assets/import", headers=H, files={"file":("t.xlsx",b"bad","application/vnd.ms-excel")}), 400, 1006)

print("\n【告警】")
rl = step("新增规则", client.post("/api/v1/alerts/rules", headers=H, json={
    "name":"测试-交换机CPU","device_type":"switch","metric_key":"cpu_usage","operator":"gt",
    "threshold":88,"level":"critical","duration_n":3,"recovery_n":2,
    "notify_channels":["email","wecom"],"enabled":True}))
rid = rl["id"]
print(f"          -> id={rid} {rl['name']} 条件={rl['condition_text']} 渠道={rl['notify_channels']}")
step("比较符非法→400", client.post("/api/v1/alerts/rules", headers=H, json={
    "name":"坏规则","metric_key":"cpu_usage","operator":">","threshold":80}), 400, 400)
step("级别非法→400", client.post("/api/v1/alerts/rules", headers=H, json={
    "name":"坏级别","metric_key":"cpu_usage","operator":"gt","threshold":80,"level":"urgent"}), 400, 400)
step("渠道非法→400", client.post("/api/v1/alerts/rules", headers=H, json={
    "name":"坏渠道","metric_key":"cpu_usage","operator":"gt","threshold":80,"notify_channels":["sms"]}), 400, 400)
step("引用不存在设备→404", client.post("/api/v1/alerts/rules", headers=H, json={
    "name":"坏设备","device_id":99999,"metric_key":"cpu_usage","operator":"gt","threshold":80}), 404, 404)
step("改阈值", client.put(f"/api/v1/alerts/rules/{rid}", headers=H, json={"threshold":90}))
step("改级别", client.put(f"/api/v1/alerts/rules/{rid}", headers=H, json={"level":"major"}))
step("改渠道非法→400", client.put(f"/api/v1/alerts/rules/{rid}", headers=H, json={"notify_channels":["sms"]}), 400, 400)
step("规则启停", client.post(f"/api/v1/alerts/rules/{rid}/toggle?enabled=false", headers=H))
step("规则静默", client.post(f"/api/v1/alerts/rules/{rid}/silence?minutes=30", headers=H))
step("规则不存在→404", client.get("/api/v1/alerts/rules/99999", headers=H), 404, 404)
step("告警概览", client.get("/api/v1/alerts/overview", headers=H))
step("告警列表", client.get("/api/v1/alerts/events", headers=H))

# ---------------------------------------------------------------------------
# 自备一条「告警中」事件，让本套件不依赖残留的演示数据
#
# 为什么必须这样做（这是本脚本自身的一个覆盖度缺陷）：
#   下面第 3 步「确认告警」会把 firing 事件改成 acked，而 ack 是不可逆的
#   （产品设计如此）。于是**第二次运行**时 event 列表里再也没有 firing，
#   这一整段 6 项用例会被静默跳过 —— 通过项从 95 悄悄降到 90，
#   而失败数始终是 0。一个"越跑越松"的回归套件比没有套件更危险：
#   它会让人误以为接口一直是好的。
#
# 为什么走 service 层而不是 API：产品里**没有**"创建告警中事件"的接口
#   （告警只能由评估器产生，这是刻意的约束）。所以只能由测试夹具直接落库，
#   这与工装夹具的性质一致；后续真正走"阈值→评估→触发"的链路
#   由 tests/test_alarm.py 覆盖。
#
# 用当前新建的测试规则 rid + 测试设备 did，指纹天然唯一，
# 不会与演示数据里的事件串味；清理阶段会连同 alarm_log 一起删除。
# ---------------------------------------------------------------------------
from app.core.database import session_scope as _scope          # noqa: E402
from app.models.alert import AlertRule as _AlertRule           # noqa: E402
from app.models.device import MonDevice as _MonDevice          # noqa: E402
from app.services import alert_service                         # noqa: E402

with _scope() as db:
    _seeded = alert_service.create_event(
        db, db.get(_AlertRule, rid), db.get(_MonDevice, did),
        "cpu_usage", 93.5, metric_name="CPU使用率",
    )
    db.commit()
    seeded_event_id = _seeded.id
print(f"          -> 已自备一条「告警中」事件 id={seeded_event_id}（保证本段用例恒定执行）")

firing = [i for i in step("告警列表(含自备事件)",
                          client.get("/api/v1/alerts/events", headers=H))["items"]
          if i["status"] == "firing"]
assert firing, "自备告警事件后仍无 firing 事件，请检查 alert_service.create_event"
eid = firing[0]["id"]

step("告警详情", client.get(f"/api/v1/alerts/events/{eid}", headers=H))
step("确认告警", client.post(f"/api/v1/alerts/events/{eid}/ack", headers=H, json={"remark":"已知晓"}))
# 重复确认「已确认」的事件是幂等的（值班交接时用来补/改备注），返回成功而非报错
step("重复确认(幂等)", client.post(f"/api/v1/alerts/events/{eid}/ack", headers=H, json={"remark":"再确认"}))
step("告警时间线", client.get(f"/api/v1/alerts/events/{eid}/timeline", headers=H))
step("推送记录", client.get(f"/api/v1/alerts/events/{eid}/notify-records", headers=H))

# 1005「当前状态不允许该操作」分支：先用 close=true 把事件关闭（确认误报），
# 再对已关闭事件发确认请求，必须被拒。
# 全程走 HTTP 接口，不借助 service 层改状态，才真正验证到接口层的状态校验。
step("关闭告警(误报)", client.post(f"/api/v1/alerts/events/{eid}/ack", headers=H,
                                    json={"remark":"确认误报","close":True}))
step("已关闭告警再确认→1005",
     client.post(f"/api/v1/alerts/events/{eid}/ack", headers=H, json={"remark":"x"}), 200, 1005)
step("告警不存在→404", client.get("/api/v1/alerts/events/99999", headers=H), 404, 404)
step("大屏最新告警", client.get("/api/v1/alerts/latest?limit=5", headers=H))
step("批量确认", client.post("/api/v1/alerts/events/batch-ack", headers=H, json={"event_ids":[]}))

print("\n【指标】")
cat = step("指标字典", client.get("/api/v1/metrics/catalog", headers=H))
print(f"          -> {len(cat)} 项")
step("最新值", client.get(f"/api/v1/metrics/latest?device_ids={did}", headers=H))
# ⚠ 窗口要覆盖到演示数据的采集时间：脚本原先写死 hours=2，
#   而该接口当时**根本没有声明 hours 参数**（FastAPI 静默忽略），
#   实际一直按默认 6 小时窗口跑 —— 数据恰好落在窗口内所以多轮全绿，
#   等演示数据过期才暴露。现在接口已支持 hours（并由静态检查 8 守住这类误用）。
h = step("历史曲线(降采样)", client.get("/api/v1/devices/2/history?metric_key=cpu_usage&hours=48", headers=H))
_first = f"，首点 {h['points'][0]['time']}" if h.get("points") else "（该窗口内无数据点）"
print(f"          -> {h['device_name']}/{h['metric_name']} 共 {h['count']} 点{_first}")
step("时间倒置→400", client.get("/api/v1/metrics/history?device_id=2&metric_key=cpu_usage&start=2026-09-21T12:00:00&end=2026-09-21T10:00:00", headers=H), 400, 400)
step("多设备对比", client.get("/api/v1/metrics/history/multi?device_id=2&metric_keys=cpu_usage&metric_keys=mem_usage&limit=20", headers=H))
step("删内置指标→200/1004", client.delete("/api/v1/metrics/catalog/1", headers=H), 200, 1004)

print("\n【配置/日志/系统】")
step("配置列表", client.get("/api/v1/configs", headers=H))
step("按组配置", client.get("/api/v1/configs/smtp", headers=H))
step("调度器状态", client.get("/api/v1/configs/scheduler/status", headers=H))
step("系统日志", client.get("/api/v1/logs/system", headers=H))
step("采集日志", client.get("/api/v1/logs/collect", headers=H))
step("采集成功率", client.get("/api/v1/logs/collect-stat", headers=H))
step("告警日志", client.get("/api/v1/logs/alarm", headers=H))
step("系统信息", client.get("/api/v1/system/info", headers=H))
step("健康检查", client.get("/api/v1/system/health", headers=H))
step("运行时状态", client.get("/api/v1/system/runtime", headers=H))

print("\n【大屏（第6步）】")
# 大屏只读聚合接口：字段完整性比"有数据"更重要 —— 空库部署时这些接口必须
# 依然 200 且结构完整（前端零业务常量约定依赖后端永远给全字段）。
ov = step("大屏总览", client.get("/api/v1/dashboard/overview", headers=H))
for k in ("kpis", "device_status", "data_fresh_at", "refresh_seconds"):
    assert k in ov, f"大屏总览缺字段 {k}"
step("大屏设备矩阵", client.get("/api/v1/dashboard/devices", headers=H))
tr = step("大屏告警趋势", client.get("/api/v1/dashboard/alert-trend?hours=24", headers=H))
assert isinstance(tr.get("points"), list), "大屏趋势缺 points 数组"
step("大屏排行", client.get("/api/v1/dashboard/rank?metric_key=cpu_usage&limit=10", headers=H))
step("大屏排行(端口指标)", client.get("/api/v1/dashboard/rank?metric_key=if_in_rate&limit=10", headers=H))
step("大屏分布", client.get("/api/v1/dashboard/distribution", headers=H))
bdl = step("大屏整屏打包", client.get("/api/v1/dashboard/bundle", headers=H))
# rank 不在打包里：排行榜跟随用户在下拉框选择的指标，30 秒增量刷新单独请求
for k in ("overview", "devices", "trend", "distribution"):
    assert k in bdl, f"整屏打包缺板块 {k}"
step("趋势窗口越界→400", client.get("/api/v1/dashboard/alert-trend?hours=999", headers=H), 400, 400)
step("未登录看大屏→401", client.get("/api/v1/dashboard/overview"), 401, 401)

print("\n【权限】")
rv = client.post("/api/v1/auth/login", json={"username":"viewer","password":"viewer123"})
VH = {"Authorization": f"Bearer {rv.json()['data']['access_token']}"}
step("viewer查设备", client.get("/api/v1/devices", headers=VH))
step("viewer建设备→403", client.post("/api/v1/devices", headers=VH, json={
    "name":"x","ip":"10.0.0.1","device_type":"switch","protocol":"icmp","credential":{"protocol":"icmp"}}), 403, 403)
step("viewer删用户→403", client.delete("/api/v1/users/2", headers=VH), 403, 403)
step("viewer改配置→403", client.put("/api/v1/configs", headers=VH, json={"items":[]}), 403, 403)

print("\n【★ 脏数据容错回归测试（本次核心修复点）】")
print("  模拟历史/手工写入的枚举外数据，验证列表接口不再整体 500")
import app.core.config as cfg
db_path = cfg.get_settings().DATABASE_URL.replace("sqlite:///", "")
con = sqlite3.connect(db_path)
# 目标行动态选取：验证脚本不能再硬编码 id=1——真实环境里用户随时可能删掉
# 最早期的设备/资产/规则（本仓库就真实发生过），ID 是运行数据不是常量。
def _pick(table):
    row = con.execute(f"SELECT id FROM {table} ORDER BY id LIMIT 1").fetchone()
    return row[0] if row else None
dev_id  = _pick("mon_device")
asset_id = _pick("asset_info")
rule_id  = _pick("alert_rule")
if dev_id is None or asset_id is None or rule_id is None:
    print("  跳过：设备/资产/规则存在空表，无法注入脏数据（先跑 seed_demo.py 再验证）")
else:
    con.execute(f"UPDATE mon_device SET device_type='legacy_type', protocol='snmp_v1' WHERE id={dev_id}")
    con.execute(f"UPDATE asset_info SET status='archived' WHERE id={asset_id}")
    con.execute(f"UPDATE alert_rule SET operator='gte', level='severe' WHERE id={rule_id}")
    con.commit(); con.close()
    step("设备列表(含脏数据)", client.get("/api/v1/devices", headers=H))
    step("资产列表(含脏数据)", client.get("/api/v1/assets", headers=H))
    step("规则列表(含脏数据)", client.get("/api/v1/alerts/rules", headers=H))
    bad = [i for i in client.get("/api/v1/devices", headers=H).json()["data"]["items"] if i["id"] == dev_id][0]
    print(f"          -> device_type='{bad['device_type']}' 透出为 '{bad['device_type_text']}'（不崩、原样透出）")
    con = sqlite3.connect(db_path)
    con.execute(f"UPDATE mon_device SET device_type='switch', protocol='snmp' WHERE id={dev_id}")
    con.execute(f"UPDATE asset_info SET status='in_use' WHERE id={asset_id}")
    con.execute(f"UPDATE alert_rule SET operator='gt', level='major' WHERE id={rule_id}")
    con.commit(); con.close()
    print("  已还原")

print("\n【清理】")
step("删资产", client.delete(f"/api/v1/assets/{aid}", headers=H))
step("删规则", client.delete(f"/api/v1/alerts/rules/{rid}", headers=H))
step("删设备", client.delete(f"/api/v1/devices/{did}", headers=H))
step("删用户", client.delete(f"/api/v1/users/{uid}", headers=H))

# 清掉自备的告警事件与它的处理时间线。
# 注意：delete_rule 只清理 alert_rule_state，**刻意保留告警事件**（审计需要），
# 所以这里必须显式删除，否则每跑一次就会多留一条 acked 事件，套件就不再"可反复执行"。
# db_path 复用上面「脏数据容错回归」一节已经解析好的路径。
_conn = sqlite3.connect(db_path)
_conn.execute("DELETE FROM alarm_log WHERE event_id=?", (seeded_event_id,))
_deleted = _conn.execute("DELETE FROM alert_event WHERE id=?", (seeded_event_id,)).rowcount
_conn.commit()
_conn.close()
print(f"          -> 已清理自备告警事件 id={seeded_event_id}（alarm_log 一并删除，rowcount={_deleted}）")
step("自备告警已清理→404", client.get(f"/api/v1/alerts/events/{seeded_event_id}", headers=H), 404, 404)

last = step("删后恢复原始台数", client.get("/api/v1/devices", headers=H))
print(f"          -> total={last['total']}")

print("\n" + "=" * 68)
print(f"通过 {R['ok']} 项，失败 {R['fail']} 项")
if FAILS:
    print("\n失败明细：")
    for n, s, c, t in FAILS:
        print(f"  - {n}: HTTP {s} code={c}\n    {t}")
