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
    # 不用 run_phase 外层超时包整个 roundtable——那会在超时时把已完成的专家批注
    # 全部丢弃（future.result(timeout) 抛异常，run_round0 的 with 块被强退、不 return）。
    # 改为直接调 run，让 run_round0 内部 as_completed(timeout) 逐个收：慢的专家超时
    # 单独失败，已返回的批注保留。
    try:
        rt_result = roundtable.run(experiences)
    except Exception as e:
        logger.error(f"圆桌研讨异常: {e}")
        rt_result = None
    if rt_result is None:
        rt_result = {}

    round0  = rt_result.get("round0", {})
    round1  = rt_result.get("round1", {})
    verdict = rt_result.get("verdict", {})

    total_anns = sum(len(v) for v in round0.values())
    logger.info(f"  批注总数：{total_anns} 条（{list(round0.keys())}）")

    # ── Step 5: 整合 ─────────────────────────────────────────────────
    logger.info("=== Step 5 整合修改 ===")
    emit_progress("integrate", "running", "专家研讨已结束，正在整合修改建议…")

    # Phase 2: 使用 verdict 的结构化指令；Phase 1: 降级为批注直整合
    if is_phase2 and verdict.get("adopted"):
        from core.integrator_phase2 import IntegratorPhase2
        integrator = IntegratorPhase2(lesson_data, verdict,
                                       args.student_id, args.sample_id)
    else:
        integrator = Integrator(lesson_data, round0,
                                args.student_id, args.sample_id)

    result = run_phase("Integrate",
                       lambda: integrator.integrate(),
                       timeout=timeouts.get("integrate", 30))
    if result is None:
        logger.error("整合超时，退出")
        return 1

    draft_text, modifications = result
    emit_progress("integrate", "completed", f"整合完成，共形成 {len(modifications)} 条修改")
    logger.info(f"  修改条数: {len(modifications)}")
    # 落实度提示：有多少条批注因 quote 定位失败而改为按章节插入（已写入教案，非静默丢失）。
    insert_fallbacks = getattr(integrator, "insert_fallbacks", [])
    if insert_fallbacks:
        emit_progress(
            "integrate", "running",
            f"其中 {len(insert_fallbacks)} 条建议未能精确定位原文，已按章节插入教案（补写类修改）。",
        )
        logger.info(f"  按章节插入的补写类修改: {len(insert_fallbacks)} 条")
    if not modifications:
        logger.error("modifications 为空，退出")
        return 1

    # ── Step 6: Judge 评审（Phase 2）──────────────────────────────────
    score_report = None
    if config.get("enable_judge", False) and not args.no_judge:
        logger.info("=== Step 6 Judge 评审 ===")
        try:
            from src.judge import Judge, JudgeBudgetExceeded
            # Judge 评分模型池：[flash(快), pro(精)]。judge.py 让 C/F 用 pro、
            # A/B/D/E 用 flash。flash 单次约 15~30s（pro 60~120s），A/B/D/E 共
            # 12 次调用换 flash 后评审墙钟从~6分钟压到~2分钟，且关键维度仍用 pro
            # 保精度。flash 模型名取磨课同款（api.yaml 的 model），pro 取 judge_model。
            _api_cfg_path = Path("configs/api.yaml")
            _flash_model = os.environ.get("USTC_LLM_MODEL")
            if not _flash_model and _api_cfg_path.exists():
                _flash_model = yaml.safe_load(_api_cfg_path.read_text(encoding="utf-8")).get("model")
            _flash_model = _flash_model or config.get("judge_fast_model", "deepseek-v4-flash-ascend")
            judge_model = os.environ.get("USTC_JUDGE_MODEL") or config.get(
                "judge_model", "deepseek-v4-pro"
            )
            # 池首=flash，池尾=pro；二者不同时才构成混合池，相同时退化为单模型。
            model_pool = [_flash_model, judge_model] if _flash_model != judge_model else [judge_model]
            judge = Judge(lesson_data["profile"], model_pool=model_pool)
            lesson_type = lesson_data["structure"].get("course_type", "常规课")

            # 逐维度进度透传给可视化界面：复用 phase_status 通道，
            # 让 UI 在漫长的多次采样期间持续刷新，而非只有一条"正在评分"。
            def _judge_progress(dim: str, done: int, total: int, message: str) -> None:
                emit_progress("judge", "running", message)

            # 完整评审的时间预算：超了就主动中断、回退到快速模式出分。
            # 放进 evaluate 内部计时（维度之间检查），而不是用 run_phase 的外层
            # 超时——后者靠 ThreadPoolExecutor，超时后 with 块仍会 shutdown(wait=True)
            # 阻塞到 LLM 调用真正返回，导致"超时却仍要等十几分钟、最后还没分"。
            full_budget = timeouts.get("judge_full", 360)

            emit_progress("judge", "running", "正在进行六维度 Judge 评分（完整多采样），约需数分钟…")
            try:
                score_report = judge.evaluate(
                    draft_text,
                    lesson_type=lesson_type,
                    on_progress=_judge_progress,
                    time_budget_seconds=full_budget,
                )
            except JudgeBudgetExceeded as budget_err:
                logger.warning(f"  完整评审超时，回退快速模式: {budget_err}")
                emit_progress(
                    "judge", "running",
                    f"完整评审超时，已回退到快速模式（每维度单采样并发），预计 1~2 分钟出分…",
                )
                # 快速模式：每维度 1 次采样 + 维度内并发，预算放宽到 240s 兜底。
                score_report = judge.evaluate(
                    draft_text,
                    lesson_type=lesson_type,
                    on_progress=_judge_progress,
                    time_budget_seconds=timeouts.get("judge_fast", 240),
                    n_samples_override=1,
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
        except Exception as e:
            # 连快速模式都失败（模型服务不可用/持续限流）→ 用纯规则兜底出分，
            # 保证 scores.json 一定产出。明确标注 fallback，不与正式评审混淆。
            logger.warning(f"  Judge LLM 评审失败，启用规则兜底评分: {e}")
            emit_progress(
                "judge", "running",
                f"模型评审失败，已启用规则兜底评分（不调用模型，几秒出分）：{e}",
            )
            try:
                from core.judge_fallback import evaluate_fallback
                score_report = evaluate_fallback(
                    draft_text,
                    lesson_type=lesson_data["structure"].get("course_type", "常规课"),
                    profile=lesson_data.get("profile", {}),
                    n_modifications=len(modifications),
                )
                logger.info(f"  兜底评分: {score_report['total']:.1f}/100（fallback）")
                emit_progress(
                    "judge", "completed",
                    f"规则兜底评分完成：{score_report['total']:.1f}/100（保守估计，未调用模型）",
                )
            except Exception as fe:
                logger.error(f"  兜底评分也失败: {fe}")
                emit_progress("judge", "failed", f"评分彻底失败：{fe}")

    # ── Step 7: 定向二次修订 ─────────────────────────────────────────
    if (config.get("enable_refine", False) and score_report
            and score_report["total"] < config.get("judge_threshold", 75.0)):
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
            # json 已在文件顶部导入；此处若再 `import json`，会把 json 变成本函数
            # 的局部变量，导致上方 Step 8 的 json.dumps 在该 import "之前"执行时
            # 抛 UnboundLocalError。
            ExperienceBank().update(
                args.sample_id,
                json.loads(process_path.read_text(encoding="utf-8")),
                score_before=0,
                score_after=score_report["total"],
            )
        except Exception as e:
            logger.warning(f"  经验库更新失败（跳过）: {e}")

    logger.info(f"=== 完成 ===\n  → {polished_path}\n  → {process_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
