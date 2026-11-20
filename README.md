# DMP API 自动化测试

本项目用于 DMP 后端接口的业务流程回归。它用 pytest 串联登录、全局看板查询和项目管理操作，通过 PostgreSQL 查询核对部分接口返回，并用 Allure 记录测试步骤及请求、响应附件。仓库也提供 OpenAPI 文档差异与迁移工具、接口用例引用分析，以及响应语义检查和可选的大模型报告分析。

## 当前测试场景

| 测试文件 | 验证内容 |
| --- | --- |
| [test_global_kanban.py](testcase/test_global_kanban.py) | 进行中、已结项及全部项目的类型统计，近 12 个月项目趋势，行业分布和最近项目。 |
| [test_project_management.py](testcase/test_project_management.py) | 项目空间创建、编辑、结项和重启，成员与机器的添加和移除、机器 Token 下载、产品新增与编辑和删除，以及项目总览。 |

## 快速开始

以下命令在仓库根目录通过 Windows PowerShell 执行，示例使用 Python 3.10。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install pytest pytest-order allure-pytest requests psycopg2-binary
Copy-Item .\config\env_config.example.ini .\config\env_config.ini
```

1. 在 `config/env_config.ini` 的 `[environment]` 中将 `execution_env` 设为 `fat`，并在 `[fat-PostgreSQL]` 填写 `host`、`port`、`user_name`、`password` 和 `database`。代码也能读取 `prod` 及对应的 `[prod-PostgreSQL]`。DMP 登录地址及登录参数按环境从 `api/api_login.py` 的映射读取，运行前确认该映射适用于目标环境。
2. 确认当前机器可以访问 DMP 服务和 PostgreSQL，且登录账号能访问预置空间 `Test_zdh`，环境中有可选成员和机器。项目管理用例会修改固定空间及其业务数据，并写入 `testdata/Token.txt`。以下运行示例以 FAT 测试环境为前提。
3. 生成 HTML 报告需要 Java 和 Allure Commandline。将 `allure` 加入 `PATH`，或设置 `ALLURE_CMD` 指向可执行文件，或设置 `ALLURE_HOME` 指向 Allure 安装目录。

```powershell
# 按默认顺序运行全局看板和项目管理
.\.venv\Scripts\python.exe .\run.py

# 只运行全局看板
.\.venv\Scripts\python.exe .\run.py --tests testcase/test_global_kanban.py
```

`--tests` 接受逗号、分号或空格分隔的文件列表。默认遇到失败时结束当前文件并停止后续文件。`--no-stop-on-fail` 允许继续执行后续文件，当前文件仍会在首个失败处停止。`--keep-results` 保留历史 Allure 原始结果，HTML 报告会重新生成。

## 报告与日志

| 默认路径 | 内容 |
| --- | --- |
| `report/allure-results/` | pytest 产生的 Allure 原始结果及附件。 |
| `report/allure-report/index.html` | Allure HTML 报告，可用 `allure open .\report\allure-report` 查看。 |
| `report/workspaces/<测试文件名>/pytest.log` | 各测试文件的 pytest 日志，目录名取文件名去掉 `.py` 后的部分。 |
| `report/semantic/semantic_summary_latest.md` | 本轮请求的响应语义问题汇总。 |
| `report/ai-analysis/ai_analysis_*.md` | 配置大模型后生成的测试结果分析，包含失败线索、断言疑点及日志信息缺口。 |

## 接口文档差异与迁移

脚本默认比较仓库内的 `api/DMP接口文档.json` 和 `api/DMP接口文档_1.json`。报告记录接口地址变化、接口增删及定义变更，默认在 `report/` 只保留最新的 `api_doc_diff_*.md`。

```powershell
.\.venv\Scripts\python.exe .\generate_api_doc_diff_and_tests.py --no-llm
```

- 有差异时，脚本把接口封装复制到 `api_new/`，迁移同一 `operationId` 的 URL 变更，并为新增接口生成方法骨架。测试副本输出到 `testcase/test_*_1.py`，导入更新为 `api_new`，文件中附有待处理清单和 `[AUTO]` 提示。
- 对可识别的参数变更，脚本会尝试移除简单的已删除命名参数，复杂表达式会留下人工处理提示。运行迁移用例前，先检查生成的方法骨架和调用处。两份文档一致时只写报告。
- 默认启用大模型建议。有新增接口且配置有效密钥时，脚本将插入建议作为注释写入测试副本。`--no-llm` 关闭这一步。
- 可用 `--old-doc`、`--new-doc`、`--report-dir`、`--testcase-dir`、`--api-dir` 和 `--api-new-dir` 指定输入及输出路径。命令行输出的 JSON 包含报告路径、差异统计、生成文件和迁移结果。

## 接口用例引用分析

脚本静态分析 `api/*.py` 的接口封装与 `testcase/test_*.py` 的引用关系，列出已引用、未找到引用和排除的接口。结果用于查找可能缺少用例的接口，统计依据是源码中可识别的调用关系。

```powershell
.\.venv\Scripts\python.exe .\generate_api_unused_report.py --no-ai
```

默认在 `report/` 生成 `api_unused_interfaces_*.md` 并保留最近一份，可用 `--keep` 调整保留数量。需要排除接口时，可将 `config/api_excluded_interfaces.example.json` 复制为 `config/api_excluded_interfaces.json`，在 `excluded_interfaces` 中登记 `method_name` 或 `chinese_name`，并按需填写 `module`、`class_name` 和 `reason`。去掉 `--no-ai` 后，脚本会尝试追加大模型分析。

## 响应语义检查与大模型配置

语义断言在请求执行时检查响应的业务状态、字段和历史基线差异，默认记录发现。可将 `config/semantic_assertion.example.ini` 复制为 `config/semantic_assertion.ini` 并设置 `strict = true`，让触发阻断规则的高风险响应导致用例失败。

```powershell
# 从已验证的 Allure 原始结果构建响应基线
.\.venv\Scripts\python.exe .\AI\run_semantic_assertion.py --build-baseline

# 从已有审计记录重新生成语义汇总，关闭大模型复核
.\.venv\Scripts\python.exe .\AI\run_semantic_assertion.py --summary --no-llm-review
```

如需单独检查已保存的 JSON 响应，可使用 `AI/run_semantic_assertion.py` 的 `--check` 和 `--json-file`，同时通过 `--method`、`--url` 和 `--status` 提供请求信息。默认基线文件为 `config/semantic_baseline.json`。

启用大模型功能时，将 `config/ai_config.example.ini` 复制为 `config/ai_config.ini`，将 `llm_provider` 设为 `github` 或 `deepseek`。密钥可在本地 `config/secret.env` 或环境变量中设置。GitHub Models 使用 `GITHUB_TOKEN`，DeepSeek 使用 `DEEPSEEK_API_KEY` 或 `APIKEYS_DS`。

## 目录结构

```text
api/          DMP 接口封装及两版 OpenAPI 文档
testcase/     全局看板与项目管理流程用例
repository/   用于断言的业务数据库查询
common/       HTTP 请求、断言、日志与数据库客户端
AI/           测试结果分析、语义断言及命令行工具
config/       环境、大模型、语义断言和接口排除清单的示例配置
run.py        顺序执行 pytest 并生成报告
```
