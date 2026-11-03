# 自适应评估框架

AgentOS metric bridge v3.4，将 AgentOS 标准输入包转换为可追溯证据，按当前运行实例化检查项，再执行程序检查与模型评分。内置 9 个 metric、21 个 rubric，运行方案使用 schema 4。

## 安装与验证

使用 Python 3.12。在项目根目录的 PowerShell 中运行以下命令。

```powershell
.\setup.ps1
.\evaluate.ps1 --help
.\verify.ps1
```

也可通过 `.\setup.ps1 -Python <python.exe路径>` 指定解释器。`verify.ps1` 执行回归测试和依赖检查，模型调用次数为零。验证记录写入 `outputs/verification/`。

Linux、macOS 或直接使用 Python 时，在项目根目录运行以下命令。

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock.txt
python tools/evaluate_agentos.py --help
python -m unittest discover -s tools/eval_bridge/tests -v
```

## 评估流程

准备 AgentOS 导出的 `eval-input.zip`，放入 `inputs/`。压缩包应包含 `eval-input/package.json`、`checksums.json`，以及观测描述引用的任务、附件和 trace 文件。输入含多条观测时，用 `--observation <observation_id>` 选择一次运行。

### 1. 整理输入与预览实例化请求

```powershell
.\evaluate.ps1 prepare --input inputs/eval-input.zip --out outputs/prepared
.\evaluate.ps1 instantiate --bundle outputs/prepared --out outputs/instantiate-preview
```

以上命令在本地校验输入、建立证据索引并导出请求，模型调用次数为零。每次运行使用新建或空的输出目录。

### 2. 配置模型

```powershell
Copy-Item model-api.example.json model-api.local.json
Copy-Item model-roles.example.json model-roles.local.json
```

在 `model-api.local.json` 中填写服务的 `base_url`、`api_key` 和 `model`。服务需支持 OpenAI-compatible Chat Completions、JSON 输出和 function tools。按所用模型限制调整 `model-roles.local.json` 中的 `max_tokens`。两个角色可分别设置模型或关联不同 API 配置，`config_path` 相对角色配置文件解析。

密钥保存在 `*.local.json` 中，此类文件以及 `.env`、`inputs/`、`outputs/` 均由 `.gitignore` 排除。仓库内的 example 配置使用占位值。

### 3. 实例化与评分

以下命令会调用配置的模型 API。

```powershell
.\evaluate.ps1 instantiate --bundle outputs/prepared --model-roles model-roles.local.json --out outputs/run-plan
.\evaluate.ps1 score --bundle outputs/prepared --plan outputs/run-plan/plan.json --model-roles model-roles.local.json --out outputs/score
```

实例化使用当前任务、参考资料和运行观测生成检查项，经本地结构、引用、方法及工具规范覆盖校验后保存 `plan.json`。评分按证据执行程序检查或分组模型判断，输出 `report.json` 等记录。缺少必要证据或没有可评分检查项时保留未知。

运行方案绑定任务、观测和代码指纹。更换输入、运行库或评估代码后，重新执行 `prepare` 和 `instantiate`。schema 2、3 的旧方案需重新生成。

### 4. 查询证据

```powershell
.\evaluate.ps1 query --bundle outputs/prepared --scope observation --kind trace_event
.\evaluate.ps1 read --bundle outputs/prepared --id task:request
```

## 目录

| 路径 | 内容 |
| --- | --- |
| `tools/evaluate_agentos.py` | 命令行入口 |
| `tools/agentos_metric_adapter.py` | AgentOS 输入包转换与校验 |
| `tools/eval_bridge/` | 证据、实例化、校验、评分和回归测试 |
| `runtime/metric_rubric_impl_v4_modular/` | metric 运行库、rubric 配置与提示词 |
| `model-*.example.json` | 模型配置模板 |
| `requirements.lock.txt` | 已验证的依赖版本 |

架构、证据规则与调用预算见 `tools/eval_bridge/README.txt`。回归测试使用合成数据；本机私有输入对应的集成测试会在输入缺失时跳过。
