# Claude A/B GSB Console

这是与旧 Claude Eval Console 完全分离的新系统。每个 Pair 从同一个 `main` 提交创建大写 `A`、`B` 分支，用两个独立目录、容器、Terminal 和 Session 并行开发；完成后分别做 Docker Compose 清洁验收、720p 真实操作录像和 GSB 人工确认。

模型职责固定如下：

- A/B 开发：Claude 容器，默认模型 `auto_model/urm`，镜像从旧系统部署配置读取。
- 找 Bug、根因分析与复现复核：Codex CLI，默认 `gpt-5.6-sol`，推理强度 `high`。
- 出题、难度、禁题与查重、基线检查、产物差异、GSB 和下一步判断：Codex CLI，默认 `gpt-5.6-sol`，推理强度 `medium`。
- GSB 公开理由复检：Codex CLI，默认 `gpt-6-astra`，推理强度 `high`；事实冲突会阻止提交，普通措辞建议保留给人工决定。
- Git、Docker、SQLite、轨迹导出和录像由系统代码执行，不把实际操作结果交给模型虚构。

页面中的“验收与录像”把 A/B Docker 验收和 720p 操作录像合并展示，可直接播放并核对录像提交与最终提交是否一致。“复核与人工确认”支持检索、分页、模型复检、应用建议及人工确认。“导出轮次”支持资料筛选、提交前检查、批量复检、Excel 复核副本、隐藏/恢复和提交状态登记。三个页面的筛选、页码和每页条数会保存在 URL 中。

录像统一使用独立 Chrome 的网页视口录制，只保存 `1280×720` 页面内容，并为真实点击显示短暂圆点。点击“一键启动并录制”或“不合格并重新录制”后，系统会启动最终提交对应的 Docker Compose、识别浏览器入口、打开 Chrome 并自动录像；技术检查通过后默认采用。历次录像保留在历史记录中，重录失败不会覆盖当前合格录像，重录成功会重新绑定提交证据并使旧 GSB 进入重新评价状态。

## 启动

```bash
./scripts/start.sh
```

默认地址是 <http://127.0.0.1:8865>。首次使用先在“系统设置”确认 GitHub 账号、Git 提交邮箱、Codex CLI、Docker 镜像和 Screen 状态。

安装为 macOS 登录后自动运行的独立服务：

```bash
./scripts/install_launch_agent.sh
```

## 数据与隔离

- 新数据库：`~/Library/Application Support/Claude A-B GSB Console/.data/pairwise.db`
- 新项目：`~/Library/Application Support/Claude A-B GSB Console/projects`
- 旧数据库只读来源：`~/Library/Application Support/Claude Eval Console/.data/console.db`
- 默认端口：`8865`

启动时会幂等导入旧系统中的困难/地狱 0–1 与 Feature 候选；历史 Bug、简单和中等题不会导入。候选仍需 Codex 完成禁题、语义查重、难度和基线复核后才能创建 Pair。

## 验证

```bash
python3 -m unittest discover -s tests -v
node --check web/app.js
python3 -m py_compile pairwise_console/*.py
```
