"""core/agents/registry.py — 角色注册表，按名字启停，支持消融"""
from core.agents.literacy_agent import LiteracyAgent
from core.agents.content_agent  import ContentAgent
from core.agents.learner_agent  import LearnerAgent
from core.agents.design_agent   import DesignAgent
from core.agents.chair_agent    import ChairAgent

AGENT_REGISTRY: dict[str, type] = {
    "r_literacy": LiteracyAgent,
    "r_content":  ContentAgent,
    "r_learner":  LearnerAgent,
    "r_design":   DesignAgent,
    "r_chair":    ChairAgent,
}


def build_agents(active_roles: list[str]) -> dict:
    """按 PipelineConfig.active_roles 实例化（不含 r_chair，Chair 单独实例化）"""
    return {
        rid: AGENT_REGISTRY[rid]()
        for rid in active_roles
        if rid in AGENT_REGISTRY and rid != "r_chair"
    }
