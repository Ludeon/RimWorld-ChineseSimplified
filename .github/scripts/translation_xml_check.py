#!/usr/bin/env python3
"""Validate changes to the RimWorld translation XML files in this repository.

The check compares every translation XML file changed between a comparison
base and a target revision.  It allows ordinary translation text edits while
rejecting changes that would break the localization data:

* the file must remain valid XML;
* existing keys, nested nodes and attributes must remain present and
  unchanged (new keys and new nodes may be added);
* placeholder names and their number of occurrences must not change
  (placeholders may be reordered);
* literal ``\\n`` formatting markers must keep the same count per key;
* the encoding, BOM and line-ending style must not change.

Duplicate ``<li>`` list items are legal and must not be treated as duplicate
keys.  Duplicate non-``li`` keys are rejected.

Only the Python standard library is used.
"""

from __future__ import annotations

import argparse
import codecs
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Iterable, Sequence


# Top-level translation folders.  Files outside these folders (for example the
# check script's own test fixtures) are never treated as translation data.
TRANSLATION_ROOTS: tuple[str, ...] = (
    "Core",
    "Royalty",
    "Ideology",
    "Biotech",
    "Anomaly",
    "Odyssey",
)

# The UTF-32 byte-order marks start with the corresponding UTF-16 marks, so
# they have to be checked first.
_BOM_PREFIXES: tuple[tuple[bytes, str], ...] = (
    (codecs.BOM_UTF8, "utf-8"),
    (codecs.BOM_UTF32_LE, "utf-32-le"),
    (codecs.BOM_UTF32_BE, "utf-32-be"),
    (codecs.BOM_UTF16_LE, "utf-16-le"),
    (codecs.BOM_UTF16_BE, "utf-16-be"),
)

_XML_DECL_RE = re.compile(
    r"^\s*<\?xml[^>]*\bencoding\s*=\s*[\"']([^\"']+)[\"']",
    re.IGNORECASE,
)
_XML_DECL_BYTES_RE = re.compile(
    rb"^\s*<\?xml[^>]*\bencoding\s*=\s*[\"']([^\"']+)[\"']",
    re.IGNORECASE,
)

# Placeholders look like {0}, {PAWN_nameDef} or
# {PAWN_gender ? 小子 : 女孩}.  Doubled braces ({{ and }}) are escaped literal
# braces and are not placeholders.
_PLACEHOLDER_RE = re.compile(r"\{\{|\}\}|\{([^{}]*)\}")


class CheckError(RuntimeError):
    """An operational error (bad revision, git failure, ...)."""


@dataclass(frozen=True)
class Document:
    text: str
    bom: str | None
    declared_encoding: str | None
    effective_encoding: str
    newline_kinds: frozenset[str]


def _canonical_encoding(name: str) -> str | None:
    try:
        return codecs.lookup(name).name
    except LookupError:
        return None


def _bom_info(data: bytes) -> tuple[bytes, str | None]:
    for prefix, name in _BOM_PREFIXES:
        if data.startswith(prefix):
            return prefix, name
    return b"", None


def _effective_encoding(
    bom: str | None, declared: str | None, errors: list[str]
) -> str | None:
    if declared is not None:
        canonical = _canonical_encoding(declared)
        if canonical is None:
            errors.append(f"XML 声明中的编码 {declared!r} 无法识别")
            return None
        if bom is not None and not _declared_matches_bom(canonical, bom):
            errors.append(
                f"XML 声明的编码 {declared!r} 与 BOM ({bom}) 不一致"
            )
        return canonical
    if bom is not None:
        # UTF-16/UTF-32 BOMs may be declared as the endian-neutral name.
        if bom.startswith("utf-16"):
            return "utf-16"
        if bom.startswith("utf-32"):
            return "utf-32"
        return bom
    return "utf-8"


def _declared_matches_bom(declared: str, bom: str) -> bool:
    if declared == bom:
        return True
    if declared == "utf-16" and bom in ("utf-16-le", "utf-16-be"):
        return True
    if declared == "utf-32" and bom in ("utf-32-le", "utf-32-be"):
        return True
    return False


def decode_document(
    data: bytes, side: str
) -> tuple[Document | None, list[str]]:
    """Decode *data*, returning the document metadata and any errors."""
    errors: list[str] = []
    bom_bytes, bom = _bom_info(data)
    body = data[len(bom_bytes) :] if bom_bytes else data

    declared_peek: str | None = None
    if bom is None or bom == "utf-8":
        match = _XML_DECL_BYTES_RE.search(body[:1024])
        if match:
            declared_peek = match.group(1).decode("ascii", errors="replace")

    if bom is not None:
        decode_encoding = bom
    else:
        decode_encoding = declared_peek or "utf-8"

    try:
        text = body.decode(decode_encoding)
    except (UnicodeDecodeError, LookupError) as exc:
        errors.append(f"{side}文件无法按 {decode_encoding} 解码: {exc}")
        return None, errors

    match = _XML_DECL_RE.match(text)
    declared = match.group(1) if match else None
    effective = _effective_encoding(bom, declared, errors)
    if effective is None:
        return None, errors

    return (
        Document(
            text=text,
            bom=bom,
            declared_encoding=declared,
            effective_encoding=effective,
            newline_kinds=_newline_kinds(data),
        ),
        errors,
    )


def _newline_kinds(data: bytes) -> frozenset[str]:
    crlf = data.count(b"\r\n")
    lf = data.count(b"\n") - crlf
    cr = data.count(b"\r") - crlf
    kinds: set[str] = set()
    if crlf:
        kinds.add("CRLF")
    if lf:
        kinds.add("LF")
    if cr:
        kinds.add("CR")
    return frozenset(kinds)


def parse_xml(document: Document, side: str) -> tuple[ET.Element | None, list[str]]:
    errors: list[str] = []
    if re.search(r"<!DOCTYPE|<!ENTITY", document.text, re.IGNORECASE):
        errors.append(f"{side}文件包含不允许的 DOCTYPE/ENTITY 声明")
        return None, errors
    try:
        return ET.fromstring(document.text), errors
    except ET.ParseError as exc:
        errors.append(f"{side}文件不是有效的 XML: {exc}")
        return None, errors


def _element_text(elem: ET.Element) -> str:
    """Return all text inside *elem* (including nested nodes' text)."""
    parts = [elem.text or ""]
    for child in elem.iter():
        if child is elem:
            continue
        parts.append(child.text or "")
        parts.append(child.tail or "")
    return "".join(parts)


def placeholder_names(text: str) -> Counter[str]:
    """Count placeholder names in *text*.

    Reordering placeholders only changes their position, not this counter.
    For conditional placeholders such as
    ``{PAWN_gender ? 小子 : 女孩}`` the placeholder name is the part before
    the first ``?``; the branches themselves are translatable text.
    """
    names: Counter[str] = Counter()
    for match in _PLACEHOLDER_RE.finditer(text):
        inner = match.group(1)
        if inner is None:
            # An escaped literal brace ({{ or }}), not a placeholder.
            continue
        if "?" in inner:
            name = inner.split("?", 1)[0].strip()
        else:
            name = inner.strip()
        names[name] += 1
    return names


def _format_marker_count(text: str) -> int:
    # In RimWorld translation strings a literal backslash-n is a line break.
    return text.count("\\n")


def _subtree_signatures(elem: ET.Element) -> Counter[tuple]:
    """Count element tags and attributes below *elem*, preserving nesting.

    The ancestor tag chain keeps nodes from different keys apart.  Sibling
    counts are preserved, so removing or changing an existing node is
    detected while adding a node is allowed.
    """
    signatures: Counter[tuple] = Counter()

    def visit(node: ET.Element, ancestors: list[ET.Element]) -> None:
        for child in node:
            signature = (
                tuple(ancestor.tag for ancestor in ancestors),
                child.tag,
                tuple(sorted(child.attrib.items())),
            )
            signatures[signature] += 1
            visit(child, ancestors + [child])

    visit(elem, [elem])
    return signatures


def _format_signature(signature: tuple) -> str:
    ancestors, tag, attributes = signature
    parent = " > ".join(ancestors)
    rendered_attributes = "".join(
        f' {name}="{value}"' for name, value in attributes
    )
    node = f"<{tag}{rendered_attributes}>"
    return f"{parent} > {node}" if parent else node


def _counter_difference(expected: Counter[str], actual: Counter[str]) -> str:
    missing = expected - actual
    extra = actual - expected
    parts: list[str] = []
    if missing:
        parts.append(
            "缺少 "
            + "、".join(
                f"{{{name}}}×{count}" for name, count in sorted(missing.items())
            )
        )
    if extra:
        parts.append(
            "多出 "
            + "、".join(
                f"{{{name}}}×{count}" for name, count in sorted(extra.items())
            )
        )
    return "；".join(parts)


def _compare_placeholders(
    label: str,
    base_text: str,
    head_text: str,
    errors: list[str],
) -> None:
    base_names = placeholder_names(base_text)
    head_names = placeholder_names(head_text)
    if base_names != head_names:
        errors.append(
            f"{label} 占位符名称或出现次数改变: "
            + _counter_difference(base_names, head_names)
        )


def _compare_format_markers(
    label: str, base_text: str, head_text: str, errors: list[str]
) -> None:
    base_count = _format_marker_count(base_text)
    head_count = _format_marker_count(head_text)
    if base_count != head_count:
        errors.append(
            f"{label} 的换行格式标记 \\n 数量由 {base_count} 变为 {head_count}"
        )


def _compare_node_set(
    label: str,
    base_signatures: Counter[tuple],
    head_signatures: Counter[tuple],
    errors: list[str],
) -> None:
    for signature, expected_count in base_signatures.items():
        actual_count = head_signatures.get(signature, 0)
        if actual_count < expected_count:
            errors.append(
                f"{label} 中已有节点或属性缺失/被修改: "
                f"{_format_signature(signature)}"
                f"（期望至少 {expected_count} 个，实际 {actual_count} 个）"
            )


def _group_children(root: ET.Element) -> dict[str, list[ET.Element]]:
    groups: dict[str, list[ET.Element]] = defaultdict(list)
    for child in root:
        groups[child.tag].append(child)
    return groups


def _group_node_signatures(elems: Iterable[ET.Element]) -> Counter[tuple]:
    return Counter(
        (elem.tag, tuple(sorted(elem.attrib.items()))) for elem in elems
    )


def _compare_key(
    tag: str, base_key: ET.Element, head_key: ET.Element, errors: list[str]
) -> None:
    label = f"键 <{tag}>"

    if dict(base_key.attrib) != dict(head_key.attrib):
        errors.append(
            f"{label} 的属性改变: 基线 {dict(base_key.attrib)!r}，"
            f"修改后 {dict(head_key.attrib)!r}"
        )

    base_text = _element_text(base_key)
    head_text = _element_text(head_key)
    _compare_placeholders(label, base_text, head_text, errors)
    _compare_format_markers(label, base_text, head_text, errors)

    base_signatures = _subtree_signatures(base_key)
    head_signatures = _subtree_signatures(head_key)
    _compare_node_set(label, base_signatures, head_signatures, errors)


def _compare_root_li_group(
    base_lis: Sequence[ET.Element],
    head_lis: Sequence[ET.Element],
    errors: list[str],
) -> None:
    label = "根节点 <li> 列表"
    if len(head_lis) < len(base_lis):
        errors.append(
            f"{label} 的节点数由 {len(base_lis)} 变为 {len(head_lis)}"
        )

    base_text = "".join(_element_text(elem) for elem in base_lis)
    head_text = "".join(_element_text(elem) for elem in head_lis)
    _compare_placeholders(label, base_text, head_text, errors)
    _compare_format_markers(label, base_text, head_text, errors)

    base_signatures: Counter[tuple] = Counter()
    head_signatures: Counter[tuple] = Counter()
    for elem in base_lis:
        base_signatures.update(_subtree_signatures(elem))
    for elem in head_lis:
        head_signatures.update(_subtree_signatures(elem))
    _compare_node_set(label, base_signatures, head_signatures, errors)

    base_nodes = _group_node_signatures(base_lis)
    head_nodes = _group_node_signatures(head_lis)
    _compare_node_set(label, base_nodes, head_nodes, errors)


def validate_pair(
    base_bytes: bytes, head_bytes: bytes
) -> list[str]:
    """Validate that *head_bytes* is a safe translation edit of *base_bytes*."""
    errors: list[str] = []

    base_doc, base_errors = decode_document(base_bytes, "基线")
    head_doc, head_errors = decode_document(head_bytes, "修改后")
    errors.extend(base_errors)
    errors.extend(head_errors)
    if base_doc is None or head_doc is None:
        return errors

    if base_doc.bom != head_doc.bom:
        errors.append(
            f"BOM 被改变: 基线为 {base_doc.bom or '无 BOM'}，"
            f"修改后为 {head_doc.bom or '无 BOM'}"
        )
    if base_doc.effective_encoding != head_doc.effective_encoding:
        errors.append(
            f"编码被改变: 基线为 {base_doc.effective_encoding}，"
            f"修改后为 {head_doc.effective_encoding}"
        )
    if base_doc.newline_kinds != head_doc.newline_kinds:
        errors.append(
            "换行格式被改变: 基线为 "
            f"{sorted(base_doc.newline_kinds) or ['无换行']}，"
            f"修改后为 {sorted(head_doc.newline_kinds) or ['无换行']}"
        )

    base_root, base_parse_errors = parse_xml(base_doc, "基线")
    head_root, head_parse_errors = parse_xml(head_doc, "修改后")
    errors.extend(base_parse_errors)
    errors.extend(head_parse_errors)
    if base_root is None or head_root is None:
        return errors

    if base_root.tag != head_root.tag:
        errors.append(
            f"根节点由 <{base_root.tag}> 变为 <{head_root.tag}>"
        )
        return errors

    base_groups = _group_children(base_root)
    head_groups = _group_children(head_root)

    for tag, base_elems in base_groups.items():
        if tag == "li":
            continue
        if len(base_elems) > 1:
            errors.append(
                f"基线中键 <{tag}> 重复出现 {len(base_elems)} 次，"
                "无法可靠比较"
            )
            continue
        head_elems = head_groups.get(tag)
        if not head_elems:
            errors.append(f"键 <{tag}> 被删除或重命名")
            continue
        if len(head_elems) > 1:
            errors.append(
                f"键 <{tag}> 在修改后重复出现 {len(head_elems)} 次"
                "（重复键）"
            )
            continue
        _compare_key(tag, base_elems[0], head_elems[0], errors)

    # A brand-new key that is duplicated in the head file is still a
    # duplicate key even though there is no baseline value to compare with.
    for tag, head_elems in head_groups.items():
        if tag == "li":
            continue
        if len(head_elems) > 1 and tag not in base_groups:
            errors.append(
                f"新增键 <{tag}> 重复出现 {len(head_elems)} 次"
                "（重复键）"
            )

    base_root_lis = base_groups.get("li", [])
    if base_root_lis:
        _compare_root_li_group(
            base_root_lis, head_groups.get("li", []), errors
        )

    return errors


def validate_new(head_bytes: bytes) -> list[str]:
    """Validate a newly added translation XML file (no baseline exists)."""
    errors: list[str] = []
    document, decode_errors = decode_document(head_bytes, "新增")
    errors.extend(decode_errors)
    if document is None:
        return errors

    root, parse_errors = parse_xml(document, "新增")
    errors.extend(parse_errors)
    if root is None:
        return errors

    groups = _group_children(root)
    for tag, elems in groups.items():
        if tag != "li" and len(elems) > 1:
            errors.append(f"新增文件中键 <{tag}> 重复出现 {len(elems)} 次（重复键）")
    return errors


def _run_git(repo: str, *args: str) -> bytes:
    command = ["git", "-C", repo, *args]
    result = subprocess.run(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise CheckError(f"git {' '.join(args)} 失败: {stderr}")
    return result.stdout


def _resolve_commit(repo: str, revision: str) -> str | None:
    if not revision or _is_zero_revision(revision):
        return None
    output = _run_git(repo, "rev-parse", "--verify", f"{revision}^{{commit}}")
    return output.decode("ascii").strip()


def _is_zero_revision(revision: str) -> bool:
    return bool(revision) and set(revision) == {"0"}


def _is_translation_xml(path: str, roots: Sequence[str]) -> bool:
    parts = path.split("/")
    return (
        len(parts) >= 2
        and parts[0] in roots
        and path.lower().endswith(".xml")
    )


def _changed_translation_files(
    repo: str, base: str, head: str, roots: Sequence[str]
) -> list[tuple[str, str]]:
    output = _run_git(
        repo,
        "diff",
        "--name-status",
        "--no-renames",
        "-z",
        f"{base}...{head}",
        "--",
        *roots,
    )
    tokens = output.split(b"\0")
    entries: list[tuple[str, str]] = []
    index = 0
    while index + 1 < len(tokens) and tokens[index]:
        status = tokens[index].decode("ascii", errors="replace")
        path = os.fsdecode(tokens[index + 1])
        index += 2
        if _is_translation_xml(path, roots):
            entries.append((status, path))
    return entries


def _list_all_translation_xml(
    repo: str, head: str, roots: Sequence[str]
) -> list[str]:
    output = _run_git(
        repo, "ls-tree", "-r", "-z", "--name-only", head, "--", *roots
    )
    paths = [os.fsdecode(token) for token in output.split(b"\0") if token]
    return [path for path in paths if _is_translation_xml(path, roots)]


def _read_blob(repo: str, revision: str, path: str) -> bytes:
    return _run_git(repo, "show", f"{revision}:{path}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Validate changed RimWorld translation XML files against a "
            "comparison base."
        )
    )
    parser.add_argument("--repo", default=".", help="repository path")
    parser.add_argument(
        "--base",
        default="",
        help=(
            "comparison base revision; empty or an all-zero SHA runs a "
            "full structural validation without preservation checks"
        ),
    )
    parser.add_argument(
        "--head",
        default="",
        help="target revision (default: HEAD)",
    )
    parser.add_argument(
        "--root",
        action="append",
        dest="roots",
        help=(
            "translation root directory; can be given more than once "
            f"(default: {' '.join(TRANSLATION_ROOTS)})"
        ),
    )
    args = parser.parse_args(argv)
    roots = tuple(args.roots) if args.roots else TRANSLATION_ROOTS

    try:
        head = _resolve_commit(args.repo, args.head or "HEAD")
        if head is None:
            raise CheckError(f"无法解析目标修订 {args.head!r}")
        base = _resolve_commit(args.repo, args.base)

        print("== 翻译 XML 校验 ==")
        print(f"目标提交: {head}")

        if base is None:
            print("比较基线: 无（执行全量结构校验，不执行保留性比较）")
            paths = _list_all_translation_xml(args.repo, head, roots)
            print(f"检查范围: {', '.join(roots)}")
            print(f"全量检查的翻译 XML: {len(paths)} 个")
            total_errors = 0
            for path in paths:
                errors = validate_new(_read_blob(args.repo, head, path))
                if errors:
                    total_errors += len(errors)
                    print(f"[失败] {path}")
                    for error in errors:
                        print(f"  - {error}")
            if total_errors:
                print(f"\n结果: 失败，发现 {total_errors} 个问题")
                return 1
            print("\n结果: 通过（全量结构校验）")
            return 0

        print(f"比较基线: {base}")
        print(f"检查范围: {', '.join(roots)}")
        changes = _changed_translation_files(args.repo, base, head, roots)
        if not changes:
            print(
                "没有增量 XML 需要检查（基线到目标提交未修改翻译 XML）。"
            )
            print(
                "校验脚本测试由 workflow 的独立步骤运行: "
                "python3 -m unittest discover -s .github/scripts/tests -v"
            )
            print("\n结果: 通过")
            return 0

        print(f"变更的翻译 XML: {len(changes)} 个")
        total_errors = 0
        for status, path in changes:
            if status == "D":
                errors = ["翻译 XML 文件被删除"]
            elif status in ("A", "C"):
                errors = validate_new(_read_blob(args.repo, head, path))
            elif status in ("M", "T"):
                base_bytes = _read_blob(args.repo, base, path)
                head_bytes = _read_blob(args.repo, head, path)
                errors = validate_pair(base_bytes, head_bytes)
            else:
                errors = [f"不支持的 git 变更状态 {status!r}"]
            if errors:
                total_errors += len(errors)
                print(f"[失败] {path} ({status})")
                for error in errors:
                    print(f"  - {error}")
            else:
                action = {"A": "新增", "C": "复制", "M": "修改", "T": "类型变更"}.get(
                    status, status
                )
                print(f"[通过] {path} ({action})")

        if total_errors:
            print(f"\n结果: 失败，发现 {total_errors} 个问题")
            return 1
        print("\n结果: 通过")
        return 0
    except CheckError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
