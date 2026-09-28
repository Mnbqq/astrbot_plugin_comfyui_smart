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
    """把**本目录**当成包 `astrbot_plugin_comfyui_smart` 导入。

    版本留档目录叫 `astrbot_plugin_comfyui_smart_v0.15.0`，包名对不上。若只是把父目录
    塞进 `sys.path`，`import astrbot_plugin_comfyui_smart` 会命中同级的**主仓库**
    （内容可能完全是另一个版本），于是「在版本目录里跑测试」测的其实是主仓库。
    这里显式按本目录构造包对象并注册进 `sys.modules`，保证测的一定是这个目录里的代码。
    """
    import importlib.util

    name = "astrbot_plugin_comfyui_smart"
    existing = sys.modules.get(name)
    if existing is not None:
        existing_path = list(getattr(existing, "__path__", []) or [])
        if existing_path and Path(existing_path[0]).resolve() == ROOT:
            return
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    if spec is None or spec.loader is None:  # pragma: no cover - 兜底
        if str(ROOT.parent) not in sys.path:
            sys.path.insert(0, str(ROOT.parent))
        return
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)


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
        self.ws_messages = []
        self.ws_calls = []
        self.ws_error = None
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

    async def ws_connect(self, url, **kw):
        """桩：WebSocket 连接，按预设消息逐条吐给调用方。"""
        self.ws_calls.append((url, kw))
        if self.ws_error is not None:
            raise self.ws_error
        return _StubWebSocket(self.ws_messages)

    async def close(self):
        self.closed = True


class _StubWebSocket:
    """桩：够用的 WebSocket —— receive() 依次返回预设消息，随后返回关闭消息。"""

    def __init__(self, messages):
        self._messages = list(messages)
        self.closed = False

    async def receive(self):
        if self._messages:
            item = self._messages.pop(0)
            if isinstance(item, _WSMessage):
                return item
            return _WSMessage(data=item)
        return _WSMessage(data=None, kind="closed")

    async def close(self):
        self.closed = True


class _WSMessage:
    def __init__(self, data=None, kind="text"):
        self.data = data
        self.type = kind
'''

ASTRBOT_API_STUB = '''
class _StubLogger:
    """桩：astrbot.api.logger。

    刻意**不使用 Python 内置 logging** —— 上架规范要求插件只能从 astrbot.api 导入 logger，
    桩跟着一起遵守，这样仓库里任何地方都不会出现 `import logging`。
    """

    def __init__(self):
        self.records = []

    def _record(self, level, msg, *args, **kw):
        try:
            text = msg % args if args else str(msg)
        except Exception:
            text = str(msg)
        self.records.append((level, text))

    def debug(self, msg, *args, **kw):
        self._record("debug", msg, *args, **kw)

    def info(self, msg, *args, **kw):
        self._record("info", msg, *args, **kw)

    def warning(self, msg, *args, **kw):
        self._record("warning", msg, *args, **kw)

    def error(self, msg, *args, **kw):
        self._record("error", msg, *args, **kw)

    def exception(self, msg, *args, **kw):
        self._record("error", msg, *args, **kw)

    def critical(self, msg, *args, **kw):
        self._record("critical", msg, *args, **kw)

    def text(self, level=""):
        return chr(10).join(t for lv, t in self.records if not level or lv == level)


logger = _StubLogger()


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


class Video:
    """桩：AstrBot 的 Video 组件（file 是 file:// URI，path 是本地路径）。"""

    def __init__(self, file="", path="", **kw):
        self.file = file
        self.path = path or (file or "")
        self.url = ""

    @classmethod
    def fromFileSystem(cls, path):
        return cls(file="file://" + str(path), path=str(path))


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


PLUGIN_SRC_ROOT = Path(__file__).resolve().parent.parent
# 参与上架规范检查的源码（tests/ 自身不算插件运行时）
_AUDIT_FILES = ("main.py", "comfyui_api.py", "llm_service.py", "storage.py",
                "permission.py", "workflow_templates.py", "queue_gate.py",
                "backend_pool.py", "ui_workflow.py", "i18n.py", "pages/__init__.py")
_AUDIT_DIRS = ("pages/settings",)


def _src(rel: str) -> str:
    """读取插件源码文件。"""
    return (PLUGIN_SRC_ROOT / rel).read_text(encoding="utf-8")


def _audited_py_files() -> list[Path]:
    """插件运行时涉及的全部 .py 文件。"""
    files = [PLUGIN_SRC_ROOT / name for name in _AUDIT_FILES]
    for sub in _AUDIT_DIRS:
        files.extend(sorted((PLUGIN_SRC_ROOT / sub).glob("*.py")))
    return [f for f in files if f.is_file()]


def _logging_violations() -> list[str]:
    """找出违规使用 Python 内置 logging 的文件与行。

    上架规范：logger 必须且只能从 astrbot.api 导入（`from astrbot.api import logger`），
    不允许 `import logging` / `logging.getLogger(...)`。曾因此在 `main.py` 里留了个
    「self.logger 缺失时回退 logging.getLogger("astrbot")」的分支，被上架审查退回。
    """
    bad: list[str] = []
    for path in _audited_py_files():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if re.search(r"(^|\s)import logging\b", line) or re.search(r"\blogging\.", line):
                bad.append(f"{path.name}:{lineno}: {stripped}")
    return bad


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
    # 上架规范：logger 必须且只能来自 astrbot.api，严禁 Python 内置 logging。
    from astrbot.api import logger as _api_logger

    check("main.py 用的就是 astrbot.api 的 logger", m.logger is _api_logger,
          type(m.logger).__name__)
    check("不再依赖 Star.logger（基类没有该属性也照跑）",
          not hasattr(plugin, "logger"))
    check("插件源码里没有 Python 内置 logging", _logging_violations() == [],
          _logging_violations())
    check("插件源码确实从 astrbot.api 导入 logger",
          any("from astrbot.api import" in t and "logger" in t
              for t in (_src("main.py"), _src("pages/__init__.py"))))

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
    # 允许目录名带版本后缀：astrbot_plugin_comfyui_smart_v0.8.0 这种按版本留档、
    # 供手动测试用的目录，目录名与 metadata.name 必然不同，不能算漂移。
    dir_name = re.sub(r"_v\d+\.\d+\.\d+$", "", ROOT.name)
    check("metadata.name 与目录名一致（参考插件里踩过这个坑；允许 _vX.Y.Z 后缀）",
          _meta_field("name") == dir_name, f"{_meta_field('name')} vs {dir_name}")
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

    loaded_builtin = wt.load_templates(ROOT / "workflows")
    for path in sorted((ROOT / "workflows").glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        check(f"模板 {path.stem} 内部 name 与文件名一致",
              data.get("name") == path.stem, data.get("name"))
        # 坏模板会被 load_templates **静默跳过**：新模板写错了会表现为「插件里根本没有它」，
        # 所以这里逐个确认「真的加载进来了」，而不是只检查文件存在。
        check(f"模板 {path.stem} 真的被加载（绑定/params/图结构都合法）",
              path.stem in loaded_builtin, sorted(loaded_builtin))

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

    print("\n=== 出图并发与排队治理（v0.7.0）===")
    from astrbot_plugin_comfyui_smart import queue_gate as qg

    async def gate_fifo():
        """并发上限 1：后来者按先来后到排队，并拿到位次。"""
        gate = qg.ConcurrencyGate(max_concurrent=1, per_user_limit=0, wait_timeout=5)
        order: list[str] = []
        waits: list[dict] = []

        async def worker(name: str, hold: float):
            async with gate.hold(name, on_wait=waits.append) as slot:
                order.append(f"{name}:start")
                await asyncio.sleep(hold)
                order.append(f"{name}:end")
            return slot

        tasks = [asyncio.create_task(worker("a", 0.2))]
        await asyncio.sleep(0.05)  # 让 a 先拿到名额
        tasks.append(asyncio.create_task(worker("b", 0.01)))
        tasks.append(asyncio.create_task(worker("c", 0.01)))
        slots = await asyncio.gather(*tasks)
        return gate, order, waits, slots

    gate, order, waits, slots = asyncio.run(gate_fifo())
    check("同时出图上限生效：同一时刻只跑一个任务",
          order == ["a:start", "a:end", "b:start", "b:end", "c:start", "c:end"], order)
    check("排队者拿到正确位次（位次要算上正在跑的）",
          [w["ahead"] for w in waits] == [1, 2], waits)
    check("排队原因标为 capacity（名额被前面的人占着）",
          all(w["reason"] == "capacity" for w in waits), waits)
    check("无需排队时等待为 0、位次为 1",
          slots[0].waited == 0 and slots[0].position == 1, slots[0])
    check("排队过的任务记录了等待时长",
          slots[1].waited > 0 and slots[2].waited >= slots[1].waited,
          [s.waited for s in slots])
    check("跑完后名额与队列都归零（没有泄漏）",
          gate.snapshot()["running"] == 0 and gate.snapshot()["waiting"] == 0, gate.snapshot())

    async def gate_per_user():
        """单人上限 1：一个人连发不会把队伍堵死，后面的人照样进场。"""
        gate = qg.ConcurrencyGate(max_concurrent=2, per_user_limit=1, wait_timeout=5)
        started: list[str] = []
        blocking = asyncio.Event()

        async def job(name: str, hold_open: bool = False):
            async with gate.hold(name):
                started.append(name)
                if hold_open:
                    await blocking.wait()

        first = asyncio.create_task(job("a", True))
        await asyncio.sleep(0.05)
        second = asyncio.create_task(job("a"))  # 自己已有一个：被单人上限挡住
        await asyncio.sleep(0.05)
        third = asyncio.create_task(job("b", True))  # 另一个人：直接进场，不用等 a 的第二个
        await asyncio.sleep(0.05)
        during = gate.snapshot()
        blocking.set()
        await asyncio.gather(first, second, third)
        return during, started

    during, started = asyncio.run(gate_per_user())
    check("单人上限生效：同一个人不会同时占两个名额",
          during["running_by_user"] == {"a": 1, "b": 1}, during["running_by_user"])
    check("单人上限挡住队首时，后面的人照样能进场（不堵队）",
          started[:2] == ["a", "b"], started)

    async def gate_timeout():
        """排队超时必须明确报错，并且不泄漏名额。"""
        gate = qg.ConcurrencyGate(max_concurrent=1, per_user_limit=0, wait_timeout=0.1)
        blocking = asyncio.Event()

        async def holder():
            async with gate.hold("x"):
                await blocking.wait()

        task = asyncio.create_task(holder())
        await asyncio.sleep(0.03)
        message = ""
        try:
            async with gate.hold("y"):
                message = "居然进场了"
        except qg.QueueTimeout as e:
            message = str(e)
        blocking.set()
        await task
        return message, gate.snapshot()

    message, snapshot = asyncio.run(gate_timeout())
    check("排队超时给出中文原因并指出可调项",
          "排队等待超过" in message and "同时出图上限" in message, message)
    check("排队超时后名额与队列都归零（不泄漏名额）",
          snapshot["running"] == 0 and snapshot["waiting"] == 0, snapshot)

    async def gate_error_release():
        """出图中途抛异常也必须归还名额。"""
        gate = qg.ConcurrencyGate(max_concurrent=1, per_user_limit=0, wait_timeout=1)
        try:
            async with gate.hold("u"):
                raise ValueError("出图过程中炸了")
        except ValueError:
            pass
        async with gate.hold("u2") as slot:
            immediate = slot.waited == 0
        return immediate, gate.snapshot()

    immediate, snapshot = asyncio.run(gate_error_release())
    check("出图中途抛异常也会归还名额（否则后面的人全被卡死）", immediate, snapshot)

    async def gate_configure():
        """配置热更新（上限调大）后，等待者立刻进场。"""
        gate = qg.ConcurrencyGate(max_concurrent=1, per_user_limit=0, wait_timeout=5)
        started: list[str] = []
        blocking = asyncio.Event()

        async def job(name: str, hold_open: bool = False):
            async with gate.hold(name):
                started.append(name)
                if hold_open:
                    await blocking.wait()

        first = asyncio.create_task(job("first", True))
        await asyncio.sleep(0.03)
        second = asyncio.create_task(job("second"))
        await asyncio.sleep(0.03)
        before = list(started)
        gate.configure(max_concurrent=2)
        await asyncio.sleep(0.05)
        after = list(started)
        blocking.set()
        await asyncio.gather(first, second)
        return before, after, gate.snapshot()

    before, after, snapshot = asyncio.run(gate_configure())
    check("上限调大后排队的人立刻进场（不必等下一次 release）",
          before == ["first"] and after == ["first", "second"], (before, after))
    check("配置热更新后状态一致",
          snapshot["max_concurrent"] == 2 and snapshot["running"] == 0, snapshot)

    async def gate_shutdown():
        """插件卸载/重载时唤醒等待者，而不是让他们挂到排队超时。"""
        gate = qg.ConcurrencyGate(max_concurrent=1, per_user_limit=0, wait_timeout=30)
        blocking = asyncio.Event()

        async def holder():
            async with gate.hold("x"):
                await blocking.wait()

        task = asyncio.create_task(holder())
        await asyncio.sleep(0.03)
        waiter = asyncio.create_task(gate.acquire("y"))
        await asyncio.sleep(0.03)
        woken = gate.shutdown("插件正在重载")
        blocking.set()
        await task
        error = ""
        try:
            await waiter
        except qg.QueueClosed as e:
            error = str(e)
        return woken, error, gate.snapshot()

    woken, error, snapshot = asyncio.run(gate_shutdown())
    check("插件卸载时唤醒排队中的人（而不是让他们干等到超时）",
          woken == 1 and "重载" in error, (woken, error))
    check("关闭后闸门状态可见（closed=True）", snapshot["closed"] is True, snapshot)

    print("\n=== 并发闸门接入出图主流程（两个人同时发 /画图）===")
    plugin.config["llm_settings"] = {"enable_prompt_optimize": False}
    plugin.config["queue"] = {"max_concurrent": 1, "per_user_limit": 0, "wait_timeout": 10}
    plugin._configure_gate()
    fresh_ok_session(pid="q-1")

    inflight = {"now": 0, "max": 0}
    real_wait = plugin.comfy.wait_for_images

    async def _slow_wait(prompt_id, output_dir, on_queued=None, on_progress=None,
                         cancel_event=None, user_cancel_event=None):
        """桩：把「等待出图」拉长到 0.1s，并记录同时在跑的任务数。"""
        inflight["now"] += 1
        inflight["max"] = max(inflight["max"], inflight["now"])
        try:
            await asyncio.sleep(0.1)
            target = Path(output_dir) / f"{prompt_id}.png"
            target.write_bytes(b"PNGQ")
            return [target]
        finally:
            inflight["now"] -= 1

    plugin.comfy.wait_for_images = _slow_wait
    waits_seen: list[dict] = []

    async def _two_users():
        return await asyncio.gather(
            plugin.generate(user_desc="一只白猫", opts={},
                            event=AstrMessageEvent(sender_id="1001"),
                            on_wait=waits_seen.append),
            plugin.generate(user_desc="一只黑猫", opts={},
                            event=AstrMessageEvent(sender_id="1002"),
                            on_wait=waits_seen.append),
        )

    results = asyncio.run(_two_users())
    plugin.comfy.wait_for_images = real_wait
    check("两人同时出图时 ComfyUI 侧只跑一个（并发上限 1 真的生效）",
          inflight["max"] == 1, inflight)
    check("两人都拿到了图", all(len(r["images"]) == 1 for r in results),
          [len(r["images"]) for r in results])
    check("被排队的人收到了排队提示（含前方任务数）",
          bool(waits_seen) and waits_seen[0]["ahead"] >= 1, waits_seen)
    check("结果里带上了排队秒数（便于解释这次为什么慢）",
          all("queued_seconds" in r for r in results),
          [r.get("queued_seconds") for r in results])
    check("排队秒数真的被记下来了（第二个人 > 0）",
          sorted(r["queued_seconds"] for r in results)[1] > 0,
          [r["queued_seconds"] for r in results])

    out_gate = asyncio.run(drive(plugin.cmd_status(AstrMessageEvent(message_str="/状态"))))
    status_text = "\n".join(x.get("text", "") for x in out_gate)
    check("/状态 显示并发上限与排队情况",
          "并发：上限 1" in status_text and "排队" in status_text, status_text[:160])
    gate_resp = asyncio.run(handlers[(f"{base}/status", ("GET",))]())
    check("Pages 状态接口带上网关信息（配置页状态栏要显示）",
          isinstance(gate_resp.get("gate"), dict) and "max_concurrent" in gate_resp["gate"],
          gate_resp.get("gate"))

    plugin.config["queue"] = {}
    plugin._configure_gate()
    default_gate = plugin.gate.snapshot()
    check("未配置队列时按保守默认（同时 1、单人 1、排队等待 300 秒）",
          (default_gate["max_concurrent"], default_gate["per_user_limit"],
           default_gate["wait_timeout"]) == (1, 1, 300.0), default_gate)
    schema_queue = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    check("schema 里有 queue 组，三个开关齐全（否则用户改不了并发）",
          {"max_concurrent", "per_user_limit", "wait_timeout"}
          <= set((schema_queue.get("queue") or {}).get("items") or {}),
          sorted((schema_queue.get("queue") or {}).get("items") or {}))

    print("\n=== 实时进度（WebSocket）与 /取消（v0.8.0）===")

    async def _idle_queue():
        return api.QueueStatus()

    async def _ws_progress_flow():
        """WebSocket 推来的步数事件要能解析出 n/总步数，并过滤掉别人的任务。"""
        client = api.ComfyUI("http://127.0.0.1:8188", 5, poll_interval=0.01)
        sess = api.aiohttp.ClientSession()
        sess.ws_messages = [
            json.dumps({"type": "progress", "data": {"value": 3, "max": 12, "prompt_id": "p1"}}),
            # 别人的任务（prompt_id 不匹配）：必须忽略，否则会把别人的进度发给自己
            json.dumps({"type": "progress", "data": {"value": 9, "max": 12, "prompt_id": "p2"}}),
            json.dumps({"type": "progress", "data": {"value": 12, "max": 12, "prompt_id": "p1"}}),
            json.dumps({"type": "executing", "data": {"node": None, "prompt_id": "p1"}}),
        ]
        client._session = sess
        client.queue_status = _idle_queue
        seen: list[dict] = []
        calls = {"n": 0}

        async def _request(method, path, **kw):
            calls["n"] += 1
            # 先让进度事件跑完再给历史，避免测试里出现竞态
            if len(seen) < 2 and calls["n"] < 200:
                return {}
            return {"p1": {"status": {"status_str": "success", "completed": True},
                           "outputs": {"7": {"images": [{"filename": "x.png"}]}}}}

        async def _download(images, output_dir):
            return [Path(output_dir) / "x.png"]

        client._request = _request
        client._download_images = _download
        paths = await client.wait_for_images(
            "p1", Path(plugin.storage.output_dir), on_progress=seen.append
        )
        return seen, paths, sess

    seen_progress, ws_paths, ws_sess = asyncio.run(_ws_progress_flow())
    check("WebSocket 进度被解析成 n/总步数（含百分比）",
          [(p["value"], p["max"], p["percent"]) for p in seen_progress] == [(3, 12, 25), (12, 12, 100)],
          seen_progress)
    check("别人的任务进度被忽略（prompt_id 过滤）",
          all(p["value"] != 9 for p in seen_progress), seen_progress)
    check("进度推送不影响等待与下载", len(ws_paths) == 1, ws_paths)
    check("WebSocket 连接地址是 /ws 且带上 client_id",
          bool(ws_sess.ws_calls) and "/ws?clientId=" in ws_sess.ws_calls[0][0],
          ws_sess.ws_calls[:1])

    async def _ws_broken_flow():
        """连不上 WebSocket（老版本/反代没转发）时必须安静退回轮询。"""
        client = api.ComfyUI("http://127.0.0.1:8188", 5, poll_interval=0.01)
        sess = api.aiohttp.ClientSession()
        sess.ws_error = RuntimeError("WebSocket 不可用")
        client._session = sess
        client.queue_status = _idle_queue
        seen: list[dict] = []

        async def _request(method, path, **kw):
            return {"p1": {"status": {"status_str": "success", "completed": True},
                           "outputs": {"7": {"images": [{"filename": "z.png"}]}}}}

        async def _download(images, output_dir):
            return [Path(output_dir) / "z.png"]

        client._request = _request
        client._download_images = _download
        paths = await client.wait_for_images(
            "p1", Path(plugin.storage.output_dir), on_progress=seen.append
        )
        return seen, paths

    broken_seen, broken_paths = asyncio.run(_ws_broken_flow())
    check("WebSocket 不可用时照样出图（进度只是锦上添花）",
          len(broken_paths) == 1 and broken_seen == [], (broken_paths, broken_seen))

    # 进度提示的节流：步数事件很密，不节流会把聊天刷爆
    plugin.config["output"] = {"show_progress": True, "progress_interval": 3600}
    ev_prog = AstrMessageEvent(message_str="/画图 猫")
    _, _, on_progress = plugin._queue_notifiers(ev_prog)
    asyncio.run(on_progress({"value": 1, "max": 10, "percent": 10}))
    asyncio.run(on_progress({"value": 2, "max": 10, "percent": 20}))
    asyncio.run(on_progress({"value": 10, "max": 10, "percent": 100}))
    check("进度提示会节流（中间步数不刷屏），但 100% 一定发",
          len(ev_prog.sent) == 2 and "100%" in ev_prog.sent[-1]["text"],
          [s["text"] for s in ev_prog.sent])
    check("进度提示里能看到 /取消 的提示语",
          "取消" in ev_prog.sent[0]["text"], ev_prog.sent[0]["text"])

    plugin.config["output"] = {"show_progress": False}
    ev_prog_off = AstrMessageEvent(message_str="/画图 猫")
    _, _, on_progress_off = plugin._queue_notifiers(ev_prog_off)
    asyncio.run(on_progress_off({"value": 5, "max": 10, "percent": 50}))
    check("关掉进度提示后一条都不发", ev_prog_off.sent == [], ev_prog_off.sent)

    async def _position_flow():
        """排队位置变化时才再提示；同一位置不重复刷屏。"""
        client = api.ComfyUI("http://127.0.0.1:8188", 5, poll_interval=0.005)
        client._session = api.aiohttp.ClientSession()
        positions = [3, 3, 2, 1]
        state = {"i": 0}
        seen: list[dict] = []
        calls = {"n": 0}

        async def _queue():
            index = min(state["i"], len(positions) - 1)
            state["i"] += 1
            return api.QueueStatus(
                own_pending=1, total_pending=5, own_positions={"p1": positions[index]}
            )

        async def _request(method, path, **kw):
            calls["n"] += 1
            if calls["n"] < 5:
                return {}
            return {"p1": {"status": {"status_str": "success", "completed": True},
                           "outputs": {"7": {"images": [{"filename": "y.png"}]}}}}

        async def _download(images, output_dir):
            return [Path(output_dir) / "y.png"]

        client.queue_status = _queue
        client._request = _request
        client._download_images = _download
        await client.wait_for_images(
            "p1", Path(plugin.storage.output_dir),
            on_queued=lambda status: seen.append(dict(status.own_positions)),
        )
        return seen

    positions_seen = asyncio.run(_position_flow())
    check("排队位置变化时才再提示（3 → 2 → 1，重复的 3 不刷屏）",
          positions_seen == [{"p1": 3}, {"p1": 2}, {"p1": 1}], positions_seen)

    print("\n--- /取消 ---")
    plugin.config["output"] = {}
    cancel_calls: list[str] = []

    async def _fake_cancel_prompt(prompt_id):
        cancel_calls.append(prompt_id)
        return "running" if prompt_id == "run-1" else "pending"

    real_cancel_prompt = plugin.comfy.cancel_prompt
    plugin.comfy.cancel_prompt = _fake_cancel_prompt
    plugin._active_jobs.clear()
    plugin._job_cancel.clear()

    ev_cancel_owner = AstrMessageEvent(sender_id="1001", message_str="/取消")
    plugin._active_jobs["run-1"] = "1001"
    job_ev = asyncio.Event()
    plugin._job_cancel["run-1"] = job_ev
    out_cancel = asyncio.run(drive(plugin.cmd_cancel(ev_cancel_owner)))
    check("进行中的任务会被真正取消（信号置位 + 通知 ComfyUI）",
          cancel_calls == ["run-1"] and job_ev.is_set(), (cancel_calls, job_ev.is_set()))
    check("/取消 会回报实际做了什么",
          "已中断正在执行的任务" in out_cancel[0]["text"], out_cancel[0]["text"])

    # 别人的任务不能被我取消
    cancel_calls.clear()
    plugin._job_cancel.pop("run-1", None)
    plugin._active_jobs.pop("run-1", None)  # 它已被取消，handler 收尾时就会从表里摘掉
    plugin._active_jobs["run-2"] = "2002"
    ev_other = AstrMessageEvent(sender_id="1009", message_str="/取消")
    out_other = asyncio.run(drive(plugin.cmd_cancel(ev_other)))
    check("只能取消自己的任务（别人的任务不受影响）",
          cancel_calls == [] and "run-2" in plugin._active_jobs, (cancel_calls, out_other[0]["text"]))
    check("没有任务可取消时明确说明",
          "没有正在排队或正在出图的任务" in out_other[0]["text"], out_other[0]["text"])

    # 管理员可以 /取消 全部
    ev_admin = AstrMessageEvent(sender_id="1", message_str="/取消 全部", admin=True)
    out_admin = asyncio.run(drive(plugin.cmd_cancel(ev_admin)))
    check("管理员 /取消 全部 能取消所有人的任务",
          cancel_calls == ["run-2"], cancel_calls)
    check("管理员取消后同样有回报", "已取消" in out_admin[0]["text"], out_admin[0]["text"])

    # 还在插件侧排队（没拿到名额）的人也要能取消
    async def _cancel_waiter_flow():
        plugin._active_jobs.clear()
        plugin._job_cancel.clear()
        plugin.gate.configure(max_concurrent=1, per_user_limit=0, wait_timeout=5)
        holder = await plugin.gate.acquire("blocker")
        waiter = asyncio.create_task(plugin.gate.acquire("1001"))
        await asyncio.sleep(0.02)
        out = [
            item
            async for item in plugin.cmd_cancel(
                AstrMessageEvent(sender_id="1001", message_str="/取消")
            )
        ]
        error = ""
        try:
            await waiter
        except qg.QueueCancelled as e:
            error = str(e)
        plugin.gate.release(holder)
        return out, error

    out_waiter, waiter_error = asyncio.run(_cancel_waiter_flow())
    check("还在插件侧排队的人也能被 /取消（不必等到排队超时）",
          "取消" in waiter_error, waiter_error)
    check("/取消 会说明取消了排队中的任务",
          "排队" in out_waiter[0]["text"], out_waiter[0]["text"])
    plugin.comfy.cancel_prompt = real_cancel_prompt
    plugin._active_jobs.clear()
    plugin._job_cancel.clear()

    print("\n=== 扩图 outpaint（v0.9.0）===")
    tpl_op = wt.load_templates(ROOT / "workflows")["outpaint_checkpoint"]
    check("内置扩图模板已加载，用途是 outpaint",
          tpl_op.purpose == "outpaint", tpl_op.purpose)
    check("扩图模板只用 ComfyUI 自带核心节点（无需自定义节点）",
          {"ImagePadForOutpaint", "SetLatentNoiseMask"} <= tpl_op.required_nodes(),
          sorted(tpl_op.required_nodes()))
    check("扩图模板声明了五个可注入参数（左右上下 + 羽化）",
          sorted(tpl_op.params) == ["bottom", "feathering", "left", "right", "top"],
          tpl_op.params)

    pads_default = m.default_outpaint_pads(640, 960)
    check("默认扩展量是每边 25%（对齐 8 的倍数）",
          (pads_default["left"], pads_default["right"],
           pads_default["top"], pads_default["bottom"]) == (160, 160, 240, 240), pads_default)
    huge = {"left": 4096, "right": 4096, "top": 4096, "bottom": 4096, "feathering": 40}
    fitted = m.fit_outpaint_pads(1024, 1024, huge)
    check("扩展后会超出像素预算时按比例压回去（防止一扩就爆显存）",
          sum(fitted[k] for k in ("left", "right", "top", "bottom")) < 16384
          and all(fitted[k] % 8 == 0 for k in ("left", "right", "top", "bottom")), fitted)

    built_op = tpl_op.build(
        positive="p", negative="n", model_name="SDXL/m.safetensors",
        width=960, height=1440, steps=20, cfg=6.0,
        sampler="dpmpp_2m", scheduler="karras", seed=1,
        image_name="astrbot/in.png",
        params={"left": 256, "right": 256, "top": 0, "bottom": 0,
                "feathering": 32, "根本没声明": 5},
    )
    pad_inputs = built_op["5"]["inputs"]
    check("扩图参数写进了 ImagePadForOutpaint 节点",
          (pad_inputs["left"], pad_inputs["right"], pad_inputs["top"],
           pad_inputs["bottom"], pad_inputs["feathering"]) == (256, 256, 0, 0, 32), pad_inputs)
    check("模板没声明的参数不会凭空写进工作流",
          "根本没声明" not in pad_inputs, pad_inputs)
    check("尺寸只写进真实存在的输入（VAEEncode 没有 width/height）",
          "width" not in built_op["6"]["inputs"] and "height" not in built_op["6"]["inputs"],
          built_op["6"]["inputs"])
    check("扩图语义：采样器吃的是「遮罩后的潜空间」",
          built_op["8"]["inputs"]["latent_image"] == ["7", 0],
          built_op["8"]["inputs"]["latent_image"])
    check("LoadImage 拿到上传后的图片引用",
          built_op["4"]["inputs"]["image"] == "astrbot/in.png", built_op["4"]["inputs"]["image"])
    check("扩图默认 denoise 保持 1.0（靠遮罩保住原图区域，不是靠低重绘幅度）",
          built_op["8"]["inputs"]["denoise"] == 1.0, built_op["8"]["inputs"]["denoise"])

    # params 写错要在**加载时**报错，而不是等出图才失败
    op_graph = json.loads(
        (ROOT / "workflows" / "outpaint_checkpoint.json").read_text(encoding="utf-8")
    )["graph"]
    bad_node = ""
    try:
        wt.WorkflowTemplate("bad_params", op_graph, params={"left": "999"})
    except wt.TemplateError as e:
        bad_node = str(e)
    check("params 指向不存在的节点时拒绝加载", "不存在的节点" in bad_node, bad_node)
    bad_key = ""
    try:
        wt.WorkflowTemplate("bad_params2", op_graph, params={"没有这个输入": "5"})
    except wt.TemplateError as e:
        bad_key = str(e)
    check("params 指向节点上不存在的输入时拒绝加载", "不存在该输入" in bad_key, bad_key)

    only_t2i = {"sd_checkpoint": plugin.templates["sd_checkpoint"]}
    picked_op, _op_arch = wt.pick_template(
        only_t2i, model_name="SDXL/m.safetensors", model_folder="checkpoints",
        purpose="outpaint",
    )
    check("没有扩图模板时明确失败，而不是退回文生图模板（否则输入图被静默忽略）",
          picked_op is None, picked_op)

    def outpaint_session(pid="op-1"):
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
        sess.route("GET", f"/history/{pid}", api.aiohttp.ClientResponse(200, payload={pid: {
            "status": {"status_str": "success", "completed": True},
            "outputs": {"10": {"images": [{"filename": "op.png", "type": "output"}]}}}}))
        sess.route("GET", "/view", api.aiohttp.ClientResponse(200, text="PNG"))
        plugin.comfy._session = sess
        plugin.comfy.invalidate_model_cache()
        return sess

    plugin.config["llm_settings"] = {"enable_prompt_optimize": False}
    plugin.config["hires"] = {"enable": False}
    plugin.config["draw_settings"] = {"default_negative": "lowres"}
    # 前面的用例已经在配额里记了账：这里显式清掉限制、并用一个干净的发送者，
    # 免得测的是「今日已达上限」而不是扩图本身
    plugin.config["permission"] = {}
    plugin.permission.reload({})
    OP_UID = "7007"

    sess_op = outpaint_session()
    ev_op = AstrMessageEvent(sender_id=OP_UID, message_str="/扩图 --left 256 --right 256",
                             message=[StubImage(str(png_path))])
    out_op = asyncio.run(drive(plugin.cmd_outpaint(ev_op)))
    submitted_op = None
    for method, path, kw in sess_op.calls:
        if method == "POST" and path == "/prompt":
            submitted_op = kw["json"]["prompt"]
    check("/扩图 提交的确实是扩图工作流",
          submitted_op is not None
          and {"ImagePadForOutpaint", "SetLatentNoiseMask"}
          <= {n["class_type"] for n in submitted_op.values()},
          sorted({n["class_type"] for n in (submitted_op or {}).values()}))
    check("「只往左右扩」时上下保持 0（不会偷偷带上默认的上下扩展）",
          (submitted_op["5"]["inputs"]["left"], submitted_op["5"]["inputs"]["right"],
           submitted_op["5"]["inputs"]["top"], submitted_op["5"]["inputs"]["bottom"])
          == (256, 256, 0, 0), submitted_op["5"]["inputs"])
    # 出图详情在 chain 里（Plain 组件），不在顶层 text
    def _flatten(items):
        parts = []
        for item in items:
            if item.get("type") == "chain":
                parts.extend(getattr(comp, "text", "") for comp in item.get("chain") or [])
            else:
                parts.append(item.get("text", ""))
        return "\n".join(p for p in parts if p)

    op_text = _flatten(out_op)
    check("/扩图 消息里报出「原图 → 扩后」尺寸",
          "扩图：640x960 → 1152x960" in op_text, op_text[:300])
    check("/扩图 结果里带上了图片",
          any(c.__class__.__name__ == "Image" for item in out_op
              if item.get("type") == "chain" for c in item.get("chain") or []),
          [item.get("type") for item in out_op])

    # 一边都没扩：明确报错，而不是提交一张等于原图的图
    sess_op2 = outpaint_session(pid="op-2")
    ev_op2 = AstrMessageEvent(sender_id=OP_UID, message_str="/扩图 --left 0",
                              message=[StubImage(str(png_path))])
    out_op2 = asyncio.run(drive(plugin.cmd_outpaint(ev_op2)))
    check("扩展量为 0 时明确报错", "至少要往一边扩" in out_op2[-1]["text"], out_op2[-1]["text"])

    # 没给扩图模板时给出可操作的报错
    saved_tpl = plugin.templates.pop("outpaint_checkpoint")
    sess_op3 = outpaint_session(pid="op-3")
    ev_op3 = AstrMessageEvent(sender_id=OP_UID, message_str="/扩图 --left 128",
                              message=[StubImage(str(png_path))])
    out_op3 = asyncio.run(drive(plugin.cmd_outpaint(ev_op3)))
    plugin.templates["outpaint_checkpoint"] = saved_tpl
    check("缺扩图模板时报错并指出该放哪个文件",
          "outpaint_checkpoint.json" in out_op3[-1]["text"], out_op3[-1]["text"])

    print("\n=== 局部重绘 inpaint（v0.10.0）===")
    tpl_ip = wt.load_templates(ROOT / "workflows")["inpaint_checkpoint"]
    check("内置局部重绘模板已加载，用途是 inpaint",
          tpl_ip.purpose == "inpaint", tpl_ip.purpose)
    check("重绘模板只用 ComfyUI 自带节点",
          {"LoadImageMask", "GrowMask", "SetLatentNoiseMask"} <= tpl_ip.required_nodes(),
          sorted(tpl_ip.required_nodes()))
    check("输入图与遮罩是两个独立角色（节点不同、输入键相同）",
          tpl_ip.bindings["image_loader"] == ("4", "image")
          and tpl_ip.bindings["mask_loader"] == ("5", "image"),
          (tpl_ip.bindings["image_loader"], tpl_ip.bindings["mask_loader"]))
    check("模板声明了遮罩外扩参数", tpl_ip.params == {"expand": "7"}, tpl_ip.params)

    built_ip = tpl_ip.build(
        positive="p", negative="n", model_name="SDXL/m.safetensors",
        width=512, height=512, steps=20, cfg=6.0, sampler="dpmpp_2m",
        scheduler="karras", seed=7, image_name="astrbot/a.png",
        mask_name="astrbot/m.png", params={"expand": 8},
    )
    check("原图写进 LoadImage", built_ip["4"]["inputs"]["image"] == "astrbot/a.png",
          built_ip["4"]["inputs"])
    check("遮罩写进 LoadImageMask", built_ip["5"]["inputs"]["image"] == "astrbot/m.png",
          built_ip["5"]["inputs"])
    check("遮罩通道走 red（白底黑字：白=重画）",
          built_ip["5"]["inputs"]["channel"] == "red", built_ip["5"]["inputs"])
    check("遮罩外扩按整数写入（不是字符串）",
          built_ip["7"]["inputs"]["expand"] == 8
          and isinstance(built_ip["7"]["inputs"]["expand"], int), built_ip["7"]["inputs"])
    check("布尔参数保持布尔（没被 int() 挤成 1/0）",
          built_ip["7"]["inputs"]["tapered_corners"] is True, built_ip["7"]["inputs"])
    check("重绘语义：采样器吃的是「遮罩外扩后的潜空间」",
          built_ip["9"]["inputs"]["latent_image"] == ["8", 0],
          built_ip["9"]["inputs"]["latent_image"])

    picked_ip, _ip_arch = wt.pick_template(
        {"sd_checkpoint": plugin.templates["sd_checkpoint"]},
        model_name="SDXL/m.safetensors", model_folder="checkpoints", purpose="inpaint",
    )
    check("没有重绘模板时明确失败，而不是退回文生图模板", picked_ip is None, picked_ip)

    def inpaint_session(pid="ip-1"):
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
        sess.route("GET", f"/history/{pid}", api.aiohttp.ClientResponse(200, payload={pid: {
            "status": {"status_str": "success", "completed": True},
            "outputs": {"11": {"images": [{"filename": "ip.png", "type": "output"}]}}}}))
        sess.route("GET", "/view", api.aiohttp.ClientResponse(200, text="PNG"))
        plugin.comfy._session = sess
        plugin.comfy.invalidate_model_cache()
        return sess

    import base64 as _b64

    def png_data_url(width, height):
        raw = (b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IHDR"
               + width.to_bytes(4, "big") + height.to_bytes(4, "big")
               + b"\x08\x02\x00\x00\x00" + b"\x00\x00\x00\x00")
        return "data:image/png;base64," + _b64.b64encode(raw).decode()

    plugin.config["llm_settings"] = {"enable_prompt_optimize": False}
    plugin.config["hires"] = {"enable": False}
    plugin.config["permission"] = {}
    plugin.permission.reload({})
    img_data = "data:image/png;base64," + _b64.b64encode(png_path.read_bytes()).decode()
    mask_data = img_data  # 同一张 640x960 的 PNG 当遮罩：尺寸一致才合法

    sess_ip = inpaint_session()
    ip_result = asyncio.run(plugin.inpaint({
        "image": img_data, "mask": mask_data,
        "prompt": "换成红色的裙子", "denoise": "0.9", "grow": "4",
    }))
    submitted_ip = None
    for method, path, kw in sess_ip.calls:
        if method == "POST" and path == "/prompt":
            submitted_ip = kw["json"]["prompt"]
    check("局部重绘提交的是重绘工作流",
          submitted_ip is not None
          and {"LoadImageMask", "GrowMask", "SetLatentNoiseMask"}
          <= {n["class_type"] for n in submitted_ip.values()},
          sorted({n["class_type"] for n in (submitted_ip or {}).values()}))
    check("遮罩引用是上传到 ComfyUI 后的那个文件",
          (submitted_ip or {}).get("5", {}).get("inputs", {}).get("image", "").startswith("astrbot/"),
          (submitted_ip or {}).get("5", {}).get("inputs"))
    check("原图引用是上传到 ComfyUI 后的那个文件",
          (submitted_ip or {}).get("4", {}).get("inputs", {}).get("image", "").startswith("astrbot/"),
          (submitted_ip or {}).get("4", {}).get("inputs"))
    check("遮罩外扩来自页面参数",
          (submitted_ip or {}).get("7", {}).get("inputs", {}).get("expand") == 4,
          (submitted_ip or {}).get("7", {}).get("inputs"))
    check("重绘幅度来自页面参数",
          (submitted_ip or {}).get("9", {}).get("inputs", {}).get("denoise") == 0.9,
          (submitted_ip or {}).get("9", {}).get("inputs"))
    check("页面拿到结果图（能按 ref 取回文件）",
          ip_result.get("ok") is True
          and ip_result.get("image", "").startswith("images/")
          and (plugin.storage.output_dir / Path(ip_result["image"]).name).is_file(),
          ip_result.get("image"))
    check("页面传的临时原图/遮罩已清理（不占磁盘）",
          not [p for p in (plugin.data_dir / "inpaint").glob("*") if p.is_file()],
          sorted(p.name for p in (plugin.data_dir / "inpaint").glob("*")))

    # 非法请求：缺遮罩 / 尺寸不一致 / 太大
    def inpaint_error(payload):
        try:
            asyncio.run(plugin.inpaint(payload))
            return ""
        except ValueError as e:
            return str(e)

    check("缺遮罩时明确拒绝",
          "遮罩" in inpaint_error({"image": img_data}), True)
    check("原图与遮罩尺寸不一致时拒绝（否则会画歪）",
          "尺寸必须一致" in inpaint_error({"image": img_data, "mask": png_data_url(512, 512)}),
          inpaint_error({"image": img_data, "mask": png_data_url(512, 512)}))
    too_big = png_data_url(3000, 3000)
    check("图片超过像素预算时拒绝并说明上限",
          "图片太大" in inpaint_error({"image": too_big, "mask": too_big}),
          inpaint_error({"image": too_big, "mask": too_big}))

    # Pages 路由：页面点「开始局部重绘」走的就是这条
    inpaint_session(pid="ip-2")
    pages.request._json = {"image": img_data, "mask": mask_data, "prompt": "x"}
    route_ip = asyncio.run(handlers[(f"{base}/inpaint", ("POST",))]())
    check("Pages /inpaint 路由能跑通并把结果回给页面",
          route_ip.get("ok") is True and str(route_ip.get("image", "")).startswith("images/"),
          {k: route_ip.get(k) for k in ("ok", "image", "seed")})
    pages.request._json = {"image": "", "mask": ""}
    bad_ip = asyncio.run(handlers[(f"{base}/inpaint", ("POST",))]())
    check("Pages /inpaint 对不合法请求回 400",
          getattr(bad_ip, "status_code", None) == 400, bad_ip)
    pages.request._json = {}

    print("\n=== 多后端调度（v0.11.0）===")
    from astrbot_plugin_comfyui_smart import backend_pool as bp

    check("解析多后端清单：主后端永远第一，支持「地址|名称」并去重",
          bp.parse_backend_specs(
              ["http://b:8188|二号线", "http://b:8188", "#注释", "c:8188"],
              "http://a:8188",
          ) == [("主", "http://a:8188"), ("二号线", "http://b:8188"), ("后端3", "http://c:8188")],
          bp.parse_backend_specs(["http://b:8188|二号线"], "http://a:8188"))
    check("没有主地址时也能只用清单里的后端",
          bp.parse_backend_specs("http://b:8188", "") == [("后端1", "http://b:8188")],
          bp.parse_backend_specs("http://b:8188", ""))
    check("只把「连不上 / 超时」算后端故障（参数错不该拉黑好机器）",
          bp.is_backend_fault("无法连接 ComfyUI：x")
          and bp.is_backend_fault("出图超时（180 秒）")
          and not bp.is_backend_fault("提交失败：value_not_in_list")
          and not bp.is_backend_fault("本次出图已被 /取消 取消"),
          [bp.is_backend_fault(t) for t in ("无法连接 ComfyUI：x", "提交失败：x", "出图超时（1 秒）")])

    def make_pool(**kw):
        """造一个后端池：用假客户端（只有 queue_status / close）。"""
        class _FakeClient:
            def __init__(self, load, fail=False):
                self.load = load
                self.fail = fail
                self.closed = False
                self.calls = 0

            async def queue_status(self, **kw):
                self.calls += 1
                if self.fail:
                    raise RuntimeError("连不上")
                return api.QueueStatus(
                    total_running=self.load[0], total_pending=self.load[1]
                )

            async def close(self):
                self.closed = True

        clients = {name: _FakeClient(load, fail) for name, load, fail in kw.pop("clients")}
        backends = [bp.Backend(name=n, url=f"http://{n}:8188", client=c)
                    for n, c in clients.items()]
        return bp.BackendPool(backends, **kw), clients

    async def _pick_flow():
        pool, clients = make_pool(clients=[("a", (1, 3), False), ("b", (0, 0), False)])
        chosen = await pool.pick()
        return chosen.name, {n: c.calls for n, c in clients.items()}

    chosen_name, probe_calls = asyncio.run(_pick_flow())
    check("least_queue：把任务派给负载最低的后端（正在跑的权重更高）",
          chosen_name == "b", (chosen_name, probe_calls))
    check("多后端时才探测（每个后端探一次）",
          probe_calls == {"a": 1, "b": 1}, probe_calls)

    async def _single_flow():
        pool, clients = make_pool(clients=[("only", (0, 0), False)])
        chosen = await pool.pick()
        return chosen.name, clients["only"].calls

    single_name, single_calls = asyncio.run(_single_flow())
    check("单后端不做任何探测（行为与没有多后端时完全一致）",
          single_name == "only" and single_calls == 0, (single_name, single_calls))

    async def _weight_flow():
        pool, _clients = make_pool(clients=[("a", (0, 3), False), ("b", (1, 0), False)])
        chosen = await pool.pick()
        return chosen.name
    check("负载权重：一个正在执行（2）比三个排队（3）更该被跳过",
          asyncio.run(_weight_flow()) == "b", asyncio.run(_weight_flow()))

    async def _strategy_flow():
        pool, _ = make_pool(clients=[("a", (0, 0), False), ("b", (0, 0), False)],
                            strategy="round_robin")
        first = (await pool.pick()).name
        second = (await pool.pick()).name
        pool.configure(strategy="primary")
        third = (await pool.pick()).name
        return first, second, third
    rr = asyncio.run(_strategy_flow())
    check("round_robin 会轮流派；切到 primary 后固定用主后端",
          rr[0] != rr[1] and rr[2] == "a", rr)

    async def _bench_flow():
        pool, _clients = make_pool(clients=[("a", (0, 0), False), ("b", (0, 5), True)])
        chosen = await pool.pick()          # b 探测失败 → 熔断
        rows = pool.snapshot()
        benched = [r for r in rows if r["benched"]]
        pool.note_success(pool.backends[1])  # 手动恢复
        return chosen.name, benched, pool.benched(pool.backends[1])
    bench_name, bench_rows, recovered = asyncio.run(_bench_flow())
    check("探测失败的后端会被熔断（并记下原因）",
          bench_name == "a" and bench_rows and bench_rows[0]["name"] == "b"
          and "探测失败" in bench_rows[0]["last_error"], (bench_name, bench_rows))
    check("成功后解除熔断", recovered is False, recovered)

    async def _all_benched_flow():
        pool, _clients = make_pool(clients=[("a", (0, 0), False), ("b", (0, 0), False)])
        pool.note_failure(pool.backends[0], "手动")
        pool.note_failure(pool.backends[1], "手动")
        chosen = await pool.pick()
        return chosen.name
    check("全部熔断时回退到主后端（宁可让主后端报真实错误，也不自己造一个）",
          asyncio.run(_all_benched_flow()) == "a", True)

    async def _snapshot_probe():
        pool, _ = make_pool(clients=[("a", (0, 0), False), ("b", (1, 2), False)])
        await pool.pick()
        return pool.snapshot()
    snap_rows = asyncio.run(_snapshot_probe())
    check("快照字段够 `/状态` 与配置页展示",
          all({"name", "url", "primary", "online", "busy", "benched", "benched_for",
               "last_error", "ok", "fail"} <= set(row) for row in snap_rows) and len(snap_rows) == 2,
          snap_rows)

    print("\n=== 多后端接入出图主流程 ===")
    plugin.config["server"] = {"base_url": "http://a:8188"}
    plugin.config["backends"] = {
        "endpoints": ["http://b:8188|二号线"],
        "strategy": "least_queue",
        "fail_cooldown": 30,
    }
    plugin.reload_components()
    plugin.config["llm_settings"] = {"enable_prompt_optimize": False}
    plugin.config["hires"] = {"enable": False}
    plugin.config["permission"] = {}
    plugin.permission.reload({})

    def two_backend_sessions(pid_a="back-1", pid_b="back-2"):
        """主后端排长队、二号线空闲：任务应当派给二号线。"""
        def build(pid, pending):
            sess = api.aiohttp.ClientSession()
            sess.route("GET", "/models", api.aiohttp.ClientResponse(200, payload=["checkpoints"]))
            sess.route("GET", "/models/checkpoints", api.aiohttp.ClientResponse(
                200, payload=["SDXL/m.safetensors"]))
            sess.route("GET", "/queue", api.aiohttp.ClientResponse(200, payload={
                "queue_running": [["1", "someone-else", {}, {}]] if pending else [],
                "queue_pending": [["1", f"p{i}", {}, {}] for i in range(pending)],
            }))
            sess.route("POST", "/prompt", api.aiohttp.ClientResponse(
                200, payload={"prompt_id": pid}))
            sess.route("GET", f"/history/{pid}", api.aiohttp.ClientResponse(200, payload={pid: {
                "status": {"status_str": "success", "completed": True},
                "outputs": {"7": {"images": [{"filename": "b.png", "type": "output"}]}}}}))
            sess.route("GET", "/view", api.aiohttp.ClientResponse(200, text="PNG"))
            return sess

        primary_sess = build(pid_a, pending=4)
        second_sess = build(pid_b, pending=0)
        plugin.pool.backends[0].client._session = primary_sess
        plugin.pool.backends[1].client._session = second_sess
        for backend in plugin.pool.backends:
            backend.client.invalidate_model_cache()
        return primary_sess, second_sess

    sess_a, sess_b = two_backend_sessions()
    multi_result = asyncio.run(plugin.generate(user_desc="一只猫", opts={}))
    submitted_a = [c for c in sess_a.calls if c[0] == "POST" and c[1] == "/prompt"]
    submitted_b = [c for c in sess_b.calls if c[0] == "POST" and c[1] == "/prompt"]
    check("任务真的被派给了空闲的那台（而不是主后端）",
          not submitted_a and len(submitted_b) == 1,
          {"a": len(submitted_a), "b": len(submitted_b)})
    check("结果里带上了后端名（出图消息会显示）",
          multi_result.get("backend") == "二号线", multi_result.get("backend"))
    check("成功后该后端被标记为可用",
          plugin.pool.backends[1].ok_count >= 1
          and not plugin.pool.benched(plugin.pool.backends[1]),
          plugin.pool.snapshot())

    # 提交失败且属于后端故障 → 熔断
    async def _fault_flow():
        plugin.pool.backends[1].client._session = api.aiohttp.ClientSession()  # 全部 404
        plugin.pool.backends[1].client.invalidate_model_cache()
        try:
            await plugin.generate(user_desc="一只猫", opts={})
        except Exception as e:
            error = str(e)
        else:
            error = ""
        return error, plugin.pool.snapshot()

    fault_error, fault_rows = asyncio.run(_fault_flow())
    check("后端故障时熔断它（队列里不再派给它）",
          plugin.pool.benched(plugin.pool.backends[1]) or "未探测" in fault_error,
          (fault_error[:40], fault_rows))

    # /状态 与 Pages 状态接口暴露后端
    plugin.config["backends"] = {"endpoints": ["http://b:8188|二号线"], "strategy": "least_queue"}
    plugin.reload_components()
    plugin.pool.backends[0].client._session = api.aiohttp.ClientSession()
    plugin.pool.backends[1].client._session = api.aiohttp.ClientSession()
    out_multi = asyncio.run(drive(plugin.cmd_status(AstrMessageEvent(message_str="/状态"))))
    multi_text = "\n".join(x.get("text", "") for x in out_multi)
    check("/状态 在多后端时列出每台后端",
          "二号线" in multi_text and "按负载分配" in multi_text, multi_text[:200])
    status_multi = asyncio.run(handlers[(f"{base}/status", ("GET",))]())
    check("Pages 状态接口带后端快照",
          isinstance(status_multi.get("backends"), list) and len(status_multi["backends"]) == 2,
          status_multi.get("backends"))

    # 复原成单后端，别影响后续用例
    plugin.config["backends"] = {}
    plugin.config["server"] = {"base_url": "127.0.0.1:8188"}
    plugin.reload_components()
    plugin.comfy.invalidate_model_cache()
    check("清空配置后回到单后端（不探测）",
          plugin.pool.multi is False and plugin.pool.primary().url == "http://127.0.0.1:8188",
          [b.url for b in plugin.pool.backends])

    print("\n=== UI 格式工作流自动转换（v0.12.0）===")
    from astrbot_plugin_comfyui_smart import ui_workflow as uw

    def ui_base() -> dict:
        """一份新式界面导出（inputs 里带 widget.name，KSampler 带 control_after_generate）。"""
        return json.loads(json.dumps({
            "last_node_id": 9, "last_link_id": 11, "version": 0.4,
            "nodes": [
                {"id": 4, "type": "CheckpointLoaderSimple", "mode": 0,
                 "outputs": [{"name": "MODEL", "links": [1]}, {"name": "CLIP", "links": [4, 5]},
                             {"name": "VAE", "links": [6]}],
                 "widgets_values": ["sd_xl_base_1.0.safetensors"]},
                {"id": 6, "type": "CLIPTextEncode",
                 "inputs": [{"name": "clip", "type": "CLIP", "link": 4},
                            {"name": "text", "type": "STRING", "widget": {"name": "text"}}],
                 "outputs": [{"name": "CONDITIONING", "links": [7]}], "widgets_values": ["a cat"]},
                {"id": 7, "type": "CLIPTextEncode",
                 "inputs": [{"name": "clip", "type": "CLIP", "link": 5},
                            {"name": "text", "type": "STRING", "widget": {"name": "text"}}],
                 "outputs": [{"name": "CONDITIONING", "links": [8]}], "widgets_values": ["bad hands"]},
                {"id": 5, "type": "EmptyLatentImage",
                 "inputs": [{"name": "width", "type": "INT", "widget": {"name": "width"}},
                            {"name": "height", "type": "INT", "widget": {"name": "height"}},
                            {"name": "batch_size", "type": "INT", "widget": {"name": "batch_size"}}],
                 "outputs": [{"name": "LATENT", "links": [9]}], "widgets_values": [1024, 1024, 1]},
                {"id": 3, "type": "KSampler", "title": "采样器",
                 "inputs": [{"name": "model", "type": "MODEL", "link": 1},
                            {"name": "positive", "type": "CONDITIONING", "link": 7},
                            {"name": "negative", "type": "CONDITIONING", "link": 8},
                            {"name": "latent_image", "type": "LATENT", "link": 9},
                            {"name": "seed", "type": "INT", "widget": {"name": "seed"}},
                            {"name": "control_after_generate", "type": "COMBO",
                             "widget": {"name": "control_after_generate"}},
                            {"name": "steps", "type": "INT", "widget": {"name": "steps"}},
                            {"name": "cfg", "type": "FLOAT", "widget": {"name": "cfg"}},
                            {"name": "sampler_name", "type": "COMBO",
                             "widget": {"name": "sampler_name"}},
                            {"name": "scheduler", "type": "COMBO", "widget": {"name": "scheduler"}},
                            {"name": "denoise", "type": "FLOAT", "widget": {"name": "denoise"}}],
                 "outputs": [{"name": "LATENT", "links": [10]}],
                 "widgets_values": [12345, "randomize", 28, 6.0, "dpmpp_2m", "karras", 1.0]},
                {"id": 8, "type": "VAEDecode",
                 "inputs": [{"name": "samples", "type": "LATENT", "link": 10},
                            {"name": "vae", "type": "VAE", "link": 6}],
                 "outputs": [{"name": "IMAGE", "links": [11]}]},
                {"id": 9, "type": "SaveImage",
                 "inputs": [{"name": "images", "type": "IMAGE", "link": 11},
                            {"name": "filename_prefix", "type": "STRING",
                             "widget": {"name": "filename_prefix"}}],
                 "widgets_values": ["ComfyUI"]},
            ],
            "links": [[1, 4, 0, 3, 0, "MODEL"], [4, 4, 1, 6, 0, "CLIP"], [5, 4, 1, 7, 0, "CLIP"],
                      [6, 4, 2, 8, 1, "VAE"], [7, 6, 0, 3, 1, "CONDITIONING"],
                      [8, 7, 0, 3, 2, "CONDITIONING"], [9, 5, 0, 3, 3, "LATENT"],
                      [10, 3, 0, 8, 0, "LATENT"], [11, 8, 0, 9, 0, "IMAGE"]],
        }))

    check("能认出界面格式（有 nodes 数组）",
          uw.is_ui_workflow(ui_base()) and not uw.is_ui_workflow({"3": {"class_type": "X"}}))

    api_ui = uw.ui_to_api(ui_base())
    ksampler = api_ui["3"]["inputs"]
    check("界面格式转成 API 格式：连线还原成 [节点id, 槽位]",
          ksampler["model"] == ["4", 0] and ksampler["positive"] == ["6", 0]
          and ksampler["latent_image"] == ["5", 0], ksampler)
    check("控件值按名字对齐（control_after_generate 被丢掉、后面的值没有整体错位）",
          ksampler["seed"] == 12345 and ksampler["steps"] == 28 and ksampler["cfg"] == 6.0
          and ksampler["sampler_name"] == "dpmpp_2m" and ksampler["scheduler"] == "karras"
          and ksampler["denoise"] == 1.0, ksampler)
    check("按钮类控件（control_after_generate）绝不写进 API 输入",
          all("control" not in key for key in ksampler), sorted(ksampler))
    check("节点标题被带进 _meta（按标题兜底的注入点仍然有效）",
          api_ui["3"].get("_meta", {}).get("title") == "采样器", api_ui["3"].get("_meta"))
    check("尺寸节点与保存节点的控件也映射正确",
          api_ui["5"]["inputs"] == {"width": 1024, "height": 1024, "batch_size": 1}
          and api_ui["9"]["inputs"]["filename_prefix"] == "ComfyUI",
          (api_ui["5"]["inputs"], api_ui["9"]["inputs"]))

    # Reroute：界面里常用来理线，转换时必须穿过
    reroute = ui_base()
    reroute["nodes"].append({
        "id": 20, "type": "Reroute", "mode": 0,
        "inputs": [{"name": "", "type": "*", "link": 7}],
        "outputs": [{"name": "", "type": "CONDITIONING", "links": [21]}],
    })
    reroute["links"].append([21, 20, 0, 3, 1, "CONDITIONING"])
    for node in reroute["nodes"]:
        if node["id"] == 3:
            node["inputs"][1] = {"name": "positive", "type": "CONDITIONING", "link": 21}
    api_rr = uw.ui_to_api(reroute)
    check("Reroute 被穿过（不会在 API 图里留下 Reroute 节点）",
          "20" not in api_rr and api_rr["3"]["inputs"]["positive"] == ["6", 0],
          (sorted(api_rr), api_rr["3"]["inputs"]["positive"]))

    # 静音 / 旁路
    muted = ui_base()
    for node in muted["nodes"]:
        if node["id"] == 7:
            node["mode"] = 2
    api_muted = uw.ui_to_api(muted)
    check("被静音的节点不会被转进 API 图", "7" not in api_muted, sorted(api_muted))

    # 原语节点：界面里用它给控件喂一个常量
    primitive = ui_base()
    primitive["nodes"].append({
        "id": 30, "type": "PrimitiveNode", "mode": 0,
        "outputs": [{"name": "INT", "links": [31]}], "widgets_values": [999],
    })
    primitive["links"].append([31, 30, 0, 3, 4, "INT"])
    for node in primitive["nodes"]:
        if node["id"] == 3:
            node["inputs"][4] = {"name": "seed", "type": "INT", "widget": {"name": "seed"},
                                 "link": 31}
    api_prim = uw.ui_to_api(primitive)
    check("PrimitiveNode 的值直接写进下游输入（不产生多余节点）",
          "30" not in api_prim and api_prim["3"]["inputs"]["seed"] == 999,
          (sorted(api_prim), api_prim["3"]["inputs"].get("seed")))

    # 老式导出（inputs 里没有 widget 信息）→ 靠 object_info 顺序
    old_style = ui_base()
    for node in old_style["nodes"]:
        node["inputs"] = [i for i in (node.get("inputs") or []) if "widget" not in i]
        if node.get("type") == "KSampler":
            node["widgets_values"] = [777, "fixed", 20, 7.0, "euler", "normal", 0.8]
    object_info = {
        "KSampler": {
            "input": {"required": {
                "model": ["MODEL"], "seed": ["INT", {"default": 0}],
                "steps": ["INT", {"default": 20}], "cfg": ["FLOAT", {"default": 8.0}],
                "sampler_name": [["euler", "dpmpp_2m"]], "scheduler": [["normal", "karras"]],
                "positive": ["CONDITIONING"], "negative": ["CONDITIONING"],
                "latent_image": ["LATENT"], "denoise": ["FLOAT", {"default": 1.0}],
            }},
            "input_order": {"required": ["model", "seed", "steps", "cfg", "sampler_name",
                                         "scheduler", "positive", "negative", "latent_image",
                                         "denoise"]},
        },
        "EmptyLatentImage": {
            "input": {"required": {"width": ["INT", {}], "height": ["INT", {}],
                                   "batch_size": ["INT", {}]}},
            "input_order": {"required": ["width", "height", "batch_size"]},
        },
        "CheckpointLoaderSimple": {
            "input": {"required": {"ckpt_name": [["a.safetensors"]]}},
            "input_order": {"required": ["ckpt_name"]},
        },
        "CLIPTextEncode": {
            "input": {"required": {"text": ["STRING", {}], "clip": ["CLIP"]}},
            "input_order": {"required": ["text", "clip"]},
        },
    }
    api_old = uw.ui_to_api(old_style, object_info=object_info)
    check("老式导出靠 /object_info 的顺序映射（同样跳过 control_after_generate）",
          api_old["3"]["inputs"]["seed"] == 777 and api_old["3"]["inputs"]["steps"] == 20
          and api_old["3"]["inputs"]["denoise"] == 0.8, api_old["3"]["inputs"])
    force_info = {"SomeNode": {"input": {"required": {
        "value": ["STRING", {"forceInput": True}], "count": ["INT", {}],
    }}, "input_order": {"required": ["value", "count"]}}}
    api_force = uw.ui_to_api(
        {"nodes": [{"id": 1, "type": "SomeNode", "widgets_values": [5]}], "links": []},
        object_info=force_info,
    )
    check("forceInput 的输入不算控件（否则控件值会整体错位）",
          api_force["1"]["inputs"] == {"count": 5}, api_force["1"]["inputs"])

    # 没有 object_info 时的内置兜底表
    api_builtin = uw.ui_to_api(old_style)
    check("没有 /object_info 时用内置兜底表也能转（KSampler 等常见节点）",
          api_builtin["3"]["inputs"]["steps"] == 20 and api_builtin["4"]["inputs"]["ckpt_name"]
          == "sd_xl_base_1.0.safetensors", (api_builtin["3"]["inputs"], api_builtin["4"]["inputs"]))

    # 转不动时明确报错
    broken = ui_base()
    for node in broken["nodes"]:
        if node["id"] == 3:
            node["widgets_values"] = [1, 2, 3]
    broken_msg = ""
    try:
        uw.ui_to_api(broken)
    except uw.UIWorkflowError as e:
        broken_msg = str(e)
    check("控件数量对不上时明确报错（说清节点与数量）",
          "控件数量对不上" in broken_msg and "KSampler" in broken_msg, broken_msg)
    not_ui_msg = ""
    try:
        uw.ui_to_api({"3": {"class_type": "KSampler"}})
    except uw.UIWorkflowError as e:
        not_ui_msg = str(e)
    check("不是界面格式时明确拒绝", "不是 ComfyUI 界面格式" in not_ui_msg, not_ui_msg)

    # 走模板引擎：把界面格式丢进模板目录就能用
    ui_dir = Path(tempfile.mkdtemp(prefix="smart_ui_tpl_"))
    wrapped = {"name": "ui_sd15", "arch": "sdxl", "loader": "checkpoint",
               "description": "界面格式自动转换", "graph": ui_base()}
    (ui_dir / "ui_sd15.json").write_text(json.dumps(wrapped, ensure_ascii=False),
                                         encoding="utf-8")
    ui_templates = wt.load_templates(ui_dir)
    check("界面格式的模板文件能被加载并自动转换",
          "ui_sd15" in ui_templates, sorted(ui_templates))
    ui_tpl = ui_templates.get("ui_sd15")
    built_ui = ui_tpl.build(positive="一只猫", negative="bad", model_name="SDXL/m.safetensors",
                            width=832, height=1216, steps=30, cfg=7.0, sampler="euler",
                            scheduler="normal", seed=42)
    check("转换后的模板照样能注入提示词/尺寸/采样参数",
          built_ui["6"]["inputs"]["text"] == "一只猫"
          and built_ui["3"]["inputs"]["steps"] == 30
          and built_ui["3"]["inputs"]["seed"] == 42,
          {k: built_ui["3"]["inputs"][k] for k in ("steps", "cfg", "seed", "sampler_name")})
    check("转换后的模板能通过图结构校验与不可达节点剔除",
          set(built_ui) == {"4", "6", "7", "5", "3", "8", "9"}, sorted(built_ui))

    # 端到端：真的用界面格式模板出一张图
    plugin.user_template_dir.mkdir(parents=True, exist_ok=True)
    (plugin.user_template_dir / "ui_sd15.json").write_text(
        json.dumps(wrapped, ensure_ascii=False), encoding="utf-8"
    )
    plugin._load_templates()
    check("插件加载用户模板时也会自动转换界面格式",
          "ui_sd15" in plugin.templates, sorted(plugin.templates))
    sess_ui = api.aiohttp.ClientSession()
    sess_ui.route("GET", "/models", api.aiohttp.ClientResponse(200, payload=["checkpoints"]))
    sess_ui.route("GET", "/models/checkpoints", api.aiohttp.ClientResponse(
        200, payload=["SDXL/m.safetensors"]))
    sess_ui.route("POST", "/prompt", api.aiohttp.ClientResponse(200, payload={"prompt_id": "ui-1"}))
    sess_ui.route("GET", "/queue", api.aiohttp.ClientResponse(
        200, payload={"queue_running": [], "queue_pending": []}))
    sess_ui.route("GET", "/history/ui-1", api.aiohttp.ClientResponse(200, payload={"ui-1": {
        "status": {"status_str": "success", "completed": True},
        "outputs": {"9": {"images": [{"filename": "ui.png", "type": "output"}]}}}}))
    sess_ui.route("GET", "/view", api.aiohttp.ClientResponse(200, text="PNG"))
    plugin.comfy._session = sess_ui
    plugin.comfy.invalidate_model_cache()
    plugin.templates["ui_sd15"] = ui_templates["ui_sd15"]
    plugin.config["llm_settings"] = {"enable_prompt_optimize": False}
    plugin.config["hires"] = {"enable": False}
    plugin.config["permission"] = {}
    plugin.permission.reload({})
    ui_result = asyncio.run(plugin.generate(user_desc="一只猫", opts={"model": "SDXL"}))
    submitted_ui = None
    for method, path, kw in sess_ui.calls:
        if method == "POST" and path == "/prompt":
            submitted_ui = kw["json"]["prompt"]
    check("界面格式模板能一路跑通出图（转换 → 校验 → 注入 → 提交）",
          ui_result["template"] == "ui_sd15" and len(ui_result["images"]) == 1
          and submitted_ui is not None
          and submitted_ui["3"]["class_type"] == "KSampler",
          (ui_result.get("template"), len(ui_result.get("images") or [])))
    (plugin.user_template_dir / "ui_sd15.json").unlink()
    plugin._load_templates()

    print("\n=== 文生视频 t2v（v0.13.0）===")
    check("帧数按 4n+1 向上对齐（宁可多一帧，不少给）",
          [m.align_video_frames(n) for n in (1, 4, 5, 49, 50, 80, 81, 100)]
          == [5, 5, 5, 49, 53, 81, 81, 101]
          and all((m.align_video_frames(n) - 1) % 4 == 0 for n in (1, 50, 80, 100)),
          [m.align_video_frames(n) for n in (1, 50, 80)])
    check("时长×帧率换算成帧数（5 秒 16fps → 81 帧）",
          m.resolve_video_params({"seconds": "5", "fps": "16"}, {})["length"] == 81,
          m.resolve_video_params({"seconds": "5", "fps": "16"}, {}))
    check("也可以直接给 --length（并对齐）",
          m.resolve_video_params({"length": "50"}, {"default_fps": 16})["length"] == 53,
          m.resolve_video_params({"length": "50"}, {"default_fps": 16}))
    check("超过配置的时长上限会被截断并标记",
          m.resolve_video_params({"seconds": "60"}, {"max_seconds": 10})["seconds"] == 9.81
          and m.resolve_video_params({"seconds": "60"}, {"max_seconds": 10})["clamped"] is True,
          m.resolve_video_params({"seconds": "60"}, {"max_seconds": 10}))

    # 产物类型判定（ComfyUI 把视频也放在 outputs 的 images 字段里）
    check("按扩展名区分视频与图片",
          api.media_kind("astrbot_video_00001_.webm") == "video"
          and api.media_kind("a/b.mp4") == "video"
          and api.media_kind("out.png") == "image"
          and api.media_kind("anim.gif") == "image",
          [api.media_kind(x) for x in ("x.webm", "x.mp4", "x.png", "x.gif")])
    mixed = api._collect_output_images({
        "10": {"images": [{"filename": "v.webm", "subfolder": "", "type": "output"},
                          {"filename": "temp.png", "type": "temp"}]},
        "11": {"images": [{"filename": "i.png", "subfolder": "", "type": "output"}]},
        "12": {"gifs": [{"filename": "old.gif", "subfolder": "", "type": "output"}]},
    })
    check("产物收集同时覆盖 images / gifs，并打上 media 标记",
          [(item["filename"], item["media"]) for item in mixed]
          == [("v.webm", "video"), ("i.png", "image"), ("old.gif", "image")],
          [(item["filename"], item["media"]) for item in mixed])
    check("预览类产物（type=temp）不会被当成结果",
          all("temp.png" != item["filename"] for item in mixed), mixed)

    tpl_video = wt.load_templates(ROOT / "workflows")["wan_t2v"]
    check("内置文生视频模板已加载，用途是 t2v", tpl_video.purpose == "t2v", tpl_video.purpose)
    check("视频模板用 Wan 原生节点（含 CreateVideo + SaveVideo 输出锚点）",
          {"UNETLoader", "CLIPLoader", "VAELoader", "EmptyHunyuanLatentVideo",
           "ModelSamplingSD3", "CreateVideo", "SaveVideo"} <= tpl_video.required_nodes(),
          sorted(tpl_video.required_nodes()))
    check("视频输出节点被当成「产物落地」的锚点（否则不可达节点会被误删）",
          tpl_video.graph[tpl_video.bindings["save"]]["class_type"] == "SaveVideo"
          and tpl_video.graph[tpl_video.graph[tpl_video.bindings["save"]]["inputs"]["video"][0]]["class_type"]
          == "CreateVideo",
          tpl_video.bindings["save"])
    built_video = tpl_video.build(
        positive="一只猫在草地上奔跑", negative="bad", model_name="wan2.1_t2v_1.3B_fp16.safetensors",
        width=832, height=480, steps=30, cfg=6.0, sampler="uni_pc", scheduler="simple", seed=9,
        params={"length": 81, "fps": 16},
    )
    check("帧数与帧率写进模板声明的参数（潜空间 + 保存节点）",
          built_video["6"]["inputs"]["length"] == 81
          and built_video[built_video["12"]["inputs"]["video"][0]]["inputs"]["fps"] == 16.0,
          (built_video["6"]["inputs"],
           built_video[built_video["12"]["inputs"]["video"][0]]["inputs"]))
    check("尺寸注入到视频潜空间节点", built_video["6"]["inputs"]["width"] == 832
          and built_video["6"]["inputs"]["height"] == 480, built_video["6"]["inputs"])
    check("Wan 档案参与采样参数（832x480 / uni_pc / simple / CFG 6）",
          wt.guess_arch("wan2.1_t2v_1.3B_fp16.safetensors") == "wan"
          and wt.arch_profile("wan")["sampler"] == "uni_pc"
          and wt.arch_profile("wan")["size"] == (832, 480),
          (wt.guess_arch("wan2.1_t2v_1.3B_fp16.safetensors"), wt.arch_profile("wan")["size"]))
    check("没有视频模板时明确失败，而不是退回文生图模板",
          wt.pick_template({"sd_checkpoint": plugin.templates["sd_checkpoint"]},
                           model_name="wan2.1_t2v.safetensors",
                           model_folder="diffusion_models", purpose="t2v")[0] is None, True)

    def video_session(pid="vid-1", filename="astrbot_video_00001_.webm"):
        sess = api.aiohttp.ClientSession()
        sess.route("GET", "/models", api.aiohttp.ClientResponse(200, payload=["diffusion_models"]))
        sess.route("GET", "/models/diffusion_models", api.aiohttp.ClientResponse(
            200, payload=["wan2.1_t2v_1.3B_fp16.safetensors"]))
        sess.route("POST", "/prompt", api.aiohttp.ClientResponse(200, payload={"prompt_id": pid}))
        sess.route("GET", "/queue", api.aiohttp.ClientResponse(
            200, payload={"queue_running": [], "queue_pending": []}))
        sess.route("GET", f"/history/{pid}", api.aiohttp.ClientResponse(200, payload={pid: {
            "status": {"status_str": "success", "completed": True},
            "outputs": {"10": {"images": [{"filename": filename, "subfolder": "",
                                           "type": "output"}], "animated": [True]}}}}))
        sess.route("GET", "/view", api.aiohttp.ClientResponse(200, text="WEBMDATA"))
        plugin.comfy._session = sess
        plugin.comfy.invalidate_model_cache()
        return sess

    plugin.config["llm_settings"] = {"enable_prompt_optimize": False}
    plugin.config["hires"] = {"enable": False}
    plugin.config["permission"] = {}
    plugin.config["video"] = {"default_seconds": 4, "default_fps": 16, "max_seconds": 10,
                              "send_video": True}
    plugin.permission.reload({})

    sess_vid = video_session()
    ev_vid = AstrMessageEvent(sender_id="9009", message_str="/视频 一只猫在草地上奔跑 --seconds 5 --fps 16")
    out_vid = asyncio.run(drive(plugin.cmd_video(ev_vid)))
    submitted_vid = None
    for method, path, kw in sess_vid.calls:
        if method == "POST" and path == "/prompt":
            submitted_vid = kw["json"]["prompt"]
    check("/视频 提交的是 Wan 文生视频工作流",
          submitted_vid is not None
          and submitted_vid["12"]["class_type"] == "SaveVideo"
          and submitted_vid["11"]["class_type"] == "CreateVideo"
          and submitted_vid["6"]["class_type"] == "EmptyHunyuanLatentVideo",
          sorted({n["class_type"] for n in (submitted_vid or {}).values()}))
    _vid_fps_node = (submitted_vid or {}).get("12", {}).get("inputs", {}).get("video", [None])[0]
    check("--seconds/--fps 落到潜空间帧数与 CreateVideo 帧率上",
          (submitted_vid or {}).get("6", {}).get("inputs", {}).get("length") == 81
          and (submitted_vid or {}).get(_vid_fps_node, {}).get("inputs", {}).get("fps") == 16.0,
          ((submitted_vid or {}).get("6", {}).get("inputs"),
           (submitted_vid or {}).get(_vid_fps_node, {}).get("inputs")))
    chain_vid = [x for x in out_vid if x.get("type") == "chain"][-1]["chain"]
    check("视频产物用 AstrBot 的 Video 组件发出（不是硬塞进 Image）",
          any(comp.__class__.__name__ == "Video" for comp in chain_vid),
          [comp.__class__.__name__ for comp in chain_vid])
    check("视频结果里带上时长/帧数/帧率，便于复现",
          any("81 帧" in getattr(comp, "text", "") for comp in chain_vid),
          [getattr(comp, "text", "")[:40] for comp in chain_vid])

    # 关掉「直接发视频」后只给路径
    plugin.config["video"] = {"default_seconds": 4, "default_fps": 16, "max_seconds": 10,
                              "send_video": False}
    video_session(pid="vid-2")
    out_vid2 = asyncio.run(drive(plugin.cmd_video(
        AstrMessageEvent(sender_id="9009", message_str="/视频 一只猫 --seconds 2"))))
    chain_vid2 = [x for x in out_vid2 if x.get("type") == "chain"][-1]["chain"]
    check("关掉后不发 Video 组件，改成给出本地路径",
          not any(comp.__class__.__name__ == "Video" for comp in chain_vid2)
          and any("已保存到" in getattr(comp, "text", "") for comp in chain_vid2),
          [getattr(comp, "text", "")[:50] for comp in chain_vid2])

    plugin.config["video"] = {"default_seconds": 4, "default_fps": 16, "max_seconds": 10,
                              "send_video": True}
    out_vid3 = asyncio.run(drive(plugin.cmd_video(
        AstrMessageEvent(sender_id="9009", message_str="/视频"))))
    check("/视频 没给描述时给出用法与参数说明",
          "用法" in out_vid3[0]["text"] and "--seconds" in out_vid3[0]["text"],
          out_vid3[0]["text"][:60])
    video_session(pid="vid-4")
    out_vid4 = asyncio.run(drive(plugin.cmd_video(
        AstrMessageEvent(sender_id="9009", message_str="/视频 一只猫 --seconds abc"))))
    check("时长参数非法时明确报错", "需要一个数字" in out_vid4[-1]["text"], out_vid4[-1]["text"][:60])

    # 尺寸/帧数上限：超出配置上限会截断并在 LOG 里说明
    video_session(pid="vid-5")
    asyncio.run(drive(plugin.cmd_video(
        AstrMessageEvent(sender_id="9009", message_str="/视频 一只猫 --seconds 60"))))
    from astrbot.api import logger as _stub_logger
    clamped_lines = [line for line in _stub_logger.text().splitlines() if "截到" in line]
    check("超长视频被配置上限截断（日志里说明）", bool(clamped_lines), clamped_lines[:1])

    print("\n=== 图生视频 i2v / 首尾帧（v0.14.0）===")
    tpl_i2v = wt.load_templates(ROOT / "workflows")["wan_i2v"]
    check("内置图生视频模板已加载，用途是 i2v", tpl_i2v.purpose == "i2v", tpl_i2v.purpose)
    check("i2v 模板用 Wan 首尾帧节点（可选 start/end image）",
          {"WanFirstLastFrameToVideo", "UNETLoader", "CLIPLoader", "VAELoader",
           "ModelSamplingSD3", "CreateVideo", "SaveVideo"} <= tpl_i2v.required_nodes(),
          sorted(tpl_i2v.required_nodes()))
    check("首帧与尾帧绑定到两个不同的 LoadImage（输入键都叫 image，必须分开）",
          tpl_i2v.bindings["image_loader"] == ("4", "image")
          and tpl_i2v.bindings["end_image_loader"] == ("7", "image"),
          (tpl_i2v.bindings["image_loader"], tpl_i2v.bindings["end_image_loader"]))

    tpl_i2v_save = tpl_i2v.graph[tpl_i2v.bindings["save"]]
    built_i2v_fps_node = tpl_i2v_save["inputs"]["video"][0]
    i2v_both = tpl_i2v.build(
        positive="p", negative="n", model_name="wan2.1_i2v_480p_14B.safetensors",
        width=832, height=480, steps=30, cfg=6.0, sampler="uni_pc", scheduler="simple",
        seed=3, image_name="astrbot/start.png", end_image_name="astrbot/end.png",
        params={"length": 81, "fps": 16},
    )
    check("有尾帧：两张图分别写进两个 LoadImage，首尾帧节点拿到两条连线",
          i2v_both["4"]["inputs"]["image"] == "astrbot/start.png"
          and i2v_both["7"]["inputs"]["image"] == "astrbot/end.png"
          and i2v_both["5"]["inputs"]["start_image"] == ["4", 0]
          and i2v_both["5"]["inputs"]["end_image"] == ["7", 0],
          (i2v_both["5"]["inputs"]["start_image"], i2v_both["5"]["inputs"]["end_image"]))
    check("帧数/帧率写进模板声明的参数",
          i2v_both["5"]["inputs"]["length"] == 81
          and i2v_both[built_i2v_fps_node]["inputs"]["fps"] == 16.0,
          (i2v_both["5"]["inputs"]["length"], i2v_both[built_i2v_fps_node]["inputs"]["fps"]))

    i2v_one = tpl_i2v.build(
        positive="p", negative="n", model_name="m", width=832, height=480, steps=30, cfg=6.0,
        sampler="uni_pc", scheduler="simple", seed=3, image_name="astrbot/start.png",
        params={"length": 49, "fps": 16, "end_image": ""},
    )
    check("只给首帧：可选输入 end_image 被摘掉（不留死链）",
          "end_image" not in i2v_one["5"]["inputs"], i2v_one["5"]["inputs"])
    check("只给首帧：没用到的第二张 LoadImage 会被剪掉（否则占位文件名会让整张图被拒）",
          "7" not in i2v_one and "4" in i2v_one and i2v_one["5"]["inputs"]["start_image"] == ["4", 0],
          sorted(i2v_one))
    check("params 空串=删除输入的语义，不会误删字符串控件（提示词仍在）",
          i2v_one["6"]["inputs"]["text"] == "p" and i2v_one["5"]["inputs"]["length"] == 49,
          i2v_one["6"]["inputs"])

    check("视频输入图尺寸按 16 的倍数贴合、并压到像素预算内",
          m.fit_video_size(640, 960) == (512, 768)
          and m.fit_video_size(1920, 1080) == (832, 464)
          and m.fit_video_size(832, 480) == (832, 480)
          and all(v % 16 == 0 for v in m.fit_video_size(1000, 700)),
          [m.fit_video_size(*wh) for wh in ((640, 960), (1920, 1080), (1000, 700))])

    def i2v_session(pid="i2v-1"):
        sess = api.aiohttp.ClientSession()
        sess.route("GET", "/models", api.aiohttp.ClientResponse(200, payload=["diffusion_models"]))
        sess.route("GET", "/models/diffusion_models", api.aiohttp.ClientResponse(
            200, payload=["wan2.1_i2v_480p_14B_fp8_e4m3fn.safetensors"]))
        sess.route("POST", "/upload/image", api.aiohttp.ClientResponse(
            200, payload={"name": "in.png", "subfolder": "astrbot", "type": "input"}))
        sess.route("POST", "/prompt", api.aiohttp.ClientResponse(200, payload={"prompt_id": pid}))
        sess.route("GET", "/queue", api.aiohttp.ClientResponse(
            200, payload={"queue_running": [], "queue_pending": []}))
        sess.route("GET", f"/history/{pid}", api.aiohttp.ClientResponse(200, payload={pid: {
            "status": {"status_str": "success", "completed": True},
            "outputs": {"12": {"images": [{"filename": "i2v_00001_.webm", "subfolder": "",
                                           "type": "output"}], "animated": [True]}}}}))
        sess.route("GET", "/view", api.aiohttp.ClientResponse(200, text="WEBMDATA"))
        plugin.comfy._session = sess
        plugin.comfy.invalidate_model_cache()
        return sess

    plugin.config["llm_settings"] = {"enable_prompt_optimize": False}
    plugin.config["hires"] = {"enable": False}
    plugin.config["permission"] = {}
    plugin.config["video"] = {"default_seconds": 4, "default_fps": 16, "max_seconds": 10,
                              "send_video": True}
    plugin.permission.reload({})

    # 单张图：以首帧为起点
    sess_i2v = i2v_session(pid="i2v-1")
    ev_i2v = AstrMessageEvent(sender_id="6006", message_str="/图生视频 让她的头发飘动 --seconds 3",
                              message=[StubImage(str(png_path))])
    out_i2v = asyncio.run(drive(plugin.cmd_image_to_video(ev_i2v)))
    submitted_i2v = None
    for method, path, kw in sess_i2v.calls:
        if method == "POST" and path == "/prompt":
            submitted_i2v = kw["json"]["prompt"]
    check("/图生视频 提交的是 Wan 首尾帧工作流",
          submitted_i2v is not None
          and submitted_i2v["5"]["class_type"] == "WanFirstLastFrameToVideo"
          and submitted_i2v["14"]["class_type"] == "SaveVideo",
          sorted({n["class_type"] for n in (submitted_i2v or {}).values()}))
    check("单张图时不带尾帧（可选输入已被摘掉、孤儿节点已剪掉）",
          submitted_i2v is not None and "end_image" not in submitted_i2v["5"]["inputs"]
          and "7" not in submitted_i2v, sorted(submitted_i2v or {}))
    check("尺寸贴合输入图（640x960 → 512x768，16 的倍数）",
          submitted_i2v["5"]["inputs"]["width"] == 512
          and submitted_i2v["5"]["inputs"]["height"] == 768, submitted_i2v["5"]["inputs"])
    # v0.15.4 起：Wan 系列没写 --fps 时按原生 24fps 换算（3 秒 → 73 帧），观感才对
    check("秒数换算成帧数写进模板（3 秒 → 原生 24fps 的 73 帧）",
          submitted_i2v["5"]["inputs"]["length"] == 73, submitted_i2v["5"]["inputs"]["length"])
    chain_i2v = [x for x in out_i2v if x.get("type") == "chain"][-1]["chain"]
    check("视频用 Video 组件发出，并说明是「以首帧为起点」",
          any(comp.__class__.__name__ == "Video" for comp in chain_i2v)
          and any("以首帧为起点" in getattr(comp, "text", "") for comp in chain_i2v),
          [getattr(comp, "text", "")[:30] for comp in chain_i2v])

    # 两张图：首帧 → 尾帧（必须是两个不同的文件：同一路径会被 _collect_images 去重）
    end_frame_path = Path(tempfile.mkdtemp(prefix="smart_i2v_end_")) / "end.png"
    end_frame_path.write_bytes(png_path.read_bytes())
    sess_i2v2 = i2v_session(pid="i2v-2")
    ev_flf = AstrMessageEvent(sender_id="6006", message_str="/图生视频 从白天过渡到夜晚 --seconds 2",
                              message=[StubImage(str(png_path)),
                                       StubImage(str(end_frame_path))])
    out_flf = asyncio.run(drive(plugin.cmd_image_to_video(ev_flf)))
    submitted_flf = None
    for method, path, kw in sess_i2v2.calls:
        if method == "POST" and path == "/prompt":
            submitted_flf = kw["json"]["prompt"]
    uploads = [c for c in sess_i2v2.calls if c[0] == "POST" and c[1] == "/upload/image"]
    check("两张图时走「首帧 → 尾帧」（两张图都上传、两条连线都在）",
          len(uploads) == 2 and submitted_flf is not None
          and submitted_flf["5"]["inputs"].get("start_image") == ["4", 0]
          and submitted_flf["5"]["inputs"].get("end_image") == ["7", 0]
          and "7" in submitted_flf,
          (len(uploads), submitted_flf["5"]["inputs"] if submitted_flf else None))
    chain_flf = [x for x in out_flf if x.get("type") == "chain"][-1]["chain"]
    check("首尾帧模式在结果里说明清楚",
          any("首帧 → 尾帧" in getattr(comp, "text", "") for comp in chain_flf),
          [getattr(comp, "text", "")[:30] for comp in chain_flf])

    # 用法与报错
    out_i2v_none = asyncio.run(drive(plugin.cmd_image_to_video(
        AstrMessageEvent(sender_id="6006", message_str="/图生视频 动起来"))))
    check("/图生视频 没给图时给出用法",
          "用法" in out_i2v_none[0]["text"] and "首帧" in out_i2v_none[0]["text"],
          out_i2v_none[0]["text"][:60])
    saved_i2v_tpl = plugin.templates.pop("wan_i2v")
    i2v_session(pid="i2v-3")
    out_i2v_missing = asyncio.run(drive(plugin.cmd_image_to_video(
        AstrMessageEvent(sender_id="6006", message_str="/图生视频 动起来",
                         message=[StubImage(str(png_path))]))))
    plugin.templates["wan_i2v"] = saved_i2v_tpl
    check("缺图生视频模板时报错并指出该放哪个文件",
          "wan_i2v.json" in out_i2v_missing[-1]["text"], out_i2v_missing[-1]["text"][:80])

    print("\n=== 国际化 i18n（v0.15.0）===")
    from astrbot_plugin_comfyui_smart import i18n as i18n_mod
    from astrbot_plugin_comfyui_smart import llm_service as _llm
    from astrbot_plugin_comfyui_smart import permission as _perm

    catalog = i18n_mod.load_translations(i18n_mod.i18n_dir(ROOT))
    check("按官方约定读到 .astrbot-plugin/i18n 下的文案",
          {"zh-CN", "en-US"} <= set(catalog), sorted(catalog))
    check("每种语言的键完全一致（少一条就是漏翻译）",
          set(catalog["zh-CN"]) == set(catalog["en-US"]),
          sorted(set(catalog["zh-CN"]) ^ set(catalog["en-US"]))[:5])
    check("中英文案确实不同（不是复制粘贴糊弄）",
          catalog["zh-CN"]["perm.whitelist"] != catalog["en-US"]["perm.whitelist"]
          and any("\u4e00" <= ch <= "\u9fff" for ch in catalog["zh-CN"]["perm.whitelist"])
          and not any("\u4e00" <= ch <= "\u9fff" for ch in catalog["en-US"]["perm.whitelist"]),
          (catalog["zh-CN"]["perm.whitelist"], catalog["en-US"]["perm.whitelist"]))

    # 源码里用到的键必须两个语言都有（这份守卫防止「改了代码忘了加文案」）
    used_keys: set[str] = set()
    for rel, text in (
        ("main.py", _src("main.py")),
        ("permission.py", _src("permission.py")),
        ("llm_service.py", _src("llm_service.py")),
        ("pages/__init__.py", _src("pages/__init__.py")),
    ):
        for match in _re.finditer(r'["\']((?:cmd|common|queue|result|error|perm|llm|ui)\.[a-z0-9_.]+)["\']', text):
            used_keys.add(match.group(1))
    missing_keys = sorted(k for k in used_keys if k not in catalog["zh-CN"] or k not in catalog["en-US"])
    check("源码里引用到的文案键在两个语言里都存在",
          used_keys and not missing_keys, missing_keys or f"{len(used_keys)} 个键全部有文案")
    check("文案键有分层前缀（cmd./queue./result./error./perm./llm./ui.）",
          all(k.split(".")[0] in {"cmd", "common", "queue", "result", "error", "perm", "llm", "ui"}
              for k in used_keys),
          sorted({k.split(".")[0] for k in used_keys}))

    # LLM 提示词：中文文案必须与代码里的常量一致（防止两边漂移）
    check("LLM 系统提示词的中文文案与代码常量一致",
          catalog["zh-CN"]["llm.optimize_system"] == _llm.PROMPT_SYSTEM
          and catalog["zh-CN"]["llm.reverse_system"] == _llm.REVERSE_SYSTEM,
          (len(catalog["zh-CN"]["llm.optimize_system"]), len(_llm.PROMPT_SYSTEM)))
    check("英文提示词也要求只输出 JSON（换语言不能丢格式约束）",
          "JSON" in catalog["en-US"]["llm.optimize_system"]
          and "JSON" in catalog["en-US"]["llm.reverse_system"], True)

    # Translator 行为
    tr_zh = i18n_mod.Translator(catalog, locale="zh-CN")
    tr_en = i18n_mod.Translator(catalog, locale="en-US")
    check("按选定语言取文案", tr_en("perm.blacklist") == catalog["en-US"]["perm.blacklist"],
          tr_en("perm.blacklist"))
    check("格式化参数生效（带 {n} 的文案）",
          tr_zh("perm.cooldown", seconds=12) == "⏱️ 冷却中，请 12 秒后再试",
          tr_zh("perm.cooldown", seconds=12))
    check("格式化参数缺失时不抛错（原样返回）",
          "{seconds}" in tr_zh("perm.cooldown"), tr_zh("perm.cooldown"))
    check("未知键返回键名本身（最坏情况也不会崩）",
          tr_zh("nope.missing.key") == "nope.missing.key", tr_zh("nope.missing.key"))
    check("选不到语言时退回默认语言",
          i18n_mod.Translator(catalog, locale="fr-FR").locale == "zh-CN"
          and i18n_mod.Translator(catalog, locale="").locale == "zh-CN",
          i18n_mod.Translator(catalog, locale="fr-FR").locale)
    check("语言代码容错：zh / EN / en_US 都能认",
          [i18n_mod.Translator(catalog, locale=x).locale
           for x in ("zh", "EN", "en_US", "en-us")] == ["zh-CN", "en-US", "en-US", "en-US"],
          [i18n_mod.Translator(catalog, locale=x).locale for x in ("zh", "EN", "en_us")])
    check("Translator 可以直接当函数用（传给别的组件）",
          callable(tr_en) and tr_en("perm.daily_limit", limit=3)
          == catalog["en-US"]["perm.daily_limit"].format(limit=3), tr_en("perm.daily_limit", limit=3))
    check("插件页文案只挑 ui. 前缀那部分",
          set(tr_en.ui_strings()) and all(k.startswith("ui.") for k in tr_en.ui_strings()),
          sorted(tr_en.ui_strings())[:3])

    # 英文模式下：权限提示与 LLM 提示词都跟着换
    en_perm = _perm.PermissionManager({"whitelist_user_ids": ["a"]}, translate=tr_en)
    ok_en, why_en = asyncio.run(en_perm.check("b", is_admin=False, storage=plugin.storage))
    check("英文模式下权限提示是英文",
          not ok_en and why_en == catalog["en-US"]["perm.whitelist"], why_en)
    llm_en = _llm.LLMService(ctx, plugin.config, translate=tr_en)
    check("英文模式下 LLM 用的是英文系统提示词",
          llm_en._t("llm.optimize_system") == catalog["en-US"]["llm.optimize_system"]
          and "prompt engineer" in llm_en._t("llm.optimize_system"),
          llm_en._t("llm.optimize_system")[:40])
    check("没注入翻译器时权限/LLM 也不会返回键名（默认中文文案兜底）",
          "白名单" in _perm.PermissionManager({"whitelist_user_ids": ["a"]})
          ._t("perm.whitelist")
          and _llm.LLMService(ctx, plugin.config)._t("llm.optimize_system") != "llm.optimize_system",
          True)

    # 端到端：把插件切到英文，聊天文案真的变英文
    plugin.config["general"] = {"language": "en-US"}
    plugin._configure_i18n()
    check("切到 en-US 后插件翻译器生效", plugin.t.locale == "en-US", plugin.t.locale)
    out_en = asyncio.run(drive(plugin.cmd_draw(AstrMessageEvent(message_str="/画图"))))
    check("英文模式下 /画图 的用法提示是英文",
          "Usage:" in out_en[0]["text"] and "🎨" in out_en[0]["text"], out_en[0]["text"][:60])
    out_en_help = asyncio.run(drive(plugin.cmd_help(AstrMessageEvent(message_str="/帮助"))))
    check("英文模式下 /帮助 整体是英文",
          "Smart Drawing" in out_en_help[0]["text"] and "用法" not in out_en_help[0]["text"],
          out_en_help[0]["text"][:60])
    i18n_resp = asyncio.run(handlers[(f"{base}/i18n", ("GET",))]())
    check("Pages /i18n 接口返回当前语言与页面文案",
          i18n_resp.get("locale") == "en-US"
          and i18n_resp.get("strings", {}).get("ui.nav.server") == "Server",
          {k: i18n_resp.get(k) for k in ("locale", "available")})
    plugin.config["general"] = {"language": "zh-CN"}
    plugin._configure_i18n()
    check("切回中文后恢复", plugin.t.locale == "zh-CN"
          and plugin.t("ui.nav.server") == "服务器", plugin.t("ui.nav.server"))

    print("\n=== 真机实测发现的两个修复（v0.15.1）===")
    # 1) 局部重绘默认 denoise 必须整段重画：不能被 i2i.denoise（0.6）顶掉
    plugin.config["i2i"] = {"enable": True, "denoise": 0.6, "max_side": 1536, "subfolder": "astrbot"}
    plugin.config["llm_settings"] = {"enable_prompt_optimize": False}
    plugin.config["hires"] = {"enable": False}
    plugin.config["permission"] = {}
    plugin.permission.reload({})

    def inpaint_denoise(opts=None):
        sess = api.aiohttp.ClientSession()
        sess.route("GET", "/models", api.aiohttp.ClientResponse(200, payload=["checkpoints"]))
        sess.route("GET", "/models/checkpoints", api.aiohttp.ClientResponse(
            200, payload=["SDXL/m.safetensors"]))
        sess.route("POST", "/upload/image", api.aiohttp.ClientResponse(
            200, payload={"name": "in.png", "subfolder": "astrbot", "type": "input"}))
        sess.route("POST", "/prompt", api.aiohttp.ClientResponse(200, payload={"prompt_id": "dn-1"}))
        sess.route("GET", "/queue", api.aiohttp.ClientResponse(
            200, payload={"queue_running": [], "queue_pending": []}))
        sess.route("GET", "/history/dn-1", api.aiohttp.ClientResponse(200, payload={"dn-1": {
            "status": {"status_str": "success", "completed": True},
            "outputs": {"11": {"images": [{"filename": "x.png", "type": "output"}]}}}}))
        sess.route("GET", "/view", api.aiohttp.ClientResponse(200, text="PNG"))
        plugin.comfy._session = sess
        plugin.comfy.invalidate_model_cache()
        asyncio.run(plugin.generate(
            user_desc="改成红裙子", opts=opts or {}, event=None,
            source_image=str(png_path), mask_image=str(png_path), force_purpose="inpaint",
        ))
        for method, path, kw in sess.calls:
            if method == "POST" and path == "/prompt":
                return kw["json"]["prompt"]["9"]["inputs"]["denoise"]
        return None

    check("局部重绘默认整段重画（denoise=1.0，不再被 i2i.denoise 顶成 0.6）",
          inpaint_denoise() == 1.0, inpaint_denoise())
    check("显式给了 --denoise 仍然尊重（想保守重绘也行）",
          inpaint_denoise({"denoise": "0.7"}) == 0.7, inpaint_denoise({"denoise": "0.7"}))
    check("图生图仍然沿用配置里的重绘幅度（没被这次改动带跑）",
          asyncio.run(plugin.generate(
              user_desc="改成冬天", opts={}, event=None, source_image=str(png_path)
          )) is not None, True)

    # 2) 没有视频权重时：直接说「缺视频底模」，而不是把 checkpoint 塞进 UNETLoader
    only_images = {"sd_checkpoint": plugin.templates["sd_checkpoint"],
                   "img2img_checkpoint": plugin.templates["img2img_checkpoint"]}
    picked_t2v, _arch = wt.pick_template(
        only_images, model_name="3Guofeng3_v34.safetensors", model_folder="checkpoints",
        purpose="t2v",
    )
    picked_i2v, _arch2 = wt.pick_template(
        only_images, model_name="3Guofeng3_v34.safetensors", model_folder="checkpoints",
        purpose="i2v",
    )
    check("视频用途不再把 checkpoint 硬套进「分离权重」模板（否则报错看不懂）",
          picked_t2v is None and picked_i2v is None, (picked_t2v, picked_i2v))

    def t2v_error():
        sess = api.aiohttp.ClientSession()
        sess.route("GET", "/models", api.aiohttp.ClientResponse(200, payload=["checkpoints"]))
        sess.route("GET", "/models/checkpoints", api.aiohttp.ClientResponse(
            200, payload=["3Guofeng3_v34.safetensors"]))
        sess.route("GET", "/queue", api.aiohttp.ClientResponse(
            200, payload={"queue_running": [], "queue_pending": []}))
        plugin.comfy._session = sess
        plugin.comfy.invalidate_model_cache()
        saved = dict(plugin.templates)
        plugin.templates.pop("wan_t2v", None)   # 模拟这台机器没有可用的视频模板
        try:
            asyncio.run(plugin.generate(user_desc="a cat", opts={}, event=None,
                                        force_purpose="t2v"))
            return ""
        except Exception as e:
            return str(e)
        finally:
            plugin.templates.clear()
            plugin.templates.update(saved)

    msg = t2v_error()
    # v0.15.2 起这条护栏更靠前（先判断底模像不像视频模型），措辞随之更新
    check("没装视频权重时给出可操作的报错（说清要放进 diffusion_models / unet_gguf）",
          "视频权重" in msg and ("diffusion_models" in msg or "unet_gguf" in msg), msg[:150])

    print("\n=== GGUF 量化 + Wan 2.2 TI2V-5B（v0.15.2）===")
    # 目录发现：GGUF 的专用目录必须进模型清单（8G 显存靠它跑视频）
    s_gguf = api.aiohttp.ClientSession()
    s_gguf.route("GET", "/models", api.aiohttp.ClientResponse(200, payload=[
        "checkpoints", "diffusion_models", "unet_gguf", "clip_gguf", "vae", "text_encoders",
        "custom_nodes", "configs",
    ]))
    s_gguf.route("GET", "/models/checkpoints", api.aiohttp.ClientResponse(
        200, payload=["sd15.safetensors"]))
    s_gguf.route("GET", "/models/diffusion_models", api.aiohttp.ClientResponse(
        200, payload=["wan2.1_t2v_1.3B_fp16.safetensors"]))
    s_gguf.route("GET", "/models/unet_gguf", api.aiohttp.ClientResponse(
        200, payload=["Wan2.2-TI2V-5B-Q4_K_M.gguf"]))
    s_gguf.route("GET", "/models/clip_gguf", api.aiohttp.ClientResponse(
        200, payload=["umt5-xxl-encoder-Q4_K_M.gguf"]))
    s_gguf.route("GET", "/models/vae", api.aiohttp.ClientResponse(
        200, payload=["Wan2.2_VAE.safetensors"]))
    s_gguf.route("GET", "/models/text_encoders", api.aiohttp.ClientResponse(200, payload=[]))
    plugin.comfy._session = s_gguf
    plugin.comfy.invalidate_model_cache()
    gguf_catalog = asyncio.run(plugin.get_catalog())
    check("GGUF 专用目录进入模型清单（unet_gguf / clip_gguf）",
          gguf_catalog.get("unet_gguf") == ["Wan2.2-TI2V-5B-Q4_K_M.gguf"]
          and gguf_catalog.get("clip_gguf") == ["umt5-xxl-encoder-Q4_K_M.gguf"],
          {k: v for k, v in gguf_catalog.items()})
    check("非模型目录仍被排除（custom_nodes / configs 不进清单）",
          "custom_nodes" not in gguf_catalog and "configs" not in gguf_catalog,
          sorted(gguf_catalog))

    tpl_gguf_t2v = plugin.templates["wan22_t2v_gguf"]
    tpl_gguf_i2v = plugin.templates["wan22_i2v_gguf"]
    check("内置 GGUF 模板已加载（t2v / i2v 各一个，装载方式 unet_gguf）",
          tpl_gguf_t2v.loader == "unet_gguf" and tpl_gguf_i2v.loader == "unet_gguf"
          and tpl_gguf_t2v.purpose == "t2v" and tpl_gguf_i2v.purpose == "i2v",
          (tpl_gguf_t2v.loader, tpl_gguf_i2v.loader))
    check("GGUF 模板用 GGUF 装载器 + Wan 2.2 潜空间节点",
          {"UnetLoaderGGUF", "CLIPLoaderGGUF", "Wan22ImageToVideoLatent",
           "ModelSamplingSD3", "CreateVideo", "SaveVideo"} <= tpl_gguf_t2v.required_nodes(),
          sorted(tpl_gguf_t2v.required_nodes()))
    check("Wan 2.2 TI2V-5B 有独立架构档案（CFG 5.0，不是 2.1 的 6.0）",
          wt.guess_arch("Wan2.2-TI2V-5B-Q4_K_M.gguf") == "wan22"
          and wt.arch_profile("wan22")["cfg"] == 5.0
          and wt.arch_profile("wan22")["sampler"] == "uni_pc",
          (wt.guess_arch("Wan2.2-TI2V-5B-Q4_K_M.gguf"), wt.arch_profile("wan22")["cfg"]))

    # 装载方式隔离：GGUF 模型不能用 safetensors 模板，反过来也一样
    picked_gguf, _a = wt.pick_template(
        plugin.templates, model_name="Wan2.2-TI2V-5B-Q4_K_M.gguf",
        model_folder="unet_gguf", purpose="t2v")
    picked_safe, _a2 = wt.pick_template(
        plugin.templates, model_name="wan2.1_t2v_1.3B_fp16.safetensors",
        model_folder="diffusion_models", purpose="t2v")
    check("GGUF 模型只挑 GGUF 模板（不会拿 UNETLoader 模板去套）",
          picked_gguf is not None and picked_gguf.name == "wan22_t2v_gguf",
          picked_gguf.name if picked_gguf else None)
    check("safetensors 模型只挑 safetensors 模板（不会拿 GGUF 模板去套）",
          picked_safe is not None and picked_safe.name == "wan_t2v",
          picked_safe.name if picked_safe else None)
    # 真机实测：有的安装把 unet_gguf 直接映射到 models/diffusion_models，
    # 于是 GGUF 权重会以 diffusion_models 目录的身份出现 —— 必须靠扩展名认出来
    picked_gguf_as_diffusion, _a3 = wt.pick_template(
        plugin.templates, model_name="Wan2.2-TI2V-5B-Q4_K_M.gguf",
        model_folder="diffusion_models", purpose="t2v")
    picked_gguf_i2v_as_diffusion, _a4 = wt.pick_template(
        plugin.templates, model_name="Wan2.2-TI2V-5B-Q4_K_M.gguf",
        model_folder="diffusion_models", purpose="i2v")
    check("GGUF 权重即使被列在 diffusion_models 下也走 GGUF 模板（按扩展名识别）",
          picked_gguf_as_diffusion is not None
          and picked_gguf_as_diffusion.name == "wan22_t2v_gguf"
          and picked_gguf_i2v_as_diffusion is not None
          and picked_gguf_i2v_as_diffusion.name == "wan22_i2v_gguf",
          (picked_gguf_as_diffusion.name if picked_gguf_as_diffusion else None,
           picked_gguf_i2v_as_diffusion.name if picked_gguf_i2v_as_diffusion else None))
    picked_safe_as_gguf_dir, _a5 = wt.pick_template(
        plugin.templates, model_name="wan2.1_t2v_1.3B_fp16.safetensors",
        model_folder="unet_gguf", purpose="t2v")
    check("反过来：safetensors 权重落在 unet_gguf 目录时仍走 safetensors 模板",
          picked_safe_as_gguf_dir is not None and picked_safe_as_gguf_dir.name == "wan_t2v",
          picked_safe_as_gguf_dir.name if picked_safe_as_gguf_dir else None)

    built_gguf_save = tpl_gguf_t2v.graph[tpl_gguf_t2v.bindings["save"]]
    built_gguf = tpl_gguf_t2v.build(
        positive="a cat running on grass", negative="bad", model_name="Wan2.2-TI2V-5B-Q4_K_M.gguf",
        width=832, height=480, steps=30, cfg=5.0, sampler="uni_pc", scheduler="simple",
        seed=5, params={"length": 81, "fps": 16},
    )
    check("GGUF 模板注入了 unet_name / clip_name / vae_name 与帧数帧率",
          built_gguf["1"]["inputs"]["unet_name"] == "Wan2.2-TI2V-5B-Q4_K_M.gguf"
          and built_gguf["2"]["inputs"]["clip_name"] == "umt5-xxl-encoder-Q4_K_M.gguf"
          and built_gguf["2"]["inputs"]["type"] == "wan"
          and built_gguf["3"]["inputs"]["vae_name"] == "Wan2.2_VAE.safetensors"
          and built_gguf["6"]["inputs"]["length"] == 81
          and built_gguf[built_gguf_save["inputs"]["video"][0]]["inputs"]["fps"] == 16.0,
          (built_gguf["1"]["inputs"], built_gguf["2"]["inputs"], built_gguf["6"]["inputs"]))
    check("文生视频的 GGUF 模板不带 start_image（纯文生）",
          "start_image" not in built_gguf["6"]["inputs"], built_gguf["6"]["inputs"])

    built_gguf_i2v = tpl_gguf_i2v.build(
        positive="let it move", negative="bad", model_name="Wan2.2-TI2V-5B-Q4_K_M.gguf",
        width=512, height=768, steps=30, cfg=5.0, sampler="uni_pc", scheduler="simple",
        seed=5, image_name="astrbot/start.png", params={"length": 49, "fps": 16},
    )
    check("图生视频的 GGUF 模板把首帧接进 start_image，尺寸贴合输入图",
          built_gguf_i2v["11"]["inputs"]["image"] == "astrbot/start.png"
          and built_gguf_i2v["6"]["inputs"]["start_image"] == ["11", 0]
          and (built_gguf_i2v["6"]["inputs"]["width"], built_gguf_i2v["6"]["inputs"]["height"])
          == (512, 768),
          (built_gguf_i2v["6"]["inputs"].get("start_image"), built_gguf_i2v["6"]["inputs"]["width"]))

    # 端到端：GGUF 视频真的走通提交（mock ComfyUI）
    def gguf_session(pid="gguf-1"):
        sess = api.aiohttp.ClientSession()
        sess.route("GET", "/models", api.aiohttp.ClientResponse(
            200, payload=["unet_gguf", "clip_gguf", "vae", "checkpoints"]))
        sess.route("GET", "/models/unet_gguf", api.aiohttp.ClientResponse(
            200, payload=["Wan2.2-TI2V-5B-Q4_K_M.gguf"]))
        sess.route("GET", "/models/clip_gguf", api.aiohttp.ClientResponse(
            200, payload=["umt5-xxl-encoder-Q4_K_M.gguf"]))
        sess.route("GET", "/models/vae", api.aiohttp.ClientResponse(
            200, payload=["Wan2.2_VAE.safetensors"]))
        sess.route("GET", "/models/checkpoints", api.aiohttp.ClientResponse(
            200, payload=["sd15.safetensors"]))
        sess.route("POST", "/upload/image", api.aiohttp.ClientResponse(
            200, payload={"name": "start.png", "subfolder": "astrbot", "type": "input"}))
        sess.route("POST", "/prompt", api.aiohttp.ClientResponse(200, payload={"prompt_id": pid}))
        sess.route("GET", "/queue", api.aiohttp.ClientResponse(
            200, payload={"queue_running": [], "queue_pending": []}))
        sess.route("GET", f"/history/{pid}", api.aiohttp.ClientResponse(200, payload={pid: {
            "status": {"status_str": "success", "completed": True},
            "outputs": {"10": {"images": [{"filename": "wan22_00001_.webm", "subfolder": "",
                                           "type": "output"}], "animated": [True]}}}}))
        sess.route("GET", "/view", api.aiohttp.ClientResponse(200, text="WEBM"))
        plugin.comfy._session = sess
        plugin.comfy.invalidate_model_cache()
        return sess

    plugin.config["video"] = {"default_seconds": 4, "default_fps": 16, "max_seconds": 10,
                              "send_video": True}
    sess_gguf = gguf_session(pid="gguf-1")
    ev_gguf = AstrMessageEvent(sender_id="8008", message_str="/视频 一只猫在草地上奔跑 --seconds 5")
    out_gguf = asyncio.run(drive(plugin.cmd_video(ev_gguf)))
    submitted_gguf = None
    for method, path, kw in sess_gguf.calls:
        if method == "POST" and path == "/prompt":
            submitted_gguf = kw["json"]["prompt"]
    check("没有 safetensors 视频权重时，/视频 自动改用 GGUF 模板真出片",
          submitted_gguf is not None and submitted_gguf["1"]["class_type"] == "UnetLoaderGGUF"
          and submitted_gguf["6"]["class_type"] == "Wan22ImageToVideoLatent",
          sorted({n["class_type"] for n in (submitted_gguf or {}).values()}))
    check("秒数换算成帧数（5 秒 → 原生 24fps 的 121 帧）",
          (submitted_gguf or {}).get("6", {}).get("inputs", {}).get("length") == 121,
          (submitted_gguf or {}).get("6", {}).get("inputs"))
    check("GGUF 视频结果也用 Video 组件发出",
          any(comp.__class__.__name__ == "Video"
              for x in out_gguf if x.get("type") == "chain" for comp in x["chain"]),
          [x.get("type") for x in out_gguf])

    # 图片底模不能被当成视频底模（真机实测的那个坑）
    def video_with_image_model():
        sess = api.aiohttp.ClientSession()
        sess.route("GET", "/models", api.aiohttp.ClientResponse(200, payload=["checkpoints"]))
        sess.route("GET", "/models/checkpoints", api.aiohttp.ClientResponse(
            200, payload=["AbyssOrangeMix2_hard.safetensors"]))
        sess.route("GET", "/queue", api.aiohttp.ClientResponse(
            200, payload={"queue_running": [], "queue_pending": []}))
        plugin.comfy._session = sess
        plugin.comfy.invalidate_model_cache()
        try:
            asyncio.run(plugin.generate(user_desc="a cat", opts={}, event=None,
                                        force_purpose="t2v"))
            return ""
        except Exception as e:
            return str(e)

    msg_img = video_with_image_model()
    check("把图片底模当视频底模时提前拦住（不再提交必失败的图）",
          "不像视频模型" in msg_img and "unet_gguf" in msg_img, msg_img[:140])

    print("\n=== 视频节奏跟随模型原生（v0.15.4，真机观感问题）===")
    # 真机实测：Wan 2.2 是按 24fps / 81~121 帧训练的。给 17 帧 @8fps 跑出来几乎是静止图，
    # 用户看到的就是「0 秒 / 内容不对」。所以没有显式指定时要自动拉到原生节奏。
    vconf = {"default_seconds": 4, "default_fps": 16, "max_seconds": 10}
    base = m.resolve_video_params({}, vconf)
    check("其它视频模型仍按配置默认（4 秒 16fps → 65 帧）",
          (base["length"], base["fps"]) == (65, 16.0), (base["length"], base["fps"]))

    nat = m.apply_native_video_defaults(dict(base), {}, vconf, "wan22")
    check("Wan 系列没指定参数 → 自动 24fps / 81 帧（原生节奏）",
          (nat["length"], nat["fps"], nat["seconds"]) == (81, 24.0, 3.38),
          (nat["length"], nat["fps"], nat["seconds"]))
    check("Wan 1.3B/14B（arch=wan）同样处理",
          m.apply_native_video_defaults(dict(base), {}, vconf, "wan")["length"] == 81, True)
    check("非 Wan 架构（sd15/video）不动",
          m.apply_native_video_defaults(dict(base), {}, vconf, "video")["length"] == 65
          and m.apply_native_video_defaults(dict(base), {}, vconf, "sd15")["fps"] == 16.0, True)

    only_fps = m.resolve_video_params({"fps": "12"}, vconf)
    nat_fps = m.apply_native_video_defaults(dict(only_fps), {"fps": "12"}, vconf, "wan22")
    check("用户只指定了 --fps：尊重帧率，帧数按它换算",
          nat_fps["fps"] == 12.0 and nat_fps["length"] == 49,
          (nat_fps["fps"], nat_fps["length"]))

    only_seconds = m.resolve_video_params({"seconds": "2"}, vconf)
    nat_sec = m.apply_native_video_defaults(dict(only_seconds), {"seconds": "2"}, vconf, "wan22")
    check("用户只指定了 --seconds：帧率仍拉到 24，帧数按 2 秒换算（49）",
          (nat_sec["fps"], nat_sec["length"]) == (24.0, 49), (nat_sec["fps"], nat_sec["length"]))

    only_length = m.resolve_video_params({"length": "33", "fps": "8"}, vconf)
    nat_len = m.apply_native_video_defaults(dict(only_length), {"length": "33", "fps": "8"}, vconf, "wan22")
    check("用户同时给了 --length 与 --fps：完全尊重，不插手",
          (nat_len["length"], nat_len["fps"]) == (33, 8.0), (nat_len["length"], nat_len["fps"]))

    tight = {"default_seconds": 4, "default_fps": 16, "max_seconds": 2}
    clamped = m.apply_native_video_defaults(
        m.resolve_video_params({}, tight), {}, tight, "wan22")
    check("原生默认也受 max_seconds 约束（2 秒上限 → 45 帧，不超时）",
          clamped["length"] == 45 and clamped["seconds"] <= 2.0,
          (clamped["length"], clamped["seconds"]))

    # 端到端：/视频 不写参数时，提交的图里就是 24fps / 81 帧
    plugin.config["video"] = {"default_seconds": 4, "default_fps": 16, "max_seconds": 10,
                              "send_video": True}
    sess_nat = gguf_session(pid="native-1")
    ev_nat = AstrMessageEvent(sender_id="8100", message_str="/视频 一只猫在草地上奔跑")
    asyncio.run(drive(plugin.cmd_video(ev_nat)))
    submitted_nat = None
    for method, path, kw in sess_nat.calls:
        if method == "POST" and path == "/prompt":
            submitted_nat = kw["json"]["prompt"]
    check("/视频 默认提交 81 帧 + 24fps（观感问题的那次是 17 帧 @8fps）",
          submitted_nat is not None
          and submitted_nat["6"]["inputs"]["length"] == 81
          and submitted_nat[submitted_nat["12"]["inputs"]["video"][0]]["inputs"]["fps"] == 24.0,
          (submitted_nat or {}).get("6", {}).get("inputs", {}).get("length"))

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
