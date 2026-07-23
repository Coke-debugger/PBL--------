"""六维度评审提示词构造。

设计原则（来自附录A"证据先行"）：模型必须先输出证据清单（含原文引用），
再按子指标判 0/1/2；不允许模型跳过证据直接给总分。C 维度更严格——只输出
「根因-原文引用-错误类型」清单，完全不给分，分数由 sampling.py 按扣分表
统一计算，避免每次采样口径漂移。
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

_RUBRIC_PATH = Path(__file__).parent / "rubric_dimensions.json"

POINT_DIMENSIONS = ("A", "B", "D", "E", "F")


@lru_cache(maxsize=1)
def load_rubric() -> dict:
    return json.loads(_RUBRIC_PATH.read_text(encoding="utf-8"))


def _sub_indicator_block(sub_indicators: list[dict]) -> str:
    lines = []
    for si in sub_indicators:
        lines.append(
            f"- {si['id']}（{si['name']}）：2分={si['level_2']}；1分={si['level_1']}；0分={si['level_0']}"
        )
    return "\n".join(lines)


def _profile_block(profile: dict) -> str:
    return json.dumps(profile, ensure_ascii=False, indent=2)


def build_point_dimension_prompt(
    dim: str, lesson_text: str, profile: dict, lesson_type: str = "常规课"
) -> str:
    """构造 A/B/D/E/F 五个计点制维度的评审提示词。"""
    if dim not in POINT_DIMENSIONS:
        raise ValueError(f"build_point_dimension_prompt 不支持维度 {dim}（该维度用扣分制，见 build_c_dimension_prompt）")

    rubric = load_rubric()
    dim_info = rubric["dimensions"][dim]
    sub_block = _sub_indicator_block(dim_info["sub_indicators"])

    extra_sections = []

    if dim == "D":
        proto = dim_info["mapping_table_protocol"]
        extra_sections.append(
            "【目标-活动映射表要求（D1专用）】\n"
            "独立构建映射表（不得采信教案自带的映射表/自评声明），JSON数组，每条格式：\n"
            '{"goal_id": "...", "goal_quote": "≤30字原文引用", "activity_quote": "≤30字原文引用", '
            '"activity_section": "所在章节", "eval_quote": "≤30字原文引用或null"}\n'
            f"- {proto['quote_rule']}\n"
            f"- {proto['eval_column_optional']}\n"
            f"- {proto['no_self_declaration']}\n"
            f"- {proto['granularity_rule']}\n"
            "- 重要：mapping_table 为辅助证据，宁可少写或留空数组 []，也要保证整个JSON"
            "合法闭合——sub_indicator_scores 是必填的打分依据，绝不能因映射表复杂而"
            "导致整体JSON输出失败。"
        )

    if dim == "F":
        subject = profile.get("subject", "")
        grade = profile.get("grade", "")
        ref = dim_info.get("literacy_reference", {}).get(subject)
        if ref:
            段位 = "高中" if "高" in grade else "义教"
            ref_text = ref.get(段位, ref.get("义教", ""))
            extra_sections.append(f"【本学科学段课标素养参照（{subject}·{grade}）】\n{ref_text}")
        extra_sections.append(
            "【关键判据操作化】\n" + "\n".join(f"- {c}" for c in dim_info["operational_criteria"])
        )
        extra_sections.append(
            "【F1贴标签分级封顶规则——你须在输出中提供 f1_goal_evidence 供程序据此判断】\n"
            + "\n".join(f"- {c}" for c in dim_info["f1_capping_rules"])
        )
        if lesson_type == "PBL":
            extra_sections.append(
                "【PBL课型加强判据】\n" + "\n".join(f"- {c}" for c in dim_info["pbl_bonus_rules"])
            )

    extra_block = ("\n\n" + "\n\n".join(extra_sections)) if extra_sections else ""

    f1_field = (
        ',\n  "f1_goal_evidence": [{"goal_quote": "...", "has_evidence": true}]  // 仅F维度必填：逐条判断每条素养目标是否有活动证据支撑'
        if dim == "F"
        else ""
    )
    mapping_field = (
        ',\n  "mapping_table": []  // 仅D维度，可空数组；保证JSON闭合优先'
        if dim == "D"
        else ""
    )

    return f"""你是教案质量评审专家，负责评审【维度{dim}·{dim_info['name']}】。

【维度定义】
{dim_info['definition']}

【子指标判据——每项独立判0/1/2分，禁止整体印象定档】
{sub_block}

【学情信息】
{_profile_block(profile)}

【课型】{lesson_type}

【教案（已剥离自述性元内容）】
{lesson_text}
{extra_block}

【输出要求——证据先行，禁止跳过证据直接给分】
严格输出JSON，格式：
{{
  "dimension": "{dim}",
  "evidence": [
    {{"sub_indicator": "{dim}1", "quote": "原文引用(≤30字)", "judgment": "满足/部分满足/不满足", "note": "简要说明"}}
  ],
  "sub_indicator_scores": {{ {", ".join(f'"{si["id"]}": 0' for si in dim_info["sub_indicators"])} }},
  "issues": [
    {{"quote": "原文引用", "problem": "问题描述", "severity": "major/minor"}}
  ]{f1_field}{mapping_field}
}}
只输出JSON，不要输出其他文字。"""


def build_c_dimension_prompt(lesson_text: str, profile: dict) -> str:
    """构造C维度（内容准确性）评审提示词：只列根因清单，不给分。"""
    rubric = load_rubric()
    dim_info = rubric["dimensions"]["C"]
    dedup_table = "\n".join(
        f"- {d['type']}：扣{d['deduction']}/根因{('，全文档封顶-' + str(d['cap'])) if 'cap' in d else ''}（{d['criteria']}）"
        for d in dim_info["deduction_table"]
    )

    return f"""你是教案质量评审专家，负责评审【维度C·内容准确性】。

【维度定义】
{dim_info['definition']}

【错误类型与扣分标准——你只需列出发现的问题，不需要自己计算总分】
{dedup_table}

【计数口径】
{chr(10).join(f"- {r}" for r in dim_info["unit_counting_rules"])}

【内容回避封顶条款】
{dim_info['floor_clause']}

【排他条款】
{dim_info['exclusion_clause']}

【历史/民俗说法免责判据——重要，防止误报】
教案背景/情境类文字中常出现历史沿用或民间技术性说法（如"古法用蜡烛火焰判断环境氧气浓度"
这类矿工/民俗经验之谈）。这类说法即使不是现代精确科学表述，只要没有明确的科学谬误证据
（定律错误、定义错误、可验证的事实性错误），不计入C维度错误。只对教学内容本身（目标、
例题、实验方案、化学式、数值计算等）里的确定性错误扣分，不要对背景情境文字里的传统说法
吹毛求疵——这类误判会同等拖累不同版本的评审分数，掩盖真正的内容质量差异。

【学情信息】
{_profile_block(profile)}

【教案（已剥离自述性元内容）】
{lesson_text}

【输出要求】
严格输出JSON，格式：
{{
  "dimension": "C",
  "has_verifiable_content": true,
  "issues": [
    {{"root_cause": "简明根因描述（用于跨采样对齐去重，措辞尽量规范化）",
      "quote": "原文引用(≤30字)",
      "error_type": "重大知识性错误|一般性不严谨|符号/公式实质问题|格式合规问题",
      "location": "首发位置"}}
  ]
}}
只输出JSON，不要输出其他文字，不要给出score字段。"""
