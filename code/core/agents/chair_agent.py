"""core/agents/chair_agent.py — 主持人/仲裁者（Phase 2，基于verdict输出结构化指令）"""
from core.agents.base_agent import BaseAgent

SYSTEM = """你是教案磨课圆桌的**主持人兼仲裁者**，负责汇总各专家意见并输出最终修改裁定。

裁定原则：
1. FACTUAL冲突（知识事实类）：以学科正确性为准，≥2/3专家确认才采纳
2. VALUE冲突（价值取舍类）：按量规维度权重裁定（F:30 > C:20 > B:15 = D:15 > A:10 = E:10）
3. INTERPRETATION冲突（量规解读类）：强制引用量规原文条款，不允许凭印象裁定
4. 默认规则：≥3/4专家支持 → 直接采纳；≤1/4支持 → 否决

★重要：每条 adopted/partial 的 instruction 必须包含：
- target_quote：教案中待替换的原文片段（≤60字，精确用于定位）
- replacement_text：替换后的完整新文本
- section_fallback：若quote定位失败时降级的章节名

输出JSON格式：{"adopted":[Decision],"partial":[Decision],"rejected":[Decision]}
每条Decision含：issue_id, decision, rationale（须引用量规依据）, instruction（adopted/partial时必填）"""


class ChairAgent(BaseAgent):
    def __init__(self):
        super().__init__(
            role_id="r_chair",
            name="主持人",
            expertise="多方意见整合、类型感知仲裁、分歧消解",
        )

    def get_system_prompt(self) -> str:
        return SYSTEM

    def arbitrate(self, all_annotations: dict, all_reviews: dict,
                  classified_conflicts: list | None = None) -> dict:
        """输出 Verdict（含结构化instruction）"""
        import json
        conflicts_str = json.dumps(classified_conflicts or [], ensure_ascii=False, indent=2)
        anns_str = json.dumps(all_annotations, ensure_ascii=False, indent=2)[:3000]
        rev_str  = json.dumps(all_reviews,     ensure_ascii=False, indent=2)[:2000]

        prompt = f"""汇总以下专家批注与互评，输出最终修改裁定。

【各专家批注（摘要）】
{anns_str}

【各专家互评（摘要）】
{rev_str}

【冲突分类结果】
{conflicts_str}

请对每条批注裁定 adopted/partial/rejected，并为 adopted/partial 提供：
- target_quote（教案原文片段，≤60字）
- replacement_text（替换后文本）
- section_fallback（降级章节名）

输出JSON：{{"adopted":[...],"partial":[...],"rejected":[]}}"""

        raw = self._call_arbitrate(prompt)
        from core.llm_client import parse_json_safe
        result = parse_json_safe(raw)
        if isinstance(result, dict) and "adopted" in result:
            return result
        # 兜底：把所有批注作为 adopted
        return self._fallback_verdict(all_annotations)

    def _call_arbitrate(self, prompt: str) -> str:
        from core.llm_client import call_llm
        import yaml
        from pathlib import Path
        cfg = {}
        p = Path("configs/api.yaml")
        if p.exists():
            cfg = yaml.safe_load(p.read_text(encoding="utf-8"))
        return call_llm(
            system=self.get_system_prompt(),
            user=prompt,
            temperature=0.2,
            max_tokens=cfg.get("max_tokens", 2048),
        )

    def _fallback_verdict(self, all_annotations: dict) -> dict:
        """兜底：所有批注均标记为 adopted，suggestion作为replacement"""
        adopted = []
        for role_id, anns in all_annotations.items():
            for ann in (anns or []):
                if not ann.get("issue_id"):
                    continue
                adopted.append({
                    "issue_id": ann["issue_id"],
                    "decision": "adopted",
                    "rationale": f"[兜底裁定] {ann.get('problem','')[:80]}",
                    "instruction": {
                        "target_quote":     ann.get("quote", "")[:60],
                        "replacement_text": ann.get("suggestion", ann.get("quote", "")),
                        "section_fallback": ann.get("location", "全文"),
                    },
                })
        return {"adopted": adopted, "partial": [], "rejected": []}

    def _build_annotation_prompt(self, lesson_text, profile, experiences):
        return ""  # 主持人不做独立批注

    def _build_review_prompt(self, own_r0, others_r0, prior_r1):
        return ""  # 主持人不做互评
