"""插件 Pages 的后端 Web API。

路由约定（官方规范）：注册 `/<插件名>/xxx`，前端 `bridge.apiGet("xxx")`
由 Dashboard 转发到 `/api/v1/plugins/extensions/<插件名>/xxx`。

与旧版的区别：
- 只使用官方 `astrbot.api.web` 的 json_response / error_response / file_response，
  删掉了「猜 file_response 不存在」而写的三层降级（最后退化成 base64 内联 JSON）。
- 配置保存改为深度合并，且失败时抛错而不是静默降级 —— 旧版把整个注册包在 try/except 里
  只打 warning，用户在界面上只看到「配置加载失败」，服务端日志也被降级成 warning。
"""
from __future__ import annotations

from pathlib import Path

from astrbot.api import logger
from astrbot.api.web import error_response, file_response, json_response, request

PLUGIN_NAME = "astrbot_plugin_comfyui_smart"


def register_pages_routes(plugin) -> bool:
    """在插件实例上注册 Web API。

    Args:
        plugin: ComfyUISmartPlugin 实例。

    Returns:
        是否全部注册成功。
    """
    context = plugin.context

    async def get_config():
        return json_response({"config": plugin.get_full_config()})

    async def save_config():
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象", status_code=400)
        try:
            result = await plugin.save_config(payload)
        except Exception as e:
            logger.error("[ComfyUI] 配置保存失败：%s", e)
            return error_response(f"配置保存失败：{e}", status_code=500)
        return json_response(result)

    async def get_models():
        catalog = await plugin.get_catalog()
        return json_response({"catalog": catalog, "folders": len(catalog)})

    async def refresh_models():
        result = await plugin.refresh_models()
        status = 200 if result.get("ok") else 502
        return json_response(result, status_code=status)

    async def get_templates():
        return json_response(
            {
                "templates": [t.describe() for t in plugin.templates.values()],
                "failed": plugin.template_errors,
                "user_dir": str(plugin.user_template_dir),
            }
        )

    async def get_status():
        return json_response(await plugin.get_server_status())

    async def get_stats():
        return json_response(plugin.storage.load_stats())

    async def clear_stats():
        await plugin.storage.clear_stats()
        return json_response({"cleared": True})

    async def get_image(filename: str = ""):
        safe_name = Path(filename or "").name
        if not safe_name:
            return error_response("缺少文件名", status_code=400)
        target = plugin.storage.output_dir / safe_name
        if not target.is_file():
            return error_response("图片不存在", status_code=404)
        return file_response(target)

    routes = (
        (f"/{PLUGIN_NAME}/config", get_config, ["GET"], "读取插件配置"),
        (f"/{PLUGIN_NAME}/config", save_config, ["POST"], "保存插件配置"),
        (f"/{PLUGIN_NAME}/models", get_models, ["GET"], "读取模型清单"),
        (f"/{PLUGIN_NAME}/models/refresh", refresh_models, ["POST"], "重新发现模型"),
        (f"/{PLUGIN_NAME}/templates", get_templates, ["GET"], "读取工作流模板"),
        (f"/{PLUGIN_NAME}/status", get_status, ["GET"], "读取 ComfyUI 状态"),
        (f"/{PLUGIN_NAME}/stats", get_stats, ["GET"], "读取统计"),
        (f"/{PLUGIN_NAME}/stats/clear", clear_stats, ["POST"], "清空统计"),
        (f"/{PLUGIN_NAME}/images/<filename>", get_image, ["GET"], "读取生成的图片"),
    )
    for route, handler, methods, desc in routes:
        context.register_web_api(route, handler, methods, desc)
    logger.info("[ComfyUI] Pages Web 路由注册完成，共 %d 条", len(routes))
    return True
