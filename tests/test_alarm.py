"""
告警推送模块的单元测试（第5步：规则评估 + 收敛抑制 + 四渠道推送）
===============================================================================
**为什么要专门给告警写测试（而不是"手工改库跑一遍看看"）**

告警系统是本平台里**唯一会主动对外发消息**的模块，评测标准不是"能不能跑"，
而是下面三条容易被写错、且写错后**从现象上极难定位**的性质：

  1. **同一次故障只报一次** —— 连续计数不清零会导致每轮采集都"触发"一次，
     事件表 occur_count 疯涨，值班群里同一条告警被反复刷；
  2. **取不到指标值时状态机必须停在原地** —— 设备离线时 `ping_latency`
     自然取不到值，若把"取不到"当成"未超标"，就会在设备离线的那一刻
     把延迟告警**误恢复**，值班人员看到"设备离线了、延迟告警恢复了"，
     从此不再相信平台上的任何数字；
  3. **配置没配好不能占用收敛限额** —— 失败的推送若计入限额，
     邮件没配好会把企业微信的额度吃光，表现为"配好了也收不到"。

这三条的共同点是：**出问题时平台不会报错**，只会安静地做错事。
所以必须有专门的用例把它们钉住。

**本文件的安全约束（重要，不要删）**

所有用例都跑在**内存 SQLite** 上，并由 `fake_notify_pool` 这个 autouse 夹具
把 notifier 的线程池换成假池。不加这层替换的话，`dispatch()` 会真的起
8 个线程，线程里 `session_scope()` 打开的是**生产库 data/itsm.db**，
并真的往企业微信/钉钉发出 HTTP 请求 —— 跑一次单测就给客户群发一堆告警。

**测试分层**

| 层次         | 被测对象                                        | 是否触网 |
|--------------|-------------------------------------------------|----------|
| 单位表       | models/alert.metric_unit + 静态检查 6            | 否       |
| 状态机       | evaluator._evaluate_one / evaluate_devices       | 否       |
| 规则筛选     | evaluator._rules_for                             | 否       |
| 取值         | evaluator._resolve_value                         | 否       |
| 收敛         | converger.decide / count_recent_notified_events  | 否       |
| 分发         | notifier.dispatch / _send_with_retry             | 否（假池）|
| 投递         | notifier._deliver（注入内存会话 + 假渠道）        | 否       |
| 消息排版     | AlertMessage.as_text / as_markdown / subject     | 否       |
| 渠道助手     | _mask_url / _truncate                            | 否       |
| 渠道配置检查 | 四渠道 check()                                   | 否       |
| 渠道响应解读 | 四渠道 _interpret()                              | 否       |
| 加签算法     | DingtalkChannel._signed_url                      | 否       |
| 端到端       | 指标 → 评估 → 触发 → 分发 → 投递 → 回写           | 否（假渠道）|

**没有放进单测的部分**：真实的 SMTP/企业微信/钉钉发送。它需要真实的
授权码与机器人 Webhook，且会给客户群发消息。第5步的真机验证用
"注入假渠道 + 真实链路"的方式覆盖（见 TestEndToEnd），
真实渠道的连通性由第8步的部署验收清单手工确认。

运行：pytest tests/test_alarm.py -v
===============================================================================
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import smtplib
import time
import urllib.parse
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta
from email.header import decode_header
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session, sessionmaker

# 说明：四个渠道模块的注册发生在 app.alarm.channels 包的 __init__ 里
# （末尾有一段容错 __import__ 循环），所以下面那行 channels 导入才是注册入口。
# 早先这里还写了一句 `import app.alarm`，注释说"导入即触发注册" —— 其实
# app/alarm/__init__.py 只有一行 docstring，什么都不做，纯属误导。
from app.alarm import converger, evaluator, notifier
from app.alarm.channels import (
    MAX_MESSAGE_BYTES,
    AlertMessage,
    BaseChannel,
    ChannelRegistry,
    ChannelResult,
)
from app.alarm.channels.dingtalk import DingtalkChannel
from app.alarm.channels.email import EmailChannel
from app.alarm.channels.wechat import WechatChannel, reset_token_cache
from app.alarm.channels.wecom import WecomChannel
from app.core.config import settings
from app.models import Base
from app.models.alert import (
    EVENT_STATUS_ACKED,
    EVENT_STATUS_FIRING,
    EVENT_STATUS_RECOVERED,
    METRIC_UNITS,
    AlertEvent,
    AlertNotifyRecord,
    AlertRule,
    metric_unit,
)
from app.models.device import MonDevice
from app.models.log import (
    ALARM_ACTION_CONVERGE,
    ALARM_ACTION_NOTIFY,
    ALARM_ACTION_SUPPRESS,
    ALARM_ACTION_TRIGGER,
    AlarmLog,
)
from app.models.metric import MetricCatalog, MetricLatest
from app.schemas.alert import AlertAckRequest
from app.services import alert_service, config_service, metric_service
from app.services.alert_service import (
    NOTIFY_FAIL,
    NOTIFY_PENDING,
    NOTIFY_SUCCESS,
    RULE_STATE_ACKED,
)
from app.tasks.executor import get_executor

# 导入告警渠道模块本体（用于 monkeypatch 其内部引用）
from app.alarm.channels import dingtalk as dingtalk_mod
from app.alarm.channels import wechat as wechat_mod


# ============================================================================
# 夹具
# ============================================================================

@pytest.fixture()
def mem_db():
    """
    内存 SQLite 会话（每个用例一个全新库）。

    与 tests/test_collect_job.py 同一套做法：不碰生产库、零残留、
    失败也不会给现场留下一库被改坏的配置。
    """
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


class FakePool:
    """
    假通知线程池：只记录投递，不真正执行。

    为什么不"投递后真跑一遍"：`_deliver()` 内部用 `session_scope()`
    取的是**生产库**会话（那是给真实运行时准备的），在单测里让它跑起来
    就会去改现场数据。要验证投递行为时，用例显式调用
    `_deliver()` 并配合 patched_scope 夹具，把会话换到内存库上。
    """

    def __init__(self) -> None:
        self.submissions: List[Tuple[Any, ...]] = []

    def submit(self, fn, *args: Any, **kwargs: Any) -> Future:
        self.submissions.append((fn,) + args)
        future: Future = Future()
        future.set_result(None)
        return future

    def shutdown(self, wait: bool = False) -> None:
        pass

    # 便于用例断言"投递了几条、投给了哪条记录"
    @property
    def record_ids(self) -> List[int]:
        return [s[1] for s in self.submissions]


@pytest.fixture(autouse=True)
def fake_notify_pool(monkeypatch):
    """
    ★ 安全网（autouse）：把 notifier 的通知线程池换成假池。

    做成 autouse 而不是"需要时再 patch"：漏 patch 的后果是
    **在跑单测的过程中给客户的运维群发真实告警**，
    这种错误不值得靠"记得加夹具"来避免。

    夹具同时交出真实实现的引用，供 Test线程池隔离 验证
    "通知池与采集池是两个独立的池"这条设计约束。
    """
    pool = FakePool()
    real_get_pool = notifier._get_pool
    monkeypatch.setattr(notifier, "_get_pool", lambda: pool)
    return SimpleNamespace(pool=pool, real_get_pool=real_get_pool)


@pytest.fixture(autouse=True)
def _clean_caches():
    """
    清掉进程级缓存，避免用例之间串味。

    两个缓存都必须清：
      - config_service：30 秒 TTL 的值缓存，上一个用例写的配置会漏给下一个；
      - wechat 的 access_token 缓存，不清会让"缓存复用"的用例被上一个用例的结果干扰。
    """
    config_service.invalidate_cache()
    reset_token_cache()
    yield
    config_service.invalidate_cache()
    reset_token_cache()


@pytest.fixture()
def patched_scope(monkeypatch, mem_db):
    """
    把 `app.core.database.session_scope` 换成内存会话。

    ⚠ 为什么必须与真实实现行为一致（正常提交、异常回滚）：
    若这里只 yield 不提交，被测代码里"设了属性但忘了 commit"的 bug
    会被完全掩盖 —— 而 notifier._deliver 的正确性恰恰依赖那次 commit。
    """
    @contextlib.contextmanager
    def _scope():
        try:
            yield mem_db
            mem_db.commit()
        except Exception:
            mem_db.rollback()
            raise

    monkeypatch.setattr("app.core.database.session_scope", _scope)
    return mem_db


@pytest.fixture()
def no_sleep(monkeypatch):
    """把重试的退避 sleep 换成记录（否则重试用例会白等 7 秒）"""
    slept: List[float] = []
    monkeypatch.setattr(notifier.time, "sleep", lambda s: slept.append(s))
    return slept


@pytest.fixture()
def sample_msg() -> AlertMessage:
    """一条典型的触发告警消息（渠道排版与发送用例共用）"""
    return AlertMessage(
        event_id=1,
        event_no="AL20260921143000001",
        title="【重要】核心交换机01 CPU使用率过高",
        device_name="核心交换机01",
        device_ip="10.10.0.1",
        device_type="switch",
        location="珠海机房A区",
        metric_name="CPU使用率",
        metric_value_text="93.5%",
        threshold_text="大于 85%",
        level="major",
        occurred_at=datetime(2026, 9, 21, 14, 30, 0),
        is_recovery=False,
        duration_text="",
        occur_count=1,
        platform_url="http://10.10.0.100:8000",
    )


@pytest.fixture()
def mail_cfg() -> Dict[str, Any]:
    """
    一份配置齐全的邮件配置。

    键名是**去掉分组前缀**之后的短名 —— config_service.get_channel_settings()
    会把 `mail.smtp_host` 转成 `smtp_host`，渠道实现拿到的是短名。
    """
    return {
        "enabled": "1",
        "smtp_host": "smtp.exmail.qq.com",
        "smtp_port": "465",
        "smtp_user": "monitor@enetop.com",
        "smtp_password": "AUTHCODE",
        "sender": "monitor@enetop.com",
        "use_ssl": "1",
        "receivers": '["ops@enetop.com", "net@enetop.com"]',
    }


# ============================================================================
# 助手
# ============================================================================

def make_device(db: Session, **overrides: Any) -> MonDevice:
    """造一台设备"""
    data: Dict[str, Any] = {
        "name": "核心交换机01",
        "ip": "10.10.0.1",
        "device_type": "switch",
        "protocol": "snmp",
        "collect_interval": 60,
        "enabled": True,
        "online_status": "online",
    }
    data.update(overrides)
    device = MonDevice(**data)
    db.add(device)
    db.commit()
    db.refresh(device)
    return device


def make_rule(db: Session, **overrides: Any) -> AlertRule:
    """造一条告警规则（默认：CPU>85 连续3次触发、连续3次恢复、不配渠道）"""
    data: Dict[str, Any] = {
        "name": "CPU使用率过高",
        "metric_key": "cpu_usage",
        "operator": "gt",
        "threshold": 85.0,
        "duration_n": 3,
        "recovery_n": 3,
        "level": "major",
        "converge_window": 300,
        "converge_max": 3,
        "enabled": True,
        "notify_channels": "[]",
    }
    data.update(overrides)
    rule = AlertRule(**data)
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return rule


def set_metric(db: Session, device_id: int, metric_key: str, value: Optional[float]) -> None:
    """写一次指标（走真实的 UPSERT 写入路径，而不是直接塞 ORM 对象）"""
    metric_service.ingest(
        db, device_id,
        [{"metric_key": metric_key, "metric_value": value}],
        write_sample=False,          # 手动测试不污染历史曲线
    )
    db.commit()


def drive(
    db: Session,
    device: MonDevice,
    value: Optional[float],
    key: str = "cpu_usage",
    rounds: int = 1,
) -> Dict[str, int]:
    """
    推进 N 轮"采集 → 评估"。

    每轮先写一次指标再评估一次，模拟真实节奏。
    为什么不是"写一次值然后连续评估 N 次"：那样会掩盖
    "取值时机/取值来源"的问题，而轮询平台的所有告警 bug 几乎都出在那里。
    """
    stats: Dict[str, int] = {}
    for _ in range(rounds):
        if value is not None or key:
            set_metric(db, device.id, key, value)
        stats = evaluator.evaluate_devices(db, [device.id])
    return stats


def get_state(db: Session, rule_id: int, device_id: int):
    return alert_service.get_rule_state(db, rule_id, device_id)


def all_events(db: Session) -> List[AlertEvent]:
    return list(db.execute(select(AlertEvent).order_by(AlertEvent.id)).scalars().all())


def alarm_actions(db: Session) -> List[str]:
    return list(db.execute(select(AlarmLog.action).order_by(AlarmLog.id)).scalars().all())


@contextlib.contextmanager
def extra_channel(key: str, result: ChannelResult, calls: Optional[List[AlertMessage]] = None):
    """
    临时注册一个渠道，退出时摘掉。

    用它替代"真的配置企业微信"：本文件要验证的是**链路与状态流转**，
    不是对方接口 —— 后者属于第8步的部署验收。
    退出时必须摘掉，否则会把假渠道留给后面的用例，
    导致"渠道未注册"类用例莫名通过。
    """
    calls = calls if calls is not None else []

    @ChannelRegistry.register(key)
    class _TempChannel(BaseChannel):
        text = "临时渠道"
        config_group = key

        def check(self, cfg: Dict[str, Any]) -> Tuple[bool, str]:
            return True, ""

        def send(self, message: AlertMessage, cfg: Dict[str, Any]) -> ChannelResult:
            calls.append(message)
            return result

    try:
        yield calls
    finally:
        ChannelRegistry._registry.pop(key, None)


class StubChannel(BaseChannel):
    """按预设序列返回结果的桩渠道（用于测重试逻辑）"""

    key = "stub"
    text = "桩渠道"
    config_group = "stub"

    def __init__(self, results: List[ChannelResult]) -> None:
        self._results = list(results)
        self.calls = 0

    def check(self, cfg: Dict[str, Any]) -> Tuple[bool, str]:
        return True, ""

    def send(self, message: AlertMessage, cfg: Dict[str, Any]) -> ChannelResult:
        self.calls += 1
        return self._results[min(self.calls - 1, len(self._results) - 1)]


# ============================================================================
# 一、指标单位表（全平台唯一来源）
# ============================================================================

class TestMetricUnit:
    """
    单位表看起来是小事，但它是"邮件写 93.5%、企微写 93.5ms"这类
    内容不一致的唯一防线，必须钉住。
    """

    @pytest.mark.parametrize("key,expected", [
        ("cpu_usage", "%"),
        ("mem_usage", "%"),
        ("disk_usage", "%"),
        ("ping_loss", "%"),
        ("ping_latency", "ms"),
        ("device_temp", "℃"),
        ("process_count", "个"),
        ("disk_free", "GB"),
        ("if_in_rate", "Mbps"),
    ])
    def test_已登记指标取到单位(self, key, expected):
        assert metric_unit(key) == expected

    @pytest.mark.parametrize("key", [
        "if_in_rate:GigabitEthernet0/0/1",
        "if_out_rate:Gi1/0/24",
        "if_bandwidth:XGE0/0/2",
        "disk_free:C:",
    ])
    def test_端口类指标按基名命中单位(self, key):
        """
        端口名千变万化（Gi0/0/1、Ten-GigabitEthernet1/0/1…），
        不可能在单位表里逐个列出来，必须按基名匹配。
        """
        assert metric_unit(key)

    def test_状态类指标没有单位返回空串(self):
        # 返回空串而不是 None，调用方才能直接 f"{value}{unit}" 拼
        assert metric_unit("if_status") == ""
        assert metric_unit("online_status") == ""

    def test_空值安全(self):
        assert metric_unit(None) == ""
        assert metric_unit("") == ""

    def test_单位表与指标字典逐项一致(self):
        """
        把 scripts/static_audit.py 的检查 6 纳入 pytest。

        为什么值得加：本表最初写的是 `"temperature": "℃"`，
        而指标字典里真实的键是 `device_temp` ——
        也就是说**真正要用的那个键没有单位**，对这个指标配的告警
        在通知里只会显示裸数字。这类漂移不会报错，
        只能靠"两条数据源交叉比对"发现，
        而"记得手动跑脚本"是靠不住的，所以放进测试套件。
        """
        from scripts.static_audit import check_metric_units

        root = Path(__file__).resolve().parents[1]
        problems = check_metric_units(
            root / "app/models/alert.py", root / "sql/init_data.sql"
        )
        assert problems == 0, "指标单位表与 metric_catalog 不一致，见上方 FAIL 明细"

    def test_单位表非空且键名不含端口后缀(self):
        """基本不变量：登记的都该是基名，带冒号的键说明写错了"""
        assert METRIC_UNITS, "单位表不应为空"
        assert not [k for k in METRIC_UNITS if ":" in k], \
            "单位表只登记基名；端口后缀由 metric_unit() 拆掉后匹配"


# ============================================================================
# 二、评估状态机：触发
# ============================================================================

class TestEvaluatorTrigger:
    """连续 N 次超标才触发，且同一次故障只报一次"""

    def test_未达连续次数不触发(self, mem_db):
        dev = make_device(mem_db)
        rule = make_rule(mem_db, duration_n=3)

        stats = drive(mem_db, dev, 95.0, rounds=2)

        assert stats["fired"] == 0
        assert not all_events(mem_db), "未达触发条件不应产生事件"
        state = get_state(mem_db, rule.id, dev.id)
        assert state.breach_count == 2
        assert state.status == evaluator.STATE_NORMAL

    def test_达到连续次数触发(self, mem_db):
        dev = make_device(mem_db)
        rule = make_rule(mem_db, duration_n=3)

        stats = drive(mem_db, dev, 95.0, rounds=3)

        assert stats["fired"] == 1
        events = all_events(mem_db)
        assert len(events) == 1
        assert events[0].status == EVENT_STATUS_FIRING
        assert events[0].metric_value == 95.0
        assert events[0].rule_id == rule.id
        assert events[0].device_id == dev.id

    def test_触发后breach_count清零(self, mem_db):
        dev = make_device(mem_db)
        rule = make_rule(mem_db, duration_n=3)
        drive(mem_db, dev, 95.0, rounds=3)

        state = get_state(mem_db, rule.id, dev.id)

        assert state.breach_count == 0, \
            "触发后必须清零，否则后续每轮都满足 >= duration_n，会反复触发"

    def test_持续超标不重复触发也不重复推送(self, mem_db, fake_notify_pool):
        """
        ★ 本模块最重要的一条性质。

        若"触发后不清零"或"firing 状态下仍判断触发"，
        第 4、5、6… 轮采集每轮都会 create_event 一次：
        事件表里 occur_count 疯涨、告警群被同一条消息反复刷 ——
        运维第一次遇到就会把告警群设成免打扰，整套系统随之作废。
        """
        dev = make_device(mem_db)
        make_rule(mem_db, duration_n=3, notify_channels='["probe"]')

        with extra_channel("probe", ChannelResult(success=True, message="ok")):
            drive(mem_db, dev, 95.0, rounds=3)
            after = drive(mem_db, dev, 96.0, rounds=10)

        assert after["fired"] == 0, "已在告警中的设备持续超标不应再次触发"
        events = all_events(mem_db)
        assert len(events) == 1, "同一次故障只能有一条事件"
        assert events[0].occur_count == 1, "未恢复前不应累加发生次数"
        # 推送也只发生了一次
        assert len(fake_notify_pool.pool.submissions) == 1

    def test_中间夹一次正常会清零超标计数(self, mem_db):
        """抖动抑制：阈值边缘来回跳动的指标不该报出来"""
        dev = make_device(mem_db)
        rule = make_rule(mem_db, duration_n=3)

        drive(mem_db, dev, 95.0, rounds=2)      # 连续 2 次超标
        drive(mem_db, dev, 10.0, rounds=1)      # 一次正常 → 归零
        stats = drive(mem_db, dev, 95.0, rounds=2)

        assert stats["fired"] == 0
        assert get_state(mem_db, rule.id, dev.id).breach_count == 2

    def test_触发后状态与事件互相绑定(self, mem_db):
        dev = make_device(mem_db)
        rule = make_rule(mem_db, duration_n=1, recovery_n=2)
        drive(mem_db, dev, 95.0, rounds=1)

        state = get_state(mem_db, rule.id, dev.id)
        event = all_events(mem_db)[0]

        assert state.status == evaluator.STATE_FIRING
        assert state.current_event_id == event.id
        assert state.last_fire_time is not None
        assert state.last_value == 95.0

    def test_正常态的normal_count封顶(self, mem_db):
        """
        长期正常时 normal_count 不应无限增长：
        前端会显示"连续正常 100000 次"这种噪音，且没有业务含义。
        """
        dev = make_device(mem_db)
        rule = make_rule(mem_db, duration_n=2, recovery_n=3)

        drive(mem_db, dev, 10.0, rounds=20)

        assert get_state(mem_db, rule.id, dev.id).normal_count == 3

    def test_每个阈值档位各报一次(self, mem_db):
        """
        "CPU>80 一般 + CPU>95 紧急"是常见配置，两条都该独立触发。
        """
        dev = make_device(mem_db)
        make_rule(mem_db, name="CPU偏高", duration_n=1, threshold=80, level="minor")
        make_rule(mem_db, name="CPU危急", duration_n=1, threshold=95, level="critical")

        stats = drive(mem_db, dev, 97.0, rounds=1)

        assert stats["fired"] == 2
        assert len(all_events(mem_db)) == 2


# ============================================================================
# 三、评估状态机：恢复
# ============================================================================

class TestEvaluatorRecovery:
    """滞回恢复：必须连续 M 次正常才算恢复"""

    def _fire(self, db, device, rule, value=95.0):
        """把设备推到告警中状态"""
        drive(db, device, value, rounds=max(int(rule.duration_n or 1), 1))

    def test_未达连续次数不恢复(self, mem_db):
        dev = make_device(mem_db)
        rule = make_rule(mem_db, duration_n=1, recovery_n=3)
        self._fire(mem_db, dev, rule)

        stats = drive(mem_db, dev, 10.0, rounds=2)

        assert stats["recovered"] == 0
        assert all_events(mem_db)[0].status == EVENT_STATUS_FIRING
        assert get_state(mem_db, rule.id, dev.id).normal_count == 2

    def test_达到连续次数恢复(self, mem_db):
        dev = make_device(mem_db)
        rule = make_rule(mem_db, duration_n=1, recovery_n=3)
        self._fire(mem_db, dev, rule)

        stats = drive(mem_db, dev, 10.0, rounds=3)

        assert stats["recovered"] == 1
        event = all_events(mem_db)[0]
        assert event.status == EVENT_STATUS_RECOVERED
        assert event.recover_at is not None

    def test_恢复中夹一次超标则重新计数(self, mem_db):
        """
        滞回的核心：阈值在边界上下摆动时，不能"恢复→告警→恢复→告警"地刷。
        """
        dev = make_device(mem_db)
        rule = make_rule(mem_db, duration_n=1, recovery_n=3)
        self._fire(mem_db, dev, rule)

        drive(mem_db, dev, 10.0, rounds=2)      # 正常 2 次
        drive(mem_db, dev, 95.0, rounds=1)      # 又超标 → 正常计数归零
        stats = drive(mem_db, dev, 10.0, rounds=1)

        assert stats["recovered"] == 0
        state = get_state(mem_db, rule.id, dev.id)
        assert state.normal_count == 1
        assert state.status == evaluator.STATE_FIRING

    def test_恢复后状态与事件都归位(self, mem_db):
        dev = make_device(mem_db)
        rule = make_rule(mem_db, duration_n=1, recovery_n=1)
        self._fire(mem_db, dev, rule)
        drive(mem_db, dev, 10.0, rounds=1)

        state = get_state(mem_db, rule.id, dev.id)

        assert state.status == evaluator.STATE_NORMAL
        assert state.current_event_id is None
        assert state.breach_count == 0
        assert state.normal_count == 0

    def test_恢复后再次超标产生新事件(self, mem_db):
        """
        跨故障周期：恢复 → 再次超标 应当是**新**事件，
        这样告警列表才能清晰呈现"发生 → 恢复"的完整周期。
        """
        dev = make_device(mem_db)
        make_rule(mem_db, duration_n=1, recovery_n=1)

        drive(mem_db, dev, 95.0, rounds=1)
        drive(mem_db, dev, 10.0, rounds=1)
        stats = drive(mem_db, dev, 95.0, rounds=1)

        assert stats["fired"] == 1
        events = all_events(mem_db)
        assert len(events) == 2
        assert [e.status for e in events] == [EVENT_STATUS_RECOVERED, EVENT_STATUS_FIRING]

    def test_已确认后持续超标不重新触发(self, mem_db):
        """
        ★ acked 的语义是"我已知晓、正在处理"。
        若还继续报，运维刚点完确认又收到一条同样的告警 ——
        这正是"确认"按钮失去意义的原因。
        """
        dev = make_device(mem_db)
        rule = make_rule(mem_db, duration_n=1, recovery_n=2)
        self._fire(mem_db, dev, rule)
        event = all_events(mem_db)[0]

        alert_service.ack_event(
            mem_db, event.id, AlertAckRequest(remark="已联系厂商处理"), "zhansan"
        )
        stats = drive(mem_db, dev, 95.0, rounds=6)

        assert stats["fired"] == 0
        assert get_state(mem_db, rule.id, dev.id).status == RULE_STATE_ACKED
        refreshed = mem_db.get(AlertEvent, event.id)
        assert refreshed.status == EVENT_STATUS_ACKED
        assert refreshed.occur_count == 1

    def test_已确认后恢复正常仍会恢复(self, mem_db):
        """确认不等于"永远不再管" —— 指标恢复了必须能走到 recovered"""
        dev = make_device(mem_db)
        rule = make_rule(mem_db, duration_n=1, recovery_n=1)
        self._fire(mem_db, dev, rule)
        event = all_events(mem_db)[0]
        alert_service.ack_event(
            mem_db, event.id, AlertAckRequest(remark="处理中"), "zhansan"
        )

        stats = drive(mem_db, dev, 10.0, rounds=1)

        assert stats["recovered"] == 1
        assert mem_db.get(AlertEvent, event.id).status == EVENT_STATUS_RECOVERED

    def test_状态卡在告警中但事件不存在时自愈(self, mem_db):
        """
        最危险的一类静默失效：current_event_id 指向的事件被人工清理掉后，
        若不自愈，该规则会永远停在"告警中"且**再也不会产生任何新告警**。
        """
        dev = make_device(mem_db)
        rule = make_rule(mem_db, duration_n=1, recovery_n=1)
        self._fire(mem_db, dev, rule)
        event = all_events(mem_db)[0]

        # 模拟"事件被人工删除/清理"
        mem_db.execute(delete(AlarmLog).where(AlarmLog.event_id == event.id))
        mem_db.execute(delete(AlertEvent).where(AlertEvent.id == event.id))
        mem_db.commit()

        stats = drive(mem_db, dev, 10.0, rounds=1)

        assert stats["recovered"] == 0, "没有事件可恢复，不该虚报恢复数"
        state = get_state(mem_db, rule.id, dev.id)
        assert state.status == evaluator.STATE_NORMAL, "状态必须自愈为正常"


# ============================================================================
# 四、取值：最容易写错的一处
# ============================================================================

class TestEvaluatorValueResolution:
    """取不到值时必须"停在原地"，既不记超标也不记正常"""

    def test_取不到指标值时不动计数器(self, mem_db):
        """
        ★ 最易写错的一条（模块 docstring 第 3 条）。

        设备离线时 ping_latency 自然不再上报。若把"取不到"当成"未超标"，
        就会在设备离线的那一刻把延迟告警**误恢复** ——
        值班人员看到"设备离线了、延迟告警恢复了"，从此不再相信平台的数字。
        """
        dev = make_device(mem_db)
        rule = make_rule(mem_db, metric_key="ping_latency", threshold=100,
                         duration_n=1, recovery_n=1)
        drive(mem_db, dev, 200.0, key="ping_latency", rounds=1)
        assert all_events(mem_db)[0].status == EVENT_STATUS_FIRING

        # 指标停止上报（把热表里的那一行删掉，模拟采集不到）
        mem_db.execute(delete(MetricLatest).where(
            MetricLatest.device_id == dev.id,
            MetricLatest.metric_key == "ping_latency",
        ))
        mem_db.commit()

        stats = evaluator.evaluate_devices(mem_db, [dev.id])

        assert stats["no_value"] == 1
        assert stats["recovered"] == 0, "取不到值绝不能判为恢复"
        state = get_state(mem_db, rule.id, dev.id)
        assert state.normal_count == 0, "取不到值时正常计数必须原样不动"
        assert state.status == evaluator.STATE_FIRING

    def test_连续多轮取不到值仍不恢复(self, mem_db):
        dev = make_device(mem_db)
        make_rule(mem_db, metric_key="ping_latency", threshold=100,
                  duration_n=1, recovery_n=1)
        drive(mem_db, dev, 200.0, key="ping_latency", rounds=1)
        mem_db.execute(delete(MetricLatest).where(MetricLatest.device_id == dev.id))
        mem_db.commit()

        for _ in range(10):
            stats = evaluator.evaluate_devices(mem_db, [dev.id])
            assert stats["recovered"] == 0

        assert mem_db.get(AlertEvent, all_events(mem_db)[0].id).status == EVENT_STATUS_FIRING

    def test_指标值为NULL视为取不到(self, mem_db):
        dev = make_device(mem_db)
        rule = make_rule(mem_db, duration_n=1)
        mem_db.add(MetricLatest(
            device_id=dev.id, metric_key="cpu_usage",
            metric_value=None, str_value=None, collect_time=datetime.now(),
        ))
        mem_db.commit()

        stats = evaluator.evaluate_devices(mem_db, [dev.id])

        assert stats["no_value"] == 1
        assert stats["fired"] == 0
        assert get_state(mem_db, rule.id, dev.id) is None, "不评估就不该建状态行"

    def test_在线状态取设备表字段而非指标表(self, mem_db):
        """
        metric_latest 里的 online_status 会滞后一轮
        （先写指标、再回写设备状态，顺序上差一拍），
        所以在线状态必须以设备表为准。
        """
        dev = make_device(mem_db, online_status="offline")
        make_rule(mem_db, metric_key="online_status", operator="eq", threshold=0,
                  duration_n=1)
        # 故意写入矛盾的指标值：设备表说离线、指标表说在线
        set_metric(mem_db, dev.id, "online_status", 1.0)

        stats = evaluator.evaluate_devices(mem_db, [dev.id])

        assert stats["fired"] == 1, "应以设备表的 offline 为准"
        assert all_events(mem_db)[0].metric_value == 0.0

    def test_未知在线状态不评估(self, mem_db):
        """
        新导入的设备在第一次采集成功前是 unknown。
        若把 unknown 当离线，导入 200 台设备会瞬间刷出 200 条"设备离线"告警。
        """
        dev = make_device(mem_db, online_status="unknown")
        rule = make_rule(mem_db, metric_key="online_status", operator="eq", threshold=0,
                         duration_n=1)

        stats = drive(mem_db, dev, None, key="online_status", rounds=1)

        assert stats["no_value"] == 1
        assert stats["fired"] == 0
        assert get_state(mem_db, rule.id, dev.id) is None

    def test_未知比较符只计数不报错(self, mem_db):
        """
        历史脏数据 / 手工改库都可能写出未知比较符。
        不能抛异常（一条坏规则会让整批评估中断），也不能按错误语义比较。
        """
        dev = make_device(mem_db)
        make_rule(mem_db, operator="between", duration_n=1)

        stats = drive(mem_db, dev, 95.0, rounds=1)

        assert stats["no_operator"] == 1
        assert stats["errors"] == 0
        assert stats["fired"] == 0

    def test_阈值或次数为0时不崩且口径明确(self, mem_db):
        """
        阈值/次数在库里是 NOT NULL，但"0"是能存进去的（手工建规则、前端没填）。
        兜底口径：阈值按 0、连续次数按 1，且不能让平台崩。

        为什么特意强调"按 1"而不是"按 0 就一直不触发"：
        次数为 0 时若照字面比较（breach_count >= 0 恒真）会让规则**每次都触发**，
        按 1 处理是"至少连续 1 次"，语义明确且与前端提示一致。
        """
        dev = make_device(mem_db)
        make_rule(mem_db, threshold=0, duration_n=0, recovery_n=0)

        stats = drive(mem_db, dev, 1.0, rounds=1)

        assert stats["errors"] == 0
        assert stats["fired"] == 1, "阈值按 0 处理，正数即超标；次数按 1，当轮触发"

    def test_单条规则出错不影响其它规则(self, mem_db, monkeypatch):
        """
        一条坏规则（例如阈值是脏字符串）不该让全平台告警停摆。
        """
        dev = make_device(mem_db)
        bad = make_rule(mem_db, name="坏规则", duration_n=1, threshold=80)
        make_rule(mem_db, name="好规则", duration_n=1, threshold=90)

        original = evaluator._evaluate_one

        def _boom(db, device, rule, *args, **kwargs):
            if rule.id == bad.id:
                raise RuntimeError("模拟坏规则炸掉")
            return original(db, device, rule, *args, **kwargs)

        monkeypatch.setattr(evaluator, "_evaluate_one", _boom)
        stats = drive(mem_db, dev, 95.0, rounds=1)

        assert stats["errors"] == 1
        assert stats["fired"] == 1, "好规则仍须正常触发"

    def test_无规则或无设备时安全返回(self, mem_db):
        dev = make_device(mem_db)
        stats = evaluator.evaluate_devices(mem_db, [dev.id])
        assert stats["rules"] == 0 and stats["fired"] == 0

        make_rule(mem_db)
        stats = evaluator.evaluate_devices(mem_db, [])
        assert stats["devices"] == 0

    def test_传device_ids时不受启用开关限制(self, mem_db):
        """
        手动"立即评估"必须能评估已停用的设备 ——
        现场实施时经常先停用采集、再手动试一次。
        """
        dev = make_device(mem_db, enabled=False)
        make_rule(mem_db, duration_n=1)
        drive(mem_db, dev, 95.0, rounds=1)

        assert all_events(mem_db)

    def test_全量评估时跳过已停用设备(self, mem_db):
        dev = make_device(mem_db, enabled=False)
        make_rule(mem_db, duration_n=1)
        set_metric(mem_db, dev.id, "cpu_usage", 95.0)

        stats = evaluator.evaluate_devices(mem_db, None)

        assert stats["devices"] == 0
        assert stats["fired"] == 0


# ============================================================================
# 五、取值：端口类指标的基名匹配
# ============================================================================

class TestPortMetricResolution:
    """
    端口类指标必须"取极值不取平均"：
    单个端口跑满就是真问题，平均到 48 个端口上会被完全抹平
    —— 这是网络告警最经典的漏报原因。
    """

    def _seed(self, db: Session, device: MonDevice, pairs: Dict[str, float]) -> None:
        metric_service.ingest(
            db, device.id,
            [{"metric_key": f"if_in_rate:{port}", "metric_value": v}
             for port, v in pairs.items()],
            write_sample=False,
        )
        db.commit()

    def _resolve(self, db: Session, device: MonDevice, rule: AlertRule):
        latest = metric_service.get_latest_map(db, [device.id])
        return evaluator._resolve_value(device, rule, latest)

    def test_大于方向取最大值(self, mem_db):
        dev = make_device(mem_db)
        self._seed(mem_db, dev, {"Gi0/0/1": 12.0, "Gi0/0/24": 940.0})
        rule = make_rule(mem_db, metric_key="if_in_rate", operator="gt", threshold=800)

        value, key = self._resolve(mem_db, dev, rule)

        assert value == 940.0
        assert key == "if_in_rate:Gi0/0/24", "必须能定位到具体端口"

    def test_小于方向取最小值(self, mem_db):
        """「某个磁盘/端口剩余不足」要用最小值判断"""
        dev = make_device(mem_db)
        dev_disk = {"C:": 4.0, "D:": 300.0}
        metric_service.ingest(
            mem_db, dev.id,
            [{"metric_key": f"disk_free:{name}", "metric_value": v}
             for name, v in dev_disk.items()],
            write_sample=False,
        )
        mem_db.commit()
        rule = make_rule(mem_db, metric_key="disk_free", operator="lt", threshold=10)

        value, key = self._resolve(mem_db, dev, rule)

        assert value == 4.0
        assert key == "disk_free:C:"

    def test_精确键优先于基名匹配(self, mem_db):
        """规则直接写了完整键时，不该再去按端口聚合"""
        dev = make_device(mem_db)
        self._seed(mem_db, dev, {"Gi0/0/1": 940.0})
        set_metric(mem_db, dev.id, "if_in_rate", 5.0)
        rule = make_rule(mem_db, metric_key="if_in_rate", operator="gt", threshold=1)

        value, key = self._resolve(mem_db, dev, rule)

        assert value == 5.0
        assert key == "if_in_rate"

    def test_无匹配端口时返回None(self, mem_db):
        dev = make_device(mem_db)
        self._seed(mem_db, dev, {"Gi0/0/1": 100.0})
        rule = make_rule(mem_db, metric_key="if_out_rate", operator="gt", threshold=1)

        value, key = self._resolve(mem_db, dev, rule)

        assert value is None
        assert key == "if_out_rate"

    def test_端口值为NULL时跳过该端口(self, mem_db):
        dev = make_device(mem_db)
        self._seed(mem_db, dev, {"Gi0/0/1": 940.0})
        mem_db.add(MetricLatest(
            device_id=dev.id, metric_key="if_in_rate:Gi0/0/24",
            metric_value=None, collect_time=datetime.now(),
        ))
        mem_db.commit()
        rule = make_rule(mem_db, metric_key="if_in_rate", operator="gt", threshold=1)

        value, _key = self._resolve(mem_db, dev, rule)

        assert value == 940.0, "NULL 值不参与比较，不能把结果带成 None"

    def test_指标中文名带端口名(self, mem_db):
        """运维要一眼看出是哪个口，不能只写「端口入向速率」"""
        catalog = {
            "if_in_rate": MetricCatalog(metric_key="if_in_rate", metric_name="端口入向速率")
        }
        name = evaluator._metric_display_name("if_in_rate:GigabitEthernet0/0/1", catalog)
        assert name == "端口入向速率(GigabitEthernet0/0/1)"

    def test_指标中文名无端口时不加括号(self, mem_db):
        catalog = {"cpu_usage": MetricCatalog(metric_key="cpu_usage", metric_name="CPU使用率")}
        assert evaluator._metric_display_name("cpu_usage", catalog) == "CPU使用率"

    def test_字典缺项时退回指标键(self, mem_db):
        assert evaluator._metric_display_name("unknown_key", {}) == "unknown_key"

    def test_告警标题带端口名(self, mem_db):
        dev = make_device(mem_db)
        self._seed(mem_db, dev, {"Gi0/0/24": 940.0})
        make_rule(mem_db, metric_key="if_in_rate", operator="gt", threshold=800,
                  duration_n=1)
        mem_db.add(MetricCatalog(
            metric_key="if_in_rate", metric_name="端口入向速率", unit="Mbps",
        ))
        mem_db.commit()

        drive(mem_db, dev, None, key="if_in_rate", rounds=0)  # 不覆盖已写入的端口指标
        stats = evaluator.evaluate_devices(mem_db, [dev.id])

        assert stats["fired"] == 1
        event = all_events(mem_db)[0]
        assert "Gi0/0/24" in (event.metric_name or "")
        assert event.metric_key == "if_in_rate:Gi0/0/24"


# ============================================================================
# 六、规则筛选
# ============================================================================

class TestRulesFor:
    """设备级规则优先于全局规则（按指标键分组覆盖）"""

    def test_设备级规则覆盖同指标的全局规则(self, mem_db):
        """
        给核心交换机单独放宽 CPU 阈值时必须真的生效。
        若全局规则也一起生效，运维会同时收到"85% 告警"和"95% 不告警"，
        完全无法解释自己的配置产生了什么行为。
        """
        dev = make_device(mem_db)
        make_rule(mem_db, name="全局CPU", metric_key="cpu_usage", threshold=85)
        dev_rule = make_rule(mem_db, name="核心交换机CPU", metric_key="cpu_usage",
                             device_id=dev.id, threshold=95)

        picked = evaluator._rules_for(dev, evaluator._load_rules(mem_db), {})

        assert [r.id for r in picked] == [dev_rule.id]

    def test_设备级规则不影响其它指标的全局规则(self, mem_db):
        dev = make_device(mem_db)
        make_rule(mem_db, name="全局CPU", metric_key="cpu_usage", threshold=85)
        mem_rule = make_rule(mem_db, name="全局内存", metric_key="mem_usage", threshold=90)
        dev_rule = make_rule(mem_db, name="本机CPU", metric_key="cpu_usage",
                             device_id=dev.id, threshold=95)

        picked = evaluator._rules_for(dev, evaluator._load_rules(mem_db), {})

        assert {r.id for r in picked} == {dev_rule.id, mem_rule.id}

    def test_同指标多条全局规则都生效(self, mem_db):
        """「CPU>80 一般 + CPU>95 紧急」是合理配置，两条都该在"""
        dev = make_device(mem_db)
        r1 = make_rule(mem_db, name="CPU偏高", metric_key="cpu_usage", threshold=80)
        r2 = make_rule(mem_db, name="CPU危急", metric_key="cpu_usage", threshold=95)

        picked = evaluator._rules_for(dev, evaluator._load_rules(mem_db), {})

        assert {r.id for r in picked} == {r1.id, r2.id}

    def test_别的设备的专属规则不生效(self, mem_db):
        dev = make_device(mem_db, name="普通交换机")
        other = make_device(mem_db, name="核心交换机", ip="10.10.0.2")
        make_rule(mem_db, name="核心专属", metric_key="cpu_usage", device_id=other.id)

        picked = evaluator._rules_for(dev, evaluator._load_rules(mem_db), {})

        assert picked == []

    def test_停用的规则不加载(self, mem_db):
        dev = make_device(mem_db)
        make_rule(mem_db, name="停用规则", duration_n=1, enabled=False)
        set_metric(mem_db, dev.id, "cpu_usage", 95.0)

        stats = evaluator.evaluate_devices(mem_db, [dev.id])

        assert stats["rules"] == 0
        assert stats["fired"] == 0

    def test_设备级规则排在后但都参与评估(self, mem_db):
        dev = make_device(mem_db)
        dev_rule = make_rule(mem_db, name="本机CPU", metric_key="cpu_usage",
                             device_id=dev.id, duration_n=1, threshold=95)
        mem_rule = make_rule(mem_db, name="内存", metric_key="mem_usage",
                             duration_n=1, threshold=90)

        picked = evaluator._rules_for(dev, evaluator._load_rules(mem_db), {})

        assert {r.id for r in picked} == {dev_rule.id, mem_rule.id}


# ============================================================================
# 七、收敛器
# ============================================================================

class TestConvergerCounting:
    """收敛计数口径：按「告警事件数」而非「推送条数」"""

    def _notified(self, db: Session, event: AlertEvent, channels: List[str],
                  status: str = NOTIFY_SUCCESS, age_seconds: int = 0) -> None:
        for ch in channels:
            db.add(AlertNotifyRecord(
                event_id=event.id, channel=ch, status=status,
                created_at=datetime.now() - timedelta(seconds=age_seconds),
            ))
        db.commit()

    def _setup(self, db: Session):
        dev = make_device(db)
        rule = make_rule(db, duration_n=1)
        event = alert_service.create_event(db, rule, dev, "cpu_usage", 95.0)
        db.commit()
        return dev, rule, event

    def test_一条告警发多个渠道只算一条(self, mem_db):
        """
        ★ converge_max=3 的语义是"窗口内最多推送 3 条告警"，
        不是"最多发 3 封邮件"。若按记录数算，配了 3 个渠道的客户
        会发现限额莫名其妙被用掉三分之二，表现为"漏报"。
        """
        _dev, _rule, event = self._setup(mem_db)
        self._notified(mem_db, event, ["email", "wecom", "dingtalk"])

        assert converger.count_recent_notified_events(
            mem_db, event.fingerprint, 300
        ) == 1

    def test_不同事件分别计数(self, mem_db):
        dev = make_device(mem_db)
        rule = make_rule(mem_db, duration_n=1, recovery_n=1)
        e1 = alert_service.create_event(mem_db, rule, dev, "cpu_usage", 95.0)
        mem_db.commit()
        self._notified(mem_db, e1, ["email"])
        # 恢复后再触发 → 新事件（同指纹）
        alert_service.recover_event(mem_db, e1)
        mem_db.commit()
        e2 = alert_service.create_event(mem_db, rule, dev, "cpu_usage", 96.0)
        mem_db.commit()
        self._notified(mem_db, e2, ["email", "wecom"])

        assert converger.count_recent_notified_events(
            mem_db, e1.fingerprint, 300
        ) == 2, "同一指纹下的 2 条事件应各算 1 条"

    def test_失败推送不占用限额(self, mem_db):
        """
        邮件没配好不该把企业微信的额度吃掉 ——
        否则现场会出现"明明配好了却收不到"的怪现象。
        """
        _dev, _rule, event = self._setup(mem_db)
        self._notified(mem_db, event, ["email"], status=NOTIFY_FAIL)

        assert converger.count_recent_notified_events(
            mem_db, event.fingerprint, 300
        ) == 0

    def test_发送中的记录计入限额(self, mem_db):
        """
        pending 必须计入：投递是异步的，若不计数，
        短时间内连续触发会全部通过判定，等于没有收敛。
        """
        _dev, _rule, event = self._setup(mem_db)
        self._notified(mem_db, event, ["email"], status=NOTIFY_PENDING)

        assert converger.count_recent_notified_events(
            mem_db, event.fingerprint, 300
        ) == 1

    def test_窗口外的记录不计入(self, mem_db):
        _dev, _rule, event = self._setup(mem_db)
        self._notified(mem_db, event, ["email"], age_seconds=600)

        assert converger.count_recent_notified_events(
            mem_db, event.fingerprint, 300
        ) == 0

    def test_指纹不同互不影响(self, mem_db):
        dev = make_device(mem_db)
        r1 = make_rule(mem_db, name="CPU", duration_n=1)
        r2 = make_rule(mem_db, name="内存", duration_n=1, metric_key="mem_usage")
        e1 = alert_service.create_event(mem_db, r1, dev, "cpu_usage", 95.0)
        mem_db.commit()
        self._notified(mem_db, e1, ["email"])

        assert converger.count_recent_notified_events(
            mem_db, alert_service.build_fingerprint(dev.id, r2.id), 300
        ) == 0

    def test_窗口或指纹为空时返回0(self, mem_db):
        _dev, _rule, event = self._setup(mem_db)
        self._notified(mem_db, event, ["email"])

        assert converger.count_recent_notified_events(mem_db, event.fingerprint, 0) == 0
        assert converger.count_recent_notified_events(mem_db, "", 300) == 0


class TestConvergerDecide:
    """收敛判定：静默期 / 窗口限流 / 恢复通知的优先级"""

    def _setup(self, db: Session, **rule_overrides: Any):
        # 用 setdefault 而不是直接传 duration_n=1：
        # 否则调用方传 duration_n=3 时会 "got multiple values for keyword argument"
        rule_overrides.setdefault("duration_n", 1)
        dev = make_device(db)
        rule = make_rule(db, **rule_overrides)
        event = alert_service.create_event(db, rule, dev, "cpu_usage", 95.0)
        db.commit()
        return dev, rule, event

    def _notified(self, db: Session, rule: AlertRule, device: MonDevice,
                  event: AlertEvent, n: int) -> None:
        """
        制造 n 条**已推送过的告警事件**（同一指纹），用于喂限流计数。

        ⚠ 不能写成"给同一个事件插 n 条通知记录"：
        计数口径是 COUNT(DISTINCT event_id)，插 100 条记录也只算 1 条。
        第一版测试就是这么写的，结果 measure 不出上限 ——
        而真实场景里"同一指纹下的多条事件"要靠"恢复 → 再次触发"产生，
        所以这里也必须走同一条路径去造数据。
        """
        current = event
        for i in range(n):
            if i > 0:
                alert_service.recover_event(db, current)
                db.commit()
                current = alert_service.create_event(db, rule, device, "cpu_usage", 95.0)
                db.commit()
            db.add(AlertNotifyRecord(
                event_id=current.id, channel="email", status=NOTIFY_SUCCESS,
                created_at=datetime.now(),
            ))
        db.commit()

    def test_未达上限允许推送(self, mem_db):
        dev, rule, event = self._setup(mem_db, converge_window=300, converge_max=3)
        self._notified(mem_db, rule, dev, event, 2)

        decision = converger.decide(mem_db, rule=rule, event=event)

        assert decision.allowed is True
        assert decision.window_count == 2

    def test_达到上限则收敛(self, mem_db):
        dev, rule, event = self._setup(mem_db, converge_window=300, converge_max=2)
        self._notified(mem_db, rule, dev, event, 2)

        decision = converger.decide(mem_db, rule=rule, event=event)

        assert decision.allowed is False
        assert decision.action == ALARM_ACTION_CONVERGE
        assert decision.window_count == 2
        assert "2 次" in decision.reason and "上限 2 次" in decision.reason

    def test_关闭收敛的规则不做限流(self, mem_db):
        """converge_window=0 表示该规则不做窗口收敛（前端有对应开关说明）"""
        dev, rule, event = self._setup(mem_db, converge_window=0, converge_max=0)
        self._notified(mem_db, rule, dev, event, 50)

        assert converger.decide(mem_db, rule=rule, event=event).allowed is True

    def test_静默期内抑制推送(self, mem_db):
        """维护窗口：仍评估、仍记录事件，只是不推送"""
        _dev, rule, event = self._setup(mem_db, silence_until=datetime.now() + timedelta(hours=1))

        decision = converger.decide(mem_db, rule=rule, event=event)

        assert decision.allowed is False
        assert decision.action == ALARM_ACTION_SUPPRESS
        assert "静默期" in decision.reason

    def test_静默期已过则正常推送(self, mem_db):
        _dev, rule, event = self._setup(mem_db, silence_until=datetime.now() - timedelta(minutes=1))

        assert converger.decide(mem_db, rule=rule, event=event).allowed is True

    def test_恢复通知跳过窗口限流(self, mem_db):
        """
        "故障恢复了"是运维最需要知道的消息，被限量压掉会造成误判。
        """
        dev, rule, event = self._setup(mem_db, converge_window=300, converge_max=1)
        self._notified(mem_db, rule, dev, event, 10)

        decision = converger.decide(mem_db, rule=rule, event=event, is_recovery=True)

        assert decision.allowed is True

    def test_恢复通知仍受静默期约束(self, mem_db):
        """维护窗口内确实不需要知道恢复消息"""
        _dev, rule, event = self._setup(mem_db, silence_until=datetime.now() + timedelta(hours=1))

        decision = converger.decide(mem_db, rule=rule, event=event, is_recovery=True)

        assert decision.allowed is False
        assert decision.action == ALARM_ACTION_SUPPRESS

    def test_恢复通知优先级高于窗口限流(self, mem_db):
        """两种抑制同时存在时，恢复通知应按窗口限流的口径放行"""
        dev, rule, event = self._setup(
            mem_db, converge_window=60, converge_max=1,
            silence_until=datetime.now() - timedelta(minutes=1),
        )
        self._notified(mem_db, rule, dev, event, 5)

        assert converger.decide(mem_db, rule=rule, event=event, is_recovery=True).allowed

    def test_位置参数传反会立刻报错(self, mem_db):
        """
        ★ 回归守卫（本轮真实缺陷）。

        `rule` 与 `event` 都是 ORM 对象，位置传反 Python 不会报任何错：
        `rule.is_silenced()` 作用在 AlertEvent 上抛 AttributeError，
        而 dispatch() 的最外层 try/except 会把它吞掉、只记一条日志 ——
        现象是"平台一切正常，但永远没人收到告警"，属于最难查的一类故障。

        因此这两个参数被设计成**强制关键字传参**（签名里有 `*`），
        写反会在第一次调用时立刻 TypeError。本用例守住这个约束。
        """
        _dev, rule, event = self._setup(mem_db)

        with pytest.raises(TypeError):
            converger.decide(mem_db, event, rule)      # type: ignore[misc]

        # 正确写法必须能过
        assert converger.decide(mem_db, rule=rule, event=event).allowed is True

    def test_规则策略描述可读(self, mem_db):
        """
        "为什么这次只收到一条告警"是现场最常被问到的问题，
        策略原文写进告警时间线比让实施人员解释快得多。
        """
        _dev, rule, _event = self._setup(
            mem_db, duration_n=3, recovery_n=2, converge_window=600, converge_max=5
        )

        text = converger.describe_rule(rule)

        assert "连续 3 次超标触发" in text
        assert "连续 2 次正常恢复" in text
        assert "600 秒内最多推送 5 条" in text

    def test_未启用收敛时描述为不做窗口收敛(self, mem_db):
        _dev, rule, _event = self._setup(mem_db, converge_window=0, converge_max=0)
        assert "不做窗口收敛" in converger.describe_rule(rule)

    def test_静默期写进策略描述(self, mem_db):
        _dev, rule, _event = self._setup(
            mem_db, silence_until=datetime.now() + timedelta(hours=2)
        )
        assert "静默期至" in converger.describe_rule(rule)


# ============================================================================
# 八、消息构造与排版
# ============================================================================

class TestBuildMessage:
    """事件 + 规则 + 设备 → 与渠道无关的消息体"""

    def _event(self, db: Session, **rule_overrides: Any):
        dev = make_device(db)
        rule = make_rule(db, duration_n=1, **rule_overrides)
        event = alert_service.create_event(db, rule, dev, "cpu_usage", 93.5,
                                          metric_name="CPU使用率")
        db.commit()
        return dev, rule, event

    def test_单位来自唯一来源(self, mem_db):
        """邮件写 93.5%、企微写 93.5ms 这类不一致由这张单位表兜住"""
        dev, rule, event = self._event(mem_db)
        msg = notifier.build_message(event, rule, dev)
        assert msg.metric_value_text == "93.5%"
        assert msg.threshold_text == "大于 85%"

    def test_无单位的指标不拼单位(self, mem_db):
        dev = make_device(mem_db)
        rule = make_rule(mem_db, metric_key="online_status", operator="eq",
                         threshold=0, duration_n=1)
        event = alert_service.create_event(mem_db, rule, dev, "online_status", 0.0,
                                          metric_name="在线状态")
        mem_db.commit()

        msg = notifier.build_message(event, rule, dev)

        assert msg.metric_value_text == "0"

    def test_指标值为None显示无法获取(self, mem_db):
        dev, rule, event = self._event(mem_db)
        event.metric_value = None
        assert notifier.build_message(event, rule, dev).metric_value_text == "无法获取"

    def test_设备信息缺失时不崩(self, mem_db):
        """事件里冗余了设备名/IP，设备被删掉后仍要能发出告警"""
        _dev, rule, event = self._event(mem_db)
        msg = notifier.build_message(event, rule, None)
        assert msg.device_name  # 用事件里的冗余字段兜底
        assert msg.device_ip

    def test_恢复标记显式传入优先于事件状态(self, mem_db):
        """
        恢复通知的构造发生在 recover_event() **之前**，
        此时 event.status 还是 firing，靠状态推断会得到"触发通知"的错误文案。
        """
        dev, rule, event = self._event(mem_db)
        event.status = EVENT_STATUS_FIRING

        msg = notifier.build_message(event, rule, dev, is_recovery=True)

        assert msg.is_recovery is True
        assert "恢复" in msg.action_text

    def test_收件人字段来自平台配置(self, mem_db):
        dev, rule, event = self._event(mem_db)
        msg = notifier.build_message(event, rule, dev,
                                    platform_name="某客户运维平台",
                                    platform_url="http://10.0.0.5:8000")
        assert msg.platform_name == "某客户运维平台"
        assert msg.platform_url == "http://10.0.0.5:8000"

    def test_持续时长中文口语化(self):
        """与 schemas/alert.py 的 duration_text 口径一致，两处不一致会让人怀疑数据"""
        start = datetime(2026, 9, 21, 10, 0, 0)
        assert notifier._duration_text(start, start + timedelta(seconds=45)) == "45秒"
        assert notifier._duration_text(start, start + timedelta(minutes=12)) == "12分钟"
        assert notifier._duration_text(start, start + timedelta(hours=2, minutes=13)) == "2小时13分"
        assert notifier._duration_text(start, start + timedelta(days=1, hours=4)) == "1天4小时"

    def test_持续时长起点缺失返回空(self):
        assert notifier._duration_text(None, datetime.now()) == ""

    def test_持续时长为负返回空(self):
        """设备时间被改过时不该显示「-3分钟」"""
        now = datetime(2026, 9, 21, 10, 0, 0)
        assert notifier._duration_text(now, now - timedelta(minutes=3)) == ""


class TestAlertMessageFormat:
    """两种排版 + 邮件标题"""

    def test_纯文本含全部关键字段(self, sample_msg):
        text = sample_msg.as_text()

        assert "【告警通知】" in text
        assert "设备名称：核心交换机01" in text
        assert "10.10.0.1" in text
        assert "93.5%" in text
        assert "大于 85%" in text
        assert "重要" in text
        assert sample_msg.event_no in text
        assert "http://10.10.0.100:8000" in text
        assert "亿网通IT运维监控管理平台" in text

    def test_触发不含恢复字段(self, sample_msg):
        text = sample_msg.as_text()
        assert "告警时间：" in text
        assert "恢复时间：" not in text
        assert "持续时长：" not in text

    def test_恢复排版含恢复与持续时长(self, sample_msg):
        sample_msg.is_recovery = True
        sample_msg.duration_text = "2小时13分"
        text = sample_msg.as_text()

        assert "【告警恢复】" in text
        assert "恢复时间：" in text
        assert "持续时长：2小时13分" in text
        assert "告警时间：" not in text

    def test_重复发生的告警显示累计次数(self, sample_msg):
        sample_msg.occur_count = 4
        assert "累计发生：4 次" in sample_msg.as_text()

    def test_平台地址为空时不显示该行(self, sample_msg):
        """
        留空时通知里不出现"前往平台"链接 ——
        给一个连不上的地址更糟（运维会真的去点）。
        """
        sample_msg.platform_url = ""
        assert "平台地址：" not in sample_msg.as_text()
        assert "]" not in sample_msg.as_markdown().split("###")[-1].split("事件编号")[-1]

    def test_空字段不产生空行(self, sample_msg):
        sample_msg.device_type = ""
        sample_msg.location = ""
        text = sample_msg.as_text()
        assert "设备类型：" not in text
        assert "位置　　：" not in text

    def test_markdown只用两家机器人支持的子集(self, sample_msg):
        """
        企业微信与钉钉的 markdown 都只支持子集（不支持表格、图片语法），
        因此统一只用「标题 + 引用 + 加粗 + font color」。
        """
        md = sample_msg.as_markdown()

        assert md.startswith("### 🟠")
        assert "> **设备**：核心交换机01（10.10.0.1）" in md
        assert '<font color="comment">93.5%</font>' in md
        assert "|" not in md, "不能用表格语法，两家都不渲染"

    def test_markdown恢复排版(self, sample_msg):
        sample_msg.is_recovery = True
        sample_msg.duration_text = "8分钟"
        md = sample_msg.as_markdown()
        assert "**持续时长**：8分钟" in md
        assert "**恢复时间**" in md

    @pytest.mark.parametrize("level,icon,color", [
        ("critical", "🔴", "warning"),
        ("major", "🟠", "comment"),
        ("minor", "🟡", "comment"),
        ("info", "🔵", "info"),
    ])
    def test_级别图标与颜色(self, sample_msg, level, icon, color):
        sample_msg.level = level
        assert sample_msg.level_icon == icon
        assert f'<font color="{color}">' in sample_msg.as_markdown()

    def test_未知级别不崩(self, sample_msg):
        """库里一条字典外的历史级别，不能让整条告警发不出去"""
        sample_msg.level = "catastrophic"
        assert sample_msg.level_text == "catastrophic"
        assert sample_msg.level_icon == "⚪"
        assert sample_msg.as_markdown()
        assert sample_msg.as_text()

    def test_邮件标题带级别前缀(self, sample_msg):
        assert sample_msg.subject().startswith("[重要]")
        sample_msg.is_recovery = True
        assert sample_msg.subject().startswith("[恢复][重要]")

    def test_邮件标题超长截断(self, sample_msg):
        """「主题过长」会让部分邮件客户端在列表里显示不全，截断比溢出好"""
        sample_msg.title = "极长的告警标题" * 30
        assert len(sample_msg.subject(max_len=60)) == 60


# ============================================================================
# 九、渠道通用助手
# ============================================================================

class TestChannelHelpers:
    """脱敏、字节截断、结果构造 —— 三个所有渠道共用、且都容易写错的地方"""

    @pytest.mark.parametrize("url,secret_part", [
        ("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc123", "key=abc123"),
        ("https://oapi.dingtalk.com/robot/send?access_token=deadbeef", "access_token=deadbeef"),
        ("https://example.com/hook?secret=xyz&t=1", "secret=xyz"),
        ("https://example.com/hook?token=xyz", "token=xyz"),
    ])
    def test_敏感参数被脱敏(self, url, secret_part):
        """
        企业微信/钉钉的 webhook 地址本身就等价于密钥，
        谁能看到它谁就能往群里发消息；而"推送记录"是运维天天要看的页面。
        """
        masked = BaseChannel._mask_url(url)
        assert secret_part not in masked
        assert "***" in masked
        assert masked.startswith("https://"), "域名与路径要保留，否则无法排障"

    def test_无敏感参数时原样返回(self):
        url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"
        assert BaseChannel._mask_url(url) == url

    def test_空地址返回空串(self):
        assert BaseChannel._mask_url(None) == ""
        assert BaseChannel._mask_url("") == ""

    def test_超长地址截断到200字符(self):
        assert len(BaseChannel._mask_url("https://x.com/" + "a" * 500)) <= 200

    def test_短文本不截断(self):
        text = "短消息"
        assert BaseChannel._truncate(text) == text

    def test_长中文按字节截断且不超限(self):
        """
        企业微信限制 4096 字节、一个中文 3 字节。
        按字符截断会在纯中文长消息上超限，渠道直接返回 400 ——
        整条告警推不出去，比截断糟糕得多。
        """
        out = BaseChannel._truncate("中" * 5000, 4000)

        assert len(out.encode("utf-8")) <= 4000
        assert out.endswith("（内容过长已截断）")

    def test_多字节边界不产生乱码(self):
        """
        截断点落在三字节字符中间时必须回退，
        否则 decode 会抛 UnicodeDecodeError，整条告警发不出去。
        """
        for n in range(1300, 1340):
            out = BaseChannel._truncate("中" * n, 4000)
            assert len(out.encode("utf-8")) <= 4000
            out.encode("utf-8").decode("utf-8")   # 能来回编码即为合法字符串

    def test_混合中英文也守限额(self):
        out = BaseChannel._truncate("a中b文c" * 800, 2000)
        assert len(out.encode("utf-8")) <= 2000

    def test_自定义限额生效(self):
        out = BaseChannel._truncate("x" * 500, 100)
        assert len(out.encode("utf-8")) <= 100

    def test_结果对象可用作布尔判断(self):
        assert bool(ChannelResult(success=True)) is True
        assert bool(ChannelResult(success=False)) is False

    def test_配置问题默认不可重试(self):
        """重试 2 次也不会让空地址变得非空"""
        channel = ChannelRegistry.get("email")
        result = channel.fail("未填写收件人")
        assert result.success is False
        assert result.retryable is False

    @pytest.mark.parametrize("raw,expected", [
        ("1", True), ("0", False), ("true", True), ("False", False),
        ("yes", True), ("on", True), ("启用", True), ("", False), (None, False),
    ])
    def test_启用开关兼容多种写法(self, raw, expected):
        channel = ChannelRegistry.get("email")
        assert channel.enabled({"enabled": raw}) is expected


class TestChannelRegistry:
    """注册器：新渠道"自己报名"，工厂与调度器零改动"""

    def test_四个内置渠道都已注册(self):
        assert set(ChannelRegistry.available()) >= {"email", "wecom", "dingtalk", "wechat"}

    def test_渠道清单供前端下拉框使用(self):
        desc = ChannelRegistry.describe()
        by_key = {d["key"]: d for d in desc}
        assert by_key["email"]["text"] == "邮件"
        assert by_key["email"]["config_group"] == "mail", \
            "邮件的配置分组是 mail（与 sys_config 的既有命名保持一致）"
        assert by_key["wechat"]["config_group"] == "wechat"

    def test_标识大小写与空格容错(self):
        assert ChannelRegistry.get("  EMAIL ") is not None

    def test_未知渠道返回None(self):
        assert ChannelRegistry.get("feishu") is None

    def test_空标识注册被拒绝(self):
        with pytest.raises(ValueError):
            ChannelRegistry.register("")(BaseChannel)


# ============================================================================
# 十、邮件渠道
# ============================================================================

class FakeSmtp:
    """
    假 SMTP 服务器：记录调用，可按需注入失败。

    通过 `behavior` 控制行为，让"认证失败/网络不可达/部分收件人被拒"
    这些分支可以在不联外网的情况下逐个验证。
    """

    instances: List["FakeSmtp"] = []

    def __init__(self, host: str, port: int, timeout: Any = None,
                 **behavior: Any) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.behavior = behavior
        self.logged: Optional[Tuple[str, str]] = None
        self.starttls_called = False
        self.quit_called = False
        self.mail: Optional[Dict[str, Any]] = None
        FakeSmtp.instances.append(self)
        if behavior.get("raise_at") == "connect":
            raise behavior["exc"]

    def starttls(self) -> None:
        self.starttls_called = True

    def login(self, user: str, password: str) -> None:
        if self.behavior.get("raise_at") == "login":
            raise self.behavior["exc"]
        self.logged = (user, password)

    def sendmail(self, sender: str, receivers: List[str], body: str) -> Dict[str, Any]:
        if self.behavior.get("raise_at") == "sendmail":
            raise self.behavior["exc"]
        self.mail = {"sender": sender, "receivers": list(receivers), "body": body}
        return dict(self.behavior.get("refused") or {})

    def quit(self) -> None:
        self.quit_called = True


@pytest.fixture()
def fake_smtp(monkeypatch):
    """
    装假 SMTP。`behavior` 字典由用例按需改写：

        behavior["raise_at"] = "connect" | "login" | "sendmail"
        behavior["exc"]      = 要抛的异常实例
        behavior["refused"]  = sendmail 返回的拒收表
    """
    FakeSmtp.instances.clear()
    behavior: Dict[str, Any] = {}

    def _factory(host: str, port: int, timeout: Any = None) -> FakeSmtp:
        return FakeSmtp(host, port, timeout, **behavior)

    monkeypatch.setattr(smtplib, "SMTP_SSL", _factory)
    monkeypatch.setattr(smtplib, "SMTP", _factory)
    yield behavior
    FakeSmtp.instances.clear()


class TestEmailChannel:
    """邮件渠道：配置检查、收件人解析、发送分支"""

    @pytest.fixture()
    def channel(self) -> EmailChannel:
        return ChannelRegistry.get("email")  # type: ignore[return-value]

    # ---------------- 配置检查 ----------------

    def test_未启用时给出开关名(self, channel, mail_cfg):
        mail_cfg["enabled"] = "0"
        ok, reason = channel.check(mail_cfg)
        assert ok is False
        assert "未启用" in reason and "mail.enabled" in reason

    @pytest.mark.parametrize("field,keyword", [
        ("smtp_host", "SMTP 服务器地址"),
        ("sender", "发件人地址"),
        ("smtp_user", "SMTP 账号"),
    ])
    def test_缺失项给出具体字段名(self, channel, mail_cfg, field, keyword):
        """
        现场 90% 的"告警没收到"都是邮件配置问题，而配置页有 8 个输入框。
        笼统回"配置不完整"会让运维挨个试一遍。
        """
        mail_cfg[field] = ""
        ok, reason = channel.check(mail_cfg)
        assert ok is False
        assert keyword in reason

    def test_收件人为空(self, channel, mail_cfg):
        mail_cfg["receivers"] = "[]"
        ok, reason = channel.check(mail_cfg)
        assert ok is False and "收件人" in reason

    def test_口令缺失提示含密钥更换(self, channel, mail_cfg):
        """口令是加密存储的，FERNET_KEY 变更会导致解密失败，也要能解释"""
        mail_cfg["smtp_password"] = ""
        ok, reason = channel.check(mail_cfg)
        assert ok is False
        assert "授权码" in reason and "密钥已更换" in reason

    def test_端口非数字(self, channel, mail_cfg):
        mail_cfg["smtp_port"] = "abc"
        ok, reason = channel.check(mail_cfg)
        assert ok is False and "不是数字" in reason

    def test_端口超范围(self, channel, mail_cfg):
        mail_cfg["smtp_port"] = "70000"
        ok, reason = channel.check(mail_cfg)
        assert ok is False and "超出范围" in reason

    def test_配置齐全通过(self, channel, mail_cfg):
        assert channel.check(mail_cfg) == (True, "")

    # ---------------- 收件人解析 ----------------

    @pytest.mark.parametrize("raw,expected_count", [
        ('["a@x.com", "b@x.com"]', 2),                    # 前端保存的标准格式
        ("a@x.com,b@x.com", 2),                           # 逗号分隔
        ("a@x.com; b@x.com\nc@x.com", 3),                 # 从 Excel 粘进来
        ('["a@x.com", "b@x.com"', 2),                     # JSON 坏了仍要兜底解析
        (["a@x.com", "b@x.com"], 2),                       # 已经是列表
        ("", 0),
        (None, 0),
    ])
    def test_收件人兼容多种写法(self, channel, raw, expected_count):
        assert len(channel._parse_receivers(raw)) == expected_count

    def test_收件人去重前后的空项被清理(self, channel):
        assert channel._parse_receivers("a@x.com, ,b@x.com") == ["a@x.com", "b@x.com"]

    # ---------------- 发送 ----------------

    def test_配置不全时不发起网络请求(self, channel, sample_msg, mail_cfg, fake_smtp):
        """
        配置问题必须"连都不连"就返回，否则每条告警都要白等 15 秒超时。
        """
        mail_cfg["enabled"] = "0"
        result = channel.send(sample_msg, mail_cfg)
        assert result.success is False
        assert result.retryable is False, "配置问题重试无用"
        assert FakeSmtp.instances == [], "不该建立 SMTP 连接"

    def test_发送成功(self, channel, sample_msg, mail_cfg, fake_smtp):
        result = channel.send(sample_msg, mail_cfg)

        assert result.success is True
        assert result.target == "ops@enetop.com,net@enetop.com"
        assert "2 个收件人" in result.message
        server = FakeSmtp.instances[0]
        assert server.host == "smtp.exmail.qq.com" and server.port == 465
        assert server.logged == ("monitor@enetop.com", "AUTHCODE")
        assert server.timeout == 15, "必须有超时，否则邮件服务器不可达时会永久占住线程"
        assert server.quit_called, "发完必须 QUIT，否则服务器上残留大量半开会话"
        assert server.mail is not None
        assert server.mail["receivers"] == ["ops@enetop.com", "net@enetop.com"]

    def test_认证失败不可重试(self, channel, sample_msg, mail_cfg, fake_smtp):
        fake_smtp["raise_at"] = "login"
        fake_smtp["exc"] = smtplib.SMTPAuthenticationError(535, b"auth failed")

        result = channel.send(sample_msg, mail_cfg)

        assert result.success is False
        assert result.retryable is False
        assert "授权码" in result.message, "最常见原因是用了登录密码而不是授权码"

    def test_全部收件人被拒不可重试(self, channel, sample_msg, mail_cfg, fake_smtp):
        fake_smtp["raise_at"] = "sendmail"
        fake_smtp["exc"] = smtplib.SMTPRecipientsRefused({"a@x.com": (550, b"no such user")})

        result = channel.send(sample_msg, mail_cfg)

        assert result.success is False and result.retryable is False

    def test_网络错误可重试(self, channel, sample_msg, mail_cfg, fake_smtp):
        fake_smtp["raise_at"] = "connect"
        fake_smtp["exc"] = OSError("connect timeout")

        result = channel.send(sample_msg, mail_cfg)

        assert result.success is False
        assert result.retryable is True, "网络类错误才值得重试"

    def test_部分收件人被拒仍算成功(self, channel, sample_msg, mail_cfg, fake_smtp):
        """
        少数地址被拒（写错、已离职）不该让整条记录判失败，
        但必须写进返回值 —— 否则运维不知道谁没收到。
        """
        fake_smtp["refused"] = {"b@x.com": (550, b"user unknown")}

        result = channel.send(sample_msg, mail_cfg)

        assert result.success is True
        assert "部分收件人被拒" in result.message
        assert "b@x.com" in result.message

    def test_587端口自动升级STARTTLS(self, channel, sample_msg, mail_cfg, fake_smtp):
        """
        587 是"提交端口"，必须先明文握手再 STARTTLS 升级。
        把它当明文处理会在 send 时报 "SMTP AUTH extension not supported"。
        """
        mail_cfg["use_ssl"] = "0"
        mail_cfg["smtp_port"] = "587"

        result = channel.send(sample_msg, mail_cfg)

        assert result.success is True
        assert FakeSmtp.instances[0].starttls_called is True

    def test_非587端口不做STARTTLS(self, channel, sample_msg, mail_cfg, fake_smtp):
        mail_cfg["use_ssl"] = "0"
        mail_cfg["smtp_port"] = "25"

        channel.send(sample_msg, mail_cfg)

        assert FakeSmtp.instances[0].starttls_called is False

    def test_收件人数量有上限(self, channel, sample_msg, mail_cfg, fake_smtp):
        """
        防止有人把整个公司通讯录粘进配置框，
        导致一次告警发几百封邮件被服务商当成垃圾邮件源封禁。
        """
        mail_cfg["receivers"] = ",".join(f"u{i}@x.com" for i in range(200))

        result = channel.send(sample_msg, mail_cfg)

        assert len(FakeSmtp.instances[0].mail["receivers"]) <= 50
        assert result.success is True

    # ---------------- 报文构造 ----------------

    def test_同时包含纯文本与HTML两部分(self, channel, sample_msg):
        """
        邮件客户端优先显示 HTML（表格化、按级别着色），
        但短信网关/工单系统/老旧终端只认纯文本，只发 HTML 会让它们看到一堆标签。
        """
        mail = channel._build_mime(sample_msg, "monitor@enetop.com", ["ops@enetop.com"])
        parts = [p.get_content_type() for p in mail.walk()]

        assert mail.get_content_subtype() == "alternative"
        assert "text/plain" in parts
        assert "text/html" in parts

    def test_中文主题按UTF8编码(self, channel, sample_msg):
        """
        中文主题是乱码的重灾区。

        这里断言的不是"主题里能看到中文" —— 正确做法是 RFC 2047 编码
        （`=?utf-8?b?...?=`），所以字面量里本来就不该出现中文。
        真正要验证的是"**能被解码回原文**"，那才是"不乱码"的定义。
        """
        mail = channel._build_mime(sample_msg, "monitor@enetop.com", ["ops@enetop.com"])
        subject = mail["Subject"]

        assert "utf-8" in subject.lower(), "必须带 utf-8 编码标记"
        decoded = "".join(
            part.decode(enc or "utf-8") if isinstance(part, bytes) else part
            for part, enc in decode_header(subject)
        )
        assert "重要" in decoded
        assert "核心交换机01" in decoded

    def test_自定义头便于邮箱过滤(self, channel, sample_msg):
        """运维常用"按规则过滤/归档"来处理告警邮件"""
        mail = channel._build_mime(sample_msg, "monitor@enetop.com", ["ops@enetop.com"])

        assert mail["X-Alert-Event"] == sample_msg.event_no
        assert mail["X-Alert-Level"] == "major"

    def test_HTML只用内联样式与表格(self, channel, sample_msg):
        """
        企业邮箱客户端（Outlook、Foxmail、企业微信内置邮箱）对现代 CSS 支持极差，
        用 flex/grid 排版的典型结果是"所有内容挤成一坨"。
        """
        html = channel._html_body(sample_msg)

        assert "<table" in html
        assert "<style" not in html
        assert "display:flex" not in html and "grid" not in html

    def test_HTML恢复版配色切换(self, channel, sample_msg):
        sample_msg.is_recovery = True
        html = channel._html_body(sample_msg)
        assert "告警恢复" in html and "#EAF7EE" in html

    def test_平台地址为空时HTML不含链接(self, channel, sample_msg):
        sample_msg.platform_url = ""
        assert "<a href" not in channel._html_body(sample_msg)


# ============================================================================
# 十一、企业微信群机器人渠道
# ============================================================================

class TestWecomChannel:

    @pytest.fixture()
    def channel(self) -> WecomChannel:
        return ChannelRegistry.get("wecom")  # type: ignore[return-value]

    @pytest.fixture()
    def cfg(self) -> Dict[str, Any]:
        return {
            "enabled": "1",
            "webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc123",
            "msg_type": "markdown",
        }

    @pytest.fixture()
    def posted(self, monkeypatch):
        """接管 HTTP 调用，记录请求体并返回预设响应"""
        calls: List[Dict[str, Any]] = []
        holder: Dict[str, Any] = {
            "resp": {"_http_ok": True, "_data": {"errcode": 0, "errmsg": "ok"}}
        }

        def _fake_post(url, payload, timeout=10):
            calls.append({"url": url, "payload": payload, "timeout": timeout})
            return holder["resp"]

        monkeypatch.setattr(BaseChannel, "_post_json", staticmethod(_fake_post))
        return SimpleNamespace(calls=calls, holder=holder)

    # ---------------- 配置检查 ----------------

    def test_未启用(self, channel, cfg):
        cfg["enabled"] = "0"
        ok, reason = channel.check(cfg)
        assert ok is False and "未启用" in reason

    def test_未填webhook(self, channel, cfg):
        cfg["webhook"] = ""
        ok, reason = channel.check(cfg)
        assert ok is False and "Webhook" in reason

    def test_必须https(self, channel, cfg):
        """
        企业微信只接受 https，填成 http 会在发送时被重定向后失败，
        提前拦下来能省一次无效的网络往返。
        """
        cfg["webhook"] = "http://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
        ok, reason = channel.check(cfg)
        assert ok is False and "https" in reason

    def test_缺少key参数说明复制不完整(self, channel, cfg):
        cfg["webhook"] = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"
        ok, reason = channel.check(cfg)
        assert ok is False and "key" in reason and "不完整" in reason

    def test_配置齐全通过(self, channel, cfg):
        assert channel.check(cfg) == (True, "")

    # ---------------- 发送 ----------------

    def test_发送成功且目标已脱敏(self, channel, cfg, sample_msg, posted):
        result = channel.send(sample_msg, cfg)

        assert result.success is True
        assert "abc123" not in result.target, "webhook 里的 key 等价于密钥，不能出现在记录里"
        assert "***" in result.target
        assert posted.calls[0]["url"] == cfg["webhook"]
        assert posted.calls[0]["payload"]["msgtype"] == "markdown"

    def test_配置不全时不发起网络请求(self, channel, cfg, sample_msg, posted):
        cfg["enabled"] = "0"
        result = channel.send(sample_msg, cfg)
        assert result.success is False and result.retryable is False
        assert posted.calls == []

    def test_text模式使用纯文本排版(self, channel, cfg, sample_msg, posted):
        cfg["msg_type"] = "text"
        channel.send(sample_msg, cfg)
        payload = posted.calls[0]["payload"]
        assert payload["msgtype"] == "text"
        assert "【告警通知】" in payload["text"]["content"]

    def test_异常msg_type退化为markdown(self, channel, cfg, sample_msg, posted):
        """配置里填了别的值时退化，而不是直接失败"""
        cfg["msg_type"] = "card"
        result = channel.send(sample_msg, cfg)
        assert result.success is True
        assert posted.calls[0]["payload"]["msgtype"] == "markdown"

    def test_超长消息被截断(self, channel, cfg, sample_msg, posted):
        sample_msg.title = "超长标题" * 2000
        sample_msg.content = "详情" * 3000

        channel.send(sample_msg, cfg)

        content = posted.calls[0]["payload"]["markdown"]["content"]
        assert len(content.encode("utf-8")) <= MAX_MESSAGE_BYTES

    def test_请求超时短于邮件(self, channel, cfg, sample_msg, posted):
        """机器人不可达时应快速失败，让邮件/钉钉渠道尽早开始"""
        channel.send(sample_msg, cfg)
        assert posted.calls[0]["timeout"] == 8

    # ---------------- 响应解读 ----------------

    def test_errcode为0即成功(self):
        assert WecomChannel._interpret(
            {"_http_ok": True, "_data": {"errcode": 0}}, "t"
        ).success is True

    def test_网络层失败可重试(self):
        result = WecomChannel._interpret({"_http_ok": False, "_error": "网络不可达"}, "t")
        assert result.success is False and result.retryable is True
        assert "请求失败" in result.message

    def test_响应不是JSON提示可能被代理拦截(self):
        """部分反向代理/上网行为管理会返回 HTML 错误页"""
        result = WecomChannel._interpret(
            {"_http_ok": True, "_raw": "<html>403</html>"}, "t"
        )
        assert result.success is False and result.retryable is True
        assert "代理" in result.message

    def test_机器人被移出群聊不可重试(self):
        """2003 是现场出现频率最高的错误，必须给出可操作提示"""
        result = WecomChannel._interpret(
            {"_http_ok": True, "_data": {"errcode": 2003, "errmsg": "invalid webhook url"}}, "t"
        )
        assert result.success is False
        assert result.retryable is False
        assert "重新添加" in result.message

    def test_webhook失效不可重试(self):
        result = WecomChannel._interpret(
            {"_http_ok": True, "_data": {"errcode": 93000}}, "t"
        )
        assert result.retryable is False and "失效" in result.message

    def test_频率超限可重试(self):
        result = WecomChannel._interpret(
            {"_http_ok": True, "_data": {"errcode": 45009, "errmsg": "api freq out of limit"}}, "t"
        )
        assert result.retryable is True and "频率超限" in result.message

    def test_未知错误码保留原文(self):
        result = WecomChannel._interpret(
            {"_http_ok": True, "_data": {"errcode": 40001, "errmsg": "invalid credential"}}, "t"
        )
        assert result.retryable is True
        assert "40001" in result.message and "invalid credential" in result.message

    def test_errcode缺失视为成功(self):
        """企业微信成功时也可能不带 errcode，默认 0 才不会误判失败"""
        assert WecomChannel._interpret({"_http_ok": True, "_data": {}}, "t").success is True


# ============================================================================
# 十二、钉钉群机器人渠道（加签）
# ============================================================================

class TestDingtalkChannel:

    @pytest.fixture()
    def channel(self) -> DingtalkChannel:
        return ChannelRegistry.get("dingtalk")  # type: ignore[return-value]

    @pytest.fixture()
    def cfg(self) -> Dict[str, Any]:
        return {
            "enabled": "1",
            "webhook": "https://oapi.dingtalk.com/robot/send?access_token=deadbeef",
            "secret": "SEC0123456789abcdef",
        }

    @pytest.fixture()
    def posted(self, monkeypatch):
        calls: List[Dict[str, Any]] = []
        holder: Dict[str, Any] = {
            "resp": {"_http_ok": True, "_data": {"errcode": 0, "errmsg": "ok"}}
        }

        def _fake_post(url, payload, timeout=10):
            calls.append({"url": url, "payload": payload})
            return holder["resp"]

        monkeypatch.setattr(BaseChannel, "_post_json", staticmethod(_fake_post))
        return SimpleNamespace(calls=calls, holder=holder)

    # ---------------- 配置检查 ----------------

    def test_未启用(self, channel, cfg):
        cfg["enabled"] = "0"
        ok, reason = channel.check(cfg)
        assert ok is False and "未启用" in reason

    def test_未填webhook(self, channel, cfg):
        cfg["webhook"] = ""
        assert channel.check(cfg)[0] is False

    def test_必须https(self, channel, cfg):
        cfg["webhook"] = "http://oapi.dingtalk.com/robot/send?access_token=x"
        ok, reason = channel.check(cfg)
        assert ok is False and "https" in reason

    def test_缺少access_token(self, channel, cfg):
        cfg["webhook"] = "https://oapi.dingtalk.com/robot/send"
        ok, reason = channel.check(cfg)
        assert ok is False and "access_token" in reason

    def test_加签密钥必须以SEC开头(self, channel, cfg):
        """
        常见错误：把 webhook 里的 access_token 粘到了 secret 里。
        填了非 SEC 开头的内容说明复制错了地方。
        """
        cfg["secret"] = "deadbeef"
        ok, reason = channel.check(cfg)
        assert ok is False and "SEC" in reason

    def test_不填密钥时支持关键词或IP白名单(self, channel, cfg):
        cfg["secret"] = ""
        assert channel.check(cfg) == (True, "")

    def test_配置齐全通过(self, channel, cfg):
        assert channel.check(cfg) == (True, "")

    # ---------------- 加签算法 ----------------

    def test_加签算法与官方一致(self, channel, monkeypatch):
        """
        这里**刻意重写一遍**官方算法做独立校验（而不是调用被测代码）：

        算法有两个容易踩的坑 ——
          坑 1：`string_to_sign` 中间的 `\\n` 不可省；
          坑 2：`sign` 必须做 URL 编码（它包含 + / = 等字符）。
        从被测代码里取期望值等于自证，重写一遍才能真正发现"少了个换行"。
        """
        monkeypatch.setattr(dingtalk_mod.time, "time", lambda: 1758432000.123)

        url = channel._signed_url("https://oapi.dingtalk.com/robot/send?access_token=abc",
                                  "SECxxx")

        timestamp = "1758432000123"                    # 必须是毫秒
        digest = hmac.new(
            b"SECxxx", f"{timestamp}\nSECxxx".encode("utf-8"), digestmod=hashlib.sha256
        ).digest()
        expected_sign = urllib.parse.quote_plus(base64.b64encode(digest).decode("utf-8"))

        assert f"timestamp={timestamp}" in url
        assert f"sign={expected_sign}" in url

    def test_加签结果已URL编码(self, channel):
        """签名里含 + / = 等字符，不编码会得到 310000"""
        url = channel._signed_url("https://oapi.dingtalk.com/robot/send?access_token=abc",
                                  "SECxxx")
        sign = url.split("sign=")[1]
        assert "+" not in sign and "/" not in sign and "=" not in sign

    def test_每次调用时间戳都重新取(self, channel, monkeypatch):
        """
        钉钉校验"与服务器时间差 < 1 小时"，缓存 timestamp 会让机器人在
        运行一小时后突然全部发送失败，且报错是 310000（与"关键词不匹配"同码，极难定位）。
        """
        monkeypatch.setattr(dingtalk_mod.time, "time", lambda: 1758432000.0)
        url1 = channel._signed_url("https://x.com/hook?access_token=a", "SECk")
        monkeypatch.setattr(dingtalk_mod.time, "time", lambda: 1758432999.0)
        url2 = channel._signed_url("https://x.com/hook?access_token=a", "SECk")

        assert url1 != url2

    def test_地址无查询串时用问号拼接(self, channel):
        url = channel._signed_url("https://x.com/hook", "SECk")
        assert "?timestamp=" in url and "&timestamp=" not in url

    def test_地址已有查询串时用与号拼接(self, channel):
        url = channel._signed_url("https://x.com/hook?a=1", "SECk")
        assert "&timestamp=" in url

    # ---------------- 发送 ----------------

    def test_发送时使用签名地址(self, channel, cfg, sample_msg, posted):
        result = channel.send(sample_msg, cfg)

        assert result.success is True
        assert "timestamp=" in posted.calls[0]["url"]
        assert "sign=" in posted.calls[0]["url"]

    def test_记录里用未签名的地址(self, channel, cfg, sample_msg, posted):
        """签名每次都会变，记下来没有价值还会刷屏"""
        result = channel.send(sample_msg, cfg)
        assert "sign=" not in result.target
        assert "deadbeef" not in result.target

    def test_无密钥时不加签(self, channel, cfg, sample_msg, posted):
        cfg["secret"] = ""
        channel.send(sample_msg, cfg)
        assert "sign=" not in posted.calls[0]["url"]

    def test_配置不全时不发起网络请求(self, channel, cfg, sample_msg, posted):
        cfg["secret"] = "wrong-format"
        result = channel.send(sample_msg, cfg)
        assert result.success is False and result.retryable is False
        assert posted.calls == []

    # ---------------- 响应解读 ----------------

    def test_310000把三种可能一次说清(self):
        """
        钉钉把"签名错 / 关键词不匹配 / IP 不在白名单"都归到 310000，
        提示必须三条都说，否则运维会来回试。
        """
        result = DingtalkChannel._interpret(
            {"_http_ok": True, "_data": {"errcode": 310000, "errmsg": "keywords not in content"}},
            "t",
        )

        assert result.success is False
        assert result.retryable is False, "配置问题重试无用"
        assert "加签密钥" in result.message
        assert "关键词" in result.message
        assert "IP" in result.message

    def test_机器人不在群里不可重试(self):
        result = DingtalkChannel._interpret(
            {"_http_ok": True, "_data": {"errcode": 300001}}, "t"
        )
        assert result.retryable is False and "重新添加" in result.message

    def test_频率超限可重试(self):
        result = DingtalkChannel._interpret(
            {"_http_ok": True, "_data": {"errcode": 410100}}, "t"
        )
        assert result.retryable is True

    def test_网络失败可重试(self):
        result = DingtalkChannel._interpret({"_http_ok": False, "_error": "超时"}, "t")
        assert result.retryable is True

    def test_非JSON响应可重试(self):
        result = DingtalkChannel._interpret({"_http_ok": True, "_raw": "<html/>"}, "t")
        assert result.retryable is True and "代理" in result.message

    def test_成功(self):
        assert DingtalkChannel._interpret(
            {"_http_ok": True, "_data": {"errcode": 0}}, "t"
        ).success is True


# ============================================================================
# 十三、微信推送渠道（企业微信应用消息）
# ============================================================================

class TestWechatChannel:

    @pytest.fixture()
    def channel(self) -> WechatChannel:
        return ChannelRegistry.get("wechat")  # type: ignore[return-value]

    @pytest.fixture()
    def cfg(self) -> Dict[str, Any]:
        return {
            "enabled": "1",
            "corp_id": "ww0123456789",
            "corp_secret": "SECRET_VALUE",
            "agent_id": "1000002",
            "to_user": "zhansan|lisi",
        }

    # ---------------- 配置检查 ----------------

    def test_未启用(self, channel, cfg):
        cfg["enabled"] = "0"
        ok, reason = channel.check(cfg)
        assert ok is False and "未启用" in reason

    @pytest.mark.parametrize("field,keyword", [
        ("corp_id", "CorpID"),
        ("agent_id", "AgentId"),
        ("to_user", "接收人"),
    ])
    def test_缺失项给出具体字段名(self, channel, cfg, field, keyword):
        cfg[field] = ""
        ok, reason = channel.check(cfg)
        assert ok is False and keyword in reason

    def test_secret缺失提示含密钥更换(self, channel, cfg):
        cfg["corp_secret"] = ""
        ok, reason = channel.check(cfg)
        assert ok is False and "密钥已更换" in reason

    def test_AgentId必须是数字(self, channel, cfg):
        """AgentId 在企业微信后台是一串数字，填成应用名称是常见的手滑"""
        cfg["agent_id"] = "运维告警应用"
        ok, reason = channel.check(cfg)
        assert ok is False and "数字" in reason

    def test_配置齐全通过(self, channel, cfg):
        assert channel.check(cfg) == (True, "")

    # ---------------- token 缓存 ----------------

    def _stub_token(self, monkeypatch, token: str = "TOKEN-1", expires_in: int = 7200):
        calls: List[str] = []

        def _fake_get(url, timeout=10):
            calls.append(url)
            return {"_http_ok": True,
                    "_data": {"errcode": 0, "access_token": token, "expires_in": expires_in}}

        monkeypatch.setattr(BaseChannel, "_get_json", staticmethod(_fake_get))
        return calls

    def test_token被缓存复用(self, channel, monkeypatch):
        """
        企业微信对 gettoken 有**每日调用次数限制**。
        若每次发告警都换一次 token，一个告警风暴（几十台设备同时离线）
        就能把当日配额打光，之后所有推送全部失败 —— 而且失败发生在
        "最需要告警的时候"，非常糟糕。
        """
        calls = self._stub_token(monkeypatch)

        t1, err1 = channel._get_token("corp", "secret")
        t2, err2 = channel._get_token("corp", "secret")

        assert (t1, err1) == ("TOKEN-1", "")
        assert (t2, err2) == ("TOKEN-1", "")
        assert len(calls) == 1, "第二次必须走缓存，不能再请求"

    def test_清缓存后会重新获取(self, channel, monkeypatch):
        """
        配置页改了 corp_id / corp_secret 之后旧 token 立刻失效，
        不清缓存会让改完配置的头几分钟仍报"Secret 无效"，
        运维会以为配置没保存成功。
        """
        calls = self._stub_token(monkeypatch)
        channel._get_token("corp", "secret")

        reset_token_cache()
        token, _err = channel._get_token("corp", "secret")

        assert token == "TOKEN-1"
        assert len(calls) == 2

    def test_token提前过期以避免边界失败(self, channel, monkeypatch):
        """
        提前 300 秒过期，避免"取出时还有 2 秒有效期、请求到达时已过期"。
        """
        self._stub_token(monkeypatch, expires_in=7200)

        channel._get_token("corp", "secret")

        remain = wechat_mod._token_expire_at - time.time()
        assert 6800 < remain < 6950, f"应提前 300 秒过期，实际剩余 {remain:.0f} 秒"

    def test_有效期极短时保底缓存60秒(self, channel, monkeypatch):
        """expires_in 异常时不能"每次都重新获取"，那会打光调用配额"""
        self._stub_token(monkeypatch, expires_in=100)

        channel._get_token("corp", "secret")

        assert wechat_mod._token_expire_at - time.time() >= 55

    def test_获取token网络失败可重试(self, channel, monkeypatch):
        monkeypatch.setattr(BaseChannel, "_get_json", staticmethod(
            lambda url, timeout=10: {"_http_ok": False, "_error": "网络不可达"}
        ))

        token, err = channel._get_token("corp", "secret")

        assert token is None
        assert "网络不可达" in err

    @pytest.mark.parametrize("errcode,keyword", [
        (40013, "CorpID"),
        (40001, "Secret"),
        (40056, "AgentId"),
    ])
    def test_获取token失败时给出可操作提示(self, channel, monkeypatch, errcode, keyword):
        monkeypatch.setattr(BaseChannel, "_get_json", staticmethod(
            lambda url, timeout=10: {"_http_ok": True,
                                     "_data": {"errcode": errcode, "errmsg": "invalid"}}
        ))

        token, err = channel._get_token("corp", "secret")

        assert token is None
        assert keyword in err

    def test_未返回token视为失败(self, channel, monkeypatch):
        monkeypatch.setattr(BaseChannel, "_get_json", staticmethod(
            lambda url, timeout=10: {"_http_ok": True, "_data": {"errcode": 0}}
        ))
        token, err = channel._get_token("corp", "secret")
        assert token is None and "未返回" in err

    # ---------------- 发送 ----------------

    def _stub_post(self, monkeypatch, resp=None):
        calls: List[Dict[str, Any]] = []

        def _fake_post(url, payload, timeout=10):
            calls.append({"url": url, "payload": payload})
            return resp or {"_http_ok": True, "_data": {"errcode": 0, "errmsg": "ok"}}

        monkeypatch.setattr(BaseChannel, "_post_json", staticmethod(_fake_post))
        return calls

    def test_发送成功(self, channel, cfg, sample_msg, monkeypatch):
        self._stub_token(monkeypatch)
        calls = self._stub_post(monkeypatch)

        result = channel.send(sample_msg, cfg)

        assert result.success is True
        assert result.target == "zhansan|lisi"
        payload = calls[0]["payload"]
        assert payload["touser"] == "zhansan|lisi"
        assert payload["agentid"] == 1000002, "agentid 必须以整数提交"
        assert payload["msgtype"] == "text"
        assert "access_token=TOKEN-1" in calls[0]["url"]

    def test_关闭重复消息检查(self, channel, cfg, sample_msg, monkeypatch):
        """
        企业微信默认会丢弃同一秒内内容相同的消息，
        而"同一批设备同时离线"恰恰会产生多条相同内容 —— 必须显式关闭。
        """
        self._stub_token(monkeypatch)
        calls = self._stub_post(monkeypatch)

        channel.send(sample_msg, cfg)

        assert calls[0]["payload"]["duplicate_check_interval"] == 0

    def test_应用消息用纯文本不经markdown(self, channel, cfg, sample_msg, monkeypatch):
        """企业微信应用消息的 text 不渲染 markdown，给它 markdown 会显示成原始符号"""
        self._stub_token(monkeypatch)
        calls = self._stub_post(monkeypatch)

        channel.send(sample_msg, cfg)

        assert "【告警通知】" in calls[0]["payload"]["text"]["content"]
        assert "###" not in calls[0]["payload"]["text"]["content"]

    def test_配置不全时不获取token(self, channel, cfg, sample_msg, monkeypatch):
        """配置没配齐时连 token 都不该去换，避免白白消耗调用配额"""
        cfg["enabled"] = "0"
        calls = self._stub_token(monkeypatch)

        result = channel.send(sample_msg, cfg)

        assert result.success is False
        assert calls == []

    def test_换token失败时错误码不可重试(self, channel, cfg, sample_msg, monkeypatch):
        monkeypatch.setattr(BaseChannel, "_get_json", staticmethod(
            lambda url, timeout=10: {"_http_ok": True,
                                     "_data": {"errcode": 40013, "errmsg": "invalid corpid"}}
        ))

        result = channel.send(sample_msg, cfg)

        assert result.success is False
        assert result.retryable is False, "账号类错误重试无用"

    def test_换token网络错误可重试(self, channel, cfg, sample_msg, monkeypatch):
        monkeypatch.setattr(BaseChannel, "_get_json", staticmethod(
            lambda url, timeout=10: {"_http_ok": False, "_error": "超时"}
        ))

        result = channel.send(sample_msg, cfg)

        assert result.success is False and result.retryable is True

    # ---------------- 响应解读 ----------------

    def test_errcode为0即成功(self):
        assert WechatChannel._interpret(
            {"_http_ok": True, "_data": {"errcode": 0, "errmsg": "ok", "invaliduser": ""}}, "u"
        ).success is True

    def test_解析invaliduser(self):
        """
        ★ 企业微信在部分成员无效时**仍返回 errcode=0**，
        只在 invaliduser 里列出无效账号。
        不解析这个字段，就会出现"运维以为通知了，实际关键责任人根本不在收件人里"。
        """
        result = WechatChannel._interpret(
            {"_http_ok": True,
             "_data": {"errcode": 0, "errmsg": "ok", "invaliduser": "lisi"}},
            "zhansan|lisi",
        )

        assert result.success is True, "部分送达仍算成功，但必须把无效成员写进记录"
        assert "lisi" in result.message

    def test_42001清掉token缓存(self, channel):
        """token 过期后必须清缓存，否则下次仍会拿旧 token 再失败一次"""
        wechat_mod._cached_token = "STALE"
        wechat_mod._token_expire_at = time.time() + 600

        result = WechatChannel._interpret(
            {"_http_ok": True, "_data": {"errcode": 42001, "errmsg": "expired"}}, "u"
        )

        assert result.success is False and result.retryable is True
        assert wechat_mod._cached_token is None
        assert "重试" in result.message

    @pytest.mark.parametrize("errcode,keyword", [
        (40013, "CorpID"),
        (40056, "AgentId"),
        (60011, "可见范围"),
        (82001, "to_user"),
    ])
    def test_配置类错误码不可重试(self, errcode, keyword):
        result = WechatChannel._interpret(
            {"_http_ok": True, "_data": {"errcode": errcode, "errmsg": "x"}}, "u"
        )
        assert result.success is False
        assert result.retryable is False
        assert keyword in result.message

    def test_频率超限可重试(self):
        result = WechatChannel._interpret(
            {"_http_ok": True, "_data": {"errcode": 45009}}, "u"
        )
        assert result.retryable is True

    def test_网络失败可重试(self):
        assert WechatChannel._interpret({"_http_ok": False, "_error": "x"}, "u").retryable

    def test_非JSON响应可重试(self):
        result = WechatChannel._interpret({"_http_ok": True, "_raw": "<html/>"}, "u")
        assert result.retryable is True and "代理" in result.message

    def test_未知错误码保留原文(self):
        result = WechatChannel._interpret(
            {"_http_ok": True, "_data": {"errcode": 99999, "errmsg": "未知"}}, "u"
        )
        assert "99999" in result.message


# ============================================================================
# 十四、通知分发器
# ============================================================================

class TestNotifierDispatch:

    def _setup(self, db: Session, **rule_overrides: Any):
        # setdefault：调用方显式传 duration_n 时不冲突
        rule_overrides.setdefault("duration_n", 1)
        dev = make_device(db)
        rule = make_rule(db, **rule_overrides)
        event = alert_service.create_event(db, rule, dev, "cpu_usage", 95.0,
                                          metric_name="CPU使用率")
        db.commit()
        return dev, rule, event

    def test_无关联规则直接返回(self, mem_db, fake_notify_pool):
        _dev, _rule, event = self._setup(mem_db)
        stats = notifier.dispatch(mem_db, event, None)
        assert stats["queued"] == 0
        assert "无关联规则" in stats["reason"]

    def test_未配置渠道直接返回(self, mem_db, fake_notify_pool):
        _dev, rule, event = self._setup(mem_db, notify_channels="[]")
        stats = notifier.dispatch(mem_db, event, rule)
        assert stats["queued"] == 0
        assert "未配置通知渠道" in stats["reason"]
        assert fake_notify_pool.pool.submissions == []

    def test_正常分发落pending记录并投递(self, mem_db, fake_notify_pool):
        dev, rule, event = self._setup(mem_db, notify_channels='["email","wecom"]')

        stats = notifier.dispatch(mem_db, event, rule, dev)

        assert stats["queued"] == 2
        assert stats["channels"] == ["email", "wecom"]

        records = mem_db.execute(
            select(AlertNotifyRecord).order_by(AlertNotifyRecord.id)
        ).scalars().all()
        assert len(records) == 2
        assert {r.channel for r in records} == {"email", "wecom"}
        assert all(r.status == NOTIFY_PENDING for r in records), \
            "必须先落 pending 再投递：任何时刻数据库都要能回答'发到哪一步了'"

        # 投递的参数顺序：(_deliver, record_id, channel_key, message)
        assert [s[1] for s in fake_notify_pool.pool.submissions] == [r.id for r in records]
        assert [s[2] for s in fake_notify_pool.pool.submissions] == ["email", "wecom"]

    def test_渠道标识大小写与空格容错(self, mem_db, fake_notify_pool):
        dev, rule, event = self._setup(mem_db, notify_channels='[" EMAIL "]')
        stats = notifier.dispatch(mem_db, event, rule, dev)
        assert stats["channels"] == ["email"]

    def test_未注册渠道被跳过并留痕(self, mem_db, fake_notify_pool):
        """规则里写了平台不认识的渠道（历史配置、手工改库）时不能崩，且要留痕"""
        dev, rule, event = self._setup(mem_db, notify_channels='["feishu","email"]')

        stats = notifier.dispatch(mem_db, event, rule, dev)

        assert stats["channels"] == ["email"]
        assert stats["queued"] == 1
        logs = mem_db.execute(select(AlarmLog.message)).scalars().all()
        assert any("未注册" in (m or "") for m in logs)

    def test_全部渠道都未注册时安全返回(self, mem_db, fake_notify_pool):
        dev, rule, event = self._setup(mem_db, notify_channels='["feishu"]')

        stats = notifier.dispatch(mem_db, event, rule, dev)

        assert stats["queued"] == 0
        assert stats["reason"] == "无可用渠道"
        assert fake_notify_pool.pool.submissions == []

    def test_被抑制时不落pending记录(self, mem_db, fake_notify_pool):
        """
        ★ 关键：收敛判定必须在**创建推送记录之前**。

        若先建记录再判断，那些"创建了但决定不发"的悬空 pending 记录
        会被统计口径算进限额，把额度白白吃掉 —— 表现为"告警越来越静"。
        """
        dev, rule, event = self._setup(
            mem_db, notify_channels='["email"]',
            silence_until=datetime.now() + timedelta(hours=1),
        )

        stats = notifier.dispatch(mem_db, event, rule, dev)

        assert stats["suppressed"] is True
        assert "静默期" in stats["reason"]
        assert mem_db.execute(select(AlertNotifyRecord)).scalars().all() == []
        assert fake_notify_pool.pool.submissions == []
        assert ALARM_ACTION_SUPPRESS in alarm_actions(mem_db)

    def test_被收敛时不落pending记录(self, mem_db, fake_notify_pool):
        dev, rule, event = self._setup(
            mem_db, notify_channels='["email"]', converge_window=300, converge_max=1
        )
        mem_db.add(AlertNotifyRecord(event_id=event.id, channel="email",
                                     status=NOTIFY_SUCCESS, created_at=datetime.now()))
        mem_db.commit()

        stats = notifier.dispatch(mem_db, event, rule, dev)

        assert stats["suppressed"] is True
        assert "上限" in stats["reason"]
        assert ALARM_ACTION_CONVERGE in alarm_actions(mem_db)

    def test_恢复通知不受窗口限流(self, mem_db, fake_notify_pool):
        dev, rule, event = self._setup(
            mem_db, notify_channels='["email"]', converge_window=300, converge_max=1
        )
        mem_db.add(AlertNotifyRecord(event_id=event.id, channel="email",
                                     status=NOTIFY_SUCCESS, created_at=datetime.now()))
        mem_db.commit()

        stats = notifier.dispatch(mem_db, event, rule, dev, is_recovery=True)

        assert stats["suppressed"] is False
        assert stats["queued"] == 1

    def test_推送策略写进告警时间线(self, mem_db, fake_notify_pool):
        """
        现场最常被问"为什么这次只收到一条告警"，
        把策略原文写在触发记录里，比让实施人员解释快得多。
        """
        dev, rule, event = self._setup(
            mem_db, notify_channels='["email"]',
            duration_n=3, recovery_n=2, converge_window=600, converge_max=5,
        )

        notifier.dispatch(mem_db, event, rule, dev)

        logs = mem_db.execute(select(AlarmLog.message)).scalars().all()
        assert any("策略" in (m or "") and "600 秒内最多推送 5 条" in (m or "") for m in logs)

    def test_平台地址为空则消息里无链接(self, mem_db, fake_notify_pool):
        """留空时通知里不出现"前往处理"链接 —— 给个连不上的地址更糟"""
        dev, rule, event = self._setup(mem_db, notify_channels='["email"]')

        notifier.dispatch(mem_db, event, rule, dev)

        message = fake_notify_pool.pool.submissions[0][3]
        assert message.platform_url == ""
        assert "平台地址" not in message.as_text()

    def test_内部异常不外抛(self, mem_db, fake_notify_pool, monkeypatch):
        """
        ★ 推送失败绝不能反向影响采集与评估：
        指标已经落库了，推送是"最后一公里"，它坏了不该让数据也丢。
        """
        dev, rule, event = self._setup(mem_db, notify_channels='["email"]')

        def _boom(key):
            raise RuntimeError("模拟渠道注册表损坏")

        monkeypatch.setattr(ChannelRegistry, "get", staticmethod(_boom))
        stats = notifier.dispatch(mem_db, event, rule, dev)

        assert stats["queued"] == 0
        assert "RuntimeError" in stats["reason"]

    def test_渠道配置为空字典时不崩(self, mem_db, fake_notify_pool):
        """某渠道从未配置过时 get_channel_settings 返回 {}，不能因此崩掉"""
        dev, rule, event = self._setup(mem_db, notify_channels='["email"]')
        stats = notifier.dispatch(mem_db, event, rule, dev)
        assert stats["queued"] == 1

    def test_通知渠道清单与注册表一致(self):
        assert notifier.available_channels() == ChannelRegistry.describe()

    def test_推送状态中文描述(self):
        record = AlertNotifyRecord(event_id=1, channel="email", status=NOTIFY_SUCCESS)
        assert notifier.notify_status_text(record) == "发送成功"
        record.status = NOTIFY_FAIL
        assert notifier.notify_status_text(record) == "发送失败"
        record.status = NOTIFY_PENDING
        assert notifier.notify_status_text(record) == "发送中"
        record.status = "weird"
        assert notifier.notify_status_text(record) == "weird"


class TestSendWithRetry:
    """重试策略：只重试"值得重试"的失败"""

    def test_成功一次即结束(self, sample_msg, no_sleep):
        channel = StubChannel([ChannelResult(success=True, message="ok")])
        result, attempts = notifier._send_with_retry(channel, sample_msg, {})
        assert result.success is True
        assert attempts == 1
        assert channel.calls == 1
        assert no_sleep == []

    def test_确定性失败立即结束(self, sample_msg, no_sleep):
        """
        "渠道没配好""密钥错"这类失败重试只是白白占用通知线程、
        还会拖慢其它渠道的送达。
        """
        channel = StubChannel([ChannelResult(success=False, message="未配置",
                                            retryable=False)])
        result, attempts = notifier._send_with_retry(channel, sample_msg, {})

        assert result.success is False
        assert attempts == 1
        assert channel.calls == 1
        assert no_sleep == []

    def test_可重试失败会重试并成功(self, sample_msg, no_sleep):
        channel = StubChannel([
            ChannelResult(success=False, message="超时", retryable=True),
            ChannelResult(success=True, message="ok"),
        ])
        result, attempts = notifier._send_with_retry(channel, sample_msg, {})

        assert result.success is True
        assert attempts == 2
        assert channel.calls == 2
        assert no_sleep == [2], "第一次重试前应退避 2 秒"

    def test_退避递增(self, sample_msg, no_sleep):
        """
        企业微信/钉钉的限流往往是短时突发，1 秒后重试仍会被拒，3 秒后成功率明显更高。
        """
        channel = StubChannel([ChannelResult(success=False, message="限流", retryable=True)])
        _result, attempts = notifier._send_with_retry(channel, sample_msg, {})

        assert attempts == notifier.MAX_RETRY + 1
        assert no_sleep == list(notifier.RETRY_BACKOFF)

    def test_全部失败返回最后一次结果(self, sample_msg, no_sleep):
        channel = StubChannel([ChannelResult(success=False, message="最后一次错误",
                                            retryable=True)])
        result, attempts = notifier._send_with_retry(channel, sample_msg, {})

        assert result.message == "最后一次错误"
        assert attempts == 3


# ============================================================================
# 十五、异步投递
# ============================================================================

class TestNotifierDeliver:

    def _record(self, db: Session, channel: str = "email") -> AlertNotifyRecord:
        dev = make_device(db)
        rule = make_rule(db, duration_n=1)
        event = alert_service.create_event(db, rule, dev, "cpu_usage", 95.0)
        record = AlertNotifyRecord(event_id=event.id, channel=channel,
                                   status=NOTIFY_PENDING, retry_count=0)
        db.add(record)
        db.commit()
        return record

    def test_成功回写记录与日志(self, mem_db, patched_scope, sample_msg, no_sleep):
        record = self._record(mem_db)
        record_id = record.id
        calls: List[AlertMessage] = []

        with extra_channel("email", ChannelResult(success=True, message="已发送至 2 个收件人",
                                                  target="ops@x.com"), calls):
            notifier._deliver(record_id, "email", sample_msg)

        rec = mem_db.get(AlertNotifyRecord, record_id)
        assert rec.status == NOTIFY_SUCCESS
        assert rec.target == "ops@x.com"
        assert rec.sent_at is not None
        assert rec.retry_count == 0
        assert "已发送" in rec.response_msg
        assert len(calls) == 1
        assert ALARM_ACTION_NOTIFY in alarm_actions(mem_db)

    def test_失败回写原因(self, mem_db, patched_scope, sample_msg, no_sleep):
        record = self._record(mem_db)
        record_id = record.id

        with extra_channel("email", ChannelResult(success=False, message="SMTP 认证失败",
                                                  retryable=False)):
            notifier._deliver(record_id, "email", sample_msg)

        rec = mem_db.get(AlertNotifyRecord, record_id)
        assert rec.status == NOTIFY_FAIL
        assert "认证失败" in rec.response_msg

    def test_重试次数记录为尝试次数减一(self, mem_db, patched_scope, sample_msg, no_sleep):
        """retry_count 是"重试次数"而不是"尝试次数"，前端文案依赖这个口径"""
        record = self._record(mem_db)
        record_id = record.id

        with extra_channel("email", ChannelResult(success=False, message="超时",
                                                  retryable=True)):
            notifier._deliver(record_id, "email", sample_msg)

        rec = mem_db.get(AlertNotifyRecord, record_id)
        assert rec.retry_count == notifier.MAX_RETRY

    def test_长错误信息被截断(self, mem_db, patched_scope, sample_msg, no_sleep):
        """渠道返回的错误体可能很长（HTML 错误页），不能整段塞进数据库"""
        record = self._record(mem_db)
        record_id = record.id

        with extra_channel("email", ChannelResult(success=False,
                                                  message="x" * 5000, retryable=False)):
            notifier._deliver(record_id, "email", sample_msg)

        assert len(mem_db.get(AlertNotifyRecord, record_id).response_msg) <= 1000

    def test_记录不存在时安全返回(self, mem_db, patched_scope, sample_msg):
        notifier._deliver(999999, "email", sample_msg)   # 不应抛异常

    def test_非pending记录不重复发送(self, mem_db, patched_scope, sample_msg):
        """
        幂等保护：重发场景下同一记录可能被提交两次，
        若不去重会把同一条告警发两遍。
        """
        record = self._record(mem_db)
        record.status = NOTIFY_SUCCESS
        mem_db.commit()
        calls: List[AlertMessage] = []

        with extra_channel("email", ChannelResult(success=True, message="ok"), calls):
            notifier._deliver(record.id, "email", sample_msg)

        assert calls == [], "已完成的记录不该重发"

    def test_渠道未注册时安全返回(self, mem_db, patched_scope, sample_msg):
        record = self._record(mem_db)
        record_id = record.id

        notifier._deliver(record_id, "no_such_channel", sample_msg)

        assert mem_db.get(AlertNotifyRecord, record_id).status == NOTIFY_PENDING

    def test_渠道抛异常时标记失败(self, mem_db, patched_scope, sample_msg):
        """
        渠道实现按约定不该抛异常，但真抛了也必须留下痕迹 ——
        线程池任务静默丢弃是最难排查的一类问题。
        """
        record = self._record(mem_db)
        record_id = record.id

        @ChannelRegistry.register("boom_chan")
        class _Boom(BaseChannel):
            text = "会炸的渠道"
            config_group = "boom_chan"

            def check(self, cfg):
                return True, ""

            def send(self, message, cfg):
                raise RuntimeError("渠道内部崩了")

        try:
            notifier._deliver(record_id, "boom_chan", sample_msg)
        finally:
            ChannelRegistry._registry.pop("boom_chan", None)

        rec = mem_db.get(AlertNotifyRecord, record_id)
        assert rec.status == NOTIFY_FAIL
        assert "RuntimeError" in (rec.response_msg or "")


class TestResendRecord:

    def _failed_record(self, db: Session) -> AlertNotifyRecord:
        dev = make_device(db)
        rule = make_rule(db, duration_n=1)
        event = alert_service.create_event(db, rule, dev, "cpu_usage", 95.0)
        record = AlertNotifyRecord(event_id=event.id, channel="email",
                                   status=NOTIFY_FAIL, response_msg="上次失败")
        db.add(record)
        db.commit()
        return record

    def test_重发走与首发相同的路径(
        self, mem_db, patched_scope, fake_notify_pool
    ):
        """
        "补发"与"首发"用两套逻辑，是"补发能成、首发不成"这类怪问题的来源。
        这里验证重发把记录重置为 pending 后，投递的仍是同一个 _deliver。
        """
        record = self._failed_record(mem_db)

        assert notifier.resend_record(record.id) is True

        assert mem_db.get(AlertNotifyRecord, record.id).status == NOTIFY_PENDING
        assert fake_notify_pool.pool.submissions[0][0] is notifier._deliver
        assert fake_notify_pool.pool.submissions[0][1] == record.id

    def test_已成功的记录拒绝重发(self, mem_db, patched_scope, fake_notify_pool):
        record = self._failed_record(mem_db)
        record.status = NOTIFY_SUCCESS
        mem_db.commit()

        assert notifier.resend_record(record.id) is False
        assert fake_notify_pool.pool.submissions == []

    def test_记录不存在返回False(self, mem_db, patched_scope, fake_notify_pool):
        assert notifier.resend_record(999999) is False


# ============================================================================
# 十六、线程池隔离（本模块最重要的结构约束）
# ============================================================================

class TestThreadPoolIsolation:
    """
    ★ 通知**不能**复用采集线程池。

    一个真实场景：机房断电恢复后，200 台设备同时离线。
    若把推送任务丢进采集线程池（60 线程），每条任务在失败重试时要 sleep 等退避
    （最长 7 秒），400 条任务（200 事件 × 2 渠道）会瞬间占满全部线程。后果是：

        采集线程被占满 → 所有设备本轮采不到 → 全部判离线 → 产生更多告警
        → 推送任务更多 → 线程更紧张 …… 雪崩。

    所以通知用独立线程池：采集再忙也能推出去，推送再积压也不影响采集。
    """

    def test_通知线程池独立且命名(self, fake_notify_pool):
        real_pool = fake_notify_pool.real_get_pool()
        try:
            assert isinstance(real_pool, ThreadPoolExecutor)
            assert real_pool._max_workers == notifier.NOTIFY_POOL_SIZE
            # 线程名前缀用于现场排障：看线程转储能一眼分辨是哪一侧积压
            assert real_pool._thread_name_prefix == "notify"
        finally:
            notifier.shutdown(wait=False)

    def test_通知线程池与采集线程池不是同一个(self, fake_notify_pool):
        notify_pool = fake_notify_pool.real_get_pool()
        collect = get_executor()
        try:
            collect.start()
            assert collect._executor is not notify_pool
            assert collect.stats()["max_workers"] == settings.COLLECT_THREAD_POOL_SIZE
        finally:
            collect.shutdown(wait=False)
            notifier.shutdown(wait=False)

    def test_通知并发数远小于采集并发数(self):
        """
        通知池的作用是"不争抢采集线程"，不是"尽快发完"。
        取值过大反而会给企业微信/钉钉造成限流。
        """
        assert notifier.NOTIFY_POOL_SIZE < settings.COLLECT_THREAD_POOL_SIZE

    def test_线程池是懒创建的单例(self, fake_notify_pool):
        """首次 dispatch 才创建，避免"从不用告警的部署"白起 8 个线程"""
        first = fake_notify_pool.real_get_pool()
        second = fake_notify_pool.real_get_pool()
        try:
            assert first is second
        finally:
            notifier.shutdown(wait=False)


# ============================================================================
# 十七、端到端链路
# ============================================================================

class TestEndToEnd:
    """
    用**假渠道**把"指标 → 评估 → 触发 → 收敛判定 → 落 pending → 投递
    → 渠道发送 → 回写结果 → 写告警日志"整条链路跑一遍。

    为什么值得单独做端到端（而不是只做各层单测）：
    各层单测都通过、但拼起来不工作，是集成类项目最常见的失败形态 ——
    例如收敛判定写在"创建记录之后"、`_deliver` 拿不到记录 ID、
    恢复通知的 is_recovery 标志没传下去。这些只有串起来才暴露。
    """

    @pytest.fixture()
    def pushable(self):
        """注册一个总能成功的渠道，并交出收到的消息"""
        sent: List[AlertMessage] = []
        with extra_channel("fakepush",
                           ChannelResult(success=True, message="已送达", target="ops"),
                           sent) as calls:
            yield calls

    def _run_deliveries(self, pool: FakePool) -> None:
        """把假池里记录的投递真正执行一遍（模拟通知线程池干活）"""
        for fn, record_id, channel_key, message in list(pool.submissions):
            fn(record_id, channel_key, message)

    def test_从超标指标到推送成功全链路(
        self, mem_db, patched_scope, fake_notify_pool, pushable
    ):
        dev = make_device(mem_db, name="核心交换机01", online_status="online")
        make_rule(mem_db, name="CPU使用率过高", metric_key="cpu_usage", operator="gt",
                  threshold=85, duration_n=2, recovery_n=2, level="major",
                  notify_channels='["fakepush"]')

        # ---- 1) 第 1 轮超标：未达连续次数，不触发 ----
        assert drive(mem_db, dev, 91.0, rounds=1)["fired"] == 0
        assert all_events(mem_db) == []

        # ---- 2) 第 2 轮超标：触发 + 已分发（pending 已落库、已投递）----
        assert drive(mem_db, dev, 93.0, rounds=1)["fired"] == 1
        records = mem_db.execute(select(AlertNotifyRecord)).scalars().all()
        assert len(records) == 1 and records[0].status == NOTIFY_PENDING
        assert len(fake_notify_pool.pool.submissions) == 1

        # ---- 3) 执行投递（假池只记录不执行）----
        self._run_deliveries(fake_notify_pool.pool)

        record_id = records[0].id
        rec = mem_db.get(AlertNotifyRecord, record_id)
        assert rec.status == NOTIFY_SUCCESS
        assert rec.target == "ops"
        assert rec.sent_at is not None

        # ---- 4) 消息内容取自本轮真实值，且单位来自唯一来源 ----
        assert len(pushable) == 1
        message = pushable[0]
        assert message.device_name == "核心交换机01"
        assert message.metric_value_text == "93%"
        assert message.threshold_text == "大于 85%"
        assert message.is_recovery is False
        assert message.platform_name  # 平台名从配置读取

        # ---- 5) 时间线完整：触发 → 通知 ----
        actions = alarm_actions(mem_db)
        assert ALARM_ACTION_TRIGGER in actions
        assert ALARM_ACTION_NOTIFY in actions

        # ---- 6) 恢复：连续 2 轮正常 ----
        assert drive(mem_db, dev, 30.0, rounds=1)["recovered"] == 0
        assert drive(mem_db, dev, 28.0, rounds=1)["recovered"] == 1

        event = mem_db.get(AlertEvent, records[0].event_id)
        assert event.status == EVENT_STATUS_RECOVERED
        assert event.recover_at is not None

        # 恢复通知走同一套分发：新的 pending 记录 + 新的投递
        assert len(fake_notify_pool.pool.submissions) == 2
        self._run_deliveries(fake_notify_pool.pool)
        assert len(pushable) == 2
        assert pushable[1].is_recovery is True
        assert "恢复" in pushable[1].as_text()

    def test_静默期内只记录事件不推送(
        self, mem_db, patched_scope, fake_notify_pool, pushable
    ):
        """
        维护窗口的语义：**仍评估、仍记录事件**，只是不推送。
        这样维护结束后若指标确实异常，能立刻收到告警而不是被吞掉。
        """
        dev = make_device(mem_db)
        make_rule(mem_db, duration_n=1, notify_channels='["fakepush"]',
                  silence_until=datetime.now() + timedelta(hours=2))

        stats = drive(mem_db, dev, 95.0, rounds=1)

        assert stats["fired"] == 1, "事件仍要记录"
        assert len(all_events(mem_db)) == 1
        assert mem_db.execute(select(AlertNotifyRecord)).scalars().all() == []
        assert fake_notify_pool.pool.submissions == []
        assert pushable == []
        assert ALARM_ACTION_SUPPRESS in alarm_actions(mem_db)

    def test_窗口限流只压触发不压恢复(
        self, mem_db, patched_scope, fake_notify_pool, pushable
    ):
        """
        converge_max=2 的端到端语义：3 个"故障周期"里，
        只有前 2 次触发被推送，第 3 次触发被合并计数；
        而 3 次恢复通知全部照发（恢复消息不该被限量压掉）。
        """
        dev = make_device(mem_db)
        make_rule(mem_db, duration_n=1, recovery_n=1, notify_channels='["fakepush"]',
                  converge_window=3600, converge_max=2)

        for _ in range(3):
            drive(mem_db, dev, 95.0, rounds=1)   # 触发
            drive(mem_db, dev, 10.0, rounds=1)   # 恢复

        # 3 次触发里 2 次被推送、1 次被收敛；3 次恢复全部推送
        assert len(fake_notify_pool.pool.submissions) == 5
        assert ALARM_ACTION_CONVERGE in alarm_actions(mem_db)

        records = mem_db.execute(select(AlertNotifyRecord)).scalars().all()
        assert len(records) == 5

    def test_已确认后不再推送同一条告警(
        self, mem_db, patched_scope, fake_notify_pool, pushable
    ):
        """确认按钮的完整语义：确认后持续超标既不重新触发、也不重复推送"""
        dev = make_device(mem_db)
        make_rule(mem_db, duration_n=1, recovery_n=5, notify_channels='["fakepush"]')

        drive(mem_db, dev, 95.0, rounds=1)
        self._run_deliveries(fake_notify_pool.pool)
        assert len(pushable) == 1

        event = all_events(mem_db)[0]
        alert_service.ack_event(
            mem_db, event.id, AlertAckRequest(remark="已联系厂商"), "zhansan"
        )

        drive(mem_db, dev, 97.0, rounds=8)

        assert len(pushable) == 1, "确认后不该再收到同一条告警"
        assert len(fake_notify_pool.pool.submissions) == 1
        assert mem_db.get(AlertEvent, event.id).occur_count == 1

    def test_推送渠道全部失败不影响告警落库(
        self, mem_db, patched_scope, fake_notify_pool
    ):
        """
        ★ "故障时系统仍可用"的兜底：外部渠道全挂，
        平台上仍然能看到告警、能看到失败原因（并可人工补发）。
        """
        dev = make_device(mem_db)
        make_rule(mem_db, duration_n=1, notify_channels='["failing"]')

        with extra_channel("failing",
                           ChannelResult(success=False, message="渠道未启用", retryable=False)):
            stats = drive(mem_db, dev, 95.0, rounds=1)
            self._run_deliveries(fake_notify_pool.pool)

        assert stats["fired"] == 1
        assert len(all_events(mem_db)) == 1

        records = mem_db.execute(select(AlertNotifyRecord)).scalars().all()
        assert len(records) == 1
        assert records[0].status == NOTIFY_FAIL
        assert "未启用" in records[0].response_msg

    def test_多设备并发评估互不影响(
        self, mem_db, patched_scope, fake_notify_pool, pushable
    ):
        """
        非功能需求："单台设备采集异常不影响其它设备"。
        告警层对应的是"单台设备评估出错不影响其它设备"。
        """
        devices = [make_device(mem_db, name=f"设备{i}", ip=f"10.0.0.{i}")
                   for i in range(1, 6)]
        make_rule(mem_db, duration_n=1, notify_channels='["fakepush"]')
        for dev in devices:
            set_metric(mem_db, dev.id, "cpu_usage", 95.0)

        stats = evaluator.evaluate_devices(mem_db, [d.id for d in devices])

        assert stats["devices"] == 5
        assert stats["fired"] == 5
        assert len(all_events(mem_db)) == 5
        assert len(fake_notify_pool.pool.submissions) == 5
