"""插件文案的国际化（i18n）。

按 AstrBot 的**官方约定**读文案：插件根目录下的 `.astrbot-plugin/i18n/<locale>.json`，
每个文件是一个 JSON 对象（键 → 文案），文件名就是 locale（`zh-CN` / `en-US`…）。
AstrBot 自己也会加载同一批文件（放进 Star 元数据的 `i18n` 字段），所以两边的目录结构一致，
不需要额外转换。

设计约定
--------
- **点号键**：`queue.waiting`、`result.params` 这样分层，读取时按 `.` 逐层下钻。
- **回退链**：选定 locale → `zh-CN` → 键名本身。任何一层缺失都不会让插件抛错，
  最坏情况是界面上出现一个键名（比崩掉强，而且一眼能看出漏翻译）。
- **格式化**：文案里用 `{name}`，`t("k", name="x")` 会做 `str.format`；
  参数缺失时**不抛错**，原样返回，避免一句文案把整个指令搞崩。
- 语言由配置项 `language` 决定：`auto` 表示跟随 AstrBot 的界面语言（拿不到就用中文）。
"""
from __future__ import annotations

import json
from pathlib import Path

# 默认语言与兜底语言（仓库里始终保证这一份齐全）
DEFAULT_LOCALE = "zh-CN"
I18N_DIRNAME = Path(".astrbot-plugin") / "i18n"
MAX_FILE_BYTES = 1024 * 1024  # 与 AstrBot 的上限保持一致


def i18n_dir(plugin_dir: Path) -> Path:
    """插件文案目录（`.astrbot-plugin/i18n`）。"""
    return Path(plugin_dir) / I18N_DIRNAME


def load_translations(directory: Path, *, logger=None) -> dict[str, dict]:
    """读取目录下的全部文案文件。

    与 AstrBot 的加载规则一致：只认 `.json`，文件名（≤32 字符）当 locale，
    用 `utf-8-sig` 解码，超过 1 MB 或不是 JSON 对象的文件跳过。

    Args:
        directory: `.astrbot-plugin/i18n` 目录。
        logger: 可选日志器（跳过坏文件时记一条 warning）。

    Returns:
        `{locale: {键: 文案}}`；目录不存在时返回空字典。
    """
    catalog: dict[str, dict] = {}
    path = Path(directory)
    if not path.is_dir():
        return catalog
    for file_path in sorted(path.iterdir()):
        if file_path.suffix.lower() != ".json" or not file_path.is_file():
            continue
        locale = file_path.stem
        if not locale or len(locale) > 32:
            continue
        try:
            if file_path.stat().st_size > MAX_FILE_BYTES:
                raise ValueError("文件超过 1 MB")
            data = json.loads(file_path.read_text(encoding="utf-8-sig"))
        except Exception as e:  # 坏文案不该拖垮插件启动
            if logger is not None:
                logger.warning("跳过无法读取的文案文件 %s：%s", file_path.name, e)
            continue
        if isinstance(data, dict):
            catalog[locale] = data
        elif logger is not None:
            logger.warning("文案文件 %s 不是 JSON 对象，已跳过", file_path.name)
    return catalog


def _lookup(flat: dict, key: str) -> str:
    """在（可能是嵌套的）文案字典里按点号键取值。"""
    if key in flat:
        return flat[key]
    current = flat
    for part in str(key).split("."):
        if not isinstance(current, dict) or part not in current:
            return ""
        current = current[part]
    return current if isinstance(current, str) else ""


class Translator:
    """把键翻译成当前语言文案。"""

    def __init__(
        self,
        catalog: dict[str, dict] | None = None,
        *,
        locale: str = "",
        default_locale: str = DEFAULT_LOCALE,
        logger=None,
    ):
        """初始化。

        Args:
            catalog: `{locale: {键: 文案}}`。
            locale: 期望语言；为空或不可用时用 `default_locale`。
            default_locale: 兜底语言。
            logger: 可选日志器。
        """
        self._catalog = catalog or {}
        self._default = default_locale if default_locale in self._catalog else (
            next(iter(self._catalog), default_locale)
        )
        self._logger = logger
        self._locale = self.pick_locale(locale)

    # ------------------------------------------------------------------ #
    # 语言
    # ------------------------------------------------------------------ #
    @property
    def available(self) -> list[str]:
        """已加载的语言列表。"""
        return sorted(self._catalog)

    @property
    def locale(self) -> str:
        """当前生效的语言。"""
        return self._locale

    def pick_locale(self, wanted: str) -> str:
        """把「想要的语言」解析成实际可用的语言。

        Args:
            wanted: 语言代码；`auto` / 空 / 不认识的值都会退回兜底语言。

        Returns:
            实际生效的语言代码。
        """
        text = str(wanted or "").strip()
        if not text or text.lower() == "auto":
            return self._default
        if text in self._catalog:
            return text
        # 容忍只写主语言（zh / en）或大小写不一致
        lowered = text.lower().replace("_", "-")
        for locale in self._catalog:
            if locale.lower().replace("_", "-") == lowered:
                return locale
        for locale in self._catalog:
            if locale.lower().split("-")[0] == lowered.split("-")[0]:
                return locale
        return self._default

    # ------------------------------------------------------------------ #
    # 取文案
    # ------------------------------------------------------------------ #
    def t(self, key: str, **kwargs) -> str:
        """取一条文案。

        Args:
            key: 点号键，例如 `queue.waiting`。
            **kwargs: 文案里的 `{name}` 参数。

        Returns:
            文案；三层都没命中时返回键名本身。格式化参数缺失时原样返回。
        """
        text = _lookup(self._catalog.get(self._locale) or {}, key)
        if not text:
            text = _lookup(self._catalog.get(self._default) or {}, key)
        if not text:
            if self._logger is not None:
                self._logger.debug("缺少文案键：%s", key)
            return key
        if kwargs:
            try:
                return text.format(**kwargs)
            except (KeyError, IndexError, ValueError):
                return text
        return text

    def __call__(self, key: str, **kwargs) -> str:
        """让 Translator 本身可调用：`t("key")` 等价于 `t.t("key")`。

        这样它能直接当「翻译函数」传给别的组件（PermissionManager / LLMService），
        同时保留 `t.locale`、`t.available` 这些属性。
        """
        return self.t(key, **kwargs)

    def ui_strings(self) -> dict:
        """插件页要用的那部分文案（`ui.` 前缀）。"""
        merged: dict = {}
        for locale in (self._default, self._locale):
            for key, value in (self._catalog.get(locale) or {}).items():
                if key.startswith("ui."):
                    merged[key] = value
        return merged


_DEFAULT_CACHE: dict[str, Translator] = {}


def default_translator(plugin_dir: "Path | str | None" = None) -> Translator:
    """没有注入翻译器时的兜底：直接用插件自带的中文文案。

    这样 `PermissionManager(config)`、`LLMService(ctx, cfg)` 这类**单独构造**的场景
    也不会返回键名（最典型的是 LLM 系统提示词：拿不到文案就会把键名当提示词发出去）。

    Args:
        plugin_dir: 插件根目录；留空用本文件所在目录。

    Returns:
        缓存过的 Translator（同一目录只加载一次）。
    """
    root = Path(plugin_dir) if plugin_dir else Path(__file__).resolve().parent
    cache_key = str(root)
    if cache_key not in _DEFAULT_CACHE:
        _DEFAULT_CACHE[cache_key] = build_translator(root)
    return _DEFAULT_CACHE[cache_key]


def build_translator(
    plugin_dir: Path,
    *,
    locale: str = "",
    logger=None,
) -> Translator:
    """从插件目录加载文案并构造 Translator。

    Args:
        plugin_dir: 插件根目录。
        locale: 配置里的 `language`（可为 auto/空）。
        logger: 可选日志器。

    Returns:
        Translator；目录缺失时也能正常构造（全部回退到键名）。
    """
    catalog = load_translations(i18n_dir(plugin_dir), logger=logger)
    return Translator(catalog, locale=locale, logger=logger)
