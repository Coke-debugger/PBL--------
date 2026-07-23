"""tests/test_preprocessor.py"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import tempfile, pytest
from pathlib import Path
from core.preprocessor import Preprocessor, LaTeXError


def make_files(lesson_text: str, profile_text: str):
    tmp = tempfile.mkdtemp()
    lp = Path(tmp) / "lesson.md"
    pp = Path(tmp) / "profile.yaml"
    lp.write_text(lesson_text, encoding="utf-8")
    pp.write_text(profile_text, encoding="utf-8")
    return str(lp), str(pp)


PROFILE = "subject: 化学\ngrade: 初中\nprior_knowledge: 已学燃烧条件\n"


def test_parse_basic():
    lp, pp = make_files("# 教学目标\n内容", PROFILE)
    data = Preprocessor(lp, pp, "S001", "C01").parse()
    assert data["text"] == "# 教学目标\n内容"
    assert data["profile"]["subject"] == "化学"
    assert data["meta"]["student_id"] == "S001"
    assert data["meta"]["sample_id"] == "C01"


def test_course_type_pbl():
    lp, pp = make_files("# 项目简介\n驱动性问题", PROFILE)
    data = Preprocessor(lp, pp).parse()
    assert data["structure"]["course_type"] == "PBL"


def test_course_type_regular():
    lp, pp = make_files("# 教学目标\n内容", PROFILE)
    data = Preprocessor(lp, pp).parse()
    assert data["structure"]["course_type"] == "常规课"


def test_latex_odd_dollars():
    lp, pp = make_files("公式 $x+y=z 下一行", PROFILE)
    with pytest.raises(LaTeXError):
        Preprocessor(lp, pp).parse()


def test_latex_even_dollars():
    lp, pp = make_files("公式 $x+y=z$ 是正确的", PROFILE)
    data = Preprocessor(lp, pp).parse()  # should not raise
    assert data is not None


def test_sections_extracted():
    text = "# 教学目标\n目标内容\n## 教学过程\n过程内容\n"
    lp, pp = make_files(text, PROFILE)
    data = Preprocessor(lp, pp).parse()
    headings = [s["heading"] for s in data["structure"]["sections"]]
    assert "教学目标" in headings
    assert "教学过程" in headings
