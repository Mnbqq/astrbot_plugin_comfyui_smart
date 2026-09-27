"""权限与配额控制。

与旧版的区别：
- 管理员身份改用 AstrBot 自带的 `event.is_admin()`，不再自造 `admin_ids` 名单。
  旧版默认 admin_ids 为空，而唯一的授权入口 `/管理员 添加` 又要求「已是管理员」，
  新用户装完无法自举，`/刷新模型` 永远不可用 —— 这个死锁在这里被移除。
- 黑白名单/每日限额/冷却全部由配置承载并落盘，重启不再清零。
"""
from __future__ import annotations

import time

from .i18n import default_translator


class PermissionManager:
    """统一的权限与配额检查。"""

    def __init__(self, config: dict, translate=None):
        """初始化。

        Args:
            config: 插件配置里的 permission 段。
            translate: 可选的翻译函数（键 → 文案），用于多语言。
        """
        self._t = translate or default_translator().t
        self.reload(config)

    def reload(self, config: dict, translate=None) -> None:
        """按最新配置重建内部状态。

        Args:
            config: 插件配置里的 permission 段。
            translate: 可选的翻译函数；给了就换用它（配置页切语言后要跟上）。
        """
        if translate is not None:
            self._t = translate
        conf = config or {}
        self.whitelist = {str(x) for x in (conf.get("whitelist_user_ids") or []) if str(x).strip()}
        self.blacklist = {str(x) for x in (conf.get("blacklist_user_ids") or []) if str(x).strip()}
        self.daily_limit = int(conf.get("daily_limit", 0) or 0)
        self.cooldown = int(conf.get("cooldown_seconds", 0) or 0)
        self.admin_bypass = bool(conf.get("admin_bypass", True))

    def _bypass(self, is_admin: bool) -> bool:
        """管理员是否豁免白名单/冷却/限额。"""
        return bool(is_admin and self.admin_bypass)

    async def check(self, user_id: str, *, is_admin: bool, storage) -> tuple[bool, str]:
        """检查用户是否可以使用出图。

        Args:
            user_id: 用户 id。
            is_admin: 是否 AstrBot 管理员（来自 event.is_admin()）。
            storage: Storage 实例，用于读取落盘的配额状态。

        Returns:
            (是否放行, 拒绝原因)；放行时原因为空字符串。
        """
        uid = str(user_id)
        # 黑名单优先级最高，管理员也不例外
        if uid in self.blacklist:
            return False, self._t("perm.blacklist")

        bypass = self._bypass(is_admin)
        if self.whitelist and uid not in self.whitelist and not bypass:
            return False, self._t("perm.whitelist")

        if not bypass and self.cooldown > 0:
            until = await storage.get_cooldown_until(uid)
            remaining = int(until - time.time())
            if remaining > 0:
                return False, self._t("perm.cooldown", seconds=remaining)

        if not bypass and self.daily_limit > 0:
            today = time.strftime("%Y-%m-%d")
            used = await storage.get_daily_count(uid, today)
            if used >= self.daily_limit:
                return False, self._t("perm.daily_limit", limit=self.daily_limit)

        return True, ""

    async def record(self, user_id: str, *, is_admin: bool, storage) -> None:
        """记录一次成功使用。

        Args:
            user_id: 用户 id。
            is_admin: 是否管理员。
            storage: Storage 实例。
        """
        uid = str(user_id)
        until = 0.0
        # 管理员豁免冷却，不写冷却时间
        if self.cooldown > 0 and not self._bypass(is_admin):
            until = time.time() + self.cooldown
        await storage.record_usage(uid, time.strftime("%Y-%m-%d"), cooldown_until=until)

    def describe(self) -> dict:
        """返回当前生效的权限设置摘要，供 Pages 展示。"""
        return {
            "whitelist": sorted(self.whitelist),
            "blacklist": sorted(self.blacklist),
            "daily_limit": self.daily_limit,
            "cooldown_seconds": self.cooldown,
            "admin_bypass": self.admin_bypass,
        }
