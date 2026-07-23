"""不调用API的聚合逻辑单元测试：根因对齐去重、映射表模糊匹配、多数投票。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.judge.sampling import (
    _majority_score,
    _normalize,
    aggregate_c_dimension,
    aggregate_point_dimension,
    verify_evidence_quotes,
    verify_mapping_table,
)


def test_majority_score_clear_winner():
    assert _majority_score([2, 2, 1]) == 2


def test_majority_score_tie_picks_lower():
    # 平票时取较低分（保守策略）
    assert _majority_score([2, 2, 1, 1]) == 1


def test_majority_score_empty_defaults_zero():
    assert _majority_score([]) == 0


def test_aggregate_point_dimension_majority_vote():
    lesson_text = "教学目标：能从露营停电情境中提出燃烧时间问题。教学过程：小组讨论烛芯粗细对燃烧的影响。"
    samples = [
        {"sub_indicator_scores": {"A1": 2, "A2": 2, "A3": 1}, "evidence": [], "issues": []},
        {"sub_indicator_scores": {"A1": 2, "A2": 1, "A3": 1}, "evidence": [], "issues": []},
        {"sub_indicator_scores": {"A1": 1, "A2": 1, "A3": 0}, "evidence": [], "issues": []},
    ]
    agg = aggregate_point_dimension("A", samples, lesson_text)
    assert agg["sub_indicator_scores"]["A1"] == 2  # 2/2/1 -> 多数2
    assert agg["sub_indicator_scores"]["A2"] == 1  # 2/1/1 -> 多数1
    assert agg["sub_indicator_scores"]["A3"] == 1  # 1/1/0 -> 1出现2次，明确多数
    assert agg["evidence_verified_ratio"] == 1.0  # 没有quote时不惩罚


def test_aggregate_point_dimension_flags_low_evidence_trust():
    lesson_text = "教学目标：能从露营停电情境中提出燃烧时间问题。"
    samples = [
        {
            "sub_indicator_scores": {"A1": 2, "A2": 2, "A3": 2},
            "evidence": [
                {"sub_indicator": "A1", "quote": "这句话根本不在原文里", "judgment": "满足"},
                {"sub_indicator": "A2", "quote": "这句话也不在原文里", "judgment": "满足"},
            ],
            "issues": [],
        }
    ]
    agg = aggregate_point_dimension("A", samples, lesson_text)
    assert agg["evidence_verified_ratio"] == 0.0
    assert all(e["quote_verified"] is False for e in agg["evidence"])


def test_aggregate_c_dimension_root_cause_dedup_and_threshold():
    samples = [
        {
            "issues": [
                {"root_cause": "石蜡完全燃烧生成CO而非CO2", "error_type": "重大知识性错误", "quote": "2C25H52+51O2->50CO+52H2O"},
            ],
            "has_verifiable_content": True,
        },
        {
            "issues": [
                {"root_cause": "石蜡完全燃烧生成CO而非CO2", "error_type": "重大知识性错误", "quote": "2C25H52+51O2->50CO+52H2O"},
            ],
            "has_verifiable_content": True,
        },
        {
            "issues": [
                {"root_cause": "个别段落史实细节偏差", "error_type": "一般性不严谨", "quote": "唐代..."},
            ],
            "has_verifiable_content": True,
        },
    ]
    result = aggregate_c_dimension(samples, n_samples=3)
    # 燃烧方程式错误命中2/3，达到阈值(3*2/3=2)，应被确认；史实偏差只命中1/3，不应被确认
    root_causes = [i["root_cause"] for i in result["confirmed_issues"]]
    assert "石蜡完全燃烧生成CO而非CO2" in root_causes
    assert "个别段落史实细节偏差" not in root_causes
    assert result["score"] == 3.0  # 5 - 2(重大知识性错误) = 3


def test_aggregate_c_dimension_floor_clause_no_verifiable_content():
    samples = [
        {"issues": [], "has_verifiable_content": False},
        {"issues": [], "has_verifiable_content": False},
        {"issues": [], "has_verifiable_content": False},
    ]
    result = aggregate_c_dimension(samples, n_samples=3)
    assert result["score"] <= 3.0  # 内容回避封顶条款


def test_verify_mapping_table_marks_invalid_quote():
    lesson_text = "教学目标：能从露营停电情境中提出燃烧时间问题。教学过程：小组讨论烛芯粗细对燃烧的影响。"
    mapping = [
        {"goal_quote": "能从露营停电情境中提出燃烧时间问题", "activity_quote": "小组讨论烛芯粗细对燃烧的影响"},
        {"goal_quote": "这句话根本不在原文里", "activity_quote": "小组讨论烛芯粗细对燃烧的影响"},
    ]
    result = verify_mapping_table(mapping, lesson_text)
    assert result[0]["valid"] is True
    assert result[1]["valid"] is False
    assert "invalid_reason" in result[1]


def test_normalize_strips_punctuation_and_whitespace():
    assert _normalize("能从 露营，停电！情境中") == _normalize("能从露营停电情境中")


def test_verify_evidence_quotes_mixed():
    lesson_text = "教学目标：能从露营停电情境中提出燃烧时间问题。"
    evidence = [
        {"quote": "能从露营停电情境中提出燃烧时间问题", "judgment": "满足"},
        {"quote": "编造的引用", "judgment": "满足"},
        {"judgment": "满足"},  # 无quote字段，不计入分母
    ]
    verified, ratio = verify_evidence_quotes(evidence, lesson_text)
    assert verified[0]["quote_verified"] is True
    assert verified[1]["quote_verified"] is False
    assert "quote_verified" not in verified[2]
    assert ratio == 0.5


def test_verify_evidence_quotes_no_quotes_not_penalized():
    verified, ratio = verify_evidence_quotes([{"judgment": "满足"}], "任意教案正文")
    assert ratio == 1.0


def test_check_core_elements_present_detects_missing_pbl_core():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from src.judge.judge import Judge

    judge = Judge({"subject": "化学", "grade": "初中"})
    text_missing_task_chain = "项目简介：应急蜡烛。项目目标：设计蜡烛。驱动性问题：如何设计蜡烛？"
    missing = judge._check_core_elements_present(text_missing_task_chain, "PBL")
    assert "任务链" in missing

    text_complete = "项目简介：应急蜡烛。项目目标：设计蜡烛。驱动性问题：如何设计蜡烛？任务1：调控配比。"
    missing_complete = judge._check_core_elements_present(text_complete, "PBL")
    assert missing_complete == []


def test_judge_version_is_stable_hash():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from src.judge.judge import Judge

    judge = Judge({"subject": "化学", "grade": "初中"})
    v1 = judge._judge_version()
    v2 = judge._judge_version()
    assert v1 == v2
    assert len(v1) == 12
