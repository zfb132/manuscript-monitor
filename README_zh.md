# 投稿状态检查

[English documentation](README.md)

## 用途

Check Submission Status 会登录一个或多个 ScholarOne 作者控制台，读取当前稿件，将历史记录持久化到 SQLite，并通过 [Apprise](https://appriseit.com/getting-started/universal-syntax/) 发送变更通知。每条有效的控制台记录都包含规范化后的短稿件 ID、完整标题、页面显示的投稿日期及解析后的日期，以及当前状态。

`main.py` 每次只执行一次检查，然后退出。它不是守护进程，也不包含调度器。

## 行为

每个账户都有独立历史记录，每次检查最多生成一条完整消息：

- 新账户或重新启用账户的第一次成功检查属于验证。当前范围内所有已出现稿件都以 `CURRENT` 报告；即使范围为空，也会发送验证消息。
- 后续检查会报告 `NEW`、`STATUS_CHANGED`、`DISAPPEARED` 和 `REAPPEARED` 事件。只有在完整且有效的控制台被成功解析，并且先前已接受的在场稿件消失时，才会生成消失事件。
- 只有标题或投稿日期发生变化时，程序会保存最新的完整快照，但不会发送通知。之后发生可通知事件时会使用这个更新后的快照。
- 同一账户的所有事件按稿件 ID 排序，并汇总为一条不截断的消息。不同账户的通知不会混合。
- 每个 Apprise 目标都会独立尝试。任意一个目标成功后，通知批次和延迟状态变更就会提交；同一批次中失败的其他目标不会重试。
- 如果所有目标都失败，本次检查会记录为失败，带通知的状态不会被接受。下一次单次运行会重新生成并重试完整通知。
- 如果外部服务已接收消息，但进程在 SQLite 提交前崩溃，后续运行可能产生重复消息。通知语义有意采用至少一次投递。
- 账户按配置顺序运行。一个账户的抓取、解析或全部投递失败不会阻止后续账户，但整个进程会以非零状态退出。

非首次检查没有任何变更时，程序会成功结束而不发送消息。

## 要求

- 首选 Python 3.14，最低支持 Python 3.10。参见 [Python 官方下载页面](https://www.python.org/downloads/)。
- 原生运行需要安装 Google Chrome 或 Chromium。
- 如果既未配置 `browser.driver_path`，也未设置 `CHROMEDRIVER_PATH`，Selenium 会使用 [Selenium Manager](https://www.selenium.dev/documentation/selenium_manager/) 查找或获取兼容驱动。首次运行可能需要网络访问和可写缓存。离线主机应安装匹配的驱动并显式配置路径。
- Docker 部署需要 Docker Engine 或 Docker Desktop，以及 [Docker Compose](https://docs.docker.com/compose/install/)。

## 配置

`--config` 默认使用当前工作目录下的 `./config.toml`。配置文件内的路径支持展开 `~`；相对路径相对于配置文件所在目录解析。解析器不会为必填键提供隐式默认值。仓库中的配置文件包含可用的示例值：

```toml
[storage]
database_path = "data/submissions.db"

[browser]
headless = true
element_timeout_seconds = 30
page_load_timeout_seconds = 60
# binary_path = "/optional/path/to/chrome"
# driver_path = "/optional/path/to/chromedriver"

[[accounts]]
name = "primary"
url = "https://mc.manuscriptcentral.com/example"
username = "${PRIMARY_SCHOLARONE_USERNAME}"
password = "${PRIMARY_SCHOLARONE_PASSWORD}"
manuscript_ids = []
apprise_urls = ["${PRIMARY_APPRISE_URL}"]
```

可接受的键如下：

| 键 | 必填 | 含义 |
| --- | --- | --- |
| `storage.database_path` | 是 | SQLite 文件路径。程序会自动创建父目录。 |
| `browser.headless` | 是 | 布尔值，用于选择无头或可见 Chrome。 |
| `browser.element_timeout_seconds` | 是 | 等待登录和控制台元素的正整数秒数。 |
| `browser.page_load_timeout_seconds` | 是 | Selenium 页面加载超时的正整数秒数。 |
| `browser.binary_path` | 否 | 显式指定 Chrome 或 Chromium 可执行文件。`CHROME_BIN` 是后备覆盖方式。 |
| `browser.driver_path` | 否 | 显式指定 ChromeDriver。`CHROMEDRIVER_PATH` 是后备覆盖方式。 |
| `accounts[].name` | 是 | 唯一、区分大小写且持久的账户身份。请保持稳定。 |
| `accounts[].url` | 是 | 使用 HTTP 或 HTTPS 的 ScholarOne 登录 URL。 |
| `accounts[].username` | 是 | ScholarOne 用户名。 |
| `accounts[].password` | 是 | ScholarOne 密码。应使用环境变量引用。 |
| `accounts[].manuscript_ids` | 是 | 空数组表示跟踪控制台中的全部稿件；否则只跟踪列出的规范化短 ID。 |
| `accounts[].apprise_urls` | 是 | 每个账户独立的非空、无重复目标列表，采用 [Apprise URL 语法](https://appriseit.com/getting-started/universal-syntax/)。 |

重复添加 `[[accounts]]` 即可配置多个账户。非空 ID 过滤器会影响通知和活动跟踪范围，但完整控制台仍必须正确解析。移除 ID 或账户会关闭其活动跟踪周期，不删除历史，也不发送消失通知。恢复相同的账户 `name` 会复用原身份，并发送新的 `CURRENT` 验证；重命名账户等同于删除旧账户并新增账户。

所有配置字符串及字符串列表元素中的 `${ENV_VAR}` 都会递归展开。变量未设置或展开出现循环时属于配置错误。原生 Python 不会读取 `.env`；必须在调用它的 shell 或调度器中导出对应变量。Docker Compose 会加载仓库根目录的 `.env`。

`config.toml` 有意不提供调度键。原生调度由操作系统负责，Docker 调度则通过 `.env` 中的 `CRON_SCHEDULE` 配置。

## 使用 pip 安装

POSIX shell：

```bash
python3.14 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python main.py --config config.toml
```

Windows PowerShell：

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py --config config.toml
```

最后一条命令执行一次完整检查。运行前应导出 `config.toml` 引用的所有变量。

## 使用 uv 安装

按照 [uv 官方安装指南](https://docs.astral.sh/uv/getting-started/installation/) 安装 uv，然后使用已提交的锁文件：

```bash
uv python install 3.14
uv sync --locked
uv run --locked python main.py --config config.toml
```

`uv sync --locked` 会拒绝改写过期的锁文件。最后一条命令仍然只执行一次检查；更多信息见官方[锁定与同步指南](https://docs.astral.sh/uv/concepts/projects/sync/)。

## 原生调度

首先手动运行一次单次检查。调度任务需要绝对路径、网络访问、数据库目录写权限，以及与成功手动运行相同的环境变量。应防止任务重叠：一个进程会在整个运行期间持有 `<database_path>.lock`，并发进程会立即以代码 1 退出。

以下示例每六小时运行一次。请替换示例用户名和安装路径。

### Linux cron

创建 `/home/checker/.config/check-submission-status/env`，写入 `config.toml` 引用变量的 shell 赋值，并使用 `chmod 600` 限制权限。然后编辑 `checker` 用户的 crontab：

```bash
sudo -u checker crontab -e
```

```cron
0 */6 * * * set -a && . /home/checker/.config/check-submission-status/env && set +a && cd /home/checker/check-submission-status && /home/checker/check-submission-status/.venv/bin/python /home/checker/check-submission-status/main.py --config /home/checker/check-submission-status/config.toml >> /home/checker/check-submission-status/data/cron.log 2>&1
```

除非另行配置，cron 使用主机本地时区。参见 Cronie 上游 [`crontab(5)` 源文件](https://github.com/cronie-crond/cronie/blob/master/man/crontab.5)。

### Linux systemd timer

创建仅 root 可读的 `/etc/check-submission-status.env`，然后创建 `/etc/systemd/system/check-submission-status.service`：

```ini
[Unit]
Description=Check ScholarOne submission statuses
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
User=checker
Group=checker
WorkingDirectory=/opt/check-submission-status
EnvironmentFile=/etc/check-submission-status.env
UMask=0077
ExecStart=/opt/check-submission-status/.venv/bin/python /opt/check-submission-status/main.py --config /opt/check-submission-status/config.toml
```

创建 `/etc/systemd/system/check-submission-status.timer`：

```ini
[Unit]
Description=Run the ScholarOne status check every six hours

[Timer]
OnCalendar=*-*-* 00,06,12,18:00:00
Persistent=true
RandomizedDelaySec=5m

[Install]
WantedBy=timers.target
```

启用并检查定时器：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now check-submission-status.timer
systemctl list-timers check-submission-status.timer
journalctl -u check-submission-status.service
```

参见上游 [`systemd.timer` 手册](https://www.freedesktop.org/software/systemd/man/latest/systemd.timer.html)。

### macOS launchd

创建受保护的 `/Users/alice/.config/check-submission-status/env`，其中包含 shell 变量赋值。将以下内容保存为 `/Users/alice/Library/LaunchAgents/io.github.check-submission-status.plist`：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>io.github.check-submission-status</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/zsh</string>
    <string>-c</string>
    <string>set -a &amp;&amp; source /Users/alice/.config/check-submission-status/env &amp;&amp; set +a &amp;&amp; exec /Users/alice/check-submission-status/.venv/bin/python /Users/alice/check-submission-status/main.py --config /Users/alice/check-submission-status/config.toml</string>
  </array>
  <key>WorkingDirectory</key>
  <string>/Users/alice/check-submission-status</string>
  <key>RunAtLoad</key>
  <true/>
  <key>StartInterval</key>
  <integer>21600</integer>
  <key>StandardOutPath</key>
  <string>/Users/alice/Library/Logs/check-submission-status.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/alice/Library/Logs/check-submission-status.error.log</string>
</dict>
</plist>
```

验证、加载、测试并检查用户代理：

```bash
plutil -lint ~/Library/LaunchAgents/io.github.check-submission-status.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/io.github.check-submission-status.plist
launchctl kickstart -k gui/$(id -u)/io.github.check-submission-status
launchctl print gui/$(id -u)/io.github.check-submission-status
```

替换或删除任务前，先执行 `launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/io.github.check-submission-status.plist`。Apple 在[创建 Launch Daemon 和 Agent](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html)中说明了周期任务。

### Windows 任务计划程序

把所需变量设置为任务账户的受保护用户环境变量，然后注销并重新登录，使新的后台进程继承它们。在 PowerShell 中注册一个每六小时运行并直接调用单次脚本的任务：

```powershell
$taskCommand = '"C:\Users\Alice\check-submission-status\.venv\Scripts\python.exe" "C:\Users\Alice\check-submission-status\main.py" --config "C:\Users\Alice\check-submission-status\config.toml"'
schtasks.exe /Create /TN "Check Submission Status" /TR $taskCommand /SC HOURLY /MO 6 /F
schtasks.exe /Run /TN "Check Submission Status"
schtasks.exe /Query /TN "Check Submission Status" /V /FO LIST
```

任务应使用已经配置环境和文件的同一用户运行。删除命令为 `schtasks.exe /Delete /TN "Check Submission Status" /F`。Microsoft 在 [`schtasks /create`](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/schtasks-create) 中说明了所有调度和账户选项。

## Docker Compose

### 配置并启动

复制示例环境文件，然后替换所有占位值，并选择 cron 调度和时区：

```bash
cp .env.example .env
```

```powershell
Copy-Item .env.example .env
```

```dotenv
CRON_SCHEDULE='0 */6 * * *'
TZ='UTC'
PRIMARY_SCHOLARONE_USERNAME='replace-me'
PRIMARY_SCHOLARONE_PASSWORD='replace-me'
PRIMARY_APPRISE_URL='replace-me'
```

秘密保存在 `.env` 中，`config.toml` 保留 `${...}` 引用。先静默验证 Compose，再构建并启动：

```bash
docker compose config --quiet
docker compose up --build -d
docker compose logs -f checker
```

验证包含秘密的部署时必须带 `--quiet`。直接运行 `docker compose config` 会渲染已经解析的环境变量值，可能在终端、日志或支持包中泄露秘密。`CRON_SCHEDULE` 为必填项；未设置或设置为空会导致 Compose 验证失败。

容器启动时，Compose 会先验证生成的五字段 crontab，立即执行一次单次检查，然后启动 Supercronic。立即检查的暂时失败会被记录，但后续计划任务仍会继续。cron 语法无效时，调度器不会启动。`TZ` 控制 cron 调度时区；应用历史时间戳始终使用 UTC。

### 日志与生命周期

```bash
docker compose logs --tail=200 checker
docker compose restart checker
docker compose stop checker
docker compose start checker
docker compose down
```

`restart` 会先再次立即检查，然后恢复调度。`stop` 和 `down` 都保留命名数据卷。除非确实要删除全部 SQLite 历史，否则不要运行 `docker compose down -v`。参见 Docker 官方 [Compose 应用模型](https://docs.docker.com/compose/intro/compose-application-model/)。

## SQLite 历史、备份与恢复

原生运行使用 `storage.database_path`。Compose 将 `submission-data` 命名卷挂载到 `/app/data`，因此示例数据库位于 `/app/data/submissions.db`。物理主机路径由 Docker 管理；普通容器替换不会删除数据卷。

SQLite 会保留账户身份、配置版本、过滤器、检查记录、跟踪周期、在场与缺席观察、已接受当前状态、完整通知消息，以及按目标记录且经过脱敏的投递结果。移除账户或过滤器只会关闭活动历史，不会删除行。数据库会把 ScholarOne URL 和用户名作为配置历史保存，也会保存稿件元数据。它绝不会保存 ScholarOne 密码或完整 Apprise URL；投递行只保留目标位置、协议、结果、时间戳和固定安全错误。

数据库没有加密。应将它及其备份作为敏感稿件数据保护。

备份前必须先停止检查器。以下 POSIX shell 流程把备份写到工作树外，打开源数据库以便 SQLite 恢复可能存在的热回滚日志，使用 [SQLite 在线备份 API](https://www.sqlite.org/backup.html)，并验证结果：

```bash
(
set -eu
BACKUP_DIR="$HOME/check-submission-status-backups/$(date +%Y%m%d-%H%M%S)"
BACKUP_CONTAINER="check-submission-status-backup-$(date +%s)"
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
docker compose stop checker
docker compose run --name "$BACKUP_CONTAINER" --no-deps --entrypoint /app/.venv/bin/python checker -c "import sqlite3, sys; expected={'account_configuration_targets','account_configurations','accounts','checks','current_states','manuscripts','notification_batches','notification_deliveries','observations','tracking_periods'}; source=sqlite3.connect('file:/app/data/submissions.db?mode=rw', uri=True); source_tables={row[0] for row in source.execute(\"SELECT name FROM sqlite_master WHERE type = 'table'\")}; sys.exit('source schema check failed') if source.execute('PRAGMA user_version').fetchone()[0] != 2 or not expected <= source_tables else None; target=sqlite3.connect('/tmp/submissions.db'); source.backup(target); target_tables={row[0] for row in target.execute(\"SELECT name FROM sqlite_master WHERE type = 'table'\")}; valid=target.execute('PRAGMA integrity_check').fetchone()[0] == 'ok' and target.execute('PRAGMA user_version').fetchone()[0] == 2 and expected <= target_tables; target.close(); source.close(); sys.exit('backup integrity or schema check failed') if not valid else None"
docker cp "$BACKUP_CONTAINER:/tmp/submissions.db" "$BACKUP_DIR/submissions.db"
docker rm "$BACKUP_CONTAINER"
chmod 600 "$BACKUP_DIR/submissions.db"
docker compose start checker
)
```

`BACKUP_DIR` 必须位于仓库外，并按实时数据库同等标准保护。如果备份创建失败，应先检查保留的 `BACKUP_CONTAINER` 日志，再删除该容器，并在确认安全后重启检查器。

恢复时选择一个已完成备份，停止检查器，以只读方式验证备份，删除旧数据库及所有可能的 SQLite 边车文件，再复制到命名卷、修正所有权，最后才启动检查器：

```bash
(
set -eu
BACKUP_DIR="$HOME/check-submission-status-backups/20260710-120000"
docker compose stop checker
docker compose run --rm --no-deps --user root --entrypoint /app/.venv/bin/python -v "$BACKUP_DIR:/backup:ro" checker -c "import sqlite3, sys; expected={'account_configuration_targets','account_configurations','accounts','checks','current_states','manuscripts','notification_batches','notification_deliveries','observations','tracking_periods'}; database=sqlite3.connect('file:/backup/submissions.db?mode=ro', uri=True); tables={row[0] for row in database.execute(\"SELECT name FROM sqlite_master WHERE type = 'table'\")}; valid=database.execute('PRAGMA integrity_check').fetchone()[0] == 'ok' and database.execute('PRAGMA user_version').fetchone()[0] == 2 and expected <= tables; database.close(); sys.exit('backup integrity or schema check failed') if not valid else None"
docker compose run --rm --no-deps --user root --entrypoint /bin/rm checker -f /app/data/submissions.db /app/data/submissions.db-journal /app/data/submissions.db-wal /app/data/submissions.db-shm
docker compose create checker
docker compose cp "$BACKUP_DIR/submissions.db" checker:/app/data/submissions.db
docker compose run --rm --no-deps --user root --entrypoint /bin/chown checker app:app /app/data/submissions.db
docker compose start checker
)
```

执行 `docker compose cp` 时目标容器可以处于停止状态；参见官方 [`docker compose cp` 参考](https://docs.docker.com/reference/cli/docker/compose/cp/)。原生部署应停止所有计划和手动运行，并使用 Python 的 `sqlite3.Connection.backup`，不要只复制采用回滚日志的主数据库文件。验证备份、保留文件权限，然后恢复调度。

## 故障排查与退出代码

- **无法完成登录：** 将 `browser.headless = false` 后手动运行一次。当前 ScholarOne 流程要求名称为 `USERID` 和 `PASSWORD` 的字段、按钮 `#logInButton`、`Author` 导航链接以及表格 `#authorDashboardQueue`。站点标记变化时需要更新选择器。
- **控制台表格缺失或损坏：** 整个账户检查会安全失败；程序绝不会将它当作空控制台，已接受状态也不会前进。检查 URL、凭据、作者角色和当前 ScholarOne 页面结构。
- **Chrome 或驱动无法启动：** 确保 Chrome 与 ChromeDriver 主版本匹配。配置 `browser.binary_path` 和 `browser.driver_path`，或设置 `CHROME_BIN` 和 `CHROMEDRIVER_PATH`。删除这些覆盖即可恢复 Selenium Manager。
- **通知重复：** 如果所有目标都失败，由于状态未提交，重复属于预期行为。应逐一测试 Apprise URL。如果至少一个目标成功，变更已经提交，失败的同批目标不会重试。
- **锁冲突：** 另一个进程正在使用同一数据库。等待它退出，检查调度器是否重叠，不要通过删除 `.lock` 文件来代替停止仍在运行的进程。
- **打开 Chrome 前就因配置退出：** 所有验证错误会一起报告。检查未知或缺失键、路径解析、HTTP(S) 账户 URL、正数超时、重复名称或目标，以及已导出的环境变量。

进程退出代码：

| 代码 | 含义 |
| --- | --- |
| `0` | 所有已配置账户成功，包括账户列表为空的情况。`--help` 也返回 0。 |
| `1` | 至少一个账户、通知、浏览器、解析器、数据库或进程锁操作失败。 |
| `2` | 命令行用法或配置加载、验证失败。 |

单个计划检查以代码 1 退出后，Docker 的 Supercronic 进程仍会继续运行；应在服务日志中查看每次运行结果。

## 安全

- 绝不要提交 `.env`、已填入秘密的配置、SQLite 数据、备份、浏览器配置文件或日志。仓库会忽略 `.env`、`/data`、本地测试和演示夹具。
- 密码和 Apprise URL 应使用 `${ENV_VAR}` 引用，不要使用字面值。环境文件只允许调度账户读取；POSIX 使用 `chmod 600`，Windows 使用等效 ACL。
- Apprise URL 应视为凭据。不要将它粘贴到问题报告中，也不要在会被捕获的终端中运行非静默 Compose 渲染。
- 即使数据库不含密码和完整目标，也必须限制 SQLite 文件访问，因为它包含用户名、ScholarOne URL、稿件标题、状态和完整通知正文。
- 原生调度器应使用非特权专用账户。容器已经使用非 root 的 `app` 用户运行。

完整英文规范指南见 [README.md](README.md)。
