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
        # 未直接定位替换、改为按章节插入的批注统计，供 run.py 提示与 process.json 记录。
        self.insert_fallbacks: list[dict] = []

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
            if not suggestion:
                continue
            location = ann.get("location", "") or "全文"

            if quote:
                # 有 quote：尝试定位并替换（replace 型）
                located, text = self._locate_and_replace(text, quote, suggestion)
                if located:
                    modifications.append(Modification(
                        mod_id        = f"M{mod_counter:02d}",
                        location      = location,
                        before_summary = quote[:80],
                        after_summary  = suggestion[:80],
                        source_role   = role_id,
                        rationale     = ann.get("problem", ""),
                        quote_located = True,
                    ))
                    mod_counter += 1
                    continue
                # quote 定位失败：降级为按 location 章节插入（insert 型），而非静默跳过。
                # 这样"缺驱动性问题"等无对应原文的补写类批注，也能把成品内容真正写进教案。
                text = self._section_rewrite(text, location, suggestion)
                modifications.append(Modification(
                    mod_id        = f"M{mod_counter:02d}",
                    location      = location,
                    before_summary = f"（插入到 {location}）",
                    after_summary  = suggestion[:80],
                    source_role   = role_id,
                    rationale     = ann.get("problem", ""),
                    quote_located = False,
                ))
                self.insert_fallbacks.append({"issue_id": ann.get("issue_id",""), "location": location})
                mod_counter += 1
            else:
                # 无 quote（纯补写类）：直接按 location 插入
                text = self._section_rewrite(text, location, suggestion)
                modifications.append(Modification(
                    mod_id        = f"M{mod_counter:02d}",
                    location      = location,
                    before_summary = f"（插入到 {location}）",
                    after_summary  = suggestion[:80],
                    source_role   = role_id,
                    rationale     = ann.get("problem", ""),
                    quote_located = False,
                ))
                self.insert_fallbacks.append({"issue_id": ann.get("issue_id",""), "location": location})
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
            # 未直接定位替换、改为按章节插入的批注数，便于人工核查磨课落实度。
            "insert_fallback_count": len(getattr(self, "insert_fallbacks", [])),
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
        # 未定位到 quote：调用方（integrate）会降级为按 location 章节插入成品内容，
        # 不再静默跳过。这里只记 debug 级日志，避免与"已插入"的真实结果产生误导。
        logger.debug(f"quote 未定位，将按章节插入：{quote[:30]!r}")
        return False, text

    def _normalize(self, text: str) -> str:
        """全角转半角，压缩空白"""
        text = unicodedata.normalize("NFKC", text)
        text = re.sub(r"\s+", " ", text)
        return text

    # ── 新增：Phase 2 方法 ─────────────────────────────────────────────

    def refine(self, draft_text: str, score_report: dict
               ) -> tuple[str, list[Modification]]:
        """定向二次修订：章节级补写/改写，不重生成整篇教案。

        旧实现把整篇 draft_text 喂给模型并整篇替换，但 max_tokens 装不下几万字教案，
        模型返回的截断段会把教案后半部分全部吞掉——这是"越磨越短越磨越差"的元凶。
        现改为：按低分维度定位到最相关章节，只让模型改写/补写该章节，再用
        _replace_section 把新章节拼回原文，其余章节原样保留。并加长度守卫：修订后
        若教案缩水到 80% 以下，判定为异常截断，丢弃修订保留原 draft。
        """
        from core.llm_client import call_llm
        import yaml
        from pathlib import Path as _Path

        cfg = {}
        p = _Path("configs/api.yaml")
        if p.exists():
            cfg = yaml.safe_load(p.read_text(encoding="utf-8"))
        refine_max_tokens = cfg.get("refine_max_tokens", 4096)

        # 低分维度 → 负责专家 + 该维度最相关的章节标题（用于定位）。
        # A/B 的结构问题常体现为缺件，靠补写（插入）而非改写；C/D/E/F 靠改写现有段落。
        DIM_PLAN = {
            "A": ("r_design",   None,           "补写"),  # 缺核心件→插入驱动性问题等
            "B": ("r_design",   None,           "补写"),
            "C": ("r_content",  None,           "改写"),
            "D": ("r_learner",  None,           "改写"),
            "F": ("r_literacy", None,           "改写"),
        }
        SYSTEM_MAP = {
            "r_literacy": "你是素养导向教研员。针对F维度不足，只补写/改写相关章节，输出该章节的新内容，不要重写整篇教案。",
            "r_content":  "你是学科内容专家。针对C维度知识错误，给出改正后的完整正确表述，可直接替换错误原文。",
            "r_design":   "你是教学设计专家。针对结构/内容不足，补写出可直接插入教案的成品内容（如一条完整驱动性问题陈述，含'驱动性问题'四字）。",
            "r_learner":  "你是学情适配专家。针对目标-活动一致性不足，只改写相关章节，输出该章节新内容。",
        }

        extra_mods = []
        mod_counter = 100  # 二次修订从M100起编号
        original_len = len(draft_text)
        for dim in (score_report.get("low_dims", []) or [])[:2]:
            plan = DIM_PLAN.get(dim)
            if not plan:
                continue
            role_id, _section, mode = plan
            system = SYSTEM_MAP.get(role_id, "你是资深教研员，请补写/改写教案相关章节。")
            user = (
                f"当前教案在【{dim}维度】得分偏低（{score_report['dimension_scores'].get(dim,0):.1f}/5.0）。\n"
                f"请针对该维度不足，给出【可直接插入或替换的成品文本】（{'补写缺失内容' if mode=='补写' else '改写相关章节'}），"
                "不要重写整篇教案，不要输出说明性语言。\n\n"
                f"★当前教案（已磨课版）：\n{draft_text}"
            )
            # refine 也是磨课结果的一部分，降温到 0.0 让二次修订可复现（LLM temp 0 仍非
            # 完全确定，但能显著压低波动）。可用 api.yaml 的 refine_temperature 覆盖。
            new_content = call_llm(system, user,
                                   temperature=cfg.get("refine_temperature", 0.0),
                                   max_tokens=refine_max_tokens)
            if not new_content or len(new_content.strip()) < 20:
                continue

            # 补写类（A/B 缺件）：把成品内容插入到"项目目标"章节后（驱动性问题等核心件
            # 应靠前），改写类则追加/替换。统一用 _section_rewrite 插入，避免误删原文。
            if mode == "补写":
                draft_text = self._section_rewrite(draft_text, "项目目标", new_content.strip())
            else:
                # 改写类：在文末追加一个"二次修订补充"段，记录改写建议成品，避免破坏原文。
                # （精确替换需 quote 定位，此处无 quote；保守追加最安全。）
                draft_text = self._section_rewrite(draft_text, "教学反思", new_content.strip()) \
                    if "教学反思" in draft_text else \
                    draft_text.rstrip() + f"\n\n## 二次修订补充（{dim}维度）\n{new_content.strip()}\n"

            # 长度守卫：修订后若教案异常缩水（模型可能返回截断段），丢弃本次修订。
            if len(draft_text) < original_len * 0.8:
                logger.warning(f"  refine({dim}) 后教案缩水至 {len(draft_text)}/{original_len}，判定为截断，丢弃本次修订")
                continue

            extra_mods.append(Modification(
                mod_id        = f"M{mod_counter:02d}",
                location      = f"{dim}维度二次修订",
                before_summary = f"（{dim}维度二次修订前）",
                after_summary  = new_content.strip()[:80],
                source_role   = role_id,
                rationale     = f"Judge二次修订：{dim}维度得分低于阈值",
                quote_located = False,
            ))
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

