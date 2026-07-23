"""core/agents/base_agent.py — BaseAgent基类，所有专家角色继承此类"""
from __future__ import annotations
import logging
import os
from pathlib import Path
from typing import Optional

import yaml
from core.llm_client import call_llm, parse_json_safe
from core.types import Annotation, Review

logger = logging.getLogger(__name__)


class BaseAgent:
    """所有专家角色的基类。子类只需实现 get_system_prompt()。"""

    def __init__(self, role_id: str, name: str, expertise: str):
        self.role_id   = role_id
        self.name      = name
        self.expertise = expertise
        self._api_cfg  = self._load_api_cfg()

    def _load_api_cfg(self) -> dict:
        p = Path("configs/api.yaml")
        if p.exists():
            return yaml.safe_load(p.read_text(encoding="utf-8"))
        return {"temperature": 0.3, "max_tokens": 2048}

    def _resolve_model(self) -> Optional[str]:
        """按角色解析专用模型：环境变量 USTC_AGENT_MODEL_{ROLE_ID 大写}。

        未设置时返回 None，call_llm 会回退到通用模型（USTC_LLM_MODEL/api.yaml）。
        这样可视化界面可以为每个专家单独指定模型，不指定则用默认模型，向后兼容。
        """
        env_name = f"USTC_AGENT_MODEL_{self.role_id.upper()}"
        return os.environ.get(env_name) or None

    def get_system_prompt(self) -> str:
        """子类必须实现：返回角色系统提示词"""
        raise NotImplementedError

    def annotate(
        self,
        lesson_text: str,
        profile: dict,
        experiences: list | None = None,
    ) -> list[Annotation]:
        """Round 0：独立批注教案，返回 list[Annotation]"""
        prompt = self._build_annotation_prompt(lesson_text, profile, experiences or [])
        # 批注是整合环节的输入，随机性会被放大成 polished 的差异 → 降温到 0.0 让同一
        # 输入产生接近的批注，是磨课结果可复现的最大杠杆。LLM 在 temp 0 仍非完全确定，
        # 但能把波动从 50~85 压到 ±5 区间附近。可用 api.yaml 的 annotation_temperature 覆盖。
        raw    = call_llm(
            system=self.get_system_prompt(),
            user=prompt,
            temperature=self._api_cfg.get("annotation_temperature", 0.0),
            max_tokens=self._api_cfg.get("max_tokens", 2048),
            model=self._resolve_model(),
        )
        result = parse_json_safe(raw)
        if result is None:
            logger.error(f"[{self.role_id}] 批注解析失败，返回空列表")
            return []
        if isinstance(result, dict):
            result = [result]
        # 补充 role_id 字段
        for ann in result:
            ann.setdefault("in_scope", True)
            ann.setdefault("refer_to", None)
            ann.setdefault("rubric_anchor", None)
            ann["role_id"] = self.role_id
        return result

    def peer_review(
        self,
        lesson_text: str,
        own_r0: list[Annotation],
        others_r0: dict,
        prior_r1: dict | None = None,
    ) -> list[Review]:
        """Round 1：互评（Phase 1 暂不使用）"""
        prompt = self._build_review_prompt(own_r0, others_r0, prior_r1 or {})
        raw    = call_llm(
            system=self.get_system_prompt(),
            user=prompt,
            temperature=self._api_cfg.get("temperature", 0.3),
            max_tokens=self._api_cfg.get("max_tokens", 2048),
            model=self._resolve_model(),
        )
        result = parse_json_safe(raw)
        if result is None:
            return []
        if isinstance(result, dict):
            result = [result]
        for r in result:
            r.setdefault("in_scope", True)
            r.setdefault("refer_to", None)
            r["role_id"] = self.role_id
        return result

    def _build_annotation_prompt(
        self, lesson_text: str, profile: dict, experiences: list
    ) -> str:
        """构建批注提示词（子类可覆盖）"""
        import json
        exp_str = ""
        if experiences:
            exp_str = f"\n【参考经验】\n{json.dumps(experiences[:2], ensure_ascii=False)}\n"
        return (
            f"请对以下{profile.get('subject','')}教案（{profile.get('grade','')}年级）"
            f"进行{self.name}视角批注。{exp_str}\n"
            f"【学情】{profile.get('prior_knowledge','')}\n\n"
            f"【教案】\n{lesson_text}\n\n"
            "输出JSON数组，每条含issue_id/dimension/severity/location/quote/problem/suggestion/in_scope。\n"
            "★suggestion 成品化要求（极重要）：suggestion 字段必须是【可直接替换 quote 原文或直接插入教案的成品文本】，"
            "禁止写'建议补写…''应当…''可以…''需要补充…'这类说明性语言——它会被程序原样写进教案。"
            "若指出的缺陷是'缺失某要素'，suggestion 必须是该要素的完整成品内容（如一条具体的驱动性问题陈述、一段改正后的表述），"
            "程序会在 location 指明的章节处插入它。\n"
            "★location 字段：填写该问题所在、或建议插入处的【现有章节标题原文】（如'项目简介''任务1''项目目标'），"
            "不要写描述性短语（如'驱动性问题缺失'），否则程序无法定位插入位置。"
        )

    def _build_review_prompt(
        self, own_r0: list, others_r0: dict, prior_r1: dict
    ) -> str:
        import json
        return (
            f"以【{self.name}】身份互评以下专家批注。\n"
            f"其他专家批注：{json.dumps(others_r0, ensure_ascii=False)[:1500]}\n"
            "输出JSON数组，每条含refers_to/stance/content："
        )
