"""Streamlit 可视化入口：为现有命令行磨课流程提供图形界面。"""
from __future__ import annotations

import json
import html
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import streamlit as st
import yaml


APP_DIR = Path(__file__).resolve().parent
CODE_DIR = APP_DIR.parent
PROJECT_DIR = CODE_DIR.parent
DATA_DIR = PROJECT_DIR / "data"
DEFAULT_OUTPUT_DIR = PROJECT_DIR / "outputs" / "streamlit"
API_CONFIG_PATH = CODE_DIR / "configs" / "api.yaml"
PIPELINE_CONFIG_PATH = CODE_DIR / "configs" / "pipeline.yaml"


def load_api_config() -> dict:
    if not API_CONFIG_PATH.exists():
        return {}
    return yaml.safe_load(API_CONFIG_PATH.read_text(encoding="utf-8")) or {}


def load_pipeline_config() -> dict:
    if not PIPELINE_CONFIG_PATH.exists():
        return {}
    data = yaml.safe_load(PIPELINE_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    return data.get("pipeline", data)


def discover_files(suffixes: tuple[str, ...]) -> list[Path]:
    """查找项目数据目录中可直接使用的输入文件。"""
    if not DATA_DIR.exists():
        return []
    return sorted(
        path for path in DATA_DIR.rglob("*")
        if path.is_file() and path.suffix.lower() in suffixes
    )


def display_path(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_DIR))
    except ValueError:
        return str(path)


def resolve_output_dir(value: str) -> Path:
    """支持项目相对路径和绝对路径。"""
    path = Path(value.strip() or str(DEFAULT_OUTPUT_DIR)).expanduser()
    if not path.is_absolute():
        path = PROJECT_DIR / path
    return path.resolve()


def safe_id(value: str, fallback: str) -> str:
    """保持输出命名契约，同时避免把路径字符带入文件名。"""
    cleaned = "".join(ch for ch in value.strip() if ch.isalnum() or ch in "-_")
    return cleaned or fallback


def save_upload(uploaded_file, target_dir: Path, fallback_name: str) -> Path:
    suffix = Path(uploaded_file.name).suffix.lower()
    target = target_dir / f"{fallback_name}{suffix}"
    target.write_bytes(uploaded_file.getvalue())
    return target


def run_workshop(
    mode: str,
    lesson_path: Path,
    profile_path: Path,
    output_dir: Path,
    student_id: str,
    sample_id: str,
    no_judge: bool,
    api_key: str,
    model_name: str,
    judge_model: str,
    agent_models: dict | None = None,
    judge_fast_model: str = "",
    on_event=None,
) -> subprocess.CompletedProcess[str]:
    entry = "baseline.py" if mode == "单模型基线" else "run.py"
    command = [
        sys.executable,
        str(CODE_DIR / entry),
        "--lesson", str(lesson_path),
        "--profile", str(profile_path),
        "--out", str(output_dir),
        "--student-id", student_id,
        "--sample-id", sample_id,
    ]
    if entry == "run.py" and no_judge:
        command.append("--no-judge")

    env = os.environ.copy()
    # 强制子进程使用 UTF-8，避免 Windows 控制台日志出现乱码。
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    config = load_api_config()
    if api_key:
        env[config.get("api_key_env", "DEEPSEEK_API_KEY")] = api_key
    if model_name.strip():
        env[config.get("model_env", "LLM_MODEL")] = model_name.strip()
    if judge_model.strip():
        env["USTC_JUDGE_MODEL"] = judge_model.strip()
    # Judge 快速模型：A/B/D/E 评审用。run.py 据此与 judge_model 构成混合池。
    if judge_fast_model.strip():
        env["USTC_LLM_MODEL"] = judge_fast_model.strip()
    # 各专家独立模型：USTC_AGENT_MODEL_{ROLE_ID 大写}，BaseAgent._resolve_model 读取。
    for role_id, m in (agent_models or {}).items():
        if m and m.strip():
            env[f"USTC_AGENT_MODEL_{role_id.upper()}"] = m.strip()

    process = subprocess.Popen(
        command,
        cwd=CODE_DIR,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    log_lines: list[str] = []
    assert process.stdout is not None
    for line in process.stdout:
        log_lines.append(line)
        if line.startswith("WORKSHOP_EVENT ") and on_event:
            try:
                on_event(json.loads(line[len("WORKSHOP_EVENT "):]))
            except (json.JSONDecodeError, TypeError):
                pass
    return_code = process.wait(timeout=1800)
    return subprocess.CompletedProcess(command, return_code, "".join(log_lines), "")


def detect_lesson_type(lesson_text: str) -> str:
    """与 Preprocessor._detect_course_type 同口径，供对比页在不走完整磨课流程时定课型。"""
    if any(kw in lesson_text for kw in ["项目", "驱动性问题", "成果交流", "任务链", "PBL", "项目化"]):
        return "PBL"
    return "常规课"


def build_judge_pool(pipeline_config: dict, judge_model: str, judge_fast_model: str) -> list[str]:
    """复刻 run.py 的混合评审模型池逻辑：[flash(快), pro(精)]。

    池首=flash 评审 A/B/D/E，池尾=pro 评审 C/F；两框填同一模型则退化为单模型。
    flash 名优先用环境变量 USTC_LLM_MODEL，其次侧边栏填的快速模型，最后 pipeline.yaml。
    """
    flash = os.environ.get("USTC_LLM_MODEL") or judge_fast_model.strip() or pipeline_config.get(
        "judge_fast_model", "deepseek-v4-flash-ascend"
    )
    pro = judge_model.strip() or pipeline_config.get("judge_model", "deepseek-v4-pro")
    return [flash, pro] if flash != pro else [pro]


def ensure_judge_importable() -> None:
    """让本进程能 import src.judge / core.preprocessor。app.py 跑在 code/ 下，
    streamlit 启动时 cwd=code/，直接 import 即可；但保险起见把 code/ 入 sys.path。"""
    if str(CODE_DIR) not in sys.path:
        sys.path.insert(0, str(CODE_DIR))


def evaluate_lesson_inprocess(
    lesson_text: str,
    profile: dict,
    lesson_type: str,
    pipeline_config: dict,
    judge_model: str,
    judge_fast_model: str,
    api_key: str,
    on_progress=None,
) -> dict:
    """在 UI 进程内调用 Judge 评审一份教案，返回与 scores.json 同结构的 report。

    三档降级与 run.py Step 6 完全对齐：完整多采样(超预算抛异常)→快速模式→规则兜底。
    api_key 注入到 USTC_LLM_API_KEY 环境变量供子进程式调用复用（judge 的 llm_client
    从 api_key_env 读）。这里用进程内调用而非再起子进程——对比页要评两份教案，子进程
    方案会因 app.py 的 run_workshop 只接受"磨课"语义而无法复用；进程内调用直接复用
    Judge.evaluate，进度回调也能驱动 UI 刷新。
    """
    ensure_judge_importable()
    if api_key:
        os.environ["USTC_LLM_API_KEY"] = api_key
    timeouts = pipeline_config.get("timeouts", {})
    model_pool = build_judge_pool(pipeline_config, judge_model, judge_fast_model)

    try:
        from src.judge import Judge, JudgeBudgetExceeded
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"无法加载评审模块：{exc}") from exc

    judge = Judge(profile, model_pool=model_pool)
    full_budget = timeouts.get("judge_full", 360)

    def _progress(dim: str, done: int, total: int, message: str) -> None:
        if on_progress:
            on_progress(message)

    try:
        return judge.evaluate(
            lesson_text, lesson_type=lesson_type,
            on_progress=_progress, time_budget_seconds=full_budget,
        )
    except JudgeBudgetExceeded:
        if on_progress:
            on_progress("完整评审超时，已回退到快速模式（每维度单采样），预计 1~2 分钟…")
        return judge.evaluate(
            lesson_text, lesson_type=lesson_type,
            on_progress=_progress,
            time_budget_seconds=timeouts.get("judge_fast", 240),
            n_samples_override=1,
        )


def evaluate_with_fallback(*args, **kwargs) -> dict:
    """evaluate_lesson_inprocess 的兜底包装：连快速模式都失败时用规则兜底出分，
    保证对比页两份教案都有分可比。与 run.py 的最终 except 行为一致。"""
    try:
        return evaluate_lesson_inprocess(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        ensure_judge_importable()
        try:
            from core.judge_fallback import evaluate_fallback
        except Exception:
            raise
        return evaluate_fallback(
            kwargs.get("lesson_text", ""),
            lesson_type=kwargs.get("lesson_type", "常规课"),
            profile=kwargs.get("profile", {}),
        )


def _dim_delta_html_table(before: dict, after: dict) -> str:
    """六维度前后对比表：行=维度，列出磨课前/后分与变化（↑绿/↓红/持平灰）。

    用原生 HTML 表而非 st.dataframe，是为了给变化值上色——dataframe 不便做条件
    着色且样式受限。满分量纲=5，总分另在卡片处体现。
    """
    dims = ["A", "B", "C", "D", "E", "F"]
    names = {
        "A": "结构完整性", "B": "内容丰富性", "C": "内容准确性",
        "D": "目标一致性", "E": "语言规范性", "F": "素养导向性",
    }
    rows = []
    for d in dims:
        b = before.get(d)
        a = after.get(d)
        if b is None or a is None:
            rows.append(
                f'<tr><td>{d}·{names[d]}</td><td>—</td><td>—</td><td>数据缺失</td></tr>'
            )
            continue
        delta = a - b
        if delta > 0.01:
            delta_cell = f'<td class="up">▲ +{delta:.2f}</td>'
        elif delta < -0.01:
            delta_cell = f'<td class="down">▼ {delta:.2f}</td>'
        else:
            delta_cell = f'<td class="flat">— 0.00</td>'
        rows.append(
            f'<tr><td>{d}·{names[d]}</td><td>{b:.2f}</td><td>{a:.2f}</td>{delta_cell}</tr>'
        )
    return f"""
    <style>
    .cmp-table {{ width:100%; border-collapse:collapse; font-size:.92rem; }}
    .cmp-table th, .cmp-table td {{ padding:.5rem .7rem; border-bottom:1px solid var(--separator-color,#e2e8f0); text-align:center; }}
    .cmp-table th {{ background:color-mix(in srgb, var(--primary-color) 10%, transparent); font-weight:650; }}
    .cmp-table td:first-child {{ text-align:left; }}
    .cmp-table td.up {{ color:#16a34a; font-weight:600; }}
    .cmp-table td.down {{ color:#dc2626; font-weight:600; }}
    .cmp-table td.flat {{ color:#94a3b8; }}
    </style>
    <table class="cmp-table">
      <thead><tr><th>维度</th><th>磨课前</th><th>磨课后</th><th>变化</th></tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
    """


def _dim_bar_chart(before: dict, after: dict) -> str:
    """六维度分组横向条形图（纯 HTML/CSS）。每维度两根条：磨课前灰、磨课后主题色。"""
    dims = ["A", "B", "C", "D", "E", "F"]
    names = {
        "A": "A 结构", "B": "B 丰富", "C": "C 准确",
        "D": "D 一致", "E": "E 语言", "F": "F 素养",
    }
    bars = []
    for d in dims:
        b = before.get(d, 0) or 0
        a = after.get(d, 0) or 0
        bars.append(
            f'<div class="bar-row">'
            f'<div class="bar-label">{names[d]}</div>'
            f'<div class="bar-track">'
            f'<div class="bar-fill before" style="width:{b / 5 * 100:.1f}%"><span>{b:.1f}</span></div>'
            f'<div class="bar-fill after" style="width:{a / 5 * 100:.1f}%"><span>{a:.1f}</span></div>'
            f'</div></div>'
        )
    return """
    <style>
    .bar-row { display:flex; align-items:center; margin:.35rem 0; }
    .bar-label { width:64px; font-size:.85rem; opacity:.8; }
    .bar-track { flex:1; display:flex; flex-direction:column; gap:3px; }
    .bar-fill { height:16px; border-radius:4px; display:flex; align-items:center;
                padding-left:.4rem; font-size:.72rem; color:#fff; min-width:24px;
                transition:width .4s ease; }
    .bar-fill.before { background:color-mix(in srgb, #94a3b8 70%, transparent); }
    .bar-fill.after { background:var(--primary-color, #6366f1); }
    </style>
    """ + '<div class="bar-chart">' + "".join(bars) + '</div>'


def _load_profile_yaml(path: Path) -> dict:
    """读学情画像，缺失字段补默认（与 Preprocessor.parse 同口径）。"""
    profile = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    profile.setdefault("target_openness_tier", 2)
    profile.setdefault("learning_motivation", "中等")
    profile.setdefault("prior_knowledge", "未知")
    return profile


def render_compare_tab(
    prefix: str,
    result_dir: Path,
    pipeline_config: dict,
    judge_model: str,
    judge_fast_model: str,
    api_key: str,
    lesson_files: list[Path],
) -> None:
    """前后对比页：同一份教案，对比磨课前(原始)与磨课后(整合)的 Judge 分数。"""
    polished_path = result_dir / f"{prefix}_polished.md"
    score_path = result_dir / f"{prefix}_scores.json"          # 磨课后分数（复用）
    before_score_path = result_dir / f"{prefix}_before_scores.json"  # 磨课前分数（缓存）
    original_path = result_dir / f"{prefix}_original.md"
    profile_path = result_dir / f"{prefix}_profile.yaml"

    st.subheader("磨课前后效果对比")
    st.caption(
        "用同一套 Judge 评审器分别给磨课前的原始教案与磨课后的整合教案打分，"
        "对比总分与六维度变化，直观展示磨课带来的提升。磨课后分数直接复用本次评分，"
        "磨课前分数单独评审一次并缓存，避免重复调用模型。"
    )

    # 磨课后分数必须有：没有 polished/scores 说明还没磨课。
    if not polished_path.exists() or not score_path.exists():
        st.info("尚未生成本样本的磨课结果。请先在“输入与运行”页完成磨课（含 Judge 评审），再回到本页对比。")
        return
    after_report = json.loads(score_path.read_text(encoding="utf-8"))

    # 磨课前教案：优先用运行时落盘的 _original.md；缺失时允许从样例/上传补选。
    before_text = None
    if original_path.exists():
        before_text = original_path.read_text(encoding="utf-8")
    else:
        st.warning(
            f"未找到磨课前原始教案（{prefix}_original.md）。该文件由较新版本在磨课时自动保存；"
            f"如本次磨课早于该功能，可在下方手动选择/上传一份原始教案用于对比。"
        )
        src = st.radio("磨课前教案来源", ("项目样例", "上传文件"), horizontal=True, key="before_src")
        if src == "项目样例":
            chosen = st.selectbox(
                "选择原始教案", lesson_files, format_func=display_path,
                index=0 if lesson_files else None, key="before_lesson_pick",
            )
            if chosen is not None:
                before_text = chosen.read_text(encoding="utf-8")
        else:
            up = st.file_uploader("上传原始 .md 教案", type=("md",), key="before_lesson_up")
            if up is not None:
                before_text = up.getvalue().decode("utf-8", errors="replace")

    if before_text is None:
        return

    # 学情画像：优先运行时落盘的 _profile.yaml，否则用磨课同款（磨课前后学情应一致，
    # 否则分数不可比——这里若取不到就提示用户）。
    profile = _load_profile_yaml(profile_path) if profile_path.exists() else None
    if profile is None:
        st.warning(
            f"未找到学情画像（{prefix}_profile.yaml），无法以同口径评审磨课前教案。"
            f"请重新在“输入与运行”页磨课（新版会自动保存画像），或在下方上传学情。"
        )
        up = st.file_uploader("上传学情画像 .yaml/.yml", type=("yaml", "yml"), key="before_profile_up")
        if up is not None:
            profile = yaml.safe_load(up.getvalue().decode("utf-8", errors="replace")) or {}

    if profile is None:
        return

    # 磨课前分数：有缓存就读，没有就评审一次。
    before_report = None
    if before_score_path.exists():
        before_report = json.loads(before_score_path.read_text(encoding="utf-8"))
        st.caption(f"已加载缓存的磨课前评分（{before_score_path.name}）。删除该文件可强制重评。")

    col_btn, col_note = st.columns([1, 2])
    with col_btn:
        rerun = st.button(
            "（重新）评审磨课前教案",
            help="用与磨课后相同的评审器给磨课前原始教案打分。首次约需数分钟，结果会缓存。",
        )
    with col_note:
        if before_report is not None:
            st.caption(f"磨课前评分模式：{before_report.get('judge_mode', 'full')} · 总分 {before_report.get('total', 0):.1f}")

    if rerun or before_report is None:
        lesson_type = detect_lesson_type(before_text)
        progress = st.progress(0.0, text="正在用 Judge 评审磨课前原始教案，约需数分钟…")
        # Judge 最多回传 6 个维度完成消息，用它做近似进度；保证单调递增且封顶 0.95，
        # 完成时推到 1.0。闭包里的 counter 每次回调自增。
        prog_state = {"done": 0}

        def _on_progress(msg: str) -> None:
            prog_state["done"] += 1
            pct = min(0.95, prog_state["done"] / 6.0)
            progress.progress(pct, text=msg)

        try:
            before_report = evaluate_with_fallback(
                lesson_text=before_text,
                profile=profile,
                lesson_type=lesson_type,
                pipeline_config=pipeline_config,
                judge_model=judge_model,
                judge_fast_model=judge_fast_model,
                api_key=api_key,
                on_progress=_on_progress,
            )
            progress.progress(1.0, text="磨课前评分完成。")
        except Exception as exc:  # noqa: BLE001
            progress.empty()
            st.error(f"评审磨课前教案失败：{exc}")
            return
        before_score_path.write_text(
            json.dumps(before_report, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    if before_report is None:
        return

    # ── 展示对比 ──────────────────────────────────────────────────
    before_total = before_report.get("total", 0)
    after_total = after_report.get("total", 0)
    delta_total = after_total - before_total

    st.markdown("##### 总分对比")
    c1, c2, c3 = st.columns(3)
    c1.metric("磨课前", f"{before_total:.1f}", help="磨课前原始教案的 Judge 总分")
    c2.metric("磨课后", f"{after_total:.1f}", help="磨课后整合教案的 Judge 总分")
    delta_str = f"+{delta_total:.1f}" if delta_total >= 0 else f"{delta_total:.1f}"
    c3.metric("提升幅度", delta_str, delta=f"{delta_total:+.1f} 分")

    # 模式/可信度提示：两边模式不同则分数口径不一致，需提示。
    bm, am = before_report.get("judge_mode", "full"), after_report.get("judge_mode", "full")
    mode_map = {"full": "完整评审", "fast": "快速模式", "fallback": "规则兜底"}
    if bm != am:
        st.warning(
            f"两侧评审模式不同（磨课前={mode_map.get(bm, bm)}，磨课后={mode_map.get(am, am)}），"
            f"分数口径不完全一致，对比仅供参考。建议重跑使两侧同为完整评审。"
        )
    else:
        st.caption(f"两侧评审模式均为：{mode_map.get(bm, bm)}")
    low_all = list(before_report.get("low_confidence_dims", [])) + list(after_report.get("low_confidence_dims", []))
    if low_all:
        st.warning(f"以下维度采样不足、分值不可靠：{('、'.join(sorted(set(low_all))))}，相关维度变化请谨慎解读。")

    st.markdown("##### 六维度对比")
    before_dims = before_report.get("dimension_scores", {})
    after_dims = after_report.get("dimension_scores", {})
    st.markdown(_dim_bar_chart(before_dims, after_dims), unsafe_allow_html=True)
    st.markdown(
        '<span style="font-size:.78rem;opacity:.7">灰色条=磨课前，主题色条=磨课后（满分5）</span>',
        unsafe_allow_html=True,
    )
    st.markdown(_dim_delta_html_table(before_dims, after_dims), unsafe_allow_html=True)

    with st.expander("查看磨课前 / 磨课后教案全文"):
        lt, rt = st.columns(2)
        with lt:
            st.markdown(f"**磨课前**（{len(before_text)} 字）")
            st.markdown(before_text)
        with rt:
            st.markdown(f"**磨课后**（{len(polished_path.read_text(encoding='utf-8'))} 字）")
            st.markdown(polished_path.read_text(encoding="utf-8"))

    st.download_button(
        "下载磨课前评分 before_scores.json",
        data=before_score_path.read_bytes() if before_score_path.exists() else
        json.dumps(before_report, ensure_ascii=False, indent=2).encode("utf-8"),
        file_name=f"{prefix}_before_scores.json",
        mime="application/json",
    )


LIVE_ROLES = [
    ("r_literacy", "素养导向教研员"),
    ("r_content", "学科内容专家"),
    ("r_learner", "学情适配专家"),
    ("r_design", "教学设计专家"),
]

# 每位专家的小人形象：emoji 头像 + 主题色（CSS 变量里直接用作背景渐变）。
# 四个角色用区分度高的色相，避免色盲混淆。状态变化只改表情/气泡，不改配色。
AVATARS = {
    "r_literacy": {"emoji": "🧑‍🏫", "color": "#6366f1"},  # 靛蓝——素养教研员
    "r_content":  {"emoji": "🔬", "color": "#0ea5e9"},   # 天蓝——学科内容
    "r_learner":  {"emoji": "🎒", "color": "#10b981"},   # 翠绿——学情适配
    "r_design":   {"emoji": "📐", "color": "#f59e0b"},   # 琥珀——教学设计
}
# 状态→小人表情映射：思考转圈、已发言点头、失败难过、等待静默。
AVATAR_MOOD = {
    "waiting":   "🧘",
    "thinking":  "💭",
    "completed": "💬",
    "failed":    "😵",
}


def avatar_seat_html(
    role_id: str,
    name: str,
    status: str,
    badge: str,
    bubble: str,
    position: str,
) -> str:
    """渲染一个小人座位：彩色圆头像（emoji）+ 名字 + 状态徽章 + 发言气泡。"""
    av = AVATARS.get(role_id, {"emoji": "🧑", "color": "#94a3b8"})
    mood = AVATAR_MOOD.get(status, "🧑")
    safe_status = html.escape(status)
    return (
        f'<section class="avatar-seat pos-{position} st-{safe_status}">'
        f'<div class="avatar-bubble" title="{html.escape(bubble[:200])}">{html.escape(bubble)}</div>'
        f'<div class="avatar-head" style="--role-color:{av["color"]}">'
        f'<span class="avatar-emoji">{av["emoji"]}</span>'
        f'<span class="avatar-mood">{mood}</span>'
        f'</div>'
        f'<div class="avatar-name">{html.escape(name)}</div>'
        f'<div class="avatar-badge">{html.escape(badge)}</div>'
        f'</section>'
    )


def avatar_styles() -> str:
    return """
    <style>
    .avatar-stage {
        display: grid;
        grid-template-columns: minmax(170px,1fr) minmax(220px,.85fr) minmax(170px,1fr);
        grid-template-areas: ". top ." "left table right" ". bottom .";
        gap: 1.1rem 1.4rem;
        align-items: center;
        margin: .75rem 0 1.4rem;
        padding: 1rem;
        border-radius: 1rem;
        background: color-mix(in srgb, var(--bg-color, #f8fafc) 60%, transparent);
    }
    .pos-top { grid-area: top; }
    .pos-right { grid-area: right; }
    .pos-bottom { grid-area: bottom; }
    .pos-left { grid-area: left; }
    .avatar-seat {
        display: flex;
        flex-direction: column;
        align-items: center;
        text-align: center;
        position: relative;
    }
    .avatar-head {
        width: 78px; height: 78px; border-radius: 50%;
        display: flex; align-items: center; justify-content: center;
        background: radial-gradient(circle at 35% 30%, color-mix(in srgb, var(--role-color) 45%, #ffffff), color-mix(in srgb, var(--role-color) 22%, #ffffff));
        border: 3px solid color-mix(in srgb, var(--role-color) 55%, transparent);
        box-shadow: 0 6px 16px color-mix(in srgb, var(--role-color) 30%, transparent);
        position: relative;
        transition: transform .25s ease, box-shadow .25s ease;
    }
    .avatar-emoji { font-size: 2rem; line-height: 1; }
    .avatar-mood { position: absolute; right: -6px; top: -6px; font-size: 1.15rem; }
    .avatar-name { font-weight: 650; margin-top: .5rem; font-size: .95rem; }
    .avatar-badge {
        margin-top: .25rem; font-size: .78rem; padding: .12rem .55rem; border-radius: 999px;
        background: color-mix(in srgb, var(--role-color) 16%, transparent);
        color: color-mix(in srgb, var(--role-color) 75%, var(--text-color));
    }
    .avatar-bubble {
        position: absolute; bottom: calc(100% + .55rem); left: 50%; transform: translateX(-50%);
        max-width: 220px; min-width: 120px; padding: .5rem .7rem;
        font-size: .82rem; line-height: 1.45; text-align: left;
        background: color-mix(in srgb, var(--role-color) 9%, var(--bg-color,#ffffff));
        border: 1px solid color-mix(in srgb, var(--role-color) 28%, transparent);
        border-radius: .7rem;
        display: none;
    }
    .avatar-bubble::after {
        content: ""; position: absolute; top: 100%; left: 50%; transform: translateX(-50%);
        border: 7px solid transparent;
        border-top-color: color-mix(in srgb, var(--role-color) 28%, transparent);
    }
    /* 思考中：头像呼吸 + 旋转光圈 */
    .st-thinking .avatar-head { animation: avatar-breathe 1.4s ease-in-out infinite; }
    .st-thinking .avatar-mood { animation: avatar-spin 1.6s linear infinite; }
    .st-thinking .avatar-bubble { display: block; }
    /* 已发言：弹气泡，头像微抬 */
    .st-completed .avatar-head { transform: translateY(-3px); box-shadow: 0 10px 22px color-mix(in srgb, var(--role-color) 38%, transparent); }
    .st-completed .avatar-bubble { display: block; }
    /* 失败：灰度 + 摇头 */
    .st-failed .avatar-head { filter: grayscale(.7); animation: avatar-shake .5s ease-in-out 2; }
    .st-failed .avatar-bubble { display: block; border-style: dashed; }
    /* 等待：半透明 */
    .st-waiting .avatar-head { opacity: .55; }
    @keyframes avatar-breathe { 0%,100% { transform: scale(1); } 50% { transform: scale(1.06); } }
    @keyframes avatar-spin { from { transform: rotate(0); } to { transform: rotate(360deg); } }
    @keyframes avatar-shake { 0%,100% { transform: translateX(0); } 25% { transform: translateX(-4px); } 75% { transform: translateX(4px); } }
    /* 中央圆桌 */
    .avatar-table {
        grid-area: table; width: 180px; height: 180px; justify-self: center;
        border-radius: 50%;
        border: 3px solid color-mix(in srgb, var(--primary-color) 62%, transparent);
        background: radial-gradient(circle, color-mix(in srgb, var(--primary-color) 16%, transparent), color-mix(in srgb, var(--primary-color) 6%, transparent));
        display: flex; flex-direction: column; align-items: center; justify-content: center; text-align: center;
        box-shadow: 0 10px 28px color-mix(in srgb, var(--text-color) 12%, transparent);
    }
    .avatar-table .t-round { font-weight: 700; font-size: 1.2rem; }
    .avatar-table .t-topic { margin-top: .35rem; opacity: .72; font-size: .88rem; line-height: 1.5; }
    @media (max-width: 700px) {
        .avatar-stage { grid-template-columns: 1fr; grid-template-areas: "table" "top" "right" "bottom" "left"; }
        .avatar-table { width: 140px; height: 140px; }
    }
    </style>
    """


def live_roundtable_html(states: dict, round_no: int = 1) -> str:
    """生成可重复刷新的运行中小人圆桌。"""
    positions = ["top", "right", "bottom", "left"]
    status_labels = {
        "waiting": "等待中", "thinking": "思考中…",
        "completed": "已发言", "failed": "调用失败",
    }
    seats = []
    completed = 0
    finished = 0
    for index, (role_id, name) in enumerate(LIVE_ROLES):
        state = states.get(role_id, {"status": "waiting"})
        status = state.get("status", "waiting")
        completed += status == "completed"
        finished += status in ("completed", "failed")
        preview = state.get("preview", "等待模型开始分析")
        count = state.get("count", 0)
        badge = f"{status_labels.get(status, status)}" + (f" · {count} 条" if status == "completed" else "")
        bubble = str(preview)[:120]
        seats.append(avatar_seat_html(role_id, name, status, badge, bubble, positions[index]))
    return (
        '<div class="avatar-stage">' + "".join(seats)
        + '<div class="avatar-table">'
        + f'<div class="t-round">第 {round_no} 轮</div>'
        + f'<div class="t-topic">{"本轮处理完成" if finished == len(LIVE_ROLES) else "实时研讨中"}<br>'
        + f'{finished} / {len(LIVE_ROLES)} 位已发言（{completed} 位有效）</div>'
        + '</div></div>'
    )


def roundtable_styles() -> str:
    return avatar_styles()


def render_process(process_path: Path) -> None:
    process = json.loads(process_path.read_text(encoding="utf-8"))
    roles = process.get("roles", [])
    discussion = process.get("discussion", [])
    modifications = process.get("modifications", [])

    left, middle, right = st.columns(3)
    left.metric("参与角色", len(roles))
    middle.metric("研讨发言", len(discussion))
    right.metric("采纳修改", len(modifications))

    if discussion:
        render_roundtable(roles, discussion)

    if modifications:
        st.subheader("修改记录")
        st.dataframe(modifications, use_container_width=True, hide_index=True)


def render_roundtable(roles: list[dict], discussion: list[dict]) -> None:
    """按轮次展示四周小人专家与中央圆桌，并提供完整发言记录。"""
    st.subheader("圆桌研讨")
    rounds = sorted({item.get("round", 1) for item in discussion})
    selected_round = st.select_slider(
        "研讨轮次",
        options=rounds,
        value=rounds[0],
        format_func=lambda value: f"第 {value} 轮",
    )
    current = [item for item in discussion if item.get("round", 1) == selected_round]
    role_map = {role.get("role_id", ""): role for role in roles}
    role_order = [role.get("role_id", "") for role in roles[:4]]
    positions = ["top", "right", "bottom", "left"]

    seats: list[str] = []
    for index, role_id in enumerate(role_order):
        role = role_map.get(role_id, {})
        messages = [item for item in current if item.get("role_id") == role_id]
        preview = messages[0].get("content", "本轮暂无发言") if messages else "本轮暂无发言"
        if len(preview) > 92:
            preview = preview[:92].rstrip() + "…"
        status = "completed" if messages else "waiting"
        badge = f"本轮 {len(messages)} 条意见"
        seats.append(avatar_seat_html(
            role_id, role.get("name", role_id or "未知角色"),
            status, badge, preview, positions[index],
        ))

    st.markdown(
        avatar_styles()
        + '<div class="avatar-stage">'
        + "".join(seats)
        + '<div class="avatar-table">'
        + f'<div class="t-round">第 {html.escape(str(selected_round))} 轮</div>'
        + f'<div class="t-topic">共同审议教案<br>{len(current)} 条发言</div>'
        + '</div></div>',
        unsafe_allow_html=True,
    )

    st.markdown("##### 本轮完整发言")
    for role_id in role_order:
        role = role_map.get(role_id, {})
        messages = [item for item in current if item.get("role_id") == role_id]
        av = AVATARS.get(role_id, {"emoji": "🧑"})
        label = f'{av["emoji"]} {role.get("name", role_id or "未知角色")} · {len(messages)} 条'
        with st.expander(label, expanded=False):
            if not messages:
                st.caption("本轮暂无发言")
            for number, item in enumerate(messages, 1):
                st.markdown(f"**意见 {number}**")
                st.write(item.get("content", ""))
                if number < len(messages):
                    st.divider()


def main() -> None:
    st.set_page_config(page_title="多智能体教案磨课", page_icon="📚", layout="wide")
    st.title("📚 多智能体教案磨课工作台")
    st.caption("保持原有 Markdown、YAML 与 JSON 文件格式，通过可视化界面调用现有磨课流程。")

    lesson_files = discover_files((".md",))
    profile_files = discover_files((".yaml", ".yml"))
    api_config = load_api_config()
    pipeline_config = load_pipeline_config()

    with st.sidebar:
        st.header("运行设置")
        mode = st.radio("运行模式", ("多智能体磨课", "单模型基线"))
        student_id = safe_id(st.text_input("学生编号", "STU001"), "STU001")
        sample_id = safe_id(st.text_input("样本编号", "SAMPLE01"), "SAMPLE01")
        no_judge = st.checkbox("跳过 Judge 评审", value=False, disabled=mode == "单模型基线")
        api_key = st.text_input(
            "API Key（可选）",
            type="password",
            help="仅注入本次子进程环境，不写入任何配置或输出文件。",
        )
        model_name = st.text_input(
            "模型名称",
            value=api_config.get("model", "deepseek-v4-flash-ascend"),
            help="四专家共用的默认模型 ID。下面可按专家单独覆盖；留空的专家用此默认。",
        )
        # 各专家独立模型覆盖：留空 = 用上方"模型名称"默认。对应环境变量
        # USTC_AGENT_MODEL_{ROLE_ID 大写}，由 BaseAgent._resolve_model 读取。
        with st.expander("专家模型（按角色单独指定，可选）", expanded=False):
            st.caption("留空则用上方默认模型。可让不同专家用不同模型。")
            agent_models = {}
            for role_id, role_name in LIVE_ROLES:
                agent_models[role_id] = st.text_input(
                    f"{role_name}（{role_id}）",
                    value="",
                    key=f"agent_model_{role_id}",
                    help=f"留空用默认 {model_name}。对应环境变量 USTC_AGENT_MODEL_{role_id.upper()}。",
                )
        # Judge 评审拆成快速/精确两个模型：A/B/D/E 用快速、C/F 用精确（judge.py 混合池）。
        # 想全用快模型就把两个都填 flash；想全用精确就两个都填 pro。
        with st.expander("评审模型（Judge，快/精分离）", expanded=False):
            st.caption("快速模型评审 A/B/D/E，精确模型评审 C/F（内容准确性/素养核心）。")
            judge_fast_model = st.text_input(
                "评审·快速模型",
                value=pipeline_config.get("judge_fast_model", "deepseek-v4-flash-ascend"),
                help="评审 A/B/D/E 四个维度用的模型，建议用快模型提速。",
            )
            judge_model = st.text_input(
                "评审·精确模型",
                value=pipeline_config.get("judge_model", "deepseek-v4-pro"),
                help="评审 C/F 两个维度用的模型；两框填同一模型=该模型评审全部维度。",
            )
        output_value = st.text_input(
            "输出目录",
            value=display_path(DEFAULT_OUTPUT_DIR),
            help="可填写项目相对路径或绝对路径。",
        )

    input_tab, result_tab, compare_tab = st.tabs(("输入与运行", "结果查看", "前后对比"))
    with input_tab:
        col_lesson, col_profile = st.columns(2)
        with col_lesson:
            st.subheader("1. 选择教案")
            lesson_source = st.radio("教案来源", ("项目样例", "上传文件"), horizontal=True)
            if lesson_source == "项目样例":
                lesson_path = st.selectbox(
                    "Markdown 教案",
                    lesson_files,
                    format_func=display_path,
                    index=0 if lesson_files else None,
                    placeholder="未找到 Markdown 教案",
                )
                lesson_upload = None
            else:
                lesson_upload = st.file_uploader("上传 .md 文件", type=("md",))
                lesson_path = None

        with col_profile:
            st.subheader("2. 选择学情")
            profile_source = st.radio("学情来源", ("项目样例", "上传文件"), horizontal=True)
            if profile_source == "项目样例":
                profile_path = st.selectbox(
                    "YAML 学情画像",
                    profile_files,
                    format_func=display_path,
                    index=0 if profile_files else None,
                    placeholder="未找到 YAML 学情画像",
                )
                profile_upload = None
            else:
                profile_upload = st.file_uploader("上传 .yaml/.yml 文件", type=("yaml", "yml"))
                profile_path = None

        output_dir = resolve_output_dir(output_value)
        st.info(f"结果保存到：{display_path(output_dir)}")
        can_run = (lesson_path is not None or lesson_upload is not None) and (
            profile_path is not None or profile_upload is not None
        )

        if st.button("开始磨课", type="primary", disabled=not can_run, use_container_width=True):
            output_dir.mkdir(parents=True, exist_ok=True)
            runtime_dir = output_dir / ".runtime"
            runtime_dir.mkdir(parents=True, exist_ok=True)
            try:
                with tempfile.TemporaryDirectory(
                    prefix="lesson_workshop_", dir=runtime_dir
                ) as temp_name:
                    temp_dir = Path(temp_name)
                    selected_lesson = (
                        save_upload(lesson_upload, temp_dir, "lesson") if lesson_upload else Path(lesson_path)
                    )
                    selected_profile = (
                        save_upload(profile_upload, temp_dir, "profile") if profile_upload else Path(profile_path)
                    )
                    # 磨课前的原始教案与学情落盘，供"前后对比"页用同一评审器复评磨课前
                    # 分数。只保存首次磨课所用文件，反复磨课不覆盖（除非 prefix 相同，
                    # 那本就是同一样本的最新一次，覆盖合理）。这与 polished.md /
                    # scores.json 同目录、同 prefix，命名契约上属于本项目四件套之外的
                    # 附加产物，不影响 validate_submission（它只校验 *_polished.md/
                    # *_process.json 对）。
                    original_md = output_dir / f"{student_id}_{sample_id}_original.md"
                    original_profile = output_dir / f"{student_id}_{sample_id}_profile.yaml"
                    try:
                        original_md.write_bytes(selected_lesson.read_bytes())
                        original_profile.write_bytes(selected_profile.read_bytes())
                    except Exception:
                        pass
                    live_states = {role_id: {"status": "waiting", "preview": "等待模型开始分析"}
                                   for role_id, _ in LIVE_ROLES}
                    live_round = 1
                    live_title = st.empty()
                    live_view = st.empty()
                    if mode == "多智能体磨课":
                        live_title.subheader("实时圆桌研讨")
                        live_view.markdown(
                            roundtable_styles() + live_roundtable_html(live_states, live_round),
                            unsafe_allow_html=True,
                        )
                    else:
                        live_title.subheader("模型正在生成教案")
                        live_view.info("单模型基线正在分析并改写教案…")

                    def update_live_view(event: dict) -> None:
                        nonlocal live_round
                        if mode != "多智能体磨课":
                            return
                        if event.get("event") == "phase_status":
                            message = str(event.get("message", "正在处理…"))
                            status = event.get("status", "running")
                            if status == "failed":
                                live_view.warning(message)
                            elif status == "completed":
                                live_view.success(message)
                            else:
                                live_view.info(message)
                            return
                        if event.get("event") != "agent_status":
                            return
                        role_id = event.get("role_id")
                        if role_id not in live_states:
                            return
                        live_round = int(event.get("round", live_round))
                        live_states[role_id] = {
                            "status": event.get("status", "waiting"),
                            "count": event.get("count", 0),
                            "preview": event.get("preview", "模型正在分析教案…"),
                        }
                        live_view.markdown(
                            roundtable_styles() + live_roundtable_html(live_states, live_round),
                            unsafe_allow_html=True,
                        )

                    with st.spinner("多智能体正在研讨并整合教案，请稍候……"):
                        completed = run_workshop(
                            mode, selected_lesson, selected_profile, output_dir,
                            student_id, sample_id, no_judge, api_key, model_name, judge_model,
                            agent_models=agent_models,
                            judge_fast_model=judge_fast_model,
                            on_event=update_live_view,
                        )
                    if completed.returncode == 0 and mode == "多智能体磨课":
                        live_title.subheader("圆桌研讨完成")
                    elif completed.returncode == 0:
                        live_title.subheader("模型输出完成")
                        live_view.success("教案已生成，可在“结果查看”中阅读。")
                st.session_state["last_prefix"] = f"{student_id}_{sample_id}"
                st.session_state["last_output_dir"] = str(output_dir)
                st.session_state["last_log"] = (completed.stdout or "") + (completed.stderr or "")
                if completed.returncode == 0:
                    st.success("磨课完成，结果已按原格式保存。")
                else:
                    st.error(f"运行未成功（退出码 {completed.returncode}），请展开日志定位原因。")
                with st.expander("运行日志", expanded=completed.returncode != 0):
                    st.code(st.session_state["last_log"], language="text")
            except subprocess.TimeoutExpired:
                st.error("运行超过 30 分钟，已停止等待。请检查模型服务或网络状态。")
            except Exception as exc:
                st.error(f"无法启动磨课：{exc}")

    with result_tab:
        prefix = st.session_state.get("last_prefix", f"{student_id}_{sample_id}")
        result_dir = Path(st.session_state.get("last_output_dir", str(output_dir)))
        polished_path = result_dir / f"{prefix}_polished.md"
        process_path = result_dir / f"{prefix}_process.json"
        score_path = result_dir / f"{prefix}_scores.json"
        if not polished_path.exists() and not process_path.exists() and not score_path.exists():
            st.info("尚无当前样本结果，请先在“输入与运行”页启动磨课。")
        else:
            if polished_path.exists():
                polished = polished_path.read_text(encoding="utf-8")
                st.subheader("磨课后教案")
                st.markdown(polished)
                st.download_button(
                    "下载 polished.md",
                    data=polished_path.read_bytes(),
                    file_name=polished_path.name,
                    mime="text/markdown",
                )
            if process_path.exists():
                st.divider()
                render_process(process_path)
                st.download_button(
                    "下载 process.json",
                    data=process_path.read_bytes(),
                    file_name=process_path.name,
                    mime="application/json",
                )
            if score_path.exists():
                st.divider()
                score_report = json.loads(score_path.read_text(encoding="utf-8"))
                st.subheader("Judge 评分")
                # 显示评分模式：完整评审 / 快速模式 / 规则兜底，便于判断分数可信度。
                mode = score_report.get("judge_mode", "full")
                mode_labels = {
                    "full": "完整多采样评审",
                    "fast": "快速模式（单采样，分值波动略大）",
                    "fallback": "规则兜底（未调用模型，保守估计）",
                }
                mode_label = mode_labels.get(mode, mode)
                if mode == "fallback":
                    st.warning(f"评分模式：{mode_label}")
                elif mode == "fast":
                    st.info(f"评分模式：{mode_label}")
                else:
                    st.caption(f"评分模式：{mode_label}")
                # 低置信维度提示：某些维度采样不足，分值不可靠，避免把"评审故障"
                # 误读为"教案差"。低置信维度的分用了保守中性分而非真实评判。
                low_conf = score_report.get("low_confidence_dims", [])
                if low_conf:
                    st.warning(
                        f"以下维度采样不足，分值不可靠（已用保守分代替0分）："
                        f"{'、'.join(low_conf)}。建议重跑或更换评审模型。"
                    )
                if score_report.get("truncated"):
                    st.caption("注：教案较长，评审时截断了部分内容。")
                st.metric("总分", f"{score_report.get('total', 0):.1f} / 100")
                dimensions = score_report.get("dimension_scores", {})
                if dimensions:
                    st.dataframe(
                        [{"维度": key, "得分（满分5）": value} for key, value in dimensions.items()],
                        use_container_width=True,
                        hide_index=True,
                    )
                st.download_button(
                    "下载 scores.json",
                    data=score_path.read_bytes(),
                    file_name=score_path.name,
                    mime="application/json",
                )

    with compare_tab:
        # 前后对比页用与本次磨课一致的评审模型；优先取侧边栏当前值，缺失回退配置默认。
        render_compare_tab(
            prefix=st.session_state.get("last_prefix", f"{student_id}_{sample_id}"),
            result_dir=Path(st.session_state.get("last_output_dir", str(output_dir))),
            pipeline_config=pipeline_config,
            judge_model=judge_model,
            judge_fast_model=judge_fast_model,
            api_key=api_key,
            lesson_files=lesson_files,
        )


if __name__ == "__main__":
    main()
