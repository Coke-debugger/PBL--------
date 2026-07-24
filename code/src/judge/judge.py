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
from pathlib import Path
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

# 采样次数：极限提速档——全部 2 次。实测 B/D 的第3次采样(S3补提)稳定卡2.5-3分钟
# 长尾，是评审6分钟的真凶。降到2次彻底不补提，杜绝长尾。稳定性靠温度0.0 + W3离群
# 剔除 + evidence_override 兜底。总采样 2*6=12。
N_SAMPLES = {"A": 2, "B": 2, "C": 2, "D": 2, "E": 2, "F": 2}

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

    # 评审结果缓存：同一份教案+学情+课型+评审器版本+模型池，第二次评直接出缓存，
    # 不再调模型。S5提速方案——零风险，只对"重复评同一份"有效（前后对比页评磨课前
    # 教案、多次跑同一教案时生效）。缓存文件存在项目根 judge_cache/ 下。
    _CACHE_DIR = Path(__file__).resolve().parents[3] / "judge_cache"
    _CACHE_ENABLED = True  # 可在调用方设 False 关闭（如校准测试要强制重评）

    def _cache_key(self, lesson_text: str, lesson_type: str, judge_mode: str) -> str:
        payload = json.dumps({
            "text": lesson_text, "profile": self.profile, "lesson_type": lesson_type,
            "judge_version": self._judge_version(), "judge_mode": judge_mode,
            "model_pool": self.model_pool,
            # 评分策略版本：改模型分配/采样数等策略时递增，自动失效旧缓存
            "strategy_v": 2,
        }, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]

    def _cache_path(self, key: str) -> Path:
        return self._CACHE_DIR / f"{key}.json"

    def _cache_get(self, key: str) -> dict | None:
        if not self._CACHE_ENABLED:
            return None
        p = self._cache_path(key)
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                return None
        return None

    def _cache_put(self, key: str, report: dict) -> None:
        if not self._CACHE_ENABLED:
            return
        try:
            self._CACHE_DIR.mkdir(parents=True, exist_ok=True)
            self._cache_path(key).write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            pass  # 缓存写入失败不影响评审

    def evaluate(
        self,
        lesson_text: str,
        lesson_type: str = "常规课",
        on_progress: Optional[ProgressCallback] = None,
        time_budget_seconds: Optional[float] = None,
        n_samples_override: Optional[int] = None,
    ) -> dict:
        # S5 缓存：按教案+学情+课型+评审器版本+模型池 哈希命中则直接返回，不调模型。
        # judge_mode 也纳入 key（full/fast/fallback 评分口径不同，不能混用缓存）。
        judge_mode_key = "fast" if n_samples_override else "full"
        cache_key = self._cache_key(lesson_text, lesson_type, judge_mode_key)
        cached = self._cache_get(cache_key)
        if cached is not None:
            if on_progress is not None:
                on_progress("-", 6, 6, "命中评审缓存，直接返回历史评分")
            return cached

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

        # S1 按维度裁剪：A 维度只判结构完整性（标题树+各节实质性），不需要全文。
        # 压成"每个标题 + 该节前200字"的骨架，3万字降到几千字，A 的2次flash调用
        # 输入大减、提速。其余维度仍用全文（C要找散落各处的知识错误、D/F要看目标-
        # 活动对应，裁剪会漏证据，不动）。A 的 _check_core_elements_present 关键词
        # 校验仍用全文 clean_text，安全网不受影响。
        text_for_dim = {dim: clean_text for dim in POINT_DIMENSIONS}
        text_for_dim["A"] = self._skeleton_for_a(clean_text)

        # 为每个维度预构造 prompt，避免在多个采样线程里重复构造。
        point_prompts = {
            dim: build_point_dimension_prompt(dim, text_for_dim[dim], self.profile, lesson_type)
            for dim in POINT_DIMENSIONS
        }
        c_prompt = build_c_dimension_prompt(clean_text, self.profile)
        c_n = n_for("C")

        # 极限提速档：所有维度都用 flash（pool[0]），不再让 C/F 用 pro。
        # pro 单次 60~120s 是墙钟大头，全换 flash(15~30s) 后墙钟砍半以上。
        # 代价：C 知识准确性可能波动变大，靠温度0.0 + 多采样多数票 + W3离群剔除
        # + evidence_override 兜底压波动。若 C 误判增多，恢复 _model_for 让 C/F 用
        # pool[-1]（pro）即可。
        def _model_for(dim: str) -> str:
            return self.model_pool[0]

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
            # S3 早一致即停：分两波提交。第一波每维度只提交 min(2,n) 个采样，等第一波
            # 返回后，对 n>=3 的维度检查前2次是否一致（sub_indicator_scores 完全相同），
            # 一致则跳过第3次（省1次pro调用），不一致才补提第3次。温度0.0下同一维度
            # 2次一致是稳定信号，第3次大概率重复，省掉它提速。
            def _dim_consistent(dim: str, samples: list) -> bool:
                """该维度前2次有效采样的子指标分是否完全一致。"""
                valid = [s for s in samples if s is not None]
                if len(valid) < 2:
                    return False
                if dim == "C":
                    # C 用根因清单比较：两次的 root_cause 集合一致即视为一致
                    rc0 = tuple(sorted(str(it.get("root_cause", "")) for it in (valid[0].get("issues") or [])))
                    rc1 = tuple(sorted(str(it.get("root_cause", "")) for it in (valid[1].get("issues") or [])))
                    return rc0 == rc1
                s0 = valid[0].get("sub_indicator_scores", {})
                s1 = valid[1].get("sub_indicator_scores", {})
                return s0 == s1

            futures: dict = {}
            wave1_futures: dict = {}
            # 第一波：每维度 min(2, n) 个
            for dim in POINT_DIMENSIONS:
                for i in range(min(2, n_for(dim))):
                    f = pool.submit(_point_sample, dim, i)
                    wave1_futures[f] = dim
            for i in range(min(2, c_n)):
                f = pool.submit(_c_sample, i)
                wave1_futures[f] = "C"

            # 等第一波完成，收样本，决定第二波
            for future in as_completed(wave1_futures):
                dim = wave1_futures[future]
                parsed = future.result()
                if parsed is not None:
                    samples_for[dim].append(parsed)
                completed_for[dim] += 1

            # 第二波：n>=3 且前2次不一致的维度，补提第3个；一致的跳过（expected 降为2）
            skip3: set[str] = set()
            for dim in list(POINT_DIMENSIONS) + ["C"]:
                n_total = c_n if dim == "C" else n_for(dim)
                if n_total >= 3 and _dim_consistent(dim, samples_for[dim]):
                    skip3.add(dim)
                    expected_for[dim] = 2  # 跳过第3次，按2次聚合
            for dim in POINT_DIMENSIONS:
                if dim in skip3:
                    continue
                if n_for(dim) >= 3:
                    futures[pool.submit(_point_sample, dim, 2)] = dim
            if c_n >= 3 and "C" not in skip3:
                futures[pool.submit(_c_sample, 2)] = "C"

            budget_hit = False
            low_confidence_dims: list[str] = []  # 有效样本不足的维度，分值不可靠

            def _aggregate_dim(dim: str) -> None:
                """对已完成全部采样的维度聚合打分、报进度。供主循环与skip3即时聚合复用。"""
                if dim in dimension_scores:
                    return
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
                nonlocal done
                done += 1
                if on_progress is not None:
                    suffix = "（低置信）" if dim in low_confidence_dims else ""
                    on_progress(dim, done, total_dims, f"已评审 {done}/{total_dims} 个维度（{dim} 完成{suffix}）")

            # S3: skip3 的维度不进第二波，第一波已完成2次采样，这里即时聚合，不等主循环。
            # 同时 n_for=2 的维度（A/E）第一波就采满2次、不进第二波，也须在此即时聚合，
            # 否则主循环 as_completed 里没有它们的 future，永不触发聚合 → 该维度缺失。
            for dim in list(POINT_DIMENSIONS) + ["C"]:
                if dim in dimension_scores:
                    continue
                if completed_for[dim] >= expected_for[dim]:
                    _aggregate_dim(dim)

            for future in as_completed(futures):
                dim = futures[future]
                parsed = future.result()
                if parsed is not None:
                    samples_for[dim].append(parsed)
                completed_for[dim] += 1
                # 该维度采样全部完成（含失败）→ 聚合打分报进度
                if completed_for[dim] >= expected_for[dim] and dim not in dimension_scores:
                    _aggregate_dim(dim)
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

        report = {
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
        # S5 缓存写入（命中 key 与开头检查一致）
        self._cache_put(cache_key, report)
        return report

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

    @staticmethod
    def _skeleton_for_a(text: str, per_section_chars: int = 200) -> str:
        """把教案压成 A 维度评审用的结构骨架：保留每个标题 + 该节前若干字。

        A 维度只判结构完整性（核心件齐全/非核心件齐全/结构件实质性），不需要读全
        活动细节——标题树 + 每节首段足够判断"有无此件""是否空壳"。3万字教案压到
        几千字，A 的采样调用输入大减、提速。实质性判断需要的"本课特定实体"在前200
        字内通常已出现（活动名、数据、例题），200字够用。
        """
        import re
        lines = text.split("\n")
        out: list[str] = []
        current_body: list[str] = []
        # 先收集每个标题块
        blocks: list[tuple[str, str]] = []  # (heading_line, body)
        for line in lines:
            if re.match(r"^#{1,6}\s+", line):
                if blocks or current_body:
                    blocks.append(("".join(current_body)))
                current_body = [line + "\n"]
            else:
                current_body.append(line + "\n")
        if current_body:
            blocks.append("".join(current_body))
        # 每块取标题 + 前 per_section_chars 字
        for blk in blocks:
            head_end = blk.find("\n")
            heading = blk[:head_end] if head_end != -1 else blk
            body = blk[head_end + 1:] if head_end != -1 else ""
            body = body.strip()
            if body:
                body = body[:per_section_chars] + ("…" if len(body) > per_section_chars else "")
            out.append(f"{heading}\n{body}" if body else heading)
        return "\n\n".join(out)
