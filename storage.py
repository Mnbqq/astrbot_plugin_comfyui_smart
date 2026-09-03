"""本地文件持久化：模型列表分析与统计信息。"""
import json
import time
from pathlib import Path


class Storage:
    """基于 JSON 文件的简单持久化。"""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.models_path = data_dir / "models_analyzed.json"
        self.stats_path = data_dir / "stats.json"
        self._ensure_files()

    def _ensure_files(self):
        if not self.models_path.exists():
            self.models_path.write_text(
                json.dumps({"checkpoint": [], "controlnet": [], "vae": [], "lora": []},
                           ensure_ascii=False, indent=2), encoding="utf-8")
        if not self.stats_path.exists():
            self.stats_path.write_text(
                json.dumps({"model_usage": {}, "users": {}, "records": [], "prompts": []},
                           ensure_ascii=False, indent=2), encoding="utf-8")

    # ---------- 模型列表 ----------
    def load_models(self) -> dict:
        try:
            return json.loads(self.models_path.read_text(encoding="utf-8"))
        except Exception:
            return {"checkpoint": [], "controlnet": [], "vae": [], "lora": []}

    def save_models(self, models: dict):
        self.models_path.write_text(
            json.dumps(models, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---------- 统计 ----------
    def load_stats(self) -> dict:
        try:
            return json.loads(self.stats_path.read_text(encoding="utf-8"))
        except Exception:
            return {"model_usage": {}, "users": {}, "records": [], "prompts": []}

    def save_stats(self, stats: dict):
        self.stats_path.write_text(
            json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")

    def record_generation(self, user_id: str, user_name: str, prompt: str,
                          negative: str, models: dict, images: list = None):
        """记录一次出图：模型调用次数、用户次数、正反向词、图片路径。"""
        stats = self.load_stats()
        # 模型调用次数
        for key in ("checkpoint", "controlnet", "vae", "lora"):
            val = models.get(key)
            if val:
                stats["model_usage"].setdefault(key, {})
                stats["model_usage"][key][val] = \
                    stats["model_usage"][key].get(val, 0) + 1
        # 用户次数
        stats["users"].setdefault(user_id, {"name": user_name, "count": 0})
        stats["users"][user_id]["count"] += 1
        stats["users"][user_id]["name"] = user_name
        # 记录（含时间戳、图片）
        record = {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "user_id": user_id,
            "user_name": user_name,
            "positive": prompt,
            "images": images or [],
        }
        stats["records"].append(record)
        # 正反向词
        stats["prompts"].append({
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "positive": prompt,
            "negative": negative,
        })
        # 限制列表长度，避免无限增长
        for key in ("records", "prompts"):
            if len(stats[key]) > 1000:
                stats[key] = stats[key][-1000:]
        self.save_stats(stats)