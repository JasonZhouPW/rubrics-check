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

  python rubrics_evaluator.py \\
    --input rubrics.json \\
    --base-url http://localhost:11434/v1 \\
    --api-key ollama \\
    --model qwen2.5:72b
"""

import argparse
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

try:
    from openai import OpenAI
except ImportError:
    print("[错误] 缺少依赖，请先运行: pip install openai rich")
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

# Module-level counter for auto-generating IDs
_item_counter = [0]

def extract_fields(r: dict) -> dict:
    """Extract and normalize all fields from a rubric item.

    Auto-inference rules (matching standard_template.json):
    - ID: auto-generated as #N if not present in the JSON
    - Binary vs. Non-binary: inferred from which score_N_condition fields exist
      if the explicit field is absent (standard template omits this field)
    """
    # ── ID ────────────────────────────────────────────────────────────────────
    rid = r.get("ID") or r.get("id") or r.get("rubric_id")
    if not rid:
        _item_counter[0] += 1
        rid = f"#{_item_counter[0]}"

    # ── Category (injected by load_rubrics from the dict key) ─────────────────
    category = (r.get("Category") or r.get("category") or
                r.get("类别Category") or r.get("类别") or "")

    # ── Description ───────────────────────────────────────────────────────────
    description = (r.get("rubric_description") or r.get("Description") or
                   r.get("描述Description") or r.get("描述") or
                   r.get("description") or r.get("criterion") or "")

    # ── Score conditions ──────────────────────────────────────────────────────
    score_0 = (r.get("score_0_condition") or r.get("0分标准") or r.get("0 分标准") or
               r.get("score_0") or "")
    score_1 = (r.get("score_1_condition") or r.get("1分标准") or r.get("1 分标准") or
               r.get("score_1") or "")
    score_2 = r.get("score_2_condition") or r.get("score_2") or ""
    score_3 = r.get("score_3_condition") or r.get("score_3") or ""
    score_4 = r.get("score_4_condition") or r.get("score_4") or ""
    score_criteria_blob = r.get("评分标准") or r.get("score_criteria") or ""

    # ── Binary vs. Non-binary: explicit field, else infer from score levels ───
    binary_explicit = (r.get("Binary vs. Non-binary") or
                       r.get("二元 / 非二元Binary vs. Non-binary") or
                       r.get("binary") or r.get("type") or "").strip()
    if binary_explicit:
        binary = binary_explicit
        binary_inferred = False
    else:
        # Infer: if any of score_2/3/4 present → Non-binary; else → Binary
        has_nonbinary_levels = bool(score_2 or score_3 or score_4)
        binary = "Non-binary" if has_nonbinary_levels else "Binary"
        binary_inferred = True

    # ── Facts ─────────────────────────────────────────────────────────────────
    related_facts   = (r.get("related_facts") or r.get("相关事实") or r.get("facts") or "")
    facts_reference = (r.get("facts_reference") or r.get("facts_ref") or
                       r.get("related_facts_reference") or "")

    return {
        "id":                  str(rid),
        "category":            category.strip(),
        "description":         description.strip(),
        "binary":              binary,
        "binary_inferred":     binary_inferred,
        "score_0":             score_0.strip(),
        "score_1":             score_1.strip(),
        "score_2":             score_2.strip(),
        "score_3":             score_3.strip(),
        "score_4":             score_4.strip(),
        "score_criteria_blob": score_criteria_blob.strip(),
        "related_facts":       related_facts.strip(),
        "facts_reference":     facts_reference,
    }


# ── 本地预检（规则检测，不消耗 LLM）────────────────────────────────────────────

def local_precheck(r: dict) -> list[dict]:
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

    # ── 1. 必填字段检查 ──────────────────────────────────────────────────────
    if not f["description"]:
        err("rubric_description", "rubric_description 字段为空")
    if not f["category"]:
        err("Category", "Category 字段为空")

    # ── 2. 类别合法性 ────────────────────────────────────────────────────────
    cat_lower = f["category"].lower()
    is_hard = "hard" in cat_lower or "硬" in cat_lower
    is_soft = "soft" in cat_lower or "软" in cat_lower
    is_optional = "optional" in cat_lower or "可选" in cat_lower

    if not (is_hard or is_soft or is_optional):
        err("Category", f"Category 值 [{f['category']}] 不在合法范围内 (Hard/Soft/Optional Constraint)")

    # ── 3. Binary vs. Non-binary ────────────────────────────────────────────
    binary_lower = f["binary"].lower()
    is_binary    = "non" not in binary_lower and "binary" in binary_lower
    is_nonbinary = "non-binary" in binary_lower or "non binary" in binary_lower

    # If the field was explicit but invalid, report error
    if not f["binary_inferred"] and not (is_binary or is_nonbinary):
        err("Binary vs. Non-binary", f"Value [{f['binary']}] is invalid; must be 'Binary' or 'Non-binary'")

    # If inferred, only warn (the standard template omits this field)
    if f["binary_inferred"]:
        warn("Binary vs. Non-binary",
             f"Field absent — inferred as '{f['binary']}' from score condition fields; "
             "add explicit 'Binary vs. Non-binary' field to conform to the standard format")

    # Hard Constraint must be Binary per spec
    if is_hard and is_nonbinary:
        err("Category+Binary", "Hard Constraint must be Binary — Non-binary is not allowed")

    # ── 4. Binary score condition checks ─────────────────────────────────────
    if is_binary:
        if not f["score_1"]:
            err("score_1_condition", "Binary type missing score_1_condition (criteria met)")
        if not f["score_0"]:
            err("score_0_condition", "Binary type missing score_0_condition (criteria not met)")
        if f["score_2"] or f["score_3"] or f["score_4"]:
            warn("score_2/3/4_condition",
                 "Binary type should not have score_2/3/4_condition fields")

    # ── 5. Non-binary score condition checks ─────────────────────────────────
    if is_nonbinary:
        missing_ends = [f"score_{k}_condition" for k in [0, 4] if not f[f"score_{k}"]]
        if missing_ends:
            err("score_conditions",
                f"Non-binary missing required endpoint(s): {', '.join(missing_ends)} "
                "(score_0 and score_4 are required)")

        mid_missing = [f"score_{i}_condition" for i in [1, 2, 3] if not f[f"score_{i}"]]
        if mid_missing:
            warn("score_conditions",
                 f"Non-binary: consider adding intermediate levels: {', '.join(mid_missing)}")

        if not f["score_0"] and not f["score_4"] and f["score_criteria_blob"]:
            warn("score_conditions",
                 "Score conditions use a merged blob field instead of individual "
                 "score_N_condition fields; recommend splitting per standard format")

    # ── 6. 主观模糊词检测 ───────────────────────────────────────────────────
    all_score_text = " ".join([
        f["description"], f["score_0"], f["score_1"],
        f["score_2"], f["score_3"], f["score_4"], f["score_criteria_blob"]
    ]).lower()
    for word in VAGUE_WORDS:
        if word.lower() in all_score_text:
            err("score_conditions", f"评分条件含主观/模糊词 [{word}]，需替换为客观量化标准")

    # ── 7. related_facts 检查 ───────────────────────────────────────────────
    # facts 不是必须的，但如果描述中涉及可验证事实则建议填写
    if not f["related_facts"]:
        warn("related_facts", "未填写 related_facts，若评分标准涉及可验证事实建议补充")

    # ── 8. facts_reference 检查 ─────────────────────────────────────────────
    if f["related_facts"] and not f["facts_reference"]:
        warn("facts_reference", "填写了 related_facts 但缺少 facts_reference（来源引用）")

    # ── 9. description 质量粗检 ─────────────────────────────────────────────
    if f["description"] and len(f["description"]) < 10:
        warn("rubric_description", "rubric_description 过短（<10字），描述可能不够清晰")

    return issues


# ── LLM 系统提示词 ────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a professional RL training data quality reviewer. Your task is to evaluate Rubric items against the official Rubrics Format Specification.

## Rubric Format Specification

### Categories (3 types)
- **Hard Constraint**: Binary only. Failure severely impairs task completion. Objective yes/no questions about explicit requirements.
- **Soft Constraint**: Binary or Non-binary. Important quality factors. Omission substantially reduces user satisfaction but doesn't cause complete failure.
- **Optional Constraint**: Binary or Non-binary. User experience enhancements not explicitly stated. Omission doesn't significantly affect results.

### Scoring Logistics
- **Binary**: score_1_condition (criteria met) + score_0_condition (criteria not met). Hard Constraints MUST be Binary.
- **Non-binary**: score_0_condition through score_4_condition. Each level must be objectively distinguishable.

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
    "binary_field_valid": true | false,
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
- **fail**: 2+ checks false, OR any of these critical failures: vague scoring language, missing score conditions, Hard Constraint marked Non-binary

Output ONLY the JSON array. No explanation text."""


def build_user_prompt(rubrics: list[dict]) -> str:
    items = []
    for r in rubrics:
        f = extract_fields(r)
        item = {
            "ID": f["id"],
            "Category": f["category"],
            "rubric_description": f["description"],
            "Binary vs. Non-binary": f["binary"],
            "score_0_condition": f["score_0"],
            "score_1_condition": f["score_1"],
            "score_2_condition": f["score_2"],
            "score_3_condition": f["score_3"],
            "score_4_condition": f["score_4"],
            "related_facts": f["related_facts"],
            "facts_reference": str(f["facts_reference"]) if f["facts_reference"] else "",
        }
        # drop empty optional fields to save tokens
        item = {k: v for k, v in item.items() if v}
        items.append(item)
    return f"Please evaluate the following {len(items)} rubric(s):\n\n{json.dumps(items, ensure_ascii=False, indent=2)}"


# ── LLM 调用 ──────────────────────────────────────────────────────────────────

def call_llm(client: "OpenAI", model: str, rubrics_batch: list[dict], retries: int = 3) -> list[dict]:
    # 本地预检先跑
    precheck_map: dict[str, list[dict]] = {}
    for r in rubrics_batch:
        rid = extract_fields(r)["id"]
        issues = local_precheck(r)
        if issues:
            precheck_map[rid] = issues

    for attempt in range(retries):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": build_user_prompt(rubrics_batch)},
                ],
                temperature=0.1,
                max_tokens=4096,
            )
            raw = resp.choices[0].message.content.strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[-1]
                raw = raw.rsplit("```", 1)[0].strip()
            llm_results: list[dict] = json.loads(raw)

            # 合并本地预检问题 → LLM 结果
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

                    # 本地 error 级别问题 → 升级 status
                    has_local_error = any(li["level"] == "error" for li in local_issues)
                    local_error_count = sum(1 for li in local_issues if li["level"] == "error")
                    if has_local_error and item.get("status") == "pass":
                        item["status"] = "warn"
                    if local_error_count >= 2:
                        item["status"] = "fail"

            return llm_results

        except json.JSONDecodeError as e:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"LLM 返回无法解析的 JSON: {e}")
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"LLM 调用失败: {e}")
    return []


# ── 批处理 ────────────────────────────────────────────────────────────────────

def process_in_batches(
    client: "OpenAI",
    model: str,
    rubrics: list[dict],
    batch_size: int,
    delay: float,
    console=None,
) -> list[dict]:
    results = []
    batches = [rubrics[i:i + batch_size] for i in range(0, len(rubrics), batch_size)]

    if HAS_RICH and console:
        with Progress(
            SpinnerColumn(),
            TextColumn("[bold blue]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total} batches"),
            console=console,
        ) as progress:
            task = progress.add_task("Evaluating", total=len(batches))
            for i, batch in enumerate(batches):
                progress.update(task, description=f"Batch {i+1}/{len(batches)} ({len(batch)} items)")
                batch_results = call_llm(client, model, batch)
                results.extend(batch_results)
                progress.advance(task)
                if i < len(batches) - 1:
                    time.sleep(delay)
    else:
        for i, batch in enumerate(batches):
            print(f"[{i+1}/{len(batches)}] Processing batch ({len(batch)} items)...")
            batch_results = call_llm(client, model, batch)
            results.extend(batch_results)
            if i < len(batches) - 1:
                time.sleep(delay)

    return results


# ── 汇总统计 ──────────────────────────────────────────────────────────────────

CHECK_LABELS = {
    "category_valid":                "类别合法",
    "binary_field_valid":            "Binary字段",
    "score_conditions_complete":     "评分条件完整",
    "objectivity":                   "客观性",
    "quantification":                "量化性",
    "mece_within_item":              "MECE原则",
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


def load_rubrics(input_path: str, recursive: bool = False) -> tuple[list[dict], list[str]]:
    """
    Load rubric items from one or more JSON files.

    Supports multiple wrapper formats:
      - Flat list:  [ {rubric}, ... ]
      - Wrapped:    { "rubric": { "Hard Constraint": [...], "Soft Constraint": [...], ... } }
      - Wrapped:    { "rubrics": [...] } / { "items": [...] }
    """
    files = collect_json_files(input_path, recursive)
    all_rubrics: list[dict] = []
    file_names: list[str] = []
    errors: list[str] = []

    for fp in files:
        _item_counter[0] = 0  # reset auto-ID counter per file
        try:
            raw = json.loads(fp.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            errors.append(f"{fp.name}: JSON parse error — {e}")
            continue

        before = len(all_rubrics)

        if isinstance(raw, list):
            all_rubrics.extend(raw)

        elif isinstance(raw, dict):
            # Official format: {"prompt": [...], "rubric": {"Hard Constraint": [...], ...}}
            rubric_block = raw.get("rubric") or raw.get("Rubric")
            if isinstance(rubric_block, dict):
                for cat_name, items in rubric_block.items():
                    if isinstance(items, list):
                        for item in items:
                            if isinstance(item, dict):
                                # Inject category if not present
                                if not item.get("Category") and not item.get("category") and not item.get("类别Category"):
                                    item = dict(item)
                                    item["Category"] = cat_name
                                all_rubrics.append(item)
            else:
                # Wrapped list formats
                for key in ("rubrics", "items", "criteria", "data"):
                    if key in raw and isinstance(raw[key], list):
                        all_rubrics.extend(raw[key])
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
    p.add_argument("--input",      "-i", required=True,
                   help="Input path: single JSON file or folder")
    p.add_argument("--recursive",  "-r", action="store_true",
                   help="Recursively scan subdirectories (folder mode only)")
    p.add_argument("--output",     "-o", default="",
                   help="Output JSON path (default: eval_result.json or <name>_eval_result.json)")
    p.add_argument("--base-url",   default="https://api.openai.com/v1",
                   help="LLM API base URL (default: OpenAI)")
    p.add_argument("--api-key",    default=os.environ.get("OPENAI_API_KEY", ""),
                   help="API key (or set OPENAI_API_KEY env var)")
    p.add_argument("--model",      default="gpt-4o",
                   help="Model name (default: gpt-4o)")
    p.add_argument("--batch-size", type=int, default=10,
                   help="Items per batch (default: 10)")
    p.add_argument("--delay",      type=float, default=1.0,
                   help="Delay between batches in seconds (default: 1.0)")
    p.add_argument("--no-color",   action="store_true",
                   help="Disable colored output")
    return p.parse_args()


def main():
    args = parse_args()
    console = Console() if HAS_RICH and not args.no_color else None

    def log(msg):
        if console: console.print(msg)
        else: print(msg)

    if not args.api_key:
        log("[red][Error][/red] Provide --api-key or set OPENAI_API_KEY" if console
            else "[Error] Provide --api-key or set OPENAI_API_KEY")
        sys.exit(1)

    # ── Load ──────────────────────────────────────────────────────────────────
    input_p = Path(args.input)
    mode = ("folder (recursive)" if args.recursive else "folder") if input_p.is_dir() else "file"
    log(f"\n[bold]Loading rubrics[/bold]: {args.input}  [{mode}]" if console
        else f"\nLoading rubrics: {args.input}  [{mode}]")

    try:
        rubrics, file_names = load_rubrics(args.input, recursive=args.recursive)
    except (FileNotFoundError, ValueError) as e:
        log(f"[red][Error][/red] {e}" if console else f"[Error] {e}")
        sys.exit(1)

    if not rubrics:
        log("[Error] No rubric items found")
        sys.exit(1)

    log(f"Loaded [bold]{len(rubrics)}[/bold] rubric(s) from {len(file_names)} file(s):" if console
        else f"Loaded {len(rubrics)} rubric(s) from {len(file_names)} file(s):")
    for fn in file_names:
        log(f"  [dim]• {fn}[/dim]" if console else f"  • {fn}")
    log(f"Model: [cyan]{args.model}[/cyan]  Batch size: {args.batch_size}  Base URL: {args.base_url}\n"
        if console else f"Model: {args.model}  Batch size: {args.batch_size}  Base URL: {args.base_url}\n")

    # ── Client ────────────────────────────────────────────────────────────────
    client = OpenAI(
        api_key=args.api_key,
        base_url=args.base_url if args.base_url != "https://api.openai.com/v1" else None,
    )

    # ── Evaluate ──────────────────────────────────────────────────────────────
    try:
        results = process_in_batches(client, args.model, rubrics, args.batch_size, args.delay, console)
    except RuntimeError as e:
        log(f"[red][Error][/red] {e}" if console else f"[Error] {e}")
        sys.exit(1)

    if not results:
        log("[Error] No results returned from LLM")
        sys.exit(1)

    summary = compute_summary(results)

    if HAS_RICH and console:
        print_rich_results(results, summary, console)
    else:
        print_plain_results(results, summary)

    # ── Save ──────────────────────────────────────────────────────────────────
    if args.output:
        output_path = args.output
    elif input_p.is_dir():
        output_path = str(input_p / "eval_result.json")
    else:
        output_path = input_p.stem + "_eval_result.json"

    output_data = {
        "meta": {
            "generated_at": datetime.now().isoformat(),
            "model": args.model,
            "base_url": args.base_url,
            "source_files": file_names,
            "total_rubrics": len(rubrics),
        },
        "summary": summary,
        "results": results,
    }
    Path(output_path).write_text(
        json.dumps(output_data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log(f"\nResults saved to: [bold green]{output_path}[/bold green]" if console
        else f"\nResults saved to: {output_path}")


if __name__ == "__main__":
    main()