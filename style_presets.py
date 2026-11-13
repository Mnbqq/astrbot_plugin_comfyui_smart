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
    # 以下 7 个是「画风」取向，与上面「玩法」取向互补（参考 ai-pose-gallery-zh 的画风清单，
    # 名字取自该仓库 README，正文措辞由本项目自己写）。
    "复古胶片": {
        "prompt": (
            "analog photography, 35mm film, kodak portra, film grain, soft contrast, "
            "warm highlights, natural skin texture"
        ),
        "negative": "digital art, 3d render, oversaturated, plastic skin",
    },
    "日韩画报": {
        "prompt": (
            "editorial portrait, magazine photography, beauty lighting, clean makeup, "
            "minimal styling, soft shadow, pastel background"
        ),
        "negative": "harsh shadow, cluttered background, low quality",
    },
    "2.5D": {
        "prompt": (
            "2.5d, semi realistic, ray tracing, soft rim light, detailed shading, "
            "anime proportions with realistic rendering"
        ),
        "negative": "flat colors, rough sketch, pure 2d",
    },
    "动画电影感": {
        "prompt": (
            "anime movie still, cinematic composition, dramatic sky, lens flare, "
            "rich colors, detailed background, film lighting"
        ),
        "negative": "flat lighting, plain background, sketch",
    },
    "黑白漫画": {
        "prompt": (
            "manga, black and white, screentone, ink lineart, high contrast, "
            "panel composition, dramatic hatching"
        ),
        "negative": "color, painting, 3d render, soft gradients",
    },
    "赛博朋克": {
        "prompt": (
            "cyberpunk, neon lights, rainy street, reflections, holographic signage, "
            "teal and magenta lighting, night"
        ),
        "negative": "daylight, plain background, pastel colors",
    },
    "潮玩手办": {
        "prompt": (
            "designer toy, vinyl figure, glossy plastic, chibi proportions, "
            "studio lighting, product photography, clean background"
        ),
        "negative": "photorealistic human, anime screenshot, messy background",
    },
}


# 画面媒介类型（「这是照片还是插画还是 3D」）。
# 与风格预设是**两个维度**：风格管玩法（手办化/证件照），媒介管画面材质。
# 两者可以叠加：`--媒介 水彩 --风格 立绘`。
BUILTIN_MEDIA: dict[str, dict] = {
    "照片": {
        "prompt": "photograph, realistic, 35mm photograph, natural lighting, film grain, sharp focus",
        "negative": "anime, illustration, 3d render, painting",
    },
    "插画": {
        "prompt": "illustration, digital painting, clean lineart, cel shading",
        "negative": "photograph, photorealistic, 3d render",
    },
    "3D": {
        "prompt": "3d render, octane render, subsurface scattering, studio lighting, high detail",
        "negative": "flat illustration, photograph, sketch",
    },
    "像素": {
        "prompt": "pixel art, 16-bit, limited palette, dithering",
        "negative": "smooth gradients, photorealistic, blurry",
    },
    "水墨": {
        "prompt": "ink wash painting, sumi-e, brush strokes, rice paper, monochrome",
        "negative": "photograph, 3d render, digital painting",
    },
}


# 画质档位（可选，默认不加）。刻意**不做成默认行为**：
# 用户原话与反推结果应当原样提交，凭空补质量词会改变他们本来写好的东西。
# 想要就显式 `--画质 高清`。
BUILTIN_QUALITY: dict[str, dict] = {
    "标准": {"prompt": "high detail, sharp focus", "negative": ""},
    "高清": {
        "prompt": "masterpiece, best quality, high detail, sharp focus, intricate details",
        "negative": "blurry, jpeg artifacts",
    },
    "极致": {
        "prompt": (
            "masterpiece, best quality, ultra detailed, absurdres, highres, "
            "intricate details, sharp focus, professional lighting"
        ),
        "negative": "blurry, lowres, jpeg artifacts, noise",
    },
}


# ---------------------------------------------------------------------- #
# 内置预设包（随插件分发）
# ---------------------------------------------------------------------- #
# 370 条手绘风格与 36 个主题色来自 yang0/handraw-style（MIT + 署名要求），
# 详见 presets/NOTICE.md。数据放在 presets/ 下，改数据不用改代码。
_BUNDLE_CACHE: dict[str, dict] = {}


def _load_bundle(filename: str) -> dict[str, dict]:
    """读取 presets/ 下的内置预设包（带缓存，失败返回空表）。

    Args:
        filename: 文件名，如 `styles_handraw.json`。

    Returns:
        `{名称: {"prompt":..., "negative":...}}`；文件缺失或损坏时返回空表，
        出图不该因为预设包坏掉而失败。
    """
    if filename not in _BUNDLE_CACHE:
        import json as _json
        from pathlib import Path as _P

        path = _P(__file__).resolve().parent / "presets" / filename
        try:
            raw = _json.loads(path.read_text(encoding="utf-8"))
            _BUNDLE_CACHE[filename] = {
                str(k): v for k, v in raw.items()
                if isinstance(v, dict) and str(v.get("prompt") or "").strip()
            }
        except (OSError, ValueError):
            _BUNDLE_CACHE[filename] = {}
    return _BUNDLE_CACHE[filename]


def bundled_styles() -> dict[str, dict]:
    """内置手绘风格包（370 条）。"""
    return _load_bundle("styles_handraw.json")


def bundled_colors() -> dict[str, dict]:
    """内置主题色包（36 条）。"""
    return _load_bundle("colors_handraw.json")


def resolve_name(name: str, table: dict) -> str:
    """把用户写的名字解析成表里的键。

    支持三种写法：完整键、风格编号前缀（`FA-001`）、名称片段（`手办`）。
    模糊匹配只做「唯一命中」时才认，避免 `--风格 风` 这种瞎猜。

    Args:
        name: 用户输入。
        table: 候选表。

    Returns:
        命中的键；没命中或命中多个时返回空字符串。
    """
    text = str(name or "").strip()
    if not text:
        return ""
    if text in table:
        return text
    low = text.lower()
    lowered = [k for k in table if k.lower() == low]
    if len(lowered) == 1:
        return lowered[0]
    # 编号前缀：FA-001 → "FA-001 Playful Deadpan Doodle"
    prefixed = [k for k in table if k.split(" ", 1)[0].lower() == low]
    if len(prefixed) == 1:
        return prefixed[0]
    # 名称或**提示词正文**包含（用户更可能记住「水墨」「klein blue」这种特征词，
    # 而不是 `FE-040 Digital Chinese Gongbi Fantasy` 这种全名）
    contained = []
    for key, value in table.items():
        if low in str(key).lower():
            contained.append(key)
            continue
        if isinstance(value, dict) and low in str(value.get("prompt") or "").lower():
            contained.append(key)
    return contained[0] if len(contained) == 1 else ""


# 模板里用来占位「画面主体」的写法。中文文章里常见【画面主体】，
# 也有人写 {subject} / 【主体】—— 都认。
SUBJECT_PLACEHOLDERS = ("【画面主体】", "【主体】", "{subject}", "{{subject}}")


def is_template_prompt(prompt: str) -> bool:
    """这条预设是「带占位符的模板」还是「一串 tag」。

    区别很重要：模板要**替换**掉用户描述的位置（它自己已经包含主体描述），
    tag 只是**追加**在用户描述前面。搞反了就会变成「一只猫，【画面主体】，…」。
    """
    text = str(prompt or "")
    return any(mark in text for mark in SUBJECT_PLACEHOLDERS)


def fill_template(prompt: str, subject: str) -> str:
    """把模板里的占位符替换成用户描述。

    Args:
        prompt: 含占位符的风格模板。
        subject: 用户的原始描述。

    Returns:
        填好主体的完整描述；没有占位符时原样返回模板。
    """
    text = str(prompt or "")
    body = str(subject or "").strip()
    for mark in SUBJECT_PLACEHOLDERS:
        if mark in text:
            text = text.replace(mark, body)
    return text.strip()


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
