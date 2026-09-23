# DMP API 文档差异与自动迁移

## 能力说明

执行 `generate_api_doc_diff_and_tests.py` 后，流程如下：

1. 对比 `api/DMP接口文档.json` 与 `api/DMP接口文档_1.json`。
2. 在 `report/` 生成差异报告，仅保留最新一份 `api_doc_diff_*.md`。
3. 如果无差异：只输出报告，不生成 `api_new` 和 `*_1.py`。
4. 如果有差异：
   - 复制 `api/` 到 `api_new/`，并将内部 `from api import ...` 改为 `from api_new import ...`。
   - 动态发现已存在模块前缀；若出现全新前缀，会在 `api_new/` 自动创建新模块文件。
   - 按接口地址变更（同 operationId）自动迁移 `api_new` 中 URL。
   - 对真实新增接口在 `api_new` 对应模块追加方法骨架。
   - 生成 `testcase/*_1.py`，并将 import 改为 `api_new`。
   - 在 `*_1.py` 文件头添加“新增/删除/变更接口待处理清单”。
   - 对能识别出的删除/变更接口调用点自动插入 `[AUTO]` 注释提醒。
   - 对参数变更场景，会尝试自动删除“简单命名参数”（如 `x=1`）；复杂表达式（如 `x=str(v)`）仅提示人工处理。
   - 默认启用 LLM：调用 `config/ai_config.ini` 对应模型，为新增接口生成插入建议并写入 `*_1.py`（注释块形式，含置信度）。
   - 如需关闭 LLM，可传入 `--no-llm`。

## 快速运行

```powershell
python -u .\generate_api_doc_diff_and_tests.py
```

默认已开启大模型建议；如需显式关闭：

```powershell
python -u .\generate_api_doc_diff_and_tests.py --no-llm
```

## 可选参数

```powershell
python -u .\generate_api_doc_diff_and_tests.py \
  --old-doc .\api\DMP接口文档.json \
  --new-doc .\api\DMP接口文档_1.json \
  --report-dir .\report \
  --testcase-dir .\testcase \
  --api-dir .\api \
  --api-new-dir .\api_new
```

## 输出结果字段

- `report`: 最新差异报告路径
- `scripts_generated`: 是否生成 `*_1.py`
- `api_new_generated`: 是否生成 `api_new/`
- `generated_tests`: 生成的测试脚本文件列表
- `generated_api_files`: 生成的接口封装文件列表
- `api_migration`: URL 迁移统计
- `api_added_methods`: 新增接口方法骨架统计
- `llm_suggestions`: 大模型建议生成与落地结果
- `diff`: 差异统计（地址变更/新增/删除/定义变更）

## LLM 配置说明

- `generate_api_doc_diff_and_tests.py` 默认会调用大模型（前提是有新增接口且配置了有效 token）。
- 传入 `--no-llm` 可关闭大模型建议。
- 模型与地址读取自 `config/ai_config.ini`，密钥优先从 `config/secret.env` 或环境变量读取（GitHub: `GITHUB_TOKEN`；DeepSeek: `DEEPSEEK_API_KEY` / `APIKEYS_DS`）。

