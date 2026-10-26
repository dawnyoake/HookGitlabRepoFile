# 部署目标：Windows PC + 公司内网 GitLab

这份目录可独立部署，不需要 LML checkout，不需要 Git 或 GitHub CLI，不需要 pip 第三方包。需要目标 PC 安装标准 CPython（建议 3.11 或更高，含 python.exe 和 pythonw.exe）。离线环境可由公司分发 Python 官方安装程序；交付包不包含 Python 运行时。

当前实现已加入 GitLab 适配器、Windows 文件锁、任务计划和新控制台窗口提示。验证环境是 macOS：32 项自动化测试通过，其中 GitLab 接口使用固定响应，Windows 分支使用 mock。尚未连接公司 GitLab，也没有在 Windows 实机验收，不能视为已完成生产部署。

## 哪些地方需要调整

| 项目 | 配置/位置 | 迁移时的动作 |
|---|---|---|
| Git 服务 | `provider` | 设置为 `gitlab`，选择现有 GitLabSource 适配器 |
| API 地址 | `api_base_url` | 填内网 GitLab 的 HTTPS API v4 根地址，含实际部署子路径 |
| 项目 | `repo` | 填 group/subgroup/project，或字符串形式的项目 ID |
| 分支/路径 | `branches`、`include_paths` | 改成公司项目的真实分支、文件和目录 |
| 认证 | `auth` | 指定环境变量名或 token 文件；不需要 gh；token 放在配置目录外也可以 |
| 企业 CA | `network.ca_file` | 有内部 CA 时填写 PEM 证书路径；系统默认证书信任仍保留 |
| 代理 | `network.proxy` | 空字符串表示直连；null 使用系统/环境代理；字符串填写代理 URL |
| Diff 文件上限 | `gitlab_diff_max_files` | 由管理员确认实例实际的 diff_max_files，开启路径过滤时必须填写 |
| 输出 | `notify` | terminal 弹出控制台；stdout 当前终端；file 只保存并标记提示完成；none 保存并保留待提示 |
| 调度 | `install` | Windows 注册当前用户的任务计划，替代 macOS LaunchAgent |
| 状态和文件 | `data_dir` | 为内网实例建立新目录，不拷贝个人 GitHub 的状态库和报告 |

只把 GitHub 域名换成 GitLab 域名是不够的。GitLab 的 project 编码、Issue IID、Notes、提交比较和分页差异都集中在 sources.py。正常迁移应只改配置；如果公司版本或定制 API 有差异，优先改 GitLabSource，避免改动 SQLite、去重、报告和路径规则。

## 在 Windows 安装

解压部署包到例如 `C:\Tools\LML-monitor`。以下命令在 PowerShell 运行。若 `py` 不可用，使用实际 Python 可执行文件的完整路径替代 `py -3`。

```powershell
Set-Location C:\Tools\LML-monitor
Copy-Item config.gitlab.windows.example.json config.json
New-Item -ItemType Directory -Force secrets | Out-Null
notepad config.json
notepad secrets\gitlab-token.txt
```

token 文件只放一行 GitLab token。账号或项目 access token 需要能够读取目标项目 Issue、Notes 和 Repository API 的权限，通常使用 read_api scope，最终以公司 GitLab 版本和管理员策略为准。secrets 目录应只允许运行该任务的用户和管理员访问；不要放到 Git 或共享目录，也不要把 token 发到聊天里。配置和命令行只保留凭据来源，不写 token 值。

配置里 token_env 优先于 token_file。`$env:LML_GITLAB_TOKEN` 只在当前 PowerShell 生效，任务计划不会继承这个临时 export；定时运行推荐使用 token_file，或由公司统一管理该用户的环境和凭据。相对的 token_file、ca_file、data_dir 都相对于 config.json 解析。

示例中的 `gitlab_diff_max_files: null` 是待填写项，不是默认上限。启用文件过滤时，未填该值会在需要查询提交文件时明确报错。请向管理员确认实际值，不要直接照抄其他实例的值。原因见 [GitLab Commits API](https://docs.gitlab.com/api/commits/#get-the-diff-of-a-commit)：达到服务端文件数量限制后，分页可能停止且不返回其余文件。

```powershell
py -3 -m unittest -v test_monitor test_migration
py -3 monitor.py --config config.json check --notify stdout
py -3 monitor.py --config config.json install
schtasks /Run /TN LML-monitor
schtasks /Query /TN LML-monitor /V /FO LIST
Get-Content .\data-gitlab\logs\monitor.log -Tail 30
```

首次 check 建立基线，不弹出历史更新。任务计划每 5 分钟启动 pythonw.exe，正常检查不弹黑框；有更新时单独启动 python.exe 的新控制台窗口，打印概览并等待回车。显示远端内容时只读 UTF-8 文件，不经 cmd.exe 拼接命令。安装位置变更或 interval_seconds 修改后，重新运行 install 更新任务；仓库路径规则等配置则下次检查自动读取。

任务使用交互式 `/IT`，当前用户登录时运行，适用于桌面提醒。注销后不提供后台桌面提醒，睡眠期间也不能及时检查；恢复后下次执行补查。如果以后要求无人登录也持续抓取，应改为独立后台任务 + 桌面提示进程，二者通过本地文件队列沟通，扩展入口是 runtime.py。

公司策略可能禁止普通用户注册任务，届时由 IT 使用同一脚本命令创建任务即可。不要改成 SYSTEM 来解决桌面弹窗：SYSTEM 任务不在用户桌面会话中显示窗口。创建任务不保存用户密码。任务名固定为 LML-monitor；当前设计是单项目、单用户部署，多项目可后续增加任务名配置。

卸载：

```powershell
py -3 monitor.py uninstall
```

仅删除任务，保留数据。手动只落盘且不弹窗：

```powershell
py -3 monitor.py --config config.json check --notify file
```

`file` 会把批次标记为处理完成；`none` 则保留待提示批次，后续切回 terminal 时会提示它们。

## 已留下的扩展边界

- `sources.py`：Transport 管 API 根地址、认证、证书、代理和分页；GitHubSource / GitLabSource 提供统一的 issues、comments、default_branch、head、compare、files 方法。新平台或定制接口在这里适配。
- `monitor.py`：平台无关的快照比较、路径匹配、去重、SQLite、Markdown/JSON 输出。check 支持注入 source，便于离线模拟公司接口。
- `runtime.py`：Windows/macos 文件锁、控制台启动和 Windows 任务计划。未来接企业通知渠道时，可替换 monitor.py 的 terminal_notify 调用点，复用已落盘批次和重试状态。
- `config*.json`：部署差异；不需要改源码中的个人目录或仓库名。
- `test_migration.py`：GitLab 标准响应、Windows 命令参数和迁移状态隔离的离线测试；可替换或增加公司实例脱敏后的样本。

数据库保存 provider + API 根地址 + 项目标识，避免不同实例同名项目混用进度。旧版本个人 GitHub 状态可自动补上来源标识；迁入 GitLab 必须使用新 data_dir。源码仓库不附带个人项目的真实写入测试脚本。例行测试使用模拟数据，不会对远端项目提交或创建 Issue。

## GitLab 适配范围和实机验收

支持 Issue 新增和最终状态变化、普通评论新增/编辑、分支提交及按文件过滤。过滤系统 Notes；不监控 MR、删除评论和中间每一次操作。为避免旧 Issue 的新评论漏查，每次列出所有可见 Issue（含关闭的），再分页读取其最近 Notes；项目 Issue 很多时，请调大检查间隔，后续可替换为 Webhook 接收器。

GitLab 正反向 compare 用于区分新增提交和历史改写。compare 超时则保留游标并报错。路径过滤读取每条 commit 的 diff；返回文件数达到已配置的实例上限时中止该轮检查，保留游标。JSON 保留监控使用的标准化记录，不承诺保留 GitLab 返回的每个原始字段。

目标 PC 上依次确认：读取项目成功、首次基线无历史弹窗、未匹配路径静默、匹配文件和子目录弹窗、重命名进出目录提醒、Issue/评论更新、重复检查不重复、关闭 PowerShell 后任务仍运行、断网恢复补查、中文路径与中文报告正常。

修改本 checkout 不会自动更新已有部署，也不会连接公司网络。迁移时使用新的配置和数据目录，并在目标 PC 验收。

接口参考：[GitLab Issues](https://docs.gitlab.com/api/issues/)、[Notes](https://docs.gitlab.com/api/notes/)、[Repositories compare](https://docs.gitlab.com/api/repositories/#compare-branches-tags-or-commits)、[Windows schtasks](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/schtasks-create)。
