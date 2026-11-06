# 自适应评估框架

AgentOS metric bridge v3.5，将 AgentOS 标准输入包转换为可追溯证据，按当前运行实例化检查项，再执行程序检查与模型评分。配置版本为 4.2.0，内置 9 个 metric、22 个 rubric，运行方案使用 schema 4。

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

实例化使用当前任务、参考资料和运行观测生成检查项，经本地结构、引用、方法及工具规范覆盖校验后保存 `plan.json`。评分默认按 metric 隔离模型请求，同一 metric 内按证据重叠及预算分组，输出 `report.json` 等记录。缺少必要证据或没有可评分检查项时保留未知。

运行方案绑定任务、观测和代码指纹。更换输入、运行库或评估代码后，重新执行 `prepare` 和 `instantiate`。schema 2、3 的旧方案需重新生成。

### EG-3 必要输入获取完整性

EG-3（Necessary Input Acquisition）按具体任务目标，比较原材料已提供的必要信息与运行在截止前实际取得的信息。可核验的实际输入、读取返回和保留必要语义的可信摘要均可作为获取证据。检查采用 J，由现有实例化阶段按目标定位必要信息及其来源，不新增独立的全局证据清单生成阶段，也不要求读取全部附件或使用固定工具。

默认检查截止点是最终交付前。首轮摘录遗漏、后续补读取得必要信息的目标可以通过。只有任务或阶段契约明确要求该阶段交付完整输入时，才使用更早的截止点。评估范围覆盖平台和 Agent 的整次获取过程，获取结果与模型、解析、选段等环节的责任归属分别记录。

原材料足以支持目标，且必要信息在截止前实际取得并保留对象、状态、时间、单位等关键语义时判 yes。必要信息已明确存在于原材料中，而完整记录证明截止时仍未取得或关键语义丢失时判 no。来源本身不足，或获取日志、必要性、截止点、语义等效性等证据不足以确定结果时保留 unknown。目标没有相关资料依赖时由上游判定 NA。补读或恢复是否可行尚不明确，不改变已经证实的必要输入获取不足。

EG-1 继续检查实际行动是否有事前证据支持，EG-2 检查证据来源、版本与时效。EG-3 检查目标所需信息的获取覆盖，独立于最终答案正确性和交付完成度。

按 T06 现有证据应用该定义，验收截止与付款到期两个目标的必要输入获取预期判 no，金额与账户暂停依据的获取预期判 yes。此处为规则应用示例，T06 的新版自动评分结果须在重新实例化和评分后生成。

### 第一期：独立指标评分与公共前缀

本次改造覆盖评分阶段。实例化继续一次处理全部 22 项 rubric；P/H 程序检查、全局依赖调度和 check → rubric → metric 汇总沿用原流程。

评分默认使用 `--scoring-mode metric --common-evidence-mode packs`。公共部分在本轮评分开始时生成一次，按稳定 ID 组织任务原文、各指标共同选中的证据及可定位索引。补充证据按引用它们的检查项集合形成固定证据包，供需要相同材料的指标复用；模型请求顺序为固定 system → 公共内容 → 证据包原文 → metric 名称与当前批次 checks。原文保留来源、哈希和完整性信息；较大的字段仍通过原有证据引用和工具补读。

证据包沿用已有 trace 事件、载荷、字段和表格记录。引用检查项集合完全相同的记录组成一个包，优先排列被更多指标/检查复用的包，包内按 ID 排序；包划分和顺序跨依赖轮次固定。每组使用的证据与旧 `shared` 模式相同，不为缓存加入额外记录。一个检查需要多个包时一起提供，所有请求仍受完整字符预算约束。`--common-evidence-mode shared` 保留精简索引版布局，`none` 保留原分组布局，方便对照。

评分输出 `evidence-packs/<内容哈希>.json` 和 `evidence-packs/manifest.json`，保存完整原文及请求引用关系。请求仍内联原文；这些本地文件用于保存和核验材料，不是服务端缓存句柄。服务端只复用连续相同的前缀；独立缓存过 A 和 B 不代表可以直接拼接其 KV 缓存。当前沿用隐式缓存，由实际评分请求建立和复用，无额外预热调用。

公共索引只包含 `id`、`kind` 和 `scope`，独立上限为 2000 字符，并受公共前缀总预算约束。模型可按 ID 读取含来源、哈希和完整性信息的完整证据。超出索引预算时保留数量与不完整标记，其余证据可通过 `query_evidence` 定位。

先用同一份新生成的冻结方案导出四组请求，检查结构。这些命令不调用模型。

```powershell
.\evaluate.ps1 score --bundle outputs/prepared --plan outputs/run-plan/plan.json --scoring-mode baseline --common-evidence-mode none --out outputs/baseline-preview
.\evaluate.ps1 score --bundle outputs/prepared --plan outputs/run-plan/plan.json --scoring-mode metric --common-evidence-mode none --out outputs/metric-preview
.\evaluate.ps1 score --bundle outputs/prepared --plan outputs/run-plan/plan.json --scoring-mode metric --common-evidence-mode shared --out outputs/shared-preview
.\evaluate.ps1 score --bundle outputs/prepared --plan outputs/run-plan/plan.json --scoring-mode metric --common-evidence-mode packs --out outputs/packs-preview
```

前两组比较评分阶段的指标隔离，第三组考察公共证据组织，第四组与第三组比较证据包排序和指标名称后移。`shared` 和 `packs` 相对 `none` 可能增加某个批次可见的公共背景，报告会记录这一点。真实对照使用相同 plan、证据包、模型和检索预算，在命令后加 `--model-roles model-roles.local.json`。测量服务端缓存时同时加 `--no-response-cache`，并为每次重复使用新的输出目录。

`requests.json` 导出实际初始 system/user、metric、公共前缀哈希和预算。`report.json` 中的 `execution.client` 分别记录本地 `response_cache_hits`、真实请求的 `server_usage` 和按指标统计的 `by_metric`。服务未报告的 token 数保持 `null`，`*_usage_calls` 表示各项统计覆盖了多少次调用。混合指标的基线请求归入 `__unassigned__`，不重复摊入各指标。`api-calls/calls.jsonl` 保存每次调用的原始 usage、耗时和前缀哈希。

`packs` 模式另外导出 `evidence_pack_ids`、各包结束位置的累计 `pack_prefixes` 和完整证据前缀的 `evidence_prefix_hash/chars`。客户端在调用前校验这些哈希，并在审计中记录包含工具定义的有效哈希；同一个包在不同前文之后会有不同的累计前缀哈希。哈希表示可复用的文本边界，不代表服务端已经命中，命中量仍以 API usage 为准。版本记录见 [CHANGELOG.md](CHANGELOG.md)。

`--max-chars` 默认 64000，预算计入初始 system/user、工具定义与消息封装，并为输出和补取各预留 8000 字符。公共前缀默认最多 12000 字符，可用 `--common-prefix-max-chars` 调整；另有 `--output-reserve-chars` 和 `--tool-reserve-chars`。这些值用于字符预算，模型输出 token 上限仍由角色配置的 `max_tokens` 控制。`--tool-reserve-chars 0` 需配合 `--max-tool-rounds 0`。超预算请求和证据不足的检查返回未知并保留诊断。

缓存收益依据真实 API 返回统计。建议后续选择 10—20 条带人工判定的 trace，每种方案重复 3 次，覆盖正常完成、工具错误、交付缺失和证据不足；分别比较判定一致率、unknown、补取次数、token 与耗时。

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
