# jiaowu-feishu-schedule

把高校教务系统的课表、考试和教学内容同步到**飞书日历**（手机端原生日程），并做成**无人值守的每日自动同步**：新增考试自动加、调课自动改、停课自动删，顺便产出 Excel / CSV / ICS。

> 这是一个「技能 + 自动化」组合仓库：
> - `skill/` 是一份可被 Claude Code / Codex / DeepSeek Harness 等 agent 直接加载的技能（SKILL.md + references + 脚本）；
> - `automation/` 是把它变成 Windows 计划任务的运行时（常驻无头 Edge + 定时续命 + 增量写飞书）。

## 它解决什么问题

| 场景 | 结果 |
|---|---|
| 教务系统只给一张「周次网格」，没有具体上课时间/教室/教学内容 | 抓接口拿到逐条排课记录，补齐时间、地点、教师、授课内容 |
| 想在手机日历里直接看课表 | 写入飞书原生日程，课前 10 分钟提醒，时区固定 Asia/Shanghai |
| 教务处临时加考试/调课/换教室 | 每天自动对账，增量创建/更新/删除 |
| 无人值守跑一个月 | 常驻无头 Edge + 每 10 分钟续命，避免「过几天就掉登录」 |
| 课程内容需要导出给别人 | 生成 详细课表.xlsx（4 个工作表）/ 飞书导入.csv / 北京时间.ics |

## 架构

```
教务接口 ──CDP──► 常驻无头 Edge(独立 profile) ──► 规范化 JSON ──► 飞书日历(增量)
                        ▲                              │
                  每 10 分钟续命                  Excel/CSV/ICS + 飞书通知
```

细节和为什么不用「浏览器扩展借窗口 / 直接读 Cookie / 账号密码自动登录」，见 [docs/architecture.md](docs/architecture.md)。

## 快速开始

### 1. 安装技能（agent 侧）

把 `skill/` 整个目录复制到你的 agent 技能目录：

```bash
# Claude Code
cp -r skill ~/.claude/skills/jiaowu-schedule
# DeepSeek Harness（本仓库开发环境）
cp -r skill ~/.dsh/skills/jiaowu-schedule
# Codex
cp -r skill ~/.codex/skills/jiaowu-schedule
```

然后对 agent 说「导出我的课表 / 同步到飞书日历」，它会按 SKILL.md 的流程走。

### 2. 配置自动化

```bash
cd automation
cp config.example.json config.json
# 改 calendarId / notifyUserId / prefix / stem / d1 / d2
```

- `calendarId`：飞书主日历 ID（`lark-cli calendar calendars primary --as user` 可查）
- `notifyUserId`：接收提醒的飞书 open_id（留空则不推送）
- `baseUrl`：你学校的教务根地址（默认 `https://jw.example.edu.cn`，**必须改**）
- `d1`/`d2`：抓取区间，覆盖整个学期
- `stem`：导出文件名前缀

### 3. 登录一次

```bash
python relogin_helper.py     # 或双击生成的「重新登录课表账号.cmd」
```

在弹出的 Edge 窗口里登录，**建议勾选「7 天内无需再次登录」**，登录后关闭窗口。

### 4. 装计划任务并验证

```powershell
powershell -ExecutionPolicy Bypass -File .\install_tasks.ps1
python sync_schedule.py --dry-run     # 先看差异，不写飞书
python sync_schedule.py               # 正式同步
```

装好后会有两个任务：

| 任务 | 频率 | 作用 |
|---|---|---|
| `JiaowuScheduleSync` | 每 6 小时 | 抓课表 → 增量更新飞书日历 → 重建交付物 |
| `JiaowuKeepAlive` | 每 10 分钟 | 给教务会话续命，避免空闲超时掉登录 |

## 目录结构

```
skill/                       # agent 技能
  SKILL.md                   # 工作流与硬性规则
  references/01-jw-api.md    # 教务接口、字段字典、交叉校验
  references/02-feishu-calendar.md
  references/03-feishu-base.md
  references/04-export-artifacts.md
  references/05-lessons-pitfalls.md
  scripts/jw_normalize.py    # 抓取结果 → 统一 JSON（含校验）
  scripts/build_artifacts.py # JSON → xlsx / csv / ics
  scripts/feishu_calendar_push.py
  scripts/feishu_calendar_verify.py
  scripts/feishu_base_push.py
  scripts/selftest.py        # 离线自测（合成数据，不联网）
automation/                  # Windows 无人值守运行时
  cdp_fetch.py / cdp_grab.mjs    # 常驻无头 Edge + DevTools 协议取数
  keepalive.py                   # 10 分钟续命 + 登录失效提醒
  relogin_helper.py              # 一键重新登录窗口
  sync_schedule.py               # 增量同步主流程
  install_tasks.ps1              # 安装/更新两个计划任务
  config.example.json
docs/architecture.md / troubleshooting.md
```

## 数据清洗与隐私

- 仓库内**不含任何真实课表、姓名、学号、Cookie、Token 或飞书资源 ID**；正文中的「示例课程」「教学楼 B101」和日期均为虚构示例。
- 运行时产生的 `config.json`、`edge-auto/`（登录态 profile）、`raw/`、`work/`、`outputs/`、`state.json`、日志全部在 `.gitignore` 中，不会误提交。
- 技能文档里的教务接口路径被抽象为 `jw.example.edu.cn`；接入其他学校时只需替换取数层和字段映射。

## 已知限制

- 自动化运行时目前是 **Windows**（依赖 Edge、`schtasks`、VBS 静默启动）；技能部分跨平台，换掉取数层即可。
- 教务系统的登录/接口会变；字段映射集中在 `jw_normalize.py` 的 `FIELD_MAP`，改一处即可。
- 登录态依赖常驻浏览器进程：**机器重启或进程被杀后需要重新登录一次**（会收到飞书提醒；登录窗口一键打开）。
- 飞书侧依赖 `lark-cli`（`@larksuite/cli`）+ 用户 OAuth；refresh token 有 7 天窗口，本项目的每次同步心跳会滚动续期。

## 许可

MIT，见 [LICENSE](LICENSE)。
