"""
数据卫生测试：级联删除契约（第5步收尾时新增）
===============================================================================
**这个文件为什么存在**

第 5 步的真机验证中发现了一个真实缺陷：`device_service.delete_device()` 只清理了
凭据、最新指标和资产绑定，**漏掉了 `alert_rule_state`（规则运行状态）**。
而该模块的文档字符串里明明写着"级联清理：凭据、最新指标、规则状态、维保提醒绑定"。

这类缺陷有三个共同特征，决定了它不能靠"下次仔细点"来防：

  1. **它是"少写一行"，不是"写错一行"** —— 没有异常、没有日志、语法正确。
  2. **它不影响任何已有设备** —— 只有恰好复用了 device_id 才会显现。
  3. **它的两个定义（文档里的清单 vs 代码里的 delete 语句）分居两处**，
     Python 不做任何一致性检查。

所以本文件用的是**数据驱动**的写法，而不是"给 delete_device 写一个断言"：

    从 SQLAlchemy 元数据里把所有带 device_id 列的表全部扫出来，
    要求每一张表必须在下面的契约清单里**恰好出现一次**，
    再真的建库、真的插数据、真的调 delete_device，逐一核对结果。

这样一来，**将来任何人给系统加一张带 device_id 的表，这个测试都会失败**，
强制他回答"删设备时这张表怎么办"。这条链路上不会再出现第二个孤儿表 ——
这正是"用一个测试消灭一整族缺陷"而不是"修好眼前这一处"的区别。

⚠ **清单定义在 scripts/check_orphans.py，本文件 import 它，不重复维护**

运维侧需要一个能对**现场库**做巡检的工具（`scripts/check_orphans.py`），
测试侧需要一个能对**代码契约**做守卫的用例（本文件）—— 两边都需要同一份清单。
如果各写一份，就又变成"同一件事两处定义"，只不过这次的受害者是我们自己的运维脚本：
清单在测试里更新了、巡检脚本没更新，现场扫出来的是旧标准，等于没扫。
所以清单只有一处，两边共用。

运行：pytest tests/test_data_hygiene.py -v
===============================================================================
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Dict, Set

import pytest
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.orm import Session, sessionmaker

# 注意：`from app.models import Base` 会执行 app/models/__init__.py，
# 该文件显式导入全部模型 —— 这正是 _device_id_tables() 能扫全表的前提。
# 因此本文件不需要再逐个 import 具体模型类。
from app.models import Base
from app.models.alert import AlertRule, AlertRuleState
from app.models.asset import AssetInfo
from app.models.device import MonDevice
from app.models.log import SysLog
from app.services import alert_service, device_service
# 契约清单的唯一来源（见文件头说明）
from scripts.check_orphans import (
    ALL_MANIFESTS,
    DEVICE_ID_CLEANED,
    DEVICE_ID_KEPT,
    DEVICE_ID_NOT_A_REFERENCE,
    DEVICE_ID_UNBOUND,
)


# ============================================================================
# 一、契约清单（唯一定义在 scripts/check_orphans.py，本文件只做一致性守卫）
# ============================================================================
# 四张清单的语义（判据写在 check_orphans.py 里，这里只列名字便于对照阅读）：
#   DEVICE_ID_CLEANED         删设备时必须清空
#   DEVICE_ID_UNBOUND         删设备时解除绑定（置 NULL）但行保留
#   DEVICE_ID_KEPT            刻意保留（历史事实）
#   DEVICE_ID_NOT_A_REFERENCE 有 device_id 但不是设备引用
_ = (DEVICE_ID_CLEANED, DEVICE_ID_UNBOUND, DEVICE_ID_KEPT, DEVICE_ID_NOT_A_REFERENCE)


def _device_id_tables() -> Set[str]:
    """从元数据里扫出所有含 device_id 列的表名（表的增删自动被感知）"""
    return {t.name for t in Base.metadata.sorted_tables if "device_id" in t.c}


# ============================================================================
# 二、元数据一致性：清单必须与真实表结构**双向**吻合
# ============================================================================

class TestCascadeContract:
    """
    「清单」与「真实表结构」的一致性检查。

    ⚠ 为什么要**双向**：

      - 只查"表 → 清单"：新增了带 device_id 的表会失败 ✔，但清单里的死条目查不出来
      - 只查"清单 → 表"：清单里写了一张早已改名的表也不报错
    漏掉任何一向，清单都会慢慢腐烂成一份"看起来很像真的"的文档，
    最后没人敢信它 —— 一份不可信的清单比没有清单更糟。
    """

    def test_每张带device_id的表都已表态(self):
        declared = set().union(*[set(m) for m in ALL_MANIFESTS])
        missing = _device_id_tables() - declared
        assert not missing, (
            f"以下表带 device_id 列但没有表态删设备时如何处理：{sorted(missing)}。"
            f"请在 tests/test_data_hygiene.py 的清单里选一张并说明理由 —— "
            f"若选 DEVICE_ID_CLEANED，必须同时在 device_service.delete_device() 里补上清理。"
        )

    def test_清单里没有早已不存在的表(self):
        declared = set().union(*[set(m) for m in ALL_MANIFESTS])
        stale = declared - _device_id_tables()
        assert not stale, (
            f"以下清单条目对应的表已不存在或已不含 device_id 列：{sorted(stale)}。"
            f"请从清单里删除，避免清单逐渐失真。"
        )

    def test_四张清单互不重叠(self):
        names = [set(m) for m in ALL_MANIFESTS]
        overlap: Set[str] = set()
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                overlap |= names[i] & names[j]
        assert not overlap, f"同一张表出现在多张清单里，契约不自洽：{sorted(overlap)}"

    def test_清理清单必须都在源码里有对应delete(self):
        """
        清单说"要清理"，源码就必须真有那句 delete —— 这一条专治本次那个缺陷：
        文档承诺、代码不做的"幽灵清理"。用源码文本做粗粒度但有效的确认。
        """
        import inspect as _inspect
        src = _inspect.getsource(device_service.delete_device)
        assert "AlertRuleState" in src, "delete_device 没有清理 alert_rule_state（本轮真实缺陷）"
        assert "MetricLatest" in src, "delete_device 没有清理 metric_latest"
        assert "MonDeviceCredential" in src, "delete_device 没有清理设备凭据"


# ============================================================================
# 三、行为验证：真的删一台设备，逐表核对
# ============================================================================

@pytest.fixture()
def mem_db():
    """内存 SQLite（每个用例一个全新库，零残留、零真实网络、毫秒级）"""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _dummy_value(column: Any) -> Any:
    """
    为 NOT NULL 且无默认值的列生成一个合法占位值。

    ⚠ 刻意用类型推断而不是硬编码列名：这样**新加一列**时本文件不用改，
    否则"给新表补一个 dummy"又会变成一处需要人工维护的清单 —— 老问题重演。
    """
    t = column.type
    name = type(t).__name__.upper()
    if "INT" in name:
        return 1
    if "FLOAT" in name or "NUMERIC" in name or "REAL" in name:
        return 1.0
    if "BOOL" in name:
        return False
    if "DATE" in name or "TIME" in name:
        # ⚠ 返回 ISO 字符串而不是 datetime 对象：Python 3.12 起 sqlite3 的
        #    隐式 datetime 适配器被标记为废弃（会打 DeprecationWarning）。
        #    走原生 SQL 时由我们自己负责格式，正好绕开这个坑。
        return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if "JSON" in name:
        return {}
    return "x"


def _insert_minimal_row(db: Session, table: Any, device_id: int) -> None:
    """
    按真实表结构插入一行最小合法数据。

    ⚠ **为什么不能用"有 default 就跳过"这条看似合理的规则**

    模型的很多列（如 `alert_event.status`、`alert_rule.enabled`）只声明了
    **Python 侧默认值**（`mapped_column(default=...)`），**没有** `server_default`。
    Python 侧默认值由 SQLAlchemy 的工作单元在 `db.add(obj)` 时施加 ——
    而本函数刻意走原生 INSERT（为了不打印"必须提供哪些列"的清单），
    此时数据库那一侧**什么默认值都不知道**，NOT NULL 列会直接报约束失败。

    这正是"同一个语义在两处实现"的又一例：ORM 认为 status 有默认值、
    数据库认为 status 必填，两边都对，只是中间那条原生 SQL 不在任何一边的考虑范围内。
    因此判定条件是 `server_default`（数据库侧）而不是 `default`（Python 侧）。
    """
    values: Dict[str, Any] = {}
    for col in table.c:
        if col.name == "device_id":
            values[col.name] = device_id
            continue
        if col.primary_key:
            continue                                   # 自增主键交给数据库
        if col.nullable or col.server_default is not None:
            continue                                   # 可空 → NULL 合法；有库侧默认值 → 库会填
        values[col.name] = _dummy_value(col)
    cols = ", ".join(f'"{k}"' for k in values)
    marks = ", ".join(f":{k}" for k in values)
    db.execute(text(f'INSERT INTO "{table.name}" ({cols}) VALUES ({marks})'), values)


def _make_device(db: Session, name: str = "卫生测试设备", ip: str = "10.99.99.1") -> MonDevice:
    device = MonDevice(
        name=name, ip=ip, device_type="server", protocol="icmp",
        collect_interval=60, enabled=True,
    )
    db.add(device)
    db.commit()
    return device


def _row_count(db: Session, table_name: str, device_id: int) -> int:
    return db.execute(
        text(f'SELECT COUNT(*) FROM "{table_name}" WHERE device_id = :d'), {"d": device_id}
    ).scalar_one()


class TestDeleteDeviceCascade:
    """真删一台设备，逐一核对三张清单的承诺是否兑现"""

    def _seed(self, db: Session, device_id: int) -> None:
        """给每一张带 device_id 的表都塞一行，然后才能验证"该清的清了、该留的留了" """
        for table in Base.metadata.sorted_tables:
            if "device_id" in table.c:
                _insert_minimal_row(db, table, device_id)
        db.commit()

    def test_清理清单的表在删设备后清零(self, mem_db):
        device = _make_device(mem_db)
        self._seed(mem_db, device.id)

        device_service.delete_device(mem_db, device.id, operator="pytest")

        for table_name in sorted(DEVICE_ID_CLEANED):
            remain = _row_count(mem_db, table_name, device.id)
            assert remain == 0, (
                f"{table_name} 仍残留 {remain} 行（理由：{DEVICE_ID_CLEANED[table_name]}）"
            )

    def test_保留清单的表在删设备后原样保留(self, mem_db):
        device = _make_device(mem_db)
        self._seed(mem_db, device.id)

        device_service.delete_device(mem_db, device.id, operator="pytest")

        for table_name in sorted(DEVICE_ID_KEPT):
            remain = _row_count(mem_db, table_name, device.id)
            assert remain == 1, (
                f"{table_name} 应保留 1 行历史（理由：{DEVICE_ID_KEPT[table_name]}），实际 {remain} 行"
            )

    def test_解绑清单的表在删设备后行保留但引用置空(self, mem_db):
        """
        ⚠ 这一类最容易被"一刀切"处理错：
        资产台账和告警历史长得一样（都带 device_id），处理方式却完全相反 ——
        既不能删行（那是删设备顺手删了资产），也不能留引用（界面会显示一个不存在的设备）。
        """
        device = _make_device(mem_db)
        self._seed(mem_db, device.id)

        device_service.delete_device(mem_db, device.id, operator="pytest")

        for table_name in sorted(DEVICE_ID_UNBOUND):
            remain = _row_count(mem_db, table_name, device.id)
            assert remain == 0, f"{table_name} 不应再引用已删除的设备"
            total = mem_db.execute(
                text(f'SELECT COUNT(*) FROM "{table_name}"')
            ).scalar_one()
            assert total == 1, f"{table_name} 的行本身必须保留（只解除绑定，不删行）"

    def test_资产被解绑但资产本身保留(self, mem_db):
        device = _make_device(mem_db)
        asset = AssetInfo(asset_code="ZC-TEST-001", name="卫生测试资产", device_id=device.id)
        mem_db.add(asset)
        mem_db.commit()
        asset_id = asset.id

        device_service.delete_device(mem_db, device.id, operator="pytest")

        row = mem_db.execute(select(AssetInfo).where(AssetInfo.id == asset_id)).scalar_one_or_none()
        assert row is not None, "资产本身不能被删除，只应解除绑定"
        assert row.device_id is None, "资产应已解除与设备的绑定"

    def test_删除结果带计数_零残留时全是0(self, mem_db):
        device = _make_device(mem_db)
        self._seed(mem_db, device.id)
        mem_db.add(SysLog(level="INFO", module="test", action="probe", message="占位"))

        result = device_service.delete_device(mem_db, device.id, operator="pytest")

        assert set(result) == {"credential", "metric_latest", "rule_state", "rule_disabled"}
        assert result["credential"] == 1
        assert result["metric_latest"] == 1
        assert result["rule_state"] == 1

    def test_删除不存在的设备报404而_不静默通过(self, mem_db):
        from app.core.exceptions import NotFoundException
        with pytest.raises(NotFoundException):
            device_service.delete_device(mem_db, 999999)


class TestRuleStateOrphan:
    """
    ★ 本轮真实缺陷的回归守卫：设备 ID 复用后不得继承旧告警状态。

    这是数据卫生里最"咬人"的一种：它不报错、不影响现有设备，
    只在新设备拿到旧 ID 时让告警**静默失效**。
    """

    def _make_rule(self, db: Session, device_id: int | None, **overrides: Any) -> AlertRule:
        data: Dict[str, Any] = {
            "name": "卫生测试规则", "device_id": device_id, "metric_key": "cpu_usage",
            "operator": ">", "threshold": 80.0, "duration_n": 1, "recovery_n": 1,
            "level": "warning", "enabled": True,
        }
        data.update(overrides)
        rule = AlertRule(**data)
        db.add(rule)
        db.commit()
        return rule

    def test_删设备会清掉该设备全部规则状态(self, mem_db):
        device = _make_device(mem_db)
        rule_a = self._make_rule(mem_db, None)          # 全局规则
        rule_b = self._make_rule(mem_db, device.id)     # 专属规则

        # 两条规则在该设备上都留下了"正在告警中"的状态
        for rule in (rule_a, rule_b):
            mem_db.add(AlertRuleState(
                rule_id=rule.id, device_id=device.id, breach_count=3,
                normal_count=0, status="firing",
            ))
        mem_db.commit()
        assert mem_db.execute(select(func.count()).select_from(AlertRuleState)).scalar_one() == 2

        device_service.delete_device(mem_db, device.id)

        assert mem_db.execute(select(func.count()).select_from(AlertRuleState)).scalar_one() == 0

    def test_设备ID复用后新设备拿到全新状态(self, mem_db):
        """把缺陷场景完整演一遍：删设备 → 复用 ID → 新设备不得"继承告警中" """
        old = _make_device(mem_db, name="旧设备", ip="10.99.99.11")
        rule = self._make_rule(mem_db, None)
        mem_db.add(AlertRuleState(
            rule_id=rule.id, device_id=old.id, breach_count=5,
            normal_count=0, status="firing", current_event_id=1,
        ))
        mem_db.commit()
        old_id = old.id

        device_service.delete_device(mem_db, old_id)

        # SQLite 的 INTEGER PRIMARY KEY 即 rowid，删掉最大 ID 后新插入会重新拿到同一个值
        new = _make_device(mem_db, name="新设备", ip="10.99.99.12")
        assert new.id == old_id, "本例依赖 SQLite 复用 rowid 的行为；若不复用则场景不成立"

        state = alert_service.get_rule_state(mem_db, rule.id, new.id)
        assert state is None, (
            "新设备不应继承旧设备的规则状态 —— 否则它会一接入就被判为"
            "'已在告警中'，既不触发新告警也不发恢复通知"
        )

    def test_删设备只停用设备专属规则_不动全局规则(self, mem_db):
        device = _make_device(mem_db)
        scoped = self._make_rule(mem_db, device.id, name="专属规则")
        scoped_id = scoped.id
        global_rule = self._make_rule(mem_db, None, name="全局规则")
        global_id = global_rule.id

        device_service.delete_device(mem_db, device.id)

        scoped_after = alert_service.get_rule(mem_db, scoped_id)
        global_after = alert_service.get_rule(mem_db, global_id)
        assert scoped_after.enabled is False, "设备专属规则应被停用，避免‘看起来生效、实际永不触发’"
        assert global_after.enabled is True, "全局规则与具体设备无关，不得被牵连"

    def test_停用的专属规则重启用后恢复生效(self, mem_db):
        """停用而非删除的价值：设备重新接入后，规则能原样启用回来，不必凭记忆重建"""
        device = _make_device(mem_db)
        rule = self._make_rule(mem_db, device.id, name="专属规则")
        rule_id = rule.id

        device_service.delete_device(mem_db, device.id)
        assert alert_service.get_rule(mem_db, rule_id).enabled is False

        alert_service.toggle_rule(mem_db, rule_id, True)
        revived = alert_service.get_rule(mem_db, rule_id)
        assert revived.enabled is True
        assert revived.threshold == 80.0, "规则定义必须完整保留（停用不是删除）"

    def test_删规则仍按规则维度清理状态(self, mem_db):
        """对称性检查：delete_rule 按 rule_id 清，delete_device 按 device_id 清，两者互不干扰"""
        device = _make_device(mem_db)
        rule = self._make_rule(mem_db, device.id)
        other = self._make_rule(mem_db, None, name="另一条规则")
        mem_db.add(AlertRuleState(rule_id=rule.id, device_id=device.id, status="firing"))
        mem_db.add(AlertRuleState(rule_id=other.id, device_id=device.id, status="normal"))
        mem_db.commit()

        alert_service.delete_rule(mem_db, rule.id)

        rows = mem_db.execute(select(AlertRuleState)).scalars().all()
        assert len(rows) == 1 and rows[0].rule_id == other.id


class TestSchemaRegistration:
    """
    顺带守住"模型必须登记到 Base.metadata"这条约定。

    漏登记的表现是"启动正常、一查询报 no such table"，排查成本高、
    而检查成本几乎为零 —— 值得放进每次回归。
    """

    def test_所有模型都已登记且能建表(self, mem_db):
        inspector = inspect(mem_db.get_bind())
        existing = set(inspector.get_table_names())
        declared = {t.name for t in Base.metadata.sorted_tables}
        assert declared <= existing, f"以下模型未建表：{sorted(declared - existing)}"

    def test_本次涉及的核心表都在(self, mem_db):
        inspector = inspect(mem_db.get_bind())
        existing = set(inspector.get_table_names())
        for name in (
            "mon_device", "mon_device_credential", "metric_catalog", "metric_latest",
            "metric_sample", "alert_rule", "alert_rule_state", "alert_event",
            "alert_notify_record", "collect_log", "alarm_log", "sys_log",
            "asset_info", "asset_maintain_remind", "sys_user", "sys_config",
        ):
            assert name in existing, f"缺少核心表 {name}"

    def test_不使用物理外键_保证SQLite到MySQL零改造迁移(self, mem_db):
        """
        本项目约定：不建物理外键，关联完整性由 Service 层维护。
        物理外键在 SQLite 里默认不生效（需 PRAGMA foreign_keys=ON）、
        迁移到 MySQL 时又会带来删改顺序与锁的问题 —— 既然两边语义不同，干脆不用。
        """
        for table in Base.metadata.sorted_tables:
            assert not table.foreign_keys, (
                f"{table.name} 声明了物理外键 {[str(fk) for fk in table.foreign_keys]}，"
                f"违反本项目‘不使用物理外键’的约定"
            )
