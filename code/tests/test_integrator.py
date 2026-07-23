"""tests/test_integrator.py"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import tempfile, json
from pathlib import Path
from core.integrator import Integrator


LESSON_DATA = {
    "text": "# 项目目标\n了解蜡烛燃烧原理\n\n# 任务一\n完成化学方程式：2C₂₅H₅₂ + 51O₂ → 50CO + 52H₂O\n",
    "profile": {"subject": "化学", "grade": "初中"},
    "meta": {"student_id": "S001", "sample_id": "C01"},
    "structure": {"course_type": "PBL"},
    "self_detected_errors": [],
}

ROUND0 = {
    "r_literacy": [
        {"issue_id": "F-01", "dimension": "F", "severity": "major",
         "location": "项目目标", "quote": "了解蜡烛燃烧原理",
         "problem": "目标去行为化", "suggestion": "能从情境中提出问题拆解变量",
         "in_scope": True, "refer_to": None, "rubric_anchor": "F1"},
    ],
    "r_content": [
        {"issue_id": "C-01", "dimension": "C", "severity": "major",
         "location": "任务一", "quote": "50CO",
         "problem": "完全燃烧应生成CO₂", "suggestion": "25CO₂",
         "in_scope": True, "refer_to": None, "rubric_anchor": "C"},
    ],
}


def test_integrate_basic():
    integ = Integrator(LESSON_DATA, ROUND0, "S001", "C01")
    draft, mods = integ.integrate()
    assert isinstance(draft, str)
    assert len(mods) >= 1


def test_locate_and_replace_exact():
    integ = Integrator(LESSON_DATA, {}, "S001", "C01")
    ok, new_text = integ._locate_and_replace(
        "了解蜡烛燃烧原理", "了解蜡烛燃烧原理", "能从情境中提出问题"
    )
    assert ok
    assert "能从情境中提出问题" in new_text


def test_locate_normalized():
    integ = Integrator(LESSON_DATA, {}, "S001", "C01")
    ok, _ = integ._locate_and_replace(
        "了解　蜡烛　燃烧原理", "了解蜡烛燃烧原理", "替换"
    )
    assert ok


def test_locate_fail():
    integ = Integrator(LESSON_DATA, {}, "S001", "C01")
    ok, original = integ._locate_and_replace(
        "原文内容", "完全不存在的片段", "替换"
    )
    assert not ok
    assert original == "原文内容"


def test_write_process(tmp_path):
    integ = Integrator(LESSON_DATA, ROUND0, "S001", "C01")
    draft, mods = integ.integrate()
    process_path = tmp_path / "S001_C01_process.json"
    integ.write_process(process_path, mods, ROUND0)
    assert process_path.exists()
    data = json.loads(process_path.read_text(encoding="utf-8"))
    assert "meta" in data
    assert "roles" in data
    assert "modifications" in data
    assert len(data["modifications"]) >= 1


def test_write_polished(tmp_path):
    integ = Integrator(LESSON_DATA, ROUND0, "S001", "C01")
    draft, _ = integ.integrate()
    p = tmp_path / "S001_C01_polished.md"
    integ.write_polished(p, draft)
    assert p.exists()
    assert len(p.read_text(encoding="utf-8")) > 0
