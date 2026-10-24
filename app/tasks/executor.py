"""
采集线程池
===============================================================================
为什么用线程池而不是 asyncio：
  采集依赖的 pysnmp / netmiko(paramiko) / pywinrm / pythonping 全是**同步阻塞**库，
  asyncio 需要逐个用 run_in_executor 包装，反而增加复杂度且没有性能收益。
  采集是 IO 密集型（等设备响应），GIL 在 IO 等待期间会释放，线程池足够支撑 200 台并发。

容量估算：
  thread_pool_size = 60，单台采集超时 10 秒
  → 理论吞吐 60 / 10s = 6 台/秒
  → 200 台设备最坏情况（全部超时）约 34 秒完成一轮
  → 与默认采集周期 60 秒匹配，不会出现"上一轮没跑完下一轮又来了"

防重入（重要）：
  平台层面为每台设备加"运行中"标记，上一轮未结束则本轮跳过。
  否则当某台设备持续超时时，任务会不断堆积，线程池被同一批慢设备占满，
  正常的设备反而采不到——这是压测中最容易暴露的问题。
===============================================================================
"""
from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Dict, Optional, Set

from app.core.config import settings
from app.core.logger import system_logger


class TaskExecutor:
    """采集专用线程池（支持运行期调整并发数）"""

    def __init__(self, max_workers: Optional[int] = None) -> None:
        self._lock = threading.RLock()
        self._max_workers = max_workers or settings.COLLECT_THREAD_POOL_SIZE
        self._executor: Optional[ThreadPoolExecutor] = None
        # 正在采集中的设备 ID 集合（防重入）
        self._running: Set[int] = set()

    # ---------------- 生命周期 ----------------

    def start(self) -> None:
        """启动线程池（幂等，重复调用不会创建多个池）"""
        with self._lock:
            if self._executor is None:
                self._executor = ThreadPoolExecutor(
                    max_workers=self._max_workers,
                    thread_name_prefix="collect",
                )
                system_logger.info("采集线程池已启动，并发数 %s", self._max_workers)

    def shutdown(self, wait: bool = False) -> None:
        """
        关闭线程池。

        :param wait: 是否等待正在执行的任务结束。
            平台正常退出传 True（让正在写的指标落库）；
            强制停止传 False（快速退出，未完成任务由 collect_log 记录为失败）。
        """
        with self._lock:
            if self._executor is not None:
                self._executor.shutdown(wait=wait)
                self._executor = None
                system_logger.info("采集线程池已关闭")

    def resize(self, max_workers: int) -> None:
        """
        调整并发数（配置变更时调用）。

        实现方式：新建线程池替换旧的（旧池 shutdown(wait=False)）。
        为什么不做"平滑扩容"：Python 的 ThreadPoolExecutor 不支持动态改 max_workers，
        而采集任务都是短任务（秒级），重建的代价可以忽略。
        """
        with self._lock:
            if max_workers == self._max_workers and self._executor is not None:
                return
            old = self._executor
            self._max_workers = max(1, int(max_workers))
            self._executor = ThreadPoolExecutor(
                max_workers=self._max_workers, thread_name_prefix="collect"
            )
            if old is not None:
                old.shutdown(wait=False)   # 旧任务自然执行完，不阻塞新池
            system_logger.info("采集线程池并发数调整为 %s", self._max_workers)

    # ---------------- 任务提交 ----------------

    def submit_device(self, device_id: int, fn: Callable[..., Any], *args: Any) -> Optional[Future]:
        """
        提交一台设备的采集任务（带防重入保护）。

        :return: Future；若该设备上一轮尚未结束则返回 None（本次跳过）
        """
        with self._lock:
            if self._executor is None:
                system_logger.warning("线程池未启动，忽略设备 %s 的采集任务", device_id)
                return None
            if device_id in self._running:
                # 防重入：慢设备（如 SSH 卡住）不应该占满线程池
                system_logger.debug("设备 %s 上一轮采集未结束，本轮跳过", device_id)
                return None
            self._running.add(device_id)

        future = self._executor.submit(fn, *args)

        def _cleanup(_fut: Future) -> None:
            """任务结束后释放防重入标记（无论成功失败都必须释放）"""
            with self._lock:
                self._running.discard(device_id)

        future.add_done_callback(_cleanup)
        return future

    def submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Future:
        """提交非设备类任务（如告警通知发送）"""
        with self._lock:
            if self._executor is None:
                self.start()
            assert self._executor is not None
            return self._executor.submit(fn, *args, **kwargs)

    # ---------------- 状态 ----------------

    @property
    def max_workers(self) -> int:
        return self._max_workers

    @property
    def running_count(self) -> int:
        """当前正在采集的设备数（大屏"采集中"提示用）"""
        with self._lock:
            return len(self._running)

    def stats(self) -> Dict[str, Any]:
        """线程池状态（系统监控页展示）"""
        with self._lock:
            return {
                "max_workers": self._max_workers,
                "running_devices": len(self._running),
                "started": self._executor is not None,
            }


# 进程级单例
_task_executor: Optional[TaskExecutor] = None
_executor_lock = threading.Lock()


def get_executor() -> TaskExecutor:
    """获取全局采集线程池（首次调用自动创建，不自动启动）"""
    global _task_executor
    if _task_executor is None:
        with _executor_lock:
            if _task_executor is None:
                _task_executor = TaskExecutor()
    return _task_executor
