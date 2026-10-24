#!/usr/bin/env python3
"""
孤儿引用扫描（数据卫生巡检）
===============================================================================
**背景：为什么不用物理外键，就必须有这样一个脚本**

本项目有一条明确约定 —— **不建物理外键**，关联完整性由 Service 层维护。
这样做换来了 SQLite ↔ MySQL 的零改造迁移，但也接管了一份责任：
**级联删除不再由数据库保证，而是由每一处 delete 逻辑自己记得**。

漏掉一处的代价不是"报错"，而是**静默错误**，而且通常要等到很久以后才暴露：

  - 删设备漏清 `alert_rule_state` → 设备 ID 被复用后，新设备继承旧设备的告警状态，
    "一接入就被判为正在告警"，既不触发也不恢复 —— 设备坏了没人知道。
  - 删设备忘了处理"设备专属规则" → 规则看着是启用中，实际永远匹配不到设备，
    "配了规则却从没报过警"，且没有任何日志可查。
  - 删规则漏清状态 → 规则重建（新 ID）后老状态行留成垃圾，随时间无限堆积。

这些都不会在删除的那一刻报错，只会在某一天以"告警不准/告警不灵"的形式出现。
所以运维手册里的定期巡检应当包含本脚本 —— 它把"静默腐化"变成"一条可查的清单"。

**四类判定的唯一来源**

下面的四张清单（CLEANED / UNBOUND / KEPT / NOT_A_REF）就是本项目对
"带 device_id 的表，删设备时怎么办"的**唯一定义**：
  - 本脚本用它来决定哪些悬挂引用算"错误"、哪些算"正常历史"
  - tests/test_data_hygiene.py 直接 import 同一份清单做回归守卫
因此新增一张带 device_id 的表时只需要在**这一处**表态，两边同时生效；
若只顾着改测试、忘了这份清单，元数据一致性用例会直接失败。

用法::

    python scripts/check_orphans.py                 # 只报告，不修改（默认）
    python scripts/check_orphans.py --db data/itsm.db
    python scripts/check_orphans.py --fix           # 清理"必须清理"类别的孤儿行
    python scripts/check_orphans.py --json          # 机器可读输出（供监控采集）

退出码：0 = 无问题；1 = 发现问题（可接入监控告警）
===============================================================================
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

DEFAULT_DB = "data/itsm.db"


# ============================================================================
# 一、契约清单（唯一定义处，测试文件直接 import 这里）
# ============================================================================

#: 删设备时**必须清空**该设备的行（判据：与设备强绑定的运行时数据，脱离设备毫无意义，
#: 且部分表的 device_id 会被数据库复用 —— 详见 device_service.delete_device 的注释）
DEVICE_ID_CLEANED: Dict[str, str] = {
    "mon_device_credential": "设备凭据（口令密文，最该销毁的敏感数据）",
    "metric_latest": "最新指标快照（热表，UNIQUE(device_id,metric_key) 会被 UPSERT 复用）",
    "alert_rule_state": "规则运行状态（业务主键含 device_id，复用后会污染新设备的告警判断）",
}

#: 删设备时**解除绑定**（device_id 置 NULL）但行保留（判据：行主体是资产台账信息，设备只是它的一项属性）
DEVICE_ID_UNBOUND: Dict[str, str] = {
    "asset_info": "IT 资产台账：删设备只意味着‘该资产暂无对应监控设备’，不等于资产报废",
}

#: 删设备时**刻意保留**该设备的行（判据：历史事实，删掉就无法回溯"当时为什么告警"）
DEVICE_ID_KEPT: Dict[str, str] = {
    "metric_sample": "历史采样明细，趋势分析资料",
    "collect_log": "采集日志，排障与审计资料",
    "alert_event": "告警事件，告警时间线的本体",
    "alarm_log": "告警操作日志（含人工确认记录）",
}

#: 有 device_id 列但**不是设备引用**（判据：语义不同，不能按"设备引用"处理）
DEVICE_ID_NOT_A_REFERENCE: Dict[str, str] = {
    "alert_rule": "device_id 为 NULL 表示全局规则；设备专属规则按‘停用’处理（仅改 enabled）",
}

ALL_MANIFESTS: Tuple[Dict[str, str], ...] = (
    DEVICE_ID_CLEANED,
    DEVICE_ID_UNBOUND,
    DEVICE_ID_KEPT,
    DEVICE_ID_NOT_A_REFERENCE,
)

#: 父表 → 子表引用关系（用于除 device_id 之外的外键式引用扫描）
#: 格式：(子表, 子表列, 父表, 父表主键, 语义说明)
OTHER_REFERENCES: Tuple[Tuple[str, str, str, str, str], ...] = (
    ("alert_rule_state", "rule_id", "alert_rule", "id",
     "规则状态必须挂在真实规则上；规则删除时未清理状态会导致状态垃圾堆积"),
    ("alert_event", "rule_id", "alert_rule", "id",
     "告警事件保留历史，规则删除后 rule_id 成为历史引用 —— 属正常，仅提示"),
    ("alarm_log", "event_id", "alert_event", "id",
     "操作日志挂在告警事件上"),
    ("alert_notify_record", "event_id", "alert_event", "id",
     "推送记录挂在告警事件上"),
    ("asset_maintain_remind", "asset_id", "asset_info", "id",
     "维保提醒挂在资产上"),
)

#: 上表里允许出现悬挂引用的（历史引用，不算问题）
REFERENCE_TOLERATED: Set[str] = {"alert_event"}


# ============================================================================
# 二、扫描
# ============================================================================

class Finding:
    """一条发现。level: error（必须处理）/ warn（建议处理）/ info（知情即可）"""

    def __init__(self, level: str, table: str, column: str, count: int, message: str) -> None:
        self.level = level
        self.table = table
        self.column = column
        self.count = count
        self.message = message

    def as_dict(self) -> Dict[str, Any]:
        return {
            "level": self.level, "table": self.table, "column": self.column,
            "count": self.count, "message": self.message,
        }

    def __str__(self) -> str:
        mark = {"error": "✗ 错误", "warn": "! 警告", "info": "· 提示"}[self.level]
        return f"  [{mark}] {self.table}.{self.column}：{self.count} 行 —— {self.message}"


def _tables(conn: sqlite3.Connection) -> Set[str]:
    return {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _columns(conn: sqlite3.Connection, table: str) -> Set[str]:
    return {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}


def _count_dangling(
    conn: sqlite3.Connection, child: str, col: str, parent: str, parent_key: str
) -> int:
    """
    统计子表里指向不存在父行的行数。

    统一用 LEFT JOIN ... IS NULL 而不是 NOT IN：
    NOT IN 遇到 NULL 会整体返回空集（SQL 三值逻辑最经典的坑），
    而 device_id 这种列恰恰允许 NULL（如全局规则的 device_id），
    用 NOT IN 会把"有问题"扫成"没问题"。
    """
    sql = (
        f'SELECT COUNT(*) FROM "{child}" AS c '
        f'LEFT JOIN "{parent}" AS p ON p."{parent_key}" = c."{col}" '
        f'WHERE c."{col}" IS NOT NULL AND p."{parent_key}" IS NULL'
    )
    return conn.execute(sql).fetchone()[0]


def scan(conn: sqlite3.Connection) -> List[Finding]:
    """执行全部扫描，返回发现列表"""
    present = _tables(conn)
    findings: List[Finding] = []

    for table, reason in sorted(DEVICE_ID_CLEANED.items()):
        if table not in present or "device_id" not in _columns(conn, table):
            continue
        n = _count_dangling(conn, table, "device_id", "mon_device", "id")
        if n:
            findings.append(Finding(
                "error", table, "device_id", n,
                f"删设备未清理干净（{reason}）。用 --fix 清理，并检查 "
                f"device_service.delete_device() 是否漏了这张表",
            ))

    for table, reason in sorted(DEVICE_ID_UNBOUND.items()):
        if table not in present or "device_id" not in _columns(conn, table):
            continue
        n = _count_dangling(conn, table, "device_id", "mon_device", "id")
        if n:
            findings.append(Finding(
                "warn", table, "device_id", n,
                f"仍绑定到已删除的设备（{reason}）；建议置 NULL 以正确显示‘未绑定’",
            ))

    # 保留清单不扫 —— 悬挂引用是它们的正常状态，扫出来只会制造噪音
    # （把噪音当问题报，运维很快就会整体忽略这个脚本，脚本也就白写了）

    for table, reason in sorted(DEVICE_ID_NOT_A_REFERENCE.items()):
        if table not in present or "device_id" not in _columns(conn, table):
            continue
        n = _count_dangling(conn, table, "device_id", "mon_device", "id")
        if n:
            findings.append(Finding(
                "warn", table, "device_id", n,
                f"设备专属配置指向已删除的设备（{reason}）；"
                f"应已自动停用，请核对 enabled=0，避免‘规则看着生效、实际永不触发’",
            ))

    # 未表态的表：新加了带 device_id 的表却没更新清单
    declared = set().union(*[set(m) for m in ALL_MANIFESTS])
    for table in sorted(present):
        if "device_id" in _columns(conn, table) and table not in declared:
            findings.append(Finding(
                "error", table, "device_id", -1,
                "本表带 device_id 但未在 scripts/check_orphans.py 的清单里表态，"
                "删设备时的行为未定义；请补清单（并同步 device_service.delete_device）",
            ))

    for child, col, parent, parent_key, desc in OTHER_REFERENCES:
        if child not in present or parent not in present:
            continue
        if col not in _columns(conn, child):
            continue
        n = _count_dangling(conn, child, col, parent, parent_key)
        if n:
            level = "info" if child in REFERENCE_TOLERATED else "error"
            findings.append(Finding(level, child, col, n, desc))

    return findings


# ============================================================================
# 三、修复
# ============================================================================

def fix(conn: sqlite3.Connection) -> List[str]:
    """
    清理"必须清理"类别的孤儿行。

    ⚠ **只清 DEVICE_ID_CLEANED 里的表**，理由：
      - UNBOUND（资产）：应该置 NULL 而不是删行 —— 删行等于"删设备顺手删了资产台账"
      - KEPT（历史）：悬挂引用是设计意图，删掉就是销毁审计资料
      - NOT_A_REFERENCE（规则）：需要停用而非删除，语义上不是"清数据"
    自动化脚本最容易犯的错就是"顺手多删一点"，所以这里刻意做窄。
    """
    present = _tables(conn)
    actions: List[str] = []
    for table in sorted(DEVICE_ID_CLEANED):
        if table not in present or "device_id" not in _columns(conn, table):
            continue
        cur = conn.execute(
            f'DELETE FROM "{table}" WHERE "device_id" IS NOT NULL '
            f'AND "device_id" NOT IN (SELECT id FROM mon_device)'
        )
        if cur.rowcount:
            actions.append(f"清理 {table} 孤儿行 {cur.rowcount} 条")
    conn.commit()
    return actions


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="亿网通IT运维监控管理平台 —— 孤儿引用巡检")
    ap.add_argument("--db", default=DEFAULT_DB, help=f"SQLite 库文件，默认 {DEFAULT_DB}")
    ap.add_argument("--fix", action="store_true",
                    help="清理‘必须清理’类别的孤儿行（默认只报告）")
    ap.add_argument("--json", action="store_true", help="输出 JSON（供监控采集）")
    args = ap.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"库文件不存在：{db_path}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(str(db_path))
    try:
        findings = scan(conn)
        errors = [f for f in findings if f.level == "error"]

        if args.json:
            print(json.dumps(
                {"db": str(db_path), "findings": [f.as_dict() for f in findings],
                 "error_count": len(errors)},
                ensure_ascii=False, indent=2,
            ))
        else:
            print("=" * 72)
            print(f"孤儿引用扫描 | {db_path}")
            print("=" * 72)
            if not findings:
                print("  [OK ] 未发现孤儿引用")
            else:
                for f in findings:
                    print(f)
            if args.fix and errors:
                print("-" * 72)
                actions = fix(conn)
                # 修复后再扫一次：确认真的清干净了，而不是"执行了删除语句"就算完
                remaining = [f for f in scan(conn) if f.level == "error"]
                print("【修复】")
                for a in actions or ["无可自动清理项"]:
                    print(f"  -> {a}")
                print(f"  -> 复扫剩余错误：{len(remaining)} 项")
            print("=" * 72)
            print(f"合计 {len(findings)} 项发现（错误 {len(errors)}）")

        return 1 if errors else 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
