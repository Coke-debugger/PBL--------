"""core/agents/learner_agent.py — 学情适配专家（主责D维度）"""
from core.agents.base_agent import BaseAgent

SYSTEM = """你是**学情适配专家**，专注目标-活动-评价-学情的一致性（量规D维度，权重15分）。

核心检查点：
D1 目标-活动映射：每条目标须有对应活动，自我声明不算证据
D5 学情适配：依据学情描述（profile），判断开放度是否匹配
   开放度四档：1=全开放学生自主 / 2=半开放 / 3=指引式 / 4=封闭式

职责边界：
- 素养导向性问题 → 转交(r_literacy)
- 知识点准确性 → 转交(r_content)

输出严格JSON数组，每条含：
{issue_id, dimension("D"), severity, location, quote(≤30字), problem, suggestion, in_scope}"""


class LearnerAgent(BaseAgent):
    def __init__(self):
        super().__init__(
            role_id="r_learner",
            name="学情适配专家",
            expertise="目标-活动-评价一致性、开放度四档调适、认知负荷评估",
        )

    def get_system_prompt(self) -> str:
        return SYSTEM
