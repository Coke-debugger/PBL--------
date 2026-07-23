"""用附录E公开练习三元组（G/I/O）校准 Judge。

附录A"评审器冻结"条款：评审实现在校准达标后才能冻结版本号使用；校准
通过条件为 G_total > O_total > I_total 且 G_total - I_total >= 40 分。
不通过则需要回去调整 prompts.py / rubric_dimensions.json 里过松或过严
的判据，而不是硬调阈值。

需要真实的 ANTHROPIC_API_KEY 环境变量（会产生实际API调用与费用）。
"""

from __future__ import annotations

import sys
from pathlib import Path

from .judge import Judge

_REPO_ROOT = Path(__file__).resolve().parents[3]
_TRIPLET_DIR = _REPO_ROOT / "附录" / "附录E_公开练习三元组"

TRIPLET_FILES = {
    "G": _TRIPLET_DIR / "G_金标准_化学_应急蜡烛.md",
    "I": _TRIPLET_DIR / "I_降质版_化学_应急蜡烛.md",
    "O": _TRIPLET_DIR / "O_参考磨课输出_化学_应急蜡烛.md",
}

# 附录E三元组固定为初中化学PBL项目课，与 lesson_data["profile"] 字段对齐。
TRIPLET_PROFILE = {
    "subject": "化学",
    "grade": "初中",
    "prior_knowledge": "已学习基础化学变化与反应现象认识",
    "learning_motivation": "中等",
    "target_openness_tier": 2,
}
TRIPLET_LESSON_TYPE = "PBL"

MIN_GAP = 40.0


def run_calibration(model_pool: list[str] | None = None) -> dict:
    """跑一遍G/I/O评审，返回各版本Judge报告与校准结论，不在此处退出进程。"""
    judge = Judge(TRIPLET_PROFILE, model_pool=model_pool)
    reports = {}
    for tag, path in TRIPLET_FILES.items():
        if not path.exists():
            raise FileNotFoundError(f"附录E三元组文件缺失: {path}")
        text = path.read_text(encoding="utf-8")
        reports[tag] = judge.evaluate(text, lesson_type=TRIPLET_LESSON_TYPE)

    g_total = reports["G"]["total"]
    o_total = reports["O"]["total"]
    i_total = reports["I"]["total"]

    order_ok = g_total > o_total > i_total
    gap_ok = (g_total - i_total) >= MIN_GAP

    return {
        "reports": reports,
        "totals": {"G": g_total, "O": o_total, "I": i_total},
        "order_ok": order_ok,
        "gap_ok": gap_ok,
        "passed": order_ok and gap_ok,
    }


def _print_summary(result: dict) -> None:
    totals = result["totals"]
    print(f"G={totals['G']:.1f}  O={totals['O']:.1f}  I={totals['I']:.1f}  (G-I={totals['G'] - totals['I']:.1f})")
    for tag in ("G", "O", "I"):
        dims = result["reports"][tag]["dimension_scores"]
        dims_str = "  ".join(f"{d}:{v:.1f}" for d, v in dims.items())
        print(f"  {tag} 维度分  {dims_str}")
    if result["passed"]:
        print("PASS — 排序与分差均满足校准通过条件。")
    else:
        if not result["order_ok"]:
            print("FAIL — 排序校准失败（要求 G>O>I）。")
        if not result["gap_ok"]:
            print(f"FAIL — 分差不足（要求 G-I>={MIN_GAP}，实际{totals['G'] - totals['I']:.1f}）。")


if __name__ == "__main__":
    result = run_calibration()
    _print_summary(result)
    sys.exit(0 if result["passed"] else 1)
