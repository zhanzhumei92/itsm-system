"""
定时任务：设备采集轮询（每 10 秒一个 tick）
===============================================================================
这是整个平台的数据入口，也是并发与容错设计最集中的地方。

**执行流程**
    ┌─ run_collect_job()  ← APScheduler 每 10 秒调用一次（调度线程）
    │
    │  1. 读采集配置（线程池并发数、默认周期、离线阈值）
    │  2. 从库里挑出「已启用 + 到点」的设备（纯 Python 比对时间，见 _select_due_devices）
    │  3. 逐台投递给采集线程池（submit_device 自带防重入）
    │  4. 再投递一个「汇总任务」，它等本批 future 全部结束后触发告警评估
    └─ 立刻返回（不阻塞调度线程）

**为什么"挑设备"要放在调度线程、而"采集"放在线程池**
    一个 tick 的成本必须极低（毫秒级），否则 10 秒的 tick 会被上一轮拖住。
    挑设备只查一次库、做内存比对；真正的网络 IO 全部丢给线程池。
    这样即使 200 台设备全部超时，也只会占用 60 个采集线程，
    调度线程始终能按时醒来投递下一批。

**为什么用"汇总任务"触发告警评估，而不是另起一个定时任务**
    若把告警评估做成独立任务，它无法知道"本批采集是否写完"，
    会出现"读到的还是上一轮的旧值"或"同一批数据被评估两次"。
    汇总任务等待本批 future 完成后才执行，天然保证：
      - 评估读到的一定是本轮刚写入的 metric_latest
      - 同一台设备在同一轮内只被评估一次
    代价是它要占用一个采集线程等待（最多等一个采集周期），
    60 线程池下有足够余量。

**单设备失败隔离（需求硬指标）**
    每台设备的采集在**独立的 Session、独立的 try/except** 中执行：
      - 采集器内部已兜底，不会抛异常；这里是第二道防线
      - 数据库写失败只影响这一台设备，事务回滚不会波及其它设备
      - 无论成败都会写 collect_log，保证"为什么没数据"可追溯
===============================================================================
"""
from __future__ import annotations

import time
from concurrent.futures import Future
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import select

from app.core.database import session_scope
from app.core.logger import collect_logger, system_logger
from app.models.device import MonDevice
from app.services import config_service, device_service, log_service, metric_service
from app.tasks.executor import get_executor

# 单台设备的最小采集周期（秒）。
# 为什么要有下限：现场有人把周期填成 1 秒"想看实时曲线"，
# 结果是设备被采挂、库被写满。30 秒是网络设备 SNMP 轮询的行业下限。
MIN_COLLECT_INTERVAL = 30

# 汇总任务等待本批采集的最长时间（秒）。
# 取值要大于"最慢一轮"：200 台 / 60 并发 × 单台 10 秒超时 ≈ 34 秒，留一倍余量。
# 超时不是错误——未完成的设备下一轮会正常纳入，只是这一轮不参与告警评估。
AGGREGATE_WAIT_SECONDS = 120

# 线程池并发数的合理区间（sys_config 里填错时不至于把平台搞崩）
POOL_SIZE_MIN = 5
POOL_SIZE_MAX = 500


# ============================================================================
# 任务入口
# ============================================================================

def run_collect_job() -> Dict[str, Any]:
    """
    采集轮询主入口（由 APScheduler 调用）。

    :return: 本轮投递摘要（写系统日志，便于事后核对"某时刻平台在做什么"）
    """
    started = time.time()
    summary: Dict[str, Any] = {
        "due": 0, "dispatched": 0, "skipped": 0, "duration_ms": 0,
    }

    try:
        # ---- 1. 读配置 + 挑设备（一次短事务，尽快释放连接）----
        with session_scope() as db:
            collect_cfg = config_service.get_collect_settings(db)
            due_ids = _select_due_devices(db, collect_cfg)

            # 并发数支持运行期调整（运维在页面上改了配置，下一轮即生效）
            pool_size = _clamp(collect_cfg.get("thread_pool_size", 60), POOL_SIZE_MIN, POOL_SIZE_MAX)
            executor = get_executor()
            if pool_size != executor.max_workers:
                executor.resize(pool_size)

        summary["due"] = len(due_ids)
        if not due_ids:
            summary["duration_ms"] = int((time.time() - started) * 1000)
            return summary

        # ---- 2. 逐台投递（不在调度线程里做任何 IO）----
        executor.start()      # 幂等；进程刚起时调度器可能先于线程池启动
        futures: List[Tuple[int, Future]] = []
        for device_id in due_ids:
            future = executor.submit_device(device_id, _collect_one_device, device_id, collect_cfg)
            if future is None:
                # submit_device 返回 None 有两种情况：该设备上一轮还没结束（防重入），
                # 或线程池未启动。前者是设计预期，后者已被 start() 排除。
                summary["skipped"] += 1
                continue
            futures.append((device_id, future))

        summary["dispatched"] = len(futures)

        # ---- 3. 投递汇总任务：本批采完即评估告警 ----
        executor.submit(_evaluate_after_collect, [device_id for device_id, _ in futures], futures)

        summary["duration_ms"] = int((time.time() - started) * 1000)
        if summary["skipped"]:
            collect_logger.warning(
                "采集轮次：到点 %s 台，投递 %s 台，跳过 %s 台（上一轮未结束）",
                summary["due"], summary["dispatched"], summary["skipped"],
            )
        return summary

    except Exception as exc:  # noqa: BLE001
        # 调度任务的异常必须自行兜住：未捕获异常会让 APScheduler 打印堆栈，
        # 但不影响后续 tick；这里主动记录，让运维在平台上能看到"投递失败"
        system_logger.exception("采集任务投递失败：%s", exc)
        summary["error"] = str(exc)
        return summary


# ============================================================================
# 设备筛选
# ============================================================================

def _select_due_devices(db: Any, collect_cfg: Dict[str, int]) -> List[int]:
    """
    挑出「已启用 + 到点」的设备 ID 列表。

    **为什么在 Python 里比对时间，而不是写进 SQL**
      "到点"的判据是 last_collect_at + collect_interval <= now，
      而每台设备的 collect_interval 不同，SQL 里需要按行做时间加法：
        SQLite:  datetime(last_collect_at, '+' || collect_interval || ' seconds')
        MySQL:   DATE_ADD(last_collect_at, INTERVAL collect_interval SECOND)
      两套语法不通用，且 SQLite 的 datetime 是字符串、索引用不上，
      反而比"取回 200 行内存比对"更慢。
      设备数量在千级以内时，这个取舍是明确的：**换方言无关性，牺牲可忽略的开销**。
    代价是每轮会取回全部启用设备的三个字段——这就是下面只 select 所需列的原因。
    """
    default_interval = int(collect_cfg.get("default_interval") or 60)
    now = datetime.now()

    rows = db.execute(
        select(MonDevice.id, MonDevice.collect_interval, MonDevice.last_collect_at)
        .where(MonDevice.enabled.is_(True))
    ).all()

    due: List[int] = []
    for device_id, interval, last_collect_at in rows:
        if last_collect_at is None:
            # 从未采集过（新建/刚导入）→ 立即采一次，让运维马上看到数据
            due.append(device_id)
            continue
        effective = max(int(interval or default_interval), MIN_COLLECT_INTERVAL)
        if (now - last_collect_at).total_seconds() >= effective:
            due.append(device_id)
    return due


# ============================================================================
# 单设备采集（在采集线程池中执行）
# ============================================================================

def _collect_one_device(device_id: int, collect_cfg: Dict[str, int]) -> Dict[str, Any]:
    """
    采集一台设备：取值 → 写指标 → 回写状态 → 记日志。

    **本函数在子线程中运行，因此必须自己开 Session**
    SQLAlchemy 的 Session 不是线程安全的，跨线程复用会出现
    "Session is already flushing" 之类的诡异错误，且极难复现。
    每个设备一个短事务，也是"单设备失败不影响其它设备"的实现基础。

    :return: 该设备的采集摘要（仅用于日志与测试，不参与业务逻辑）
    """
    outcome: Dict[str, Any] = {"device_id": device_id, "ok": False}

    try:
        with session_scope() as db:
            device = db.get(MonDevice, device_id)
            if device is None:
                # 设备在挑选与执行之间被删除了。属正常竞态，静默跳过
                outcome["reason"] = "设备已不存在"
                return outcome

            protocol = device.protocol
            device_name = device.name
            device_ip = device.ip

            # ---- 1. 取采集器 ----
            # 延迟导入：避免 collectors ↔ services 的循环依赖
            from app.collectors import CollectorFactory

            collector = CollectorFactory.get(protocol)
            if collector is None:
                message = (
                    f"暂不支持的采集协议：{protocol}"
                    f"（当前已支持：{'/'.join(CollectorFactory.available_protocols()) or '无'}）"
                )
                log_service.write_collect_log(
                    db, device_id, device_name, device_ip, protocol,
                    status="fail", duration_ms=0, metric_count=0, error_msg=message,
                )
                # 协议不支持属于"配置问题"而非"设备故障"，
                # 不应累加 fail_count 把设备判成离线——那会掩盖真实原因
                outcome["reason"] = message
                return outcome

            # ---- 2. 组装参数（含解密口令）+ 执行采集 ----
            params = device_service.build_collect_params(db, device)
            result = collector.collect(device_id, params)

            # ---- 3. 写指标（仅成功且有数据时）----
            written = 0
            if result.success and result.metrics:
                try:
                    written = metric_service.ingest(db, device_id, result.metrics)
                except Exception as exc:  # noqa: BLE001
                    # 写库失败要单独标注：这与"采集失败"是两回事，
                    # 排查方向完全不同（一个是网络/凭据，一个是数据库）
                    collect_logger.exception(
                        "指标入库失败 | 设备=%s(%s) | %s", device_name, device_ip, exc
                    )
                    log_service.write_collect_log(
                        db, device_id, device_name, device_ip, protocol,
                        status="fail", duration_ms=result.duration_ms,
                        metric_count=0, error_msg=f"指标入库失败：{type(exc).__name__}: {exc}",
                    )
                    outcome["reason"] = "指标入库失败"
                    return outcome

            # ---- 4. 回写设备状态 ----
            # 先记下旧状态用于对比：update_device_status 会就地修改 device 对象，
            # 调用后再读就拿不到"变化前"的值了
            previous_online = device.online_status
            previous_fail_count = device.fail_count or 0

            device_service.update_device_status(
                db,
                device,
                online_status=result.online_status,
                success=result.success,
                offline_threshold=int(collect_cfg.get("offline_threshold") or 3),
            )

            # ---- 5. 写采集日志 ----
            log_service.write_collect_log(
                db, device_id, device_name, device_ip, protocol,
                status=result.status,
                duration_ms=result.duration_ms,
                metric_count=written,
                error_msg=result.error,
            )

            # ---- 6. 状态变化时记一条系统日志 ----
            # 只在"真的变了"时写：这段代码每轮每台设备都会执行，
            # 若不加判断，200 台设备每分钟会产生 200 条无意义的日志。
            # 记录价值在于回答"这台设备是什么时候掉的、掉了多久"。
            if device.online_status != previous_online:
                log_service.write_sys_log(
                    db=db,
                    level="WARNING" if device.online_status == "offline" else "INFO",
                    module="collector",
                    action="device_status_change",
                    message=(
                        f"设备在线状态变化：{device_name}({device_ip}) "
                        f"{previous_online} → {device.online_status}"
                        f"（连续失败 {previous_fail_count} → {device.fail_count or 0} 次，"
                        f"协议={protocol}，原因={result.error or '正常'}）"
                    ),
                )

            outcome.update({
                "ok": result.success,
                "status": result.status,
                "metric_count": written,
                "duration_ms": result.duration_ms,
                "error": result.error,
            })
            return outcome

    except Exception as exc:  # noqa: BLE001
        # 第二道防线：函数内任何未预期的异常（ORM、解密、第三方库怪异常）
        # 都必须在这里终止，绝不允许逃逸到线程池
        collect_logger.exception("设备 %s 采集任务异常：%s", device_id, exc)
        outcome["reason"] = f"{type(exc).__name__}: {exc}"
        return outcome


# ============================================================================
# 采集后处理：告警评估
# ============================================================================

def _evaluate_after_collect(
    device_ids: Sequence[int], futures: Sequence[Tuple[int, Future]]
) -> None:
    """
    等待本批采集完成后触发告警评估。

    等待策略：用 wait() 加总超时，而不是逐台 result()——
    逐台 result() 会在第一台卡住的设备上阻塞到底，
    而 wait() 能在总预算耗尽时立刻返回，让其余已完成设备的数据及时进入评估。
    """
    from concurrent.futures import wait

    pending = [f for _, f in futures]
    done_count = 0
    if pending:
        done, not_done = wait(pending, timeout=AGGREGATE_WAIT_SECONDS)  # noqa: F841
        done_count = len(futures) - len([f for f in pending if not f.done()])
        if len(done) < len(pending):
            collect_logger.warning(
                "本批采集有 %s 台未在 %s 秒内完成，本轮告警评估不含这些设备",
                len(pending) - len(done), AGGREGATE_WAIT_SECONDS,
            )

    if not device_ids:
        return

    # ---- 触发告警评估（容错导入：告警引擎与采集器分步交付）----
    try:
        from app.alarm import evaluator
    except ImportError as exc:
        collect_logger.debug("告警评估模块未加载，跳过本轮评估：%s", exc)
        return

    try:
        with session_scope() as db:
            stats = evaluator.evaluate_devices(db, list(device_ids))
        if stats.get("fired") or stats.get("recovered"):
            collect_logger.info(
                "告警评估完成（评估 %s 台，完成 %s 台）：新增告警 %s 条，恢复 %s 条",
                len(device_ids), done_count, stats.get("fired", 0), stats.get("recovered", 0),
            )
    except Exception as exc:  # noqa: BLE001
        # 告警评估失败不能影响采集——指标已经落库，下一轮还会再评估
        collect_logger.exception("告警评估失败：%s", exc)


# ============================================================================
# 手动采集（供"测试连接"之外的运维入口使用）
# ============================================================================

def collect_one_device_now(device_id: int) -> Dict[str, Any]:
    """
    立即采集一台设备（同步执行，供 API 的"立即采集"按钮调用）。

    与定时任务共用同一套逻辑，保证"手动采到"与"定时采到"的结果一致
    ——两套逻辑是现场最常见的"定时能采、手动采不到"类问题的根源。
    """
    with session_scope() as db:
        collect_cfg = config_service.get_collect_settings(db)
    return _collect_one_device(device_id, collect_cfg)


# ============================================================================
# 助手
# ============================================================================

def _clamp(value: Any, low: int, high: int) -> int:
    """把配置值收敛到合理区间（配置表里填错不该让平台出问题）"""
    try:
        return max(low, min(int(value), high))
    except (TypeError, ValueError):
        return low


def reset_device_schedule(db: Any, device_id: Optional[int] = None) -> int:
    """
    把设备的 last_collect_at 置空，让下一轮 tick 立刻采集。

    用途：运维在页面上改了设备的采集参数（如换了团体字），
    不希望等一整个采集周期才验证是否修好。返回受影响行数。
    """
    from sqlalchemy import update

    stmt = update(MonDevice).values(last_collect_at=None)
    if device_id is not None:
        stmt = stmt.where(MonDevice.id == device_id)
    result = db.execute(stmt)
    db.commit()
    return result.rowcount or 0
