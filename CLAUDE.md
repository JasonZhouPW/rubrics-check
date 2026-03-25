# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Python CLI tool that evaluates Rubrics JSON files for quality and format compliance. It performs local pre-checks and LLM-based evaluation of rubric items across Hard/Soft/Optional Constraint categories.

## Commands

```bash
# Run evaluator (single file)
python rubrics_evaluator.py --input rubrics.json --api-key $API_KEY --model claude-sonnet-4-20250514

# Run evaluator (directory)
python rubrics_evaluator.py --input ./rubrics_dir/ --api-key $API_KEY --model gpt-4o

# With custom API endpoint (e.g., Ollama)
python rubrics_evaluator.py --input ./rubrics_dir/ --base-url http://localhost:11434/v1 --api-key ollama --model qwen2.5:72b

# Recursive directory scan
python rubrics_evaluator.py --input ./data/ --recursive --api-key $API_KEY --model gpt-4o

# View example rubric format
cat rubrics_example.json
```

## Architecture

**Core modules** (`rubrics_evaluator.py`):
- `local_precheck()` — Rule-based validation (category合法性，binary/non-binary consistency, vague word detection)
- `call_llm()` — LLM evaluation via OpenAI-compatible API
- `process_in_batches()` — Batch processing with rate limiting
- `compute_summary()` — Aggregate statistics and check pass rates

**Data format**:
- Input: JSON with `rubric` block containing `Hard Constraint`, `Soft Constraint`, `Optional Constraint` arrays
- Output: `eval_result.json` with per-rubric checks (MECE, objectivity, quantification, etc.) and status (pass/warn/fail)

**Evaluation checks**:
1. Category validity (Hard/Soft/Optional)
2. Binary vs Non-binary field consistency
3. Score conditions completeness
4. Objectivity (no vague language)
5. Quantification (measurable thresholds)
6. MECE principle (mutually exclusive, collectively exhaustive)
7. Completion vs Quality separation
8. Facts reference presence

## Dependencies

```bash
pip install openai rich
```

## Example Format

See `rubrics_example.json` for the official format:
- Binary rubrics: `score_0_condition` + `score_1_condition`
- Non-binary rubrics: `score_0_condition` through `score_4_condition`
- Hard Constraints must be Binary
