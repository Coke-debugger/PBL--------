"""LLM-as-Judge 教案评分主模块。

对外唯一入口：Judge.evaluate(lesson_text, lesson_type) -> dict。

【输出schema的兼容性约定】
evaluate() 返回的 dict 只承诺"当前这几个 key 一定存在、类型不变"，不承诺
"以后不会有新 key"。下游消费方（integrator、实验脚本、未来可能的
rob_measurer.py）必须按 key 取值（如 report["total"]），不得假设 dict 是
封闭 schema、不得做"多一个字段就报错"式的严格校验。这样以后要加 ROB /
delta_rubric 等字段时，只是在本文件里追加赋值，不需要改任何已有消费方
的代码。
"""

from __future__ import annotations

import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional

from .preprocess import detect_manipulation_flags, strip_meta_content
from .prompts import POINT_DIMENSIONS, load_rubric
from .sampling import (
    SAMPLE_WORKERS,
    _normalize,
    aggregate_c_dimension,
    aggregate_point_dimension,
    sample_c_dimension,
    sample_dimension,
    verify_mapping_table,
)

DEFAULT_MODEL_POOL = ["claude-opus-4-8"]

# 采样次数：C/F（权重最高，合计50/100）3次维持根因去重/封顶判断稳定性；B/D 也
# 用 3 次——这两个维度最容易被 flash 的"evidence 判满足却填0"自相矛盾拖累，2
# 次采样时偶发矛盾无第三票稀释会稳稳得0分，3 次更稳（配合 evidence_override 兜底
# 进一步修正）。A/E 这类以结构/语言判定为主的维度 2 次即可。总采样 2+3+3+3+2+3=16。
N_SAMPLES = {"A": 2, "B": 3, "C": 3, "D": 3, "E": 2, "F": 3}

# 进度回调签名：on_progress(dim, done, total, message)。done/total 为已完成的
# 维度数与维度总数（6），message 为面向用户的一行说明。回调为空时无副作用，
# 不影响返回值与计分逻辑。
ProgressCallback = Callable[[str, int, int, str], None]


class JudgeBudgetExceeded(RuntimeError):
    """完整评审超出时间预算。由 run.py 捕获后回退到快速模式，保证分数一定产出。

    选择"主动抛异常"而非"外层 ThreadPoolExecutor 超时"的原因：run_phase 用的是
    future.result(timeout=)，超时只是不再等结果，但 with 块退出时 executor 会
    shutdown(wait=True) 阻塞到正在跑的 LLM 调用真正返回——也就是"超时了却仍要
    等十几分钟、最后还拿不到分"。把时间预算放进 evaluate 内部、在维度之间主动
    中断，才能真停、真回退。
    """

# 长教案截断保护。PBL 教案常带大量表格/数据，3万字并不少见；15000 会切掉
# 超过一半内容，导致 D 的目标-活动映射、F 的素养检核因后半段缺失而采不到样。
# 提到 30000 覆盖绝大多数教案全文。若未来遇到更长的，应改为按维度裁剪相关
# 章节而非简单截断尾部。
MAX_TEXT_LEN = 30000


def _truncate_keep_ends(text: str, limit: int = MAX_TEXT_LEN) -> str:
    """超长教案保留首尾两段，而非只留头部。

    只留头部（旧做法）会砍掉 PBL 教案尾部的"成果交流节"，导致 A 维度的核心件
    校验误判为缺件、触发封顶2.0。这里头部留约70%、尾部留约30%，中间用省略标记
    衔接，让首尾的关键结构件（开头的目标/任务链、结尾的成果交流/评价）都能被
    评审采到。截断点取换行边界，避免切断表格行或句子。
    """
    if len(text) <= limit:
        return text
    head_budget = int(limit * 0.7)
    tail_budget = limit - head_budget
    head = text[:head_budget]
    tail = text[-tail_budget:]
    # 把截断点回退到最近的换行，避免切在表格行/句子中间
    head_cut = head.rfind("\n")
    if head_cut > head_budget // 2:
        head = head[:head_cut]
    tail_cut = tail.find("\n")
    if tail_cut != -1 and tail_cut < tail_budget // 2:
        tail = tail[tail_cut + 1:]
    return head.rstrip() + "\n\n…（中间部分因教案过长已省略，仅保留首尾供评审）…\n\n" + tail.lstrip()

# evidence引用核实率过低时的保守封顶分（疑似模型编造证据，非零分——遵循
# 附录A"人工复核而非程序自判死"的原则，只是压低信任度）。
# 阈值从0.5降到0.3：quote 校验已放宽（容忍 flash 改写引用，见 _quote_matches），
# 仍达不到0.3才说明引用大量对不上，此时才封顶。避免"引用是改写而非逐字"把
# 好教案误压成低分。
LOW_EVIDENCE_TRUST_THRESHOLD = 0.3
LOW_EVIDENCE_TRUST_CAP = 4.0


class Judge:
    def __init__(self, profile: dict, model_pool: list[str] | None = None):
        self.profile = profile
        # model_pool 显式可配置：先传单模型（同模型多次采样代替"多模型家族"），
        # 未来要接入第二个模型家族时，调用方直接传多元素列表即可，本类与
        # sampling.py 均不需要改动。
        self.model_pool = model_pool or list(DEFAULT_MODEL_POOL)
        self.rubric = load_rubric()

    def evaluate(
        self,
        lesson_text: str,
        lesson_type: str = "常规课",
        on_progress: Optional[ProgressCallback] = None,
        time_budget_seconds: Optional[float] = None,
        n_samples_override: Optional[int] = None,
    ) -> dict:
        manipulation_flags = detect_manipulation_flags(lesson_text)
        clean_text = strip_meta_content(lesson_text)

        truncated = len(clean_text) > MAX_TEXT_LEN
        if truncated:
            clean_text = _truncate_keep_ends(clean_text, MAX_TEXT_LEN)

        dimension_scores: dict[str, float] = {}
        details: dict[str, dict] = {}

        # 六维度共 6 个进度点；C 维度采样次数最多放最后报。
        total_dims = 6
        done = 0

        # 每个维度的采样次数：n_samples_override 用于快速模式（每维度 1 次采样，
        # 配合并发几十秒出分）；默认走 N_SAMPLES 的多采样（C/F 3次，其余2次）。
        n_for = lambda dim: max(1, int(n_samples_override)) if n_samples_override else N_SAMPLES[dim]

        # time_budget_seconds：完整评审的时间预算。在每个维度完成后检查——超了就
        # 抛 JudgeBudgetExceeded，让 run.py 回退到快速模式出分。预算 None=不限。
        deadline = (time.monotonic() + time_budget_seconds) if time_budget_seconds else None

        # 6 个维度并发评审：把每一次 LLM 采样调用作为一个顶层 future 直接提交到
        # 同一个线程池（单层，当前 N_SAMPLES 下约16个采样future），按维度分组累计
        # ——某维度的采样全部返回就立即聚合打分并报进度。该平台单次调用 60~120s，
        # 串行6维度要十几分钟；扁平并发后墙钟≈最慢单维度（约1~2分钟），能在预算内
        # 跑完出模型分。
        #
        # 单层而非"维度任务包采样"的嵌套：嵌套会让6个维度任务占满6个worker、各自
        # 又向同一池提交采样子任务，池满拿不到worker → 死锁。扁平提交只有一层future，
        # SAMPLE_WORKERS(=14) 约束瞬时请求数，避免一次打出全部采样撞429。
        from .sampling import _parse_json_object, _call_model
        from .prompts import build_c_dimension_prompt, build_point_dimension_prompt

        # 为每个维度预构造 prompt，避免在多个采样线程里重复构造。
        point_prompts = {
            dim: build_point_dimension_prompt(dim, clean_text, self.profile, lesson_type)
            for dim in POINT_DIMENSIONS
        }
        c_prompt = build_c_dimension_prompt(clean_text, self.profile)
        c_n = n_for("C")

        # 混合模型池：池首为快速模型（如 flash），池尾为精确模型（如 pro）。
        # C（内容准确性，要判知识对错）和 F（权重30的核心维度）用 pro 保精度；
        # A/B/D/E 用 flash 提速。单元素池时退化为全用该模型，向后兼容。
        # 这是评审耗时的大头杠杆：pro 单次 60~120s，flash 约 15~30s，A/B/D/E
        # 占 12 次调用，换 flash 后墙钟从~6分钟压到~2分钟。
        def _model_for(dim: str) -> str:
            if len(self.model_pool) == 1:
                return self.model_pool[0]
            return self.model_pool[-1] if dim in ("C", "F") else self.model_pool[0]

        def _point_sample(dim: str, i: int):
            model = _model_for(dim)
            raw = _call_model(point_prompts[dim], model=model)
            try:
                return _parse_json_object(raw)
            except (ValueError, json.JSONDecodeError):
                return None

        def _c_sample(i: int):
            model = _model_for("C")
            # C 维度温度 0.0：与其他维度一致，保证评审可复现（见 sampling._call_model）。
            raw = _call_model(c_prompt, model=model, temperature=0.0)
            try:
                return _parse_json_object(raw)
            except (ValueError, json.JSONDecodeError):
                return None

        # dim_for_future：future → 它属于哪个维度；samples_for[dim] 收该维度已返回的采样。
        # completed_for[dim] 计该维度已完成的 future 数（含解析失败的），到 expected
        # 就触发聚合——必须用 future 完成数而非解析成功数判断，否则某维度若有 1 次
        # JSON 解析失败，有效样本数永远到不了 expected，聚合永不触发，该维度缺失导致
        # 最后算总分时 KeyError，且预算检查点也卡在聚合处永不生效（一直阻塞到全跑完）。
        samples_for: dict[str, list] = {dim: [] for dim in list(POINT_DIMENSIONS) + ["C"]}
        completed_for: dict[str, int] = {dim: 0 for dim in list(POINT_DIMENSIONS) + ["C"]}
        expected_for: dict[str, int] = {dim: n_for(dim) for dim in POINT_DIMENSIONS}
        expected_for["C"] = c_n

        pool = ThreadPoolExecutor(max_workers=SAMPLE_WORKERS)
        try:
            futures: dict = {}
            for dim in POINT_DIMENSIONS:
                for i in range(n_for(dim)):
                    futures[pool.submit(_point_sample, dim, i)] = dim
            for i in range(c_n):
                futures[pool.submit(_c_sample, i)] = "C"

            budget_hit = False
            low_confidence_dims: list[str] = []  # 有效样本不足的维度，分值不可靠
            for future in as_completed(futures):
                dim = futures[future]
                parsed = future.result()
                if parsed is not None:
                    samples_for[dim].append(parsed)
                completed_for[dim] += 1
                # 该维度采样全部完成（含失败）→ 聚合打分报进度
                if completed_for[dim] >= expected_for[dim] and dim not in dimension_scores:
                    valid_n = len(samples_for[dim])
                    if valid_n == 0:
                        # 采样全解析失败：不该直接判 0 分（那是把"评审系统故障"
                        # 当成"教案最差"）。改给保守中性分 2.5 并标记低置信，
                        # 让 UI/下游知道这一维没真正评上，总分也不被 0 拖垮。
                        dimension_scores[dim] = 2.5
                        details[dim] = {
                            "dimension": dim,
                            "n_valid_samples": 0,
                            "low_confidence": True,
                            "note": "采样全部解析失败，已用保守中性分代替0分，该维度分值不可靠",
                        }
                        low_confidence_dims.append(dim)
                    elif dim == "C":
                        agg = aggregate_c_dimension(samples_for[dim], n_samples=c_n)
                        dimension_scores["C"] = agg["score"]
                        details["C"] = agg
                        if valid_n < max(1, c_n // 2):
                            low_confidence_dims.append("C")
                            agg["low_confidence"] = True
                    else:
                        agg = aggregate_point_dimension(dim, samples_for[dim], clean_text)
                        score = self._score_point_dimension(dim, agg, clean_text, lesson_type)
                        dimension_scores[dim] = score
                        details[dim] = agg
                        if valid_n < max(1, expected_for[dim] // 2):
                            low_confidence_dims.append(dim)
                            agg["low_confidence"] = True
                    done += 1
                    if on_progress is not None:
                        suffix = "（低置信）" if dim in low_confidence_dims else ""
                        on_progress(dim, done, total_dims, f"已评审 {done}/{total_dims} 个维度（{dim} 完成{suffix}）")
                    if deadline is not None and time.monotonic() > deadline:
                        budget_hit = True
                        break
            if budget_hit:
                # 超预算：立即停止，不等剩余在途采样。cancel_futures 取消尚未开始的
                # 任务，wait=False 不阻塞已在跑的 HTTP 请求——让进程能马上进入回退/兜底。
                pool.shutdown(wait=False, cancel_futures=True)
                raise JudgeBudgetExceeded(
                    f"完整评审超 {time_budget_seconds:.0f}s 预算（已完成 {done}/{total_dims} 维度）"
                )
        finally:
            pool.shutdown(wait=False)

        if "mapping_table" in details.get("D", {}):
            verified = verify_mapping_table(details["D"]["mapping_table"], clean_text)
            details["D"]["mapping_table"] = verified
            valid_count = sum(1 for m in verified if m.get("valid"))
            details["D"]["mapping_validity_ratio"] = (
                valid_count / len(verified) if verified else None
            )

        weights = self.rubric["weights"]
        total = sum(dimension_scores[d] / 5 * weights[d] for d in weights)
        low_dims = sorted(dimension_scores, key=dimension_scores.get)[:2]

        # judge_mode：标注本次评审用的采样档位。full=附录A多采样（默认），
        # fast=单采样快速模式（完整评审超预算回退时使用，分值波动略大但一定能出分）。
        judge_mode = "fast" if n_samples_override else "full"

        return {
            "total": round(total, 2),
            "dimension_scores": {d: round(v, 2) for d, v in dimension_scores.items()},
            "low_dims": low_dims,
            "low_confidence_dims": low_confidence_dims,  # 有效样本不足的维度，分值不可靠
            "details": details,
            "manipulation_flags": manipulation_flags,
            "lesson_type": lesson_type,
            "truncated": truncated,
            "judge_version": self._judge_version(),
            "judge_mode": judge_mode,
            "ROB": None,  # 占位字段：团队分工方案方向3（ROB量规优化偏差测量）预留钩子，暂不实现
        }

    def _score_point_dimension(self, dim: str, agg: dict, clean_text: str, lesson_type: str) -> float:
        dim_info = self.rubric["dimensions"][dim]
        max_points = dim_info["max_points"]
        sub_scores = agg["sub_indicator_scores"]
        raw_score = 5 * sum(sub_scores.values()) / max_points

        if dim == "A":
            a1 = sub_scores.get("A1", 2)
            missing_core = self._check_core_elements_present(clean_text, lesson_type)
            if missing_core:
                agg["a_core_check_override"] = True
                agg["a_core_missing"] = missing_core
                a1 = min(a1, 1)  # 关键词交叉校验发现核心件查无实据，强制降级触发封顶
            if a1 < 2:
                raw_score = min(raw_score, 2.0)  # 缺任一核心件 → 封顶2.0

        if dim == "F":
            f1_evidence = agg.get("f1_goal_evidence", [])
            if f1_evidence:
                no_evidence_ratio = sum(1 for e in f1_evidence if not e.get("has_evidence")) / len(f1_evidence)
                if no_evidence_ratio >= 1.0:
                    raw_score = min(raw_score, 2.0)
                elif no_evidence_ratio > 1 / 3:
                    raw_score = min(raw_score, 3.0)

        verified_ratio = agg.get("evidence_verified_ratio", 1.0)
        if verified_ratio < LOW_EVIDENCE_TRUST_THRESHOLD:
            agg["low_evidence_trust"] = True
            raw_score = min(raw_score, LOW_EVIDENCE_TRUST_CAP)

        return max(0.0, min(5.0, raw_score))

    def _check_core_elements_present(self, lesson_text: str, lesson_type: str) -> list[str]:
        """A维度核心结构件的关键词交叉校验（安全网，非结构解析）。

        用途：防止LLM在A1子指标里幻觉"核心件齐全"，但实际原文里一个同义词
        都命中不了的情况。只做子串匹配，命中任一同义词即算存在——这是廉价
        安全网，不是取代'结构抽取以LLM为主'的量规要求。
        """
        course_key = "PBL" if lesson_type == "PBL" else "常规课"
        structure = self.rubric.get("course_type_structure", {}).get(course_key, {})
        synonyms = structure.get("synonyms", {})
        normalized_text = _normalize(lesson_text)

        missing = []
        for core_name, syn_list in synonyms.items():
            if not any(_normalize(s) in normalized_text for s in syn_list):
                missing.append(core_name)
        return missing

    def _judge_version(self) -> str:
        """rubric内容的哈希，对齐附录A'评审器冻结'要求——rubric变了这个值就变，
        方便发现'用了不同版本评审器却混进同一张分数表'的问题。"""
        payload = json.dumps(self.rubric, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
