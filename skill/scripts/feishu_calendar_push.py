#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把规范化课表写入飞书日历（用户身份，批量、幂等、可重跑）。

依赖：lark-cli（npm 包 @larksuite/cli），且已用用户身份登录并有 calendar 写权限。
用法示例：
  python feishu_calendar_push.py --normalized normalized.json
  python feishu_calendar_push.py --normalized normalized.json --from-date 2026-10-28
  python feishu_calendar_push.py --normalized normalized.json --limit 3 --dry-run

设计要点（都是踩坑换来的，别改）：
1. 用底层 API `calendar events create` 而不是 `calendar +create` 快捷命令，
   因为快捷命令会给每条日程自动加视频会议(vchat)和 5 分钟提醒，批量课表不需要。
2. 时间用 {timestamp: 秒级 Unix, timezone: Asia/Shanghai}，不要用本地时区换算。
3. --idempotency-key 由源记录 ID 派生，重跑不会产生重复日程。
4. 默认把「同课程、同起止、两间教室」的平行排课合并成一条，地点写 A/B。
5. 失败重试 3 次；每条结果写日志 JSON，便于审计和续跑。
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

BJ = timezone(timedelta(hours=8))
DEFAULT_REMINDER_MINUTES = 10


# ---------------------------------------------------------------- CLI 定位
def cli_argv():
    """返回调用 lark-cli 的 argv 前缀。优先 PATH 里的 shim，其次 node + run.js。"""
    exe = shutil.which('lark-cli')
    if exe:
        return [exe]
    npm_global = os.path.join(os.environ.get('APPDATA', ''), 'npm')
    run_js = os.path.join(npm_global, 'node_modules', '@larksuite', 'cli', 'scripts', 'run.js')
    node = (shutil.which('node')
            or os.path.join(os.path.expanduser('~'), '.cache', 'codex-runtimes',
                            'codex-primary-runtime', 'dependencies', 'node', 'bin', 'node.exe'))
    if os.path.exists(run_js) and node and os.path.exists(node):
        return [node, run_js]
    sys.exit('找不到 lark-cli：请先 `npm i -g @larksuite/cli` 并确认 PATH 可用。')


def run_cli(args, payload=None, timeout=120):
    cmd = cli_argv() + args
    env = dict(os.environ)
    env.setdefault('PYTHONIOENCODING', 'utf-8')
    proc = subprocess.run(cmd, input=payload, capture_output=True, text=True,
                          encoding='utf-8', errors='replace', timeout=timeout, env=env)
    out = (proc.stdout or '').strip()
    try:
        body = json.loads(out)
    except Exception:
        body = {'ok': False, '_raw': out[-1500:], '_stderr': (proc.stderr or '')[-1500:]}
    if proc.returncode != 0:
        body.setdefault('ok', False)
    return body


# ---------------------------------------------------------------- 数据处理
def epoch(dt_str):
    """'2026-09-07 08:30:00'（北京时间）-> 秒级 Unix 时间戳字符串。"""
    naive = datetime.strptime(dt_str, '%Y-%m-%d %H:%M:%S')
    return str(int(naive.replace(tzinfo=BJ).timestamp()))


def merge_parallel(events):
    """同课程/同起止的记录合并为一条，地点用 / 连接，sourceId 用逗号连接。"""
    out, seen = [], {}
    for e in events:
        key = (e['date'], e['start'], e['end'], e['course'], e['type'])
        if key in seen:
            prev = seen[key]
            locs = [x for x in (prev.get('location'), e.get('location')) if x]
            prev['location'] = '/'.join(dict.fromkeys(locs))
            prev['sourceId'] += ',' + e['sourceId']
            prev['mergedCount'] = prev.get('mergedCount', 1) + 1
            continue
        seen[key] = e
        out.append(e)
    return out


def build_body(e, exam_prefix='【考试】', reminder_minutes=DEFAULT_REMINDER_MINUTES):
    lines = []
    if e['type'] == '考试':
        summary = exam_prefix + e['course']
        lines.append('**考场**：%s' % (('%s · %s' % (e['location'], e['campus'] or ''))
                                       if e['location'] else '教务系统未提供'))
        extra = []
        if e.get('seat') not in (None, ''):
            extra.append('座位号 %s' % e['seat'])
        if e.get('scheduledStudents') not in (None, ''):
            extra.append('考试人数 %s' % e['scheduledStudents'])
        if extra:
            lines.append('**座位/人数**：' + ' · '.join(extra))
        if e.get('teacher'):
            lines.append('**监考教师（接口）**：%s' % e['teacher'])
    else:
        summary = e['course']
        lines.append('**授课内容**：%s' % (e.get('content') or '未提供'))
        lines.append('**教师**：%s' % (e.get('teacher') or '未提供'))
        if e.get('location'):
            lines.append('**场地**：%s · %s' % (e['location'], e.get('campus') or ''))
        else:
            lines.append('**场地**：教务系统未提供（慕课/线上或未排教室）')
        if e.get('teachingClass'):
            lines.append('**教学班**：%s' % e['teachingClass'])
        if e.get('assessment'):
            lines.append('**考核方式**：%s' % e['assessment'])
        if e.get('mergedCount', 1) > 1:
            lines.append('**说明**：教务系统同课同时段有 %d 条平行排课（多教室），已合并'
                         % e['mergedCount'])
    if e.get('periods'):
        lines.append('**节次**：%s（第 %s 周 · %s）' % (e['periods'], e['week'], e['weekday']))
    note = (e.get('notes') or '').strip().rstrip('。')
    if note:
        lines.append('**教务提示**：%s' % note)
    lines.append('')
    lines.append('---')
    lines.append('源记录ID：%s · 课表快照' % e['sourceId'])

    body = {
        'summary': summary,
        'description_rich': '\n'.join(lines),
        'start_time': {'timestamp': epoch(e['start']), 'timezone': 'Asia/Shanghai'},
        'end_time': {'timestamp': epoch(e['end']), 'timezone': 'Asia/Shanghai'},
        'free_busy_status': 'busy',
        'visibility': 'default',
    }
    if reminder_minutes is not None and reminder_minutes >= 0:
        body['reminders'] = [{'minutes': int(reminder_minutes)}]
    if e.get('location'):
        body['location'] = {'name': e['location']}
    return body


def idempotency_key(source_id, salt='jiaowu-schedule'):
    return hashlib.md5(('%s:%s' % (salt, source_id)).encode('utf-8')).hexdigest()


# ---------------------------------------------------------------- 主流程
def resolve_primary_calendar(as_identity):
    res = run_cli(['calendar', 'calendars', 'primary', '--as', as_identity, '--format', 'json'])
    try:
        cal = res['data']['calendars'][0]['calendar']
        return cal['calendar_id'], cal.get('summary', '')
    except Exception:
        sys.exit('无法获取主日历：%s' % json.dumps(res, ensure_ascii=False)[:800])


def main():
    ap = argparse.ArgumentParser(description='课表写入飞书日历（幂等批量）')
    ap.add_argument('--normalized', required=True, help='normalized_schedule.json 路径')
    ap.add_argument('--calendar-id', default='', help='默认自动取用户主日历')
    ap.add_argument('--from-date', default='', help='只推送 date >= 该日期（YYYY-MM-DD）')
    ap.add_argument('--to-date', default='', help='只推送 date <= 该日期')
    ap.add_argument('--limit', type=int, default=0, help='最多推送 N 条（调试用）')
    ap.add_argument('--dry-run', action='store_true', help='只打印请求体，不写飞书')
    ap.add_argument('--no-merge', action='store_true', help='不合并同课同时段的平行排课')
    ap.add_argument('--exam-prefix', default='【考试】')
    ap.add_argument('--reminder-minutes', type=int, default=DEFAULT_REMINDER_MINUTES,
                    help='课前提醒分钟数，-1 表示不设提醒')
    ap.add_argument('--as', dest='as_identity', default='user', choices=['user', 'bot'])
    ap.add_argument('--log', default='feishu_calendar_push_log.json')
    ap.add_argument('--max-retries', type=int, default=3)
    args = ap.parse_args()

    norm = json.load(open(args.normalized, encoding='utf-8'))
    events = [e for e in norm['events'] if e['type'] in ('上课', '考试')]
    total_raw = len(events)
    if not args.no_merge:
        events = merge_parallel(events)
    if args.from_date:
        events = [e for e in events if e['date'] >= args.from_date]
    if args.to_date:
        events = [e for e in events if e['date'] <= args.to_date]
    if args.limit:
        events = events[:args.limit]
    print('待推送：%d 条（原始事件 %d）' % (len(events), total_raw), flush=True)

    cal_id, cal_name = (args.calendar_id, '') if args.calendar_id else resolve_primary_calendar(args.as_identity)
    print('目标日历：%s %s' % (cal_id, ('(%s)' % cal_name) if cal_name else ''), flush=True)
    if args.dry_run:
        for e in events[:5]:
            print('[dry-run] %s %s %s' % (e['date'], e['start'][11:16],
                                         build_body(e, args.exam_prefix, args.reminder_minutes)['summary']),
                  flush=True)
        print('[dry-run] 共 %d 条，未写入' % len(events), flush=True)
        return 0

    log = {}
    ok = fail = 0
    for i, e in enumerate(events, 1):
        body = build_body(e, args.exam_prefix, args.reminder_minutes)
        cmd = ['calendar', 'events', 'create', '--calendar-id', cal_id,
               '--idempotency-key', idempotency_key(e['sourceId']),
               '--data', '-', '--as', args.as_identity, '--format', 'json']
        res = None
        for attempt in range(max(1, args.max_retries)):
            res = run_cli(cmd, payload=json.dumps(body, ensure_ascii=False))
            if res.get('ok'):
                break
            time.sleep(1.2 * (attempt + 1))
        if res and res.get('ok'):
            ok += 1
            ev = (res.get('data') or {}).get('event') or {}
            log[e['sourceId']] = {'ok': True, 'event_id': ev.get('event_id'),
                                  'summary': body['summary'], 'start': e['start'], 'end': e['end']}
            print('[%3d/%3d] OK %s %s %s' % (i, len(events), e['date'], e['start'][11:16], body['summary']),
                  flush=True)
        else:
            fail += 1
            log[e['sourceId']] = {'ok': False, 'error': json.dumps(res, ensure_ascii=False)[:800]}
            print('[%3d/%3d] FAIL %s %s' % (i, len(events), e['sourceId'],
                                            json.dumps(res, ensure_ascii=False)[:300]), flush=True)

    json.dump({'calendarId': cal_id, 'total': len(events), 'ok': ok, 'fail': fail,
               'events': log}, open(args.log, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print('完成：成功 %d / 失败 %d / 合计 %d；日志 %s' % (ok, fail, len(events), args.log), flush=True)
    return 0 if fail == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
