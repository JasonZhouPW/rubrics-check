#!/usr/bin/env python3
"""
Rubrics Quality Evaluator
批量读入 Rubrics JSON，通过指定 LLM 进行质检评分，输出结构化结果。

支持官方格式规范（Hard / Soft / Optional Constraint，binary / non-binary）。

用法:
  python rubrics_evaluator.py \\
    --input ./rubrics_dir/ \\
    --base-url https://api.anthropic.com \\
    --api-key YOUR_KEY \\
    --model claude-sonnet-4-20250514

  # 仅本地预检（不调用 LLM）
  python rubrics_evaluator.py --input rubrics.json --no-llm

  python rubrics_evaluator.py \\
    --input rubrics.json \\
    --base-url http://localhost:11434/v1 \\
    --api-key ollama \\
    --model qwen2.5:72b

  python rubrics_evaluator.py --config config.yaml --input ./samples/
"""

import argparse
import csv as csv_lib
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Optional

# YAML 配置导入
try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False

# httpx 导入（用于配置 OpenAI 客户端）
import httpx

# ── 时间工具函数 ──────────────────────────────────────────────────────────────

def format_elapsed(seconds: float) -> str:
    """格式化时间为可读字符串"""
    if seconds < 1:
        return f"{seconds*1000:.0f}ms"
    elif seconds < 60:
        return f"{seconds:.2f}s"
    else:
        mins = int(seconds // 60)
        secs = seconds % 60
        return f"{mins}m{secs:.1f}s"


# ── 全局计时器 ────────────────────────────────────────────────────────────────

class Timer:
    """简单的时间追踪器"""
    def __init__(self):
        self.start_time: Optional[float] = None
        self.step_times: list[dict] = []
        self.current_step: Optional[str] = None

    def start(self):
        self.start_time = time.time()
        self.step_times = []

    def begin_step(self, step_name: str):
        """开始一个新步骤"""
        if self.current_step:
            self.end_step()
        self.current_step = step_name
        self.step_start = time.time()

    def end_step(self):
        """结束当前步骤"""
        if self.current_step and hasattr(self, 'step_start'):
            elapsed = time.time() - self.step_start
            self.step_times.append({
                "step": self.current_step,
                "elapsed": elapsed,
                "elapsed_str": format_elapsed(elapsed)
            })
        self.current_step = None

    def total_elapsed(self) -> float:
        """返回总耗时（秒）"""
        return time.time() - self.start_time if self.start_time else 0

    def print_summary(self, console=None):
        """打印时间汇总"""
        self.end_step()  # 确保最后一步结束
        log_func = console.print if console else print

        log_func("")
        log_func("=" * 60)
        log_func("执行时间汇总")
        log_func("=" * 60)
        for item in self.step_times:
            log_func(f"  {item['step']:<35} {item['elapsed_str']:>12}")
        log_func("-" * 60)
        total = self.total_elapsed()
        log_func(f"  {'总耗时':<35} {format_elapsed(total):>12}")
        log_func("=" * 60)


# ── 依赖导入 ──────────────────────────────────────────────────────────────────

try:
    from openai import OpenAI
except ImportError:
    print("[错误] 缺少依赖，请先运行：pip install openai rich")
    sys.exit(1)

try:
    from rich.console import Console
    from rich.table import Table
    from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn
    from rich.panel import Panel
    from rich import box
    HAS_RICH = True
except ImportError:
    HAS_RICH = False


# ── 常量 ──────────────────────────────────────────────────────────────────────

VALID_CATEGORIES = {"hard constraint", "soft constraint", "optional constraint",
                    "硬约束", "软约束", "可选约束"}

# 主观模糊词，出现在任何评分条件字段中均视为问题
VAGUE_WORDS = [
    "一定程度", "充分提及", "充分说明", "充分描述", "充分体现",
    "简洁美观", "专业规范", "适当使用", "适当地", "基本上",
    "较为完整", "较为详细", "有所提及", "相关法律", "相关法规",
    "比较好", "比较完整", "fairly", "sufficiently", "adequately",
    "somewhat", "reasonably", "quite good", "properly addressed",
]


# ── 字段提取（兼容中英文两套字段名）────────────────────────────────────────────

def extract_fields(r: dict) -> dict:
    """从一条 rubric dict 中统一提取各字段，兼容中英文字段名及嵌套结构。

    category 从 r.get("_category") 读取（load_rubrics 设置），fallback 到 Category 字段。
    """
    rid = (r.get("ID") or r.get("id") or r.get("rubric_id") or "")
    category = (r.get("_category") or r.get("Category") or r.get("category") or
                r.get("类别 Category") or r.get("类别") or "")
    description = (r.get("rubric_description") or r.get("Description") or
                   r.get("描述 Description") or r.get("描述") or
                   r.get("description") or r.get("criterion") or "")

    score_0 = (r.get("score_0_condition") or r.get("0 分标准") or
               r.get("score_0") or "")
    score_1 = (r.get("score_1_condition") or r.get("1 分标准") or
               r.get("score_1") or "")
    score_2 = r.get("score_2_condition") or r.get("score_2") or ""
    score_3 = r.get("score_3_condition") or r.get("score_3") or ""
    score_4 = r.get("score_4_condition") or r.get("score_4") or ""

    score_criteria_blob = r.get("评分标准") or r.get("score_criteria") or ""

    related_facts = (r.get("related_facts") or r.get("相关事实") or r.get("facts") or "")
    facts_reference = (r.get("facts_reference") or r.get("facts_ref") or
                       r.get("related_facts_reference") or "")

    # 根据 score_N_condition 字段数量自动判断 binary/non-binary
    is_nonbinary = bool(score_2 or score_3 or score_4)

    return {
        "id": str(rid) if rid else "",
        "category": category.strip(),
        "description": description.strip(),
        "is_nonbinary": is_nonbinary,
        "score_0": score_0.strip(),
        "score_1": score_1.strip(),
        "score_2": score_2.strip(),
        "score_3": score_3.strip(),
        "score_4": score_4.strip(),
        "score_criteria_blob": score_criteria_blob.strip(),
        "related_facts": related_facts.strip(),
        "facts_reference": facts_reference,
    }


# ── 本地预检（规则检测，不消耗 LLM）────────────────────────────────────────────

def local_precheck(r: dict, console=None) -> list[dict]:
    """
    对一条 rubric 做本地规则检测，返回 issue 列表。
    每个 issue: {"level": "error"|"warning", "field": str, "msg": str}
    """
    f = extract_fields(r)
    issues = []

    def err(field, msg):
        issues.append({"level": "error", "field": field, "msg": msg})

    def warn(field, msg):
        issues.append({"level": "warning", "field": field, "msg": msg})

    if not f["description"]:
        err("rubric_description", "rubric_description 字段为空")

    cat_lower = f["category"].lower()
    is_hard = "hard" in cat_lower or "硬" in cat_lower
    is_soft = "soft" in cat_lower or "软" in cat_lower
    is_optional = "optional" in cat_lower or "可选" in cat_lower

    if not (is_hard or is_soft or is_optional):
        err("Category", f"Category 值 [{f['category']}] 不在合法范围内 (Hard/Soft/Optional Constraint)")

    is_nonbinary = f["is_nonbinary"]

    if is_hard and is_nonbinary:
        err("Category+Binary", "Hard Constraint 必须是 Binary（只有 score_0 和 score_1），不能有 score_2/3/4")

    # Binary: 只需 score_0 + score_1
    if not is_nonbinary:
        if not f["score_1"]:
            err("score_1_condition", "Binary 类型缺少 score_1_condition（满足条件）")
        if not f["score_0"]:
            err("score_0_condition", "Binary 类型缺少 score_0_condition（不满足条件）")
        if f["score_2"] or f["score_3"] or f["score_4"]:
            warn("score_2/3/4_condition", "Binary 类型不应存在 score_2/3/4_condition 字段")

    # Non-binary: 至少需要 score_0 和 score_4
    if is_nonbinary:
        missing = []
        if not f["score_0"]:
            missing.append("score_0_condition")
        if not f["score_4"]:
            missing.append("score_4_condition")
        if missing:
            err("score_conditions", f"Non-binary 缺少关键档位：{', '.join(missing)}（至少需要 score_0 和 score_4）")

        mid_missing = [f"score_{i}_condition" for i in [1, 2, 3] if not f[f"score_{i}"]]
        if mid_missing:
            warn("score_conditions", f"Non-binary 建议补充中间档位：{', '.join(mid_missing)}")

        if not f["score_0"] and not f["score_4"] and f["score_criteria_blob"]:
            warn("score_conditions", "评分标准使用了合并字段而非拆分的 score_N_condition 字段，建议按规范拆分")

    all_score_text = " ".join([
        f["description"], f["score_0"], f["score_1"],
        f["score_2"], f["score_3"], f["score_4"], f["score_criteria_blob"]
    ]).lower()
    for word in VAGUE_WORDS:
        if word.lower() in all_score_text:
            err("score_conditions", f"评分条件含主观/模糊词 [{word}]，需替换为客观量化标准")

    if not f["related_facts"]:
        warn("related_facts", "未填写 related_facts，若评分标准涉及可验证事实建议补充")

    if f["related_facts"] and not f["facts_reference"]:
        warn("facts_reference", "填写了 related_facts 但缺少 facts_reference（来源引用）")

    if f["description"] and len(f["description"]) < 10:
        warn("rubric_description", "rubric_description 过短（<10 字），描述可能不够清晰")

    return issues


# ── LLM 系统提示词 ────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a professional RL training data quality reviewer. Your task is to evaluate Rubric items against the official Rubrics Format Specification.

## Rubric Format Specification

### Categories (3 types)
- **Hard Constraint**: Binary only (only score_0 + score_1). Failure severely impairs task completion. Objective yes/no questions about explicit requirements.
- **Soft Constraint**: Binary (score_0 + score_1) or Non-binary (score_0 through score_4). Important quality factors. Omission substantially reduces user satisfaction but doesn't cause complete failure.
- **Optional Constraint**: Binary or Non-binary. User experience enhancements not explicitly stated. Omission doesn't significantly affect results.

### Scoring Logistics
- **Binary**: score_0_condition (not met) + score_1_condition (met). Hard Constraints MUST be Binary.
- **Non-binary**: score_0_condition through score_4_condition. Each level must be objectively distinguishable.

Note: Whether a rubric is Binary or Non-binary is determined by the presence of score_2/3/4_condition fields — there is no "Binary vs. Non-binary" field in the input.

### Quality Requirements
1. **MECE Principle**: Rubrics must be mutually exclusive and collectively exhaustive — no overlap between items, and together they should cover all key evaluation dimensions.
2. **Completion vs. Quality**: Task completion and output quality must be evaluated as separate rubric items.
3. **Objectivity**: Score conditions must use objective, verifiable criteria. No vague language like "sufficiently", "appropriately", "adequately", "充分", "适当".
4. **Quantification**: Non-binary score conditions must include quantifiable thresholds (counts, percentages, specific named items).
5. **Facts Verification**: If a rubric can be fact-checked, related_facts and facts_reference should be provided.

## Your Task
Evaluate each rubric on the following checks. Return a JSON array — one object per rubric.

### Output Format (per rubric)
{
  "id": "<same as input ID>",
  "constraint_type": "hard" | "soft" | "optional" | "unknown",
  "binary_type": "binary" | "nonbinary" | "unknown",
  "checks": {
    "category_valid": true | false,
    "score_conditions_complete": true | false,
    "objectivity": true | false,
    "quantification": true | false,
    "mece_within_item": true | false,
    "completion_quality_separated": true | false,
    "facts_reference_present": true | false
  },
  "status": "pass" | "warn" | "fail",
  "issues": ["Specific issue description referencing the field name and exact problem"],
  "suggestion": "Actionable improvement suggestion; empty string if no issues"
}

### Status Rules
- **pass**: All checks true, no issues
- **warn**: 1 check false, or minor formatting issues that don't affect usability
- **fail**: 2+ checks false, OR any of these critical failures: vague scoring language, missing score conditions, Hard Constraint having score_2/3/4 (must be Binary)

Output ONLY the JSON array. No explanation text."""



def build_user_prompt(rubrics: list[dict]) -> str:
    items = []
    for r in rubrics:
        f = extract_fields(r)
        item = {
            "ID": f["id"] or "(auto)",
            "Category": f["category"],
            "rubric_description": f["description"],
            "score_0_condition": f["score_0"],
            "score_1_condition": f["score_1"],
            "score_2_condition": f["score_2"] or None,
            "score_3_condition": f["score_3"] or None,
            "score_4_condition": f["score_4"] or None,
            "related_facts": f["related_facts"] or None,
            "facts_reference": f["facts_reference"] or None,
        }
        item = {k: v for k, v in item.items() if v is not None}
        items.append(item)
    return f"Please evaluate the following {len(items)} rubric(s):\n\n{json.dumps(items, ensure_ascii=False, indent=2)}"


# ── LLM 调用 ──────────────────────────────────────────────────────────────────

def call_llm(client: "OpenAI", model: str, rubrics_batch: list[dict], retries: int = 3,
             console=None, batch_idx: int = 0, total_batches: int = 0,
             timeout: int = 60) -> list[dict]:
    """LLM 调用函数，带详细日志"""
    log_func = console.print if console else print
    style = lambda x, c: f"[{c}]{x}[/{c}]" if console else x

    batch_start = time.time()

    # [步骤 1] 本地预检
    log_func(f"\n  {style(f' Batch {batch_idx}/{total_batches}', 'bold')}")
    log_func(f"    {style('[步骤 1/3] 本地预检', 'cyan')}...")

    precheck_map: dict[str, list[dict]] = {}
    precheck_issues_count = 0
    precheck_start = time.time()
    for idx, r in enumerate(rubrics_batch):
        rid = extract_fields(r)["id"]
        issues = local_precheck(r)
        if issues:
            precheck_map[rid] = issues
            precheck_issues_count += len(issues)
        log_func(f"      条目 {idx+1}/{len(rubrics_batch)}: ID={rid}, issues={len(issues)}")

    precheck_time = time.time() - precheck_start
    log_func(f"    {style('本地预检完成', 'green')} | {precheck_issues_count} issues | {format_elapsed(precheck_time)}")

    # [步骤 2] 构建 prompt
    log_func(f"    {style('[步骤 2/3] 构建 Prompt', 'cyan')}...")
    prompt_start = time.time()
    prompt = build_user_prompt(rubrics_batch)
    prompt_time = time.time() - prompt_start
    prompt_size = len(prompt)
    log_func(f"    {style('Prompt 构建完成', 'green')} | {prompt_size} chars | {format_elapsed(prompt_time)}")

    # [步骤 3] LLM 调用
    log_func(f"    {style('[步骤 3/3] 调用 LLM', 'cyan')}...")
    llm_start = time.time()
    for attempt in range(retries):
        try:
            log_func(f"      发送请求 (尝试 {attempt+1}/{retries})...")
            log_func(f"      参数：model={model}, max_tokens=1024, timeout={timeout}s")
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
                max_tokens=1024,  # 降低 token 上限加快响应
            )
            raw = resp.choices[0].message.content
            if raw is None:
                log_func(f"      {style('LLM 返回空内容', 'red')}")
                raise RuntimeError("LLM 返回空内容")
            raw = raw.strip()
            log_func(f"      LLM 原始回复 (前 200 字符): {raw[:200]}...")

            # 清理 markdown 格式
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[-1]
                raw = raw.rsplit("```", 1)[0].strip()

            # 尝试解析 JSON
            try:
                llm_results: list[dict] = json.loads(raw)
            except json.JSONDecodeError as e:
                log_func(f"      {style(f'JSON 解析失败：{e}', 'red')}")
                # 尝试提取 JSON 数组
                if "[" in raw and "]" in raw:
                    start = raw.index("[")
                    end = raw.rindex("]") + 1
                    json_str = raw[start:end]
                    log_func(f"      尝试提取 JSON: {json_str[:100]}...")
                    llm_results = json.loads(json_str)
                else:
                    raise

            # 确保是列表
            if isinstance(llm_results, str):
                raise RuntimeError(f"LLM 返回的是字符串而非 JSON: {llm_results[:100]}...")
            if not isinstance(llm_results, list):
                llm_results = [llm_results]

            llm_time = time.time() - llm_start
            log_func(f"    {style('LLM 响应成功', 'green')} | {format_elapsed(llm_time)} | {len(llm_results)} 结果")

            # 合并本地预检问题
            for item in llm_results:
                rid = str(item.get("id", ""))
                local_issues = precheck_map.get(rid, [])
                if local_issues:
                    existing_msgs = item.get("issues", [])
                    for li in local_issues:
                        msg = f"[local:{li['level']}] {li['field']}: {li['msg']}"
                        if msg not in existing_msgs:
                            existing_msgs.append(msg)
                    item["issues"] = existing_msgs

                    has_local_error = any(li["level"] == "error" for li in local_issues)
                    local_error_count = sum(1 for li in local_issues if li["level"] == "error")
                    if has_local_error and item.get("status") == "pass":
                        item["status"] = "warn"
                    if local_error_count >= 2:
                        item["status"] = "fail"

            return llm_results

        except json.JSONDecodeError as e:
            log_func(f"      {style(f'JSON 解析失败，重试 {attempt+1}/{retries}', 'yellow')}")
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"LLM 返回无法解析的 JSON: {e}")
        except Exception as e:
            log_func(f"      {style(f'LLM 调用失败，重试 {attempt+1}/{retries}: {e}', 'yellow')}")
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"LLM 调用失败：{e}")
    return []


# ── 本地预检单独运行 ────────────────────────────────────────────────────────────

def run_local_only(
    rubrics: list[dict],
    console=None,
) -> list[dict]:
    """
    仅运行本地预检，不调用 LLM。
    返回与 LLM 评估兼容的结果格式。
    """
    log_func = console.print if console else print
    style = lambda x, c: f"[{c}]{x}[/{c}]" if console else x

    results = []
    for idx, r in enumerate(rubrics):
        f = extract_fields(r)
        rid = f["id"]
        issues = local_precheck(r)

        # 将本地问题转换为 LLM 兼容格式
        local_issues = []
        has_error = False
        for issue in issues:
            msg = f"[local:{issue['level']}] {issue['field']}: {issue['msg']}"
            local_issues.append(msg)
            if issue["level"] == "error":
                has_error = True

        # 基于本地预检推断状态
        error_count = sum(1 for i in issues if i["level"] == "error")
        if error_count >= 2:
            status = "fail"
        elif has_error:
            status = "warn"
        else:
            status = "pass"

        # 判断 constraint_type
        cat_lower = f["category"].lower()
        is_hard = "hard" in cat_lower or "硬" in cat_lower
        is_soft = "soft" in cat_lower or "软" in cat_lower
        is_optional = "optional" in cat_lower or "可选" in cat_lower
        constraint_type = "unknown"
        if is_hard:
            constraint_type = "hard"
        elif is_soft:
            constraint_type = "soft"
        elif is_optional:
            constraint_type = "optional"

        # 判断 binary_type
        binary_type = "nonbinary" if f["is_nonbinary"] else "binary"

        # 本地预检无法判断的 LLM 检查项设为 None
        result = {
            "id": rid,
            "constraint_type": constraint_type,
            "binary_type": binary_type,
            "checks": {
                "category_valid": is_hard or is_soft or is_optional,
                "score_conditions_complete": None,  # LLM 检查项
                "objectivity": None,
                "quantification": None,
                "mece_within_item": None,
                "completion_quality_separated": None,
                "facts_reference_present": None,
            },
            "status": status,
            "issues": local_issues,
            "suggestion": "",
        }
        results.append(result)

        log_func(f"  条目 {idx+1}/{len(rubrics)}: ID={rid}, status={status}, issues={len(local_issues)}")

    return results


# ── 批处理 ────────────────────────────────────────────────────────────────────

def process_in_batches(
    client: "OpenAI",
    model: str,
    rubrics: list[dict],
    batch_size: int,
    delay: float,
    console=None,
    timer: Timer = None,
    timeout: int = 60,
) -> list[dict]:
    results = []
    batches = [rubrics[i:i + batch_size] for i in range(0, len(rubrics), batch_size)]
    total_batches = len(batches)

    log_func = console.print if console else print
    style = lambda x, c: f"[{c}]{x}[/{c}]" if console else x

    log_func("")
    log_func(f"开始评估，共 {total_batches} 批次，每批 {batch_size} 条...")
    log_func("")

    for i, batch in enumerate(batches):
        batch_results = call_llm(
            client, model, batch,
            batch_idx=i + 1,
            total_batches=total_batches,
            console=console,
            timeout=timeout,
        )
        results.extend(batch_results)
        if i < len(batches) - 1:
            time.sleep(delay)

    return results


# ── 汇总统计 ──────────────────────────────────────────────────────────────────

CHECK_LABELS = {
    "category_valid":                "类别合法",
    "score_conditions_complete":     "评分条件完整",
    "objectivity":                   "客观性",
    "quantification":                "量化性",
    "mece_within_item":              "MECE 原则",
    "completion_quality_separated":  "完成度/质量分离",
    "facts_reference_present":       "事实来源",
}

CONSTRAINT_LABEL = {
    "hard": "Hard",
    "soft": "Soft",
    "optional": "Optional",
    "unknown": "Unknown",
}


def compute_summary(results: list[dict]) -> dict:
    if not results:
        return {}
    total = len(results)
    pass_n  = sum(1 for r in results if r.get("status") == "pass")
    warn_n  = sum(1 for r in results if r.get("status") == "warn")
    fail_n  = sum(1 for r in results if r.get("status") == "fail")

    type_counts = Counter(r.get("constraint_type", "unknown") for r in results)
    binary_counts = Counter(r.get("binary_type", "unknown") for r in results)

    check_rates = {}
    for k in CHECK_LABELS:
        vals = [r["checks"][k] for r in results if isinstance(r.get("checks", {}).get(k), bool)]
        check_rates[k] = f"{round(sum(vals)/len(vals)*100, 1)}%" if vals else "N/A"

    all_issues = []
    for r in results:
        all_issues.extend(r.get("issues", []))
    top_issues = [issue for issue, _ in Counter(all_issues).most_common(5)]

    return {
        "total": total,
        "pass": pass_n,
        "warn": warn_n,
        "fail": fail_n,
        "pass_rate": f"{round(pass_n/total*100, 1)}%",
        "constraint_type_counts": dict(type_counts),
        "binary_type_counts": dict(binary_counts),
        "check_pass_rates": check_rates,
        "top_issues": top_issues,
    }


# ── 展示 ──────────────────────────────────────────────────────────────────────

def print_rich_results(results: list[dict], summary: dict, console: "Console"):
    console.print()
    s = summary
    tc = s.get("constraint_type_counts", {})
    cr = s.get("check_pass_rates", {})

    pass_pct = s["pass"] / max(s["total"], 1)
    pass_color = "green" if pass_pct >= 0.8 else "yellow" if pass_pct >= 0.6 else "red"

    type_line = (
        f"Hard: [blue]{tc.get('hard',0)}[/blue]  "
        f"Soft: [magenta]{tc.get('soft',0)}[/magenta]  "
        f"Optional: [cyan]{tc.get('optional',0)}[/cyan]"
        + (f"  [red]Unknown: {tc.get('unknown',0)}[/red]" if tc.get("unknown") else "")
    )
    check_lines = "  ".join(
        f"{v}: [cyan]{cr.get(k,'N/A')}[/cyan]" for k, v in CHECK_LABELS.items()
    )
    top = s.get("top_issues", [])
    top_text = "\n".join(f"  · {i}" for i in top) if top else "  None"

    panel_text = (
        f"Total: [bold]{s['total']}[/bold]   {type_line}\n"
        f"Pass: [green]{s['pass']}[/green]  Warn: [yellow]{s['warn']}[/yellow]  "
        f"Fail: [red]{s['fail']}[/red]  Pass rate: [bold {pass_color}]{s['pass_rate']}[/bold {pass_color}]\n\n"
        f"Check pass rates:\n  {check_lines}\n\n"
        f"Top issues:\n{top_text}"
    )
    console.print(Panel(panel_text, title="[bold]Quality Check Summary[/bold]", border_style="blue"))

    table = Table(box=box.SIMPLE_HEAVY, show_lines=True, header_style="bold")
    table.add_column("ID", style="dim", width=10)
    table.add_column("Type", width=9)
    table.add_column("B/NB", width=7)
    table.add_column("Status", width=7)
    for v in CHECK_LABELS.values():
        table.add_column(v, width=8)
    table.add_column("Issues & Suggestion", min_width=36)

    STATUS_STR = {
        "pass": "[green]pass[/green]",
        "warn": "[yellow]warn[/yellow]",
        "fail": "[red]FAIL[/red]",
    }
    TYPE_STR = {
        "hard": "[blue]Hard[/blue]",
        "soft": "[magenta]Soft[/magenta]",
        "optional": "[cyan]Optional[/cyan]",
        "unknown": "[dim]?[/dim]",
    }
    BNB_STR = {
        "binary": "[green]Bin[/green]",
        "nonbinary": "[yellow]Non[/yellow]",
        "unknown": "[dim]?[/dim]",
    }

    def fmt_check(v):
        if v is True:  return "[green]✓[/green]"
        if v is False: return "[red]✗[/red]"
        return "[dim]-[/dim]"

    for r in results:
        checks = r.get("checks", {})
        issues = r.get("issues", [])
        suggestion = r.get("suggestion", "")
        detail_parts = []
        if issues:
            detail_parts.append("[red]" + "\n".join(issues) + "[/red]")
        if suggestion:
            detail_parts.append("[dim]→ " + suggestion + "[/dim]")
        detail = "\n".join(detail_parts) if detail_parts else "[green]No issues[/green]"

        table.add_row(
            str(r.get("id", "?")),
            TYPE_STR.get(r.get("constraint_type", "unknown"), "?"),
            BNB_STR.get(r.get("binary_type", "unknown"), "?"),
            STATUS_STR.get(r.get("status", "?"), r.get("status", "?")),
            *[fmt_check(checks.get(k)) for k in CHECK_LABELS],
            detail,
        )
    console.print(table)


def print_plain_results(results: list[dict], summary: dict):
    s = summary
    tc = s.get("constraint_type_counts", {})
    print("\n" + "=" * 70)
    print("Quality Check Summary")
    print("=" * 70)
    print(f"Total: {s['total']}  Hard: {tc.get('hard',0)}  Soft: {tc.get('soft',0)}  Optional: {tc.get('optional',0)}")
    print(f"Pass: {s['pass']}  Warn: {s['warn']}  Fail: {s['fail']}  Pass rate: {s['pass_rate']}")
    print("Check pass rates:")
    for k, v in CHECK_LABELS.items():
        print(f"  {v}: {s.get('check_pass_rates',{}).get(k,'N/A')}")
    top = s.get("top_issues", [])
    if top:
        print("Top issues:")
        for i in top:
            print(f"  · {i}")
    print("=" * 70)
    print(f"{'ID':<12} {'Type':<10} {'B/NB':<7} {'Status':<7} Issues")
    print("-" * 70)
    for r in results:
        ctype = CONSTRAINT_LABEL.get(r.get("constraint_type", "?"), "?")
        bnb = r.get("binary_type", "?")
        issues_str = " | ".join(r.get("issues", [])) or "None"
        print(f"{str(r.get('id','?')):<12} {ctype:<10} {bnb:<7} {r.get('status','?'):<7} {issues_str}")


# ── 文件加载 ──────────────────────────────────────────────────────────────────

def collect_json_files(input_path: str, recursive: bool) -> list[Path]:
    p = Path(input_path)
    if p.is_file():
        if p.suffix.lower() != ".json":
            raise ValueError(f"Not a JSON file: {p}")
        return [p]
    if p.is_dir():
        pattern = "**/*.json" if recursive else "*.json"
        files = sorted(p.glob(pattern))
        if not files:
            raise FileNotFoundError(
                f"No .json files found in '{p}'"
                + (" (recursive)" if recursive else " — try --recursive to scan subdirectories")
            )
        return files
    raise FileNotFoundError(f"Path not found: {p}")


_id_counter = 0

def _next_id() -> str:
    global _id_counter
    _id_counter += 1
    return f"_autogen_{_id_counter}"

def load_rubrics(input_path: str, recursive: bool = False) -> tuple[list[dict], list[str]]:
    global _id_counter
    _id_counter = 0

    files = collect_json_files(input_path, recursive)
    all_rubrics: list[dict] = []
    file_names: list[str] = []
    errors: list[str] = []

    for fp in files:
        try:
            raw = json.loads(fp.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            errors.append(f"{fp.name}: JSON parse error — {e}")
            continue

        before = len(all_rubrics)

        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict):
                    item = dict(item)
                    if not item.get("ID") and not item.get("id") and not item.get("rubric_id"):
                        item["ID"] = _next_id()
                    all_rubrics.append(item)

        elif isinstance(raw, dict):
            rubric_block = raw.get("rubric") or raw.get("Rubric")
            if isinstance(rubric_block, dict):
                for cat_name, items in rubric_block.items():
                    if isinstance(items, list):
                        for item in items:
                            if isinstance(item, dict):
                                item = dict(item)
                                item["_category"] = cat_name
                                if not item.get("ID") and not item.get("id") and not item.get("rubric_id"):
                                    item["ID"] = _next_id()
                                all_rubrics.append(item)
            else:
                for key in ("rubrics", "items", "criteria", "data"):
                    if key in raw and isinstance(raw[key], list):
                        for item in raw[key]:
                            if isinstance(item, dict):
                                item = dict(item)
                                if not item.get("ID") and not item.get("id") and not item.get("rubric_id"):
                                    item["ID"] = _next_id()
                                all_rubrics.append(item)
                        break
                else:
                    all_rubrics.append(raw)

        count = len(all_rubrics) - before
        file_names.append(f"{fp.name} ({count} items)")

    if errors:
        print("[Warning] Skipped files:")
        for e in errors:
            print(f"  - {e}")

    return all_rubrics, file_names


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Rubrics Quality Evaluator — supports Hard/Soft/Optional Constraint format",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  # Single file
  python rubrics_evaluator.py \\
    --input rubrics.json \\
    --api-key sk-xxx --model gpt-4o

  # Folder (top-level only)
  python rubrics_evaluator.py \\
    --input ./data/ \\
    --api-key sk-xxx --model gpt-4o

  # Folder + recursive
  python rubrics_evaluator.py \\
    --input ./data/ --recursive \\
    --api-key sk-xxx --model gpt-4o

  # Anthropic Claude
  python rubrics_evaluator.py \\
    --input ./rubrics_dir/ \\
    --base-url https://api.anthropic.com \\
    --api-key sk-ant-xxx \\
    --model claude-sonnet-4-20250514

  # Local Ollama
  python rubrics_evaluator.py \\
    --input ./rubrics_dir/ \\
    --base-url http://localhost:11434/v1 \\
    --api-key ollama \\
    --model qwen2.5:72b
""")
    p.add_argument("--config",     "-c", default="",
                   help="Config file path (YAML format)")
    p.add_argument("--input",      "-i", required=True,
                   help="Input path: single JSON file or folder")
    p.add_argument("--recursive",  "-r", action="store_true",
                   help="Recursively scan subdirectories (folder mode only)")
    p.add_argument("--output",     "-o", default="",
                   help="Output JSON path (default: eval_result.json or <name>_eval_result.json)")
    p.add_argument("--base-url",   default="",
                   help="LLM API base URL (default: https://api.openai.com/v1)")
    p.add_argument("--api-key",    default="",
                   help="API key (or set OPENAI_API_KEY env var)")
    p.add_argument("--model",      default="",
                   help="Model name (default: gpt-4o)")
    p.add_argument("--batch-size", type=int, default=0,
                   help="Items per batch (default: 10)")
    p.add_argument("--delay",      type=float, default=0,
                   help="Delay between batches in seconds (default: 1.0)")
    p.add_argument("--no-color",   action="store_true",
                   help="Disable colored output")
    p.add_argument("--no-llm",    action="store_true",
                   help="Skip LLM evaluation, only run local precheck")
    return p.parse_args()


def load_config(config_path: str) -> dict:
    """加载 YAML 配置文件"""
    if not config_path or not HAS_YAML:
        return {}
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    except Exception as e:
        print(f"[Warning] 配置文件加载失败：{e}")
        return {}


def get_value(args, config, key, default, config_keys=None):
    """
    优先级：命令行参数 > 配置文件 > 默认值
    config_keys: 配置文件中嵌套的 key 路径，如 ['llm', 'base_url']
    """
    # 1. 命令行参数优先
    args_val = getattr(args, key.replace('-', '_'), None)
    if args_val and args_val != default and args_val != "" and args_val != 0:
        return args_val

    # 2. 配置文件
    if config_keys:
        val = config
        for k in config_keys:
            if isinstance(val, dict) and k in val:
                val = val[k]
            else:
                val = None
                break
        if val is not None:
            return val

    # 3. 回退到命令行参数（可能为空）
    if args_val:
        return args_val

    # 4. 默认值
    return default


def main():
    args = parse_args()

    # 加载配置文件
    config = load_config(args.config) if HAS_YAML else {}

    # 从配置文件获取 LLM 参数
    base_url = get_value(args, config, 'base-url', 'https://api.openai.com/v1', ['llm', 'base_url'])
    api_key = get_value(args, config, 'api-key', '', ['llm', 'api_key']) or os.environ.get("OPENAI_API_KEY", "")
    model = get_value(args, config, 'model', 'gpt-4o', ['llm', 'model'])
    batch_size = get_value(args, config, 'batch-size', 10, ['batch', 'size'])
    delay = get_value(args, config, 'delay', 1.0, ['batch', 'delay'])
    timeout = get_value(args, config, 'timeout', 60, ['llm', 'timeout'])
    max_retries = get_value(args, config, 'max-retries', 3, ['llm', 'max_retries'])

    console = Console() if HAS_RICH and not args.no_color else None
    timer = Timer()

    log = lambda msg: console.print(msg) if console else print(msg)
    style = lambda x, c: f"[{c}]{x}[/{c}]" if console else x

    use_llm = not args.no_llm

    if use_llm and not api_key:
        log(style("[Error] Provide --api-key or set OPENAI_API_KEY", "red") if console
            else "[Error] Provide --api-key or set OPENAI_API_KEY")
        sys.exit(1)

    # ── 加载 ──────────────────────────────────────────────────────────────────
    timer.start()
    input_p = Path(args.input)
    mode = ("folder (recursive)" if args.recursive else "folder") if input_p.is_dir() else "file"

    timer.begin_step("加载 Rubrics 文件")
    log(f"\n{style('加载 Rubrics', 'bold')}: {args.input}  [{mode}]")

    try:
        rubrics, file_names = load_rubrics(args.input, recursive=args.recursive)
    except (FileNotFoundError, ValueError) as e:
        log(style(f"[Error] {e}", "red") if console else f"[Error] {e}")
        sys.exit(1)

    if not rubrics:
        log("[Error] No rubric items found")
        sys.exit(1)

    log(f"加载 {style(str(len(rubrics)), 'bold')} 条 rubric，来自 {len(file_names)} 个文件:")
    for fn in file_names:
        log(f"  [dim]• {fn}[/dim]" if console else f"  • {fn}")

    if use_llm:
        log(f"模式：LLM 评估  模型：{style(model, 'cyan')}  批次大小：{batch_size}  API: {style(base_url, 'dim')}\n")
    else:
        log(f"模式：本地预检 only（无 LLM）\n")

    # ── 评估 ──────────────────────────────────────────────────────────────────
    if use_llm:
        # ── 客户端 ──────────────────────────────────────────────────────────
        timer.begin_step("初始化 API 客户端")

        httpx_client = httpx.Client(
            timeout=httpx.Timeout(timeout=120.0, connect=10.0),
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=10),
            http2=False,
            trust_env=False,
        )
        client = OpenAI(
            api_key=api_key,
            base_url=base_url if base_url != "https://api.openai.com/v1" else None,
            max_retries=max_retries,
            http_client=httpx_client,
        )

        timer.begin_step("执行 LLM 评估")
        try:
            results = process_in_batches(
                client, model, rubrics,
                batch_size, delay,
                console, timer,
                timeout,
            )
        except RuntimeError as e:
            log(style(f"[Error] {e}", "red") if console else f"[Error] {e}")
            sys.exit(1)

        if not results:
            log("[Error] No results returned from LLM")
            sys.exit(1)
    else:
        # ── 本地预检 only ───────────────────────────────────────────────────
        timer.begin_step("执行本地预检")
        log("")
        log(f"  {style('[本地预检]', 'bold')}")

        try:
            results = run_local_only(rubrics, console)
        except Exception as e:
            log(style(f"[Error] {e}", "red") if console else f"[Error] {e}")
            sys.exit(1)

        if not results:
            log("[Error] No results returned from local precheck")
            sys.exit(1)

    if not results:
        log("[Error] No results returned from LLM")
        sys.exit(1)

    # ── 汇总 ──────────────────────────────────────────────────────────────────
    timer.begin_step("计算汇总统计")
    summary = compute_summary(results)

    if HAS_RICH and console:
        print_rich_results(results, summary, console)
    else:
        print_plain_results(results, summary)

    # ── 保存 ──────────────────────────────────────────────────────────────────
    timer.begin_step("保存结果")
    if args.output:
        output_path = args.output
    elif input_p.is_dir():
        output_path = str(input_p / "eval_result.json")
    else:
        output_path = input_p.stem + "_eval_result.json"

    output_data = {
        "meta": {
            "generated_at": datetime.now().isoformat(),
            "mode": "local_only" if not use_llm else "llm",
            "model": model if use_llm else None,
            "base_url": base_url if use_llm else None,
            "source_files": file_names,
            "total_rubrics": len(rubrics),
        },
        "summary": summary,
        "results": results,
    }
    Path(output_path).write_text(
        json.dumps(output_data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log(f"\n结果已保存到：{style(output_path, 'bold green') if console else output_path}")

    # ── 保存 CSV ────────────────────────────────────────────────────────────────
    csv_path = output_path.replace(".json", ".csv")

    check_keys = list(CHECK_LABELS.keys())
    fieldnames = ["ID", "constraint_type", "binary_type", "status"] + check_keys + ["issues"]

    with open(csv_path, "w", newline="", encoding="utf-8") as csvf:
        writer = csv_lib.DictWriter(csvf, fieldnames=fieldnames)
        writer.writeheader()
        for r in results:
            checks = r.get("checks", {})
            row = {
                "ID": r.get("id", ""),
                "constraint_type": r.get("constraint_type", ""),
                "binary_type": r.get("binary_type", ""),
                "status": r.get("status", ""),
            }
            for k in check_keys:
                v = checks.get(k)
                row[k] = "pass" if v is True else ("fail" if v is False else "N/A")
            row["issues"] = " | ".join(r.get("issues", [])) if r.get("issues") else ""
            writer.writerow(row)

    log(f"CSV 已保存到：{style(csv_path, 'bold green') if console else csv_path}")

    # ── 时间汇总 ──────────────────────────────────────────────────────────────
    timer.print_summary(console)


if __name__ == "__main__":
    main()
