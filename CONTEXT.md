# MAP 领域术语

本文件只定义 MAP 领域中的统一语言。架构、接口和存储细节分别见 `docs/` 与 `SPEC/`。

## Workspace（工作空间）

隔离一组用户、配置、执行记录和资源的租户范围。所有有归属的数据都属于且只属于一个
Workspace。

_避免：Tenant、Project、Namespace。_

## Principal（主体）

发起请求或代表其他主体执行操作的已识别参与者，可以是用户或服务身份。

_避免：Actor、Caller、Operator。_

## Conversation（会话）

用户可见的连续交流容器，按顺序包含 Message，并可触发一个或多个 Run。

_避免：Session、Chat、Thread。_

## Message（消息）

Conversation 中由用户、助手或系统产生的一条交流事实。Message 表达内容，不代表一次
执行生命周期。

_避免：Prompt、Turn、Run。_

## Run（运行）

对一个用户意图的持久执行实例，具有独立身份、生命周期和最终结果。

_避免：Task、Execution、Request、Job。_

## Step（步骤）

Run 内具有明确输入、输出和状态的一段工作。Step 可以被重试，但其业务含义保持不变。

_避免：Stage、Node、Task。_

## Attempt（尝试）

对同一个 Step 的一次具体执行。重试会产生新的 Attempt，而不是改写已结束的 Attempt。

_避免：Retry、Generation、Try。_

## Invocation（调用）

Attempt 中对受治理能力的一次有身份、有状态的调用，是 Model Invocation 和 Tool
Invocation 的统称。

_避免：Call、Action、Operation。_

## Model Invocation（模型调用）

向模型提交上下文并取得输出的一次 Invocation，包括选择、用量、延迟与结果语义。

_避免：LLM Call、Completion。_

## Tool Invocation（工具调用）

请求工具读取信息或产生结果的一次 Invocation；若可能改变外部状态，则同时构成 Effect。

_避免：Function Call、Tool Run。_

## Effect（副作用）

可能改变 Run 之外状态的操作。Effect 必须能区分未开始、结果确定与结果不确定。

_避免：Write、Action、Mutation。_

## Approval（审批）

授权某个尚未执行的受控操作继续进行的显式决定。Approval 不等同于执行结果。

_避免：Confirmation、Permission、Review。_

## Event（事件）

描述 Run 生命周期中已发生事实的有序、不可变记录。Event 用于重放状态，不是命令。

_避免：Log、Message、Notification。_

## Checkpoint（检查点）

Run 在特定位置可恢复状态的持久快照，必须能关联到产生它的 Event 位置。

_避免：Cache、Backup、Snapshot。_

## Artifact（制品）

由 Run 消费或产生、拥有稳定身份和完整性信息的内容对象，适合承载较大或需独立保留的
数据。

_避免：File、Blob、Payload。_

## Attachment（附件）

由用户提供并关联到 Message 或 Run 输入的内容引用。Attachment 描述使用关系；其内容可以
由 Artifact 承载。

_避免：Upload、File、Artifact。_

## Runtime Snapshot（运行时快照）

Run 开始时解析并固定的有效配置集合。之后的配置变化不应改变该 Run 的解释。

_避免：Config、Runtime Config、Admin State。_

## Agent（智能体）

在 Run 中承担明确职责、使用模型和工具完成工作的执行角色。Agent 不是独立的持久执行
实例。

_避免：Bot、Worker、Assistant。_

## Scenario（场景）

描述某类业务意图、可用能力和执行策略的命名规则集合。

_避免：Scene、Use Case、Mode。_

## Skill（技能）

Agent 可发现、可治理、可组合的专用能力说明及其资源集合。

_避免：Tool、Plugin、Capability。_

## Flow（流程）

按依赖关系组织多个 Step 的执行结构。Flow 描述结构，不代表某一次 Run。

_避免：Pipeline、DAG、Workflow。_

## Job（作业）

交给 Worker 领取和推进的持久工作指令。Job 是调度机制，不是用户可见的 Run。

_避免：Task、Run、Queue Item。_

## Outbox Event（发件箱事件）

与业务事实在同一提交中登记、等待可靠投递的通知意图。它不是领域 Event 的替代品。

_避免：Event、Message、Job。_

## Evidence（证据）

用于证明某项验收结论的、可追溯到具体实现和验证过程的记录集合。

_避免：Report、Log、Artifact。_
