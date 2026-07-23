"""G1保真检测 / G2改劣检测——磨课输出专用的门槛检查（最小实现）。

附录A原文明确规定：G1"疑似不通过 → 人工双确认后方生效"，程序不得自作
主张判定失败；G2是"渐进扣分机制，没有'不过则记0'的悬崖"。因此本模块的
函数只输出诊断信息（needs_human_review 的具体项），不输出终局的
pass/fail 结论——终局判定留给人工层，这是刻意的设计，不是偷懒。

接口用普通 dict 输入（不依赖 integrator/preprocessor 的类），因为这两个
模块在其他队友手里还没落地，本模块不应因此被阻塞。
"""

from __future__ import annotations

from .sampling import _normalize  # 复用同一套原文子串归一化逻辑，避免两套口径


def check_g1_fidelity(input_meta: dict, output_text: str) -> dict:
    """G1保真检测：磨课输出是否仍是'同一节课'。

    input_meta 期望字段（均可选，缺失的检查项自动跳过）：
      subject: str            # 学科
      grade: str              # 学段
      topic: str              # 课题/主题
      course_type: str        # "常规课" | "PBL"
      lesson_hours: float     # 声明课时数
    """
    checks = []

    def add(name: str, status: str, note: str = ""):
        checks.append({"check": name, "status": status, "note": note})

    normalized_output = _normalize(output_text)

    subject = input_meta.get("subject")
    if subject:
        ok = _normalize(subject) in normalized_output
        add("subject_consistency", "pass" if ok else "needs_human_review",
            "" if ok else f"输出中未直接检出学科关键词'{subject}'，允许标题措辞改写，需人工确认语义是否一致")

    grade = input_meta.get("grade")
    if grade:
        ok = _normalize(grade) in normalized_output
        add("grade_consistency", "pass" if ok else "needs_human_review",
            "" if ok else f"输出中未直接检出学段关键词'{grade}'，需人工确认")

    topic = input_meta.get("topic")
    if topic:
        # 课题允许标题措辞改写，只做弱信号：截取核心词做子串检查，命中失败仅提示不判死。
        ok = _normalize(topic) in normalized_output
        add("topic_consistency", "pass" if ok else "needs_human_review",
            "" if ok else "课题原文精确匹配未命中（允许改写），需人工确认学科主题语义是否一致")

    course_type = input_meta.get("course_type")
    if course_type:
        add("course_type_declared", "needs_human_review",
            f"声明课型为'{course_type}'，课型是否被输出改变需人工/结构解析模块核对（本模块不做结构抽取）")

    lesson_hours = input_meta.get("lesson_hours")
    if lesson_hours is not None:
        add("lesson_hours_declared", "needs_human_review",
            f"声明课时数为{lesson_hours}，实际课时规模变化是否≤±1课时需人工核对（容差±25%按45分钟/课时折算）")

    overall = "pass" if all(c["status"] == "pass" for c in checks) else "needs_human_review"
    return {"overall": overall, "checks": checks}


def check_g2_degradation(defect_registry: list[dict], output_text: str) -> dict:
    """G2改劣检测：植入错误清单逐条核对处置方式。

    defect_registry 每项期望字段：
      defect_id: str
      root_cause: str
      original_quote: str      # 输入教案中该缺陷对应的原文片段
      fixed_quote: str | None  # 若已知正确写法，用于命中"已修复"判定；不提供则该项恒需人工复核
    """
    results = []
    for defect in defect_registry:
        original_quote = defect.get("original_quote", "")
        fixed_quote = defect.get("fixed_quote")
        still_present = _normalize(original_quote) in _normalize(output_text) if original_quote else False

        if still_present:
            disposition = "unresolved"
            note = "原始错误片段仍原样存在于输出中，按C维度扣分表正常扣分（未处理）"
        elif fixed_quote and _normalize(fixed_quote) in _normalize(output_text):
            disposition = "resolved"
            note = "已命中已知正确写法，判定为修复"
        else:
            disposition = "needs_human_review"
            note = "原始错误片段已不在输出中，但无法自动区分'已修复为正确内容'与'删除回避'（G2要求二者处置不同），需人工核对"

        results.append({
            "defect_id": defect.get("defect_id"),
            "root_cause": defect.get("root_cause"),
            "disposition": disposition,
            "note": note,
        })

    return {
        "results": results,
        "unresolved_count": sum(1 for r in results if r["disposition"] == "unresolved"),
        "needs_human_review_count": sum(1 for r in results if r["disposition"] == "needs_human_review"),
    }
