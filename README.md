# Claude A/B GSB Console

这是与旧 Claude Eval Console 完全分离的新系统。每个 Pair 从同一个 `main` 提交创建大写 `A`、`B` 分支，用两个独立目录、容器、Terminal 和 Session 并行开发；完成后分别做 Docker Compose 清洁验收、720p 真实操作录像和 GSB 人工确认。

模型职责固定如下：

- A/B 开发：Claude 容器，默认模型 `auto_model/urm`，镜像从旧系统部署配置读取。
- 找 Bug、根因分析与复现复核：Codex CLI，默认 `gpt-5.6-sol`，推理强度 `high`。
- 出题、难度、禁题与查重、基线检查、产物差异、GSB 和下一步判断：Codex CLI，默认 `gpt-5.6-sol`，推理强度 `medium`。
- Git、Docker、SQLite、轨迹导出和录像由系统代码执行，不把实际操作结果交给模型虚构。

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

