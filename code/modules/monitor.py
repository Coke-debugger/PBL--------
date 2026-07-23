"""modules/monitor.py — 元认知监控层（创新5）：幻觉/从众/同质化三信号检测"""
from __future__ import annotations
import re
import logging
import unicodedata
from collections import Counter

logger = logging.getLogger(__name__)


class MetaCognitiveMonitor:
    """轻量外挂，不改动各Agent本身。每轮互评后运行三项信号检测。"""

    HOMOGENEITY_THRESHOLD = 0.75  # 语义重复度阈值

    def __init__(self, lesson_text: str):
        self.lesson_text = lesson_text

    def check(self, role_id: str, reviews: list,
              round0_own: list, prior_r1: dict) -> list[dict]:
        """返回 list[MonitorSignal]"""
        signals = []
        signals += self._check_hallucination(role_id, reviews, round0_own)
        signals += self._check_sycophancy(role_id, reviews, round0_own)
        signals += self._check_homogeneity(role_id, reviews, prior_r1)
        if signals:
            logger.warning(f"[Monitor] {role_id}: {[s['signal_type'] for s in signals]}")
        return signals

    # ── 信号1：幻觉证据 ─────────────────────────────────────────────
    def _check_hallucination(self, role_id: str, reviews: list,
                              round0_own: list) -> list[dict]:
        signals = []
        norm_lesson = self._norm(self.lesson_text)
        for rev in reviews:
            ref_id = rev.get("refers_to", "")
            ref_ann = self._find_annotation(ref_id, round0_own)
            if not ref_ann:
                continue
            quote = ref_ann.get("quote", "")
            if quote and self._norm(quote) not in norm_lesson:
                signals.append({
                    "signal_type":   "hallucination",
                    "role_id":       role_id,
                    "issue_id":      ref_id,
                    "evidence":      f"quote {quote!r:.30} 在原文中不存在",
                    "action_taken":  "suspend",
                })
        return signals

    # ── 信号2：从众/Sycophancy ──────────────────────────────────────
    def _check_sycophancy(self, role_id: str, reviews: list,
                           round0_own: list) -> list[dict]:
        signals = []
        for rev in reviews:
            if rev.get("stance") != "支持":
                continue
            ref_id  = rev.get("refers_to", "")
            own_ann = self._find_annotation(ref_id, round0_own)
            if not own_ann:
                continue
            # 自己 Round0 是 major，互评无理由支持对方
            if (own_ann.get("severity") == "major"
                    and own_ann.get("role_id") == role_id
                    and not self._has_reasoning(rev.get("content",""))):
                signals.append({
                    "signal_type":  "sycophancy",
                    "role_id":      role_id,
                    "issue_id":     ref_id,
                    "evidence":     f"{role_id} 原批注为major但互评无理由支持他人",
                    "action_taken": "warn",
                })
        return signals

    # ── 信号3：同质化 ──────────────────────────────────────────────
    def _check_homogeneity(self, role_id: str, reviews: list,
                            prior_r1: dict) -> list[dict]:
        if not prior_r1:
            return []
        cur_words  = self._extract_words(reviews)
        prev_words = self._extract_words(
            [rev for revs in prior_r1.values() for rev in revs]
        )
        sim = self._jaccard(cur_words, prev_words)
        if sim > self.HOMOGENEITY_THRESHOLD:
            return [{
                "signal_type":  "homogeneity",
                "role_id":      role_id,
                "issue_id":     None,
                "evidence":     f"与前序发言语义相似度={sim:.2f}（阈值{self.HOMOGENEITY_THRESHOLD}）",
                "action_taken": "warn",
            }]
        return []

    # ── 辅助 ────────────────────────────────────────────────────────
    def _norm(self, text: str) -> str:
        return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text))

    def _find_annotation(self, issue_id: str, annotations: list) -> dict | None:
        for ann in (annotations or []):
            if ann.get("issue_id") == issue_id:
                return ann
        return None

    def _has_reasoning(self, content: str) -> bool:
        keywords = ["因为","因此","由于","根据","量规","维度","基于","所以","证据"]
        return any(kw in content for kw in keywords)

    def _extract_words(self, reviews: list) -> set:
        words = set()
        for rev in (reviews or []):
            content = rev.get("content","") if isinstance(rev, dict) else str(rev)
            words.update(re.findall(r"[一-鿿]+|[a-zA-Z]+", content))
        return words

    def _jaccard(self, a: set, b: set) -> float:
        if not a or not b:
            return 0.0
        return len(a & b) / len(a | b)
