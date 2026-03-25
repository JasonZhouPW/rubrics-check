# Rubrics Evaluator

Rubrics JSON 质量评估工具，支持本地规则预检 + LLM 深度评估。

## 安装

```bash
pip install openai rich
```

## 快速开始

### 仅本地预检（不调用 LLM，毫秒级）

```bash
python rubrics_evaluator.py --input rubrics.json  --no-llm
```

### LLM 评估（支持 OpenAI / Anthropic / Ollama / NVIDIA / 通义等）

```bash

# 配置文件（推荐）
python rubrics_evaluator.py --input rubrics.json --config config.yaml
```

### 批量评估目录

```bash
python rubrics_evaluator.py --input ./rubrics/ --recursive --api-key $API_KEY --model gpt-4o
```

## 输出

每次评估同时生成两个文件：

- **控制台**：彩色表格（pass/warn/fail 状态）
- **eval_result.json**：详细结构化结果
- **eval_result.csv**：CSV 表格

### JSON 输出格式

```json
{
  "meta": { "generated_at": "...", "mode": "llm", "total_rubrics": 8 },
  "summary": { "pass": 7, "warn": 1, "fail": 0, "pass_rate": "87.5%" },
  "results": [
    {
      "id": "HC-1",
      "constraint_type": "hard",
      "binary_type": "binary",
      "checks": {
        "category_valid": true,
        "score_conditions_complete": true,
        "objectivity": true,
        "quantification": true,
        "mece_within_item": true,
        "completion_quality_separated": true,
        "facts_reference_present": true
      },
      "status": "pass",
      "issues": [],
      "suggestion": ""
    }
  ]
}
```

### CSV 输出格式

| 列名 | 说明 |
|------|------|
| `ID` | 条目标识 |
| `constraint_type` | hard / soft / optional |
| `binary_type` | binary / nonbinary |
| `status` | pass / warn / fail |
| 各检查项列 | pass / fail / N/A |
| `issues` | 问题列表，竖线分隔 |

## Rubrics 格式

### 格式 1：rubric 块（推荐）

Category 由外层 key 决定，item 内不需要 `Category` 字段。

```json
{
  "rubric": {
    "Hard Constraint": [
      {
        "ID": "HC-1",
        "rubric_description": "Whether two independent Word (.docx) files are generated.",
        "score_0_condition": "Fewer than two files, wrong format, or content merged.",
        "score_1_condition": "Two separate .docx files generated with correct names.",
        "related_facts": "User requested two separate Word docs.",
        "facts_reference": ""
      }
    ],
    "Soft Constraint": [
      {
        "ID": "SC-1",
        "rubric_description": "How accurately the legal basis is cited.",
        "score_0_condition": "No relevant articles cited.",
        "score_1_condition": "One article cited with wrong number.",
        "score_2_condition": "One correct article cited.",
        "score_3_condition": "Both required articles cited correctly.",
        "score_4_condition": "Both required articles plus additional relevant articles.",
        "related_facts": "Applicable legal articles.",
        "facts_reference": "民法典第1198条；民事诉讼法第119条"
      }
    ],
    "Optional Constraint": [
      {
        "rubric_description": "Whether it provides precautions for using the button.",
        "score_0_condition": "Fails to mention any precautions.",
        "score_1_condition": "Mentions at least one reasonable precaution."
      }
    ]
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
| `ID` | 唯一标识 | 可选（未填则自动生成 `_autogen_1`） |
| `rubric_description` | 评分项描述 | 必须 |
| `score_0_condition` | 0 分条件 | 必须 |
| `score_1_condition` | 1 分条件（Binary 用） | 必须 |
| `score_2/3/4_condition` | 中间档位（Non-binary 用） | 可选 |
| `related_facts` | 可验证事实描述 | 建议填写 |
| `facts_reference` | 事实来源引用 | related_facts 存在时必填 |

### Binary vs Non-binary 自动判断

- **Binary**：只有 `score_0_condition` + `score_1_condition`
- **Non-binary**：存在 `score_2_condition` / `score_3_condition` / `score_4_condition`

### 约束规则

- **Hard Constraint**：必须是 Binary（只有 `score_0` + `score_1`）
- **Soft / Optional Constraint**：可以是 Binary 或 Non-binary

## 评估检查项

| 检查项 | 本地预检 | LLM 评估 |
|--------|:-------:|:--------:|
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
| `--output`, `-o` | 输出 JSON 路径（CSV 同名） | auto |
| `--no-llm` | 仅本地预检，不调用 LLM | false |
| `--api-key` | API 密钥 | env: OPENAI_API_KEY |
| `--model` | 模型名称 | gpt-4o |
| `--base-url` | API 地址 | https://api.openai.com/v1 |
| `--batch-size` | 每批条目数 | 10 |
| `--delay` | 批次间隔（秒） | 1.0 |
| `--config`, `-c` | YAML 配置文件 | - |
| `--no-color` | 禁用彩色输出 | false |

## 配置文件

支持 YAML 配置文件，`--config` 指定路径：

```yaml
# config.yaml
# 使用方法：python rubrics_evaluator.py --input rubrics.json --config config.yaml

# LLM 配置
llm:
  base_url: "https://integrate.api.nvidia.com/v1"   # 或 https://api.anthropic.com 等
  api_key: "your-api-key"
  model: "z-ai/glm4.7"       # 模型名称
  timeout: 60                # 请求超时（秒）
  max_retries: 3            # 最大重试次数

# 批处理配置
batch:
  size: 1                   # 每批条目数
  delay: 0                  # 批次间延迟（秒）
```

命令行参数优先于配置文件。

### 配置示例

| 使用场景 | base_url | model |
|---------|----------|-------|
| NVIDIA NIM | `https://integrate.api.nvidia.com/v1` | `z-ai/glm4.7` |
| 阿里通义 | `https://coding.dashscope.aliyuncs.com/v1` | `qwen3.5-plus` |
| Anthropic | `https://api.anthropic.com` | `claude-sonnet-4-20250514` |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o` |
| Ollama 本地 | `http://localhost:11434/v1` | `qwen2.5:72b` |

## 依赖

- `openai` — LLM API 调用
- `rich` — 彩色终端输出
- `pyyaml` — YAML 配置（可选）
