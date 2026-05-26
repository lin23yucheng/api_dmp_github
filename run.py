"""
顺序执行入口，仅保留顺序执行，并保留 AI 报告能力。
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from common.Log import MyLog, set_log_level
from AI.ai_analyzer import analyze_test_report
from AI.semantic_assertion_engine import (
    cleanup_semantic_runtime,
    generate_semantic_summary_report,
    get_semantic_report_dir,
)


ROOT_DIR = Path(__file__).resolve().parent
set_log_level("info")

# 默认顺序执行列表
DEFAULT_TEST_FILES = [
    "testcase/test_global_kanban.py",
    "testcase/test_project_management.py",
]


def format_time(seconds: float) -> str:
    """将秒数格式化为 X分X秒。"""
    minutes = int(seconds // 60)
    remain_seconds = int(seconds % 60)
    return f"{minutes}分{remain_seconds}秒"


def resolve_allure_command():
    """解析可用的 allure 命令。"""
    custom_cmd = os.environ.get("ALLURE_CMD", "").strip()
    if custom_cmd and Path(custom_cmd).exists():
        return custom_cmd

    in_path = shutil.which("allure")
    if in_path:
        return in_path

    allure_home = os.environ.get("ALLURE_HOME", "").strip()
    if allure_home:
        candidate = Path(allure_home) / "bin" / ("allure.bat" if os.name == "nt" else "allure")
        if candidate.exists():
            return str(candidate)

    candidates = [
        Path("D:/Pycharm/allure-2.34.0/bin/allure.bat"),
        Path("C:/allure/bin/allure.bat"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)

    return None


def generate_allure_report(allure_results: Path, allure_report: Path) -> int:
    """生成 Allure HTML 报告。"""
    allure_cmd = resolve_allure_command()
    if not allure_cmd:
        MyLog.error("未找到allure命令，请先安装Allure Commandline并加入PATH。")
        print("未找到allure命令，请先安装Allure Commandline并加入PATH。")
        return 127

    cmd = [allure_cmd, "generate", str(allure_results), "-o", str(allure_report), "-c"]
    print("执行命令:", " ".join(cmd))
    result = subprocess.run(cmd, cwd=ROOT_DIR)
    return result.returncode


def parse_tests_arg(raw_tests: str):
    """解析 --tests 参数（支持逗号/分号/空格分隔）。"""
    if not raw_tests.strip():
        return []
    return [item.replace("\\", "/") for item in re.split(r"[,;\s]+", raw_tests) if item.strip()]


def execute_test(test_file: str, allure_results: Path) -> int:
    """执行单个测试文件。"""
    target_path = (ROOT_DIR / test_file).resolve()
    if not target_path.exists():
        MyLog.error(f"错误：测试文件 {target_path} 不存在！")
        print(f"[ERROR] 测试文件不存在: {target_path}")
        return 2

    test_name = target_path.stem
    test_workspace = allure_results.parent / "workspaces" / test_name
    test_workspace.mkdir(parents=True, exist_ok=True)

    os.environ["TEST_WORKSPACE"] = str(test_workspace)
    os.environ["CURRENT_TEST_FILE"] = test_file

    log_file = test_workspace / "pytest.log"
    pytest_args = [
        "-v", "-s", "-x",
        str(target_path),
        f"--alluredir={allure_results}",
        f"--log-file={log_file}",
    ]

    MyLog.info(f"开始执行测试文件: {test_file}")
    exit_code = pytest.main(pytest_args)

    os.environ.pop("TEST_WORKSPACE", None)
    os.environ.pop("CURRENT_TEST_FILE", None)

    if exit_code == 0:
        MyLog.info(f"测试文件 {test_file} 全部通过")
    elif exit_code == 1:
        MyLog.error(f"测试文件 {test_file} 存在失败用例")
    else:
        MyLog.critical(f"测试文件 {test_file} 执行错误，退出码: {exit_code}")

    return exit_code


def update_environment_widget(allure_report: Path, formatted_overall: str):
    """向 Allure 报告写入执行时间信息。"""
    report_env_file = allure_report / "widgets" / "environment.json"
    if not report_env_file.exists():
        return

    try:
        with report_env_file.open("r", encoding="utf-8") as f:
            env_data = json.load(f)

        if not isinstance(env_data, list):
            env_data = []

        env_data.append({
            "name": "执行时间",
            "values": [f"整体耗时: {formatted_overall}"],
        })

        with report_env_file.open("w", encoding="utf-8") as f:
            json.dump(env_data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        MyLog.error(f"更新Allure环境信息失败: {e}")


def run_order_tests(test_files, allure_results: Path, allure_report: Path, stop_on_fail: bool = True) -> int:
    """顺序执行测试（失败即停），并在结束后生成报告。"""
    MyLog.info("===== 开始执行测试任务 =====")

    start_time = time.time()
    file_times = {}
    total_elapsed_time = 0.0
    final_exit_code = 0

    def generate_report_with_ai() -> int:
        formatted_overall = format_time(time.time() - start_time)

        allure_code = generate_allure_report(allure_results, allure_report)
        if allure_code == 0:
            update_environment_widget(allure_report, formatted_overall)
            MyLog.info(f"测试报告生成成功: {allure_report / 'index.html'}")
            print(f"Allure HTML报告已生成: {allure_report}")

        # AI 分析
        try:
            ai_output_dir = ROOT_DIR / "report" / "ai-analysis"
            analyze_test_report(str(allure_results), str(ai_output_dir))
            MyLog.info(f"AI分析报告生成成功: {ai_output_dir}")
        except Exception as ai_err:
            MyLog.warning(f"AI分析报告生成失败（不影响正常流程）: {ai_err}")

        # 语义问题汇总
        try:
            semantic_report_dir = get_semantic_report_dir()
            semantic_summary_path = generate_semantic_summary_report(semantic_report_dir, use_llm_review=True)
            MyLog.info(f"语义问题汇总报告生成成功: {semantic_summary_path}")
            print(f"\033[32m📄 语义问题汇总报告: {semantic_summary_path}\033[0m\n")
        except Exception as semantic_err:
            MyLog.warning(f"语义问题汇总报告生成失败（不影响正常流程）: {semantic_err}")

        return allure_code

    try:
        for test_file in test_files:
            file_start_time = time.time()
            exit_code = execute_test(test_file, allure_results)

            file_elapsed = time.time() - file_start_time
            total_elapsed_time += file_elapsed
            file_times[test_file] = file_elapsed

            MyLog.info(f"测试文件 {test_file} 执行完成，耗时: {format_time(file_elapsed)}")
            print(f"[INFO] {test_file} 执行耗时: {format_time(file_elapsed)}")

            if exit_code != 0:
                final_exit_code = exit_code
                failure_type = "测试用例失败" if exit_code == 1 else "框架错误"
                MyLog.critical(f"执行测试文件 {test_file} 发生{failure_type}，程序终止")
                print(f"[ERROR] {test_file} 执行失败，退出码: {exit_code}")
                if stop_on_fail:
                    print("[INFO] 已启用失败即停止，终止后续文件执行。")
                    break

            MyLog.info("===== 测试文件耗时明细 =====")
            for file_name, cost in file_times.items():
                MyLog.info(f"{file_name}: {format_time(cost)}")
            MyLog.info(f"测试文件累加总耗时: {format_time(total_elapsed_time)}")

    except KeyboardInterrupt:
        MyLog.info("用户中断测试执行")
        print("\n用户中断测试执行，正在生成报告...")
        if final_exit_code == 0:
            final_exit_code = 130

    finally:
        if allure_results.exists() and any(allure_results.iterdir()):
            allure_code = generate_report_with_ai()
        else:
            allure_code = 0
            MyLog.info("没有生成测试结果，跳过报告生成")
            print("[WARN] 未检测到Allure结果文件，跳过HTML报告生成。")

        overall = format_time(time.time() - start_time)
        MyLog.info(f"整体耗时: {overall}")
        print(f"[INFO] 整体耗时: {overall}")
        MyLog.info("===== 测试任务完成 =====")

    if final_exit_code != 0:
        return final_exit_code
    return allure_code


def main():
    parser = argparse.ArgumentParser(description="顺序执行pytest并生成Allure + AI报告")
    parser.add_argument(
        "--tests",
        default="",
        help="按顺序执行的测试文件列表，支持逗号/分号/空格分隔；为空时使用默认列表",
    )
    parser.add_argument("--allure-results", default="report/allure-results", help="allure原始结果目录")
    parser.add_argument("--allure-report", default="report/allure-report", help="allure HTML报告目录")
    parser.add_argument("--keep-results", action="store_true", help="保留历史allure结果目录")
    parser.add_argument("--no-stop-on-fail", action="store_true", help="失败后继续执行后续测试文件")
    args = parser.parse_args()

    # 传入 --tests 时覆盖默认列表；否则使用顶部固定执行列表 DEFAULT_TEST_FILES
    if args.tests.strip():
        test_files = parse_tests_arg(args.tests)
    else:
        test_files = DEFAULT_TEST_FILES

    allure_results = ROOT_DIR / args.allure_results
    allure_report = ROOT_DIR / args.allure_report

    # 每次新执行前清理语义runtime数据，防止历史审计记录混入本轮汇总。
    semantic_report_dir = get_semantic_report_dir()
    removed_runtime_files = cleanup_semantic_runtime(semantic_report_dir)
    if removed_runtime_files > 0:
        MyLog.info(f"已清理语义runtime历史文件: {removed_runtime_files} 个")

    if not args.keep_results and allure_results.exists():
        shutil.rmtree(allure_results)
    allure_results.mkdir(parents=True, exist_ok=True)

    if allure_report.exists():
        shutil.rmtree(allure_report)

    exit_code = run_order_tests(
        test_files=test_files,
        allure_results=allure_results,
        allure_report=allure_report,
        stop_on_fail=not args.no_stop_on_fail,
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
