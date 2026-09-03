"""LLM 服务：模型列表分析 + 中文提示词优化 + 智能选模型。

优先使用插件配置的 LLM 提供商；留空则回退到 AstrBot 默认 LLM。
"""
import json
import re

from astrbot.api import logger


class LLMService:
    """封装 LLM 调用，负责模型分析和提示词优化。"""

    def __init__(self, context, config: dict):
        self.context = context
        self.config = config or {}
        self.llm_conf = self.config.get("llm_settings", {}) or {}

    def _has_custom_llm(self) -> bool:
        """是否配置了自定义 LLM 提供商。"""
        return bool(self.llm_conf.get("provider") or self.llm_conf.get("base_url"))

    async def _call_text_gen(self, system: str, user: str) -> str:
        """调用 LLM 生成文本，返回纯文本。"""
        if self._has_custom_llm():
            # TODO: 自定义 LLM 提供商调用（openai 兼容接口）
            return await self._call_custom(system, user)
        # 回退到 AstrBot 默认 LLM
        return await self._call_astrbot_default(system, user)

    async def _call_custom(self, system: str, user: str) -> str:
        import aiohttp
        base = (self.llm_conf.get("base_url") or "").rstrip("/")
        model = self.llm_conf.get("model") or "gpt-3.5-turbo"
        api_key = self.llm_conf.get("api_key") or ""
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.3,
        }
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{base}/chat/completions", json=payload,
                                    headers=headers) as resp:
                data = await resp.json()
        try:
            return data["choices"][0]["message"]["content"]
        except Exception as e:
            logger.error(f"[ComfyUI] 自定义 LLM 解析失败: {data}")
            raise RuntimeError(f"LLM 返回异常: {e}")

    async def _call_astrbot_default(self, system: str, user: str) -> str:
        """使用 AstrBot 默认 LLM 提供商。

        采用官方 v4.5.7+ 提供的 context.llm_generate(chat_provider_id=..., prompt=...)。
        将 system 提示词拼接到 user prompt 前。
        """
        try:
            # 尝试获取当前会话使用的模型 ID；无会话上下文时留空回退默认
            provider_id = None
            try:
                provider_id = await self.context.get_current_chat_provider_id(umo=None)
            except Exception:
                provider_id = None

            prompt = user
            if system:
                prompt = f"{system}\n\n{user}"

            if provider_id:
                llm_resp = await self.context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=prompt,
                )
            else:
                # 无 provider_id 时尝试不带 chat_provider_id 调用
                llm_resp = await self.context.llm_generate(prompt=prompt)

            text = getattr(llm_resp, "completion_text", None)
            if text is None:
                text = str(llm_resp)
            return text or ""
        except Exception as e:
            logger.error(f"[ComfyUI] AstrBot 默认 LLM 调用失败: {e}")
            raise RuntimeError(f"LLM 调用失败: {e}")

    # ---------- 模型分析 ----------
    async def analyze_models(self, model_lists: dict, enabled: dict) -> dict:
        """让 LLM 分析每个模型的类型、风格、适合场景，返回结构化列表。

        enabled: {"checkpoint": bool, "controlnet": bool, ...} 只有开启的类型才分析。
        """
        result = {"checkpoint": [], "controlnet": [], "vae": [], "lora": []}
        for key in ("checkpoint", "controlnet", "vae", "lora"):
            if not enabled.get(key, False):
                continue
            names = model_lists.get(key, [])
            if not names:
                continue
            # 分批给 LLM 分析（每批 30 个）
            batch_size = 30
            for i in range(0, len(names), batch_size):
                batch = names[i:i + batch_size]
                analyzed = await self._analyze_batch(key, batch)
                result[key].extend(analyzed)
        return result

    async def _analyze_batch(self, model_type: str, names: list) -> list:
        system = (
            "你是 Stable Diffusion / ComfyUI 模型分析助手。"
            "根据模型文件名，判断每个模型的风格和适用场景。"
            "严格只输出 JSON 数组，不要输出任何其他文字。"
            f'每个元素格式：{{"name": "文件名", "style": "风格", "tags": ["标签1", "标签2"]}}'
        )
        user = f"模型类型：{model_type}\n文件名列表：\n" + "\n".join(names)
        text = await self._call_text_gen(system, user)
        return self._parse_json_array(text, names)

    @staticmethod
    def _parse_json_array(text: str, names: list) -> list:
        """从 LLM 输出中提取 JSON 数组，容错处理。"""
        text = (text or "").strip()
        # 去掉可能的 markdown 代码块
        text = re.sub(r"```(?:json)?", "", text).strip()
        try:
            data = json.loads(text)
        except Exception:
            # 尝试提取第一个 [ ... ] 段
            m = re.search(r"\[.*\]", text, re.DOTALL)
            if not m:
                return []
            try:
                data = json.loads(m.group(0))
            except Exception:
                return []
        if not isinstance(data, list):
            return []
        out = []
        for item in data:
            if isinstance(item, dict) and item.get("name"):
                name = item.get("name", "").strip()
                # 过滤无效或异常的模型名称
                if not name:
                    continue
                if name == "目录项名称":
                    continue
                if name.isnumeric():
                    continue
                # 检查是否是有效的文件名（不包含常见的无效字符）
                if "\\" in name or "/" in name or name.startswith("."):
                    continue
                    
                out.append({
                    "name": name,
                    "style": item.get("style", ""),
                    "tags": item.get("tags", []),
                })
        # 补充 LLM 没覆盖到的模型名（也要过滤）
        covered = {item["name"] for item in out}
        for n in names:
            n_str = str(n).strip()
            if n_str not in covered and n_str:
                # 对补充的模型名也进行过滤
                if (n_str != "目录项名称" and not n_str.isnumeric() 
                    and "\\" not in n_str and "/" not in n_str and not n_str.startswith(".")):
                    out.append({"name": n_str, "style": "", "tags": []})
        return out

    # ---------- 提示词优化（中文→英文） ----------
    async def optimize_prompt(self, user_desc: str, model_pool: dict) -> dict:
        """把用户中文描述优化成英文提示词，并结合可用模型选型。

        返回 {"positive": str, "negative": str, "models": {...}}
        """
        system = (
            "你是 ComfyUI 绘图提示词工程师。把用户的中文描述转成 Danbooru 风格英文 tags。"
            "根据可用模型列表，选择最合适的 checkpoint（必选）以及可选的 lora/vae。"
            "严格只输出 JSON，格式："
            '{"positive": "英文tags", "negative": "英文tags", '
            '"checkpoint": "文件名", "lora": "文件名或空", "vae": "文件名或空"}'
        )
        # 只塞 checkpoint 和 lora 的候选（controlnet 需要参考图，v1 不涉及）
        candidates = {"checkpoint": [], "lora": []}
        for key in ("checkpoint", "lora"):
            candidates[key] = [m["name"] for m in model_pool.get(key, [])]
        user = (
            f"用户描述：{user_desc}\n"
            f"可用模型：{json.dumps(candidates, ensure_ascii=False)}"
        )
        text = await self._call_text_gen(system, user)
        return self._parse_optimize_result(text)

    @staticmethod
    def _parse_optimize_result(text: str) -> dict:
        text = (text or "").strip()
        text = re.sub(r"```(?:json)?", "", text).strip()
        try:
            data = json.loads(text)
        except Exception:
            m = re.search(r"\{.*\}", text, re.DOTALL)
            if not m:
                return {"positive": "", "negative": "", "checkpoint": "", "lora": "", "vae": ""}
            try:
                data = json.loads(m.group(0))
            except Exception:
                return {"positive": "", "negative": "", "checkpoint": "", "lora": "", "vae": ""}
        if not isinstance(data, dict):
            return {"positive": "", "negative": "", "checkpoint": "", "lora": "", "vae": ""}
        return {
            "positive": data.get("positive", ""),
            "negative": data.get("negative", ""),
            "checkpoint": data.get("checkpoint", ""),
            "lora": data.get("lora", ""),
            "vae": data.get("vae", ""),
        }