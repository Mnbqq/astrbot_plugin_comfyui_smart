#!/usr/bin/env python3
"""从 `docs/CHANGELOG.md` 抽取某个 tag 的版本说明，生成 Release 正文。

为什么需要它
------------
`softprops/action-gh-release` 的 `generate_release_notes: true` 只会生成
「What's Changed」——而那个列表来自**已合并的 PR**。本仓库是直接往 main 推提交的、
不开 PR，于是自动生成的正文只有孤零零一行 `**Full Changelog**: ...`，
发出去的 Release 等于没有说明。CHANGELOG 本来就是逐版本写好的，直接拿它当正文。

用法
----
    python3 .github/scripts/release_notes.py v0.29.0 [owner/repo]

结果写到当前目录的 `release_body.md`。**总是**会写出一个非空文件：
CHANGELOG 里找不到对应条目时退化成一句说明 + compare 链接，绝不产出空正文
（空正文会让 Release 页面变成一片空白，比没有说明更糟）。

环境变量
--------
GITHUB_REPOSITORY   形如 `owner/repo`；没传第二个参数时用它拼 compare 链接。
"""
from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys

CHANGELOG = pathlib.Path("docs/CHANGELOG.md")
OUTPUT = pathlib.Path("release_body.md")


def _version_key(tag: str) -> tuple:
    """把 `v0.29.0` 变成可比较的元组；不是纯数字段就当 0，避免排序炸掉。"""
    parts = tag.lstrip("vV").split(".")
    out = []
    for part in parts:
        digits = re.match(r"^\d+", part)
        out.append(int(digits.group()) if digits else 0)
    return tuple(out)


def _all_tags() -> list[str]:
    """取仓库里全部 `v*` tag，按版本号升序。取不到就返回空列表。"""
    try:
        raw = subprocess.run(
            ["git", "tag", "-l", "v*"], capture_output=True, text=True, check=True
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return []
    tags = [line.strip() for line in raw.splitlines() if line.strip()]
    return sorted(tags, key=_version_key)


def _previous_tag(tag: str) -> str:
    """比给定 tag 小的最近一个版本；没有就返回空串。"""
    tags = _all_tags()
    if tag not in tags:
        # tag 还没进本地列表（少见）：退化成「比它小的最后一个」
        smaller = [t for t in tags if _version_key(t) < _version_key(tag)]
        return smaller[-1] if smaller else ""
    index = tags.index(tag)
    return tags[index - 1] if index > 0 else ""


def _section(tag: str) -> str:
    """从 CHANGELOG 里截出 `**vX.Y.Z** — ...` 到下一个版本标题之前那一整段。"""
    if not CHANGELOG.is_file():
        return ""
    text = CHANGELOG.read_text(encoding="utf-8")
    # 标题行形如 `**v0.29.0** — 一句话`；到下一个以 `**v` 开头的行之前结束
    pattern = re.compile(
        rf"^\*\*{re.escape(tag)}\*\*.*?(?=^\*\*v\d|\Z)", re.MULTILINE | re.DOTALL
    )
    matched = pattern.search(text)
    if not matched:
        return ""
    section = matched.group(0).strip()
    # 文件末尾（或段落之间）可能有 `---` 分隔线，带进 Release 正文会显得突兀
    while section.endswith("---"):
        section = section[: -len("---")].rstrip()
    return section


def _compare_line(tag: str, repo: str) -> str:
    """拼 `**Full Changelog**` 那一行；找不到上一个版本就返回空串。"""
    if not repo:
        return ""
    previous = _previous_tag(tag)
    if not previous:
        return ""
    return f"**Full Changelog**: https://github.com/{repo}/compare/{previous}...{tag}"


def build_body(tag: str, repo: str = "") -> tuple[str, bool]:
    """生成 Release 正文。

    Args:
        tag: 形如 `v0.29.0`。
        repo: `owner/repo`，用于拼 compare 链接；留空则不加那一行。

    Returns:
        (正文, 是否命中 CHANGELOG 条目)。
    """
    section = _section(tag)
    found = bool(section)
    if not found:
        section = (
            f"> ⚠️ `docs/CHANGELOG.md` 里没有 `{tag}` 的条目，本页说明未能生成。\n"
            f"> 请检查是不是漏写了这一版的更新日志。"
        )
    parts = [section]
    compare = _compare_line(tag, repo)
    if compare:
        parts.append(compare)
    return "\n\n---\n\n".join(parts) + "\n", found


def main() -> int:
    if len(sys.argv) < 2 or not sys.argv[1].strip():
        print("用法：release_notes.py <tag> [owner/repo]", file=sys.stderr)
        return 2
    tag = sys.argv[1].strip()
    repo = sys.argv[2].strip() if len(sys.argv) > 2 else os.environ.get(
        "GITHUB_REPOSITORY", ""
    )
    body, found = build_body(tag, repo)
    OUTPUT.write_text(body, encoding="utf-8")
    print(f"tag={tag} repo={repo or '(未提供)'} CHANGELOG命中={found} "
          f"正文字数={len(body)}")
    print("---- release_body.md ----")
    print(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
