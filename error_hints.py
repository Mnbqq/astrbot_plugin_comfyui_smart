"""错误翻译，以及「按需下载权重的节点」的能力探测。

真机踩过的坑（v0.26.0 补）
==========================

DepthAnything 这类**预处理器的权重不在 models/ 目录下**：它们由自定义节点
（comfyui_controlnet_aux）在第一次用到时从 HuggingFace 下载。于是会出现一种
非常反直觉的失败：

    /体检 全绿（节点在、底模在、ControlNet 权重也在）
    真去 --control depth 出图，跑到一半报 LocalEntryNotFoundError

`diagnostics.check_requirements()` 那种「拿模板里写死的权重名去 /object_info 的
下拉列表里核对」的做法**永远发现不了它** —— 下拉列表是节点源码里写死的候选名，
不代表服务器上真有这个文件。更坑的是 DepthAnythingV2 的默认值是 `vitl`（约 1.3G），
而多数人只下过 `vits`（约 95M）：**用默认值就炸，用 vits 就没事**。

这里给出两条互补的路：

- `build_probe_graph()` / `find_probe_targets()`：用一张 64x64 空白图把可疑节点
  单独跑一遍。「跑得通」才算数，跑到一半才报错的坑被提前到提交前。
- `explain()`：把这类原始报错翻译成中文，并给出三条可执行的出路。

注意探测结果**不能一票否决**：探测本身也可能因为别的原因失败（比如
EmptyImage 节点被裁掉了）。因此调用方必须先问 `is_missing_weight_error()`，
只有确认是「权重/联网」这一类问题才降级，否则视为探测无结论、照常出图。
"""
from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------- #
# 按需下载权重的节点
# ---------------------------------------------------------------------- #
# 这些节点的权重不在 models/ 下，没法用模型清单核对，只能真跑一次才知道。
# 名单不需要穷尽：漏掉的节点只是少了探测，不影响出图。
PROBE_NODES: frozenset[str] = frozenset({
    # 深度（--control depth 走的就是这一类）
    "DepthAnythingV2Preprocessor",
    "DepthAnythingPreprocessor",
    "Zoe-DepthMapPreprocessor",
    "Zoe_DepthAnythingPreprocessor",
    "MiDaS-DepthMapPreprocessor",
    "LeReS-DepthMapPreprocessor",
    "Metric3D-DepthMapPreprocessor",
    "MeshGraphormer-DepthMapPreprocessor",
    # 其它常见的 controlnet_aux 预处理器（同样按需下载）
    "CannyEdgePreprocessor",
    "LineArtPreprocessor",
    "AnimeLineArtPreprocessor",
    "Manga2Anime_LineArt_Preprocessor",
    "HEDPreprocessor",
    "PidiNetPreprocessor",
    "Scribble_XDoG_Preprocessor",
    "OpenposePreprocessor",
    "DWPreprocessor",
    "AnimalPosePreprocessor",
    "MediaPipe-FaceMeshPreprocessor",
    "SAMPreprocessor",
    "TEEDPreprocessor",
})

# 探测图的边长。只要够触发「加载权重」这一步就行，越小越快。
PROBE_SIZE = 64


def find_probe_targets(graph: dict) -> list[dict]:
    """挑出工作流里需要探测的节点。

    Args:
        graph: API 格式工作流。

    Returns:
        [{"node_id", "class_type", "inputs", "weight"}]；inputs 已摘掉 image
        （探测时用空白图代替），weight 是用于缓存与展示的权重名。
    """
    targets: list[dict] = []
    for node_id, node in (graph or {}).items():
        if not isinstance(node, dict):
            continue
        class_type = node.get("class_type")
        if class_type not in PROBE_NODES:
            continue
        inputs = {
            key: value
            for key, value in (node.get("inputs") or {}).items()
            if key != "image" and not isinstance(value, list)   # 摘掉连线，只留常量
        }
        targets.append({
            "node_id": node_id,
            "class_type": class_type,
            "inputs": inputs,
            "weight": str(inputs.get("ckpt_name") or inputs.get("model_name") or ""),
        })
    return targets


def build_probe_graph(target: dict, size: int = PROBE_SIZE) -> dict:
    """把一个待探测节点包成「空白图 → 节点 → 保存」的最小工作流。

    用 EmptyImage 而不是 LoadImage：探测发生在参考图上传之前，而且这样不占上传配额。
    """
    inputs = dict(target.get("inputs") or {})
    inputs["image"] = ["1", 0]
    return {
        "1": {
            "class_type": "EmptyImage",
            "inputs": {"width": size, "height": size, "batch_size": 1, "color": 0},
        },
        "2": {"class_type": target["class_type"], "inputs": inputs},
        "3": {
            "class_type": "SaveImage",
            "inputs": {"images": ["2", 0], "filename_prefix": "astrbot_probe"},
        },
    }


def probe_cache_key(target: dict, backend: str = "") -> tuple:
    """探测结果的缓存键：换后端 / 换节点 / 换权重都要重新探测。"""
    return (backend, target.get("class_type"), target.get("weight"))


# ---------------------------------------------------------------------- #
# 报错翻译
# ---------------------------------------------------------------------- #
# HuggingFace 拉取失败的几种写法（huggingface_hub 各版本措辞不同）。
# 注意：真实报错里**未必出现 huggingface.co** —— `_describe_history_error` 只拿到
# exception_message 时，常见的是「trying to locate the file on the Hub ... local cache」
# 这一句。所以不能拿域名当必要条件（真机踩过：只按域名判会漏掉，降级就不触发）。
_HF_MARKERS: tuple[str, ...] = (
    "localentrynotfounderror",
    "cannot find the requested files in the local cache",
    "trying to locate the file on the hub",
    "couldn't connect to 'https://huggingface.co'",
    "we couldn't connect to 'https://huggingface.co'",
    "offline mode is enabled",
)


def is_missing_weight_error(text: str) -> bool:
    """这条报错是不是「权重没下载 / 服务器连不上 HuggingFace」。

    单独抽出来是因为**降级决策必须用严格判据**：只有确认是这一类问题，
    才允许把 ControlNet 换成图生图；探测因其它原因失败时不能擅自改用途。
    """
    low = (text or "").lower()
    return any(marker in low for marker in _HF_MARKERS)


# 报错特征 -> 提示。按顺序匹配，命中即返回。
_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        ("探测超时",),
        "　· 节点已经开始执行却迟迟不返回，最常见的还是**卡在下载权重上**："
        "连不上 HuggingFace 时\n"
        "　　huggingface_hub 会反复退避重试（真机实测单发一次约 42 秒，连着发多个最久见过 3.5 分钟）。\n"
        "　　也可能是显存不足在换页，或队列前面压着超大任务。",
    ),
    (
        ("localentrynotfounderror", "local cache", "huggingface",
         "locate the file on the hub"),
        "　· 这多半是「辅助权重没下载，而 ComfyUI 那台机器又连不上 HuggingFace」。\n"
        "　　出问题的通常不是底模，而是预处理节点自带的权重（例如 DepthAnything 的\n"
        "　　depth_anything_v2_*.pth）—— 它们在 models/ 里看不到，/体检 也查不出来。\n"
        "　　注意 DepthAnythingV2 的默认档位是 vitl（约 1.3G），多数机器只装了 vits（约 95M）。\n"
        "　　怎么办（任选其一）：\n"
        "　　1. 在 ComfyUI 那台机器上先手动触发一次让它下载，或把权重放进自定义节点的\n"
        "　　　 ckpts 目录后重启 ComfyUI（断网机器只能这样）；\n"
        "　　2. 换成已装档位/别的预处理方式；\n"
        "　　3. 直接去掉 --control，改用图生图保构图：/画图 <描述> --denoise 0.4（0.4 保色调，\n"
        "　　　 0.55 开始换画风，0.7 以上接近重画）。",
    ),
    (
        ("controlnet for sdxl on sd1", "y is none"),
        "　· SDXL 的 ControlNet 不能配 SD1.5 底模。请换 SDXL 系底模（如 animagine-xl），"
        "或改用不挂 ControlNet 的模板。",
    ),
    (
        ("out of memory", "allocation on device", "cuda error", "os error 1455"),
        "　· 显存/内存不够。可依次尝试：降低分辨率（--size 1024x1024 或更小）、"
        "关掉 --hires、换更小的底模、关掉其它占用显存的程序。",
    ),
    (
        ("no module named", "importerror", "cannot import"),
        "　· ComfyUI 缺 Python 依赖或自定义节点没装好。请在 ComfyUI 那台机器上看控制台，"
        "按提示补装（多数是缺 requirements.txt 里的包）。",
    ),
    # 注：`prompt_outputs_failed_validation`（ComfyUI 没给节点级原因）已由
    # comfyui_api.format_submit_error 自己说清，这里不再重复提示。
)


def explain(text: str) -> str:
    """把 ComfyUI 的原始报错翻译成中文提示。

    Args:
        text: 原始报错（history 的 exception_message，或 /prompt 的响应）。

    Returns:
        可直接拼到错误信息后面的提示块；没有匹配到就返回空串（不要硬凑）。
    """
    low = (text or "").lower()
    if not low:
        return ""
    for markers, hint in _HINTS:
        if any(marker in low for marker in markers):
            return hint
    return ""


def describe_probe_failure(node_type: str, weight: str, error: str) -> str:
    """探测器失败时，给用户一句「为什么没用 ControlNet」的解释。"""
    name = weight or "内置权重"
    return (
        f"ControlNet 深度预处理（{node_type}，权重 {name}）在这台 ComfyUI 上跑不起来，"
        f"已自动改用图生图保构图。\n"
        f"　· 原因：{error}\n"
        f"　· 修好后可重试 --control depth；想手动确认请用 /体检 --probe。"
    )


def attach_hint(message: str) -> str:
    """给一条错误信息补上提示块（幂等：已经有提示就不重复加）。"""
    hint = explain(message)
    if not hint or hint in message:
        return message
    return f"{message}\n{hint}"


def probe_summary(results: list[dict]) -> str:
    """把 /体检 --probe 的探测结果整理成一段中文。

    三种状态各有各的记号：✅ 可用、❌ 跑不起来、⚠️ 没结论（任务一直排在别人后面）。
    """
    if not results:
        return "没有需要探测的节点（当前模板不含按需下载权重的预处理器）。"
    lines: list[str] = []
    for item in results:
        state = _state_of(item)
        weight = item.get("weight") or "内置权重"
        line = f"{_STATE_MARK.get(state, '❔')} {item.get('class_type')}（{weight}）"
        if state != PROBE_STATE_OK and item.get("error"):
            line += f"：{_first_line(item['error'])}"
        lines.append(line)
    failed = [x for x in results if _state_of(x) == PROBE_STATE_FAILED]
    if failed:
        lines.append("")
        lines.append(
            explain(str(failed[0].get("error") or ""))
            or "　· 建议在 ComfyUI 控制台确认原因。"
        )
    return "\n".join(lines)


# 探测结论（与 comfyui_api.PROBE_* 对应）。这里再定义一份是为了让本模块
# 不反向依赖 comfyui_api（error_hints 要保持零依赖，方便被任何一侧引用）。
PROBE_STATE_OK = "ok"
PROBE_STATE_FAILED = "failed"
PROBE_STATE_UNKNOWN = "unknown"
_STATE_MARK = {PROBE_STATE_OK: "✅", PROBE_STATE_FAILED: "❌", PROBE_STATE_UNKNOWN: "⚠️"}


def _state_of(item: dict) -> str:
    """取探测结论；兼容只有布尔 ok 的旧结构。"""
    state = str(item.get("state") or "")
    if state in _STATE_MARK:
        return state
    return PROBE_STATE_OK if item.get("ok") else PROBE_STATE_FAILED


def _first_line(text: Any, limit: int = 160) -> str:
    """取报错的第一行（后面的 traceback 对聊天窗口没意义）。"""
    line = str(text or "").strip().splitlines()
    first = line[0].strip() if line else ""
    return first if len(first) <= limit else first[:limit] + "…"
