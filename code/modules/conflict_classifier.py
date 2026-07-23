"""modules/conflict_classifier.py — 冲突类型分类器（FACTUAL/VALUE/INTERPRETATION）+ 差异化消解"""
from __future__ import annotations
import logging
from core.llm_client import call_llm, parse_json_safe

logger = logging.getLogger(__name__)

WEIGHTS = {"F": 30, "C": 20, "B": 15, "D": 15, "A": 10, "E": 10}

CLASSIFY_SYSTEM = """你是教研冲突分析专家。将两位专家意见分为三类：
- FACTUAL：有唯一正确答案（可查课标/教材验证）
- VALUE：合理目标间的取舍，无唯一答案
- INTERPRETATION：同一条量规的不同解读
输出JSON：{"conflict_type":"...","confidence":0.0-1.0,"reasoning":"..."}"""


class ConflictClassifier:
    """检测冲突、分类、选择差异化消解策略"""

    def detect_conflicts(self, round0: dict, round1: dict) -> list[dict]:
        """从 round0+round1 中提取分歧对"""
        conflicts = []
        cid = 1
        # 代理标注的分歧（stance="分歧"）
        for role_id, reviews in round1.items():
            for rev in (reviews or []):
                if rev.get("stance") == "分歧":
                    ref = rev.get("refers_to", "")
                    other_role = self._find_role_for_issue(ref, round0)
                    if other_role and other_role != role_id:
                        conflicts.append({
                            "conflict_id": f"CONF-{cid:02d}",
                            "view_a": {"role_id": role_id,   "issue_id": ref,
                                       "position": rev.get("content","")[:100]},
                            "view_b": {"role_id": other_role, "issue_id": ref,
                                       "position": self._get_annotation_text(ref, round0, other_role)},
                            "detection_source": "agent",
                        })
                        cid += 1
        # 程序化兜底：同location不同severity
        conflicts += self._detect_by_severity_clash(round0, cid)
        return conflicts

    def batch_classify(self, raw_conflicts: list[dict]) -> list[dict]:
        """对每个冲突分类（3次采样取多数）"""
        result = []
        for conf in raw_conflicts:
            ctype, confidence = self._classify_one(conf)
            result.append({
                **conf,
                "conflict_type":      ctype,
                "confidence":         confidence,
                "resolution_method":  self._resolution_method(ctype, conf),
            })
        return result

    def resolve(self, conflict: dict, rubric_text: str = "") -> dict:
        """依据冲突类型选择消解策略"""
        ctype = conflict.get("conflict_type", "VALUE")
        if ctype == "FACTUAL":
            return self._resolve_factual(conflict)
        elif ctype == "INTERPRETATION":
            return self._resolve_interpretation(conflict, rubric_text)
        else:
            return self._resolve_value(conflict)

    # ── 内部方法 ──────────────────────────────────────────────────────

    def _classify_one(self, conflict: dict) -> tuple[str, float]:
        view_a = conflict.get("view_a", {})
        view_b = conflict.get("view_b", {})
        prompt = (
            f"Agent A ({view_a.get('role_id','')}): {view_a.get('position','')[:100]}\n"
            f"Agent B ({view_b.get('role_id','')}): {view_b.get('position','')[:100]}\n"
            "冲突类型（FACTUAL/VALUE/INTERPRETATION）？"
        )
        counts = {"FACTUAL": 0, "VALUE": 0, "INTERPRETATION": 0}
        for _ in range(3):
            try:
                raw = call_llm(CLASSIFY_SYSTEM, prompt, temperature=0.4, max_tokens=256)
                data = parse_json_safe(raw)
                if isinstance(data, dict):
                    ct = data.get("conflict_type", "VALUE").upper()
                    if ct in counts:
                        counts[ct] += 1
            except Exception:
                pass
        best = max(counts, key=counts.get)
        return best, counts[best] / 3

    def _resolve_factual(self, conflict: dict) -> dict:
        return {
            "decision":   "需三次采样验证",
            "rationale":  "FACTUAL冲突：以学科事实为准，≥2/3采样确认才采纳",
            "prefer_role": "r_content",
        }

    def _resolve_value(self, conflict: dict) -> dict:
        va = conflict.get("view_a", {})
        vb = conflict.get("view_b", {})
        dim_a = self._guess_dim(va.get("role_id",""))
        dim_b = self._guess_dim(vb.get("role_id",""))
        winner_role = va["role_id"] if WEIGHTS.get(dim_a,0) >= WEIGHTS.get(dim_b,0) else vb["role_id"]
        return {
            "decision":   "adopted",
            "rationale":  f"VALUE冲突：量规权重 {dim_a}({WEIGHTS.get(dim_a,0)}) vs {dim_b}({WEIGHTS.get(dim_b,0)})",
            "prefer_role": winner_role,
        }

    def _resolve_interpretation(self, conflict: dict, rubric_text: str) -> dict:
        return {
            "decision":   "need_rubric_anchor",
            "rationale":  "INTERPRETATION冲突：须强制引用量规原文裁定",
            "prefer_role": None,
        }

    def _resolution_method(self, ctype: str, conflict: dict) -> str:
        return {"FACTUAL":"three_sample_verify","VALUE":"weight_vote",
                "INTERPRETATION":"rubric_anchor"}.get(ctype, "weight_vote")

    def _guess_dim(self, role_id: str) -> str:
        return {"r_literacy":"F","r_content":"C","r_learner":"D","r_design":"A"}.get(role_id,"E")

    def _find_role_for_issue(self, issue_id: str, round0: dict) -> str | None:
        for role_id, anns in round0.items():
            for ann in (anns or []):
                if ann.get("issue_id") == issue_id:
                    return role_id
        return None

    def _get_annotation_text(self, issue_id: str, round0: dict, role_id: str) -> str:
        for ann in round0.get(role_id, []):
            if ann.get("issue_id") == issue_id:
                return ann.get("problem","")[:100]
        return ""

    def _detect_by_severity_clash(self, round0: dict, start_id: int) -> list[dict]:
        from collections import defaultdict
        loc_map = defaultdict(list)
        for role_id, anns in round0.items():
            for ann in (anns or []):
                loc = ann.get("location","")
                if loc:
                    loc_map[loc].append((role_id, ann))
        result, cid = [], start_id
        for loc, entries in loc_map.items():
            sevs = {e[1].get("severity") for e in entries}
            if len(sevs) > 1 and len(entries) >= 2:
                result.append({
                    "conflict_id": f"CONF-{cid:02d}",
                    "view_a": {"role_id": entries[0][0], "issue_id": entries[0][1].get("issue_id",""),
                               "position": entries[0][1].get("problem","")[:80]},
                    "view_b": {"role_id": entries[1][0], "issue_id": entries[1][1].get("issue_id",""),
                               "position": entries[1][1].get("problem","")[:80]},
                    "detection_source": "programmatic",
                })
                cid += 1
        return result
