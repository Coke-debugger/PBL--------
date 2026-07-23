"""core/preprocessor.py — 输入解析：教案 Markdown + 学情 YAML → lesson_data"""
from __future__ import annotations
import re
import logging
from pathlib import Path
from typing import Optional

import yaml
from core.types import Profile

logger = logging.getLogger(__name__)


class LaTeXError(ValueError):
    pass


class Preprocessor:
    def __init__(
        self,
        lesson_path: str,
        profile_path: str,
        student_id: str = "STU001",
        sample_id: str = "SAMPLE01",
    ):
        self.lesson_path  = Path(lesson_path)
        self.profile_path = Path(profile_path)
        self.student_id   = student_id
        self.sample_id    = sample_id

    def parse(self) -> dict:
        """解析输入文件，返回 lesson_data 字典"""
        text    = self.lesson_path.read_text(encoding="utf-8")
        profile = yaml.safe_load(self.profile_path.read_text(encoding="utf-8"))

        # 补齐 Profile 默认值
        profile.setdefault("target_openness_tier", 2)
        profile.setdefault("learning_motivation", "中等")
        profile.setdefault("prior_knowledge", "未知")

        # 契约预检：LaTeX 定界符
        self._check_latex(text)

        sections    = self._extract_sections(text)
        course_type = self._detect_course_type(text)
        errors      = self._detect_obvious_errors(text, profile)

        return {
            "text":    text,
            "profile": profile,
            "structure": {
                "sections":    sections,
                "course_type": course_type,
                "subject":     profile.get("subject", ""),
                "grade_level": profile.get("grade", ""),
            },
            "meta": {
                "student_id": self.student_id,
                "sample_id":  self.sample_id,
            },
            "self_detected_errors": errors,
        }

    # ── 内部方法 ──────────────────────────────────────────────────

    def _check_latex(self, text: str) -> None:
        """LaTeX $定界符奇偶校验（排除代码块和转义）"""
        # 移除代码块
        clean = re.sub(r"```[\s\S]*?```", "", text)
        clean = re.sub(r"`[^`]*`", "", clean)
        # 计算未转义的 $
        dollars = re.findall(r"(?<!\\)\$", clean)
        if len(dollars) % 2 != 0:
            raise LaTeXError(
                f"LaTeX $ 定界符为奇数（{len(dollars)}个），请修复后重提交"
            )

    def _extract_sections(self, text: str) -> list[dict]:
        """提取标题树：[{"level":int, "heading":str, "content":str}]"""
        sections = []
        current  = {"level": 0, "heading": "序言", "content": ""}
        for line in text.split("\n"):
            m = re.match(r"^(#{1,6})\s+(.*)", line)
            if m:
                if current["content"].strip() or current["heading"] != "序言":
                    sections.append(current)
                current = {"level": len(m.group(1)), "heading": m.group(2).strip(), "content": ""}
            else:
                current["content"] += line + "\n"
        sections.append(current)
        return sections

    def _detect_course_type(self, text: str) -> str:
        pbl_keywords = ["项目", "驱动性问题", "成果交流", "任务链", "PBL", "项目化"]
        if any(kw in text for kw in pbl_keywords):
            return "PBL"
        return "常规课"

    def _detect_obvious_errors(self, text: str, profile: dict) -> list[dict]:
        """简单自检：找明显缺陷（穷人版 G2 用）"""
        errors = []
        # 检测空目标节
        if re.search(r"(教学目标|学习目标|项目目标)[\s\S]{0,50}\n\n", text):
            errors.append({
                "location": "目标节",
                "quote": "",
                "error_type": "minor",
                "root_cause": "目标节内容过短或为空",
            })
        return errors
