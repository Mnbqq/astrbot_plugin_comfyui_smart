"""权限控制：白名单 / 黑名单 / 管理员 / 每日限额 / 冷却。"""
import time


class PermissionManager:
    """统一权限检查。"""

    def __init__(self, config: dict):
        self.conf = config or {}
        self.admin_ids = set(map(str, self.conf.get("admin_ids", [])))
        self.whitelist = set(map(str, self.conf.get("whitelist_user_ids", [])))
        self.blacklist = set(map(str, self.conf.get("blacklist_user_ids", [])))
        self.daily_limit = int(self.conf.get("daily_limit", 0) or 0)
        self.cooldown = int(self.conf.get("cooldown_seconds", 10) or 10)
        # 内存态：冷却表 + 每日计数表（按当天日期）
        self._cooldowns = {}
        self._daily = {}  # {date_str: {user_id: count}}

    def reload(self, config: dict):
        self.__init__(config)

    # ---------- 判断 ----------
    def is_admin(self, user_id: str) -> bool:
        return str(user_id) in self.admin_ids

    def check(self, user_id: str) -> tuple:
        """返回 (是否通过, 拒绝原因)。"""
        uid = str(user_id)
        # 管理员放行（但黑名单优先级最高，管理员可豁免黑名单？一般黑名单优先）
        # 黑名单优先
        if uid in self.blacklist:
            return False, "🚫 你已被加入黑名单，无法使用"
        # 白名单（留空表示不限制）
        if self.whitelist and uid not in self.whitelist and not self.is_admin(uid):
            return False, "🚫 你不在白名单中，无法使用"
        # 冷却
        now = time.time()
        last = self._cooldowns.get(uid, 0)
        if now - last < self.cooldown and not self.is_admin(uid):
            remain = int(self.cooldown - (now - last))
            return False, f"⏱️ 冷却中，请 {remain} 秒后再试"
        # 每日限额
        if self.daily_limit > 0 and not self.is_admin(uid):
            today = time.strftime("%Y-%m-%d")
            count = self._daily.get(today, {}).get(uid, 0)
            if count >= self.daily_limit:
                return False, f"📊 今日出图次数已达上限（{self.daily_limit} 次）"
        return True, ""

    def record_usage(self, user_id: str):
        """记录一次使用（更新冷却表和每日计数）。"""
        uid = str(user_id)
        self._cooldowns[uid] = time.time()
        today = time.strftime("%Y-%m-%d")
        self._daily.setdefault(today, {})
        self._daily[today][uid] = self._daily[today].get(uid, 0) + 1