---
name: jiaowu-schedule
description: 抓取高校教务系统本人的课表、授课内容与考试安排，规范化为统一 JSON，导出 Excel/CSV/ICS，并写入飞书日历（手机端原生日程）与飞书多维表格。当用户说「导出课表/抓课表/把课表放进飞书日历/同步教务系统日程/查上课时间和教学内容/课表导入多维表格」时使用。内置示例高校 jw.example.edu.cn 的接口细节；其他学校替换取数层即可复用后三步。
whenToUse: 用户要获取或导出本人课表、课程内容、考试安排；或要同步到飞书日历/飞书多维表格；或要把教务官方导出（只有课程名和周次）补全成含起止钟点与教学内容的明细表。不用于查成绩、选课、抢课、代课、刷课。
---

# 教务课表 → 飞书日历/多维表格（全流程）

**产出三件套 + 两个线上目标**，顺序不能乱：先拿到**带时间和内容**的原始数据，再做规范化，再生成交付物，最后才推飞书。

| 阶段 | 产出 | 脚本/命令 |
|---|---|---|
| 1 取数 | `calendar_raw.json`（接口原始响应） | 浏览器内 fetch / DevTools（见 `references/01`） |
| 2 规范化 | `normalized_schedule.json` | `scripts/jw_normalize.py` |
| 3 交付物 | `*_详细课表.xlsx`、`*_飞书导入.csv`、`*_北京时间.ics` | `scripts/build_artifacts.py` |
| 4 推飞书 | 日历全量日程 / 多维表格全量记录 | `scripts/feishu_calendar_push.py`、`references/03` |
| 5 验收 | PASS/FAIL 报告 | `scripts/feishu_calendar_verify.py` + 本文件 §验收 |

工作目录建议：`<项目>\{raw,work,outputs}`，所有中间件落盘，保证任何一步都能重跑。

> 下文命令里的 `scripts/` 都相对**本 skill 目录**（`~/.dsh/skills/jiaowu-schedule/`）。先 `cd` 到技能目录或写绝对路径，别在项目目录里找脚本。

## 0. 开工前必须确认的三件事

1. **登录态**：能拿到教务系统已登录页面（`https://jw.example.edu.cn/new/welcome.page?ui=new`）里的课表接口响应。拿不到就先解决登录，不要用 UI 自动化硬点。
2. **交付形态**：用户要「多维表格」还是「手机日历」还是都要。日历=飞书原生日程（有提醒/锁屏），多维表格=可筛选的数据库视图。**两者要分别写**，别指望一个代替另一个。
3. **权威口径**：以教务接口返回值为准，**不静默修改**。发现异常（时间重叠、缺场地、内容里写的时段与排课不一致）只加标注，并在最终回复里列出。

## 1. 取数（以 示例高校 为例）

核心接口（详见 `references/01-jw-api.md`）：

```
POST https://jw.example.edu.cn/new/desktop/getCalendar
Content-Type: application/x-www-form-urlencoded
d1=2026-08-01 00:00:00&d2=2027-03-01 00:00:00
→ JSON 数组：lx=kb(上课) / ks(考试) / xk(选课时间)
```

- **推荐**：借用用户已登录的浏览器标签页执行 `fetch`（浏览器桥接 / DevTools 控制台都行），一次拿全学期，直接落盘。
- 官方「导出」拿到的是 `printCalendar` 的 xls：**只有课程名 + 周次网格，没有钟点和教学内容**。它只配当交叉校验基准，不能当数据源。
- 抓完先核数量：上课记录数应等于官方 xls 底部「课表摘要」展开后的条数（两边的条数必须相等），不一致就停下查原因。

## 2. 规范化

```
python scripts/jw_normalize.py --raw raw/calendar_raw.json --out work/normalized_schedule.json
```

脚本会输出 events（上课+考试）、selections（选课时间段）、overlaps（时间重叠对），并做硬校验：日期与星期一致、`date-(week-1)*7` 恒定、结束晚于开始。**校验失败直接报错退出，不许绕过。**

需要人工过目的三类源数据异常（脚本会写进 `notes`，不要改数据）：

1. **时间重叠**：不同课同一时段、上课撞考试、同课多教室。要区分「真冲突」和「平行排课」。
2. **缺教学场地**：慕课/部分实验课教务系统就是空的。表格里写「未提供」，CSV 留空，不要编。
3. **授课内容里的时段与排课不一致**：例如实验课内容写 10:20–15:40，排课到 18:00。保留排课值 + 原文内容，加标注。

## 3. 交付物

```
python scripts/build_artifacts.py --normalized work/normalized_schedule.json --outdir outputs --stem 我的课表 --prefix 我的名字 --report outputs/自检报告.txt
```

- xlsx：`课次明细`（N 上课，23 列，含数据提示列）、`课程总览`（若干门课计数）、`考试安排`、`选课时间`；表头深蓝底白字、冻结窗格、日期/时间用原生格式。
- csv：27 列 × 全量行，UTF-8 无 BOM + CRLF，给飞书多维表格导入。
- ics：事件数与 normalized 一致，`TZID=Asia/Shanghai` + VTIMEZONE，物理行 ≤75 字节，考试事件带 `[考试] ` 前缀。
- 细节规格与验收项见 `references/04-export-artifacts.md`。

## 4. 推飞书

### 4a. 飞书日历（用户要「日历里显示日程」时走这条）

```
# 首次/上次登录过期时：先设备码授权（10 分钟内完成，见 references/02）
python scripts/feishu_calendar_push.py --normalized work/normalized_schedule.json
# 只推今天之后：加 --from-date 2026-10-28
```

- 用**底层 API** `calendar events create`，**不要**用 `calendar +create`：快捷命令会自动给每条加视频会议和 5 分钟提醒，批量课表不需要。
- `--idempotency-key` 由源记录 ID 派生，重跑不重复。
- 同课同时段多教室默认合并成一条（地点 `A/B`），否则日历里会出现两条重叠的同名日程。
- `start_time/end_time` 用 `{timestamp: 秒级Unix, timezone: Asia/Shanghai}`，禁止用本机时区换算。
- 详细字段、错误码、回滚见 `references/02-feishu-calendar.md`。

### 4b. 飞书多维表格（用户要「可筛选的数据库」时走这条）

见 `references/03-feishu-base.md`：
`base +base-create`（含 26 字段 schema）→ `+record-batch-create`（全量记录）→ `+view-create`（日历视图 + 按课程分组）→ `+record-list` 读回逐字段比对。

## 验收（缺一不可）

| 检查 | 通过标准 |
|---|---|
| 条数 | 上课+考试 = 接口返回数；多维表格/日历条数一致 |
| 日历 | 缺失 0 / 多余 0 / 重复 0；抽查提醒、地点、描述非空 |
| 时区 | 北京时间 08:30 落在正确时刻；抽查 2 条用 `+get` 核对 |
| 交付物 | xlsx 行数、CSV 行数、ICS 事件数与 normalized 事件数一致；三份的源记录 ID 集合相等 |
| 官方导出 | 课程门数/排课条数与 xls 摘要一致（本次 若干门 / N 条） |
| 异常 | 时间重叠、无场地、内容时段不一致等标注都出现在数据提示列与最终回复里 |

```
python scripts/feishu_calendar_verify.py --normalized work/normalized_schedule.json
```

**验收必须由独立脚本做，不能只信写入脚本自己的日志。**

## 铁律

1. **不静默修数据**。所有异常只标注不解散；用户要的是「教务系统里是什么」，不是「你猜是什么」。
2. **时间永远显式时区**。抓取按北京时间存储，推送用 `Asia/Shanghai` + Unix 秒；不要相信进程默认时区。
3. **幂等**。日历用 idempotency key、多维表格用「源记录ID」去重；重跑必须是升级而不是叠加。
4. **批量前先试一条**。`--limit 1` 建一条、`+get` 核实字段无误，再全量，省一次全量回滚。
5. **别把个人课表数据外发**。只写本地文件和用户自己的飞书空间；日志文件不含无关个人信息。

## 故障速查

| 现象 | 原因 | 处理 |
|---|---|---|
| 授权页 `20001 请求不合法` | 设备码过期（10 分钟） | 重新 `auth login --no-wait`，立刻让用户点 |
| `need_user_authorization / token_missing` | 用户 refresh token 过期被清空 | 重新设备码授权，见 `references/02` |
| 日历事件时间差 8 小时 | 用了本机时区换算 timestamp | 改 `Asia/Shanghai` 显式换算 |
| 日历里同课两条重叠 | 教务多教室平行排课未合并 | 用 `--no-merge` 反义的默认合并逻辑 |
| 多维表格 `--output` 报 unsafe path | CLI 只接受相对路径 | 先 `cd` 到目录再用 `./x.ndjson` |
| Python 读 JSON 报 `0xff` | PowerShell 5.1 重定向写的是 UTF-16 | 用 Python 写文件，或指定 `utf-16` 读取 |
| 表格里中文全乱码 | 控制台编码/文件编码 | 数据落盘用 UTF-8，看结果用文件别用回显 |
| 接口拿不到数据（跳登录页） | 浏览器标签被关/桥接切到了别的浏览器 | 让用户把已登录页面固定到前台，再 `find_tab` |

## 换学校 / 换学期

- 换学校：只改 `references/01` 里的取数与字段映射（`jw_normalize.py` 顶部有一个 `FIELD_MAP` 常量），后三步（交付物、飞书日历、多维表格）完全不用动。
- 换学期：确认 `xnxqdm`（如 `202601`）、把 `--semester` 和抓取区间改掉，其余同流程。
- 历史学期：同样流程再跑一遍，飞书日历记得加 `--from-date`/`--to-date` 限定，避免把往期课程堆进当前视图。

## 离线自测（改脚本后先跑这个）

```
python scripts/selftest.py
```

合成数据全流程自测（**不碰真实课表、不联网**）：原始 JSON → normalize → xlsx/csv/ics 三件套 → 3 类硬校验的反向用例。当前基线 **107/107 PASS**。

## 已验证状态（2026-10 实战基线）

- `jw_normalize.py`：对真实原始响应输出与已验收 `normalized_schedule.json` **字节级相同**（同一输入、同一输出）。
- `build_artifacts.py`：内置自检 **28/28 PASS**；生成的 CSV 与已验收成品**逐字节相同**；xlsx 与源逐格 0 差异。
- `feishu_calendar_push.py`：真实写入全量日程，失败 0，重跑无重复。
- `feishu_calendar_verify.py`：对线上日历复核 **PASS**（缺失 0 / 多余 0 / 重复 0）。
- `feishu_base_push.py`：生成的 payload 与线上已验证 Base 的 K 条记录**逐字段 0 差异**。
- 独立审计（另一个只读 agent + 官方 xls 交叉核对）：A–G 全 PASS，无 P0/P1 数据错误。

## 文件地图

```
scripts/jw_normalize.py            原始 JSON → normalized JSON（含校验/重叠/提示）
scripts/build_artifacts.py         normalized → xlsx + csv + ics（含自检）
scripts/feishu_calendar_push.py    normalized → 飞书日历（幂等批量）
scripts/feishu_calendar_verify.py  飞书日历 → 与 normalized 对账
scripts/selftest.py                离线自测：合成数据跑 normalize + 三件套（不联网）
references/01-jw-api.md      教务接口、字段字典、官方导出交叉校验
references/02-feishu-calendar.md   设备码授权、事件 body、时区、回滚
references/03-feishu-base.md       多维表格建表/写入/读回校验
references/04-export-artifacts.md  xlsx/csv/ics 规格与验收
references/05-lessons-pitfalls.md  本次实战踩过的坑（排障先读这个）
```
