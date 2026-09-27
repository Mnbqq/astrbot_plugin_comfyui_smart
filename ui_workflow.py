"""把 ComfyUI「界面格式」的工作流自动转成「API 格式」。

为什么需要
----------
插件只认 API 格式：

    {"3": {"class_type": "KSampler", "inputs": {...}}, ...}

而用户在 ComfyUI 界面里点「保存 / 导出」拿到的是**界面格式**：

    {"nodes": [{"id": 3, "type": "KSampler", "widgets_values": [...], "inputs": [...]}, ...],
     "links": [[1, 4, 0, 3, 0, "MODEL"], ...]}

让每个人自己进「开发者模式 → Export (API)」是常见的第一道坎，所以这里做自动转换：
把界面格式丢进 `workflows/` 就能用。转不动的地方**明确报错**（而不是静默出错图）。

转换规则（都是实测界面导出的结构）
--------------------------------
- `links` 表 + 各节点的 `inputs[].link` 还原连线；`Reroute` 直接穿过。
- `PrimitiveNode` 之类的「原语节点」不是真节点：它的值直接写进下游的输入。
- `mode=2`（静音）的节点跳过；`mode=4`（旁路）的节点按槽位穿过。
- `widgets_values` 按顺序映射到「控件输入」：优先用节点自带的 `inputs[].widget.name`
  （新版导出带这个），其次 `/object_info` 的输入顺序，最后用内置的常见节点表。
- 界面上的 `control_after_generate`、图片节点的 `upload` 不是真实输入，会被丢掉 ——
  这也是「用 object_info 顺序硬套会整体错位」的根因。
"""
from __future__ import annotations

from typing import Any

# 界面上有、但不该出现在 API 输入里的控件
NON_API_WIDGETS = {"control_after_generate", "upload", "image_upload", "choose file to upload"}
# 会多带一个 upload 控件的节点
UPLOAD_WIDGET_CLASSES = {"LoadImage", "LoadImageMask", "LoadImageOutput", "LoadAudio", "LoadVideo"}
# 直接丢掉的节点类型（纯注释）
NOTE_TYPES = {"Note", "MarkdownNote"}
# 只有一条入口一条出口的转接节点
REROUTE_TYPES = {"Reroute"}
# 控件型输入的类型名
WIDGET_TYPES = {"INT", "FLOAT", "STRING", "BOOLEAN", "COMBO", "NUMBER"}
# 界面上的「生成后控制」取值
CONTROL_VALUES = {"fixed", "increment", "decrement", "randomize"}

# 没有 object_info、节点自己也不带 widget 名字时的兜底表（常见节点）。
# 顺序 = 界面里控件的顺序（已排除 control_after_generate / upload）。
WIDGET_SPECS: dict[str, list[str]] = {
    "CheckpointLoaderSimple": ["ckpt_name"],
    "CheckpointLoader": ["config_name", "ckpt_name"],
    "UNETLoader": ["unet_name", "weight_dtype"],
    "UnetLoaderGGUF": ["unet_name"],
    "VAELoader": ["vae_name"],
    "DualCLIPLoader": ["clip_name1", "clip_name2", "type"],
    "CLIPTextEncode": ["text"],
    "CLIPTextEncodeSDXL": ["width", "height", "crop_w", "crop_h", "target_width",
                           "target_height", "text_g", "text_l"],
    "CLIPTextEncodeFlux": ["clip_l", "t5xxl", "guidance"],
    "EmptyLatentImage": ["width", "height", "batch_size"],
    "EmptySD3LatentImage": ["width", "height", "batch_size"],
    "KSampler": ["seed", "steps", "cfg", "sampler_name", "scheduler", "denoise"],
    "KSamplerAdvanced": ["add_noise", "noise_seed", "steps", "cfg", "sampler_name",
                         "scheduler", "start_at_step", "end_at_step",
                         "return_with_leftover_noise"],
    "LoraLoader": ["lora_name", "strength_model", "strength_clip"],
    "LoraLoaderModelOnly": ["lora_name", "strength_model"],
    "SaveImage": ["filename_prefix"],
    "SaveImageWebsocket": [],
    "PreviewImage": [],
    "LoadImage": ["image"],
    "LoadImageMask": ["image", "channel"],
    "LoadImageOutput": ["image"],
    "ImageScale": ["upscale_method", "width", "height", "crop"],
    "ImageScaleBy": ["upscale_method", "scale_by"],
    "ImagePadForOutpaint": ["left", "top", "right", "bottom", "feathering"],
    "GrowMask": ["expand", "tapered_corners"],
    "FeatherMask": ["left", "top", "right", "bottom"],
    "FluxGuidance": ["guidance"],
    "ConditioningCombine": [],
    "VAEEncode": [],
    "VAEDecode": [],
    "VAEEncodeForInpaint": ["grow_mask_by"],
    "SetLatentNoiseMask": [],
    "LatentUpscale": ["upscale_method", "width", "height", "crop"],
    "LatentUpscaleBy": ["upscale_method", "scale_by"],
    "ImageBlur": ["blur_radius", "sigma"],
    "ImageInvert": [],
    "ImageBatch": [],
    "RepeatLatentBatch": ["amount"],
    "ModelSamplingFlux": ["max_shift", "base_shift", "width", "height"],
    "BasicScheduler": ["scheduler", "steps", "denoise"],
    "KSamplerSelect": ["sampler_name"],
    "EmptyImage": ["width", "height", "batch_size", "color"],
}


class UIWorkflowError(ValueError):
    """界面格式转换失败（消息面向用户，说清哪一步转不了）。"""


class _Resolved:
    """一条连线的解析结果：要么是连线 `[节点id, 槽位]`，要么是一个原语值。"""

    __slots__ = ("link", "value")

    def __init__(self, link: list | None = None, value: Any = None):
        self.link = link
        self.value = value

    @property
    def is_link(self) -> bool:
        return self.link is not None


def is_ui_workflow(payload: Any) -> bool:
    """是否是 ComfyUI 界面导出的工作流（有 `nodes` 数组）。"""
    return isinstance(payload, dict) and isinstance(payload.get("nodes"), list)


def ui_to_api(payload: dict, object_info: dict | None = None) -> dict:
    """把界面格式的工作流转成 API 格式。

    Args:
        payload: 界面导出的 JSON（`nodes` / `links` / `widgets_values`）。
        object_info: `/object_info` 的结果（可选）。有它就能精确知道每个节点有哪些
            控件输入、按什么顺序；没有则退回节点自带的 `widget` 信息与内置表。

    Returns:
        API 格式工作流 `{节点id: {"class_type":..., "inputs": {...}, "_meta": {...}}}`。

    Raises:
        UIWorkflowError: 结构无法识别或某一步转不动（消息里带节点与原因）。
    """
    if not is_ui_workflow(payload):
        raise UIWorkflowError("这不是 ComfyUI 界面格式的工作流（缺少 nodes 数组）")
    nodes = [n for n in payload.get("nodes") or [] if isinstance(n, dict)]
    if not nodes:
        raise UIWorkflowError("界面工作流里没有任何节点")
    nodes_by_id = {int(n["id"]): n for n in nodes if str(n.get("id", "")).strip().lstrip("-").isdigit()}
    if not nodes_by_id:
        raise UIWorkflowError("界面工作流里的节点没有可用的 id")
    links = _index_links(payload, nodes_by_id)

    api: dict[str, dict] = {}
    for node in nodes:
        class_type = str(node.get("type") or "").strip()
        if not class_type or class_type in NOTE_TYPES:
            continue
        if class_type in REROUTE_TYPES or class_type.startswith("Primitive"):
            continue  # 转接与常量节点不落地，连线时穿过/取值
        mode = int(node.get("mode") or 0)
        if mode == 2:
            continue  # 界面里被静音：本来就不执行
        node_id = int(node["id"])
        inputs: dict[str, Any] = {}
        for item in node.get("inputs") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            link_id = item.get("link")
            if link_id in (None, ""):
                continue
            resolved = _resolve(link_id, links=links, nodes_by_id=nodes_by_id)
            if resolved is None:
                continue
            if resolved.is_link:
                inputs[name] = resolved.link
            else:
                inputs[name] = resolved.value
        inputs.update(
            _widget_values(
                node, class_type, object_info=object_info,
                already=set(inputs), node_id=node_id,
            )
        )
        entry: dict[str, Any] = {"class_type": class_type, "inputs": inputs}
        title = str(node.get("title") or "").strip()
        if title:
            entry["_meta"] = {"title": title}
        api[str(node_id)] = entry
    if not api:
        raise UIWorkflowError("界面工作流转换后没有任何可用节点")
    return api


def _index_links(payload: dict, nodes_by_id: dict) -> dict[int, tuple[int, int]]:
    """建立 `link_id -> (来源节点, 来源槽位)`。

    优先用顶层 `links` 表；没有（或不全）时，用各节点 `outputs[].links` 反推。
    """
    links: dict[int, tuple[int, int]] = {}
    for entry in payload.get("links") or []:
        if isinstance(entry, (list, tuple)) and len(entry) >= 3:
            try:
                links[int(entry[0])] = (int(entry[1]), int(entry[2]))
            except (TypeError, ValueError):
                continue
        elif isinstance(entry, dict) and entry.get("id") is not None:
            try:
                links[int(entry["id"])] = (
                    int(entry.get("origin_id")), int(entry.get("origin_slot") or 0)
                )
            except (TypeError, ValueError):
                continue
    for node_id, node in nodes_by_id.items():
        for slot, output in enumerate(node.get("outputs") or []):
            if not isinstance(output, dict):
                continue
            for link_id in output.get("links") or []:
                try:
                    links.setdefault(int(link_id), (int(node_id), int(slot)))
                except (TypeError, ValueError):
                    continue
    return links


def _resolve(
    link_id,
    *,
    links: dict[int, tuple[int, int]],
    nodes_by_id: dict,
    depth: int = 0,
) -> _Resolved | None:
    """解析一条连线，穿过 Reroute / 旁路节点；遇到原语节点则直接取值。"""
    if depth > 32:
        raise UIWorkflowError("连线里有环（Reroute/旁路节点套了太多层），无法转换")
    try:
        origin_id, origin_slot = links[int(link_id)]
    except (KeyError, TypeError, ValueError):
        return None
    node = nodes_by_id.get(origin_id)
    if node is None:
        return None
    class_type = str(node.get("type") or "")
    if class_type.startswith("Primitive"):
        return _Resolved(value=_primitive_value(node))
    if class_type in REROUTE_TYPES or int(node.get("mode") or 0) == 4:
        # 转接节点 / 旁路节点：沿它自己的输入继续往上找
        hidden = [i for i in node.get("inputs") or [] if isinstance(i, dict)]
        if not hidden:
            return None
        carried = hidden[0] if class_type in REROUTE_TYPES else (
            hidden[origin_slot] if origin_slot < len(hidden) else hidden[0]
        )
        if carried.get("link") in (None, ""):
            return None
        return _resolve(carried.get("link"), links=links, nodes_by_id=nodes_by_id, depth=depth + 1)
    return _Resolved(link=[str(origin_id), int(origin_slot)])


def _primitive_value(node: dict) -> Any:
    """取原语节点的值（界面里它就是给别的节点喂一个常量）。"""
    values = node.get("widgets_values")
    if isinstance(values, list) and values:
        return values[0]
    if isinstance(values, dict):
        for key in ("value", "text", "int", "float"):
            if key in values:
                return values[key]
    return None


def _widget_values(
    node: dict,
    class_type: str,
    *,
    object_info: dict | None,
    already: set[str],
    node_id: int,
) -> dict[str, Any]:
    """把 `widgets_values` 映射成 `{输入名: 值}`。

    优先用节点自带的 `inputs[].widget.name`（新版导出），它能顺带告诉我们哪个位置是
    `control_after_generate`；老导出没有这个信息，才退回 `/object_info` 或内置表，
    并按「数量对不上时容忍多出来的 control/upload」处理。
    """
    values = node.get("widgets_values")
    entries = _widget_entries(node)

    if isinstance(values, dict):
        # 新式导出偶尔把 widgets_values 写成对象
        result = {}
        for name, keep in (entries or [(str(k), True) for k in values]):
            if keep and name in values and name not in already:
                result[name] = values[name]
        return result

    values = list(values or [])
    if not values:
        return {}

    if entries:
        names = [name for name, _keep in entries]
        keep_flags = [keep for _name, keep in entries]
    else:
        names = _widget_names_from_info(class_type, object_info)
        keep_flags = [True] * len(names)

    aligned = _align_values(values, names, class_type, node_id)
    result: dict[str, Any] = {}
    for index, name in enumerate(names):
        if index >= len(aligned):
            break
        if not keep_flags[index] or name in NON_API_WIDGETS or name in already:
            continue
        result[name] = aligned[index]
    return result


def _widget_entries(node: dict) -> list[tuple[str, bool]]:
    """从节点 `inputs` 里按顺序取出控件输入（新版导出带 `widget.name`）。"""
    entries: list[tuple[str, bool]] = []
    for item in node.get("inputs") or []:
        if not isinstance(item, dict):
            continue
        widget = item.get("widget")
        if isinstance(widget, dict) and widget.get("name"):
            name = str(widget["name"])
            entries.append((name, name not in NON_API_WIDGETS))
    return entries


def _widget_names_from_info(class_type: str, object_info: dict | None) -> list[str]:
    """按 `/object_info` 的输入顺序取控件名；没有该节点的定义时退回内置表。"""
    names = _widget_names_from_object_info(class_type, object_info)
    if names is not None:
        return names
    return list(WIDGET_SPECS.get(class_type, []))


def _widget_names_from_object_info(class_type: str, object_info: dict | None) -> list[str] | None:
    """从 `/object_info` 里推出控件输入的名字顺序；没有该节点时返回 None。"""
    if not isinstance(object_info, dict):
        return None
    info = object_info.get(class_type)
    if not isinstance(info, dict):
        return None
    spec = info.get("input") if isinstance(info.get("input"), dict) else {}
    order = info.get("input_order") if isinstance(info.get("input_order"), dict) else {}
    names: list[str] = []
    for section in ("required", "optional"):
        section_spec = spec.get(section) if isinstance(spec.get(section), dict) else {}
        keys = order.get(section) if isinstance(order.get(section), list) else list(section_spec)
        for key in keys:
            if _is_widget_spec(section_spec.get(key)):
                names.append(str(key))
    return names


def _is_widget_spec(entry: Any) -> bool:
    """这个输入是不是界面上的控件（而不是一条连线插槽）。"""
    if isinstance(entry, list):
        if not entry:
            return False
        head = entry[0]
        if isinstance(head, list):
            return True  # COMBO：[[选项...], {..}]
        if isinstance(head, str) and head.upper() in WIDGET_TYPES:
            options = entry[1] if len(entry) > 1 and isinstance(entry[1], dict) else {}
            return not options.get("forceInput")
        return False
    if isinstance(entry, dict):
        kind = str(entry.get("type") or "").upper()
        if kind not in WIDGET_TYPES:
            return False
        return not entry.get("forceInput")
    return False


def _align_values(values: list, names: list[str], class_type: str, node_id: int) -> list:
    """把界面的控件值对齐到控件名上（容忍多出来的 control_after_generate / upload）。"""
    if len(values) == len(names):
        return values
    if len(values) == len(names) + 1:
        # 种子后面的「生成后控制」：fixed / increment / decrement / randomize
        for index, name in enumerate(names):
            if name in ("seed", "noise_seed") and index + 1 < len(values):
                if str(values[index + 1]).strip().lower() in CONTROL_VALUES:
                    return values[: index + 1] + values[index + 2:]
        # 图片加载节点的 upload 控件
        if class_type in UPLOAD_WIDGET_CLASSES:
            return values[:-1]
    raise UIWorkflowError(
        f"节点 {node_id}（{class_type}）的控件数量对不上：界面给了 {len(values)} 个值，"
        f"但按节点定义只认识 {len(names)} 个控件（{', '.join(names) or '无'}）。"
        "请改用「开发者模式 → Export (API)」导出的工作流，或把该节点换成核心节点"
    )
