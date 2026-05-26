"""
AI 测试报告分析器
使用 GitHub Models API 自动分析 Allure 测试报告
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
    default_model = "gpt-4o"

    # 优先尝试从 ini 读取默认模型，避免代码内固定模型名称。
    if os.path.exists(ini_path):
        try:
            import configparser
            parser = configparser.ConfigParser()
            parser.read(ini_path, encoding="utf-8")
            sec = "ai_model"
            if parser.has_section(sec):
                default_model = parser.get(sec, "github_model", fallback=default_model)
        except Exception as e:
            MyLog.warning(f"AI分析：读取默认模型配置失败 {ini_path}: {e}")

    cfg = {
        "provider":  "github",
        "token":     "",
        "model":     default_model,
        "api_url":   "https://models.inference.ai.azure.com/chat/completions",
        "timeout":   int(os.environ.get("AI_ANALYZER_TIMEOUT", "120")),
        "secret_env_file": secret_env_path,
    }

    # 1) 先读取 ini 的非敏感默认项
    if os.path.exists(ini_path):
        import configparser
        parser = configparser.ConfigParser()
        parser.read(ini_path, encoding="utf-8")
        sec = "ai_model"
        if parser.has_section(sec):
            cfg["provider"] = parser.get(sec, "llm_provider", fallback=cfg["provider"])
            cfg["model"] = parser.get(sec, "github_model", fallback=cfg["model"])
            cfg["api_url"] = parser.get(sec, "github_models_api_url", fallback=cfg["api_url"])
            cfg["secret_env_file"] = parser.get(sec, "secret_env_file", fallback=cfg["secret_env_file"])
            # 兼容旧配置：仅在不是占位值时才使用 ini token
            ini_token = parser.get(sec, "github_token", fallback="").strip()
            if ini_token and "your_github_token_here" not in ini_token.lower():
                cfg["token"] = ini_token

    # 2) 读取本地密钥文件（默认 config/secret.env，可被 ini 覆盖）
    secret_file = str(cfg.get("secret_env_file", secret_env_path))
    if secret_file and not os.path.isabs(secret_file):
        secret_file = os.path.join(base_dir, secret_file)
    secret_values = _load_env_file(secret_file)
    if secret_values.get("GITHUB_TOKEN"):
        cfg["token"] = secret_values["GITHUB_TOKEN"]

    # 3) 最后用系统环境变量覆盖（最高优先级）
    cfg["provider"] = os.environ.get("LLM_PROVIDER", cfg["provider"])
    cfg["model"] = os.environ.get("GITHUB_MODEL", cfg["model"])
    cfg["api_url"] = os.environ.get("GITHUB_MODELS_API_URL", cfg["api_url"])
    if os.environ.get("GITHUB_TOKEN"):
        cfg["token"] = os.environ["GITHUB_TOKEN"]


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
        for att in attachments:
            att_name = att.get("name", "")
            if "Response" in att_name or "response" in att_name:
                content = _read_attachment(att.get("source", ""), allure_results_dir, 600)
                if content:
                    response_snippets.append(f"[{att_name}]: {content}")
            if "Semantic Assertion" in att_name:
                content = _read_attachment(att.get("source", ""), allure_results_dir, 800)
                if content:
                    semantic_snippets.append(f"[{att_name}]: {content}")
        if response_snippets:
            entry["response"] = response_snippets[:2]  # 最多 2 个
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
            for resp in step.get("response", []):
                payload = _parse_response_payload(resp)
                if isinstance(payload, dict):
                    msg = str(payload.get("msg", "") or payload.get("message", ""))
                    msg_lower = msg.lower()
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

def _build_prompt(results: list, stats: dict) -> str:
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

    candidates = _collect_false_pass_candidates(results)
    semantic_observability = _collect_semantic_observability(results)
    if candidates:
        candidate_lines = []
        for i, c in enumerate(candidates, 1):
            candidate_lines.append(
                f"[{i}] {c['test_name']} | confidence={c['confidence']} | score={c['score']}\n"
                f"    full_name={c['full_name']}\n"
                + "\n".join([f"    evidence: {e}" for e in c.get("evidence", [])])
            )
        candidate_text = "\n".join(candidate_lines)
    else:
        candidate_text = "未发现可验证证据候选。"

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

## 语义断言引擎上下文（已接入）
- 本项目已接入语义感知断言引擎，接口请求会自动附带 `Semantic Assertion` 附件。
- 你不能建议“引入语义断言引擎”，只能评估“语义断言覆盖质量、证据质量、规则有效性”。
- 若语义证据不足，请明确写“语义证据可观测性不足”，并指出缺失证据类型。
- 语义可观测性统计：{json.dumps(semantic_observability, ensure_ascii=False)}

---
请按以下结构输出分析报告：

### 一、整体质量评估
简述本次测试的整体质量，评分（1-10分）及理由。

### 二、⚠️ 疑似"假通过"用例（passed 但可能实际失败）
仅允许从"基于证据的疑似假通过候选（程序预筛）"中选择，用例必须有可验证证据；禁止仅凭"日志未展示/步骤较少/猜测断言不足"判定。逐条说明：
- 用例名称
- 证据（引用候选中的原始证据行）
- 疑似原因
- 改进建议

如果候选为空或证据不足，请写"未发现可验证的假通过用例"，不要给经验性提示。

### 三、🔴 失败/异常用例根因分析
对 failed/broken 用例逐条分析根本原因（环境问题？断言错误？接口变更？数据依赖？），并给出修复建议。

补充约束：
- 禁止输出“建议引入语义断言引擎/语义规则”这类已完成事项。
- 允许输出“优化现有语义规则配置、可观测性、阈值、抑制规则治理”的建议。

注意：仅输出以上三个章节，不要额外扩展其他章节。
"""
    return prompt


# ─── GitHub Models API 调用 ───────────────────────────────────────────────────

def call_github_models_api(prompt: str, cfg: dict) -> str:
    """调用 GitHub Models API，返回分析文本"""
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
        "temperature": 0.3,
        "max_tokens":  3000,
    }

    try:
        MyLog.info(f"AI分析：正在调用 {cfg['model']} API...")
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
        msg = f"AI分析：API HTTP 错误 {e.response.status_code}: {e.response.text[:500]}"
        MyLog.error(msg)
        return f"❌ {msg}"
    except Exception as e:
        msg = f"AI分析：API 调用异常: {e}"
        MyLog.error(msg)
        return f"❌ {msg}"


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


def save_analysis_report(analysis_text: str, stats: dict, output_dir: str, keep: int = 3, model_name: str = "") -> str:
    """将 AI 分析结果保存为 Markdown 文件，并清理超出 keep 份的旧报告"""
    os.makedirs(output_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    file_name = f"ai_analysis_{timestamp}.md"
    file_path = os.path.join(output_dir, file_name)

    model_for_display = model_name or "unknown"

    header = (
        f"# 🤖 AI 测试报告分析\n\n"
        f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  \n"
        f"> 分析模型：GitHub Models ({model_for_display})  \n"
        f"> 测试统计：总计 {stats['total']} 条 | "
        f"通过 {stats['passed']} | 失败 {stats['failed']} | "
        f"异常 {stats['broken']} | 跳过 {stats['skipped']}\n\n"
        f"---\n\n"
    )

    with open(file_path, "w", encoding="utf-8") as f:
        f.write(header + analysis_text)

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
        msg = "AI分析：未配置 GITHUB_TOKEN，跳过 AI 分析"
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
    prompt   = _build_prompt(results, stats)
    analysis = call_github_models_api(prompt, cfg)

    # 保存报告
    report_path = save_analysis_report(analysis, stats, report_output_dir, model_name=cfg.get("model", ""))
    print(f"\n\033[32m📄 AI测试报告分析: {report_path}\033[0m\n")
    MyLog.info("🤖 AI 测试报告分析完成")

    return report_path



