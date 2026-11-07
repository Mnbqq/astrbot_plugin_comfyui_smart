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

import base64
import mimetypes
from pathlib import Path

from astrbot.api import logger
from astrbot.api.web import error_response, file_response, json_response, request

PLUGIN_NAME = "astrbot_plugin_comfyui_smart"


THUMB_MAX_SIDE = 512
THUMB_QUALITY = 82


def make_thumbnail(data_dir: Path, source: Path) -> tuple[bytes, str, bool]:
    """把图片压成缩略图（有 Pillow 时），否则原样返回。

    为什么按需生成 + 落盘缓存：一张 1024×1216 的 PNG 动辄 2~3 MB，
    经 base64 走 postMessage 到页面会变成 3~4 MB 字符串，画廊一屏七八张就很卡。
    缩到 512 长边的 JPEG 通常只有几十 KB。

    为什么做成可选：本插件只依赖 aiohttp，不强制装 Pillow。
    Pillow 不在时**原样返回**，功能仍然可用，只是不省流量。

    Args:
        data_dir: 插件数据目录（缩略图缓存在 `<data_dir>/thumbs/`）。
        source: 原图路径。

    Returns:
        `(字节内容, MIME, 是否是缩略图)`。
    """
    mime = mimetypes.guess_type(source.name)[0] or "image/png"
    try:
        from PIL import Image  # noqa: PLC0415 - 运行时可选依赖
    except ImportError:
        return source.read_bytes(), mime, False

    cache_dir = data_dir / "thumbs"
    cache = cache_dir / f"{source.stem}_{THUMB_MAX_SIDE}.jpg"
    try:
        if cache.is_file() and cache.stat().st_mtime >= source.stat().st_mtime:
            return cache.read_bytes(), "image/jpeg", True
    except OSError:
        pass

    try:
        with Image.open(source) as img:
            img.thumbnail((THUMB_MAX_SIDE, THUMB_MAX_SIDE))
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            cache_dir.mkdir(parents=True, exist_ok=True)
            tmp = cache.with_suffix(".jpg.tmp")
            img.save(tmp, "JPEG", quality=THUMB_QUALITY, optimize=True)
            tmp.replace(cache)
        return cache.read_bytes(), "image/jpeg", True
    except Exception:  # noqa: BLE001 - 图像解码器会抛各种异常（Pillow 对坏 PNG 抛 SyntaxError）
        # 图片坏了、格式不认、磁盘写不了 —— 都退回原图，别让画廊因此空掉。
        # 这里刻意不窄化：PIL 可能抛 SyntaxError / UnidentifiedImageError /
        # DecompressionBombError / struct.error，逐个列举既漏又脆。
        logger.warning("缩略图生成失败，退回原图：%s", source.name)
        return source.read_bytes(), mime, False


def image_roots(data_dir: Path) -> list[Path]:
    """图片可能所在的目录，按优先级排序。

    正常情况下只有 `data_dir/output` 一个。但如果插件目录名曾经变过
    （例如从带版本号的发布包解压成 `astrbot_plugin_comfyui_smart_v1.1.1`），
    AstrBot 会给出**另一个**数据目录，那段时间出的图就落在同级兄弟目录里 ——
    改回目录名后，画廊能列出记录却加载不出图。这里把同级同前缀的旧目录也纳入查找，
    让这类历史遗留图片还能显示，而不是一律 404。

    Args:
        data_dir: 当前插件数据目录。

    Returns:
        待查找的目录列表，当前目录在最前。
    """
    roots = [data_dir / "output"]
    try:
        own = data_dir.name
        for alt in data_dir.parent.iterdir():
            if alt.is_dir() and alt.name != own and alt.name.startswith(f"{own}_"):
                roots.append(alt / "output")
    except OSError:
        pass
    return roots


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

    async def get_i18n():
        """插件页文案：前端按 data-i18n 属性套用（导航、标签页标题、主要按钮）。"""
        translator = getattr(plugin, "t", None)
        strings = translator.ui_strings() if translator is not None else {}
        return json_response({
            "locale": getattr(translator, "locale", ""),
            "available": getattr(translator, "available", []),
            "strings": strings,
        })

    async def get_stats():
        return json_response(plugin.storage.load_stats())

    async def get_diagnose():
        """巡检 + 体检报告（配置页「状态 → 诊断报告」用）。"""
        return json_response(await plugin.get_diagnostics())

    async def get_audit(limit: str = "50", user_id: str = ""):
        """审计日志（管理面板可查；出图记录 + 内容过滤拦截）。"""
        try:
            count = max(1, min(500, int(limit)))
        except (TypeError, ValueError):
            count = 50
        rows = plugin.storage.load_audit(limit=count, user_id=str(user_id or ""))
        return json_response({"total": len(rows), "records": rows})

    async def run_inpaint():
        """局部重绘：配置页涂好遮罩后调这里跑一次。"""
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象", status_code=400)
        try:
            result = await plugin.inpaint(payload)
        except ValueError as e:
            # 请求本身不合法（缺图、尺寸不一致、太大）：400，由页面直接展示
            return error_response(str(e), status_code=400)
        except Exception as e:
            logger.warning("[ComfyUI] 局部重绘失败：%s", e)
            return error_response(f"局部重绘失败：{e}", status_code=502)
        return json_response(result)

    async def clear_stats():
        await plugin.storage.clear_stats()
        return json_response({"cleared": True})

    async def get_image(filename: str = ""):
        safe_name = Path(filename or "").name
        if not safe_name:
            return error_response("缺少文件名", status_code=400)
        roots = image_roots(plugin.data_dir)
        for root in roots:
            target = root / safe_name
            if target.is_file():
                return file_response(target)
        logger.info("画廊图片未找到：%s（查找过 %s）", safe_name,
                    "、".join(str(r) for r in roots))
        return error_response("图片不存在", status_code=404)

    async def get_image_data(filename: str = ""):
        """把图片以 data URL 返回（JSON）。

        为什么需要：AstrBot 面板把插件页放在
        `<iframe sandbox="allow-scripts ...">` 里（**没有 allow-same-origin**），
        页面处于不透明源 —— `<img src="/api/...">` 这类原生请求根本到不了宿主，
        只有 `window.AstrBotPluginPage` 桥（postMessage）能通。所以画廊里的图
        必须由桥取回成 data URL 再塞给 `<img>`。
        """
        safe_name = Path(filename or "").name
        if not safe_name:
            return error_response("缺少文件名", status_code=400)
        for root in image_roots(plugin.data_dir):
            target = root / safe_name
            if not target.is_file():
                continue
            try:
                raw = target.read_bytes()
            except OSError as e:
                return error_response(f"读取失败：{e}", status_code=500)
            mime = mimetypes.guess_type(target.name)[0] or "image/png"
            return json_response({
                "name": safe_name,
                "size": len(raw),
                "data_url": f"data:{mime};base64,{base64.b64encode(raw).decode()}",
            })
        return error_response("图片不存在", status_code=404)

    async def get_thumb_data(filename: str = ""):
        """缩略图的 data URL（画廊用；点开详情再走 `images-data` 取原图）。"""
        safe_name = Path(filename or "").name
        if not safe_name:
            return error_response("缺少文件名", status_code=400)
        for root in image_roots(plugin.data_dir):
            target = root / safe_name
            if not target.is_file():
                continue
            try:
                raw, mime, thumbed = make_thumbnail(plugin.data_dir, target)
            except OSError as e:
                return error_response(f"读取失败：{e}", status_code=500)
            return json_response({
                "name": safe_name,
                "size": len(raw),
                "thumbnail": thumbed,
                "data_url": f"data:{mime};base64,{base64.b64encode(raw).decode()}",
            })
        return error_response("图片不存在", status_code=404)

    routes = (
        (f"/{PLUGIN_NAME}/config", get_config, ["GET"], "读取插件配置"),
        (f"/{PLUGIN_NAME}/config", save_config, ["POST"], "保存插件配置"),
        (f"/{PLUGIN_NAME}/models", get_models, ["GET"], "读取模型清单"),
        (f"/{PLUGIN_NAME}/models/refresh", refresh_models, ["POST"], "重新发现模型"),
        (f"/{PLUGIN_NAME}/templates", get_templates, ["GET"], "读取工作流模板"),
        (f"/{PLUGIN_NAME}/status", get_status, ["GET"], "读取 ComfyUI 状态"),
        (f"/{PLUGIN_NAME}/i18n", get_i18n, ["GET"], "读取插件页文案（多语言）"),
        (f"/{PLUGIN_NAME}/stats", get_stats, ["GET"], "读取统计"),
        (f"/{PLUGIN_NAME}/audit", get_audit, ["GET"], "读取审计日志"),
        (f"/{PLUGIN_NAME}/diagnose", get_diagnose, ["GET"], "巡检与体检报告"),
        (f"/{PLUGIN_NAME}/inpaint", run_inpaint, ["POST"], "局部重绘（涂抹遮罩）"),
        (f"/{PLUGIN_NAME}/stats/clear", clear_stats, ["POST"], "清空统计"),
        (f"/{PLUGIN_NAME}/images/<filename>", get_image, ["GET"], "读取生成的图片"),
        (f"/{PLUGIN_NAME}/images-data/<filename>", get_image_data, ["GET"],
         "读取生成的图片（data URL，供沙箱内的页面用）"),
        (f"/{PLUGIN_NAME}/thumb-data/<filename>", get_thumb_data, ["GET"],
         "读取图片缩略图（data URL，画廊用）"),
    )
    for route, handler, methods, desc in routes:
        context.register_web_api(route, handler, methods, desc)
    logger.info("[ComfyUI] Pages Web 路由注册完成，共 %d 条", len(routes))
    return True
