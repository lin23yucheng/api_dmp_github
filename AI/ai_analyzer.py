"""
AI 测试报告分析器
使用 LLM API 自动分析 Allure 测试报告
功能：
  1. 识别 passed 但实际可能失败的用例（断言不充分、只验证状态码等）
  2. 找出可优化的用例（重复断言、缺少边界判断、步骤设计问题等）
  3. 生成结构化中文分析报告
"""
import os
import json
import glob
import ast
import re
import requests
from datetime import datetime
from common.Log import MyLog


ERROR_KEYWORDS = ["error", "fail", "failed", "exception", "traceback", "错误", "失败", "异常"]


def _normalize_provider(provider: str) -> str:
    text = str(provider or "").strip().lower()
    if text in {"deepseek", "deepseek-ai", "ds"}:
        return "deepseek"
    return "github"


def _provider_defaults(provider: str) -> dict:
    provider = _normalize_provider(provider)
    if provider == "deepseek":
        return {
            "provider": "deepseek",
            "default_model": "deepseek-v4-pro",
            "default_api_url": "https://api.deepseek.com/chat/completions",
            "ini_model_key": "deepseek_model",
            "ini_api_url_key": "deepseek_models_api_url",
            "ini_token_key": "deepseek_apikeys",
            "env_model_keys": ["DEEPSEEK_MODEL", "LLM_MODEL"],
            "env_api_url_keys": ["DEEPSEEK_MODELS_API_URL", "LLM_API_URL"],
            "env_token_keys": ["DEEPSEEK_API_KEY", "APIKEYS_DS", "LLM_API_KEY"],
            "secret_token_keys": ["DEEPSEEK_API_KEY", "APIKEYS_DS"],
            "display_name": "DeepSeek",
        }
    return {
        "provider": "github",
        "default_model": "gpt-4o",
        "default_api_url": "https://models.inference.ai.azure.com/chat/completions",
        "ini_model_key": "github_model",
        "ini_api_url_key": "github_models_api_url",
        "ini_token_key": "github_token",
        "env_model_keys": ["GITHUB_MODEL", "LLM_MODEL"],
        "env_api_url_keys": ["GITHUB_MODELS_API_URL", "LLM_API_URL"],
        "env_token_keys": ["GITHUB_TOKEN", "LLM_API_KEY"],
        "secret_token_keys": ["GITHUB_TOKEN"],
        "display_name": "GitHub Models",
    }


# ─── 配置读取 ─────────────────────────────────────────────────────────────────

def _load_env_file(env_file_path: str) -> dict:
    """读取简单 KEY=VALUE 的环境变量文件（忽略注释和空行）。"""
    values = {}
    if not env_file_path or not os.path.exists(env_file_path):
        return values

    try:
        with open(env_file_path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or line.startswith(";"):
                    continue
                if "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip().lstrip("\ufeff")
                val = val.strip().strip('"').strip("'")
                if key:
                    values[key] = val
    except Exception as e:
        MyLog.warning(f"AI分析：读取本地密钥文件失败 {env_file_path}: {e}")

    return values

def _load_ai_config() -> dict:
    """从环境变量 / 本地密钥文件 / config/ai_config.ini 读取 AI 配置。"""
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ini_path = os.path.join(base_dir, "config", "ai_config.ini")
    secret_env_path = os.path.join(base_dir, "config", "secret.env")
    ini_provider = "github"
    parser = None

    # 支持重复键场景（strict=False）：当配置里存在两行 llm_provider 时，按最后一行生效。
    if os.path.exists(ini_path):
        try:
            import configparser
            parser = configparser.ConfigParser(strict=False)
            parser.read(ini_path, encoding="utf-8")
            sec = "ai_model"
            if parser.has_section(sec):
                ini_provider = parser.get(sec, "llm_provider", fallback=ini_provider)
        except Exception as e:
            MyLog.warning(f"AI分析：读取 provider 配置失败 {ini_path}: {e}")

    provider = _normalize_provider(os.environ.get("LLM_PROVIDER", ini_provider))
    provider_cfg = _provider_defaults(provider)
    cfg = {
        "provider": provider_cfg["provider"],
        "provider_display": provider_cfg["display_name"],
        "token": "",
        "model": provider_cfg["default_model"],
        "api_url": provider_cfg["default_api_url"],
        "timeout": int(os.environ.get("AI_ANALYZER_TIMEOUT", "120")),
        "secret_env_file": secret_env_path,
    }
    token_ref = ""

    # 1) 先读取 ini 的非敏感默认项
    if parser is not None:
        sec = "ai_model"
        if parser.has_section(sec):
            cfg["model"] = parser.get(sec, provider_cfg["ini_model_key"], fallback=cfg["model"])
            cfg["api_url"] = parser.get(sec, provider_cfg["ini_api_url_key"], fallback=cfg["api_url"])
            cfg["secret_env_file"] = parser.get(sec, "secret_env_file", fallback=cfg["secret_env_file"])

            ini_token = parser.get(sec, provider_cfg["ini_token_key"], fallback="").strip()
            if ini_token:
                token_ref = ini_token
                # 允许直接把真实 token 写在 ini；若是变量名（如 APIKEYS_DS）后续会从 secret/env 解析。
                if "your_" not in ini_token.lower():
                    cfg["token"] = ini_token

    # 2) 读取本地密钥文件（默认 config/secret.env，可被 ini 覆盖）
    secret_file = str(cfg.get("secret_env_file", secret_env_path))
    if secret_file and not os.path.isabs(secret_file):
        secret_file = os.path.join(base_dir, secret_file)
    secret_values = _load_env_file(secret_file)
    for key in [token_ref] + list(provider_cfg["secret_token_keys"]):
        if key and secret_values.get(key):
            cfg["token"] = secret_values[key]
            break

    # 3) 最后用系统环境变量覆盖（最高优先级）
    for key in provider_cfg["env_model_keys"]:
        if os.environ.get(key):
            cfg["model"] = os.environ[key]
            break
    for key in provider_cfg["env_api_url_keys"]:
        if os.environ.get(key):
            cfg["api_url"] = os.environ[key]
            break
    for key in [token_ref] + list(provider_cfg["env_token_keys"]):
        if key and os.environ.get(key):
            cfg["token"] = os.environ[key]
            break

    # deepseek 常见误配是只写域名，自动补齐 chat/completions 路径。
    if cfg["provider"] == "deepseek":
        url = str(cfg.get("api_url", "")).rstrip("/")
        if url == "https://api.deepseek.com":
            cfg["api_url"] = "https://api.deepseek.com/chat/completions"


    return cfg


# ─── Allure 结果解析 ──────────────────────────────────────────────────────────

def _read_attachment(source: str, allure_results_dir: str, max_chars: int = 800) -> str:
    """读取 attachment 文件内容（截断过长内容）"""
    path = os.path.join(allure_results_dir, source)
    if not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read(max_chars + 200)
        if len(content) > max_chars:
            content = content[:max_chars] + "...[截断]"
        return content.strip()
    except Exception:
        return ""


def _extract_response_body_text(content: str) -> str:
    """从 Response Details 附件中提取 Response Body 文本主体。"""
    text = str(content or "")
    marker = "Response Body:"
    if marker in text:
        body = text.split(marker, 1)[1].strip()
        # 防止后续段落（若有）污染 body
        for stop in ("\nRequest ", "\nResponse ", "\nSemantic "):
            if stop in body:
                body = body.split(stop, 1)[0].strip()
        return body
    return text.strip()


def _to_compact_json_text(payload) -> str:
    if isinstance(payload, (dict, list)):
        try:
            return json.dumps(payload, ensure_ascii=False)
        except Exception:
            return str(payload)
    return str(payload)


def _parse_steps(steps: list, allure_results_dir: str, depth: int = 0) -> list:
    """递归解析步骤，返回简洁的步骤摘要列表"""
    result = []
    indent = "  " * depth
    for step in steps:
        name   = step.get("name", "")
        status = step.get("status", "unknown")
        entry  = {"step": f"{indent}{name}", "status": status}

        # 收集关键附件（response JSON，限制大小）
        attachments = step.get("attachments", [])
        response_snippets = []
        semantic_snippets = []
        response_parseable = False
        for att in attachments:
            att_name = att.get("name", "")
            if "Response" in att_name or "response" in att_name:
                # 为提高解析率，响应附件读取更长；展示时再截断。
                content = _read_attachment(att.get("source", ""), allure_results_dir, 4000)
                if content:
                    body_text = _extract_response_body_text(content)
                    parsed = _parse_response_payload(body_text)
                    if parsed is not None:
                        response_parseable = True
                        compact = _to_compact_json_text(parsed)
                        response_snippets.append(f"[{att_name}]: Response Body: {compact[:600]}")
                    else:
                        response_snippets.append(f"[{att_name}]: {content[:600]}")
            if "Semantic Assertion" in att_name:
                content = _read_attachment(att.get("source", ""), allure_results_dir, 800)
                if content:
                    semantic_snippets.append(f"[{att_name}]: {content}")
        if response_snippets:
            entry["response"] = response_snippets[:2]  # 最多 2 个
            entry["response_parseable"] = response_parseable
        if semantic_snippets:
            entry["semantic"] = semantic_snippets[:1]  # 每步最多 1 个语义摘要

        # 收集子步骤状态摘要（不深度展开）
        sub_steps = step.get("steps", [])
        if sub_steps:
            entry["sub_steps"] = _parse_steps(sub_steps, allure_results_dir, depth + 1)

        result.append(entry)
    return result


def collect_test_results(allure_results_dir: str) -> list:
    """
    扫描 allure-results 目录，收集所有测试结果
    返回结构化列表，按状态分组（failed/broken 优先）
    """
    pattern = os.path.join(allure_results_dir, "*-result.json")
    result_files = glob.glob(pattern)
    if not result_files:
        MyLog.warning(f"AI分析：未找到测试结果文件，目录：{allure_results_dir}")
        return []

    results = []
    for file_path in result_files:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            MyLog.warning(f"AI分析：读取结果文件失败 {file_path}: {e}")
            continue

        status    = data.get("status", "unknown")
        test_name = data.get("name", "")
        full_name = data.get("fullName", "")
        labels    = {lb["name"]: lb["value"] for lb in data.get("labels", [])}
        feature   = labels.get("feature", "")
        story     = labels.get("story", "")

        # 解析步骤
        steps = _parse_steps(data.get("steps", []), allure_results_dir)

        # 失败详情
        status_details = data.get("statusDetails", {})
        failure_msg    = status_details.get("message", "")
        failure_trace  = (status_details.get("trace", "") or "")[:500]

        # 日志附件（简要）
        log_snippet = ""
        for att in data.get("attachments", []):
            if att.get("name") == "log":
                log_snippet = _read_attachment(att.get("source", ""), allure_results_dir, 300)
                break

        entry = {
            "test_name":    test_name,
            "full_name":    full_name,
            "feature":      feature,
            "story":        story,
            "status":       status,
            "steps":        steps,
            "failure_msg":  failure_msg,
            "failure_trace": failure_trace,
            "log_snippet":  log_snippet,
        }
        results.append(entry)

    # 排序：failed/broken 优先，然后 passed，最后 skipped
    order = {"failed": 0, "broken": 1, "passed": 2, "skipped": 3}
    results.sort(key=lambda x: order.get(x["status"], 9))
    return results


def _build_statistics(results: list) -> dict:
    """统计各状态用例数"""
    stats = {"total": len(results), "passed": 0, "failed": 0, "broken": 0, "skipped": 0, "other": 0}
    for r in results:
        s = r["status"]
        if s in stats:
            stats[s] += 1
        else:
            stats["other"] += 1
    return stats


def _iter_all_steps(steps: list):
    for step in steps:
        yield step
        for sub_step in _iter_all_steps(step.get("sub_steps", [])):
            yield sub_step


def _parse_response_payload(response_text: str):
    """从 response 摘要中尽量提取结构化 body。"""
    if not response_text:
        return None
    text = str(response_text)
    marker = "Response Body:"
    payload = text.split(marker, 1)[1].strip() if marker in text else text
    if payload.startswith("[") and "]:" in payload:
        payload = payload.split("]:", 1)[1].strip()

    try:
        return json.loads(payload)
    except Exception:
        try:
            parsed = ast.literal_eval(payload)
            if isinstance(parsed, (dict, list)):
                return parsed
        except Exception:
            pass

    # 容错：兼容 {code:500,msg:error} 这类松散文本格式。
    loose = str(payload).strip()
    if loose.startswith("{") and loose.endswith("}"):
        normalized = re.sub(r'([\{,]\s*)([A-Za-z_][\w-]*)(\s*:)', r'\1"\2"\3', loose)

        def _normalize_value(match):
            raw = match.group(1)
            lowered = raw.lower()
            if lowered in {"true", "false", "null"}:
                return ": " + lowered
            if re.match(r"^-?\d+(?:\.\d+)?$", raw):
                return ": " + raw
            return ': "' + raw + '"'

        normalized = re.sub(r':\s*([A-Za-z_][\w-]*)\s*(?=[,}])', _normalize_value, normalized)
        try:
            parsed = json.loads(normalized)
            if isinstance(parsed, (dict, list)):
                return parsed
        except Exception:
            pass

    return None


def _collect_false_pass_candidates(results: list) -> list:
    """仅基于可验证证据筛选 passed 用例候选，避免经验性提示。"""
    candidates = []
    for r in results:
        if r.get("status") != "passed":
            continue

        score = 0
        evidence = []
        for step in _iter_all_steps(r.get("steps", [])):
            if _is_expected_negative_step(step.get("step", "")):
                # 预期异常分支不纳入假通过候选，避免把“故意校验错误码”的步骤当成缺陷。
                continue

            for resp in step.get("response", []):
                payload = _parse_response_payload(resp)
                if isinstance(payload, dict):
                    msg = str(payload.get("msg", "") or payload.get("message", ""))
                    msg_lower = msg.lower()
                    if _is_duplicate_conflict_message(msg):
                        continue
                    if msg and any(k in msg_lower for k in ERROR_KEYWORDS):
                        score += 3
                        evidence.append(f"step={step.get('step', '')} | msg疑似异常: {msg[:120]}")

                    for key in ("success", "ok"):
                        if key in payload and payload.get(key) is False:
                            score += 4
                            evidence.append(f"step={step.get('step', '')} | {key}=False")

                    code_val = payload.get("code")
                    if isinstance(code_val, int) and code_val not in (0, 200):
                        score += 3
                        evidence.append(f"step={step.get('step', '')} | code={code_val} (非常见成功码)")
                else:
                    # 文本响应仅在出现明确错误关键词时计为弱证据。
                    text = str(resp).lower()
                    if any(k in text for k in ERROR_KEYWORDS):
                        score += 1
                        evidence.append(f"step={step.get('step', '')} | 文本含错误关键词")

        if score >= 3:
            confidence = "HIGH" if score >= 7 else "MEDIUM"
            candidates.append(
                {
                    "test_name": r.get("test_name", ""),
                    "full_name": r.get("full_name", ""),
                    "confidence": confidence,
                    "score": score,
                    "evidence": evidence[:4],
                }
            )
    return candidates


def _contains_assertion_signal(text: str) -> bool:
    """从步骤名称中粗粒度识别断言/校验语义，作为可观测性信号。"""
    low = str(text or "").lower()
    keywords = ["assert", "校验", "断言", "验证", "check", "核对"]
    return any(k in low for k in keywords)


def _is_expected_negative_step(step_name: str) -> bool:
    """识别显式预期异常/负向步骤，降低假通过误判。"""
    text = str(step_name or "").lower()
    markers = [
        "预期异常", "预期失败", "预期报错", "异常分支", "负向", "negative",
        "重复创建", "已存在", "冲突场景", "expect fail", "expected fail",
    ]
    return any(m in text for m in markers)


def _is_duplicate_conflict_message(msg: str) -> bool:
    """识别“已存在/重复创建”类业务冲突文案，通常属于可预期负向分支。"""
    text = str(msg or "").lower()
    markers = ["已存在", "重复", "already exists", "duplicate", "conflict"]
    return any(m in text for m in markers)


def _collect_observability_metrics(results: list) -> dict:
    """统计报告中的证据可观测性，避免模型凭空放大风险描述。"""
    total_steps = 0
    response_steps = 0
    response_parseable_steps = 0
    semantic_steps = 0
    assertion_signal_steps = 0

    for r in results:
        for step in _iter_all_steps(r.get("steps", [])):
            total_steps += 1
            if _contains_assertion_signal(step.get("step", "")):
                assertion_signal_steps += 1

            responses = step.get("response", [])
            if responses:
                response_steps += 1
                if step.get("response_parseable") is True or any(
                    _parse_response_payload(resp) is not None for resp in responses
                ):
                    response_parseable_steps += 1

            if step.get("semantic", []):
                semantic_steps += 1

    def _ratio(num: int, den: int) -> float:
        return round((num / den), 4) if den else 0.0

    return {
        "total_steps": total_steps,
        "interface_steps": total_steps,
        "response_steps": response_steps,
        "interface_analyzed_steps": response_steps,
        "interface_analysis_coverage": _ratio(response_steps, total_steps),
        "response_attachment_coverage": _ratio(response_steps, total_steps),
        "response_parseable_steps": response_parseable_steps,
        "interface_parseable_steps": response_parseable_steps,
        "response_json_parse_rate": _ratio(response_parseable_steps, response_steps),
        "semantic_steps": semantic_steps,
        "semantic_attachment_coverage": _ratio(semantic_steps, total_steps),
        "assertion_signal_steps": assertion_signal_steps,
        "assertion_signal_coverage": _ratio(assertion_signal_steps, total_steps),
        # 新命名用于避免误解：该指标是“步骤命名可观测性”，非真实断言执行覆盖率。
        "step_name_assertion_signal_coverage": _ratio(assertion_signal_steps, total_steps),
    }


def _collect_observability_gap_candidates(results: list) -> list:
    """识别 passed 用例中的证据盲区（不等同于假通过）。"""
    candidates = []
    for r in results:
        if r.get("status") != "passed":
            continue

        total_steps = 0
        response_steps = 0
        response_parseable_steps = 0
        semantic_steps = 0
        assertion_signal_steps = 0

        for step in _iter_all_steps(r.get("steps", [])):
            total_steps += 1
            if _contains_assertion_signal(step.get("step", "")):
                assertion_signal_steps += 1

            responses = step.get("response", [])
            if responses:
                response_steps += 1
                if step.get("response_parseable") is True or any(
                    _parse_response_payload(resp) is not None for resp in responses
                ):
                    response_parseable_steps += 1

            if step.get("semantic", []):
                semantic_steps += 1

        if total_steps <= 0:
            continue

        score = 0
        evidence = []
        if response_steps == 0:
            score += 3
            evidence.append("无响应附件可用于业务字段核验")
        elif response_parseable_steps == 0:
            score += 2
            evidence.append("存在响应附件，但无法解析为结构化JSON")

        if assertion_signal_steps == 0:
            score += 2
            evidence.append("步骤名称中无断言/校验信号，断言可观测性偏低")

        if semantic_steps == 0:
            score += 1
            evidence.append("未观测到 Semantic Assertion 附件")

        if score >= 3:
            confidence = "MEDIUM" if score >= 4 else "LOW"
            coverage = round(response_steps / total_steps, 4)
            candidates.append(
                {
                    "test_name": r.get("test_name", ""),
                    "full_name": r.get("full_name", ""),
                    "confidence": confidence,
                    "score": score,
                    "gap_type": "OBSERVABILITY_GAP",
                    "summary": (
                        f"response_coverage={coverage}, "
                        f"response_parseable_steps={response_parseable_steps}/{response_steps}, "
                        f"assertion_signal_steps={assertion_signal_steps}/{total_steps}"
                    ),
                    "evidence": evidence[:4],
                }
            )
    return candidates


def _normalize_evidence_text(text: str) -> str:
    """将证据文本归一化，用于去重与复用 evidence_id。"""
    normalized = re.sub(r"\s+", " ", str(text or "")).strip()
    return normalized


def _attach_evidence_ids(candidates: list, start: int = 1) -> tuple[list, int]:
    """为候选证据分配稳定 evidence_id，便于模型引用。"""
    next_idx = start
    global_evidence_map = {}
    enriched = []
    for c in candidates:
        grouped = []
        local_index = {}
        for ev in c.get("evidence", []):
            raw_text = str(ev)
            norm = _normalize_evidence_text(raw_text)
            if not norm:
                continue

            if norm in local_index:
                grouped[local_index[norm]]["count"] += 1
                continue

            local_index[norm] = len(grouped)
            grouped.append({"norm": norm, "text": raw_text, "count": 1})

        rows = []
        for item in grouped:
            norm = item["norm"]
            if norm not in global_evidence_map:
                global_evidence_map[norm] = f"E{next_idx:03d}"
                next_idx += 1
            rows.append(
                {
                    "id": global_evidence_map[norm],
                    "text": item["text"],
                    "count": item["count"],
                }
            )

        clone = dict(c)
        clone["evidence_rows"] = rows
        enriched.append(clone)
    return enriched, next_idx


def _render_candidate_text(candidates: list, title: str) -> str:
    if not candidates:
        return f"{title}：无。"

    lines = [f"{title}："]
    for i, c in enumerate(candidates, 1):
        lines.append(
            f"[{i}] {c.get('test_name', '')} | confidence={c.get('confidence', '')} | "
            f"score={c.get('score', '')} | type={c.get('gap_type', 'FALSE_PASS')}"
        )
        if c.get("full_name"):
            lines.append(f"    full_name={c['full_name']}")
        if c.get("summary"):
            lines.append(f"    summary={c['summary']}")
        for row in c.get("evidence_rows", []):
            repeat = f" x{row['count']}" if row.get("count", 1) > 1 else ""
            lines.append(f"    [{row['id']}] {row['text']}{repeat}")
    return "\n".join(lines)


def _build_prompt_context(results: list) -> dict:
    """统一收集提示词与报告摘要所需上下文，避免重复统计。"""
    false_pass_candidates = _collect_false_pass_candidates(results)
    gap_candidates = _collect_observability_gap_candidates(results)
    observability_metrics = _collect_observability_metrics(results)
    semantic_observability = _collect_semantic_observability(results)

    false_pass_candidates, next_eid = _attach_evidence_ids(false_pass_candidates, start=1)
    gap_candidates, _ = _attach_evidence_ids(gap_candidates, start=next_eid)

    return {
        "false_pass_candidates": false_pass_candidates,
        "gap_candidates": gap_candidates,
        "observability_metrics": observability_metrics,
        "semantic_observability": semantic_observability,
    }


def _build_quick_overview(stats: dict, context: dict) -> str:
    """生成简洁可读的固定摘要，帮助先看结论再看长文。"""
    obs = context.get("observability_metrics", {})
    sem = context.get("semantic_observability", {})
    fp = context.get("false_pass_candidates", [])
    gaps = context.get("gap_candidates", [])

    resp_rate = obs.get("response_json_parse_rate", 0.0)
    assertion_rate = obs.get("step_name_assertion_signal_coverage", obs.get("assertion_signal_coverage", 0.0))
    interface_coverage = obs.get("interface_analysis_coverage", 0.0)
    sem_mid_high = sem.get("semantic_medium_high_steps", 0)

    if fp:
        overall = "高风险（存在可验证假通过候选）"
    elif sem_mid_high > 0 or gaps:
        overall = "中风险（无直接假通过证据，但存在可观测性/语义风险）"
    else:
        overall = "低风险（当前证据未发现明显风险）"

    lines = [
        "## 快速阅读",
        "",
        f"- 总体判定：**{overall}**",
        f"- 用例统计：total={stats.get('total', 0)} | passed={stats.get('passed', 0)} | failed={stats.get('failed', 0)} | broken={stats.get('broken', 0)} | skipped={stats.get('skipped', 0)}",
        f"- 可验证假通过候选：{len(fp)} 条；证据盲区候选：{len(gaps)} 条",
        f"- 接口步骤覆盖率（按步骤/子步骤视作接口）：{interface_coverage:.2%}",
        f"- 响应可解析率：{resp_rate:.2%}；步骤命名断言信号覆盖率：{assertion_rate:.2%}",
        f"- 语义中高风险步数：{sem_mid_high}（semantic_steps={sem.get('semantic_steps', 0)}）",
        "",
        "---",
        "",
    ]
    return "\n".join(lines)


def _collect_semantic_observability(results: list) -> dict:
    """统计语义断言在测试报告中的可观测性，避免AI给出'未引入语义断言'类建议。"""
    total_steps = 0
    semantic_steps = 0
    semantic_findings = 0
    high_or_medium_hits = 0

    for r in results:
        for step in _iter_all_steps(r.get("steps", [])):
            total_steps += 1
            semantic_list = step.get("semantic", [])
            if not semantic_list:
                continue
            semantic_steps += 1

            text = "\n".join(str(x) for x in semantic_list)
            if "Findings:" in text and "No semantic anomaly detected" not in text:
                semantic_findings += 1
            if "Risk: **HIGH**" in text or "Risk: **MEDIUM**" in text:
                high_or_medium_hits += 1

    coverage_ratio = (semantic_steps / total_steps) if total_steps else 0.0
    return {
        "total_steps": total_steps,
        "semantic_steps": semantic_steps,
        "semantic_coverage_ratio": round(coverage_ratio, 4),
        "semantic_findings_steps": semantic_findings,
        "semantic_medium_high_steps": high_or_medium_hits,
    }


# ─── AI 分析提示词构建 ────────────────────────────────────────────────────────

def _build_prompt(results: list, stats: dict, context: dict = None) -> str:
    """构建发送给 LLM 的分析提示词"""

    # 序列化测试结果（限制总长度，避免超出 token 限制）
    results_text_parts = []
    total_chars = 0
    MAX_TOTAL = 30000  # 约 7500 token，安全范围

    for i, r in enumerate(results, 1):
        part = (
            f"【用例 {i}】名称: {r['test_name']}\n"
            f"  全路径: {r['full_name']}\n"
            f"  Feature: {r['feature']} | Story: {r['story']}\n"
            f"  状态: {r['status']}\n"
        )
        if r["failure_msg"]:
            part += f"  失败信息: {r['failure_msg'][:300]}\n"
        if r["failure_trace"]:
            part += f"  失败堆栈: {r['failure_trace'][:300]}\n"
        if r["log_snippet"]:
            part += f"  日志片段: {r['log_snippet'][:200]}\n"

        # 步骤摘要（只收集步骤名和状态）
        step_lines = []
        for s in r["steps"]:
            line = f"    - [{s['status']}] {s['step']}"
            # 附加一部分 response（帮助 LLM 判断断言质量）
            for resp in s.get("response", []):
                line += f"\n      响应: {resp[:300]}"
            for sem in s.get("semantic", []):
                line += f"\n      语义断言: {sem[:300]}"
            step_lines.append(line)
        if step_lines:
            part += "  步骤:\n" + "\n".join(step_lines[:15]) + "\n"  # 最多 15 步

        if total_chars + len(part) > MAX_TOTAL:
            results_text_parts.append("...[更多用例因长度限制已省略]")
            break
        results_text_parts.append(part)
        total_chars += len(part)

    results_text = "\n".join(results_text_parts)

    context = context or _build_prompt_context(results)
    candidates = context.get("false_pass_candidates", [])
    gap_candidates = context.get("gap_candidates", [])
    observability_metrics = context.get("observability_metrics", {})
    semantic_observability = context.get("semantic_observability", {})
    candidate_text = _render_candidate_text(candidates, "疑似假通过候选")
    gap_candidate_text = _render_candidate_text(gap_candidates, "证据盲区候选")

    prompt = f"""你是一位资深自动化测试专家，请分析以下 pytest + allure 接口自动化测试报告，给出专业、有价值的中文分析意见。

## 测试统计
- 总用例数: {stats['total']}
- 通过(passed): {stats['passed']}
- 失败(failed): {stats['failed']}
- 异常(broken): {stats['broken']}
- 跳过(skipped): {stats['skipped']}

## 测试结果详情
{results_text}

## 基于证据的疑似假通过候选（程序预筛）
{candidate_text}

## 证据盲区候选（用于评估可观测性，不等同于假通过）
{gap_candidate_text}

## 可观测性统计
{json.dumps(observability_metrics, ensure_ascii=False)}

说明：`assertion_signal_coverage` / `step_name_assertion_signal_coverage` 表示“步骤名称包含断言关键词的覆盖率”，用于衡量可读性，不等价于真实断言覆盖率。
说明：每个步骤/子步骤均视为一个接口调用单元，`interface_analysis_coverage` 表示接口级可观测覆盖率。

## 证据索引说明
- 你可引用的证据ID来自候选中的 `[E***]`。
- 同一 `evidence_id` 可能在多个候选复用（表示同源证据）。

## 语义断言引擎上下文（已接入）
- 本项目已接入语义感知断言引擎，接口请求会自动附带 `Semantic Assertion` 附件。
- 你不能建议“引入语义断言引擎”，只能评估“语义断言覆盖质量、证据质量、规则有效性”。
- 若语义证据不足，请明确写“语义证据可观测性不足”，并指出缺失证据类型。
- 语义可观测性统计：{json.dumps(semantic_observability, ensure_ascii=False)}

---
请按以下结构输出分析报告：

### 一、整体质量评估
必须按以下三个小节输出，并给出评分（1-10分）：
- 1) 事实：仅允许引用“可观测性统计字段”或 evidence_id，逐条标注来源。
- 2) 推断：基于事实得出结论，逐条标注置信度（高/中/低）。
- 3) 建议：与推断一一对应，必须可执行，禁止泛化口号。
- 对 `assertion_signal_coverage` 的解读必须注明“这是步骤命名信号，不代表真实断言执行覆盖率”。

### 二、⚠️ 疑似"假通过"用例（passed 但可能实际失败）
仅允许从"基于证据的疑似假通过候选（程序预筛）"中选择，用例必须有可验证证据；禁止仅凭"日志未展示/步骤较少/猜测断言不足"判定。逐条说明：
- 用例名称
- 证据（必须引用 evidence_id，如 E003）
- 疑似原因
- 改进建议

如果候选为空或证据不足，请写"未发现可验证的假通过用例"，不要给经验性提示。
对“已存在/重复创建/冲突”这类可预期分支，如无其他失败证据，不应判定为假通过。

### 三、🔴 失败/异常用例根因分析
对 failed/broken 用例逐条分析根本原因（环境问题？断言错误？接口变更？数据依赖？），并给出修复建议。

若 failed/broken 为空，请输出“本次无 failed/broken 用例”。

补充约束：
- 禁止输出“建议引入语义断言引擎/语义规则”这类已完成事项。
- 允许输出“优化现有语义规则配置、可观测性、阈值、抑制规则治理”的建议。
- 输出中每个“事实判断”都必须给出 evidence_id；若没有匹配 evidence_id，必须明确写“证据不足，不下结论”。
- 禁止使用“致命/严重”等绝对词，除非同一结论至少引用 2 条独立 evidence_id。
- 若多个结论引用同一 evidence_id，不要重复粘贴证据原文，保持结论去重。
- 每个章节优先使用短列表：每章最多 5 条主点，每条不超过 2 行，避免长段落叙述。

注意：仅输出以上三个章节，不要额外扩展其他章节。
"""
    return prompt


def _extract_evidence_ids_from_prompt(prompt: str) -> set:
    """从 prompt 中提取可引用 evidence_id 集合。"""
    return set(re.findall(r"\bE\d{3}\b", str(prompt or "")))


def _validate_analysis_report(analysis_text: str, allowed_evidence_ids: set) -> tuple[bool, list]:
    """校验 AI 报告是否满足结构与证据引用约束。"""
    text = str(analysis_text or "")
    issues = []

    # 允许 emoji/引号细微差异，按关键片段校验章节存在性。
    section_checks = [
        ("### 一、", ["整体质量评估"]),
        ("### 二、", ["疑似", "假通过"]),
        ("### 三、", ["失败/异常", "根因分析"]),
    ]
    for sec_prefix, keywords in section_checks:
        start = text.find(sec_prefix)
        if start < 0:
            issues.append(f"缺少章节前缀: {sec_prefix}")
            continue
        window = text[start:start + 120]
        if not all(k in window for k in keywords):
            issues.append(f"章节标题不符合预期: {sec_prefix}{' '.join(keywords)}")

    if "建议引入语义断言引擎" in text or "建议引入语义规则" in text:
        issues.append("包含禁用建议：引入语义断言/语义规则")

    used_ids = set(re.findall(r"\bE\d{3}\b", text))
    invalid_ids = sorted([eid for eid in used_ids if eid not in allowed_evidence_ids])
    if invalid_ids:
        issues.append(f"引用了不存在的 evidence_id: {', '.join(invalid_ids[:10])}")

    # 若存在可引用证据，要求报告至少引用1个 evidence_id，或明确声明证据不足。
    if allowed_evidence_ids:
        has_evidence_ref = any(eid in text for eid in allowed_evidence_ids)
        has_insufficient_note = "证据不足" in text or "未发现可验证的假通过用例" in text
        if not has_evidence_ref and not has_insufficient_note:
            issues.append("存在可引用证据但报告未引用 evidence_id")

    # 对绝对词做硬约束：出现时至少给出2个独立 evidence_id。
    absolute_words = ["致命", "严重"]
    if any(w in text for w in absolute_words) and len(used_ids) < 2:
        issues.append("使用了绝对词（致命/严重）但未提供至少2条独立 evidence_id")

    return len(issues) == 0, issues


def _build_repair_prompt(original_prompt: str, bad_output: str, issues: list) -> str:
    """构建修复提示词：仅修正格式与证据引用，不重写业务输入。"""
    issue_text = "\n".join([f"- {x}" for x in issues]) if issues else "- 未知问题"
    return (
        "你上一次输出不合规，请仅基于原始输入重新输出，严格满足格式与证据约束。\n\n"
        "【不合规问题】\n"
        f"{issue_text}\n\n"
        "【修复要求】\n"
        "1) 只输出指定三章，不要增加新章节。\n"
        "2) 每个事实判断必须引用 evidence_id（如 E003）；若无证据，明确写‘证据不足，不下结论’。\n"
        "3) 禁止建议‘引入语义断言引擎/语义规则’。\n"
        "4) 若使用‘致命/严重’，需至少引用2条独立 evidence_id。\n"
        "5) 保持中文、简洁、可执行。\n\n"
        "【你上次的输出（供纠错参考）】\n"
        f"{bad_output[:6000]}\n\n"
        "【原始任务输入（必须以此为准）】\n"
        f"{original_prompt}"
    )


# ─── LLM API 调用 ─────────────────────────────────────────────────────────────

def call_llm_api(prompt: str, cfg: dict) -> str:
    """调用配置的 LLM API，返回分析文本"""
    headers = {
        "Authorization": f"Bearer {cfg['token']}",
        "Content-Type":  "application/json",
    }
    payload = {
        "model": cfg["model"],
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是一位专业的自动化测试专家，擅长分析 pytest/allure 接口测试报告，"
                    "能够发现假通过用例、提供优化建议，输出语言为中文。"
                )
            },
            {
                "role": "user",
                "content": prompt
            }
        ],
        "temperature": 0.2,
        "max_tokens":  3000,
    }

    try:
        MyLog.info(f"AI分析：正在调用 {cfg.get('provider_display', cfg.get('provider', 'LLM'))} {cfg['model']} API...")
        resp = requests.post(
            cfg["api_url"],
            headers=headers,
            json=payload,
            timeout=cfg["timeout"]
        )
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        MyLog.info("AI分析：API 调用成功")
        return content
    except requests.exceptions.Timeout:
        msg = f"AI分析：API 请求超时（{cfg['timeout']}s），请检查网络连接"
        MyLog.error(msg)
        return f"❌ {msg}"
    except requests.exceptions.HTTPError as e:
        msg = f"AI分析：{cfg.get('provider', 'llm')} API HTTP 错误 {e.response.status_code}: {e.response.text[:500]}"
        MyLog.error(msg)
        return f"❌ {msg}"
    except Exception as e:
        msg = f"AI分析：{cfg.get('provider', 'llm')} API 调用异常: {e}"
        MyLog.error(msg)
        return f"❌ {msg}"


def call_github_models_api(prompt: str, cfg: dict) -> str:
    """兼容旧调用名：内部转发到通用 LLM 调用。"""
    return call_llm_api(prompt, cfg)


# ─── 报告保存 ─────────────────────────────────────────────────────────────────

def _cleanup_old_reports(directory: str, pattern: str, keep: int = 3) -> None:
    """
    清理旧报告，仅保留最新 `keep` 份匹配 `pattern` 的文件。
    按文件名（含时间戳）降序排列，超出部分删除。
    """
    matched = sorted(glob.glob(os.path.join(directory, pattern)), reverse=True)
    to_delete = matched[keep:]
    for path in to_delete:
        try:
            os.remove(path)
            MyLog.info(f"已清理旧报告: {path}")
        except Exception as e:
            MyLog.warning(f"清理旧报告失败 {path}: {e}")


def save_analysis_report(
    analysis_text: str,
    stats: dict,
    output_dir: str,
    keep: int = 3,
    model_name: str = "",
    provider_name: str = "LLM",
    quick_overview: str = "",
) -> str:
    """将 AI 分析结果保存为 Markdown 文件，并清理超出 keep 份的旧报告"""
    os.makedirs(output_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    file_name = f"ai_analysis_{timestamp}.md"
    file_path = os.path.join(output_dir, file_name)

    model_for_display = model_name or "unknown"
    provider_for_display = provider_name or "LLM"

    header = (
        f"# 🤖 AI 测试报告分析\n\n"
        f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  \n"
        f"> 分析模型：{provider_for_display} ({model_for_display})  \n"
        f"> 测试统计：总计 {stats['total']} 条 | "
        f"通过 {stats['passed']} | 失败 {stats['failed']} | "
        f"异常 {stats['broken']} | 跳过 {stats['skipped']}\n\n"
        f"---\n\n"
    )

    with open(file_path, "w", encoding="utf-8") as f:
        body = header
        if quick_overview:
            body += quick_overview
        body += analysis_text
        f.write(body)

    MyLog.info(f"AI分析报告已保存: {file_path}")
    # 只清理带时间戳的历史文件（以年份开头，避免误删 *_latest.md 等固定文件）
    _cleanup_old_reports(output_dir, "ai_analysis_2*.md", keep=keep)
    return file_path


# ─── 主入口 ───────────────────────────────────────────────────────────────────

def analyze_test_report(allure_results_dir: str, report_output_dir: str = None) -> str:
    """
    主函数：分析 allure-results 目录并生成 AI 分析报告

    :param allure_results_dir:  allure-results 目录路径
    :param report_output_dir:   AI 报告输出目录（默认与 allure-results 同级的 ai-analysis 目录）
    :return:                    分析报告文件路径
    """
    MyLog.info("=" * 60)
    MyLog.info("🤖 开始 AI 自动分析测试报告...")
    MyLog.info("=" * 60)

    # 确定输出目录
    if report_output_dir is None:
        report_output_dir = os.path.join(os.path.dirname(allure_results_dir), "ai-analysis")

    # 加载配置
    cfg = _load_ai_config()
    if not cfg["token"]:
        msg = "AI分析：未配置 LLM API Key，跳过 AI 分析"
        MyLog.warning(msg)
        print(f"\n⚠️  {msg}")
        return ""

    # 收集测试结果
    results = collect_test_results(allure_results_dir)
    if not results:
        msg = "AI分析：没有可分析的测试结果"
        MyLog.warning(msg)
        print(f"\n⚠️  {msg}")
        return ""

    stats = _build_statistics(results)
    MyLog.info(
        f"AI分析：收集到 {stats['total']} 条用例 "
        f"(passed={stats['passed']}, failed={stats['failed']}, "
        f"broken={stats['broken']}, skipped={stats['skipped']})"
    )

    # 构建提示词并调用 API
    prompt_context = _build_prompt_context(results)
    prompt = _build_prompt(results, stats, context=prompt_context)
    analysis = call_llm_api(prompt, cfg)

    # 对 AI 输出做一次合规校验，不合规时自动重试一次纠偏。
    allowed_evidence_ids = _extract_evidence_ids_from_prompt(prompt)
    valid, issues = _validate_analysis_report(analysis, allowed_evidence_ids)
    if not valid:
        MyLog.warning("AI分析：首次输出不合规，触发一次自动纠偏重试")
        MyLog.warning("AI分析：不合规项 -> " + " | ".join(issues))
        repair_prompt = _build_repair_prompt(prompt, analysis, issues)
        repaired = call_llm_api(repair_prompt, cfg)
        repaired_valid, repaired_issues = _validate_analysis_report(repaired, allowed_evidence_ids)
        if repaired_valid:
            analysis = repaired
            MyLog.info("AI分析：纠偏重试成功，已采用修复后的报告")
        else:
            MyLog.warning("AI分析：纠偏重试后仍存在不合规项 -> " + " | ".join(repaired_issues))

    # 保存报告
    report_path = save_analysis_report(
        analysis,
        stats,
        report_output_dir,
        model_name=cfg.get("model", ""),
        provider_name=cfg.get("provider_display", cfg.get("provider", "LLM")),
        quick_overview=_build_quick_overview(stats, prompt_context),
    )
    print(f"\n\033[32m📄 AI测试报告分析: {report_path}\033[0m\n")
    MyLog.info("🤖 AI 测试报告分析完成")

    return report_path



