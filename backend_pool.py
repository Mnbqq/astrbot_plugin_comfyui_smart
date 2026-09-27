"""多后端调度：把任务派给最闲的那个 ComfyUI，坏掉的临时熔断。

为什么需要它
-----------
一台机器一张卡时，只要把 ComfyUI 地址填对就够了。但不少人手里有两台机器（或一个
ComfyUI 开了多个实例），希望「谁空谁上」；还有人夜里跑图，希望某台挂了不要连累所有人。

这个模块只做三件事：
1. **选一个后端**：按策略挑（默认挑队列最短的）；
2. **熔断**：提交/探测失败的后端先歇一会儿，别把下一个任务又送到坑里；
3. **如实汇报**：谁在线、队列多长、谁在熔断、熔断还有多久 —— 供 `/状态` 与配置页显示。

设计约定
--------
- 单后端（没配 `backends.endpoints`）时 `pick()` **不做任何探测**，直接返回主后端：
  常见部署的行为与没有这个功能时完全一致。
- 模型清单只从**主后端**发现（多机共享模型目录是常见前提）；
  如果某个后端缺模型，提交时会得到 ComfyUI 的明确报错，而不是静默换一个后端重试。
- 所有状态变更是同步的（不夹 `await`），asyncio 单线程下天然原子。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from .comfyui_api import normalize_base_url

# 可选策略：最闲 / 轮询 / 只用主后端
STRATEGIES = ("least_queue", "round_robin", "primary")


def parse_backend_specs(raw, primary_url: str) -> list[tuple[str, str]]:
    """把配置里的多后端清单解析成 `[(名称, 地址)]`，主后端永远排在第一位。

    每行支持 `地址` 或 `地址|名称` 两种写法（也容忍全角 `｜`、逗号分隔、`#` 注释）。
    同一个地址只保留第一次出现的名字。

    Args:
        raw: 配置里的清单（list 或字符串）。
        primary_url: `server.base_url`，作为第一个后端。

    Returns:
        `[(名称, 规范化地址)]`，至少包含主后端（地址为空时返回空列表）。
    """
    if isinstance(raw, str):
        items = [chunk for line in raw.splitlines() for chunk in line.split(",")]
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        items = []

    specs: list[tuple[str, str]] = []
    seen: set[str] = set()

    def _add(url: str, name: str) -> None:
        normalized = normalize_base_url(url)
        if not normalized or normalized in seen:
            return
        seen.add(normalized)
        specs.append((name.strip() or f"后端{len(specs) + 1}", normalized))

    _add(primary_url, "主")
    for item in items:
        text = str(item or "").strip()
        if not text or text.startswith("#"):
            continue
        url, _, name = text.replace("｜", "|").partition("|")
        _add(url, name)
    return specs


def is_backend_fault(message: str) -> bool:
    """这条报错是否说明「后端本身有问题」（据此决定要不要熔断）。

    只有连接不上、等待超时这类问题才该熔断；「你的工作流参数不对」是用户侧问题，
    把健康的后端拉黑只会让所有人一起排到更慢的机器上。

    Args:
        message: 面向用户的错误文本。

    Returns:
        是否属于后端故障。
    """
    text = str(message or "")
    marks = (
        "无法连接",
        "出图超时",
        "不支持该接口",
        "HTTP 404",
        "HTTP 5",
        "Connection",
        "connection",
        "timed out",
        "Timeout",
    )
    return any(mark in text for mark in marks)


@dataclass
class Backend:
    """一个 ComfyUI 后端。"""

    name: str
    url: str
    client: object = None
    benched_until: float = 0.0
    last_error: str = ""
    ok_count: int = 0
    fail_count: int = 0
    last_ok_at: float = 0.0
    busy: int = 0
    online: bool | None = None


class BackendPool:
    """后端池：选一个能用的，并且记住谁刚出过问题。"""

    def __init__(
        self,
        backends: list[Backend],
        *,
        strategy: str = "least_queue",
        fail_cooldown: float = 60.0,
        probe_timeout: float = 3.0,
        logger=None,
    ):
        """初始化后端池。

        Args:
            backends: 后端列表（第一个是主后端）。
            strategy: least_queue / round_robin / primary。
            fail_cooldown: 失败后的熔断时长（秒）。
            probe_timeout: 探测单个后端队列的超时（秒）。
            logger: 可选日志器。
        """
        self._backends = list(backends)
        self._logger = logger
        self._strategy = "least_queue"
        self._fail_cooldown = 60.0
        self._probe_timeout = 3.0
        self._cursor = 0
        self.configure(
            strategy=strategy, fail_cooldown=fail_cooldown, probe_timeout=probe_timeout
        )

    # ------------------------------------------------------------------ #
    # 配置
    # ------------------------------------------------------------------ #
    def configure(
        self,
        *,
        strategy: str | None = None,
        fail_cooldown: float | None = None,
        probe_timeout: float | None = None,
    ) -> None:
        """按最新配置更新策略与熔断参数（不重建后端）。"""
        if strategy is not None:
            chosen = str(strategy or "").strip().lower()
            self._strategy = chosen if chosen in STRATEGIES else "least_queue"
        if fail_cooldown is not None:
            self._fail_cooldown = max(0.0, float(fail_cooldown or 0))
        if probe_timeout is not None:
            self._probe_timeout = max(0.5, float(probe_timeout or 3.0))

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    @property
    def backends(self) -> list[Backend]:
        """全部后端（含熔断中的）。"""
        return self._backends

    @property
    def multi(self) -> bool:
        """是否真的配了多个后端（单后端时不必探测）。"""
        return len(self._backends) > 1

    def primary(self) -> Backend:
        """主后端（`server.base_url`）。"""
        return self._backends[0]

    def benched(self, backend: Backend, now: float | None = None) -> bool:
        """该后端是否在熔断中。"""
        moment = time.time() if now is None else now
        return backend.benched_until > moment

    def available(self) -> list[Backend]:
        """当前不在熔断中的后端。"""
        return [b for b in self._backends if not self.benched(b)]

    # ------------------------------------------------------------------ #
    # 成败反馈
    # ------------------------------------------------------------------ #
    def note_success(self, backend: Backend | None) -> None:
        """记一次成功：解除熔断。"""
        if backend is None:
            return
        backend.ok_count += 1
        backend.last_ok_at = time.time()
        backend.benched_until = 0.0
        backend.last_error = ""
        backend.online = True

    def note_failure(self, backend: Backend | None, reason: str = "") -> None:
        """记一次失败：熔断一段时间，并把原因留着给 `/状态` 看。"""
        if backend is None:
            return
        backend.fail_count += 1
        backend.last_error = str(reason or "")[:200]
        backend.online = False
        if self._fail_cooldown > 0:
            backend.benched_until = time.time() + self._fail_cooldown
        if self._logger is not None:
            self._logger.warning(
                "后端 %s（%s）失败，熔断 %.0f 秒：%s",
                backend.name, backend.url, self._fail_cooldown, backend.last_error,
            )

    # ------------------------------------------------------------------ #
    # 选后端
    # ------------------------------------------------------------------ #
    async def pick(self) -> Backend:
        """按策略挑一个后端。

        Returns:
            选中的后端；全部熔断或探测失败时回退到主后端（保证总有地方可提交，
            由主后端给出真实错误，而不是插件自己造一个）。
        """
        primary = self.primary()
        if not self.multi:
            return primary

        healthy = self.available()
        if not healthy:
            self._warn("所有后端都在熔断中，回退到主后端")
            return primary
        if self._strategy == "primary":
            return healthy[0]
        if self._strategy == "round_robin":
            self._cursor = (self._cursor + 1) % len(healthy)
            return healthy[self._cursor]

        loads = await asyncio.gather(
            *(self._probe(backend) for backend in healthy), return_exceptions=True
        )
        best: Backend | None = None
        best_load = 0
        for backend, load in zip(healthy, loads):
            if isinstance(load, Exception) or load is None:
                continue
            backend.busy = load
            backend.online = True
            backend.last_ok_at = time.time()
            if best is None or load < best_load:
                best, best_load = backend, load
        if best is None:
            self._warn("所有后端都探测失败，回退到主后端")
            return primary
        return best

    async def _probe(self, backend: Backend) -> int | None:
        """探测一个后端的负载；探测失败即熔断，返回 None。

        负载权重：正在执行的算 2 个，排队的算 1 个 —— 正在跑的那个还占着显存。
        """
        client = backend.client
        if client is None:
            return None
        try:
            status = await asyncio.wait_for(
                client.queue_status(strict=True), self._probe_timeout
            )
        except Exception as e:
            self.note_failure(backend, f"探测失败：{e}")
            return None
        return int(getattr(status, "total_running", 0)) * 2 + int(
            getattr(status, "total_pending", 0)
        )

    # ------------------------------------------------------------------ #
    # 汇报与收尾
    # ------------------------------------------------------------------ #
    def snapshot(self) -> list[dict]:
        """后端状态快照（供 `/状态` 与配置页显示）。"""
        now = time.time()
        rows: list[dict] = []
        for index, backend in enumerate(self._backends):
            benched_for = max(0.0, backend.benched_until - now)
            rows.append(
                {
                    "name": backend.name,
                    "url": backend.url,
                    "primary": index == 0,
                    "online": backend.online,
                    "busy": backend.busy,
                    "benched": benched_for > 0,
                    "benched_for": round(benched_for, 1),
                    "last_error": backend.last_error,
                    "ok": backend.ok_count,
                    "fail": backend.fail_count,
                }
            )
        return rows

    async def close(self) -> None:
        """关闭所有后端连接（插件卸载/重建时调用）。"""
        for backend in self._backends:
            client = backend.client
            if client is None:
                continue
            try:
                await client.close()
            except Exception:
                pass

    def _warn(self, message: str) -> None:
        """打一条调度告警（没有 logger 就安静跳过）。"""
        if self._logger is not None:
            self._logger.warning("%s", message)
