"""对照真实 AstrBot 源码核验插件用到的 API 面。

用法：
    python tests/check_api_surface.py /path/to/AstrBot

为什么需要这个：
    插件最容易在「AstrBot 升级后某个方法改名/被移除」时静默失效。
    本脚本不去运行 AstrBot，而是直接读它的源码，核验两件事：
      1. 插件 import 的每个 astrbot 符号（模块与顶层名字）确实存在；
      2. 插件调用的每个方法/属性在对应类里确实存在。
    这样在没有完整运行环境的机器上也能给出确定的结论。

退出码：0 全部通过；1 有缺失（会逐条打印）；2 未提供 AstrBot 路径。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent

# (说明, 相对 AstrBot 根目录的路径, 类名, 需要存在的成员)
MEMBER_TARGETS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("Star Context", "astrbot/core/star/context.py", "Context",
     ("register_web_api", "activate_llm_tool", "deactivate_llm_tool",
      "get_all_providers", "get_provider_by_id", "llm_generate",
      "get_current_chat_provider_id")),
    ("StarTools", "astrbot/core/star/star_tools.py", "StarTools", ("get_data_dir",)),
    ("Star 基类", "astrbot/core/star/base.py", "Star",
     ("html_render", "text_to_image", "initialize", "terminate")),
    ("AstrBotConfig", "astrbot/core/config/astrbot_config.py", "AstrBotConfig",
     ("save_config",)),
    ("AstrMessageEvent", "astrbot/core/platform/astr_message_event.py", "AstrMessageEvent",
     ("is_admin", "get_sender_id", "get_sender_name", "get_group_id",
      "plain_result", "chain_result", "send", "unified_msg_origin")),
    ("消息组件 Image", "astrbot/core/message/components.py", "Image", ("fromFileSystem",)),
    ("Pages 请求代理", "astrbot/api/web.py", "PluginRequest",
     ("json", "form", "files", "body", "query", "path_params", "username",
      "plugin_name", "method")),
)
# 缺失时插件有「文档化回退」的成员：不要求存在，但会单独报告，
# 以便一眼看出当前 AstrBot 版本走的是哪条路径。
FALLBACK_MEMBERS: tuple[tuple[str, str, str, str], ...] = (
    ("AstrBotConfig", "astrbot/core/config/astrbot_config.py", "AstrBotConfig",
     "save_config_async"),
)
# 回退说明（用于输出）
FALLBACK_NOTES = {
    "save_config_async": "缺失时 main.py 回退到 AstrBotConfig.save_config()",
}
# 注意：Star.logger 不再被使用。上架规范要求 logger 必须且只能来自
# `from astrbot.api import logger`，插件里任何 `logging.getLogger(...)` 回退都属违规，
# 因此这里也不再把它当作「可选回退成员」来报告。

# astrbot/api/web.py 需要导出的顶层名字
WEB_EXPORTS = ("json_response", "error_response", "file_response", "request",
               "PluginUploadFile")
# 权限类型枚举所在模块
PERMISSION_MODULE = "astrbot/core/star/filter/permission.py"


def _find_module(root: Path, module: str) -> Path | None:
    """模块名转源码文件。"""
    base = root / Path(*module.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


class _Exports:
    """带缓存的模块顶层导出集合（处理 import * 转发）。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._cache: dict[str, set[str] | None] = {}
        self._busy: set[str] = set()

    def module_exists(self, module: str) -> bool:
        return _find_module(self.root, module) is not None

    def of(self, module: str, depth: int = 0) -> set[str] | None:
        if module in self._cache:
            return self._cache[module]
        if depth > 5 or module in self._busy:
            return set()
        path = _find_module(self.root, module)
        if path is None:
            return None
        self._busy.add(module)
        names: set[str] = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        names.add(target.id)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names.add(node.target.id)
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name == "*":
                        names |= self.of(node.module, depth + 1) or set()
                    else:
                        names.add(alias.asname or alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    names.add(alias.asname or alias.name.split(".")[0])
        self._cache[module] = names
        self._busy.discard(module)
        return names


def _class_members(path: Path, class_name: str) -> set[str] | None:
    """收集类的成员名（含 __init__ 里 self.xxx 注入的属性）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.ClassDef) and node.name == class_name):
            continue
        names: set[str] = set()
        for sub in ast.walk(node):
            if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names.add(sub.name)
            elif isinstance(sub, ast.AnnAssign) and isinstance(sub.target, ast.Name):
                names.add(sub.target.id)
            elif isinstance(sub, ast.Assign):
                for target in sub.targets:
                    if isinstance(target, ast.Name):
                        names.add(target.id)
            elif (isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name)
                  and sub.value.id == "self"):
                names.add(sub.attr)
        return names
    return None


def check_imports(exports: _Exports) -> tuple[int, list[str]]:
    """核验插件 import 的 astrbot 符号全部存在。"""
    checked = 0
    problems: list[str] = []
    for path in sorted(PLUGIN_ROOT.rglob("*.py")):
        if path.parent.name == "tests":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = path.relative_to(PLUGIN_ROOT)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("astrbot"):
                for alias in node.names:
                    checked += 1
                    available = exports.of(node.module)
                    if available is None:
                        problems.append(f"{rel}: 模块不存在 -> {node.module}")
                    elif alias.name not in available and not exports.module_exists(
                        f"{node.module}.{alias.name}"
                    ):
                        problems.append(f"{rel}: {node.module} 里没有 {alias.name}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("astrbot"):
                        checked += 1
                        if not exports.module_exists(alias.name):
                            problems.append(f"{rel}: 模块不存在 -> {alias.name}")
    return checked, problems


def check_members(root: Path) -> tuple[int, list[str], list[str]]:
    """核验插件调用的成员在对应类里存在。

    Returns:
        (核验项数, 问题列表, 走回退路径的说明列表)。
    """
    checked = 0
    problems: list[str] = []
    unavailable: list[str] = []
    for label, rel, class_name, members in MEMBER_TARGETS:
        path = root / rel
        if not path.is_file():
            problems.append(f"{label}: 源文件不存在 {rel}")
            continue
        found = _class_members(path, class_name)
        if found is None:
            problems.append(f"{label}: {rel} 里没有类 {class_name}")
            continue
        for member in members:
            checked += 1
            if member not in found:
                problems.append(f"{label}（{class_name}）缺少成员：{member}")

    # 有回退的成员：只报告，不算失败
    for label, rel, class_name, member in FALLBACK_MEMBERS:
        path = root / rel
        if not path.is_file():
            continue
        found = _class_members(path, class_name)
        if found is not None and member not in found:
            note = FALLBACK_NOTES.get(member, "")
            unavailable.append(f"{label}.{member} 不存在 -> 走回退路径（{note}）")

    # 上架规范：日志必须来自官方 `from astrbot.api import logger`。
    # 这里核验 astrbot/api/__init__.py 真的导出了 logger（含 v4.26.0 的转发写法）。
    api_init = root / "astrbot/api/__init__.py"
    if api_init.is_file():
        checked += 1
        if "logger" not in (_Exports(root).of("astrbot.api") or set()):
            problems.append(
                "astrbot/api/__init__.py 未导出 logger（插件按上架规范依赖它，不能回退到 logging）"
            )

    web = root / "astrbot/api/web.py"
    if web.is_file():
        available = _Exports(root).of("astrbot.api.web") or set()
        for helper in WEB_EXPORTS:
            checked += 1
            if helper not in available:
                problems.append(f"astrbot/api/web.py 未导出：{helper}")
    # 权限枚举（@filter.permission_type(filter.PermissionType.ADMIN) 依赖它）
    perm = root / PERMISSION_MODULE
    if perm.is_file():
        checked += 1
        if "PermissionType" not in (_Exports(root).of("astrbot.core.star.filter.permission") or set()):
            problems.append(f"{PERMISSION_MODULE} 未定义 PermissionType")
    return checked, problems, unavailable


def main(argv: list[str]) -> int:
    """入口。"""
    if len(argv) < 2:
        print(__doc__)
        print("提示：未提供 AstrBot 源码路径，跳过核验。")
        return 2
    root = Path(argv[1]).expanduser().resolve()
    if not (root / "astrbot").is_dir():
        print(f"路径不对：{root} 下没有 astrbot/ 目录")
        return 2

    exports = _Exports(root)
    total = 0
    problems: list[str] = []
    checked, found = check_imports(exports)
    total += checked
    print(f"[import 符号] 核验 {checked} 项，问题 {len(found)} 个")
    problems.extend(found)

    checked, found, fallbacks = check_members(root)
    total += checked
    print(f"[API 成员] 核验 {checked} 项，问题 {len(found)} 个")
    problems.extend(found)
    if fallbacks:
        print("[回退路径] 当前版本上以下成员不存在，插件会走已实现的回退：")
        for item in fallbacks:
            print("  ~", item)

    if problems:
        print("\n发现问题：")
        for item in problems:
            print("  !!", item)
        return 1
    print(f"\n插件用到的全部 {total} 项 AstrBot API 在 {root} 中均存在 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
