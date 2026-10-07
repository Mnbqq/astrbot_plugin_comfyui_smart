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

from .i18n import default_translator
from .workflow_templates import guess_arch

# 提示词模板：要求只输出 JSON，避免解析歧义
#
# 改写原则（v0.27.0 重整）：
# - **忠于原意**：只画用户说过的内容。LLM 最爱犯的错是「脑补」——用户说「一个女孩在看书」，
#   它加上樱花、夕阳、风吹裙摆。出图结果与用户想象中的画面完全是两张图。
# - **别重复插件本地会做的事**：通用质量词（masterpiece 等）由架构档案自动补，
#   通用手部/肢体内脏词由 ANATOMY_NEGATIVE 自动补。让 LLM 再写一遍纯属烧 token，
#   合并去重后一点增益都没有。
# - **给数量区间**：tag 堆太多会稀释每个词的影响力（插件自己都会为此提示用户）。
PROMPT_SYSTEM = (
    "你是 ComfyUI 绘图提示词工程师：把用户的中文描述改写成能直接出图的英文 tag 提示词，"
    "并从清单里挑选模型。\n"
    "【改写】\n"
    "1. **忠于原意**：只画用户说过的内容。**补全尺度**由用户消息里的【补全尺度】那一行决定 —— "
    "严格时角色、服装、道具、场景都**不要新增**（只有明显留白处补必要信息：画幅、光线、构图，要克制）；"
    "委托时才允许补少量**呈现层**细节，并且必须写进 added 报账。\n"
    "2. 英文逗号分隔、全小写，不要句子、不要解释、不要编号。\n"
    "3. 15~30 个 tag。太少说不清画面，太多会稀释每个词的权重。\n"
    "4. **七段都要尽量覆盖**：主体（数量与类型）／场景与环境／动作与状态／细节特征／光照氛围／"
    "风格媒介／质量修饰词（见第 7 条）。但**只写画面里真有的**，写不出就跳过 —— "
    "不要为了凑满七段编内容（第 1 条）。顺序（越靠前权重越大）：主体数量与类型"
    "（1girl, solo / 2girls / no humans / landscape）→ 外貌（发色发型、瞳色、表情、视线）"
    "→ 服装与配饰 → 细节特征（材质、纹理、颜色）→ 动作与手部状态 → 视角与构图 → 场景背景 "
    "→ 光线氛围 → 画风媒介。\n"
    "5. 手部要写出**具体动作**（hands on hips、holding a cup、arms crossed、"
    "hand on own cheek）—— 让人有明确的手可画，比事后堆负面词有效得多。\n"
    "6. 中文专有名词用通行的 Danbooru 写法：初音未来 → hatsune miku、水墨 → ink painting。\n"
    "7. **质量修饰词**这一段由谁写，看用户消息里【质量词】那一行：没让你写就**不要**写通用质量词"
    "（masterpiece、best quality、8k、ultra detailed、highres）—— 插件会按底模架构自动补，"
    "你写了也是重复；让你写就只写 2~4 个与本次画面相关的（例如 film grain、soft bokeh），"
    "不要堆一长串清单。\n"
    "8. 不要用 (word:1.2) 权重语法，也不要把负面词写进 positive。\n"
    "【负面词】\n"
    "9. negative 只写**与这次描述相关**的缺陷词；通用的手部与肢体内脏词插件会自动补齐，"
    "不要重复列举。\n"
    "10. 没有针对性的就留空字符串。\n"
    "【选型】\n"
    "11. checkpoint 必须从清单里**原样复制**文件名，不要编造、改大小写或写路径。"
    "按风格倾向选：照片感/写实 → 写实系底模；动漫/插画 → 动漫系底模。"
    "模型名通常自带线索（realvis / realistic / photo / juggernaut 偏写实，"
    "animagine / anything / anime / pony / illustrious 偏动漫）；不确定就选清单里第一个。\n"
    "12. lora 只在名字与用户描述的主题或画风**明确相关**时才填，否则留空字符串；"
    "lora_strength 一般 0.6~0.9，不确定填 0.8。\n"
    "13. vae 默认留空字符串。\n"
    "【输出风格】\n"
    "14. 看**你选中的那个 checkpoint** 属于哪一类，决定 positive 怎么写，并如实填 prompt_style：\n"
    "　· 名字里有 qwen-image 的底模（Qwen-Image 一类，文本编码器是 LLM）→ prompt_style 填 "
    "\"natural\"，positive 写成**通顺的英文句子**（1~3 句、40~80 词），按这个顺序把七段写进句子："
    "主体 → 场景与环境 → 动作与状态 → 细节特征 → 光照与氛围 → 风格媒介（质量词见第 7 条）；"
    "**不要**堆逗号 tag，不要写 ((强调))。\n"
    "　· 其余底模（SD1.5 / SDXL / Pony / Flux 等）→ prompt_style 填 \"tags\"，"
    "positive 按上面第 2~4 条写英文 tag。\n"
    "【输出】\n"
    "15. 只输出一个 JSON 对象：不要 markdown 代码块、不要前后说明、不要注释、不要多余字段。\n"
    '{"positive": "...", "negative": "...", "checkpoint": "...", "lora": "", '
    '"lora_strength": 0.8, "vae": "", "width": 0, "height": 0, "prompt_style": "tags", '
    '"added": []}\n'
    "added 是你**额外补的**、用户没提的呈现层细节（委托模式才允许非空），每条一句话，最多 3 条；"
    "严格模式下必须是空数组。"
)
# positive/negative 之外的可选覆盖字段：宽高为 0 表示沿用架构默认
OPTIONAL_KEYS = ("positive", "negative", "checkpoint", "lora", "vae")
# 提示词输出风格：tags = 逗号 tag（CLIP 系底模）；natural = 通顺句子（LLM 编码器底模）
PROMPT_STYLE_TAGS = "tags"
PROMPT_STYLE_NATURAL = "natural"
_PROMPT_STYLE_ALIASES = frozenset({
    "natural", "natural language", "sentence", "sentences", "prose",
    "自然语言", "自然语言句子", "句子",
})
# 质量修饰词（七段的最后一段）由谁写：plugin = 插件按架构补（默认）；llm = 也允许模型自己写
QUALITY_WORDS_BY_PLUGIN = "plugin"
QUALITY_WORDS_BY_LLM = "llm"
# 补全尺度：strict = 只画用户说过的（默认，v0.27.0 防脑补那条）；commission = 委托模式
COMPLETION_MODE_STRICT = "strict"
COMPLETION_MODE_COMMISSION = "commission"
_COMMISSION_ALIASES = frozenset({"commission", "委托", "委托模式", "自由", "自由发挥"})
# 委托模式下最多允许补几条（提示词里写的也是 3，插件侧再兜一次）
MAX_ADDED = 3


def normalize_quality_words_by(value) -> str:
    """把配置值归一成 plugin / llm。

    认不出来一律按 `plugin`（老行为）：那套是「插件按架构补」，就算配置写错也照旧出图。

    Args:
        value: 配置里的 `draw_settings.quality_words_by`。

    Returns:
        `QUALITY_WORDS_BY_PLUGIN` 或 `QUALITY_WORDS_BY_LLM`。
    """
    text = str(value or "").strip().lower()
    return QUALITY_WORDS_BY_LLM if text == QUALITY_WORDS_BY_LLM else QUALITY_WORDS_BY_PLUGIN


def normalize_completion_mode(value) -> str:
    """把配置值归一成 strict / commission。

    **认不出来一律按 strict**：委托模式会放开「不要新增」那条约束，猜错方向的代价
    （用户没要的东西被画进去）比「没放开」大得多 —— 跟 `prompt_style` 同一个取舍。

    Args:
        value: 配置里的 `draw_settings.completion_mode`，或行内参数。

    Returns:
        `COMPLETION_MODE_STRICT` 或 `COMPLETION_MODE_COMMISSION`。
    """
    text = str(value or "").strip().lower()
    return COMPLETION_MODE_COMMISSION if text in _COMMISSION_ALIASES else COMPLETION_MODE_STRICT
MAX_CHECKPOINTS = 60
MAX_LORAS = 80
MAX_OTHERS = 40
# 详细分析里「可疑处」最多保留几条：模型很爱一口气列十条，
# 聊天窗里刷屏，而且后几条多半是硬凑的。提示词里也写了同一个上限。
MAX_ANOMALIES = 5
# 详细分析的三个文字分区（anomalies 是列表，另行处理）
ANALYSIS_KEYS = ("composition", "lighting", "style")


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

    def __init__(self, context, config: dict, translate=None):
        """初始化。

        Args:
            context: AstrBot Star Context。
            config: 插件配置。
            translate: 可选的翻译函数（键 → 文案）；提示词模板也走多语言。
        """
        self.context = context
        # 翻译函数：拿不到就用插件自带的中文文案（绝不能把键名当提示词发给模型）
        self._t = translate or default_translator().t
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
        detail: bool = False,
    ) -> dict:
        """看图反推提示词。

        Args:
            image_refs: 图片引用列表（本地路径）。
            hint: 用户附加的要求，例如「只要人物特征」。
            event: 可选消息事件。
            provider_id: 指定用哪个 provider 看图（留空则用配置或会话默认）。
            detail: 详细分析模式（`/反推 --详细`）：除 tag 外再要一份中文分区分析。

        Returns:
            {"positive": str, "negative": str, "summary": str, "analysis": dict,
             "raw_ok": bool, "model": str}
            `analysis` 只在下过详细分析指令时有内容，结构见 `parse_reverse_result`。

        Raises:
            RuntimeError: LLM 不可用或调用失败。
        """
        if detail:
            system = self._t("llm.reverse_detail_system") or REVERSE_DETAIL_SYSTEM
            user = self._t("llm.reverse_detail_user", hint="")
        else:
            system = self._t("llm.reverse_system") or REVERSE_SYSTEM
            user = self._t("llm.reverse_user", hint="")
        if hint.strip():
            user += self._t("llm.reverse_hint", hint=hint.strip())

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
            system,
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

    async def optimize_video_prompt(
        self,
        user_desc: str,
        *,
        has_start_image: bool = False,
        seconds: float = 0.0,
        fps: float = 0.0,
        negative: str = "",
        event=None,
    ) -> dict:
        """把描述改写成**视频**提示词（重点补动作与镜头，保持用户语言）。

        Args:
            user_desc: 用户的原始描述。
            has_start_image: 是否图生视频（有首帧图）。
            seconds: 计划时长（秒）。
            fps: 计划帧率。
            negative: 配置里的默认负面词，供模型参考。
            event: 可选消息事件。

        Returns:
            含 positive / negative / _raw 的字典（解析失败时只有 _raw）。

        Raises:
            RuntimeError: LLM 不可用或调用失败。
        """
        kind = "图生视频（用户会发一张首帧图）" if has_start_image else "文生视频（从零生成）"
        hint = ("动作要围绕这张首帧图展开，例如「她缓缓回头」「镜头缓缓推近」"
                if has_start_image else
                "主体、动作、环境都要写清楚，并给一个镜头运动")
        system = self._t("llm.video_system") or VIDEO_PROMPT_SYSTEM
        user = self._t(
            "llm.video_user",
            desc=user_desc,
            kind=kind,
            hint=hint,
            seconds=f"{seconds:g}" if seconds else "未指定",
            fps=f"{fps:g}" if fps else "未指定",
            negative=negative or "（无）",
        )
        text = await self.generate(system, user, event=event)
        parsed = parse_optimize_result(text)
        parsed["_raw"] = text
        return parsed

    async def optimize_prompt(
        self,
        user_desc: str,
        catalog: dict[str, list[str]],
        defaults: dict | None = None,
        *,
        quality_words_by: str = QUALITY_WORDS_BY_PLUGIN,
        completion_mode: str = COMPLETION_MODE_STRICT,
        event=None,
    ) -> dict:
        """把中文描述转成提示词并选型。

        Args:
            user_desc: 用户的中文描述。
            catalog: 真实模型清单 {文件夹: [文件名...]}。
            defaults: 默认参数（负面词/宽高），用于填充缺省。
            quality_words_by: 质量修饰词（七段的最后一段）由谁写；见
                `normalize_quality_words_by()`。
            completion_mode: 补全尺度；见 `normalize_completion_mode()`。
                `strict` 只画用户说过的，`commission` 允许补少量呈现层细节并要求报账。
            event: 可选消息事件。

        Returns:
            规范化后的结果字典，含 positive / negative / checkpoint / lora /
            lora_strength / vae / width / height / prompt_style / added，
            以及 raw_ok 标记解析是否成功。

        Raises:
            RuntimeError: LLM 不可用或调用失败。
        """
        defaults = defaults or {}
        catalog_text = self.build_catalog(catalog)
        rule_key = ("llm.quality_rule_llm"
                    if normalize_quality_words_by(quality_words_by) == QUALITY_WORDS_BY_LLM
                    else "llm.quality_rule_plugin")
        completion_key = ("llm.completion_rule_commission"
                          if normalize_completion_mode(completion_mode) == COMPLETION_MODE_COMMISSION
                          else "llm.completion_rule_strict")
        user = self._t(
            "llm.optimize_user",
            desc=user_desc,
            negative=defaults.get("negative") or "（无）",
            catalog=catalog_text,
            quality_rule=self._t(rule_key),
            completion_rule=self._t(completion_key),
        )
        system = self._t("llm.optimize_system") or PROMPT_SYSTEM
        text = await self.generate(system, user, event=event)
        parsed = parse_optimize_result(text)
        parsed["_raw"] = text
        return parsed


# 视频提示词的系统提示词（与图片不同：视频要动作 + 镜头，不要堆 tag）
VIDEO_PROMPT_SYSTEM = (
    "你是 AI 视频提示词工程师。把用户的描述改写成**视频**提示词，而不是图片 tag。\n"
    "规则：\n"
    "1. 必须写清**动作**：谁在做什么、动作怎么变化（例如「缓缓回头」「身体轻轻晃动」「转头看向镜头」）。\n"
    "2. 写**一个**镜头运动：固定镜头 / 缓缓推近 / 横向平移 / 轻微手持感。\n"
    "3. 画面要素按「主体 + 外貌服装 → 环境 → 光影氛围」各一两句，别堆砌形容词。\n"
    "4. **保持用户的语言**：用户写中文就输出中文（Wan 系列原生懂中文）。\n"
    "5. 长度 40~80 个汉字（或 25~50 个英文词）。\n"
    "6. 不要写「静止」「不动」「视频」「帧」这类词，也不要点名模型或参数。\n"
    "7. 不要写通用质量词（masterpiece、best quality、8k、4k）；通用的画质与畸形规避词"
    "插件会自动补齐，negative 里不用重复列举。\n"
    "8. 只输出 JSON：{\"positive\": \"改写后的提示词\", \"negative\": \"一句针对性负面词或空串\"}；"
    "negative 只写与本次画面相关的那一两条。\n"
    "只输出 JSON，不要解释。"
)
VIDEO_PROMPT_USER = (
    "用户描述：{desc}\n"
    "本次任务：{kind}\n"
    "要求：{hint}\n"
    "计划时长约 {seconds} 秒、{fps} fps。\n"
    "默认负面词（可参考，不要照抄）：{negative}"
)


# 反推提示词的系统提示词
#
# 三个真机上最容易出问题的地方（v0.27.0 补）：
# - **签名/水印**：反推原图右下角写着 `Xxun1003`，不明确禁止的话模型会把它当内容推出来。
# - **真人照片**：照片被推成 1girl, anime style 之后，拿去出图就变成二次元了。
# - **编造细节**：看不清的纹样硬猜，重绘出来和原图南辕北辙 —— 宁可少写。
#
# v0.30.0 起分成两套：`REVERSE_SYSTEM`（只出 tag）与 `REVERSE_DETAIL_SYSTEM`
# （tag + 中文分区分析）。**tag 规则与看图规则是同一段常量**（`REVERSE_TAG_RULES`），
# 两种模式共用 —— 复制一份出来改，迟早会出现「普通模式禁了某件事、详细模式没禁」的漂移。
REVERSE_TAG_RULES = (
    "【看图】\n"
    "1. 只描述画面里**真实存在**的内容。看不清的纹样、配饰不要猜 —— "
    "编造的 tag 会让重绘结果偏离原图。\n"
    "2. **忽略**画师签名、水印、平台角标与画面上的文字：它们不是画面内容，不要反推成 tag。\n"
    "3. 先判断画面属于哪一类，再决定用词：\n"
    "　· 动漫/插画 → Danbooru 风格 tag + 画风媒介（anime、cel shading、watercolor）\n"
    "　· 真人照片 → 摄影描述（photo、realistic、35mm photograph），主体用 woman / man / person，"
    "**不要**输出 1girl / anime 这类二次元 tag\n"
    "　· 无人物 → 以场景与光线为主，用 no humans / landscape\n"
    "【positive】\n"
    "4. 英文逗号分隔、全小写，15~45 个 tag，按这个顺序：画面类型与主体数量 → "
    "外貌（发色发型、瞳色、表情、视线）→ 服装与配饰 → 动作与**手部状态**"
    "（hands on hips、holding a cup、arms crossed）→ 视角与构图"
    "（from above、upper body、full body、close-up）→ 场景背景 → 光线氛围 → 画风媒介。\n"
    "5. 不要写通用质量词（masterpiece、best quality、8k）—— 插件出图时会按底模自动补。\n"
    "【negative】\n"
    "6. 只写**与这张图相关**的缺陷词（多人构图写 extra limbs、写实照片写 anime style）；"
    "通用的手部与肢体内脏词插件会自动补齐，不要重复列举。\n"
    "7. 想不出针对性的就留空字符串。\n"
)

# 一句中文概括：两套模式共用（详细分析在它后面再补一段 analysis）
REVERSE_SUMMARY_RULE = (
    "【summary】\n"
    "8. 用一句中文概括画面：主体 + 在做什么 + 环境与光线。\n"
)

REVERSE_SYSTEM = (
    "你是插画提示词反推助手：看图，输出可直接用于 Stable Diffusion 的英文 tag 提示词。\n"
    + REVERSE_TAG_RULES
    + REVERSE_SUMMARY_RULE
    + "【输出】\n"
    "9. 只输出一个 JSON 对象：不要 markdown 代码块、不要说明文字、不要注释。\n"
    '{"positive": "...", "negative": "...", "summary": "..."}'
)

# 详细分析模式（`/反推 --详细`）：tag 规则与上面**逐字相同**，只是另外要一份中文分区分析。
# 为什么分区而不是「随便写段分析」：分区是**可核对的** —— 模型没法用一句漂亮话糊过去；
# 而 anomalies 明确允许空数组，是为了让它能说「没看出问题」，而不是硬编几条凑数。
REVERSE_DETAIL_SYSTEM = (
    "你是插画提示词反推与画面分析助手：看图，输出可直接用于 Stable Diffusion 的英文 tag 提示词，"
    "并附一份中文画面分析（构图 / 光线色彩 / 画风 / 可疑处）。\n"
    + REVERSE_TAG_RULES
    + REVERSE_SUMMARY_RULE
    + "【analysis】\n"
    "9. 用中文分四块写，每块 1~2 句、不超过 80 字；**只写看得见的**，看不清就写「无法判断」，不要编：\n"
    "　· composition：画面构成与取景（主体位置、视线引导、前中后景、留白）\n"
    "　· lighting：光线与色彩（主光方向、冷暖对比、光源类型、整体色调）\n"
    "　· style：画风与笔触（媒介、完成度、参考风格；**不要断言具体模型或画师**）\n"
    "　· anomalies：可疑处 —— 结构崩坏（手指、四肢、器物）、镜像或透视漂移、画面上的伪文字与水印；\n"
    "　　每条一句话，最多 5 条，**没有就留空数组**，不要为了凑数编问题\n"
    "【输出】\n"
    "10. 只输出一个 JSON 对象：不要 markdown 代码块、不要说明文字、不要注释。\n"
    '{"positive": "...", "negative": "...", "summary": "...", '
    '"analysis": {"composition": "...", "lighting": "...", "style": "...", "anomalies": ["..."]}}'
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


def _normalize_anomalies(value) -> list[str]:
    """把 anomalies 字段整理成字符串列表。

    模型有时给数组、有时给一整段带 `-` / 序号的文本，两种都收；
    空项一律丢掉（宁可这块不显示，也不要出现一个空的「·」）。
    """
    if isinstance(value, list):
        raw_items = [item for item in value if isinstance(item, str)]
    elif isinstance(value, str):
        raw_items = value.splitlines()
    else:
        return []
    items: list[str] = []
    for item in raw_items:
        text = re.sub(r"^[\s\-·*•　]*(?:\d+[.、)]\s*)?", "", item).strip()
        if text:
            items.append(text)
    return items


def _normalize_analysis(value) -> dict:
    """规范化 `analysis` 字段（详细分析模式）。

    容错三种写法，都是为了「模型不按格式来也能显示」：
    - 对象 → 取 composition / lighting / style（字符串）+ anomalies（列表）；
    - 字符串 → 放进 `text`，调用方原样展示（模型把整段分析写成一段话时）；
    - 其它 / 缺失 → 空字典（普通模式就是这种，不算错）。

    Args:
        value: LLM 给的 analysis 字段。

    Returns:
        规范化后的字典；可能为空。
    """
    if isinstance(value, str):
        text = value.strip()
        return {"text": text} if text else {}
    if not isinstance(value, dict):
        return {}
    analysis: dict = {}
    for key in ANALYSIS_KEYS:
        item = value.get(key)
        if isinstance(item, str) and item.strip():
            analysis[key] = item.strip()
    anomalies = _normalize_anomalies(value.get("anomalies"))
    if anomalies:
        analysis["anomalies"] = anomalies[:MAX_ANOMALIES]
    return analysis


def parse_reverse_result(text: str) -> dict:
    """解析反推结果。

    Args:
        text: LLM 原始输出。

    Returns:
        {"positive": str, "negative": str, "summary": str, "analysis": dict,
         "raw_ok": bool}；
        解析失败时 positive 为空且 raw_ok 为 False。
    """
    result = {
        "positive": "",
        "negative": "",
        "summary": "",
        "analysis": {},
        "raw_ok": False,
    }
    data = _load_json_object(text)
    if not isinstance(data, dict):
        return result
    result["raw_ok"] = True
    for key in ("positive", "negative", "summary"):
        value = data.get(key)
        if isinstance(value, str):
            result[key] = value.strip()
    result["analysis"] = _normalize_analysis(data.get("analysis"))
    return result


def _strip_code_fence(text: str) -> str:
    """去掉 markdown 代码块包裹。"""
    text = (text or "").strip()
    text = re.sub(r"```(?:json)?", "", text).strip()
    return text.replace("```", "").strip()


def _normalize_prompt_style(value) -> str:
    """把 `prompt_style` 归一成 tags / natural。

    模型可能写 natural、natural language，也可能写中文或干脆不写。**认不出来一律按 tags** ——
    这是刻意的保守：猜成 natural 会把「本该堆 tag」的常规底模改成一段散文，
    而 tag 场景下散文的命中率明显更差；反过来退化成 tag 只是没那么贴合，代价小得多。

    Args:
        value: LLM 给的 prompt_style 字段。

    Returns:
        `PROMPT_STYLE_TAGS` 或 `PROMPT_STYLE_NATURAL`。
    """
    text = str(value or "").strip().lower().replace("_", " ").replace("-", " ")
    return PROMPT_STYLE_NATURAL if text in _PROMPT_STYLE_ALIASES else PROMPT_STYLE_TAGS


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
        "prompt_style": PROMPT_STYLE_TAGS,
        "added": [],
        "raw_ok": False,
    }
    data = _load_json_object(text)
    if not isinstance(data, dict):
        return result

    result["raw_ok"] = True
    for key in OPTIONAL_KEYS:
        value = data.get(key)
        result[key] = value.strip() if isinstance(value, str) else ""
    result["prompt_style"] = _normalize_prompt_style(data.get("prompt_style"))
    # added：模型报账「我补了什么」。给数组或给一整段带 `-`/序号的文本都收，
    # 空项丢掉、最多 MAX_ADDED 条（与提示词里的上限一致）。
    result["added"] = _normalize_anomalies(data.get("added"))[:MAX_ADDED]
    strength = data.get("lora_strength")
    if isinstance(strength, (int, float)):
        result["lora_strength"] = max(0.0, min(float(strength), 2.0))
    for key in ("width", "height"):
        value = data.get(key)
        if isinstance(value, (int, float)) and value > 0:
            result[key] = int(value)
    return result
