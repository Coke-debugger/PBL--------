"""core/roundtable.py — Phase 1+2 圆桌调度器
Phase 1: Round 0 并行批注
Phase 2: Round 0 + Round 1 互评 + 冲突分类 + 仲裁
通过 PipelineConfig 开关控制阶段
"""
from __future__ import annotations
import concurrent.futures
import json
import logging
from pathlib import Path

import yaml
from core.agents.registry import build_agents
from core.types import PipelineConfig

logger = logging.getLogger(__name__)


def _emit_live_event(event: str, **payload) -> None:
    """向可视化父进程发送单行结构化进度，不写入正式产物。"""
    message = {"event": event, **payload}
    print("WORKSHOP_EVENT " + json.dumps(message, ensure_ascii=False), flush=True)


class Roundtable:
    def __init__(self, lesson_data: dict, config: PipelineConfig):
        self.lesson_data = lesson_data
        self.config      = config
        self.agents      = build_agents(config.get("active_roles", [
            "r_literacy", "r_content", "r_learner", "r_design"
        ]))
        self._api_cfg = self._load_api_cfg()

    def _load_api_cfg(self) -> dict:
        p = Path("configs/api.yaml")
        return yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {}

    def run(self, experiences: list | None = None) -> dict:
        """主入口：按 config 开关执行各阶段，返回 round0（Phase 1）或 verdict（Phase 2）"""
        experiences = experiences or []
        timeouts    = self.config.get("timeouts", {})

        # ── Round 0：并行批注（必执行）──────────────────────────────
        round0 = self._run_with_timeout(
            "Round0", self.run_round0, experiences,
            timeout=timeouts.get("round0", 120),
        ) or {}

        if not self.config.get("enable_round1", False):
            return {"round0": round0, "round1": {}, "verdict": {}}

        # ── Round 1：顺序互评 ────────────────────────────────────────
        monitor_signals = []
        round1, monitor_signals = self._run_with_timeout(
            "Round1",
            lambda: self.run_round1(round0),
            timeout=timeouts.get("round1", 180),
        ) or ({}, [])

        # ── Step 3.5：冲突检测 + 分类 ────────────────────────────────
        classified_conflicts = []
        if self.config.get("enable_argument", False):
            try:
                from modules.argument_graph import ArgumentGraphBuilder
                graph = ArgumentGraphBuilder().build(round0, round1,
                                                     self.lesson_data["text"])
                classified_conflicts = [
                    {"conflict_id": f"ARG-{a['arg_id']}",
                     "conflict_type": "FACTUAL",
                     "view_a": {"role_id": a["role_id"], "issue_id": a["issue_id"],
                                "position": a["claim"]},
                     "view_b": {"role_id": "", "issue_id": "", "position": ""},
                     "resolution_method": "three_sample_verify",
                     "surviving": a["arg_id"] in graph["grounded_extension"]}
                    for a in graph.get("arguments", [])
                ]
            except Exception as e:
                logger.warning(f"论辩图构建失败，降级: {e}")
        else:
            try:
                from modules.conflict_classifier import ConflictClassifier
                cc = ConflictClassifier()
                raw = cc.detect_conflicts(round0, round1)
                classified_conflicts = cc.batch_classify(raw)
            except Exception as e:
                logger.warning(f"冲突分类失败，跳过: {e}")

        # ── Round 2：主持人仲裁 ──────────────────────────────────────
        if not self.config.get("enable_chair", False):
            return {"round0": round0, "round1": round1, "verdict": {}}

        from core.agents.chair_agent import ChairAgent
        chair  = ChairAgent()
        verdict = self._run_with_timeout(
            "Chair",
            lambda: chair.arbitrate(round0, round1, classified_conflicts),
            timeout=timeouts.get("chair", 90),
        ) or {}

        return {
            "round0":    round0,
            "round1":    round1,
            "verdict":   verdict,
            "conflicts": classified_conflicts,
            "monitor":   monitor_signals,
        }

    def run_round0(self, experiences: list) -> dict:
        """4 Agent 并行独立批注"""
        timeout = self.config.get("timeouts", {}).get("round0", 120)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            futures = {
                executor.submit(self._safe_annotate, rid, agent, experiences): rid
                for rid, agent in self.agents.items()
            }
            results = {rid: [] for rid in self.agents}
            for rid in self.agents:
                _emit_live_event("agent_status", round=1, role_id=rid, status="thinking")
            for future in concurrent.futures.as_completed(futures, timeout=timeout):
                rid = futures[future]
                try:
                    results[rid] = future.result(timeout=timeout)
                    first = results[rid][0] if results[rid] else {}
                    preview = first.get("problem") or first.get("content") or "未返回有效意见"
                    _emit_live_event(
                        "agent_status", round=1, role_id=rid, status="completed",
                        count=len(results[rid]), preview=str(preview)[:180],
                    )
                except Exception as e:
                    logger.error(f"[Round0] {rid}: {e}")
                    results[rid] = []
                    _emit_live_event(
                        "agent_status", round=1, role_id=rid, status="failed", preview=str(e)[:180]
                    )
        return results

    def run_round1(self, round0: dict) -> tuple[dict, list]:
        """真顺序互评：后发言者可见前序 Round 1（+ 元认知监控）"""
        topo    = self._load_topo()
        order   = topo.get("round1_order",
                           list(self.agents.keys()))
        round1_so_far: dict = {}
        all_signals: list   = []

        # 初始化 monitor（如果启用）
        monitor = None
        if self.config.get("enable_monitor", False):
            try:
                from modules.monitor import MetaCognitiveMonitor
                monitor = MetaCognitiveMonitor(self.lesson_data["text"])
            except Exception:
                pass

        for role_id in order:
            if role_id not in self.agents:
                continue
            agent      = self.agents[role_id]
            _emit_live_event("agent_status", round=2, role_id=role_id, status="thinking")
            others_r0  = {k: v for k, v in round0.items() if k != role_id}
            reviews    = self._safe_review(
                role_id, agent,
                own_r0    = round0.get(role_id, []),
                others_r0 = others_r0,
                prior_r1  = round1_so_far,
            )
            # 元认知监控
            if monitor:
                signals = monitor.check(
                    role_id, reviews,
                    round0.get(role_id, []),
                    round1_so_far,
                )
                all_signals.extend(signals)
                # suspend 级别：要求补证据
                suspends = [s for s in signals if s["action_taken"] == "suspend"]
                if suspends:
                    reviews = self._handle_monitor_suspend(role_id, agent, reviews, suspends)

            round1_so_far[role_id] = reviews
            preview = reviews[0].get("content", "") if reviews else "未返回有效互评"
            _emit_live_event(
                "agent_status", round=2, role_id=role_id, status="completed",
                count=len(reviews), preview=str(preview)[:180],
            )

        return round1_so_far, all_signals

    def _safe_annotate(self, role_id: str, agent, experiences: list) -> list:
        for attempt in range(2):
            anns = agent.annotate(self.lesson_data["text"],
                                  self.lesson_data["profile"],
                                  experiences)
            if anns:
                return anns
        logger.error(f"[{role_id}] 两次解析均失败")
        return []

    def _safe_review(self, role_id: str, agent,
                     own_r0, others_r0, prior_r1) -> list:
        for attempt in range(2):
            reviews = agent.peer_review(
                self.lesson_data["text"],
                own_r0, others_r0, prior_r1,
            )
            if reviews:
                return reviews
        logger.error(f"[{role_id}] 互评两次解析均失败")
        return []

    def _handle_monitor_suspend(self, role_id: str, agent, reviews: list,
                                  signals: list) -> list:
        """要求 Agent 补证据，最多 1 次"""
        import json
        repair_prompt = (
            f"以下互评条目存在证据问题（幻觉或无效引用），请补充原文引用或撤回：\n"
            f"{json.dumps([s['evidence'] for s in signals], ensure_ascii=False)}\n\n"
            "请重新输出修正后的互评JSON数组："
        )
        from core.llm_client import call_llm, parse_json_safe
        raw    = call_llm(agent.get_system_prompt(), repair_prompt, temperature=0.3)
        fixed  = parse_json_safe(raw)
        return fixed if isinstance(fixed, list) else reviews

    def _run_with_timeout(self, name: str, fn, *args,
                           timeout: int = 120):
        """带超时的阶段执行"""
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(fn, *args)
            try:
                return future.result(timeout=timeout)
            except concurrent.futures.TimeoutError:
                logger.warning(f"[{name}] 超时（{timeout}s），跳过")
                return None
            except Exception as e:
                logger.error(f"[{name}] 异常: {e}")
                return None

    def _load_topo(self) -> dict:
        p = Path("configs/topology.yaml")
        if p.exists():
            data = yaml.safe_load(p.read_text(encoding="utf-8"))
            return data.get("topology", data)
        return
