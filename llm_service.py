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
    async def generate(self, system: str, user: str, event=None) -> str:
        """调用 LLM 生成文本。

        Args:
            system: 系统提示词。
            user: 用户内容。
            event: 可选消息事件，用于会话级 provider 解析。

        Returns:
            生成的纯文本。

        Raises:
            RuntimeError: 没有可用 LLM 或调用失败，message 面向用户。
        """
        if self._has_custom_endpoint():
            return await self._call_custom(system, user)

        provider_id = await self.resolve_provider_id(event)
        if not provider_id:
            names = "、".join(self.available_providers()) or "（当前没有任何 LLM 提供商）"
            raise RuntimeError(
                f"没有可用的 LLM：请在 AstrBot 中配置对话模型，"
                f"或在插件配置的 llm_settings.provider 里指定。当前可用：{names}"
            )
        try:
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=user,
                system_prompt=system,
            )
        except Exception as e:
            raise RuntimeError(f"调用 LLM 失败（{provider_id}）：{e}") from e

        text = getattr(resp, "completion_text", None)
        return (text or "").strip()

    async def _call_custom(self, system: str, user: str) -> str:
        """调用配置里的 OpenAI 兼容端点。

        Args:
            system: 系统提示词。
            user: 用户内容。

        Returns:
            生成的纯文本。

        Raises:
            RuntimeError: 请求失败。
        """
        import aiohttp

        base = str(self.llm_conf.get("base_url") or "").rstrip("/")
        model = str(self.llm_conf.get("model") or "").strip() or "gpt-4o-mini"
        api_key = str(self.llm_conf.get("api_key") or "").strip()
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
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
    cleaned = _strip_code_fence(text)
    data = None
    try:
        data = json.loads(cleaned)
    except Exception:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(0))
            except Exception:
                data = None
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
