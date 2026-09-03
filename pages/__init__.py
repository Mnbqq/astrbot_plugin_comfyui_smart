"""Pages 配置页后端路由。

通过 AstrBot 的 context.register_web_api() 注册插件 Web API，
供前端 Page（pages/settings/index.html）通过 window.AstrBotPluginPage bridge 调用。

路由设计（以插件名 astrbot_plugin_comfyui_smart 为例）：
  - 前端 bridge endpoint 为相对路径，如 "config"、"models/refresh"
  - Dashboard 会转发到 /api/v1/plugins/extensions/{plugin_name}/<endpoint>
  - 因此注册的 route 需要包含插件名前缀：/{PLUGIN_NAME}/config 等
"""

from astrbot.api import logger
from astrbot.api.web import json_response, error_response, request
from pathlib import Path

# file_response 可能不存在于部分 AstrBot 版本，做安全降级
try:
    from astrbot.api.web import file_response
except ImportError:
    file_response = None

PLUGIN_NAME = "astrbot_plugin_comfyui_smart"


def register_pages_routes(plugin):
    """在插件实例（Star 子类）上注册 Web 路由。

    使用官方 context.register_web_api(route, handler, methods, desc)。
    handler 接收动态路由参数作为关键字参数。
    """
    ctx = plugin.context

    # 读取配置
    async def get_config():
        return json_response({"status": "ok", "data": plugin.get_full_config()})

    # 保存配置
    async def save_config():
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("invalid payload", status_code=400)
        plugin.save_config(payload)
        return json_response({"status": "ok", "message": "配置已保存"})

    # 读取模型列表
    async def get_models():
        return json_response({"status": "ok", "data": plugin.storage.load_models()})

    # 刷新模型
    async def refresh_models():
        result = await plugin.refresh_models()
        return json_response(result)

    # 读取统计
    async def get_stats():
        return json_response({"status": "ok", "data": plugin.storage.load_stats()})

    # 读取生成图片（按文件名）
    async def get_image(filename=""):
        if not filename:
            return error_response("missing filename", status_code=400)
        # 防目录穿越
        safe_name = Path(filename).name
        img_path = plugin.output_dir / safe_name
        if not img_path.exists():
            return error_response("image not found", status_code=404)
        # 优先用官方 file_response，失败则用标准库手动构造
        try:
            if file_response is not None:
                return file_response(str(img_path))
            raise ImportError("file_response unavailable")
        except Exception as e:
            logger.warning(f"[ComfyUI] file_response 不可用，降级手动响应: {e}")
            data = img_path.read_bytes()
            # 简易 MIME 判断
            suffix = img_path.suffix.lower()
            mime = {
                ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
            }.get(suffix, "application/octet-stream")
            # 尝试用 astrbot 的 Response 类；拿不到则直接返回 json_response 兜底
            try:
                from astrbot.api.web import Response
                return Response(content=data, media_type=mime)
            except Exception:
                # 最终兜底：base64 内联（不推荐但保证不崩）
                import base64
                b64 = base64.b64encode(data).decode("ascii")
                return json_response({
                    "status": "ok", "mime": mime, "base64": b64,
                    "data_url": f"data:{mime};base64,{b64}",
                })

    try:
        ctx.register_web_api(f"/{PLUGIN_NAME}/config", get_config, ["GET"], "获取配置")
        ctx.register_web_api(f"/{PLUGIN_NAME}/config", save_config, ["POST"], "保存配置")
        ctx.register_web_api(f"/{PLUGIN_NAME}/models", get_models, ["GET"], "获取模型列表")
        ctx.register_web_api(f"/{PLUGIN_NAME}/models/refresh", refresh_models, ["POST"], "刷新模型")
        ctx.register_web_api(f"/{PLUGIN_NAME}/stats", get_stats, ["GET"], "获取统计")
        ctx.register_web_api(f"/{PLUGIN_NAME}/images/<filename>", get_image, ["GET"], "获取生成图片")
        logger.info("[ComfyUI] Pages Web 路由注册成功")
        return True
    except Exception as e:
        logger.warning(f"[ComfyUI] Pages Web 路由注册失败: {e}")
        return False