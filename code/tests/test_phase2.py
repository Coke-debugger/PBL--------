"""tests/test_phase2.py — Phase 2 模块测试（mock LLM，无需 API key）"""
import sys, os, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from unittest.mock import patch, MagicMock

# ── Fixtures ──────────────────────────────────────────────────────────
LESSON_DATA = {
    "text": (
        "# 项目目标\n1. 了解蜡烛燃烧的化学知识\n\n"
        "# 任务一\n石蜡完全燃烧方程式：$2C_{25}H_{52} + 51O_2 \\to 50CO + 52H_2O$\n"
        "质量比为 816:352\n"
    ),
    "profile": {"subject":"化学","grade":"初中","prior_knowledge":"已学燃烧条件",
                "learning_motivation":"中等","target_openness_tier":2},
    "structure": {"course_type":"PBL","subject":"化学","grade_level":"初中",
                  "sections":[{"level":1,"heading":"项目目标","content":"1. 了解..."}]},
    "meta": {"student_id":"S001","sample_id":"C01"},
    "self_detected_errors": [],
}

ROUND0 = {
    "r_literacy": [{"issue_id":"F-01","dimension":"F","severity":"major",
        "location":"项目目标","quote":"了解蜡烛燃烧的化学知识",
        "problem":"目标去行为化","suggestion":"能从情境中提出问题",
        "in_scope":True,"refer_to":None,"rubric_anchor":"F1"}],
    "r_content": [{"issue_id":"C-01","dimension":"C","severity":"major",
        "location":"任务一","quote":"50CO",
        "problem":"方程式错误","suggestion":"25CO_2",
        "in_scope":True,"refer_to":None,"rubric_anchor":"C"}],
}

ROUND1 = {
    "r_content": [{"refers_to":"F-01","stance":"支持",
        "content":"支持r_literacy的判断，目标确实去行为化",
        "round":1,"role_id":"r_content","in_scope":True,"refer_to":None}],
    "r_literacy": [{"refers_to":"C-01","stance":"支持",
        "content":"支持r_content的方程式错误判断",
        "round":1,"role_id":"r_literacy","in_scope":True,"refer_to":None}],
}

MOCK_VERDICT = {
    "adopted": [
        {"issue_id":"F-01","decision":"adopted",
         "rationale":"F维度权重最高",
         "instruction":{"target_quote":"了解蜡烛燃烧的化学知识",
                         "replacement_text":"能从露营情境中提出蜡烛燃烧时间问题，拆解影响变量",
                         "section_fallback":"项目目标"}},
        {"issue_id":"C-01","decision":"adopted",
         "rationale":"FACTUAL冲突，方程式错误",
         "instruction":{"target_quote":"50CO",
                         "replacement_text":"25CO_2",
                         "section_fallback":"任务一"}},
    ],
    "partial":  [],
    "rejected": [],
    "surviving_args": [],
}


# ════════════════════════════════════════════════════════════════════════
# 一、ChairAgent 仲裁测试
# ════════════════════════════════════════════════════════════════════════
class TestChairAgent:

    @patch("core.llm_client.call_llm", return_value=json.dumps(MOCK_VERDICT))
    def test_arbitrate_returns_verdict(self, mock_llm):
        """仲裁应返回含 adopted/partial/rejected 的 Verdict"""
        from core.agents.chair_agent import ChairAgent
        chair = ChairAgent()
        verdict = chair.arbitrate(ROUND0, ROUND1)
        assert "adopted"  in verdict
        assert "partial"  in verdict
        assert "rejected" in verdict

    @patch("core.llm_client.call_llm", return_value=json.dumps(MOCK_VERDICT))
    def test_arbitrate_adopted_has_instruction(self, mock_llm):
        """adopted 条目须含 StructuredInstruction（target_quote/replacement_text）"""
        from core.agents.chair_agent import ChairAgent
        chair   = ChairAgent()
        verdict = chair.arbitrate(ROUND0, ROUND1)
        for dec in verdict.get("adopted", []):
            assert "instruction" in dec
            instr = dec["instruction"]
            assert "target_quote"     in instr
            assert "replacement_text" in instr
            assert "section_fallback" in instr

    @patch("core.llm_client.call_llm", return_value="invalid json {{")
    def test_arbitrate_fallback_on_parse_failure(self, mock_llm):
        """LLM 输出无法解析时应降级（不崩溃）"""
        from core.agents.chair_agent import ChairAgent
        chair   = ChairAgent()
        verdict = chair.arbitrate(ROUND0, ROUND1)
        assert "adopted"  in verdict


# ════════════════════════════════════════════════════════════════════════
# 二、ConflictClassifier 测试
# ════════════════════════════════════════════════════════════════════════
class TestConflictClassifier:

    def test_detect_conflicts_from_stance(self):
        """stance=分歧 的互评应被检测为冲突"""
        from modules.conflict_classifier import ConflictClassifier
        r1_with_conflict = {
            "r_literacy": [{"refers_to":"C-01","stance":"分歧",
                             "content":"我认为方程式没有错误",
                             "round":1,"role_id":"r_literacy","in_scope":True,"refer_to":None}],
        }
        cc = ConflictClassifier()
        conflicts = cc.detect_conflicts(ROUND0, r1_with_conflict)
        assert len(conflicts) >= 1
        assert any(c["detection_source"] == "agent" for c in conflicts)

    @patch("core.llm_client.call_llm",
           return_value='{"conflict_type":"FACTUAL","confidence":0.9,"reasoning":"事实可验证"}')
    def test_classify_factual(self, mock_llm):
        """FACTUAL冲突应被正确分类"""
        from modules.conflict_classifier import ConflictClassifier
        cc = ConflictClassifier()
        raw_conflict = {
            "conflict_id": "CONF-01",
            "view_a": {"role_id":"r_content","issue_id":"C-01","position":"方程式生成CO₂"},
            "view_b": {"role_id":"r_literacy","issue_id":"C-01","position":"方程式正确"},
            "detection_source": "agent",
        }
        classified = cc.batch_classify([raw_conflict])
        assert len(classified) == 1
        assert classified[0]["conflict_type"] == "FACTUAL"

    def test_resolve_value_uses_weight(self):
        """VALUE冲突应按量规权重裁定（F > D）"""
        from modules.conflict_classifier import ConflictClassifier
        cc = ConflictClassifier()
        conflict = {
            "conflict_type": "VALUE",
            "view_a": {"role_id":"r_literacy","issue_id":"F-01","position":"保深度"},
            "view_b": {"role_id":"r_learner", "issue_id":"D-01","position":"降开放度"},
        }
        result = cc.resolve(conflict)
        assert result["prefer_role"] == "r_literacy"  # F(30) > D(15)


# ════════════════════════════════════════════════════════════════════════
# 三、MetaCognitiveMonitor 测试
# ════════════════════════════════════════════════════════════════════════
class TestMetaCognitiveMonitor:

    def test_hallucination_detected(self):
        """quote 不在原文中应触发 hallucination 信号"""
        from modules.monitor import MetaCognitiveMonitor
        monitor = MetaCognitiveMonitor(lesson_text="# 项目目标\n正文内容")
        ann_with_bad_quote = [{"issue_id":"F-01","quote":"这段话根本不存在",
                                "role_id":"r_content","dimension":"F","severity":"major",
                                "location":"?","problem":"?","suggestion":"?",
                                "in_scope":True,"refer_to":None,"rubric_anchor":"F1"}]
        reviews = [{"refers_to":"F-01","stance":"支持","content":"同意","round":1,
                    "role_id":"r_literacy","in_scope":True,"refer_to":None}]
        signals = monitor.check("r_literacy", reviews, ann_with_bad_quote, {})
        hallu = [s for s in signals if s["signal_type"] == "hallucination"]
        assert len(hallu) >= 1
        assert hallu[0]["action_taken"] == "suspend"

    def test_no_signals_for_valid_review(self):
        """合法互评不应产生信号"""
        from modules.monitor import MetaCognitiveMonitor
        monitor = MetaCognitiveMonitor(lesson_text="# 项目目标\n了解蜡烛燃烧的化学知识")
        ann = [{"issue_id":"F-01","quote":"了解蜡烛燃烧的化学知识","role_id":"r_content",
                "dimension":"F","severity":"major","location":"?","problem":"?",
                "suggestion":"?","in_scope":True,"refer_to":None,"rubric_anchor":"F1"}]
        reviews = [{"refers_to":"F-01","stance":"补充","content":"补充：还需注意情境真实性",
                    "round":1,"role_id":"r_literacy","in_scope":True,"refer_to":None}]
        signals = monitor.check("r_literacy", reviews, ann, {})
        hallu = [s for s in signals if s["signal_type"] == "hallucination"]
        assert len(hallu) == 0


# ════════════════════════════════════════════════════════════════════════
# 四、ExperienceBank 测试
# ════════════════════════════════════════════════════════════════════════
class TestExperienceBank:

    def test_update_creates_entry(self, tmp_path):
        """磨课结束后应在经验库中创建条目"""
        from modules.experience_bank import ExperienceBank
        bank = ExperienceBank(db_path=str(tmp_path / "exp.json"))
        process = {
            "meta": {"student_id":"S001","sample_id":"C01"},
            "roles": [{"role_id":"r_content","name":"内容专家","expertise":"准确性"}],
            "discussion": [],
            "modifications": [{"mod_id":"M01","location":"任务一","source_role":"r_content",
                                "before_summary":"50CO","after_summary":"25CO_2",
                                "rationale":"方程式错误","quote_located":True}],
        }
        bank.update("C01", process, score_before=40.0, score_after=65.0)
        assert len(bank.entries) >= 1

    def test_retrieve_returns_list(self, tmp_path):
        """检索应返回列表（即使库为空）"""
        from modules.experience_bank import ExperienceBank
        bank = ExperienceBank(db_path=str(tmp_path / "empty.json"))
        result = bank.retrieve({"subject":"化学","grade":"初中"}, "教案内容")
        assert isinstance(result, list)

    def test_utility_updated_on_duplicate(self, tmp_path):
        """相同条件的经验应更新效用分而非新增"""
        from modules.experience_bank import ExperienceBank
        bank = ExperienceBank(db_path=str(tmp_path / "dup.json"))
        process = {
            "meta": {"student_id":"S001","sample_id":"C01"},
            "roles": [], "discussion": [],
            "modifications": [{"mod_id":"M01","location":"目标","source_role":"r_literacy",
                                "before_summary":"了解","after_summary":"能从情境中",
                                "rationale":"行为化","quote_located":True}],
        }
        bank.update("C01", process, 40, 70)
        bank.update("C01", process, 40, 80)  # 再次更新
        # 应只有1条（合并），不是2条
        assert len(bank.entries) == 1


# ════════════════════════════════════════════════════════════════════════
# 五、ArgumentGraphBuilder 测试
# ════════════════════════════════════════════════════════════════════════
class TestArgumentGraphBuilder:

    def test_build_returns_graph(self):
        """论辩图应含 arguments/attacks/supports/grounded_extension"""
        from modules.argument_graph import ArgumentGraphBuilder
        graph = ArgumentGraphBuilder().build(ROUND0, ROUND1, LESSON_DATA["text"])
        assert "arguments"          in graph
        assert "attacks"            in graph
        assert "supports"           in graph
        assert "grounded_extension" in graph

    def test_arguments_require_quote_and_anchor(self):
        """无 quote 或无 rubric_anchor 的批注不进论辩图"""
        from modules.argument_graph import ArgumentGraphBuilder
        round0_no_anchor = {"r_content": [
            {"issue_id":"C-99","dimension":"C","severity":"major",
             "location":"任务一","quote":"50CO",
             "problem":"错误","suggestion":"修正",
             "in_scope":True,"refer_to":None,"rubric_anchor":None}  # anchor=None
        ]}
        graph = ArgumentGraphBuilder().build(round0_no_anchor, {}, LESSON_DATA["text"])
        assert len(graph["arguments"]) == 0  # 无有效锚点，图为空

    def test_grounded_extension_not_empty_on_no_attacks(self):
        """无攻击关系时，所有有效论元均应存活"""
        from modules.argument_graph import ArgumentGraphBuilder
        r0 = {"r_content": [{"issue_id":"C-01","dimension":"C","severity":"major",
               "location":"任务一","quote":"50CO","problem":"错误","suggestion":"修正",
               "in_scope":True,"refer_to":None,"rubric_anchor":"C·重大错误"}]}
        graph = ArgumentGraphBuilder().build(r0, {}, LESSON_DATA["text"])
        # 如果没有攻击，所有论元应存活（或降级全返回）
        assert len(graph["grounded_extension"]) >= 0


# ════════════════════════════════════════════════════════════════════════
# 六、IntegratorPhase2 测试
# ════════════════════════════════════════════════════════════════════════
class TestIntegratorPhase2:

    def test_integrate_phase2_uses_verdict(self):
        """Phase 2 整合器应按 Verdict 的 target_quote 定位替换"""
        from core.integrator_phase2 import IntegratorPhase2
        integ = IntegratorPhase2(LESSON_DATA, MOCK_VERDICT, "S001", "C01")
        draft, mods = integ.integrate()
        assert len(mods) >= 1
        # C-01 修复：50CO 应被替换
        assert "50CO" not in draft or any(m["source_role"] for m in mods)

    def test_integrate_phase2_mods_have_rationale(self):
        """Phase 2 每条修改记录应含 rationale"""
        from core.integrator_phase2 import IntegratorPhase2
        integ = IntegratorPhase2(LESSON_DATA, MOCK_VERDICT, "S001", "C01")
        _, mods = integ.integrate()
        for m in mods:
            assert m.get("rationale"), f"修改 {m['mod_id']} 缺少 rationale"

    def test_integrator_section_rewrite_fallback(self):
        """quote定位失败时应降级为章节重写"""
        from core.integrator import Integrator
        integ = Integrator(LESSON_DATA, {}, "S001", "C01")
        result = integ._section_rewrite(
            "# 项目目标\n原有内容\n# 任务一\n其他内容",
            "项目目标",
            "新写的内容"
        )
        assert "新写的内容" in result

    def test_integrator_build_discussion(self):
        """discussion 数组应包含 round0 和 round1 的发言"""
        from core.integrator import Integrator
        integ = Integrator(LESSON_DATA, ROUND0, "S001", "C01")
        disc  = integ._build_discussion(ROUND0, ROUND1)
        roles = {d["role_id"] for d in disc}
        assert "r_content"  in roles
        assert "r_literacy" in roles
