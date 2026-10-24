"""
定时采集任务的单元测试（第4步：SNMP/SSH/WinRM/ICMP 采集与调度）
===============================================================================
**为什么要专门给调度任务写测试**

第4步的验证最早是用一个临时脚本跑的：临时停用全库演示设备、只让回环设备到点、
跑完再恢复。那次确实跑通了，但存在三个问题：
  1. 脚本放在 /tmp，随进程消失，无法回归；
  2. "临时改数据库再改回来" 一旦中途失败，会给现场留下一库被停用的设备；
  3. 它只验证了"ICMP + 回环"这一条路径，协议不支持、采集失败、
     未到点、防重入这些分支完全没有覆盖。

所以本文件把它固化为正式用例，全部跑在**内存 SQLite** 上：
不碰生产库、不发真实网络包、不需要"先改库再改回来"，
并行执行也不会互相干扰（每个用例一个独立内存库）。

**测试分层**

| 层次                | 被测对象                                  | 是否真机 |
|---------------------|-------------------------------------------|----------|
| 纯函数              | _clamp、MIN_COLLECT_INTERVAL 收敛          | 否       |
| 调度筛选（真实 SQL）| _select_due_devices、reset_device_schedule | 否（内存库）|
| 单设备采集全流程    | _collect_one_device（注入假采集器）        | 否（内存库）|
| 任务入口            | run_collect_job（注入假线程池）            | 否       |
| 采集后告警评估      | _evaluate_after_collect（注入假评估器）    | 否       |

真机链路（ICMP 探测、降级为系统 ping）已在 tests/test_collectors.py 覆盖，
本文件专测"调度与落库"，两者合起来才是完整的第4步验证。

运行：pytest tests/test_collect_job.py -v
===============================================================================
"""
from __future__ import annotations

import contextlib
from concurrent.futures import Future
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

import app.alarm
import app.collectors
from app.collectors.base import BaseCollector, CollectResult
from app.models import Base
from app.models.device import MonDevice
from app.models.log import SysLog, CollectLog
from app.models.metric import MetricLatest
from app.scheduler.jobs import collect_job
from app.services import config_service


# ============================================================================
# 夹具与助手
# ============================================================================

@pytest.fixture()
def mem_db():
    """
    内存 SQLite 会话（每个用例一个全新库）。

    为什么用内存库而不是 data/itsm.db：
      - 用例之间零残留，失败也不会污染现场数据
      - 不需要"停用全部设备 → 跑 → 恢复"这种危险操作
      - 快（create_all 在内存里是毫秒级）
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


@pytest.fixture(autouse=True)
def _clean_config_cache():
    """清掉进程级配置缓存，避免上一个用例/真实库的配置串味"""
    config_service.invalidate_cache()
    yield
    config_service.invalidate_cache()


def patch_scope(monkeypatch, db: Session) -> None:
    """
    把 collect_job 的 session_scope 换成内存会话。

    ⚠ 必须与真实 session_scope 行为一致：正常退出提交、异常回滚。
    若这里只 yield 不提交，被测代码里"设了属性但没 commit"的 bug 就会被掩盖。
    """
    @contextlib.contextmanager
    def _scope():
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise

    monkeypatch.setattr(collect_job, "session_scope", _scope)


def make_device(db: Session, **overrides: Any) -> MonDevice:
    """造一台设备（默认参数可被 overrides 覆盖）"""
    data: Dict[str, Any] = {
        "name": "测试设备",
        "ip": "127.0.0.1",
        "device_type": "server",
        "protocol": "icmp",
        "collect_interval": 60,
        "enabled": True,
    }
    data.update(overrides)
    device = MonDevice(**data)
    db.add(device)
    db.commit()
    db.refresh(device)
    return device


class FakeCollector(BaseCollector):
    """假采集器：直接返回预设结果，不发任何网络请求"""

    protocol = "fake"

    def __init__(self, result: CollectResult) -> None:
        self._result = result
        self.called_with: List[Dict[str, Any]] = []

    def collect(self, device_id: int, params: Dict[str, Any]) -> CollectResult:
        self.called_with.append(params)
        return self._result


class FakeExecutor:
    """
    假线程池：只记录投递，不真正执行。

    这样 run_collect_job() 的"筛选 → 投递 → 计数"能被独立验证，
    而真正的采集行为交给 _collect_one_device 的用例去测——
    两件事分开测，失败时能一眼看出是"调度错了"还是"采集错了"。
    """

    def __init__(self, max_workers: int = 60, reject: Optional[List[int]] = None) -> None:
        self.max_workers = max_workers
        self.reject = set(reject or [])
        self.started = False
        self.resized_to: List[int] = []
        self.device_submissions: List[Tuple[int, Dict[str, Any]]] = []
        self.generic_submissions: List[Any] = []

    def start(self) -> None:
        self.started = True

    def resize(self, max_workers: int) -> None:
        self.resized_to.append(max_workers)
        self.max_workers = max_workers

    def submit_device(self, device_id: int, fn, *args: Any) -> Optional[Future]:
        if device_id in self.reject:
            return None            # 模拟防重入：上一轮未结束
        self.device_submissions.append((device_id, args[0] if args else {}))
        future: Future = Future()
        future.set_result({"device_id": device_id, "ok": True})
        return future

    def submit(self, fn, *args: Any, **kwargs: Any) -> Future:
        self.generic_submissions.append((fn, args))
        future: Future = Future()
        future.set_result(None)
        return future


# ============================================================================
# 一、纯函数：_clamp
# ============================================================================

class TestClamp:
    """配置值收敛（配置表被填成 0、-1、"abc" 时不能让平台崩）"""

    def test_区间内原样返回(self):
        assert collect_job._clamp(60, 5, 500) == 60

    def test_低于下限取下限(self):
        assert collect_job._clamp(1, 5, 500) == 5

    def test_高于上限取上限(self):
        assert collect_job._clamp(9999, 5, 500) == 500

    @pytest.mark.parametrize("bad", ["abc", None, "", [], {}])
    def test_非法值回退到下限(self, bad):
        assert collect_job._clamp(bad, 5, 500) == 5

    def test_数字字符串可转换(self):
        assert collect_job._clamp("120", 5, 500) == 120

    def test_池大小常量区间合理(self):
        """下限不能是 1（一台台采），上限不能大到把设备采挂"""
        assert collect_job.POOL_SIZE_MIN >= 1
        assert collect_job.POOL_SIZE_MAX >= 200


# ============================================================================
# 二、设备筛选：_select_due_devices
# ============================================================================

class TestSelectDueDevices:
    """挑"已启用 + 到点"的设备：第4步最核心的调度判据"""

    CFG = {"default_interval": 60, "offline_threshold": 3}

    def test_从未采集过的设备立即纳入(self, mem_db):
        """新建/刚导入的设备 last_collect_at 为空 → 立即采一次，让运维马上看到数据"""
        device = make_device(mem_db, last_collect_at=None)
        assert collect_job._select_due_devices(mem_db, self.CFG) == [device.id]

    def test_已到点的设备被纳入(self, mem_db):
        device = make_device(mem_db, last_collect_at=datetime.now() - timedelta(seconds=90))
        assert device.id in collect_job._select_due_devices(mem_db, self.CFG)

    def test_未到点的设备被跳过(self, mem_db):
        make_device(mem_db, last_collect_at=datetime.now() - timedelta(seconds=10))
        assert collect_job._select_due_devices(mem_db, self.CFG) == []

    def test_停用的设备即使到点也不采集(self, mem_db):
        make_device(mem_db, enabled=False, last_collect_at=datetime.now() - timedelta(days=1))
        assert collect_job._select_due_devices(mem_db, self.CFG) == []

    def test_周期小于下限时按30秒处理(self, mem_db):
        """
        有人把采集周期填成 1 秒"想看实时曲线"——设备会被采挂。
        下限 30 秒的落实点就在这里：interval=1 也必须等到 30 秒才到点。
        """
        device = make_device(
            mem_db, collect_interval=1, last_collect_at=datetime.now() - timedelta(seconds=10)
        )
        assert collect_job._select_due_devices(mem_db, self.CFG) == []

        # 满 30 秒后到点
        device.last_collect_at = datetime.now() - timedelta(seconds=31)
        mem_db.commit()
        assert device.id in collect_job._select_due_devices(mem_db, self.CFG)

    def test_周期为空时用全局默认周期(self, mem_db):
        """脏数据（interval 写成 NULL/0）不能导致"永远不到点，设备静默失联" """
        device = make_device(mem_db, collect_interval=0, last_collect_at=datetime.now() - timedelta(seconds=40))
        assert collect_job._select_due_devices(mem_db, self.CFG) == []
        device.last_collect_at = datetime.now() - timedelta(seconds=61)
        mem_db.commit()
        assert device.id in collect_job._select_due_devices(mem_db, self.CFG)

    def test_多台设备混合筛选(self, mem_db):
        """一个 tick 里同时存在"到点/未到点/停用/从未采集"四种设备"""
        fresh = make_device(mem_db, name="新设备", last_collect_at=None)
        due = make_device(mem_db, name="到点", last_collect_at=datetime.now() - timedelta(seconds=120))
        make_device(mem_db, name="未到点", last_collect_at=datetime.now() - timedelta(seconds=5))
        make_device(mem_db, name="已停用", enabled=False, last_collect_at=None)

        result = collect_job._select_due_devices(mem_db, self.CFG)
        assert sorted(result) == sorted([fresh.id, due.id])

    def test_返回的是设备ID列表(self, mem_db):
        make_device(mem_db, last_collect_at=None)
        result = collect_job._select_due_devices(mem_db, self.CFG)
        assert isinstance(result, list)
        assert all(isinstance(i, int) for i in result)


# ============================================================================
# 三、手动重排：reset_device_schedule
# ============================================================================

class TestResetDeviceSchedule:
    """运维改了设备采集参数后，不希望等一整个周期才验证是否修好"""

    def test_指定设备置空采集时间(self, mem_db):
        device = make_device(mem_db, last_collect_at=datetime.now())
        affected = collect_job.reset_device_schedule(mem_db, device.id)
        assert affected == 1
        mem_db.refresh(device)
        assert device.last_collect_at is None

    def test_不指定设备则全部置空(self, mem_db):
        make_device(mem_db, name="A", last_collect_at=datetime.now())
        make_device(mem_db, name="B", last_collect_at=datetime.now())
        assert collect_job.reset_device_schedule(mem_db) == 2
        assert mem_db.execute(
            select(MonDevice).where(MonDevice.last_collect_at.isnot(None))
        ).scalars().all() == []

    def test_重置后下一轮立即被纳入采集(self, mem_db):
        """这是本函数存在的意义：改完配置马上能验证"""
        device = make_device(mem_db, last_collect_at=datetime.now())
        cfg = {"default_interval": 60, "offline_threshold": 3}
        assert collect_job._select_due_devices(mem_db, cfg) == []

        collect_job.reset_device_schedule(mem_db, device.id)
        assert collect_job._select_due_devices(mem_db, cfg) == [device.id]


# ============================================================================
# 四、单设备采集全流程：_collect_one_device
# ============================================================================

class TestCollectOneDevice:
    """取值 → 写指标 → 回写状态 → 记日志，四步都要落到库里"""

    CFG = {"default_interval": 60, "offline_threshold": 3}

    def test_协议不支持时写失败日志且不累加失败次数(self, mem_db, monkeypatch):
        """
        协议不支持属于**配置问题**，不是设备故障。
        若在这里累加 fail_count，设备会被误判离线，
        现场排查方向会被彻底带偏（去查网络，其实是用错了协议）。
        """
        patch_scope(monkeypatch, mem_db)
        device = make_device(mem_db, protocol="nosuchproto")

        outcome = collect_job._collect_one_device(device.id, self.CFG)

        assert outcome["ok"] is False
        assert "暂不支持的采集协议" in outcome["reason"]

        mem_db.refresh(device)
        assert device.fail_count == 0
        assert device.online_status == "unknown"      # 状态未被改动

        log = mem_db.execute(select(CollectLog)).scalars().one()
        assert log.status == "fail"
        assert "暂不支持的采集协议" in (log.error_msg or "")

    def test_采集成功时指标入库并回写状态(self, mem_db, monkeypatch):
        patch_scope(monkeypatch, mem_db)
        device = make_device(mem_db, protocol="fake")

        collector = FakeCollector(CollectResult(
            success=True,
            online_status="online",
            duration_ms=12,
            metrics=[
                {"metric_key": "cpu_usage", "metric_value": 37.5, "unit": "%"},
                {"metric_key": "online_status", "str_value": "online"},
            ],
        ))
        monkeypatch.setattr(app.collectors.CollectorFactory, "get", staticmethod(lambda p: collector))

        outcome = collect_job._collect_one_device(device.id, self.CFG)

        assert outcome["ok"] is True
        assert outcome["metric_count"] == 2

        # 指标落到热表（大屏"当前值"的数据来源）
        latest = {
            row.metric_key: row
            for row in mem_db.execute(
                select(MetricLatest).where(MetricLatest.device_id == device.id)
            ).scalars().all()
        }
        assert latest["cpu_usage"].metric_value == pytest.approx(37.5)
        assert latest["online_status"].str_value == "online"

        # 设备状态回写：在线、失败计数清零、采集时间刷新
        mem_db.refresh(device)
        assert device.online_status == "online"
        assert device.fail_count == 0
        assert device.last_collect_at is not None
        assert device.last_online_at is not None

        log = mem_db.execute(select(CollectLog)).scalars().one()
        assert log.status == "success"
        assert log.metric_count == 2

    def test_采集失败累加失败次数但未达阈值不判离线(self, mem_db, monkeypatch):
        """一次网络抖动不应该让整屏设备变红，也不应该触发离线告警"""
        patch_scope(monkeypatch, mem_db)
        device = make_device(mem_db, protocol="fake")

        collector = FakeCollector(CollectResult(success=False, error="连接超时"))
        monkeypatch.setattr(app.collectors.CollectorFactory, "get", staticmethod(lambda p: collector))

        outcome = collect_job._collect_one_device(device.id, self.CFG)

        assert outcome["ok"] is False
        mem_db.refresh(device)
        assert device.fail_count == 1
        assert device.online_status == "unknown"       # 阈值 3，尚未判离线

    def test_连续失败达阈值判定离线并写状态变化日志(self, mem_db, monkeypatch):
        """
        状态变化日志的价值是回答"这台设备什么时候掉的"。
        只在真的变化时写——否则 200 台设备每分钟会产生 200 条无意义日志。
        """
        patch_scope(monkeypatch, mem_db)
        device = make_device(mem_db, protocol="fake", online_status="online")

        collector = FakeCollector(CollectResult(success=False, error="请求超时"))
        monkeypatch.setattr(app.collectors.CollectorFactory, "get", staticmethod(lambda p: collector))

        for _ in range(3):
            collect_job._collect_one_device(device.id, self.CFG)

        mem_db.refresh(device)
        assert device.fail_count == 3
        assert device.online_status == "offline"

        sys_logs = mem_db.execute(
            select(SysLog).where(SysLog.action == "device_status_change")
        ).scalars().all()
        assert len(sys_logs) == 1                      # 只在"online → offline"那一次写
        assert sys_logs[0].level == "WARNING"
        assert "online → offline" in sys_logs[0].message

    def test_状态未变化时不写系统日志(self, mem_db, monkeypatch):
        """这条用例守住"日志不被刷屏"：连续 3 次都离线，日志只应有一条"""
        patch_scope(monkeypatch, mem_db)
        device = make_device(mem_db, protocol="fake", online_status="offline", fail_count=5)

        collector = FakeCollector(CollectResult(success=False, error="超时"))
        monkeypatch.setattr(app.collectors.CollectorFactory, "get", staticmethod(lambda p: collector))

        for _ in range(3):
            collect_job._collect_one_device(device.id, self.CFG)

        assert mem_db.execute(
            select(SysLog).where(SysLog.action == "device_status_change")
        ).scalars().all() == []

    def test_设备已被删除时静默跳过(self, mem_db, monkeypatch):
        """挑选与执行之间设备被删除，属正常竞态，不应报错刷日志"""
        patch_scope(monkeypatch, mem_db)
        outcome = collect_job._collect_one_device(999999, self.CFG)
        assert outcome["ok"] is False
        assert outcome["reason"] == "设备已不存在"
        assert mem_db.execute(select(CollectLog)).scalars().all() == []

    def test_采集器抛异常不会逃逸到线程池(self, mem_db, monkeypatch):
        """
        第二道防线：函数内任何未预期异常都必须在此终止。
        否则异常会传进 Future，让人误以为是调度器的问题。
        """
        patch_scope(monkeypatch, mem_db)
        device = make_device(mem_db, protocol="fake")

        class BoomCollector(BaseCollector):
            protocol = "fake"

            def collect(self, device_id, params):
                raise RuntimeError("解析器内部炸了")

        monkeypatch.setattr(
            app.collectors.CollectorFactory, "get", staticmethod(lambda p: BoomCollector())
        )

        outcome = collect_job._collect_one_device(device.id, self.CFG)

        assert outcome["ok"] is False
        assert "RuntimeError" in outcome["reason"]

    def test_手动采集与定时采集走同一套逻辑(self, mem_db, monkeypatch):
        """
        "定时能采、手动采不到" 是现场最常见的坑，根源就是两套逻辑。
        本用例确认 collect_one_device_now 只是包了一层配置读取，
        采集路径与 _collect_one_device 完全一致。
        """
        patch_scope(monkeypatch, mem_db)
        device = make_device(mem_db, protocol="fake")

        collector = FakeCollector(CollectResult(
            success=True, online_status="online",
            metrics=[{"metric_key": "cpu_usage", "metric_value": 10.0, "unit": "%"}],
        ))
        monkeypatch.setattr(app.collectors.CollectorFactory, "get", staticmethod(lambda p: collector))

        result = collect_job.collect_one_device_now(device.id)

        assert result["ok"] is True
        assert result["metric_count"] == 1
        # 参数确实由 build_collect_params 组装（含 ip/timeout 等）
        assert collector.called_with[0]["ip"] == device.ip


# ============================================================================
# 五、任务入口：run_collect_job
# ============================================================================

class TestRunCollectJob:
    """每个 tick 只做"筛选 + 投递"，必须在毫秒级返回"""

    def _patch_executor(self, monkeypatch, executor: FakeExecutor) -> None:
        monkeypatch.setattr(collect_job, "get_executor", lambda: executor)

    def test_没有到点设备时直接返回(self, mem_db, monkeypatch):
        patch_scope(monkeypatch, mem_db)
        executor = FakeExecutor()
        self._patch_executor(monkeypatch, executor)
        make_device(mem_db, last_collect_at=datetime.now())

        summary = collect_job.run_collect_job()

        assert summary["due"] == 0
        assert summary["dispatched"] == 0
        assert executor.device_submissions == []
        assert executor.generic_submissions == []       # 无设备则不投递汇总任务

    def test_到点设备被逐台投递(self, mem_db, monkeypatch):
        patch_scope(monkeypatch, mem_db)
        executor = FakeExecutor()
        self._patch_executor(monkeypatch, executor)

        ids = [make_device(mem_db, name=f"设备{i}", last_collect_at=None).id for i in range(4)]

        summary = collect_job.run_collect_job()

        assert summary["due"] == 4
        assert summary["dispatched"] == 4
        assert summary["skipped"] == 0
        assert sorted(d for d, _ in executor.device_submissions) == sorted(ids)
        assert executor.started is True

    def test_上一轮未结束的设备被跳过(self, mem_db, monkeypatch):
        """
        防重入：慢设备（SSH 卡住）不应该占满线程池，
        否则正常设备反而采不到——这是压测最容易暴露的问题。
        """
        patch_scope(monkeypatch, mem_db)
        stuck = make_device(mem_db, name="卡住的设备", last_collect_at=None)
        ok_dev = make_device(mem_db, name="正常设备", last_collect_at=None)

        executor = FakeExecutor(reject=[stuck.id])
        self._patch_executor(monkeypatch, executor)

        summary = collect_job.run_collect_job()

        assert summary["due"] == 2
        assert summary["dispatched"] == 1
        assert summary["skipped"] == 1
        assert [d for d, _ in executor.device_submissions] == [ok_dev.id]

    def test_投递摘要包含耗时(self, mem_db, monkeypatch):
        patch_scope(monkeypatch, mem_db)
        executor = FakeExecutor()
        self._patch_executor(monkeypatch, executor)
        make_device(mem_db, last_collect_at=None)

        summary = collect_job.run_collect_job()

        assert isinstance(summary["duration_ms"], int)
        assert summary["duration_ms"] >= 0

    def test_线程池并发数与配置不一致时调整(self, mem_db, monkeypatch):
        """运维在页面上改了并发数，下一轮即生效（不重启平台）"""
        patch_scope(monkeypatch, mem_db)
        executor = FakeExecutor(max_workers=10)
        self._patch_executor(monkeypatch, executor)
        make_device(mem_db, last_collect_at=None)

        collect_job.run_collect_job()

        assert executor.resized_to == [60]              # 配置默认 60

    def test_配置里并发数填错时收敛到区间内(self, mem_db, monkeypatch):
        """
        配置值 = 99999 时不能真的开 99999 个线程（会 OOM）。
        注意：本用例依赖 _clamp 的区间，而不是去改配置表。
        """
        assert collect_job._clamp(99999, collect_job.POOL_SIZE_MIN, collect_job.POOL_SIZE_MAX) \
            == collect_job.POOL_SIZE_MAX

    def test_投递阶段异常被吞住并记入摘要(self, mem_db, monkeypatch):
        """
        调度任务自身抛异常会让 APScheduler 打印堆栈并影响可信度。
        异常必须被转成摘要里的 error 字段，让运维在平台上能看到"投递失败"。
        """
        def boom():
            raise RuntimeError("数据库连不上")

        monkeypatch.setattr(collect_job, "session_scope", boom)
        executor = FakeExecutor()
        self._patch_executor(monkeypatch, executor)

        summary = collect_job.run_collect_job()

        assert summary["dispatched"] == 0
        assert "数据库连不上" in summary["error"]


# ============================================================================
# 六、采集后告警评估：_evaluate_after_collect
# ============================================================================

class TestEvaluateAfterCollect:
    """
    校验与告警引擎的**调用契约**：
        evaluator.evaluate_devices(db, device_ids) -> {"fired": n, "recovered": m}

    这里注入假评估器，只验证"参数对不对、异常会不会外泄"。
    真实评估逻辑（连续 N 次、滞回恢复）由 tests/test_alarm.py 覆盖。
    """

    def _inject_evaluator(self, monkeypatch, recorder: Dict[str, Any], raises: bool = False):
        """
        把 app.alarm.evaluator 替换成一个假模块。

        为什么用 setattr 到包上而不是 sys.modules：
        `from app.alarm import evaluator` 的 IMPORT_FROM 字节码优先做属性查找，
        所以覆盖包属性才是生效的那一处；只改 sys.modules 会时灵时不灵。
        """
        def evaluate_devices(db, ids):
            recorder["device_ids"] = list(ids)
            if raises:
                raise RuntimeError("评估炸了")
            return {"fired": 1, "recovered": 0}

        module = type("FakeEvaluator", (), {"evaluate_devices": staticmethod(evaluate_devices)})()
        monkeypatch.setattr(app.alarm, "evaluator", module, raising=False)
        return module

    @staticmethod
    def _done_futures(count: int) -> List[Tuple[int, Future]]:
        futures: List[Tuple[int, Future]] = []
        for i in range(count):
            future: Future = Future()
            future.set_result({"device_id": i, "ok": True})
            futures.append((i, future))
        return futures

    def test_按设备ID列表调用评估器(self, mem_db, monkeypatch):
        patch_scope(monkeypatch, mem_db)
        recorder: Dict[str, Any] = {}
        self._inject_evaluator(monkeypatch, recorder)

        collect_job._evaluate_after_collect([1, 2, 3], self._done_futures(3))

        assert recorder["device_ids"] == [1, 2, 3]

    def test_空设备列表不触发评估(self, mem_db, monkeypatch):
        """没有设备采集时不该白跑一次规则匹配"""
        patch_scope(monkeypatch, mem_db)
        recorder: Dict[str, Any] = {}
        self._inject_evaluator(monkeypatch, recorder)

        collect_job._evaluate_after_collect([], [])

        assert "device_ids" not in recorder

    def test_评估异常不影响采集流程(self, mem_db, monkeypatch):
        """
        指标已经落库，下一轮还会再评估。
        告警评估失败绝不能反向污染采集——这是"低耦合"的具体体现。
        """
        patch_scope(monkeypatch, mem_db)
        recorder: Dict[str, Any] = {}
        self._inject_evaluator(monkeypatch, recorder, raises=True)

        collect_job._evaluate_after_collect([1], self._done_futures(1))   # 不应抛出

        assert recorder["device_ids"] == [1]

    def test_汇总任务等待未完成采集(self, mem_db, monkeypatch):
        """
        等待策略：wait() 加总超时。
        未完成的 future 不应让汇总任务永远卡住（否则线程池会被慢慢吃光）。
        """
        patch_scope(monkeypatch, mem_db)
        recorder: Dict[str, Any] = {}
        self._inject_evaluator(monkeypatch, recorder)
        monkeypatch.setattr(collect_job, "AGGREGATE_WAIT_SECONDS", 0.2)

        slow: Future = Future()                     # 永不完成
        done: Future = Future()
        done.set_result(None)

        collect_job._evaluate_after_collect([1, 2], [(1, slow), (2, done)])

        assert recorder["device_ids"] == [1, 2]     # 仍会评估（已完成设备的数据不该白采）


# ============================================================================
# 七、与调度器注册的对接
# ============================================================================

class TestSchedulerWiring:
    """采集任务必须被注册进 APScheduler，否则代码写得再好也不会跑"""

    def test_采集任务可被导入且返回字典(self, mem_db, monkeypatch):
        patch_scope(monkeypatch, mem_db)
        monkeypatch.setattr(collect_job, "get_executor", lambda: FakeExecutor())
        summary = collect_job.run_collect_job()
        assert isinstance(summary, dict)
        assert set(summary) >= {"due", "dispatched", "skipped", "duration_ms"}

    def test_任务入口不接收参数(self):
        """
        APScheduler 的 add_job 以无参形式调用（或按 id 传参）。
        若在入口加了必填位置参数，调度时会直接 TypeError。
        """
        import inspect

        params = inspect.signature(collect_job.run_collect_job).parameters
        required = [p for p in params.values() if p.default is inspect.Parameter.empty]
        assert required == []

    def test_聚合等待预算大于最慢一轮(self):
        """
        AGGREGATE_WAIT_SECONDS 要大于"最慢一轮"：
        200 台 / 60 并发 × 单台 10 秒超时 ≈ 34 秒，留一倍余量。
        这条断言防止有人随手把它改成 5 秒，导致告警评估长期漏设备。
        """
        assert collect_job.AGGREGATE_WAIT_SECONDS >= 60

    def test_最小采集周期不低于30秒(self):
        """守住建库/设备采集的基础设施安全水位"""
        assert collect_job.MIN_COLLECT_INTERVAL >= 30
