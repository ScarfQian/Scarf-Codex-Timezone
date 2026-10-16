# Scarf-Codex-Timezone

**让 macOS 上的 Codex 桌面应用在启动时采用 GPT 网络出口对应的时区，并核验实际页面。**

A macOS launcher that starts Codex with the timezone of its verified GPT egress, checks the actual application pages, and restores the normal system timezone on the next launch.

作者：ScarfQian · 首次发布：2026-10-15 · 更新：2026-10-16 · MIT · macOS 实验版 0.2.1

## 功能

**首次打开管理工具即默认开启自动跟随，无需先选模式。** 开关面板只显示开启或关闭，开启时提供 **关闭自动 / 更多操作 / 完成**；关闭后显示 **开启自动 / 更多操作 / 完成**。日常启动、恢复、开关操作和后台检查均不弹时区报告或系统通知；仅主动查看状态或执行检测时显示报告。

- **默认开启**：首次正常打开时安装用户级后台检测，并将原 Codex Dock 入口接入时区启动器。关闭管理窗口不会取消自动，重新登录后继续生效。Codex 未运行时只准备出口目标，不擅自打开软件；从 Dock 时区入口启动时自动设置和核验。
- **手动关闭**：点击“关闭自动”即停止并卸载后台检测，恢复本工具替换的原 Dock 入口；关闭设置一直保留到再次点击“开启自动”，不会因重开工具或升级而自行开启。正在运行的 Codex 时区保持原样。

“更多操作”保留原来的功能，并增加“查看自动状态”：

- **按出口启动**：检查 Mihomo 路由，检测 `chatgpt.com` 路径的公网 IP，查询该 IP 的 IANA 时区，使用进程环境变量 `TZ` 启动 Codex。
- **检查是否成功**：读取实际 Codex 页面，核对时区、当地日期和 UTC 偏移，并重新检查当前出口。报告成功、不一致或无法验证。
- **恢复本机时区**：完整退出 Codex 后，以不含 `TZ` 的子进程环境重新启动，并核验页面。

不改变 macOS 系统时间或系统时区，不改写 Codex/Clash 的配置，不强制退出应用。Unix 时间戳不变；继承 `TZ` 的子进程如何解释当地时间也可能改变。

**自动模式不等于任意启动方式都能即时改变整个应用。** 在运行中跨时区换出口，或绕过 Dock 时区入口启动原应用时，后台会记录“待同步”；主动查看状态时可看到保存后完整退出并重开的说明。它不会只改页面来冒充主进程和子进程都已同步，也不会修改已保存的自动化时区。

## 让自己的 Codex 帮忙配置

下载本仓库（GitHub 的 **Code → Download ZIP**）并解压，把仓库地址和下面这段话交给自己的 Codex：

> 请阅读 README 和 docs/BUILD_WITH_CODEX.md，先只读识别我的 macOS、Codex 可执行文件、GPT 请求路径、Clash/Mihomo 实际代理端口、控制接口和策略组。为我的环境填写配置，运行测试并构建桌面应用。不要复制别人的端口和策略组名称，不要修改系统时区或代理规则，不要退出正在运行的 Codex。未验证的部分请明确报告。

该项目需要 **macOS、已有的 Python 3.9+、curl、Codex 桌面应用，以及带可访问本机 Unix 控制 socket 的 Clash Verge/Mihomo**。没有 pip 依赖。只发布源码与本机构建脚本，不包含 Codex 或 Python，也未签名、公证；Windows、Linux 和其他代理客户端尚未验证。

## 配置与启动

进入解压后的目录，先确认 `python3 --version` 可用，然后将配置示例复制到用户目录：

```sh
mkdir -p "$HOME/Library/Application Support/Scarf-Codex-Timezone"
cp config.example.json "$HOME/Library/Application Support/Scarf-Codex-Timezone/config.json"
```

编辑这个 JSON，填入**自己电脑上观察到的值**：

| 字段 | 用途 |
| --- | --- |
| `codex_group` | OpenAI/ChatGPT 显式域名规则指向的策略组；示例 `GPT` 只是占位名称 |
| `proxy` | HTTP/mixed 正向代理地址；不是本地模型 API 网关端口 |
| `allowed_countries` | 默认 `["US"]`；可设 `["US", "CA"]` 或 `["*"]` |
| `timeout` | 每项请求的超时秒数，默认 12 |
| `codex_app` | 可选，Codex 的可执行文件完整路径，包含 `Contents/MacOS/...` |
| `mihomo_socket` | 可选，有效运行实例的 Unix 控制 socket 路径 |

省略 `codex_app` 时依次检查 `/Applications`、`~/Applications` 中的 `Codex.app` 和 `ChatGPT.app`；两者都存在时请明确配置。省略 `mihomo_socket` 时使用 Clash Verge 服务的当前用户路径 `/var/run/clash-verge-service/users/<uid>/verge-mihomo.sock`，其他版本或安装方式需要手动指定。

配置优先级：命令行参数 → 指定配置文件。未指定 `--config` 时，先读取源码/资源目录旁的 `config.json`，否则读取上述用户目录文件；两者都不存在则使用默认值。例：

```sh
python3 -B launcher.py --check-only --json
python3 -B launcher.py --gui
python3 -B launcher.py --status --json
python3 -B launcher.py --restore --json
python3 -B launcher.py --set-mode auto --json
python3 -B launcher.py --automation-status --json
python3 -B launcher.py --set-mode manual --json
python3 -B launcher.py --config /path/to/config.json --proxy http://127.0.0.1:7890 --codex-group GPT --countries US,CA --check-only --json
```

`--check-only` 仅检测出口，不代表当前实例已切换。检测、查看状态和 `--help` 均不会安装后台服务。命令行保留 `--set-mode manual` 关闭、`--set-mode auto` 开启。源码目录也可以双击 `launch.command`（首次必要时执行 `chmod +x launch.command`）。

### 构建可双击的应用

```sh
python3 -B build_app.py
```

生成 `dist/Scarf-Codex-Timezone.app`，可复制到桌面。应用包包含必要脚本，默认读取上述用户目录配置；不复制你的私有配置或系统图标。它依赖构建时选定的 Python 路径，不能把在一台电脑上构建的包当作任意电脑的免依赖安装包。需要时用 `--python /absolute/path/to/python3` 指定本机已有 Python。

双击管理工具即可默认开启；已有关闭设置优先保留。“更多操作”中的检测、切换和恢复仍需明确选择执行；点击“完成”或关闭窗口不会取消自动跟随。

### 自动模式如何持续生效

首次正常打开时会把必要脚本复制到 `~/Library/Application Support/Scarf-Codex-Timezone/runtime/`，后台不依赖源码目录或外置磁盘。用户级 LaunchAgent 为 `io.github.scarfqian.codex-timezone.auto`，在登录时开始，并约每 15 秒检查一次节点与实例；同节点约每两分钟复查公网 IP。没有系统级服务，也不设置全局 `TZ`。

默认入口安装在 `~/Applications/Scarf-Codex-Timezone-Launch.app`，Dock 名称为“Codex（时区启动）”。重复点击已运行的应用只激活和核验，不关闭进程。原应用更新时不改它的安装包；若更新后由其他入口重开，后台会报告待同步。

关闭自动会移除本工具的 LaunchAgent，并恢复它替换的那个 Dock 项，保留你对其他 Dock 项的修改。关闭不自动恢复当前进程时区；恢复是单独的手动操作。关闭后，残留的时区启动入口也按正常环境打开，不再自动检测。

后台只保存一份 `auto-status.json`，记录目标、当前页面核验与检查时间；出口不可确认时不会沿用旧成功结果。后台不弹窗，也不发送系统通知。需要查看当前结果时，在“更多操作”选择“检查是否成功”或“查看自动状态”。

## 何时恢复

切换和恢复前都要保存工作，并在 **Codex 中按 ⌘Q 完整退出**。工具不替你结束应用。完整退出后从原来的 Codex 图标打开，也会恢复正常启动环境。

只关闭 Codex 窗口、关闭工具菜单或结果提示，均不会恢复仍在运行的进程。恢复前先点击“关闭自动”。运行中换节点不会直接改进程时区；检测不一致时，完整退出后再按出口启动。后台检测持续保留，直到手动关闭。

## 成功报告与数据边界

启动时只为实际页面核验启用随机本机诊断端口：`--remote-debugging-port`，绑定 `127.0.0.1`。检查只读取原始 `Intl`、日期和偏移，不设置页面时区覆盖，不读取聊天内容，也不自动发送模型消息。该接口具备页面调试能力，随本次 Codex 进程退出关闭；不要将它转发或绑定到局域网/公网。

全部可读的 `app://` 页面时区、当地日期、UTC 偏移均符合目标，且本机系统时区未变，才报告 `VERIFIED`。实例无启动记录、页面未就绪、路由无法确认或数据不一致时，不冒充成功。本机 `session.json` 只记录进程、端口和时区，保存在配置目录，检查时重新核对进程。

**页面成功与模型收到的客户端时区分别验收。** 通过工具重启后，在自己的实际登录实例中新发一条消息，让 Codex 报告“本轮客户端提供的 timezone/current_date；没有提供就直说，不运行命令、不根据 IP 或聊天历史推测”。工具本身始终将 `model_context_verified` 标为 `false`，因为它没有检查模型请求。

IP 检测会访问 `chatgpt.com/cdn-cgi/trace`；指定公网 IP 会提交给 ipwho.is 和 ipinfo.io 查询时区。可用结果冲突时停止，一个服务成功时会标明实际来源。地理位置查询存在误差；同节点按连接轮换 IP 时不能保证诊断连接与每条模型连接相同。

## 当前支持范围

- 要求 Mihomo **规则模式**，以及显式的 `DOMAIN-SUFFIX,openai.com,<group>` 和 `DOMAIN-SUFFIX,chatgpt.com,<group>` 规则。它不是通用 Mihomo 规则解释器：更高优先级的进程/端口规则、复杂 rule-set 等仍需人工核实，不支持时应先适配，不能放宽检查冒充确认。
- 可解析嵌套 Selector、Fallback、URLTest。LoadBalance、Relay 不按单一节点推断。
- 只支持已经确认经由本机代理的 ChatGPT/OpenAI 路径。远程网关自行选择上游时，本机 trace 不能代表网关出口。
- 依赖当前应用支持进程 `TZ`、本机页面诊断和 `app://` 页面。应用升级后可能需要重新验证。
- 新建/重存自动化的默认时区可能受影响；退出或恢复不回改已经保存的任务时区。远端云任务的机器时区也不由本工具控制。

## 验证与开发

```sh
python3 -B -m unittest -v test_timezone
python3 -B timezone_probe.py --self-test
```

回归测试使用合成数据和本机 HTTP fixture，不查询示例 IP；这些示例与实际地理位置没有对应关系。隔离实测（另启临时用户目录）需要显式运行：

```sh
python3 -B timezone_probe.py --codex-group YOUR_GROUP --proxy http://127.0.0.1:YOUR_PORT --json
```

`timezone_probe.py` 是独立诊断原型，不读取启动器的 JSON 配置；默认只接受美国出口。其他国家的配置入口在 `launcher.py`。隔离页面测试不使用你的登录目录，也不能替代当前登录实例的验收。

0.2.0 已在开发者电脑完成 43 项回归检查，以及用户级 LaunchAgent、Dock 接入、自动→手动→自动切换和原 Dock 项恢复的实际验收。桌面菜单关闭后，后台仍定时核验；运行脚本保存在用户目录，不依赖外置磁盘。

0.2.1 已通过 51 项回归检查，包括启动核验成功、需要重启和暂时无法核验时均保持静默，以及主动查看仍可显示报告。实际桌面工具已验证首次打开默认启用、手动关闭后重开仍保持关闭、点击“完成”后后台继续核验。只读命令不安装服务，已有关闭设置不会被默认开启覆盖。

独立空白实例通过出口时区启动、持续页面核验、手动正常启动和系统时区恢复。跨时区出口变化使用明确标注的合成情景验证，确认报告待重启并保留运行实例；没有为此切换真实代理节点。实际登录实例的页面与本轮模型收到的客户端时区均为 `America/New_York`，系统仍为 `Asia/Shanghai`。登录时启动已配置，但未执行退出登录或重启电脑实测；其他电脑尚未验收。

## 许可

采用 [MIT License](LICENSE)。项目独立维护，与 OpenAI 或 Clash/Mihomo 无隶属关系。
