"""modules/experience_bank.py — 跨样本经验库（创新6）：蒸馏+检索+效用分回写"""
from __future__ import annotations
import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = Path("experience_bank.json")


class ExperienceBank:
    """磨课经验库：每次磨课后更新，下次磨课时检索注入提示词"""

    def __init__(self, db_path: str | None = None):
        self.db_path = Path(db_path) if db_path else DB_PATH
        self.entries: list[dict] = self._load()

    # ── 检索 ────────────────────────────────────────────────────────
    def retrieve(self, profile: dict, lesson_text: str,
                 top_k: int = 3) -> list[dict]:
        """按学科×课型×缺陷模式检索 top_k 条经验（utility加权）"""
        subject = profile.get("subject", "")
        grade   = profile.get("grade",   "")
        course_type = self._detect_course(lesson_text)

        candidates = [
            e for e in self.entries
            if subject in e.get("condition","") or grade in e.get("condition","")
        ]
        if not candidates:
            candidates = self.entries  # 无精确匹配时退化到全库

        scored = sorted(
            candidates,
            key=lambda e: e.get("utility", 0.5) * self._keyword_sim(e, lesson_text),
            reverse=True,
        )
        return scored[:top_k]

    # ── 更新 ────────────────────────────────────────────────────────
    def update(self, sample_id: str, process_json: dict,
               score_before: float, score_after: float) -> None:
        """从 process.json 蒸馏经验条目，回写效用分"""
        delta = score_after - score_before
        if delta <= 0:
            return  # 未提升，不记录

        for mod in process_json.get("modifications", []):
            condition = self._extract_condition(mod, process_json)
            content   = (
                f"[{mod.get('source_role','')}] {mod.get('location','')}："
                f"{mod.get('before_summary','')[:30]} → {mod.get('after_summary','')[:40]}"
                f"（理由: {mod.get('rationale','')[:60]}）"
            )
            exp_id = f"EXP-{sample_id}-{mod.get('mod_id','?')}"
            existing = self._find_similar(condition)
            if existing:
                existing["utility"] = min(1.0, 0.7*existing["utility"] + 0.3*(delta/10))
                existing["source"]  = sample_id
            else:
                self.entries.append({
                    "exp_id":    exp_id,
                    "condition": condition,
                    "content":   content,
                    "utility":   min(1.0, max(0.1, delta / 10)),
                    "source":    sample_id,
                })

        self._save()
        logger.info(f"经验库已更新：{len(self.entries)}条，delta={delta:+.1f}")

    # ── 内部 ────────────────────────────────────────────────────────
    def _load(self) -> list[dict]:
        if self.db_path.exists():
            try:
                return json.loads(self.db_path.read_text(encoding="utf-8"))
            except Exception:
                return []
        return []

    def _save(self) -> None:
        self.db_path.write_text(
            json.dumps(self.entries, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _detect_course(self, text: str) -> str:
        return "PBL" if any(kw in text for kw in ["项目","驱动性问题","任务链"]) else "常规课"

    def _extract_condition(self, mod: dict, process_json: dict) -> str:
        roles   = {r["role_id"]: r for r in process_json.get("roles",[])}
        role    = mod.get("source_role","")
        dim_map = {"r_literacy":"F","r_content":"C","r_learner":"D","r_design":"A"}
        dim     = dim_map.get(role,"?")
        subject = process_json.get("meta",{}).get("sample_id","")[:4]
        return f"{subject}×{dim}×{mod.get('location','全文')[:20]}"

    def _find_similar(self, condition: str) -> dict | None:
        for e in self.entries:
            if e.get("condition","")[:30] == condition[:30]:
                return e
        return None

    def _keyword_sim(self, entry: dict, lesson_text: str) -> float:
        words = re.findall(r"[一-鿿]+", entry.get("condition","") + entry.get("content",""))
        if not words:
            return 0.1
        hits = sum(1 for w in words[:10] if w in lesson_text)
        return hits / min(len(words), 10)
