"""AstrBot ComfyUI 智能绘图插件主入口。"""
from __future__ import annotations

import asyncio
import json
import random
import struct
import re
import time
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Image, Plain, Reply
from astrbot.api.star import Context, Star, StarTools

from .comfyui_api import ComfyUI, ComfyUIError, normalize_base_url
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

PLUGIN_NAME = "astrbot_plugin_comfyui_smart"
# 与 metadata.yaml 的 version 保持一致（tests/test_logic.py 会校验二者不漂移）
PLUGIN_VERSION = "0.9.0"
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
    # 扩图（outpaint）：左右上下扩展量与羽化
    "left": "left", "左": "left", "左边": "left",
    "right": "right", "右": "right", "右边": "right",
    "top": "top", "上": "top", "上边": "top",
    "bottom": "bottom", "下": "bottom", "下边": "bottom",
    "feather": "feather", "feathering": "feather", "羽化": "feather",
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

        self.permission = PermissionManager(self.config.get("permission", {}) or {})
        self.llm = LLMService(context, self.config)
        self.comfy = self._build_client()
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
    def _build_client(self) -> ComfyUI:
        """按当前配置构造 ComfyUI 客户端。"""
        server = self.config.get("server", {}) or {}
        return ComfyUI(
            normalize_base_url(server.get("base_url", "http://127.0.0.1:8188")),
            int(server.get("timeout", 180) or 180),
            poll_interval=float(server.get("poll_interval", 1.5) or 1.5),
            max_tasks_ahead=int(server.get("max_tasks_ahead", 10) or 10),
            logger=logger,
        )

    @property
    def user_template_dir(self) -> Path:
        """用户自带模板目录（位于数据目录，插件更新不会覆盖）。"""
        return self.data_dir / "workflows"

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
        self.permission.reload(self.config.get("permission", {}) or {})
        self.llm = LLMService(self.context, self.config)
        old_client = self.comfy
        self.comfy = self._build_client()
        # 继承旧的模型缓存，避免改配置后要重新全量扫描
        self.comfy._model_cache = getattr(old_client, "_model_cache", {})
        self.comfy._model_cache_at = getattr(old_client, "_model_cache_at", 0.0)
        self._configure_gate()
        self._load_templates()

    def _load_templates(self) -> None:
        """加载内置模板与用户自带模板（用户同名模板优先）。"""
        self.user_template_dir.mkdir(parents=True, exist_ok=True)
        templates = load_templates(BUILTIN_TEMPLATE_DIR)
        user_templates = load_templates(self.user_template_dir)
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
            try:
                await self.comfy.interrupt(prompt_id)
            except Exception:
                pass
        self._active_jobs.clear()
        self._job_cancel.clear()
        await self.comfy.close()
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
        }
        try:
            stats = await self.comfy.ping()
            info["online"] = True
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
    # 出图核心
    # ------------------------------------------------------------------ #
    async def _resolve_selection(
        self, catalog: dict[str, list[str]], opt: dict, opts: dict, purpose: str = "t2i"
    ) -> dict:
        """校验 LLM（或用户）选定的模型是否真实存在，并挑出模板。

        Args:
            catalog: 真实模型清单。
            opt: LLM 返回的选型结果。
            opts: 用户行内参数。
            purpose: t2i（文生图）或 i2i（图生图）。

        Returns:
            {"model":..., "folder":..., "lora":..., "vae":..., "template":..., "arch":...}
        """
        checkpoints = catalog.get("checkpoints") or []
        unets = catalog.get("diffusion_models") or []
        loras = catalog.get("loras") or []
        vaes = catalog.get("vae") or []

        # 模型选择：用户行内参数 > LLM > 第一个可用
        model = str(opts.get("model") or "").strip()
        folder = "checkpoints"
        pool = checkpoints
        if model:
            matched = _match_model(model, checkpoints)
            if not matched:
                matched = _match_model(model, unets)
                if matched:
                    folder, pool = "diffusion_models", unets
            model = matched
        if not model:
            wanted = str(opt.get("checkpoint") or "").strip()
            if wanted and wanted in checkpoints:
                model = wanted
            elif wanted and wanted in unets:
                model, folder, pool = wanted, "diffusion_models", unets
            elif checkpoints:
                model = checkpoints[0]
            elif unets:
                model, folder, pool = unets[0], "diffusion_models", unets
        if not model:
            raise ComfyUIError(
                "ComfyUI 里没有发现任何可用的底模（checkpoints / diffusion_models 都是空的）"
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
        available = await self.comfy.node_classes()
        arch_override = str(
            (self.config.get("draw_settings") or {}).get("arch_override") or ""
        ).strip().lower()
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
            source_image: 图生图的输入图本地路径；给了就走图生图模板。
            force_purpose: 强制用途（如 outpaint 扩图），空则按有无输入图推断。

        Returns:
            {"images": [Path...], "template": str, "model": str, "lora": str,
             "vae": str, "positive": str, "negative": str, "seconds": float,
             "queued_seconds": float, "arch": str, "seed": int, "width": int, "height": int}

        Raises:
            ComfyUIError: 出图失败，message 面向用户。
            TemplateError: 工作流模板问题。
            RuntimeError: LLM 不可用，或排队等待超时（QueueTimeout）。
        """
        opts = opts or {}
        draw_conf = self.config.get("draw_settings", {}) or {}
        catalog = await self.get_catalog()
        if not catalog:
            raise ComfyUIError(
                "没有发现任何模型。请先在配置里填好 ComfyUI 地址，或用 /刷新模型 重新拉取"
            )

        default_negative = str(draw_conf.get("default_negative") or "")
        llm_conf = self.config.get("llm_settings", {}) or {}
        opt: dict = {}
        positive = user_desc
        llm_negative = ""
        llm_note = ""
        preset = preset or {}
        if preset.get("positive"):
            # 已经有现成提示词（例如 /反推 的结果）：不再让 LLM 改写一遍
            positive = str(preset["positive"])
            llm_negative = str(preset.get("negative") or "")
            llm_note = "（提示词来自反推结果，未再改写）"
            if preset.get("checkpoint"):
                opt["checkpoint"] = preset["checkpoint"]
        elif bool(llm_conf.get("enable_prompt_optimize", True)):
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

        # 图生图 / 扩图：先把输入图上传到 ComfyUI，拿到 LoadImage 能用的引用
        purpose = force_purpose or ("i2i" if source_image else "t2i")
        image_ref = ""
        if source_image:
            i2i_sub = str((self.config.get("i2i", {}) or {}).get("subfolder") or "astrbot").strip()
            image_ref = await self.comfy.upload_image(source_image, subfolder=i2i_sub)

        selection = await self._resolve_selection(catalog, opt, opts, purpose=purpose)
        template: WorkflowTemplate = selection["template"]
        sampling = self._resolve_sampling(selection["arch"], opt, opts, draw_conf)

        # 图生图：尺寸按原图比例（受 max_side 限制），重绘幅度可调
        # 扩图：尺寸由「原图 + 四周扩展量」决定（节点自己会算，这里只用于汇报与限额）
        denoise = None
        outpaint_info: dict = {}
        template_params: dict = {}
        if purpose == "i2i":
            i2i_conf = self.config.get("i2i", {}) or {}
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
        if purpose in ("i2i", "outpaint"):
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
        problems = await self.comfy.precheck(graph)
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
                    prompt_id = await self.comfy.submit(
                        graph, extra_data={"astrbot_plugin": PLUGIN_NAME}
                    )
                except ComfyUIError as e:
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
                # 登记在途任务：`/取消` 靠这两张表找到「这个人的任务」并定点打断
                job_cancel = asyncio.Event()
                self._active_jobs[prompt_id] = uid
                self._job_cancel[prompt_id] = job_cancel
                try:
                    images = await self.comfy.wait_for_images(
                        prompt_id,
                        self.storage.output_dir,
                        on_queued=on_queued,
                        on_progress=on_progress,
                        cancel_event=self._cancel,
                        user_cancel_event=job_cancel,
                    )
                finally:
                    self._active_jobs.pop(prompt_id, None)
                    self._job_cancel.pop(prompt_id, None)
        except QueueTimeout as e:
            # 排队超时不是「出图失败」而是「没轮上」：记 info 便于区分，再交给指令层告知用户
            logger.info("出图排队超时：%s", e)
            raise
        if not images:
            raise ComfyUIError("出图失败：没有取到任何图片")
        return {
            "images": images,
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
            "i2i": bool(image_ref),
            "denoise": denoise if denoise is not None else 1.0,
            "outpaint": outpaint_info,
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
                text = (
                    f"⏳ 你已有一张在出，本次排队等待"
                    f"（同时出图上限 {info['max_concurrent']}）…"
                )
            else:
                text = (
                    f"⏳ 前面还有 {info['ahead']} 个任务，排队中"
                    f"（同时出图上限 {info['max_concurrent']}）…"
                )
            await event.send(event.plain_result(text))

        async def _on_queued(status):
            await event.send(
                event.plain_result(
                    f"⏳ 已提交，队列第 {min(status.own_positions.values() or [1])} 位"
                    f"（前方 {status.tasks_ahead} 个任务）"
                )
            )

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
            await event.send(
                event.plain_result(
                    f"🎨 采样中 {info.get('value', 0)}/{info.get('max', 0)}（{percent}%）"
                    f"　用 /取消 可以中止这次出图"
                )
            )

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
            outcome = await self.comfy.cancel_prompt(prompt_id)
            results.append(
                {
                    "running": "已中断正在执行的任务",
                    "pending": "已从 ComfyUI 队列里移除",
                    "not_found": "任务已不在 ComfyUI 队列里（可能刚好跑完）",
                }.get(outcome, outcome)
            )

        # 还没拿到名额、在插件侧排队的人，也要能取消
        waiting = self.gate.cancel_waiting(
            "" if want_all else uid, "本次出图已被 /取消 取消"
        )

        if not results and not waiting:
            yield event.plain_result(
                "🤔 你现在没有正在排队或正在出图的任务"
                + ("" if is_admin else "（管理员可用 /取消 全部 取消所有人的）")
            )
            return
        lines = ["🛑 已取消"]
        for item in results:
            lines.append(f"　· {item}")
        if waiting:
            lines.append(f"　· 已取消 {waiting} 个在插件侧排队等待的任务")
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

        raw = _extract_command_payload(event, "画图", "绘图", "draw", "生成图片")
        desc, opts = parse_inline_params(raw)
        if not desc:
            yield event.plain_result(
                "🎨 用法：/画图 <描述> [参数]\n"
                "例如：/画图 一个白裙少女站在樱花树下\n"
                "　　　/画图 16:9 赛博朋克城市 --seed 42 --steps 30"
            )
            return

        # 带了图片就走图生图（可在配置里关掉）
        source_image = ""
        images = await self._collect_images(event)
        if images:
            if bool((self.config.get("i2i", {}) or {}).get("enable", True)):
                source_image = images[0]
            else:
                yield event.plain_result("ℹ️ 检测到图片，但图生图已在配置里关闭，本次按文生图处理")

        yield event.plain_result(
            "🖼 收到图片，正在按你的描述重绘…" if source_image
            else "🎨 收到灵感，正在分析并生成…"
        )

        on_wait, on_queued, on_progress = self._queue_notifiers(event)

        try:
            result = await self.generate(
                user_desc=desc,
                opts=opts,
                event=event,
                on_wait=on_wait,
                on_queued=on_queued,
                on_progress=on_progress,
                source_image=source_image,
            )
        except (ComfyUIError, TemplateError) as e:
            logger.warning("出图失败：%s", e)
            yield event.plain_result(f"💥 出图失败：{e}")
            return
        except RuntimeError as e:
            logger.warning("LLM 调用失败：%s", e)
            yield event.plain_result(f"💥 {e}")
            return
        except Exception as e:  # pragma: no cover - 兜底，避免 handler 抛出
            logger.exception("出图时发生未预期错误")
            yield event.plain_result(f"💥 出图时发生未预期错误：{e}")
            return

        await self.permission.record(uid, is_admin=is_admin, storage=self.storage)
        await self._record_generation(uid, event, result)

        chain = self._compose_result_chain(event, uid, result)
        yield event.chain_result(chain)

    async def _record_generation(
        self, uid: str, event: AstrMessageEvent, result: dict
    ) -> None:
        """记录一次成功出图（含完整参数，供画廊展示与复现）。

        Args:
            uid: 触发者 id。
            event: 消息事件，用于取昵称。
            result: generate() 的返回值。
        """
        await self.storage.record_generation(
            user_id=uid,
            user_name=_sender_name(event, uid),
            positive=result["positive"],
            negative=result["negative"],
            models={
                "checkpoint": result["model"],
                "lora": result["lora"],
                "vae": result["vae"],
                "template": result["template"],
            },
            images=[f"images/{p.name}" for p in result["images"]],
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
            detail = (
                f"🖼 {result['template']}（{result.get('arch', '?')}）· {result['model']}"
                f" · {result['width']}x{result['height']}"
                f" · seed {result['seed']} · {result['seconds']:.1f}s"
            )
            if result.get("lora"):
                detail += f"\n🎯 LoRA：{result['lora']}"
            if result.get("llm_note"):
                detail += f"\nℹ️ {result['llm_note']}"
            if result.get("i2i"):
                detail += f"\n🖼 图生图：重绘幅度 {result.get('denoise', 0.6)}"
            if result.get("outpaint"):
                _op = result["outpaint"]
                _pads = _op.get("pads") or {}
                _src = _op.get("source") or (0, 0)
                detail += (
                    f"\n🪄 扩图：{_src[0]}x{_src[1]} → {_op.get('width')}x{_op.get('height')}"
                    f"（左右 +{_pads.get('left', 0)}/+{_pads.get('right', 0)}"
                    f"、上下 +{_pads.get('top', 0)}/+{_pads.get('bottom', 0)}）"
                )
            if result.get("hires"):
                _hw = result["hires"].get("width")
                _hh = result["hires"].get("height")
                if _hw and _hh:
                    detail += f"\n🔍 Hires Fix：{_hw}x{_hh}"
                else:
                    # 尺寸由工作流自己决定，只说倍数，避免编一个尺寸出来
                    detail += f"\n🔍 Hires Fix：×{result['hires'].get('scale', '')}"
            if result.get("hires_note"):
                detail += f"\n⚠️ {result['hires_note']}"
            if result.get("prompt_note"):
                detail += f"\nℹ️ {result['prompt_note']}"
            if result.get("queued_seconds"):
                # 排队时间与出图时间分开报，否则「这次怎么这么慢」说不清
                detail += f"\n⏳ 排队等待 {result['queued_seconds']:.0f}s"
            chain.append(Plain(detail + "\n"))
        for path in result["images"]:
            chain.append(Image.fromFileSystem(str(path)))
        return chain

    @filter.command("刷新模型")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_refresh_models(self, event: AstrMessageEvent):
        """重新发现 ComfyUI 里可用的模型（管理员）。"""
        yield event.plain_result("🔍 正在读取 ComfyUI 的模型清单…")
        result = await self.refresh_models()
        if not result["ok"]:
            yield event.plain_result(f"⚠️ {result['message']}")
            return
        lines = [f"✅ {result['message']}"]
        for folder, files in sorted(result["catalog"].items()):
            lines.append(f"　【{folder}】{len(files)} 个")
        yield event.plain_result("\n".join(lines))

    @filter.command("模型列表", alias={"模型"})
    async def cmd_model_list(self, event: AstrMessageEvent):
        """查看可用的模型清单。"""
        catalog = await self.get_catalog()
        if not catalog:
            yield event.plain_result("📭 暂无模型数据，请先 /刷新模型 或检查 ComfyUI 地址")
            return
        lines = ["📦 可用模型"]
        for folder, files in sorted(catalog.items()):
            lines.append(f"\n【{folder}】({len(files)} 个)")
            for name in files[:12]:
                lines.append(f"　- {name}")
            if len(files) > 12:
                lines.append(f"　… 共 {len(files)} 个（完整清单见配置页）")
        yield event.plain_result("\n".join(lines))

    @filter.command("模板列表", alias={"工作流"})
    async def cmd_template_list(self, event: AstrMessageEvent):
        """查看当前加载的工作流模板。"""
        if not self.templates:
            yield event.plain_result("⚠️ 没有加载到任何工作流模板，请检查插件 workflows 目录")
            return
        lines = ["🧩 工作流模板"]
        for name in sorted(self.templates):
            tpl = self.templates[name]
            info = tpl.describe()
            lines.append(
                f"　- {name}（架构 {info['arch']}，加载方式 {info['loader']}，{info['nodes']} 节点）"
            )
        lines.append(f"\n架构档案：{'、'.join(sorted(ARCH_PROFILES))}")
        user_dir = self.user_template_dir
        lines.append(f"自定义模板目录（放 *.json 即可）：{user_dir}")
        if self.template_errors:
            lines.append(f"⚠️ 以下文件加载失败：{'、'.join(self.template_errors)}")
        yield event.plain_result("\n".join(lines))

    @filter.command("状态")
    async def cmd_status(self, event: AstrMessageEvent):
        """查看 ComfyUI 连接与队列状态。"""
        info = await self.get_server_status()
        lines = [f"🖥 ComfyUI：{info['base_url']}"]
        lines.append("　连接：✅ 正常" if info["online"] else f"　连接：❌ {info.get('error', '不可用')}")
        if info.get("device"):
            lines.append(f"　设备：{info['device']}")
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

        raw = _extract_command_payload(event, "图生图", "改图", "i2i", "重绘")
        desc, opts = parse_inline_params(raw)

        images = await self._collect_images(event)
        if not images:
            yield event.plain_result(
                "🖼 用法：把图片和 /图生图 一起发，或者回复一张图片再发\n"
                "　　/图生图 改成冬天，围上红色围巾\n"
                "　　/图生图 换成赛博朋克风格 --denoise 0.7\n"
                "　　/图生图 只修细节 --denoise 0.3\n"
                "重绘幅度 --denoise：0.3 微调、0.5~0.6 改风格、0.8+ 接近重画（默认 0.6）"
            )
            return
        if not desc:
            yield event.plain_result(
                "🖼 请说明想怎么改，例如：/图生图 改成冬天，围上红色围巾\n"
                "（如果想保留原图不动只做细节修补，用 --denoise 0.3）"
            )
            return

        yield event.plain_result("🖼 正在按你的描述重绘…")

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
            )
        except (ComfyUIError, TemplateError) as e:
            logger.warning("图生图失败：%s", e)
            yield event.plain_result(f"💥 出图失败：{e}")
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

        raw = _extract_command_payload(event, "扩图", "外扩", "outpaint", "扩画")
        desc, opts = parse_inline_params(raw)

        images = await self._collect_images(event)
        if not images:
            yield event.plain_result(
                "🪄 用法：把图片和 /扩图 一起发，或者回复一张图片再发\n"
                "　　/扩图 把画面往两边扩成宽幅　（不给参数时四周各扩原图的 25%）\n"
                "　　/扩图 --left 256 --right 256　只往左右扩\n"
                "　　/扩图 往下补出脚和地面 --bottom 384 --top 0\n"
                "参数：--left/--right/--top/--bottom（像素，自动对齐 8 的倍数）、\n"
                "　　　--feather（接缝羽化，默认 40）、--model 指定底模、--seed 复现"
            )
            return
        if not desc:
            # 不给描述也能用：给一句通用的「往外延伸」，交给 LLM 改写（若开启）
            desc = "继续向外延伸画面，补全被裁掉的构图，保持一致的风格、光影与细节"

        yield event.plain_result("🪄 正在把画面往外扩…")
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
            yield event.plain_result(f"💥 扩图失败：{e}")
            return
        except RuntimeError as e:
            yield event.plain_result(f"💥 {e}")
            return

        await self.permission.record(uid, is_admin=is_admin, storage=self.storage)
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

        raw = _extract_command_payload(event, "反推", "反推提示词", "识图", "img2prompt")
        hint, opts = parse_inline_params(raw)

        images = await self._collect_images(event)
        if not images:
            yield event.plain_result(
                "🖼 用法：把图片和 /反推 一起发，或者回复一张图片再发 /反推\n"
                "　　/反推 帮我看看这张图怎么写提示词\n"
                "　　/反推 --画　反推后直接出图\n"
                "　　/反推 --provider <provider_id>　指定看图模型（当前会话模型不支持看图时用）\n"
                "　　/反推 --model juggernaut --画　指定底模出图"
            )
            return

        pid = str(opts.get("provider") or "").strip()
        yield event.plain_result(
            f"🔍 正在用 {self.llm.provider_label(pid) if pid else '当前会话模型'} 分析 "
            f"{len(images)} 张图片…"
        )
        try:
            result = await self.llm.reverse_prompt(
                images, hint=hint, event=event, provider_id=pid
            )
        except RuntimeError as e:
            logger.warning("反推失败：%s", e)
            yield event.plain_result(f"💥 {e}")
            return

        if not result.get("positive"):
            yield event.plain_result(
                "💥 没能从图里反推出提示词。请确认所用对话模型支持看图，或换一张更清晰的图"
            )
            return

        lines = ["🔍 反推结果"]
        if result.get("model"):
            lines.append(f"（看图模型：{result['model']}）")
        if result.get("summary"):
            lines.append(f"画面：{result['summary']}")
        lines.append(f"\n正向提示词：\n{result['positive']}")
        if result.get("negative"):
            lines.append(f"\n建议负面词：\n{result['negative']}")

        if not opts.get("draw"):
            lines.append("\n出图：/反推 --画　（或把上面的正向提示词交给 /画图）")
            yield event.plain_result("\n".join(lines))
            return

        yield event.plain_result("\n".join(lines) + "\n\n🎨 正在按反推结果出图…")

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
            yield event.plain_result(f"💥 出图失败：{e}")
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
        lines = ["📊 使用统计"]
        users = stats.get("users") or {}
        if users:
            lines.append("【用户出图次数】")
            for uid, info in sorted(
                users.items(), key=lambda kv: -int(kv[1].get("count", 0))
            )[:10]:
                lines.append(f"　- {info.get('name', uid)}：{info.get('count', 0)} 次")
        usage = stats.get("model_usage") or {}
        for key, label in (("checkpoint", "底模"), ("lora", "LoRA"), ("vae", "VAE")):
            if usage.get(key):
                lines.append(f"【{label}调用】")
                for name, count in sorted(
                    usage[key].items(), key=lambda kv: -kv[1]
                )[:5]:
                    lines.append(f"　- {name}：{count} 次")
        records = stats.get("records") or []
        if records:
            lines.append(f"【最近出图】共 {len(records)} 条记录，配置页可查看画廊")
        if len(lines) == 1:
            lines.append("暂无记录")
        yield event.plain_result("\n".join(lines))

    @filter.command("帮助", alias={"comfy帮助"})
    async def cmd_help(self, event: AstrMessageEvent):
        """查看帮助。"""
        yield event.plain_result(
            "🎨 ComfyUI 智能绘图\n"
            "━━━━━━━━━━━━━━\n"
            "/画图 <描述>　用一句中文出图\n"
            "　行内参数：16:9 / --size 1024x1536 / --seed 42\n"
            "　　　　　　--steps 30 / --cfg 6 / --lora 名字:0.8\n"
            "　　　　　　--model 关键词 / --batch 2 / --negative \"...\"\n"
            "/图生图　　以图为底按描述重绘（别名 /改图，--denoise 控制幅度）\n"
            "/扩图　　　把画面往外扩，补全构图（--left/--right/--top/--bottom 像素）\n"
            "/反推　　　看图反推提示词（发图或回复图片，加 --画 直接出图）\n"
            "/模型列表　查看可用模型\n"
            "/模板列表　查看工作流模板（可放自定义模板）\n"
            "/状态　　　查看 ComfyUI 连接与队列\n"
            "/取消　　　取消自己正在排队或正在出图的任务（管理员：/取消 全部）\n"
            "/统计　　　查看出图统计\n"
            "/刷新模型　重新读取模型清单（管理员）\n"
            "/帮助　　　显示本帮助"
        )

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
        if not agent_conf.get("enable_llm_tool", False):
            yield event.plain_result("绘图工具当前未启用，请让管理员在插件配置里打开「无指令出图」。")
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
