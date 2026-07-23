# 多智能体教案磨课系统

> **大模型 Agent 应用开发实践：基于多智能体圆桌研讨的教案智能磨课系统**  
> 高校 AI 实践课 · 校企合作课题

---

## 项目简介

本系统模拟真实教研圆桌，通过多个具有差异化专业角色的 AI 智能体对普通教案进行磨课，产出高质量教案及完整的研讨过程记录。

**两种模式：**

| 模式 | 命令 | 说明 |
|------|------|------|
| 单模型基线 | `python baseline.py` | 精心设计的单次 LLM 调用，用于对照实验 |
| 4专家多角色 | `python run.py` | 4个专家角色并行批注→整合输出（Phase 1）|

---

## 环境要求

- Python 3.11+
- 支持 Anthropic API 或 DeepSeek API（OpenAI 兼容）

```bash
pip install -r requirements.txt
```

---

## 快速开始

### 1. 配置 API

编辑 `configs/api.yaml`，选择模型：

```yaml
# 使用中科大大模型公共服务平台（OpenAI 兼容格式）
provider: openai
model: deepseek-v4-flash-ascend
model_env: USTC_LLM_MODEL
base_url: https://api.llm.ustc.edu.cn/v1
api_key_env: USTC_LLM_API_KEY

# 或使用 Anthropic
# provider: anthropic
# model: claude-haiku-4-5-20251001
# api_key_env: ANTHROPIC_API_KEY
```

运行前通过环境变量提供 API Key（不要把密钥写入配置文件）：

```powershell
$env:USTC_LLM_API_KEY="sk-你的APIKey"
$env:USTC_LLM_MODEL="deepseek-v4-flash-ascend"  # 可选：临时覆盖模型
$env:USTC_JUDGE_MODEL="deepseek-v4-pro"          # 可选：临时覆盖评分模型
```

**模型选择建议：**

| 用途 | 推荐模型 |
|------|---------|
| 开发测试（省 token）| `deepseek-chat` 或 `claude-haiku-4-5-20251001` |
| 质量/速度平衡 | `deepseek-reasoner` 或 `claude-sonnet-5` |
| 最高质量（生产）| `claude-opus-4-8` |

### 2. 准备输入文件

教案（Markdown）+ 学情（YAML）：

```yaml
# profile.yaml 示例
subject: 化学
grade: 初中
prior_knowledge: 已学燃烧条件，未学化学方程式
learning_motivation: 中等
target_openness_tier: 2   # 1=全开放 2=半开放 3=指引 4=封闭
```

### 3. 运行磨课

```bash
# Phase 1：4专家并行批注直出
python run.py \
  --lesson  ../data/appendix_E/I_降质版_化学_应急蜡烛.md \
  --profile ../data/appendix_E/profile_chem.yaml \
  --out     ../outputs/practice \
  --student-id 20250101 \
  --sample-id  CHEM01

# 单模型基线（对照实验用）
python baseline.py \
  --lesson  ../data/appendix_E/I_降质版_化学_应急蜡烛.md \
  --profile ../data/appendix_E/profile_chem.yaml \
  --out     ../outputs/practice \
  --student-id 20250101 \
  --sample-id  CHEM01
```

### 4. 查看输出

```
outputs/practice/
├── 20250101_CHEM01_polished.md    # 磨课后教案
└── 20250101_CHEM01_process.json   # 研讨过程记录（含各角色发言和修改追踪）
```

---

## Phase 1 → Phase 2 切换

修改 `configs/pipeline.yaml` 开启更多功能：

```yaml
pipeline:
  # Phase 1（当前默认）
  enable_round1: false    # 互评轮次
  enable_chair:  false    # 主持人仲裁
  enable_judge:  false    # LLM-as-Judge 评审

  # Phase 2（全功能）
  enable_round1: true
  enable_chair:  true
  enable_judge:  true
  enable_refine: true     # 二次修订
```

---

## 系统架构

```
输入：lesson.md + profile.yaml
       │
  [Step 1] 预处理（Preprocessor）
       │ lesson_data
       │
  [Step 2] Round 0：4专家并行批注
       ├── r_literacy  素养导向教研员  → F维度（权重30分）
       ├── r_content   学科内容专家    → C维度（权重20分）
       ├── r_learner   学情适配专家    → D维度（权重15分）
       └── r_design    教学设计专家    → A+B维度（权重25分）
       │ round0
       │
  [Step 3] Round 1 互评 + 元认知监控      ← Phase 2
  [Step 3.5] 冲突分类/论辩图构建          ← Phase 2
  [Step 4] 主持人仲裁                     ← Phase 2
       │
  [Step 5] 整合修改（确定性替换）
       │
  [Step 6] LLM-as-Judge 评审              ← Phase 2
  [Step 7] 定向二次修订（条件触发）        ← Phase 2
       │
  [Step 8] 写出 polished.md + process.json
           契约校验（NAME/ENC/TEX/IMG/SCH）
```

---

## 目录结构

```
{学号}_final/
├── code/
│   ├── run.py                    # 统一 CLI 入口（契约接口）
│   ├── baseline.py               # 单模型基线
│   ├── requirements.txt
│   ├── README.md
│   │
│   ├── configs/
│   │   ├── api.yaml              # ★ API 配置（换模型改这里）
│   │   ├── pipeline.yaml         # 流水线开关（Phase 1/2 切换）
│   │   └── topology.yaml         # 发言顺序配置
│   │
│   ├── core/                     # 最小闭环（验收方只依赖这层）
│   │   ├── types.py              # 全部 TypedDict 数据类型
│   │   ├── llm_client.py         # LLM 抽象层（支持 Anthropic/DeepSeek）
│   │   ├── preprocessor.py       # 输入解析 + LaTeX 预检
│   │   ├── roundtable.py         # 圆桌调度器（Phase 1+2）
│   │   ├── integrator.py         # 确定性修改整合
│   │   ├── integrator_phase2.py  # Phase 2 Verdict 精确替换
│   │   ├── judge.py              # LLM-as-Judge 六维度评审
│   │   ├── validate_submission.py# 契约校验（NAME/ENC/TEX/IMG/SCH）
│   │   └── agents/
│   │       ├── registry.py       # 角色注册表（按名字启停）
│   │       ├── base_agent.py     # BaseAgent 基类
│   │       ├── literacy_agent.py # F维度：素养导向教研员
│   │       ├── content_agent.py  # C维度：学科内容专家
│   │       ├── learner_agent.py  # D维度：学情适配专家
│   │       ├── design_agent.py   # A+B维度：教学设计专家
│   │       └── chair_agent.py    # 主持人/仲裁者
│   │
│   ├── modules/                  # 可插拔创新模块（core 不直接依赖）
│   │   ├── conflict_classifier.py# 创新2：冲突类型分类+差异化消解
│   │   ├── argument_graph.py     # 创新2：Dung 论辩图框架
│   │   ├── monitor.py            # 创新5：元认知监控
│   │   ├── experience_bank.py    # 创新6：跨样本经验库
│   │   └── rob_measurer.py       # 创新3：量规优化偏差 ROB
│   │
│   ├── prompts/                  # 角色提示词
│   │   ├── manifest.json         # 版本追踪（支持 judge_version 冻结）
│   │   └── *.md                  # 各角色系统提示词
│   │
│   ├── schemas/                  # JSON Schema 校验文件
│   └── tests/                    # 单元测试（70个，无需 API key）
│
├── data/                         # 附录材料包（只读）
│   ├── appendix_B/               # 5个脱敏金标准案例
│   └── appendix_E/               # 公开练习三元组（G/I/O）
│
├── outputs/
│   └── practice/                 # 契约要求的练习输出
│
├── report/                       # 技术报告
└── ai_collab/                    # AI 协作开发记录
    ├── CLAUDE.md
    └── notes.md
```

---

## 运行测试

所有测试不依赖 API key，可直接本地运行：

```bash
cd code
python -m pytest tests/ -v
# 预期：70 passed
```

测试覆盖：
- `test_preprocessor.py`  — 输入解析、LaTeX 预检、课型识别
- `test_integrator.py`   — 精确/模糊定位替换、process.json 写出
- `test_contract.py`     — 契约四件套（NAME/ENC/TEX/IMG/SCH）
- `test_baseline.py`     — 基线输出结构、提示词六维覆盖
- `test_multirole.py`    — 4专家批注、Round 0 并行、整合逻辑
- `test_phase2.py`       — Chair仲裁、冲突分类、监控、经验库、论辩图

---

## 输出格式说明

### polished.md
- 编码：UTF-8 无 BOM
- 数学公式：行内 `$...$`，独立块 `$$...$$`
- 支架材料以附录章节内联（不得外置）
- 禁止外链图片

### process.json

```json
{
  "meta": {
    "student_id": "20250101",
    "sample_id": "CHEM01",
    "timestamp": "2026-07-18T..."
  },
  "roles": [
    {"role_id": "r_literacy", "name": "素养导向教研员", "expertise": "..."},
    {"role_id": "r_content",  "name": "学科内容专家",   "expertise": "..."},
    {"role_id": "r_learner",  "name": "学情适配专家",   "expertise": "..."},
    {"role_id": "r_design",   "name": "教学设计专家",   "expertise": "..."}
  ],
  "discussion": [
    {"round": 1, "role_id": "r_content", "content": "...", "refers_to": null}
  ],
  "modifications": [
    {
      "mod_id": "M01",
      "location": "项目目标",
      "before_summary": "了解蜡烛燃烧原理",
      "after_summary": "能从露营情境中提出问题…",
      "source_role": "r_literacy",
      "rationale": "目标行为化改写（F1判据）",
      "quote_located": true
    }
  ]
}
```

---

## 契约校验

提交前自检：

```bash
cd code
python -c "
from core.validate_submission import run
result = run('../outputs/practice')
print('PASS' if not result['has_fail'] else result['failures'])
"
```

---

## 创新模块说明

系统实现了4个研究级创新方向，通过 `pipeline.yaml` 开关控制：

| 方向 | 模块 | 开关 |
|------|------|------|
| 创新2：Dung 论辩框架 | `modules/argument_graph.py` | `enable_argument: true` |
| 创新3：ROB 量规优化偏差 | `modules/rob_measurer.py` | 实验脚本调用 |
| 创新5：元认知监控 | `modules/monitor.py` | `enable_monitor: true` |
| 创新6：经验库 | `modules/experience_bank.py` | `enable_experience: true` |

---

## 参考

- 教案质量评价量规：`附录A`
- 脱敏金标准案例：`附录B`（化学/数学/生物/语文/环境）
- 公开练习三元组：`附录E`（G金标准 / I降质版 / O参考输出）
- 提交与运行契约：`附录D`
