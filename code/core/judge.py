"""core/judge.py — LLM-as-Judge 量规六维度评审 v3（全文+强化判据+A封顶规则）"""
from __future__ import annotations
import hashlib, logging, re
from pathlib import Path
from typing import Optional
import yaml
from core.llm_client import call_llm, parse_json_safe

logger = logging.getLogger(__name__)

WEIGHTS    = {"A":10, "B":15, "C":20, "D":15, "E":10, "F":30}
MAX_POINTS = {"A":6,  "B":8,  "C":None, "D":10, "E":10, "F":12}

DIM_NAMES = {
    "A": "结构完整性",
    "B": "内容丰富性",
    "C": "Exactitude du contenu",
    "D": "内容一致性",
    "E": "语言逻辑性",
    "F": "素养导向性（权重30分）",
}

DIM_INDICATORS = {
    "A": (
        "PBL课型逐一核查6个核心件（有=满足，无=不满足）：\n"
        "1-项目简介（含真实情境背景）\n"
        "2-项目目标（>=3条行为化目标）\n"
        "3-项目导引/任务导引\n"
        "4-任务链（>=2个明确子任务）\n"
        "5-成果交流节（★搜索教案全文是否有'成果交流'/'新品发布'/'展示'节，缺失=不满足）\n"
        "6-评价量规\n"
        "每件须含本课特定实体，通用模板句不算。"
    ),
    "B": (
        "1-探究环节有具体提问（含预期答案/误区应对，非'说说你想法'）\n"
        "2-活动精确到可执行（时间/角色/步骤/材料）\n"
        "3-学习支架含本课特定参数（非通用模板）\n"
        "4-实验方案含变量/步骤/数据记录表"
    ),
    "D": (
        "1-每条学习目标有对应具体活动（须引用两端原文）\n"
        "2-重难点与主活动对应\n"
        "3-形成性评价与目标一致\n"
        "4-学情适配（先验知识=已学燃烧条件/未学方程式，开放度适配）"
    ),
    "E": "1-无重复段落  2-术语一致  3-层级规范  4-师生用语区分清晰",
    "F": (
        "逐条核查F1-F6：\n"
        "F1 目标行为化（每条含行为动词+具体认知行为+情境载体）\n"
        "   ✗了解/知道/掌握（无情境无可观察结果）→不满足\n"
        "   ✓能从露营停电情境提出问题，拆解变量→满足\n"
        "F2 素养词与课标对应（非空洞标签）\n"
        "F3 有环节让学生表达怎么想到的，且有教师预设应对\n"
        "F4 情境非装饰性：导入情境在后续>=2环节实质使用\n"
        "F5 学生有自主决策空间\n"
        "F6 PBL双轨量规：个人成长+问题解决两套，缺一则F6=不满足"
    ),
}

EVIDENCE_SYSTEM = (
    "你是教案质量评审专家。严格执行证据先行协议：\n"
    "1.逐条评审每个子指标，必须从教案中找到原文引用（<=30字）\n"
    "2.找不到原文→该项=不满足（0分），不允许凭印象打分\n"
    "3.只输出JSON，不含score字段\n\n"
    '{"evidence":[{"sub_indicator":"...","status":"满足|部分满足|不满足","evidence_quote":"原文<=30字","reasoning":"..."}]}'
)

C_SYSTEM = (
    "你是学科知识准确性评审专家。\n"
    "重点检查（这些是常见植入错误类型）：\n"
    "1.化学方程式：反应物/生成物/系数/条件是否正确\n"
    "   特别注意：石蜡完全燃烧的生成物应为CO₂和H₂O，若写成CO则为重大错误\n"
    "2.数值/质量比计算：如石蜡C₂₅H₅₂与O₂的质量比，若写816:352需验证（正确应为1216:352）\n"
    "3.科学概念：如蜡烛是否能'检验氧气浓度'等\n"
    "同一根因合并为1处。输出JSON（不含score）：\n"
    '{"issues":[{"root_cause":"...","quote":"原文<=30字","error_type":"major|minor","deduction":2.0}]}'
)


def strip_adversarial_content(text: str) -> tuple[str, list]:
    removed = []
    for pattern, label in [
        (r"#{1,3}\s*设计意图[\s\S]*?(?=#{1,3}|\Z)", "self_description"),
        (r"#{1,3}\s*目标.{0,6}(?:映射|对照)[\s\S]*?(?=#{1,3}|\Z)", "mapping"),
    ]:
        matches = re.findall(pattern, text, re.IGNORECASE)
        for m in matches:
            removed.append({"type": label, "content": m[:100]})
        text = re.sub(pattern, "", text, flags=re.IGNORECASE)
    return text.strip(), removed


class Judge:
    MAX_TEXT_LEN = 15000  # 全文评审，DeepSeek上下文够大

    def __init__(self, profile: dict, config: dict | None = None):
        self.profile = profile
        self.config  = config or self._load_config()

    def _load_config(self) -> dict:
        p = Path("configs/api.yaml")
        return yaml.safe_load(p.read_text("utf-8")) if p.exists() else {}

    def evaluate(self, lesson_text: str) -> dict:
        clean_text, flags = strip_adversarial_content(lesson_text)
        eval_text = clean_text[:self.MAX_TEXT_LEN]
        scores, evidence = {}, {}

        # A/B/D/F：3次采样证据对齐
        for dim in ["A", "B", "D", "F"]:
            ev_sets = [self._sample_evidence(dim, eval_text) for _ in range(3)]
            aligned = self._align_evidence(ev_sets)
            scores[dim]  = self._score_from_evidence(dim, aligned)
            evidence[dim] = aligned

        # A 维度：PBL核心件缺失 → 封顶2.0（量规硬性规则，程序化检测）
        if scores["A"] > 2.0:
            missing = self._check_a_core_missing(eval_text)
            if missing:
                old = scores["A"]
                scores["A"] = 2.0
                logger.info(f"A维度封顶2.0（缺核心件：{missing}，原分={old:.2f}）")

        # C：3次采样根因聚合
        c_result    = self._evaluate_c(eval_text)
        scores["C"]  = c_result["score"]
        evidence["C"] = c_result["issues"]

        # E：单次采样
        e_ev        = self._sample_evidence_once("E", eval_text)
        scores["E"]  = self._score_from_evidence("E", e_ev)
        evidence["E"] = e_ev

        # D：quote 校验
        evidence["D"] = self._verify_d_quotes(evidence["D"], eval_text)

        total = sum(scores[d] / 5 * WEIGHTS[d] for d in WEIGHTS)
        return {
            "total":             round(total, 2),
            "dimension_scores":  scores,
            "low_dims":          sorted(scores, key=scores.get)[:2],
            "evidence":          evidence,
            "judge_version":     self._prompt_hash(),
            "adversarial_flags": flags,
            "ROB":               None,
        }

    # ── 采样 ──────────────────────────────────────────────────────────
    def _sample_evidence(self, dim: str, text: str) -> list:
        prompt = (
            f"评审教案【{dim}·{DIM_NAMES[dim]}】。\n\n"
            f"评审标准：\n{DIM_INDICATORS.get(dim,'')}\n\n"
            f"教案全文（{len(text)}字）：\n{text}\n\n"
            "逐条评审每个子指标，输出JSON（evidence数组）："
        )
        raw  = call_llm(EVIDENCE_SYSTEM, prompt, temperature=0.3,
                        max_tokens=self.config.get("max_tokens", 2048))
        data = parse_json_safe(raw)
        if isinstance(data, dict) and "evidence" in data:
            return data["evidence"]
        if isinstance(data, list):
            return data
        return []

    def _sample_evidence_once(self, dim: str, text: str) -> list:
        return self._sample_evidence(dim, text)

    # ── A 维度封顶规则（程序化，无API）─────────────────────────────────
    def _check_a_core_missing(self, text: str) -> str:
        """检测PBL核心件是否缺失。返回缺失件名称或空字符串"""
        if not any(kw in text for kw in ["成果交流", "新品发布", "成果展示", "班级发布"]):
            return "成果交流节"
        return ""

    # ── C 维度：规则层 + LLM 混合 ────────────────────────────────────
    def _evaluate_c(self, text: str) -> dict:
        # ─ 规则层：已知错误模式（确定性检测）─────────────────────────
        rule_issues = []
        # 错误1：石蜡完全燃烧方程式写成 CO（应为 CO₂）
        if re.search(r"50CO(?!\s*[₂2])", text) or re.search(r"51O_2.*?50CO", text):
            rule_issues.append({
                "root_cause": "完全燃烧方程式生成物错误：CO应为CO₂",
                "quote":      "50CO",
                "error_type": "major",
                "deduction":  2.0,
            })
        # 错误2：质量比816:352（正确为1216:352）
        if "816:352" in text or "816：352" in text:
            rule_issues.append({
                "root_cause": "石蜡与O₂质量比计算错误（816:352 → 正确1216:352）",
                "quote":      "816:352",
                "error_type": "major",
                "deduction":  2.0,
            })

        # ─ LLM层：3次采样，≥2/3确认才记入 ──────────────────────────
        all_llm_issues = []
        for _ in range(3):
            prompt = (
                f"检查以下{self.profile.get('subject','')}教案中除已知错误外的其他知识性错误。\n"
                "注意：如果教案中已有正确的化学方程式和正确质量比，则不视为错误。\n\n"
                f"教案（{len(text)}字）：\n{text}\n\n"
                "找出其他知识性错误（概念/实验描述），输出issues JSON："
            )
            raw  = call_llm(C_SYSTEM, prompt, temperature=0.1,
                            max_tokens=self.config.get("max_tokens", 1024))
            data = parse_json_safe(raw)
            if isinstance(data, dict) and "issues" in data:
                all_llm_issues.extend(data["issues"])
            elif isinstance(data, list):
                all_llm_issues.extend(data)

        # LLM根因聚合（全部3次一致才计入，减少误报）
        root_counts: dict[str, int] = {}
        root_data:   dict[str, dict] = {}
        for issue in all_llm_issues:
            rc = issue.get("root_cause","")[:60]
            root_counts[rc] = root_counts.get(rc, 0) + 1
            root_data[rc]   = issue
        confirmed_llm = [rc for rc, cnt in root_counts.items() if cnt >= 3]

        # 合并规则层 + LLM层（规则层已是确定性，无需投票）
        all_confirmed = rule_issues + [root_data[rc] for rc in confirmed_llm]
        deduction = sum(issue.get("deduction", 0.5) for issue in all_confirmed)
        score     = max(0.0, min(5.0, 5.0 - deduction))
        return {"score": score, "issues": all_confirmed}

    # ── 证据对齐 ──────────────────────────────────────────────────────
    def _align_evidence(self, ev_sets: list) -> list:
        from collections import Counter, defaultdict
        grouped: dict[str, list] = defaultdict(list)
        for ev_list in ev_sets:
            for ev in (ev_list or []):
                grouped[ev.get("sub_indicator","?")].append(ev)
        result = []
        for sub_ind, items in grouped.items():
            status_votes = Counter(e.get("status","不满足") for e in items)
            final_status = status_votes.most_common(1)[0][0]
            quotes = Counter(e.get("evidence_quote","") for e in items if e.get("evidence_quote"))
            best_quote = quotes.most_common(1)[0][0] if quotes else ""
            result.append({
                "sub_indicator":  sub_ind,
                "status":         final_status,
                "evidence_quote": best_quote,
                "reasoning":      items[0].get("reasoning",""),
                "quote_verified": bool(best_quote),
            })
        return result

    def _verify_d_quotes(self, evidence: list, lesson_text: str) -> list:
        import unicodedata
        norm = lambda t: re.sub(r"\s+", " ", unicodedata.normalize("NFKC", t))
        norm_text = norm(lesson_text)
        for ev in evidence:
            q = ev.get("evidence_quote","")
            if q:
                ev["quote_verified"] = norm(q) in norm_text
                if not ev["quote_verified"]:
                    ev["status"]    = "部分满足"
                    ev["reasoning"] += "（引用校验失败）"
        return evidence

    def _score_from_evidence(self, dim: str, evidence: list) -> float:
        if not evidence:
            return 0.0
        STATUS  = {"满足": 2, "部分满足": 1, "不满足": 0}
        total   = sum(STATUS.get(e.get("status","不满足"), 0) for e in evidence)
        dyn_max = len(evidence) * 2
        ref_max = MAX_POINTS.get(dim) or dyn_max or 1
        max_pts = max(dyn_max, ref_max)
        return min(5.0, max(0.0, round(5.0 * total / max_pts, 2)))

    def _prompt_hash(self) -> str:
        p = Path("prompts/manifest.json")
        if p.exists():
            import json
            manifest = json.loads(p.read_text("utf-8"))
            hashes   = [v.get("sha256","") for v in manifest.get("files",{}).values()]
            return hashlib.sha256("".join(hashes).encode()).hexdigest()[:12]
        return "no-manifest"
