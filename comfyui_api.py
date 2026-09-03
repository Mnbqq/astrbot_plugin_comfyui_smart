"""ComfyUI API 封装：模型列表拉取、出图队列、进度轮询、图片下载。"""
import asyncio
import json
import re
import uuid
from pathlib import Path

import aiohttp
from astrbot.api import logger


def _normalize_base_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    return url.rstrip("/")


class ComfyUI:
    """ComfyUI HTTP 客户端。"""

    def __init__(self, base_url: str, timeout: int = 120):
        self.base_url = _normalize_base_url(base_url)
        self.timeout = timeout

    # ---------- 基础请求 ----------
    async def _get(self, path: str):
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{self.base_url}{path}",
                                   timeout=aiohttp.ClientTimeout(total=self.timeout)) as resp:
                return await resp.json()

    async def _post(self, path: str, payload: dict):
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{self.base_url}{path}", json=payload,
                                    timeout=aiohttp.ClientTimeout(total=self.timeout)) as resp:
                return await resp.json()

    # ---------- 模型列表 ----------
    @staticmethod
    def _extract_model_names(param_value) -> list:
        """兼容多种ComfyUI格式提取模型名称列表。

        兼容格式：
        1. ckpt_name = [[...模型名...], {"tooltip": ...}]  -> 模型在[0]
        2. ckpt_name = ["STRING", {"ui_list": [...]}]       -> 模型在[1]["ui_list"]
        3. ckpt_name 直接是一个字符串列表
        """
        if not isinstance(param_value, list):
            return []
        models = []
        for item in param_value:
            if isinstance(item, list):
                # 直接是字符串列表（模型名数组）
                for n in item:
                    if isinstance(n, str) and n.strip():
                        models.append(n.strip())
            elif isinstance(item, dict):
                # dict 中有 ui_list
                for key in ("ui_list", "choices"):
                    if isinstance(item.get(key), list):
                        for n in item[key]:
                            if isinstance(n, str) and n.strip():
                                models.append(n.strip())
            elif isinstance(item, str):
                # 字符串本身（排除 "STRING" 这类类型占位）
                if item.strip() and item != "STRING":
                    models.append(item.strip())
        # 去重并保持顺序
        seen = set()
        unique = []
        for m in models:
            if m not in seen:
                seen.add(m)
                unique.append(m)
        return unique

    async def get_model_lists(self) -> dict:
        """拉取 Checkpoint / ControlNet / VAE / LORA 列表。

        返回 { "checkpoint": [..], "controlnet": [..], "vae": [..], "lora": [..] }
        """
        result = {"checkpoint": [], "controlnet": [], "vae": [], "lora": []}
        try:
            data = await self._get("/object_info")
        except Exception as e:
            logger.error(f"[ComfyUI] 拉取 object_info 失败: {e}")
            return result

        # 提取Checkpoint模型列表（兼容多种格式）
        if "CheckpointLoaderSimple" in data:
            ckpt_node = data["CheckpointLoaderSimple"]
            try:
                param = ckpt_node.get("input", {}).get("required", {}).get("ckpt_name", [])
                models = self._extract_model_names(param)
                result["checkpoint"] = sorted(models)
                logger.info(f"[ComfyUI] 找到 {len(models)} 个Checkpoint模型")
                if models:
                    logger.info(f"[ComfyUI] 前3个Checkpoint: {models[:3]}")
            except Exception as e:
                logger.error(f"[ComfyUI] 解析Checkpoint失败: {e}")

        # 提取VAE模型列表
        if "VAELoader" in data:
            vae_node = data["VAELoader"]
            try:
                param = vae_node.get("input", {}).get("required", {}).get("vae_name", [])
                models = self._extract_model_names(param)
                result["vae"] = sorted(models)
                logger.info(f"[ComfyUI] 找到 {len(models)} 个VAE模型")
            except Exception as e:
                logger.error(f"[ComfyUI] 解析VAE失败: {e}")

        # 提取LoRA模型列表
        if "LoraLoader" in data:
            lora_node = data["LoraLoader"]
            try:
                param = lora_node.get("input", {}).get("required", {}).get("lora_name", [])
                models = self._extract_model_names(param)
                result["lora"] = sorted(models)
                logger.info(f"[ComfyUI] 找到 {len(models)} 个LoRA模型")
            except Exception as e:
                logger.error(f"[ComfyUI] 解析LoRA失败: {e}")

        # 提取ControlNet模型列表（多加载器合并）
        controlnet_models = set()
        for cls_name in ["ControlNetLoader", "ControlNetLoaderAdvanced"]:
            if cls_name in data:
                cn_node = data[cls_name]
                try:
                    param = cn_node.get("input", {}).get("required", {}).get("control_net_name", [])
                    models = self._extract_model_names(param)
                    controlnet_models.update(models)
                except Exception as e:
                    logger.error(f"[ComfyUI] 解析{cls_name}失败: {e}")
        result["controlnet"] = sorted(controlnet_models)

        # 记录总的模型数量用于调试
        total_models = sum(len(v) for v in result.values())
        logger.info(f"[ComfyUI] 总共解析到 {total_models} 个模型 "
                    f"(checkpoint={len(result['checkpoint'])}, controlnet={len(result['controlnet'])}, "
                    f"vae={len(result['vae'])}, lora={len(result['lora'])})")

        return result

    # ---------- 出图 ----------
    def build_t2i_workflow(self, prompt: str, negative: str, model: dict,
                           width: int, height: int, steps: int,
                           sampler: str, cfg: float, seed: int, lora: dict = None) -> dict:
        """按内置标准文生图模板构建 API 格式工作流。"""
        # 节点 ID 约定（详见 workflows/text2img_api.json）
        wf = {
            "1": {"class_type": "CheckpointLoaderSimple",
                  "inputs": {"ckpt_name": model["checkpoint"]}},
            "2": {"class_type": "CLIPTextEncode",
                  "inputs": {"text": prompt, "clip": ["1", 1]}},
            "3": {"class_type": "CLIPTextEncode",
                  "inputs": {"text": negative, "clip": ["1", 1]}},
            "4": {"class_type": "EmptyLatentImage",
                  "inputs": {"width": width, "height": height, "batch_size": 1}},
            "5": {"class_type": "KSampler",
                  "inputs": {
                      "model": ["1", 0],
                      "positive": ["2", 0],
                      "negative": ["3", 0],
                      "latent_image": ["4", 0],
                      "seed": seed,
                      "steps": steps,
                      "cfg": cfg,
                      "sampler_name": sampler,
                      "scheduler": "normal",
                      "denoise": 1.0}},
            "6": {"class_type": "VAEDecode",
                  "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
            "7": {"class_type": "SaveImage",
                  "inputs": {"images": ["6", 0], "filename_prefix": "astrbot_smart"}},
        }

        # 可选 VAE 覆盖
        if model.get("vae"):
            wf["8"] = {"class_type": "VAELoader",
                       "inputs": {"vae_name": model["vae"]}}
            wf["6"]["inputs"]["vae"] = ["8", 0]

        # 可选 LORA 接入（在 checkpoint 和 KSampler 之间插入）
        if lora and lora.get("name"):
            wf["9"] = {"class_type": "LoraLoader",
                       "inputs": {
                           "model": ["1", 0],
                           "clip": ["1", 1],
                           "lora_name": lora["name"],
                           "strength_model": lora.get("strength", 1.0),
                           "strength_clip": lora.get("strength", 1.0)}}
            wf["5"]["inputs"]["model"] = ["9", 0]
            wf["2"]["inputs"]["clip"] = ["9", 1]
            wf["3"]["inputs"]["clip"] = ["9", 1]

        return wf

    async def queue_prompt(self, workflow: dict) -> str:
        """提交工作流，返回 prompt_id。"""
        data = await self._post("/prompt", {"prompt": workflow})
        pid = data.get("prompt_id")
        if not pid:
            raise RuntimeError(f"ComfyUI 未返回 prompt_id: {data}")
        return pid

    async def get_history(self, prompt_id: str) -> dict:
        try:
            return await self._get(f"/history/{prompt_id}")
        except Exception:
            return {}

    async def wait_and_download(self, prompt_id: str, output_dir: Path,
                                poll_interval: float = 1.5) -> list[Path]:
        """轮询直到出图完成，下载图片，返回本地路径列表。"""
        output_dir.mkdir(parents=True, exist_ok=True)
        while True:
            await asyncio.sleep(poll_interval)
            history = await self.get_history(prompt_id)
            if prompt_id not in history:
                continue
            entry = history[prompt_id]
            if entry.get("status", {}).get("status_str") == "error":
                raise RuntimeError("ComfyUI 出图失败")
            outputs = entry.get("outputs", {})
            images = []
            for node_output in outputs.values():
                for img in node_output.get("images", []):
                    images.append(img)
            if not images:
                continue
            # 下载图片
            paths = []
            for img in images:
                filename = img.get("filename") or f"{uuid.uuid4().hex}.png"
                subfolder = img.get("subfolder", "")
                type_ = img.get("type", "output")
                url_path = f"/view?filename={filename}&subfolder={subfolder}&type={type_}"
                async with aiohttp.ClientSession() as session:
                    async with session.get(f"{self.base_url}{url_path}",
                                           timeout=aiohttp.ClientTimeout(total=self.timeout)) as resp:
                        content = await resp.read()
                local = output_dir / filename
                local.write_bytes(content)
                paths.append(local)
            return paths
