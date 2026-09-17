BANNED_TASKS = """禁止生成或放行这些题型及换皮版本：经典小游戏与图形模拟；通用命令行、本地文件和桌面小工具；电商、订单、RBAC、库存、OA、CMS、挂号、CRM、聊天、拍卖、停车、工单、预约等通用业务 CRUD；报表、CSV 看板、记账、健身、菜谱、天气、番茄钟、习惯、播放器、旅行或观影记录等常见页面。"""

DIFFICULTY_RULES = """只有困难或地狱可进入 A/B。困难必须整合多个模块或系统约束，做关键设计取舍，并处理复杂状态、兼容性、权限、并发、性能或异常链路；地狱还要求架构级判断、多步复杂调试或大量边界场景。文件多、页面多、字段多、题面长不能单独证明难度。"""


def task_validation_prompt(task_text: str, known_titles: str) -> str:
    return f"""你负责新系统的题目准入复核。根据给定题目判断是否可以进入 A/B 开发。

{DIFFICULTY_RULES}
{BANNED_TASKS}

还要检查：必须能用 Docker Compose 清洁启动；应包含可操作的功能验收；不能只是改名换皮；与已有题目不能在核心功能、交互、数据模型、验收或技术实现上高度重复。

判定应依据题面真实内容，不能用内部字段是否单独填写代替事实判断。zero_to_one 本来就是从空仓库开始，baseline_path 为空属于正确基线，baselineReady 应判为 true。历史任务的 acceptance 数组可能为空；只要原始 prompt 已经写明 Docker Compose、验证服务、接口结果或可执行验收场景，就视为具备验收条件，不能仅因 acceptance 字段为空拒绝。只有发现明确的难度不足、禁出题、实质重复、Feature/Bug 缺少准确任务前代码，或题面确实无法验收时才拒绝；不要把可由系统直接整理的元数据缺项当成阻塞。

已有题目摘要：
{known_titles or '无'}

待复核题目：
{task_text}

只按 Schema 返回事实结论。"""


def task_generation_prompt(existing: str, task_type: str = "zero_to_one") -> str:
    return f"""生成一个可真实开发并验收的 {task_type} 编程任务。

{DIFFICULTY_RULES}
{BANNED_TASKS}

直接生成困难或地狱任务，不先生成低难度再升级。projectCategory 必须明确选择纯后端、纯前端或全栈；纯后端不得创建前端，纯前端不得创建业务后端，全栈必须通过真实 API 联调。题面要给出明确业务背景、复杂状态或异常链路、技术约束、边界条件与验收条件；必须要求 Dockerfile、Docker Compose、健康检查、可配置宿主机端口，并保证克隆后只依赖 Docker 即可运行。避免复述实现方案，给开发者保留关键设计取舍。

已有题目标题与摘要，必须避免雷同：
{existing or '无'}

只按 Schema 输出。"""


def feature_generation_prompt(original_task: str, artifact_summary: str, existing: str, project_category: str = "") -> str:
    return f"""为一个已经完成并通过 Docker 验收的项目生成下一轮 Feature 迭代任务。

{DIFFICULTY_RULES}
{BANNED_TASKS}

必须在现有产品和代码结构上增加真实的新能力，保留现有功能、接口与 Docker Compose 验收链路。projectCategory 必须保持为 {project_category or '原项目类别'}，不得把纯后端擅自改成全栈或给纯前端增加业务后端。直接生成困难或地狱任务，复杂度必须来自跨模块状态、性能、并发、异常恢复或兼容性等真实约束，不能靠堆字段或扩大文字。题面要明确新行为、边界条件与可执行验收，但给开发者保留实现取舍。taskType 必须为 feature。

原始任务：
{original_task}

当前获胜产物摘要：
{artifact_summary}

已有题目标题与摘要，必须避免雷同：
{existing or '无'}

只按 Schema 输出。"""


def gsb_prompt(task: str, a_evidence: str, b_evidence: str,
               process_evidence: str = "[]") -> str:
    return f"""比较同一道开发任务的 A、B 两份最终产物，给出 GSB 结论。只依据可见题面、代码提交、Docker 验收、真实操作录像和开发轨迹，不索取内部思维。

结论只能是 A better、Same 或 B better。只填写 aReason 和 bReason 两段，不生成单独的偏好依据。aReason 说明 A 的可见操作、产物、验收结果和具体问题，并自然交代这些事实如何支持最终结论；bReason 对 B 做同样说明。两段合起来必须能直接看出为什么选择 A better、Same 或 B better。

写得像同事看完轨迹后在说明判断，重点是口语化、好理解，不要为了显得简短而删掉能支撑结论的证据。每段先概括这一侧做成了什么，再结合真实操作、测试、报错、Bug 或未验证项说明结果，最后自然交代为什么更好、稍弱或与另一侧接近。篇幅由证据决定，可以保留多个有因果关系的开发与验证事实，也可以写必要的测试数字和代码位置；不要机械罗列与结论无关的数字、文件或实现细节。

公开理由不得出现“第175步”“第207、212步”这类轨迹行号，也不要写“某测试文件第 15、16、21、22 项通过”。应直接说做了什么和结果如何，例如“实际跑过临界值两侧、写回和输入变化后的失效处理”。“83 个单测、23 个端到端测试、38 秒录像均通过”这类数字堆叠也不够口语化；录像时长只能说明录像文件合规，不能用来证明功能没有缺陷。需要保留的具体证据仍要保留，不能只剩“整体很好”“未见问题”这类空话。

具体位置必须来自 traceEvidence，例如文件、函数、关键命令、接口状态或报错原文。traceEvidence 的 step 只供内部找到证据，不写进 aReason 或 bReason。某次测试先失败、后来修好时，要把失败原因和最终结果连起来说清楚，不能把已经修好的问题写成最终缺陷。discoveredBugs 中已复现并影响比较的 Bug 要写清触发场景和客观结果；静态猜测或未复现候选不能当成事实。某一侧没有跑业务测试，可以直接说“没有实际跑接口流程”，不能把编译通过说成业务已验证。

每段控制在 Schema 允许的 20–300 个字符内，以把判断说明白为准，不额外追求最短。使用自然、口语化中文，不使用反引号、Markdown 列表、JSON 文本或模型名称。不要因为一侧步骤更多就判优劣。选择 Same 时也要分别说明两边表现接近的关键原因。

任务：
{task}

A 证据：
{a_evidence}

B 证据：
{b_evidence}

A/B 当前有效开发过程事件：
{process_evidence}

只按 Schema 返回。"""


def gsb_recheck_prompt(task: str, verdict: str, a_reason: str, b_reason: str,
                       evidence: str) -> str:
    return f"""复检一条 A/B 开发任务的公开 GSB 理由。只依据给出的可见证据检查，不修改代码，也不索取内部思维。

复检首先检查首次生成的 GSB 逻辑是否正确：结论是否由 A/B 事实支持，事实是否归到了正确一侧，开发过程与后续独立验收是否混淆，已经修好的中间失败是否被误写成最终缺陷，是否遗漏会改变结论的已复现 Bug 或未验证范围，以及测试、文件、命令和结果是否真实存在。A、B 理由要分别描述对应产物，并把偏好自然融入两段，不新增单独的偏好依据。

其次检查表达是否口语化、连贯、自己能看懂。评价可以保留多处真实操作、必要数字、代码位置和测试结果，只要这些信息确实支撑结论；不要因为理由较长或证据较多就要求精简，也不能把有说服力的事实删成空泛总结。但公开理由一律不保留“第175步”这类轨迹行号，应该改成对应的操作、测试场景或报错。只有机械堆叠无关细节、语句明显生硬或读不懂时，才建议整理其他表达；改写时必须保留原评价中所有会影响结论的有效证据。

明确区分“证据详细”和“机械罗列”：有因果关系的开发事实、真实失败与恢复、影响结论的测试结果可以详细写；轨迹步骤号、连续的测试用例编号、同时堆单测数量和端到端测试数量，或用录像秒数推出“没有功能缺陷”，属于不口语化，应返回 suggested_revision。改写时把编号换成对应的业务场景，把测试结果与结论连起来，不能简单删除后只剩“验收通过、整体很好”。

每段仍需有可核对的具体位置，例如文件、函数、关键命令、接口状态或报错。traceEvidence 的 step 只用于内部核对，建议理由中不得出现第几步。缺少证据、逻辑有明显问题或表达不口语化时返回 suggested_revision，并给出证据充分的完整版本；结论与证据相反、关键事实错误或引用不存在时使用 fact_conflict；事实、逻辑和口语化表达都可接受时返回 passed。不得仅因篇幅、数字数量或代码细节较多判为需要修改。建议内容保持在每段 20–300 个字符内，不含反引号、Markdown、JSON 或模型名称。

任务：
{task}

当前结论：{verdict}

当前 A 理由：
{a_reason}

当前 B 理由：
{b_reason}

可见证据：
{evidence}

只按 Schema 返回。"""


def bug_discovery_prompt(task: str, arm: str, commit_sha: str, docker_evidence: str) -> str:
    return f"""你负责在已经真实开发并通过 Docker 初步验收的产物中寻找 Bug。当前只做只读代码和证据分析，提出可由系统随后在清洁 Docker 环境中实际复现的候选；不能把静态猜测直接写成已复现事实。

优先寻找复杂状态、并发、持久化、兼容性、权限、性能边界或异常恢复中的真实缺陷。候选必须给出明确前置条件、逐步操作、预期结果、预计实际结果、代码位置和修复复杂度。每个候选还必须提供 reproductionCommands：每项只写 docker compose 的子参数，例如 ["exec","-T","api","pytest","tests/test_x.py::test_case"] 或 ["run","--rm","verify",...]；系统会统一补上 docker compose、项目名和 Compose 文件，并在两次全新启动中执行。命令必须只读取或测试当前产物，不能修改源码、宿主设置、Git 或其他项目。expectedExitCode 与 expectedOutputContains 要能客观判断该缺陷是否复现。没有现成的可重复命令就不要输出该候选。

修复复杂度只有困难或地狱才可能生成 Bug Pair；简单和中等也可以记录，但会被标记为 difficulty_rejected。API 限流、证书、网络临时中断和机器资源不足不是产品 Bug。找不到可信候选时返回空 candidates，禁止编造。

原任务：
{task}

待检查产物：Arm {arm}，提交 {commit_sha}

现有 Docker 验收证据：
{docker_evidence}

只按 Schema输出。"""
