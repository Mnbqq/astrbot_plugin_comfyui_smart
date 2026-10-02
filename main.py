"""AstrBot ComfyUI 智能绘图插件主入口。"""
from __future__ import annotations

import asyncio
import base64
import json
import random
import struct
import re
import time
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Image, Plain, Reply, Video
from astrbot.api.star import Context, Star, StarTools

from .backend_pool import Backend, BackendPool, is_backend_fault, parse_backend_specs
from .comfyui_api import (
    PROBE_FAILED,
    PROBE_OK,
    PROBE_UNKNOWN,
    ComfyUI,
    ComfyUIError,
    media_kind,
    normalize_base_url,
)
from .diagnostics import (
    check_requirements,
    collect_requirements,
    health_report,
    inspect_models,
)
from .i18n import build_translator
from .llm_service import LLMService
from .pages import register_pages_routes
from .permission import PermissionManager
from .queue_gate import ConcurrencyGate, QueueTimeout
from .storage import Storage
from .workflow_templates import (
    ARCH_PROFILES,
    add_hires_fix,
    profile_pixels,
    TemplateError,
    WorkflowTemplate,
    arch_profile,
    guess_arch,
    load_templates,
    pick_template,
)
from .error_hints import (
    build_probe_graph,
    describe_probe_failure,
    find_probe_targets,
    is_missing_weight_error,
    probe_cache_key,
    probe_summary,
)

PLUGIN_NAME = "astrbot_plugin_comfyui_smart"
# 与 metadata.yaml 的 version 保持一致（tests/test_logic.py 会校验二者不漂移）
PLUGIN_VERSION = "0.26.0"

# ControlNet 的深度预处理器权重是「按需下载」的（不在 models/ 下），
# 探测结果按（后端 + 节点 + 权重名）缓存这么久，避免每次出图都多一次往返。
PROBE_TTL_SECONDS = 600.0
# 深度预处理器不可用时，降级成图生图用的重绘幅度。
# 0.4~0.45 是「保住原图色调与构图、只重绘细节」的区间（真机实测：
# 0.40 色调几乎不变，0.55 开始换画风，0.7 以上接近重画）。
CONTROL_FALLBACK_DENOISE = 0.45
# 单次最多出几档 denoise（多档是串行跑的，档数直接乘等待时间）
MAX_DENOISE_LEVELS = 4

# 机器档位：决定分辨率/帧数/步数上限。auto 时按显存判定（内存太小再降一档）。
MACHINE_PRESETS = {
    "low": {
        "label": "低配（≤10G 显存 / ≤16G 内存）",
        "max_pixels": 832 * 480,      # 480p
        "max_length": 97,             # 约 4 秒 @24fps
        "steps_cap": 12,              # 蒸馏/少步模型够用
    },
    "mid": {
        "label": "标配（12~20G 显存 / 32G 内存）",
        "max_pixels": 1280 * 720,     # 720p
        "max_length": 121,            # 5 秒 @24fps
        "steps_cap": 30,
    },
    "high": {
        "label": "高配（≥24G 显存 / ≥64G 内存）",
        "max_pixels": 1280 * 720,
        "max_length": 161,            # 6.7 秒 @24fps
        "steps_cap": 50,
    },
}


def resolve_machine_tier(configured: str, vram_gb: float = 0.0, ram_gb: float = 0.0) -> str:
    """决定用哪一档：配置显式指定优先，否则按显存（内存太小再降一档）。

    Args:
        configured: 配置里的 video.machine（auto/low/mid/high）。
        vram_gb: 服务端显存总量（GB，取不到给 0）。
        ram_gb: 服务端内存总量（GB，取不到给 0）。

    Returns:
        "low" / "mid" / "high"。
    """
    tier = str(configured or "auto").strip().lower()
    if tier in MACHINE_PRESETS:
        return tier
    if vram_gb <= 0:
        # 探测不到显存/内存时按**最低档**处理：宁可保守（少一档分辨率），
        # 也不要在不认识的机器上按高配去跑
        return "low"
    if vram_gb <= 10:
        tier = "low"
    elif vram_gb < 22:
        tier = "mid"
    else:
        tier = "high"
    if 0 < ram_gb <= 20 and tier != "low":                # 内存不够再降一档
        tier = "low" if tier == "mid" else "mid"
    return tier


# 名字里带这些词的视频权重通常是「加速/蒸馏」版（步数少、CFG 1 单遍），
# 低配机器上默认挑它们能省几倍时间（实测 Turbo 4 步 74 秒 vs 原版 20 步 281 秒）
SPEED_HINTS = ("turbo", "lightning", "distill", "schnell", "flash", "lite", "fast")

# 功能开关：默认**只开文生图**，其余按需开启（缺省组也按这份默认值处理，
# 这样老配置升级上来同样是「只开文生图」，与配置页显示一致）
FEATURE_DEFAULTS = {
    "t2i": True,
    "i2i": False,
    "outpaint": False,
    "inpaint": False,
    "t2v": False,
    "i2v": False,
    "reverse_prompt": False,
    "control": False,
    "upscale": False,
}
# 开关名 -> (界面上的中文名, 对应指令)，用于「未开启」时的提示
FEATURE_LABELS = {
    "t2i": ("文生图", "/画图"),
    "i2i": ("图生图", "/图生图"),
    "outpaint": ("扩图", "/扩图"),
    "inpaint": ("局部重绘", "配置页的局部重绘"),
    "t2v": ("文生视频", "/视频"),
    "i2v": ("图生视频", "/图生视频"),
    "reverse_prompt": ("看图反推提示词", "/反推"),
    "control": ("ControlNet 深度控制", "/画图 --control depth"),
    "upscale": ("放大", "/放大"),
}


class FeatureDisabledError(ComfyUIError):
    """功能开关关掉了（继承 ComfyUIError，好让各指令的既有错误分支原样兜住）。"""


class ContentBlockedError(ComfyUIError):
    """描述命中了内容过滤词（同样继承 ComfyUIError，内部保险用）。"""


# 视频架构白名单：视频用途挑底模、以及「像不像视频模型」的校验都用它
VIDEO_ARCHES = frozenset({"wan", "wan22", "ltxv", "video"})

# 这些架构的模型动辄 4~20 GB，切换时最容易把提交内存顶满（真机 os error 1455 崩溃）
BIG_MEMORY_ARCHES = frozenset({
    "sdxl", "pony", "illustrious", "flux", "flux_schnell", "lumina2",
    "wan", "wan22", "video",
})
PLUGIN_DIR = Path(__file__).resolve().parent
BUILTIN_TEMPLATE_DIR = PLUGIN_DIR / "workflows"

# 行内参数别名 -> 规范名
PARAM_ALIASES = {
    "size": "size", "尺寸": "size", "分辨率": "size",
    "ratio": "ratio", "比例": "ratio",
    "宽": "width", "width": "width", "w": "width",
    "高": "height", "height": "height", "h": "height",
    "seed": "seed", "种子": "seed",
    "steps": "steps", "步数": "steps",
    "cfg": "cfg",
    "batch": "batch", "批次": "batch",
    "sampler": "sampler", "采样器": "sampler",
    "hires": "hires", "放大": "hires", "hires-denoise": "hires_denoise",
    "denoise": "denoise", "重绘": "denoise",
    "provider": "provider", "模型商": "provider",
    "draw": "draw", "画": "draw",
    "hires-steps": "hires_steps",
    "lora": "lora", "模型": "model", "model": "model",
    "negative": "negative", "负面": "negative",
    # 提示词优化开关（本次生效）：--llm 强制改写 / --no-llm 本次不改写
    "llm": "llm", "优化": "llm", "no-llm": "no_llm", "no_llm": "no_llm", "不优化": "no_llm",
    # ControlNet 控制 / 放大
    "control": "control", "控制": "control", "controlnet": "control",
    "control-model": "control_model", "control_model": "control_model", "控制模型": "control_model",
    "control-strength": "control_strength", "control_strength": "control_strength",
    "控制强度": "control_strength",
    "control-end": "control_end", "control_end": "control_end", "控制结束": "control_end",
    "scale": "scale", "倍数": "scale", "放大倍数": "scale",
    "upscale": "upscale", "放大": "upscale",
    # 放大到精确宽高（比按倍数更常用：壁纸/头像要的是固定尺寸）
    "scale-to": "scale_to", "scale_to": "scale_to", "放大到": "scale_to", "目标尺寸": "scale_to",
    # 体检加 --probe：把「按需下载权重」的预处理器真跑一遍
    "probe": "probe", "探测": "probe",
    # 扩图（outpaint）：左右上下扩展量与羽化
    "left": "left", "左": "left", "左边": "left",
    "right": "right", "右": "right", "右边": "right",
    "top": "top", "上": "top", "上边": "top",
    "bottom": "bottom", "下": "bottom", "下边": "bottom",
    "feather": "feather", "feathering": "feather", "羽化": "feather",
    # 视频（t2v / i2v）
    "seconds": "seconds", "second": "seconds", "时长": "seconds", "秒": "seconds",
    "fps": "fps", "帧率": "fps",
    "length": "length", "frames": "length", "帧数": "length",
}
RATIO_PRESETS = {
    "1:1": (1024, 1024), "16:9": (1344, 768), "9:16": (768, 1344),
    "4:3": (1152, 896), "3:4": (896, 1152), "3:2": (1216, 832),
    "2:3": (832, 1216), "21:9": (1536, 640),
}
# 采样尺寸必须是 8 的倍数
DIM_ALIGN = 8
# 扩图接缝羽化的默认值（ImagePadForOutpaint 的 feathering）
DEFAULT_OUTPAINT_FEATHER = 40
# 配置页局部重绘：单个 data URL 的体积上限（原图 + 遮罩各一份）
INPAINT_MAX_BYTES = 24 * 1024 * 1024
DIM_MIN = 64
DIM_MAX = 4096
MAX_PIXELS = 2048 * 2048


def _clamp_dimensions(width: int, height: int) -> tuple[int, int]:
    """把尺寸对齐到 8 的倍数并限制在合理范围与总像素上限内。

    Args:
        width: 期望宽度。
        height: 期望高度。

    Returns:
        (宽, 高)。
    """
    width = max(DIM_MIN, min(int(width), DIM_MAX))
    height = max(DIM_MIN, min(int(height), DIM_MAX))
    width -= width % DIM_ALIGN
    height -= height % DIM_ALIGN
    if width * height > MAX_PIXELS:
        scale = (MAX_PIXELS / (width * height)) ** 0.5
        width = max(DIM_MIN, int(width * scale) // DIM_ALIGN * DIM_ALIGN)
        height = max(DIM_MIN, int(height * scale) // DIM_ALIGN * DIM_ALIGN)
    return width, height


def parse_inline_params(text: str) -> tuple[str, dict]:
    """解析描述里的行内参数。

    支持 `--key value` 与 `key:value` 两种写法，支持中文别名，例如：
        /画图 16:9 一个白裙少女 --seed 42 --steps 30
        /画图 一个白裙少女 比例:16:9 lora:style/anime.safetensors:0.8

    Args:
        text: 去掉指令后的原始文本。

    Returns:
        (纯描述, 参数字典)。
    """
    opts: dict = {}
    tokens = text.split()
    kept: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        # --key value，以及无值旗标 --flag（例如 --画）
        if token.startswith("--") and len(token) > 2:
            key = PARAM_ALIASES.get(token[2:].lower())
            if key:
                nxt = tokens[index + 1] if index + 1 < len(tokens) else None
                if nxt is None or nxt.startswith("--"):
                    # 后面没有值（或紧跟下一个参数）：当成开关，标记为 "1"
                    opts[key] = "1"
                    index += 1
                    continue
                opts[key] = nxt
                index += 2
                continue
        # key:value（注意比例 16:9 这类值本身含冒号）
        if ":" in token:
            head, _, tail = token.partition(":")
            key = PARAM_ALIASES.get(head.lower())
            if key == "ratio" and tail:
                opts["ratio"] = f"{tail}"
                index += 1
                continue
            if key and key != "ratio" and tail:
                opts[key] = tail
                index += 1
                continue
        kept.append(token)
        index += 1
    return " ".join(kept).strip(), opts


def _extract_command_payload(event: AstrMessageEvent, *commands: str) -> str:
    """从消息里剥离指令本身，返回其余文本。

    Args:
        event: 消息事件。
        *commands: 该 handler 注册的指令名（含别名）。

    Returns:
        指令之后的文本。
    """
    text = (getattr(event, "message_str", "") or "").strip()
    if not text:
        return ""
    parts = text.split(None, 1)
    head = parts[0].lstrip("/").strip()
    if head in commands or any(head.startswith(c) for c in commands):
        return parts[1].strip() if len(parts) > 1 else ""
    return text


class ComfyUISmartPlugin(Star):
    """连接 ComfyUI，用一句中文出图。

    插件名称/作者/版本/简介一律以 `metadata.yaml` 为准：
    AstrBot 会通过 `Star.__init_subclass__` 自动识别 Star 子类，
    并读取 metadata.yaml 覆盖这些字段，因此**不需要**（也不应再使用）
    已废弃的 `@register` 装饰器 —— 去掉它可避免两处版本号各自漂移。
    """

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        """初始化插件。

        Args:
            context: AstrBot Star Context。
            config: 由 _conf_schema.json 生成的 AstrBotConfig（支持 save_config）。
        """
        super().__init__(context)
        # 日志统一走 AstrBot 官方 logger（模块顶层从 astrbot.api 导入，所有受支持版本均可用）。
        # 不引入标准库日志模块做回退：Star.logger 是 v4.27.3 才有的属性，不能作为唯一来源，
        # 但回退到标准库日志属上架违规 —— 官方 logger 在 4.26.0 起就已存在，无需回退。
        # 保留 AstrBotConfig 实例本身，绝不替换成普通 dict —— 否则无法落盘
        self.config: AstrBotConfig | dict = config if config is not None else {}

        # 官方数据目录（>=4.9.2 可用 self.name）
        try:
            self.data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception:
            self.data_dir = Path(__file__).resolve().parent / "data"
            self.data_dir.mkdir(parents=True, exist_ok=True)
        self.storage = Storage(self.data_dir)

        # 国际化：按官方约定读 .astrbot-plugin/i18n/*.json（配置项 language 可切）
        self.t = build_translator(PLUGIN_DIR, logger=logger)
        self._configure_i18n()
        self.permission = PermissionManager(
            self.config.get("permission", {}) or {}, translate=self.t
        )
        # 上一次用过的大模型家族（换家族前会先 /free，避免内存顶爆）
        self._last_big_arch = ""
        # 机器档位缓存（只探测一次：显存/内存总量）
        self._machine_tier = ""
        self.llm = LLMService(context, self.config, translate=self.t)
        # 后端池：单后端时等价于原来的单客户端；配了多个则按负载分配
        self._retired_pools: list[BackendPool] = []
        self.pool = self._build_pool()
        self.comfy = self.pool.primary().client
        # 出图并发闸门：限制同时交给 ComfyUI 的任务数，超出的在插件侧排队
        self.gate = ConcurrencyGate(logger=logger)
        self._configure_gate()
        self.templates: dict[str, WorkflowTemplate] = {}
        self.template_errors: list[str] = []
        # 在 __init__ 里就加载，避免任何早于 initialize() 的调用（Pages /模板列表）看到空模板
        self._load_templates()
        self._cancel = asyncio.Event()
        # 提示词过长提示每次加载只发一次，避免每条出图都刷屏（日志里仍每次都记）
        self._prompt_note_shown = False
        # 正在进行的任务：prompt_id -> 触发者 id（`/取消` 据此判断「谁的任务」）
        self._active_jobs: dict[str, str] = {}
        # 每个在途任务一个取消信号，供 `/取消` 定点打断（与插件卸载的全局信号分开）
        self._job_cancel: dict[str, asyncio.Event] = {}
        # 每个在途任务落在哪个后端：多后端时取消/中断必须找对那台
        self._job_backend: dict[str, Backend] = {}
        # 预处理器探测结果：(后端, 节点类, 权重名) -> (时间戳, 是否可用, 失败原因)
        self._probe_cache: dict[tuple, tuple[float, bool, str]] = {}

        # 注册插件 Pages 的后端 API。
        # 注意：必须在 __init__ 里调用——漏掉的话，配置页/模型页/状态页的每个请求
        # 都会被 Dashboard 回以「未找到该路由」。
        self.pages_ready = False
        try:
            self.pages_ready = bool(register_pages_routes(self))
        except Exception as e:
            # Pages 不可用不应让聊天指令一起失效，但必须在日志里显式报错（不是 warning）
            logger.error(
                "插件 Pages 路由注册失败，配置页/状态页将无法使用：%s", e, exc_info=True
            )

        logger.info("ComfyUI 智能绘图已加载，数据目录：%s", self.data_dir)

    # ------------------------------------------------------------------ #
    # 配置与生命周期
    # ------------------------------------------------------------------ #
    def _build_client(self, base_url: str = "") -> ComfyUI:
        """按当前配置构造 ComfyUI 客户端。

        Args:
            base_url: 显式指定地址（多后端时用）；留空则用 `server.base_url`。
        """
        server = self.config.get("server", {}) or {}
        return ComfyUI(
            normalize_base_url(base_url or server.get("base_url", "http://127.0.0.1:8188")),
            int(server.get("timeout", 180) or 180),
            poll_interval=float(server.get("poll_interval", 1.5) or 1.5),
            max_tasks_ahead=int(server.get("max_tasks_ahead", 10) or 10),
            logger=logger,
        )

    def _build_pool(self) -> BackendPool:
        """按配置构造后端池：第一个是 `server.base_url`，其余来自 `backends.endpoints`。

        单后端时池里只有一项，`pick()` 不做任何探测 —— 与没有多后端功能时行为一致。
        """
        server = self.config.get("server", {}) or {}
        primary_url = normalize_base_url(server.get("base_url", "http://127.0.0.1:8188"))
        conf = self.config.get("backends", {}) or {}
        specs = parse_backend_specs(conf.get("endpoints"), primary_url)
        if not specs:
            specs = [("主", primary_url)]
        backends = [
            Backend(name=name, url=url, client=self._build_client(url)) for name, url in specs
        ]
        return BackendPool(
            backends,
            strategy=conf.get("strategy") or "least_queue",
            fail_cooldown=conf.get("fail_cooldown", 60),
            logger=logger,
        )

    async def _flush_retired_pools(self) -> None:
        """关闭被配置重建换下来的旧后端池（否则每次保存配置都漏一个 session）。"""
        while self._retired_pools:
            pool = self._retired_pools.pop()
            try:
                await pool.close()
            except Exception:
                pass

    @property
    def user_template_dir(self) -> Path:
        """用户自带模板目录（位于数据目录，插件更新不会覆盖）。"""
        return self.data_dir / "workflows"

    def _configure_i18n(self) -> None:
        """按配置项 `language` 选定语言（auto=跟随 AstrBot 界面语言，拿不到就用中文）。"""
        wanted = str((self.config.get("general", {}) or {}).get("language") or "auto")
        if wanted.strip().lower() == "auto":
            try:
                global_conf = self.context.get_config() or {}
                wanted = str(
                    global_conf.get("language") or global_conf.get("locale") or ""
                )
            except Exception:
                wanted = ""
        self.t = build_translator(PLUGIN_DIR, locale=wanted, logger=logger)

    def _configure_gate(self) -> None:
        """按最新配置更新出图并发闸门（同时上限 / 单人上限 / 排队等待时长）。"""
        queue_conf = self.config.get("queue", {}) or {}
        self.gate.configure(
            max_concurrent=queue_conf.get("max_concurrent", 1),
            per_user_limit=queue_conf.get("per_user_limit", 1),
            wait_timeout=queue_conf.get("wait_timeout", 300),
        )

    def reload_components(self) -> None:
        """按最新配置重建各组件（配置变更后调用）。"""
        self._configure_i18n()
        self.permission.reload(self.config.get("permission", {}) or {}, translate=self.t)
        self.llm = LLMService(self.context, self.config, translate=self.t)
        old_client = self.comfy
        old_pool = self.pool
        self.pool = self._build_pool()
        self.comfy = self.pool.primary().client
        # 继承旧的模型缓存，避免改配置后要重新全量扫描
        self.comfy._model_cache = getattr(old_client, "_model_cache", {})
        self.comfy._model_cache_at = getattr(old_client, "_model_cache_at", 0.0)
        # 旧池里的连接由下一次 _flush_retired_pools()/terminate() 收口（这里不能 await）
        self._retired_pools.append(old_pool)
        self._configure_gate()
        self._load_templates()

    def _load_templates(self) -> None:
        """加载内置模板与用户自带模板（用户同名模板优先）。

        把上次拉到的 `/object_info` 传下去：用户放的**界面格式**工作流要靠它
        把界面控件值映射到真实输入名（没有也能转，靠节点自带的 widget 信息与内置表）。
        """
        self.user_template_dir.mkdir(parents=True, exist_ok=True)
        object_info = getattr(self.comfy, "_object_info", None) or None
        templates = load_templates(BUILTIN_TEMPLATE_DIR, object_info=object_info)
        user_templates = load_templates(self.user_template_dir, object_info=object_info)
        templates.update(user_templates)
        self.templates = templates

        # 收集坏模板，供 /模板列表 暴露
        errors: list[str] = []
        for directory in (self.user_template_dir, BUILTIN_TEMPLATE_DIR):
            for path in sorted(directory.glob("*.json")):
                if path.stem in user_templates or path.stem in templates:
                    continue
                errors.append(path.name)
        self.template_errors = errors

    async def initialize(self) -> None:
        """插件激活时调用。"""
        self._load_templates()
        await self._flush_retired_pools()
        # 插件可能被禁用后再启用（同一实例）：把闸门重新打开，否则所有出图都会被判为「已关闭」
        self.gate.resume()
        self._configure_gate()
        # 启动横幅：一眼确认「跑的是哪一版、加载了几个模板、日志走哪条路径」。
        # 排查「装的是新版还是旧版」这类问题时非常省事。
        logger.info(
            "ComfyUI 智能绘图 v%s 已激活｜模板 %d 个｜数据目录 %s｜"
            "日志走 astrbot.api.logger｜Pages %s｜排队补偿上限 %s 个任务｜同时出图上限 %d",
            PLUGIN_VERSION,
            len(self.templates),
            self.data_dir,
            "已注册" if self.pages_ready else "注册失败（配置页与状态页将不可用）",
            self.comfy.max_tasks_ahead,
            self.gate.snapshot()["max_concurrent"],
        )
        keep = int((self.config.get("output") or {}).get("keep_images", 500) or 500)
        age = int((self.config.get("output") or {}).get("image_max_age_days", 30) or 30)
        removed = self.storage.prune_images(keep=keep, max_age_days=age)
        if removed:
            logger.info("已清理 %d 张过期图片", removed)
        self._sync_llm_tool()

    async def terminate(self) -> None:
        """插件被禁用/重载时调用：收口连接与在途任务。"""
        self._cancel.set()
        # 先把排队等名额的人唤醒（否则这些 handler 会一直挂到排队超时）
        woken = self.gate.shutdown("插件正在重载，本次出图已取消，请稍后再发一次")
        if woken:
            logger.info("插件卸载：已取消 %d 个排队中的出图请求", woken)
        # 在途任务：先置取消信号（等待循环会立刻退出），再打断 ComfyUI 端
        for job_event in self._job_cancel.values():
            job_event.set()
        for prompt_id in list(self._active_jobs):
            backend = self._job_backend.get(prompt_id)
            client = backend.client if backend is not None else self.comfy
            try:
                await client.interrupt(prompt_id)
            except Exception:
                pass
        self._active_jobs.clear()
        self._job_cancel.clear()
        self._job_backend.clear()
        await self.pool.close()
        await self._flush_retired_pools()
        logger.info("ComfyUI 智能绘图已卸载")

    # ------------------------------------------------------------------ #
    # Pages 后端接口
    # ------------------------------------------------------------------ #
    def get_full_config(self) -> dict:
        """返回完整配置（供 Pages 读取）。"""
        return dict(self.config)

    async def save_config(self, payload: dict) -> dict:
        """保存配置并落盘。

        采用**深度合并**而非整体替换：前端只提交它管理的字段，未提交的字段保持原值。
        这样即使配置页漏了某个配置项，也不会被静默清空（旧版正是整体替换导致
        output.save_image 每次保存都被抹掉）。

        Args:
            payload: 前端提交的配置片段。

        Returns:
            保存结果摘要。

        Raises:
            ValueError: payload 非法。
            RuntimeError: 配置对象不支持保存。
        """
        if not isinstance(payload, dict):
            raise ValueError("配置格式不正确")

        merged = _deep_merge(dict(self.config), payload)
        # 就地更新，保留 AstrBotConfig 实例身份，否则 dashboard 侧会脱钩
        if isinstance(self.config, dict):
            self.config.clear()
            self.config.update(merged)
        else:  # pragma: no cover - 兜底
            self.config = merged

        saved = False
        save_async = getattr(self.config, "save_config_async", None)
        if callable(save_async):
            saved = bool(await save_async())
        else:
            save_sync = getattr(self.config, "save_config", None)
            if callable(save_sync):
                save_sync()
                saved = True
        if not saved:
            raise RuntimeError("当前 AstrBot 版本的配置对象不支持保存，请升级 AstrBot")

        self.reload_components()
        await self._flush_retired_pools()
        self._sync_llm_tool()
        logger.info("配置已通过 Pages 更新并落盘")
        return {"saved": True, "keys": sorted(payload.keys())}

    def _sync_llm_tool(self) -> None:
        """按配置启用/停用「无指令出图」工具。"""
        enabled = bool((self.config.get("agent") or {}).get("enable_llm_tool", False))
        try:
            if enabled:
                self.context.activate_llm_tool("generate_image")
            else:
                self.context.deactivate_llm_tool("generate_image")
        except Exception as e:
            logger.debug("切换 LLM 工具状态失败：%s", e)

    async def refresh_models(self) -> dict:
        """重新发现 ComfyUI 模型并写入缓存。

        Returns:
            {"ok": bool, "catalog": {...}, "total": int, "folders": int, "message": str}
        """
        self.comfy.invalidate_model_cache()
        try:
            catalog = await self.comfy.discover_models(max_age=0)
        except ComfyUIError as e:
            return {"ok": False, "message": str(e), "catalog": {}, "total": 0, "folders": 0}
        total = sum(len(v) for v in catalog.values())
        if total == 0:
            return {
                "ok": False,
                "message": "没有发现任何模型。请确认 ComfyUI 地址正确，且 models 目录下已放入模型",
                "catalog": {},
                "total": 0,
                "folders": 0,
            }
        await self.storage.save_catalog(catalog)
        return {
            "ok": True,
            "catalog": catalog,
            "total": total,
            "folders": len(catalog),
            "message": f"已发现 {total} 个模型，覆盖 {len(catalog)} 个文件夹",
        }

    async def get_catalog(self) -> dict[str, list[str]]:
        """取当前模型清单：优先实时发现，失败时回退到磁盘缓存。"""
        try:
            catalog = await self.comfy.discover_models()
            if catalog:
                return catalog
        except ComfyUIError:
            pass
        return self.storage.load_catalog()

    async def get_server_status(self) -> dict:
        """返回服务器状态，供 Pages 与 /状态 使用。"""
        info = {
            "base_url": self.comfy.base_url,
            "online": False,
            "templates": [t.describe() for t in self.templates.values()],
            # 插件侧并发闸门：配置页状态栏与 /状态 都读它
            "gate": self.gate.snapshot(),
            # 多后端：每个后端的在线/队列/熔断情况
            "backends": self.pool.snapshot(),
        }
        info["features"] = self.enabled_features()
        # 机器档位（配置页状态面板与 /状态 都会显示）：探测失败不影响状态返回
        try:
            machine = await self._machine()
            info["machine"] = {
                "tier": machine["tier"],
                "label": machine["label"],
                "max_pixels": machine["max_pixels"],
                "max_length": machine["max_length"],
                "steps_cap": machine["steps_cap"],
            }
        except Exception as exc:      # noqa: BLE001 - 状态面板不该因为这一项挂掉
            logger.debug("取机器档位失败：%s", exc)
        try:
            stats = await self.comfy.ping()
            info["online"] = True
            system = stats.get("system") or {}
            info["argv"] = system.get("argv") or []
            info["ram_total"] = system.get("ram_total", 0)
            info["ram_free"] = system.get("ram_free", 0)
            info["version"] = system.get("comfyui_version") or ""
            devices = stats.get("devices") or []
            if devices and isinstance(devices, list):
                first = devices[0] if isinstance(devices[0], dict) else {}
                info["device"] = first.get("name", "")
                info["vram_total"] = first.get("vram_total", 0)
                info["vram_free"] = first.get("vram_free", 0)
            queue = await self.comfy.queue_status()
            info["queue"] = {
                "running": queue.total_running,
                "pending": queue.total_pending,
            }
        except ComfyUIError as e:
            info["error"] = str(e)
        return info

    # ------------------------------------------------------------------ #
    # 巡检与体检（诊断报告）
    # ------------------------------------------------------------------ #
    def _local_model_sizes(self, folders: list[dict]) -> dict[str, int]:
        """同机时读权重体积（跨机读不到就返回空）。

        Args:
            folders: /experiment/models 的结果（含真实路径）。

        Returns:
            {文件名: 字节数}；一个都读不到时返回 {}。
        """
        sizes: dict[str, int] = {}
        for item in folders or []:
            for path_str in item.get("folders") or []:
                folder = Path(str(path_str))
                if not folder.is_dir():
                    continue
                try:
                    for child in folder.iterdir():
                        if child.is_file():
                            sizes[child.name] = child.stat().st_size
                except OSError:
                    continue
        return sizes

    async def get_diagnostics(self) -> dict:
        """生成巡检 + 体检报告（配置页与 /巡检、/体检 共用）。

        Returns:
            {"models": {...}, "health": {...}}
        """
        catalog = await self.get_catalog()
        stats = self.storage.load_stats()
        sizes: dict[str, int] = {}
        try:
            sizes = self._local_model_sizes(await self.comfy.experiment_model_paths())
        except Exception as exc:      # noqa: BLE001 - 拿不到体积不影响巡检
            logger.debug("读取模型体积失败：%s", exc)

        findings: list[dict] = []
        try:
            nodes = await self.comfy.node_classes()
            requirements = collect_requirements(self.templates)
            findings = check_requirements(requirements, catalog, nodes)
        except Exception as exc:      # noqa: BLE001 - ComfyUI 离线时跳过依赖核对
            logger.debug("模板依赖核对失败：%s", exc)

        machine = await self._machine()
        health = health_report(
            status=await self.get_server_status(),
            catalog=catalog,
            findings=findings,
            tier=machine["tier"],
            video_max_seconds=int((self.config.get("video", {}) or {}).get("max_seconds", 0) or 0),
            config=self.config,
        )
        return {"models": inspect_models(catalog, stats=stats, sizes=sizes), "health": health}

    @staticmethod
    def _render_findings(title: str, findings: list[dict], limit: int = 12) -> str:
        """把 findings 渲染成聊天可读的文本。"""
        icon = {"error": "❌", "warn": "⚠️", "info": "ℹ️"}
        lines = [title]
        for item in findings[:limit]:
            lines.append(f"{icon.get(item.get('severity'), '·')} {item.get('message')}")
            if item.get("suggestion"):
                lines.append(f"　 ↳ {item['suggestion']}")
        if len(findings) > limit:
            lines.append(f"　…还有 {len(findings) - limit} 条，配置页「状态 → 诊断报告」可看全部")
        return "\n".join(lines)

    @filter.command("巡检", alias={"inspect", "模型巡检"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_inspect(self, event: AstrMessageEvent):
        """查看模型资产巡检报告（管理员）。"""
        try:
            report = await self.get_diagnostics()
        except ComfyUIError as e:
            yield event.plain_result(f"💥 {e}")
            return
        data = report["models"]
        head = self.t("cmd.inspect.title", files=data["summary"]["files"],
                      pools=data["summary"]["pools"], dup=data["summary"]["duplicates"],
                      misplaced=data["summary"]["misplaced"])
        yield event.plain_result(self._render_findings(head, data["findings"]))

    @filter.command("体检", alias={"health", "诊断", "自检"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_health(self, event: AstrMessageEvent):
        """查看运行环境体检报告（管理员）。

        加 `--probe` 会把「按需下载权重的预处理器」真跑一遍 —— 这是常规体检的盲区：
        /object_info 的下拉列表是节点源码写死的候选名，不代表服务器上真有那个文件。
        """
        try:
            report = await self.get_diagnostics()
        except ComfyUIError as e:
            yield event.plain_result(f"💥 {e}")
            return
        data = report["health"]
        s = data["summary"]
        head = self.t("cmd.health.title", online=("✅" if s["online"] else "❌"),
                      device=s["device"] or "-", tier=s["tier"],
                      vram=s["vram_free_gb"], ram=s["ram_free_gb"],
                      errors=s["errors"], warnings=s["warnings"])
        yield event.plain_result(self._render_findings(head, data["findings"]))

        raw = _extract_command_payload(event, "体检", "health", "诊断", "自检")
        _desc, opts = parse_inline_params(raw)
        if str(opts.get("probe") or "").strip().lower() in ("", "0", "off", "false", "none"):
            return
        backend = await self.pool.pick()
        comfy = backend.client or self.comfy
        yield event.plain_result("🔬 正在探测预处理器权重（要在 ComfyUI 上真跑一次，稍等）…")
        try:
            results = await self._probe_preprocessors(comfy, backend)
        except ComfyUIError as e:
            yield event.plain_result(f"💥 探测失败：{e}")
            return
        yield event.plain_result("🔬 预处理器探测\n" + probe_summary(results))

    # ------------------------------------------------------------------ #
    # 出图核心
    # ------------------------------------------------------------------ #
    async def _resolve_selection(
        self,
        catalog: dict[str, list[str]],
        opt: dict,
        opts: dict,
        purpose: str = "t2i",
        client: ComfyUI | None = None,
    ) -> dict:
        """校验 LLM（或用户）选定的模型是否真实存在，并挑出模板。

        Args:
            catalog: 真实模型清单。
            opt: LLM 返回的选型结果。
            opts: 用户行内参数。
            purpose: t2i（文生图）/ i2i（图生图）/ outpaint（扩图）/ inpaint（局部重绘）。
            client: 本次任务选中的后端客户端；留空用主后端。

        Returns:
            {"model":..., "folder":..., "lora":..., "vae":..., "template":..., "arch":...}
        """
        checkpoints = catalog.get("checkpoints") or []
        # unet 类底模可能来自两个目录：safetensors 的 diffusion_models 与 GGUF 的 unet_gguf。
        # 目录决定了候选模板（UNETLoader vs UnetLoaderGGUF），所以必须记住它来自哪里。
        unet_pools = [
            ("diffusion_models", catalog.get("diffusion_models") or []),
            ("unet_gguf", catalog.get("unet_gguf") or []),
        ]
        unets = [name for _folder, files in unet_pools for name in files]
        pools = dict(unet_pools)

        def _pool_of(name: str) -> str:
            """这个名字属于哪个 unet 目录（用于挑对装载器的模板）。"""
            for folder_name, files in unet_pools:
                if name in files:
                    return folder_name
            return "diffusion_models"

        loras = catalog.get("loras") or []
        vaes = catalog.get("vae") or []

        # 模型选择：用户行内参数 > LLM > 第一个可用
        # （arch_override 先算出来：视频用途要靠它挑「像视频的底模」）
        arch_override = str(
            (self.config.get("draw_settings") or {}).get("arch_override") or ""
        ).strip().lower()
        model = str(opts.get("model") or "").strip()
        folder = "checkpoints"
        pool = checkpoints
        if model:
            matched = _match_model(model, checkpoints)
            if matched:
                folder, pool = "checkpoints", checkpoints
            else:
                matched = _match_model(model, unets)
                if matched:
                    folder = _pool_of(matched)
                    pool = pools.get(folder, [])
            model = matched
        if not model and purpose in ("t2v", "i2v"):
            # 1) 配置里按功能指定的默认模型（video.t2v_model / video.i2v_model）
            video_conf = self.config.get("video", {}) or {}
            conf_key = "t2v_model" if purpose == "t2v" else "i2v_model"
            preferred = str(video_conf.get(conf_key) or "").strip()
            if preferred:
                for folder_name, files in unet_pools:
                    hit = _match_model(preferred, files)
                    if hit:
                        model, folder, pool = hit, folder_name, files
                        break
                else:
                    hit = _match_model(preferred, checkpoints)
                    if hit:
                        model, folder, pool = hit, "checkpoints", checkpoints
                    else:
                        logger.warning("配置的视频模型 %r 在模型清单里找不到，改用自动挑选", preferred)
        if not model and purpose in ("t2v", "i2v"):
            # 2) 自动挑：视频任务**先看 unet 类目录**（diffusion_models / unet_gguf）。
            # checkpoints 里几乎不会有视频权重，回退到第一个 checkpoint 只会得到
            # 「拿图片底模当视频底模」的必然失败（真机实测过）。
            video_pool = [
                name for name in unets
                if guess_arch(name, arch_override) in VIDEO_ARCHES
            ]
            # 低配/标配机器：默认优先「加速版」权重，省几倍等待时间（名字里带 turbo/distill/…）。
            # 想固定用哪个模型，配置 video.t2v_model / i2v_model，或聊天里 --model 指定。
            if video_pool:
                try:
                    tier = (await self._machine(client))["tier"]
                except Exception:      # noqa: BLE001 - 探测失败就按原顺序
                    tier = ""
                if tier in ("low", "mid") and len(video_pool) > 1:
                    ordered = sorted(
                        video_pool,
                        key=lambda n: (0 if any(h in n.lower() for h in SPEED_HINTS) else 1,
                                       video_pool.index(n)),
                    )
                    if ordered != video_pool:
                        logger.info("机器档位 %s：视频默认模型优先挑加速版（%s）",
                                    tier, ordered[0])
                        video_pool = ordered
            if video_pool:
                model = video_pool[0]
                folder = _pool_of(model)
                pool = pools.get(folder, [])
        if not model:
            wanted = str(opt.get("checkpoint") or "").strip()
            if wanted and wanted in checkpoints:
                model = wanted
            elif wanted and wanted in unets:
                model, folder = wanted, _pool_of(wanted)
                pool = pools.get(folder, [])
            elif checkpoints:
                model = checkpoints[0]
            elif unets:
                model, folder = unets[0], _pool_of(unets[0])
                pool = pools.get(folder, [])
        if not model:
            raise ComfyUIError(
                "ComfyUI 里没有发现任何可用的底模（checkpoints / diffusion_models 都是空的）"
            )

        # ControlNet 用途：装的是 SDXL 深度 ControlNet，配 SD1.5 底模会直接报
        # 「y is None, did you try using a controlnet for SDXL on SD1?」（真机实测）。
        # 没显式指定底模时，自动挑一个 SDXL 系（sdxl / pony / illustrious）。
        if purpose == "control" and not str(opts.get("model") or opt.get("checkpoint") or "").strip():
            if guess_arch(model, arch_override) not in ("sdxl", "pony", "illustrious"):
                # 排除「非纯文生图」的 SDXL 变体：inpaint/instruct 的 UNet 输入通道不同，
                # 挂 ControlNet 会报 y is None（真机踩过：AnythingXL_inkBase 就是这种）
                skip_hints = ("inpaint", "instruct", "inkbase", "ink_base", "tile", "refiner",
                              "canny", "depth", "lineart", "sketch", "seg", "pose")
                sdxl_like = [
                    name for name in checkpoints
                    if guess_arch(name, arch_override) in ("sdxl", "pony", "illustrious")
                    and not any(h in name.lower() for h in skip_hints)
                ]
                if sdxl_like:
                    logger.info("ControlNet 需要 SDXL 系底模，自动从 %s 改用 %s", model, sdxl_like[0])
                    model = sdxl_like[0]
                    folder = _pool_of(model)
                    pool = pools.get(folder, [])
                else:
                    logger.warning(
                        "ControlNet 模板用的是 SDXL 深度 ControlNet，但清单里没有 SDXL 系底模（当前 %s），"
                        "可能会报 controlnet for SDXL on SD1", model
                    )

        lora = ""
        wanted_lora = str(opts.get("lora") or opt.get("lora") or "").strip()
        if wanted_lora:
            # 行内参数允许 name:strength
            name_part = wanted_lora.split(":")[0].strip()
            lora = _match_model(name_part, loras) or ""

        vae = ""
        wanted_vae = str(opt.get("vae") or "").strip()
        if wanted_vae:
            vae = wanted_vae if wanted_vae in vaes else _match_model(wanted_vae, vaes)

        # 能力探测：只挑「你这台 ComfyUI 真的装得出来」的模板，
        # 避免把缺自定义节点的工作流提交过去再被服务端拒绝。
        available = await (client or self.comfy).node_classes()
        arch = guess_arch(model, arch_override)
        if purpose in ("t2v", "i2v") and arch not in VIDEO_ARCHES:
            raise ComfyUIError(
                f"选中的底模 {model} 不像视频模型（识别为 {arch} 架构），"
                "视频需要专门的视频权重（如 Wan 2.2 TI2V-5B / Wan 2.1 T2V），"
                "放进 diffusion_models（safetensors）或 unet_gguf（GGUF）后再试"
            )

        # 换「大模型家族」前先请 ComfyUI 卸载上一套（真机踩过 os error 1455 崩溃）。
        # 只对大模型触发，且同一家族连续出图不会重复卸载，避免白等一次加载。
        if arch in BIG_MEMORY_ARCHES and arch != self._last_big_arch:
            server_conf = self.config.get("server", {}) or {}
            if bool(server_conf.get("free_before_switch", True)):
                freed = await (client or self.comfy).free_memory()
                if freed:
                    logger.info("切换到 %s，已请 ComfyUI 卸载上一套模型释放内存", arch)
                    self._last_big_arch = arch
        elif arch in BIG_MEMORY_ARCHES:
            self._last_big_arch = arch

        template, arch = pick_template(
            self.templates,
            model_name=model,
            model_folder=folder,
            available_nodes=available,
            arch_override=arch_override,
            purpose=purpose,
        )
        if template is None:
            if purpose == "i2i":
                raise ComfyUIError(
                    "没有可用的图生图模板。请确认插件 workflows 目录里有 img2img_checkpoint.json"
                )
            if purpose == "outpaint":
                raise ComfyUIError(
                    "没有可用的扩图模板。请确认插件 workflows 目录里有 outpaint_checkpoint.json"
                    "（用到 ComfyUI 自带的 ImagePadForOutpaint 与 SetLatentNoiseMask 节点）"
                )
            if purpose == "inpaint":
                raise ComfyUIError(
                    "没有可用的局部重绘模板。请确认插件 workflows 目录里有 inpaint_checkpoint.json"
                    "（用到 ComfyUI 自带的 LoadImageMask / GrowMask / SetLatentNoiseMask 节点）"
                )
            if purpose == "i2v":
                raise ComfyUIError(
                    "没有可用的图生视频模板。插件内置的 wan_i2v.json 需要 ComfyUI 的原生 Wan "
                    "支持（UNETLoader / CLIPLoader / VAELoader / WanFirstLastFrameToVideo / "
                    "ModelSamplingSD3 / SaveWEBM）与 Wan I2V 权重。用别的视频模型时，"
                    "请把对应的 API 格式工作流放进模板目录并声明 \"purpose\": \"i2v\""

                    "（另一个常见原因是：diffusion_models 里没有视频权重，"
                    "插件就只能拿到 checkpoints 里的图片底模，套不进「分离权重」模板）"
                )
            if purpose == "t2v":
                raise ComfyUIError(
                    "没有可用的文生视频模板。插件内置的 wan_t2v.json 需要 ComfyUI 的原生 Wan 支持"
                    "（UNETLoader / CLIPLoader / VAELoader / EmptyHunyuanLatentVideo / "
                    "ModelSamplingSD3 / SaveWEBM）。如果你的视频模型是别的家族，"
                    "请把对应的 API 格式工作流放进模板目录并声明 \"purpose\": \"t2v\""

                    "（另一个常见原因是：diffusion_models 里没有视频权重，"
                    "插件就只能拿到 checkpoints 里的图片底模，套不进「分离权重」模板）"
                )
            if folder == "diffusion_models" and arch != "flux":
                raise ComfyUIError(
                    f"模型 {model} 位于 diffusion_models 目录，但它被识别为 {arch} 架构，"
                    f"而内置的「分离权重」模板只支持 Flux。"
                    f"请把该模型移到 checkpoints 目录，或为它提供自己的工作流模板"
                )
            raise ComfyUIError(
                f"没有与模型 {model}（识别为 {arch} 架构）匹配的工作流模板。"
                f"请检查插件 workflows 目录，或把适配它的工作流 JSON 放进模板目录"
            )
        missing = template.missing_nodes(available)
        if missing:
            raise ComfyUIError(
                f"模板 {template.name} 需要以下节点，但你的 ComfyUI 没有安装："
                f"{'、'.join(sorted(missing))}。"
                f"请安装对应自定义节点，或把适配你环境的工作流 JSON 放进模板目录后再试"
            )
        return {
            "model": model,
            "folder": folder,
            "lora": lora,
            "vae": vae,
            "template": template,
            "arch": arch,
            "pool": pool,
        }

    def _resolve_upscale_selection(self, *, exact: bool = False) -> dict:
        """放大用途的「选型」：不需要底模，只要 upscale 模板。

        Args:
            exact: True 选「缩到精确宽高」的模板（--scale-to），
                False 选「按倍数缩放」的模板（--scale）。

        Returns:
            与 _resolve_selection 同构的字典（model 等为空）。

        Raises:
            ComfyUIError: 没有可用的放大模板。
        """
        want = "upscale_exact" if exact else "upscale"
        template = self.templates.get(want)
        if template is None or getattr(template, "purpose", "") != "upscale":
            # 模板被改名/删掉时退回按用途挑，别让整个放大功能跟着不可用
            template, _ = pick_template(
                self.templates, model_name="", model_folder="", purpose="upscale"
            )
        if template is None:
            raise ComfyUIError(
                "没有可用的放大模板。请确认插件 workflows 目录里有 upscale.json"
            )
        return {"model": "", "folder": "", "lora": "", "vae": "",
                "template": template, "arch": "generic", "pool": ""}

    async def _probe_one(
        self, comfy: ComfyUI, target: dict, backend: Backend
    ) -> tuple[str, str]:
        """探测一个「按需下载权重」的节点，结果带 TTL 缓存。

        Returns:
            (状态, 说明)，状态取 PROBE_OK / PROBE_FAILED / PROBE_UNKNOWN。
        """
        key = probe_cache_key(target, backend.name)
        now = time.time()
        cached = self._probe_cache.get(key)
        if cached and now - cached[0] < PROBE_TTL_SECONDS:
            return cached[1], cached[2]
        state, detail = await comfy.probe(build_probe_graph(target))
        self._probe_cache[key] = (now, state, detail)
        return state, detail

    async def _probe_preprocessors(self, comfy: ComfyUI, backend: Backend) -> list[dict]:
        """探测所有模板里「按需下载权重」的节点（`/体检 --probe` 用）。

        Returns:
            [{"class_type", "weight", "state", "ok", "error"}]；同一份权重只探一次。
        """
        seen: dict[tuple, dict] = {}
        for template in self.templates.values():
            for target in find_probe_targets(getattr(template, "graph", {}) or {}):
                seen.setdefault(probe_cache_key(target, backend.name), target)
        results: list[dict] = []
        for target in seen.values():
            try:
                state, detail = await self._probe_one(comfy, target, backend)
            except ComfyUIError as exc:
                state, detail = PROBE_FAILED, str(exc)
            results.append({
                "class_type": target.get("class_type"),
                "weight": target.get("weight"),
                "state": state,
                "ok": state == PROBE_OK,
                "error": detail,
            })
        return results

    async def _control_fallback_note(
        self, comfy: ComfyUI, template: WorkflowTemplate, backend: Backend
    ) -> str:
        """ControlNet 提交前的探测：预处理器跑不起来就返回给用户看的说明。

        为什么要探测而不是直接提交：这类节点的权重不在 models/ 下，是自定义节点
        第一次用到时从 HuggingFace 下载的。节点在、/体检 也全绿，但服务器连不上
        HF 时会「跑到一半才炸」——而且**要退避重试约 3.5 分钟**才抛出
        LocalEntryNotFoundError，用户干等一场还拿不到图。

        Returns:
            空串表示可以照常出图；非空表示应当降级，内容是给用户的原因说明。
        """
        for target in find_probe_targets(getattr(template, "graph", {}) or {}):
            try:
                state, detail = await self._probe_one(comfy, target, backend)
            except ComfyUIError as exc:
                # 探测连提交都失败：说明服务器另有问题，让真正的出图去报错更准确
                logger.warning("预处理器探测未能提交，跳过探测：%s", exc)
                return ""
            if state == PROBE_OK:
                continue
            if state == PROBE_UNKNOWN:
                # 一直没轮到执行（服务器忙）：没有结论，不能据此改用途
                logger.info("预处理器探测无结论，照常出图：%s", detail)
                continue
            if not is_missing_weight_error(detail) and "探测超时" not in detail:
                # 既不是「权重/联网」也不是「卡住不返回」：结论不足以否决出图
                # （例如 EmptyImage 被裁掉），照常提交由服务端裁决
                logger.info("预处理器探测失败但原因与权重无关，按无结论处理：%s", detail)
                continue
            return describe_probe_failure(
                str(target.get("class_type") or ""),
                str(target.get("weight") or ""),
                detail,
            )
        return ""

    def feature_enabled(self, name: str, event=None) -> bool:
        """该功能是否开启。

        优先级：按范围规则（user > group > default）> 全局 features 开关。
        全局缺省按 FEATURE_DEFAULTS（只开文生图）。

        Args:
            name: 开关名（t2i/i2i/outpaint/inpaint/t2v/i2v/reverse_prompt）。
            event: 消息事件；给了就按「群/用户功能白名单」再判一次。

        Returns:
            True 表示可用。
        """
        if event is not None:
            try:
                uid = str(event.get_sender_id())
                gid = str(event.get_group_id() or "")
                override = self.permission.feature_override(
                    uid, gid, is_admin=bool(event.is_admin())
                )
            except Exception:      # noqa: BLE001 - 事件结构异常就退回全局开关
                override = None
            if override is not None:
                return name in override
        conf = self.config.get("features") or {}
        value = conf.get(name, FEATURE_DEFAULTS.get(name, True))
        if isinstance(value, str):      # 手改 JSON 写成 "false"/"off" 也要认
            return value.strip().lower() not in ("", "0", "false", "off", "no")
        return bool(value)

    def require_feature(self, name: str, event=None) -> None:
        """功能没开就抛 FeatureDisabledError（消息里写清去哪打开）。

        Args:
            name: 开关名。
            event: 消息事件（用于按群/用户规则判断）。

        Raises:
            FeatureDisabledError: 该功能被关闭。
        """
        if self.feature_enabled(name, event):
            return
        label, how = FEATURE_LABELS.get(name, (name, ""))
        try:
            message = self.t("error.feature_disabled", feature=label, how=how)
        except Exception:      # noqa: BLE001 - 翻译缺失也要给出可操作提示
            message = f"🚫 功能「{label}」当前未开启，请到插件配置的功能开关里打开。"
        raise FeatureDisabledError(message)

    def enabled_features(self, event=None) -> list[str]:
        """已开启功能的展示名列表（给 /帮助 与状态面板用）。

        Args:
            event: 消息事件；给了就按该用户/群的范围规则列。
        """
        return [
            FEATURE_LABELS.get(k, (k, ""))[0]
            for k in FEATURE_DEFAULTS
            if self.feature_enabled(k, event)
        ]

    async def _machine(self, client=None) -> dict:
        """取当前机器档位（按配置 + 显存/内存自动判定，只探测一次）。

        Args:
            client: 可选的 ComfyUI 客户端（多后端时用当前这台）。

        Returns:
            MACHINE_PRESETS 里的一条，外加 "tier" 键。
        """
        conf = (self.config.get("video", {}) or {}).get("machine", "auto")
        tier = str(conf or "auto").strip().lower()
        if tier in MACHINE_PRESETS:
            return {"tier": tier, **MACHINE_PRESETS[tier]}
        if not self._machine_tier:
            vram_gb = ram_gb = 0.0
            try:
                stats = await (client or self.comfy).ping()
                devices = stats.get("devices") or []
                if devices:
                    vram_gb = float(devices[0].get("vram_total") or 0) / 1024 ** 3
                ram_gb = float((stats.get("system") or {}).get("ram_total") or 0) / 1024 ** 3
            except Exception as exc:      # noqa: BLE001 - 探测失败就按保守档
                logger.debug("探测显存失败，按保守档处理：%s", exc)
            self._machine_tier = resolve_machine_tier("auto", vram_gb, ram_gb)
            logger.info("机器档位自动判定：%s（显存 %.1fG / 内存 %.1fG）→ %s",
                        self._machine_tier, vram_gb, ram_gb,
                        MACHINE_PRESETS[self._machine_tier]["label"])
        return {"tier": self._machine_tier, **MACHINE_PRESETS[self._machine_tier]}

    def _resolve_sampling(
        self, arch: str, opt: dict, opts: dict, draw_conf: dict
    ) -> dict:
        """按架构档案与用户覆盖决定采样参数。

        Args:
            arch: 架构 key。
            opt: LLM 返回的可选覆盖。
            opts: 用户行内参数。
            draw_conf: 配置里的 draw_settings。

        Returns:
            含 width/height/steps/cfg/sampler/scheduler/guidance/batch 的字典。
        """
        profile = arch_profile(arch)
        # 配置里的 override_arch_defaults 打开时，配置值优先于架构档案
        use_conf = bool(draw_conf.get("override_arch_defaults", False))
        default_w, default_h = profile["size"]

        width = height = 0
        explicit = False  # 用户是否显式指定过尺寸（行内参数 / 配置覆盖）
        ratio = str(opts.get("ratio") or "").strip()
        if ratio in RATIO_PRESETS:
            width, height = RATIO_PRESETS[ratio]
            explicit = True
        size = str(opts.get("size") or "").strip().lower()
        if size:
            matched = re.match(r"^(\d{2,4})\s*[x*×]\s*(\d{2,4})$", size)
            if matched:
                width, height = int(matched.group(1)), int(matched.group(2))
                explicit = True
        for key, is_width in (("width", True), ("height", False)):
            value = opts.get(key)
            if value and str(value).isdigit():
                if is_width:
                    width = int(value)
                else:
                    height = int(value)
                explicit = True

        from_llm = False
        if not width or not height:
            if use_conf:
                width = width or int(draw_conf.get("default_width") or default_w)
                height = height or int(draw_conf.get("default_height") or default_h)
                explicit = True
            else:
                # LLM 给的 width/height 只当**比例意图**看待，绝对像素交给架构档案决定
                llm_w = int(opt.get("width") or 0) or 0
                llm_h = int(opt.get("height") or 0) or 0
                if llm_w > 0 and llm_h > 0:
                    width, height = llm_w, llm_h
                    from_llm = True
                else:
                    width = width or default_w
                    height = height or default_h

        width, height = _clamp_dimensions(width, height)
        # 把 LLM 给的尺寸按比例归一到该架构的像素预算：
        # 否则「默认尺寸 1024x1024」会被原样用回 SD1.5 模型上，
        # 而 SD1.5 在 1024 档正是多手指、肢体错乱的高发区。
        if from_llm and not explicit:
            budget = profile_pixels(arch)
            if budget and width * height > 0:
                scale = (budget / (width * height)) ** 0.5
                width, height = _clamp_dimensions(int(width * scale), int(height * scale))

        def _pick(opts_key: str, opt_key: str, conf_key: str, profile_key: str, cast):
            if opts.get(opts_key) not in (None, ""):
                try:
                    return cast(opts[opts_key])
                except (TypeError, ValueError):
                    pass
            if opt.get(opt_key) not in (None, "", 0) and not use_conf:
                try:
                    return cast(opt[opt_key])
                except (TypeError, ValueError):
                    pass
            if use_conf and draw_conf.get(conf_key) not in (None, ""):
                try:
                    return cast(draw_conf[conf_key])
                except (TypeError, ValueError):
                    pass
            return profile.get(profile_key)

        sampler = str(opts.get("sampler") or "").strip() or (
            str(draw_conf.get("default_sampler") or "").strip()
            if use_conf
            else ""
        ) or profile["sampler"]
        batch = opts.get("batch")
        try:
            batch_size = max(1, min(int(batch), 8)) if batch else 1
        except (TypeError, ValueError):
            batch_size = 1

        return {
            "width": width,
            "height": height,
            "steps": _pick("steps", "steps", "default_steps", "steps", int),
            "cfg": _pick("cfg", "cfg", "default_cfg", "cfg", float),
            "sampler": sampler,
            "scheduler": profile["scheduler"],
            "guidance": profile.get("guidance"),
            "batch_size": batch_size,
        }

    async def generate(
        self,
        *,
        user_desc: str,
        opts: dict | None = None,
        event: AstrMessageEvent | None = None,
        on_queued=None,
        on_wait=None,
        on_progress=None,
        preset: dict | None = None,
        source_image: str = "",
        mask_image: str = "",
        end_image: str = "",
        force_purpose: str = "",
    ) -> dict:
        """完整出图流程：选型 → 建图 → 排队取名额 → 提交 → 等待 → 下载。

        Args:
            user_desc: 用户描述。
            opts: 行内参数。
            event: 消息事件，用于 LLM 会话级 provider、统计与「单人并发上限」。
            on_queued: ComfyUI 队列位置提示回调（已提交，等 ComfyUI 轮到）。
            on_wait: 插件侧排队提示回调（并发已满，还没轮到提交）。
            on_progress: 出图进度回调（WebSocket 推送的第 n / 总步数）。
            preset: 现成的提示词（如反推结果），给了就跳过 LLM 改写。
            source_image: 图生图/扩图/局部重绘的输入图本地路径。
            mask_image: 局部重绘的遮罩图本地路径（白=重画，黑=保留）。
            end_image: 图生视频的尾帧图本地路径（给了就走「首尾帧」模式）。
            force_purpose: 强制用途（outpaint 扩图 / inpaint 局部重绘 / t2v 文生视频 /
                i2v 图生视频），空则按有无输入图推断。

        Returns:
            {"images": [Path...], "template": str, "model": str, "lora": str,
             "vae": str, "positive": str, "negative": str, "seconds": float,
             "queued_seconds": float, "arch": str, "seed": int, "width": int, "height": int,
             "videos": [Path...], "video": {"seconds":.., "fps":.., "length":..}}

        Raises:
            ComfyUIError: 出图失败，message 面向用户。
            TemplateError: 工作流模板问题。
            RuntimeError: LLM 不可用，或排队等待超时（QueueTimeout）。
        """
        opts = opts or {}
        draw_conf = self.config.get("draw_settings", {}) or {}
        catalog = await self.get_catalog()
        if not catalog:
            raise ComfyUIError(self.t("error.no_models"))

        default_negative = str(draw_conf.get("default_negative") or "")
        llm_conf = self.config.get("llm_settings", {}) or {}
        opt: dict = {}
        positive = user_desc
        llm_negative = ""
        llm_note = ""
        preset = preset or {}
        # 本次是否让 LLM 改写：行内 --llm / --no-llm 优先于配置
        purpose_preview = force_purpose or ("i2i" if source_image else "t2i")
        # 内容过滤（内部保险：指令入口已拦过，这里挡住 LLM 工具/配置页等其它入口）
        hit = self.permission.nsfw_hit(user_desc)
        if hit:
            self._audit(event.get_sender_id() if event else "", purpose="blocked_nsfw",
                        ok=False, note=hit, prompt=user_desc, event=event)
            raise ContentBlockedError(self.t("perm.nsfw_blocked", word=hit))
        # 功能开关：所有生成能力（含 LLM 无指令出图）都从这一个入口走
        self.require_feature(purpose_preview, event)
        is_video = purpose_preview in ("t2v", "i2v")
        flag_llm = str(opts.get("llm") or "").strip() not in ("", "0", "false")
        flag_no_llm = str(opts.get("no_llm") or "").strip() not in ("", "0", "false")
        if is_video:
            want_llm = bool(llm_conf.get("optimize_for_video", True))
        elif purpose_preview == "upscale":
            want_llm = False          # 放大不涉及提示词，别浪费一次 LLM 调用
        else:
            want_llm = bool(llm_conf.get("enable_prompt_optimize", True))
        if flag_llm:
            want_llm = True
        elif flag_no_llm:
            want_llm = False

        if preset.get("positive"):
            # 已经有现成提示词（例如 /反推 的结果）：不再让 LLM 改写一遍
            positive = str(preset["positive"])
            llm_negative = str(preset.get("negative") or "")
            llm_note = "（提示词来自反推结果，未再改写）"
            if preset.get("checkpoint"):
                opt["checkpoint"] = preset["checkpoint"]
        elif want_llm and is_video:
            # 视频：改写重点是「动作 + 镜头」，且保持用户语言（与图片 tag 那套完全不同）
            try:
                video_conf = self.config.get("video", {}) or {}
                try:
                    vinfo = resolve_video_params(opts, video_conf)
                    seconds, fps = vinfo["seconds"], vinfo["fps"]
                except ComfyUIError:
                    seconds = fps = 0.0
                opt = await self.llm.optimize_video_prompt(
                    user_desc,
                    has_start_image=bool(source_image),
                    seconds=seconds,
                    fps=fps,
                    negative=default_negative,
                    event=event,
                )
            except RuntimeError as e:
                logger.warning("LLM 不可用，视频提示词直接用原话：%s", e)
                opt = {}
                llm_note = "（未启用/无可用 LLM，已直接用你的原话出视频）"
            if opt.get("positive"):
                positive = opt["positive"]
                llm_note = "（已让 AI 按视频视角补全动作与镜头）"
            llm_negative = str(opt.get("negative") or "")
        elif want_llm:
            try:
                opt = await self.llm.optimize_prompt(
                    user_desc,
                    catalog,
                    defaults={
                        "negative": default_negative,
                        "width": draw_conf.get("default_width") or 1024,
                        "height": draw_conf.get("default_height") or 1024,
                    },
                    event=event,
                )
            except RuntimeError as e:
                # 没有可用 LLM 时不应直接失败：退化为原描述 + 首个可用底模照常出图
                logger.warning("LLM 不可用，退化为直接使用原描述出图：%s", e)
                opt = {}
                llm_note = "（未启用/无可用 LLM，已直接用你的原话出图）"
            if opt.get("positive"):
                positive = opt["positive"]
            # LLM 的负面词**不再整体替换**默认词：具体合并见下方（需要先知道架构）
            llm_negative = str(opt.get("negative") or "")
        else:
            llm_note = "（提示词优化已关闭，直接使用你的原话）"

        # 多后端：按负载挑一个后端，本次任务的提交/等待/下载都走它。
        # 单后端时 pick() 直接返回主后端、不做任何探测，行为与以前一致。
        backend = await self.pool.pick()
        comfy = backend.client or self.comfy
        if self.pool.multi:
            logger.info("本次任务派给后端 %s（%s）", backend.name, backend.url)

        # 图生图 / 扩图：先把输入图上传到 ComfyUI，拿到 LoadImage 能用的引用
        purpose = purpose_preview
        image_ref = ""
        mask_ref = ""
        if source_image:
            i2i_sub = str((self.config.get("i2i", {}) or {}).get("subfolder") or "astrbot").strip()
            image_ref = await comfy.upload_image(source_image, subfolder=i2i_sub)
        if mask_image:
            mask_sub = str(
                (self.config.get("i2i", {}) or {}).get("subfolder") or "astrbot"
            ).strip()
            mask_ref = await comfy.upload_image(mask_image, subfolder=mask_sub)
        end_ref = ""
        if end_image:
            end_sub = str(
                (self.config.get("i2i", {}) or {}).get("subfolder") or "astrbot"
            ).strip()
            end_ref = await comfy.upload_image(end_image, subfolder=end_sub)

        if purpose_preview == "upscale":
            # --scale-to 走「缩到精确宽高」的模板，--scale 走「按倍数」
            selection = self._resolve_upscale_selection(
                exact=bool(str(opts.get("scale_to") or "").strip())
            )
        else:
            selection = await self._resolve_selection(
            catalog, opt, opts, purpose=purpose, client=comfy
        )
        template: WorkflowTemplate = selection["template"]
        sampling = self._resolve_sampling(selection["arch"], opt, opts, draw_conf)
        # 机器档位：步数上限（低配机器少步模型就够，别让 30 步把等待拖长）
        machine = await self._machine()
        steps_cap = int(machine.get("steps_cap") or 0)
        if steps_cap and int(sampling.get("steps") or 0) > steps_cap:
            logger.info("机器档位 %s：步数 %s → %s", machine["tier"], sampling["steps"], steps_cap)
            sampling["steps"] = steps_cap

        # 图生图 / 局部重绘：尺寸按原图比例（受 max_side 限制），重绘幅度可调
        # 扩图：尺寸由「原图 + 四周扩展量」决定（节点自己会算，这里只用于汇报与限额）
        # 文生视频：尺寸走架构档案（Wan 就是 832x480），帧数与帧率写进模板声明的参数
        denoise = None
        outpaint_info: dict = {}
        inpaint_info: dict = {}
        video_info: dict = {}
        template_params: dict = {}
        if purpose == "i2v":
            video_conf = self.config.get("video", {}) or {}
            video_info = resolve_video_params(opts, video_conf)
            video_info = apply_native_video_defaults(
                video_info, opts, video_conf, str(selection.get("arch") or ""), machine
            )
            template_params = {
                "length": video_info["length"],
                "fps": int(round(video_info["fps"])),
                "steps": sampling.get("steps"),
            }
            if not end_ref:
                # 没有尾帧：把模板里的可选输入 end_image 摘掉（否则会留一条指向占位文件的死链）
                template_params["end_image"] = ""
            source_size = read_image_size(source_image)
            if source_size:
                sampling["width"], sampling["height"] = fit_video_size(
                    source_size[0], source_size[1], budget=int(machine["max_pixels"])
                )
            logger.info(
                "图生视频｜首帧 %s｜尾帧 %s｜%sx%s｜%s 帧｜%.0f fps｜约 %.1f 秒",
                image_ref, end_ref or "无", sampling["width"], sampling["height"],
                video_info["length"], video_info["fps"], video_info["seconds"],
            )
        if purpose == "t2v":
            video_conf = self.config.get("video", {}) or {}
            video_info = resolve_video_params(opts, video_conf)
            video_info = apply_native_video_defaults(
                video_info, opts, video_conf, str(selection.get("arch") or ""), machine
            )
            template_params = {
                "length": video_info["length"],
                "fps": int(round(video_info["fps"])),
            }
            if video_info["clamped"]:
                logger.info(
                    "视频时长被配置上限截到 %.1f 秒（video.max_seconds）", video_info["seconds"]
                )
            logger.info(
                "文生视频｜%sx%s｜%s 帧｜%.0f fps｜约 %.1f 秒",
                sampling["width"], sampling["height"], video_info["length"],
                video_info["fps"], video_info["seconds"],
            )
        if purpose == "upscale":
            scale_to = str(opts.get("scale_to") or "").strip().lower()
            if scale_to:
                # 精确尺寸：交给 upscale_exact 模板的 ImageScale。
                # 为什么需要：4x 模型再乘 0.5 只保证「倍数」，落不到你要的宽高
                # （1920x1088 这种非 4 的整数倍尺寸尤其对不上）。
                matched = re.match(r"^(\d{2,5})\s*[x*×]\s*(\d{2,5})$", scale_to)
                if not matched:
                    raise ComfyUIError(
                        f"--scale-to 要写成「宽x高」，例如 --scale-to 1920x1088"
                        f"（收到的是 {scale_to!r}）"
                    )
                if "width" not in getattr(template, "params", {}):
                    raise ComfyUIError(
                        "当前放大模板不支持指定精确尺寸（缺 width/height 参数）。"
                        "请确认插件 workflows 目录里有 upscale_exact.json，"
                        "或改用 --scale 按倍数放大"
                    )
                out_w, out_h = _clamp_dimensions(
                    int(matched.group(1)), int(matched.group(2))
                )
                template_params = {"width": out_w, "height": out_h}
                sampling["width"], sampling["height"] = out_w, out_h
                logger.info("放大｜输入 %s｜目标尺寸 %sx%s（4x 模型 → Lanczos 归一）",
                            image_ref, out_w, out_h)
            else:
                # 放大：倍数换算成「4x 模型 + 缩放」（scale_by = 目标倍数 / 4）
                try:
                    scale = float(str(opts.get("scale") or 2).strip() or 2)
                except ValueError:
                    scale = 2.0
                scale = min(4.0, max(1.0, scale))
                template_params = {"scale_by": round(scale / 4.0, 4)}
                logger.info("放大｜输入 %s｜目标倍数 %.1fx（模型 4x，缩放 %.3f）",
                            image_ref, scale, scale / 4.0)
        elif purpose in ("i2i", "inpaint"):
            i2i_conf = self.config.get("i2i", {}) or {}
            if purpose == "inpaint":
                # 局部重绘靠遮罩保住其余像素，默认**整段重画**（denoise=1.0）。
                # 注意别再回落到 i2i.denoise（那是图生图的幅度）：真机实测过这个坑，
                # denoise 被顶成 0.6 后遮罩里几乎没变，看起来像「涂抹没生效」。
                denoise = 1.0
            else:
                try:
                    denoise = float(i2i_conf.get("denoise", 0.6) or 0.6)
                except (TypeError, ValueError):
                    denoise = 0.6
            if opts.get("denoise") not in (None, ""):
                try:
                    denoise = min(1.0, max(0.05, float(opts["denoise"])))
                except (TypeError, ValueError):
                    pass
            try:
                max_side = int(i2i_conf.get("max_side", 1536) or 0)
            except (TypeError, ValueError):
                max_side = 1536
            source_size = read_image_size(source_image)
            if source_size:
                sampling["width"], sampling["height"] = fit_to_limit(
                    source_size[0], source_size[1], max_side
                )
            if purpose == "inpaint":
                inpaint_info = {
                    "mask": mask_ref,
                    "source": source_size or (0, 0),
                    "grow": int(opts.get("grow") or 0) if str(opts.get("grow") or "").isdigit() else 0,
                }
                template_params = {"expand": inpaint_info["grow"]}
                logger.info(
                    "局部重绘｜输入 %s｜遮罩 %s｜目标 %sx%s｜denoise %s｜遮罩外扩 %s",
                    image_ref, mask_ref, sampling["width"], sampling["height"],
                    denoise, inpaint_info["grow"],
                )
            else:
                logger.info(
                    "图生图｜输入 %s｜目标 %sx%s｜denoise %s",
                    image_ref, sampling["width"], sampling["height"], denoise,
                )
        elif purpose == "outpaint":
            source_size = read_image_size(source_image)
            if not source_size:
                raise ComfyUIError(
                    "读不到输入图的尺寸，扩图需要知道原图宽高。请换一张 PNG / JPEG / WebP 图片"
                )
            outpaint_info = self._resolve_outpaint(source_size[0], source_size[1], opts)
            template_params = outpaint_info["pads"]
            sampling["width"] = outpaint_info["width"]
            sampling["height"] = outpaint_info["height"]
            logger.info(
                "扩图｜输入 %s（%sx%s）→ 目标 %sx%s｜扩展 %s/%s/%s/%s｜羽化 %s",
                image_ref, source_size[0], source_size[1],
                outpaint_info["width"], outpaint_info["height"],
                template_params.get("left"), template_params.get("right"),
                template_params.get("top"), template_params.get("bottom"),
                template_params.get("feathering"),
            )
        if purpose == "control":
            # ControlNet 深度：强度与结束时机可调（越大越贴参考图的构图/姿势）
            try:
                ctrl_strength = float(str(opts.get("control_strength") or 0.8).strip() or 0.8)
            except ValueError:
                ctrl_strength = 0.8
            try:
                ctrl_end = float(str(opts.get("control_end") or 0.7).strip() or 0.7)
            except ValueError:
                ctrl_end = 0.7
            template_params = {
                "strength": min(2.0, max(0.0, ctrl_strength)),
                "end_percent": min(1.0, max(0.05, ctrl_end)),
            }
            # ControlNet 权重可换：--control-model 文件名（默认用模板里的标准 SDXL 深度模型）
            ctrl_model = str(opts.get("control_model") or "").strip()
            if ctrl_model:
                template_params["control_net_name"] = ctrl_model
            logger.info("ControlNet｜参考图 %s｜强度 %.2f｜结束 %.2f",
                        image_ref, ctrl_strength, ctrl_end)

        profile = arch_profile(selection["arch"])

        # 正向：按架构补质量词（SD1.5 系模型不加质量词出图会明显发糊）
        if bool(draw_conf.get("add_quality_tags", True)) and profile.get("quality_tags"):
            positive = merge_tags(profile["quality_tags"], positive)

        # Pony 系需要分数前缀
        prefix = profile.get("score_prefix")
        if prefix and "score_" not in positive:
            positive = prefix + positive

        # 负向词策略（配置项 negative_mode）：
        #   merge       —— 默认词 + 手部/肢体规避词 + 架构附加词 + LLM 词（默认，最稳）
        #   guard_only  —— 你的词 + 手部/肢体规避词，不采纳 LLM 的负面词
        #   custom_only —— 只用你自己的词，**自动去重**（((x)) 与 x 视为同一个词）
        #   raw         —— 严格原样，连去重都不做
        neg_mode = str(draw_conf.get("negative_mode") or "merge").strip().lower()
        if neg_mode not in ("merge", "guard_only", "custom_only", "raw"):
            neg_mode = "merge"
        if neg_mode == "raw":
            # 完全按你写的来：行内 --negative 优先，否则用配置里的默认词，一个字符都不改
            negative = str(opts.get("negative") or "").strip() or str(default_negative or "").strip()
        else:
            neg_chunks: list[str] = [default_negative]
            if neg_mode != "custom_only" and profile.get("negative", True):
                neg_chunks.append(ANATOMY_NEGATIVE)
                if profile.get("negative_extra"):
                    neg_chunks.append(str(profile["negative_extra"]))
            if neg_mode == "merge":
                neg_chunks.append(llm_negative)
            # 行内 --negative 是你显式写的，任何档位都尊重
            neg_chunks.append(str(opts.get("negative") or ""))
            negative = merge_tags(*neg_chunks)

        # LLM 改写后的正向词也要再查一遍（模型可能自己加进露骨词）
        hit = self.permission.nsfw_hit(positive)
        if hit:
            raise ContentBlockedError(self.t("perm.nsfw_blocked", word=hit))

        # 内容过滤开启时附加负面词（配合过滤一起用，降低擦边概率）
        if self.permission.nsfw_filter and self.permission.nsfw_negative:
            negative = merge_tags(negative, self.permission.nsfw_negative)

        # 配置里强制指定的 VAE 优先（模型自带 VAE 有问题时用于纠正偏色）
        force_vae = str(draw_conf.get("force_vae") or "").strip()
        if force_vae:
            selection["vae"] = force_vae

        # 提示词长度诊断。注意：ComfyUI 对超长提示词**不是截断**，而是切成多段
        # 分别编码后拼接，所以长提示词依然生效；但每段都要插入 start/end 与 padding，
        # 词越多，单个词的相对影响力越低 —— 这是「负面词写了一百个反而没效果」的成因。
        logger.info(
            "本次提示词长度｜正向 %s｜负面 %s",
            describe_prompt(positive),
            describe_prompt(negative),
        )
        prompt_note = ""
        if estimate_clip_chunks(negative)[1] >= 3 and not self._prompt_note_shown:
            self._prompt_note_shown = True
            prompt_note = (
                f"负面词较长（{describe_prompt(negative)}）。过长的负面词不会失效，"
                f"但会稀释每个词的影响力，可考虑精简重复项"
            )

        lora_strength = float(opt.get("lora_strength") or 1.0)
        if opts.get("lora") and ":" in str(opts["lora"]):
            tail = str(opts["lora"]).split(":")[-1]
            try:
                lora_strength = max(0.0, min(float(tail), 2.0))
            except ValueError:
                pass

        # 注意：Flux 架构的负向提示词由模板层按架构自动忽略，无需在此特判
        seed = random.randint(0, 2**31 - 1)
        if opts.get("seed") and str(opts["seed"]).lstrip("-").isdigit():
            seed = int(opts["seed"]) % (2**31)

        # ControlNet 的深度预处理器权重不在 models/ 下，是自定义节点按需从 HuggingFace
        # 下载的：节点在、/体检 全绿，但服务器连不上 HF 时会「跑到一半才炸」，报错还是
        # 一段英文 LocalEntryNotFoundError（真机踩过：节点默认档 vitl 没下过，只装了 vits）。
        # 所以提交前先探一次，探不通就降级成图生图保构图，而不是把注定失败的活儿排进队列。
        if purpose == "control":
            fallback_note = await self._control_fallback_note(comfy, template, backend)
            if fallback_note:
                if not self.feature_enabled("i2i", event):
                    raise ComfyUIError(
                        f"{fallback_note}\n　· 图生图功能没开（features.i2i），无法自动降级；"
                        f"请先修好深度预处理器权重，或打开图生图"
                    )
                fallback_opts = dict(opts)
                # 用户显式写过 --denoise 就尊重他，否则用「保色调保构图」的默认幅度
                if not str(fallback_opts.get("denoise") or "").strip():
                    fallback_opts["denoise"] = f"{CONTROL_FALLBACK_DENOISE:g}"
                fallback_opts.pop("control", None)
                logger.warning(
                    "ControlNet 不可用，已降级为图生图（denoise %s）：%s",
                    fallback_opts.get("denoise"), fallback_note.replace("\n", " "),
                )
                # preset 直接把已经算好的正/负提示词传过去：降级不该再花一次 LLM 调用
                degraded = await self.generate(
                    user_desc=user_desc,
                    opts=fallback_opts,
                    event=event,
                    on_queued=on_queued,
                    on_wait=on_wait,
                    on_progress=on_progress,
                    preset={"positive": positive, "negative": negative},
                    source_image=source_image,
                    force_purpose="i2i",
                )
                degraded["control_fallback"] = fallback_note
                return degraded

        graph = template.build(
            positive=positive,
            negative=negative,
            model_name=selection["model"],
            vae_name=selection["vae"],
            lora_name=selection["lora"],
            lora_strength=lora_strength,
            seed=seed,
            filename_prefix="astrbot_smart",
            denoise=denoise,
            image_name=image_ref,
            mask_name=mask_ref,
            end_image_name=end_ref,
            params=template_params,
            **sampling,
        )

        # Hires Fix：先出小图，再放大重绘一遍。改善手部与人脸最有效的手段之一，
        # 但会让出图时间显著变长（约两倍），因此默认关闭。
        hires_conf = self.config.get("hires", {}) or {}
        hires_scale = float(hires_conf.get("scale", 1.5) or 1.5)
        hires_denoise = float(hires_conf.get("denoise", 0.5) or 0.5)
        hires_steps = int(hires_conf.get("steps", 0) or 0)
        hires_method = str(hires_conf.get("method") or "bislerp")

        # 自动开关分文生图/图生图两个，互不影响（扩图属于「改图」，跟随图生图那一个）
        if purpose == "upscale":
            hires_on = False          # 放大本身就是后处理，不再叠 Hires
        elif purpose in ("i2i", "outpaint", "inpaint"):
            hires_on = bool(hires_conf.get("enable", False)) and bool(
                hires_conf.get("enable_for_i2i", True)
            )
        else:
            hires_on = bool(hires_conf.get("enable", False))

        # 单次指令的 --hires：可被管理员关掉（避免有人把倍数开到爆显存）
        if str(opts.get("hires") or "").strip():
            if bool(hires_conf.get("allow_inline", True)):
                try:
                    hires_scale = float(opts["hires"])
                except (TypeError, ValueError):
                    pass
                # 行内给了倍数就以它为准：0 或 1 表示本次关闭
                hires_on = hires_scale > 1.0
            else:
                logger.info("单次 --hires 已被配置禁用，本次按配置设置处理")
        if opts.get("hires_denoise") not in (None, ""):
            try:
                hires_denoise = min(1.0, max(0.0, float(opts["hires_denoise"])))
            except (TypeError, ValueError):
                pass
        if opts.get("hires_steps") not in (None, ""):
            try:
                hires_steps = max(0, int(opts["hires_steps"]))
            except (TypeError, ValueError):
                pass

        hires_info: dict = {}
        hires_note = ""
        if hires_on and hires_scale > 1.0:
            hires_info = add_hires_fix(
                graph,
                template.bindings,
                scale=hires_scale,
                denoise=hires_denoise,
                steps=hires_steps,
                seed=random.randint(0, 2**31 - 1),
                upscale_method=hires_method,
            )
            if hires_info:
                # 图生图的尺寸看输入图，不一定等于配置里的宽高：以实际结果为准
                if hires_info["width"] and hires_info["height"]:
                    logger.info(
                        "Hires Fix 已启用｜%s → %sx%s｜denoise %s｜第二轮步数 %s",
                        f"{sampling['width']}x{sampling['height']}",
                        hires_info["width"], hires_info["height"],
                        hires_denoise, hires_steps or "同首轮",
                    )
                else:
                    # 尺寸完全由工作流决定，插件拿不到具体数值
                    logger.info(
                        "Hires Fix 已启用｜按 ×%s 放大（首次尺寸由工作流决定）"
                        "｜denoise %s｜第二轮步数 %s",
                        hires_scale, hires_denoise, hires_steps or "同首轮",
                    )
                # 放大后的像素量才是显存真正吃紧的地方，提前提醒而不是等它 OOM
                target_pixels = hires_info["width"] * hires_info["height"]
                if target_pixels > 2048 * 2048:
                    logger.warning(
                        "Hires 目标尺寸 %sx%s（%.1f MP）偏大，小显存机器容易 OOM 或极慢；"
                        "建议把放大倍数降到 1.5 以内",
                        hires_info["width"], hires_info["height"],
                        target_pixels / 1_000_000,
                    )
                    hires_note = (
                        f"Hires 目标 {hires_info['width']}x{hires_info['height']}"
                        f"（{target_pixels / 1_000_000:.1f} MP）偏大，小显存容易爆或很慢"
                    )
            else:
                logger.warning("Hires Fix 未能插入（模板结构不支持），本次按普通出图处理")

        # 提交前用服务端自己的输入约束做一次本地预检。
        # 注意：这里**只做诊断、不做拦截**。有些节点用 VALIDATE_INPUTS 自己校验输入
        # （典型是 LoadImage，允许写 "子目录/文件名"），拿 /object_info 的下拉列表去卡
        # 会把合法请求误杀。服务端始终是最终裁判；预检结论在服务端拒绝时一并给出，
        # 正好补上「ComfyUI 只回一句 failed validation、不给节点级原因」的场景。
        problems = await comfy.precheck(graph)
        if problems:
            logger.warning(
                "提交前预检发现问题（仍会提交，由服务端裁决）：%s", "；".join(problems)
            )

        started = time.time()
        uid = str(event.get_sender_id()) if event is not None else "anonymous"
        queued_seconds = 0.0
        # 并发闸门：名额在「提交 + 等待成图」期间持有，超出的在插件侧按先来后到排队。
        # 拿到名额才算真正开始占用 ComfyUI，因此提示分两条：插件侧排队（on_wait）
        # 与 ComfyUI 自己的队列（on_queued），含义不同，都需要告诉用户。
        try:
            async with self.gate.hold(uid, on_wait=on_wait) as slot:
                queued_seconds = slot.waited
                if queued_seconds >= 1.0:
                    logger.info("插件侧排队 %.1f 秒后获得出图名额", queued_seconds)
                try:
                    prompt_id = await comfy.submit(
                        graph, extra_data={"astrbot_plugin": PLUGIN_NAME}
                    )
                except ComfyUIError as e:
                    if is_backend_fault(str(e)):
                        self.pool.note_failure(backend, str(e))
                    # 把「实际提交的图」落盘：服务端偶尔不返回节点级原因，没有这个就只能靠猜
                    path = self._dump_failed_graph(graph, e)
                    logger.warning(
                        "提交失败｜正向提示词 %d 字符｜模板 %s｜底模 %s｜LoRA %s｜图已存 %s",
                        len(positive), template.name, selection["model"],
                        selection["lora"] or "无", path,
                    )
                    detail = f"{e}\n　· 本次工作流已保存到 {path}，可据此排查"
                    if problems:
                        # 服务端没给节点级原因时，预检结论就是最有用的线索
                        detail += "\n　· 本地预检发现（供参考）：\n" + "\n".join(
                            f"　　- {item}" for item in problems
                        )
                    raise ComfyUIError(detail) from e
                # 登记在途任务：`/取消` 靠这几张表找到「这个人的任务」并定点打断
                job_cancel = asyncio.Event()
                self._active_jobs[prompt_id] = uid
                self._job_cancel[prompt_id] = job_cancel
                self._job_backend[prompt_id] = backend
                try:
                    images = await comfy.wait_for_images(
                        prompt_id,
                        self.storage.output_dir,
                        on_queued=on_queued,
                        on_progress=on_progress,
                        cancel_event=self._cancel,
                        user_cancel_event=job_cancel,
                    )
                except ComfyUIError as e:
                    # 被用户 /取消 或插件卸载打断不算后端故障，别把好机器拉黑
                    if is_backend_fault(str(e)) and not job_cancel.is_set():
                        self.pool.note_failure(backend, str(e))
                    raise
                else:
                    self.pool.note_success(backend)
                finally:
                    self._active_jobs.pop(prompt_id, None)
                    self._job_cancel.pop(prompt_id, None)
                    self._job_backend.pop(prompt_id, None)
        except QueueTimeout as e:
            # 排队超时不是「出图失败」而是「没轮上」：记 info 便于区分，再交给指令层告知用户
            logger.info("出图排队超时：%s", e)
            raise
        if not images:
            raise ComfyUIError(self.t("error.no_output"))
        # 审计：在唯一出口写，覆盖指令 / LLM 无指令出图 / 配置页等所有入口
        audit_uid = str(getattr(event, "get_sender_id", lambda: "")() or "") if event else ""
        self._audit(
            audit_uid, name=_sender_name(event, audit_uid) if event else "",
            purpose=purpose, model=selection["model"],
            seconds=time.time() - started, ok=True, prompt=positive, seed=seed, event=event,
        )
        videos = [p for p in images if media_kind(p.name) == "video"]
        pictures = [p for p in images if media_kind(p.name) != "video"]

        # 可选放大后处理：--upscale 2（需要「放大」功能开启）。
        # 复用 generate() 的 upscale 用途：上传产物 → 4x 模型 → 缩回目标倍数。
        upscale_note = ""
        upscale_arg = str(opts.get("upscale") or "").strip()
        if (purpose != "upscale" and pictures and upscale_arg
                and self.feature_enabled("upscale", event)):
            # --upscale 2 按倍数；--upscale 1920x1088 出精确尺寸
            sub_opts: dict = {}
            if re.match(r"^\d{2,5}\s*[x*×]\s*\d{2,5}$", upscale_arg):
                sub_opts["scale_to"] = upscale_arg
                scale_label = f"已放大到 {upscale_arg}"
            else:
                try:
                    scale = float(upscale_arg)
                except ValueError:
                    scale = 2.0
                sub_opts["scale"] = f"{scale:g}"
                scale_label = f"已 {scale:g}x 放大"
            try:
                sub_result = await self.generate(
                    user_desc="", opts=sub_opts, event=event,
                    force_purpose="upscale", source_image=str(pictures[0]),
                )
                if sub_result.get("images"):
                    pictures = sub_result["images"]
                    upscale_note = scale_label
            except (ComfyUIError, TemplateError) as exc:
                logger.warning("放大后处理失败，保留原图：%s", exc)
                upscale_note = "放大失败，已返回原图"

        return {
            "images": pictures,
            "videos": videos,
            "purpose": purpose,
            "template": template.name,
            "model": selection["model"],
            "lora": selection["lora"],
            "vae": selection["vae"],
            "positive": positive,
            "negative": negative,
            "arch": selection["arch"],
            "seed": seed,
            "llm_note": llm_note,
            "prompt_note": prompt_note,
            "hires": hires_info,
            "hires_note": hires_note,
            "upscale_note": upscale_note,
            "i2i": bool(image_ref),
            "denoise": denoise if denoise is not None else 1.0,
            "outpaint": outpaint_info,
            "inpaint": inpaint_info,
            "video": video_info,
            "i2v": bool(purpose == "i2v" and image_ref),
            "end_frame": bool(end_ref),
            "backend": backend.name if self.pool.multi else "",
            "seconds": time.time() - started,
            "queued_seconds": queued_seconds,
            # 有 Hires 时对外报最终尺寸，消息与画廊显示的才是真实产物尺寸
            # Hires 生效时以放大后的最终尺寸为准；拿不到具体数值（0）则退回配置尺寸
            "width": hires_info.get("width") or sampling["width"],
            "height": hires_info.get("height") or sampling["height"],
            **{k: sampling[k] for k in ("steps", "cfg", "sampler")},
        }

    def _resolve_outpaint(self, width: int, height: int, opts: dict) -> dict:
        """算出扩图的四周扩展量与最终尺寸。

        Args:
            width: 原图宽。
            height: 原图高。
            opts: 行内参数（left/right/top/bottom/feather）。

        Returns:
            {"pads": {left,right,top,bottom,feathering}, "width":.., "height":.., "source": (w,h)}

        Raises:
            ComfyUIError: 扩展量非法（全是 0、或超出预算后仍为 0）。
        """
        # 明确给了任意一边，就**只用给的这些边**（其余为 0）：
        # `/扩图 --left 256 --right 256` 的语义是「只往左右扩」，不是「左右 256 + 默认的上下」
        given_sides: dict[str, int] = {}
        for key in ("left", "right", "top", "bottom"):
            raw = opts.get(key)
            if raw in (None, ""):
                continue
            try:
                value = max(0, int(float(str(raw))))
            except (TypeError, ValueError):
                raise ComfyUIError(f"扩图参数 --{key} 需要是像素数，收到的是 {raw!r}")
            given_sides[key] = value - value % DIM_ALIGN
        pads = default_outpaint_pads(width, height) if not given_sides else {}
        pads.update(given_sides)
        for key in ("left", "right", "top", "bottom"):
            pads.setdefault(key, 0)
        pads.setdefault("feathering", DEFAULT_OUTPAINT_FEATHER)
        if opts.get("feather") not in (None, ""):
            try:
                pads["feathering"] = max(0, min(256, int(float(str(opts["feather"])))))
            except (TypeError, ValueError):
                raise ComfyUIError(f"羽化参数 --feather 需要是像素数，收到的是 {opts['feather']!r}")
        pads = fit_outpaint_pads(width, height, pads)
        total = sum(int(pads[k]) for k in ("left", "right", "top", "bottom"))
        if total <= 0:
            raise ComfyUIError(
                "扩图至少要往一边扩一点：用 --left/--right/--top/--bottom 指定像素数"
                "（例如 /扩图 描述 --left 256 --right 256）"
            )
        return {
            "pads": pads,
            "width": width + int(pads["left"]) + int(pads["right"]),
            "height": height + int(pads["top"]) + int(pads["bottom"]),
            "source": (width, height),
        }

    def _queue_notifiers(self, event: AstrMessageEvent):
        """构造出图过程中的三条提示回调，供各指令复用。

        三条提示含义不同，都需要：
        - `on_wait`：并发名额已满（或自己已有任务在跑），还没轮到提交；
        - `on_queued`：已提交给 ComfyUI，等它自己的队列轮到（位置变化时才再提）；
        - `on_progress`：ComfyUI 通过 WebSocket 推来的「第 n / 总步数」。

        Args:
            event: 消息事件，提示直接发回当前会话。

        Returns:
            (on_wait, on_queued, on_progress) 三个回调。
        """
        output_conf = self.config.get("output", {}) or {}
        show_progress = bool(output_conf.get("show_progress", True))
        try:
            interval = float(output_conf.get("progress_interval", 5) or 5)
        except (TypeError, ValueError):
            interval = 5.0
        # 节流状态：步数事件很密（每步一条），不节流会把聊天刷爆
        state = {"last_at": 0.0, "last_percent": -1}

        async def _on_wait(info: dict):
            if info.get("reason") == "user":
                text = self.t("queue.waiting_self", max=info["max_concurrent"])
            else:
                text = self.t(
                    "queue.waiting", ahead=info["ahead"], max=info["max_concurrent"]
                )
            await event.send(event.plain_result(text))

        async def _on_queued(status):
            await event.send(event.plain_result(self.t(
                "queue.queued",
                position=min(status.own_positions.values() or [1]),
                ahead=status.tasks_ahead,
            )))

        async def _on_progress(info: dict):
            if not show_progress:
                return
            percent = int(info.get("percent", 0))
            now = time.time()
            # 同一百分比不重复发；未到间隔时间也不发（100% 一定发，收个尾）
            if percent == state["last_percent"]:
                return
            if percent < 100 and now - state["last_at"] < interval:
                return
            state["last_at"] = now
            state["last_percent"] = percent
            await event.send(event.plain_result(self.t(
                "queue.progress",
                value=info.get("value", 0), max=info.get("max", 0), percent=percent,
            )))

        return _on_wait, _on_queued, _on_progress

    @filter.command("取消", alias={"停止", "cancel", "stop", "中断"})
    async def cmd_cancel(self, event: AstrMessageEvent):
        """取消自己正在排队或正在出图的任务。"""
        uid = str(event.get_sender_id())
        is_admin = bool(event.is_admin())
        raw = _extract_command_payload(event, "取消", "停止", "cancel", "stop", "中断")
        want_all = bool(is_admin and raw.strip() in ("全部", "所有", "--all", "--全部", "all"))

        running: list[str] = []
        if want_all:
            running = list(self._active_jobs)
        else:
            # 同一用户可能有多张（把单人上限调大过），只取消最后发起的那个
            for prompt_id, owner in reversed(list(self._active_jobs.items())):
                if owner == uid:
                    running.append(prompt_id)
                    break

        results: list[str] = []
        for prompt_id in running:
            job_event = self._job_cancel.get(prompt_id)
            if job_event is not None:
                job_event.set()
            backend = self._job_backend.get(prompt_id)
            client = backend.client if backend is not None else self.comfy
            outcome = await client.cancel_prompt(prompt_id)
            results.append(self.t({
                "running": "cmd.cancel.running",
                "pending": "cmd.cancel.pending",
                "not_found": "cmd.cancel.not_found",
            }.get(outcome, outcome)))

        # 还没拿到名额、在插件侧排队的人，也要能取消
        waiting = self.gate.cancel_waiting(
            "" if want_all else uid, "本次出图已被 /取消 取消"
        )

        if not results and not waiting:
            yield event.plain_result(self.t("cmd.cancel.none"))
            return
        lines = [self.t("cmd.cancel.done")]
        for item in results:
            lines.append(f"　· {item}")
        if waiting:
            lines.append("　· " + self.t("cmd.cancel.waiting", count=waiting))
        logger.info("用户 %s 取消出图：task=%s waiting=%s", uid, len(results), waiting)
        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------------ #
    # 指令
    # ------------------------------------------------------------------ #
    @filter.command("画图", alias={"绘图", "draw", "生成图片"})
    async def cmd_draw(self, event: AstrMessageEvent):
        """根据描述生成图片。"""
        uid = str(event.get_sender_id())
        is_admin = bool(event.is_admin())
        allowed, reason = await self.permission.check(
            uid, is_admin=is_admin, storage=self.storage
        )
        if not allowed:
            yield event.plain_result(reason)
            return

        # 功能关闭：静默忽略，不做任何回复（全局开关 + 按群/用户白名单）
        if not self.feature_enabled("t2i", event):
            return

        raw = _extract_command_payload(event, "画图", "绘图", "draw", "生成图片")
        desc, opts = parse_inline_params(raw)

        # 内容过滤：命中就拦下（默认静默，可配置成提示一句）
        hit = self.permission.nsfw_hit(desc)
        if hit:
            logger.info("内容过滤拦截：user=%s hit=%s", uid, hit)
            self._audit(uid, name=_sender_name(event, uid), purpose="blocked_nsfw", ok=False,
                        note=hit, prompt=desc, event=event)
            if self.permission.nsfw_notify:
                yield event.plain_result(self.t("perm.nsfw_blocked", word=hit))
            return
        if not desc:
            yield event.plain_result(self.t("cmd.draw.usage"))
            return

        # 带了图片就走图生图（可在配置里关掉）；--control 则走 ControlNet 深度控制
        source_image = ""
        control_mode = str(opts.get("control") or "").strip().lower()
        images = await self._collect_images(event)
        if control_mode and control_mode not in ("0", "off", "false", "none"):
            if not self.feature_enabled("control", event):
                return
            if not images:
                yield event.plain_result(self.t("cmd.draw.control_no_image"))
                return
            source_image = images[0]
        elif images:
            if bool((self.config.get("i2i", {}) or {}).get("enable", True)):
                source_image = images[0]
            else:
                yield event.plain_result(self.t("cmd.draw.i2i_disabled"))

        yield event.plain_result(
            self.t("cmd.draw.received_image") if source_image else self.t("cmd.draw.received")
        )

        on_wait, on_queued, on_progress = self._queue_notifiers(event)

        try:
            result = await self._generate_variants(
                user_desc=desc,
                opts=opts,
                event=event,
                on_wait=on_wait,
                on_queued=on_queued,
                on_progress=on_progress,
                source_image=source_image,
                force_purpose="control" if control_mode else "",
            )
        except (ComfyUIError, TemplateError) as e:
            logger.warning("出图失败：%s", e)
            yield event.plain_result(self.t("error.failed", error=e))
            return
        except RuntimeError as e:
            logger.warning("LLM 调用失败：%s", e)
            yield event.plain_result(self.t("error.runtime", error=e))
            return
        except Exception as e:  # pragma: no cover - 兜底，避免 handler 抛出
            logger.exception("出图时发生未预期错误")
            yield event.plain_result(self.t("error.unexpected", error=e))
            return

        await self.permission.record(uid, is_admin=is_admin, storage=self.storage)
        await self._record_generation(uid, event, result)

        chain = self._compose_result_chain(event, uid, result)
        yield event.chain_result(chain)

    def _audit(self, uid: str, *, name: str = "", purpose: str = "", model: str = "",
               seconds: float = 0.0, ok: bool = True, note: str = "",
               prompt: str = "", seed=None, event=None) -> None:
        """写一条审计记录（配置 permission.audit_log 打开时）。

        审计失败绝不影响出图：这里吞掉异常，只记一条调试日志。

        Args:
            uid: 用户 id。
            name: 昵称。
            purpose: 用途（t2i/t2v/... 或 blocked_nsfw）。
            model: 使用的底模。
            seconds: 耗时。
            ok: 是否成功。
            note: 备注（例如命中的过滤词）。
            prompt: 提示词（只存前 60 字）。
            seed: 随机种子。
            event: 消息事件（用于取群号）。
        """
        if not bool((self.config.get("permission", {}) or {}).get("audit_log", True)):
            return
        try:
            entry = {
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "user_id": str(uid),
                "user_name": name or "",
                "group_id": str(getattr(event, "get_group_id", lambda: "")() or "") if event else "",
                "purpose": purpose,
                "model": model,
                "seconds": round(float(seconds or 0), 1),
                "ok": bool(ok),
            }
            if note:
                entry["note"] = note
            if prompt:
                entry["prompt"] = str(prompt)[:60]
            if seed is not None:
                entry["seed"] = seed
            self.storage.append_audit(entry)
        except Exception as exc:      # noqa: BLE001 - 审计永远不能影响出图
            logger.debug("写审计失败：%s", exc)

    async def _record_generation(
        self, uid: str, event: AstrMessageEvent, result: dict
    ) -> None:
        """记录一次成功出图（含完整参数，供画廊展示与复现）。

        Args:
            uid: 触发者 id。
            event: 消息事件，用于取昵称。
            result: generate() 的返回值。
        """
        await self._record_result(uid, _sender_name(event, uid), result)

    async def _record_result(self, uid: str, name: str, result: dict) -> None:
        """把一次出图写进统计（昵称由调用方给出，配置页没有消息事件）。

        Args:
            uid: 触发者 id。
            name: 展示用昵称。
            result: generate() 的返回值。
        """
        await self.storage.record_generation(
            user_id=uid,
            user_name=name,
            positive=result["positive"],
            negative=result["negative"],
            models={
                "checkpoint": result["model"],
                "lora": result["lora"],
                "vae": result["vae"],
                "template": result["template"],
            },
            images=[f"images/{p.name}" for p in (result["images"] + (result.get("videos") or []))],
            seconds=result["seconds"],
            params={
                "width": result.get("width"),
                "height": result.get("height"),
                "steps": result.get("steps"),
                "cfg": result.get("cfg"),
                "sampler": result.get("sampler"),
                "seed": result.get("seed"),
                "arch": result.get("arch", ""),
                "lora": result.get("lora", ""),
                "vae": result.get("vae", ""),
                "template": result.get("template", ""),
                "model": result.get("model", ""),
            },
        )

    # ------------------------------------------------------------------ #
    # 局部重绘（配置页的涂抹工具调用）
    # ------------------------------------------------------------------ #
    _DATA_URL_PREFIX = "data:image/"

    def _save_data_url(self, value, field: str) -> Path:
        """把配置页传来的 PNG/JPEG data URL 落成一个临时文件。

        Args:
            value: 形如 `data:image/png;base64,....` 的字符串。
            field: 字段名（用于文件名与报错）。

        Returns:
            落盘后的路径。

        Raises:
            ValueError: 不是合法的 data URL，或图片过大。
        """
        text = str(value or "").strip()
        if not text.startswith(self._DATA_URL_PREFIX) or "base64," not in text:
            raise ValueError(f"{field} 必须是图片的 data URL（data:image/png;base64,...）")
        head, _, encoded = text.partition("base64,")
        ext = ".png" if "png" in head.lower() else (".jpg" if "jp" in head.lower() else ".png")
        try:
            raw = base64.b64decode(encoded, validate=False)
        except Exception as e:
            raise ValueError(f"{field} 的 base64 解析失败：{e}") from e
        if not raw:
            raise ValueError(f"{field} 是空图片")
        if len(raw) > INPAINT_MAX_BYTES:
            raise ValueError(
                f"{field} 太大（{len(raw) // 1024 // 1024} MB），"
                f"上限 {INPAINT_MAX_BYTES // 1024 // 1024} MB"
            )
        target_dir = self.data_dir / "inpaint"
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / f"{int(time.time() * 1000)}_{field}{ext}"
        path.write_bytes(raw)
        return path

    async def inpaint(self, payload: dict) -> dict:
        """局部重绘：按配置页涂抹的遮罩重画指定区域。

        页面会把「原图」与「遮罩」都渲染成同尺寸的 PNG 再发过来
        （遮罩是白底黑字：白=重画，黑=保留），所以这里只需要校验尺寸一致。

        Args:
            payload: `{"image": dataURL, "mask": dataURL, "prompt": str,
                "denoise": float|str, "grow": int|str, "model": str, "seed": int|str}`。

        Returns:
            `{"ok": True, "image": "images/xxx.png", "seed":..., "seconds":...,
              "width":..., "height":..., "positive":..., "template":...}`

        Raises:
            ValueError: 请求不合法（缺图/缺遮罩/尺寸不一致/太大）。
            ComfyUIError: 出图失败。
        """
        if not isinstance(payload, dict):
            raise ValueError("请求体必须是 JSON 对象")
        image_path = self._save_data_url(payload.get("image"), "原图")
        mask_path = self._save_data_url(payload.get("mask"), "遮罩")
        try:
            source_size = read_image_size(str(image_path))
            mask_size = read_image_size(str(mask_path))
            if not source_size or not mask_size:
                raise ValueError("读不出图片尺寸，请用 PNG / JPEG 图片")
            if source_size != mask_size:
                raise ValueError(
                    f"原图与遮罩的尺寸必须一致（{source_size[0]}x{source_size[1]} vs "
                    f"{mask_size[0]}x{mask_size[1]}）"
                )
            if source_size[0] * source_size[1] > MAX_PIXELS:
                raise ValueError(
                    f"图片太大（{source_size[0]}x{source_size[1]}），"
                    f"请控制在 {int(MAX_PIXELS ** 0.5)}x{int(MAX_PIXELS ** 0.5)} 以内"
                )

            opts: dict = {}
            for key in ("model", "seed", "lora", "steps", "cfg", "sampler", "denoise", "grow"):
                if payload.get(key) not in (None, ""):
                    opts[key] = str(payload[key])
            prompt = str(payload.get("prompt") or "").strip() or (
                "重画这块区域，与周围的风格、光影和细节自然衔接"
            )
            result = await self.generate(
                user_desc=prompt,
                opts=opts,
                event=None,
                source_image=str(image_path),
                mask_image=str(mask_path),
                force_purpose="inpaint",
            )
        finally:
            # 原图与遮罩都已经上传给 ComfyUI 了，本地临时文件不留着占地方
            for path in (image_path, mask_path):
                try:
                    path.unlink()
                except OSError:
                    pass

        await self._record_result("dashboard", "配置页", result)
        return {
            "ok": True,
            "image": f"images/{result['images'][0].name}" if result.get("images") else "",
            "positive": result.get("positive", ""),
            "negative": result.get("negative", ""),
            "template": result.get("template", ""),
            "model": result.get("model", ""),
            "seed": result.get("seed"),
            "seconds": result.get("seconds"),
            "width": result.get("width"),
            "height": result.get("height"),
        }

    async def _collect_images(self, event: AstrMessageEvent) -> list[str]:
        """从当前消息或引用消息里取出图片的本地路径。

        约定与 AstrBot 自身一致：`Image` 组件用 `convert_to_file_path()` 得到本地路径，
        再作为 `image_urls` 交给 LLM（provider 内部会转成 base64）。
        引用消息的图片藏在 `Reply.chain` 里，需要递归取。

        Args:
            event: 消息事件。

        Returns:
            本地图片路径列表（已去重）。
        """
        message = getattr(getattr(event, "message_obj", None), "message", None) or []

        def walk(components) -> list:
            collected: list = []
            for comp in components or []:
                if isinstance(comp, Image):
                    collected.append(comp)
                elif isinstance(comp, Reply):
                    collected.extend(walk(getattr(comp, "chain", None) or []))
            return collected

        paths: list[str] = []
        for comp in walk(message):
            try:
                path = await comp.convert_to_file_path()
            except Exception as e:
                logger.warning("读取消息里的图片失败：%s", e)
                continue
            if path and str(path) not in paths:
                paths.append(str(path))
        return paths

    def _dump_failed_graph(self, graph: dict, error) -> Path:
        """把提交失败的工作流写到数据目录，供排查。

        Args:
            graph: 实际提交的 API 格式工作流。
            error: 失败原因（字符串或异常）。

        Returns:
            落盘路径。
        """
        path = self.data_dir / "last_failed_prompt.json"
        payload = {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "plugin_version": PLUGIN_VERSION,
            "comfyui": self.comfy.base_url,
            "error": str(error) if error else "",
            "graph": graph,
        }
        try:
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as e:
            logger.warning("写入失败工作流时出错：%s", e)
        return path

    async def _generate_variants(
        self, *, event: AstrMessageEvent | None = None, opts: dict, **kwargs
    ) -> dict:
        """一次出多档重绘幅度：`--denoise 0.4,0.55,0.7`。

        只有图生图这条路上才有意义 —— denoise 是图生图独有的旋钮，文生图、
        放大、视频都没有它；给这些用途传多档会得到一堆一模一样的图，所以这里
        先判断用途，不是图生图就原样交给 generate()。

        Returns:
            generate() 的结果字典；多档时 images 是各档产物的合并，
            denoise_levels 记录跑过哪几档（消息里会列出来）。
        """
        force = str(kwargs.get("force_purpose") or "")
        is_i2i = force == "i2i" or (not force and bool(kwargs.get("source_image")))
        levels = parse_denoise_levels(opts.get("denoise")) if is_i2i else []
        if len(levels) <= 1:
            return await self.generate(event=event, opts=opts, **kwargs)

        results: list[dict] = []
        for index, level in enumerate(levels):
            sub_opts = dict(opts)
            sub_opts["denoise"] = f"{level:g}"
            if index and results:
                # 第一档已经算好的提示词直接复用：多档对比不该多花 N-1 次 LLM 调用
                kwargs["preset"] = {
                    "positive": results[0]["positive"],
                    "negative": results[0]["negative"],
                }
            results.append(await self.generate(event=event, opts=sub_opts, **kwargs))

        merged = dict(results[0])
        merged["images"] = [path for item in results for path in item["images"]]
        merged["denoise_levels"] = levels
        merged["seconds"] = sum(float(item.get("seconds") or 0) for item in results)
        logger.info(
            "一次出 %d 档 denoise：%s（共 %d 张）",
            len(levels), "、".join(f"{x:g}" for x in levels), len(merged["images"]),
        )
        return merged

    def _compose_result_chain(self, event: AstrMessageEvent, uid: str, result: dict):
        """拼装出图结果消息链。

        Args:
            event: 消息事件。
            uid: 触发者 id。
            result: generate() 的返回值。

        Returns:
            message component 列表。
        """
        output_conf = self.config.get("output", {}) or {}
        chain = []
        is_group = bool(getattr(event, "get_group_id", lambda: None)())
        if is_group and output_conf.get("mention_trigger_user", True):
            chain.append(At(qq=uid))
            chain.append(Plain(" "))
        if output_conf.get("show_params", True):
            detail = self.t(
                "result.params",
                template=result["template"], arch=result.get("arch", "?"),
                model=result["model"], width=result["width"], height=result["height"],
                seed=result["seed"], seconds=result["seconds"],
            )
            if result.get("lora"):
                detail += self.t("result.lora", lora=result["lora"])
            if result.get("llm_note"):
                detail += self.t("result.note", note=result["llm_note"])
            if result.get("denoise_levels"):
                detail += self.t(
                    "result.i2i_levels",
                    levels="、".join(f"{x:g}" for x in result["denoise_levels"]),
                )
            elif result.get("i2i"):
                detail += self.t("result.i2i", denoise=result.get("denoise", 0.6))
            if result.get("control_fallback"):
                # ControlNet 探测失败自动降级时，必须说清「为什么没用 ControlNet」
                detail += self.t("result.warning", text=result["control_fallback"])
            if result.get("outpaint"):
                _op = result["outpaint"]
                _pads = _op.get("pads") or {}
                _src = _op.get("source") or (0, 0)
                detail += self.t(
                    "result.outpaint",
                    sw=_src[0], sh=_src[1], width=_op.get("width"), height=_op.get("height"),
                    left=_pads.get("left", 0), right=_pads.get("right", 0),
                    top=_pads.get("top", 0), bottom=_pads.get("bottom", 0),
                )
            if result.get("hires"):
                _hw = result["hires"].get("width")
                _hh = result["hires"].get("height")
                if _hw and _hh:
                    detail += self.t("result.hires_fixed", width=_hw, height=_hh)
                else:
                    # 尺寸由工作流自己决定，只说倍数，避免编一个尺寸出来
                    detail += self.t("result.hires_scale", scale=result["hires"].get("scale", ""))
            if result.get("hires_note"):
                detail += self.t("result.warning", text=result["hires_note"])
            if result.get("upscale_note"):
                detail += self.t("result.note", note=result["upscale_note"])
            if result.get("prompt_note"):
                detail += self.t("result.note", note=result["prompt_note"])
            if result.get("i2v"):
                detail += self.t(
                    "result.i2v_frames" if result.get("end_frame") else "result.i2v_start"
                )
            if result.get("backend"):
                detail += self.t("result.backend", name=result["backend"])
            if result.get("queued_seconds"):
                # 排队时间与出图时间分开报，否则「这次怎么这么慢」说不清
                detail += self.t("result.queued", seconds=result["queued_seconds"])
            chain.append(Plain(detail + "\n"))
        shown_video = False
        video_conf = self.config.get("video", {}) or {}
        for path in result.get("videos") or []:
            seconds = result.get("video", {}).get("seconds", "")
            if video_conf.get("send_video", True):
                chain.append(Video.fromFileSystem(str(path)))
                shown_video = True
                detail = self.t("result.video", seconds=seconds)
            else:
                detail = self.t("result.video_saved", seconds=seconds, path=path)
            chain.append(Plain(detail + "\n"))
        if result.get("videos") and shown_video:
            chain.append(Plain(self.t(
                "result.video_meta",
                length=result["video"].get("length"),
                fps=result["video"].get("fps", 0),
            )))
        for path in result["images"]:
            chain.append(Image.fromFileSystem(str(path)))
        return chain

    @filter.command("放大", alias={"upscale", "画质提升", "高清化"})
    async def cmd_upscale(self, event: AstrMessageEvent):
        """把图片放大（默认 2 倍，用 4x 模型再缩回目标倍数）。"""
        uid = str(event.get_sender_id())
        is_admin = bool(event.is_admin())
        allowed, reason = await self.permission.check(
            uid, is_admin=is_admin, storage=self.storage
        )
        if not allowed:
            yield event.plain_result(reason)
            return

        if not self.feature_enabled("upscale", event):
            return

        raw = _extract_command_payload(event, "放大", "upscale", "画质提升", "高清化")
        _desc, opts = parse_inline_params(raw)
        images = await self._collect_images(event)
        if not images:
            yield event.plain_result(self.t("cmd.upscale.usage"))
            return

        yield event.plain_result(self.t("cmd.upscale.received"))
        on_wait, on_queued, on_progress = self._queue_notifiers(event)
        try:
            result = await self.generate(
                user_desc="",
                opts=opts,
                event=event,
                on_wait=on_wait,
                on_queued=on_queued,
                on_progress=on_progress,
                source_image=images[0],
                force_purpose="upscale",
            )
        except (ComfyUIError, TemplateError) as e:
            logger.warning("放大失败：%s", e)
            yield event.plain_result(self.t("error.failed", error=e))
            return
        except RuntimeError as e:
            yield event.plain_result(f"💥 {e}")
            return

        await self.permission.record(uid, is_admin=is_admin, storage=self.storage)
        await self._record_generation(uid, event, result)
        yield event.chain_result(self._compose_result_chain(event, uid, result))

    @filter.command("审计", alias={"audit", "日志", "记录"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_audit(self, event: AstrMessageEvent):
        """查看最近的审计记录（管理员）。"""
        raw = _extract_command_payload(event, "审计", "audit", "日志", "记录")
        _desc, opts = parse_inline_params(raw)
        try:
            limit = max(1, min(100, int(str(opts.get("count") or "").strip() or 20)))
        except ValueError:
            limit = 20
        only_user = str(opts.get("user") or "").strip()
        rows = self.storage.load_audit(limit=limit, user_id=only_user)
        if not rows:
            yield event.plain_result(self.t("cmd.audit.empty"))
            return
        total = len(self.storage.load_audit(limit=100000, user_id=only_user))
        lines = [self.t("cmd.audit.title", count=len(rows), total=total)]
        for row in rows:
            lines.append(self.t(
                "cmd.audit.line",
                time=row.get("time", ""),
                name=row.get("user_name") or "-",
                uid=row.get("user_id", ""),
                purpose=row.get("purpose") or row.get("note") or "-",
                model=(row.get("model") or "-").split("/")[-1][:28],
                seconds=row.get("seconds", 0),
                ok=self.t("cmd.audit.ok") if row.get("ok", True) else self.t("cmd.audit.fail"),
            ))
        yield event.plain_result("\n".join(lines))

    @filter.command("刷新模型")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_refresh_models(self, event: AstrMessageEvent):
        """重新发现 ComfyUI 里可用的模型（管理员）。"""
        yield event.plain_result(self.t("cmd.refresh.doing"))
        result = await self.refresh_models()
        if not result["ok"]:
            yield event.plain_result(self.t("cmd.refresh.failed", message=result["message"]))
            return
        lines = [self.t("cmd.refresh.done", message=result["message"])]
        for folder, files in sorted(result["catalog"].items()):
            lines.append(self.t("cmd.refresh.folder", folder=folder, count=len(files)))
        yield event.plain_result("\n".join(lines))

    @filter.command("模型列表", alias={"模型"})
    async def cmd_model_list(self, event: AstrMessageEvent):
        """查看可用的模型清单。"""
        catalog = await self.get_catalog()
        if not catalog:
            yield event.plain_result(self.t("cmd.models.empty"))
            return
        lines = [self.t("cmd.models.title")]
        for folder, files in sorted(catalog.items()):
            lines.append(self.t("cmd.models.folder", folder=folder, count=len(files)))
            for name in files[:12]:
                lines.append(f"　- {name}")
            if len(files) > 12:
                lines.append(self.t("cmd.models.more", count=len(files)))
        yield event.plain_result("\n".join(lines))

    @filter.command("模板列表", alias={"工作流"})
    async def cmd_template_list(self, event: AstrMessageEvent):
        """查看当前加载的工作流模板。"""
        if not self.templates:
            yield event.plain_result(self.t("cmd.templates.empty"))
            return
        lines = [self.t("cmd.templates.title")]
        for name in sorted(self.templates):
            tpl = self.templates[name]
            info = tpl.describe()
            lines.append(self.t(
                "cmd.templates.item", name=name, arch=info["arch"],
                loader=info["loader"], nodes=info["nodes"],
            ))
        lines.append(self.t("cmd.templates.arch", arches="、".join(sorted(ARCH_PROFILES))))
        lines.append(self.t("cmd.templates.user_dir", path=self.user_template_dir))
        if self.template_errors:
            lines.append(self.t("cmd.templates.failed", names="、".join(self.template_errors)))
        yield event.plain_result("\n".join(lines))

    @filter.command("状态")
    async def cmd_status(self, event: AstrMessageEvent):
        """查看 ComfyUI 连接与队列状态。"""
        info = await self.get_server_status()
        lines = [f"🖥 ComfyUI：{info['base_url']}"]
        lines.append("　连接：✅ 正常" if info["online"] else f"　连接：❌ {info.get('error', '不可用')}")
        if info.get("device"):
            lines.append(f"　设备：{info['device']}")
        lines.append("　功能：" + ("、".join(self.enabled_features()) or "（无）"))
        if info.get("machine"):
            mach = info["machine"]
            lines.append(
                f"　机器档位：{mach['label']}（上限 {round(mach['max_pixels'] / 10000)} 万像素"
                f" · {mach['max_length']} 帧 · {mach['steps_cap']} 步）"
            )
        if info.get("queue"):
            lines.append(
                f"　队列：执行中 {info['queue']['running']} · 等待中 {info['queue']['pending']}"
            )
        version = await self.comfy.comfyui_version()
        if version:
            lines.append(f"　ComfyUI 版本：{version}")
        # 插件侧并发闸门：出图慢/排队久时，先看这一行
        gate = self.gate.snapshot()
        per_user = (
            f"｜单人上限 {gate['per_user_limit']}"
            if gate["per_user_limit"] > 0
            else "｜单人不限"
        )
        wait_limit = f"{gate['wait_timeout']:.0f} 秒" if gate["wait_timeout"] else "不限"
        lines.append(
            f"　并发：上限 {gate['max_concurrent']}｜进行中 {gate['running']}"
            f"｜排队 {gate['waiting']}{per_user}｜排队等待上限 {wait_limit}"
        )
        if self.pool.multi:
            rows = self.pool.snapshot()
            lines.append(f"　后端（{len(rows)} 个，按负载分配）：")
            for row in rows:
                if row["benched"]:
                    mark = f"⛔ 熔断中（还有 {row['benched_for']:.0f} 秒）"
                elif row["online"] is False:
                    mark = "❌ 上次失败"
                elif row["online"]:
                    mark = f"✅ 队列 {row['busy']}"
                else:
                    mark = "❔ 未探测"
                tag = "（主）" if row["primary"] else ""
                lines.append(f"　　- {row['name']}{tag} {row['url']}｜{mark}")
        lines.append(f"　模板：{len(self.templates)} 个")
        hires_conf = self.config.get("hires", {}) or {}
        lines.append(
            "　Hires Fix：文生图 {}｜图生图 {}｜单次 --hires {}".format(
                "开" if hires_conf.get("enable") else "关",
                "开" if (hires_conf.get("enable") and hires_conf.get("enable_for_i2i", True)) else "关",
                "允许" if hires_conf.get("allow_inline", True) else "已禁用",
            )
        )
        lines.append(f"　配置页 API：{'已注册' if self.pages_ready else '❌ 注册失败，请查看日志'}")

        # 对话模型与看图能力：/反推 依赖这个，直接在这里暴露，省得靠猜
        try:
            vision_settings = self.config.get("vision_settings", {}) or {}
            if str(vision_settings.get("base_url") or "").strip():
                lines.append(
                    f"　看图模型（自定义接口）：{vision_settings.get('base_url')}"
                    f"｜模型 {vision_settings.get('model') or '未填'}"
                )
                vision_conf = ""
            else:
                vision_conf = str(vision_settings.get("provider") or "").strip()
            if vision_conf:
                support = self.llm.provider_vision_support(vision_conf)
                mark = "✅ 支持" if support else ("❌ 不支持" if support is False else "❔ 未知")
                lines.append(f"　看图模型（已指定）：{self.llm.provider_label(vision_conf)}｜看图 {mark}")
            else:
                pid = await self.llm.resolve_provider_id(event)
                if pid:
                    support = self.llm.provider_vision_support(pid)
                    mark = "✅ 支持" if support else ("❌ 不支持" if support is False else "❔ 未知（没配 modalities）")
                    lines.append(f"　对话模型：{self.llm.provider_label(pid)}｜看图 {mark}")
                else:
                    lines.append("　对话模型：❌ 未配置")
            lines.append("　提示：/反推 必须用支持看图的模型；不支持时请用配置里的「看图反推专用提供商」或 --provider 指定")
        except Exception as e:
            logger.debug("读取对话模型信息失败：%s", e)
        yield event.plain_result("\n".join(lines))

    @filter.command("图生图", alias={"改图", "i2i", "重绘"})
    async def cmd_img2img(self, event: AstrMessageEvent):
        """以一张图为底，按描述重绘。"""
        uid = str(event.get_sender_id())
        is_admin = bool(event.is_admin())
        allowed, reason = await self.permission.check(
            uid, is_admin=is_admin, storage=self.storage
        )
        if not allowed:
            yield event.plain_result(reason)
            return

        # 功能关闭：静默忽略，不做任何回复（全局开关 + 按群/用户白名单）
        if not self.feature_enabled("i2i", event):
            return

        raw = _extract_command_payload(event, "图生图", "改图", "i2i", "重绘")
        desc, opts = parse_inline_params(raw)

        # 内容过滤：命中就拦下（默认静默，可配置成提示一句）
        hit = self.permission.nsfw_hit(desc)
        if hit:
            logger.info("内容过滤拦截：user=%s hit=%s", uid, hit)
            self._audit(uid, name=_sender_name(event, uid), purpose="blocked_nsfw", ok=False,
                        note=hit, prompt=desc, event=event)
            if self.permission.nsfw_notify:
                yield event.plain_result(self.t("perm.nsfw_blocked", word=hit))
            return

        images = await self._collect_images(event)
        if not images:
            yield event.plain_result(self.t("cmd.img2img.usage"))
            return
        if not desc:
            yield event.plain_result(self.t("cmd.img2img.need_desc"))
            return

        yield event.plain_result(self.t("cmd.img2img.received"))

        on_wait, on_queued, on_progress = self._queue_notifiers(event)

        try:
            result = await self._generate_variants(
                user_desc=desc,
                opts=opts,
                event=event,
                on_wait=on_wait,
                on_queued=on_queued,
                on_progress=on_progress,
                source_image=images[0],
            )
        except (ComfyUIError, TemplateError) as e:
            logger.warning("图生图失败：%s", e)
            yield event.plain_result(self.t("error.failed", error=e))
            return
        except RuntimeError as e:
            yield event.plain_result(f"💥 {e}")
            return

        await self.permission.record(uid, is_admin=is_admin, storage=self.storage)
        await self._record_generation(uid, event, result)
        yield event.chain_result(self._compose_result_chain(event, uid, result))

    @filter.command("扩图", alias={"外扩", "outpaint", "扩画"})
    async def cmd_outpaint(self, event: AstrMessageEvent):
        """把图片的构图往外扩（outpaint）。"""
        uid = str(event.get_sender_id())
        is_admin = bool(event.is_admin())
        allowed, reason = await self.permission.check(
            uid, is_admin=is_admin, storage=self.storage
        )
        if not allowed:
            yield event.plain_result(reason)
            return

        # 功能关闭：静默忽略，不做任何回复（全局开关 + 按群/用户白名单）
        if not self.feature_enabled("outpaint", event):
            return

        raw = _extract_command_payload(event, "扩图", "外扩", "outpaint", "扩画")
        desc, opts = parse_inline_params(raw)

        # 内容过滤：命中就拦下（默认静默，可配置成提示一句）
        hit = self.permission.nsfw_hit(desc)
        if hit:
            logger.info("内容过滤拦截：user=%s hit=%s", uid, hit)
            self._audit(uid, name=_sender_name(event, uid), purpose="blocked_nsfw", ok=False,
                        note=hit, prompt=desc, event=event)
            if self.permission.nsfw_notify:
                yield event.plain_result(self.t("perm.nsfw_blocked", word=hit))
            return

        images = await self._collect_images(event)
        if not images:
            yield event.plain_result(self.t("cmd.outpaint.usage"))
            return
        if not desc:
            # 不给描述也能用：给一句通用的「往外延伸」，交给 LLM 改写（若开启）
            desc = self.t("cmd.outpaint.default_prompt")

        yield event.plain_result(self.t("cmd.outpaint.received"))
        on_wait, on_queued, on_progress = self._queue_notifiers(event)

        try:
            result = await self.generate(
                user_desc=desc,
                opts=opts,
                event=event,
                on_wait=on_wait,
                on_queued=on_queued,
                on_progress=on_progress,
                source_image=images[0],
                force_purpose="outpaint",
            )
        except (ComfyUIError, TemplateError) as e:
            logger.warning("扩图失败：%s", e)
            yield event.plain_result(self.t("error.outpaint_failed", error=e))
            return
        except RuntimeError as e:
            yield event.plain_result(f"💥 {e}")
            return

        await self.permission.record(uid, is_admin=is_admin, storage=self.storage)
        await self._record_generation(uid, event, result)
        yield event.chain_result(self._compose_result_chain(event, uid, result))

    @filter.command("图生视频", alias={"首尾帧", "i2v", "让图动起来"})
    async def cmd_image_to_video(self, event: AstrMessageEvent):
        """以一张图为起点生成视频（可给第二张图当尾帧）。"""
        uid = str(event.get_sender_id())
        is_admin = bool(event.is_admin())
        allowed, reason = await self.permission.check(
            uid, is_admin=is_admin, storage=self.storage, video=True
        )
        if not allowed:
            yield event.plain_result(reason)
            return

        # 功能关闭：静默忽略，不做任何回复（全局开关 + 按群/用户白名单）
        if not self.feature_enabled("i2v", event):
            return

        raw = _extract_command_payload(event, "图生视频", "首尾帧", "i2v", "让图动起来")
        desc, opts = parse_inline_params(raw)

        # 内容过滤：命中就拦下（默认静默，可配置成提示一句）
        hit = self.permission.nsfw_hit(desc)
        if hit:
            logger.info("内容过滤拦截：user=%s hit=%s", uid, hit)
            self._audit(uid, name=_sender_name(event, uid), purpose="blocked_nsfw", ok=False,
                        note=hit, prompt=desc, event=event)
            if self.permission.nsfw_notify:
                yield event.plain_result(self.t("perm.nsfw_blocked", word=hit))
            return

        images = await self._collect_images(event)
        if not images:
            yield event.plain_result(self.t("cmd.i2v.usage"))
            return
        if not desc:
            desc = self.t("cmd.i2v.default_prompt")

        start_image = images[0]
        end_image = images[1] if len(images) > 1 else ""
        tip_key = "cmd.i2v.received_frames" if end_image else "cmd.i2v.received"
        yield event.plain_result(self.t(tip_key))

        on_wait, on_queued, on_progress = self._queue_notifiers(event)
        try:
            result = await self.generate(
                user_desc=desc,
                opts=opts,
                event=event,
                on_wait=on_wait,
                on_queued=on_queued,
                on_progress=on_progress,
                source_image=start_image,
                end_image=end_image,
                force_purpose="i2v",
            )
        except (ComfyUIError, TemplateError) as e:
            logger.warning("图生视频失败：%s", e)
            yield event.plain_result(self.t("error.video_failed", error=e))
            return
        except RuntimeError as e:
            yield event.plain_result(f"💥 {e}")
            return

        await self.permission.record(uid, is_admin=is_admin, storage=self.storage, video=True)
        await self._record_generation(uid, event, result)
        yield event.chain_result(self._compose_result_chain(event, uid, result))

    @filter.command("视频", alias={"生成视频", "文生视频", "video", "t2v"})
    async def cmd_video(self, event: AstrMessageEvent):
        """按描述生成一段短视频（文生视频）。"""
        uid = str(event.get_sender_id())
        is_admin = bool(event.is_admin())
        allowed, reason = await self.permission.check(
            uid, is_admin=is_admin, storage=self.storage, video=True
        )
        if not allowed:
            yield event.plain_result(reason)
            return

        # 功能关闭：静默忽略，不做任何回复（全局开关 + 按群/用户白名单）
        if not self.feature_enabled("t2v", event):
            return

        raw = _extract_command_payload(event, "视频", "生成视频", "文生视频", "video", "t2v")
        desc, opts = parse_inline_params(raw)

        # 内容过滤：命中就拦下（默认静默，可配置成提示一句）
        hit = self.permission.nsfw_hit(desc)
        if hit:
            logger.info("内容过滤拦截：user=%s hit=%s", uid, hit)
            self._audit(uid, name=_sender_name(event, uid), purpose="blocked_nsfw", ok=False,
                        note=hit, prompt=desc, event=event)
            if self.permission.nsfw_notify:
                yield event.plain_result(self.t("perm.nsfw_blocked", word=hit))
            return
        if not desc:
            video_conf = self.config.get("video", {}) or {}
            yield event.plain_result(self.t(
                "cmd.video.usage",
                seconds=video_conf.get("default_seconds", 4),
                max_seconds=video_conf.get("max_seconds", 10),
                fps=video_conf.get("default_fps", 16),
            ))
            return

        yield event.plain_result(self.t("cmd.video.received"))
        on_wait, on_queued, on_progress = self._queue_notifiers(event)

        try:
            result = await self.generate(
                user_desc=desc,
                opts=opts,
                event=event,
                on_wait=on_wait,
                on_queued=on_queued,
                on_progress=on_progress,
                force_purpose="t2v",
            )
        except (ComfyUIError, TemplateError) as e:
            logger.warning("文生视频失败：%s", e)
            yield event.plain_result(self.t("error.video_failed", error=e))
            return
        except RuntimeError as e:
            yield event.plain_result(f"💥 {e}")
            return

        await self.permission.record(uid, is_admin=is_admin, storage=self.storage, video=True)
        await self._record_generation(uid, event, result)
        yield event.chain_result(self._compose_result_chain(event, uid, result))

    @filter.command("反推", alias={"反推提示词", "识图", "img2prompt"})
    async def cmd_reverse_prompt(self, event: AstrMessageEvent):
        """看一张图，反推出可用的提示词。"""
        uid = str(event.get_sender_id())
        is_admin = bool(event.is_admin())
        allowed, reason = await self.permission.check(
            uid, is_admin=is_admin, storage=self.storage
        )
        if not allowed:
            yield event.plain_result(reason)
            return

        # 功能关闭：静默忽略，不做任何回复（全局开关 + 按群/用户白名单）
        if not self.feature_enabled("reverse_prompt", event):
            return

        raw = _extract_command_payload(event, "反推", "反推提示词", "识图", "img2prompt")
        hint, opts = parse_inline_params(raw)

        images = await self._collect_images(event)
        if not images:
            yield event.plain_result(self.t("cmd.reverse.usage"))
            return

        pid = str(opts.get("provider") or "").strip()
        yield event.plain_result(self.t(
            "cmd.reverse.analyzing",
            model=self.llm.provider_label(pid) if pid else self.t("cmd.draw.vision_current"),
            count=len(images),
        ))
        try:
            result = await self.llm.reverse_prompt(
                images, hint=hint, event=event, provider_id=pid
            )
        except RuntimeError as e:
            logger.warning("反推失败：%s", e)
            yield event.plain_result(f"💥 {e}")
            return

        if not result.get("positive"):
            yield event.plain_result(self.t("cmd.reverse.empty"))
            return

        lines = [self.t("cmd.reverse.result")]
        if result.get("model"):
            lines.append(self.t("cmd.reverse.vision_model", model=result["model"]))
        if result.get("summary"):
            lines.append(self.t("cmd.reverse.scene", summary=result["summary"]))
        lines.append(self.t("cmd.reverse.positive", positive=result["positive"]))
        if result.get("negative"):
            lines.append(self.t("cmd.reverse.negative", negative=result["negative"]))

        if not opts.get("draw"):
            lines.append(self.t("cmd.reverse.draw_hint"))
            yield event.plain_result("\n".join(lines))
            return

        yield event.plain_result("\n".join(lines) + self.t("cmd.reverse.drawing"))

        on_wait, on_queued, on_progress = self._queue_notifiers(event)

        try:
            drawn = await self.generate(
                user_desc=result["positive"],
                opts=opts,
                event=event,
                on_wait=on_wait,
                on_queued=on_queued,
                on_progress=on_progress,
                preset=result,
            )
        except (ComfyUIError, TemplateError) as e:
            logger.warning("按反推结果出图失败：%s", e)
            yield event.plain_result(self.t("error.failed", error=e))
            return
        except RuntimeError as e:
            yield event.plain_result(f"💥 {e}")
            return

        await self.permission.record(uid, is_admin=is_admin, storage=self.storage)
        await self._record_generation(uid, event, drawn)
        yield event.chain_result(self._compose_result_chain(event, uid, drawn))

    @filter.command("统计")
    async def cmd_stats(self, event: AstrMessageEvent):
        """查看出图统计。"""
        stats = self.storage.load_stats()
        lines = [self.t("cmd.stats.title")]
        users = stats.get("users") or {}
        if users:
            lines.append(self.t("cmd.stats.users"))
            for uid, info in sorted(
                users.items(), key=lambda kv: -int(kv[1].get("count", 0))
            )[:10]:
                lines.append(self.t(
                    "cmd.stats.user_item", name=info.get("name", uid),
                    count=info.get("count", 0),
                ))
        usage = stats.get("model_usage") or {}
        for key, label in (("checkpoint", "底模"), ("lora", "LoRA"), ("vae", "VAE")):
            if usage.get(key):
                lines.append(self.t("cmd.stats.usage", label=label))
                for name, count in sorted(
                    usage[key].items(), key=lambda kv: -kv[1]
                )[:5]:
                    lines.append(self.t("cmd.stats.usage_item", name=name, count=count))
        records = stats.get("records") or []
        if records:
            lines.append(self.t("cmd.stats.records", count=len(records)))
        if len(lines) == 1:
            lines.append(self.t("cmd.stats.empty"))
        yield event.plain_result("\n".join(lines))

    @filter.command("帮助", alias={"comfy帮助"})
    async def cmd_help(self, event: AstrMessageEvent):
        """查看帮助。"""
        text = self.t("cmd.help.body")
        opened = self.enabled_features(event)
        text += "\n" + self.t("cmd.help.features",
                             **{"list": "、".join(opened) if opened else "（无）"})
        yield event.plain_result(text)

    # ------------------------------------------------------------------ #
    # 无指令出图（可在配置中开关）
    # ------------------------------------------------------------------ #
    @filter.llm_tool(name="generate_image")
    async def tool_generate_image(
        self, event: AstrMessageEvent, prompt: str, aspect_ratio: str = ""
    ):
        """根据描述生成一张图片。当用户要求画画、画图、生成图片或出图时调用。

        Args:
            prompt(string): 要生成画面的详细描述，中文或英文均可
            aspect_ratio(string): 画面比例，可选 1:1、16:9、9:16、4:3、3:4，留空则自动
        """
        agent_conf = self.config.get("agent", {}) or {}
        # 无指令出图关闭、或文生图功能关闭：静默忽略，不做任何回复
        if not agent_conf.get("enable_llm_tool", False) or not self.feature_enabled("t2i", event):
            return

        uid = str(event.get_sender_id())
        is_admin = bool(event.is_admin())
        allowed, reason = await self.permission.check(
            uid, is_admin=is_admin, storage=self.storage
        )
        if not allowed:
            yield event.plain_result(reason)
            return

        opts = {}
        ratio = (aspect_ratio or "").strip()
        if ratio in RATIO_PRESETS:
            opts["ratio"] = ratio
        try:
            result = await self.generate(user_desc=prompt, opts=opts, event=event)
        except (ComfyUIError, TemplateError, RuntimeError) as e:
            logger.warning("无指令出图失败：%s", e)
            yield event.plain_result(f"💥 出图失败：{e}")
            return

        await self.permission.record(uid, is_admin=is_admin, storage=self.storage)
        await self._record_generation(uid, event, result)
        yield event.chain_result(
            [Image.fromFileSystem(str(p)) for p in result["images"]]
        )


# 手部/肢体畸形的规避词：无论 LLM 或用户怎么写负面词，这部分都会被合并保留。
# 这是「多手指、手穿模、肢体错乱」最直接的对治手段。
ANATOMY_NEGATIVE = (
    "bad hands, extra fingers, fewer fingers, fused fingers, extra digits, "
    "missing fingers, mutated hands, poorly drawn hands, malformed limbs, "
    "extra limbs, bad anatomy"
)


# 显式权重后缀，例如 "word:1.3"
_EXPLICIT_WEIGHT = re.compile(r":\s*[0-9]*\.?[0-9]+\s*$")
# 外层的括号/方括号
_WRAPPERS = "()[]{}"


def tag_key(tag: str) -> str:
    """把标签归一成用于去重的键。

    关键点：`((extra limbs))`、`(extra limbs)`、`extra limbs`、`[[extra limbs]]`
    在 ComfyUI 里表达的是**同一个词**（区别只在权重），去重时必须视为同一个，
    否则像「Deep Negative」这类流行负面词里的重复写法会原样保留，
    白白吃掉大量 token 并稀释每个词的影响力。

    Args:
        tag: 单个标签。

    Returns:
        归一化后的键。
    """
    key = _EXPLICIT_WEIGHT.sub("", tag.strip())
    key = key.strip(_WRAPPERS).strip()
    return re.sub(r"\s+", " ", key.replace("_", " ")).strip().lower()


def tag_weight(tag: str) -> float:
    """估算标签权重：括号层数（每层 `(` ×1.1、`[` ×0.9）或显式 `:权重`。

    Args:
        tag: 单个标签。

    Returns:
        估算权重；无括号时为 1.0。
    """
    text = tag.strip()
    matched = _EXPLICIT_WEIGHT.search(text)
    if matched:
        try:
            return float(matched.group(0).lstrip(":").strip())
        except ValueError:
            pass
    lead = len(text) - len(text.lstrip("([" ))
    trail = len(text) - len(text.rstrip(")]" ))
    depth = min(lead, trail)
    if depth <= 0:
        return 1.0
    weight = 1.0
    for char in text[:depth]:
        weight *= 1.1 if char == "(" else 0.9
    return weight


def read_image_size(path: str) -> tuple[int, int] | None:
    """读取图片尺寸，不依赖 Pillow。

    只解析文件头，覆盖 PNG / GIF / BMP / WebP(VP8X) / JPEG。
    图生图需要按原图比例决定目标尺寸，为此引入 Pillow 不划算。

    Args:
        path: 本地图片路径。

    Returns:
        (宽, 高)；无法识别时返回 None。
    """
    try:
        with open(path, "rb") as handle:
            head = handle.read(32)
            if len(head) < 12:
                return None
            if head[:8] == b"\x89PNG\r\n\x1a\n":
                width, height = struct.unpack(">II", head[16:24])
                return int(width), int(height)
            if head[:3] == b"GIF":
                width, height = struct.unpack("<HH", head[6:10])
                return int(width), int(height)
            if head[:2] == b"BM":
                width, height = struct.unpack("<ii", head[18:26])
                return abs(int(width)), abs(int(height))
            if head[:4] == b"RIFF" and head[8:12] == b"WEBP" and head[12:16] == b"VP8X":
                width = int.from_bytes(head[24:27], "little") + 1
                height = int.from_bytes(head[27:30], "little") + 1
                return width, height
            if head[:2] == b"\xff\xd8":
                # JPEG：逐段查找 SOFn（帧起始）拿尺寸
                handle.seek(2)
                while True:
                    byte = handle.read(1)
                    while byte and byte != b"\xff":
                        byte = handle.read(1)
                    marker = handle.read(1)
                    while marker == b"\xff":
                        marker = handle.read(1)
                    if not marker:
                        return None
                    if marker[0] in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6,
                                     0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                        handle.read(3)
                        height, width = struct.unpack(">HH", handle.read(4))
                        return int(width), int(height)
                    length = handle.read(2)
                    if len(length) < 2:
                        return None
                    handle.seek(struct.unpack(">H", length)[0] - 2, 1)
    except (OSError, struct.error, ValueError):
        return None
    return None


def default_outpaint_pads(width: int, height: int) -> dict:
    """扩图的默认扩展量：每边扩原图对应边长的 25%，对齐 8 的倍数且至少 64。

    Args:
        width: 原图宽。
        height: 原图高。

    Returns:
        {"left":.., "right":.., "top":.., "bottom":.., "feathering":..}
    """
    side_x = max(64, int(width * 0.25))
    side_y = max(64, int(height * 0.25))
    return {
        "left": side_x - side_x % DIM_ALIGN,
        "right": side_x - side_x % DIM_ALIGN,
        "top": side_y - side_y % DIM_ALIGN,
        "bottom": side_y - side_y % DIM_ALIGN,
        "feathering": DEFAULT_OUTPAINT_FEATHER,
    }


def fit_outpaint_pads(width: int, height: int, pads: dict) -> dict:
    """把扩展量压到像素预算内（避免一扩就爆显存）。

    Args:
        width: 原图宽。
        height: 原图高。
        pads: 期望的扩展量（已含 left/right/top/bottom）。

    Returns:
        缩放后的扩展量（各值对齐 8 的倍数）。
    """
    left, right = int(pads.get("left") or 0), int(pads.get("right") or 0)
    top, bottom = int(pads.get("top") or 0), int(pads.get("bottom") or 0)
    final_w, final_h = width + left + right, height + top + bottom
    if final_w * final_h > MAX_PIXELS:
        scale = (MAX_PIXELS / (final_w * final_h)) ** 0.5
        left = int(left * scale)
        right = int(right * scale)
        top = int(top * scale)
        bottom = int(bottom * scale)
    fitted = dict(pads)
    for key, value in (("left", left), ("right", right), ("top", top), ("bottom", bottom)):
        value = max(0, min(int(value), DIM_MAX))
        fitted[key] = value - value % DIM_ALIGN
    return fitted


def align_video_frames(frames: int, *, block: int = 4, up: bool = True) -> int:
    """把帧数对齐到视频模型要求的 `block*n + 1`（Wan / HunyuanVideo 的约定）。

    Args:
        frames: 期望帧数。
        block: 对齐块（默认 4）。
        up: True 向上对齐（宁可多一帧），False 向下对齐（用于「不许超时长上限」）。

    Returns:
        对齐后的帧数（至少 5 帧）。
    """
    frames = max(1, int(frames or 0))
    steps = -(-(frames - 1) // block) if up else ((frames - 1) // block)
    return max(5, steps * block + 1)


def fit_video_size(width: int, height: int, *, align: int = 16,
                   budget: int = 832 * 480) -> tuple[int, int]:
    """把输入图尺寸调整成视频模型能接受的大小。

    视频模型对尺寸有硬约束：宽高必须是 16 的倍数（Wan / HunyuanVideo 都一样），
    而且比出图更容易爆显存，所以按像素预算等比压一档。

    Args:
        width: 原图宽。
        height: 原图高。
        align: 对齐倍数（默认 16）。
        budget: 像素预算（默认 832x480，Wan 480p 档）。

    Returns:
        (宽, 高)，均为 align 的倍数。
    """
    if width <= 0 or height <= 0:
        return width, height
    scale = 1.0
    if budget > 0 and width * height > budget:
        scale = (budget / (width * height)) ** 0.5
    out_w = max(align, int(width * scale))
    out_h = max(align, int(height * scale))
    out_w -= out_w % align
    out_h -= out_h % align
    return max(align, out_w), max(align, out_h)


def resolve_video_params(opts: dict, video_conf: dict) -> dict:
    """算出这次视频的秒数、帧率与帧数。

    Args:
        opts: 行内参数（--seconds / --fps / --length）。
        video_conf: 配置里的 video 段。

    Returns:
        {"seconds": float, "fps": float, "length": int, "clamped": bool}

    Raises:
        ComfyUIError: 参数非法或超出上限。
    """
    def _number(value, fallback, name, low, high):
        if value in (None, ""):
            return float(fallback)
        try:
            number = float(str(value))
        except (TypeError, ValueError):
            raise ComfyUIError(f"{name} 需要一个数字，收到的是 {value!r}")
        if number <= 0:
            raise ComfyUIError(f"{name} 必须大于 0，收到的是 {value!r}")
        return min(max(number, low), high)

    default_seconds = float(video_conf.get("default_seconds", 4) or 4)
    default_fps = float(video_conf.get("default_fps", 16) or 16)
    max_seconds = float(video_conf.get("max_seconds", 10) or 0)
    seconds = _number(opts.get("seconds"), default_seconds, "--seconds", 0.5, 600)
    fps = _number(opts.get("fps"), default_fps, "--fps", 1, 60)
    clamped = False
    if max_seconds > 0 and seconds > max_seconds:
        seconds = max_seconds
        clamped = True
    if opts.get("length") not in (None, ""):
        try:
            length = align_video_frames(int(float(str(opts["length"]))))
        except (TypeError, ValueError):
            raise ComfyUIError(f"--length 需要帧数，收到的是 {opts['length']!r}")
    else:
        length = align_video_frames(int(round(seconds * fps)))
        if max_seconds > 0 and length / fps > max_seconds:
            # 截断时改成向下对齐：对齐后不允许再超过上限
            length = align_video_frames(int(max_seconds * fps), up=False)
            clamped = True
    seconds = round(length / fps, 2)
    return {"seconds": seconds, "fps": fps, "length": length, "clamped": clamped}


# Wan 系列是按 24fps、81~121 帧训练的。用低帧率/极少帧去跑，模型几乎没有时间维上下文，
# 而且按原速播放时会变成「几乎不动的糊图」——真机实测踩过这个坑（17 帧 @8fps 出片像静止图）。
WAN_NATIVE_FPS = 24.0
WAN_NATIVE_LENGTH = 81          # 3.375 秒；官方示例是最长 121 帧（5 秒）

# 各视频架构的原生节奏：(帧率, 默认帧数, 帧数对齐块)
# Wan 系列 24fps / 4n+1；LTX-Video 25fps / 8n+1（官方模板 97 帧）
NATIVE_VIDEO_RHYTHM = {
    "wan": (24.0, 81, 4),
    "wan22": (24.0, 81, 4),
    "ltxv": (25.0, 97, 8),
}


def apply_native_video_defaults(
    info: dict, opts: dict, video_conf: dict, arch: str, preset: dict | None = None
) -> dict:
    """把 Wan 系列的帧率默认拉回原生 24fps（用户显式给了就完全尊重）。

    Args:
        info: resolve_video_params() 的结果。
        opts: 行内参数（用户显式指定过就不再改）。
        video_conf: 配置里的 video 段（读 max_seconds 上限）。
        arch: 当前架构（只有视频架构会被调整）。
        preset: 机器档位（可选），用来再收紧一次帧数上限。

    Returns:
        调整后的 info；非视频架构或用户显式指定过则原样返回。
    """
    rhythm = NATIVE_VIDEO_RHYTHM.get(arch)
    if rhythm is None:
        return info
    native_fps, native_length, block = rhythm
    given_fps = opts.get("fps") not in (None, "")
    given_seconds = opts.get("seconds") not in (None, "")
    given_length = opts.get("length") not in (None, "")
    if given_fps and (given_seconds or given_length):
        return info                     # 用户把节奏定死了，不插手
    fps = float(info["fps"]) if given_fps else native_fps
    if given_length:
        length = int(info["length"])
    elif given_seconds or given_fps:
        # 只给了时长（或只给了帧率）：按已定的那一半换算长度，别丢用户的意思
        length = align_video_frames(int(round(float(info["seconds"]) * fps)), block=block)
    else:
        length = native_length
        length = align_video_frames(length, block=block)
        try:
            max_seconds = float(video_conf.get("max_seconds", 10) or 0)
        except (TypeError, ValueError):
            max_seconds = 10.0
        if max_seconds > 0 and length / fps > max_seconds:
            length = align_video_frames(int(max_seconds * fps), block=block, up=False)
    # 机器档位再收一道：低配就别出 121 帧的长片
    cap = int((preset or {}).get("max_length") or 0)
    if cap and length > cap:
        length = align_video_frames(cap, block=block, up=False)
    adjusted = dict(info)
    adjusted.update({
        "fps": fps,
        "length": length,
        "seconds": round(length / fps, 2),
        "native": True,
    })
    return adjusted


def fit_to_limit(width: int, height: int, max_side: int) -> tuple[int, int]:
    """把尺寸等比缩到最长边不超过 max_side，并对齐到 8 的倍数。

    Args:
        width: 原宽。
        height: 原高。
        max_side: 最长边上限；<=0 表示不限制。

    Returns:
        (宽, 高)。
    """
    if width <= 0 or height <= 0:
        return width, height
    if max_side and max_side > 0 and max(width, height) > max_side:
        ratio = float(max_side) / max(width, height)
        width = max(DIM_ALIGN, int(width * ratio))
        height = max(DIM_ALIGN, int(height * ratio))
    width = max(DIM_ALIGN, width - width % DIM_ALIGN)
    height = max(DIM_ALIGN, height - height % DIM_ALIGN)
    return width, height


def parse_denoise_levels(raw) -> list[float]:
    """把 `--denoise` 的值解析成一档或多档。

    为什么支持多档：denoise 是图生图里唯一一个「说不清该给多少」的参数。
    实测口径是 0.40 几乎只换细节、0.55 开始换画风、0.70 以上接近重画，
    但这个分界随底模与图片而变。与其来回试，不如一次跑三档挑一张。

    Args:
        raw: 行内参数原值，例如 "0.4" 或 "0.4,0.55,0.7"（也认中文逗号）。

    Returns:
        升序去重后的档位列表；非法输入返回 []（调用方据此回退到配置默认值）。
    """
    text = str(raw or "").strip()
    if not text:
        return []
    levels: list[float] = []
    for chunk in text.replace("，", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            value = float(chunk)
        except ValueError:
            # 有一段读不懂就整段作废：宁可回到默认幅度，也别猜用户想要哪档
            return []
        levels.append(min(1.0, max(0.05, value)))
    return sorted(set(levels))[:MAX_DENOISE_LEVELS]


def merge_tags(*chunks: str) -> str:
    """按逗号合并多段提示词，**归一化去重**并保持首次出现的顺序。

    去重时同一个词的不同括号写法会被合并，保留权重最高的那种写法
    （权重相同时保留先出现的），这样既大幅缩短长度，又不削弱规避强度。

    Args:
        *chunks: 多段以逗号分隔的提示词。

    Returns:
        合并后的提示词。
    """
    order: list[str] = []
    best: dict[str, tuple[float, str]] = {}
    for chunk in chunks:
        for raw in str(chunk or "").split(","):
            tag = raw.strip()
            if not tag:
                continue
            key = tag_key(tag)
            if not key:
                continue
            weight = tag_weight(tag)
            if key not in best:
                order.append(key)
                best[key] = (weight, tag)
            elif weight > best[key][0]:
                best[key] = (weight, tag)
    return ", ".join(best[key][1] for key in order)


def estimate_clip_chunks(text: str) -> tuple[int, int]:
    """粗略估算提示词会占用多少个 CLIP 77-token 编码段。

    ComfyUI 对超出 77 token 的提示词**不是截断，而是切成多段分别编码后拼接**
    （见 `sd_clip.process_tokens` 里的 `torch.cat(embeds_out)`）。因此过长的提示词
    不会失效，但每段都要插入 start/end 与 padding，词越多、每段里单个词的相对
    影响力就越低 —— 这是「负面词写了一百个反而没效果」的原因。

    这里用「词数 + 括号字符数」做粗略估算（没有 CLIP 的 BPE 词表，无法精确计数）。

    Args:
        text: 提示词原文。

    Returns:
        (估算 token 数, 估算编码段数)。
    """
    tokens = len(re.findall(r"[()\[\]]|[^\s()\[\],]+", str(text or "")))
    return tokens, max(1, -(-tokens // 77))


def describe_prompt(text: str) -> str:
    """返回提示词长度的简短描述，用于日志与结果提示。"""
    tags = len([t for t in str(text or "").split(",") if t.strip()])
    tokens, chunks = estimate_clip_chunks(text)
    return f"{tags} 个标签 / 约 {tokens} token / 约 {chunks} 段 CLIP 编码"


def _deep_merge(base: dict, patch: dict) -> dict:
    """把 patch 深度合并进 base 的副本。

    Args:
        base: 原始配置。
        patch: 要覆盖的片段。

    Returns:
        合并后的新字典（不修改入参）。
    """
    result = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _sender_name(event: AstrMessageEvent, fallback: str) -> str:
    """取发送者昵称，取不到时回退到 id。"""
    try:
        return str(event.get_sender_name() or fallback)
    except Exception:
        return fallback


def _match_model(wanted: str, pool: list[str]) -> str:
    """在真实模型清单里匹配用户/LLM 给出的名字。

    依次尝试：完全一致 > 忽略扩展名一致 > 结尾一致 > 唯一子串匹配。
    这样既尊重「只能用真实存在的模型」这条底线，又能容忍 LLM 少写目录前缀。

    Args:
        wanted: 期望的名字或关键词。
        pool: 真实存在的文件名列表。

    Returns:
        匹配到的真实文件名；匹配不到返回空字符串。
    """
    wanted = (wanted or "").strip()
    if not wanted or not pool:
        return ""
    if wanted in pool:
        return wanted

    lowered = wanted.lower()
    bare = lowered.rsplit(".", 1)[0]
    for name in pool:
        if name.lower().rsplit(".", 1)[0] == bare:
            return name
    for name in pool:
        if name.lower().endswith("/" + lowered) or name.lower().endswith("\\" + lowered):
            return name
    candidates = [name for name in pool if bare in name.lower()]
    if len(candidates) == 1:
        return candidates[0]
    return ""
