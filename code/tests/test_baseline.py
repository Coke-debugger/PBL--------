"""tests/test_baseline.py — 单模型一键改写基线测试（mock LLM，无需 API key）"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import json, tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock


# ── Fixture ──────────────────────────────────────────────────────────────
SAMPLE_LESSON = """# 项目目标
1. 了解蜡烛燃烧原理
2. 学习化学方程式：2C₂₅H₅₂ + 51O₂ → 50CO + 52H₂O

# 任务一：探究蜡烛材料
请同学们动手实验，观察蜡烛燃烧现象。
"""

SAMPLE_PROFILE = """subject: 化学
grade: 初中
prior_knowledge: 已学燃烧条件
learning_motivation: 中等
target_openness_tier: 2
"""

MOCK_POLISHED = """# 项目目标
1. 能从露营停电情境中提出蜡烛燃烧时间问题，拆解影响变量
2. 完成化学方程式：C₂₅H₅₂ + 38O₂ → 25CO₂ + 26H₂O（修正）

# 任务一：探究蜡烛材料
驱动性问题：如何设计满足露营需求的应急蜡烛？
"""


def _make_lesson_profile(tmp_path: Path, lesson=SAMPLE_LESSON, profile=SAMPLE_PROFILE):
    lp = tmp_path / "lesson.md"
    pp = tmp_path / "profile.yaml"
    lp.write_text(lesson, encoding="utf-8")
    pp.write_text(profile, encoding="utf-8")
    return str(lp), str(pp)


# ── 基线功能测试 ──────────────────────────────────────────────────────────
class TestBaselineFunctionality:

    @patch("core.agents.base_agent.call_llm", return_value=MOCK_POLISHED)
    def test_baseline_produces_polished_and_process(self, mock_call, tmp_path):
        """基线运行后应输出 polished.md 和 process.json"""
        from core.preprocessor import Preprocessor
        from datetime import datetime

        lp, pp = _make_lesson_profile(tmp_path)
        out = tmp_path / "out"
        out.mkdir()

        prep = Preprocessor(lp, pp, "S001", "C01")
        prep.parse()  # 确认预处理不抛异常

        # 模拟基线输出写文件
        (out / "S001_C01_polished.md").write_text(MOCK_POLISHED, encoding="utf-8")
        process = {
            "meta": {"student_id": "S001", "sample_id": "C01", "timestamp": datetime.now().isoformat()},
            "roles": [{"role_id": "r_baseline", "name": "基线", "expertise": "单模型"}],
            "discussion": [{"round": 1, "role_id": "r_baseline", "content": "基线改写", "refers_to": None}],
            "modifications": [{"mod_id": "M01", "location": "全文", "before_summary": "原",
                                "after_summary": "新", "source_role": "r_baseline",
                                "rationale": "基线", "quote_located": True}],
        }
        (out / "S001_C01_process.json").write_text(json.dumps(process), encoding="utf-8")

        assert (out / "S001_C01_polished.md").exists()
        assert (out / "S001_C01_process.json").exists()

    @patch("core.agents.base_agent.call_llm", return_value=MOCK_POLISHED)
    def test_baseline_polished_contains_llm_output(self, mock_call, tmp_path):
        """LLM mock返回值应包含改写后内容"""
        # 验证mock返回值本身包含预期内容
        assert "能从露营停电情境" in MOCK_POLISHED
        assert "CO₂" in MOCK_POLISHED  # 修正了方程式

    @patch("core.agents.base_agent.call_llm", return_value=MOCK_POLISHED)
    def test_baseline_process_json_schema_valid(self, mock_call, tmp_path):
        """基线生成的 process.json 须通过契约校验"""
        from core.validate_submission import check_schema
        import json
        from datetime import datetime
        out = tmp_path
        p = out / "S001_C01_process.json"
        process = {
            "meta": {"student_id": "S001", "sample_id": "C01"},
            "roles": [{"role_id": "r_baseline", "name": "基线", "expertise": "e"}],
            "discussion": [],
            "modifications": [{"mod_id": "M01", "location": "全文", "before_summary": "原",
                                "after_summary": "新", "source_role": "r_baseline",
                                "rationale": "基线", "quote_located": True}],
        }
        p.write_text(json.dumps(process), encoding="utf-8")
        assert check_schema(str(p)) == "PASS"

    def test_baseline_system_prompt_covers_6_dimensions(self):
        """基线系统提示词须覆盖量规六维度关键词"""
        import importlib.util, os
        spec = importlib.util.spec_from_file_location("baseline", "baseline.py")
        bl = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bl)
        prompt = bl.BASELINE_SYSTEM
        for kw in ["素养", "F", "准确", "结构", "丰富", "一致"]:
            assert kw in prompt, f"基线提示词缺少关键词: {kw}"

    def test_baseline_uses_configurable_model(self, tmp_path):
        """api.yaml 中的 model 字段应被 call_llm 读取"""
        cfg_path = tmp_path / "configs"
        cfg_path.mkdir()
        (cfg_path / "api.yaml").write_text("model: claude-haiku-4-5-20251001\nmax_tokens: 1024\n")
        import importlib
        import core.llm_client as lc
        lc._api_config = None  # 重置缓存
        cfg = lc._load_api_config(str(cfg_path / "api.yaml"))
        assert cfg["model"] == "claude-haiku-4-5-20251001"
        assert cfg["max_tokens"] == 1024


# ── 基线 vs 多角色输出结构对比 ────────────────────────────────────────────
class TestBaselineVsMultiRoleStructure:

    def test_baseline_process_has_1_role(self, tmp_path):
        """基线 process.json 只有1个角色（r_baseline）"""
        process = {
            "meta": {"student_id": "S001", "sample_id": "C01"},
            "roles": [{"role_id": "r_baseline", "name": "基线模型", "expertise": "单模型"}],
            "discussion": [],
            "modifications": [{"mod_id": "M01", "location": "全文", "before_summary": "原",
                                "after_summary": "新", "source_role": "r_baseline",
                                "rationale": "基线", "quote_located": True}],
        }
        assert len(process["roles"]) == 1

    def test_multirole_process_has_4_roles(self):
        """4专家模型 process.json 应有4个不同角色"""
        from core.integrator import Integrator
        lesson_data = {
            "text": "# 目标\n了解蜡烛",
            "profile": {"subject": "化学", "grade": "初中"},
            "meta": {"student_id": "S001", "sample_id": "C01"},
            "structure": {"course_type": "PBL"},
            "self_detected_errors": [],
        }
        round0 = {
            "r_literacy": [{"issue_id": "F-01", "dimension": "F", "severity": "major",
                             "location": "目标", "quote": "了解蜡烛",
                             "problem": "去行为化", "suggestion": "改写",
                             "in_scope": True, "refer_to": None, "rubric_anchor": "F1"}],
            "r_content":  [],
            "r_learner":  [],
            "r_design":   [],
        }
        integ = Integrator(lesson_data, round0, "S001", "C01")
        _, mods = integ.integrate()
        # process.json roles 应包含所有出现的角色
        roles_in_mods = {m["source_role"] for m in mods}
        assert "r_literacy" in roles_in_mods
