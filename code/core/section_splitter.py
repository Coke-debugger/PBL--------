"""教案章节切分 / 定位 / 替换工具——分段聚焦改写的基础。

设计目的：磨课重构前，专家要通读 3 万字全文才能批注，注意力被稀释导致幻觉
（把"修改说明"和成品内容混在一起）。改为"Judge 指出问题 → 专家只改问题所在
那一段"后，需要能：①把教案按 markdown 标题切成章节；②给定一条问题的原文引用
quote，找到它属于哪个章节；③把某章节的正文替换为专家改写的新内容，其余章节
原样保留。本模块提供这三个能力，纯文本处理，不依赖 LLM。
"""
from __future__ import annotations

import re
import unicodedata


def _normalize(text: str) -> str:
    """全角转半角，压缩空白。与 Integrator._normalize 同口径，保证 quote 定位一致。"""
    text = unicodedata.normalize("NFKC", text)
    return re.sub(r"\s+", " ", text)


def split_into_sections(text: str) -> list[dict]:
    """按 markdown 标题（#{1,6}）把教案切成章节列表。

    返回 [{"heading","level","content","start","end"}]：
    - heading: 标题文字（不含 # 前缀）
    - level:   标题层级（1~6）；首个无标题的引导段记为 level 0、heading "序言"
    - content: 该章节标题之后、到下一同级或更高级标题之前的正文（不含标题行）
    - start/end: 该章节【含标题行】在原文中的字符偏移，供 replace_section 精确替换

    无标题的纯文本教案会退化为一整个"序言"章节，调用方据此回退到全文处理。
    """
    sections: list[dict] = []
    # 用 finditer 拿到每个标题的位置，再据此切分，保证 start/end 偏移精确。
    heading_re = re.compile(r"^(#{1,6})\s+(.*)$", re.MULTILINE)
    matches = list(heading_re.finditer(text))

    if not matches:
        return [{
            "heading": "序言", "level": 0, "content": text,
            "start": 0, "end": len(text),
        }]

    # 第一个标题之前的引导段（若非空）作为"序言"章节
    first_start = matches[0].start()
    if first_start > 0 and text[:first_start].strip():
        sections.append({
            "heading": "序言", "level": 0, "content": text[:first_start],
            "start": 0, "end": first_start,
        })

    for i, m in enumerate(matches):
        level = len(m.group(1))
        heading = m.group(2).strip()
        body_start = m.end()  # 标题行之后
        # 该章节正文结束于：下一个标题的起点；没有下一个则到文末
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        content = text[body_start:body_end]
        sections.append({
            "heading": heading, "level": level, "content": content,
            "start": m.start(), "end": body_end,
        })
    return sections


def locate_section_for_quote(sections: list[dict], quote: str) -> dict | None:
    """给定一条问题的原文引用 quote，返回它所在的章节 dict（找不到返回 None）。

    宽容匹配：先归一化后逐字子串命中；命中不到再取 quote 中最长连续片段模糊匹配
    （复用 sampling._quote_matches 的思路），容忍专家改写引用。优先在 content 里
    找；找不到才回退到整个章节（含标题）。
    """
    if not quote:
        return None
    norm_quote = _normalize(quote)
    if not norm_quote:
        return None

    # 1. 归一化逐字子串命中（最可靠）
    for sec in sections:
        if norm_quote in _normalize(sec["content"]):
            return sec
    # 2. 模糊：取最长连续片段（≥5字）在 content 里搜
    window = 5
    if len(norm_quote) >= window:
        chunks = [norm_quote[i:i + window] for i in range(0, len(norm_quote) - window + 1)]
        for sec in sections:
            norm_content = _normalize(sec["content"])
            if any(c in norm_content for c in chunks):
                return sec
    # 3. 退化：quote 极短时整章节（含标题）匹配
    for sec in sections:
        if norm_quote in _normalize(text_of_section(sec)):
            return sec
    return None


def text_of_section(sec: dict) -> str:
    """章节的完整文本（标题行 + 正文），用于退化匹配。"""
    if sec["level"] > 0:
        return f"{'#' * sec['level']} {sec['heading']}\n{sec['content']}"
    return sec["content"]


def replace_section(text: str, sec: dict, new_content: str) -> str:
    """把章节 sec 的【正文】替换为 new_content，保留标题行，其余原文不动。

    用 sec 的 start/end 偏移做精确切片：保留 [0,start) 的标题行（其实是 start 指向
    标题行起点，这里替换的是 [start,end) 整段含标题——见下方处理）。实际为保持标题
    不被改写，单独保留标题行，只换标题之后的正文。

    返回替换后的完整文本。new_content 为空时等价于删除该章节正文（保留标题）。
    """
    if sec["level"] > 0:
        # 有标题：保留 "# heading\n" 这一行，替换其后的正文
        # 找标题行的结束位置（第一个 \n 之后）
        heading_line_end = text.find("\n", sec["start"])
        if heading_line_end == -1:
            heading_line_end = len(text)
        # 正文区间 = (heading_line_end, end)，含其后的空行也一并替换以保持整洁
        body_start = heading_line_end + 1 if heading_line_end < len(text) else len(text)
        # 新内容末尾保证以空行收尾，避免和下一个标题挤在同一行
        tail = new_content if new_content.endswith("\n\n") else (
            new_content.rstrip("\n") + "\n\n"
        )
        return text[:body_start] + tail + text[sec["end"]:]
    # 无标题的序言段：直接替换 [start,end)
    return text[:sec["start"]] + new_content + text[sec["end"]:]
