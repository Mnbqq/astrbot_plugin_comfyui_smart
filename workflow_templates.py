"""工作流模板引擎。

职责：
1. 加载 API 格式工作流（内置模板 + 用户自带模板）。
2. 载入时严格校验图结构（节点 id、class_type、inputs、连线目标是否存在）。
3. 通过**图语义**推导出注入点（提示词节点 / 尺寸节点 / 采样器 / 模型加载器 / VAE / SaveImage），
   而不是硬编码节点 id —— 这样用户直接丢进任意 API 工作流都能用。
4. 提交前剔除不可达节点（ComfyUI 会校验不可达节点的 class_type，
   一个缺失自定义节点的孤儿节点会导致整个 prompt 被拒）。
5. 按架构档案（SD1.5 / SDXL / Pony / Flux ...）注入分辨率、CFG、采样器等正确默认值。
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

# 文本注入键的候选顺序：命中第一个存在的键。
TEXT_INPUT_KEYS = ("text", "clip_l", "t5xxl", "prompt", "text_g", "string", "value")
# 尺寸节点候选（SD 系用 EmptyLatentImage，Flux/SD3 用 EmptySD3LatentImage）。
LATENT_CLASSES = ("EmptyLatentImage", "EmptySD3LatentImage", "EmptyLatentImagePresets")
SAMPLER_CLASSES = ("KSampler", "KSamplerAdvanced")
SAVE_CLASSES = ("SaveImage", "SaveImageWebsocket")
# 模型加载器 -> 输入键
MODEL_LOADER_KEYS = {
    "CheckpointLoaderSimple": "ckpt_name",
    "CheckpointLoader": "ckpt_name",
    "UNETLoader": "unet_name",
    "UnetLoaderGGUF": "unet_name",
}
VAE_LOADER_KEYS = {"VAELoader": "vae_name"}
# 节点标题关键词：当图结构推导失败时，用 _meta.title / title 兜底定位
TITLE_HINTS = {
    "positive": ("正面", "正向", "positive", "prompt"),
    "negative": ("负面", "反向", "negative"),
    "latent": ("尺寸", "latent", "empty"),
    "sampler": ("采样", "sampler", "ksa"),
    "save": ("保存", "save", "输出"),
}
# 各角色对应的默认输入键（模板清单里省略 input 时按此推断）
ROLE_DEFAULT_INPUT = {
    "lora_loader": "lora_name",
}
# 这些角色的绑定形态是 (节点id, 输入键)
KEYED_ROLES = ("positive", "negative", "model_loader", "vae_loader", "lora_loader")
# 这些角色的绑定形态只有一个节点 id
NODE_ROLES = ("sampler", "latent", "save", "guidance")

# 采样参数在各节点上的键名（KSamplerAdvanced 用 noise_seed）。
SAMPLER_FIELDS = {
    "seed": ("seed", "noise_seed"),
    "steps": ("steps",),
    "cfg": ("cfg",),
    "sampler_name": ("sampler_name",),
    "scheduler": ("scheduler",),
    "denoise": ("denoise",),
}


class TemplateError(ValueError):
    """模板不合法。"""


# --------------------------------------------------------------------------- #
# 图工具
# --------------------------------------------------------------------------- #
def prune_unreachable(graph: dict) -> dict:
    """剔除从输出节点回溯不可达的节点。

    ComfyUI 会对整个 prompt 里每个节点做 class_type 校验，包括那些不会被执行的节点。
    用户的工作流常残留已删除自定义节点的孤儿节点，若不剔除会导致整图提交失败。

    Args:
        graph: API 格式工作流。

    Returns:
        仅保留可达节点的新图。
    """
    outputs = [
        nid for nid, node in graph.items() if node.get("class_type") in SAVE_CLASSES
    ]
    if not outputs:
        return graph

    keep: set[str] = set()
    stack = list(outputs)
    while stack:
        nid = stack.pop()
        if nid in keep or nid not in graph:
            continue
        keep.add(nid)
        inputs = graph[nid].get("inputs")
        if isinstance(inputs, dict):
            for value in inputs.values():
                if (
                    isinstance(value, list)
                    and len(value) == 2
                    and isinstance(value[0], str)
                    and value[0] in graph
                ):
                    stack.append(value[0])
    return {nid: node for nid, node in graph.items() if nid in keep}


def validate_graph(graph: dict) -> None:
    """校验 API 格式图结构，不合法则抛 TemplateError。

    Args:
        graph: API 格式工作流。

    Raises:
        TemplateError: 图结构不合法。
    """
    if not isinstance(graph, dict) or not graph:
        raise TemplateError("工作流为空或不是对象")
    for node_id, node in graph.items():
        if not isinstance(node, dict):
            raise TemplateError(f"节点 {node_id} 不是对象")
        class_type = node.get("class_type")
        if not isinstance(class_type, str) or not class_type:
            raise TemplateError(f"节点 {node_id} 缺少 class_type")
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            raise TemplateError(f"节点 {node_id} 缺少 inputs")
        for key, value in inputs.items():
            if not isinstance(value, list):
                continue
            # 连线必须是 [源节点id, 输出序号]
            if len(value) != 2:
                raise TemplateError(f"节点 {node_id}.{key} 的连线格式错误：{value!r}")
            source, port = value
            if not isinstance(source, str) or source not in graph:
                raise TemplateError(
                    f"节点 {node_id}.{key} 指向不存在的节点 {source!r}"
                )
            if not isinstance(port, int) or port < 0:
                raise TemplateError(
                    f"节点 {node_id}.{key} 的输出端口非法：{port!r}"
                )
            # 自环：节点把自己的输出当输入，ComfyUI 会判定为依赖环并拒绝整张图
            if source == node_id:
                raise TemplateError(
                    f"节点 {node_id}.{key} 指向自身，会形成依赖环（ComfyUI 会拒绝该工作流）"
                )


def _upstream_text_node(graph: dict, link, seen: set[str]) -> tuple[str, str] | None:
    """从一条 conditioning 连线回溯，找到第一个带文本输入的节点。

    用于在任意用户工作流中定位正向/负向提示词节点，而不依赖节点 id 或标题。
    会穿过 FluxGuidance / ConditioningZeroOut 之类的中间节点。

    Args:
        graph: API 格式工作流。
        link: [源节点id, 输出序号] 形式的连线。
        seen: 已访问节点，防止环。

    Returns:
        (节点id, 文本输入键)；找不到返回 None。
    """
    if not (isinstance(link, list) and len(link) == 2 and isinstance(link[0], str)):
        return None
    node_id = link[0]
    if node_id in seen or node_id not in graph:
        return None
    seen.add(node_id)
    node = graph[node_id]
    inputs = node.get("inputs")
    if not isinstance(inputs, dict):
        return None
    for key in TEXT_INPUT_KEYS:
        # 文本输入是字面量字符串（不是连线）
        if key in inputs and not isinstance(inputs[key], list):
            return node_id, key
    # 类名带 text/prompt/string/encode 的自定义节点：其字面量字符串输入即为提示词
    lowered = str(node.get("class_type") or "").lower()
    if any(hint in lowered for hint in ("text", "prompt", "string", "encode")):
        key = _text_key_of(node)
        if key:
            return node_id, key
    # 继续向更上游回溯
    for value in inputs.values():
        found = _upstream_text_node(graph, value, seen)
        if found:
            return found
    return None


def _detect_loader(graph: dict) -> str:
    """从图里推断模型加载方式。

    Args:
        graph: API 格式工作流。

    Returns:
        checkpoint 或 unet。
    """
    for node in graph.values():
        class_type = node.get("class_type")
        if class_type in ("CheckpointLoaderSimple", "CheckpointLoader"):
            return "checkpoint"
        if class_type in ("UNETLoader", "UnetLoaderGGUF"):
            return "unet"
    return "checkpoint"


def node_title(node: dict) -> str:
    """取节点标题：兼容 ComfyUI 的 _meta.title 与顶层 title。

    Args:
        node: 工作流节点。

    Returns:
        标题字符串，没有则返回空串。
    """
    meta = node.get("_meta")
    if isinstance(meta, dict) and isinstance(meta.get("title"), str):
        return meta["title"]
    title = node.get("title")
    return title if isinstance(title, str) else ""


def _find_by_title(graph: dict, keywords: tuple[str, ...], classes: tuple[str, ...]) -> str:
    """按标题关键词查找节点，找不到时返回空串。

    先要求标题命中，再要求 class_type 属于给定集合，避免误配到无关节点。

    Args:
        graph: 工作流。
        keywords: 标题关键词（不区分大小写）。
        classes: 允许的 class_type 集合；为空表示不限。

    Returns:
        节点 id 或空串。
    """
    for node_id, node in graph.items():
        if classes and node.get("class_type") not in classes:
            continue
        title = node_title(node).lower()
        if title and any(k.lower() in title for k in keywords):
            return node_id
    return ""


def _text_key_of(node: dict, preferred: str = "") -> str:
    """取节点上实际存在的文本输入键。

    关键点：**必须返回该节点 inputs 里真实存在的键**。
    自定义提示词节点常常不叫 text（例如 wildcard_text），
    若凭空返回 "text" 会往节点里塞一个不存在的输入，提交时被服务端拒绝。

    Args:
        node: 节点。
        preferred: 首选键。

    Returns:
        真实存在的输入键名；实在找不到时返回空字符串。
    """
    inputs = node.get("inputs") or {}
    if preferred and preferred in inputs:
        return preferred
    for key in TEXT_INPUT_KEYS:
        if key in inputs and not isinstance(inputs[key], list):
            return key
    # 退一步：值为字符串的输入极可能就是提示词字段（自定义节点的 wildcard_text / value 等）
    for key, value in inputs.items():
        if isinstance(value, str):
            return key
    for key in TEXT_INPUT_KEYS:
        if key in inputs:
            return key
    return ""


def _apply_binding_overrides(graph: dict, bindings: dict, spec: dict, name: str) -> dict:
    """按模板清单里的 bindings 覆盖自动推导结果。

    支持按节点 id 或按节点标题（`_meta.title`）指定，用于自动推导选错节点的场景。

    Args:
        graph: 工作流。
        bindings: 自动推导出的绑定（原地修改）。
        spec: 清单里的 bindings 段。
        name: 模板名，用于报错信息。

    Returns:
        覆盖后的绑定。

    Raises:
        TemplateError: 指定的节点不存在或角色名不被支持。
    """
    if not isinstance(spec, dict):
        return bindings

    known = set(KEYED_ROLES) | set(NODE_ROLES)
    for role, target in spec.items():
        if role not in known:
            raise TemplateError(f"模板 {name} 的 bindings 里有未知角色：{role}")
        if not isinstance(target, dict):
            raise TemplateError(f"模板 {name} 的 bindings.{role} 必须是对象")

        node_id = str(target.get("node") or "").strip()
        if not node_id and target.get("title"):
            keywords = (str(target["title"]).strip(),)
            classes = ()
            if role in ("positive", "negative"):
                classes = ()
            node_id = _find_by_title(graph, keywords, classes)
            if not node_id:
                raise TemplateError(
                    f"模板 {name} 的 bindings.{role} 指定的标题 {target['title']!r} 未匹配到任何节点"
                )
        if not node_id:
            raise TemplateError(
                f"模板 {name} 的 bindings.{role} 必须提供 node 或 title"
            )
        if node_id not in graph:
            raise TemplateError(
                f"模板 {name} 的 bindings.{role} 指向不存在的节点 {node_id!r}"
            )

        if role in NODE_ROLES:
            bindings[role] = node_id
            continue

        # 需要输入键的角色：显式给出优先，否则按角色/节点结构推断
        node = graph[node_id]
        explicit = str(target.get("input") or "").strip()
        if explicit:
            if explicit not in (node.get("inputs") or {}):
                raise TemplateError(
                    f"模板 {name} 的 bindings.{role} 指定的输入 {explicit!r} "
                    f"在节点 {node_id} 上不存在"
                )
            key = explicit
        elif role in ("positive", "negative"):
            key = _text_key_of(node)
        elif role == "model_loader":
            key = MODEL_LOADER_KEYS.get(node.get("class_type"), "")
            if not key:
                raise TemplateError(
                    f"模板 {name} 的 bindings.{role} 的节点 {node_id} 不是已知的模型加载器"
                )
        elif role == "vae_loader":
            key = VAE_LOADER_KEYS.get(node.get("class_type"), "")
            if not key:
                raise TemplateError(
                    f"模板 {name} 的 bindings.{role} 的节点 {node_id} 不是 VAE 加载器"
                )
        else:
            key = ROLE_DEFAULT_INPUT.get(role, "")
        bindings[role] = (node_id, key)
    return bindings


def _derive_bindings(graph: dict) -> dict:
    """从图结构推导出各注入点。

    Args:
        graph: 已校验的 API 格式工作流。

    Returns:
        绑定字典，供 build() 注入。

    Raises:
        TemplateError: 找不到必需的注入点。
    """
    bindings: dict = {
        "sampler": None,
        "positive": None,
        "negative": None,
        "latent": None,
        "model_loader": None,
        "vae_loader": None,
        "lora_loader": None,
        "save": None,
        "guidance": None,
    }

    for node_id, node in graph.items():
        class_type = node.get("class_type")
        inputs = node.get("inputs") or {}
        if class_type in SAMPLER_CLASSES and bindings["sampler"] is None:
            bindings["sampler"] = node_id
            bindings["positive"] = _upstream_text_node(
                graph, inputs.get("positive"), set()
            )
            bindings["negative"] = _upstream_text_node(
                graph, inputs.get("negative"), set()
            )
        elif class_type in LATENT_CLASSES and bindings["latent"] is None:
            bindings["latent"] = node_id
        elif class_type in MODEL_LOADER_KEYS and bindings["model_loader"] is None:
            bindings["model_loader"] = (node_id, MODEL_LOADER_KEYS[class_type])
        elif class_type in VAE_LOADER_KEYS and bindings["vae_loader"] is None:
            bindings["vae_loader"] = (node_id, VAE_LOADER_KEYS[class_type])
        elif class_type in ("LoraLoader", "LoraLoaderModelOnly") and bindings[
            "lora_loader"
        ] is None:
            bindings["lora_loader"] = (node_id, "lora_name")
        elif class_type in SAVE_CLASSES and bindings["save"] is None:
            bindings["save"] = node_id
        elif class_type == "FluxGuidance" and bindings["guidance"] is None:
            bindings["guidance"] = node_id

    # 图结构推导失败时，用节点标题兜底（ComfyUI 导出的工作流通常带 _meta.title）
    if bindings["sampler"] is None:
        bindings["sampler"] = _find_by_title(graph, TITLE_HINTS["sampler"], SAMPLER_CLASSES)
    if bindings["latent"] is None:
        bindings["latent"] = _find_by_title(graph, TITLE_HINTS["latent"], LATENT_CLASSES)
    if bindings["save"] is None:
        bindings["save"] = _find_by_title(graph, TITLE_HINTS["save"], SAVE_CLASSES)

    # 提示词节点仍缺失时同样按标题找；正负都缺则按「先正后负」分配
    if bindings["positive"] is None or bindings["negative"] is None:
        by_title_pos = _find_by_title(graph, TITLE_HINTS["positive"], ())
        by_title_neg = _find_by_title(graph, TITLE_HINTS["negative"], ())
        if bindings["positive"] is None and by_title_pos:
            bindings["positive"] = (by_title_pos, _text_key_of(graph[by_title_pos]))
        if bindings["negative"] is None and by_title_neg:
            bindings["negative"] = (by_title_neg, _text_key_of(graph[by_title_neg]))
        if bindings["positive"] is None and bindings["negative"] is None:
            # 无任何提示时，取图中前两个文本节点，先作正向后作负向
            text_nodes = [
                nid
                for nid, node in graph.items()
                if any(k in (node.get("inputs") or {}) for k in TEXT_INPUT_KEYS)
            ]
            if len(text_nodes) >= 2:
                bindings["positive"] = (text_nodes[0], _text_key_of(graph[text_nodes[0]]))
                bindings["negative"] = (text_nodes[1], _text_key_of(graph[text_nodes[1]]))

    if bindings["sampler"] is None:
        raise TemplateError("工作流中没有 KSampler 节点，无法注入出图参数")
    if bindings["positive"] is None:
        raise TemplateError(
            "无法定位正向提示词节点：既没能从 KSampler 的连接回溯到，也没有带「正面/positive」"
            "标题的节点。可在模板清单的 bindings 里显式指定节点 id 或标题。"
        )
    if bindings["latent"] is None:
        raise TemplateError(
            "工作流中没有 EmptyLatentImage / EmptySD3LatentImage 节点，"
            "且在模板清单的 bindings 里也未指定尺寸节点"
        )
    return bindings


# --------------------------------------------------------------------------- #
# 架构档案
# --------------------------------------------------------------------------- #
ARCH_PROFILES: dict[str, dict] = {
    "sd15": {
        # 该架构的推荐总像素：用于把外部给出的尺寸按比例归一到合适档位
        "pixels": 393216,
        "label": "Stable Diffusion 1.5",
        "size": (512, 768),
        "steps": 25,
        "cfg": 7.0,
        "sampler": "dpmpp_2m",
        "scheduler": "karras",
        "guidance": None,
        "negative": True,
        # SD1.5 系模型不加质量词出图会明显发糊、细节崩坏
        "quality_tags": "masterpiece, best quality",
        "negative_extra": "worst quality, low quality, jpeg artifacts",
    },
    "sdxl": {
        # 该架构的推荐总像素：用于把外部给出的尺寸按比例归一到合适档位
        "pixels": 1048576,
        "label": "SDXL",
        "size": (1024, 1024),
        "steps": 28,
        "cfg": 6.0,
        "sampler": "dpmpp_2m",
        "scheduler": "karras",
        "guidance": None,
        "negative": True,
        "quality_tags": "",
        "negative_extra": "worst quality, low quality",
    },
    "pony": {
        # 该架构的推荐总像素：用于把外部给出的尺寸按比例归一到合适档位
        "pixels": 1048576,
        "label": "Pony / Illustrious（SDXL 系）",
        "size": (1024, 1024),
        "steps": 28,
        "cfg": 7.0,
        "sampler": "dpmpp_2m",
        "scheduler": "karras",
        "guidance": None,
        "negative": True,
        "score_prefix": "score_9, score_8_up, score_7_up, ",
        "quality_tags": "",
        # Pony 系的负面词必须显式排除低分档，否则容易出糊图
        "negative_extra": "score_6, score_5, score_4, worst quality, low quality",
    },
    "flux": {
        # 该架构的推荐总像素：用于把外部给出的尺寸按比例归一到合适档位
        "pixels": 1048576,
        "label": "FLUX.1",
        "size": (1024, 1024),
        "steps": 20,
        "cfg": 1.0,
        "sampler": "euler",
        "scheduler": "simple",
        "guidance": 3.5,
        "negative": False,
    },
    "sd3": {
        # 该架构的推荐总像素：用于把外部给出的尺寸按比例归一到合适档位
        "pixels": 1048576,
        "label": "Stable Diffusion 3",
        "size": (1024, 1024),
        "steps": 28,
        "cfg": 4.5,
        "sampler": "dpmpp_2m",
        "scheduler": "sgm_uniform",
        "guidance": None,
        "negative": True,
    },
}
# 未识别时的兜底架构。
# 取 sd15 而不是 sdxl：社区里 SDXL 系模型的命名几乎都带 xl（juggernautXL、
# dreamshaperXL、albedoBaseXL…），而不带 xl 的老模型绝大多数是 SD1.5 时代作品
# （AbyssOrangeMix、chilloutmix、cetusMix…）。判错时 sd15 的参数用在大模型上
# 只是偏小，反过来把 512 档参数用在 SD1.5 上则明显劣化。
DEFAULT_ARCH = "sd15"

# 显式关键词 -> 架构（顺序即优先级，"xl" 这条在后面单独判断）
_ARCH_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("flux",), "flux"),
    (("pony", "illustrious", "noobai", "animagine", "autismmix"), "pony"),
    (("sd3", "stable-diffusion-3", "sd35", "sd35"), "sd3"),
    # 明确的 SD1.5 时代模型家族
    (
        (
            "sd15", "sd_15", "v1-5", "v1.5", "anything-v", "abyssorangemix", "aom3",
            "counterfeit", "meinamix", "chilloutmix", "cetusmix", "basil", "aingdiffusion",
            "beautifulrealistic", "realisticvision", "dreamshaper_8", "pastelmix",
            "darksushi", "orange", "guofeng", "3guofeng", "cheesedaddys", "babe",
            "aniverse", "animerge", "arthemy", "analogmadness", "blazingdrive",
            "corneo", "cuteyuki", "colorful", "absolutereality", "artificialjourney",
            "aZovya", "yabai", "majicmix", "brav6", "toonyou", "mistoonanime",
        ),
        "sd15",
    ),
    # SDXL 系：社区惯例是名字里带 xl
    (("xl", "juggernaut", "realvis"), "sdxl"),
)


def profile_pixels(arch: str) -> int:
    """返回该架构的推荐总像素（宽 × 高）。

    Args:
        arch: 架构 key。

    Returns:
        总像素；未知架构返回 0。
    """
    return int(arch_profile(arch).get("pixels") or 0)


def guess_arch(model_name: str, override: str = "") -> str:
    """按模型文件名猜测架构。

    Args:
        model_name: 模型文件名，可含子目录。
        override: 用户在配置里指定的架构；非空且合法时直接采用。

    Returns:
        架构 key；未识别时返回 DEFAULT_ARCH。
    """
    if override and override in ARCH_PROFILES:
        return override
    lowered = (model_name or "").lower()
    for keywords, arch in _ARCH_HINTS:
        if any(k in lowered for k in keywords):
            return arch
    return DEFAULT_ARCH


def arch_profile(arch: str) -> dict:
    """取架构档案，未知架构回退默认。"""
    return ARCH_PROFILES.get(arch) or ARCH_PROFILES[DEFAULT_ARCH]


# --------------------------------------------------------------------------- #
# 模板
# --------------------------------------------------------------------------- #
class WorkflowTemplate:
    """一个 API 格式工作流模板及其注入绑定。"""

    def __init__(
        self,
        name: str,
        graph: dict,
        *,
        arch: str = "",
        loader: str = "",
        source: str = "",
        bindings: dict | None = None,
    ):
        """初始化模板。

        Args:
            name: 模板名。
            graph: API 格式工作流（会被深拷贝并剔除不可达节点）。
            arch: 绑定的架构，空或 generic 表示通用。
            loader: 模型加载方式，checkpoint 或 unet。
            source: 来源描述（内置或文件路径）。
            bindings: 可选的注入点覆盖（按节点 id 或标题），用于自动推导选错节点的场景。

        Raises:
            TemplateError: 图结构不合法、缺必需注入点，或 bindings 指向不存在的节点。
        """
        if not isinstance(graph, dict):
            raise TemplateError(f"模板 {name} 不是有效的工作流对象")
        self.name = name
        self.arch = arch
        self.loader = loader or _detect_loader(graph)
        self.source = source
        cleaned = prune_unreachable(copy.deepcopy(graph))
        validate_graph(cleaned)
        self.graph = cleaned
        self.bindings = _apply_binding_overrides(
            cleaned, _derive_bindings(cleaned), bindings or {}, name
        )

    def describe(self) -> dict:
        """返回模板摘要，供 /模板列表 与 Pages 展示。"""
        return {
            "name": self.name,
            "arch": self.arch or "generic",
            "loader": self.loader,
            "source": self.source,
            "nodes": len(self.graph),
            "class_types": sorted(self.required_nodes()),
        }

    def required_nodes(self) -> set[str]:
        """返回该模板需要的全部节点类名。

        用于在提交前对照 ComfyUI 实际安装的节点做能力探测，
        避免把「缺自定义节点」的工作流发过去再被服务端拒绝。

        Returns:
            class_type 集合。
        """
        return {
            node["class_type"]
            for node in self.graph.values()
            if isinstance(node.get("class_type"), str) and node["class_type"]
        }

    def missing_nodes(self, available: set[str] | None) -> set[str]:
        """对照服务器可用节点，返回该模板缺失的节点类名。

        Args:
            available: 服务器已安装的节点类名；为空或 None 表示探测失败，不做判断。

        Returns:
            缺失的 class_type 集合。
        """
        if not available:
            return set()
        return {c for c in self.required_nodes() if c not in available}

    def build(
        self,
        *,
        positive: str,
        negative: str = "",
        model_name: str = "",
        vae_name: str = "",
        lora_name: str = "",
        lora_strength: float = 1.0,
        width: int | None = None,
        height: int | None = None,
        steps: int | None = None,
        cfg: float | None = None,
        sampler: str = "",
        scheduler: str = "",
        seed: int | None = None,
        batch_size: int = 1,
        guidance: float | None = None,
        filename_prefix: str = "astrbot_smart",
    ) -> dict:
        """按模板生成可直接提交的图。

        Args:
            positive: 正向提示词。
            negative: 负向提示词（不支持负向的架构会被忽略）。
            model_name: 要加载的模型文件名。
            vae_name: 独立 VAE 文件名，空则沿用工作流默认。
            lora_name: LoRA 文件名，空则不注入。
            lora_strength: LoRA 强度。
            width: 宽度，None 用工作流默认。
            height: 高度，None 用工作流默认。
            steps: 采样步数。
            cfg: CFG 强度。
            sampler: 采样器名。
            scheduler: 调度器名。
            seed: 随机种子。
            batch_size: 批次大小。
            guidance: Flux guidance 值。
            filename_prefix: 输出文件名前缀。

        Returns:
            可提交给 /prompt 的图。
        """
        graph = copy.deepcopy(self.graph)
        b = self.bindings

        # 1) 模型
        if model_name and b["model_loader"]:
            node_id, key = b["model_loader"]
            graph[node_id]["inputs"][key] = model_name
        # 2) 独立 VAE：模板自带 VAELoader 就改写，否则插入一个再改接 VAEDecode。
        #    否则「指定 VAE」会被静默忽略 —— sd_checkpoint 这类模板用的是底模自带 VAE，
        #    没有 VAELoader 节点，配置里的强制 VAE 与 LLM 选中的 VAE 都会失效。
        if vae_name:
            if b["vae_loader"]:
                node_id, key = b["vae_loader"]
                graph[node_id]["inputs"][key] = vae_name
            else:
                _apply_vae(graph, vae_name)
        # 3) 提示词（正/负）
        pos_id, pos_key = b["positive"]
        if not pos_key:
            pos_key = _text_key_of(graph[pos_id])
        if pos_key:
            for key in _text_keys(graph, pos_id, pos_key):
                graph[pos_id]["inputs"][key] = positive
        if b["negative"] and arch_profile(self.arch).get("negative", True):
            neg_id, neg_key = b["negative"]
            if not neg_key:
                neg_key = _text_key_of(graph[neg_id])
            if neg_key:
                for key in _text_keys(graph, neg_id, neg_key):
                    graph[neg_id]["inputs"][key] = negative
        # 4) 尺寸与批次
        if b["latent"]:
            latent_inputs = graph[b["latent"]]["inputs"]
            if width:
                latent_inputs["width"] = int(width)
            if height:
                latent_inputs["height"] = int(height)
            if "batch_size" in latent_inputs:
                latent_inputs["batch_size"] = int(batch_size)
        # 5) 采样参数
        sampler_inputs = graph[b["sampler"]]["inputs"]
        _set_first(sampler_inputs, SAMPLER_FIELDS["seed"], seed)
        _set_first(sampler_inputs, SAMPLER_FIELDS["steps"], steps)
        _set_first(sampler_inputs, SAMPLER_FIELDS["cfg"], cfg)
        _set_first(sampler_inputs, SAMPLER_FIELDS["sampler_name"], sampler or None)
        _set_first(sampler_inputs, SAMPLER_FIELDS["scheduler"], scheduler or None)
        # 6) Flux guidance
        if b["guidance"] and guidance is not None:
            if "guidance" in graph[b["guidance"]]["inputs"]:
                graph[b["guidance"]]["inputs"]["guidance"] = float(guidance)
        # 7) LoRA（模板自带则改写，否则插入并重接）
        if lora_name:
            _apply_lora(graph, b, lora_name, lora_strength)
        # 8) 输出前缀
        if b["save"] and "filename_prefix" in graph[b["save"]]["inputs"]:
            graph[b["save"]]["inputs"]["filename_prefix"] = filename_prefix
        return graph


def _text_keys(graph: dict, node_id: str, primary: str) -> list[str]:
    """返回该节点上需要写入同一段提示词的文本输入键。

    CLIPTextEncodeFlux 同时有 clip_l 与 t5xxl，需要一起写。

    Args:
        graph: 工作流。
        node_id: 文本节点 id。
        primary: 主键。

    Returns:
        需要写入的键列表。
    """
    inputs = graph[node_id]["inputs"]
    keys = [primary]
    if graph[node_id].get("class_type") == "CLIPTextEncodeFlux":
        keys = [k for k in ("clip_l", "t5xxl") if k in inputs]
    return keys or [primary]


def _set_first(inputs: dict, keys: tuple[str, ...], value) -> None:
    """把值写入候选键中第一个存在的键。"""
    if value is None:
        return
    for key in keys:
        if key in inputs:
            inputs[key] = value
            return


# 潜空间尺寸必须是 8 的倍数（SD 的下采样倍率）
DIM_ALIGN = 8

# Hires Fix 第二轮的采样参数（从首轮 KSampler 复制过来）
SAMPLER_COPY_FIELDS = ("model", "positive", "negative", "cfg", "sampler_name", "scheduler")


def _new_node_id(graph: dict) -> str:
    """返回一个未被占用的数字节点 id。"""
    return str(max((int(k) for k in graph if str(k).isdigit()), default=0) + 1)


def add_hires_fix(
    graph: dict,
    bindings: dict,
    *,
    scale: float = 1.5,
    denoise: float = 0.5,
    steps: int = 0,
    seed: int = 0,
    upscale_method: str = "bislerp",
) -> dict:
    """插入 Hires Fix：首轮采样 → 潜空间放大 → 二次采样重绘。

    这是改善手部、人脸与细节最有效的手段之一。只用核心节点 `LatentUpscale`，
    不需要额外的放大模型（ESRGAN 之类）。

    结构（原地修改）：
        EmptyLatent → KSampler → LatentUpscale → KSampler2 → VAEDecode

    Args:
        graph: 已构建好的工作流（原地修改）。
        bindings: 模板绑定。
        scale: 放大倍数。
        denoise: 第二轮重绘幅度，0.4~0.6 常见；过高会改变构图。
        steps: 第二轮步数；0 表示沿用首轮步数。
        seed: 第二轮种子。
        upscale_method: LatentUpscale 的放大算法。

    Returns:
        含 hires_sampler / hires_upscale 节点 id 的字典；条件不满足时为空字典。
    """
    sampler_id = bindings.get("sampler")
    if not sampler_id or sampler_id not in graph:
        return {}
    first = graph[sampler_id]
    first_inputs = first.get("inputs") or {}

    # 找出所有消费首轮采样结果的节点（正常是 VAEDecode）
    consumers = [
        node_id
        for node_id, node in graph.items()
        if isinstance(node.get("inputs"), dict)
        and node["inputs"].get("samples") == [sampler_id, 0]
    ]
    if not consumers:
        return {}

    # 尺寸取自潜空间节点（缺省则回退 512x512）
    width = height = 0
    latent_id = bindings.get("latent")
    if latent_id and latent_id in graph:
        latent_inputs = graph[latent_id].get("inputs") or {}
        width = int(latent_inputs.get("width") or 0)
        height = int(latent_inputs.get("height") or 0)
    if width <= 0 or height <= 0:
        width, height = 512, 512
    target_w = max(DIM_ALIGN, int(width * float(scale)))
    target_h = max(DIM_ALIGN, int(height * float(scale)))
    target_w -= target_w % DIM_ALIGN
    target_h -= target_h % DIM_ALIGN

    up_id = _new_node_id(graph)
    second_id = str(int(up_id) + 1)
    graph[up_id] = {
        "class_type": "LatentUpscale",
        "_meta": {"title": "Hires 放大"},
        "inputs": {
            "samples": [sampler_id, 0],
            "upscale_method": upscale_method,
            "width": target_w,
            "height": target_h,
            "crop": "disabled",
        },
    }

    second_inputs: dict = {"latent_image": [up_id, 0], "denoise": float(denoise)}
    for field in SAMPLER_COPY_FIELDS:
        if field in first_inputs:
            second_inputs[field] = first_inputs[field]
    second_inputs["seed"] = int(seed) if seed else first_inputs.get("seed", 0)
    second_inputs["steps"] = int(steps) if steps and steps > 0 else first_inputs.get("steps", 20)

    graph[second_id] = {
        "class_type": first.get("class_type", "KSampler"),
        "_meta": {"title": "Hires 二次采样"},
        "inputs": second_inputs,
    }

    # 把消费首轮结果的下游节点改接到第二轮
    for node_id in consumers:
        graph[node_id]["inputs"]["samples"] = [second_id, 0]

    return {"hires_sampler": second_id, "hires_upscale": up_id,
            "width": target_w, "height": target_h}


def _apply_vae(graph: dict, vae_name: str) -> None:
    """把独立 VAE 接进图里。

    模板没有 VAELoader 节点时（例如 sd_checkpoint 直接用底模自带 VAE），
    插入一个 VAELoader 并把所有 VAEDecode 的 vae 输入改接到它。
    出图偏色发灰时，换成独立 VAE 是最常见的解法，因此这条路径必须真的生效。

    Args:
        graph: 工作流（原地修改）。
        vae_name: VAE 文件名。
    """
    new_id = str(max((int(k) for k in graph if k.isdigit()), default=0) + 1)
    graph[new_id] = {"class_type": "VAELoader", "inputs": {"vae_name": vae_name}}
    for node in graph.values():
        if node.get("class_type") != "VAEDecode":
            continue
        inputs = node.get("inputs")
        if isinstance(inputs, dict):
            inputs["vae"] = [new_id, 0]


def _apply_lora(graph: dict, bindings: dict, lora_name: str, strength: float) -> None:
    """把 LoRA 接进已有的采样图。

    模板自带 LoraLoader 时直接改写；否则插入一个 LoraLoader 并把
    model/clip 的下游连接改到新节点，同时按需串联多个 LoRA。

    Args:
        graph: 工作流（原地修改）。
        bindings: 模板绑定。
        lora_name: LoRA 文件名。
        strength: 强度。
    """
    existing = bindings.get("lora_loader")
    if existing and existing[0] in graph:
        node_id, key = existing
        graph[node_id]["inputs"][key] = lora_name
        if "strength_model" in graph[node_id]["inputs"]:
            graph[node_id]["inputs"]["strength_model"] = float(strength)
        if "strength_clip" in graph[node_id]["inputs"]:
            graph[node_id]["inputs"]["strength_clip"] = float(strength)
        return

    # 找到当前的 model / clip 源头
    model_src = None
    clip_src = None
    if bindings["model_loader"]:
        loader_id, _ = bindings["model_loader"]
        loader_class = graph.get(loader_id, {}).get("class_type")
        model_src = [loader_id, 0]
        # 只有 checkpoint 加载器同时输出 CLIP（端口 1）；UNETLoader 没有 CLIP 输出
        if loader_class in ("CheckpointLoaderSimple", "CheckpointLoader"):
            clip_src = [loader_id, 1]

    # 复用模板里已有的 (model, clip) 来源
    sampler_inputs = graph[bindings["sampler"]]["inputs"]
    if isinstance(sampler_inputs.get("model"), list):
        model_src = list(sampler_inputs["model"])
    # CLIP 来源优先取提示词节点的连线（比模型加载器更可靠）
    for role in ("positive", "negative"):
        candidate = bindings.get(role)
        if not candidate:
            continue
        link = (graph.get(candidate[0], {}).get("inputs") or {}).get("clip")
        if isinstance(link, list) and len(link) == 2:
            clip_src = list(link)
            break

    if model_src is None:
        return

    # 关键：**先把需要重接的下游节点收集起来，再插入 LoraLoader**。
    # 否则这个循环会把刚插入的 LoraLoader 自己也算进去，
    # 把它的 clip 改写为指向自身，形成依赖环 —— ComfyUI 会直接拒绝整张图
    # （而且只回一句 prompt_outputs_failed_validation，不给任何节点级原因）。
    rewire_clip: list[str] = []
    if clip_src is not None:
        rewire_clip = [
            node_id
            for node_id, node in graph.items()
            if isinstance(node.get("inputs"), dict)
            and node["inputs"].get("clip") == clip_src
        ]

    new_id = str(max((int(k) for k in graph if k.isdigit()), default=0) + 1)
    graph[new_id] = {
        "class_type": "LoraLoader",
        "inputs": {
            "model": model_src,
            "clip": clip_src if clip_src is not None else model_src,
            "lora_name": lora_name,
            "strength_model": float(strength),
            "strength_clip": float(strength),
        },
    }
    # 重接下游：KSampler.model 与各 CLIPTextEncode.clip
    sampler_inputs["model"] = [new_id, 0]
    for node_id in rewire_clip:
        graph[node_id]["inputs"]["clip"] = [new_id, 1]


# --------------------------------------------------------------------------- #
# 加载与选择
# --------------------------------------------------------------------------- #
def parse_template_payload(payload: dict, name: str, source: str = "") -> dict:
    """解析模板文件内容，支持裸图与带元信息的包装两种写法。

    Args:
        payload: 文件解析出的对象。
        name: 模板名。
        source: 来源描述。

    Returns:
        {"name":..., "arch":..., "loader":..., "graph":...}

    Raises:
        TemplateError: 无法识别的结构。
    """
    if not isinstance(payload, dict):
        raise TemplateError(f"模板 {name} 不是对象")

    def _field(key: str) -> str:
        value = payload.get(key)
        return value if isinstance(value, str) else ""

    spec = payload.get("bindings")
    spec = spec if isinstance(spec, dict) else {}

    if "graph" in payload and isinstance(payload["graph"], dict):
        return {
            "name": _field("name") or name,
            "arch": _field("arch"),
            "loader": _field("loader"),
            "bindings": spec,
            "graph": payload["graph"],
        }
    # 裸 API 图：顶层就是 {"节点id": {...}}；也容忍 ComfyUI 的 {"prompt": {...}} 包裹
    if "prompt" in payload and isinstance(payload["prompt"], dict):
        return {
            "name": name,
            "arch": "",
            "loader": "",
            "bindings": {},
            "graph": payload["prompt"],
        }
    if all(isinstance(v, dict) and "class_type" in v for v in payload.values()):
        return {"name": name, "arch": "", "loader": "", "bindings": {}, "graph": payload}
    raise TemplateError(f"模板 {name} 结构无法识别（既不是 API 图也不是包装格式）")


def load_templates(*dirs: Path) -> dict[str, WorkflowTemplate]:
    """加载目录下的全部模板，后者覆盖同名前者。

    Args:
        *dirs: 模板目录，按顺序加载。

    Returns:
        模板名 -> WorkflowTemplate。坏模板会被跳过，不影响其余模板。
    """
    templates: dict[str, WorkflowTemplate] = {}
    for directory in dirs:
        if not directory or not Path(directory).is_dir():
            continue
        for path in sorted(Path(directory).glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                meta = parse_template_payload(payload, path.stem, str(path))
                templates[meta["name"]] = WorkflowTemplate(
                    meta["name"],
                    meta["graph"],
                    arch=meta["arch"],
                    loader=meta["loader"],
                    source=str(path),
                    bindings=meta.get("bindings") or {},
                )
            except (OSError, json.JSONDecodeError, TemplateError):
                # 坏模板不应拖垮插件启动
                continue
    return templates


def is_compatible(template: WorkflowTemplate, arch: str) -> bool:
    """判断模板是否适用于该架构。

    generic（或未声明）的模板适用于任何架构；否则要求架构一致。
    这个检查能避免把 Flux 模板套到 SD1.5 模型上：那会提交一堆目标服务器上
    根本不存在的 text_encoders / vae 取值（实测会被 ComfyUI 以
    value_not_in_list 拒绝）。

    Args:
        template: 候选模板。
        arch: 由文件名推导出的架构。

    Returns:
        是否兼容。
    """
    tpl_arch = template.arch or "generic"
    if tpl_arch == "generic" or arch == "generic":
        return True
    return tpl_arch == arch


def pick_template(
    templates: dict[str, WorkflowTemplate],
    *,
    model_name: str,
    model_folder: str = "checkpoints",
    available_nodes: set[str] | None = None,
    arch_override: str = "",
) -> tuple[WorkflowTemplate | None, str]:
    """按模型所在文件夹、文件名与服务端能力选择模板。

    选择顺序：
    1. 加载方式匹配（checkpoints 用 CheckpointLoaderSimple 系模板，diffusion_models 用 UNETLoader 系）；
    2. 排除「需要而服务端没装的节点」的模板（能力探测）；
    3. 架构精确匹配，其次通用模板。

    Args:
        templates: 可用模板。
        model_name: 选中的模型文件名。
        model_folder: 模型所在文件夹，checkpoints 或 diffusion_models。
        available_nodes: 服务端已安装的节点类名；None 或空集表示探测失败，此时跳过能力过滤。
        arch_override: 用户在配置里强制指定的架构，优先于文件名推断。

    Returns:
        (模板, 架构key)。模板为 None 表示没有任何可用模板。
    """
    if not templates:
        return None, guess_arch(model_name, arch_override)

    arch = guess_arch(model_name, arch_override)
    want_loader = "unet" if model_folder == "diffusion_models" else "checkpoint"

    # 优先在加载方式匹配的模板里选
    pool = [t for t in templates.values() if t.loader == want_loader]
    if not pool:
        pool = list(templates.values())
    # 架构兼容：不允许把 Flux 模板用在 SD 模型上（反之亦然）
    compatible = [t for t in pool if is_compatible(t, arch)]
    if not compatible:
        return None, arch
    pool = compatible
    # 能力过滤：节点齐全的模板优先；若全部缺节点则退回原候选（由调用方给出明确错误）
    usable = [t for t in pool if not t.missing_nodes(available_nodes)]
    candidates = usable or pool
    # 架构精确匹配
    for tpl in candidates:
        if tpl.arch == arch:
            return tpl, arch
    # 通用模板兜底
    for tpl in candidates:
        if not tpl.arch or tpl.arch == "generic":
            return tpl, arch
    return candidates[0], arch
