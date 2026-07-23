# -*- coding: utf-8 -*-
"""tests/io_trace_demo.py -- 全流程 I/O 追踪演示 (mock LLM，无需 API key)"""
import sys, os, io, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
import textwrap, tempfile, yaml
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from unittest.mock import patch
from pathlib import Path

SEP  = "=" * 70
SEP2 = "-" * 70

def box(title, content, width=68):
    out = [f"+-{title}-" + "-" * max(0, width - len(title) - 3) + "+"]
    for line in str(content).split("\n"):
        for chunk in textwrap.wrap(line, width - 2) or [""]:
            out.append(f"| {chunk:<{width-2}} |")
    out.append("+" + "-" * width + "+")
    return "\n".join(out)

# ─────────────────────────────────────────────────────────────────────
# 1. 读取真实降质版教案
# ─────────────────────────────────────────────────────────────────────
LESSON_PATH  = Path("../data/appendix_E")
I_FILE = None
for f in LESSON_PATH.iterdir():
    if "降质" in f.name or "degraded" in f.name.lower():
        I_FILE = f
        break

if I_FILE and I_FILE.exists():
    lesson_text = I_FILE.read_text(encoding="utf-8")
    print(f"[数据源] 使用真实降质版: {I_FILE.name}")
else:
    lesson_text = (
        "# 基于对化学反应认识与调控的项目学习\n"
        "-- 应急蜡烛的设计与制作\n\n"
        "## 项目目标\n"
        "1. 了解蜡烛燃烧的化学知识\n"
        "2. 学习化学实验的基本操作\n\n"
        "## 任务一：认识石蜡\n"
        "石蜡完全燃烧方程式：$2C_{25}H_{52} + 51O_2 \\to 50CO + 52H_2O$\n"
        "石蜡与氧气质量比为 816:352\n"
    )
    print("[数据源] 使用内置 stub 教案")

profile = {
    "subject": "化学",
    "grade": "初中",
    "prior_knowledge": "已学燃烧条件，未学化学方程式",
    "learning_motivation": "中等",
    "target_openness_tier": 2,
}

# ─────────────────────────────────────────────────────────────────────
# 2. Mock 批注（各专家角色的 LLM 模拟返回，使用 ASCII 引号）
# ─────────────────────────────────────────────────────────────────────
MOCK_LITERACY = [
    {
        "issue_id": "F-01", "dimension": "F", "severity": "major",
        "location": "项目目标",
        "quote": "了解蜡烛燃烧的化学知识",
        "problem": "目标去行为化：知识点罗列，无行为动词、情境、可观察结果",
        "suggestion": (
            "改写为：能从露营停电情境中提出蜡烛燃烧时间问题，"
            "拆解影响变量（烛芯/蜡组分/环境），形成设计调控问题解决思路"
        ),
        "in_scope": True, "refer_to": None,
        "rubric_anchor": "F1·行为动词三元素",
    },
    {
        "issue_id": "F-02", "dimension": "F", "severity": "major",
        "location": "项目目标",
        "quote": "学习化学实验的基本操作",
        "problem": "目标为通用模板，与本课情境无关，无法通过特异性检验",
        "suggestion": "改写为：通过正交实验设计比较石蜡配比对燃烧时间的影响，形成证据链",
        "in_scope": True, "refer_to": None,
        "rubric_anchor": "F1·本课特异性",
    },
]

MOCK_CONTENT = [
    {
        "issue_id": "C-01", "dimension": "C", "severity": "major",
        "location": "任务一·化学方程式",
        "quote": "50CO",
        "problem": "化学方程式错误：石蜡完全燃烧应生成 CO2，原式写成 CO（不完全燃烧产物）",
        "suggestion": "修正为：$C_{25}H_{52} + 38O_2 \\to 25CO_2 + 26H_2O$",
        "in_scope": True, "refer_to": None,
        "rubric_anchor": "C·重大错误·-2分",
    },
    {
        "issue_id": "C-02", "dimension": "C", "severity": "major",
        "location": "任务一·质量比",
        "quote": "质量比为 816:352",
        "problem": "质量比计算错误：C25H52摩尔质量352，38×O2=1216，正确比为 1216:352",
        "suggestion": "修正为：石蜡与氧气质量比 = 1216:352（约 11:3.2）",
        "in_scope": True, "refer_to": None,
        "rubric_anchor": "C·计算错误·-2分",
    },
]

MOCK_LEARNER = [
    {
        "issue_id": "D-01", "dimension": "D", "severity": "minor",
        "location": "任务一",
        "quote": "学习化学实验的基本操作",
        "problem": "活动目标与学情不匹配：已有操作基础，此目标重复低效",
        "suggestion": "将目标改为聚焦本课变量控制，与学情中已学燃烧条件形成合理进阶",
        "in_scope": True, "refer_to": None,
        "rubric_anchor": "D5·学情适配",
    },
]

MOCK_DESIGN = [
    {
        "issue_id": "A-01", "dimension": "A", "severity": "major",
        "location": "全文",
        "quote": "（全文无此节）",
        "problem": "PBL课型缺少成果交流核心结构件，缺失则A维度封顶2.0，同时F6=0",
        "suggestion": "在任务三之后新增：成果交流·班级蜡烛新品发布会（含AB演讲和双轨评价量规）",
        "in_scope": True, "refer_to": None,
        "rubric_anchor": "A·PBL核心件·成果交流",
    },
]

# ─────────────────────────────────────────────────────────────────────
# 演示开始
# ─────────────────────────────────────────────────────────────────────
print("\n" + SEP)
print("  全流程 I/O 追踪演示（Mock LLM，无需 API key）")
print(SEP)

# ── Step 1: 输入 ──────────────────────────────────────────────────────
print("\n[Step 1] 输入层")
print(box("输入教案（前 500 字）", lesson_text[:500] + "\n...（下略）"))
print()
print(box("学情描述 profile.yaml", json.dumps(profile, ensure_ascii=False, indent=2)))

# ── Step 2: 预处理 ────────────────────────────────────────────────────
print(f"\n{SEP2}")
print("[Step 2] Preprocessor.parse()")

from core.preprocessor import Preprocessor
with tempfile.TemporaryDirectory() as tmp:
    lp = Path(tmp) / "lesson.md"
    pp = Path(tmp) / "profile.yaml"
    lp.write_text(lesson_text, encoding="utf-8")
    pp.write_text(yaml.dump(profile, allow_unicode=True), encoding="utf-8")
    prep = Preprocessor(str(lp), str(pp), "20250101", "CHEM01")
    lesson_data = prep.parse()

sections_preview = [(s["level"], s["heading"]) for s in lesson_data["structure"]["sections"][:6]]
print(box("lesson_data（输出）",
    f"keys:            {list(lesson_data.keys())}\n"
    f"subject:         {lesson_data['profile']['subject']}\n"
    f"grade:           {lesson_data['profile']['grade']}\n"
    f"course_type:     {lesson_data['structure']['course_type']}\n"
    f"sections(前6):   {sections_preview}\n"
    f"meta:            {lesson_data['meta']}\n"
    f"self_errors:     {len(lesson_data['self_detected_errors'])} 条"))

# ── Step 3: Round 0 ───────────────────────────────────────────────────
print(f"\n{SEP2}")
print("[Step 3] Round 0 · 4专家并行批注（ThreadPoolExecutor × 4）")
print("  mock 注入：每个角色 call_llm() 返回预设批注 JSON")

mock_side_effects = [
    json.dumps(MOCK_LITERACY),
    json.dumps(MOCK_CONTENT),
    json.dumps(MOCK_LEARNER),
    json.dumps(MOCK_DESIGN),
]
with patch("core.agents.base_agent.call_llm", side_effect=mock_side_effects):
    from core.roundtable import Roundtable
    config = {
        "active_roles": ["r_literacy", "r_content", "r_learner", "r_design"],
        "timeouts": {"round0": 60},
    }
    rt = Roundtable(lesson_data, config)
    round0 = rt.run()

print()
total_anns = sum(len(v) for v in round0.values())
print(f"  批注总数：{total_anns} 条（覆盖维度：{sorted({a['dimension'] for anns in round0.values() for a in anns})}）")
for role_id, anns in round0.items():
    print(f"\n  ┌ {role_id}  → {len(anns)} 条")
    for ann in anns:
        print(f"  │  [{ann['dimension']}·{ann['severity']}] {ann['location']}")
        print(f"  │    原文: {ann['quote']!r}")
        print(f"  │    问题: {ann['problem'][:60]}...")
        print(f"  │    建议: {ann['suggestion'][:60]}...")
    print(f"  └─")

# ── Step 4: 整合 ──────────────────────────────────────────────────────
print(f"\n{SEP2}")
print("[Step 4] Integrator.integrate()  (无 API，确定性替换)")

from core.integrator import Integrator
integ  = Integrator(lesson_data, round0, "20250101", "CHEM01")
draft, mods = integ.integrate()

print(f"\n  修改记录：{len(mods)} 条")
for m in mods:
    located = "[OK]" if m["quote_located"] else "[降级]"
    print(f"  {located} {m['mod_id']} | {m['source_role']} | {m['location']}")
    print(f"      BEFORE: {m['before_summary'][:50]!r}")
    print(f"      AFTER:  {m['after_summary'][:50]!r}")

# ── Step 5: 写出文件 ──────────────────────────────────────────────────
print(f"\n{SEP2}")
print("[Step 5] 写出 polished.md + process.json")

with tempfile.TemporaryDirectory() as out_dir:
    out        = Path(out_dir)
    pol_path   = out / "20250101_CHEM01_polished.md"
    proc_path  = out / "20250101_CHEM01_process.json"

    integ.write_polished(pol_path, draft)
    integ.write_process(proc_path, mods, round0)

    polished_text = pol_path.read_text(encoding="utf-8")
    proc_data     = json.loads(proc_path.read_text(encoding="utf-8"))

    # ── 输出：polished.md ────────────────────────────────────────────
    print()
    print(box("polished.md（磨课后教案，前 600 字）",
              polished_text[:600] + "\n...（下略）"))

    # ── 输出：process.json ───────────────────────────────────────────
    print()
    print(box("process.json（研讨记录）",
        f"meta.student_id:   {proc_data['meta']['student_id']}\n"
        f"meta.sample_id:    {proc_data['meta']['sample_id']}\n"
        f"roles:             {[r['role_id'] for r in proc_data['roles']]}\n"
        f"discussion 条数:   {len(proc_data['discussion'])}\n"
        f"modifications 条数:{len(proc_data['modifications'])}\n\n"
        "--- 前 2 条 modifications ---\n" +
        "\n".join(
            f"  {m['mod_id']} [{m['source_role']}] {m['location']}\n"
            f"    rationale: {m['rationale'][:50]}"
            for m in proc_data["modifications"][:2]
        )))

    # ── 契约校验 ─────────────────────────────────────────────────────
    from core.validate_submission import run as validate
    val = validate(out_dir)
    print()
    status = "PASS" if not val["has_fail"] else "FAIL"
    print(box("validate_submission.py 结果",
              f"status:   {status}\n"
              f"failures: {val['failures'] or '（无）'}\n"
              f"warnings: {val['warnings'] or '（无）'}"))

# ── Step 6: Before / After 对比 ───────────────────────────────────────
print(f"\n{SEP2}")
print("[Step 6] Before / After 关键改动对比")
located_mods = [m for m in mods if m["quote_located"]]
if located_mods:
    for i, m in enumerate(located_mods, 1):
        print(f"\n  改动 {i}（{m['source_role']} | {m['location']}）")
        print(f"  BEFORE: {m['before_summary']!r}")
        print(f"  AFTER:  {m['after_summary']!r}")
else:
    print("  所有 quote 均未精确定位（说明降质版与 stub 文本差异大），触发章节级降级重写")

# ── 汇总 ─────────────────────────────────────────────────────────────
print(f"\n{SEP}")
total_located = sum(1 for m in mods if m["quote_located"])
print(f"  演示完成")
print(f"  修改总数: {len(mods)} 条  |  精确定位: {total_located}  |  降级重写: {len(mods)-total_located}")
print(f"  契约校验: {'PASS' if not val['has_fail'] else 'FAIL'}")
print(SEP)
