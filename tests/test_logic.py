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
                 group_id="20002"):
        self._sender_id = sender_id
        self._name = name
        self.message_str = message_str
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
        raise RuntimeError("stub llm_generate must be patched")

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
    def __init__(self, path=""):
        self.path = path

    @classmethod
    def fromFileSystem(cls, path):
        return cls(path)
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
    check("加载三个内置模板", len(templates) == 3, sorted(templates))
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
    check("templates 处理器返回内置模板",
          len(tpl_resp.get("templates", [])) == 3, [t.get("name") for t in tpl_resp.get("templates", [])])

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

    try:
        asyncio.run(plugin.generate(user_desc="一只猫", opts={}))
        check("预检不通过时不提交并报明确原因", False)
    except api.ComfyUIError as exc:
        text = str(exc)
        check("预检不通过时不提交并报明确原因",
              "本地校验未通过" in text and "dpmpp_2m" in text, text.splitlines()[0][:70])
        check("失败的工作流被落盘以便排查",
              (plugin.data_dir / "last_failed_prompt.json").is_file())

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
