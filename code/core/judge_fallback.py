"""core/judge_fallback.py — 纯规则兜底评分（零 LLM 调用）。

用途：当完整多采样评审超时且回退到快速模式后仍失败（例如模型服务不可用、
持续 429）时，作为最后一道保险给出分数，保证 *_scores.json 一定产出，UI
分数栏一定有数。

设计取舍：
- 只用量规里"程序化、确定性"的部分（附录A里标注 scoring_method 含"规则校验/
  Tier-0规则"的判据），不臆造需要 LLM 判断的子指标得分。
- 因此 fallback 分数系统性偏低且偏保守——它衡量的是"结构是否齐全、是否有
  明显知识错误"，不衡量"素养目标行为化""情境非装饰性"这类必须语义判断的维度。
- 在 report 里用 judge_mode="fallback" 明确标注，UI 和下游都能识别这是兜底分
  而非正式评审，避免和完整/快速评审的分数混为一谈。

返回结构与 src.judge.Judge.evaluate 保持一致（total/dimension_scores/
low_dims/judge_version/judge_mode），消费方按 key 取值即可，不假设封闭 schema。
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from pathlib import Path

# 维度权重与 Judge 一致，保证总分口径可比。
WEIGHTS = {"A": 10, "B": 15, "C": 20, "D": 15, "E": 10, "F": 30}

# PBL 核心件同义词（与 rubric_dimensions.json 的 course_type_structure 对齐）。
_PBL_CORE = {
    "项目简介": ["项目简介", "项目背景", "项目导引"],
    "项目目标": ["项目目标", "学习目标", "教学目标"],
    "驱动性问题": ["驱动性问题", "驱动问题", "核心问题", "问题导引"],
    "任务链": ["任务链", "任务1", "任务一", "子任务", "任务设计"],
    "成果交流": ["成果交流", "新品发布", "成果展示", "班级发布"],
}
_REGULAR_CORE = {
    "教学目标": ["教学目标", "学习目标", "项目目标", "课时目标", "单元目标", "素养目标"],
    "教学重难点": ["教学重难点", "教学重点", "教学难点", "重点难点"],
    "教学过程": ["教学过程", "学习过程", "教学环节", "教学活动", "活动设计"],
}


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    return re.sub(r"\s+", "", text)


def _count_core(text: str, syn_map: dict) -> tuple[int, list[str]]:
    """返回（命中的核心件数，缺失件名列表）。"""
    norm = _normalize(text)
    missing = [name for name, syns in syn_map.items()
               if not any(_normalize(s) in norm for s in syns)]
    return len(syn_map) - len(missing), missing


def _check_c_errors(text: str) -> float:
    """C 维度：检测已知确定性知识错误，按扣分表计分。与 core/judge.py 规则层一致。

    返回 0~5 的维度分（5 = 未检出错误）。只检测"硬"错误，不做语义判断，因此
    会高估 C 分（漏判模型才能发现的软错误）——这是 fallback 的固有局限。
    """
    deduction = 0.0
    # 石蜡完全燃烧生成物写成 CO（应为 CO₂）
    if re.search(r"50CO(?!\s*[₂2])", text) or re.search(r"51O_2.*?50CO", text):
        deduction += 2.0
    # 石蜡与 O₂ 质量比错误
    if "816:352" in text or "816：352" in text:
        deduction += 2.0
    return max(0.0, min(5.0, 5.0 - deduction))


def _check_e_redundancy(text: str) -> float:
    """E 维度 Tier-0 规则：检测大段重复段落。无重复给 4.0（保守，留 1 分给需
    语义判断的术语一致/层级规范）；检测到明显拼贴重复给 2.0。"""
    norm = _normalize(text)
    if len(norm) < 200:
        return 4.0
    # 取 40 字滑窗检测重复块
    chunks = [norm[i:i + 40] for i in range(0, len(norm) - 40, 20)]
    unique = set(chunks)
    dup_ratio = 1 - len(unique) / max(1, len(chunks))
    return 2.0 if dup_ratio > 0.25 else 4.0


def evaluate_fallback(
    lesson_text: str,
    lesson_type: str = "常规课",
    profile: dict | None = None,
    n_modifications: int = 0,
) -> dict:
    """兜底评分主入口。n_modifications 用于给 B/D 维度一个"已改写"的弱信号。"""
    profile = profile or {}
    syn_map = _PBL_CORE if lesson_type == "PBL" else _REGULAR_CORE
    core_hit, missing = _count_core(lesson_text, syn_map)
    core_total = len(syn_map)

    scores: dict[str, float] = {}

    # A 维度：核心件齐全度。缺任一核心件封顶 2.0（量规硬性规则）。
    if core_hit == core_total:
        a = 4.0  # 结构齐全但"实质性"需 LLM 判断，保守给 4
    elif core_hit >= core_total - 1:
        a = 2.5
    else:
        a = 1.5
    # PBL 缺成果交流节 → 同时计 F6 缺失（见下方 F）
    pbl_missing_eval = "成果交流" in missing if lesson_type == "PBL" else False
    scores["A"] = a

    # B 维度：无 LLM 难判可执行颗粒度，按"是否有实质修改/活动描述"给弱分。
    has_activity = bool(re.search(r"(活动|环节|任务|步骤)", lesson_text))
    scores["B"] = 3.5 if (has_activity or n_modifications > 0) else 2.5

    # C 维度：确定性错误检测
    scores["C"] = _check_c_errors(lesson_text)

    # D 维度：无映射表无法判一致性，给中性偏低分
    scores["D"] = 3.0 if n_modifications > 0 else 2.0

    # E 维度：Tier-0 重复检测
    scores["E"] = _check_e_redundancy(lesson_text)

    # F 维度：PBL 缺双轨量规 → F6=0（量规联动规则），整体保守压低
    has_rubric = bool(re.search(r"(量规|评价标准|自评|互评)", lesson_text))
    if pbl_missing_eval or not has_rubric:
        scores["F"] = 2.0
    else:
        scores["F"] = 3.0

    total = sum(scores[d] / 5 * WEIGHTS[d] for d in WEIGHTS)
    low_dims = sorted(scores, key=scores.get)[:2]

    # judge_version 用固定标记区分兜底路径，不与正式 rubric 哈希混淆。
    return {
        "total": round(total, 2),
        "dimension_scores": {d: round(v, 2) for d, v in scores.items()},
        "low_dims": low_dims,
        "details": {
            "fallback_note": "纯规则兜底评分，未调用 LLM；维度分系统性偏低，仅作保守参考。",
            "missing_core": missing,
            "n_modifications": n_modifications,
        },
        "manipulation_flags": [],
        "lesson_type": lesson_type,
        "truncated": False,
        "judge_version": "fallback-rules-v1",
        "judge_mode": "fallback",
        "ROB": None,
    }
