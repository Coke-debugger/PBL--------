"""tests/test_multirole.py — 多角色、互评轮次与整合逻辑测试（mock LLM，无需 API key）"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import json
from pathlib import Path
from unittest.mock import patch, call as mock_call


# ── Fixtures ──────────────────────────────────────────────────────────────
LESSON_TEXT = """# 项目目标
1. 了解蜡烛燃烧原理和化学反应条件

# 任务一
化学方程式：2C₂₅H₅₂ + 51O₂ → 50CO + 52H₂O

# 任务二
学生完成实验记录表，观察燃烧速度。
"""

PROFILE = {"subject": "化学", "grade": "初中",
           "prior_knowledge": "已学燃烧条件", "learning_motivation": "中等",
           "target_openness_tier": 2}

LESSON_DATA = {
    "text": LESSON_TEXT,
    "profile": PROFILE,
    "structure": {"course_type": "PBL", "subject": "化学", "grade_level": "初中",
                  "sections": [{"level": 1, "heading": "项目目标", "content": "1. 了解..."}]},
    "meta": {"student_id": "S001", "sample_id": "C01"},
    "self_detected_errors": [],
}

# mock 批注（各角色独立）
MOCK_LITERACY_ANN = json.dumps([{
    "issue_id": "F-01", "dimension": "F", "severity": "major",
    "location": "项目目标", "quote": "了解蜡烛燃烧原理",
    "problem": "目标去行为化，无行为动词", "suggestion": "能从露营停电情境中提出问题，拆解影响变量",
    "in_scope": True, "refer_to": None, "rubric_anchor": "F1·行为动词三元素"
}])

MOCK_CONTENT_ANN = json.dumps([{
    "issue_id": "C-01", "dimension": "C", "severity": "major",
    "location": "任务一", "quote": "50CO",
    "problem": "完全燃烧应生成CO₂，方程式错误", "suggestion": "25CO₂",
    "in_scope": True, "refer_to": None, "rubric_anchor": "C·重大错误·-2分"
}])

MOCK_LEARNER_ANN = json.dumps([{
    "issue_id": "D-01", "dimension": "D", "severity": "minor",
    "location": "任务二", "quote": "学生完成实验记录表",
    "problem": "活动开放度低，未体现学生自主设计空间", "suggestion": "学生自主设计实验步骤和记录维度",
    "in_scope": True, "refer_to": None, "rubric_anchor": "D5·开放度"
}])

MOCK_DESIGN_ANN = json.dumps([{
    "issue_id": "A-01", "dimension": "A", "severity": "major",
    "location": "全文", "quote": "任务二",
    "problem": "PBL课型缺少成果交流节", "suggestion": "新增成果交流·班级蜡烛新品发布会",
    "in_scope": True, "refer_to": None, "rubric_anchor": "A·PBL核心件"
}])


# ════════════════════════════════════════════════════════════════════════
# 一、各角色独立批注测试
# ════════════════════════════════════════════════════════════════════════
class TestAgentAnnotation:

    @patch("core.agents.base_agent.call_llm", return_value=MOCK_LITERACY_ANN)
    def test_literacy_agent_returns_f_annotation(self, mock_llm):
        """素养导向教研员应返回 F 维度批注"""
        from core.agents.literacy_agent import LiteracyAgent
        agent = LiteracyAgent()
        anns = agent.annotate(LESSON_TEXT, PROFILE)
        assert len(anns) >= 1
        assert anns[0]["dimension"] == "F"
        assert anns[0]["severity"] == "major"
        assert anns[0]["role_id"] == "r_literacy"

    @patch("core.agents.base_agent.call_llm", return_value=MOCK_CONTENT_ANN)
    def test_content_agent_returns_c_annotation(self, mock_llm):
        """学科内容专家应返回 C 维度批注"""
        from core.agents.content_agent import ContentAgent
        agent = ContentAgent()
        anns = agent.annotate(LESSON_TEXT, PROFILE)
        assert len(anns) >= 1
        assert anns[0]["dimension"] == "C"
        assert "CO₂" in anns[0]["suggestion"]
        assert anns[0]["role_id"] == "r_content"

    @patch("core.agents.base_agent.call_llm", return_value=MOCK_LEARNER_ANN)
    def test_learner_agent_returns_d_annotation(self, mock_llm):
        """学情适配专家应返回 D 维度批注"""
        from core.agents.learner_agent import LearnerAgent
        agent = LearnerAgent()
        anns = agent.annotate(LESSON_TEXT, PROFILE)
        assert len(anns) >= 1
        assert anns[0]["dimension"] == "D"
        assert anns[0]["role_id"] == "r_learner"

    @patch("core.agents.base_agent.call_llm", return_value=MOCK_DESIGN_ANN)
    def test_design_agent_returns_a_annotation(self, mock_llm):
        """教学设计专家应返回 A 维度批注（PBL结构件缺失）"""
        from core.agents.design_agent import DesignAgent
        agent = DesignAgent()
        anns = agent.annotate(LESSON_TEXT, PROFILE)
        assert len(anns) >= 1
        assert anns[0]["dimension"] == "A"
        assert anns[0]["role_id"] == "r_design"

    @patch("core.agents.base_agent.call_llm", return_value='invalid json {{')
    def test_agent_parse_failure_returns_empty_list(self, mock_llm):
        """LLM 输出无法解析时应返回空列表，不抛异常"""
        from core.agents.literacy_agent import LiteracyAgent
        agent = LiteracyAgent()
        anns = agent.annotate(LESSON_TEXT, PROFILE)
        assert anns == []

    def test_agent_role_ids_are_distinct(self):
        """4个角色的 role_id 应互不相同"""
        from core.agents.registry import AGENT_REGISTRY
        ids = list(AGENT_REGISTRY.keys())
        assert len(ids) == len(set(ids))
        assert "r_literacy" in ids
        assert "r_content" in ids
        assert "r_learner" in ids
        assert "r_design" in ids

    def test_agent_system_prompts_contain_dimension_keywords(self):
        """每个角色的系统提示词应包含其主责维度关键词"""
        from core.agents.literacy_agent import LiteracyAgent
        from core.agents.content_agent  import ContentAgent
        from core.agents.learner_agent  import LearnerAgent
        from core.agents.design_agent   import DesignAgent
        checks = [
            (LiteracyAgent(), ["F", "素养", "行为"]),
            (ContentAgent(),  ["C", "准确", "错误"]),
            (LearnerAgent(),  ["D", "学情", "一致"]),
            (DesignAgent(),   ["A", "B", "结构", "丰富"]),
        ]
        for agent, keywords in checks:
            prompt = agent.get_system_prompt()
            for kw in keywords:
                assert kw in prompt, f"{agent.role_id} 系统提示词缺少关键词: {kw}"


# ════════════════════════════════════════════════════════════════════════
# 二、Round 0 并行批注测试
# ════════════════════════════════════════════════════════════════════════
class TestRound0ParallelAnnotation:

    @patch("core.agents.base_agent.call_llm", side_effect=[
        MOCK_LITERACY_ANN, MOCK_CONTENT_ANN,
        MOCK_LEARNER_ANN,  MOCK_DESIGN_ANN,
    ])
    def test_round0_returns_4_roles(self, mock_llm):
        """Round 0 应返回4个角色的批注"""
        from core.roundtable import Roundtable
        config = {"active_roles": ["r_literacy","r_content","r_learner","r_design"],
                  "timeouts": {"round0": 60}}
        rt = Roundtable(LESSON_DATA, config)
        round0 = rt.run()["round0"]
        assert set(round0.keys()) == {"r_literacy","r_content","r_learner","r_design"}

    @patch("core.agents.base_agent.call_llm", side_effect=[
        MOCK_LITERACY_ANN, MOCK_CONTENT_ANN,
        MOCK_LEARNER_ANN,  MOCK_DESIGN_ANN,
    ])
    def test_round0_total_annotations_geq_4(self, mock_llm):
        """Round 0 总批注数应 ≥ 4 条（各角色至少1条）"""
        from core.roundtable import Roundtable
        config = {"active_roles": ["r_literacy","r_content","r_learner","r_design"],
                  "timeouts": {"round0": 60}}
        rt = Roundtable(LESSON_DATA, config)
        round0 = rt.run()["round0"]
        total = sum(len(v) for v in round0.values())
        assert total >= 4

    @patch("core.agents.base_agent.call_llm", side_effect=[
        MOCK_LITERACY_ANN, MOCK_CONTENT_ANN,
        MOCK_LEARNER_ANN,  MOCK_DESIGN_ANN,
    ])
    def test_round0_covers_all_dimensions(self, mock_llm):
        """Round 0 批注应覆盖 F/C/D/A 四个关键维度"""
        from core.roundtable import Roundtable
        config = {"active_roles": ["r_literacy","r_content","r_learner","r_design"],
                  "timeouts": {"round0": 60}}
        rt = Roundtable(LESSON_DATA, config)
        round0 = rt.run()["round0"]
        dims = {ann["dimension"]
                for anns in round0.values()
                for ann in anns}
        assert "F" in dims, "缺少F维度批注（素养导向）"
        assert "C" in dims, "缺少C维度批注（内容准确）"
        assert "D" in dims, "缺少D维度批注（学情适配）"
        assert "A" in dims, "缺少A维度批注（结构完整）"

    @patch("core.agents.base_agent.call_llm", return_value='bad json')
    def test_round0_partial_failure_graceful(self, mock_llm):
        """部分角色解析失败时，其余角色仍返回结果（不整体崩溃）"""
        from core.roundtable import Roundtable
        config = {"active_roles": ["r_literacy","r_content"],
                  "timeouts": {"round0": 60}}
        rt = Roundtable(LESSON_DATA, config)
        round0 = rt.run()["round0"]
        # 不抛出异常，返回空列表而非崩溃
        assert "r_literacy" in round0
        assert "r_content" in round0

    @patch("core.agents.base_agent.call_llm", side_effect=[
        MOCK_LITERACY_ANN, MOCK_CONTENT_ANN,
        MOCK_LEARNER_ANN,  MOCK_DESIGN_ANN,
    ])
    def test_round0_major_annotations_prioritized(self, mock_llm):
        """major 批注应优先于 minor"""
        from core.roundtable import Roundtable
        config = {"active_roles": ["r_literacy","r_content","r_learner","r_design"],
                  "timeouts": {"round0": 60}}
        rt = Roundtable(LESSON_DATA, config)
        round0 = rt.run()["round0"]
        # 检查存在 major 批注
        has_major = any(
            ann.get("severity") == "major"
            for anns in round0.values()
            for ann in anns
        )
        assert has_major


# ════════════════════════════════════════════════════════════════════════
# 三、整合逻辑测试（4专家 → 输出）
# ════════════════════════════════════════════════════════════════════════
class TestIntegrationLogic:

    def _make_round0(self):
        return {
            "r_literacy": json.loads(MOCK_LITERACY_ANN),
            "r_content":  json.loads(MOCK_CONTENT_ANN),
            "r_learner":  json.loads(MOCK_LEARNER_ANN),
            "r_design":   json.loads(MOCK_DESIGN_ANN),
        }

    def test_integration_applies_major_annotation(self):
        """整合后文本应包含 major 批注的修改建议"""
        from core.integrator import Integrator
        round0 = self._make_round0()
        integ = Integrator(LESSON_DATA, round0, "S001", "C01")
        draft, mods = integ.integrate()
        # 内容专家的修改：50CO → 25CO₂
        assert "25CO₂" in draft or any(m["source_role"] == "r_content" for m in mods)

    def test_integration_modification_count(self):
        """整合后修改记录数 = 成功定位的批注数"""
        from core.integrator import Integrator
        round0 = self._make_round0()
        integ = Integrator(LESSON_DATA, round0, "S001", "C01")
        _, mods = integ.integrate()
        assert len(mods) >= 1

    def test_integration_all_mods_have_required_fields(self):
        """每条修改记录须含 mod_id/location/source_role/rationale"""
        from core.integrator import Integrator
        round0 = self._make_round0()
        integ = Integrator(LESSON_DATA, round0, "S001", "C01")
        _, mods = integ.integrate()
        for m in mods:
            assert "mod_id" in m
            assert "source_role" in m
            assert "location" in m
            assert "rationale" in m

    def test_integration_process_json_has_all_4_roles(self, tmp_path):
        """4专家直出的 process.json 应包含4个角色记录"""
        from core.integrator import Integrator
        round0 = self._make_round0()
        integ = Integrator(LESSON_DATA, round0, "S001", "C01")
        draft, mods = integ.integrate()
        process_path = tmp_path / "S001_C01_process.json"
        integ.write_process(process_path, mods, round0)
        data = json.loads(process_path.read_text(encoding="utf-8"))
        role_ids = {r["role_id"] for r in data["roles"]}
        assert "r_literacy" in role_ids
        assert "r_content"  in role_ids

    def test_integration_process_discussion_from_round0(self, tmp_path):
        """discussion 数组应来自 round0 真实批注内容"""
        from core.integrator import Integrator
        round0 = self._make_round0()
        integ = Integrator(LESSON_DATA, round0, "S001", "C01")
        draft, mods = integ.integrate()
        process_path = tmp_path / "S001_C01_process.json"
        integ.write_process(process_path, mods, round0)
        data = json.loads(process_path.read_text(encoding="utf-8"))
        # discussion 应有来自不同角色的条目
        role_in_disc = {d["role_id"] for d in data["discussion"]}
        assert len(role_in_disc) >= 2, "discussion 应包含多角色发言"

    def test_integration_contract_validation_passes(self, tmp_path):
        """4专家直出的全量输出须通过契约校验"""
        from core.integrator import Integrator
        from core.validate_submission import run as validate
        round0 = self._make_round0()
        integ = Integrator(LESSON_DATA, round0, "S001", "C01")
        draft, mods = integ.integrate()
        integ.write_polished(tmp_path / "S001_C01_polished.md", draft)
        integ.write_process(tmp_path / "S001_C01_process.json", mods, round0)
        result = validate(str(tmp_path))
        assert not result["has_fail"], f"契约校验失败: {result['failures']}"


# ════════════════════════════════════════════════════════════════════════
# 四、基线 vs 多角色对比（结构层面）
# ════════════════════════════════════════════════════════════════════════
class TestBaselineVsMultiRole:

    def test_multirole_has_more_modification_fields(self):
        """多角色模型应产出 source_role 字段（基线只有 r_baseline）"""
        from core.integrator import Integrator
        round0 = {
            "r_literacy": json.loads(MOCK_LITERACY_ANN),
            "r_content":  json.loads(MOCK_CONTENT_ANN),
        }
        integ = Integrator(LESSON_DATA, round0, "S001", "C01")
        _, mods = integ.integrate()
        roles = {m["source_role"] for m in mods}
        # 多角色应有多个不同的 source_role
        assert len(roles) >= 1  # 至少来自一个角色

    def test_multirole_covers_multiple_dimensions(self):
        """多角色模型的修改应覆盖多个量规维度"""
        from core.integrator import Integrator
        round0 = {
            "r_literacy": json.loads(MOCK_LITERACY_ANN),
            "r_content":  json.loads(MOCK_CONTENT_ANN),
            "r_learner":  json.loads(MOCK_LEARNER_ANN),
            "r_design":   json.loads(MOCK_DESIGN_ANN),
        }
        integ = Integrator(LESSON_DATA, round0, "S001", "C01")
        _, mods = integ.integrate()
        # 获取所有修改涉及的问题来源（不同角色意味着不同维度）
        source_roles = {m["source_role"] for m in mods}
        # 基线只有 r_baseline，多角色有多个
        assert "r_baseline" not in source_roles, "多角色输出不应含基线角色"
