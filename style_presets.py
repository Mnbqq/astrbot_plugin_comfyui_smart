"""风格预设与出口安全后缀。

两个概念分开：

- **风格预设**：一句 `--风格 手办化` 就把一组正向/负向词插进本次出图，
  用来「一句话换玩法」。存 `data/style_presets.json`，用户可增删。
- **出口安全后缀**：所有出图在**最后一步**统一追加一组规避词（水印、签名、文字…），
  幂等 —— 已经在负面词里的不会重复加。这样一个地方就能管全部预设与全部入口，
  不用去改每一条预设。

内置预设的措辞由本项目自己撰写，不复制第三方仓库文案（那批仓库多为 AGPL / 未声明授权）。
"""

# 出口统一追加的规避词。挑的是「不管画什么都该避免」的那一类。
SAFE_SUFFIX = "watermark, signature, text, username, logo, web address"

# 内置风格预设。`prompt` 会被放到正向词**最前面**（越靠前权重越大），
# `negative` 会并入负面词。
BUILTIN_PRESETS: dict[str, dict] = {
    "手办化": {
        "prompt": (
            "collectible figure, 1/7 scale, pvc figure, product photography, acrylic base, "
            "clear display case, studio lighting, sharp focus, high detail"
        ),
        "negative": "flat illustration, 2d, sketch, cel shading",
    },
    "立绘": {
        "prompt": (
            "full body, standing, character sheet, simple background, white background, "
            "front view, clean lineart, official art"
        ),
        "negative": "crowded background, scenery",
    },
    "证件照": {
        "prompt": (
            "id photo, portrait, front view, looking at viewer, neutral expression, "
            "plain white background, even lighting, upper body"
        ),
        "negative": "scenery, dynamic pose, full body",
    },
    "游戏CG": {
        "prompt": (
            "key visual, official art, cinematic composition, dramatic lighting, "
            "detailed background, high production value"
        ),
        "negative": "rough sketch, flat colors",
    },
    "水彩": {
        "prompt": (
            "watercolor, traditional media, wet on wet, soft edges, paper texture, pastel colors"
        ),
        "negative": "photorealistic, 3d render",
    },
}


def normalize_preset(raw) -> dict | None:
    """把用户写的预设整理成 `{"prompt": str, "negative": str}`。

    容忍三种写法：纯字符串（只当正向词）、`{"prompt": ...}`、以及带 negative 的完整形式。

    Args:
        raw: 预设原始值。

    Returns:
        规范化后的预设；无法解析（空、类型不对、prompt 为空）时返回 None。
    """
    if isinstance(raw, str):
        text = raw.strip()
        return {"prompt": text, "negative": ""} if text else None
    if not isinstance(raw, dict):
        return None
    prompt = str(raw.get("prompt") or "").strip()
    if not prompt:
        return None
    return {"prompt": prompt, "negative": str(raw.get("negative") or "").strip()}


def normalize_all(data) -> dict[str, dict]:
    """把整份预设表规范化，丢掉解析不了的那些。"""
    out: dict[str, dict] = {}
    for name, raw in (data or {}).items():
        key = str(name).strip()
        if not key:
            continue
        preset = normalize_preset(raw)
        if preset:
            out[key] = preset
    return out


def _tags(text: str) -> list[str]:
    return [t.strip() for t in str(text or "").replace("，", ",").split(",") if t.strip()]


def ensure_safe_suffix(negative: str, suffix: str = SAFE_SUFFIX) -> str:
    """把安全后缀**幂等**并入负面词。

    幂等的意义：出图链路上有多个地方会拼负面词（配置默认词、架构附加词、LLM 词、预设词、
    行内 `--negative`），如果每处都加一遍，负面词会越来越长、稀释权重。
    这里只补「还没有的」，所以可以放心在出口调一次。

    Args:
        negative: 已拼好的负面词。
        suffix: 要保证出现的规避词（逗号分隔）。

    Returns:
        合并后的负面词；原样保留已有顺序，缺的追加在末尾。
    """
    have = {t.lower() for t in _tags(negative)}
    extra = [t for t in _tags(suffix) if t.lower() not in have]
    if not extra:
        return str(negative or "").strip()
    base = str(negative or "").strip().rstrip(",")
    return f"{base}, {', '.join(extra)}" if base else ", ".join(extra)
