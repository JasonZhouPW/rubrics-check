# Rubrics Evaluator

Rubrics JSON 质量评估工具，支持本地规则预检 + LLM 深度评估。

## 安装

```bash
pip install openai rich
```

## 快速开始

### 仅本地预检（不调用 LLM，毫秒级）

```bash
python rubrics_evaluator.py --input rubrics.json --no-llm
```

### LLM 评估（支持 OpenAI / Anthropic / Ollama）

```bash
# OpenAI
python rubrics_evaluator.py --input rubrics.json --api-key $API_KEY --model gpt-4o

# Anthropic Claude
python rubrics_evaluator.py \
  --input ./rubrics_dir/ \
  --base-url https://api.anthropic.com \
  --api-key sk-ant-xxx \
  --model claude-sonnet-4-20250514

# 本地 Ollama
python rubrics_evaluator.py \
  --input ./rubrics_dir/ \
  --base-url http://localhost:11434/v1 \
  --api-key ollama \
  --model qwen2.5:72b
```

### 批量评估目录

```bash
python rubrics_evaluator.py --input ./rubrics/ --recursive --api-key $API_KEY --model gpt-4o
```

## 输出

- 控制台彩色表格（pass/warn/fail 状态）
- `eval_result.json` — 详细结构化结果
- `eval_result.csv` — CSV 表格（ID、类型、状态、各检查项 pass/fail/N/A、issues）

```json
{
  "meta": { "generated_at": "...", "mode": "llm", "total_rubrics": 8 },
  "summary": { "pass": 7, "warn": 1, "fail": 0, "pass_rate": "87.5%" },
  "results": [
    {
      "id": "HC-1",
      "constraint_type": "hard",
      "binary_type": "binary",
      "checks": { "category_valid": true, "score_conditions_complete": true, "objectivity": true, ... },
      "status": "pass",
      "issues": [],
      "suggestion": ""
    }
  ]
}
```

## Rubrics 格式

输入 JSON 支持两种格式：

### 格式 1：rubric 块（推荐）

```json
{
  "rubric": {
    "Hard Constraint": [
      {
        "rubric_description": "Whether two independent Word (.docx) files are generated.",
        "score_0_condition": "Fewer than two files, wrong format, or content merged.",
        "score_1_condition": "Two separate .docx files generated with correct names.",
        "related_facts": "User requested two separate Word docs.",
        "facts_reference": ""
      }
    ],
    "Soft Constraint": [...],
    "Optional Constraint": [...]
  }
}
```

### 格式 2：扁平数组

```json
[
  {
    "ID": "HC-1",
    "rubric_description": "...",
    "score_0_condition": "...",
    "score_1_condition": "...",
    "related_facts": "...",
    "facts_reference": ""
  }
]
```

### 字段说明

| 字段 | 说明 | 要求 |
|------|------|------|
| `ID` | 唯一标识 | 可选（未填则自动生成） |
| `rubric_description` | 评分项描述 | 必须 |
| `score_N_condition` | 各档位评分条件 | 依类型而定 |
| `related_facts` | 可验证事实描述 | 建议填写 |
| `facts_reference` | 事实来源引用 | related_facts 存在时必填 |

**Binary vs. Non-binary 由字段自动判断**：存在 `score_2/3/4_condition` → Non-binary，否则 → Binary。

**Hard Constraint 必须为 Binary**（只有 `score_0_condition` + `score_1_condition`）。

**Non-binary** 至少需要 `score_0_condition` 和 `score_4_condition`。

## 评估检查项

| 检查项 | 本地预检 | LLM 评估 |
|--------|---------|---------|
| Category 合法性 | ✅ | ✅ |
| 评分条件完整性 | ✅ | ✅ |
| 客观性（无模糊词） | ✅ | ✅ |
| 量化性（可测量阈值） | - | ✅ |
| MECE 原则 | - | ✅ |
| 完成度/质量分离 | - | ✅ |
| 事实来源引用 | ✅ | ✅ |

## 命令行参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--input`, `-i` | 输入文件或目录 | 必填 |
| `--recursive`, `-r` | 递归扫描子目录 | false |
| `--output`, `-o` | 输出 JSON 路径 | auto |
| `--no-llm` | 仅本地预检，不调用 LLM | false |
| `--api-key` | API 密钥 | env: OPENAI_API_KEY |
| `--model` | 模型名称 | gpt-4o |
| `--base-url` | API 地址 | https://api.openai.com/v1 |
| `--batch-size` | 每批条目数 | 10 |
| `--delay` | 批次间隔（秒） | 1.0 |
| `--config`, `-c` | YAML 配置文件 | - |
| `--no-color` | 禁用彩色输出 | false |

## 配置文件

支持 YAML 配置文件：

```yaml
llm:
  base_url: https://api.anthropic.com
  api_key: sk-ant-xxx
  model: claude-sonnet-4-20250514
  timeout: 60
  max_retries: 3

batch:
  size: 10
  delay: 1.0
```

命令行参数优先于配置文件。

## 依赖

- `openai` — LLM API 调用
- `rich` — 彩色终端输出
- `pyyaml` — YAML 配置（可选）
