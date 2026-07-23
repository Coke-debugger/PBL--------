# Judge 模块 — LLM-as-Judge 教案评分器

对磨课系统输出的教案，按《附录A·教案质量评价量规 v1.1》六维度（A结构完整性/
B内容丰富性/C内容准确性/D内容一致性/E语言逻辑性/F素养导向性）自动打分。

只依赖 `profile` dict + 教案全文，不依赖 preprocessor/roundtable/integrator
等其他模块是否已实现，可以独立接入。

## 1. 目录内容

```
src/judge/
├── __init__.py            # 对外导出：Judge / strip_meta_content / detect_manipulation_flags / check_g1_fidelity / check_g2_degradation
├── judge.py                # 主类 Judge，串起全流程、算分、应用特殊规则
├── rubric_dimensions.json  # 量规六维度权重/满分/子指标判据/扣分表/课型结构清单——唯一权威判据数据源
├── prompts.py              # 从rubric_dimensions.json动态构造各维度评审提示词
├── sampling.py              # 多次采样调用 + 聚合（众数投票/根因去重/证据引用核实）
├── preprocess.py            # 反评审对抗预处理（剥离"设计意图/自评"等自述内容）
├── gates.py                  # G1保真检测 / G2改劣检测（只给诊断信息，不自判终局pass/fail）
├── llm_client.py             # LLM调用层，支持 anthropic/deepseek/openai 三种供应商
├── configs/api.yaml          # LLM供应商/模型配置，换模型只改这个文件
├── calibrate.py               # 用附录E的G/I/O三元组做校准回归
└── __main__.py                 # 独立命令行入口，`python -m src.judge ...`
```

依赖：`code/requirements.txt`（`anthropic`/`PyYAML`/`pytest`必装，`openai`仅当
`configs/api.yaml` 里 `provider` 切到 `deepseek`/`openai` 时才需要）。

## 2. 接口契约

```python
from src.judge import Judge

profile = {
    "subject": "化学",                 # 学科
    "grade": "初中",                   # 学段
    "prior_knowledge": "已学燃烧条件，未学化学方程式",
    "learning_motivation": "中等",
    "target_openness_tier": 2,         # 开放度档位1-4
}

judge = Judge(profile, model_pool=["claude-opus-4-8"])  # model_pool可选，不传用默认配置
report = judge.evaluate(lesson_text, lesson_type="常规课")  # lesson_type: "常规课" | "PBL"
```

`report` 是一个 dict，**只承诺以下key存在、类型不变，不承诺是封闭schema**——
以后可能追加新key（比如方向3的ROB相关字段），消费方请按key取值，不要做
"多一个字段就报错"式的严格校验：

| key | 类型 | 说明 |
|---|---|---|
| `total` | float | 总分，0-100 |
| `dimension_scores` | dict[str, float] | 六维度各自0-5分，如 `{"A":4.5,"B":3.9,"C":5.0,"D":3.8,"E":4.1,"F":4.2}` |
| `low_dims` | list[str] | 得分最低的2个维度，如 `["F","B"]`，用于触发定向二次修订 |
| `details` | dict | 每个维度的完整评审细节（证据、子指标判定、issues等），供审计/调试用 |
| `manipulation_flags` | list[str] | 命中的诱导评审话术原文，非空时需人工确认是否按学术不端处理 |
| `lesson_type` | str | 回显调用时传入的课型 |
| `truncated` | bool | 教案是否因超长（>15000字符）被截断评审 |
| `judge_version` | str | rubric内容的hash（12位），rubric/prompt改了这个值就变，用于识别"用了不同版本评审器" |
| `ROB` | None | 占位字段，团队分工方案方向3（量规优化偏差测量）预留，当前恒为None |

## 3. 环境配置

```bash
pip install -r code/requirements.txt
export ANTHROPIC_API_KEY=sk-...     # 或按 configs/api.yaml 里 api_key_env 指定的变量名
```

换模型/换供应商：改 `src/judge/configs/api.yaml` 的 `provider`/`model`，不用改代码。

## 4. 独立调试

```bash
python -m src.judge --lesson <教案.md路径> --profile <profile.yaml或json路径> --lesson-type PBL
```

## 5. 跑测试

```bash
python -m pytest code/tests/ -q
```
离线聚合逻辑单测（13个）不需要API Key。端到端校准测试
（`test_judge_calibration.py`，用附录E的G/I/O三元组验证 `G分>O分>I分` 且分差≥40）
需要真实 `ANTHROPIC_API_KEY`，未设置时自动跳过（skip不是fail）。

## 6. 已知局限（对接时需要知道）

- C维度"应≥2个模型家族独立评审"目前简化为同模型多次采样；`model_pool` 参数已支持传多个模型名，接入第二家族时只是传参数，不用改代码。
- F维度课标素养参照表只有骨架（5个学科），未接完整版。
- G1/G2门槛检查是关键词弱匹配+人工复核标记，不做语义级判断，也不做终局pass/fail自判（按量规要求必须人工双确认）。
- **端到端校准尚未用真实API Key跑通过**——目前只验证了离线聚合逻辑，实际把G/I/O三元组打分校准的这一步还没做，接入前建议先跑一遍确认分数区分度符合预期。
- 字段命名（如`details`要不要改叫`evidence`）暂未跟其他成员的实现对齐，如接口会议有新约定，以会议结论为准。
