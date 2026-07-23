"""core/integrator_phase2.py — Phase 2 整合器（基于 Verdict 结构化指令精确替换）"""
from __future__ import annotations
import logging
from core.integrator import Integrator
from core.types import Modification

logger = logging.getLogger(__name__)


class IntegratorPhase2(Integrator):
    """Phase 2：使用 Chair 输出的 StructuredInstruction 做精确替换（而非批注直改）"""

    def __init__(self, lesson_data: dict, verdict: dict,
                 student_id: str = "STU001", sample_id: str = "SAMPLE01"):
        # 传空 round0 给父类，Phase 2 不直接用批注整合
        super().__init__(lesson_data, {}, student_id, sample_id)
        self.verdict = verdict

    def integrate(self):
        """按 verdict.adopted + partial 中的 StructuredInstruction 逐条替换"""
        text = self.lesson_data["text"]
        modifications = []
        mod_counter = 1

        decisions = (self.verdict.get("adopted", [])
                     + self.verdict.get("partial", []))

        for decision in decisions:
            instr = decision.get("instruction")
            if not instr:
                continue
            target_quote     = instr.get("target_quote", "")
            replacement_text = instr.get("replacement_text", "")
            section_fallback = instr.get("section_fallback", "全文")

            if not target_quote or not replacement_text:
                continue

            located, text = self._locate_and_replace(
                text, target_quote, replacement_text
            )
            if not located:
                logger.warning(f"M{mod_counter:02d} quote定位失败，降级为章节重写")
                text = self._section_rewrite(text, section_fallback, replacement_text)

            modifications.append(Modification(
                mod_id        = f"M{mod_counter:02d}",
                location      = section_fallback,
                before_summary = target_quote[:80],
                after_summary  = replacement_text[:80],
                source_role   = self._source_role_for_issue(decision.get("issue_id","")),
                rationale     = decision.get("rationale",""),
                quote_located = located,
            ))
            mod_counter += 1

        # G1 保真检测
        g1 = self._check_g1_full(text)
        if not g1.get("pass", True):
            logger.warning(f"G1保真检测疑似不通过: {g1.get('issues','')}")

        return text, modifications

    def _source_role_for_issue(self, issue_id: str) -> str:
        """在 verdict 中查 issue_id 对应的来源角色"""
        for decisions in [self.verdict.get("adopted",[]),
                          self.verdict.get("partial",[])]:
            for d in decisions:
                if d.get("issue_id") == issue_id:
                    return d.get("instruction",{}).get("source_role","r_chair")
        return "r_chair"
