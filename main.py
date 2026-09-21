"""AstrBot ComfyUI 智能绘图插件主入口。"""
from __future__ import annotations

import asyncio
import json
import random
import re
import time
from pathlib import Path

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Image, Plain
from astrbot.api.star import Context, Star, StarTools

from .comfyui_api import ComfyUI, ComfyUIError, normalize_base_url
from .llm_service import LLMService
from .pages import register_pages_routes
from .permission import PermissionManager
from .storage import Storage
from .workflow_templates import (
    ARCH_PROFILES,
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
PLUGIN_VERSION = "0.3.7"
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
    "lora": "lora", "模型": "model", "model": "model",
    "negative": "negative", "负面": "negative",
}
RATIO_PRESETS = {
    "1:1": (1024, 1024), "16:9": (1344, 768), "9:16": (768, 1344),
    "4:3": (1152, 896), "3:4": (896, 1152), "3:2": (1216, 832),
    "2:3": (832, 1216), "21:9": (1536, 640),
}
# 采样尺寸必须是 8 的倍数
DIM_ALIGN = 8
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
        # --key value
        if token.startswith("--") and len(token) > 2:
            key = PARAM_ALIASES.get(token[2:].lower())
            if key and index + 1 < len(tokens):
                opts[key] = tokens[index + 1]
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
        # 插件专属 logger（self.logger）是 AstrBot v4.27.3 才提供的；
        # 更早的版本上回退到全局 logger，避免整个插件在实例化阶段就崩。
        self._has_plugin_logger = bool(getattr(self, "logger", None))
        if not self._has_plugin_logger:
            import logging

            self.logger = logging.getLogger("astrbot")

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
        self.templates: dict[str, WorkflowTemplate] = {}
        self.template_errors: list[str] = []
        # 在 __init__ 里就加载，避免任何早于 initialize() 的调用（Pages /模板列表）看到空模板
        self._load_templates()
        self._cancel = asyncio.Event()
        self._active_jobs: set[str] = set()

        # 注册插件 Pages 的后端 API。
        # 注意：必须在 __init__ 里调用——漏掉的话，配置页/模型页/状态页的每个请求
        # 都会被 Dashboard 回以「未找到该路由」。
        self.pages_ready = False
        try:
            self.pages_ready = bool(register_pages_routes(self))
        except Exception as e:
            # Pages 不可用不应让聊天指令一起失效，但必须在日志里显式报错（不是 warning）
            self.logger.error(
                "插件 Pages 路由注册失败，配置页/状态页将无法使用：%s", e, exc_info=True
            )

        self.logger.info("ComfyUI 智能绘图已加载，数据目录：%s", self.data_dir)

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
            logger=self.logger,
        )

    @property
    def user_template_dir(self) -> Path:
        """用户自带模板目录（位于数据目录，插件更新不会覆盖）。"""
        return self.data_dir / "workflows"

    def reload_components(self) -> None:
        """按最新配置重建各组件（配置变更后调用）。"""
        self.permission.reload(self.config.get("permission", {}) or {})
        self.llm = LLMService(self.context, self.config)
        old_client = self.comfy
        self.comfy = self._build_client()
        # 继承旧的模型缓存，避免改配置后要重新全量扫描
        self.comfy._model_cache = getattr(old_client, "_model_cache", {})
        self.comfy._model_cache_at = getattr(old_client, "_model_cache_at", 0.0)
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
        # 启动横幅：一眼确认「跑的是哪一版、加载了几个模板、日志走哪条路径」。
        # 排查「装的是新版还是旧版」这类问题时非常省事。
        self.logger.info(
            "ComfyUI 智能绘图 v%s 已激活｜模板 %d 个｜数据目录 %s｜"
            "插件专属日志 %s｜Pages %s｜排队补偿上限 %s 个任务",
            PLUGIN_VERSION,
            len(self.templates),
            self.data_dir,
            "可用" if self._has_plugin_logger else "不可用（已回退全局 logger）",
            "已注册" if self.pages_ready else "注册失败（配置页与状态页将不可用）",
            self.comfy.max_tasks_ahead,
        )
        keep = int((self.config.get("output") or {}).get("keep_images", 500) or 500)
        age = int((self.config.get("output") or {}).get("image_max_age_days", 30) or 30)
        removed = self.storage.prune_images(keep=keep, max_age_days=age)
        if removed:
            self.logger.info("已清理 %d 张过期图片", removed)
        self._sync_llm_tool()

    async def terminate(self) -> None:
        """插件被禁用/重载时调用：收口连接与在途任务。"""
        self._cancel.set()
        for prompt_id in list(self._active_jobs):
            try:
                await self.comfy.interrupt(prompt_id)
            except Exception:
                pass
        self._active_jobs.clear()
        await self.comfy.close()
        self.logger.info("ComfyUI 智能绘图已卸载")

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
        self.logger.info("配置已通过 Pages 更新并落盘")
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
            self.logger.debug("切换 LLM 工具状态失败：%s", e)

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
        self, catalog: dict[str, list[str]], opt: dict, opts: dict
    ) -> dict:
        """校验 LLM（或用户）选定的模型是否真实存在，并挑出模板。

        Args:
            catalog: 真实模型清单。
            opt: LLM 返回的选型结果。
            opts: 用户行内参数。

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
        )
        if template is None:
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
    ) -> dict:
        """完整出图流程：选型 → 建图 → 提交 → 等待 → 下载。

        Args:
            user_desc: 用户描述。
            opts: 行内参数。
            event: 消息事件，用于 LLM 会话级 provider 与统计。
            on_queued: 排队提示回调。

        Returns:
            {"images": [Path...], "template": str, "model": str, "lora": str,
             "vae": str, "positive": str, "negative": str, "seconds": float,
             "arch": str, "seed": int, "width": int, "height": int}

        Raises:
            ComfyUIError: 出图失败，message 面向用户。
            TemplateError: 工作流模板问题。
            RuntimeError: LLM 不可用。
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
        if bool(llm_conf.get("enable_prompt_optimize", True)):
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
                self.logger.warning("LLM 不可用，退化为直接使用原描述出图：%s", e)
                opt = {}
                llm_note = "（未启用/无可用 LLM，已直接用你的原话出图）"
            if opt.get("positive"):
                positive = opt["positive"]
            # LLM 的负面词**不再整体替换**默认词：具体合并见下方（需要先知道架构）
            llm_negative = str(opt.get("negative") or "")
        else:
            llm_note = "（提示词优化已关闭，直接使用你的原话）"

        selection = await self._resolve_selection(catalog, opt, opts)
        template: WorkflowTemplate = selection["template"]
        sampling = self._resolve_sampling(selection["arch"], opt, opts, draw_conf)
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
        self.logger.info(
            "本次提示词长度｜正向 %s｜负面 %s",
            describe_prompt(positive),
            describe_prompt(negative),
        )
        prompt_note = ""
        if estimate_clip_chunks(negative)[1] >= 3:
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
            **sampling,
        )

        # 提交前用服务端自己的输入约束做本地预检：
        # 万一 ComfyUI 回一个不带任何节点级原因的「failed validation」，这里能先拦住
        problems = await self.comfy.precheck(graph)
        if problems:
            path = self._dump_failed_graph(graph, "本地预检未通过")
            raise ComfyUIError(
                "提交前的本地校验未通过（按你 ComfyUI 的输入约束检查）：\n"
                + "\n".join(f"　· {p}" for p in problems)
                + f"\n　· 本次工作流已保存到 {path}"
            )

        started = time.time()
        try:
            prompt_id = await self.comfy.submit(
                graph, extra_data={"astrbot_plugin": PLUGIN_NAME}
            )
        except ComfyUIError as e:
            # 把「实际提交的图」落盘：服务端偶尔不返回节点级原因，没有这个就只能靠猜
            path = self._dump_failed_graph(graph, e)
            self.logger.warning(
                "提交失败｜正向提示词 %d 字符｜模板 %s｜底模 %s｜LoRA %s｜图已存 %s",
                len(positive), template.name, selection["model"],
                selection["lora"] or "无", path,
            )
            raise ComfyUIError(f"{e}\n　· 本次工作流已保存到 {path}，可据此排查") from e
        self._active_jobs.add(prompt_id)
        try:
            images = await self.comfy.wait_for_images(
                prompt_id,
                self.storage.output_dir,
                on_queued=on_queued,
                cancel_event=self._cancel,
            )
        finally:
            self._active_jobs.discard(prompt_id)
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
            "seconds": time.time() - started,
            **{k: sampling[k] for k in ("width", "height", "steps", "cfg", "sampler")},
        }

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

        yield event.plain_result("🎨 收到灵感，正在分析并生成…")

        async def _notify_queue(status):
            await event.send(
                event.plain_result(
                    f"⏳ 已提交，队列第 {min(status.own_positions.values() or [1])} 位"
                    f"（前方 {status.tasks_ahead} 个任务）"
                )
            )

        try:
            result = await self.generate(
                user_desc=desc, opts=opts, event=event, on_queued=_notify_queue
            )
        except (ComfyUIError, TemplateError) as e:
            self.logger.warning("出图失败：%s", e)
            yield event.plain_result(f"💥 出图失败：{e}")
            return
        except RuntimeError as e:
            self.logger.warning("LLM 调用失败：%s", e)
            yield event.plain_result(f"💥 {e}")
            return
        except Exception as e:  # pragma: no cover - 兜底，避免 handler 抛出
            self.logger.exception("出图时发生未预期错误")
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
            self.logger.warning("写入失败工作流时出错：%s", e)
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
            if result.get("prompt_note"):
                detail += f"\nℹ️ {result['prompt_note']}"
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
        lines.append(f"　模板：{len(self.templates)} 个")
        lines.append(f"　配置页 API：{'已注册' if self.pages_ready else '❌ 注册失败，请查看日志'}")
        yield event.plain_result("\n".join(lines))

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
            "/模型列表　查看可用模型\n"
            "/模板列表　查看工作流模板（可放自定义模板）\n"
            "/状态　　　查看 ComfyUI 连接与队列\n"
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
            self.logger.warning("无指令出图失败：%s", e)
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
