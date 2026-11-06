AgentOS metric bridge v3.5
========================

框架将 AgentOS 标准输入包转换为共享证据，按 9 个 metric、22 项固定 rubric 为当前运行生成检查项，再执行程序评分和分组模型评分。
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
- EG-3 按目标比较原附件必要信息与最终交付前实际取得的输入，接受后续补读和可靠等效摘要；不新增全局证据清单生成阶段。仅在明确要求阶段完整输入时提前检查，取得缺口不自动归责模型。
- 过程 rubric 定位工具尝试、异常、恢复窗口和审批机会。TU-1、TU-2、TU-3 绑定工具过程观测。

每个检查保存 criterion、observation_ids、reference_ids、source_refs、P/J/H 方法和相应程序参数或 Judge 指令。
P 表示程序评分，J 表示模型评分，H 组合两者。AC-3、BC-1 采用 P-only，程序执行范围限于固定运行库注册的处理器。
AC-3 的预期常数、公式、非默认容差或单位转换须绑定可信参考或显式程序评估配置。
检查标准来自固定 rubric、任务、可信参考和适用工具规范，待评输出用于提供观测证据。

本地校验检查 22 项 rubric 覆盖、检查 ID、方法约束、证据引用、规则来源、程序路径和依赖关系。EG-3 与 TC-1 均须绑定任务或参考来源，不能只引用通用 rubric 自拟要求。
TU-2 还核对工具尝试覆盖及可用业务 schema 对应的 P/H 检查，显式 J-only 配置保留语义检查范围。
发现覆盖缺口或结构错误时，同一 instantiate 角色最多修正一次。全部校验通过后冻结 agentos-rubric-plan/4 方案，失败时保存候选与诊断。
本地校验负责结构和可检查约束，事实切分、要求枚举及语义解释由实例化模型完成。

引用校验仍要求 source_refs.quote 为逐字原文，不忽略标点或引号。
引用不匹配时，诊断提供 rubric_id、check_id、source_ref_index、evidence_id、
原引用预览、相近原文片段与字符编辑差异，并传入自动修正反馈。相近片段仅用于
定位，不自动替换引用，也不作为通过依据；没有合适片段时要求重新读取来源。
实例化返回完整但无法解析的 JSON 时，客户端最多额外请求一次格式修正，提供
解析错误的行、列和位置，禁止补调工具，随后仍执行全部本地校验。原始请求与
响应均保留，修正调用标记为 instantiate_json_repair。截断输出和超过 60000
字符的无效输出不进入该修正；评分阶段的预算及失败行为保持不变。

TU-2 的 P 检查可绑定 /observation/tool_attempts/<index>，先检查原始协议错误，再校验业务 schema。
protocol_only=true 与 schema_only=true 组合用于仅依据原始协议的检查。缺少原始载荷或明确截断时返回未知。
独立 J 检查使用具体任务依据和实际参数评估语义。源码快照检查须注明完整 source_revision 或 snapshot_id，P/H 另设 contract_basis="source_snapshot"。
具名源码快照的结论限定于该参考版本。适用的运行时业务规范存在冲突时，校验会阻止以源码快照替换规范。

评分与方案绑定
--------------

score 为每项检查绑定具体证据，附加必要的表格 schema、span 头、任务和运行状态。
第一期默认 --scoring-mode metric：每个模型请求只含一个 metric 的检查规则，同一 metric 内继续按 rubric family、证据重叠和预算分组。
--scoring-mode baseline 保留按 rubric family 混合指标的分组方式。两种模式使用同一份冻结方案和全局程序依赖调度，P/J/H 与汇总规则相同。

--common-evidence-mode packs 为默认布局。在评分开始时构造一次公共证据前缀和补充证据包，跨分组、跨依赖轮次复用。
请求顺序为固定 system → 公共任务和证据索引/原文 → 本组所需证据包原文 → metric 名称 → 当前批次 checks。
引用检查项集合完全相同的证据记录组成一个包，按指标复用数、检查复用数递减及内容哈希排序，包内按 ID 排序。
现有 trace 事件、输入输出、字段引用及来源信息保留；每组证据与 shared 相同，一个检查所需的多个包一起提供。包大小不改变完整请求预算。
evidence-packs/<内容哈希>.json 保存完整包，manifest.json 记录包内证据 ID、复用数及请求组。模型请求继续内联原文。
证据包文件是本地材料，不是服务端 KV 句柄；缓存复用要求此前全部内容及顺序一致，不能任意拼接独立块的 KV 缓存。
沿用自动隐式缓存，由真实评分请求建立和复用，不增加专用预热请求。
公共内容按稳定 ID 排序，证据原文保留来源、内容哈希和完整性标记。任务与各参评指标共同选中的证据优先。
索引仅保留 id/kind/scope，独立限制为 2000 字符并受公共前缀总预算约束。按 ID 补读时返回完整记录及其元数据。
索引超限显式标记不完整；未内联原文仍可通过 evidence_ids 和 query_evidence/read_evidence 获取，不据索引推断事实缺失。
metric 名称、批次与规则只进入证据之后的后缀，运行时间和请求编号只进入审计。common_prefix_hash 标识 system 与 user 公共部分，effective_prefix_hash 额外纳入工具定义及顺序。
evidence_pack_ids 标识本组的包；pack_prefixes 记录各包末尾的累计前缀长度和哈希，evidence_prefix_hash/chars 记录全部证据之后、指标规则之前的边界。客户端调用前验证实际文本，并记录包含工具定义的有效哈希。
--common-evidence-mode shared 保留上一版公共前缀 → metric 名称 → 补充证据 → checks 的布局。
--common-evidence-mode none 保留原分组证据布局，适合 baseline/metric 的纯隔离对照。shared/packs 相对 none 可能增加批次可见背景，execution.evidence_visibility_expanded 和各组 additional_common_evidence_ids 记录差异。

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

同一冻结方案的第一期离线对照：

.\evaluate.ps1 score --bundle outputs/prepared --plan outputs/run-plan/plan.json --scoring-mode baseline --common-evidence-mode none --out outputs/baseline-preview
.\evaluate.ps1 score --bundle outputs/prepared --plan outputs/run-plan/plan.json --scoring-mode metric --common-evidence-mode none --out outputs/metric-preview
.\evaluate.ps1 score --bundle outputs/prepared --plan outputs/run-plan/plan.json --scoring-mode metric --common-evidence-mode shared --out outputs/shared-preview

真实评分加 --model-roles；服务端缓存测量再加 --no-response-cache。每次使用新的输出目录。
新增代码改变适配器指纹，先重新 prepare/instantiate，再让以上模式共用新方案。第二期按 metric 实例化暂未实施。

按证据类型查询记录，再按返回的 ID 读取内容。

.\evaluate.ps1 query --bundle outputs/prepared --scope observation --kind trace_event
.\evaluate.ps1 read --bundle outputs/prepared --id task:request

执行本地回归测试与依赖检查。

.\verify.ps1

调用预算与输出
--------------

一次完整评估包含 1 次实例化生成、至多 1 次本地诊断驱动的修正，以及 G 次分组模型评分。
G 由评分模式、检查数量、共享证据、--max-chars 和 --max-checks 决定，纯 P 检查在本地执行。
score 的 --max-chars 默认 64000，计初始 system/user、固定工具和消息封装、输出预留及补取预留。
--common-prefix-max-chars 默认 12000；--output-reserve-chars、--tool-reserve-chars 默认各 8000。字符预算与 API 的 max_tokens 分别配置。
每次逻辑模型调用默认允许 2 轮补读证据，最多发起 3 次 HTTP 请求。--max-tool-rounds 可设为 0..8，本地响应命中可减少 HTTP 请求。
单轮最多 8 个工具调用，单次逻辑调用累计最多 16 个。score 补取限制在配置预留内，实例化沿用工具返回累计 60000 字符上限。
--tool-reserve-chars 0 需配合 --max-tool-rounds 0。传输错误、截断或超出预算时停止当前分支并保存记录，检查保留未知。

实例化目录保存 instantiate.request.json、instantiate.response.json、requests.json、status.json，以及 attempts/ 中的每轮候选。校验通过后写入 plan.json。
评分目录保存 report.json、bound-checks.json、selection.json、requests.json 和 run-binding.json。
评分模型为每个 check 返回 status、verdict 和非空 reason，解释判定依据及相关证据 ID；无法核验时说明具体缺口。report.checks 保存解释，rubrics 提供 check_ids 和 failed_check_ids，metrics 提供 failed_rubric_ids。
H 检查的 reason 同时注明程序与模型各自结果，保留模型解释。兼容历史仅含判分的返回，在 execution.diagnostics 标记 missing_judge_reason；空白或类型错误的 reason 标记 invalid_result_reason，该检查保留未知。
requests.json 保存实际初始 system/user、metric_id(s)、common_prefix_hash、common_prefix_chars 及完整字符预算，便于离线检查请求。
api-calls 保存模型请求、响应与 calls.jsonl，逐次记录角色、metric、usage、normalized_usage、耗时及成功、本地缓存或失败状态。认证配置与运行记录分别管理。
report.execution.client.response_cache_hits 仅计本地响应命中；server_usage 仅累计真实调用的输入、缓存输入及输出 token 和耗时；by_metric 保存分指标统计。
DeepSeek prompt_cache_hit_tokens 与 OpenAI-compatible prompt_tokens_details.cached_tokens 均可读取。缺失统计为 null，*_usage_calls 表示有效报告次数。
混合指标基线调用进入 __unassigned__ 统计，避免一个请求被重复累计。禁用本地缓存时，每次真实请求/响应各自保存，保留全部观测。
report.execution.duration_seconds 记录评分执行耗时。费用需按实际服务价格另算，前缀一致与缓存命中/成本收益分别验证。
