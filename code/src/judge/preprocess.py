"""反评审对抗预处理：评审前剥离教案中的自述性元内容。

附录A第五节'反评审对抗条款'要求：评审输入前，程序移除或标记教案中的
自述性元内容——"设计意图"小节、自带的"目标-活动映射表"、自评分、
任何指向评审行为的语句。此类内容仅作人工参考，不得直接引为给分证据。

规则匹配覆盖不了的极端案例（如巧妙伪装的诱导性语句）留给人工层——
这是附录A原文本身承认的边界，本模块不追求穷尽式检测。
"""

from __future__ import annotations

import re

# 各正则默认作用于整个"标题+其后内容直到下一个同级或更高级标题"的小节块。
_SECTION_PATTERNS = [
    r"#{1,6}\s*设计意图.*?(?=\n#{1,6}\s|\Z)",
    r"#{1,6}\s*自评.*?(?=\n#{1,6}\s|\Z)",
    r"#{1,6}\s*目标[-—－]活动映射表?.*?(?=\n#{1,6}\s|\Z)",
    r"#{1,6}\s*评审说明.*?(?=\n#{1,6}\s|\Z)",
]

_INLINE_PATTERNS = [
    r"【设计意图[：:].*?】",
    r"【自评[：:].*?】",
    r"<!--.*?-->",
]

# 诱导/操纵评审的常见话术（虚构权威背书、评分指令等）。命中即剥离该句，
# 且调用方应将此类命中作为学术不端的复核信号单独记录（附录A规定"经人工
# 确认后按学术不端处理"，程序层不自动定性，只负责标记）。
_MANIPULATION_PATTERNS = [
    r"本设计经[^。\n]{0,30}(教研员|专家|评审)[^。\n]{0,30}(指导|审核|修订)[^。\n]*。",
    r"(请|烦请)?(评审|打分|评分)(时|者)?[^。\n]{0,40}(给出高分|从高|从优|酌情给分)[^。\n]*。",
    r"以上设计[^。\n]{0,20}(完全|充分)符合(量规|评价标准)[^。\n]*。",
]

_ALL_SECTION_RE = re.compile("|".join(_SECTION_PATTERNS), re.DOTALL)
_ALL_INLINE_RE = re.compile("|".join(_INLINE_PATTERNS), re.DOTALL)
_ALL_MANIPULATION_RE = re.compile("|".join(_MANIPULATION_PATTERNS))


def strip_meta_content(lesson_text: str) -> str:
    """移除教案中的自述性元内容，防止面向评审写作。"""
    text = _ALL_SECTION_RE.sub("", lesson_text)
    text = _ALL_INLINE_RE.sub("", text)
    text = _ALL_MANIPULATION_RE.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def detect_manipulation_flags(lesson_text: str) -> list[str]:
    """返回命中的诱导/操纵评审语句列表，供人工复核队列使用。

    不在此处自动判定学术不端——附录A明确要求"经人工确认后"才定性。
    """
    return [m.group(0) for m in _ALL_MANIPULATION_RE.finditer(lesson_text)]
