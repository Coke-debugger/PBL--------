"""端到端校准回归测试：调真实API，用附录E的G/I/O三元组验证 G>O>I 且分差≥40。

需要 ANTHROPIC_API_KEY 环境变量；未设置时自动跳过（不计入CI失败），
因为这是需要真实调用付费API的测试，不应该在没有key的环境里报红。
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.judge.calibrate import MIN_GAP, run_calibration

pytestmark = pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="需要 ANTHROPIC_API_KEY 才能跑真实评审调用，未设置则跳过",
)


def test_g_o_i_calibration():
    result = run_calibration()
    totals = result["totals"]
    assert totals["G"] > totals["O"] > totals["I"], (
        f"排序校准失败: G={totals['G']} O={totals['O']} I={totals['I']}"
    )
    assert totals["G"] - totals["I"] >= MIN_GAP, (
        f"分差不足: G-I={totals['G'] - totals['I']}，要求>={MIN_GAP}"
    )
