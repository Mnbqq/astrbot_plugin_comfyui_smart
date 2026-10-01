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


# 内容过滤的默认词表：只放「明显露骨」的通用词，可按需在配置里覆盖
DEFAULT_NSFW_WORDS = (
    "nsfw", "nude", "naked", "topless", "explicit", "porn", "hentai",
    "裸体", "裸露", "露点", "色情", "全裸", "脱光",
)


def parse_feature_rules(text: str) -> tuple[dict[str, set], dict[str, set], set | None]:
    """解析「按范围的功能白名单」配置。

    每行一条规则，格式（大小写不敏感，`#` 开头是注释）：

        user:123456=t2i,i2v        某个用户可用哪些功能
        group:987654=t2i           某个群可用哪些功能
        default=t2i,i2i            其余人的可用集合（不写就用全局 features 开关）

    功能名：t2i / i2i / outpaint / inpaint / t2v / i2v / reverse_prompt，
    也可写 `all`（全部）或 `none`（全禁）。优先级：user > group > default > 全局开关。

    Args:
        text: 配置里的多行文本。

    Returns:
        (用户规则, 群规则, 默认规则)；默认规则为 None 表示没配。
    """
    users: dict[str, set] = {}
    groups: dict[str, set] = {}
    fallback: set | None = None
    for raw in str(text or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        left, _, right = line.partition("=")
        left = left.strip()
        names = {
            n.strip().lower()
            for n in right.replace("，", ",").replace("、", ",").split(",")
            if n.strip()
        }
        if "all" in names:
            features = set(FEATURE_NAMES)
        elif "none" in names:
            features = set()
        else:
            features = {n for n in names if n in FEATURE_NAMES}
        low = left.lower()
        if low.startswith("user:"):
            users[left.split(":", 1)[1].strip()] = features
        elif low.startswith("group:"):
            groups[left.split(":", 1)[1].strip()] = features
        elif low in ("default", "*"):
            fallback = features
    return users, groups, fallback


# 可用功能名（与 main.py 的 FEATURE_DEFAULTS 保持一致）
FEATURE_NAMES = ("t2i", "i2i", "outpaint", "inpaint", "t2v", "i2v", "reverse_prompt")


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
        # 视频独立配额：一条视频动辄几分钟，不该和出图同权
        self.video_daily_limit = int(conf.get("video_daily_limit", 0) or 0)
        self.video_cooldown = int(conf.get("video_cooldown", 0) or 0)
        # 按范围（用户/群）的功能白名单：见 parse_feature_rules
        self.feature_users, self.feature_groups, self.feature_default = parse_feature_rules(
            conf.get("feature_rules") or ""
        )
        # 内容过滤
        self.nsfw_filter = bool(conf.get("nsfw_filter", False))
        self.nsfw_notify = bool(conf.get("nsfw_notify", False))
        self.nsfw_negative = str(conf.get("nsfw_negative") or "").strip()
        words = [w.strip() for w in str(conf.get("nsfw_words") or "").splitlines() if w.strip()]
        self.nsfw_words = [w.lower() for w in (words or DEFAULT_NSFW_WORDS)]

    def _bypass(self, is_admin: bool) -> bool:
        """管理员是否豁免白名单/冷却/限额。"""
        return bool(is_admin and self.admin_bypass)

    def feature_override(self, user_id: str, group_id: str = "", *, is_admin: bool = False) -> set | None:
        """按范围查功能白名单。

        Args:
            user_id: 用户 id。
            group_id: 群 id（私聊留空）。
            is_admin: 是否管理员（管理员豁免时返回 None，即跟随全局开关）。

        Returns:
            该范围允许的功能集合；None 表示没有规则，跟随全局 features 开关。
        """
        if is_admin and self.admin_bypass:
            return None
        uid, gid = str(user_id), str(group_id or "")
        if uid and uid in self.feature_users:
            return set(self.feature_users[uid])
        if gid and gid in self.feature_groups:
            return set(self.feature_groups[gid])
        if self.feature_default is not None:
            return set(self.feature_default)
        return None

    def nsfw_hit(self, text: str) -> str:
        """内容过滤：命中返回命中的词，没命中返回空串。

        Args:
            text: 待检查的文本（用户描述或最终提示词）。

        Returns:
            命中的词（用于日志/提示），未命中为空串。
        """
        if not self.nsfw_filter or not text:
            return ""
        low = str(text).lower()
        for word in self.nsfw_words:
            if word and word in low:
                return word
        return ""

    async def check(
        self, user_id: str, *, is_admin: bool, storage, video: bool = False
    ) -> tuple[bool, str]:
        """检查用户是否可以使用出图。

        Args:
            user_id: 用户 id。
            is_admin: 是否 AstrBot 管理员（来自 event.is_admin()）。
            storage: Storage 实例，用于读取落盘的配额状态。
            video: 本次是视频任务（走视频独立配额与冷却）。

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

        cooldown = self.video_cooldown if video else self.cooldown
        daily_limit = self.video_daily_limit if video else self.daily_limit
        if not bypass and cooldown > 0:
            until = await storage.get_cooldown_until(uid)
            remaining = int(until - time.time())
            if remaining > 0:
                return False, self._t("perm.cooldown", seconds=remaining)

        if not bypass and daily_limit > 0:
            today = time.strftime("%Y-%m-%d")
            used = await storage.get_daily_count(uid, today, video=video)
            if used >= daily_limit:
                key = "perm.video_daily_limit" if video else "perm.daily_limit"
                return False, self._t(key, limit=daily_limit)

        return True, ""

    async def record(
        self, user_id: str, *, is_admin: bool, storage, video: bool = False
    ) -> None:
        """记录一次成功使用。

        Args:
            user_id: 用户 id。
            is_admin: 是否管理员。
            storage: Storage 实例。
            video: 本次是视频任务（同时记进视频配额桶）。
        """
        uid = str(user_id)
        until = 0.0
        cooldown = self.video_cooldown if video else self.cooldown
        # 管理员豁免冷却，不写冷却时间
        if cooldown > 0 and not self._bypass(is_admin):
            until = time.time() + cooldown
        await storage.record_usage(
            uid, time.strftime("%Y-%m-%d"), cooldown_until=until, video=video
        )

    def describe(self) -> dict:
        """返回当前生效的权限设置摘要，供 Pages 展示。"""
        return {
            "whitelist": sorted(self.whitelist),
            "blacklist": sorted(self.blacklist),
            "daily_limit": self.daily_limit,
            "cooldown_seconds": self.cooldown,
            "video_daily_limit": self.video_daily_limit,
            "video_cooldown": self.video_cooldown,
            "admin_bypass": self.admin_bypass,
            "feature_rules": {
                "users": {k: sorted(v) for k, v in self.feature_users.items()},
                "groups": {k: sorted(v) for k, v in self.feature_groups.items()},
                "default": sorted(self.feature_default) if self.feature_default is not None else None,
            },
            "nsfw_filter": self.nsfw_filter,
            "nsfw_notify": self.nsfw_notify,
            "nsfw_words": list(self.nsfw_words),
        }
