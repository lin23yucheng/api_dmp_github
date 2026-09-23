"""
对比两份 OpenAPI JSON 文档，生成差异 Markdown 报告；
当检测到变更时，同时产出 `api_new` 接口封装与带 `_1` 后缀的测试脚本副本。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from AI.ai_analyzer import _load_ai_config, call_github_models_api


WORKSPACE_ROOT = Path(__file__).resolve().parent
DEFAULT_OLD_DOC = WORKSPACE_ROOT / "api" / "DMP接口文档.json"
DEFAULT_NEW_DOC = WORKSPACE_ROOT / "api" / "DMP接口文档_1.json"
DEFAULT_REPORT_DIR = WORKSPACE_ROOT / "report"
DEFAULT_TESTCASE_DIR = WORKSPACE_ROOT / "testcase"
DEFAULT_API_DIR = WORKSPACE_ROOT / "api"
DEFAULT_API_NEW_DIR = WORKSPACE_ROOT / "api_new"
REPORT_FILE_GLOB = "api_doc_diff_*.md"

API_FILE_GLOB = "api_*.py"
LLM_SUGGESTION_BEGIN = "# === LLM 新增接口建议（自动生成，需人工确认） BEGIN ==="
LLM_SUGGESTION_END = "# === LLM 新增接口建议 END ==="


@dataclass
class EndpointSpec:
    key: str  # 接口唯一键：METHOD + PATH
    method: str
    path: str
    operation_id: str
    summary: str
    parameters: List[Dict[str, Any]]
    request_body_fields: List[str]
    request_schema_ref: str
    tags: List[str]


def _load_json(path: Path) -> Dict[str, Any]:
    """读取 UTF-8 JSON 文件并返回字典对象。"""
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _ref_name(ref: str) -> str:
    """从 OpenAPI $ref 中提取末尾 schema 名称。"""
    if not ref:
        return ""
    return ref.split("/")[-1]


def _schema_type_name(schema: Dict[str, Any]) -> str:
    """将 schema 转成可读类型名，兼容 $ref 与 array[item] 表达。"""
    if "$ref" in schema:
        return _ref_name(schema["$ref"])
    schema_type = schema.get("type", "object")
    if schema_type == "array":
        item = schema.get("items", {})
        if isinstance(item, dict):
            if "$ref" in item:
                return f"array[{_ref_name(item['$ref'])}]"
            if "type" in item:
                return f"array[{item['type']}]"
        return "array"
    return str(schema_type)


def _resolve_ref_schema(schema: Dict[str, Any], components: Dict[str, Any]) -> Dict[str, Any]:
    """若 schema 为 $ref，返回 components 中解析后的真实 schema。"""
    if "$ref" not in schema:
        return schema
    ref = schema["$ref"]
    name = _ref_name(ref)
    return components.get(name, {})


def _collect_object_fields(
    schema: Dict[str, Any],
    components: Dict[str, Any],
    prefix: str = "",
    required_fields: Optional[Set[str]] = None,
    depth: int = 0,
    seen: Optional[Set[str]] = None,
) -> List[str]:
    """递归提取对象字段清单，输出 `path:type`，必填字段附带 `*` 标记。"""
    if seen is None:
        seen = set()

    if depth > 6:
        return []

    # 优先展开 $ref，确保后续字段提取基于真实 schema。
    if "$ref" in schema:
        ref = schema["$ref"]
        if ref in seen:
            return []
        seen.add(ref)
        resolved = _resolve_ref_schema(schema, components)
        return _collect_object_fields(
            resolved,
            components,
            prefix=prefix,
            required_fields=set(resolved.get("required", []) if isinstance(resolved, dict) else []),
            depth=depth + 1,
            seen=seen,
        )

    current_required = required_fields or set(schema.get("required", []))
    schema_type = schema.get("type", "object")
    fields: List[str] = []

    if schema_type == "object":
        properties = schema.get("properties", {}) or {}
        for field_name in sorted(properties.keys()):
            field_schema = properties[field_name]
            field_prefix = f"{prefix}.{field_name}" if prefix else field_name
            mark = "*" if field_name in current_required else ""

            if isinstance(field_schema, dict) and (field_schema.get("type") == "object" or "$ref" in field_schema):
                nested = _collect_object_fields(
                    field_schema,
                    components,
                    prefix=field_prefix,
                    required_fields=None,
                    depth=depth + 1,
                    seen=set(seen),
                )
                if nested:
                    fields.extend(nested)
                else:
                    fields.append(f"{field_prefix}{mark}:object")
            elif isinstance(field_schema, dict) and field_schema.get("type") == "array":
                item_type = "array"
                items = field_schema.get("items", {})
                if isinstance(items, dict):
                    if "$ref" in items:
                        item_type = f"array[{_ref_name(items['$ref'])}]"
                    elif "type" in items:
                        item_type = f"array[{items['type']}]"
                fields.append(f"{field_prefix}{mark}:{item_type}")
            else:
                field_type = _schema_type_name(field_schema if isinstance(field_schema, dict) else {})
                fields.append(f"{field_prefix}{mark}:{field_type}")
    else:
        fields.append(f"{prefix or '<root>'}:{schema_type}")

    return fields


def _extract_parameters(operation: Dict[str, Any]) -> List[Dict[str, Any]]:
    """提取并标准化 operation 的 parameters 列表。"""
    result: List[Dict[str, Any]] = []
    for p in operation.get("parameters", []) or []:
        schema = p.get("schema", {}) or {}
        result.append(
            {
                "name": p.get("name", ""),
                "in": p.get("in", ""),
                "required": bool(p.get("required", False)),
                "type": _schema_type_name(schema),
            }
        )
    result.sort(key=lambda x: (x["in"], x["name"]))
    return result


def _extract_request_schema(operation: Dict[str, Any]) -> Tuple[str, List[str]]:
    """提取 application/json 的请求体 schema 引用（当前仅返回 schema_ref）。"""
    request_body = operation.get("requestBody", {}) or {}
    content = request_body.get("content", {}) or {}
    app_json = content.get("application/json", {}) or {}
    schema = app_json.get("schema", {}) or {}
    schema_ref = schema.get("$ref", "")
    return schema_ref, []


def _extract_endpoint_specs(openapi_doc: Dict[str, Any]) -> Dict[str, EndpointSpec]:
    """将 OpenAPI paths 展平为 endpoint spec 映射，key 为 `METHOD PATH`。"""
    paths = openapi_doc.get("paths", {}) or {}
    components = ((openapi_doc.get("components", {}) or {}).get("schemas", {}) or {})

    specs: Dict[str, EndpointSpec] = {}
    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue

        for method in ["get", "post", "put", "delete", "patch", "options", "head"]:
            operation = path_item.get(method)
            if not isinstance(operation, dict):
                continue

            key = f"{method.upper()} {path}"
            params = _extract_parameters(operation)
            request_schema_ref, _ = _extract_request_schema(operation)

            request_fields: List[str] = []
            if request_schema_ref:
                request_fields = _collect_object_fields({"$ref": request_schema_ref}, components)

            specs[key] = EndpointSpec(
                key=key,
                method=method.upper(),
                path=path,
                operation_id=str(operation.get("operationId", "")),
                summary=str(operation.get("summary", "")),
                parameters=params,
                request_body_fields=sorted(request_fields),
                request_schema_ref=request_schema_ref,
                tags=[str(t) for t in (operation.get("tags") or [])],
            )
    return specs


def _compare_specs(old_specs: Dict[str, EndpointSpec], new_specs: Dict[str, EndpointSpec]) -> Dict[str, Any]:
    """比较新旧 endpoint spec，返回新增/删除/变更/地址迁移结果。"""
    old_keys = set(old_specs.keys())
    new_keys = set(new_specs.keys())

    added = sorted(new_keys - old_keys)
    removed = sorted(old_keys - new_keys)
    common = sorted(old_keys & new_keys)

    changed: List[Dict[str, Any]] = []
    for key in common:
        old = old_specs[key]
        new = new_specs[key]
        field_changes: List[str] = []

        if old.parameters != new.parameters:
            field_changes.append("parameters")
        if old.request_schema_ref != new.request_schema_ref:
            field_changes.append("request_schema")
        if old.request_body_fields != new.request_body_fields:
            field_changes.append("request_fields")
        if old.summary != new.summary:
            field_changes.append("summary")

        if field_changes:
            changed.append(
                {
                    "key": key,
                    "operationId": new.operation_id or old.operation_id,
                    "changed_fields": field_changes,
                    "old": old,
                    "new": new,
                }
            )

    # 地址迁移判定：同一 operationId 在新旧文档中的 METHOD+PATH 集合不同。
    old_op_map: Dict[str, Set[str]] = {}
    new_op_map: Dict[str, Set[str]] = {}
    for key, spec in old_specs.items():
        if spec.operation_id:
            old_op_map.setdefault(spec.operation_id, set()).add(key)
    for key, spec in new_specs.items():
        if spec.operation_id:
            new_op_map.setdefault(spec.operation_id, set()).add(key)

    address_changed: List[Dict[str, Any]] = []
    for op_id in sorted(set(old_op_map.keys()) & set(new_op_map.keys())):
        old_set = old_op_map[op_id]
        new_set = new_op_map[op_id]
        if old_set != new_set:
            address_changed.append(
                {
                    "operationId": op_id,
                    "old": sorted(old_set),
                    "new": sorted(new_set),
                }
            )

    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "address_changed": address_changed,
    }


def _fmt_param_list(params: List[Dict[str, Any]]) -> str:
    """将参数列表格式化为 Markdown 可读文本。"""
    if not params:
        return "无"
    return ", ".join(
        f"{p['in']}:{p['name']}({p['type']}, {'required' if p['required'] else 'optional'})"
        for p in params
    )


def _fmt_field_diff(old_fields: List[str], new_fields: List[str]) -> str:
    """计算请求字段增删差异，并输出单行摘要。"""
    old_set = set(old_fields)
    new_set = set(new_fields)
    added = sorted(new_set - old_set)
    removed = sorted(old_set - new_set)
    parts: List[str] = []
    if added:
        parts.append("新增字段: " + "; ".join(added))
    if removed:
        parts.append("删除字段: " + "; ".join(removed))
    return " | ".join(parts) if parts else "无"


def _generate_markdown(
    old_doc_name: str,
    new_doc_name: str,
    diff_result: Dict[str, Any],
    report_time: str,
) -> str:
    """根据 diff 结果生成完整 Markdown 报告正文。"""
    added = diff_result["added"]
    removed = diff_result["removed"]
    changed = diff_result["changed"]
    address_changed = diff_result["address_changed"]

    lines: List[str] = []
    lines.append("# DMP API文档差异报告")
    lines.append("")
    lines.append(f"- 生成时间: {report_time}")
    lines.append(f"- 旧文档: `{old_doc_name}`")
    lines.append(f"- 新文档: `{new_doc_name}`")
    lines.append("")
    lines.append("## 变更总览")
    lines.append("")
    lines.append(f"- 接口地址变更(operationId匹配): **{len(address_changed)}**")
    lines.append(f"- 新增接口: **{len(added)}**")
    lines.append(f"- 删除接口: **{len(removed)}**")
    lines.append(f"- 既有接口定义变更(参数/请求体/摘要): **{len(changed)}**")
    lines.append("")

    lines.append("## 接口地址变更")
    lines.append("")
    if not address_changed:
        lines.append("无")
        lines.append("")
    else:
        for item in address_changed:
            lines.append(f"- operationId: `{item['operationId']}`")
            lines.append(f"  - 旧地址: `{', '.join(item['old'])}`")
            lines.append(f"  - 新地址: `{', '.join(item['new'])}`")
        lines.append("")

    lines.append("## 新增接口")
    lines.append("")
    if not added:
        lines.append("无")
        lines.append("")
    else:
        for key in added:
            lines.append(f"- `{key}`")
        lines.append("")

    lines.append("## 删除接口")
    lines.append("")
    if not removed:
        lines.append("无")
        lines.append("")
    else:
        for key in removed:
            lines.append(f"- `{key}`")
        lines.append("")

    lines.append("## 既有接口变更详情")
    lines.append("")
    if not changed:
        lines.append("无")
        lines.append("")
    else:
        for item in changed:
            old_spec: EndpointSpec = item["old"]
            new_spec: EndpointSpec = item["new"]
            lines.append(f"### `{item['key']}`")
            lines.append("")
            lines.append(f"- operationId: `{item['operationId']}`")
            lines.append(f"- 变更项: `{', '.join(item['changed_fields'])}`")

            if "parameters" in item["changed_fields"]:
                lines.append(f"- 旧参数: { _fmt_param_list(old_spec.parameters) }")
                lines.append(f"- 新参数: { _fmt_param_list(new_spec.parameters) }")

            if "request_schema" in item["changed_fields"]:
                lines.append(f"- 旧请求体Schema: `{old_spec.request_schema_ref or '无'}`")
                lines.append(f"- 新请求体Schema: `{new_spec.request_schema_ref or '无'}`")

            if "request_fields" in item["changed_fields"]:
                lines.append(f"- 请求字段差异: {_fmt_field_diff(old_spec.request_body_fields, new_spec.request_body_fields)}")

            if "summary" in item["changed_fields"]:
                lines.append(f"- 旧摘要: `{old_spec.summary}`")
                lines.append(f"- 新摘要: `{new_spec.summary}`")

            lines.append("")

    lines.append("## 结论")
    lines.append("")
    if len(address_changed) + len(added) + len(removed) + len(changed) == 0:
        lines.append("两份文档未发现接口差异。")
    else:
        lines.append("发现接口变更，请根据上述差异更新脚本与回归范围。")

    return "\n".join(lines) + "\n"


def _build_added_endpoint_todo_lines(added_specs: List[EndpointSpec]) -> str:
    """为测试副本生成“新增接口待补充”注释块。"""
    if not added_specs:
        return ""

    lines = [
        "# 新增接口待补充说明",
        "# 以下接口来自新文档，已生成到 api_new 对应模块；请根据业务流程补充测试步骤。",
    ]
    for spec in added_specs:
        summary = spec.summary or "无摘要"
        lines.append(f"# - {spec.method} {spec.path} | {summary} | operationId={spec.operation_id or '无'}")
    return "\n".join(lines) + "\n\n"


def _build_changed_endpoint_todo_lines(changed_items: List[Dict[str, Any]]) -> str:
    """为测试副本生成“变更接口待处理”注释块。"""
    if not changed_items:
        return ""

    lines = [
        "# 变更接口待处理说明",
        "# 以下接口在新文档中发生了参数/请求体/摘要变化，请检查对应测试步骤。",
    ]
    for item in changed_items:
        key = item.get("key", "")
        fields = ", ".join(item.get("changed_fields") or []) or "未知"
        op_id = item.get("operationId") or "无"
        lines.append(f"# - {key} | operationId={op_id} | 变更项={fields}")
    return "\n".join(lines) + "\n\n"


def _build_removed_endpoint_todo_lines(removed_specs: List[EndpointSpec]) -> str:
    """为测试副本生成“删除接口待处理”注释块。"""
    if not removed_specs:
        return ""

    lines = [
        "# 删除接口待处理说明",
        "# 以下接口在新文档中已删除，请删除或替换相关测试步骤。",
    ]
    for spec in removed_specs:
        lines.append(
            f"# - {spec.method} {spec.path} | {spec.summary or '无摘要'} | operationId={spec.operation_id or '无'}"
        )
    return "\n".join(lines) + "\n\n"


def _annotate_testcase_calls(
    text: str,
    removed_method_names: Set[str],
    changed_method_names: Set[str],
    method_param_changes: Dict[str, Dict[str, Set[str]]],
) -> str:
    """为测试调用打自动提示，并尝试移除已删除的简单命名参数。"""
    if not removed_method_names and not changed_method_names:
        return text

    lines = text.splitlines()
    out: List[str] = []
    call_pattern = re.compile(r"\.(\w+)\s*\(")

    for line in lines:
        match = call_pattern.search(line)
        if not match:
            out.append(line)
            continue

        method_name = match.group(1)
        indent = re.match(r"\s*", line).group(0)
        if method_name in removed_method_names:
            out.append(f"{indent}# [AUTO] 此调用对应接口已在新文档删除，请手工删除或替换")
        elif method_name in changed_method_names:
            out.append(f"{indent}# [AUTO] 此调用对应接口参数/请求体已变更，请核对调用参数")

            param_change = method_param_changes.get(method_name, {"removed": set(), "added": set()})
            removed_params = set(param_change.get("removed") or set())
            added_params = set(param_change.get("added") or set())

            if removed_params:
                original_line = line
                for p in sorted(removed_params):
                    # 仅自动删除简单命名参数（值中不含括号）；复杂表达式仅给出提示。
                    value_pattern = r"[^,\(\)\n]+"
                    # 删除中间参数，例如：p=1,
                    line = re.sub(rf"\b{re.escape(p)}\s*=\s*{value_pattern}\s*,\s*", "", line)
                    # 删除末尾参数，例如：, p=1
                    line = re.sub(rf",\s*\b{re.escape(p)}\s*=\s*{value_pattern}\s*(?=\))", "", line)
                    # 删除唯一参数，例如：p=1
                    line = re.sub(rf"\b{re.escape(p)}\s*=\s*{value_pattern}\s*(?=\))", "", line)
                if line != original_line:
                    out.append(f"{indent}# [AUTO] 已尝试移除已删除参数: {', '.join(sorted(removed_params))}")

            if added_params:
                out.append(f"{indent}# [AUTO] 新增参数待补充: {', '.join(sorted(added_params))}")

        out.append(line)

    return "\n".join(out) + "\n"


def _clone_testcase_file(
    src: Path,
    dst: Path,
    added_specs: List[EndpointSpec],
    removed_specs: List[EndpointSpec],
    changed_items: List[Dict[str, Any]],
    removed_method_names: Set[str],
    changed_method_names: Set[str],
    method_param_changes: Dict[str, Dict[str, Set[str]]],
) -> None:
    """克隆测试文件为 `_1` 副本，并注入待办信息与调用注释。"""
    text = src.read_text(encoding="utf-8")

    # 重命名测试类，避免 pytest 节点重名并提升报告可读性。
    class_match = re.search(r"class\s+(\w+)\s*:", text)
    if class_match:
        old_class = class_match.group(1)
        new_class = f"{old_class}_1"
        text = re.sub(rf"\b{re.escape(old_class)}\b", new_class, text)

    header = (
        "\"\"\"\n"
        "基于 DMP接口文档_1.json 自动生成的测试脚本副本。\n"
        "由 generate_api_doc_diff_and_tests.py 自动生成\n"
        "\"\"\"\n\n"
    )

    # 在保留原脚本主体的前提下，追加自动生成说明与待办信息。
    text = re.sub(r"\bfrom\s+api\s+import\b", "from api_new import", text)
    todo_block = (
        _build_added_endpoint_todo_lines(added_specs)
        + _build_removed_endpoint_todo_lines(removed_specs)
        + _build_changed_endpoint_todo_lines(changed_items)
    )

    text = _annotate_testcase_calls(
        text=text,
        removed_method_names=removed_method_names,
        changed_method_names=changed_method_names,
        method_param_changes=method_param_changes,
    )

    if text.lstrip().startswith('"""'):
        dst.write_text(header + todo_block + text, encoding="utf-8")
    else:
        dst.write_text(header + todo_block + text, encoding="utf-8")


def _copy_api_package_to_new(api_dir: Path, api_new_dir: Path) -> List[str]:
    """复制 `api` 到 `api_new`，并把内部 `from api import` 改写为 `from api_new import`。"""
    if api_new_dir.exists():
        shutil.rmtree(api_new_dir)
    api_new_dir.mkdir(parents=True, exist_ok=True)

    copied_files: List[str] = []
    for src in sorted(api_dir.glob("*.py")):
        dst = api_new_dir / src.name
        shutil.copy2(src, dst)
        if dst.name != "__init__.py":
            text = dst.read_text(encoding="utf-8")
            text = re.sub(r"\bfrom\s+api\s+import\b", "from api_new import", text)
            dst.write_text(text, encoding="utf-8")
        copied_files.append(str(dst))
    return copied_files


def _split_endpoint_key(endpoint_key: str) -> Tuple[str, str]:
    """拆分 `METHOD PATH` 字符串。"""
    method, path = endpoint_key.split(" ", 1)
    return method.strip().upper(), path.strip()


def _to_snake_case(name: str) -> str:
    """将任意名称归一化为 snake_case。"""
    if not name:
        return "auto_generated"
    name = re.sub(r"[^0-9a-zA-Z_]+", "_", name)
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    name = re.sub(r"_+", "_", name).strip("_")
    return (name or "auto_generated").lower()


def _to_pascal_case(name: str) -> str:
    """将任意名称转换为 PascalCase。"""
    parts = [p for p in _to_snake_case(name).split("_") if p]
    if not parts:
        return "AutoGenerated"
    return "".join(p.capitalize() for p in parts)


def _path_prefix_for_match(path: str) -> str:
    """从接口路径提取模块匹配前缀。"""
    segments = [seg for seg in path.split("/") if seg]
    if not segments:
        return "/"
    # feign 接口按二级路径前缀分组，其余接口按一级前缀分组。
    if segments[0] == "feign" and len(segments) >= 2:
        return f"/{segments[0]}/{segments[1]}"
    return f"/{segments[0]}"


def _discover_api_module_map(api_dir: Path) -> List[Dict[str, str]]:
    """扫描 API 封装文件，构建“路径前缀 -> 模块/类”映射。"""
    module_map: List[Dict[str, str]] = []
    for file_path in sorted(api_dir.glob(API_FILE_GLOB)):
        if file_path.name == "api_login.py":
            continue

        text = file_path.read_text(encoding="utf-8")
        class_match = re.search(r"class\s+(\w+)\s*:", text)
        if not class_match:
            continue
        class_name = class_match.group(1)

        paths = re.findall(r'f"\{env\}(/[^"\n]+)"', text)
        prefixes = sorted({_path_prefix_for_match(p) for p in paths})
        for prefix in prefixes:
            module_map.append(
                {
                    "prefix": prefix,
                    "file_name": file_path.name,
                    "class_name": class_name,
                }
            )

    # 长前缀优先匹配，避免 `/feign` 抢占 `/feign/space`。
    module_map.sort(key=lambda x: len(x["prefix"]), reverse=True)
    return module_map


def _create_new_api_module_file(
    api_new_dir: Path,
    file_name: str,
    class_name: str,
    module_desc: str,
) -> None:
    """按模板创建新的 `api_new/api_xxx.py` 模块（若文件不存在）。"""
    file_path = api_new_dir / file_name
    if file_path.exists():
        return

    content = (
        "\"\"\"\n"
        f"{module_desc}接口（自动生成）\n"
        "\"\"\"\n"
        "import time\n"
        "from api_new import api_login\n"
        "from common.Request_Response import ApiClient\n\n"
        "env = api_login.url\n"
        "time_str = time.strftime(\"%Y%m%d%H%M%S\", time.localtime())\n\n\n"
        f"class {class_name}:\n"
        "    def __init__(self, client: ApiClient):\n"
        "        self.client = client\n"
    )
    file_path.write_text(content, encoding="utf-8")


def _default_target_module(
    path: str,
    api_new_dir: Path,
    module_map: List[Dict[str, str]],
    tag_name: str = "",
) -> Tuple[str, str]:
    """为新增接口选择目标模块；找不到映射时自动创建新模块。"""
    for item in module_map:
        prefix = item["prefix"]
        if path.startswith(prefix):
            return item["file_name"], item["class_name"]

    # 无前缀命中时自动创建新模块，减少对静态映射配置的依赖。
    prefix = _path_prefix_for_match(path).strip("/").replace("/", "_")
    seed = tag_name or prefix or "auto"
    seed_ascii = _to_snake_case(seed)
    if seed_ascii == "auto_generated" and prefix:
        seed_ascii = _to_snake_case(prefix)
    file_name = f"api_{seed_ascii}.py"
    class_name = f"Api{_to_pascal_case(seed_ascii)}"
    _create_new_api_module_file(api_new_dir, file_name, class_name, module_desc=tag_name or prefix or "自动")

    module_map.append({"prefix": _path_prefix_for_match(path), "file_name": file_name, "class_name": class_name})
    module_map.sort(key=lambda x: len(x["prefix"]), reverse=True)
    return file_name, class_name


def _extract_existing_method_names(text: str) -> Set[str]:
    """提取文件中已定义的方法名，避免新增方法重名。"""
    return set(re.findall(r"def\s+(\w+)\s*\(", text))


def _discover_wrapper_endpoint_map(api_dir: Path) -> Dict[str, Dict[str, str]]:
    """从旧 API 封装推导 `METHOD PATH -> 文件/类/方法` 映射。"""
    endpoint_map: Dict[str, Dict[str, str]] = {}
    for file_path in sorted(api_dir.glob(API_FILE_GLOB)):
        if file_path.name == "api_login.py":
            continue

        lines = file_path.read_text(encoding="utf-8").splitlines()
        class_name = ""
        current_method = ""
        current_path = ""
        current_http = ""

        for line in lines:
            class_match = re.match(r"\s*class\s+(\w+)\s*:", line)
            if class_match:
                class_name = class_match.group(1)

            method_match = re.match(r"\s*def\s+(\w+)\s*\(", line)
            if method_match:
                current_method = method_match.group(1)
                current_path = ""
                current_http = ""
                continue

            url_match = re.search(r'f"\{env\}(/[^"\n]+)"', line)
            if url_match:
                current_path = url_match.group(1)

            if "post_with_retry" in line:
                current_http = "POST"
            elif "get_with_retry" in line:
                current_http = "GET"
            else:
                req_match = re.search(r"request_with_retry\('([A-Z]+)'", line)
                if req_match:
                    current_http = req_match.group(1).upper()

            if current_method and current_path and current_http and "response" in line and "=" in line:
                key = f"{current_http} {current_path}"
                endpoint_map[key] = {
                    "file_name": file_path.name,
                    "class_name": class_name,
                    "method_name": current_method,
                }

    return endpoint_map


def _collect_method_names_for_keys(
    endpoint_keys: List[str],
    wrapper_map: Dict[str, Dict[str, str]],
) -> Set[str]:
    """将 endpoint key 列表映射为对应封装方法名集合。"""
    names: Set[str] = set()
    for key in endpoint_keys:
        info = wrapper_map.get(key)
        if info and info.get("method_name"):
            names.add(info["method_name"])
    return names


def _candidate_param_names(param_name: str) -> Set[str]:
    """为一个文档参数生成多种代码命名候选（原样/下划线/snake_case）。"""
    # 文档参数名可能含连字符，测试代码通常改写为下划线命名。
    normalized = param_name.strip()
    if not normalized:
        return set()
    return {
        normalized,
        normalized.replace("-", "_"),
        _to_snake_case(normalized.replace("-", "_")),
    }


def _build_method_param_change_map(
    changed_items: List[Dict[str, Any]],
    wrapper_map: Dict[str, Dict[str, str]],
) -> Dict[str, Dict[str, Set[str]]]:
    """按方法汇总参数变更：哪些参数被删除、哪些参数被新增。"""
    change_map: Dict[str, Dict[str, Set[str]]] = {}

    for item in changed_items:
        if "parameters" not in (item.get("changed_fields") or []):
            continue

        endpoint_key = str(item.get("key", ""))
        wrapper = wrapper_map.get(endpoint_key)
        if not wrapper:
            continue

        method_name = wrapper.get("method_name", "")
        if not method_name:
            continue

        old_spec: EndpointSpec = item.get("old")
        new_spec: EndpointSpec = item.get("new")
        old_params = {str(p.get("name", "")) for p in (old_spec.parameters if old_spec else [])}
        new_params = {str(p.get("name", "")) for p in (new_spec.parameters if new_spec else [])}

        removed = set()
        for p in sorted(old_params - new_params):
            removed.update(_candidate_param_names(p))

        added = set()
        for p in sorted(new_params - old_params):
            added.update(_candidate_param_names(p))

        if removed or added:
            change_map[method_name] = {"removed": removed, "added": added}

    return change_map


def _build_added_method_code(spec: EndpointSpec, existing_method_names: Set[str]) -> str:
    """根据 endpoint spec 生成可追加到 API 封装类中的方法代码。"""
    method_source = spec.operation_id or f"{spec.method}_{spec.path}"
    method_name = _to_snake_case(method_source)
    if method_name in existing_method_names:
        suffix = 1
        while f"{method_name}_{suffix}" in existing_method_names:
            suffix += 1
        method_name = f"{method_name}_{suffix}"
    existing_method_names.add(method_name)

    path_params = re.findall(r"\{([^{}]+)\}", spec.path)
    arg_list = [p for p in path_params]
    arg_list.append("payload=None")
    arg_list.append("headers=None")
    args_text = ", ".join(arg_list)

    method_lines: List[str] = []
    method_lines.append(f"    # {spec.summary or '新增接口'}")
    method_lines.append(f"    def {method_name}(self, {args_text}):")
    method_lines.append(f"        url = f\"{{env}}{spec.path}\"")
    method_lines.append("        headers = headers or {}")

    if spec.method == "GET":
        method_lines.append("        response = self.client.get_with_retry(url, headers=headers)")
    elif spec.method == "POST":
        method_lines.append("        payload = payload or {}")
        method_lines.append("        response = self.client.post_with_retry(url, json=payload, headers=headers)")
    else:
        method_lines.append("        payload = payload or {}")
        method_lines.append(
            f"        response = self.client.request_with_retry('{spec.method}', url, json=payload, headers=headers)"
        )
    method_lines.append("        return response")
    method_lines.append("")
    return "\n".join(method_lines)


def _append_added_endpoint_methods(
    api_new_dir: Path,
    added_specs: List[EndpointSpec],
    module_map: List[Dict[str, str]],
) -> Dict[str, Any]:
    """将新增接口方法追加到 `api_new` 目标模块，并返回变更统计。"""
    if not added_specs:
        return {"added_methods": 0, "touched_files": []}

    file_to_methods: Dict[Path, List[EndpointSpec]] = {}
    for spec in added_specs:
        tag_name = spec.tags[0] if spec.tags else ""
        file_name, _ = _default_target_module(
            path=spec.path,
            api_new_dir=api_new_dir,
            module_map=module_map,
            tag_name=tag_name,
        )
        file_to_methods.setdefault(api_new_dir / file_name, []).append(spec)

    touched_files: List[str] = []
    added_methods = 0

    for file_path, specs in file_to_methods.items():
        if not file_path.exists():
            continue

        text = file_path.read_text(encoding="utf-8")
        existing_names = _extract_existing_method_names(text)
        method_blocks = [
            _build_added_method_code(spec, existing_names)
            for spec in specs
        ]
        append_content = "\n" + "\n".join(method_blocks)
        file_path.write_text(text.rstrip() + append_content + "\n", encoding="utf-8")
        touched_files.append(str(file_path))
        added_methods += len(method_blocks)

    return {"added_methods": added_methods, "touched_files": touched_files}


def _filter_real_added_specs(
    added_specs: List[EndpointSpec],
    address_changed: List[Dict[str, Any]],
) -> List[EndpointSpec]:
    """过滤“伪新增”接口：仅因地址迁移产生的接口不再重复按新增处理。"""
    moved_operation_ids = {
        str(item.get("operationId", ""))
        for item in address_changed
        if item.get("operationId")
    }
    return [
        spec
        for spec in added_specs
        if not spec.operation_id or spec.operation_id not in moved_operation_ids
    ]


def _apply_address_changes_to_api_new(
    api_new_dir: Path,
    address_changed: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """把地址迁移变更应用到 `api_new`：同方法下替换旧路径为新路径。"""
    replacements: List[Dict[str, str]] = []
    for item in address_changed:
        old_list = item.get("old") or []
        new_list = item.get("new") or []
        if len(old_list) != 1 or len(new_list) != 1:
            continue

        old_method, old_path = _split_endpoint_key(old_list[0])
        new_method, new_path = _split_endpoint_key(new_list[0])
        if old_method != new_method:
            continue
        replacements.append({"operationId": item.get("operationId", ""), "old_path": old_path, "new_path": new_path})

    touched_files: List[str] = []
    total_replacements = 0
    for py_file in sorted(api_new_dir.glob("*.py")):
        if py_file.name == "__init__.py":
            continue

        text = py_file.read_text(encoding="utf-8")
        original_text = text
        file_replacements = 0
        for replacement in replacements:
            old_path = replacement["old_path"]
            new_path = replacement["new_path"]
            count = text.count(old_path)
            if count > 0:
                text = text.replace(old_path, new_path)
                file_replacements += count

        if text != original_text:
            py_file.write_text(text, encoding="utf-8")
            touched_files.append(str(py_file))
            total_replacements += file_replacements

    return {
        "replacements_defined": len(replacements),
        "replacements_applied": total_replacements,
        "touched_files": touched_files,
    }


def _has_differences(diff_result: Dict[str, Any]) -> bool:
    """判断 diff 是否包含任一类型变更。"""
    return any(
        len(diff_result[k]) > 0
        for k in ("address_changed", "added", "removed", "changed")
    )


def _cleanup_old_reports(report_dir: Path, latest_report: Path) -> None:
    """仅保留最新一份差异报告，清理历史 `api_doc_diff_*.md`。"""
    for old_report in report_dir.glob(REPORT_FILE_GLOB):
        if old_report.resolve() == latest_report.resolve():
            continue
        old_report.unlink(missing_ok=True)


def _extract_json_from_text(text: str) -> Dict[str, Any]:
    """从 LLM 返回文本中提取 JSON（支持 ```json fenced block 或裸 JSON）。"""
    text = (text or "").strip()
    if not text:
        return {}

    fence_match = re.search(r"```json\s*(\{[\s\S]*?\})\s*```", text, flags=re.IGNORECASE)
    if fence_match:
        try:
            return json.loads(fence_match.group(1))
        except Exception:
            pass

    brace_match = re.search(r"(\{[\s\S]*\})", text)
    if brace_match:
        try:
            return json.loads(brace_match.group(1))
        except Exception:
            return {}
    return {}


def _build_llm_prompt_for_added_endpoints(added_specs: List[EndpointSpec], testcase_contents: Dict[str, str]) -> str:
    """构造给 LLM 的提示词，约束输出为可解析 JSON。"""
    endpoint_lines = []
    for idx, spec in enumerate(added_specs, 1):
        endpoint_lines.append(
            f"[{idx}] {spec.method} {spec.path} | operationId={spec.operation_id or '无'} | 摘要={spec.summary or '无'}"
        )

    testcase_lines = []
    for file_name, content in testcase_contents.items():
        testcase_lines.append(f"### 文件: {file_name}\n{content[:6000]}")

    return (
        "你是接口自动化测试代码生成助手。请根据新增接口，给出各测试文件中的插入建议。\n"
        "输出必须是 JSON，不要包含额外解释。\n"
        "JSON 格式：\n"
        "{\n"
        "  \"files\": [\n"
        "    {\n"
        "      \"file\": \"testcase/test_xxx_1.py\",\n"
        "      \"suggestions\": [\n"
        "        {\n"
        "          \"anchor\": \"建议插入位置关键词，如 步骤27 或 test_xxx\",\n"
        "          \"reason\": \"原因\",\n"
        "          \"confidence\": 0.0,\n"
        "          \"snippet\": \"建议插入的 pytest/allure 代码片段\"\n"
        "        }\n"
        "      ]\n"
        "    }\n"
        "  ]\n"
        "}\n\n"
        "新增接口如下：\n"
        + "\n".join(endpoint_lines)
        + "\n\n现有测试文件内容如下：\n"
        + "\n\n".join(testcase_lines)
    )


def _load_testcase_contents(paths: List[str]) -> Dict[str, str]:
    """读取测试文件内容，键名优先采用 `目录/文件名` 便于提示词定位。"""
    result: Dict[str, str] = {}
    for p in paths:
        path_obj = Path(p)
        try:
            result[path_obj.as_posix().split("/")[-2] + "/" + path_obj.name if len(path_obj.parts) >= 2 else path_obj.name] = (
                path_obj.read_text(encoding="utf-8")
            )
        except Exception:
            result[path_obj.name] = ""
    return result


def _normalize_llm_suggestions(raw: Dict[str, Any], generated_tests: List[str]) -> Dict[str, List[Dict[str, Any]]]:
    """校验并规范化 LLM 建议，只保留命中目标测试文件的有效片段。"""
    by_file: Dict[str, List[Dict[str, Any]]] = {}
    valid_names = {Path(p).name: p for p in generated_tests}

    files = raw.get("files") if isinstance(raw, dict) else []
    if not isinstance(files, list):
        return by_file

    for item in files:
        if not isinstance(item, dict):
            continue
        file_hint = str(item.get("file", "")).strip()
        target_name = Path(file_hint).name if file_hint else ""
        if target_name not in valid_names:
            continue

        suggestions = item.get("suggestions", [])
        if not isinstance(suggestions, list):
            continue

        normalized = []
        for s in suggestions:
            if not isinstance(s, dict):
                continue
            snippet = str(s.get("snippet", "")).strip()
            if not snippet:
                continue
            try:
                confidence = float(s.get("confidence", 0.0) or 0.0)
            except Exception:
                confidence = 0.0
            normalized.append(
                {
                    "anchor": str(s.get("anchor", "")).strip(),
                    "reason": str(s.get("reason", "")).strip(),
                    "confidence": max(0.0, min(1.0, confidence)),
                    "snippet": snippet,
                }
            )

        if normalized:
            by_file[valid_names[target_name]] = normalized

    return by_file


def _strip_existing_llm_block(text: str) -> str:
    """移除旧的 LLM 建议注释块，避免重复注入。"""
    pattern = re.compile(
        rf"\n?{re.escape(LLM_SUGGESTION_BEGIN)}[\s\S]*?{re.escape(LLM_SUGGESTION_END)}\n?",
        flags=re.MULTILINE,
    )
    return re.sub(pattern, "\n", text)


def _inject_llm_suggestions_into_test(path: Path, suggestions: List[Dict[str, Any]]) -> bool:
    """将 LLM 建议以注释块写入测试文件（优先插在 `__main__` 之前）。"""
    if not suggestions:
        return False

    text = path.read_text(encoding="utf-8")
    text = _strip_existing_llm_block(text)

    lines = [LLM_SUGGESTION_BEGIN]
    for idx, s in enumerate(suggestions, 1):
        lines.append(f"# [{idx}] anchor: {s['anchor'] or '未指定'}")
        lines.append(f"#     reason: {s['reason'] or '无'}")
        lines.append(f"#     confidence: {s['confidence']:.2f}")
        lines.append("#     snippet:")
        for snip_line in s["snippet"].splitlines():
            lines.append(f"#     {snip_line}")
    lines.append(LLM_SUGGESTION_END)
    block = "\n" + "\n".join(lines) + "\n\n"

    marker = "if __name__ == \"__main__\":"
    pos = text.find(marker)
    if pos >= 0:
        new_text = text[:pos] + block + text[pos:]
    else:
        new_text = text.rstrip() + block
    path.write_text(new_text, encoding="utf-8")
    return True


def _apply_llm_suggestions_to_tests(added_specs: List[EndpointSpec], generated_tests: List[str]) -> Dict[str, Any]:
    """调用 LLM 生成建议并写回测试副本，返回执行与落地结果。"""
    result = {
        "enabled": True,
        "used": False,
        "model": "",
        "applied_files": [],
        "error": "",
    }

    if not added_specs or not generated_tests:
        result["error"] = "无新增接口或无生成测试文件，跳过LLM建议"
        return result

    cfg = _load_ai_config()
    result["model"] = str(cfg.get("model", ""))
    if not cfg.get("token"):
        result["error"] = "未配置LLM API Key，跳过LLM建议"
        return result

    testcase_contents = _load_testcase_contents(generated_tests)
    prompt = _build_llm_prompt_for_added_endpoints(added_specs, testcase_contents)
    llm_text = call_github_models_api(prompt, cfg)
    parsed = _extract_json_from_text(llm_text)
    suggestions = _normalize_llm_suggestions(parsed, generated_tests)

    applied = []
    for file_path, rows in suggestions.items():
        ok = _inject_llm_suggestions_into_test(Path(file_path), rows)
        if ok:
            applied.append(file_path)

    result["used"] = True
    result["applied_files"] = applied
    if not applied:
        result["error"] = "LLM返回为空或未命中目标文件"
    return result


def generate_report_and_tests(
    old_doc: Path,
    new_doc: Path,
    report_dir: Path,
    testcase_dir: Path,
    api_dir: Path,
    api_new_dir: Path,
    use_llm: bool = False,
) -> Dict[str, Any]:
    """
    对比新旧 API 文档并生成产物。

    处理流程：
    1) 解析文档并输出差异报告；
    2) 若存在差异，生成 `api_new`、迁移地址、追加新增接口方法；
    3) 生成测试 `_1` 副本并标注待办；
    4) 可选执行 LLM 建议注入。

    返回值包含报告路径、生成文件列表、迁移统计、LLM 执行结果与差异计数。
    """
    # Phase 1: 解析文档并计算差异。
    old_json = _load_json(old_doc)
    new_json = _load_json(new_doc)

    old_specs = _extract_endpoint_specs(old_json)
    new_specs = _extract_endpoint_specs(new_json)
    diff_result = _compare_specs(old_specs, new_specs)
    added_specs = [new_specs[k] for k in sorted(set(new_specs.keys()) - set(old_specs.keys()))]
    real_added_specs = _filter_real_added_specs(added_specs, diff_result["address_changed"])
    removed_specs = [old_specs[k] for k in sorted(set(old_specs.keys()) - set(new_specs.keys()))]
    changed_items = diff_result["changed"]

    now = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"api_doc_diff_{now}.md"

    markdown = _generate_markdown(old_doc.name, new_doc.name, diff_result, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    report_path.write_text(markdown, encoding="utf-8")
    _cleanup_old_reports(report_dir, report_path)

    has_diff = _has_differences(diff_result)
    generated_tests: List[str] = []
    generated_api_files: List[str] = []
    api_migration = {"replacements_defined": 0, "replacements_applied": 0, "touched_files": []}
    api_added_methods = {"added_methods": 0, "touched_files": []}
    llm_suggestions = {"enabled": use_llm, "used": False, "model": "", "applied_files": [], "error": ""}

    # Phase 2: 仅在有差异时生成 api_new 与测试副本。
    if has_diff:
        generated_api_files = _copy_api_package_to_new(api_dir=api_dir, api_new_dir=api_new_dir)
        dynamic_module_map = _discover_api_module_map(api_new_dir)
        wrapper_endpoint_map = _discover_wrapper_endpoint_map(api_dir)

        removed_method_names = _collect_method_names_for_keys(
            endpoint_keys=[spec.key for spec in removed_specs],
            wrapper_map=wrapper_endpoint_map,
        )
        changed_method_names = _collect_method_names_for_keys(
            endpoint_keys=[str(item.get("key", "")) for item in changed_items],
            wrapper_map=wrapper_endpoint_map,
        )
        method_param_changes = _build_method_param_change_map(
            changed_items=changed_items,
            wrapper_map=wrapper_endpoint_map,
        )

        api_migration = _apply_address_changes_to_api_new(
            api_new_dir=api_new_dir,
            address_changed=diff_result["address_changed"],
        )
        api_added_methods = _append_added_endpoint_methods(
            api_new_dir=api_new_dir,
            added_specs=real_added_specs,
            module_map=dynamic_module_map,
        )
        generated_api_files = [str(p) for p in sorted(api_new_dir.glob("*.py"))]

        # Phase 3: 生成 testcase `_1` 副本并注入自动注释。
        for src in sorted(testcase_dir.glob("test_*.py")):
            if src.stem.endswith("_1"):
                continue
            dst = src.with_name(f"{src.stem}_1.py")
            _clone_testcase_file(
                src=src,
                dst=dst,
                added_specs=real_added_specs,
                removed_specs=removed_specs,
                changed_items=changed_items,
                removed_method_names=removed_method_names,
                changed_method_names=changed_method_names,
                method_param_changes=method_param_changes,
            )
            generated_tests.append(str(dst))

        # Phase 4: 可选执行 LLM 建议注入（不影响主流程成功）。
        if use_llm:
            llm_suggestions = _apply_llm_suggestions_to_tests(
                added_specs=real_added_specs,
                generated_tests=generated_tests,
            )

    return {
        "report": str(report_path),
        "generated_tests": generated_tests,
        "scripts_generated": has_diff,
        "api_new_generated": has_diff,
        "generated_api_files": generated_api_files,
        "api_migration": api_migration,
        "api_added_methods": api_added_methods,
        "llm_suggestions": llm_suggestions,
        "diff": {
            "address_changed": len(diff_result["address_changed"]),
            "added": len(diff_result["added"]),
            "removed": len(diff_result["removed"]),
            "changed": len(diff_result["changed"]),
        },
    }


def main() -> None:
    """命令行入口：解析参数并打印 JSON 结果。"""
    parser = argparse.ArgumentParser(description="对比两份 API 文档，并按需生成 api_new 接口封装与测试脚本副本")
    parser.add_argument("--old-doc", default=str(DEFAULT_OLD_DOC), help="旧版 API 文档路径")
    parser.add_argument("--new-doc", default=str(DEFAULT_NEW_DOC), help="新版 API 文档路径")
    parser.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR), help="报告输出目录")
    parser.add_argument("--testcase-dir", default=str(DEFAULT_TESTCASE_DIR), help="测试脚本目录")
    parser.add_argument("--api-dir", default=str(DEFAULT_API_DIR), help="旧版接口封装目录")
    parser.add_argument("--api-new-dir", default=str(DEFAULT_API_NEW_DIR), help="新版接口封装输出目录")
    parser.add_argument("--use-llm", dest="use_llm", action="store_true", help="启用大模型生成新增接口插入建议（默认开启）")
    parser.add_argument("--no-llm", dest="use_llm", action="store_false", help="关闭大模型建议，仅执行规则迁移")
    parser.set_defaults(use_llm=True)
    args = parser.parse_args()

    result = generate_report_and_tests(
        old_doc=Path(args.old_doc),
        new_doc=Path(args.new_doc),
        report_dir=Path(args.report_dir),
        testcase_dir=Path(args.testcase_dir),
        api_dir=Path(args.api_dir),
        api_new_dir=Path(args.api_new_dir),
        use_llm=bool(args.use_llm),
    )

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

