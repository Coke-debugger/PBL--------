"""run.py — 统一 CLI 入口（Phase 1 + Phase 2）
Phase 1: 4专家并行批注直出（enable_round1=false）
Phase 2: 互评 + 冲突分类 + 仲裁 + Judge（enable_round1=true）
"""
from __future__ import annotations
import argparse
import concurrent.futures
import json
import logging
import os
import sys
import time
from pathlib import Path

import yaml
from core.preprocessor import Preprocessor, LaTeXError
from core.roundtable   import Roundtable
from core.integrator   import Integrator
from core.validate_submission import run as validate
from core.types import PipelineConfig

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


def emit_progress(phase: str, status: str, message: str) -> None:
    """向可视化界面发送阶段进度；命令行运行时只是普通日志行。"""
    payload = {"event": "phase_status", "phase": phase, "status": status, "message": message}
    print("WORKSHOP_EVENT " + json.dumps(payload, ensure_ascii=False), flush=True)


def _build_judge_pool(config: dict) -> list[str]:
    """构建 [flash(快), pro(精)] 混合评审模型池。与旧 Step6 逻辑一致，抽出复用。"""
    _api_cfg_path = Path("configs/api.yaml")
    _flash_model = os.environ.get("USTC_LLM_MODEL")
    if not _flash_model and _api_cfg_path.exists():
        _flash_model = yaml.safe_load(_api_cfg_path.read_text(encoding="utf-8")).get("model")
    _flash_model = _flash_model or config.get("judge_fast_model", "deepseek-v4-flash-ascend")
    judge_model = os.environ.get("USTC_JUDGE_MODEL") or config.get("judge_model", "deepseek-v4-pro")
    return [_flash_model, judge_model] if _flash_model != judge_model else [judge_model]


def _run_judge(
    judge, text: str, lesson_type: str, timeouts: dict,
    n_samples_override: int | None = None, label: str = "Judge",
) -> dict | None:
    """跑一次 Judge 评审，含完整→快速→兜底三级降级，保证一定出分。

    n_samples_override=None 走完整多采样（出最终分用）；=1 走快速模式（首轮轻量
    评原教案拿 low_dims+issues 用）。兜底（fallback）仅在完整+快速都失败时启用，
    轻量首轮不兜底（拿不到真实 issues 时直接返回 None，由调用方决定回退路径）。
    """
    from src.judge import JudgeBudgetExceeded
    full_budget = timeouts.get("judge_full", 360)
    try:
        return judge.evaluate(
            text, lesson_type=lesson_type,
            on_progress=lambda d, done, total, msg: emit_progress("judge", "running", msg),
            time_budget_seconds=full_budget,
            n_samples_override=n_samples_override,
        )
    except JudgeBudgetExceeded as budget_err:
        if n_samples_override is not None:
            # 已是快速模式还超时：轻量首轮返回 None，最终评审走兜底
            logger.warning(f"  [{label}] 快速评审仍超时: {budget_err}")
            return None
        logger.warning(f"  [{label}] 完整评审超时，回退快速模式: {budget_err}")
        emit_progress("judge", "running", f"[{label}] 完整评审超时，回退快速模式…")
        return judge.evaluate(
            text, lesson_type=lesson_type,
            on_progress=lambda d, done, total, msg: emit_progress("judge", "running", msg),
            time_budget_seconds=timeouts.get("judge_fast", 240),
            n_samples_override=1,
        )


def _judge_with_fallback(
    judge, text: str, lesson_type: str, timeouts: dict,
    profile: dict, n_modifications: int, label: str = "Judge",
) -> dict | None:
    """_run_judge 的兜底包装：连快速模式都失败时用规则兜底出分。仅最终评审用。"""
    report = _run_judge(judge, text, lesson_type, timeouts, n_samples_override=None, label=label)
    if report is not None:
        return report
    logger.warning(f"  [{label}] 模型评审失败，启用规则兜底评分")
    emit_progress("judge", "running", f"[{label}] 模型评审失败，启用规则兜底评分…")
    try:
        from core.judge_fallback import evaluate_fallback
        return evaluate_fallback(text, lesson_type=lesson_type, profile=profile, n_modifications=n_modifications)
    except Exception as fe:
        logger.error(f"  [{label}] 兜底评分也失败: {fe}")
        emit_progress("judge", "failed", f"评分彻底失败：{fe}")
        return None


def parse_args():
    p = argparse.ArgumentParser(description="多智能体教案磨课系统 (Phase 1+2)")
    p.add_argument("--lesson",      required=True)
    p.add_argument("--profile",     required=True)
    p.add_argument("--out",         required=True)
    p.add_argument("--student-id",  default="STU001")
    p.add_argument("--sample-id",   default="SAMPLE01")
    p.add_argument("--config",      default="configs/pipeline.yaml")
    p.add_argument("--topology",    default="configs/topology.yaml")
    p.add_argument("--no-judge",    action="store_true")
    return p.parse_args()


def load_config(path: str) -> PipelineConfig:
    p = Path(path)
    if p.exists():
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
        return data.get("pipeline", data)
    return {}


def run_phase(name: str, fn, timeout: int = 120):
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        future = ex.submit(fn)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            logger.warning(f"[{name}] 超时（{timeout}s），跳过")
            return None
        except Exception as e:
            logger.error(f"[{name}] 异常: {e}")
            return None


def main() -> int:
    args    = parse_args()
    config  = load_config(args.config)
    timeouts = config.get("timeouts", {})

    is_phase2 = config.get("enable_round1", False)
    logger.info(f"=== 磨课系统启动（{'Phase 2' if is_phase2 else 'Phase 1'}）===")

    # ── Step 0: 经验检索 ──────────────────────────────────────────────
    experiences = []
    if config.get("enable_experience", False):
        try:
            from modules.experience_bank import ExperienceBank
            lesson_text_tmp = Path(args.lesson).read_text(encoding="utf-8")
            profile_tmp     = yaml.safe_load(Path(args.profile).read_text(encoding="utf-8"))
            experiences = ExperienceBank().retrieve(profile_tmp, lesson_text_tmp)
            logger.info(f"  经验检索: {len(experiences)} 条")
        except Exception as e:
            logger.warning(f"  经验检索失败（跳过）: {e}")

    # ── Step 1: 预处理 ────────────────────────────────────────────────
    logger.info("=== Step 1 预处理 ===")
    try:
        prep = Preprocessor(args.lesson, args.profile,
                             args.student_id, args.sample_id)
        lesson_data = prep.parse()
    except LaTeXError as e:
        logger.error(f"LaTeX 预检失败: {e}")
        return 1
    except FileNotFoundError as e:
        logger.error(f"文件不存在: {e}")
        return 1

    logger.info(
        f"  学科={lesson_data['profile'].get('subject','')} "
        f"年级={lesson_data['profile'].get('grade','')} "
        f"课型={lesson_data['structure']['course_type']}"
    )

    # ── Step 2-4: 圆桌研讨（按 Phase 自动路由）─────────────────────────
    logger.info(f"=== Step 2{'~4' if is_phase2 else ''} 圆桌研讨 ===")
    roundtable = Roundtable(lesson_data, config)
    rt_result = run_phase(
        "Roundtable",
        lambda: roundtable.run(experiences),
        timeout=sum(timeouts.get(k, 0) for k in ["round0","round1","chair"]) or 300,
    )
    if rt_result is None:
        rt_result = {}

    round0  = rt_result.get("round0", {})
    round1  = rt_result.get("round1", {})
    verdict = rt_result.get("verdict", {})

    total_anns = sum(len(v) for v in round0.values())
    logger.info(f"  批注总数：{total_anns} 条（{list(round0.keys())}）")

    # ── Step 5: 整合修改 ─────────────────────────────────────────────
    logger.info("=== Step 5 整合修改 ===")
    emit_progress("integrate", "running", "正在整合修改建议…")

    # 磨课前分数（Phase 1 轻量评原教案得到），供 Step9 经验库算 delta；未走该路径时为 None。
    initial_report = None
    # Phase 2: 使用 verdict 的结构化指令精确替换（保留原路径）
    if is_phase2 and verdict.get("adopted"):
        from core.integrator_phase2 import IntegratorPhase2
        integrator = IntegratorPhase2(lesson_data, verdict,
                                       args.student_id, args.sample_id)
        result = run_phase("Integrate",
                           lambda: integrator.integrate(),
                           timeout=timeouts.get("integrate", 30))
        if result is None:
            logger.error("整合超时，退出")
            return 1
        draft_text, modifications = result
    else:
        # Phase 1 重构路径：Judge 先行评原教案 → 章节切分 → 分段聚焦改写。
        # 不再用"4专家通读全文批注直整合"（幻觉重、多专家改同章节打架），改为
        # "Judge 指出低分维度+具体issue → 专家只改issue所在章节 → 按段落替换"。
        from core.integrator import Integrator
        from core.section_splitter import split_into_sections
        integrator = Integrator(lesson_data, round0,
                                args.student_id, args.sample_id)
        original_text = lesson_data["text"]
        modifications: list = []

        if config.get("enable_judge", False) and not args.no_judge:
            from src.judge import Judge
            lesson_type = lesson_data["structure"].get("course_type", "常规课")
            judge = Judge(lesson_data["profile"], model_pool=_build_judge_pool(config))

            # Step 5a: 轻量评审原教案（快速模式 n_samples=1），拿 low_dims + issues
            emit_progress("judge", "running", "正在轻量评审原教案，定位待改进的维度与问题…")
            logger.info("=== Step 5a 轻量评审原教案（定位问题）===")
            initial_report = _run_judge(
                judge, original_text, lesson_type, timeouts,
                n_samples_override=1, label="首轮轻量",
            )
            if initial_report is None:
                # 轻量评审也失败：回退到旧的批注直整合路径，保证有产出
                logger.warning("  首轮轻量评审失败，回退批注直整合路径")
                result = run_phase("Integrate",
                                   lambda: integrator.integrate(),
                                   timeout=timeouts.get("integrate", 30))
                if result is None:
                    logger.error("整合超时，退出")
                    return 1
                draft_text, modifications = result
            else:
                logger.info(
                    f"  原教案轻量分: {initial_report['total']:.1f}/100，"
                    f"低分维度: {initial_report.get('low_dims')}"
                )
                sections = split_into_sections(original_text)
                api_cfg = {}
                _api_p = Path("configs/api.yaml")
                if _api_p.exists():
                    api_cfg = yaml.safe_load(_api_p.read_text(encoding="utf-8"))

                # Step 5b: 多轮分段聚焦改写，取最高分。
                # 单次 LLM 改写不可靠，靠"多轮带随机性 + 选优"保证质量，而非压随机性。
                # 每轮：focused_revise(temp 0.3 产生多样性) → 轻量复评(n=1)拿该轮分；
                # 取分数最高那轮的 polished 作为 draft_text，Step6 再对它做一次完整评审。
                n_rounds = max(1, int(config.get("n_rounds", 1)))
                logger.info(f"=== Step 5b 分段聚焦改写（{n_rounds} 轮取最好）===")
                best = {"score": -1.0, "draft": original_text, "mods": []}
                for r in range(1, n_rounds + 1):
                    emit_progress(
                        "integrate", "running",
                        f"第 {r}/{n_rounds} 轮分段聚焦改写（专家据评审意见改写相关章节）…",
                    )
                    r_draft, r_mods = integrator.focused_revise(
                        original_text, initial_report, sections, api_cfg=api_cfg,
                    )
                    if not r_mods:
                        logger.info(f"  第{r}轮未产出修改，跳过")
                        continue
                    # 轻量复评该轮改后教案，拿分数比较（n=1 快速，省成本）
                    r_report = _run_judge(
                        judge, r_draft, lesson_type, timeouts,
                        n_samples_override=1, label=f"第{r}轮复评",
                    )
                    r_score = r_report["total"] if r_report else -1.0
                    logger.info(f"  第{r}轮分: {r_score:.1f}/100（修改 {len(r_mods)} 条）")
                    if r_score > best["score"]:
                        best = {"score": r_score, "draft": r_draft, "mods": r_mods}
                        emit_progress("integrate", "running", f"第{r}轮暂为最优：{r_score:.1f}/100")
                draft_text, modifications = best["draft"], best["mods"]
                if not modifications:
                    logger.warning("  多轮分段聚焦均未产出修改，使用原教案")
                    draft_text = original_text
                else:
                    logger.info(
                        f"  选优完成：最优轮 {best['score']:.1f}/100，"
                        f"采纳 {len(modifications)} 条修改"
                    )
        else:
            # 未启用 Judge：回退旧的批注直整合
            result = run_phase("Integrate",
                               lambda: integrator.integrate(),
                               timeout=timeouts.get("integrate", 30))
            if result is None:
                logger.error("整合超时，退出")
                return 1
            draft_text, modifications = result

    emit_progress("integrate", "completed", f"整合完成，共形成 {len(modifications)} 条修改")
    logger.info(f"  修改条数: {len(modifications)}")
    insert_fallbacks = getattr(integrator, "insert_fallbacks", [])
    if insert_fallbacks:
        emit_progress(
            "integrate", "running",
            f"其中 {len(insert_fallbacks)} 条建议未能精确定位原文，已按章节插入教案（补写类修改）。",
        )
    if not modifications and not is_phase2:
        logger.warning("  modifications 为空，将使用原教案作为 polished")

    # ── Step 6: Judge 评审（最终评分）──────────────────────────────────
    score_report = None
    if config.get("enable_judge", False) and not args.no_judge:
        logger.info("=== Step 6 Judge 评审（最终）===")
        from src.judge import Judge
        lesson_type = lesson_data["structure"].get("course_type", "常规课")
        judge = Judge(lesson_data["profile"], model_pool=_build_judge_pool(config))
        emit_progress("judge", "running", "正在进行六维度 Judge 最终评分（完整多采样），约需数分钟…")
        score_report = _judge_with_fallback(
            judge, draft_text, lesson_type, timeouts,
            profile=lesson_data.get("profile", {}),
            n_modifications=len(modifications), label="最终",
        )
        if score_report:
            mode_tag = score_report.get("judge_mode", "full")
            logger.info(f"  Judge 总分: {score_report['total']:.1f}/100（{mode_tag}）")
            logger.info(f"  低分维度: {score_report['low_dims']}")
            emit_progress(
                "judge", "completed",
                f"Judge 评分完成：{score_report['total']:.1f}/100"
                f"（{'快速模式' if mode_tag == 'fast' else '完整评审'}）",
            )

    # ── Step 7: 定向二次修订 ─────────────────────────────────────────
    # Phase 1 重构后，分段聚焦改写(focused_revise)已在 Step5b 基于 Judge 意见改完
    # 低分维度，refine 与之重复且会往文末追加污染教案，故 Phase 1 路径下跳过 refine。
    # 仅 Phase 2（批注直整合无 focused_revise）保留 refine 作为兜底补写。
    if (config.get("enable_refine", False) and score_report
            and score_report["total"] < config.get("judge_threshold", 75.0)
            and is_phase2):
        logger.info("=== Step 7 定向二次修订 ===")
        try:
            result2 = run_phase("Refine",
                                 lambda: integrator.refine(draft_text, score_report),
                                 timeout=timeouts.get("refine", 240))
            if result2:
                draft_text, extra_mods = result2
                modifications.extend(extra_mods)
                logger.info(f"  补写后总修改: {len(modifications)}")
        except Exception as e:
            logger.warning(f"  二次修订失败（跳过）: {e}")

    # ── Step 8: 写出文件 ──────────────────────────────────────────────
    logger.info("=== Step 8 写出文件 ===")
    emit_progress("export", "running", "正在保存教案、研讨记录和评分报告…")
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    prefix = f"{args.student_id}_{args.sample_id}"

    polished_path = out_dir / f"{prefix}_polished.md"
    process_path  = out_dir / f"{prefix}_process.json"

    integrator.write_polished(polished_path, draft_text)
    integrator.write_process(process_path, modifications, round0, round1)
    if score_report:
        score_path = out_dir / f"{prefix}_scores.json"
        score_path.write_text(
            json.dumps(score_report, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # 契约校验
    val = validate(str(out_dir))
    for w in val["warnings"]:
        logger.warning(f"  WARN: {w}")
    for f in val["failures"]:
        logger.error(f"  FAIL: {f}")
    if val["has_fail"]:
        return 1
    emit_progress("export", "completed", "结果文件已保存")

    # ── Step 9: 经验库更新 ────────────────────────────────────────────
    if config.get("enable_experience", False) and score_report:
        try:
            from modules.experience_bank import ExperienceBank
            # score_before 用磨课前分数：Phase 1 重构路径有 initial_report（轻量评原教案），
            # 取不到时记 0（经验库内部 delta<=0 不记录，不影响正确性）。
            score_before = initial_report["total"] if initial_report else 0
            ExperienceBank().update(
                args.sample_id,
                json.loads(process_path.read_text(encoding="utf-8")),
                score_before=score_before,
                score_after=score_report["total"],
            )
            logger.info(
                f"  经验库更新：before={score_before:.1f} after={score_report['total']:.1f}"
                f" delta={score_report['total']-score_before:+.1f}"
            )
        except Exception as e:
            logger.warning(f"  经验库更新失败（跳过）: {e}")

    logger.info(f"=== 完成 ===\n  → {polished_path}\n  → {process_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
