# 架构与设计取舍

## 数据流

```
教务系统 (jw.example.edu.cn)
        │  ① CDP: 页面上下文 POST /new/desktop/getCalendar
        ▼
常驻无头 Edge（独立 profile edge-auto）
        │  ② keepalive 任务每 10 分钟打一次真实请求 → 服务端会话不过期
        ▼
jw_normalize.py  ──►  统一 JSON（上课/考试/选课时间段 + 校验）
        │
        ├─► build_artifacts.py ─► Excel / CSV / ICS（本地交付物）
        │
        ▼
sync_schedule.py  (每 6 小时)
        │  ③ 与 state.json 比对：新增 → 创建、内容变了 → 更新、教务删了 → 删除
        ▼
飞书日历（用户身份 lark-cli，幂等键 = md5(源记录ID)）
        │  ④ 有变化或失败 → 飞书 bot 私聊通知（失败每天最多一条）
        ▼
用户
```

## 为什么用「常驻无头 Edge + CDP」

| 方案 | 结果 | 原因 |
|---|---|---|
| 每次抓取启动一个无头 Edge，抓完就杀 | ❌ 登录很快失效 | 教务是**服务端会话**，浏览器进程没了/空闲超时就掉登录；每次都要重新登录 |
| 浏览器扩展（如 WebBridge）借当前窗口 | ❌ 后台任务不可用 | 扩展只认「有焦点的窗口」，计划任务抢不到前台，报 `No current window`，还会反复弹窗 |
| 直接读浏览器 Cookie 发 HTTP 请求 | ❌ 解密不了 | Edge 156+ 的 Cookie 是 v20（App-Bound）加密、且数据库运行时被独占锁，复制/换路径都解不开 |
| 账号密码自动登录 | ❌ 有验证码 | 登录页需要图形验证码（部分学校还会风控），不适合无人值守 |
| **常驻无头 Edge + 定时续命** | ✅ 实测稳定 | 登录态在进程内存里一直有效；续命请求让服务端会话不过期 |

## 关键实现点

- `cdp_fetch.py`：固定 CDP 端口（默认 9240）、独立 profile；只杀名字含 `edge-auto` 的 Edge 进程，绝不影响用户自己的浏览器。
- `cdp_grab.mjs`：Node 22+ 原生 WebSocket 连接 DevTools，`Runtime.evaluate` 里 `fetch` 接口；被踢回登录页时先走一次 `/new/ssoLogin`（有 CAS 票据会自动回跳）。
- `keepalive.py`：每 10 分钟真实请求一次；登录失效时释放 profile 并把提醒压到每天一条；用户手动登录期间暂停 15 分钟避免抢 profile。
- `sync_schedule.py`：内容指纹（23 个字段 sha1）判断是否需要更新；创建用 md5(sourceId) 幂等键；删除有阈值护栏（默认 5 条）。
- 时区：日期时间字符串按 `Asia/Shanghai` 写入飞书；`timestamp` 用 UTC 秒 + 显式 timezone，避免 8 小时偏差。

## 失败恢复

| 情况 | 行为 |
|---|---|
| 教务会话过期（进程死/重启） | 飞书提醒一次；双击「重新登录课表账号.cmd」，keepalive 15 分钟内接管 |
| 飞书 user token 过期 | 每次同步先做一次 API 心跳滚动续期；真过期时提醒重新授权 |
| 抓取异常导致大量记录消失 | 删除超过阈值直接中止，避免误删整张日历 |
| 单条写飞书失败 | 记录到 `sync_status.json`，不更新 state，下次重试 |
