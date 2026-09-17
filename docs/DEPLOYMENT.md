# 部署与迁移说明

## 1. 适用环境

当前版本面向 macOS，使用 Terminal、GNU Screen、Docker Desktop 和 Google Chrome。建议使用与 Claude Docker 镜像相同架构的电脑；当前默认镜像 `claude-eval-runtime:claude-2.1.269` 为 Apple Silicon `arm64` 镜像。

需要提前安装并登录：

- Python 3.9 或更高版本；
- Node.js 20 或更高版本及 npm；
- Docker Desktop，包含 Docker Compose；
- Google Chrome，安装在 `/Applications/Google Chrome.app`；
- Git 和 GitHub CLI；
- Codex CLI；
- macOS 自带的 `screen` 与 `osascript`。

认证检查：

```bash
gh auth login
gh auth status
codex login status
```

Claude 容器从 `~/.claude/settings.json` 的 `env.ANTHROPIC_AUTH_TOKEN` 或 `env.ANTHROPIC_API_KEY` 读取凭据。该文件只保存在本机，不要提交到 Git。

## 2. 准备 Claude Docker 镜像

目标电脑必须存在配置指定的 Claude 镜像：

```bash
docker image inspect claude-eval-runtime:claude-2.1.269
```

如果镜像只在原电脑上，可离线迁移：

```bash
# 原电脑
docker save claude-eval-runtime:claude-2.1.269 | gzip > claude-eval-runtime-2.1.269.tar.gz

# 把 tar.gz 复制到目标电脑后
gunzip -c claude-eval-runtime-2.1.269.tar.gz | docker load
docker image inspect claude-eval-runtime:claude-2.1.269
```

目标电脑架构不同时应重新构建对应架构镜像，并在 `config.env` 中修改 `PAIRWISE_CLAUDE_IMAGE`。

## 3. 全新安装

```bash
git clone https://github.com/chijiangmiao-source/claude-pairwise-gsb-console.git
cd claude-pairwise-gsb-console
chmod +x scripts/*.sh
./scripts/preflight.sh
./scripts/install_launch_agent.sh
```

首次执行安装脚本会创建：

```text
~/Library/Application Support/Claude A-B GSB Console/config.env
```

脚本会根据当前 `gh` 登录账号填写 GitHub Owner 和 noreply 邮箱，并把提交人设为刘昱。检查该文件；如需修改，保存后重新运行安装脚本。升级时脚本不会覆盖它。

主要配置示例见仓库根目录的 `config.example.env`。Pair 并发可设为 1–3，服务端始终拒绝大于 3 的值。

安装完成后验证：

```bash
curl -fsS http://127.0.0.1:8865/api/health
launchctl print "gui/$(id -u)/com.local.claude-pairwise-gsb-console" | grep 'state ='
open http://127.0.0.1:8865
```

进入“系统设置”，确认 Git/GitHub、Codex CLI、Claude Docker 和浏览器录像四项均显示可用。

## 4. 安装 SOLO-QA 提交小助手

1. 在 Chrome 打开 `chrome://extensions/`。
2. 打开右上角“开发者模式”。
3. 点击“加载未打包的扩展程序”。
4. 选择：

```text
~/Library/Application Support/Claude A-B GSB Console/app/chrome-solo-qa-gsb-helper
```

5. 打开并登录 `https://solo2.jzxhnh.com`。
6. 刷新 `http://127.0.0.1:8865/#exports`，页面应显示“提交助手已连接”。

小助手固定连接本机 `8865` 端口。修改端口会使扩展无法连接，除非同步修改扩展清单和后台脚本后重新加载。

## 5. 旧系统题目导入

默认读取当前用户目录下：

```text
~/Library/Application Support/Claude Eval Console/.data/console.db
```

没有旧系统时会直接跳过，不影响新系统启动。需要指定其他位置时，在 `config.env` 增加：

```bash
PAIRWISE_OLD_DB=/absolute/path/to/console.db
```

系统只导入旧库中的困难/地狱 0–1 和 Feature 候选，仍需重新完成准入检查。历史 Bug、简单和中等题不会导入。

## 6. 数据迁移

只部署程序时建议在目标电脑使用新数据库，不复制原电脑的数据。

如确实要整体迁移，先在原电脑停止服务并复制整个目录：

```bash
launchctl bootout "gui/$(id -u)/com.local.claude-pairwise-gsb-console"
ditto "$HOME/Library/Application Support/Claude A-B GSB Console" /path/to/backup
```

数据库保存了项目、录像和轨迹的绝对路径。目标电脑的用户名或目录不同，直接复制后这些历史路径不会自动生效；应保持相同目录，或在迁移前单独制定路径重写与文件校验方案。不能只复制 `pairwise.db` 而遗漏 `.data/recordings`、`.data/claude-runs` 和 `projects`。

## 7. 升级

```bash
cd claude-pairwise-gsb-console
git pull --ff-only
python3 -m unittest discover -s tests -v
npm ci
npm test
./scripts/install_launch_agent.sh
```

安装脚本在每次升级前使用 SQLite Backup API 创建一致性备份：

```text
~/Library/Application Support/Claude A-B GSB Console/.data/backups/
```

`config.env`、数据库、项目、录像和轨迹位于运行副本之外，不会被 `rsync --delete` 删除。

升级完成后打开“题目池”，点击“一键自动运行完整流程”即可持续维持 3 个活动 Pair。该开关保存在数据库中，服务重启后会继续生效；需要暂停自动补位时点击“停止自动运行”，已启动的项目不会被强制中断。

## 8. 日志、重启与卸载

日志：

```text
~/Library/Logs/Claude A-B GSB Console/server.log
~/Library/Logs/Claude A-B GSB Console/server-error.log
```

重启：

```bash
launchctl kickstart -k "gui/$(id -u)/com.local.claude-pairwise-gsb-console"
```

停止自动启动：

```bash
launchctl bootout "gui/$(id -u)/com.local.claude-pairwise-gsb-console"
rm "$HOME/Library/LaunchAgents/com.local.claude-pairwise-gsb-console.plist"
```

上述命令不会删除数据库和项目。确认不再需要数据后，再手动删除 `~/Library/Application Support/Claude A-B GSB Console`。

## 9. 常见问题

- 页面打不开：运行 `curl http://127.0.0.1:8865/api/health`，再查看 `server-error.log`。
- Docker 不可用：先启动 Docker Desktop，然后运行 `scripts/preflight.sh`。
- Claude 容器起不来：检查镜像架构、镜像标签和 `~/.claude/settings.json` 的凭据。
- GitHub 建仓失败：确认 `gh auth status`、仓库权限和 `config.env` 中的 Owner、邮箱。
- Codex 作业失败：确认 `codex login status`，并从“Codex 作业”查看错误。
- 录像失败：确认 Chrome 位于 `/Applications`，重新运行安装脚本安装 Playwright FFmpeg，再检查项目的 Compose 端口配置。
- 小助手未连接：确认扩展已启用、加载的是运行副本目录，并刷新本地导出页。
