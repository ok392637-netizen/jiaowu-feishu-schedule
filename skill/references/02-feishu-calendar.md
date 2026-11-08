# 02 · 飞书日历写入（lark-cli）

> 目标：让 M 条课程/考试直接出现在用户**手机飞书日历**里，带地点、授课内容、课前提醒。

## 1. 身份与授权

| 场景 | 身份 | 说明 |
|---|---|---|
| 写「我」本人的日程 | `--as user` | 唯一正确选择；用户主日历 |
| bot 自己的日历 | `--as bot` | 无法写用户主日历，别试 |
| 读多维表格 | `--as bot` 常可 | 用户 token 过期时用它复核（见 `03`） |

**授权（设备码，10 分钟有效）**：

```bash
lark-cli auth login --domain calendar --scope offline_access --no-wait --json
# → {device_code, verification_url, expires_in:600}
lark-cli auth qrcode "<verification_url>" --output ./qr.png --size 320   # 让用户扫码
# 用户授权完成后：
lark-cli auth login --device-code "<device_code>"
lark-cli auth status --json --verify     # identity=user, identities.user.status=ready
```

要点（都是实战踩出来的）：

1. **先准备好所有脚本，再生成设备码**。10 分钟很短，生成完还在写代码就会看到授权页 `20001 请求不合法`。
2. **别去“修”验证码**。`flow_id`/`device_code` 里会出现连续 10 个 `O`（如 `...xROOOOOOOOOO2fGR...`）——**这是飞书的真实格式**，不是打码。改动任何一个字符都会 20001。
3. 授权页打不开时的兜底：把 URL 原样给出 + 同时给出 4-4 的 `user_code`；二维码用 `auth qrcode` 生成 PNG 后**直接把图片发给用户**（不要让用户复制 URL 里的长 query）。
4. 用户 refresh token 过期时，`auth status` 会显示 `missing` 并且 CLI 会清空本地身份；此时必须重新走设备码，不能靠 `--as bot` 顶替。
5. access token 约 2 小时；批量写入全量只需几分钟，通常一次授权够用；`offline_access` 让 token 可刷新，避免每次都重登。

## 2. 主日历

```bash
lark-cli calendar calendars primary --as user --format json
# data.calendars[0].calendar.calendar_id = feishu.cn_xxx@group.calendar.feishu.cn
# data.calendars[0].calendar.summary = 用户746931
```

不传 `--calendar-id` 时 `+create`/events API 默认主日历，但**显式取一次并存下来**更稳（写日志、便于回滚）。

## 3. 事件写入：用底层 API，不要用 `+create`

`calendar +create` 会自动：
- 给每条日程加 **飞书视频会议（vchat）**
- 只给 **5 分钟**提醒
- 设置 `attendee_ability=can_modify_event`

批量课表不需要这些，改完还要逐条 update。直接用：

```bash
lark-cli calendar events create \
  --calendar-id "<CAL_ID>" \
  --idempotency-key "<key>" \
  --data @body.json \
  --as user --format json
```

`--data` 支持 `-`（stdin）和 `@file`；用 Python 脚本循环时用 stdin 传，避免生成大量临时文件。

### 请求体（已验证可用）

```json
{
  "summary": "示例课程B",
  "description_rich": "**授课内容**：总论\n**教师**：示例教师\n**场地**：教学楼B101 · 大学城\n**节次**：1、2、3（第 1 周 · 星期一）\n**教学班**：示例班级A,...\n**考核方式**：考试\n\n---\n源记录ID：kb-1234567",
  "start_time": {"timestamp": "1788741000", "timezone": "Asia/Shanghai"},
  "end_time": {"timestamp": "1788750000", "timezone": "Asia/Shanghai"},
  "free_busy_status": "busy",
  "visibility": "default",
  "reminders": [{"minutes": 10}],
  "location": {"name": "教学楼B101"}
}
```

- `timestamp` 是**秒级 Unix 字符串**。计算时必须显式按 UTC+8：`datetime.strptime(s,'%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone(timedelta(hours=8))).timestamp()`。用本机时区会整体偏 8 小时（本机在 America/Chicago 时尤其明显）。
- `description_rich` 是 Markdown（`description` 是纯文本版，二者**互斥**）。日程详情里标题、列表都能正常渲染。
- `location.name` 只填教室名，不要把校区塞进去。
- 不传 `reminders` 就没有提醒；课表建议 10 分钟。
- 考试建议标题前缀 `【考试】`，一眼能区分。

### 幂等键

`idempotency_key` 在「应用 + 日历」维度唯一。用源记录 ID 派生：

```python
hashlib.md5(('jiaowu-schedule:' + source_id).encode()).hexdigest()
```

重跑同一批不会产生重复日程（先单条试写、再全量，重跑仍是同一批，0 重复）。

### 合并规则

教务系统里“同课程、同起止、两间教室”的平行排课（本次 2026-11-18 示例实验课 B201/B202）在日历里必须合并成一条，地点写成 `教学楼B201/教学楼B202`，描述里注明“已合并”，否则用户在日历里看到两条重叠的同名日程会以为是重复数据。

真正的时间冲突（不同课程重叠、上课撞考试）**不要合并**，保留两条，把冲突写进描述——那正是用户需要看到的。

## 4. 批量节奏

- 顺序写，每条约 1~2 秒；全量约 4 分钟。并发容易触发限流，不值当。
- 失败重试 3 次（1.2s/2.4s 退避）；单条失败不中断整批，最后汇总。
- 每批结束把 `{sourceId: {event_id, summary, start, end}}` 写入日志 JSON——回滚和后续 `--from-date` 增量都靠它。

## 5. 验收（必须做）

```bash
python scripts/feishu_calendar_verify.py --normalized normalized.json
```

检查点：

1. `calendar +agenda --start 2026-09-01 --end 2027-02-01 --as user --format json` 拉回区间内全部日程，只统计描述含 `源记录ID` 的（避免混入用户自己的其他日程）。
2. 用 **(标题, 开始epoch, 结束epoch) 多重集**与 normalized 合并后的期望集比对：缺失 0 / 多余 0 / 重复 0。
3. **提醒和地点不在 agenda 返回里**，要抽样 `calendar +get --event-id` 复核 reminders/location/description。
4. `agenda` 返回的 `start_time.datetime` 是**本机时区**的展示值（如America/Chicago 的 `-05:00`），但 `start_time.timezone` 是 `Asia/Shanghai`。比较时一律换算成 epoch，别拿字符串比。

## 6. 回滚 / 只保留未来课程

**删单条**：

```bash
lark-cli calendar events delete --calendar-id "<CAL_ID>" --event-id "<EVENT_ID>" --as user
```

**清掉“已过去的课”**（用户常见追加需求）：拉 agenda → 取描述含 `源记录ID` 且 `start < 今天` 的 event_id → 逐条删。删前先打印清单让用户确认条数，删除是**不可逆**的高影响操作，必须用户明确同意。

**重来一遍**：先按上面撤掉旧数据，再用 `--from-date` 限定区间重推；幂等键能保证不会因为重跑产生重复。

## 7. 错误码

| 返回 | 含义 | 处理 |
|---|---|---|
| 授权页 `20001` | 设备码/flow 失效 | 重新 `auth login --no-wait`，别复用旧链接 |
| `need_user_authorization (token_missing)` | 用户身份缺失/过期 | 重新设备码授权 |
| `permission denied` / 99991672 | scope 未授予 | 确认 `--domain calendar` 并重新授权 |
| `invalid param: start_time` | timestamp 不是秒级字符串 | 检查换算与类型 |
| 429 / 频繁失败 | 限流 | 串行 + 退避重试，不要并发 |

## 8. 已验证的本次结果（可作为基线）

- 全量日程 = 全部上课记录（合并平行排课后）+ 全部考试记录
- 缺失 0 / 多余 0 / 重复 0；部分记录教务系统本就无场地（`jxcdmc` 为空）
- 提醒 10 分钟、`free_busy_status=busy`、`visibility=default` 均按 body 落库
