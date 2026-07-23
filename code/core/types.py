"""core/types.py — 全部 TypedDict 集中定义，其他文件统一 from core.types import *"""
from __future__ import annotations
from typing import TypedDict, List, Dict, Optional, Literal


class Profile(TypedDict):
    subject: str
    grade: str
    prior_knowledge: str
    learning_motivation: str
    target_openness_tier: int   # 1-4，默认 2


class Annotation(TypedDict):
    issue_id: str
    dimension: str              # "A"|"B"|"C"|"D"|"E"|"F"
    severity: str               # "major"|"minor"
    location: str
    quote: str
    problem: str
    suggestion: str
    in_scope: bool
    refer_to: Optional[str]
    rubric_anchor: Optional[str]


class Review(TypedDict):
    refers_to: str
    stance: str
    content: str
    round: int
    role_id: str
    in_scope: bool
    refer_to: Optional[str]


class StructuredInstruction(TypedDict):
    target_quote: str
    replacement_text: str
    section_fallback: str


class Decision(TypedDict):
    issue_id: str
    decision: str               # "adopted"|"partial"|"rejected"
    rationale: str
    instruction: Optional[StructuredInstruction]


class Verdict(TypedDict):
    adopted: List[Decision]
    partial: List[Decision]
    rejected: List[Decision]
    surviving_args: List[str]


class Modification(TypedDict):
    mod_id: str
    location: str
    before_summary: str
    after_summary: str
    source_role: str
    rationale: str
    quote_located: bool


class DimEvidence(TypedDict):
    sub_indicator: str
    status: str                 # "满足"|"部分满足"|"不满足"
    evidence_quote: str
    reasoning: str
    quote_verified: bool


class ScoreReport(TypedDict):
    total: float
    dimension_scores: Dict[str, float]
    low_dims: List[str]
    evidence: Dict[str, List[DimEvidence]]
    judge_version: str
    ROB: Optional[float]


class PipelineConfig(TypedDict, total=False):
    enable_round0: bool
    enable_round1: bool
    enable_argument: bool
    enable_chair: bool
    enable_judge: bool
    enable_refine: bool
    enable_monitor: bool
    enable_experience: bool
    enable_bias_audit: bool
    enable_shapley: bool
    active_roles: List[str]
    judge_threshold: float
    timeouts: Dict[str, int]


class RunTrace(TypedDict, total=False):
    call_id: str
    step: str
    model: str
    prompt_hash: str
    latency_ms: int
    input_tokens: int
    output_tokens: int
    parse_ok: bool
    retries: int
