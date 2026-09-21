"""本地持久化：模型清单缓存、出图统计、配额状态。

与旧版的区别：
- 所有读写走同一把 asyncio 锁并采用「临时文件 + 原子替换」写入，避免两人同时出图时
  读-改-写互相覆盖（旧版是整文件重写，存在竞态）。
- 配额/冷却状态落盘，重启不再清零（旧版只在内存里，重启即失效）。
- 增加图片目录的保留策略，避免 output/ 无限膨胀。
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

EMPTY_STATS = {"model_usage": {}, "users": {}, "records": []}
EMPTY_QUOTA = {"daily": {}, "cooldown": {}}
MAX_RECORDS = 500


def _atomic_write(path: Path, payload) -> None:
    """原子写 JSON：先写临时文件再替换，避免半截文件。

    Args:
        path: 目标文件。
        payload: 可 JSON 序列化的对象。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(tmp, path)


def _read_json(path: Path, fallback):
    """读 JSON，失败或不存在时返回 fallback。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback


class Storage:
    """插件数据目录下的 JSON 持久化。"""

    def __init__(self, data_dir: Path):
        """初始化。

        Args:
            data_dir: 插件数据目录。
        """
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir = self.data_dir / "output"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.catalog_path = self.data_dir / "catalog.json"
        self.stats_path = self.data_dir / "stats.json"
        self.quota_path = self.data_dir / "quota.json"
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # 模型清单缓存（ComfyUI 不可达时仍能展示）
    # ------------------------------------------------------------------ #
    def load_catalog(self) -> dict[str, list[str]]:
        """读取上次发现的模型清单。

        Returns:
            {文件夹: [文件名...]}。
        """
        data = _read_json(self.catalog_path, {})
        if not isinstance(data, dict):
            return {}
        return {
            str(k): [str(x) for x in v if isinstance(x, str)]
            for k, v in data.items()
            if isinstance(v, list)
        }

    async def save_catalog(self, catalog: dict[str, list[str]]) -> None:
        """保存模型清单缓存。

        Args:
            catalog: {文件夹: [文件名...]}。
        """
        async with self._lock:
            _atomic_write(self.catalog_path, catalog)

    # ------------------------------------------------------------------ #
    # 统计与出图记录
    # ------------------------------------------------------------------ #
    def load_stats(self) -> dict:
        """读取统计信息。"""
        data = _read_json(self.stats_path, None)
        if not isinstance(data, dict):
            return json.loads(json.dumps(EMPTY_STATS))
        for key, default in EMPTY_STATS.items():
            if not isinstance(data.get(key), type(default)):
                data[key] = json.loads(json.dumps(default))
        return data

    async def record_generation(
        self,
        *,
        user_id: str,
        user_name: str,
        positive: str,
        negative: str,
        models: dict,
        images: list[str],
        seconds: float = 0.0,
        params: dict | None = None,
    ) -> None:
        """记录一次出图。

        Args:
            user_id: 触发者 id。
            user_name: 触发者昵称。
            positive: 正向提示词。
            negative: 负向提示词。
            models: 本次使用的模型 {"checkpoint":..., "lora":..., "vae":..., "template":...}。
            images: 图片相对引用列表，形如 images/xxx.png。
            seconds: 本次出图耗时。
            params: 出图参数（分辨率/步数/CFG/采样器/种子等），供画廊展示与复现。
        """
        async with self._lock:
            stats = self.load_stats()
            for key in ("checkpoint", "lora", "vae", "controlnet"):
                value = models.get(key)
                if value:
                    stats["model_usage"].setdefault(key, {})
                    stats["model_usage"][key][value] = (
                        stats["model_usage"][key].get(value, 0) + 1
                    )
            entry = stats["users"].setdefault(user_id, {"name": user_name, "count": 0})
            entry["count"] = int(entry.get("count", 0)) + 1
            entry["name"] = user_name
            record = {
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "user_id": user_id,
                "user_name": user_name,
                "positive": positive,
                "negative": negative,
                "template": models.get("template", ""),
                "model": models.get("checkpoint", ""),
                "seconds": round(seconds, 1),
                "images": images,
            }
            if isinstance(params, dict):
                record["params"] = params
            stats["records"].append(record)
            if len(stats["records"]) > MAX_RECORDS:
                stats["records"] = stats["records"][-MAX_RECORDS:]
            _atomic_write(self.stats_path, stats)

    async def clear_stats(self) -> None:
        """清空统计，供 Pages 使用。"""
        async with self._lock:
            _atomic_write(self.stats_path, json.loads(json.dumps(EMPTY_STATS)))

    # ------------------------------------------------------------------ #
    # 配额状态（落盘，重启不清零）
    # ------------------------------------------------------------------ #
    def _load_quota(self) -> dict:
        data = _read_json(self.quota_path, None)
        if not isinstance(data, dict):
            return json.loads(json.dumps(EMPTY_QUOTA))
        data.setdefault("daily", {})
        data.setdefault("cooldown", {})
        return data

    async def get_daily_count(self, user_id: str, day: str) -> int:
        """读取某用户当天的出图次数。

        Args:
            user_id: 用户 id。
            day: 日期字符串 YYYY-MM-DD。

        Returns:
            次数。
        """
        async with self._lock:
            quota = self._load_quota()
            return int((quota["daily"].get(day) or {}).get(user_id, 0))

    async def get_cooldown_until(self, user_id: str) -> float:
        """读取某用户的冷却截止时间戳。

        Args:
            user_id: 用户 id。

        Returns:
            Unix 时间戳；无冷却返回 0。
        """
        async with self._lock:
            quota = self._load_quota()
            try:
                return float(quota["cooldown"].get(user_id, 0) or 0)
            except (TypeError, ValueError):
                return 0.0

    async def record_usage(
        self, user_id: str, day: str, *, cooldown_until: float
    ) -> None:
        """记录一次使用：当天计数 +1 并写入冷却截止时间。

        Args:
            user_id: 用户 id。
            day: 日期字符串 YYYY-MM-DD。
            cooldown_until: 冷却截止时间戳。
        """
        async with self._lock:
            quota = self._load_quota()
            bucket = quota["daily"].setdefault(day, {})
            bucket[user_id] = int(bucket.get(user_id, 0)) + 1
            # 只保留最近 7 天的计数，避免文件无限增长
            for stale in sorted(quota["daily"])[:-7]:
                quota["daily"].pop(stale, None)
            if cooldown_until:
                quota["cooldown"][user_id] = cooldown_until
            _atomic_write(self.quota_path, quota)

    # ------------------------------------------------------------------ #
    # 图片保留
    # ------------------------------------------------------------------ #
    def prune_images(self, *, keep: int = 500, max_age_days: int = 30) -> int:
        """清理图片目录，超出保留策略的文件会被删除。

        Args:
            keep: 最多保留的文件数（按修改时间取最新）。
            max_age_days: 超过该天数的文件删除；<=0 表示只按数量保留。

        Returns:
            删除的文件数。
        """
        if not self.output_dir.is_dir():
            return 0
        files = [p for p in self.output_dir.iterdir() if p.is_file()]
        if not files:
            return 0
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        removed = 0
        deadline = (
            time.time() - max_age_days * 86400 if max_age_days and max_age_days > 0 else 0
        )
        for index, path in enumerate(files):
            too_many = keep > 0 and index >= keep
            too_old = bool(deadline) and path.stat().st_mtime < deadline
            if not (too_many or too_old):
                continue
            try:
                path.unlink()
                removed += 1
            except OSError:
                continue
        return removed
