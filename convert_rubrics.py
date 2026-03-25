#!/usr/bin/env python3
"""
Rubrics 格式转换器
将 raw_files 目录下的非标格式转换为标准格式，保持中文内容不变
"""

import json
from pathlib import Path


def convert_fields(fields: dict) -> dict:
    """转换字段名称，保持内容不变"""
    result = {}

    # ID 字段
    result["ID"] = fields.get("ID") or fields.get("id") or fields.get("rubric_id")

    # 类别字段
    category = fields.get("Category") or fields.get("category") or fields.get("类别 Category") or fields.get("类别") or fields.get("constraint_type") or fields.get("type", "")
    # 转换类别值
    category_map = {
        "硬性约束": "Hard Constraint",
        "硬约束": "Hard Constraint",
        "硬": "Hard Constraint",
        "hard": "Hard Constraint",
        "软性约束": "Soft Constraint",
        "软约束": "Soft Constraint",
        "软": "Soft Constraint",
        "soft": "Soft Constraint",
        "可选约束": "Optional Constraint",
        "optional": "Optional Constraint",
    }
    result["Category"] = category_map.get(category.lower() if category else "", category)

    # 描述字段
    result["rubric_description"] = (
        fields.get("rubric_description") or
        fields.get("Description") or
        fields.get("描述 Description") or
        fields.get("描述") or
        fields.get("description") or
        ""
    )

    # Binary vs Non-binary 字段
    binary = (
        fields.get("Binary vs. Non-binary") or
        fields.get("二元 / 非二元 Binary vs. Non-binary") or
        fields.get("二元/非二元 Binaryvs.Non-binary") or
        fields.get("binary") or
        fields.get("type") or
        fields.get("score_type") or
        ""
    )
    # 转换 binary 值
    if "二元" in binary and "非二元" not in binary:
        result["Binary vs. Non-binary"] = "Binary"
    elif "非二元" in binary or binary == "non-binary":
        result["Binary vs. Non-binary"] = "Non-binary"
    elif binary in ["binary", "Binary"]:
        result["Binary vs. Non-binary"] = "Binary"
    elif "0-4" in binary:
        result["Binary vs. Non-binary"] = "Non-binary"
    else:
        result["Binary vs. Non-binary"] = binary

    # 评分条件字段 - Binary 类型
    result["score_1_condition"] = (
        fields.get("score_1_condition") or
        fields.get("1 分标准") or
        fields.get("1 分") or
        fields.get("pass_criteria") or
        fields.get("scoring", {}).get("1") or
        fields.get("scores", {}).get("1") or
        ""
    )

    result["score_0_condition"] = (
        fields.get("score_0_condition") or
        fields.get("0 分标准") or
        fields.get("0 分") or
        fields.get("fail_criteria") or
        fields.get("scoring", {}).get("0") or
        fields.get("scores", {}).get("0") or
        ""
    )

    # 评分条件字段 - Non-binary 类型
    for i in [4, 3, 2, 1]:
        score_key = f"score_{i}_condition"
        result[score_key] = (
            fields.get(score_key) or
            fields.get(f"{i}分标准") or
            fields.get(f"{i} 分") or
            fields.get("scoring", {}).get(str(i)) or
            fields.get("scores", {}).get(str(i)) or
            fields.get("scoring_criteria", {}).get(str(i)) or
            ""
        )

    # 相关事实字段
    related_facts = fields.get("related_facts") or fields.get("相关事实") or ""
    if isinstance(related_facts, list):
        related_facts = "".join(related_facts)
    result["related_facts"] = related_facts

    # facts_reference 字段
    facts_ref = fields.get("facts_reference") or ""
    if isinstance(facts_ref, list):
        # 处理嵌套结构
        refs = []
        for item in facts_ref:
            if isinstance(item, dict) and "fact_reference" in item:
                fr = item["fact_reference"]
                if isinstance(fr, dict):
                    file_name = fr.get("file", "")
                    location = fr.get("location", {})
                    if isinstance(location, dict):
                        section = location.get("section", "")
                    else:
                        section = ""
                    if file_name and section:
                        refs.append(f"{file_name} {section}")
                    elif file_name:
                        refs.append(file_name)
        facts_ref = "; ".join(refs) if refs else ""
    result["facts_reference"] = facts_ref

    return result


def convert_file(input_path: Path, output_path: Path):
    """转换单个文件"""
    with open(input_path, 'r', encoding='utf-8') as f:
        content = f.read().strip()

    # 修复格式问题：多个对象没有被包裹在数组中
    if content.startswith('{') and not content.startswith('[{'):
        # 尝试将多个对象包装成数组
        lines = content.split('\n')
        objects = []
        current_obj = []
        brace_count = 0
        for line in lines:
            for char in line:
                if char == '{':
                    brace_count += 1
                elif char == '}':
                    brace_count -= 1
            current_obj.append(line)
            if brace_count == 0 and current_obj:
                obj_str = '\n'.join(current_obj).strip()
                if obj_str.endswith('},') or obj_str.endswith('}'):
                    objects.append(obj_str.rstrip(',').rstrip())
                    current_obj = []
        if current_obj:
            objects.append('\n'.join(current_obj).strip())
        # 包装成数组
        content = '[\n' + ',\n'.join(objects) + '\n]'

    # 清理 trailing commas
    import re
    content = re.sub(r',\s*}', '}', content)
    content = re.sub(r',\s*]', ']', content)

    raw = json.loads(content)
    rubrics = []

    # 处理不同结构
    if isinstance(raw, list):
        # 直接是数组
        rubrics = raw
    elif isinstance(raw, dict):
        # 可能是包装结构
        if "rubric" in raw and isinstance(raw["rubric"], dict):
            # {"prompt": [...], "rubric": {"Hard Constraint": [...], ...}}
            for cat_name, items in raw["rubric"].items():
                if isinstance(items, list):
                    for item in items:
                        if isinstance(item, dict):
                            item["Category"] = cat_name
                            rubrics.append(item)
        elif "评分表数据" in raw:
            # {"文件信息": {...}, "评分表数据": [...]}
            rubrics = raw["评分表数据"]
        elif "criteria" in raw:
            # {"title": "...", "criteria": [...]}
            rubrics = raw["criteria"]
        elif "rubrics" in raw:
            # {"task_name": "...", "rubrics": [...]}
            rubrics = raw["rubrics"]
        else:
            # 单个对象
            rubrics = [raw]

    # 转换每个 rubric
    converted = []
    for r in rubrics:
        converted.append(convert_fields(r))

    # 构建输出结构
    output = {
        "rubric": {
            "Hard Constraint": [],
            "Soft Constraint": [],
            "Optional Constraint": []
        }
    }

    for item in converted:
        cat = item.get("Category", "")
        if "Hard" in cat:
            output["rubric"]["Hard Constraint"].append(item)
        elif "Soft" in cat:
            output["rubric"]["Soft Constraint"].append(item)
        elif "Optional" in cat:
            output["rubric"]["Optional Constraint"].append(item)

    # 移除空类别
    output["rubric"] = {k: v for k, v in output["rubric"].items() if v}

    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"转换完成：{input_path.name} -> {output_path.name}")


def main():
    raw_dir = Path("/Users/jasonzhou/work/other/rubrics/raw_files")
    samples_dir = Path("/Users/jasonzhou/work/other/rubrics/samples")
    samples_dir.mkdir(exist_ok=True)

    # 文件映射：原文件 -> 新文件名
    file_mapping = {
        "3.json": "1_contract_review.json",
        "4.json": "2_lubricant_dispute.json",
        "5.json": "3_legal_civil_complaint.json",
        "6.json": "4_gym_membership_dispute.json",
        "7.json": "5_animal_damage_dispute.json",
        "8.json": "6_water_engineering_contract.json",
        "10.json": "7_loan_dispute.json",
    }

    for src, dst in file_mapping.items():
        input_path = raw_dir / src
        output_path = samples_dir / dst
        if input_path.exists():
            convert_file(input_path, output_path)
        else:
            print(f"文件不存在：{input_path}")


if __name__ == "__main__":
    main()
