"""出图并发闸门：限制「同时进行」的出图任务数，超出的按先来后到排队。

为什么需要它
-----------
ComfyUI 的 `/prompt` 是来者不拒的：多个人同时发 `/画图`，插件会把任务全部塞进
ComfyUI 的队列。队列一长，谁先出图、还要等多久就说不清楚（插件虽有按队列长度的
等待补偿，但补偿本身也有上限，见 `comfyui_api.py`），显存也容易被一波并发任务
同时冲击。这个闸门把「同时进行」卡在配置上限，超出的任务在**插件侧**排队，
并提示用户排在第几位；等前面的人做完再放行。

口径
----
「同时进行」= 从 `acquire()` 拿到名额，到 `release()` 归还名额，也就是
「提交给 ComfyUI + 等待出图 + 下载图片」的全过程。

实现约定（重要）
----------------
- 所有状态变更都是**同步**的（中间不夹 `await`），因此不需要锁：asyncio 只在
  `await` 点切换协程，同步代码段天然是原子的。
- 排队提示回调只是通知，抛异常不能让出图失败 —— 一律吞掉并记 debug 日志。
- 超时/取消时如果恰好在同一瞬间被放行，名额必须还回去，否则会永久泄漏一个名额。
"""
from __future__ import annotations

import asyncio
import inspect
import time
from collections import deque
from dataclasses import dataclass

# 闸门被关闭（插件卸载/重载）时，唤醒等待者的提示
CLOSED_MESSAGE = "插件正在重载，本次出图已取消，请稍后再发一次"


class QueueTimeout(RuntimeError):
    """在插件侧排队等待超过上限（消息面向用户）。"""


class QueueClosed(RuntimeError):
    """闸门已关闭（插件卸载/重载）：唤醒等待者，避免 handler 永久挂住。"""


@dataclass(frozen=True)
class Slot:
    """一次获准执行的名额，`release()` 时凭它归还。"""

    user_id: str
    position: int  # 进场时的位次，1 表示无需排队
    waited: float  # 在插件侧排队等待的秒数


@dataclass
class _Waiter:
    """排队中的等待者。"""

    user_id: str
    future: "asyncio.Future[Slot]"
    enqueued_at: float
    position: int


def _count_by_user(waiters) -> dict[str, int]:
    """统计每个用户的等待任务数（供状态展示）。"""
    counts: dict[str, int] = {}
    for waiter in waiters:
        counts[waiter.user_id] = counts.get(waiter.user_id, 0) + 1
    return counts


async def _safe_callback(callback, payload: dict, logger=None) -> None:
    """调用排队提示回调（同步/异步写法都支持），回调出错不影响出图。"""
    if callback is None:
        return
    try:
        result = callback(payload)
        if inspect.isawaitable(result):
            await result
    except Exception as e:
        if logger is not None:
            logger.debug("排队提示回调失败（不影响出图）：%s", e)


class ConcurrencyGate:
    """先来后到的出图名额闸门。"""

    def __init__(
        self,
        *,
        max_concurrent: int = 1,
        per_user_limit: int = 1,
        wait_timeout: float = 300.0,
        logger=None,
    ):
        """初始化闸门。

        Args:
            max_concurrent: 同时进行的出图数上限（小于 1 视为 1）。
            per_user_limit: 单个用户同时进行的上限，0 表示不限制。
            wait_timeout: 插件侧排队等待上限（秒），0 表示不限制。
            logger: astrbot.api.logger（可省略，仅用于回调失败的 debug 日志）。
        """
        self._logger = logger
        self._max = 1
        self._per_user = 1
        self._timeout = 300.0
        self._running: dict[str, int] = {}
        self._running_total = 0
        self._waiters: deque[_Waiter] = deque()
        self._closed = False
        self.configure(
            max_concurrent=max_concurrent,
            per_user_limit=per_user_limit,
            wait_timeout=wait_timeout,
        )

    # ------------------------------------------------------------------ #
    # 配置与生命周期
    # ------------------------------------------------------------------ #
    def configure(
        self,
        *,
        max_concurrent=None,
        per_user_limit=None,
        wait_timeout=None,
    ) -> None:
        """按最新配置调整上限（可在运行中调用，改完立刻放行等待者）。

        Args:
            max_concurrent: 同时进行的出图数上限。
            per_user_limit: 单个用户同时进行的上限，0 表示不限制。
            wait_timeout: 排队等待上限（秒），0 表示不限制。
        """
        if max_concurrent is not None:
            self._max = max(1, int(max_concurrent or 1))
        if per_user_limit is not None:
            self._per_user = max(0, int(per_user_limit or 0))
        if wait_timeout is not None:
            self._timeout = max(0.0, float(wait_timeout or 0))
        # 上限调大（或改为不限）后，等待者应当立刻进场，而不是等下一次 release
        self._pump()

    def resume(self) -> None:
        """重新打开闸门（插件再次激活时调用）。"""
        self._closed = False

    def shutdown(self, reason: str = "") -> int:
        """关闭闸门并唤醒所有等待者，返回被唤醒的数量。

        正在进行的任务不受影响（它们会照常跑完并归还名额）。

        Args:
            reason: 唤醒等待者时给用户看的原因。
        """
        self._closed = True
        waiters = list(self._waiters)
        self._waiters.clear()
        for waiter in waiters:
            if not waiter.future.done():
                waiter.future.set_exception(QueueClosed(reason or CLOSED_MESSAGE))
        return len(waiters)

    # ------------------------------------------------------------------ #
    # 名额
    # ------------------------------------------------------------------ #
    async def acquire(
        self,
        user_id: str,
        *,
        on_wait=None,
        timeout: float | None = None,
    ) -> Slot:
        """取一个出图名额；满员时排队等待。

        Args:
            user_id: 发起者 id（用于「单人上限」与状态展示）。
            on_wait: 需要排队时调用一次，参数是排队信息 dict
                （position / ahead / running / waiting / max_concurrent / reason）。
            timeout: 覆盖默认的排队等待上限（秒）。

        Returns:
            已获准执行的名额。

        Raises:
            QueueClosed: 闸门已关闭。
            QueueTimeout: 排队等待超时。
        """
        if self._closed:
            raise QueueClosed(CLOSED_MESSAGE)
        user_id = str(user_id or "anonymous")
        if self._can_start(user_id):
            self._start(user_id)
            return Slot(user_id=user_id, position=1, waited=0.0)
        waiter = _Waiter(
            user_id=user_id,
            future=asyncio.get_running_loop().create_future(),
            enqueued_at=time.monotonic(),
            # 位次要算上「正在跑的」：他们也在你前面，只数排队的人会少报
            position=self._running_total + len(self._waiters) + 1,
        )
        self._waiters.append(waiter)
        info = {
            "position": waiter.position,
            "ahead": waiter.position - 1,
            "running": self._running_total,
            "waiting": len(self._waiters),
            "max_concurrent": self._max,
            # 名额被前面的任务占着，还是被自己已有的任务占着 —— 提示语据此区分
            "reason": "capacity" if self._running_total >= self._max else "user",
        }
        await _safe_callback(on_wait, info, self._logger)
        limit = self._timeout if timeout is None else max(0.0, float(timeout))
        try:
            if limit:
                return await asyncio.wait_for(waiter.future, limit)
            return await waiter.future
        except asyncio.TimeoutError as exc:
            # 恰好在超时那一瞬间被放行时，名额已经算在我们头上，必须还回去
            if not self._remove_waiter(waiter):
                self._finish(user_id)
            raise QueueTimeout(self._timeout_message(info, limit)) from exc
        except asyncio.CancelledError:
            if not self._remove_waiter(waiter):
                self._finish(user_id)
            raise

    def release(self, slot: Slot | None) -> None:
        """归还名额并放行后续排队者（必须与 `acquire()` 成对，通常在 finally 里）。"""
        if slot is None:
            return
        self._finish(slot.user_id)
        self._pump()

    def hold(self, user_id: str, *, on_wait=None, timeout: float | None = None) -> "_Hold":
        """`async with gate.hold(uid) as slot:` 的写法糖，退出时自动归还名额。"""
        return _Hold(self, user_id, on_wait, timeout)

    def snapshot(self) -> dict:
        """当前并发状态（供 `/状态` 与配置页展示）。"""
        return {
            "max_concurrent": self._max,
            "per_user_limit": self._per_user,
            "wait_timeout": self._timeout,
            "running": self._running_total,
            "waiting": len(self._waiters),
            "running_by_user": dict(self._running),
            "waiting_by_user": _count_by_user(self._waiters),
            "closed": self._closed,
        }

    # ------------------------------------------------------------------ #
    # 内部（全部同步，见模块文档）
    # ------------------------------------------------------------------ #
    def _can_start(self, user_id: str) -> bool:
        """现在能否直接进场。"""
        if self._running_total >= self._max:
            return False
        if self._per_user > 0 and self._running.get(user_id, 0) >= self._per_user:
            return False
        return True

    def _start(self, user_id: str) -> None:
        """占用一个名额。"""
        self._running[user_id] = self._running.get(user_id, 0) + 1
        self._running_total += 1

    def _finish(self, user_id: str) -> None:
        """归还一个名额（重复归还不计数，避免把计数压成负数）。"""
        current = self._running.get(user_id, 0)
        if current <= 0:
            return
        if current == 1:
            self._running.pop(user_id, None)
        else:
            self._running[user_id] = current - 1
        self._running_total = max(0, self._running_total - 1)

    def _remove_waiter(self, waiter: _Waiter) -> bool:
        """把等待者从队列里摘掉；已经不在队列里（说明已被放行）返回 False。"""
        for index, item in enumerate(self._waiters):
            if item is waiter:
                del self._waiters[index]
                return True
        return False

    def _pump(self) -> None:
        """把队列里能进场的等待者放进来（无 await，调用期间不会被打断）。

        单人上限挡住队首时**跳过它**继续看后面的人：否则一个人连发十次
        `/画图` 就能把队伍堵死，所有人都得等他把十张图跑完。
        """
        if self._closed or not self._waiters:
            return
        # 已被超时/取消、但还没来得及从队列里摘掉的，先清掉
        if any(waiter.future.done() for waiter in self._waiters):
            self._waiters = deque(w for w in self._waiters if not w.future.done())
        admitted: list[_Waiter] = []
        planned: dict[str, int] = {}
        for waiter in self._waiters:
            if self._running_total + len(admitted) >= self._max:
                break
            if self._per_user > 0:
                used = self._running.get(waiter.user_id, 0) + planned.get(waiter.user_id, 0)
                if used >= self._per_user:
                    continue
            planned[waiter.user_id] = planned.get(waiter.user_id, 0) + 1
            admitted.append(waiter)
        for waiter in admitted:
            self._waiters.remove(waiter)
            self._start(waiter.user_id)
            waiter.future.set_result(
                Slot(
                    user_id=waiter.user_id,
                    position=waiter.position,
                    waited=max(0.0, time.monotonic() - waiter.enqueued_at),
                )
            )

    def _timeout_message(self, info: dict, limit: float) -> str:
        """排队超时给用户看的话（说清现状与去哪里调）。"""
        return (
            f"排队等待超过 {limit:.0f} 秒（前方还有 {info['ahead']} 个任务，"
            f"同时出图上限 {info['max_concurrent']}），本次出图已取消。"
            "可在插件配置的「出图队列与并发」里调大上限或等待时长"
        )


class _Hold:
    """`async with` 形式的占位对象：进入时取名额，退出时自动归还。"""

    def __init__(self, gate: ConcurrencyGate, user_id: str, on_wait, timeout):
        self._gate = gate
        self._user_id = user_id
        self._on_wait = on_wait
        self._timeout = timeout
        self._slot: Slot | None = None

    async def __aenter__(self) -> Slot:
        self._slot = await self._gate.acquire(
            self._user_id, on_wait=self._on_wait, timeout=self._timeout
        )
        return self._slot

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        self._gate.release(self._slot)
        self._slot = None
        return False
