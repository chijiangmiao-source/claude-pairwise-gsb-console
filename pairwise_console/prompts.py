import re
from typing import Any, Dict, List, Optional


BANNED_TASKS = """禁止生成或放行这些题型及换皮版本：经典小游戏与图形模拟；通用命令行、本地文件和桌面小工具；电商、订单、RBAC、库存、OA、CMS、挂号、CRM、聊天、拍卖、停车、工单、预约等通用业务 CRUD；报表、CSV 看板、记账、健身、菜谱、天气、番茄钟、习惯、播放器、旅行或观影记录等常见页面。"""

DIFFICULTY_RULES = """0–1、Feature 和 Bug 修复任务都只有困难或地狱可进入 A/B；简单或中等任务均拒绝。困难必须整合多个模块或系统约束，做关键设计取舍，并处理复杂状态、兼容性、权限、并发、性能或异常链路；地狱还要求架构级判断、多步复杂调试或大量边界场景。文件多、页面多、字段多、题面长不能单独证明难度。

必须按“满足题面所需的最小实现”预判，而不是按可能出现的最复杂实现判定。若现有架构已经提供核心状态机、事务、算法、恢复或兼容机制，任务只是增加字段、接口、条件分支、页面展示、迁移列、包装现有算法或补测试，即使跨多个文件也通常只是中等。困难任务只选择一个真实主难点，并让它贯穿三至四个实现模块；主难点可以是状态不变量、故障恢复、复杂跨层契约或有独立判据的领域算法。不要再叠加第二套无关的复杂机制来制造难度，开发者自行增加的复杂设计也不能计入。"""


ZERO_TO_ONE_PROMPT_MIN_CHARS = 300
ZERO_TO_ONE_PROMPT_MAX_CHARS = 600
FEATURE_PROMPT_MIN_CHARS = 300
FEATURE_PROMPT_MAX_CHARS = 480
TASK_PROMPT_MIN_SENTENCES = 4
TASK_PROMPT_MAX_SENTENCES = 6
TASK_PROMPT_MAX_SENTENCE_CHARS = 120
TASK_PROMPT_MAX_SEMICOLONS = 2
TASK_MIN_MODULES = 3
TASK_MAX_MODULES = 4
ZERO_TO_ONE_MAX_RUNTIME_COMPONENTS = 2
TASK_MAX_AUXILIARY_MECHANISMS = 2
TASK_MAX_OPERATIONS = 2
TASK_MAX_STATE_SETS = 1
TASK_MIN_ACCEPTANCE = 3
ZERO_TO_ONE_MAX_ACCEPTANCE = 6
FEATURE_MAX_ACCEPTANCE = 4
TASK_PROMPT_AI_STYLE_MARKERS = (
    "需求如下",
    "具体要求如下",
    "沿用既有不变量",
    "其余失败沿用错误信封",
    "不变量不变",
)


def _task_scope_list(candidate: Dict[str, Any], field: str) -> List[Any]:
    value = candidate.get(field)
    return value if isinstance(value, list) else []


def generated_task_prompt_issues(task_type: str, prompt: str,
                                 acceptance: Optional[List[Any]] = None,
                                 candidate: Optional[Dict[str, Any]] = None) -> List[str]:
    """Return deterministic scope and readability failures for generated tasks."""
    task_type = str(task_type or "zero_to_one")
    cleaned = re.sub(r"\s+", " ", str(prompt or "")).strip()
    issues: List[str] = []
    minimum, maximum = (
        (FEATURE_PROMPT_MIN_CHARS, FEATURE_PROMPT_MAX_CHARS)
        if task_type == "feature"
        else (ZERO_TO_ONE_PROMPT_MIN_CHARS, ZERO_TO_ONE_PROMPT_MAX_CHARS)
    )
    if not minimum <= len(cleaned) <= maximum:
        issues.append(f"题面应为 {minimum} 至 {maximum} 字，当前 {len(cleaned)} 字")
    if "\n" in str(prompt or "") or re.search(r"(?:^|\n)\s*(?:[-*•]|\d+[.、)])", str(prompt or "")):
        issues.append("题面应为自然连贯的一段话，不使用标题或列表")
    sentences = [
        part.strip(" ，,；;：:")
        for part in re.split(r"[。！？!?]+", cleaned)
        if part.strip(" ，,；;：:")
    ]
    if not TASK_PROMPT_MIN_SENTENCES <= len(sentences) <= TASK_PROMPT_MAX_SENTENCES:
        issues.append(
            f"题面应有 {TASK_PROMPT_MIN_SENTENCES} 至 {TASK_PROMPT_MAX_SENTENCES} 个完整句子，"
            f"当前 {len(sentences)} 句"
        )
    longest = max((len(sentence) for sentence in sentences), default=0)
    if longest > TASK_PROMPT_MAX_SENTENCE_CHARS:
        issues.append(f"题面单句最多 {TASK_PROMPT_MAX_SENTENCE_CHARS} 字，当前最长 {longest} 字")
    semicolons = cleaned.count("；") + cleaned.count(";")
    if semicolons > TASK_PROMPT_MAX_SEMICOLONS:
        issues.append(f"题面分号最多 {TASK_PROMPT_MAX_SEMICOLONS} 个，当前 {semicolons} 个")
    marker = next((item for item in TASK_PROMPT_AI_STYLE_MARKERS if item in cleaned), "")
    if marker:
        issues.append(f"题面包含模板化表达：{marker}")
    if task_type == "zero_to_one" and not re.search(
        r"(?:名为|名称为|叫作|叫做)?\s*`?verify`?[^。！？]{0,64}(?:服务|验收)",
        cleaned,
        re.IGNORECASE,
    ):
        issues.append("0–1 题面必须明确要求 Compose 中提供名为 verify 的可执行验收服务")
    if task_type == "zero_to_one" and candidate is not None and not (
        re.search(r"verify[^。！？]{0,40}(?:一次性|自行退出|自动退出|执行后退出|运行后退出)", cleaned, re.IGNORECASE)
        or re.search(r"(?:一次性|自行退出|自动退出|执行后退出|运行后退出)[^。！？]{0,40}verify", cleaned, re.IGNORECASE)
    ):
        issues.append("0–1 新题必须说明 verify 是执行后退出的一次性验收服务")
    if task_type == "zero_to_one" and candidate is not None:
        verify_sentences = [
            sentence for sentence in sentences
            if re.search(r"\bverify\b", sentence, re.IGNORECASE)
        ]
        verify_scope = " ".join(verify_sentences)
        if not (
            re.search(r"(?:代码|单元|集成|项目)?测试", verify_scope)
            and re.search(r"构建(?:检查|校验|验证)?", verify_scope)
            and re.search(r"(?:API|HTTP|接口)[^。！？]{0,24}冒烟", verify_scope, re.IGNORECASE)
        ):
            issues.append("0–1 新题的 verify 只要求代码测试、构建检查和 API/HTTP 冒烟")
        if re.search(r"(?:verify|验收服务)[^。！？]{0,80}(?:Playwright|浏览器|E2E|端到端)", cleaned, re.IGNORECASE):
            issues.append("0–1 新题不得要求 verify 安装 Playwright 或运行浏览器验收")
        if re.search(
            r"(?:安装|引入|依赖|使用|运行)[^。！？]{0,32}Playwright|Playwright[^。！？]{0,32}(?:安装|引入|依赖|使用|运行)",
            cleaned,
            re.IGNORECASE,
        ):
            issues.append("0–1 新题不得要求项目仓库安装或运行 Playwright")

    scenarios = acceptance if isinstance(acceptance, list) else []
    acceptance_max = FEATURE_MAX_ACCEPTANCE if task_type == "feature" else ZERO_TO_ONE_MAX_ACCEPTANCE
    if scenarios and not TASK_MIN_ACCEPTANCE <= len(scenarios) <= acceptance_max:
        issues.append(f"验收场景应为 {TASK_MIN_ACCEPTANCE} 至 {acceptance_max} 个")

    if candidate is None:
        return issues
    if not str(candidate.get("engineeringCore") or "").strip():
        issues.append("内部范围缺少唯一工程核心")
    if not str(candidate.get("mainUserFlow") or "").strip():
        issues.append("内部范围缺少唯一用户主流程")
    modules = _task_scope_list(candidate, "implementationModules")
    if not TASK_MIN_MODULES <= len(modules) <= TASK_MAX_MODULES:
        issues.append(f"实现模块应为 {TASK_MIN_MODULES} 至 {TASK_MAX_MODULES} 个")
    runtime = _task_scope_list(candidate, "runtimeComponents")
    if task_type == "feature" and runtime:
        issues.append("Feature 不得新增独立运行组件")
    if task_type != "feature" and not 1 <= len(runtime) <= ZERO_TO_ONE_MAX_RUNTIME_COMPONENTS:
        issues.append(f"0–1 应有一至 {ZERO_TO_ONE_MAX_RUNTIME_COMPONENTS} 个应用运行组件")
    if len(_task_scope_list(candidate, "auxiliaryMechanisms")) > TASK_MAX_AUXILIARY_MECHANISMS:
        issues.append(f"辅助机制最多 {TASK_MAX_AUXILIARY_MECHANISMS} 项")
    if len(_task_scope_list(candidate, "newOperations")) > TASK_MAX_OPERATIONS:
        issues.append(f"新增接口或用户操作最多 {TASK_MAX_OPERATIONS} 个")
    if len(_task_scope_list(candidate, "newStateSets")) > TASK_MAX_STATE_SETS:
        issues.append(f"新增状态集合最多 {TASK_MAX_STATE_SETS} 组")
    return issues


def actual_difficulty_review_prompt(task: str, original_difficulty: str,
                                    a_evidence: str, b_evidence: str,
                                    task_type: str = "zero_to_one") -> str:
    threshold = "无论是 0–1、Feature 还是 Bug 修复，只有整体实际难度仍为困难或地狱才允许进入录像和 GSB，简单或中等必须停止该 Pair 并换题。"
    return f"""A/B 两侧已经完成开发并通过 Docker 验收。请根据实际交付重新评估任务难度，而不是照抄出题时的难度。

固定标准：
简单：任务意图明确，主要沿已有模式机械修改或局部实现；即使改多个文件，只要模式高度一致、核心判断很少，也算简单。
中等：需要理解局部到跨模块的调用或数据流，做一些实现决策并处理若干边界条件，但不需要重新设计架构。
困难：需要整合多个模块或系统约束，做关键设计取舍，处理复杂状态、兼容性、权限、并发、性能或异常链路。
地狱：决策复杂度极高，需求开放或约束隐蔽，需要极深项目理解、架构级判断、多步复杂调试或大量边界场景，即使当前先进模型也很难成功交付。

分别判断 A 和 B 的实际必要工作，再给出任务的整体实际难度。整体难度应反映有效交付中完成需求所必需的最低复杂度；某一侧自行过度设计、环境安装、步骤多、文件多、题面长或日志长，不能抬高难度。真实业务状态、异常链路、并发/一致性、兼容迁移、性能约束和跨模块设计可以作为证据。若 A/B 采用不同复杂度的实现，要说明差异，但不能因为写法不同就把同一任务虚高。

{threshold}

题面：
{task}

出题难度：{original_difficulty}

A 的真实开发、代码与验收证据：
{a_evidence}

B 的真实开发、代码与验收证据：
{b_evidence}

只按 Schema 返回。reason 用中文说明关键复杂度与判定依据；evidence 只列真实可核对的文件、函数、命令、测试场景或结果。"""


def task_validation_prompt(task_text: str, known_titles: str,
                           baseline_evidence: str = "",
                           recent_rejections: str = "") -> str:
    return f"""你负责新系统的题目准入复核。根据给定题目判断是否可以进入 A/B 开发。

{DIFFICULTY_RULES}
{BANNED_TASKS}

还要检查：必须能用 Docker Compose 清洁启动；应包含可操作的功能验收；不能只是改名换皮；与已有题目不能在核心功能、交互、数据模型、验收或技术实现上高度重复。新生成的 0–1 题保留 Dockerfile、Docker Compose、健康检查、可配置宿主机端口和一次性 verify 服务，verify 执行后以退出码报告结果，范围只包括代码测试、构建检查和 API/HTTP 冒烟，不要求项目仓库安装 Playwright 或运行浏览器验收。去重按业务目标、核心机制和验收链路判断，不能只看标题或字面是否完全一致；相同机制换行业名、换角色名或改写措辞仍算重复。

先做一次最小实现预演：列出题面不可省略的状态变化、数据边界和验收，再对照基线已有能力，判断最短正确实现的真实难度。Feature 或 Bug 必须检查准确基线中的现有模块；如果主要工作可以复用现有机制并通过直接扩字段、加路由、循环筛选、区间切分、调用既有算法或增加前端状态完成，应判为中等并拒绝。困难候选应只有一个贯穿三至四个模块的真实主难点，验收必须能证明它确实被实现；不要用多套无关机制、字段数量或测试数量抬高难度。difficultyEvidence 必须说明判定依据，不能只复述题面。

新生成的 0–1 和 Feature 还要按范围预算复核：一个工程核心、一条用户主流程、三至四个实现模块、最多两项辅助机制、最多两个新增接口或用户操作、最多一组新增状态。0–1 最多两个应用运行组件，验收为三至六个场景；Feature 不增加独立运行组件，验收为三至四个场景。题面应为四至六个完整句子，单句不超过 120 字，分号不超过两个，并按业务因果自然组织；不得机械拼接数据库、接口、页面、测试和交付清单。

判定应依据题面真实内容，不能用内部字段是否单独填写代替事实判断。zero_to_one 本来就是从空仓库开始，baseline_path 为空属于正确基线，baselineReady 应判为 true。历史任务的 acceptance 数组可能为空；只要原始 prompt 已经写明 Docker Compose、验证服务、接口结果或可执行验收场景，就视为具备验收条件，不能仅因 acceptance 字段为空拒绝。只有发现明确的难度不足、禁出题、实质重复、Feature/Bug 缺少准确任务前代码，或题面确实无法验收时才拒绝；不要把可由系统直接整理的元数据缺项当成阻塞。

从本系统完整题库和历史提交题库召回的相近题目摘要；similarityHint 只用于帮助定位，最终仍按语义判断。source 为 legacy 的任务是调用方明确允许从上一期复用的旧题，不因“历史提交题库”中存在同一题面而拒绝；仍要阻止它与本期系统题库内的题目重复。新生成任务没有这项例外，应避免继续生成与历史题库同质的新题：
{known_titles or '无'}

准确基线摘要；Feature/Bug 可在当前工作目录继续查看代码，并以 baseline_sha 对应提交为准：
{baseline_evidence or '0–1 从空仓库开始，无既有实现可复用'}

近期开发完成后的真实难度案例。0–1、Feature 和 Bug 修复若采用同类的简单或中等最小实现都应拒绝：
{recent_rejections or '无'}

待复核题目：
{task_text}

只按 Schema 返回事实结论。"""


def task_generation_prompt(existing: str, task_type: str = "zero_to_one",
                           desired_project_category: str = "") -> str:
    category_rule = (
        f"本次项目形态固定为{desired_project_category}，projectCategory 必须填写"
        f"{desired_project_category}，题面、stack 和运行组件都要与该形态一致。"
        if desired_project_category else
        "projectCategory 必须明确选择纯后端、纯前端或全栈。"
    )
    return f"""生成一个可真实开发并验收的 {task_type} 编程任务。

{DIFFICULTY_RULES}
{BANNED_TASKS}

直接生成困难或地狱任务，不先生成低难度再升级。沿用老系统的范围预算：题面有且只有一个可独立验收的工程核心和一条主要纵向链路，实际涉及三至四个实现模块；0–1 最多两个应用运行组件、两项辅助机制、两个接口或用户操作和一组状态。困难度只选择一个真实主轴，可以是状态不变量、故障恢复、复杂跨层契约或有独立判据的领域算法，并让它贯穿主流程；不要再叠加第二套无关算法、恢复链路、调度器或工作台。普通 CRUD、字段贯通、直接循环、既有算法包装和常规页面状态不能单独成为主难点。

prompt 目标约 450 字，生成时控制在 300 至 520 字，写成四至六个完整中文句子，单句不超过 120 字，分号不超过两个；本地只为轻微偏差保留 300 至 600 字的硬边界。按业务背景、用户操作、关键约束、失败反馈和可观察验收的因果顺序自然展开，不加标题、列表或“需求如下”，不把数据库、接口、页面、测试数量和交付要求机械拼成一串。acceptance 只列三至六个可独立操作并观察结果的场景，同一次操作产生同一结果的校验要合并。{category_rule}纯后端不得创建前端；纯前端必须交付可真实操作的浏览器页面且不得创建业务后端；全栈必须同时交付浏览器页面和业务后端，并通过真实 API 联调。stack 只写主要编程语言和主要应用框架，用英文逗号加空格分隔，例如 Python 3.13, FastAPI 或 TypeScript, React。

题面从空仓库起步，并在正文中用一句话自然交代 Dockerfile、Docker Compose、健康检查、可配置宿主机端口，以及 Compose 中名为 verify、执行验收后自行退出并以退出码报告结果的一次性服务。verify 的范围只包括代码测试、构建检查和 API/HTTP 冒烟，不要求项目仓库安装 Playwright 或运行浏览器验收；页面真实操作与录像由交付流程另行完成，不写成项目内 verify 的义务。不要在结尾堆 README、测试、.gitignore 等通用清单。只固定会改变核心验收结果的业务规则，字段命名、页面布局和内部实现留给开发者。内部范围字段只供系统校验，必须如实填写，不能写进 prompt。

已有题目标题与摘要，必须避免核心问题、机制和验收链路雷同；换行业背景或改写措辞不能算新题：
{existing or '无'}

只按 Schema 输出。"""


def feature_generation_prompt(original_task: str, artifact_summary: str, existing: str, project_category: str = "") -> str:
    return f"""为一个已经完成并通过 Docker 验收的项目生成下一轮 Feature 迭代任务。

{DIFFICULTY_RULES}
{BANNED_TASKS}

必须在现有产品和代码结构上增加真实的新能力，保留现有功能、接口与 Docker Compose 验收链路。先检查摘要和当前基线已经具备的状态机、事务、算法、恢复和兼容能力；这些既有能力不能再次当作新题难度。最短正确实现若只是扩字段、加接口、调用既有算法、增加条件分支或页面状态，必须放弃该候选并重新生成。

沿用老系统的迭代范围预算：只增加一个工程核心，围绕一条用户主流程改动三至四个现有模块；最多两个新增接口或用户操作、一组新增状态、两项辅助机制和一个复杂主轴，不增加独立运行组件。困难主轴可以是状态不变量、故障恢复、跨层一致性或有独立判据的领域算法，但不能同时堆多套机制。acceptance 只列三至四个可独立操作并观察结果的场景，覆盖主流程、直接相关的失败边界和兼容回归。

prompt 控制在 300 至 480 字，写成四至六个完整中文句子，单句不超过 120 字，分号不超过两个。按新行为如何进入现有流程的业务因果自然展开，不加标题、列表、生成说明或结尾清单，不机械拼接数据库、接口、页面和测试字段。projectCategory 必须保持为 {project_category or '原项目类别'}，不得改变项目形态；stack 只写主要编程语言和主要应用框架。题面要给开发者保留设计取舍，内部范围字段只供系统校验，不能写进 prompt。taskType 必须为 feature。

原始任务：
{original_task}

当前获胜产物摘要：
{artifact_summary}

已有题目标题与摘要，必须避免核心问题、机制和验收链路雷同；换业务名、页面名或接口名不能算新迭代：
{existing or '无'}

只按 Schema 输出。"""


def gsb_prompt(task: str, a_evidence: str, b_evidence: str,
               process_evidence: str = "[]") -> str:
    return f"""比较同一道开发任务的 A、B 两份最终产物，给出 GSB 结论。只依据可见题面、代码提交、Docker 验收、真实操作录像和开发轨迹，不索取内部思维。

结论只能是 A better、Same 或 B better。只填写 aReason 和 bReason 两段，不生成单独的偏好依据。aReason 说明 A 的可见操作、产物、验收结果和具体问题，并自然交代这些事实如何支持最终结论；bReason 对 B 做同样说明。两段合起来必须能直接看出为什么选择 A better、Same 或 B better。

写得像同事看完轨迹后在说明判断，重点是口语化、好理解，不要为了显得简短而删掉能支撑结论的证据。每段先概括这一侧做成了什么，再结合真实操作、测试、报错、Bug 或未验证项说明结果，最后自然交代为什么更好、稍弱或与另一侧接近。篇幅由证据决定，可以保留多个有因果关系的开发与验证事实，也可以写必要的测试数字和代码位置；不要机械罗列与结论无关的数字、文件或实现细节。

公开理由不得出现“第175步”“第207、212步”这类轨迹行号，也不要写“某测试文件第 15、16、21、22 项通过”。应直接说做了什么和结果如何，例如“实际跑过临界值两侧、写回和输入变化后的失效处理”。“83 个单测、23 个端到端测试、38 秒录像均通过”这类数字堆叠也不够口语化；录像时长只能说明录像文件合规，不能用来证明功能没有缺陷。需要保留的具体证据仍要保留，不能只剩“整体很好”“未见问题”这类空话。

具体位置必须来自 traceEvidence，例如文件、函数、关键命令、接口状态或报错原文。traceEvidence 的 step 只供内部找到证据，不写进 aReason 或 bReason。某次测试先失败、后来修好时，要把失败原因和最终结果连起来说清楚，不能把已经修好的问题写成最终缺陷。discoveredBugs 中已复现并影响比较的 Bug 要写清触发场景和客观结果；静态猜测或未复现候选不能当成事实。某一侧没有跑业务测试，可以直接说“没有实际跑接口流程”，不能把编译通过说成业务已验证。

如果 Docker 状态是 observed_failed，表示 Claude 已经结束开发，但原始交付在清洁环境中无法构建、启动或通过测试。对应短录像只是在终端样式页面中保留真实失败命令和输出，并不表示功能通过。必须把检查项和报错当作最终缺陷如实写入该侧评价，不能暗示系统后来修好了它。

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

只生成实际修复难度达到困难或地狱的候选。熟悉项目的开发者应预计需要约 45 至 90 分钟，合理修复至少涉及 20 行有效生产代码；改常量、恢复一行条件、补一个显而易见的校验或照现有测试抄答案的缺陷不合格。API 限流、证书、网络临时中断和机器资源不足不是产品 Bug。找不到可信候选时返回空 candidates，禁止编造。

原任务：
{task}

待检查产物：Arm {arm}，提交 {commit_sha}

现有 Docker 验收证据：
{docker_evidence}

只按 Schema输出。"""


def bugfix_task_prompt(candidate_evidence: str, existing_tasks: str,
                       previous_prompt: str = "", issues: str = "") -> str:
    correction = ""
    if previous_prompt:
        correction = f"""

上一次草稿：
{previous_prompt}

上一次草稿存在的问题：
{issues or '没有按真实证据自然组织题面'}

不要修补原句式，请根据原始证据重新组织一份全新的题面。
"""
    return f"""把已经在两次清洁 Docker 环境中真实复现的 Bug 整理成一份交给开发者的修复题面。

题面必须忠实使用候选中的前置条件、操作、真实结果、正确行为和复现结果。不得把预计结果改写成已发生事实，也不得补造接口、文件、数字、原因或测试结论。需要把触发条件、关键操作、用户可见后果和修复后的可执行验收讲清楚，并保留现有 Docker Compose 启动与验收链路。

开发者只能看到业务现象和可复现证据。题面不得透露根因，不得写内部文件名、函数名、代码行号、修改位置、补丁策略、建议算法或实现答案；候选中的 sourcePaths 和内部分析只供你核对，不能出现在 prompt。公开接口、页面控件、输入数据、状态码和通过真实业务入口执行的命令可以保留。若现有复现命令直接导入内部函数或读取源码，应把它改写成业务场景说明，并要求修复后通过真实页面、HTTP API 或仓库已有公开验收入口验证，不能把内部调用链发给开发者。

根据这个 Bug 自身的因果关系自然组织文字。不要套用固定开头、固定段落顺序或固定结尾；不要使用“前置条件：”“复现步骤：”“实际结果：”“预期结果：”四段结构，也不要写“请修复该问题，保留现有 Docker Compose 启动与验收链路，并补充覆盖复现路径的自动化验收”。可以使用自然段；只有确实有助于执行时才使用短列表。回归验收要说明真实需要执行的场景和结果，不能只写“补充测试”。

避免与历史题目重复核心问题、组织骨架和验收表达。不要通过替换业务名、接口名或同义词来改写历史题。

Bug 候选与两次真实复现证据：
{candidate_evidence}

相近的已有题目：
{existing_tasks or '无'}
{correction}

只按 Schema 返回。prompt 是最终完整题面；evidenceUsed 简要列出题面实际采用的候选字段、命令输出或源码位置，供系统内部核对，不把这份列表追加进题面。"""


def discovered_bug_review_prompt(title: str, task_prompt: str,
                                 candidate_evidence: str,
                                 project_summary: str) -> str:
    return f"""独立复核一份准备进入 A/B 开发的真实 Bug 修复题。你可以只读检查当前仓库；候选证据和源码信息仅供内部核对，不能要求把它们补进公开题面。

只在以下条件全部满足时 accepted=true：候选已经由两次清洁 Docker 环境稳定复现，题面中的事实与复现证据一致；实际修复难度至少为困难；熟悉项目的开发者预计需要 45 至 90 分钟；合理修复预计至少涉及 20 行有效生产代码；题面能通过真实业务入口复现和验收，同时没有透露根因、内部文件名、函数名、代码行号、修改位置、补丁策略、建议算法或实现答案。

公开 API 路径、页面控件、输入数据、状态码和业务错误可以作为复现锚点，不算答案泄露。直接告诉开发者哪个内部判断错了、应改哪个函数、采用线程池、缓存或动态规划等具体修法，或把只读源码和直接导入内部求解器当成主要验收，均视为 answerLeak=true。不要因为题面长、数字多或业务名复杂就判为困难。

estimatedChangedLines 和 estimatedChangedFiles 评估正确修复的合理生产代码改动量。若证据不足以支持困难级别、45 至 90 分钟或至少 20 行修复，必须拒绝，不能按题面自报难度通过。

题目标题：{title}

公开题面：
{task_prompt}

内部候选与复现证据：
{candidate_evidence}

内部项目摘要：
{project_summary}

只按 Schema 返回。"""


def seeded_bug_prompt(original_task: str, project_summary: str,
                      existing_tasks: str) -> str:
    return f"""你要为一个隔离的 A/B 修复基准制作带缺陷的初始仓库。直接检查并修改当前目录里的生产代码，只引入一个真实、稳定、可由现有业务入口触发的 Bug；预期修复工作量为 45 至 90 分钟，难度必须达到困难或地狱，合理修复至少需要 20 行有效生产代码，并且需要理解跨模块状态、持久化、并发、异常恢复、精确算法或前后端契约中的至少一项关键关系。

只改实现该缺陷所必需的生产代码。注入变更本身必须包含至少 12 行有意义的生产逻辑改动，并形成需要理解状态、算法、并发、持久化、异常恢复或跨层契约才能修复的缺陷；不得通过注释、格式化、重复代码或无关重写凑行数。优先选择两个以上状态或模块共同作用才会出现的错误，正确修复应需要恢复完整契约或不变量。删除一个特例分支、补全一个缓存键、恢复一条条件、替换一个公式或常量、修正单个坐标映射等简单根因，即使注入代码很多也不合格。不要新增或修改测试、README、注释、Dockerfile、Compose、verify 脚本、锁文件和依赖清单，也不要留下 TODO、故障开关、补丁说明、答案提示或故意报错文本。不得硬编码只针对一个示例的分支。修改后原有启动、健康检查和 verify 仍应通过，缺陷只在题面给出的特定业务场景中显现。

同时生成自然中文修复题面。题面说明真实触发条件、操作、实际现象、正确行为和回归验收，但不能透露根因、修改位置、实现方案或你改过哪些文件；不要套用“前置条件/复现步骤/实际结果/预期结果”四段模板。题面须保留现有 Docker Compose 启动方式，并自然写明用真实页面、HTTP API 或现有公开入口执行“自动化验收”的具体场景和应核对结果；最终题面必须同时出现“自动化”和“验收”或“测试”。不要加入仓库内已有测试可以直接抄出的答案。

原项目要求：
{original_task}

当前项目摘要：
{project_summary}

已有 Bug 题摘要，新的核心缺陷和题面组织都不能重复：
{existing_tasks or '无'}

完成代码修改后再按 Schema 返回。changedPaths 只列实际改动的生产代码路径；difficultyEvidence 说明为什么修复需要困难级别的工程判断；stack 只写主要语言与应用框架。"""


def seeded_bug_review_prompt(title: str, task_prompt: str, diff_text: str,
                             project_summary: str) -> str:
    return f"""独立复核一个准备进入 A/B 的隐藏 Bug 基准。你可以读取当前仓库，下面还提供了注入变更的内部 diff；这些内部信息不会发给开发者。

只在同时满足以下条件时 accepted=true：Bug 可由题面描述的真实业务入口稳定触发；修复需要理解关键状态、算法、并发、持久化、异常恢复或跨层契约，实际难度至少为困难；熟悉项目的开发者预计需要约 45 至 90 分钟；合理修复预计至少涉及 20 行有效生产代码，可以只改一个核心文件，但不能只是改常量、恢复一行条件或照题面抄答案；注入 diff 至少要有 12 行有意义的生产逻辑变更，不能是单行公式、坐标映射、常量或条件替换，也不能靠无关重写凑行数；题面没有出现内部文件名、函数名、修改位置、根因、补丁策略或其他解法提示；注入本身不是显眼的故障开关、特例硬编码、故意抛错或破坏启动。

estimatedChangedLines 和 estimatedChangedFiles 评估的是正确修复的合理改动量，不是下面注入 diff 的行数。answerLeak 只要题面足以直接定位实现位置或基本给出修法就为 true。不要因为题面文字长或涉及多个名词就判困难。

题目标题：{title}

发给开发者的题面：
{task_prompt}

项目摘要：
{project_summary}

内部注入 diff：
{diff_text[:20000]}

只按 Schema 返回。"""
