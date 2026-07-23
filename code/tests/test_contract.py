"""tests/test_contract.py — 契约四件套校验测试"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import json, tempfile
from pathlib import Path
from core.validate_submission import (
    check_naming, check_encoding, check_latex, check_images, check_schema, run
)


# ── check_naming ──────────────────────────────────────────────
def test_naming_valid():
    assert check_naming("20250101_MATH01_polished.md") == "PASS"

def test_naming_chinese_fail():
    assert check_naming("stu003_样本A_polished.md") == "FAIL"

def test_naming_underscore_in_id_fail():
    assert check_naming("stu_003_MATH01_polished.md") == "FAIL"


# ── check_latex ──────────────────────────────────────────────
def test_latex_even():
    assert check_latex("公式 $x+y$ 是正确的") == "PASS"

def test_latex_odd():
    assert check_latex("公式 $x+y=z 未闭合") == "FAIL"

def test_latex_escaped():
    assert check_latex(r"价格 \$10 元") == "PASS"

def test_latex_in_codeblock():
    assert check_latex("```\n$x$\n```\n正文无公式") == "PASS"


# ── check_images ──────────────────────────────────────────────
def test_images_no_external():
    assert check_images("正常文本") == "PASS"

def test_images_external_warn():
    assert check_images("![图片](https://example.com/img.png)") == "WARN"


# ── check_schema ──────────────────────────────────────────────
def test_schema_valid(tmp_path):
    p = tmp_path / "S001_C01_process.json"
    p.write_text(json.dumps({
        "meta": {"student_id": "S001", "sample_id": "C01"},
        "roles": [{"role_id": "r_literacy", "name": "教研员", "expertise": "素养"}],
        "discussion": [],
        "modifications": [{"mod_id": "M01", "location": "目标", "before_summary": "原",
                            "after_summary": "新", "source_role": "r_literacy",
                            "rationale": "改写", "quote_located": True}],
    }), encoding="utf-8")
    assert check_schema(str(p)) == "PASS"

def test_schema_missing_modifications(tmp_path):
    p = tmp_path / "S001_C01_process.json"
    p.write_text(json.dumps({
        "meta": {"student_id": "S001", "sample_id": "C01"},
        "roles": [], "discussion": [], "modifications": [],
    }), encoding="utf-8")
    assert check_schema(str(p)) == "FAIL"


# ── run() 完整测试 ────────────────────────────────────────────
def test_run_pass(tmp_path):
    # 创建合规文件对
    prefix = "S001_C01"
    (tmp_path / f"{prefix}_polished.md").write_text(
        "# 教案\n正文内容", encoding="utf-8"
    )
    (tmp_path / f"{prefix}_process.json").write_text(
        json.dumps({
            "meta": {"student_id": "S001", "sample_id": "C01"},
            "roles": [{"role_id": "r_x", "name": "n", "expertise": "e"}],
            "discussion": [],
            "modifications": [{"mod_id": "M01", "location": "全文",
                                "before_summary": "原", "after_summary": "新",
                                "source_role": "r_x", "rationale": "r",
                                "quote_located": True}],
        }), encoding="utf-8"
    )
    result = run(str(tmp_path))
    assert not result["has_fail"], result["failures"]

def test_run_fail_no_pair(tmp_path):
    (tmp_path / "S001_C01_polished.md").write_text("# 教案", encoding="utf-8")
    # 无配对 process.json
    result = run(str(tmp_path))
    assert result["has_fail"]
