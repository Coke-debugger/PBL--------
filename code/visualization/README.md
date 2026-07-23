# Streamlit 可视化界面说明

## 功能

本目录为多智能体教案磨课系统的独立可视化层，不改变原有业务代码和文件契约。

- 从项目 `data/` 目录选择 Markdown 教案和 YAML 学情画像。
- 上传 `.md`、`.yaml` 或 `.yml` 文件；上传内容按原始字节临时保存，不做格式转换。
- 选择“多智能体磨课”或“单模型基线”模式。
- 填写学生编号、样本编号，并可选择跳过 Judge 评审。
- 查看磨课后教案、角色研讨记录和修改记录。
- 下载原格式的 `*_polished.md` 与 `*_process.json`。

运行结果统一保存在项目的 `outputs/streamlit/` 目录，不覆盖原有 `outputs/practice/` 结果。

## 安装

在项目 `code` 目录打开终端，先安装原项目依赖，再安装界面依赖：

```powershell
python -m pip install -r requirements.txt
python -m pip install -r visualization/requirements.txt
```

DeepSeek/OpenAI 兼容接口还需要 `openai` 包；如当前环境尚未安装，请执行：

```powershell
python -m pip install openai
```

## 启动

在 `code` 目录运行：

```powershell
python -m streamlit run visualization/app.py
```

浏览器会自动打开工作台。若没有自动打开，可访问终端显示的本地地址（通常为 `http://localhost:8501`）。

## 输入格式

界面沿用现有命令行程序的输入要求：

- 教案：UTF-8 编码的 Markdown 文件（`.md`）。
- 学情画像：YAML 文件（`.yaml` 或 `.yml`），建议至少包含 `subject`、`grade`、`prior_knowledge`、`learning_motivation` 和 `target_openness_tier`。

示例：

```yaml
subject: 化学
grade: 初中
prior_knowledge: 已学燃烧条件
learning_motivation: 中等
target_openness_tier: 2
```

## API Key 与安全

界面侧边栏中的 API Key 为可选项。填写后，它只会传给本次运行的子进程，不会写入项目配置、日志或结果文件。也可以沿用 `configs/api.yaml` 中指定的环境变量。

## 常见问题

1. 提示缺少 `streamlit`：按“安装”章节安装界面依赖。
2. 模型调用失败：检查网络、模型服务、`configs/api.yaml` 和对应 API Key。
3. 找不到样例：确认输入文件位于项目 `data/` 目录，或改用上传方式。
4. 运行完成但结果页为空：确认学生编号和样本编号未改变，然后重新打开结果页。
5. 输出契约校验失败：展开“运行日志”，根据 `NAME/ENC/TEX/IMG/SCH` 提示修正输入。
