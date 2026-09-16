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

直接生成困难或地狱任务，不先生成低难度再升级。题面要给出明确业务背景、复杂状态或异常链路、技术约束、边界条件与验收条件；必须要求 Dockerfile、Docker Compose、健康检查、可配置宿主机端口，并保证克隆后只依赖 Docker 即可运行。避免复述实现方案，给开发者保留关键设计取舍。

已有题目标题与摘要，必须避免雷同：
{existing or '无'}

只按 Schema 输出。"""


def feature_generation_prompt(original_task: str, artifact_summary: str, existing: str) -> str:
    return f"""为一个已经完成并通过 Docker 验收的项目生成下一轮 Feature 迭代任务。

{DIFFICULTY_RULES}
{BANNED_TASKS}

必须在现有产品和代码结构上增加真实的新能力，保留现有功能、接口与 Docker Compose 验收链路。直接生成困难或地狱任务，复杂度必须来自跨模块状态、性能、并发、异常恢复或兼容性等真实约束，不能靠堆字段或扩大文字。题面要明确新行为、边界条件与可执行验收，但给开发者保留实现取舍。taskType 必须为 feature。

原始任务：
{original_task}

当前获胜产物摘要：
{artifact_summary}

已有题目标题与摘要，必须避免雷同：
{existing or '无'}

只按 Schema 输出。"""


def gsb_prompt(task: str, a_evidence: str, b_evidence: str) -> str:
    return f"""比较同一道开发任务的 A、B 两份最终产物，给出 GSB 结论。只依据可见题面、代码提交、Docker 验收、真实操作录像和开发轨迹，不索取内部思维。

结论只能是 A better、Same 或 B better。理由用自然、口语化中文写成一个完整段落，同时评价 A 和 B；默认 180–500 个汉字，硬上限 600 个字符。不要使用反引号、Markdown 列表、JSON 文本、模型名称或空泛套话。Same 也必须具体说明两边相同的交付结果和各自问题。

任务：
{task}

A 证据：
{a_evidence}

B 证据：
{b_evidence}

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
