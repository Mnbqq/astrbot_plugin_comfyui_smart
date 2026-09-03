"""AstrBot ComfyUI 智能绘图插件主入口。"""
import json
import os
import random
import re
from pathlib import Path

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.message_components import Image, Plain, At
from astrbot.api import logger

from .comfyui_api import ComfyUI
from .storage import Storage
from .llm_service import LLMService
from .permission import PermissionManager
from .pages import register_pages_routes

PLUGIN_DIR = Path(os.path.dirname(os.path.abspath(__file__)))

try:
    from astrbot.api.star import StarTools
    HAS_STAR_TOOLS = True
except ImportError:
    HAS_STAR_TOOLS = False


@register(
    "astrbot_plugin_comfyui_smart",
    "Mnbqq",
    "ComfyUI 智能绘图",
    "0.2.0",
    ""
)
class ComfyUISmartPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config or {}

        # 持久化数据目录
        self.data_dir = self._get_persistent_dir()
        self.storage = Storage(self.data_dir)
        self.output_dir = self.data_dir / "output"
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # 权限
        self.permission = PermissionManager(self.config.get("permission", {}))

        # ComfyUI 客户端
        server = self.config.get("server", {}) or {}
        self.comfy = ComfyUI(
            server.get("base_url", "http://127.0.0.1:8188"),
            int(server.get("timeout", 120) or 120),
        )

        # LLM 服务
        self.llm = LLMService(context, self.config)

        # 模型开关、出图默认参数
        self.model_switch = self.config.get("model_switch", {}) or {}
        self.draw_conf = self.config.get("draw_settings", {}) or {}
        self.output_conf = self.config.get("output", {}) or {}

        # 注册 Pages Web 路由
        try:
            register_pages_routes(self)
        except Exception as e:
            logger.warning(f"[ComfyUI] Pages 路由注册失败: {e}")

    def _get_persistent_dir(self) -> Path:
        """返回插件数据目录，遵循官方规范 data/plugin_data/{plugin_name}/。

        优先使用官方 get_astrbot_plugin_data_path / get_astrbot_data_path，
        失败时回退到本地 cwd。
        """
        plugin_name = "astrbot_plugin_comfyui_smart"
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path
            d = get_astrbot_plugin_data_path()
            if d:
                return Path(d) / plugin_name
        except Exception:
            pass
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path
            d = Path(get_astrbot_data_path()) / "plugin_data" / plugin_name
            d.mkdir(parents=True, exist_ok=True)
            return d
        except Exception:
            pass
        d = Path.cwd() / "data" / "plugin_data" / plugin_name
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ---------- 工具方法 ----------
    def _is_group(self, event: AstrMessageEvent) -> bool:
        gid = self._get_group_id(event)
        return bool(gid)

    @staticmethod
    def _get_group_id(event: AstrMessageEvent):
        for attr in ("get_group_id", "group_id"):
            try:
                getter = getattr(event, attr, None)
                if callable(getter):
                    gid = getter()
                    if gid:
                        return str(gid)
                elif getter:
                    return str(getter)
            except Exception:
                pass
        return None

    def _extract_prompt(self, event: AstrMessageEvent) -> str:
        msg = (getattr(event, "message_str", "") or "").strip()
        parts = msg.split(None, 1)
        return parts[1].strip() if len(parts) > 1 else ""

    # ---------- 指令 ----------
    @filter.command("刷新模型")
    async def cmd_refresh_models(self, event: AstrMessageEvent):
        uid = str(event.get_sender_id())
        if not self.permission.is_admin(uid):
            yield event.plain_result("🚫 仅管理员可用")
            return
        yield event.plain_result("🔍 正在拉取 ComfyUI 模型列表...")
        lists = await self.comfy.get_model_lists()
        total = sum(len(v) for v in lists.values())
        if total == 0:
            yield event.plain_result("⚠️ 未拉取到任何模型，请检查 ComfyUI 地址")
            return
        yield event.plain_result(f"✅ 已拉取 {total} 个模型，开始 LLM 分析（可能需要一些时间）...")
        analyzed = await self.llm.analyze_models(lists, self.model_switch)
        self.storage.save_models(analyzed)
        summary = []
        for key in ("checkpoint", "controlnet", "vae", "lora"):
            if analyzed.get(key):
                summary.append(f"{key}: {len(analyzed[key])} 个")
        yield event.plain_result(
            f"✨ 模型分析完成并已保存！\n" + "\n".join(summary)
        )

    @filter.command("模型列表")
    async def cmd_model_list(self, event: AstrMessageEvent):
        models = self.storage.load_models()
        lines = []
        for key in ("checkpoint", "controlnet", "vae", "lora"):
            items = models.get(key, [])
            if not items:
                continue
            lines.append(f"【{key}】({len(items)} 个)")
            for m in items[:10]:
                style = m.get("style", "")
                line = f"  - {m['name']}"
                if style:
                    line += f"  [{style}]"
                lines.append(line)
            if len(items) > 10:
                lines.append(f"  ...共 {len(items)} 个")
        if not lines:
            yield event.plain_result("📭 暂无模型数据，请先发送 /刷新模型")
            return
        yield event.plain_result("\n".join(lines))

    @filter.command("画图")
    async def cmd_draw(self, event: AstrMessageEvent):
        uid = str(event.get_sender_id())
        # 权限
        ok, reason = self.permission.check(uid)
        if not ok:
            yield event.plain_result(reason)
            return

        desc = self._extract_prompt(event)
        if not desc:
            yield event.plain_result("🎨 用法：/画图 <描述>，例如 /画图 一个白裙少女站在樱花树下")
            return

        # 模型池
        models = self.storage.load_models()

        # 如果没有模型数据，尝试先拉取裸列表（不分析）
        await self._ensure_models()

        models = self.storage.load_models()

        yield event.plain_result("🎨 收到灵感，正在分析并生成...")

        # LLM 优化提示词 + 选模型
        try:
            opt = await self.llm.optimize_prompt(desc, models)
        except Exception as e:
            logger.error(f"[ComfyUI] 提示词优化失败: {e}")
            yield event.plain_result(f"💥 提示词优化失败：{e}")
            return

        positive = opt.get("positive") or desc
        negative = opt.get("negative") or self.draw_conf.get("default_negative", "")

        # 选中的模型
        ckpt = opt.get("checkpoint") or self._first_model(models, "checkpoint")
        
        # 验证模型名称有效性
        if ckpt:
            ckpt_str = str(ckpt).strip()
            # 检查是否是无效名称
            if (ckpt_str in ["目录项名称", "Item Name", "名称", "name"] or 
                ckpt_str.isnumeric() or 
                "\\" in ckpt_str or "/" in ckpt_str or
                ckpt_str.startswith(".")):
                logger.warning(f"[ComfyUI] 检测到无效模型名称: '{ckpt_str}'，尝试使用备用模型")
                ckpt = None
        
        if not ckpt:
            ckpt = self._first_model(models, "checkpoint")
            if not ckpt:
                logger.error("[ComfyUI] 模型池为空或全部无效，请执行 /刷新模型")
                yield event.plain_result("❌ 未找到可用 Checkpoint，请先 /刷新模型")
                return
                
        model_sel = {
            "checkpoint": ckpt,
            "lora": opt.get("lora") or "",
            "vae": opt.get("vae") or "",
        }

        # 生成参数
        width = int(self.draw_conf.get("default_width", 512) or 512)
        height = int(self.draw_conf.get("default_height", 768) or 768)
        steps = int(self.draw_conf.get("default_steps", 20) or 20)
        sampler = self.draw_conf.get("default_sampler", "euler") or "euler"
        cfg = float(self.draw_conf.get("default_cfg", 7.0) or 7.0)
        seed = random.randint(0, 2**31 - 1)

        lora_sel = {"name": model_sel["lora"]} if model_sel["lora"] else None

        # 构建工作流并提交
        try:
            wf = self.comfy.build_t2i_workflow(
                positive, negative, model_sel, width, height,
                steps, sampler, cfg, seed, lora_sel,
            )
            pid = await self.comfy.queue_prompt(wf)
        except Exception as e:
            logger.error(f"[ComfyUI] 提交任务失败: {e}")
            yield event.plain_result(f"💥 出图失败：{e}")
            return

        yield event.plain_result("🖌️ 正在绘制中，请稍候...")

        try:
            paths = await self.comfy.wait_and_download(pid, self.output_dir)
        except Exception as e:
            logger.error(f"[ComfyUI] 出图等待失败: {e}")
            yield event.plain_result(f"💥 出图失败：{e}")
            return

        if not paths:
            yield event.plain_result("💥 出图失败：未获取到图片")
            return

        # 记录统计
        self.permission.record_usage(uid)
        uname = str(getattr(event, "get_sender_name", lambda: uid)() or uid)
        # 图片相对标识（供 Pages 通过 /images/<filename> 读取）
        img_refs = [f"images/{p.name}" for p in paths]
        self.storage.record_generation(uid, uname, positive, negative,
                                       {"checkpoint": ckpt, "lora": model_sel["lora"], "vae": model_sel["vae"]},
                                       images=img_refs)

        # 发送结果
        if self._is_group(event) and self.output_conf.get("mention_trigger_user", True):
            # 群聊，@ 触发人
            yield event.chain_result([At(qq=uid), Plain(" "), Image.fromFileSystem(str(paths[0]))])
        else:
            yield event.chain_result([Image.fromFileSystem(str(paths[0]))])

    @filter.command("统计")
    async def cmd_stats(self, event: AstrMessageEvent):
        stats = self.storage.load_stats()
        lines = ["📊 使用统计"]
        # 用户次数
        users = stats.get("users", {})
        if users:
            lines.append("【用户出图次数】")
            sorted_users = sorted(users.items(), key=lambda x: -x[1]["count"])
            for uid, info in sorted_users[:10]:
                lines.append(f"  - {info.get('name', uid)}({uid}): {info['count']} 次")
        # 模型调用
        mu = stats.get("model_usage", {})
        if mu:
            lines.append("【模型调用次数】")
            for key in ("checkpoint", "lora", "vae", "controlnet"):
                if key in mu and mu[key]:
                    for name, cnt in sorted(mu[key].items(), key=lambda x: -x[1])[:5]:
                        lines.append(f"  - [{key}] {name}: {cnt} 次")
        yield event.plain_result("\n".join(lines))

    @filter.command("管理员")
    async def cmd_admin(self, event: AstrMessageEvent):
        uid = str(event.get_sender_id())
        if not self.permission.is_admin(uid):
            yield event.plain_result("🚫 仅管理员可用")
            return
        msg = self._extract_prompt(event)
        m = re.match(r"(添加|移除)\s+(\d+)", msg)
        if not m:
            yield event.plain_result("用法：/管理员 添加|移除 <QQ号>")
            return
        action, target = m.group(1), m.group(2)
        conf = self.config.setdefault("permission", {})
        admins = set(map(str, conf.get("admin_ids", [])))
        if action == "添加":
            admins.add(target)
        else:
            admins.discard(target)
        conf["admin_ids"] = list(admins)
        self.permission.reload(conf)
        yield event.plain_result(f"✅ 已{action}管理员 {target}")

    @filter.command("白名单")
    async def cmd_whitelist(self, event: AstrMessageEvent):
        uid = str(event.get_sender_id())
        if not self.permission.is_admin(uid):
            yield event.plain_result("🚫 仅管理员可用")
            return
        msg = self._extract_prompt(event)
        m = re.match(r"(添加|移除)\s+(\d+)", msg)
        if not m:
            yield event.plain_result("用法：/白名单 添加|移除 <QQ号>")
            return
        action, target = m.group(1), m.group(2)
        conf = self.config.setdefault("permission", {})
        wl = set(map(str, conf.get("whitelist_user_ids", [])))
        if action == "添加":
            wl.add(target)
        else:
            wl.discard(target)
        conf["whitelist_user_ids"] = list(wl)
        self.permission.reload(conf)
        yield event.plain_result(f"✅ 已{action}白名单 {target}")

    @filter.command("黑名单")
    async def cmd_blacklist(self, event: AstrMessageEvent):
        uid = str(event.get_sender_id())
        if not self.permission.is_admin(uid):
            yield event.plain_result("🚫 仅管理员可用")
            return
        msg = self._extract_prompt(event)
        m = re.match(r"(添加|移除)\s+(\d+)", msg)
        if not m:
            yield event.plain_result("用法：/黑名单 添加|移除 <QQ号>")
            return
        action, target = m.group(1), m.group(2)
        conf = self.config.setdefault("permission", {})
        bl = set(map(str, conf.get("blacklist_user_ids", [])))
        if action == "添加":
            bl.add(target)
        else:
            bl.discard(target)
        conf["blacklist_user_ids"] = list(bl)
        self.permission.reload(conf)
        yield event.plain_result(f"✅ 已{action}黑名单 {target}")

    @filter.command("帮助")
    async def cmd_help(self, event: AstrMessageEvent):
        yield event.plain_result(
            "🎨 ComfyUI 智能绘图指令\n"
            "/画图 <描述> - 生成图片\n"
            "/刷新模型 - 拉取并分析模型（管理员）\n"
            "/模型列表 - 查看已保存的模型\n"
            "/统计 - 查看使用统计\n"
            "/帮助 - 查看帮助"
        )

    # ---------- 辅助 ----------
    async def _ensure_models(self):
        """若模型列表为空，尝试拉取裸列表（不分析）。"""
        models = self.storage.load_models()
        if any(models.get(k) for k in ("checkpoint", "controlnet", "vae", "lora")):
            return
        try:
            lists = await self.comfy.get_model_lists()
            result = {"checkpoint": [], "controlnet": [], "vae": [], "lora": []}
            for key in ("checkpoint", "controlnet", "vae", "lora"):
                for name in lists.get(key, []):
                    result[key].append({"name": name, "style": "", "tags": []})
            self.storage.save_models(result)
        except Exception as e:
            logger.warning(f"[ComfyUI] 自动拉取模型失败: {e}")

    @staticmethod
    def _first_model(models: dict, key: str):
        items = models.get(key, [])
        return items[0]["name"] if items else ""

    # ---------- Pages 辅助方法 ----------
    def get_full_config(self) -> dict:
        """返回完整配置（供 Pages 读取）。"""
        return self.config

    def save_config(self, new_config: dict):
        """更新配置并重载相关组件（供 Pages 保存）。"""
        self.config = new_config or {}
        self.model_switch = self.config.get("model_switch", {}) or {}
        self.draw_conf = self.config.get("draw_settings", {}) or {}
        self.output_conf = self.config.get("output", {}) or {}
        # 重载权限
        self.permission.reload(self.config.get("permission", {}))
        # 重载 ComfyUI 地址
        server = self.config.get("server", {}) or {}
        self.comfy = ComfyUI(
            server.get("base_url", "http://127.0.0.1:8188"),
            int(server.get("timeout", 120) or 120),
        )
        logger.info("[ComfyUI] 配置已通过 Pages 更新")

    async def refresh_models(self) -> dict:
        """拉取 + 分析模型，供 Pages 调用。返回摘要。"""
        lists = await self.comfy.get_model_lists()
        total = sum(len(v) for v in lists.values())
        if total == 0:
            return {"ok": False, "message": "未拉取到任何模型，请检查 ComfyUI 地址"}
        analyzed = await self.llm.analyze_models(lists, self.model_switch)
        self.storage.save_models(analyzed)
        return {"ok": True, "total": total, "result": analyzed}
