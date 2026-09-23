"""LLM 服务：把中文描述转成提示词，并从真实模型清单里选型。

与旧版的区别：
- `llm_generate` 的 `chat_provider_id` 是必填 keyword-only 参数，旧版那个「不传 provider 的回退」
  必然抛 TypeError 并被 except 吞成 "LLM 调用失败"。这里改为显式解析 provider，解析不到就给出
  明确提示（含可用 provider 列表）。
- 使用官方 `system_prompt=` 参数，而不是把 system 拼进 user。
- 删除了「把几百个模型名分批喂给 LLM 生成 style/tags」的空转链路：那条链路耗时长、烧 token，
  而产出从未被出图流程使用。改为本地启发式标注（架构/风格猜测），只把精简后的候选清单交给 LLM。
"""
from __future__ import annotations

import json
import re

from .workflow_templates import guess_arch

# 提示词模板：要求只输出 JSON，避免解析歧义
PROMPT_SYSTEM = (
    "你是 ComfyUI 绘图提示词工程师。把用户的中文描述改写成高质量英文提示词，"
    "并从给定的可用模型清单里挑选最合适的模型。\n"
    "规则：\n"
    "1. 只使用清单里**原样出现**的模型文件名，不要编造、不要改写大小写或路径。\n"
    "2. 不确定 LoRA 是否合适时，lora 字段留空字符串。\n"
    "3. 提示词用英文逗号分隔的 tag 风格，不要加入解释性文字。\n"
    "4. negative（负面提示词）必须包含手部与肢体畸形的常用规避词，"
    "至少覆盖：bad hands、extra fingers、fewer fingers、fused fingers、"
    "extra digits、missing fingers、mutated hands、malformed limbs、bad anatomy。\n"
    "5. 正向提示词要写清手部状态（例如 hands on hips、holding a cup、arms crossed），"
    "让人物手部有明确动作可画，这比事后加负面词更有效。\n"
    "6. 严格只输出一个 JSON 对象，不要 markdown 代码块，不要任何其他文字。\n"
    '输出格式：{"positive": "...", "negative": "...", "checkpoint": "...", '
    '"lora": "...", "lora_strength": 1.0, "vae": "", "width": 0, "height": 0}'
)
# positive/negative 之外的可选覆盖字段：宽高为 0 表示沿用架构默认
OPTIONAL_KEYS = ("positive", "negative", "checkpoint", "lora", "vae")
MAX_CHECKPOINTS = 60
MAX_LORAS = 80
MAX_OTHERS = 40


def _to_data_url(ref: str) -> str:
    """把本地图片路径或 http 地址转成 OpenAI 多模态可用的 data URL。

    Args:
        ref: 本地路径或 http(s) 地址。

    Returns:
        data URL；无法处理时返回空字符串（由调用方跳过）。
    """
    import base64
    import mimetypes
    from pathlib import Path

    text = str(ref or "").strip()
    if not text:
        return ""
    if text.startswith(("http://", "https://", "data:")):
        return text
    path = Path(text)
    try:
        raw = path.read_bytes()
    except OSError:
        return ""
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


class LLMService:
    """LLM 调用与提示词优化。"""

    def __init__(self, context, config: dict):
        """初始化。

        Args:
            context: AstrBot Star Context。
            config: 插件配置。
        """
        self.context = context
        self.config = config or {}
        self.llm_conf = self.config.get("llm_settings", {}) or {}
        # 反推专用的看图模型配置（结构同 llm_settings）
        self.vision_conf = self.config.get("vision_settings", {}) or {}

    # ------------------------------------------------------------------ #
    # provider 解析
    # ------------------------------------------------------------------ #
    def _has_custom_endpoint(self) -> bool:
        """是否配置了独立于 AstrBot 的 OpenAI 兼容端点。"""
        return bool(str(self.llm_conf.get("base_url") or "").strip())

    def available_providers(self) -> list[str]:
        """返回可用于文本生成的 provider id 列表，供配置提示与报错使用。

        Returns:
            provider id 列表。
        """
        try:
            return [
                p.meta().id
                for p in self.context.get_all_providers()
                if getattr(p, "meta", None)
            ]
        except Exception:
            return []

    def provider_vision_support(self, provider_id: str) -> bool | None:
        """判断某个 provider 是否支持看图。

        AstrBot 依据 `provider.provider_config["modalities"]` 决定是否保留图片；
        若该列表存在但不含 "image"，图片会被替换成字面量 "[Image]" 再发给模型 ——
        模型于是看不到图却照常"编"出一段描述。这正是「反推结果全错」的典型成因，
        所以这里必须提前判断，而不是把编出来的结果当反推结果返回。

        Args:
            provider_id: provider id。

        Returns:
            True 支持 / False 不支持 / None 无法判断（未配置 modalities，AstrBot 按支持处理）。
        """
        try:
            provider = self.context.get_provider_by_id(provider_id)
        except Exception:
            return None
        config = getattr(provider, "provider_config", None)
        if not isinstance(config, dict):
            return None
        modalities = config.get("modalities")
        if not modalities or not isinstance(modalities, list):
            return None
        return "image" in modalities

    def provider_label(self, provider_id: str) -> str:
        """返回便于排查的 provider 描述（id + 模型名）。"""
        try:
            provider = self.context.get_provider_by_id(provider_id)
        except Exception:
            return provider_id
        model = ""
        try:
            model = str(provider.meta().model or "")
        except Exception:
            model = ""
        return f"{provider_id}（{model}）" if model else str(provider_id)

    async def resolve_provider_id(self, event=None) -> str:
        """解析要使用的 provider id。

        优先级：配置里指定的 provider > 当前会话所属 provider > 默认 provider。

        Args:
            event: 可选的 AstrBot 消息事件，用于取会话级模型偏好。

        Returns:
            provider id；解析不到时返回空字符串。
        """
        configured = str(self.llm_conf.get("provider") or "").strip()
        if configured:
            try:
                if self.context.get_provider_by_id(configured) is not None:
                    return configured
            except Exception:
                pass

        umo = getattr(event, "unified_msg_origin", None)
        for candidate in (umo, None):
            try:
                provider_id = await self.context.get_current_chat_provider_id(
                    umo=candidate
                )
                if provider_id:
                    return str(provider_id)
            except Exception:
                continue
        return ""

    # ------------------------------------------------------------------ #
    # 文本生成
    # ------------------------------------------------------------------ #
    async def generate(
        self,
        system: str,
        user: str,
        event=None,
        image_urls: list[str] | None = None,
        provider_id: str = "",
        custom_conf: dict | None = None,
    ) -> str:
        """调用 LLM 生成文本（可选带图，用于看图反推）。

        Args:
            system: 系统提示词。
            user: 用户内容。
            event: 可选消息事件，用于会话级 provider 解析。
            image_urls: 图片引用列表（本地路径 / http / base64:// / data:），
                由 AstrBot 的 MediaResolver 统一处理。
            provider_id: 强制指定 provider（用于反推时指定看图模型）。
            custom_conf: 用这组配置走自定义 OpenAI 兼容端点（反推使用 vision_settings 时传入）。

        Returns:
            生成的纯文本。

        Raises:
            RuntimeError: 没有可用 LLM 或调用失败，message 面向用户。
        """
        if isinstance(custom_conf, dict) and str(custom_conf.get("base_url") or "").strip():
            return await self._call_custom(
                system, user, image_urls=image_urls, conf=custom_conf
            )
        if self._has_custom_endpoint():
            return await self._call_custom(system, user, image_urls=image_urls)

        provider_id = provider_id or await self.resolve_provider_id(event)
        if not provider_id:
            names = "、".join(self.available_providers()) or "（当前没有任何 LLM 提供商）"
            raise RuntimeError(
                f"没有可用的 LLM：请在 AstrBot 中配置对话模型，"
                f"或在插件配置的 llm_settings.provider 里指定。当前可用：{names}"
            )
        if image_urls and not custom_conf:
            vision = self.provider_vision_support(provider_id)
            if vision is False:
                raise RuntimeError(
                    f"当前使用的模型不支持看图（{self.provider_label(provider_id)}），"
                    f"无法反推。请在配置页「反推专用模型」里指定一个支持看图的提供商或自定义接口，"
                    f"在 AstrBot 的「服务提供商」里为该提供商勾选 image 模态，"
                    f"也可临时用 --provider <id> 指定"
                )
        try:
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=user,
                system_prompt=system,
                image_urls=list(image_urls) if image_urls else None,
            )
        except Exception as e:
            hint = ""
            if image_urls:
                # 看图失败最常见的原因是所用模型不支持图片输入
                hint = "（如果这是看图反推，请确认所用对话模型支持图片输入）"
            raise RuntimeError(f"调用 LLM 失败（{provider_id}）：{e}{hint}") from e

        text = getattr(resp, "completion_text", None)
        return (text or "").strip()

    async def _call_custom(
        self,
        system: str,
        user: str,
        image_urls: list[str] | None = None,
        conf: dict | None = None,
    ) -> str:
        """调用配置里的 OpenAI 兼容端点。

        Args:
            system: 系统提示词。
            user: 用户内容。
            image_urls: 可选的图片引用列表（本地路径或 http URL），会按
                OpenAI 多模态格式编码进 user 消息。
            conf: 覆盖用的配置（反推使用 vision_settings 时可传入）。

        Returns:
            生成的纯文本。

        Raises:
            RuntimeError: 请求失败。
        """
        import aiohttp

        source = conf if isinstance(conf, dict) else self.llm_conf
        base = str(source.get("base_url") or "").rstrip("/")
        model = str(source.get("model") or "").strip() or "gpt-4o-mini"
        api_key = str(source.get("api_key") or "").strip()
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        user_content: object = user
        if image_urls:
            user_content = [{"type": "text", "text": user}]
            for ref in image_urls:
                data_url = _to_data_url(ref)
                if data_url:
                    user_content.append(
                        {"type": "image_url", "image_url": {"url": data_url}}
                    )
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0.3,
        }
        url = f"{base}/chat/completions" if base.endswith("/v1") else f"{base}/v1/chat/completions"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, headers=headers) as resp:
                    text = await resp.text()
                    if resp.status >= 400:
                        raise RuntimeError(
                            f"自定义 LLM 返回 {resp.status}：{text[:200]}"
                        )
                    data = json.loads(text)
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"调用自定义 LLM 失败（{url}）：{e}") from e

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise RuntimeError(f"自定义 LLM 返回结构异常：{e}") from e
        return (content or "").strip()

    # ------------------------------------------------------------------ #
    # 提示词优化与选型
    # ------------------------------------------------------------------ #
    @staticmethod
    def build_catalog(catalog: dict[str, list[str]]) -> str:
        """把真实模型清单压缩成给 LLM 看的候选文本。

        只列文件名，checkpoint 额外标注本地启发式猜出的架构，帮助 LLM 正确选型。
        超长的清单会被截断，避免提示词爆炸。

        Args:
            catalog: {文件夹: [文件名...]}。

        Returns:
            多行文本。
        """
        lines: list[str] = []
        checkpoints = catalog.get("checkpoints") or []
        unets = catalog.get("diffusion_models") or []
        loras = catalog.get("loras") or []
        vaes = catalog.get("vae") or []

        if checkpoints:
            lines.append("【checkpoints（单文件底模，可被直接选用）】")
            for name in checkpoints[:MAX_CHECKPOINTS]:
                lines.append(f"- {name}  [架构: {guess_arch(name)}]")
            if len(checkpoints) > MAX_CHECKPOINTS:
                lines.append(f"… 另有 {len(checkpoints) - MAX_CHECKPOINTS} 个未列出")
        if unets:
            lines.append("【diffusion_models（分离权重，需配套 text_encoders）】")
            for name in unets[:MAX_OTHERS]:
                lines.append(f"- {name}  [架构: {guess_arch(name)}]")
        if loras:
            lines.append("【loras（可选，最多选 1 个）】")
            for name in loras[:MAX_LORAS]:
                lines.append(f"- {name}")
            if len(loras) > MAX_LORAS:
                lines.append(f"… 另有 {len(loras) - MAX_LORAS} 个未列出")
        if vaes:
            lines.append("【vae（可选，仅当需要独立 VAE 时才选）】")
            for name in vaes[:MAX_OTHERS]:
                lines.append(f"- {name}")
        return "\n".join(lines) if lines else "（ComfyUI 里没有发现任何可用模型）"

    async def reverse_prompt(
        self,
        image_refs: list[str],
        *,
        hint: str = "",
        event=None,
        provider_id: str = "",
    ) -> dict:
        """看图反推提示词。

        Args:
            image_refs: 图片引用列表（本地路径）。
            hint: 用户附加的要求，例如「只要人物特征」。
            event: 可选消息事件。
            provider_id: 指定用哪个 provider 看图（留空则用配置或会话默认）。

        Returns:
            {"positive": str, "negative": str, "summary": str, "raw_ok": bool,
             "model": str}

        Raises:
            RuntimeError: LLM 不可用或调用失败。
        """
        user = "请看这张图，反推出可直接用于 Stable Diffusion 的提示词。"
        if hint.strip():
            user += f"\n额外要求：{hint.strip()}"

        # 看图模型的解析顺序：
        #   行内 --provider > 独立的 vision_settings（自定义接口 / AstrBot 提供商）
        #   > 旧的 llm_settings.vision_provider（兼容） > 当前会话模型
        vision = self.vision_conf
        custom_conf = None
        chosen = provider_id or str(vision.get("provider") or "").strip() \
            or str(self.llm_conf.get("vision_provider") or "").strip()

        if not provider_id and str(vision.get("base_url") or "").strip():
            # 配了独立接口就用它（可自带 api_key / model）
            custom_conf = vision
            chosen = ""
        if chosen and self.context.get_provider_by_id(chosen) is None:
            raise RuntimeError(
                f"配置里指定的看图模型（{chosen}）不存在，请在 AstrBot 的「服务提供商」里核对 ID"
            )

        text = await self.generate(
            REVERSE_SYSTEM,
            user,
            event=event,
            image_urls=image_refs,
            provider_id=chosen,
            custom_conf=custom_conf,
        )
        result = parse_reverse_result(text)
        if custom_conf is not None:
            result["model"] = (
                f"自定义接口（{custom_conf.get('model') or '未填模型名'}）"
            )
        else:
            result["model"] = self.provider_label(
                chosen or await self.resolve_provider_id(event)
            )
        return result

    async def optimize_prompt(
        self,
        user_desc: str,
        catalog: dict[str, list[str]],
        defaults: dict | None = None,
        *,
        event=None,
    ) -> dict:
        """把中文描述转成提示词并选型。

        Args:
            user_desc: 用户的中文描述。
            catalog: 真实模型清单 {文件夹: [文件名...]}。
            defaults: 默认参数（负面词/宽高），用于填充缺省。
            event: 可选消息事件。

        Returns:
            规范化后的结果字典，含 positive / negative / checkpoint / lora /
            lora_strength / vae / width / height，以及 raw_ok 标记解析是否成功。

        Raises:
            RuntimeError: LLM 不可用或调用失败。
        """
        defaults = defaults or {}
        catalog_text = self.build_catalog(catalog)
        user = (
            f"用户描述：{user_desc}\n\n"
            f"默认负面提示词：{defaults.get('negative') or '（无）'}\n"
            "尺寸说明：插件会按所选底模的架构自动决定分辨率，你只需在需要时用 "
            "width/height 表达**画面比例意图**（例如横构图 1344x768、竖构图 768x1344），"
            "不确定就都填 0。\n\n"
            f"可用模型清单：\n{catalog_text}"
        )
        text = await self.generate(PROMPT_SYSTEM, user, event=event)
        parsed = parse_optimize_result(text)
        parsed["_raw"] = text
        return parsed


# 反推提示词的系统提示词
REVERSE_SYSTEM = (
    "你是动画/插画提示词反推助手。看图片，输出可直接用于 Stable Diffusion 的英文 tag 提示词。\n"
    "规则：\n"
    "1. 只描述**画面里真实存在**的内容，看不清的服装纹样、配饰不要编造。\n"
    "2. positive 用英文逗号分隔的 Danbooru 风格 tag，按这个顺序组织：\n"
    "   人物数量（solo / 1girl / 2girls）→ 外貌（发色发型、瞳色）→ 服装 → "
    "动作与**手部状态**（如 hands on hips、holding a cup）→ 视角与构图（from above、"
    "upper body、full body）→ 场景背景 → 光线氛围 → 画风（anime style、watercolor 等）。\n"
    "3. negative 给出 8~15 个与画面缺陷相关的常用规避词"
    "（含手部与肢体：bad hands、extra fingers、fused fingers、missing fingers、malformed limbs）。\n"
    "4. summary 用一句中文概括画面。\n"
    "5. 严格只输出一个 JSON 对象，不要 markdown 代码块，不要任何其他文字。\n"
    '格式：{"positive": "...", "negative": "...", "summary": "..."}'
)


def _load_json_object(text: str) -> dict | None:
    """从 LLM 输出里容错提取一个 JSON 对象。

    Args:
        text: LLM 原始输出，可能带 markdown 代码块或前后解释文字。

    Returns:
        解析出的字典；失败返回 None。
    """
    cleaned = _strip_code_fence(text)
    try:
        data = json.loads(cleaned)
    except Exception:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except Exception:
            return None
    return data if isinstance(data, dict) else None


def parse_reverse_result(text: str) -> dict:
    """解析反推结果。

    Args:
        text: LLM 原始输出。

    Returns:
        {"positive": str, "negative": str, "summary": str, "raw_ok": bool}；
        解析失败时 positive 为空且 raw_ok 为 False。
    """
    result = {"positive": "", "negative": "", "summary": "", "raw_ok": False}
    data = _load_json_object(text)
    if not isinstance(data, dict):
        return result
    result["raw_ok"] = True
    for key in ("positive", "negative", "summary"):
        value = data.get(key)
        if isinstance(value, str):
            result[key] = value.strip()
    return result


def _strip_code_fence(text: str) -> str:
    """去掉 markdown 代码块包裹。"""
    text = (text or "").strip()
    text = re.sub(r"```(?:json)?", "", text).strip()
    return text.replace("```", "").strip()


def parse_optimize_result(text: str) -> dict:
    """容错解析 LLM 输出的 JSON。

    Args:
        text: LLM 原始输出。

    Returns:
        规范化后的结果；解析失败时 positive 为空且 raw_ok 为 False（由调用方回退到原文）。
    """
    result = {
        "positive": "",
        "negative": "",
        "checkpoint": "",
        "lora": "",
        "lora_strength": 1.0,
        "vae": "",
        "width": 0,
        "height": 0,
        "raw_ok": False,
    }
    data = _load_json_object(text)
    if not isinstance(data, dict):
        return result

    result["raw_ok"] = True
    for key in OPTIONAL_KEYS:
        value = data.get(key)
        result[key] = value.strip() if isinstance(value, str) else ""
    strength = data.get("lora_strength")
    if isinstance(strength, (int, float)):
        result["lora_strength"] = max(0.0, min(float(strength), 2.0))
    for key in ("width", "height"):
        value = data.get(key)
        if isinstance(value, (int, float)) and value > 0:
            result[key] = int(value)
    return result
