"""ComfyUI HTTP 客户端。

设计要点（对照上游 server.py / folder_paths.py 的真实契约）：
- 复用单一 aiohttp session，由 close() 收口；不再每次请求新建连接。
- 模型发现走 `GET /models`（服务器实际有哪些模型文件夹）+ `GET /models/{folder}`（该文件夹的文件），
  而不是硬解析 `/object_info` 的固定四个节点 —— 后者在新版 ComfyUI 里节点 schema 有两种写法
  （V2 的 `[[名字...], {tooltip}]` 与 V3 的 combo 描述），解析脆弱且漏掉自定义文件夹。
- 老版本没有 `/models/{folder}` 时回退到 `/object_info` 解析，两种 schema 都能读。
- 提交时带 client_id，用于在共享的 ComfyUI 上区分「自己的任务」。
- 等待有硬超时，并按队列前方任务数补偿；不再是无上限的 while True。
- `/prompt` 返回 400 时解析 node_errors，翻译成可读中文，而不是把原始 dict 甩给用户。
"""
from __future__ import annotations

import asyncio
import json
import mimetypes
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp

# 模型文件夹 -> 探测用的 (节点类, 输入键)。用于无 /models 端点时回退解析 object_info。
FOLDER_PROBE: dict[str, tuple[str, str]] = {
    "checkpoints": ("CheckpointLoaderSimple", "ckpt_name"),
    "diffusion_models": ("UNETLoader", "unet_name"),
    "loras": ("LoraLoader", "lora_name"),
    "vae": ("VAELoader", "vae_name"),
    "text_encoders": ("DualCLIPLoader", "clip_name1"),
    "unet_gguf": ("UnetLoaderGGUF", "unet_name"),
    "clip_gguf": ("CLIPLoaderGGUF", "clip_name"),
    "controlnet": ("ControlNetLoader", "control_net_name"),
    "clip_vision": ("CLIPVisionLoader", "clip_name"),
    "upscale_models": ("UpscaleModelLoader", "model_name"),
}
# 出图流程真正会用到的模型目录：排在清单最前
PRIMARY_MODEL_FOLDERS: tuple[str, ...] = (
    "checkpoints",
    "diffusion_models",
    # GGUF 量化模型（ComfyUI-GGUF 注册的专用目录）：8G 显存/16G 内存的机器靠它跑视频
    "unet_gguf",
    "loras",
    "vae",
    "text_encoders",
    "controlnet",
)
# 其它确实是模型、但插件不直接使用的目录：保留但排在后面
OTHER_MODEL_FOLDERS: tuple[str, ...] = (
    "clip_vision",
    "style_models",
    "gligen",
    "photomaker",
    "model_patches",
    "hypernetworks",
    "diffusers",
    "upscale_models",
    "latent_upscale_models",
    "audio_encoders",
    "background_removal",
    "frame_interpolation",
    "geometry_estimation",
    "optical_flow",
    "detection",
    "classifiers",
)
# 明确排除的目录，原因分两类：
#   1) 根本不是模型目录：ComfyUI 的 GET /models 返回的是 folder_names_and_paths 的**全部**键，
#      其中包含 custom_nodes / configs / datasets。
#   2) 是模型但只会污染清单：embeddings（动辄上千个文本反演文件，本插件的工作流不用）、
#      vae_approx（taesd 预览用小模型，不是真正的 VAE）。
#
# 尤其注意 custom_nodes：它注册时的扩展名白名单是**空列表**，而
# folder_paths.filter_files_extensions 对空列表放行所有文件，
# 于是递归 custom_nodes 会把每个自定义节点包里的 .py/.js/node_modules 全列出来
# —— 这正是「模型数量突然变成几千个」的原因。
EXCLUDED_FOLDERS: frozenset[str] = frozenset(
    {
        "custom_nodes",
        "configs",
        "datasets",
        "embeddings",
        "vae_approx",
    }
)
# 允许进入清单的目录（白名单，避免 ComfyUI 新增非模型目录时又被算进来）
MODEL_FOLDERS: frozenset[str] = frozenset(
    PRIMARY_MODEL_FOLDERS + OTHER_MODEL_FOLDERS + ("unet", "clip", "clip_gguf", "text_encoders")
)

# 旧文件夹名 -> 新名（ComfyUI 的 folder_paths.map_legacy）
LEGACY_FOLDERS = {
    "unet": "diffusion_models",
    "clip": "text_encoders",
    "clip_gguf": "text_encoders",
}
DEFAULT_FOLDERS = ("checkpoints", "diffusion_models", "loras", "vae", "text_encoders")

# 这些输入的取值由节点自己的 VALIDATE_INPUTS 校验（ComfyUI 会跳过标准下拉校验），
# 并且允许写子目录路径（如 "astrbot/xxx.png"）。本地预检不能拿 /object_info 的下拉列表去卡，
# 否则会把完全合法的请求误判为错误 —— 实测 LoadImage 就是这种（子目录引用可以正常出图）。
SELF_VALIDATED_PATH_INPUTS = frozenset({
    ("LoadImage", "image"),
    ("LoadImageMask", "image"),
    ("LoadImageOutput", "image"),
})


class ComfyUIError(RuntimeError):
    """ComfyUI 交互失败，message 已经是给用户看的中文。

    Attributes:
        raw_response: 服务端返回的原始错误结构（若可用），供日志与排查使用。
    """

    raw_response: dict | None = None


def normalize_base_url(url: str) -> str:
    """补全协议并去掉尾部斜杠。

    Args:
        url: 用户填写的地址，如 127.0.0.1:8188。

    Returns:
        规范化后的地址。
    """
    url = (url or "").strip()
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    return url.rstrip("/")


@dataclass
class QueueStatus:
    """队列状态。own_* 只统计本插件实例提交的任务。"""

    own_running: int = 0
    own_pending: int = 0
    total_running: int = 0
    total_pending: int = 0
    own_positions: dict[str, int] = field(default_factory=dict)

    @property
    def tasks_ahead(self) -> int:
        """本插件任务前方还有多少个任务。"""
        if not self.own_positions:
            return 0
        return max(min(self.own_positions.values()) - 1, 0)


def _combo_options(spec_entry) -> list[str] | None:
    """从 object_info 的输入描述里取下拉可选项（兼容 V2 与 V3 两种写法）。

    Args:
        spec_entry: 形如 `[[选项...], {tooltip}]`（V2）或
            `{"type": "COMBO", "options": [...]}`（V3）。

    Returns:
        选项列表；不是下拉类型时返回 None。
    """
    if isinstance(spec_entry, dict):
        options = spec_entry.get("options")
        if isinstance(options, list) and all(isinstance(o, str) for o in options):
            return options
        return None
    if isinstance(spec_entry, list) and spec_entry and isinstance(spec_entry[0], list):
        first = spec_entry[0]
        if first and all(isinstance(o, str) for o in first):
            return first
    return None


def _numeric_bounds(spec_entry) -> tuple[float | None, float | None]:
    """取输入描述里的数值上下界。"""
    meta = None
    if isinstance(spec_entry, dict):
        meta = spec_entry
    elif isinstance(spec_entry, list) and len(spec_entry) > 1 and isinstance(spec_entry[1], dict):
        meta = spec_entry[1]
    if not isinstance(meta, dict):
        return (None, None)
    low, high = meta.get("min"), meta.get("max")
    return (
        low if isinstance(low, (int, float)) and not isinstance(low, bool) else None,
        high if isinstance(high, (int, float)) and not isinstance(high, bool) else None,
    )


def validate_graph_locally(graph: dict, specs: dict) -> list[str]:
    """在本地复刻 ComfyUI 最常用的两类输入校验。

    只检查字面量输入（连线交给服务端），覆盖：
      - 下拉取值不在可选项里（模型名、采样器名、调度器名等）
      - 数值超出 min/max（步数、CFG、尺寸等）

    Args:
        graph: API 格式工作流。
        specs: {class_type: /object_info 中该类节点的原始描述}。

    Returns:
        问题描述列表（面向用户的中文）。
    """
    problems: list[str] = []
    for node_id, node in graph.items():
        class_type = node.get("class_type")
        info = specs.get(class_type) if isinstance(class_type, str) else None
        if not isinstance(info, dict):
            continue
        required = (info.get("input") or {}).get("required")
        if not isinstance(required, dict):
            continue
        for key, value in (node.get("inputs") or {}).items():
            if isinstance(value, list) or key not in required:
                continue
            spec_entry = required[key]
            options = _combo_options(spec_entry)
            if options is not None:
                # 自校验的路径型输入：跳过下拉校验，交给服务端
                if (class_type, key) in SELF_VALIDATED_PATH_INPUTS:
                    continue
                if value not in options:
                    sample = "、".join(str(o) for o in options[:6])
                    suffix = f" 等 {len(options)} 项" if len(options) > 6 else ""
                    problems.append(
                        f"节点 {node_id}（{class_type}）的 {key} 取值 {value!r} 不存在"
                        f"（可用项：{sample}{suffix}）"
                    )
                continue
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                low, high = _numeric_bounds(spec_entry)
                if low is not None and value < low:
                    problems.append(
                        f"节点 {node_id}（{class_type}）的 {key}={value} 小于最小值 {low}"
                    )
                elif high is not None and value > high:
                    problems.append(
                        f"节点 {node_id}（{class_type}）的 {key}={value} 大于最大值 {high}"
                    )
    return problems


def _order_folders(folders: list[str]) -> list[str]:
    """过滤并按重要性排序模型目录。

    Args:
        folders: 服务器通过 GET /models 报告的目录名。

    Returns:
        只保留白名单内的模型目录，主用途目录在前、其余在后。
    """
    relevant = [f for f in folders if f in MODEL_FOLDERS and f not in EXCLUDED_FOLDERS]
    primary = [f for f in PRIMARY_MODEL_FOLDERS if f in relevant]
    others = sorted(f for f in relevant if f not in primary)
    return primary + others


def _collect_string_lists(value, out: list[str], depth: int = 0) -> None:
    """从 object_info 的输入描述里递归收集所有字符串列表。

    同时兼容 V2 的 `[[名字...], {tooltip}]` 与 V3 的 combo 描述。
    只有非空字符串列表才被当作候选，并过滤掉 "STRING"/"INT" 这类类型占位符。

    Args:
        value: 待扫描的任意结构。
        out: 收集结果（原地追加）。
        depth: 递归深度，防止异常结构导致爆栈。
    """
    if depth > 6:
        return
    if isinstance(value, list):
        if value and all(isinstance(v, str) for v in value):
            out.extend(
                v for v in value if v and v not in ("STRING", "INT", "FLOAT", "BOOLEAN")
            )
            return
        for item in value:
            _collect_string_lists(item, out, depth + 1)
    elif isinstance(value, dict):
        for key in ("options", "choices", "ui_list", "values"):
            if key in value:
                _collect_string_lists(value[key], out, depth + 1)
        for key in ("required", "optional"):
            if key in value:
                _collect_string_lists(value[key], out, depth + 1)


class ComfyUI:
    """ComfyUI HTTP 客户端。"""

    def __init__(
        self,
        base_url: str,
        timeout: int = 180,
        *,
        poll_interval: float = 1.5,
        max_tasks_ahead: int = 10,
        logger=None,
    ):
        """初始化客户端。

        Args:
            base_url: ComfyUI 地址。
            timeout: 单次出图的等待上限（秒），不含队列补偿时间。
            poll_interval: 轮询间隔（秒）。
            max_tasks_ahead: 队列补偿时最多按多少个前方任务计算。
            logger: 可选日志器；提交失败时用它记录 ComfyUI 的原始响应，便于排查。
        """
        self.base_url = normalize_base_url(base_url)
        self.timeout = int(timeout or 180)
        self.poll_interval = float(poll_interval or 1.5)
        self.max_tasks_ahead = int(max_tasks_ahead or 10)
        self.client_id = uuid.uuid4().hex
        self._session: aiohttp.ClientSession | None = None
        self._model_cache: dict[str, list[str]] = {}
        self._model_cache_at: float = 0.0
        self.logger = logger
        self._nodes_cache: set[str] = set()
        self._nodes_cache_at: float = 0.0
        self._object_info: dict = {}

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    async def _get_session(self) -> aiohttp.ClientSession:
        """返回复用的 session，必要时创建。"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout)
            )
        return self._session

    async def close(self) -> None:
        """关闭底层连接，由插件 terminate() 调用。"""
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _request(self, method: str, path: str, **kwargs):
        """发起请求并返回解析后的 JSON，出错时抛 ComfyUIError。"""
        if not self.base_url:
            raise ComfyUIError("尚未配置 ComfyUI 地址，请在插件配置里填写")
        session = await self._get_session()
        url = f"{self.base_url}{path}"
        try:
            async with session.request(method, url, **kwargs) as resp:
                text = await resp.text()
                if resp.status == 404:
                    raise ComfyUIError(f"ComfyUI 不支持该接口（404）：{path}")
                if resp.status >= 400:
                    raise ComfyUIError(f"ComfyUI 返回 {resp.status}：{_shorten(text)}")
                if not text.strip():
                    return {}
                try:
                    return await resp.json(content_type=None)
                except Exception:
                    raise ComfyUIError(f"ComfyUI 返回了非 JSON 内容：{_shorten(text)}")
        except aiohttp.ClientError as e:
            raise ComfyUIError(f"无法连接 ComfyUI（{self.base_url}）：{e}") from e
        except asyncio.TimeoutError as e:
            raise ComfyUIError(f"连接 ComfyUI 超时（{self.base_url}）") from e

    # ------------------------------------------------------------------ #
    # 健康与能力探测
    # ------------------------------------------------------------------ #
    async def ping(self) -> dict:
        """探测服务器是否可用，返回 system_stats。

        Raises:
            ComfyUIError: 不可用或返回格式异常。
        """
        data = await self._request("GET", "/system_stats")
        if not isinstance(data, dict):
            raise ComfyUIError("ComfyUI 的 /system_stats 返回格式异常")
        return data

    async def node_classes(self, *, max_age: float = 300.0) -> set[str]:
        """返回服务器已安装的全部节点类名（带缓存）。

        `/object_info` 的响应可能很大，因此结果按 max_age 秒缓存，
        供模板能力探测复用。

        Args:
            max_age: 缓存有效期（秒）。

        Returns:
            类名集合；失败时返回空集合（探测失败不应阻断出图）。
        """
        now = time.time()
        if self._nodes_cache and now - self._nodes_cache_at < max_age:
            return self._nodes_cache
        try:
            data = await self._request("GET", "/object_info")
        except ComfyUIError:
            return set()
        result = set(data.keys()) if isinstance(data, dict) else set()
        if result:
            self._nodes_cache = result
            self._nodes_cache_at = now
            self._object_info = data
        return result

    async def object_info(self, *, max_age: float = 300.0) -> dict:
        """返回（带缓存的）/object_info 原始数据。

        用于在提交前复刻服务端的输入校验，从而给出精确报错。

        Args:
            max_age: 缓存有效期（秒）。

        Returns:
            /object_info 的原始字典；不可用时为空字典。
        """
        await self.node_classes(max_age=max_age)
        return self._object_info

    async def comfyui_version(self) -> str:
        """返回 ComfyUI 版本号，取不到时返回空串。"""
        try:
            stats = await self.ping()
        except ComfyUIError:
            return ""
        system = stats.get("system")
        if isinstance(system, dict):
            return str(system.get("comfyui_version") or "")
        return ""

    async def upload_image(
        self,
        image_path,
        *,
        subfolder: str = "astrbot",
        overwrite: bool = True,
    ) -> str:
        """把本地图片上传到 ComfyUI 的 input 目录，返回可填进 LoadImage 的引用。

        用子目录存放，避免污染用户的 input 根目录；`LoadImage` 接受
        `subfolder/name` 形式的引用（已实测）。

        Args:
            image_path: 本地图片路径。
            subfolder: input 下的子目录；空串表示根目录。
            overwrite: 同名文件是否覆盖。

        Returns:
            形如 `astrbot/xxx.png` 的引用（无子目录时为 `xxx.png`）。

        Raises:
            ComfyUIError: 读取或上传失败。
        """
        path = Path(image_path)
        try:
            raw = path.read_bytes()
        except OSError as e:
            raise ComfyUIError(f"读取待上传的图片失败：{e}") from e

        session = await self._get_session()
        form = aiohttp.FormData()
        form.add_field(
            "image",
            raw,
            filename=path.name,
            content_type=mimetypes.guess_type(path.name)[0] or "image/png",
        )
        form.add_field("type", "input")
        form.add_field("overwrite", "true" if overwrite else "false")
        if subfolder:
            form.add_field("subfolder", subfolder)

        try:
            async with session.post(f"{self.base_url}/upload/image", data=form) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    raise ComfyUIError(
                        f"上传图片到 ComfyUI 失败（HTTP {resp.status}）：{_shorten(text)}"
                    )
                data = json.loads(text) if text.strip() else {}
        except aiohttp.ClientError as e:
            raise ComfyUIError(f"无法连接 ComfyUI 上传图片：{e}") from e
        except json.JSONDecodeError as e:
            raise ComfyUIError(f"ComfyUI 上传接口返回了非 JSON 内容：{_shorten(text)}") from e

        name = str(data.get("name") or "")
        if not name:
            raise ComfyUIError(f"ComfyUI 未返回上传后的文件名：{_shorten(text)}")
        sub = str(data.get("subfolder") or "")
        return f"{sub}/{name}" if sub else name

    async def precheck(self, graph: dict) -> list[str]:
        """提交前用服务端自己的输入约束做本地校验。

        ComfyUI 在个别情况下会返回「Prompt outputs failed validation」却不带任何
        节点级原因（node_errors 与 details 都是空的），此时用户与开发者都无从下手。
        本地预检能把这些常见原因（下拉取值不在列表、数值越界）提前拦下并说清。

        Args:
            graph: 待提交的 API 格式工作流。

        Returns:
            问题描述列表；为空表示未发现问题（不代表服务端一定接受）。
        """
        info = await self.object_info()
        if not info:
            return []  # 拿不到约束就交给服务端判断，不要误拦
        specs: dict[str, dict] = {}
        for node in graph.values():
            class_type = node.get("class_type")
            if isinstance(class_type, str) and class_type in info:
                specs.setdefault(class_type, info[class_type])
        return validate_graph_locally(graph, specs)

    # ------------------------------------------------------------------ #
    # 模型发现
    # ------------------------------------------------------------------ #
    async def list_model_folders(self) -> list[str]:
        """列出服务器实际存在的模型文件夹。

        Returns:
            文件夹名列表；老版本不支持时返回内置候选。
        """
        try:
            data = await self._request("GET", "/models")
        except ComfyUIError:
            return list(DEFAULT_FOLDERS)
        if isinstance(data, list):
            return [str(x) for x in data if isinstance(x, str)]
        return list(DEFAULT_FOLDERS)

    async def list_models(self, folder: str) -> list[str]:
        """列出某个文件夹下的模型文件名（含子目录相对路径）。

        Args:
            folder: 文件夹名。

        Returns:
            文件名列表；失败返回空列表。
        """
        try:
            data = await self._request("GET", f"/models/{folder}")
        except ComfyUIError:
            return []
        if isinstance(data, list):
            return [str(x) for x in data if isinstance(x, str) and x.strip()]
        return []

    async def discover_models(self, *, max_age: float = 300.0) -> dict[str, list[str]]:
        """发现各文件夹下的模型清单。

        优先 /models/{folder}；若服务器不支持则回退到解析 /object_info。
        结果按 max_age 秒缓存，避免每次出图都扫一遍。

        Args:
            max_age: 缓存有效期（秒）。

        Returns:
            {文件夹名: [文件名...]}，只包含非空文件夹。
        """
        now = time.time()
        if self._model_cache and now - self._model_cache_at < max_age:
            return self._model_cache

        result: dict[str, list[str]] = {}
        ordered = _order_folders(await self.list_model_folders())
        for folder in ordered:
            files = await self.list_models(folder)
            if files:
                result[folder] = sorted(files)

        if not result:
            result = await self._discover_models_via_object_info()

        self._model_cache = result
        self._model_cache_at = now
        return result

    async def _discover_models_via_object_info(self) -> dict[str, list[str]]:
        """老版本 ComfyUI 的模型发现回退：解析 /object_info。

        Returns:
            {文件夹名: [文件名...]}。
        """
        try:
            data = await self._request("GET", "/object_info")
        except ComfyUIError:
            return {}
        if not isinstance(data, dict):
            return {}
        result: dict[str, list[str]] = {}
        for folder, (class_type, input_key) in FOLDER_PROBE.items():
            node = data.get(class_type)
            if not isinstance(node, dict):
                continue
            spec = (node.get("input") or {}).get("required", {}).get(input_key)
            if spec is None:
                continue
            names: list[str] = []
            _collect_string_lists(spec, names)
            unique = sorted({n for n in names if n})
            if unique:
                result[folder] = unique
        return result

    def invalidate_model_cache(self) -> None:
        """让模型清单与节点能力缓存失效，供 /刷新模型 使用。"""
        self._model_cache = {}
        self._model_cache_at = 0.0
        self._nodes_cache = set()
        self._nodes_cache_at = 0.0
        self._object_info = {}

    # ------------------------------------------------------------------ #
    # 队列
    # ------------------------------------------------------------------ #
    @staticmethod
    def _queue_item_id(item) -> tuple[str, str | None]:
        """从 /queue 的一项里取出 (prompt_id, client_id)。"""
        if isinstance(item, (list, tuple)):
            if len(item) > 1 and isinstance(item[1], str):
                extra = item[3] if len(item) > 3 else None
                client = extra.get("client_id") if isinstance(extra, dict) else None
                return item[1], client
            return "", None
        if isinstance(item, dict):
            extra = item.get("extra_data")
            extra = extra if isinstance(extra, dict) else {}
            return str(item.get("prompt_id", "")), extra.get("client_id")
        return "", None

    async def queue_status(self, *, strict: bool = False) -> QueueStatus:
        """读取队列状态，并标出本插件任务的排位。

        Args:
            strict: 读不到队列时是否抛错。默认 False —— 出图等待路径上「读不到队列」
                只该降级（继续等历史），不该让整次出图失败；多后端探测需要 True，
                否则一台挂掉的机器会以「队列 0」的假象被派上任务。

        Returns:
            QueueStatus。

        Raises:
            ComfyUIError: strict=True 且读取失败。
        """
        try:
            data = await self._request("GET", "/queue")
        except ComfyUIError:
            if strict:
                raise
            return QueueStatus()
        if not isinstance(data, dict):
            if strict:
                raise ComfyUIError("ComfyUI 的 /queue 返回了非对象内容")
            return QueueStatus()

        running = data.get("queue_running") or []
        pending = data.get("queue_pending") or []
        status = QueueStatus(total_running=len(running), total_pending=len(pending))
        position = 1
        for item in running:
            pid, client = self._queue_item_id(item)
            if client and client == self.client_id:
                status.own_running += 1
                status.own_positions.setdefault(pid, 1)
            position += 1
        for item in pending:
            pid, client = self._queue_item_id(item)
            if client and client == self.client_id:
                status.own_pending += 1
                status.own_positions.setdefault(pid, position)
            position += 1
        return status

    # ------------------------------------------------------------------ #
    # 提交与等待
    # ------------------------------------------------------------------ #
    async def submit(self, graph: dict, *, extra_data: dict | None = None) -> str:
        """提交工作流。

        Args:
            graph: API 格式工作流。
            extra_data: 附加到 extra_data 的字段。

        Returns:
            prompt_id。

        Raises:
            ComfyUIError: 提交失败，message 已是可读中文。
        """
        payload = {
            "prompt": graph,
            "client_id": self.client_id,
            "extra_data": {"client_id": self.client_id, **(extra_data or {})},
        }
        session = await self._get_session()
        try:
            async with session.post(f"{self.base_url}/prompt", json=payload) as resp:
                text = await resp.text()
                data = {}
                if text.strip():
                    try:
                        data = await resp.json(content_type=None)
                    except Exception:
                        data = {}
                if resp.status >= 400 or "prompt_id" not in data:
                    # 把原始响应写进日志：ComfyUI 的真正原因常在被我们折叠掉的结构里
                    if self.logger is not None:
                        self.logger.warning(
                            "ComfyUI 拒绝了这次出图（HTTP %s），原始响应：%s",
                            resp.status,
                            _shorten(text, 1500),
                        )
                    exc = ComfyUIError(format_submit_error(data, resp.status, text))
                    exc.raw_response = data
                    raise exc
                return str(data["prompt_id"])
        except aiohttp.ClientError as e:
            raise ComfyUIError(f"无法连接 ComfyUI：{e}") from e

    async def interrupt(self, prompt_id: str = "") -> bool:
        """取消任务。

        Args:
            prompt_id: 目标任务；为空则取消当前正在执行的任务。

        Returns:
            是否成功发出取消请求。
        """
        body = {"prompt_id": prompt_id} if prompt_id else {}
        try:
            await self._request("POST", "/interrupt", json=body)
            return True
        except ComfyUIError:
            return False

    async def cancel_prompt(self, prompt_id: str) -> str:
        """尽量取消一个任务，并说明实际做了什么。

        ComfyUI 的两条路要分开走：
        - 任务**正在执行**：`POST /interrupt`（`/queue` 的 delete 停不掉正在跑的那个）；
        - 任务**还在排队**：`POST /queue {"delete": [prompt_id]}`。

        上游没有「一步取消」的接口，所以这里先读 `/queue` 判断它在哪一段，
        再发对应的请求；两段都试过仍没命中时返回 `not_found`，由调用方决定怎么告知用户。

        Args:
            prompt_id: 目标任务 id。

        Returns:
            `running` / `pending` / `not_found`。
        """
        try:
            data = await self._request("GET", "/queue")
        except ComfyUIError:
            data = {}
        running = data.get("queue_running") or [] if isinstance(data, dict) else []
        pending = data.get("queue_pending") or [] if isinstance(data, dict) else []
        if any(self._queue_item_id(item)[0] == prompt_id for item in running):
            ok = await self.interrupt(prompt_id)
            return "running" if ok else "not_found"
        if any(self._queue_item_id(item)[0] == prompt_id for item in pending):
            try:
                await self._request("POST", "/queue", json={"delete": [prompt_id]})
                return "pending"
            except ComfyUIError:
                return "not_found"
        return "not_found"

    def _ws_url(self) -> str:
        """把 base_url 换成 WebSocket 地址（进度推送用）。"""
        base = self.base_url
        if base.startswith("https://"):
            base = "wss://" + base[len("https://"):]
        elif base.startswith("http://"):
            base = "ws://" + base[len("http://"):]
        return f"{base}/ws?clientId={self.client_id}"

    async def _watch_progress(self, prompt_id: str, on_progress) -> None:
        """监听 ComfyUI 的 WebSocket 进度事件，并回调给上层。

        ComfyUI 通过 `/ws` 推送 `progress`（`value`/`max` 就是「第 n 步 / 共 m 步」）
        与 `executing`。这条通道**只是锦上添花**：连不上（老版本、反向代理没转发
        WebSocket、被防火墙拦）就安静退出，等待流程仍由轮询兜底，不会因此出图失败。

        Args:
            prompt_id: 目标任务 id，用于过滤别人的任务。
            on_progress: 回调，参数形如
                `{"value": 3, "max": 28, "percent": 11, "node": "5", "prompt_id": ...}`。
        """
        session = await self._get_session()
        try:
            ws = await session.ws_connect(self._ws_url(), heartbeat=30)
        except Exception as e:
            if self.logger is not None:
                self.logger.debug("ComfyUI 进度推送不可用（继续用轮询）：%s", e)
            return
        try:
            while True:
                msg = await ws.receive()
                data = getattr(msg, "data", None)
                if not isinstance(data, str):
                    return  # CLOSED / ERROR / 二进制：收工，交给轮询
                try:
                    payload = json.loads(data)
                except ValueError:
                    continue
                if not isinstance(payload, dict):
                    continue
                kind = payload.get("type")
                body = payload.get("data") if isinstance(payload.get("data"), dict) else {}
                if kind == "progress":
                    if body.get("prompt_id") not in (None, "", prompt_id):
                        continue
                    try:
                        value = int(body.get("value", 0))
                        maximum = int(body.get("max", 0))
                    except (TypeError, ValueError):
                        continue
                    if maximum <= 0:
                        continue
                    await _safe_callback(
                        on_progress,
                        {
                            "value": value,
                            "max": maximum,
                            "percent": min(100, int(value * 100 / maximum)),
                            "node": str(body.get("node") or ""),
                            "prompt_id": prompt_id,
                        },
                    )
                elif kind == "executing":
                    if body.get("prompt_id") not in (None, "", prompt_id):
                        continue
                    if body.get("node") in (None, ""):
                        return  # node 为空 = 这个任务执行结束
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if self.logger is not None:
                self.logger.debug("读取 ComfyUI 进度时出错（不影响出图）：%s", e)
        finally:
            try:
                await ws.close()
            except Exception:
                pass

    async def wait_for_images(
        self,
        prompt_id: str,
        output_dir: Path,
        *,
        on_queued=None,
        on_progress=None,
        cancel_event: asyncio.Event | None = None,
        user_cancel_event: asyncio.Event | None = None,
    ) -> list[Path]:
        """等待任务完成并下载产物（图片或视频）。

        超时 = 配置的 timeout + 队列补偿（前方任务数 * 60s，最多 max_tasks_ahead 个）。

        Args:
            prompt_id: 任务 id。
            output_dir: 图片保存目录。
            on_queued: 可选的 async 回调，参数为 QueueStatus，用于提示排队位置。
                排队位置**变化时**会再回调一次（同一位置不重复刷屏）。
            on_progress: 可选的 async 回调，收到 WebSocket 进度事件时调用（第 n / 总步数）。
            cancel_event: 可选的外部取消信号（插件卸载时置位）。
            user_cancel_event: 可选的用户取消信号（`/取消` 时置位），与上面区分开，
                以便给出不同的提示。

        Returns:
            下载到本地的文件路径列表（图片与视频混在一起，按扩展名区分）。

        Raises:
            ComfyUIError: 超时、执行失败、被取消或未产出任何产物。
        """
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        deadline = time.time() + self.timeout
        compensated = False
        last_queue_notice = 0.0
        last_position: int | None = None
        last_error = ""
        unreachable_since: float | None = None
        unreachable_reason = ""
        missing_polls = 0
        missing_since = 0.0
        watcher: asyncio.Task | None = None
        if on_progress is not None:
            # 进度推送与轮询并行：WS 断了不影响等待，轮询依然是唯一的事实来源
            watcher = asyncio.create_task(self._watch_progress(prompt_id, on_progress))

        try:
            while True:
                if user_cancel_event is not None and user_cancel_event.is_set():
                    await self.cancel_prompt(prompt_id)
                    raise ComfyUIError("本次出图已被 /取消 取消")
                if cancel_event is not None and cancel_event.is_set():
                    await self.interrupt(prompt_id)
                    raise ComfyUIError("任务已取消（插件正在卸载或重载）")
                if time.time() > deadline:
                    await self.interrupt(prompt_id)
                    raise ComfyUIError(
                        f"出图超时（{self.timeout} 秒）"
                        + (f"；最后状态：{last_error}" if last_error else "")
                    )

                await asyncio.sleep(self.poll_interval)

                # 排队位置：首次必报，之后位置有变化再报；位置不变则最多 60 秒提一次
                status = await self.queue_status()
                mine = status.own_positions.get(prompt_id)
                if mine is not None and status.own_running == 0 and status.own_pending > 0:
                    if not compensated:
                        # 排队补偿只在第一次看到自己在排队时算一次，避免反复加时间
                        ahead = min(status.tasks_ahead, self.max_tasks_ahead)
                        if ahead > 0:
                            deadline += max(120, ahead * 60)
                        compensated = True
                    if mine != last_position or time.time() - last_queue_notice > 60:
                        last_position = mine
                        last_queue_notice = time.time()
                        await _safe_callback(on_queued, status)

                try:
                    history = await self._request("GET", f"/history/{prompt_id}")
                    unreachable_since = None
                except ComfyUIError as exc:
                    # 连不上：ComfyUI 可能被关掉或重启了。连续一段时间连不上就别再等，
                    # 否则用户要白等满 timeout（实测整机重启时白等了 30 分钟）。
                    if unreachable_since is None:
                        unreachable_since = time.time()
                        unreachable_reason = str(exc)
                    elif time.time() - unreachable_since > UNREACHABLE_GRACE_SECONDS:
                        raise ComfyUIError(
                            "连不上 ComfyUI 了（可能被关闭或重启过），本次已放弃等待："
                            f"{unreachable_reason}"
                        ) from exc
                    last_error = "连不上 ComfyUI"
                    continue
                if not isinstance(history, dict) or prompt_id not in history:
                    if mine is not None:
                        # 还在队列里（排队中/执行中）：历史里当然还没有它，这是**正常**状态。
                        # 真机踩过这个坑：不加这个判断会把正在跑的任务误判成「丢了」，
                        # 22 秒就放弃等待，而任务其实还在正常采样。
                        missing_polls = 0
                        last_error = "等待 ComfyUI 执行"
                        continue
                    # 队列里没有、历史里也没有 → 才可能是任务真的丢了（服务重启/队列被清空）
                    missing_polls += 1
                    if missing_polls == 1:
                        missing_since = time.time()
                    if (missing_polls >= MISSING_PROMPT_POLLS
                            and time.time() - missing_since > MISSING_PROMPT_SECONDS):
                        raise ComfyUIError(
                            "任务已不在 ComfyUI 的队列/历史里（服务可能重启过或队列被清空），"
                            "本次已放弃等待，请重新发一次。"
                        )
                    last_error = "等待 ComfyUI 执行"
                    continue
                missing_polls = 0

                entry = history[prompt_id]
                if not isinstance(entry, dict):
                    continue
                status_obj = entry.get("status") or {}
                status_str = status_obj.get("status_str")
                if status_str == "error":
                    raise ComfyUIError(_describe_history_error(entry))
                if status_str == "interrupted":
                    raise ComfyUIError("任务被中断（ComfyUI 端取消或被打断）")

                images = _collect_output_images(entry.get("outputs") or {})
                if images:
                    paths = await self._download_images(images, output_dir)
                    if paths:
                        return paths
                    last_error = "产物下载失败"
                    continue
                # 尚未产出图片：可能仍在执行，也可能该工作流没有图片输出节点。
                # 只有执行已明确结束时才判定失败，否则继续等待，避免误报。
                if status_obj.get("completed"):
                    raise ComfyUIError(
                        "任务已结束但没有任何产物。请确认工作流包含 SaveImage / SaveWEBM / "
                        "SaveVideo 这类输出节点。"
                    )
                last_error = "仍在执行中"
        finally:
            if watcher is not None:
                watcher.cancel()
                try:
                    await watcher
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    if self.logger is not None:
                        self.logger.debug("收尾进度监听任务时出错：%s", e)

    async def _download_images(self, images: list[dict], output_dir: Path) -> list[Path]:
        """下载图片到本地。

        Args:
            images: /history 输出里的图片描述列表。
            output_dir: 保存目录。

        Returns:
            本地路径列表（失败条目会被跳过）。
        """
        session = await self._get_session()
        saved: list[Path] = []
        stamp = int(time.time() * 1000)
        for index, img in enumerate(images):
            filename = str(img.get("filename") or "")
            if not filename:
                continue
            params = {
                "filename": filename,
                "subfolder": img.get("subfolder", "") or "",
                "type": img.get("type", "output") or "output",
            }
            try:
                async with session.get(f"{self.base_url}/view", params=params) as resp:
                    if resp.status >= 400:
                        continue
                    content = await resp.read()
            except (aiohttp.ClientError, asyncio.TimeoutError):
                continue
            if not content:
                continue
            local = output_dir / f"{stamp}_{index}_{Path(filename).name}"
            try:
                local.write_bytes(content)
            except OSError:
                continue
            saved.append(local)
        return saved


async def _safe_callback(callback, *args) -> None:
    """调用可选回调并吞掉其异常，避免提示失败影响出图。"""
    if callback is None:
        return
    try:
        await callback(*args)
    except Exception:
        pass


# 视频产物的扩展名（ComfyUI 的 PreviewVideo/SaveVideo/SaveWEBM 都落在 outputs 的
# images 字段里，靠扩展名区分是图片还是视频）
# 等待出图时的「服务消失」判定阈值（真机实测：整机重启后任务会凭空消失，
# 旧实现只能一直轮询到 timeout，用户白等半小时）
UNREACHABLE_GRACE_SECONDS = 60.0   # 连续多久连不上就放弃
MISSING_PROMPT_POLLS = 6           # 连续多少轮查不到任务
MISSING_PROMPT_SECONDS = 20.0      # 且至少过了这么多秒

VIDEO_SUFFIXES = (".mp4", ".webm", ".mkv", ".mov", ".m4v", ".avi")
# 动图：当图片发（很多平台能直接显示）
ANIMATED_SUFFIXES = (".gif", ".webp")


def media_kind(filename: str) -> str:
    """按扩展名判断产物是视频还是图片。

    Args:
        filename: 产物文件名（可含子目录）。

    Returns:
        `"video"` 或 `"image"`。
    """
    name = str(filename or "").lower()
    if name.endswith(VIDEO_SUFFIXES):
        return "video"
    return "image"


def _collect_output_images(outputs: dict) -> list[dict]:
    """从 /history 的 outputs 里取出真正的产物（图片**和**视频）。

    ComfyUI 的约定：
    - `PreviewImage` / `SaveImage` → outputs 的 `images` 字段；
    - `PreviewVideo` / `SaveVideo` / `SaveWEBM` → **同样**落在 `images` 字段，靠扩展名区分；
    - 老版本的动图节点会写到 `gifs` 字段。

    只保留 `type == output`（跳过 temp 预览），并给每条打上 `media` 标记。

    Args:
        outputs: /history 条目的 outputs 字段。

    Returns:
        产物描述列表（含 `media` 字段：image / video）。
    """
    collected: list[dict] = []
    if not isinstance(outputs, dict):
        return collected
    for node_output in outputs.values():
        if not isinstance(node_output, dict):
            continue
        for key in ("images", "gifs", "videos"):
            for item in node_output.get(key) or []:
                if not isinstance(item, dict):
                    continue
                if item.get("type", "output") != "output":
                    continue
                if not item.get("filename"):
                    continue
                entry = dict(item)
                entry["media"] = media_kind(entry["filename"])
                collected.append(entry)
    return collected


def _describe_history_error(entry: dict) -> str:
    """把 /history 里的执行错误整理成一句可读中文。"""
    for message in reversed(entry.get("status", {}).get("messages") or []):
        if not (isinstance(message, list) and len(message) >= 2):
            continue
        kind, payload = message[0], message[1]
        if kind not in ("execution_error", "execution_interrupted"):
            continue
        if not isinstance(payload, dict):
            continue
        node_type = payload.get("node_type") or payload.get("node_id") or "未知节点"
        if kind == "execution_interrupted":
            return f"任务被中断（节点 {node_type}）"
        detail = payload.get("exception_message") or payload.get("exception_type") or ""
        return f"出图执行失败：节点 {node_type} 报错 {detail}".strip()
    return "ComfyUI 执行出错（未提供详细信息）"


def format_submit_error(data: dict, status: int, raw: str = "") -> str:
    """把 /prompt 的失败响应翻译成可读中文。

    刻意做得「话多」：ComfyUI 拒绝一次出图时，真正的线索在
    `error.type` / `error.message` / `error.details` 和 `node_errors` 里，
    只回一句「Prompt outputs failed validation」等于什么也没说。

    Args:
        data: 响应 JSON。
        status: HTTP 状态码。
        raw: 原始响应文本，用于兜底。

    Returns:
        面向用户的中文错误信息（已控制长度）。
    """
    if not isinstance(data, dict):
        return f"提交失败（HTTP {status}）：{_shorten(raw)}"

    lines: list[str] = []
    error = data.get("error")
    error = error if isinstance(error, dict) else {}
    error_type = str(error.get("type") or "")
    message = str(error.get("message") or "").strip()
    details = str(error.get("details") or "").strip()

    if error_type == "prompt_no_outputs":
        return "提交失败：工作流里没有输出节点，请确认模板保留了 SaveImage 节点"

    node_errors = data.get("node_errors")
    has_node_errors = isinstance(node_errors, dict) and bool(node_errors)
    if message:
        headline = message
    elif error_type:
        headline = error_type
    elif has_node_errors:
        # 只有节点级原因、没有顶层描述时，别输出无意义的「HTTP 400」
        headline = "工作流校验未通过"
    else:
        headline = f"HTTP {status}"
    lines.append(f"提交失败：{headline}")

    # error.details 常含逐条原因（多个输出各自失败时的汇总），信息量最大
    if details and details != message:
        for line in details.splitlines():
            line = line.strip()
            if line:
                lines.append(f"　· {line}")

    # node_errors：逐节点列出「全部」原因（不是只列第一条）
    if has_node_errors:
        shown = 0
        for node_id, payload in node_errors.items():
            if shown >= 4:
                lines.append(f"　… 另有 {len(node_errors) - shown} 个节点报错")
                break
            lines.append(f"　· {_describe_node_error(node_id, payload)}")
            shown += 1
    elif error_type == "prompt_outputs_failed_validation":
        # 走到这里说明 ComfyUI 没给出可解析的节点级原因，给出下一步怎么查
        lines.append(
            "　· ComfyUI 未返回节点级原因。请查看 ComfyUI 控制台里 "
            "“Failed to validate prompt for output” 附近的日志，那里有完整原因。"
        )

    if error_type == "missing_node_type":
        lines.append("　· 提示：该自定义节点没装，或工作流里有残留的孤儿节点（可换用自定义模板）")
    elif error_type == "value_not_in_list":
        lines.append("　· 提示：某个下拉参数取值不在服务器的可选项里，通常是模型名或采样器名不匹配")
    elif error_type == "dependency_cycle":
        lines.append("　· 提示：工作流里存在环路，请检查模板连线")

    # exception_during_validation 的 traceback 在 extra_info 里，指向 ComfyUI 控制台
    extra = error.get("extra_info")
    if isinstance(extra, dict) and extra.get("exception_type"):
        lines.append(f"　· 校验期异常：{extra['exception_type']}（详情见 ComfyUI 控制台）")

    text = "\n".join(lines)
    return text if len(text) <= 900 else text[:900] + "…"


def _describe_node_error(node_id: str, payload) -> str:
    """描述单个节点的校验错误（列出该节点的全部原因）。"""
    if not isinstance(payload, dict):
        return f"节点 {node_id} 校验失败"
    class_type = payload.get("class_type") or ""
    prefix = f"节点 {node_id}"
    if class_type:
        prefix += f"（{class_type}）"

    reasons = payload.get("errors") or []
    described: list[str] = []
    for err in reasons:
        if not isinstance(err, dict):
            continue
        described.append(_describe_reason(err, payload))
    if not described:
        message = payload.get("message")
        described.append(str(message) if message else "校验失败")
    # 同一节点多条原因时合并，避免刷屏
    return f"{prefix}：" + "；".join(described[:3])


def _describe_reason(err: dict, payload: dict) -> str:
    """把单条 node_errors 原因翻译成中文。"""
    kind = err.get("type") or ""
    info = err.get("extra_info")
    info = info if isinstance(info, dict) else {}
    message = str(err.get("message") or "").strip()
    details = str(err.get("details") or "").strip()

    if kind == "value_not_in_list":
        field_name = info.get("input_name") or "参数"
        received = info.get("received_value")
        options = info.get("list_content") or []
        sample = "、".join(str(o) for o in options[:5])
        text = f"{field_name} 取值 {received!r} 不存在"
        if sample:
            more = "" if len(options) <= 5 else f" 等 {len(options)} 项"
            text += f"（可用项：{sample}{more}）"
        return text
    if kind == "missing_node_type":
        return f"缺少自定义节点 {info.get('node_type') or payload.get('class_type') or '?'}"
    if kind == "value_smaller_than_min":
        return f"{info.get('input_name')} 小于最小值 {info.get('min')}"
    if kind == "value_bigger_than_max":
        return f"{info.get('input_name')} 大于最大值 {info.get('max')}"
    if kind == "required_input_missing":
        return f"缺少必填输入 {info.get('input_name') or details}"
    if kind == "return_type_mismatch":
        return f"连线类型不匹配：{details or message}"
    if kind == "exception_during_validation":
        return f"校验期异常：{details or message}"
    if message and details and details != message:
        return f"{message}：{details}"
    return message or details or kind or "校验失败"


def _shorten(text: str, limit: int = 200) -> str:
    """截断过长文本，避免把整段 HTML 错误页塞进聊天。"""
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"
