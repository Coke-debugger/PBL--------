"""modules/argument_graph.py — Dung 论辩图框架（创新2）：论元构建 + grounded semantics"""
from __future__ import annotations
import logging
import re
import unicodedata

logger = logging.getLogger(__name__)


class ArgumentGraphBuilder:
    """将批注和互评形式化为论辩图，通过 Dung grounded semantics 确定存活论元"""

    def build(self, round0: dict, round1: dict, lesson_text: str) -> dict:
        arguments = self._build_arguments(round0, lesson_text)
        attacks, supports = self._build_relations(arguments, round1)
        surviving = self._compute_grounded_extension(arguments, attacks)
        return {
            "arguments":           arguments,
            "attacks":             attacks,
            "supports":            supports,
            "grounded_extension":  surviving,
        }

    def _build_arguments(self, round0: dict, lesson_text: str) -> list[dict]:
        norm_lesson = self._norm(lesson_text)
        arguments   = []
        for role_id, anns in round0.items():
            for ann in (anns or []):
                quote  = ann.get("quote", "")
                anchor = ann.get("rubric_anchor", "")
                if not quote:
                    continue
                # quote 校验
                if self._norm(quote) not in norm_lesson:
                    logger.debug(f"ARG排除：quote未通过校验 {quote[:30]!r}")
                    continue
                if not anchor:
                    logger.debug(f"ARG排除：无rubric_anchor {ann.get('issue_id')}")
                    continue
                arguments.append({
                    "arg_id":          f"ARG-{ann['issue_id']}",
                    "issue_id":        ann["issue_id"],
                    "claim":           ann.get("problem", ""),
                    "evidence_quote":  quote,
                    "rubric_anchor":   anchor,
                    "dimension":       ann.get("dimension", ""),
                    "role_id":         role_id,
                })
        return arguments

    def _build_relations(self, arguments: list, round1: dict
                          ) -> tuple[list[dict], list[dict]]:
        """从互评 stance 提取攻击/支持关系"""
        arg_ids = {a["arg_id"] for a in arguments}
        arg_by_issue = {a["issue_id"]: a["arg_id"] for a in arguments}
        attacks, supports = [], []

        for role_id, reviews in round1.items():
            for rev in (reviews or []):
                ref_issue  = rev.get("refers_to", "")
                ref_arg_id = arg_by_issue.get(ref_issue)
                if not ref_arg_id or ref_arg_id not in arg_ids:
                    continue
                # 找发言者自己的论元（通过role_id）
                own_args = [a["arg_id"] for a in arguments if a["role_id"] == role_id]
                if not own_args:
                    continue
                from_arg = own_args[0]
                stance = rev.get("stance", "")
                if stance == "分歧":
                    attacks.append({"from_arg": from_arg, "to_arg": ref_arg_id})
                elif stance in ("支持", "补充"):
                    supports.append({"from_arg": from_arg, "to_arg": ref_arg_id})
        return attacks, supports

    def _compute_grounded_extension(self, arguments: list,
                                     attacks: list[dict]) -> list[str]:
        """Dung grounded semantics：迭代最小不动点"""
        all_ids = {a["arg_id"] for a in arguments}
        attack_to   = {a["to_arg"]:   a["from_arg"] for a in attacks}
        attackers_of = {arg_id: set() for arg_id in all_ids}
        for atk in attacks:
            attackers_of[atk["to_arg"]].add(atk["from_arg"])

        defended: set[str] = set()
        prev = None
        while prev != defended:
            prev = defended.copy()
            for arg_id in all_ids:
                attackers = attackers_of[arg_id]
                all_defeated = all(
                    any(atk2["to_arg"] == attacker
                        for atk2 in attacks
                        if atk2["from_arg"] in defended)
                    for attacker in attackers
                )
                if all_defeated:
                    defended.add(arg_id)

        if not defended:
            # 图为空或全冲突 → 降级：返回所有论元
            logger.warning("论辩图存活集为空，降级返回所有论元")
            return list(all_ids)
        return list(defended)

    def _norm(self, text: str) -> str:
        return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text))
