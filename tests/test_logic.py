"""自包含逻辑测试：不依赖 AstrBot 与 aiohttp 的真实实现。

用法：
    python tests/test_logic.py

设计说明：
- 插件本身需要 astrbot / aiohttp。本测试在 import 之前把它们替换成最小桩实现，
  因此可以在没有 AstrBot 的机器上（含 CI）直接跑，用于验证：
  参数解析、尺寸对齐、模型名校验、ComfyUI 错误解码、模型发现的两条路径、
  模板推导与注入、存储与配额、Pages 保存的「合并而非替换」语义、以及出图主流程。
- 如果环境里装了真实的 astrbot，则不会注入桩（见 _install_stubs 的提前返回）。
"""
from __future__ import annotations

import asyncio
import copy
import json
import re
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PASSED = 0
FAILED = 0


def check(label: str, condition: bool, extra="") -> None:
    """记录一条断言结果。"""
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  PASS {label} {extra}")
    else:
        FAILED += 1
        print(f"  FAIL {label} {extra}")


def _install_stubs() -> Path:
    """在无法导入真实依赖时，写入最小桩实现并加入 sys.path。

    Returns:
        桩目录；若真实 AstrBot 可用则返回空路径。
    """
    try:
        import astrbot  # noqa: F401
        import aiohttp  # noqa: F401

        return Path()
    except ImportError:
        pass

    stub_root = Path(tempfile.mkdtemp(prefix="smart_stubs_"))
    (stub_root / "aiohttp").mkdir(parents=True, exist_ok=True)
    (stub_root / "astrbot" / "api" / "event").mkdir(parents=True, exist_ok=True)
    (stub_root / "astrbot" / "api" / "star").mkdir(parents=True, exist_ok=True)

    (stub_root / "aiohttp" / "__init__.py").write_text(AIOHTTP_STUB, encoding="utf-8")
    (stub_root / "astrbot" / "__init__.py").write_text("", encoding="utf-8")
    (stub_root / "astrbot" / "api" / "__init__.py").write_text(ASTRBOT_API_STUB, encoding="utf-8")
    (stub_root / "astrbot" / "api" / "event" / "__init__.py").write_text(EVENT_STUB, encoding="utf-8")
    (stub_root / "astrbot" / "api" / "star" / "__init__.py").write_text(STAR_STUB, encoding="utf-8")
    (stub_root / "astrbot" / "api" / "message_components.py").write_text(COMPONENTS_STUB, encoding="utf-8")
    (stub_root / "astrbot" / "api" / "web.py").write_text(WEB_STUB, encoding="utf-8")
    sys.path.insert(0, str(stub_root))
    return stub_root


def _make_importable() -> None:
    """把插件目录挂到 sys.path 上，使其可作为包导入。"""
    if str(ROOT.parent) not in sys.path:
        sys.path.insert(0, str(ROOT.parent))


AIOHTTP_STUB = '''
class ClientError(Exception):
    pass


class ClientTimeout:
    def __init__(self, total=None, **kw):
        self.total = total


class ClientResponse:
    def __init__(self, status=200, text="", payload=None):
        self.status = status
        self._text = text
        self._payload = payload

    async def text(self):
        # 真实 HTTP 响应总有 body：有 payload 时按 JSON 序列化，避免上层误判为空响应
        if not self._text and self._payload is not None:
            import json

            return json.dumps(self._payload)
        return self._text

    async def read(self):
        return (await self.text()).encode()

    async def json(self, content_type=None):
        if self._payload is not None:
            return self._payload
        import json
        return json.loads(self._text)


class FormData:
    """桩：记录 multipart 字段，供上传测试断言。"""

    def __init__(self):
        self.fields = {}

    def add_field(self, name, value, **kw):
        self.fields[name] = {"value": value, "kwargs": kw}


class _Ctx:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *exc):
        return False


class ClientSession:
    instances = []

    def __init__(self, timeout=None, **kw):
        self.timeout = timeout
        self.closed = False
        self.responses = {}
        self.calls = []
        ClientSession.instances.append(self)

    def route(self, method, path, response):
        self.responses[(method.upper(), path)] = response

    def request(self, method, url, **kw):
        from urllib.parse import urlsplit

        path = urlsplit(url).path or "/"
        self.calls.append((method.upper(), path, kw))
        return _Ctx(self.responses.get((method.upper(), path)) or ClientResponse(404, "not found"))

    def get(self, url, **kw):
        return self.request("GET", url, **kw)

    def post(self, url, **kw):
        return self.request("POST", url, **kw)

    async def close(self):
        self.closed = True
'''

ASTRBOT_API_STUB = '''
import logging

logger = logging.getLogger("astrbot-test")


class AstrBotConfig(dict):
    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.saved = 0

    def save_config(self, replace_config=None, **kw):
        self.saved += 1

    async def save_config_async(self, replace_config=None, **kw):
        self.saved += 1
        return True
'''

EVENT_STUB = '''
class AstrMessageEvent:
    def __init__(self, sender_id="10001", name="tester", message_str="", admin=False,
                 group_id="20002", message=None):
        self._sender_id = sender_id
        self._name = name
        self.message_str = message_str
        self.message_obj = type("Obj", (), {"message": list(message or [])})()
        self._admin = admin
        self._group_id = group_id
        self.unified_msg_origin = "test:FriendMessage:10001"
        self.sent = []

    def get_sender_id(self):
        return self._sender_id

    def get_sender_name(self):
        return self._name

    def is_admin(self):
        return self._admin

    def get_group_id(self):
        return self._group_id

    def plain_result(self, text):
        return {"type": "plain", "text": text}

    def chain_result(self, chain):
        return {"type": "chain", "chain": chain}

    async def send(self, result):
        self.sent.append(result)


class _Filter:
    class PermissionType:
        ADMIN = "admin"
        MEMBER = "member"

    @staticmethod
    def _passthrough(*args, **kwargs):
        def deco(fn):
            return fn
        return deco

    command = _passthrough
    permission_type = _passthrough
    llm_tool = _passthrough


filter = _Filter()
'''

STAR_STUB = '''
from pathlib import Path


class Context:
    def __init__(self):
        self.registered_web_apis = []
        self.active_tools = set()
        self._providers = {}
        self.llm_calls = []
        self.llm_reply = "{}"

    def register_web_api(self, route, handler, methods, desc):
        self.registered_web_apis.append((route, handler, methods, desc))

    def get_all_providers(self):
        return list(self._providers.values())

    def get_provider_by_id(self, provider_id):
        return self._providers.get(provider_id)

    async def get_current_chat_provider_id(self, umo=None):
        if not self._providers:
            raise RuntimeError("no provider")
        return next(iter(self._providers))

    async def llm_generate(self, **kw):
        self.llm_calls.append(kw)
        text = self.llm_reply
        return type("Resp", (), {"completion_text": text})()

    def activate_llm_tool(self, name):
        self.active_tools.add(name)
        return True

    def deactivate_llm_tool(self, name):
        self.active_tools.discard(name)
        return True


class Star:
    # 刻意与 AstrBot v4.26.0 一致：基类不提供 self.logger，
    # 用于验证 main.py 里的 logger 回退真的生效（v4.27.3 起才有该属性）。
    def __init__(self, context, config=None):
        self.context = context


class StarTools:
    _root = None

    @classmethod
    def set_root(cls, path):
        cls._root = Path(path)

    @classmethod
    def get_data_dir(cls, plugin_name=None):
        base = cls._root or Path("/tmp/astrbot_stub_data")
        target = Path(base) / "plugin_data" / (plugin_name or "unknown")
        target.mkdir(parents=True, exist_ok=True)
        return target


def register(*args, **kwargs):
    def deco(cls):
        return cls
    return deco
'''

WEB_STUB = '''
class _Response(dict):
    """把 json_response 的结果包成可检查的 dict。"""

    def __init__(self, payload, status_code=200):
        super().__init__(payload if isinstance(payload, dict) else {"data": payload})
        self.status_code = status_code
        self.payload = payload


def json_response(payload=None, status_code=200, **kw):
    return _Response(payload or {}, status_code)


def error_response(message="", status_code=400, **kw):
    return _Response({"status": "error", "message": message}, status_code)


def file_response(path, filename=None, content_type=None, **kw):
    return _Response({"file": str(path)}, 200)


class _Request:
    """请求上下文桩。"""

    def __init__(self):
        self.query = {}
        self.path_params = {}
        self.plugin_name = "test"
        self.username = "tester"
        self._json = {}

    async def json(self, default=None):
        return self._json if self._json else (default if default is not None else {})


request = _Request()
'''

COMPONENTS_STUB = '''
class Plain:
    def __init__(self, text=""):
        self.text = text


class At:
    def __init__(self, qq=""):
        self.qq = qq


class Image:
    """桩：file 里放本地路径，convert_to_file_path 直接返回它。"""

    def __init__(self, path="", file=None):
        self.path = path or (file or "")
        self.file = self.path
        self.url = ""

    @classmethod
    def fromFileSystem(cls, path):
        return cls(path)

    async def convert_to_file_path(self):
        return self.path or None

    async def convert_to_base64(self):
        return "AAAA"


class Reply:
    """桩：chain 是被引用消息的组件列表。"""

    def __init__(self, chain=None, **kw):
        self.chain = chain or []
'''


def main() -> int:
    """运行全部断言，返回进程退出码。"""
    _install_stubs()
    _make_importable()

    import astrbot_plugin_comfyui_smart.main as m
    from astrbot_plugin_comfyui_smart import comfyui_api as api
    from astrbot_plugin_comfyui_smart import llm_service as llm
    from astrbot_plugin_comfyui_smart import permission as pm
    from astrbot_plugin_comfyui_smart import storage as st
    from astrbot_plugin_comfyui_smart import workflow_templates as wt
    from astrbot_plugin_comfyui_smart import pages
    from astrbot.api.star import Context, StarTools

    print("=== 行内参数解析 ===")
    _, opts = m.parse_inline_params("16:9 一个白裙少女 --seed 42 --steps 30")
    check("识别 --seed/--steps", opts.get("seed") == "42" and opts.get("steps") == "30", opts)
    desc, opts2 = m.parse_inline_params("赛博朋克城市 --lora style/a.safetensors:0.8 --negative blur")
    check("--lora 保留强度", opts2.get("lora") == "style/a.safetensors:0.8", opts2)
    check("--negative 取值", opts2.get("negative") == "blur", opts2)
    check("描述被正确剥离", desc == "赛博朋克城市", repr(desc))

    print("\n=== 尺寸对齐 ===")
    w, h = m._clamp_dimensions(1023, 1025)
    check("对齐到 8 的倍数", w % 8 == 0 and h % 8 == 0, (w, h))
    w, h = m._clamp_dimensions(4096, 4096)
    check("不超总像素上限", w * h <= m.MAX_PIXELS, (w, h))

    print("\n=== 模型名匹配（旧版会丢弃子目录模型）===")
    pool = ["SDXL/juggernautXL.safetensors", "anything-v5.safetensors", "style/anime.safetensors"]
    check("完全一致", m._match_model("anything-v5.safetensors", pool) == "anything-v5.safetensors")
    check("子目录精确匹配", m._match_model("SDXL/juggernautXL.safetensors", pool) == "SDXL/juggernautXL.safetensors")
    check("省略目录前缀可匹配", m._match_model("juggernautXL.safetensors", pool) == "SDXL/juggernautXL.safetensors")
    check("唯一子串匹配", m._match_model("anime", pool) == "style/anime.safetensors")
    check("不存在的名字被拒绝", m._match_model("目录项名称", pool) == "")

    print("\n=== 配置深度合并（旧版整体替换会清空未提交字段）===")
    merged = m._deep_merge({"a": {"x": 1, "y": 2}, "b": 3}, {"a": {"y": 9}})
    check("未提交字段保留", merged == {"a": {"x": 1, "y": 9}, "b": 3}, merged)

    print("\n=== ComfyUI 错误解码 ===")
    err = {"node_errors": {"1": {"errors": [{"type": "value_not_in_list", "extra_info": {
        "input_name": "ckpt_name", "received_value": "nope.safetensors",
        "list_content": ["a.safetensors", "b.safetensors"]}}]}}}
    msg = api.format_submit_error(err, 400)
    check("value_not_in_list 可读", "ckpt_name" in msg and "nope.safetensors" in msg and "a.safetensors" in msg, msg)
    msg2 = api.format_submit_error({"node_errors": {"5": {"errors": [
        {"type": "missing_node_type", "extra_info": {"node_type": "FooBar"}}]}}}, 400)
    check("missing_node_type 可读", "FooBar" in msg2, msg2)
    check("无输出节点提示 SaveImage", "SaveImage" in api.format_submit_error({"error": {"type": "prompt_no_outputs"}}, 400))

    print("\n=== object_info 两种 schema ===")
    out_v2: list[str] = []
    out_v3: list[str] = []
    api._collect_string_lists([["a.safetensors", "b.safetensors"], {"tooltip": "x"}], out_v2)
    api._collect_string_lists({"type": "COMBO", "options": ["c.safetensors"]}, out_v3)
    check("V2 格式", out_v2 == ["a.safetensors", "b.safetensors"], out_v2)
    check("V3 combo 格式", out_v3 == ["c.safetensors"], out_v3)

    print("\n=== 提交失败的报错信息必须可定位 ===")
    # ComfyUI 真正的原因分散在 error.details 与 node_errors 里，都要带出来
    rich = {
        "error": {"type": "prompt_outputs_failed_validation",
                  "message": "Prompt outputs failed validation",
                  "details": "Return type mismatch between linked nodes: model, MODEL != CLIP"},
        "node_errors": {"8": {"class_type": "LoraLoader", "errors": [
            {"type": "value_not_in_list",
             "extra_info": {"input_name": "lora_name", "received_value": "ghost.safetensors",
                            "list_content": ["a.safetensors", "b.safetensors"]}},
            {"type": "required_input_missing", "extra_info": {"input_name": "clip"}}]}},
    }
    rich_msg = api.format_submit_error(rich, 400)
    check("带出 error.details", "Return type mismatch" in rich_msg, rich_msg[:120])
    check("带出节点级原因", "lora_name" in rich_msg and "ghost.safetensors" in rich_msg)
    check("同一节点的多条原因都列出", "clip" in rich_msg)
    check("报错控制在可读长度内", len(rich_msg) < 1000, len(rich_msg))

    bare = {"error": {"type": "prompt_outputs_failed_validation",
                      "message": "Prompt outputs failed validation", "details": ""},
            "node_errors": {}}
    bare_msg = api.format_submit_error(bare, 400)
    check("无节点级原因时给出排查指引", "ComfyUI 控制台" in bare_msg, bare_msg[:120])

    exc_info = {"error": {"type": "prompt_outputs_failed_validation",
                          "message": "Prompt outputs failed validation",
                          "details": "boom", "extra_info": {"exception_type": "AttributeError"}},
                "node_errors": {}}
    check("校验期异常类型被带出", "AttributeError" in api.format_submit_error(exc_info, 400))

    check("无输出节点给出 SaveImage 提示",
          "SaveImage" in api.format_submit_error({"error": {"type": "prompt_no_outputs"}}, 400))

    print("\n=== 产物过滤与队列解析 ===")
    imgs = api._collect_output_images({"9": {"images": [
        {"filename": "out.png", "type": "output"},
        {"filename": "preview.png", "type": "temp"}]}})
    check("只保留 output 产物", len(imgs) == 1 and imgs[0]["filename"] == "out.png", imgs)

    client = api.ComfyUI("127.0.0.1:8188")
    session = api.aiohttp.ClientSession()
    session.route("GET", "/queue", api.aiohttp.ClientResponse(200, payload={
        "queue_running": [[0, "run-1", {}, {"client_id": "other"}]],
        "queue_pending": [[1, "pend-1", {}, {"client_id": client.client_id}]]}))
    client._session = session
    qs = asyncio.run(client.queue_status())
    check("只统计自己的任务", qs.own_running == 0 and qs.own_pending == 1, (qs.own_running, qs.own_pending))
    check("队列位置与前方任务数", qs.own_positions.get("pend-1") == 2 and qs.tasks_ahead == 1, qs)

    print("\n=== 模型发现的两条路径 ===")
    c2 = api.ComfyUI("127.0.0.1:8188")
    s2 = api.aiohttp.ClientSession()
    s2.route("GET", "/models", api.aiohttp.ClientResponse(200, payload=["checkpoints", "loras"]))
    s2.route("GET", "/models/checkpoints", api.aiohttp.ClientResponse(200, payload=["SDXL/m.safetensors"]))
    s2.route("GET", "/models/loras", api.aiohttp.ClientResponse(200, payload=["style/a.safetensors"]))
    c2._session = s2
    cat = asyncio.run(c2.discover_models())
    check("/models + /models/{folder} 生效", cat.get("checkpoints") == ["SDXL/m.safetensors"], cat)

    c3 = api.ComfyUI("127.0.0.1:8188")
    s3 = api.aiohttp.ClientSession()
    object_info_payload = {
        # V2 写法：[[名字...], {tooltip}]
        "CheckpointLoaderSimple": {
            "input": {"required": {"ckpt_name": [["x.safetensors"], {"tooltip": "t"}]}}
        },
        # V3 写法：combo 描述
        "LoraLoader": {
            "input": {"required": {"lora_name": {"type": "COMBO", "options": ["y.safetensors"]}}}
        },
    }
    s3.route("GET", "/object_info", api.aiohttp.ClientResponse(200, payload=object_info_payload))
    c3._session = s3
    cat3 = asyncio.run(c3.discover_models())
    check("object_info 回退兼容 V2+V3", cat3.get("checkpoints") == ["x.safetensors"] and cat3.get("loras") == ["y.safetensors"], cat3)

    print("\n=== 模型目录白名单（防止把 custom_nodes 当成模型）===")
    check("custom_nodes/configs/datasets 被排除",
          all(f not in api._order_folders([f]) for f in ("custom_nodes", "configs", "datasets")),
          api._order_folders(["custom_nodes", "configs", "datasets"]))
    check("embeddings / vae_approx 被排除（文本反演与预览小模型）",
          api._order_folders(["embeddings", "vae_approx"]) == [])
    ordered = api._order_folders(["custom_nodes", "loras", "checkpoints", "upscale_models"])
    check("只保留模型目录且主用途在前",
          ordered == ["checkpoints", "loras", "upscale_models"], ordered)

    s9 = api.aiohttp.ClientSession()
    # 模拟真实 ComfyUI：/models 返回全部键，其中 custom_nodes 会递归出上万个文件
    s9.route("GET", "/models", api.aiohttp.ClientResponse(200, payload=[
        "checkpoints", "loras", "custom_nodes", "configs", "datasets", "embeddings"]))
    s9.route("GET", "/models/checkpoints", api.aiohttp.ClientResponse(200, payload=["a.safetensors"]))
    s9.route("GET", "/models/loras", api.aiohttp.ClientResponse(200, payload=["b.safetensors"]))
    s9.route("GET", "/models/custom_nodes", api.aiohttp.ClientResponse(
        200, payload=[f"pkg/node_modules/f{i}.js" for i in range(2935)]))
    s9.route("GET", "/models/configs", api.aiohttp.ClientResponse(200, payload=["x.json"]))
    s9.route("GET", "/models/datasets", api.aiohttp.ClientResponse(200, payload=["y.csv"]))
    s9.route("GET", "/models/embeddings", api.aiohttp.ClientResponse(200, payload=["e.pt"]))
    c9 = api.ComfyUI("127.0.0.1:8188")
    c9._session = s9
    cat9 = asyncio.run(c9.discover_models())
    total9 = sum(len(v) for v in cat9.values())
    check("2935 个 custom_nodes 文件不会被计入模型数", total9 == 2, f"total={total9} {list(cat9)}")
    check("清单里只有真正的模型目录", set(cat9) == {"checkpoints", "loras"}, list(cat9))

    print("\n=== 存储与配额 ===")
    data_dir = Path(tempfile.mkdtemp(prefix="smart_data_"))
    storage = st.Storage(data_dir)

    async def storage_flow():
        await storage.save_catalog({"checkpoints": ["a.safetensors"]})
        assert storage.load_catalog() == {"checkpoints": ["a.safetensors"]}
        await storage.record_usage("u1", "2026-01-01", cooldown_until=time.time() + 100)
        assert await storage.get_daily_count("u1", "2026-01-01") == 1
        assert await storage.get_cooldown_until("u1") > time.time()
        await storage.record_generation(user_id="u1", user_name="n", positive="p", negative="q",
                                        models={"checkpoint": "a.safetensors"},
                                        images=["images/x.png"], seconds=1.5)
        stats = storage.load_stats()
        assert stats["users"]["u1"]["count"] == 1
        assert stats["model_usage"]["checkpoint"]["a.safetensors"] == 1
        assert stats["records"][-1]["images"] == ["images/x.png"]

    asyncio.run(storage_flow())
    check("catalog / 配额 / 统计往返", True)

    for i in range(5):
        (storage.output_dir / f"f{i}.png").write_bytes(b"x")
        time.sleep(0.01)
    removed = storage.prune_images(keep=2, max_age_days=0)
    check("图片保留策略", removed == 3 and len(list(storage.output_dir.iterdir())) == 2, f"removed={removed}")

    print("\n=== 权限 ===")
    pmgr = pm.PermissionManager({"blacklist_user_ids": ["bad"], "whitelist_user_ids": ["good"],
                                 "daily_limit": 1, "cooldown_seconds": 60})

    async def perm_flow():
        ok, _ = await pmgr.check("good", is_admin=False, storage=storage)
        assert ok, "白名单用户应放行"
        ok, why = await pmgr.check("other", is_admin=False, storage=storage)
        assert not ok and "白名单" in why, why
        ok, why = await pmgr.check("bad", is_admin=True, storage=storage)
        assert not ok and "黑名单" in why, "黑名单应优先于管理员"
        ok, _ = await pmgr.check("other", is_admin=True, storage=storage)
        assert ok, "管理员应豁免白名单"

    asyncio.run(perm_flow())
    check("白名单/黑名单/管理员豁免", True)

    print("\n=== 模板推导与注入 ===")
    templates = wt.load_templates(ROOT / "workflows")
    check("内置模板齐全（含图生图模板）",
          {"sd_checkpoint", "flux_unet", "flux_checkpoint", "img2img_checkpoint"}
          <= set(templates), sorted(templates))
    tpl, arch = wt.pick_template(templates, model_name="flux1-dev-fp8.safetensors", model_folder="diffusion_models")
    check("Flux 分离权重走 flux_unet", tpl.name == "flux_unet" and arch == "flux", (tpl.name, arch))
    tpl2, arch2 = wt.pick_template(templates, model_name="SDXL/juggernautXL.safetensors", model_folder="checkpoints")
    check("SDXL 走通用 checkpoint 模板", tpl2.name == "sd_checkpoint" and arch2 == "sdxl", (tpl2.name, arch2))
    graph = tpl2.build(positive="1girl", negative="lowres", model_name="SDXL/juggernautXL.safetensors",
                       width=1024, height=1024, steps=28, cfg=6.0, sampler="dpmpp_2m",
                       scheduler="karras", seed=1)
    wt.validate_graph(graph)
    check("参数注入生效", graph["5"]["inputs"]["seed"] == 1 and graph["4"]["inputs"]["width"] == 1024)
    gflux = tpl.build(positive="cat", negative="SHOULD_NOT_APPEAR",
                      model_name="flux1-dev-fp8.safetensors", vae_name="ae.safetensors",
                      cfg=1.0, guidance=3.5, seed=2, width=1024, height=1024)
    check("Flux 忽略负向提示词", gflux["5"]["inputs"]["text"] == "", repr(gflux["5"]["inputs"]["text"]))
    check("Flux guidance 注入", gflux["6"]["inputs"]["guidance"] == 3.5)
    dirty = dict(graph)
    dirty["999"] = {"class_type": "MissingCustomNode", "inputs": {}}
    check("不可达节点被剔除", len(wt.prune_unreachable(dirty)) == len(graph))
    try:
        wt.validate_graph({"1": {"class_type": "KSampler", "inputs": {"model": ["404", 0]}}})
        check("坏连线被校验拦下", False)
    except wt.TemplateError:
        check("坏连线被校验拦下", True)

    print("\n=== 按节点 title 注入（自定义提示词节点）===")

    def _base_graph(prompt_class: str, prompt_key: str, title_pos: str, title_neg: str) -> dict:
        """构造一个提示词节点「不叫 text」的工作流，用于验证标题兜底与绑定覆盖。"""
        return {
            "1": {"class_type": "CheckpointLoaderSimple",
                  "inputs": {"ckpt_name": "m.safetensors"}},
            "2": {"class_type": prompt_class, "_meta": {"title": title_pos},
                  "inputs": {prompt_key: "", "clip": ["1", 1]}},
            "3": {"class_type": prompt_class, "_meta": {"title": title_neg},
                  "inputs": {prompt_key: "", "clip": ["1", 1]}},
            "4": {"class_type": "EmptyLatentImage",
                  "inputs": {"width": 1024, "height": 1024, "batch_size": 1}},
            "5": {"class_type": "KSampler",
                  "inputs": {"model": ["1", 0], "seed": 0, "steps": 20, "cfg": 6.0,
                             "sampler_name": "euler", "scheduler": "normal",
                             "positive": ["2", 0], "negative": ["3", 0],
                             "latent_image": ["4", 0], "denoise": 1.0}},
            "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
            "7": {"class_type": "SaveImage",
                  "inputs": {"images": ["6", 0], "filename_prefix": "x"}},
        }

    # 自定义节点名与输入键都不常规：靠标题定位，且必须写进真实存在的输入键
    odd = wt.WorkflowTemplate(
        "odd",
        _base_graph("MyOddPromptNode", "wildcard_text", "正面提示词", "负面提示词"),
    )
    check("标题兜底找到正向节点", odd.bindings["positive"] == ("2", "wildcard_text"),
          odd.bindings["positive"])
    check("标题兜底找到负向节点", odd.bindings["negative"] == ("3", "wildcard_text"),
          odd.bindings["negative"])
    odd_built = odd.build(positive="正", negative="负", model_name="m.safetensors")
    check("提示词写入真实存在的键", odd_built["2"]["inputs"]["wildcard_text"] == "正"
          and odd_built["3"]["inputs"]["wildcard_text"] == "负"
          and "text" not in odd_built["2"]["inputs"], odd_built["2"]["inputs"])

    # 清单里显式按 title / 按节点 id 覆盖
    overridden = wt.WorkflowTemplate(
        "ov",
        _base_graph("MyOddPromptNode", "wildcard_text", "随便一个标题", "另一个标题"),
        bindings={
            "positive": {"title": "随便一个标题"},
            "negative": {"node": "3", "input": "wildcard_text"},
            "sampler": {"node": "5"},
        },
    )
    check("按 title 覆盖正向", overridden.bindings["positive"] == ("2", "wildcard_text"),
          overridden.bindings["positive"])
    check("按节点 id 覆盖负向", overridden.bindings["negative"] == ("3", "wildcard_text"),
          overridden.bindings["negative"])
    check("覆盖采样器节点", overridden.bindings["sampler"] == "5")

    for bad, label in (
        ({"positive": {"node": "999"}}, "指向不存在的节点"),
        ({"positive": {"title": "不存在的标题"}}, "标题匹配不到节点"),
        ({"positive": {"node": "2", "input": "nope"}}, "输入键不存在"),
        ({"unknown_role": {"node": "2"}}, "未知角色名"),
    ):
        try:
            wt.WorkflowTemplate("bad", _base_graph("MyOddPromptNode", "wildcard_text", "a", "b"),
                                bindings=bad)
            check(f"非法 bindings 被拒绝（{label}）", False)
        except wt.TemplateError:
            check(f"非法 bindings 被拒绝（{label}）", True)

    print("\n=== 能力探测（缺节点不该发过去再被拒）===")
    core = {"CheckpointLoaderSimple", "CLIPTextEncode", "MyOddPromptNode", "EmptyLatentImage",
            "KSampler", "VAEDecode", "SaveImage"}

    def _mk(name: str, arch: str, decode_class: str = "VAEDecode") -> wt.WorkflowTemplate:
        graph = _base_graph("CLIPTextEncode", "text", "正面提示词", "负面提示词")
        graph["6"]["class_type"] = decode_class
        return wt.WorkflowTemplate(name, graph, arch=arch, loader="checkpoint")

    tpl_core = _mk("core_only", "sdxl")
    tpl_custom = _mk("needs_custom", "sdxl", decode_class="MySpecialDecode")
    check("缺节点集合计算正确",
          tpl_custom.missing_nodes(core) == {"MySpecialDecode"}, tpl_custom.missing_nodes(core))
    check("节点齐全时无缺失", tpl_core.missing_nodes(core) == set())
    check("探测失败时不误判", tpl_core.missing_nodes(set()) == set() and tpl_custom.missing_nodes(None) == set())

    chosen, _ = wt.pick_template({"needs_custom": tpl_custom, "core_only": tpl_core},
                                 model_name="SDXL/m.safetensors", model_folder="checkpoints",
                                 available_nodes=core)
    check("能力探测跳过缺节点的模板", chosen.name == "core_only", chosen.name)
    chosen2, _ = wt.pick_template({"needs_custom": tpl_custom, "core_only": tpl_core},
                                  model_name="SDXL/m.safetensors", model_folder="checkpoints")
    check("不传能力信息时仍能选出模板", chosen2 is not None, chosen2.name)

    print("\n=== LLM 输出解析 ===")
    r = llm.parse_optimize_result('```json\n{"positive":"a, b","negative":"c","lora_strength":0.8}\n```')
    check("代码块容错", r["raw_ok"] and r["positive"] == "a, b" and r["lora_strength"] == 0.8, r)
    check("非 JSON 不崩", llm.parse_optimize_result("这不是 JSON")["raw_ok"] is False)

    print("\n=== 插件装配与 Pages ===")
    StarTools.set_root(tempfile.mkdtemp(prefix="smart_root_"))
    ctx = Context()
    from astrbot.api import AstrBotConfig

    cfg = AstrBotConfig({"server": {"base_url": "127.0.0.1:8188"},
                         "llm_settings": {"enable_prompt_optimize": False}})
    plugin = m.ComfyUISmartPlugin(ctx, cfg)
    routes = [r[0] for r in ctx.registered_web_apis]
    # 回归守卫：曾经因为漏调 register_pages_routes，Pages 的每个请求都被
    # Dashboard 回以「未找到该路由」。这里**不手动调用**，只断言构造即注册。
    check("构造插件时自动注册 Pages 路由（漏调会导致全部 404）",
          len(ctx.registered_web_apis) >= 9, f"{len(ctx.registered_web_apis)} 条")
    check("plugin.pages_ready 为真", plugin.pages_ready is True)
    check("Pages 路由齐全", all(any(k in r for r in routes) for k in (
        "/config", "/models", "/models/refresh", "/templates", "/status", "/stats", "/images/")), routes)
    check("地址自动补协议", plugin.comfy.base_url == "http://127.0.0.1:8188", plugin.comfy.base_url)
    check("无指令出图默认关闭", "generate_image" not in ctx.active_tools)
    import logging as _logging

    check("基类无 logger 时回退到全局 logger",
          isinstance(getattr(plugin, "logger", None), _logging.Logger),
          type(getattr(plugin, "logger", None)).__name__)

    print("\n=== Pages 处理器端到端（路由 → 处理器 → 响应结构）===")
    handlers = {}
    for route, handler, methods, _desc in ctx.registered_web_apis:
        handlers[(route, tuple(methods))] = handler

    s_ui = api.aiohttp.ClientSession()
    s_ui.route("GET", "/models", api.aiohttp.ClientResponse(200, payload=["checkpoints"]))
    s_ui.route("GET", "/models/checkpoints", api.aiohttp.ClientResponse(
        200, payload=["SDXL/m.safetensors"]))
    s_ui.route("GET", "/system_stats", api.aiohttp.ClientResponse(
        200, payload={"devices": [{"name": "FakeGPU", "vram_total": 1, "vram_free": 1}]}))
    s_ui.route("GET", "/queue", api.aiohttp.ClientResponse(
        200, payload={"queue_running": [], "queue_pending": []}))
    plugin.comfy._session = s_ui
    plugin.comfy.invalidate_model_cache()

    base = "/astrbot_plugin_comfyui_smart"
    cfg_resp = asyncio.run(handlers[(f"{base}/config", ("GET",))]())
    check("config 处理器返回 config 字段", "config" in cfg_resp, list(cfg_resp)[:4])

    models_resp = asyncio.run(handlers[(f"{base}/models", ("GET",))]())
    check("models 处理器返回非空 catalog",
          models_resp.get("catalog", {}).get("checkpoints") == ["SDXL/m.safetensors"],
          models_resp)

    status_resp = asyncio.run(handlers[(f"{base}/status", ("GET",))]())
    check("status 处理器返回连接信息",
          status_resp.get("online") is True and status_resp.get("device") == "FakeGPU",
          {k: status_resp.get(k) for k in ("online", "device", "base_url")})

    tpl_resp = asyncio.run(handlers[(f"{base}/templates", ("GET",))]())
    check("templates 处理器返回内置模板（含 purpose 字段）",
          {"sd_checkpoint", "img2img_checkpoint"}
          <= {t.get("name") for t in tpl_resp.get("templates", [])}
          and all("purpose" in t for t in tpl_resp.get("templates", [])),
          [(t.get("name"), t.get("purpose")) for t in tpl_resp.get("templates", [])])

    stats_resp = asyncio.run(handlers[(f"{base}/stats", ("GET",))]())
    check("stats 处理器返回统计结构", "users" in stats_resp and "records" in stats_resp)

    img_file = plugin.storage.output_dir / "probe.png"
    img_file.write_bytes(b"PNG")
    img_resp = asyncio.run(handlers[(f"{base}/images/<filename>", ("GET",))](filename="probe.png"))
    check("images 处理器能取到图片", img_resp.get("file", "").endswith("probe.png"), img_resp)
    missing = asyncio.run(handlers[(f"{base}/images/<filename>", ("GET",))](filename="../secret"))
    check("images 处理器拒绝目录穿越（取的是文件名，不拼路径）",
          getattr(missing, "status_code", None) == 404, missing)

    refresh_resp = asyncio.run(handlers[(f"{base}/models/refresh", ("POST",))]())
    check("models/refresh 处理器能重新发现并写回清单",
          refresh_resp.get("ok") is True and refresh_resp.get("total") == 1, refresh_resp)

    # 刷新成功后清单已落盘：即使 ComfyUI 暂时不可达，模型页也应能展示上次结果
    plugin.comfy._session = api.aiohttp.ClientSession()  # 全部 404
    plugin.comfy.invalidate_model_cache()
    offline = asyncio.run(handlers[(f"{base}/models", ("GET",))]())
    check("ComfyUI 不可达时回退到磁盘缓存而非报空",
          offline.get("catalog", {}).get("checkpoints") == ["SDXL/m.safetensors"], offline)

    async def save_flow():
        await plugin.save_config({"permission": {"daily_limit": 5}})
        return plugin.config

    saved_config = asyncio.run(save_flow())
    check("Pages 保存走合并语义", saved_config["permission"]["daily_limit"] == 5
          and saved_config["server"]["base_url"] == "127.0.0.1:8188", saved_config.get("server"))
    check("Pages 保存确实落盘", getattr(plugin.config, "saved", 0) >= 1)
    plugin.config["agent"] = {"enable_llm_tool": True}
    plugin._sync_llm_tool()
    check("开关可激活 LLM 工具", "generate_image" in ctx.active_tools, ctx.active_tools)

    print("\n=== 出图主流程（mock ComfyUI）===")
    s4 = api.aiohttp.ClientSession()
    s4.route("GET", "/models", api.aiohttp.ClientResponse(200, payload=["checkpoints", "loras", "vae"]))
    s4.route("GET", "/models/checkpoints", api.aiohttp.ClientResponse(200, payload=["SDXL/m.safetensors"]))
    s4.route("GET", "/models/loras", api.aiohttp.ClientResponse(200, payload=[]))
    s4.route("GET", "/models/vae", api.aiohttp.ClientResponse(200, payload=[]))
    s4.route("POST", "/prompt", api.aiohttp.ClientResponse(200, payload={"prompt_id": "pid-1", "number": 1}))
    s4.route("GET", "/queue", api.aiohttp.ClientResponse(200, payload={"queue_running": [], "queue_pending": []}))
    s4.route("GET", "/history/pid-1", api.aiohttp.ClientResponse(200, payload={"pid-1": {
        "status": {"status_str": "success", "completed": True},
        "outputs": {"7": {"images": [{"filename": "out.png", "subfolder": "", "type": "output"}]}}}}))
    s4.route("GET", "/view", api.aiohttp.ClientResponse(200, text="PNGDATA"))
    plugin.comfy._session = s4

    result = asyncio.run(plugin.generate(user_desc="一个白裙少女", opts={}))
    check("取到并落盘图片", len(result["images"]) == 1 and result["images"][0].read_bytes() == b"PNGDATA")
    check("只使用真实清单里的底模", result["model"] == "SDXL/m.safetensors", result["model"])
    check("模板与架构识别", result["template"] == "sd_checkpoint" and result["arch"] == "sdxl",
          (result["template"], result["arch"]))
    check("SDXL 自动 1024x1024", (result["width"], result["height"]) == (1024, 1024))
    check("SDXL 自动 cfg/采样器", result["cfg"] == 6.0 and result["sampler"] == "dpmpp_2m",
          (result["cfg"], result["sampler"]))

    print("\n=== LLM 不可用时自动退化（否则装了也用不了）===")
    plugin.config["llm_settings"] = {"enable_prompt_optimize": True}
    s7 = api.aiohttp.ClientSession()
    s7.route("GET", "/models", api.aiohttp.ClientResponse(200, payload=["checkpoints"]))
    s7.route("GET", "/models/checkpoints", api.aiohttp.ClientResponse(200, payload=["SDXL/m.safetensors"]))
    s7.route("POST", "/prompt", api.aiohttp.ClientResponse(200, payload={"prompt_id": "pid-2"}))
    s7.route("GET", "/queue", api.aiohttp.ClientResponse(200, payload={"queue_running": [], "queue_pending": []}))
    s7.route("GET", "/history/pid-2", api.aiohttp.ClientResponse(200, payload={"pid-2": {
        "status": {"status_str": "success", "completed": True},
        "outputs": {"7": {"images": [{"filename": "b.png", "type": "output"}]}}}}))
    s7.route("GET", "/view", api.aiohttp.ClientResponse(200, text="PNG2"))
    plugin.comfy._session = s7
    plugin.comfy.invalidate_model_cache()
    fallback = asyncio.run(plugin.generate(user_desc="一只白猫", opts={}))
    check("无可用 LLM 仍能出图", fallback["positive"] == "一只白猫" and bool(fallback["llm_note"]),
          fallback["llm_note"])
    check("退化后仍走真实的模型与模板", fallback["model"] == "SDXL/m.safetensors",
          fallback["model"])

    print("\n=== 回归：LoRA 注入曾产生自环（真实服务器上复现过）===")
    # 事故回顾：重接下游的循环把「刚插入的 LoraLoader」自己也算了进去，
    # 把它的 clip 改成指向自身，形成依赖环；ComfyUI 只回一句
    # prompt_outputs_failed_validation，不给任何节点级原因，极难排查。
    base_tpl = wt.load_templates(ROOT / "workflows")["sd_checkpoint"]
    with_lora = base_tpl.build(
        positive="1girl", negative="lowres", model_name="m.safetensors",
        lora_name="style/a.safetensors", lora_strength=0.8,
        width=512, height=768, steps=20, cfg=6.5,
        sampler="dpmpp_2m", scheduler="karras", seed=1)
    wt.validate_graph(with_lora)  # 自环会在这里抛 TemplateError

    lora_nodes = [nid for nid, n in with_lora.items() if n["class_type"] == "LoraLoader"]
    check("LoRA 节点被插入", len(lora_nodes) == 1, lora_nodes)
    lora_id = lora_nodes[0]
    check("LoRA 的 model 接到底模", with_lora[lora_id]["inputs"]["model"] == ["1", 0],
          with_lora[lora_id]["inputs"]["model"])
    check("LoRA 的 clip 接到底模而不是自己（曾经的 bug）",
          with_lora[lora_id]["inputs"]["clip"] == ["1", 1],
          with_lora[lora_id]["inputs"]["clip"])
    self_links = [
        (nid, k) for nid, n in with_lora.items()
        for k, v in (n.get("inputs") or {}).items()
        if isinstance(v, list) and v[0] == nid
    ]
    check("整张图没有任何自环", not self_links, self_links)
    check("采样器的 model 被改接到 LoRA", with_lora["5"]["inputs"]["model"] == [lora_id, 0],
          with_lora["5"]["inputs"]["model"])
    check("提示词节点的 clip 被改接到 LoRA",
          with_lora["2"]["inputs"]["clip"] == [lora_id, 1]
          and with_lora["3"]["inputs"]["clip"] == [lora_id, 1],
          (with_lora["2"]["inputs"]["clip"], with_lora["3"]["inputs"]["clip"]))

    # 自环校验本身要有牙齿
    try:
        wt.validate_graph({"1": {"class_type": "LoraLoader", "inputs": {"clip": ["1", 1]}}})
        check("自环会被校验拦下", False)
    except wt.TemplateError as exc:
        check("自环会被校验拦下", "自身" in str(exc) or "依赖环" in str(exc), str(exc)[:50])

    print("\n=== 回归：模板与架构必须相容 ===")
    tpls = wt.load_templates(ROOT / "workflows")
    # SD1.5 模型放在 diffusion_models 里时，不允许退回 Flux 模板
    bad, bad_arch = wt.pick_template(tpls, model_name="anything-v5-PrtRE.safetensors",
                                     model_folder="diffusion_models")
    check("SD1.5 模型不会套上 Flux 模板", bad is None and bad_arch == "sd15", (bad, bad_arch))
    good, good_arch = wt.pick_template(tpls, model_name="flux1-dev-fp8.safetensors",
                                       model_folder="diffusion_models")
    check("Flux 模型正常拿到 flux_unet", good is not None and good.name == "flux_unet",
          (good.name if good else None, good_arch))
    check("is_compatible 语义", wt.is_compatible(base_tpl, "flux")
          and not wt.is_compatible(tpls["flux_unet"], "sd15")
          and wt.is_compatible(tpls["flux_unet"], "flux"))

    print("\n=== 架构启发式（用真实模型名验证）===")
    for name, want in (
        ("juggernautXL_v9Rdphoto2Lightning.safetensors", "sdxl"),
        ("AnythingXL_xl.safetensors", "sdxl"),
        ("albedobaseXL_v21.safetensors", "sdxl"),
        ("AbyssOrangeMix2_hard.safetensors", "sd15"),
        ("CounterfeitV30_v30.safetensors", "sd15"),
        ("chilloutmix_NiPrunedFp32Fix.safetensors", "sd15"),
        ("3Guofeng3_v34.safetensors", "sd15"),
        ("ponyDiffusionV6XL.safetensors", "pony"),
        ("flux1-dev-fp8.safetensors", "flux"),
        ("some_unknown_model_v1.safetensors", "sd15"),  # 未识别时兜底 sd15
    ):
        got = wt.guess_arch(name)
        check(f"{name[:34]:<34} -> {want}", got == want, got)
    check("配置可强制指定架构", wt.guess_arch("whatever.safetensors", "sdxl") == "sdxl")
    check("非法覆盖值被忽略", wt.guess_arch("juggernautXL.safetensors", "不存在的架构") == "sdxl")

    print("\n=== 出图前的能力校验报错 ===")
    s8 = api.aiohttp.ClientSession()
    s8.route("GET", "/models", api.aiohttp.ClientResponse(200, payload=["checkpoints"]))
    s8.route("GET", "/models/checkpoints", api.aiohttp.ClientResponse(200, payload=["SDXL/m.safetensors"]))
    # 故意不提供 SaveImage -> 内置模板全都缺节点
    partial = {k: {"input": {"required": {}}} for k in core if k != "SaveImage"}
    s8.route("GET", "/object_info", api.aiohttp.ClientResponse(200, payload=partial))
    plugin.comfy._session = s8
    plugin.comfy.invalidate_model_cache()
    try:
        asyncio.run(plugin.generate(user_desc="一只猫", opts={}))
        check("缺节点时给出明确中文报错", False)
    except api.ComfyUIError as exc:
        text = str(exc)
        check("缺节点时给出明确中文报错", "没有安装" in text and "SaveImage" in text, text[:90])

    print("\n=== 画质：质量词与负面词合并（真实服务器上画质问题的对治）===")
    check("merge_tags 去重且保序",
          m.merge_tags("a, b", "B, c", "") == "a, b, c", m.merge_tags("a, b", "B, c", ""))
    check("手部规避词覆盖手指与肢体",
          all(k in m.ANATOMY_NEGATIVE for k in
              ("bad hands", "extra fingers", "fewer fingers", "fused fingers",
               "extra digits", "mutated hands", "malformed limbs")),
          m.ANATOMY_NEGATIVE[:60])

    # 用 mock 捕获实际提交的图，检查正/负向提示词
    def capture_graph(desc, opts=None, arch_model="3Guofeng3_v34.safetensors"):
        sess = api.aiohttp.ClientSession()
        sess.route("GET", "/models", api.aiohttp.ClientResponse(200, payload=["checkpoints"]))
        sess.route("GET", "/models/checkpoints", api.aiohttp.ClientResponse(
            200, payload=[arch_model]))
        sess.route("POST", "/prompt", api.aiohttp.ClientResponse(
            200, payload={"prompt_id": "cap-1"}))
        sess.route("GET", "/queue", api.aiohttp.ClientResponse(
            200, payload={"queue_running": [], "queue_pending": []}))
        sess.route("GET", "/history/cap-1", api.aiohttp.ClientResponse(200, payload={"cap-1": {
            "status": {"status_str": "success", "completed": True},
            "outputs": {"7": {"images": [{"filename": "c.png", "type": "output"}]}}}}))
        sess.route("GET", "/view", api.aiohttp.ClientResponse(200, text="PNG"))
        plugin.comfy._session = sess
        plugin.comfy.invalidate_model_cache()
        asyncio.run(plugin.generate(user_desc=desc, opts=opts or {}))
        for method, path, kw in sess.calls:
            if method == "POST" and path == "/prompt":
                return kw["json"]["prompt"]
        return {}

    async def fake_opt(user_desc, catalog, defaults=None, event=None):
        # 故意返回一个很短、缺少手部规避词的负面词
        return {"positive": "1girl, standing", "negative": "blurry", "checkpoint": "",
                "lora": "", "lora_strength": 1.0, "vae": "", "width": 0, "height": 0,
                "raw_ok": True}

    plugin.llm.optimize_prompt = fake_opt
    plugin.config["llm_settings"] = {"enable_prompt_optimize": True}
    plugin.config["draw_settings"] = {
        "default_negative": "lowres, worst quality",
        "add_quality_tags": True, "keep_default_negative": True,
    }

    graph = capture_graph("一个少女站在窗边")
    pos_text = graph["2"]["inputs"]["text"]
    neg_text = graph["3"]["inputs"]["text"]
    check("SD1.5 正向被补上质量词", pos_text.startswith("masterpiece, best quality"),
          pos_text[:50])
    check("LLM 的负面词没有挤掉手部规避词",
          "blurry" in neg_text and "bad hands" in neg_text and "extra fingers" in neg_text,
          neg_text[:80])
    check("默认负面词被保留", "lowres" in neg_text, neg_text[:40])

    # custom_only：只用你自己的词，不追加任何内容，也不采纳 LLM 的
    plugin.config["draw_settings"] = {"default_negative": "我的负面词A",
                                      "negative_mode": "custom_only"}
    neg_custom = capture_graph("一个少女")["3"]["inputs"]["text"]
    check("custom_only 只用你自己的词",
          neg_custom == "我的负面词A", neg_custom)
    check("custom_only 不追加手部规避词", "bad hands" not in neg_custom, neg_custom)
    check("custom_only 不采纳 LLM 的负面词", "blurry" not in neg_custom, neg_custom)

    # custom_only 下，行内 --negative 仍然生效（那是你显式写的）
    neg_custom2 = capture_graph("一个少女", opts={"negative": "我的行内词B"})["3"]["inputs"]["text"]
    check("custom_only 下行内 --negative 仍生效",
          neg_custom2 == "我的负面词A, 我的行内词B", neg_custom2)

    # guard_only：保留手部安全网，但不采纳 LLM 的负面词
    plugin.config["draw_settings"] = {"default_negative": "我的词",
                                      "negative_mode": "guard_only"}
    neg_guard = capture_graph("一个少女")["3"]["inputs"]["text"]
    check("guard_only 保留手部规避词", "我的词" in neg_guard and "bad hands" in neg_guard,
          neg_guard[:60])
    check("guard_only 不采纳 LLM 的负面词", "blurry" not in neg_guard, neg_guard[:60])

    # 非法取值回退到 merge
    plugin.config["draw_settings"] = {"default_negative": "d", "negative_mode": "乱填"}
    neg_bad = capture_graph("一个少女")["3"]["inputs"]["text"]
    check("非法策略值回退到 merge",
          "bad hands" in neg_bad and "blurry" in neg_bad, neg_bad[:60])

    # Pony 系：分数前缀 + 低分档负面词
    plugin.config["draw_settings"] = {"default_negative": "lowres", "add_quality_tags": True,
                                     "keep_default_negative": True}
    pgraph = capture_graph("1girl", arch_model="ponyDiffusionV6XL_v6.safetensors")
    check("Pony 正向带分数前缀", pgraph["2"]["inputs"]["text"].startswith("score_9"),
          pgraph["2"]["inputs"]["text"][:50])
    check("Pony 负向含低分档排除词", "score_6" in pgraph["3"]["inputs"]["text"],
          pgraph["3"]["inputs"]["text"][:60])

    # LLM 给的尺寸只当比例意图：SD1.5 模型不能被拉回 1024 档
    async def make_opt(w, h):
        async def _opt(user_desc, catalog, defaults=None, event=None):
            return {"positive": "1girl", "negative": "", "checkpoint": "", "lora": "",
                    "lora_strength": 1.0, "vae": "", "width": w, "height": h, "raw_ok": True}
        return _opt

    plugin.config["draw_settings"] = {"default_negative": "lowres"}
    plugin.llm.optimize_prompt = asyncio.run(make_opt(1024, 1024))
    lat = capture_graph("少女", arch_model="3Guofeng3_v34.safetensors")["4"]["inputs"]
    total = lat["width"] * lat["height"]
    sdxl_budget = 1024 * 1024
    check("SD1.5 不会被 LLM 的 1024x1024 拉回大尺寸",
          total < sdxl_budget * 0.7, f"{lat['width']}x{lat['height']} = {total}")
    check("归一后仍是对齐到 8 的合法尺寸",
          lat["width"] % 8 == 0 and lat["height"] % 8 == 0, (lat["width"], lat["height"]))

    plugin.llm.optimize_prompt = asyncio.run(make_opt(1344, 768))
    lat2 = capture_graph("横构图", arch_model="3Guofeng3_v34.safetensors")["4"]["inputs"]
    check("保留 LLM 的横构图比例意图",
          lat2["width"] > lat2["height"], f"{lat2['width']}x{lat2['height']}")
    check("横构图同样归一到 SD1.5 像素预算",
          lat2["width"] * lat2["height"] < sdxl_budget * 0.7,
          f"{lat2['width']}x{lat2['height']}")

    # 用户行内参数是显式意图，必须原样生效
    lat3 = capture_graph("少女", opts={"size": "832x1216"},
                         arch_model="3Guofeng3_v34.safetensors")["4"]["inputs"]
    check("行内 --size 优先于架构归一",
          (lat3["width"], lat3["height"]) == (832, 1216), (lat3["width"], lat3["height"]))

    # SDXL 模型：1024 档本来就是对的，不该被缩小（先把假 LLM 重置成方形意图）
    plugin.llm.optimize_prompt = asyncio.run(make_opt(1024, 1024))
    lat4 = capture_graph("少女", arch_model="juggernautXL_v9.safetensors")["4"]["inputs"]
    check("SDXL 保持 1024x1024",
          (lat4["width"], lat4["height"]) == (1024, 1024), (lat4["width"], lat4["height"]))

    # 强制 VAE
    plugin.config["draw_settings"] = {"default_negative": "lowres", "force_vae": "my_vae.safetensors"}
    vgraph = capture_graph("1girl")
    check("强制 VAE 会写入 VAELoader", "my_vae.safetensors" in json.dumps(vgraph, ensure_ascii=False),
          [n.get("class_type") for n in vgraph.values()])

    print("\n=== 提交前的本地预检（服务端不给原因时的兜底）===")
    # V2 与 V3 两种输入描述都要能读出下拉选项
    check("V2 下拉选项", api._combo_options([["euler", "dpmpp_2m"], {"tooltip": "t"}])
          == ["euler", "dpmpp_2m"])
    check("V3 下拉选项", api._combo_options({"type": "COMBO", "options": ["a", "b"]}) == ["a", "b"])
    check("数值型输入不是下拉", api._combo_options(["INT", {"min": 1, "max": 10}]) is None)
    check("读得出数值上下界", api._numeric_bounds(["INT", {"min": 1, "max": 10}]) == (1, 10))

    specs = {
        "KSampler": {"input": {"required": {
            "sampler_name": [["euler", "dpmpp_2m"], {}],
            "scheduler": [["normal", "karras", "simple"], {}],
            "steps": ["INT", {"min": 1, "max": 10000}],
            "cfg": ["FLOAT", {"min": 0.0, "max": 100.0}],
            "model": ["MODEL", {}],
        }}},
    }
    good_graph = {"5": {"class_type": "KSampler", "inputs": {
        "sampler_name": "dpmpp_2m", "scheduler": "karras", "steps": 28, "cfg": 6.0,
        "model": ["1", 0]}}}
    check("合法图无问题", api.validate_graph_locally(good_graph, specs) == [],
          api.validate_graph_locally(good_graph, specs))

    bad_graph = {"5": {"class_type": "KSampler", "inputs": {
        "sampler_name": "not_a_sampler", "scheduler": "nope", "steps": 0, "cfg": 999.0,
        "model": ["1", 0]}}}
    found = api.validate_graph_locally(bad_graph, specs)
    check("抓出采样器/调度器取值不存在", any("not_a_sampler" in f for f in found) and any("nope" in f for f in found), found)
    check("抓出步数小于最小值", any("steps=0" in f and "最小值" in f for f in found), found)
    check("抓出 CFG 大于最大值", any("cfg=999.0" in f and "最大值" in f for f in found), found)
    check("连线不被误判", all("model" not in f or "取值" not in f for f in found), found)

    # precheck：命中服务端约束时拦下，且 generate() 不应提交
    s10 = api.aiohttp.ClientSession()
    s10.route("GET", "/models", api.aiohttp.ClientResponse(200, payload=["checkpoints"]))
    s10.route("GET", "/models/checkpoints", api.aiohttp.ClientResponse(
        200, payload=["SDXL/m.safetensors"]))
    # 节点齐全（否则会先被能力探测拦下），但故意给一个不含 dpmpp_2m 的采样器列表
    s10.route("GET", "/object_info", api.aiohttp.ClientResponse(200, payload={
        "CheckpointLoaderSimple": {"input": {"required": {
            "ckpt_name": [["SDXL/m.safetensors"], {}]}}},
        "CLIPTextEncode": {"input": {"required": {
            "text": ["STRING", {}], "clip": ["CLIP", {}]}}},
        "EmptyLatentImage": {"input": {"required": {
            "width": ["INT", {"min": 64, "max": 16384}],
            "height": ["INT", {"min": 64, "max": 16384}],
            "batch_size": ["INT", {"min": 1, "max": 4096}]}}},
        "KSampler": {"input": {"required": {
            "sampler_name": [["euler"], {}], "scheduler": [["normal"], {}],
            "steps": ["INT", {"min": 1, "max": 10000}],
            "cfg": ["FLOAT", {"min": 0.0, "max": 100.0}]}}},
        "VAEDecode": {"input": {"required": {
            "samples": ["LATENT", {}], "vae": ["VAE", {}]}}},
        "SaveImage": {"input": {"required": {
            "images": ["IMAGE", {}], "filename_prefix": ["STRING", {}]}}},
    }))
    plugin.comfy._session = s10
    plugin.comfy.invalidate_model_cache()
    problems = asyncio.run(plugin.comfy.precheck(
        {"5": {"class_type": "KSampler", "inputs": {"sampler_name": "dpmpp_2m"}}}))
    check("precheck 命中服务端约束", any("dpmpp_2m" in p for p in problems), problems)

    # 预检只做诊断不做拦截：仍会提交，由服务端裁决；服务端拒绝时预检结论一并给出
    try:
        asyncio.run(plugin.generate(user_desc="一只猫", opts={}))
        check("预检不拦截提交（改由服务端裁决）", True)
    except api.ComfyUIError as exc:
        text = str(exc)
        check("服务端拒绝时把预检结论一并给出（补上没原因的失败）",
              "dpmpp_2m" in text, text.replace("\n", " ")[:90])
        check("失败的工作流被落盘以便排查",
              (plugin.data_dir / "last_failed_prompt.json").is_file())

    # 防止误杀回归：LoadImage 的子目录引用由节点自校验，预检不应报错
    path_specs = {"LoadImage": {"input": {"required": {
        "image": [["root_only.png"], {"image_upload": True}]}}}}
    path_graph = {"4": {"class_type": "LoadImage",
                        "inputs": {"image": "astrbot/sub_folder_pic.png"}}}
    check("LoadImage 的子目录引用不会被预检误判",
          api.validate_graph_locally(path_graph, path_specs) == [],
          api.validate_graph_locally(path_graph, path_specs))
    # 但普通下拉仍然照常校验
    combo_specs = {"CheckpointLoaderSimple": {"input": {"required": {
        "ckpt_name": [["a.safetensors"], {}]}}}}
    combo_graph = {"1": {"class_type": "CheckpointLoaderSimple",
                         "inputs": {"ckpt_name": "nope.safetensors"}}}
    check("普通下拉取值仍会被预检抓出",
          len(api.validate_graph_locally(combo_graph, combo_specs)) == 1,
          api.validate_graph_locally(combo_graph, combo_specs))

    print("\n=== 提交失败与超时（旧版会永久挂起）===")
    s5 = api.aiohttp.ClientSession()
    s5.route("POST", "/prompt", api.aiohttp.ClientResponse(400, text=json.dumps(err)))
    plugin.comfy._session = s5
    try:
        asyncio.run(plugin.comfy.submit({"1": {"class_type": "X", "inputs": {}}}))
        check("提交失败抛可读错误", False)
    except api.ComfyUIError as exc:
        check("提交失败抛可读错误", "ckpt_name" in str(exc), str(exc)[:70])

    s6 = api.aiohttp.ClientSession()
    s6.route("GET", "/queue", api.aiohttp.ClientResponse(200, payload={"queue_running": [], "queue_pending": []}))
    s6.route("GET", "/history/pid-x", api.aiohttp.ClientResponse(200, text="{}"))
    c6 = api.ComfyUI("127.0.0.1:8188", timeout=2, poll_interval=0.2)
    c6._session = s6
    started = time.time()
    try:
        asyncio.run(c6.wait_for_images("pid-x", data_dir / "out2"))
        check("无产出时按超时退出", False)
    except api.ComfyUIError as exc:
        elapsed = time.time() - started
        check("无产出时按超时退出", elapsed < 8 and "超时" in str(exc), f"{elapsed:.1f}s")

    print("\n=== 元数据与模板文件一致性 ===")
    meta_text = (ROOT / "metadata.yaml").read_text(encoding="utf-8")
    # 不引入 yaml 依赖：这几个字段都是简单标量，直接按行取
    def _meta_field(key: str) -> str:
        for line in meta_text.splitlines():
            if line.startswith(f"{key}:"):
                return line.split(":", 1)[1].strip().strip('"').strip("'")
        return ""

    check("metadata 必填字段齐全",
          all(_meta_field(k) for k in ("name", "desc", "version", "author")),
          {k: _meta_field(k)[:24] for k in ("name", "version", "author")})
    check("metadata.name 与目录名一致（参考插件里踩过这个坑）",
          _meta_field("name") == ROOT.name, f"{_meta_field('name')} vs {ROOT.name}")
    check("metadata.name 与代码里的 PLUGIN_NAME 一致",
          _meta_field("name") == m.PLUGIN_NAME, m.PLUGIN_NAME)
    version = _meta_field("version")
    check("版本号是语义化版本", bool(re.match(r"^\d+\.\d+\.\d+$", version)), version)
    check("代码里的 PLUGIN_VERSION 与 metadata 不漂移（启动横幅会打印它）",
          m.PLUGIN_VERSION == version, f"{m.PLUGIN_VERSION} vs {version}")
    floor = _meta_field("astrbot_version")
    check("声明的 astrbot_version 与实测下界一致（astrbot.api.web 自 v4.26.0 起）",
          floor == ">=4.26.0", floor)
    check("未再使用已废弃的 @register 装饰器",
          "@register(" not in (ROOT / "main.py").read_text(encoding="utf-8"))

    for path in sorted((ROOT / "workflows").glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        check(f"模板 {path.stem} 内部 name 与文件名一致",
              data.get("name") == path.stem, data.get("name"))

    print("\n=== 负面词归一化去重（针对真实的长负面词）===")
    check("括号写法归一为同一个键",
          m.tag_key("((extra limbs))") == m.tag_key("(extra limbs)") == m.tag_key("extra limbs")
          == m.tag_key("[[extra limbs]]") == "extra limbs")
    check("显式权重后缀被剥掉", m.tag_key("word:1.3") == "word")
    check("权重估算：((x)) ≈ 1.21", abs(m.tag_weight("((x))") - 1.21) < 1e-6,
          m.tag_weight("((x))"))
    check("权重估算：[x] = 0.9（降权）", abs(m.tag_weight("[x]") - 0.9) < 1e-6,
          m.tag_weight("[x]"))
    check("权重估算：显式 :1.3", m.tag_weight("x:1.3") == 1.3)
    check("权重估算：无括号 = 1.0", m.tag_weight("x") == 1.0)

    dup = "((extra limbs)), extra limbs, (extra limbs), [[extra limbs]], ugly, ((ugly))"
    deduped = m.merge_tags(dup)
    check("同词不同写法只保留一个", len(deduped.split(",")) == 2, deduped)
    check("保留权重最高的写法", "((extra limbs))" in deduped and "((ugly))" in deduped, deduped)

    # 真实场景：流行长负面词里的重复写法
    popular = ("canvas frame, cartoon, 3d, ((disfigured)), ((bad art)), ((extra limbs)), "
               "extra fingers, mutated hands, ((poorly drawn hands)), blurry, (((duplicate))), "
               "((bad anatomy)), extra limbs, ugly, extra limbs, extra legs, extra arms, "
               "disfigured, blurred, blurry, (((duplicate))), bad anatomy, ugly, extra limbs")
    reduced = m.merge_tags(popular)
    before = len([x for x in popular.split(",") if x.strip()])
    after = len(reduced.split(","))
    check("真实长负面词显著缩短", after < before * 0.75, f"{before} → {after} 个标签")

    print("\n=== 提示词长度估算与提示 ===")
    check("短词只占 1 段", m.estimate_clip_chunks("a, b, c")[1] == 1)
    check("长词会占多段", m.estimate_clip_chunks(", ".join(f"tag{i}" for i in range(300)))[1] >= 3,
          m.estimate_clip_chunks(", ".join(f"tag{i}" for i in range(300))))
    check("describe_prompt 输出可读描述", "标签" in m.describe_prompt("a, b, c"),
          m.describe_prompt("a, b, c"))

    # 这一段自己准备干净的 mock 会话：前面的预检测试留下的会话只提供 euler 采样器
    def fresh_session(pid="ok-1"):
        sess = api.aiohttp.ClientSession()
        sess.route("GET", "/models", api.aiohttp.ClientResponse(200, payload=["checkpoints"]))
        sess.route("GET", "/models/checkpoints", api.aiohttp.ClientResponse(
            200, payload=["SDXL/m.safetensors"]))
        sess.route("POST", "/prompt", api.aiohttp.ClientResponse(200, payload={"prompt_id": pid}))
        sess.route("GET", "/queue", api.aiohttp.ClientResponse(
            200, payload={"queue_running": [], "queue_pending": []}))
        sess.route("GET", f"/history/{pid}", api.aiohttp.ClientResponse(200, payload={pid: {
            "status": {"status_str": "success", "completed": True},
            "outputs": {"7": {"images": [{"filename": "ok.png", "type": "output"}]}}}}))
        sess.route("GET", "/view", api.aiohttp.ClientResponse(200, text="PNG"))
        plugin.comfy._session = sess
        plugin.comfy.invalidate_model_cache()
        return sess

    fresh_session()
    plugin.config["draw_settings"] = {"default_negative": "lowres", "negative_mode": "merge"}
    plugin.llm.optimize_prompt = asyncio.run(make_opt(0, 0))
    short_res = asyncio.run(plugin.generate(user_desc="少女", opts={}))
    check("短负面词不触发长度提示", short_res.get("prompt_note") == "", short_res.get("prompt_note"))

    fresh_session()
    long_neg = ", ".join(f"badword{i}" for i in range(260))
    plugin.config["draw_settings"] = {"default_negative": long_neg, "negative_mode": "custom_only"}
    long_res = asyncio.run(plugin.generate(user_desc="少女", opts={}))
    check("超长负面词会给出精简提示",
          "负面词较长" in str(long_res.get("prompt_note")), long_res.get("prompt_note")[:60])
    check("custom_only 下不追加任何内容（只有你自己的词）",
          "bad hands" not in long_res["negative"] and long_res["negative"].startswith("badword0"),
          long_res["negative"][:40])

    # raw：严格原样，连去重都不做
    fresh_session()
    raw_text = "((extra limbs)), extra limbs, (extra limbs)"
    plugin.config["draw_settings"] = {"default_negative": raw_text, "negative_mode": "raw"}
    raw_res = asyncio.run(plugin.generate(user_desc="少女", opts={}))
    check("raw 档严格原样不修改", raw_res["negative"] == raw_text, raw_res["negative"])
    plugin.config["draw_settings"] = {"default_negative": raw_text, "negative_mode": "raw"}
    fresh_session()
    raw_inline = asyncio.run(plugin.generate(user_desc="少女", opts={"negative": "只有这句"}))
    check("raw 档下行内 --negative 直接覆盖", raw_inline["negative"] == "只有这句",
          raw_inline["negative"])

    print("\n=== Hires Fix（放大重绘）===")
    hires_tpl = wt.load_templates(ROOT / "workflows")["sd_checkpoint"]
    base_graph = hires_tpl.build(
        positive="1girl", negative="bad hands", model_name="m.safetensors",
        width=512, height=768, steps=25, cfg=7.0, sampler="dpmpp_2m",
        scheduler="karras", seed=111)
    before_nodes = len(base_graph)

    hgraph = copy.deepcopy(base_graph)
    info = wt.add_hires_fix(hgraph, hires_tpl.bindings, scale=1.5, denoise=0.5, seed=222)
    wt.validate_graph(hgraph)
    check("插入了放大与二次采样节点", info.get("hires_upscale") and info.get("hires_sampler"), info)
    check("节点数 +2", len(hgraph) == before_nodes + 2, f"{before_nodes} → {len(hgraph)}")
    check("放大后尺寸正确且已对齐 8", (info["width"], info["height"]) == (768, 1152), info)
    up = hgraph[info["hires_upscale"]]
    check("LatentUpscale 接在首轮采样之后",
          up["class_type"] == "LatentUpscale" and up["inputs"]["samples"] == ["5", 0],
          up["inputs"])
    second = hgraph[info["hires_sampler"]]
    check("二次采样读取放大后的潜空间", second["inputs"]["latent_image"] == [info["hires_upscale"], 0])
    check("二次采样继承了首轮的模型与条件",
          second["inputs"]["model"] == base_graph["5"]["inputs"]["model"]
          and second["inputs"]["positive"] == base_graph["5"]["inputs"]["positive"]
          and second["inputs"]["cfg"] == base_graph["5"]["inputs"]["cfg"], second["inputs"])
    check("二次采样 denoise < 1（是重绘不是重画）",
          second["inputs"]["denoise"] == 0.5, second["inputs"]["denoise"])
    check("二次采样换了新种子", second["inputs"]["seed"] == 222)
    check("VAEDecode 改接到二次采样",
          hgraph["6"]["inputs"]["samples"] == [info["hires_sampler"], 0],
          hgraph["6"]["inputs"]["samples"])
    self_loops = [(nid, k) for nid, n in hgraph.items()
                  for k, v in (n.get("inputs") or {}).items()
                  if isinstance(v, list) and v[0] == nid]
    check("放大后依然没有自环", not self_loops, self_loops)

    # 边界：结构不支持时不应崩，而是原样返回
    orphan = {"1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "m"}}}
    check("模板结构不支持时安全返回空", wt.add_hires_fix(orphan, {"sampler": "1"}, scale=2.0) == {})

    # Flux 模板同样可用
    flux_tpl = wt.load_templates(ROOT / "workflows")["flux_unet"]
    fgraph = flux_tpl.build(positive="cat", negative="", model_name="f.safetensors",
                            vae_name="ae.safetensors", width=1024, height=1024, steps=20,
                            cfg=1.0, sampler="euler", scheduler="simple", seed=1, guidance=3.5)
    finfo = wt.add_hires_fix(fgraph, flux_tpl.bindings, scale=1.5, denoise=0.5, seed=9)
    wt.validate_graph(fgraph)
    check("Flux 模板也能插入 Hires", finfo.get("hires_sampler") and finfo["width"] == 1536, finfo)

    # 端到端：配置与行内参数
    def capture_result(desc, opts, cfg_hires):
        sess = api.aiohttp.ClientSession()
        sess.route("GET", "/models", api.aiohttp.ClientResponse(200, payload=["checkpoints"]))
        sess.route("GET", "/models/checkpoints", api.aiohttp.ClientResponse(
            200, payload=["SDXL/m.safetensors"]))
        sess.route("POST", "/prompt", api.aiohttp.ClientResponse(200, payload={"prompt_id": "h-1"}))
        sess.route("GET", "/queue", api.aiohttp.ClientResponse(
            200, payload={"queue_running": [], "queue_pending": []}))
        sess.route("GET", "/history/h-1", api.aiohttp.ClientResponse(200, payload={"h-1": {
            "status": {"status_str": "success", "completed": True},
            "outputs": {"7": {"images": [{"filename": "h.png", "type": "output"}]}}}}))
        sess.route("GET", "/view", api.aiohttp.ClientResponse(200, text="PNG"))
        plugin.comfy._session = sess
        plugin.comfy.invalidate_model_cache()
        plugin.config["hires"] = cfg_hires
        res = asyncio.run(plugin.generate(user_desc=desc, opts=opts))
        for method, path, kw in sess.calls:
            if method == "POST" and path == "/prompt":
                return res, kw["json"]["prompt"]
        return res, {}

    plugin.config["draw_settings"] = {"default_negative": "lowres"}
    plugin.llm.optimize_prompt = asyncio.run(make_opt(0, 0))

    res_off, g_off = capture_result("少女", {}, {"enable": False, "scale": 1.5})
    check("默认关闭时不插入 Hires", not res_off.get("hires") and len(g_off) == 7,
          (res_off.get("hires"), len(g_off)))

    res_on, g_on = capture_result("少女", {}, {"enable": True, "scale": 1.5, "denoise": 0.5})
    check("配置开启后插入 Hires 且报最终尺寸",
          res_on.get("hires") and res_on["width"] > 512, (res_on.get("hires"), res_on["width"]))

    res_inline, _ = capture_result("少女", {"hires": "2.0"}, {"enable": False})
    # mock 用的是 SDXL 模型（基准 1024x1024），2 倍即 2048
    check("行内 --hires 2.0 对单次生效",
          res_inline.get("hires") and res_inline["width"] == 2048, res_inline.get("width"))

    res_zero, g_zero = capture_result("少女", {"hires": "0"}, {"enable": True, "scale": 1.5})
    check("行内 --hires 0 对单次关闭",
          not res_zero.get("hires") and len(g_zero) == 7, (res_zero.get("hires"), len(g_zero)))

    res_dn, g_dn = capture_result("少女", {"hires": "1.5", "hires_denoise": "0.35"},
                                  {"enable": False})
    second_nodes = [n for n in g_dn.values() if n.get("class_type") == "KSampler"]
    check("行内 --hires-denoise 生效",
          len(second_nodes) == 2 and second_nodes[-1]["inputs"]["denoise"] == 0.35,
          [n["inputs"].get("denoise") for n in second_nodes])

    res_hs, g_hs = capture_result("少女", {"hires": "1.5", "hires_steps": "12"}, {"enable": False})
    second_nodes2 = [n for n in g_hs.values() if n.get("class_type") == "KSampler"]
    check("行内 --hires-steps 生效",
          len(second_nodes2) == 2 and second_nodes2[-1]["inputs"]["steps"] == 12,
          [n["inputs"].get("steps") for n in second_nodes2])

    print("\n=== Hires 尺寸推导（图生图不能改宽高比）===")

    i2i_tpl = wt.load_templates(ROOT / "workflows")["img2img_checkpoint"]
    i2i_g = i2i_tpl.build(positive="a", negative="b",
                          model_name="anything-v5-PrtRE.safetensors", seed=1,
                          width=512, height=768, denoise=0.6, image_name="x.png")
    # 图生图的潜在空间来自 VAEEncode（没有 width/height），必须从上游 ImageScale 推导
    i2i_info = wt.add_hires_fix(i2i_g, i2i_tpl.bindings, scale=1.5, denoise=0.45)
    wt.validate_graph(i2i_g)
    check("图生图 Hires 尺寸取自输入图缩放节点（512x768 → 768x1152）",
          (i2i_info["width"], i2i_info["height"]) == (768, 1152), i2i_info)
    check("图生图 Hires 不会把 2:3 压成 1:1（回归：曾回退成 512x512）",
          i2i_info["width"] * 3 == i2i_info["height"] * 2, (i2i_info["width"], i2i_info["height"]))
    check("图生图用 LatentUpscale 显式指定尺寸",
          i2i_info["upscale_node"] == "LatentUpscale", i2i_info["upscale_node"])

    # 拿不到任何尺寸信息（自定义工作流）→ 按比例放大，绝不自作主张写 512x512
    bare = {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "m.safetensors"}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "a", "clip": ["1", 1]}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"text": "b", "clip": ["1", 1]}},
        "4": {"class_type": "LoadImage", "inputs": {"image": "x.png"}},
        "6": {"class_type": "VAEEncode", "inputs": {"pixels": ["4", 0], "vae": ["1", 2]}},
        "7": {"class_type": "KSampler", "inputs": {
            "model": ["1", 0], "seed": 1, "steps": 20, "cfg": 7.0,
            "sampler_name": "euler", "scheduler": "normal", "denoise": 0.6,
            "positive": ["2", 0], "negative": ["3", 0], "latent_image": ["6", 0]}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["7", 0], "vae": ["1", 2]}},
        "9": {"class_type": "SaveImage", "inputs": {"images": ["8", 0], "filename_prefix": "x"}},
    }
    bare_bindings = {"sampler": "7", "positive": ["2", "text"], "negative": ["3", "text"],
                     "latent": "6", "save": "9", "image_loader": ["4", "image"]}
    bare_info = wt.add_hires_fix(bare, bare_bindings, scale=1.5, denoise=0.5)
    wt.validate_graph(bare)
    check("尺寸完全未知时改用按比例放大（保宽高比）",
          bare_info["upscale_node"] == "LatentUpscaleBy", bare_info)
    check("尺寸未知时不谎报具体尺寸",
          (bare_info["width"], bare_info["height"]) == (0, 0), bare_info)
    check("按比例放大节点带 scale_by 且没有写死的宽高",
          bare[bare_info["hires_upscale"]]["inputs"].get("scale_by") == 1.5
          and "width" not in bare[bare_info["hires_upscale"]]["inputs"],
          bare[bare_info["hires_upscale"]]["inputs"])

    # 结果行：尺寸未知时说倍数，而不是显示 0x0
    real_add = m.add_hires_fix
    m.add_hires_fix = lambda graph, bindings, **kw: {
        "hires_sampler": "90", "hires_upscale": "91", "upscale_node": "LatentUpscaleBy",
        "width": 0, "height": 0, "scale": 2.0}
    try:
        res_unknown, _ = capture_result("少女", {}, {"enable": True, "scale": 2.0})
    finally:
        m.add_hires_fix = real_add
    check("尺寸未知时结果为 ×倍数 而不是 0x0",
          res_unknown["hires"].get("scale") == 2.0
          and res_unknown["width"] > 0, res_unknown.get("hires"))

    # 结果文案：尺寸未知只说倍数，绝不编一个尺寸出来
    from astrbot.api.event import AstrMessageEvent as _Ev

    def result_text(hires):
        chain = plugin._compose_result_chain(
            _Ev(message_str="/画图 x"), "u1",
            {"template": "t", "arch": "sd15", "model": "m", "width": 512, "height": 768,
             "seed": 1, "seconds": 1.0, "images": [], "hires": hires})
        return "".join(getattr(c, "text", "") for c in chain)

    text_unknown = result_text({"width": 0, "height": 0, "scale": 2.0})
    check("尺寸未知时结果行显示 ×倍数 且不出现 0x0",
          "×2.0" in text_unknown and "0x0" not in text_unknown, text_unknown)
    text_known = result_text({"width": 768, "height": 1152, "scale": 1.5})
    check("尺寸已知时结果行显示最终尺寸",
          "Hires Fix：768x1152" in text_known, text_known)

    print("\n=== 画廊详情：参数落盘 + 界面字段一致性 ===")
    # 界面字段一致性：弹窗里展示的参数名必须都是后端真的会写的
    import re as _re

    app_js = (ROOT / "pages" / "settings" / "app.js").read_text(encoding="utf-8")
    main_py = (ROOT / "main.py").read_text(encoding="utf-8")

    rec_dir = Path(tempfile.mkdtemp(prefix="smart_gallery_"))
    rec_storage = st.Storage(rec_dir)

    async def record_flow():
        await rec_storage.record_generation(
            user_id="u9", user_name="画手", positive="1girl, masterpiece",
            negative="bad hands, extra fingers", models={"checkpoint": "m.safetensors",
                                                         "lora": "a.safetensors",
                                                         "vae": "", "template": "sd_checkpoint"},
            images=["images/x.png"], seconds=12.5,
            params={"width": 512, "height": 768, "steps": 25, "cfg": 7.0,
                    "sampler": "dpmpp_2m", "seed": 12345, "arch": "sd15",
                    "lora": "a.safetensors", "vae": "", "template": "sd_checkpoint",
                    "model": "m.safetensors"})

    asyncio.run(record_flow())
    rec = rec_storage.load_stats()["records"][-1]
    check("记录里保存了完整参数", isinstance(rec.get("params"), dict)
          and rec["params"]["seed"] == 12345 and rec["params"]["steps"] == 25, rec.get("params"))
    check("正/负面提示词都在记录里",
          rec["positive"] == "1girl, masterpiece" and "extra fingers" in rec["negative"])
    # 向后兼容：老版本写下的 stats.json 里没有 params 字段，画廊必须照常工作
    legacy_dir = Path(tempfile.mkdtemp(prefix="smart_legacy_"))
    legacy = st.Storage(legacy_dir)
    legacy.stats_path.write_text(json.dumps({
        "model_usage": {}, "users": {},
        "records": [{"time": "2026-01-01 00:00:00", "user_id": "u1", "user_name": "旧",
                     "positive": "old prompt", "negative": "old neg",
                     "template": "sd_checkpoint", "model": "m.safetensors",
                     "seconds": 3.0, "images": ["images/old.png"]}],
    }, ensure_ascii=False), encoding="utf-8")
    legacy_rec = legacy.load_stats()["records"][0]
    check("老记录的 stats.json 仍能正常读取", legacy_rec["positive"] == "old prompt"
          and "params" not in legacy_rec, list(legacy_rec))
    check("界面为缺失的 params 做了兜底（record.params || {}）",
          "record.params || {}" in app_js)

    block_match = _re.search(r"PARAM_LABELS = \[(.*?)\];", app_js, _re.S)
    check("app.js 里能解析出 PARAM_LABELS", block_match is not None)
    if block_match:
        ui_keys = set(_re.findall(r"\['([a-z_]+)',", block_match.group(1)))
        # 后端写入 params 的键：从 _record_generation 里提取
        params_block = _re.search(r"params=\{(.*?)\n            \},", main_py, _re.S)
        check("main.py 里能解析出 params 写入", params_block is not None)
        if params_block:
            written = set(_re.findall(r'"([a-z_]+)":', params_block.group(1)))
            # 弹窗里的字段必须能在记录里找到（model/template 另有顶层兜底）
            missing = sorted(ui_keys - written - {"model", "template"})
            check("弹窗展示的参数后端都会写入（避免永远显示空白）", not missing, missing or "全部对齐")
            uncovered = sorted(written - ui_keys)
            check("后端记录的参数界面都能看到", not uncovered, uncovered or "全部展示")

    print("\n=== 反推提示词（看图 → 提示词）===")
    check("解析带代码块的 JSON", llm.parse_reverse_result(
        '```json\n{"positive":"1girl, kimono","negative":"bad hands","summary":"和服少女"}\n```'
    )["positive"] == "1girl, kimono")
    check("解析夹杂说明文字的 JSON", llm.parse_reverse_result(
        '好的：{"positive":"a","negative":"b","summary":"c"} 完成'
    )["negative"] == "b")
    bad = llm.parse_reverse_result("完全不是 JSON")
    check("非 JSON 不崩且标记失败", bad["raw_ok"] is False and bad["positive"] == "")

    from astrbot.api.event import AstrMessageEvent
    from astrbot.api.message_components import Image as StubImage
    from astrbot.api.message_components import Reply as StubReply

    async def collect(message):
        return await plugin._collect_images(AstrMessageEvent(message=message))

    check("能从当前消息取到图片",
          asyncio.run(collect([StubImage("/tmp/a.png")])) == ["/tmp/a.png"])
    check("能从引用消息里取到图片",
          asyncio.run(collect([StubReply(chain=[StubImage("/tmp/quoted.png")])]))
          == ["/tmp/quoted.png"])
    check("纯文字消息取不到图片", asyncio.run(collect([])) == [])
    check("重复图片会去重",
          len(asyncio.run(collect([StubImage("/tmp/a.png"), StubImage("/tmp/a.png")]))) == 1)

    # 启用一个假的 LLM provider
    async def enable_llm(reply):
        ctx._providers = {"fake": object()}
        ctx.llm_reply = reply
        ctx.llm_calls = []

    async def drive(agen):
        return [item async for item in agen]

    asyncio.run(enable_llm('{"positive":"1girl, kimono, cherry blossoms","negative":"bad hands",'
                           '"summary":"一位穿和服的少女"}'))

    # 没有图片时给出用法而不是报错
    ev_none = AstrMessageEvent(message_str="/反推")
    out_none = asyncio.run(drive(plugin.cmd_reverse_prompt(ev_none)))
    check("/反推 没有图片时给出用法提示",
          "用法" in out_none[0]["text"] and "反推" in out_none[0]["text"],
          out_none[0]["text"][:40])

    # 带图片：应把图片作为 image_urls 传给 LLM
    ev_img = AstrMessageEvent(message_str="/反推", message=[StubImage("/tmp/pic.png")])
    out_img = asyncio.run(drive(plugin.cmd_reverse_prompt(ev_img)))
    text_all = "\n".join(x["text"] for x in out_img)
    check("反推结果里带出正向提示词", "1girl, kimono" in text_all, text_all[:60])
    check("反推结果里带出画面概括", "和服的少女" in text_all)
    check("反推结果里带出建议负面词", "bad hands" in text_all)
    check("图片确实作为 image_urls 传给了 LLM",
          ctx.llm_calls and ctx.llm_calls[-1].get("image_urls") == ["/tmp/pic.png"],
          ctx.llm_calls[-1].get("image_urls") if ctx.llm_calls else None)

    # --画：反推后直接出图，且不再让 LLM 改写提示词
    def fresh_ok_session(pid="rev-1"):
        sess = api.aiohttp.ClientSession()
        sess.route("GET", "/models", api.aiohttp.ClientResponse(200, payload=["checkpoints"]))
        sess.route("GET", "/models/checkpoints", api.aiohttp.ClientResponse(
            200, payload=["SDXL/m.safetensors"]))
        sess.route("POST", "/prompt", api.aiohttp.ClientResponse(200, payload={"prompt_id": pid}))
        sess.route("GET", "/queue", api.aiohttp.ClientResponse(
            200, payload={"queue_running": [], "queue_pending": []}))
        sess.route("GET", f"/history/{pid}", api.aiohttp.ClientResponse(200, payload={pid: {
            "status": {"status_str": "success", "completed": True},
            "outputs": {"7": {"images": [{"filename": "rev.png", "type": "output"}]}}}}))
        sess.route("GET", "/view", api.aiohttp.ClientResponse(200, text="PNG"))
        plugin.comfy._session = sess
        plugin.comfy.invalidate_model_cache()
        return sess

    sess = fresh_ok_session()
    asyncio.run(enable_llm('{"positive":"1girl, kimono, cherry blossoms","negative":"bad words",'
                           '"summary":"和服少女"}'))
    plugin.config["draw_settings"] = {"default_negative": "lowres"}
    plugin.config["llm_settings"] = {"enable_prompt_optimize": True}
    ev_draw = AstrMessageEvent(message_str="/反推 --画", message=[StubImage("/tmp/pic.png")])
    out_draw = asyncio.run(drive(plugin.cmd_reverse_prompt(ev_draw)))
    submitted = None
    for method, path, kw in sess.calls:
        if method == "POST" and path == "/prompt":
            submitted = kw["json"]["prompt"]
    check("--画 会真的提交出图任务", submitted is not None)
    check("提交的正向提示词就是反推结果（没有被 LLM 二次改写）",
          submitted is not None and submitted["2"]["inputs"]["text"].startswith("1girl, kimono"),
          submitted["2"]["inputs"]["text"][:50] if submitted else None)
    check("反推只调用了一次 LLM（没有再做提示词优化）", len(ctx.llm_calls) == 1,
          len(ctx.llm_calls))
    check("出图结果发给了用户",
          any(x.get("type") == "chain" for x in out_draw), [x.get("type") for x in out_draw])

    print("\n=== 反推：模型看不见图时必须报错，而不是编内容 ===")

    class _FakeProvider:
        """桩：带 provider_config.modalities，模拟 AstrBot 的模态判定。"""

        def __init__(self, pid, model="", modalities=None):
            self._pid = pid
            self._model = model
            self.provider_config = {} if modalities is None else {"modalities": modalities}

        def meta(self):
            return type("M", (), {"id": self._pid, "model": self._model})()

    vision = _FakeProvider("vision-model", "qwen-vl-max", ["text", "image"])
    textonly = _FakeProvider("text-model", "deepseek-chat", ["text"])
    unknown = _FakeProvider("unknown-model", "whatever", None)
    ctx._providers = {"vision-model": vision, "text-model": textonly,
                      "unknown-model": unknown}
    svc = plugin.llm

    check("模态含 image → 判定支持", svc.provider_vision_support("vision-model") is True)
    check("模态不含 image → 判定不支持", svc.provider_vision_support("text-model") is False)
    check("未配置 modalities → 无法判断（按 AstrBot 语义视作支持）",
          svc.provider_vision_support("unknown-model") is None)
    check("provider 不存在 → 无法判断", svc.provider_vision_support("nope") is None)
    check("provider 描述带模型名",
          "qwen-vl-max" in svc.provider_label("vision-model"), svc.provider_label("vision-model"))

    asyncio.run(enable_llm('{"positive":"1girl","negative":"bad hands","summary":"x"}'))
    ctx._providers = {"vision-model": vision, "text-model": textonly,
                      "unknown-model": unknown}
    ctx.llm_calls = []

    # 用不支持看图的模型反推 → 必须明确报错
    try:
        asyncio.run(svc.reverse_prompt(["/tmp/pic.png"], provider_id="text-model"))
        check("不支持看图时报错而不是返回编造内容", False)
    except RuntimeError as exc:
        text = str(exc)
        check("不支持看图时报错而不是返回编造内容",
              "不支持看图" in text and "反推专用模型" in text, text[:90])
    check("报错时不消耗一次 LLM 调用", ctx.llm_calls == [], ctx.llm_calls)

    # 指定支持看图的模型 → 正常反推，且用的是它
    res_vision = asyncio.run(svc.reverse_prompt(["/tmp/pic.png"], provider_id="vision-model"))
    check("指定看图模型后正常反推", res_vision.get("positive") == "1girl", res_vision.get("positive"))
    check("确实把该 provider 传给了 AstrBot",
          ctx.llm_calls and ctx.llm_calls[-1].get("chat_provider_id") == "vision-model",
          ctx.llm_calls[-1].get("chat_provider_id") if ctx.llm_calls else None)
    check("结果里带上看图模型，便于排查",
          "qwen-vl-max" in str(res_vision.get("model")), res_vision.get("model"))

    # 配置里的 vision_provider 生效（无需行内指定）
    # LLMService 接收的是完整插件配置（不是 llm_settings 子字典）
    svc2 = llm.LLMService(ctx, {"llm_settings": {"vision_provider": "vision-model"}})
    ctx.llm_calls = []
    asyncio.run(svc2.reverse_prompt(["/tmp/pic.png"]))
    check("配置了 vision_provider 时自动用它",
          ctx.llm_calls and ctx.llm_calls[-1].get("chat_provider_id") == "vision-model",
          ctx.llm_calls[-1].get("chat_provider_id") if ctx.llm_calls else None)

    # 配置了一个不存在的 provider → 明确报错
    svc3 = llm.LLMService(ctx, {"llm_settings": {"vision_provider": "不存在的模型"}})
    try:
        asyncio.run(svc3.reverse_prompt(["/tmp/pic.png"]))
        check("vision_provider 填错时报错", False)
    except RuntimeError as exc:
        check("vision_provider 填错时报错", "不存在" in str(exc), str(exc)[:70])

    # 行内 --provider
    plugin.config["llm_settings"] = {"enable_prompt_optimize": False}
    ctx.llm_calls = []
    ev_prov = AstrMessageEvent(message_str="/反推 --provider vision-model",
                              message=[StubImage("/tmp/pic.png")])
    out_prov = asyncio.run(drive(plugin.cmd_reverse_prompt(ev_prov)))
    check("行内 --provider 生效",
          ctx.llm_calls and ctx.llm_calls[-1].get("chat_provider_id") == "vision-model",
          ctx.llm_calls[-1].get("chat_provider_id") if ctx.llm_calls else None)
    check("结果里标注看图模型",
          any("看图模型" in x.get("text", "") for x in out_prov),
          [x.get("text", "")[:40] for x in out_prov])

    ctx._providers = {}
    plugin.config["llm_settings"] = {"enable_prompt_optimize": False}

    print("\n=== 图生图 ===")
    # 合成一个只含 PNG 头的文件，验证尺寸解析（不依赖 Pillow）
    img_dir = Path(tempfile.mkdtemp(prefix="smart_img_"))
    png_path = img_dir / "in.png"
    png_path.write_bytes(
        b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IHDR"
        + (640).to_bytes(4, "big") + (960).to_bytes(4, "big")
        + b"\x08\x02\x00\x00\x00" + b"\x00\x00\x00\x00"
    )
    check("不依赖 Pillow 读出 PNG 尺寸", m.read_image_size(str(png_path)) == (640, 960),
          m.read_image_size(str(png_path)))
    check("读不出的文件返回 None", m.read_image_size("/tmp/不存在.png") is None)
    # 1000x500 等比缩放后，高度 500 不是 8 的倍数，对齐为 496
    check("等比缩放并限制最长边（并对齐 8）", m.fit_to_limit(2000, 1000, 1000) == (1000, 496),
          m.fit_to_limit(2000, 1000, 1000))
    check("不超上限时保持原尺寸并对齐 8", m.fit_to_limit(642, 962, 1536) == (640, 960),
          m.fit_to_limit(642, 962, 1536))

    tpls = wt.load_templates(ROOT / "workflows")
    i2i_tpl = tpls["img2img_checkpoint"]
    check("图生图模板的用途被识别为 i2i", i2i_tpl.purpose == "i2i", i2i_tpl.purpose)
    check("文生图模板的用途是 t2i", tpls["sd_checkpoint"].purpose == "t2i")
    check("图生图模板的潜空间来源是 VAEEncode",
          i2i_tpl.bindings["latent"] == "6", i2i_tpl.bindings["latent"])
    check("图生图模板找得到输入图与缩放节点",
          i2i_tpl.bindings["image_loader"] == ("4", "image") and i2i_tpl.bindings["scaler"] == "5",
          (i2i_tpl.bindings["image_loader"], i2i_tpl.bindings["scaler"]))
    picked, _ = wt.pick_template(tpls, model_name="m.safetensors", model_folder="checkpoints",
                                purpose="i2i")
    check("按用途选到图生图模板", picked and picked.name == "img2img_checkpoint",
          picked.name if picked else None)
    picked_t2i, _ = wt.pick_template(tpls, model_name="m.safetensors", model_folder="checkpoints")
    check("默认用途仍是文生图", picked_t2i.name == "sd_checkpoint", picked_t2i.name)

    g_i2i = i2i_tpl.build(positive="1girl, winter", negative="bad hands",
                          model_name="m.safetensors", image_name="astrbot/in.png",
                          width=640, height=960, steps=25, cfg=7.0,
                          sampler="dpmpp_2m", scheduler="karras", seed=5, denoise=0.45)
    wt.validate_graph(g_i2i)
    check("输入图被注入 LoadImage", g_i2i["4"]["inputs"]["image"] == "astrbot/in.png")
    check("尺寸写进 ImageScale 而不是 VAEEncode",
          g_i2i["5"]["inputs"]["width"] == 640 and g_i2i["5"]["inputs"]["height"] == 960
          and "width" not in g_i2i["6"]["inputs"], g_i2i["6"]["inputs"])
    check("denoise 被注入采样器", g_i2i["7"]["inputs"]["denoise"] == 0.45,
          g_i2i["7"]["inputs"]["denoise"])

    # 上传接口
    up_sess = api.aiohttp.ClientSession()
    up_sess.route("POST", "/upload/image", api.aiohttp.ClientResponse(
        200, payload={"name": "in.png", "subfolder": "astrbot", "type": "input"}))
    up_client = api.ComfyUI("127.0.0.1:8188")
    up_client._session = up_sess
    ref = asyncio.run(up_client.upload_image(str(png_path), subfolder="astrbot"))
    check("上传后返回 子目录/文件名 引用", ref == "astrbot/in.png", ref)
    posted = [kw for method, path, kw in up_sess.calls if method == "POST"]
    form = posted[0]["data"] if posted else None
    check("multipart 里带上了 image/type/overwrite/subfolder",
          isinstance(form, api.aiohttp.FormData)
          and {"image", "type", "overwrite", "subfolder"} <= set(form.fields),
          sorted(form.fields) if isinstance(form, api.aiohttp.FormData) else None)

    # 端到端：/图生图
    def i2i_session(pid="i2i-1"):
        sess = api.aiohttp.ClientSession()
        sess.route("GET", "/models", api.aiohttp.ClientResponse(200, payload=["checkpoints"]))
        sess.route("GET", "/models/checkpoints", api.aiohttp.ClientResponse(
            200, payload=["SDXL/m.safetensors"]))
        sess.route("POST", "/upload/image", api.aiohttp.ClientResponse(
            200, payload={"name": "in.png", "subfolder": "astrbot", "type": "input"}))
        sess.route("POST", "/prompt", api.aiohttp.ClientResponse(200, payload={"prompt_id": pid}))
        sess.route("GET", "/queue", api.aiohttp.ClientResponse(
            200, payload={"queue_running": [], "queue_pending": []}))
        sess.route("GET", f"/history/{pid}", api.aiohttp.ClientResponse(200, payload={pid: {
            "status": {"status_str": "success", "completed": True},
            "outputs": {"9": {"images": [{"filename": "i2i.png", "type": "output"}]}}}}))
        sess.route("GET", "/view", api.aiohttp.ClientResponse(200, text="PNG"))
        plugin.comfy._session = sess
        plugin.comfy.invalidate_model_cache()
        return sess

    plugin.config["llm_settings"] = {"enable_prompt_optimize": False}
    plugin.config["draw_settings"] = {"default_negative": "lowres"}
    plugin.config["i2i"] = {"enable": True, "denoise": 0.6, "max_side": 1536, "subfolder": "astrbot"}

    ev_no_img = AstrMessageEvent(message_str="/图生图 改成冬天")
    out_no_img = asyncio.run(drive(plugin.cmd_img2img(ev_no_img)))
    check("/图生图 没有图片时给出用法", "用法" in out_no_img[0]["text"], out_no_img[0]["text"][:40])

    ev_no_desc = AstrMessageEvent(message_str="/图生图",
                                  message=[StubImage(str(png_path))])
    out_no_desc = asyncio.run(drive(plugin.cmd_img2img(ev_no_desc)))
    check("/图生图 没说怎么改时给出提示", "说明想怎么改" in out_no_desc[0]["text"],
          out_no_desc[0]["text"][:40])

    sess = i2i_session()
    ev_i2i = AstrMessageEvent(message_str="/图生图 改成冬天，围上红色围巾",
                              message=[StubImage(str(png_path))])
    out_i2i = asyncio.run(drive(plugin.cmd_img2img(ev_i2i)))
    submitted_i2i = None
    for method, path, kw in sess.calls:
        if method == "POST" and path == "/prompt":
            submitted_i2i = kw["json"]["prompt"]
    check("/图生图 提交了任务", submitted_i2i is not None)
    check("提交的是图生图工作流（含 LoadImage 与 VAEEncode）",
          submitted_i2i is not None
          and any(n["class_type"] == "LoadImage" for n in submitted_i2i.values())
          and any(n["class_type"] == "VAEEncode" for n in submitted_i2i.values()),
          sorted({n["class_type"] for n in (submitted_i2i or {}).values()}))
    check("尺寸按原图（640x960）走", submitted_i2i is not None
          and submitted_i2i["5"]["inputs"]["width"] == 640
          and submitted_i2i["5"]["inputs"]["height"] == 960,
          (submitted_i2i or {}).get("5", {}).get("inputs"))
    check("默认 denoise 生效", submitted_i2i is not None
          and submitted_i2i["7"]["inputs"]["denoise"] == 0.6,
          (submitted_i2i or {}).get("7", {}).get("inputs", {}).get("denoise"))
    check("结果消息标注图生图",
          any("图生图" in x.get("text", "") for x in out_i2i if x.get("type") == "plain")
          or any(x.get("type") == "chain" for x in out_i2i),
          [x.get("type") for x in out_i2i])

    # --denoise 覆盖
    sess2 = i2i_session(pid="i2i-2")
    ev_dn = AstrMessageEvent(message_str="/图生图 只修细节 --denoise 0.25",
                             message=[StubImage(str(png_path))])
    asyncio.run(drive(plugin.cmd_img2img(ev_dn)))
    sub_dn = None
    for method, path, kw in sess2.calls:
        if method == "POST" and path == "/prompt":
            sub_dn = kw["json"]["prompt"]
    check("行内 --denoise 覆盖默认值",
          sub_dn is not None and sub_dn["7"]["inputs"]["denoise"] == 0.25,
          (sub_dn or {}).get("7", {}).get("inputs", {}).get("denoise"))

    # /画图 带图自动转图生图
    sess3 = i2i_session(pid="i2i-3")
    ev_draw_img = AstrMessageEvent(message_str="/画图 改成赛博朋克风格",
                                   message=[StubImage(str(png_path))])
    asyncio.run(drive(plugin.cmd_draw(ev_draw_img)))
    sub_auto = None
    for method, path, kw in sess3.calls:
        if method == "POST" and path == "/prompt":
            sub_auto = kw["json"]["prompt"]
    check("/画图 带图时自动走图生图",
          sub_auto is not None
          and any(n["class_type"] == "LoadImage" for n in sub_auto.values()),
          sorted({n["class_type"] for n in (sub_auto or {}).values()}))

    # 关闭开关后 /画图 带图仍走文生图
    plugin.config["i2i"] = {"enable": False}
    sess4 = i2i_session(pid="i2i-4")
    ev_draw_off = AstrMessageEvent(message_str="/画图 少女",
                                   message=[StubImage(str(png_path))])
    asyncio.run(drive(plugin.cmd_draw(ev_draw_off)))
    sub_off = None
    for method, path, kw in sess4.calls:
        if method == "POST" and path == "/prompt":
            sub_off = kw["json"]["prompt"]
    check("关闭开关后 /画图 带图仍走文生图",
          sub_off is not None
          and not any(n["class_type"] == "LoadImage" for n in sub_off.values()),
          sorted({n["class_type"] for n in (sub_off or {}).values()}))
    plugin.config["i2i"] = {"enable": True, "denoise": 0.6, "max_side": 1536, "subfolder": "astrbot"}

    print("\n=== Hires 三个开关 ===")

    def hires_run(purpose_i2i, hires_cfg, opts=None, pid="hsw"):
        sess = api.aiohttp.ClientSession()
        sess.route("GET", "/models", api.aiohttp.ClientResponse(200, payload=["checkpoints"]))
        sess.route("GET", "/models/checkpoints", api.aiohttp.ClientResponse(
            200, payload=["SDXL/m.safetensors"]))
        sess.route("POST", "/upload/image", api.aiohttp.ClientResponse(
            200, payload={"name": "in.png", "subfolder": "astrbot", "type": "input"}))
        sess.route("POST", "/prompt", api.aiohttp.ClientResponse(
            200, payload={"prompt_id": pid}))
        sess.route("GET", "/queue", api.aiohttp.ClientResponse(
            200, payload={"queue_running": [], "queue_pending": []}))
        hist_node = "9" if purpose_i2i else "7"
        sess.route("GET", f"/history/{pid}", api.aiohttp.ClientResponse(200, payload={pid: {
            "status": {"status_str": "success", "completed": True},
            "outputs": {hist_node: {"images": [{"filename": "h.png", "type": "output"}]}}}}))
        sess.route("GET", "/view", api.aiohttp.ClientResponse(200, text="PNG"))
        plugin.comfy._session = sess
        plugin.comfy.invalidate_model_cache()
        plugin.config["hires"] = hires_cfg
        plugin.config["i2i"] = {"enable": True, "denoise": 0.6, "max_side": 1536,
                                "subfolder": "astrbot"}
        res = asyncio.run(plugin.generate(
            user_desc="少女", opts=opts or {},
            source_image=str(png_path) if purpose_i2i else ""))
        return res

    # 文生图：enable 控制
    check("enable=false 时文生图不用 Hires",
          not hires_run(False, {"enable": False}).get("hires"))
    check("enable=true 时文生图自动用 Hires",
          bool(hires_run(False, {"enable": True, "scale": 1.5}).get("hires")))

    # 图生图：enable 与 enable_for_i2i 共同决定
    check("图生图：enable=true 且 enable_for_i2i=true → 用",
          bool(hires_run(True, {"enable": True, "enable_for_i2i": True}).get("hires")))
    check("图生图：enable_for_i2i=false → 不用",
          not hires_run(True, {"enable": True, "enable_for_i2i": False}).get("hires"))
    check("图生图：enable=false → 不用",
          not hires_run(True, {"enable": False, "enable_for_i2i": True}).get("hires"))

    # 单次指令覆盖
    check("allow_inline=true 时 --hires 生效",
          bool(hires_run(False, {"enable": False, "allow_inline": True},
                         opts={"hires": "1.5"}).get("hires")))
    check("allow_inline=false 时 --hires 被忽略",
          not hires_run(False, {"enable": False, "allow_inline": False},
                        opts={"hires": "2.0"}).get("hires"))
    check("allow_inline=false 时仍按配置启用",
          bool(hires_run(False, {"enable": True, "allow_inline": False},
                         opts={"hires": "0"}).get("hires")))
    check("--hires 0 可单次关闭",
          not hires_run(False, {"enable": True, "allow_inline": True},
                        opts={"hires": "0"}).get("hires"))

    # /状态 里能看到 Hires 状态
    plugin.config["draw_settings"] = {"default_negative": "lowres"}
    ev_status = AstrMessageEvent(message_str="/状态")
    out_status = asyncio.run(drive(plugin.cmd_status(ev_status)))
    check("/状态 显示 Hires 开关状态",
          any("Hires Fix" in x.get("text", "") for x in out_status),
          [x.get("text", "")[:60] for x in out_status])

    print("\n=== 反推专用模型（vision_settings）===")

    class _VP:
        def __init__(self, pid, model="", modalities=None):
            self._pid = pid
            self._model = model
            self.provider_config = {} if modalities is None else {"modalities": modalities}

        def meta(self):
            return type("M", (), {"id": self._pid, "model": self._model})()

    ctx._providers = {"vis": _VP("vis", "qwen-vl-max", ["text", "image"]),
                      "txt": _VP("txt", "deepseek-chat", ["text"])}
    ctx.llm_calls = []
    ctx.llm_reply = '{"positive":"1girl, kimono","negative":"bad hands","summary":"和服"}'

    svc_v = llm.LLMService(ctx, {"vision_settings": {"provider": "vis"}})
    res_v = asyncio.run(svc_v.reverse_prompt(["/tmp/pic.png"]))
    check("vision_settings.provider 被用于反推",
          ctx.llm_calls and ctx.llm_calls[-1].get("chat_provider_id") == "vis",
          ctx.llm_calls[-1].get("chat_provider_id") if ctx.llm_calls else None)
    check("反推结果正常返回", res_v.get("positive") == "1girl, kimono", res_v.get("positive"))

    # vision_settings.base_url → 走自定义接口，不用 AstrBot 的 provider
    ctx.llm_calls = []
    captured = {}

    async def fake_custom(system, user, image_urls=None, conf=None):
        captured["conf"] = conf
        captured["image_urls"] = image_urls
        return '{"positive":"cat","negative":"dog","summary":"猫"}'

    svc_c = llm.LLMService(ctx, {"vision_settings": {
        "base_url": "https://example.com/v1", "api_key": "k", "model": "qwen-vl-max"}})
    svc_c._call_custom = fake_custom
    res_c = asyncio.run(svc_c.reverse_prompt(["/tmp/pic.png"]))
    check("配了自定义接口时走自定义端点（不经过 AstrBot 提供商）",
          ctx.llm_calls == [] and captured.get("conf", {}).get("model") == "qwen-vl-max",
          (len(ctx.llm_calls), captured.get("conf")))
    check("自定义接口也带上了图片",
          captured.get("image_urls") == ["/tmp/pic.png"], captured.get("image_urls"))
    check("结果里标注用的是自定义接口",
          "自定义接口" in str(res_c.get("model")), res_c.get("model"))

    # 自定义接口时不做 modalities 校验（无法判定）
    check("自定义接口不因看不到模态而误拦", res_c.get("positive") == "cat")

    ctx._providers = {}
    plugin.config["llm_settings"] = {"enable_prompt_optimize": False}

    print("\n=== 配置与表单双向一致（防止配置项没暴露 / 表单指向不存在的键）===")
    # 用固定正则从 app.js 抽出表单字段（注意：组名可能含数字，如 i2i）
    field_re = _re.compile(
        r"\{ id: '([a-z0-9_]+)', path: \['([a-z0-9_]+)', '([a-z0-9_]+)'\], kind: '([a-z]+)'"
    )
    form_fields = field_re.findall(app_js)
    check("能从 app.js 解析出表单字段", len(form_fields) >= 30, len(form_fields))

    form_paths = {f"{group}.{key}" for _id, group, key, _kind in form_fields}
    schema_obj = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    schema_paths = {
        f"{group}.{key}"
        for group, spec in schema_obj.items()
        for key in (spec.get("items") or {})
    }
    missing_in_form = sorted(schema_paths - form_paths)
    check("schema 里的每个配置项都在配置页出现（否则用户改不了）",
          not missing_in_form, missing_in_form or "全部覆盖")
    stale_in_form = sorted(form_paths - schema_paths)
    check("配置页没有指向不存在的配置项（否则保存后无效）",
          not stale_in_form, stale_in_form or "全部有效")

    # select 类型字段的 options 必须与 schema 一致
    select_fields = _re.findall(
        r"\{ id: '([a-z0-9_]+)', path: \['([a-z0-9_]+)', '([a-z0-9_]+)'\], kind: 'select',\s*"
        r"options: \[([^\]]*)\]",
        app_js,
    )
    check("存在 select 字段", len(select_fields) >= 2, len(select_fields))
    mismatched = []
    for _id, group, key, opts_raw in select_fields:
        js_opts = [x.strip().strip("'") for x in opts_raw.split(",") if x.strip()]
        schema_opts = (schema_obj.get(group, {}).get("items", {}).get(key) or {}).get("options")
        if schema_opts != js_opts:
            mismatched.append((f"{group}.{key}", js_opts, schema_opts))
    check("下拉选项与 schema 完全一致", not mismatched, mismatched or "全部一致")

    print("\n=== 死代码守卫（写了却从没接上的函数）===")
    # 这次事故的根因就是 register_pages_routes 定义完整却从未被调用。
    # 用静态检查把这类问题挡在提交前 —— 也是原插件「半死配置」毛病的对治。
    import ast as _ast

    prod = {
        path.relative_to(ROOT): path.read_text(encoding="utf-8")
        for path in sorted(ROOT.rglob("*.py"))
        if path.parent.name != "tests"
    }
    # 由 AstrBot 反射调用的入口，不算未使用
    framework_hooks = {"initialize", "terminate"}
    definitions: dict[str, str] = {}
    for rel, text in prod.items():
        for node in _ast.walk(_ast.parse(text)):
            if not isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                continue
            if node.name.startswith("__") or node.name in framework_hooks:
                continue
            if any(_ast.unparse(d).split("(")[0].startswith("filter") for d in node.decorator_list):
                continue  # @filter.command / @filter.llm_tool 等由框架调用
            definitions.setdefault(node.name, str(rel))

    referenced: set[str] = set()
    for text in prod.values():
        for node in _ast.walk(_ast.parse(text)):
            if isinstance(node, _ast.Name):
                referenced.add(node.id)
            elif isinstance(node, _ast.Attribute):
                referenced.add(node.attr)
            elif isinstance(node, _ast.Constant) and isinstance(node.value, str):
                referenced.add(node.value)  # 形如 getattr(obj, "name")

    dead = sorted((where, name) for name, where in definitions.items() if name not in referenced)
    check("没有从未被调用的函数（register_pages_routes 那类漏接）",
          not dead, dead or "全部已接上")
    check("Pages 注册函数确实被主模块调用",
          "register_pages_routes(self)" in prod[Path("main.py")],
          [str(r) for r in prod if "register_pages_routes(" in prod[r]])

    print(f"\n=== 结果：{PASSED} passed, {FAILED} failed ===")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
