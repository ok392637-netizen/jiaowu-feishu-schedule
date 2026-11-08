# 排障手册

先看两个日志：`automation/sync.log`（每 6 小时的同步）、`automation/keepalive.log`（每 10 分钟的续命）。

## 抓取相关

| 症状 | 原因 | 处理 |
|---|---|---|
| `登录过期` / `接口返回非课表数据（42896 字节 HTML）` | 教务会话失效（进程被杀或机器重启） | 双击 `重新登录课表账号.cmd`，用统一认证登录并勾选「7 天内无需再次登录」 |
| `无头 Edge 起不来（CDP 端口 9240 无响应）` | Edge 路径不对 / 端口被占用 | 检查 `edge_exe()` 候选路径；改 `config.json` 的 `cdpPort` |
| `抓取失败：CDP 脚本没有产出结果` | Node 版本太旧（<22，无全局 WebSocket） | 升级 Node，或在 `cdp_fetch.py` 里指定 `_node()` 路径 |
| 抓取成功但所有 `lx` 为空 | 接口/字段换了 | 用浏览器 F12 抓一次真实请求，更新 `skill/references/01-jw-api.md` 与 `jw_normalize.py` 的字段映射 |
| 每次都要重新登录 | 登录时没勾 rememberMe；或 keepalive 没运行 | 检查 `JiaowuKeepAlive` 任务状态；登录时勾「7 天内无需再次登录」 |

## 飞书相关

| 症状 | 原因 | 处理 |
|---|---|---|
| `飞书授权已过期` | `lark-cli` user token 的 refresh 窗口（通常 7 天）过了 | `lark-cli auth login --domain calendar,base --scope offline_access` 重新扫码 |
| 日程时间整体差 8 小时 | 写接口时只给了 `timestamp` 没给 `timezone` | 用 `{start_time:{timestamp:"...",timezone:"Asia/Shanghai"}}` |
| 重复日程 | 没用幂等键 / 描述里没有源记录ID | 创建时带 `--idempotency-key md5(sourceId)`；校验脚本按描述里的源记录ID 判重 |
| `+create` 自动多出会议和提醒 | 高层封装默认行为 | 用底层 `calendar events create` 原始接口 |

## 计划任务相关

| 症状 | 检查 |
|---|---|
| 任务不跑 | `schtasks /query /tn JiaowuScheduleSync /v /fo list` 看 Last Result；确认“仅在用户登录时运行”且用户已登录 |
| 跑了但没日志 | 任务动作是否指向 `run_sync.vbs`；vbs 里的 pythonw 路径是否存在 |
| 开机后第一次不跑 | 已设置 `StartWhenAvailable`，等任务计划程序补跑；或手动 `schtasks /run` |

## 数据正确性

- `jw_normalize.py` 会做校验：日期-星期一致性、结束时间晚于开始时间（真实解析时间）、周次锚点一致；失败会非零退出。时间重叠只标注，不判失败。
- 官方导出（xls）只有「课程名 + 周次网格」，没有具体时间和教学内容，不能作为时间来源，只能用来交叉核对课程集合。
- 时间重叠、内容里写的时段与排课冲突等源数据问题**不修改**，只写进 `notes`（「数据提示」列）。
- 删除保护：一次要删超过 `maxDeletes`（默认 5）条会中止；确认是教务真的清空后再加 `--allow-mass-delete`。
