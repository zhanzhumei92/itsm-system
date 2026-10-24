"""
自定义定时任务测试（sys_scheduled_task + 动作注册器 + 调度热更新）
===============================================================================
覆盖重点：
  - 注册器：动作可注册、重复注册报错、未知动作被 Schema 拦截
  - Service：重名拦截、启停切换、删除、执行记录回写
  - 周期校验：cron 非法表达式在保存时报错（而不是等调度才失败）
  - 立即执行：直接调用 run_task_by_id（绕过线程，保证测试确定性）
调度器真正的"到点触发"由 APScheduler 保证，这里不测时间维度。
===============================================================================
"""
import pytest
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, SysScheduledTask
from app.scheduler import custom_tasks
from app.scheduler.custom_tasks import get_action, run_task_by_id
from app.schemas.system import ScheduledTaskIn
from app.services import scheduled_task_service as svc


@pytest.fixture()
def db_session(monkeypatch):
    """
    内存 SQLite 会话（与 test_alarm.mem_db 同一套做法：零残留）。

    额外把 custom_tasks.session_scope 指到本测试库：
    run_task_by_id 内部用 session_scope 自管会话，若不重定向，
    测试会写到生产 itsm.db 去——测试绝不允许碰生产库。
    """
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = factory()
    import contextlib

    @contextlib.contextmanager
    def fake_scope():
        yield session

    monkeypatch.setattr(custom_tasks, "session_scope", fake_scope)
    try:
        yield session
    finally:
        session.close()


# ---------------- 注册器 ----------------

def test_内置动作已注册():
    for key in ("cleanup_data", "refresh_maintain_remind", "collect_round", "sqlite_backup"):
        assert get_action(key) is not None, f"动作 {key} 应已注册"
    assert len(custom_tasks.list_actions()) >= 4


def test_动作重复注册报错():
    with pytest.raises(ValueError, match="重复注册"):
        custom_tasks.register_action("cleanup_data", "x", "y")(lambda: {})


def test_未知动作被schema拦截():
    with pytest.raises(Exception):
        ScheduledTaskIn(name="t1", action="not_exist", trigger_type="interval", interval_seconds=60)


# ---------------- 周期校验 ----------------

def test_cron非法表达式在保存时报错():
    with pytest.raises(Exception, match="cron"):
        ScheduledTaskIn(name="t2", action="cleanup_data", trigger_type="cron", cron_expr="99 * * *")


def test_interval缺少秒数报错():
    with pytest.raises(Exception, match="间隔秒数"):
        ScheduledTaskIn(name="t3", action="cleanup_data", trigger_type="interval")


def test_cron表达式可被APScheduler解析():
    payload = ScheduledTaskIn(name="t4", action="cleanup_data", trigger_type="cron",
                              cron_expr="30 3 * * *")
    # 与调度器 _build_trigger 同源：能构造出触发器即合法
    CronTrigger.from_crontab(payload.cron_expr, timezone="Asia/Shanghai")


# ---------------- Service CRUD ----------------

def _payload(name, **kw):
    base = dict(name=name, action="cleanup_data", trigger_type="interval",
                interval_seconds=60, enabled=True, remark=None)
    base.update(kw)
    return ScheduledTaskIn(**base)


def test_创建与重名拦截(db_session):
    created = svc.create_task(db_session, _payload("单测-清理任务"))
    assert created["id"] > 0 and created["action_label"] == "指标降采样与日志清理"
    with pytest.raises(svc.ScheduledTaskError, match="已存在"):
        svc.create_task(db_session, _payload("单测-清理任务"))


def test_启停与编辑(db_session):
    created = svc.create_task(db_session, _payload("单测-启停任务"))
    tid = created["id"]
    out = svc.toggle_task(db_session, tid, False)
    assert out["enabled"] is False
    out = svc.update_task(db_session, tid, _payload("单测-启停任务", interval_seconds=120))
    assert out["interval_seconds"] == 120
    row = db_session.get(SysScheduledTask, tid)
    assert row.interval_seconds == 120


def test_删除(db_session):
    created = svc.create_task(db_session, _payload("单测-删除任务"))
    tid = created["id"]
    svc.delete_task(db_session, tid)
    assert db_session.get(SysScheduledTask, tid) is None
    with pytest.raises(svc.ScheduledTaskError, match="不存在"):
        svc.delete_task(db_session, tid)


def test_立即执行并回写记录(db_session):
    """用 sqlite_backup 真动作：备份文件落盘 + last_run_* 三列被回写"""
    created = svc.create_task(db_session, _payload("单测-备份任务", action="sqlite_backup"))
    tid = created["id"]
    result = run_task_by_id(tid, operator="tester")
    assert result["ok"] is True, result
    row = db_session.get(SysScheduledTask, tid)
    assert row.last_run_at is not None
    assert row.last_run_ok is True
    assert "backup_file" in (row.last_run_message or "")
    svc.delete_task(db_session, tid)


def test_失败动作记录为不成功(db_session):
    """注册一个必然抛错的临时动作，验证失败也被如实记录（不能无声吞掉）"""
    custom_tasks.register_action("_unit_fail", "必然失败", "测试用")(
        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    try:
        created = svc.create_task(db_session, _payload("单测-失败任务", action="_unit_fail"))
        tid = created["id"]
        result = run_task_by_id(tid, operator="tester")
        assert result["ok"] is False
        row = db_session.get(SysScheduledTask, tid)
        assert row.last_run_ok is False
        assert "boom" in row.last_run_message
    finally:
        custom_tasks.ACTION_REGISTRY.pop("_unit_fail", None)
