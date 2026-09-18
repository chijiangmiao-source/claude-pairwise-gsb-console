# Claude A/B GSB Console

这是与旧 Claude Eval Console 完全分离的新系统。每个 Pair 从同一个 `main` 提交创建大写 `A`、`B` 分支，用两个独立目录、容器、Terminal 和 Session 并行开发；完成后分别做 Docker Compose 清洁验收、实际难度复评、720p 真实操作录像和 GSB 评价。每个 Pair 占用两个终端，因此开发并发硬上限为 3 个 Pair。

完整文档：

- [需求说明](docs/REQUIREMENTS.md)
- [部署与迁移说明](docs/DEPLOYMENT.md)

模型职责固定如下：

- A/B 开发：Claude 容器，默认模型 `auto_model/urm`，镜像从旧系统部署配置读取。
- 找 Bug、根因分析与复现复核：Codex CLI，默认 `gpt-5.6-sol`，推理强度 `high`。
- 出题、难度、禁题与查重、基线检查、产物差异、GSB 和下一步判断：Codex CLI，默认 `gpt-5.6-sol`，推理强度 `medium`。
- GSB 公开理由复检：Codex CLI，默认 `gpt-6-astra`，推理强度 `high`；事实冲突会阻止提交，普通措辞建议保留给人工决定。
- GSB 只保存 A 评价和 B 评价两段，最终选择依据自然融入两段；页面、复检与 Excel 使用同一结构。
- 每个有效 Claude Session 只发送一次原题。A/B 容器都准备完成后先向 A 发送，默认 30 秒后再向 B 发送同一题面；两侧的告警和停止计时都从各自实际发送时刻开始。429、504、证书与连接错误等 API Error 会保留在原生轨迹中，但不会单独使会话作废，也不计入开发失败次数；系统继续等待同一 Session 后续形成正常最终回复。只有真实人工追加消息、容器或终端异常退出、无代码超时以及收尾失败等独立故障才触发全新 Session 重跑，不发送“继续”。轨迹复制或校验失败时保留旧容器。累计三次真实开发失败后退役当前 Pair，并自动换题启动新 Pair。
- Git、Docker、SQLite、轨迹导出和录像由系统代码执行，不把实际操作结果交给模型虚构。

页面中的“验收与录像”把 A/B Docker 验收和 720p 操作录像合并展示，可直接播放并核对录像提交与最终提交是否一致。通过技术检查的录像按用户授权默认审核通过，人工抽查发现问题后可重新录制。GSB 生成或应用复检建议后同样默认确认，仍可在“复核与人工确认”中检索、复检、编辑和重新确认。“导出轮次”支持资料筛选、提交前检查、批量复检、Excel 复核副本、隐藏/恢复，以及通过 Chrome 小助手向本期 SOLO-QA Pair-wise 表单提交、返修和同步质检状态；页头、勾选批量操作和每条已提交记录均提供状态同步入口，待返修记录会直接显示“提交返修”，也可先“只选待返修项”再批量提交。三个页面的筛选、页码和每页条数会保存在 URL 中。

“题目池”中的“一键自动运行完整流程”会持续维持 3 个活动 Pair。系统优先使用现有已通过准入的题目，自动创建仓库、准备并启动 A/B、执行 Docker 验收、根据真实改动与验证结果复评难度、依次录制两侧真实操作、生成并默认确认 GSB；任一 Pair 完成后会自动补位。0–1 和 Feature 的实际难度必须为困难或地狱，Bug 修复允许中等、困难或地狱；低于对应准入线的 Pair 会停止并释放名额。没有可用题目时才启动自动出题和准入复核。停止自动运行只停止后续推进和补位，不会强制中断已经启动的开发容器。

录像统一使用独立 Chrome 的网页视口录制，输出通用的 H.264 MP4，只保存 `1280×720` 页面内容，并持续显示页面内鼠标指针和点击圆点。点击“一键启动并录制”或“不合格并重新录制”后，系统会启动最终提交对应的 Docker Compose、识别浏览器入口、打开 Chrome 并自动录像；Swagger 文档页会隐藏自动生成的 `/openapi.json` 链接，再按“展开业务接口、Try it out、填写有效参数、Execute、展示成功响应”的真实流程操作。普通前端录像也必须检测到页面点击和同源成功接口请求。A/B 任一侧完成后立即核验首轮轨迹并对该提交做 Docker 清洁验收；结果按提交 SHA 复用。两侧当前提交都通过后，Codex 会依据题面、代码差异、开发轨迹和 Docker 结果分别判断 A/B 难度并给出整体实际难度；0–1/Feature 仍为困难或地狱、Bug 修复至少为中等，才允许录像和生成 GSB。缺少 Dockerfile、Compose 或启动失败时只返工失败 Arm，达到三次上限后整组失败并自动补位。没有完成真实功能流程的录像即使分辨率和时长正确也不会替换当前合格录像。历次录像保留在历史记录中，重录失败不会覆盖当前合格录像，重录成功会重新绑定提交证据并使旧 GSB 进入重新评价状态。

## 启动

首次部署运行安装脚本；它会先构建预装开发镜像，再执行依赖预检：

```bash
chmod +x scripts/*.sh
./scripts/install_launch_agent.sh
```

需要单独诊断环境时运行 `./scripts/preflight.sh`；需要前台启动时运行 `./scripts/start.sh`。

默认地址是 <http://127.0.0.1:8865>。首次使用先在“系统设置”确认 GitHub 账号、Git 提交邮箱、Codex CLI、Docker 镜像和浏览器录像状态。

安装脚本会创建并保留用户配置 `~/Library/Application Support/Claude A-B GSB Console/config.env`，且每次升级前自动备份 SQLite。其他电脑的从零安装、Docker 镜像迁移、Chrome 小助手、升级和回滚步骤见[部署与迁移说明](docs/DEPLOYMENT.md)。

## SOLO-QA 提交小助手

在 Chrome 的扩展管理页打开开发者模式，加载部署目录中的 `chrome-solo-qa-gsb-helper`，再保持已登录的 SOLO-QA 页面打开。部署目录为：

```text
~/Library/Application Support/Claude A-B GSB Console/app/chrome-solo-qa-gsb-helper
```

小助手每次提交都会动态读取 `/api/v1/gsb/form-schema`，按本期字段上传 A/B 两份 `.jsonl` 轨迹和两份录像，并提交题面、任务类型、困难度、运行环境、共同起点、两份产物快照、GSB 结论、两段合并后的 GSB 理由及有效性。平台新增无法映射的必填字段时会停止提交并显示字段名。提交前还会核验题面与两份轨迹逐字一致、SessionID 不同、Harness 版本相同、产物父提交为共同起点、文件摘要未变化及平台大小上限。

## 数据与隔离

- 新数据库：`~/Library/Application Support/Claude A-B GSB Console/.data/pairwise.db`
- 新项目：`~/Library/Application Support/Claude A-B GSB Console/projects`
- 旧数据库只读来源：`~/Library/Application Support/Claude Eval Console/.data/console.db`
- 默认端口：`8865`

启动时会幂等导入旧系统中的困难/地狱 0–1 与 Feature 候选；历史 Bug、简单和中等题不会导入。候选仍需 Codex 完成禁题、语义查重、难度和基线复核后才能创建 Pair。

## 验证

```bash
python3 -m unittest discover -s tests -v
npm test
python3 -m py_compile pairwise_console/*.py
./scripts/preflight.sh
```
