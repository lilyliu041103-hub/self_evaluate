AgentOS metric bridge v3.4
========================

框架将 AgentOS 标准输入包转换为共享证据，按 9 个 metric、21 项固定 rubric 为当前运行生成检查项，再执行程序评分和分组模型评分。
流程为 prepare → instantiate → 本地校验 → score。两个模型角色分别使用独立上下文和配置。
入口为 tools/evaluate_agentos.py。默认运行库位于 runtime/metric_rubric_impl_v4_modular，prepare、instantiate、score 均支持 --engine 指定运行库。

证据与评估对象
--------------

prepare 校验输入 ZIP 的文件哈希，整理任务、参考附件、回答、trace、工具声明、参数、返回和运行状态。
prepared/audit 保存原始输入、源 ZIP 与转换记录。证据记录带有稳定 ID、内容哈希和原始 JSON Pointer，供模型引用及程序绑定。
模型通过 query_evidence/read_evidence 按 ID 补读材料，省略、分页和读取预算均显式返回。

agent_state/tools/*.json 及源码版本声明进入 reference.agent_tool_catalog 和 tool_contract 证据，保留 schema、字段默认值、来源哈希、source_revision 与 historical_version_match。
运行时规范从 input.tools、明确标识的 tool_contracts 消息和动作 response_format 提取，分别标记业务 schema 与传输格式约束。
evidence.tool_bindings 按调用来源、事件关联、时间和版本筛选规范，记录适用、冲突、未来版本及具名源码快照等状态。
初始上下文为参考证据、观测证据和规范关联分别提供 22000、34000、8000 字符预算。超预算 schema 标记 read_required，完整内容可补读。

observation.tool_attempts 从正式输出、历史 assistant 动作及直接工具事件提取完整尝试清单，覆盖未执行、缺少返回、畸形参数和缺失字段的动作。
每个尝试使用日志位置派生的 evaluation_id，reported_call_id 保存原始 ID，raw 和 provenance.occurrences 保留原始内容及出现位置。
execution_evidence、protocol_errors、association_errors 分别记录执行证据、协议错误和关联冲突。已确认属于同一调用的重复记录合并为一个对象，并保留来源。
tool_calls 提供执行关联视图，评估对象覆盖以 tool_attempts 为准。

运行级实例化与本地校验
----------------------

instantiate 模型读取当前任务、可信参考、全部 rubric 和运行观测，按各 rubric 的 instantiate_prompt 展开独立检查项。
- AC-1 拆分交付答案中的非计算事实，AC-2 定位公开论证，两者绑定 final_answer 或 artifact。
- TC-1 从任务和参考要求拆分验收条件，并匹配交付证据。尚未匹配输出的明确要求也生成检查项。
- 过程 rubric 定位工具尝试、异常、恢复窗口和审批机会。TU-1、TU-2、TU-3 绑定工具过程观测。

每个检查保存 criterion、observation_ids、reference_ids、source_refs、P/J/H 方法和相应程序参数或 Judge 指令。
P 表示程序评分，J 表示模型评分，H 组合两者。AC-3、BC-1 采用 P-only，程序执行范围限于固定运行库注册的处理器。
AC-3 的预期常数、公式、非默认容差或单位转换须绑定可信参考或显式程序评估配置。
检查标准来自固定 rubric、任务、可信参考和适用工具规范，待评输出用于提供观测证据。

本地校验检查 21 项 rubric 覆盖、检查 ID、方法约束、证据引用、规则来源、程序路径和依赖关系。
TU-2 还核对工具尝试覆盖及可用业务 schema 对应的 P/H 检查，显式 J-only 配置保留语义检查范围。
发现覆盖缺口或结构错误时，同一 instantiate 角色最多修正一次。全部校验通过后冻结 agentos-rubric-plan/4 方案，失败时保存候选与诊断。
本地校验负责结构和可检查约束，事实切分、要求枚举及语义解释由实例化模型完成。

TU-2 的 P 检查可绑定 /observation/tool_attempts/<index>，先检查原始协议错误，再校验业务 schema。
protocol_only=true 与 schema_only=true 组合用于仅依据原始协议的检查。缺少原始载荷或明确截断时返回未知。
独立 J 检查使用具体任务依据和实际参数评估语义。源码快照检查须注明完整 source_revision 或 snapshot_id，P/H 另设 contract_basis="source_snapshot"。
具名源码快照的结论限定于该参考版本。适用的运行时业务规范存在冲突时，校验会阻止以源码快照替换规范。

评分与方案绑定
--------------

score 为每项检查绑定具体证据，附加必要的表格 schema、span 头、任务和运行状态，共享证据在分组请求内去重。
各 rubric 汇总已生成检查的实际评分，明确失败优先返回 false。检查证据不足或 rubric 没有检查时返回未知。
unresolved 保留实例化说明，报告通过 instantiation_notes 呈现。完整交付中可确认的遗漏可判失败，缺少交付内容或完整记录时按证据门槛处理。

方案绑定 task_fingerprint、run_fingerprint、固定 rubric 和代码指纹。同一运行可复用已校验方案。
回答、trace、运行状态、任务附件、字段契约或代码发生变化后，重新执行 prepare 和 instantiate。
当前评分使用 schema 4 冻结方案，旧 schema 2/3 方案需重新生成。

运行命令
--------

在项目根目录运行 PowerShell。环境安装和配置示例见根目录 README.md。
模型角色配置使用根目录 model-roles.local.json，config_path 相对该配置文件解析。服务需支持 OpenAI-compatible chat/completions、JSON 输出和 function tools。
每次运行使用不存在或为空的输出目录。

整理标准输入包，仅执行本地处理。

.\evaluate.ps1 prepare --input inputs/eval-input.zip --out outputs/prepared

使用 instantiate 角色生成并校验方案。

.\evaluate.ps1 instantiate --bundle outputs/prepared --model-roles model-roles.local.json --out outputs/run-plan

使用 score 角色执行评分。

.\evaluate.ps1 score --bundle outputs/prepared --plan outputs/run-plan/plan.json --model-roles model-roles.local.json --out outputs/score

instantiate 省略 --model-roles 时导出实例化请求。score 省略 --model-roles 时执行本地程序检查并导出分组模型请求。
静态模板诊断使用 score --template-preview，并选择独立输出目录。真实评分需要上述已冻结方案。

按证据类型查询记录，再按返回的 ID 读取内容。

.\evaluate.ps1 query --bundle outputs/prepared --scope observation --kind trace_event
.\evaluate.ps1 read --bundle outputs/prepared --id task:request

执行本地回归测试与依赖检查。

.\verify.ps1

调用预算与输出
--------------

一次完整评估包含 1 次实例化生成、至多 1 次本地诊断驱动的修正，以及 G 次分组模型评分。
G 由检查数量、共享证据、--max-chars 和 --max-checks 决定，纯 P 检查在本地执行。
每次逻辑模型调用默认允许 2 轮补读证据，最多发起 3 次 HTTP 请求。--max-tool-rounds 可设为 0..8，缓存命中可减少请求。
单轮最多 8 个工具调用，单次逻辑调用累计最多 16 个，工具返回累计最多 60000 字符。传输错误、截断或超出预算时停止并保存记录。

实例化目录保存 instantiate.request.json、instantiate.response.json、requests.json、status.json，以及 attempts/ 中的每轮候选。校验通过后写入 plan.json。
评分目录保存 report.json、bound-checks.json、selection.json、requests.json 和 run-binding.json。
api-calls 保存模型请求、响应与 calls.jsonl，记录角色、模型、usage、耗时及成功、缓存或失败状态。认证配置与运行记录分别管理。
