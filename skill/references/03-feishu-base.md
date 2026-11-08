# 03 · 飞书多维表格写入（lark-cli base）

> 目标：把 K 条课次/考试记录写成可筛选、可分组、带日历视图的 Base。用户说“放在飞书多维表格里”走这条；说“在日历里显示日程”走 `02`。

## 1. 一键脚本

```bash
python scripts/feishu_base_push.py --normalized normalized.json --base-name "张三 2026-2027-1 课表与教学内容"
python scripts/feishu_base_push.py --normalized normalized.json --base-name "测试" --dry-run  # 只生成 payload
```

脚本做的事（每条命令都已在 2026-10 实战验证）：

1. `base +base-create --name ... --time-zone Asia/Shanghai --table-name 课表与考试 --fields @fields.json`
   → 返回 `base_token`、`table.id`、`base.url`；CLI 会自动删掉默认空表。
2. `base +record-batch-create --base-token ... --table-id ... --json @records.json`
   → K 条一次可过（脚本按 `--chunk 100` 分批更稳）。
3. `base +view-create --json '[{"name":"按课程分组","type":"grid"},{"name":"课表日历","type":"calendar"}]'`
4. `base +view-set-timebar --view-id <日历视图> --json '{"start_time":"开始时间","end_time":"结束时间","title":"日程标题"}'`
5. 默认表格视图改名「全部课次与考试」+ 按开始时间升序；分组视图按「课程」分组。

产物：`lark_base_resources.json`（baseToken/baseUrl/tableId/viewId/calendarUrl/记录数），后续读回、分享都用它。

## 2. 字段模型（26 个）

| 字段 | 类型 | 来源（normalized event） | 备注 |
|---|---|---|---|
| 日程标题 | text | `course`（考试加 `[考试] ` 前缀） | |
| 类型 | select（上课/考试） | `type` | 写入值是数组 `["上课"]` |
| 开始时间 / 结束时间 | datetime（`yyyy-MM-dd HH:mm`） | `start`/`end` 取前 16 位 | 必须带时间，不能只给日期 |
| 课程 | select | `course` | 选项按数据自动生成，避免预先漏课 |
| 授课内容 | text | `content` | 空→null |
| 授课或监考教师 | text | `teacher` | |
| 校区 / 教学场地 | text | `campus` / `location` | 无场地→null（不要写“未提供”，与 xlsx 渲染约定区分） |
| 教学周 | number | `week` | |
| 星期 | select（星期一…星期日） | `weekday` | 数组 |
| 节次 / 教学环节 / 教学形式 / 教学班 / 考核方式 | text | `periods`/`phase`/`form`/`teachingClass`/`assessment` | |
| 课程编号 / 平台课程编号 / 课序号 | text | `courseCode`/`platformCourseCode`/`sequence` | 丢掉前导零会串课，保持字符串 |
| 排课人数 / 上课人数 / 座位号 | number | `scheduledStudents`/`enrolledStudents`/`seat` | 非数字给 null |
| 源备注 / 数据提示 | text | `sourceRemark`/`notes` | 数据提示=冲突/异常标注 |
| 源记录ID | text | `sourceId` | **去重与回滚的锚点**，必须写 |
| 学期 | text | normalized 顶层 `semester` | |

## 3. 视图配置

- **全部课次与考试**（grid）：按 `开始时间` 升序；可见字段建议保留到「数据提示」为止，把 ID 类字段放后面。
- **按课程分组**（grid）：`group_config: [{"field":"课程","desc":false}]`。
- **课表日历**（calendar）：`timebar` 必须绑定 `start_time=开始时间 / end_time=结束时间 / title=日程标题`，否则日历视图是空白。

```bash
lark-cli base +view-list --base-token <B> --table-id <T> --as user
lark-cli base +view-get-timebar --base-token <B> --table-id <T> --view-id <V> --as user
lark-cli base +view-set-visible-fields --base-token <B> --table-id <T> --view-id <V> \
  --json '{"visible_fields":["日程标题","类型","开始时间","结束时间","授课内容","授课或监考教师","教学场地"]}' --as user
```

## 4. 读回校验（必须做）

```bash
cd <目录>                       # --output 只接受相对路径！
lark-cli base +record-list --base-token <B> --table-id <T> --format ndjson \
  --output ./readback.ndjson --as user
```

- 同目录会生成 `readback.manifest.json`：`records_count`、`has_more`、`page_count`。**`has_more` 必须为 false**。
- 然后按「源记录ID」逐条比对全部字段（datetime 允许 `T...+08:00` 与 `YYYY-MM-DD HH:mm` 两种表示，比较时归一化）。
- 本次结果：记录数一致，字段级 0 差异。

### 用户 token 过期时怎么办

Base 是用户身份创建的，`--as user` 过期后读不了。若应用已被授权访问该 Base，`--as bot` 通常能读：

```bash
lark-cli base +base-get --base-token <B> --as bot
lark-cli base +record-list ... --as bot --format ndjson --output ./verify.ndjson
```

本次就是靠 bot 身份在用户 token 过期后完成线上复核（K 条、0 差异、3 个视图都在）。

## 5. 常见坑

| 坑 | 现象 | 处理 |
|---|---|---|
| `--output` 绝对路径 | `unsafe output path: --output must be a relative path` | 先 `cd` 到目标目录，用 `./x.ndjson` |
| datetime 只给日期 | 时间显示 00:00，日历视图错位 | 写 `YYYY-MM-DD HH:mm` |
| select 写字符串 | 报类型错误或选项不匹配 | 写数组 `["上课"]` |
| 选项漏课 | 记录里课程变成新选项但筛选列表不全 | 用数据自动生成 options |
| 一次塞太多 | 超时/部分失败 | `--chunk 100` 分批 |
| 中文控制台乱码 | 看不出写了什么 | 数据落 UTF-8 文件再核对 |
| 重复建表 | 用户目录里出现多个同名 Base | 建表前先用 `+title-resolve`/搜索确认是否已存在，或直接复用 `lark_base_resources.json` |

## 6. 清理

- 单条：`base +record-delete --base-token ... --table-id ... --record-ids ...`（有风险，先确认）。
- 表：`base +table-delete --base-token ... --table-id ...`。
- 整个 Base：lark-cli 没有 `+base-delete`；去飞书云文档里删除，或改用 `drive` 的文件删除接口。**删之前先确认 token/URL，别删错用户文件。**

## 7. 本次基线（可作为验收参考）

- Base：`张三 2026-2027-1 课表与教学内容`；主表 K 条（N 上课 + 3 考试）+ 第二张表「选课时间」6 条
- 3 个视图：全部课次与考试（按开始时间升序）、按课程分组（按课程）、课表日历（timebar 已绑定）
- 字段 26 个，读回字段级比对 0 差异；CSV 导入路径同样可走（`课表与教学内容_飞书导入.csv`，27 列）
