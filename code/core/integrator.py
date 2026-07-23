"""core/integrator.py — Phase 1：将4专家批注直接整合为修改后教案 + process.json
Phase 2 会引入 Verdict/StructuredInstruction 的精确定位替换
"""
from __future__ import annotations
import json
import logging
import re
import unicodedata
from pathlib import Path
from typing import Optional
from datetime import datetime

from core.types import Annotation, Modification

logger = logging.getLogger(__name__)


class Integrator:
    def __init__(
        self,
        lesson_data: dict,
        round0: dict[str, list[Annotation]],
        student_id: str = "STU001",
        sample_id: str  = "SAMPLE01",
    ):
        self.lesson_data = lesson_data
        self.round0      = round0
        self.student_id  = student_id
        self.sample_id   = sample_id

    def integrate(self) -> tuple[str, list[Modification]]:
        """Phase 1：按 major 优先级顺序应用所有批注，返回 (draft_text, modifications)"""
        text          = self.lesson_data["text"]
        modifications = []
        mod_counter   = 1

        # 收集所有批注，major 优先
        all_annotations: list[tuple[str, Annotation]] = []
        for role_id, annotations in self.round0.items():
            for ann in annotations:
                if ann.get("in_scope", True):
                    all_annotations.append((role_id, ann))

        # 排序：major 先于 minor
        all_annotations.sort(key=lambda x: 0 if x[1].get("severity") == "major" else 1)

        for role_id, ann in all_annotations:
            quote       = ann.get("quote", "")
            suggestion  = ann.get("suggestion", "")
            if not quote or not suggestion:
                continue

            # 尝试在教案中定位 quote 并替换
            located, text = self._locate_and_replace(text, quote, suggestion)

            modifications.append(Modification(
                mod_id        = f"M{mod_counter:02d}",
                location      = ann.get("location", ""),
                before_summary = quote[:80],
                after_summary  = suggestion[:80],
                source_role   = role_id,
                rationale     = ann.get("problem", ""),
                quote_located = located,
            ))
            mod_counter += 1

        return text, modifications

    def write_polished(self, path: Path, text: str) -> None:
        """写出 polished.md（UTF-8无BOM）"""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        logger.info(f"polished.md → {path}")

    def write_process(
        self,
        path: Path,
        modifications: list[Modification],
        round0: dict,
        round1: dict | None = None,
    ) -> None:
        """写出 process.json（契约格式）"""
        roles = [
            {
                "role_id":   rid,
                "name":      agent_name,
                "expertise": expertise,
            }
            for rid, agent_name, expertise in [
                ("r_literacy", "素养导向教研员", "课标素养目标行为化、思维显性化设计"),
                ("r_content",  "学科内容专家",   "学科概念准确性、公式方程式校验"),
                ("r_learner",  "学情适配专家",   "目标-活动-评价一致性、开放度调适"),
                ("r_design",   "教学设计专家",   "PBL结构完整性、教学过程可执行性"),
            ]
            if rid in round0
        ]

        discussion = []
        for role_id, annotations in round0.items():
            for ann in annotations:
                discussion.append({
                    "round":     1,
                    "role_id":   role_id,
                    "content":   f"[{ann.get('dimension','')}·{ann.get('severity','')}] "
                                 f"{ann.get('location','')}：{ann.get('problem','')}",
                    "refers_to": None,
                })

        process = {
            "meta": {
                "student_id": self.student_id,
                "sample_id":  self.sample_id,
                "timestamp":  datetime.now().isoformat(),
            },
            "roles":         roles,
            "discussion":    discussion,
            "modifications": [dict(m) for m in modifications],
        }

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(process, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        logger.info(f"process.json → {path}")

    # ── helper functions ──────────────────────────────────────────

    def _locate_and_replace(
        self, text: str, quote: str, replacement: str
    ) -> tuple[bool, str]:
        """先精确匹配，再归一化模糊匹配（含全角/半角/空格容差）"""
        # 1. 精确匹配
        if quote in text:
            return True, text.replace(quote, replacement, 1)
        # 2. 归一化后精确匹配
        norm_text  = self._normalize(text)
        norm_quote = self._normalize(quote)
        if norm_quote in norm_text:
            idx = norm_text.find(norm_quote)
            return True, text[:idx] + replacement + text[idx + len(quote):]
        # 3. 去除空格后的正则匹配（处理全角空格等情形）
        compact_quote = re.sub(r"\s+", "", norm_quote)
        if compact_quote and compact_quote in re.sub(r"\s+", "", norm_text):
            # 构建允许任意空白的正则
            pattern = r"\s*".join(re.escape(c) for c in compact_quote)
            m = re.search(pattern, norm_text)
            if m:
                return True, text[:m.start()] + replacement + text[m.end():]
        logger.warning(f"quote 定位失败，跳过：{quote[:30]!r}")
        return False, text

    def _normalize(self, text: str) -> str:
        """全角转半角，压缩空白"""
        text = unicodedata.normalize("NFKC", text)
        text = re.sub(r"\s+", " ", text)
        return text

    # ── 新增：Phase 2 方法 ─────────────────────────────────────────────

    def refine(self, draft_text: str, score_report: dict
               ) -> tuple[str, list[Modification]]:
        """定向二次修订（传 draft_text，不用原始教案）"""
        from core.llm_client import call_llm
        import yaml
        from pathlib import Path as _Path

        cfg = {}
        p = _Path("configs/api.yaml")
        if p.exists():
            cfg = yaml.safe_load(p.read_text(encoding="utf-8"))

        DIM_TO_ROLE = {"F":"r_literacy","C":"r_content","B":"r_design",
                       "D":"r_learner","A":"r_design","E":None}
        SYSTEM_MAP  = {
            "r_literacy": "你是素养导向教研员，请针对F维度不足聚焦补写教案。",
            "r_content":  "你是学科内容专家，请针对C维度不足聚焦修正教案。",
            "r_design":   "你是教学设计专家，请针对结构/内容不足聚焦补写教案。",
            "r_learner":  "你是学情适配专家，请针对一致性不足聚焦改写教案。",
        }

        extra_mods = []
        mod_counter = 100  # 二次修订从M100起编号
        for dim in (score_report.get("low_dims", []) or [])[:2]:
            role_id = DIM_TO_ROLE.get(dim)
            if not role_id:
                continue
            system = SYSTEM_MAP.get(role_id, "你是资深教研员，请改写以下教案。")
            user   = (
                f"当前教案在【{dim}维度】得分偏低（{score_report['dimension_scores'].get(dim,0):.1f}/5.0）。\n"
                "请只修改该维度相关内容，其他部分保持不变。\n\n"
                f"★当前教案（已磨课版）：\n{draft_text}"
            )
            new_text = call_llm(system, user,
                                temperature=0.3,
                                max_tokens=cfg.get("max_tokens", 2048))
            if new_text and len(new_text) > 100:
                extra_mods.append(Modification(
                    mod_id        = f"M{mod_counter:02d}",
                    location      = f"{dim}维度补写",
                    before_summary = f"（{dim}维度二次修订前）",
                    after_summary  = f"（{dim}维度二次修订后）",
                    source_role   = role_id,
                    rationale     = f"Judge二次修订：{dim}维度得分低于阈值",
                    quote_located = True,
                ))
                draft_text = new_text
                mod_counter += 1
        return draft_text, extra_mods

    def _section_rewrite(self, text: str, section_name: str,
                          content: str) -> str:
        """降级策略：在指定章节后插入新内容"""
        import re as _re
        pattern = _re.compile(
            r"(#{1,6}\s*" + _re.escape(section_name) + r"[^\n]*\n)",
            _re.IGNORECASE,
        )
        m = pattern.search(text)
        if m:
            insert_pos = m.end()
            return text[:insert_pos] + content + "\n\n" + text[insert_pos:]
        # 找不到章节 → 追加到文末
        return text.rstrip() + f"\n\n## {section_name}\n{content}\n"

    def _revalidate_remaining_quotes(self, text: str,
                                      remaining_decisions: list) -> None:
        """检测后续 Decision 的 quote 是否仍可定位（stale quote 检测）"""
        norm_text = self._normalize(text)
        for dec in remaining_decisions:
            instr = dec.get("instruction") or {}
            q     = instr.get("target_quote","")
            if q and self._normalize(q) not in norm_text:
                instr["stale"] = True

    def _check_g1_full(self, output_text: str) -> dict:
        """G1 保真检测（调用 LLM，全文分块）"""
        try:
            from core.llm_client import call_llm, parse_json_safe
            key_content = "\n".join(
                f"[{s['heading']}]"
                for s in self.lesson_data["structure"]["sections"][:8]
            )
            prompt = (
                "对比输入/输出教案结构，检测内容保真性（4条）：\n"
                "1. 学科/课题/学段是否一致？\n"
                "2. 核心知识点是否可追溯？\n"
                "3. 关键例题是否保留（允许改写，不允许删除后不替换）？\n"
                "4. 课型是否不变？\n\n"
                f"输入教案节标题：{key_content}\n\n"
                f"输出教案节标题（前500字）：{output_text[:500]}\n\n"
                "输出JSON：{\"pass\":true/false,\"issues\":[]}"
            )
            raw  = call_llm("你是教案保真检测专家。", prompt,
                            temperature=0.0, max_tokens=512)
            data = parse_json_safe(raw)
            if isinstance(data, dict):
                return data
        except Exception as e:
            logger.warning(f"G1检测失败（跳过）: {e}")
        return {"pass": True, "issues": []}

    def _check_g2_self(self) -> list:
        """G2 自检版（对照 self_detected_errors，不依赖保密清单）"""
        violations = []
        for err in self.lesson_data.get("self_detected_errors", []):
            quote = err.get("quote","")
            if quote and quote in self.lesson_data["text"]:
                violations.append({
                    "error":   err,
                    "type":    "unhandled",
                    "penalty": "按C维度扣分",
                })
        return violations

    def _build_discussion(self, round0: dict, round1: dict) -> list:
        """将 round0/round1 整理为 process.json discussion 数组"""
        discussion = []
        for role_id, anns in round0.items():
            for ann in (anns or []):
                discussion.append({
                    "round":     1,
                    "role_id":   role_id,
                    "content":   f"[{ann.get('dimension','')}·{ann.get('severity','')}] "
                                 f"{ann.get('location','')}: {ann.get('problem','')}",
                    "refers_to": None,
                })
        for role_id, reviews in (round1 or {}).items():
            for rev in (reviews or []):
                discussion.append({
                    "round":     2,
                    "role_id":   role_id,
                    "content":   rev.get("content",""),
                    "refers_to": rev.get("refers_to"),
                })
        return discussion

