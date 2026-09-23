#!/usr/bin/env python3
"""生成流程自动化中未使用接口的 Markdown 报告。

规则：
- 扫描 api/*.py 的公开方法（排除 __init__ 和私有方法）
- 扫描 testcase/test_*.py 中 self.api_xxx.method() 调用
- 输出按模块汇总 + 逐项明细（含 URL 模板）
- 自动仅保留最近 N 份报告（默认 3）
"""

from __future__ import annotations

import argparse
import ast
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Set, Tuple

from AI.ai_analyzer import _load_ai_config, call_github_models_api


REPORT_PREFIX = "api_unused_interfaces_"


@dataclass
class ApiMethod:
    module: str
    module_menu_name: str
    class_name: str
    method_name: str
    chinese_name: str
    url_template: str
    called_self_methods: List[str]


@dataclass
class ExcludedInterfaceRule:
    module: str
    class_name: str
    method_name: str
    chinese_name: str
    reason: str


def _joined_str_to_template(node: ast.JoinedStr) -> str:
    parts: List[str] = []
    for v in node.values:
        if isinstance(v, ast.Constant) and isinstance(v.value, str):
            parts.append(v.value)
        elif isinstance(v, ast.FormattedValue):
            parts.append("{...}")
    return "".join(parts)


def _extract_url_template(func_node: ast.FunctionDef) -> str:
    candidates: List[Tuple[int, str]] = []

    for node in ast.walk(func_node):
        line_no = getattr(node, "lineno", 10**9)

        if isinstance(node, ast.Assign):
            targets = node.targets
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target] if isinstance(node.target, (ast.Name, ast.Attribute, ast.Subscript)) else []
            value = node.value
        else:
            continue

        for target in targets:
            if not isinstance(target, ast.Name) or target.id != "url":
                continue
            if isinstance(value, ast.JoinedStr):
                candidates.append((line_no, _joined_str_to_template(value)))
            elif isinstance(value, ast.Constant) and isinstance(value.value, str):
                candidates.append((line_no, value.value))

    if not candidates:
        return ""

    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


def _extract_called_self_methods(func_node: ast.FunctionDef) -> List[str]:
    """提取函数体内 self.xxx() 的方法调用（用于折算间接使用）。"""
    called: Set[str] = set()
    for node in ast.walk(func_node):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute):
            continue
        owner = node.func.value
        if isinstance(owner, ast.Name) and owner.id == "self":
            name = node.func.attr
            if name and not name.startswith("_"):
                called.add(name)
    return sorted(called)


def _extract_chinese_name(source_lines: List[str], func_node: ast.FunctionDef) -> str:
    """提取函数定义前紧邻的中文注释名称（如：# 新增产品）。"""
    line_idx = getattr(func_node, "lineno", 1) - 2
    if line_idx < 0:
        return ""

    comment_lines: List[str] = []
    while line_idx >= 0:
        text = source_lines[line_idx].strip()
        if not text:
            if comment_lines:
                break
            line_idx -= 1
            continue
        if text.startswith("#"):
            comment_lines.append(text.lstrip("#").strip())
            line_idx -= 1
            continue
        break

    if not comment_lines:
        return ""

    comment_lines.reverse()
    return " / ".join(x for x in comment_lines if x)


def _extract_module_menu_name(tree: ast.Module) -> str:
    """提取 api 文件最上方模块注释（文档字符串）作为模块菜单名称。"""
    doc = ast.get_docstring(tree) or ""
    return str(doc).strip()


def load_excluded_interface_rules(project_root: Path, exclude_file: str) -> List[ExcludedInterfaceRule]:
    """从独立清单文件加载需排除的接口规则。"""
    config_path = (project_root / exclude_file).resolve()
    if not config_path.exists():
        return []

    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    rules: List[ExcludedInterfaceRule] = []
    for item in data.get("excluded_interfaces", []):
        if not isinstance(item, dict):
            continue
        rules.append(
            ExcludedInterfaceRule(
                module=str(item.get("module") or "").strip(),
                class_name=str(item.get("class_name") or "").strip(),
                method_name=str(item.get("method_name") or "").strip(),
                chinese_name=str(item.get("chinese_name") or "").strip(),
                reason=str(item.get("reason") or "人工排除").strip(),
            )
        )
    return rules


def _is_method_excluded(method: ApiMethod, rules: List[ExcludedInterfaceRule]) -> Tuple[bool, str]:
    for rule in rules:
        if rule.module and rule.module != method.module:
            continue
        if rule.class_name and rule.class_name != method.class_name:
            continue

        # 优先按 method_name 匹配；method_name 写错时可回退到中文名匹配。
        hit_method_name = bool(rule.method_name and rule.method_name == method.method_name)
        hit_chinese_name = bool(rule.chinese_name and rule.chinese_name == method.chinese_name)
        if hit_method_name or hit_chinese_name:
            return True, rule.reason or "人工排除"
    return False, ""


def scan_api_methods(project_root: Path, api_glob: str, excluded_rules: List[ExcludedInterfaceRule]) -> List[ApiMethod]:
    api_methods: List[ApiMethod] = []
    excluded_module_by_rule: Dict[str, Dict[str, str]] = {}
    excluded_interface_by_rule: List[Dict[str, str]] = []

    # 按模块菜单名称过滤：包含“对外”或“初始化”的模块接口全部排除。
    exclude_keywords = ("对外", "初始化")

    for py_file in sorted(project_root.glob(api_glob)):
        if py_file.name == "__init__.py":
            continue

        source = py_file.read_text(encoding="utf-8")
        source_lines = source.splitlines()
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue

        module_menu_name = _extract_module_menu_name(tree)
        hit_keyword = next((kw for kw in exclude_keywords if kw in module_menu_name), "")
        if hit_keyword:
            excluded_module_by_rule[py_file.name] = {
                "menu_name": module_menu_name,
                "reason": f"模块菜单名包含“{hit_keyword}”",
            }
            continue

        module_name = py_file.name
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            for child in node.body:
                if not isinstance(child, ast.FunctionDef):
                    continue
                if child.name == "__init__" or child.name.startswith("_"):
                    continue
                method = ApiMethod(
                    module=module_name,
                    module_menu_name=module_menu_name,
                    class_name=node.name,
                    method_name=child.name,
                    chinese_name=_extract_chinese_name(source_lines, child),
                    url_template=_extract_url_template(child),
                    called_self_methods=_extract_called_self_methods(child),
                )

                excluded, reason = _is_method_excluded(method, excluded_rules)
                if excluded:
                    excluded_interface_by_rule.append(
                        {
                            "module": method.module,
                            "menu_name": method.module_menu_name or "未标注",
                            "class_name": method.class_name,
                            "method_name": method.method_name,
                            "chinese_name": method.chinese_name or "未标注",
                            "url_template": method.url_template,
                            "reason": reason,
                        }
                    )
                    continue

                api_methods.append(method)

    setattr(scan_api_methods, "excluded_module_by_rule", excluded_module_by_rule)
    setattr(scan_api_methods, "excluded_interface_by_rule", excluded_interface_by_rule)

    return api_methods


def _collect_alias_map_from_test_class(class_node: ast.ClassDef) -> Dict[str, str]:
    alias_to_class: Dict[str, str] = {}

    for node in ast.walk(class_node):
        if not isinstance(node, ast.Assign):
            continue
        if not isinstance(node.value, ast.Call):
            continue

        class_name = ""
        func_node = node.value.func
        if isinstance(func_node, ast.Attribute):
            class_name = func_node.attr
        elif isinstance(func_node, ast.Name):
            class_name = func_node.id

        if not class_name:
            continue

        for target in node.targets:
            if not isinstance(target, ast.Attribute):
                continue
            if not isinstance(target.value, ast.Name):
                continue
            if target.value.id not in {"cls", "self"}:
                continue
            alias_to_class[target.attr] = class_name

    return alias_to_class


def scan_test_usages(project_root: Path, test_glob: str) -> Dict[str, Set[str]]:
    used_methods: Dict[str, Set[str]] = {}

    for py_file in sorted(project_root.glob(test_glob)):
        source = py_file.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue

        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue

            alias_map = _collect_alias_map_from_test_class(node)

            for call in ast.walk(node):
                if not isinstance(call, ast.Call):
                    continue
                if not isinstance(call.func, ast.Attribute):
                    continue

                method_name = call.func.attr
                owner = call.func.value

                # 场景1：self.api_xxx.method()
                if isinstance(owner, ast.Attribute) and isinstance(owner.value, ast.Name) and owner.value.id == "self":
                    alias_name = owner.attr
                    class_name = alias_map.get(alias_name)
                    if class_name:
                        used_methods.setdefault(class_name, set()).add(method_name)

                # 场景2：直接链式调用 module.Class(...).method()
                # 示例：api_login.ApiLogin().login()
                if isinstance(owner, ast.Call):
                    ctor_func = owner.func
                    if isinstance(ctor_func, ast.Attribute):
                        direct_class_name = ctor_func.attr
                        if direct_class_name:
                            used_methods.setdefault(direct_class_name, set()).add(method_name)

        # 场景3：模块级直接链式调用 module.Class(...).method()
        # 示例：base_headers = {"Authorization": api_login.ApiLogin().login()}
        for call in ast.walk(tree):
            if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
                continue
            method_name = call.func.attr
            owner = call.func.value
            if not isinstance(owner, ast.Call):
                continue
            ctor_func = owner.func
            if isinstance(ctor_func, ast.Attribute):
                direct_class_name = ctor_func.attr
                if direct_class_name:
                    used_methods.setdefault(direct_class_name, set()).add(method_name)

    return used_methods


def cleanup_old_reports(output_dir: Path, keep: int) -> None:
    """只保留最新 keep 份报告。"""
    reports = sorted(output_dir.glob(f"{REPORT_PREFIX}*.md"), reverse=True)
    for old_file in reports[keep:]:
        try:
            old_file.unlink()
        except OSError:
            pass


def build_ai_prompt(report_markdown: str) -> str:
    """构建用于接口覆盖报告二次分析的提示词。"""
    excerpt = report_markdown[:12000]
    return (
        "你是资深接口测试专家。请基于以下‘流程业务接口未使用清单报告’输出中文分析，"
        "重点给出可执行建议，避免泛泛而谈。\n\n"
        "输出结构固定为：\n"
        "1) 覆盖率结论（简洁）\n"
        "2) 高优先级补测接口 Top5（按业务风险排序，给出理由）\n"
        "3) 可暂缓项（说明暂缓原因）\n\n"
        "要求：仅依据输入报告内容作结论；如证据不足请明确写‘证据不足’。"
        "禁止输出‘建议本周补齐计划’或按天拆分计划。\n\n"
        "==== 报告内容开始 ====\n"
        f"{excerpt}\n"
        "==== 报告内容结束 ====\n"
    )


def append_ai_analysis(markdown: str) -> str:
    """调用 AI 并将分析结果追加到同一份报告末尾。"""
    ai_cfg = _load_ai_config()
    lines: List[str] = ["", "## AI分析", ""]

    model_name = ai_cfg.get("model", "unknown")
    lines.append(f"- 模型: `{model_name}`")

    if not ai_cfg.get("token"):
        lines.append("- 状态: 未配置 `LLM API Key`，已跳过 AI 分析")
        return markdown.rstrip() + "\n" + "\n".join(lines) + "\n"

    try:
        prompt = build_ai_prompt(markdown)
        analysis_text = call_github_models_api(prompt, ai_cfg)
        lines.append("- 状态: 分析完成")
        lines.append("")
        lines.append(analysis_text.strip() if analysis_text else "AI 返回为空")
    except Exception as e:
        lines.append(f"- 状态: 分析失败（不影响主报告）：{e}")

    return markdown.rstrip() + "\n" + "\n".join(lines) + "\n"


def build_markdown(api_methods: List[ApiMethod], used_map: Dict[str, Set[str]]) -> str:
    methods_by_module: Dict[str, List[ApiMethod]] = {}
    for item in api_methods:
        methods_by_module.setdefault(item.module, []).append(item)

    excluded_module_by_rule = getattr(scan_api_methods, "excluded_module_by_rule", {})
    excluded_interface_by_rule = getattr(scan_api_methods, "excluded_interface_by_rule", [])

    total_count = len(api_methods)
    used_count = 0
    unused_count = 0

    module_summary: Dict[str, Dict[str, str]] = {}
    detail_blocks: List[str] = []

    # 构建每个类的方法调用图：method -> {called methods}
    class_call_graph: Dict[str, Dict[str, Set[str]]] = {}
    for item in api_methods:
        class_call_graph.setdefault(item.class_name, {})
        class_call_graph[item.class_name].setdefault(item.method_name, set())
        class_call_graph[item.class_name][item.method_name].update(item.called_self_methods)

    # 基于调用图扩展“已使用方法”：若A被测试直接调用且A内部调用B，则B也视为已使用。
    effective_used_map: Dict[str, Set[str]] = {k: set(v) for k, v in used_map.items()}
    for class_name, method_set in list(effective_used_map.items()):
        graph = class_call_graph.get(class_name, {})
        queue = list(method_set)
        while queue:
            cur = queue.pop(0)
            for nxt in graph.get(cur, set()):
                if nxt not in method_set:
                    method_set.add(nxt)
                    queue.append(nxt)

    for module_name in sorted(methods_by_module.keys()):
        items = sorted(
            methods_by_module[module_name],
            key=lambda x: (x.class_name, x.method_name),
        )

        module_used = 0
        module_unused = 0

        lines: List[str] = []
        lines.append(f"## 模块 `{module_name}`")
        lines.append(f"- 模块菜单名称: {items[0].module_menu_name or '未标注'}")

        class_groups: Dict[str, List[ApiMethod]] = {}
        for m in items:
            class_groups.setdefault(m.class_name, []).append(m)

        for class_name in sorted(class_groups.keys()):
            class_items = sorted(class_groups[class_name], key=lambda x: x.method_name)
            class_used_methods: List[str] = []
            class_unused_items: List[ApiMethod] = []

            for method in class_items:
                is_used = method.method_name in effective_used_map.get(class_name, set())
                if is_used:
                    used_count += 1
                    module_used += 1
                    class_used_methods.append(method.method_name)
                else:
                    unused_count += 1
                    module_unused += 1
                    class_unused_items.append(method)

            lines.append(f"### 类 `{class_name}`")
            lines.append(f"- 公开方法总数: {len(class_items)}")
            lines.append(f"- 流程脚本已使用: {len(class_used_methods)}")
            lines.append(f"- 流程脚本未使用: {len(class_unused_items)}")
            if class_used_methods:
                used_text = "、".join(f"`{x}`" for x in class_used_methods)
                lines.append(f"- 已使用方法: {used_text}")
            else:
                lines.append("- 已使用方法: 无")

            if class_used_methods:
                lines.append("- 已使用方法明细:")
                for method in class_items:
                    if method.method_name not in class_used_methods:
                        continue
                    cn_name = method.chinese_name or "未标注"
                    lines.append(
                        f"  - `{method.method_name}`（中文名：{cn_name}，模块：`{method.module}`）"
                    )

            if class_unused_items:
                lines.append("- 未使用方法:")
                for method in class_unused_items:
                    cn_name = method.chinese_name or "未标注"
                    suffix = f" -> `{method.url_template}`" if method.url_template else ""
                    lines.append(
                        f"  - `{method.method_name}`（中文名：{cn_name}，模块：`{method.module}`）{suffix}"
                    )
            else:
                lines.append("- 未使用方法: 无")
            lines.append("")

        module_summary[module_name] = {
            "unused_count": str(module_unused),
            "menu_name": items[0].module_menu_name or "未标注",
        }
        detail_blocks.append("\n".join(lines).rstrip())

    lines: List[str] = []
    lines.append("# 流程业务接口未使用清单报告")
    lines.append("")
    lines.append("## 扫描信息")
    lines.append(f"- 生成时间: `{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`")
    lines.append("- 统计范围: `api/*.py` 公开方法 vs `testcase/test_*.py` 调用")
    lines.append("- 调用识别规则:")
    lines.append("  - `self.api_xxx.method()`")
    lines.append("  - `module.Class(...).method()`（类内或模块级）")
    lines.append("  - 同类方法内 `self.other_method()` 间接调用折算为已使用")
    lines.append("- 过滤规则:")
    lines.append("  - 排除模块菜单名称包含“对外”的全部接口")
    lines.append("  - 排除模块菜单名称包含“初始化”的全部接口")
    lines.append("  - 排除 `config/api_excluded_interfaces.json` 中登记的接口")
    lines.append("- 报告保留策略: 每次执行仅保留最近 `1` 份同类报告")
    lines.append("")
    lines.append("## 总览")
    lines.append(f"- API 公开方法总数: {total_count}")
    lines.append(f"- 流程脚本已使用方法数: {used_count}")
    lines.append(f"- 流程脚本未使用方法数: {unused_count}")
    lines.append("")
    lines.append("## 各模块未使用数量")
    for module_name in sorted(module_summary.keys()):
        lines.append(
            f"- `{module_name}`（模块菜单：{module_summary[module_name]['menu_name']}）: "
            f"{module_summary[module_name]['unused_count']}"
        )
    lines.append("")

    lines.append("## 已按规则排除的模块")
    if excluded_module_by_rule:
        for module_name in sorted(excluded_module_by_rule.keys()):
            menu_name = excluded_module_by_rule[module_name].get("menu_name") or "未标注"
            reason = excluded_module_by_rule[module_name].get("reason") or "命中过滤规则"
            lines.append(f"- `{module_name}`（模块菜单：{menu_name}）: {reason}")
    else:
        lines.append("- 无")
    lines.append("")

    lines.append("## 已按清单排除的接口")
    if excluded_interface_by_rule:
        for item in sorted(
            excluded_interface_by_rule,
            key=lambda x: (x.get("module", ""), x.get("class_name", ""), x.get("method_name", "")),
        ):
            url_suffix = f" -> `{item['url_template']}`" if item.get("url_template") else ""
            lines.append(
                f"- `{item['module']}`.`{item['class_name']}`.`{item['method_name']}`"
                f"（中文名：{item['chinese_name']}，模块菜单：{item['menu_name']}）: {item['reason']}{url_suffix}"
            )
    else:
        lines.append("- 无")
    lines.append("")

    lines.append("## 逐模块明细")
    lines.append("")

    for block in detail_blocks:
        lines.append(block)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="生成流程脚本未使用接口报告（Markdown）")
    parser.add_argument("--project-root", default=".", help="项目根目录")
    parser.add_argument("--api-glob", default="api/*.py", help="API 封装文件匹配规则")
    parser.add_argument("--test-glob", default="testcase/test_*.py", help="流程测试文件匹配规则")
    parser.add_argument("--output-dir", default="report", help="报告输出目录")
    parser.add_argument(
        "--exclude-file",
        default="config/api_excluded_interfaces.json",
        help="接口级排除清单文件（相对项目根目录）",
    )
    parser.add_argument("--keep", type=int, default=1, help="保留最近 N 份报告")
    parser.add_argument("--no-ai", action="store_true", help="不追加 AI 分析章节")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    project_root = Path(args.project_root).resolve()
    output_dir = (project_root / args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    excluded_rules = load_excluded_interface_rules(project_root, args.exclude_file)
    api_methods = scan_api_methods(project_root, args.api_glob, excluded_rules)
    used_map = scan_test_usages(project_root, args.test_glob)

    markdown = build_markdown(api_methods, used_map)
    if not args.no_ai:
        markdown = append_ai_analysis(markdown)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = output_dir / f"{REPORT_PREFIX}{timestamp}.md"
    report_path.write_text(markdown, encoding="utf-8")

    cleanup_old_reports(output_dir, keep=max(1, args.keep))

    print(f"报告已生成: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

