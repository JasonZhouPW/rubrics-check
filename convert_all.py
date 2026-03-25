#!/usr/bin/env python3
"""Rubrics 格式转换器 - 保持中文内容不变，仅转换字段名称，符合 standard_template.json 格式"""

import json
import re
from pathlib import Path


def parse_multiple_objects(content):
    """解析多个 JSON 对象（处理没有数组包装的情况）"""
    objects = []
    current = ''
    brace_count = 0
    in_string = False
    escape_next = False

    for char in content:
        if escape_next:
            escape_next = False
            current += char
            continue

        if char == '\\':
            escape_next = True
            current += char
            continue

        if char == '"' and not escape_next:
            in_string = not in_string

        if not in_string:
            if char == '{':
                brace_count += 1
            elif char == '}':
                brace_count -= 1

        current += char

        if brace_count == 0 and current.strip():
            current = current.strip().rstrip(',').strip()
            if current.startswith('{') and current.endswith('}'):
                try:
                    obj = json.loads(current)
                    objects.append(obj)
                except:
                    pass
            current = ''

    return objects


def parse_json_file(content):
    """尝试多种方式解析 JSON 内容"""
    content = content.strip()

    # 清理 trailing commas
    content = re.sub(r',\s*}', '}', content)
    content = re.sub(r',\s*]', ']', content)

    # 尝试直接解析
    try:
        raw = json.loads(content)
        if isinstance(raw, list):
            return raw
        elif isinstance(raw, dict):
            # 检查是否有包装结构
            if "rubrics" in raw:
                return raw["rubrics"]
            elif "criteria" in raw:
                return raw["criteria"]
            elif "评分表数据" in raw:
                return raw["评分表数据"]
            elif "rubric" in raw and isinstance(raw["rubric"], dict):
                result = []
                for cat_name, items in raw["rubric"].items():
                    if isinstance(items, list):
                        for item in items:
                            if isinstance(item, dict):
                                item["Category"] = cat_name
                                result.append(item)
                return result
            return [raw]
    except:
        pass

    # 尝试包装成数组
    if content.startswith('{'):
        try:
            raw = json.loads('[' + content + ']')
            return raw
        except:
            pass

    # 使用多对象解析
    return parse_multiple_objects(content)


def get_category_mapping(cat):
    """将类别映射到英文标准值"""
    cat_str = str(cat) if cat else ""
    cat_lower = cat_str.lower()

    category_map = {
        # Chinese variants
        "硬性约束": "Hard Constraint", "硬约束": "Hard Constraint", "硬": "Hard Constraint",
        "软性约束": "Soft Constraint", "软约束": "Soft Constraint", "软": "Soft Constraint",
        "可选约束": "Optional Constraint",
        # English variants
        "hard": "Hard Constraint", "hard constraint": "Hard Constraint",
        "soft": "Soft Constraint", "soft constraint": "Soft Constraint",
        "optional": "Optional Constraint", "optional constraint": "Optional Constraint",
    }

    return category_map.get(cat_str) or category_map.get(cat_lower, str(cat))


def convert_fields(item):
    """转换字段名称，保持中文内容不变 - 符合 standard_template.json 格式"""

    # 类别 - 扩展映射 (includes both no-space and space variants)
    cat = (item.get("Category") or item.get("category") or
           item.get('类别Category') or
           item.get("类别 Category") or
           item.get("类别 Category") or
           item.get("类别") or
           item.get("constraint_type") or
           item.get("type", ""))

    category = get_category_mapping(cat)

    # 描述 (includes both no-space and space variants)
    rubric_description = (
        item.get("rubric_description") or item.get("Description") or
        item.get('描述Description') or
        item.get("描述 Description") or
        item.get("描述 Description") or
        item.get("描述") or
        item.get("description", "")
    )

    # Binary vs Non-binary - determine type
    binary = (
        item.get("Binary vs. Non-binary") or item.get("二元 / 非二元 Binary vs. Non-binary") or
        item.get("二元/非二元 Binaryvs.Non-binary") or item.get("binary") or
        item.get("type") or item.get("score_type", "")
    )
    binary_str = str(binary)

    # Determine if Binary or Non-binary
    is_binary = False
    if "二元" in binary_str and "非二元" not in binary_str:
        is_binary = True
    elif binary_str.lower() == "binary":
        is_binary = True
    elif "0-4" in binary_str or "非二元" in binary_str:
        is_binary = False
    else:
        is_binary = True  # Default to Binary

    # 评分条件
    score_0 = (
        item.get("score_0_condition") or item.get("0 分标准") or item.get("0 分") or
        item.get("fail_criteria") or item.get("scoring", {}).get("0") or
        item.get("scores", {}).get("0", "")
    )
    score_1 = (
        item.get("score_1_condition") or item.get("1 分标准") or item.get("1 分") or
        item.get("pass_criteria") or item.get("scoring", {}).get("1") or
        item.get("scores", {}).get("1", "")
    )

    # 评分条件 - Non-binary (score_2 to score_4)
    score_2 = (
        item.get("score_2_condition") or item.get("2 分标准") or item.get("2 分") or
        item.get("scoring", {}).get("2") or item.get("scores", {}).get("2", "")
    )
    score_3 = (
        item.get("score_3_condition") or item.get("3 分标准") or item.get("3 分") or
        item.get("scoring", {}).get("3") or item.get("scores", {}).get("3", "")
    )
    score_4 = (
        item.get("score_4_condition") or item.get("4 分标准") or item.get("4 分") or
        item.get("scoring", {}).get("4") or item.get("scores", {}).get("4", "")
    )

    # related_facts
    rf = item.get("related_facts") or item.get("相关事实", "")
    if isinstance(rf, list):
        rf = "".join(rf)

    # facts_reference
    fr = item.get("facts_reference") or ""
    if isinstance(fr, list):
        refs = []
        for f_item in fr:
            if isinstance(f_item, dict) and "fact_reference" in f_item:
                fr_obj = f_item["fact_reference"]
                if isinstance(fr_obj, dict):
                    file_name = fr_obj.get("file", "")
                    loc = fr_obj.get("location", {})
                    section = loc.get("section", "") if isinstance(loc, dict) else ""
                    if file_name and section:
                        refs.append(f"{file_name} {section}")
                    elif file_name:
                        refs.append(file_name)
        fr = "; ".join(refs) if refs else ""

    # Build output - standard_template format (no ID, no Binary vs. Non-binary)
    converted = {"rubric_description": rubric_description}

    if is_binary:
        converted["score_0_condition"] = score_0
        converted["score_1_condition"] = score_1
    else:
        converted["score_0_condition"] = score_0
        converted["score_1_condition"] = score_1
        converted["score_2_condition"] = score_2
        converted["score_3_condition"] = score_3
        converted["score_4_condition"] = score_4

    # Only add related_facts and facts_reference if non-empty
    if rf:
        converted["related_facts"] = rf
    if fr:
        converted["facts_reference"] = fr

    return category, converted


def build_output(rubrics):
    """构建输出结构 - 符合 standard_template.json 格式"""
    output = {"rubric": {"Hard Constraint": [], "Soft Constraint": [], "Optional Constraint": []}}

    for item in rubrics:
        category, converted = convert_fields(item)
        if "Hard" in category:
            output["rubric"]["Hard Constraint"].append(converted)
        elif "Soft" in category:
            output["rubric"]["Soft Constraint"].append(converted)
        elif "Optional" in category:
            output["rubric"]["Optional Constraint"].append(converted)

    output["rubric"] = {k: v for k, v in output["rubric"].items() if v}
    return output


def save_output(output, filename):
    with open(f'/Users/jasonzhou/work/other/rubrics/samples/{filename}', 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"完成：{filename}")


def main():
    samples_dir = Path('/Users/jasonzhou/work/other/rubrics/samples')
    samples_dir.mkdir(exist_ok=True)

    # 文件映射
    files = [
        ("3.json", "1_contract_review.json"),
        ("4.json", "2_lubricant_dispute.json"),
        ("5.json", "3_legal_civil_complaint.json"),
        ("6.json", "4_gym_membership_dispute.json"),
        ("7.json", "5_animal_damage_dispute.json"),
        ("8.json", "6_water_engineering_contract.json"),
        ("10.json", "7_loan_dispute.json"),
    ]

    for src, dst in files:
        src_path = Path(f'/Users/jasonzhou/work/other/rubrics/raw_files/{src}')
        if not src_path.exists():
            print(f"文件不存在：{src_path}")
            continue

        with open(src_path, 'r', encoding='utf-8') as f:
            content = f.read()

        raw = parse_json_file(content)
        print(f"{src}: 解析成功，共 {len(raw)} 条记录")

        output = build_output(raw)
        save_output(output, dst)

    print("\n全部转换完成!")


if __name__ == "__main__":
    main()
