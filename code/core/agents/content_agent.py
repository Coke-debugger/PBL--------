"""core/agents/content_agent.py — 学科内容专家（主责C维度）"""
from core.agents.base_agent import BaseAgent

SYSTEM = """你是资深**学科内容专家**，专注教案的知识准确性（量规C维度，权重20分）。

扣分规则（从5分起扣）：
- 重大错误（事实/公式/方程式错误）：-2分/根因
- 次要错误（模糊表述/遗漏条件）：-0.5分/根因
- 格式错误（LaTeX/量纲）：全文-1分

关键规则：
- 同一根因连锁后果合并为1处（例题第1步错导致全错→1个根因）
- 磨课义务是改正确，不是删掉（G2义务）

职责边界：只关注学科事实准确性，语言表达问题转交语言专家(r_design)

输出严格JSON数组，每条含：
{issue_id, dimension("C"), severity, location, quote(≤30字), problem, suggestion, in_scope, rubric_anchor}"""


class ContentAgent(BaseAgent):
    def __init__(self):
        super().__init__(
            role_id="r_content",
            name="学科内容专家",
            expertise="学科概念准确性、公式方程式校验、实验方案安全性",
        )

    def get_system_prompt(self) -> str:
        return SYSTEM

    def _build_annotation_prompt(self, lesson_text, profile, experiences):
        import json
        return (
            f"请校验以下{profile.get('subject','')}教案的知识准确性。\n\n"
            f"【教案】\n{lesson_text}\n\n"
            "找出所有知识性错误（公式/方程式/计算/事实），"
            "输出JSON数组，每条含issue_id/dimension/severity/location/quote/problem/suggestion/in_scope。\n"
            "★suggestion 成品化要求（极重要）：suggestion 必须是【改正后的完整正确表述】（含正确公式/方程式/数据/结论），"
            "程序会用它直接替换 quote 中的错误原文。禁止只写'此处有误''建议修正''应改为…'这类说明性语言——"
            "它会被原样写进教案。例如 quote 是错误结论'酥油比例40%-50%最佳'，suggestion 应写改正后的完整结论句。\n"
            "★location 字段：填写错误所在【现有章节标题原文】（如'任务1''项目目标'），不要写描述性短语。"
        )
