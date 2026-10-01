"""模型资产巡检与运行环境体检。

设计原则：**只用插件已经拿到的数据**（模型清单、统计、模板描述、节点可用性、
/system_stats 返回值），因此不假设 AstrBot 与 ComfyUI 在同一台机器，也不需要文件系统权限。

两类检查：

- `inspect_models()`：模型资产（重名重复、放错目录、长时间没用、体积——体积只有同机可读时才有）
- `health_report()`：运行环境（离线、启动参数、显存内存余量、档位与时长冲突、
  **模板依赖的节点与权重文件是否齐全**）

`collect_requirements()` 把模板里硬编码的权重名与节点类型抽出来，交给
`check_requirements()` 与真实清单比对 —— 真机踩过的两类坑（升级后缺节点、
模板写着 fp8 的 umt5 而本地只有 GGUF 量化版）都能被它提前发现。
"""
from __future__ import annotations

from typing import Any

SEVERITY_ORDER = {"error": 0, "warn": 1, "info": 2}

# 节点类型 -> (在清单里的目录, 输入键名)：用于核对模板引用的权重是否真的存在
REQUIREMENT_NODES: dict[str, tuple[str, str]] = {
    "CheckpointLoaderSimple": ("checkpoints", "ckpt_name"),
    "CheckpointLoader": ("checkpoints", "ckpt_name"),
    "UNETLoader": ("diffusion_models", "unet_name"),
    "UnetLoaderGGUF": ("unet_gguf", "unet_name"),
    "VAELoader": ("vae", "vae_name"),
    "CLIPLoader": ("text_encoders", "clip_name"),
    "CLIPLoaderGGUF": ("clip_gguf", "clip_name"),
    "DualCLIPLoader": ("text_encoders", "clip_name1"),
    "DualCLIPLoaderGGUF": ("clip_gguf", "clip_name1"),
    "LoraLoader": ("loras", "lora_name"),
    "ControlNetLoader": ("controlnet", "control_net_name"),
    "UpscaleModelLoader": ("upscale_models", "model_name"),
}

# 文件名特征：用来识别「放错目录」的权重
_TEXT_ENCODER_HINTS = ("t5xxl", "umt5", "clip_l", "clip_g", "text_encoder")
# 「自带 VAE 的底模」名字里也有 vae，不能当独立 VAE 判（真机踩过：4 条误报）
_BAKED_HINTS = ("baked", "clipfix", "allinone", "vae_fix", "vaefix")
# 独立 VAE 的常见命名：vae / vae-ft-xxx / kl-f8-xxx / sdxl_vae / ae / *_vae
_VAE_PREFIXES = ("vae", "kl-f8", "sdxl_vae", "sdxl-vae", "ae.")
_VAE_SUFFIXES = ("_vae", "-vae", ".vae", "vae_ft", "vae-ft")
_CONTROLNET_HINTS = ("controlnet", "control_", "depth", "canny", "openpose")
_UPSCALE_HINTS = ("esrgan", "ultrasharp", "4x", "upscale")

# 看名字就知道是「占位/示例」的值，不参与依赖核对
_PLACEHOLDER_HINTS = ("astrbot/", "example", "put_", "这里")

# ComfyUI 里 GGUF 与 safetensors 常常映射到同一批目录（真机实测：
# unet_gguf → models/unet + models/diffusion_models，clip_gguf → models/text_encoders + models/clip），
# 因此核对依赖时要按「别名组」取并集，否则会误报「文件不在这个目录」。
POOL_ALIASES: tuple[tuple[str, ...], ...] = (
    ("unet_gguf", "diffusion_models", "unet"),
    ("clip_gguf", "text_encoders", "clip"),
)


def _basename(value: Any) -> str:
    """取路径里的文件名（ComfyUI 有时给相对路径）。"""
    return str(value or "").replace("\\", "/").split("/")[-1].strip()


def _stem(name: str) -> str:
    """去掉扩展名，便于判定「同一个模型的两份拷贝」。"""
    base = _basename(name).lower()
    for ext in (".safetensors", ".gguf", ".ckpt", ".pt", ".pth", ".bin", ".sft"):
        if base.endswith(ext):
            return base[: -len(ext)]
    return base


def _looks_like_vae(low_name: str) -> bool:
    """判断文件名是否像「独立 VAE」（自带 VAE 的底模会被排除）。

    Args:
        low_name: 小写文件名（含扩展名）。

    Returns:
        True 表示像独立 VAE。
    """
    base = low_name.replace("\\", "/").split("/")[-1]
    if any(h in base for h in _BAKED_HINTS):
        return False
    stem = base.rsplit(".", 1)[0] if "." in base else base
    if stem in ("vae", "ae"):
        return True
    if stem.startswith(_VAE_PREFIXES):
        return True
    return stem.endswith(_VAE_SUFFIXES)


def find_duplicates(catalog: dict[str, list[str]]) -> list[dict]:
    """跨目录找同一个模型（同文件名或同主干名）。

    Args:
        catalog: {目录: [文件名...]}。

    Returns:
        每条 {"stem": 主干名, "files": [{"folder","name"}...]}，按目录数从多到少排序。
    """
    by_stem: dict[str, list[dict]] = {}
    for folder, files in (catalog or {}).items():
        for name in files or []:
            key = _stem(name)
            if not key:
                continue
            by_stem.setdefault(key, []).append({"folder": folder, "name": name})
    out = []
    for key, items in by_stem.items():
        folders = {i["folder"] for i in items}
        if len(folders) > 1:
            out.append({"stem": key, "files": items})
    out.sort(key=lambda x: (-len({i["folder"] for i in x["files"]}), x["stem"]))
    return out


def find_misplaced(catalog: dict[str, list[str]]) -> list[dict]:
    """找「放错目录」的权重：VAE / 文本编码器 / ControlNet / 放大模型 混在底模目录里。

    Args:
        catalog: {目录: [文件名...]}。

    Returns:
        每条 {"name", "folder", "suggest", "why"}。
    """
    out: list[dict] = []
    for folder, files in (catalog or {}).items():
        for name in files or []:
            low = _basename(name).lower()
            if folder == "checkpoints":
                if _looks_like_vae(low):
                    out.append({"name": name, "folder": folder, "suggest": "vae",
                                "why": "名字像 VAE，放在 checkpoints 里不会被 VAELoader 列出来"})
                elif any(h in low for h in _CONTROLNET_HINTS):
                    out.append({"name": name, "folder": folder, "suggest": "controlnet",
                                "why": "名字像 ControlNet，放在 checkpoints 里没法用 ControlNetLoader 加载"})
                elif any(h in low for h in _UPSCALE_HINTS):
                    out.append({"name": name, "folder": folder, "suggest": "upscale_models",
                                "why": "名字像放大模型，放在 checkpoints 里没意义"})
                elif any(h in low for h in _TEXT_ENCODER_HINTS):
                    out.append({"name": name, "folder": folder, "suggest": "text_encoders",
                                "why": "名字像文本编码器，放在 checkpoints 里不会被 CLIPLoader 列出来"})
                elif low.endswith(".gguf"):
                    out.append({"name": name, "folder": folder, "suggest": "diffusion_models",
                                "why": "GGUF 权重放在 checkpoints 里，UNETLoader/UnetLoaderGGUF 都看不到"})
    return out


def find_unused(catalog: dict[str, list[str]], stats: dict | None, *, limit: int = 15) -> list[dict]:
    """找统计里从未用过的底模（只在有统计时给结论）。

    Args:
        catalog: {目录: [文件名...]}。
        stats: plugin.storage.load_stats() 的结果。
        limit: 最多列出多少个。

    Returns:
        每条 {"name", "folder"}。
    """
    used = set()
    for key in ("checkpoint", "lora", "vae", "controlnet"):
        used |= {_stem(k) for k in ((stats or {}).get("model_usage", {}) or {}).get(key, {})}
    if not used:
        return []
    out = []
    for folder in ("checkpoints", "diffusion_models", "unet_gguf", "loras", "vae"):
        for name in (catalog or {}).get(folder) or []:
            if _stem(name) not in used:
                out.append({"name": name, "folder": folder})
    out.sort(key=lambda x: (x["folder"], x["name"]))
    return out[:limit]


def inspect_models(
    catalog: dict[str, list[str]],
    *,
    stats: dict | None = None,
    sizes: dict[str, int] | None = None,
) -> dict:
    """模型资产巡检汇总。

    Args:
        catalog: {目录: [文件名...]}。
        stats: 统计信息（用于「从未用过」）。
        sizes: 可选的 {绝对路径或文件名: 字节数}；同机可读时由调用方提供。

    Returns:
        {"findings": [{"severity","category","message","suggestion"}...], "summary": {...}}
    """
    findings: list[dict] = []
    duplicates = find_duplicates(catalog)
    for item in duplicates[:10]:
        spots = "、".join(f"{i['folder']}/{i['name']}" for i in item["files"])
        findings.append({
            "severity": "info",
            "category": "duplicate",
            "message": f"同名模型出现在多个目录：{spots}",
            "suggestion": "确认哪一份是插件会用到的，其余可以挪走省磁盘",
        })
    for item in find_misplaced(catalog):
        findings.append({
            "severity": "warn",
            "category": "misplaced",
            "message": f"{item['folder']}/{item['name']}：{item['why']}",
            "suggestion": f"挪到 models/{item['suggest']}/",
        })
    unused = find_unused(catalog, stats)
    if unused:
        names = "、".join(f"{i['folder']}/{i['name']}" for i in unused[:8])
        findings.append({
            "severity": "info",
            "category": "unused",
            "message": f"从未在统计里出现过、且是底模类的模型（前 {len(unused)} 个）：{names}",
            "suggestion": "长期不用的可以挪到别的盘，插件不会因此出错",
        })
    if sizes:
        big = sorted(sizes.items(), key=lambda kv: -kv[1])[:5]
        total = sum(sizes.values())
        findings.append({
            "severity": "info",
            "category": "size",
            "message": "最大的 5 个权重：" + "、".join(f"{_basename(k)} {v / 1024 ** 3:.1f}GB" for k, v in big),
            "suggestion": f"这些加起来 {total / 1024 ** 3:.1f} GB，磁盘紧张时优先考虑",
        })
    else:
        findings.append({
            "severity": "info",
            "category": "size",
            "message": "拿不到权重体积（AstrBot 与 ComfyUI 不同机时正常）",
            "suggestion": "想按体积清理，可在 ComfyUI 那台机器上直接看 models 目录",
        })
    summary = {
        "pools": len(catalog or {}),
        "files": sum(len(v or []) for v in (catalog or {}).values()),
        "duplicates": len(duplicates),
        "misplaced": len(find_misplaced(catalog)),
        "unused": len(unused),
    }
    findings.sort(key=lambda f: SEVERITY_ORDER.get(f["severity"], 9))
    return {"findings": findings, "summary": summary}


def collect_requirements(templates: dict[str, Any]) -> list[dict]:
    """把模板里硬编码的权重名与用到的节点类型抽出来。

    Args:
        templates: {名字: 模板对象}（模板需提供 .graph；.required_nodes() 有则更好）。

    Returns:
        权重核对项列表：{"template", "node_type", "input", "value", "pool"}。
    """
    out: list[dict] = []
    for name, tpl in (templates or {}).items():
        for node in (getattr(tpl, "graph", {}) or {}).values():
            class_type = node.get("class_type")
            spec = REQUIREMENT_NODES.get(class_type)
            if not spec:
                continue
            pool, key = spec
            value = (node.get("inputs") or {}).get(key)
            if not value or not isinstance(value, str):
                continue
            if any(h in value.lower() for h in _PLACEHOLDER_HINTS):
                continue
            out.append({"template": name, "node_type": class_type, "input": key,
                        "value": value, "pool": pool})
    return out


def _pools_of(pool: str) -> tuple[str, ...]:
    """返回该目录及其别名目录（同映射时文件在任何一边都算存在）。"""
    for group in POOL_ALIASES:
        if pool in group:
            return group
    return (pool,)


def check_requirements(
    requirements: list[dict],
    catalog: dict[str, list[str]],
    available_nodes: set[str] | None = None,
) -> list[dict]:
    """核对模板依赖：节点是否存在、权重文件是否在清单里。

    Args:
        requirements: collect_requirements() 的结果。
        catalog: 真实模型清单 {目录: [文件名...]}。
        available_nodes: ComfyUI 当前可用的节点类型集合；None 表示不检查节点。

    Returns:
        findings 列表（error 表示这条模板现在根本跑不起来）。
    """
    findings: list[dict] = []
    checked_nodes: set[str] = set()
    missing_files: dict[tuple[str, str], list[str]] = {}
    for item in requirements:
        node_type = item["node_type"]
        if available_nodes is not None and node_type not in checked_nodes:
            checked_nodes.add(node_type)
            if node_type not in available_nodes:
                findings.append({
                    "severity": "error",
                    "category": "missing_node",
                    "message": f"ComfyUI 里没有节点 {node_type}（模板 {item['template']} 需要）",
                    "suggestion": "装回对应自定义节点（如 ComfyUI-GGUF），或换用不需要它的模板",
                })
        pool_files = []
        for pool_name in _pools_of(item["pool"]):
            pool_files += [_basename(x) for x in (catalog or {}).get(pool_name) or []]
        if _basename(item["value"]) not in pool_files:
            missing_files.setdefault((item["value"], item["pool"]), []).append(item["template"])
    for (value, pool), templates in missing_files.items():
        findings.append({
            "severity": "warn",
            "category": "missing_model",
            "message": f"模板引用的权重不在 models/{pool}/：{value}（模板：{'、'.join(sorted(set(templates)))}）",
            "suggestion": "命令里用 --model/--vae 指定已有文件即可；想用模板默认值就补下这个权重",
        })
    return findings


def health_report(
    *,
    status: dict,
    catalog: dict[str, list[str]] | None = None,
    findings: list[dict] | None = None,
    tier: str = "",
    video_max_seconds: int = 0,
    config: dict | None = None,
) -> dict:
    """运行环境体检。

    Args:
        status: plugin.get_server_status() 的结果（含 online/device/vram_*/queue/argv/templates）。
        catalog: 真实模型清单（可选）。
        findings: 模板依赖核对结果（check_requirements 的输出 + 节点检查）。
        tier: 机器档位（low/mid/high）。
        video_max_seconds: 配置里的视频时长上限。
        config: 插件配置（用于判断是否开了 free_before_switch 等）。

    Returns:
        {"findings": [...], "summary": {...}, "ok": bool}
    """
    out: list[dict] = list(findings or [])
    if not status.get("online"):
        out.append({
            "severity": "error",
            "category": "offline",
            "message": f"ComfyUI 连不上（{status.get('base_url')}）：{status.get('error') or '无响应'}",
            "suggestion": "确认 ComfyUI 已启动、地址与防火墙正确；重启机器后 ComfyUI 不会自动起来，需要手动或做计划任务",
        })
    argv = [str(a) for a in (status.get("argv") or [])]
    ram_gb = float(status.get("ram_total") or 0) / 1024 ** 3
    if argv and "--cache-none" not in argv and 0 < ram_gb <= 20:
        out.append({
            "severity": "warn",
            "category": "flags",
            "message": f"ComfyUI 没加 --cache-none（本机内存 {ram_gb:.0f}G）",
            "suggestion": "内存小的机器建议加 --cache-none：出完图即卸载，避免切换大模型时报 os error 1455",
        })
    vram_free = float(status.get("vram_free") or 0) / 1024 ** 3
    ram_free = float(status.get("ram_free") or 0) / 1024 ** 3
    if status.get("online") and vram_free and vram_free < 1.0:
        out.append({
            "severity": "info",
            "category": "memory",
            "message": f"显存只剩 {vram_free:.2f} GB，下一个任务可能要等卸载",
            "suggestion": "正常现象（--cache-none + 换模型前 /free）；持续不足就降分辨率或换更小的模型",
        })
    if tier == "low" and video_max_seconds and video_max_seconds > 6:
        out.append({
            "severity": "warn",
            "category": "tier",
            "message": f"低配档把视频上限放到 {video_max_seconds} 秒，8G 显存上很容易跑一半崩掉",
            "suggestion": "建议 video.max_seconds ≤ 6，或用蒸馏模型（Turbo）少步出片",
        })
    if not (status.get("templates") or []):
        out.append({
            "severity": "error",
            "category": "templates",
            "message": "一个工作流模板都没加载到",
            "suggestion": "确认 workflows/ 目录里有 JSON（插件自带 13 个）；自定义模板放对位置",
        })
    conf = config or {}
    if status.get("online") and not bool((conf.get("server") or {}).get("free_before_switch", True)):
        out.append({
            "severity": "warn",
            "category": "flags",
            "message": "「换大模型前先卸载」被关掉了",
            "suggestion": "16G 内存的机器开着更稳（server.free_before_switch）",
        })
    errors = [f for f in out if f.get("severity") == "error"]
    out.sort(key=lambda f: SEVERITY_ORDER.get(f.get("severity"), 9))
    return {
        "findings": out,
        "ok": not errors,
        "summary": {
            "online": bool(status.get("online")),
            "device": status.get("device", ""),
            "version": status.get("version", ""),
            "vram_free_gb": round(vram_free, 2),
            "ram_free_gb": round(ram_free, 2),
            "tier": tier,
            "templates": len(status.get("templates") or []),
            "errors": len(errors),
            "warnings": len([f for f in out if f.get("severity") == "warn"]),
            "models": sum(len(v or []) for v in (catalog or {}).values()),
        },
    }
