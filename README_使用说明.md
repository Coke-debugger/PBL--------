# 多智能体教案磨课系统

本程序通过四位虚拟教研专家协同分析教案，生成修改后的教案、研讨记录和 Judge 评分，并在运行过程中显示实时圆桌研讨状态。

## 一、运行环境

- Windows 10/11
- Python 3.11 或更高版本
- 可访问大模型 API 的网络
- 中科大大模型公共服务平台 API Key（或兼容 OpenAI 格式的平台密钥）

检查 Python 是否安装：

```powershell
python --version
```

如未安装，请先从 Python 官方网站安装，并在安装时勾选“Add Python to PATH”。

## 二、最快启动方式

1. 解压 ZIP 文件，建议放在不需要管理员权限的目录。
2. 双击根目录中的 `启动程序.bat`。
3. 第一次运行会创建 `.venv` 并安装依赖，需要等待几分钟。
4. 浏览器通常会自动打开；若未打开，请访问终端显示的地址，一般为 `http://localhost:8501`。
5. 在左侧填写 API Key、模型名称和输出目录。
6. 选择教案与学情画像，点击“开始磨课”。

## 三、界面参数

- **运行模式**：多智能体磨课或单模型基线。
- **API Key**：仅传给本次运行进程，不写入配置、日志或结果文件。
- **模型名称**：默认 `deepseek-v4-flash-ascend`，必须与 API Key 权限一致。
- **评分模型**：默认 `deepseek-v4-pro`；不需要评分时可勾选“跳过 Judge 评审”。
- **输出目录**：支持项目相对路径或绝对路径。

## 四、输入文件

教案使用 UTF-8 编码的 Markdown 文件（`.md`）。学情画像使用 YAML 文件，示例：

```yaml
subject: 化学
grade: 初中
prior_knowledge: 已学燃烧条件，未学化学方程式
learning_motivation: 中等
target_openness_tier: 2
```

包内 `data/appendix_E/` 提供了可直接使用的示例。

## 五、输出文件

每次成功运行会生成：

- `*_polished.md`：磨课后的完整教案。
- `*_process.json`：角色发言、修改记录及运行元数据。
- `*_scores.json`：启用 Judge 时生成的评分结果。

结果也可以在界面的“结果查看”页浏览和下载。

## 六、手动启动

在解压目录打开 PowerShell：

```powershell
cd code
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -r visualization\requirements.txt
.\.venv\Scripts\python.exe -m streamlit run visualization\app.py
```

## 七、常见问题

### 一直显示 0/4 位专家已发言

说明模型请求尚未返回。共享平台繁忙时单次调用可能需要一至数分钟，请等待或减少并发。模型名称必须在 API Key 的权限列表中。

### 返回 401、403 或模型无权限

检查 API Key，以及界面填写的模型 ID。不同用户可访问的模型可能不同。

### 返回 429

表示调用过于频繁或超过并发额度，稍后重试。

### 模型返回空文本或 JSON 解析失败

程序会自动尝试兼容 `reasoning_content` 并进行有限重试。若持续失败，可切换模型或降低输出长度。

### Judge 阶段耗时较长

Judge 会进行多次证据采样。调试时可以勾选“跳过 Judge 评审”。

### 关闭程序

回到启动程序的命令窗口，按 `Ctrl+C`，然后关闭窗口。

## 八、安全说明

- 不要把 API Key 写进 `configs/api.yaml`。
- 不要将包含 API Key 的截图、终端记录或配置文件发给他人。
- 模型生成内容只能作为教研辅助，最终教案应由教师审核。

