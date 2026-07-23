"""LLM-as-Judge 教案评分模块。

对外接口：
    from src.judge import Judge
    report = Judge(profile).evaluate(lesson_text, lesson_type="常规课")

【输出schema的兼容性约定】——团队接口契约的一部分
Judge.evaluate() 返回的 dict 只承诺当前已有的 key（total/dimension_scores/
low_dims/details/manipulation_flags/lesson_type/truncated/judge_version/ROB）
一定存在、类型不变，不承诺"以后不会有新 key"。下游消费方（integrator、
实验脚本、未来可能的 rob_measurer.py）必须按 key 取值，不得假设 dict 是
封闭 schema、不得做"多一个字段就报错"式的严格校验。`ROB` 目前恒为 None，
是团队分工方案方向3（ROB量规优化偏差测量）预留的钩子，后续要填充具体值
或加 delta_rubric / delta_extra 等字段，只需在 judge.py 里追加赋值。

字段命名（如 details 是否要改叫 evidence）是否要对齐其他团队成员的实现，
留给团队接口会议决定，本模块目前只做加法、不做破坏性重命名。
"""

from .gates import check_g1_fidelity, check_g2_degradation
from .judge import Judge, JudgeBudgetExceeded
from .preprocess import detect_manipulation_flags, strip_meta_content

__all__ = [
    "Judge",
    "JudgeBudgetExceeded",
    "strip_meta_content",
    "detect_manipulation_flags",
    "check_g1_fidelity",
    "check_g2_degradation",
]
