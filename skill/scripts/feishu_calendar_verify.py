#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""核对飞书日历里写入的课表是否与 normalized 数据一一对应（只读，不写飞书）。

用法：
  python feishu_calendar_verify.py --normalized normalized.json
  python feishu_calendar_verify.py --normalized normalized.json --from 2026-09-01 --to 2027-02-01

判定依据：只统计描述里含「源记录ID」的日程，避免把用户自己的日程算进来。
"""
import argparse
import collections
import json
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone

BJ = timezone(timedelta(hours=8))
MARK = '源记录ID'


def cli_argv():
    exe = shutil.which('lark-cli')
    if exe:
        return [exe]
    import os
    npm_global = os.path.join(os.environ.get('APPDATA', ''), 'npm')
    run_js = os.path.join(npm_global, 'node_modules', '@larksuite', 'cli', 'scripts', 'run.js')
    node = shutil.which('node')
    if os.path.exists(run_js) and node:
        return [node, run_js]
    sys.exit('找不到 lark-cli')


def run_cli(args):
    proc = subprocess.run(cli_argv() + args, capture_output=True, text=True,
                          encoding='utf-8', errors='replace', timeout=180)
    try:
        return json.loads((proc.stdout or '').strip())
    except Exception:
        sys.exit('lark-cli 返回无法解析：%s' % (proc.stdout or proc.stderr)[-800:])


def epoch(dt_str):
    return int(datetime.strptime(dt_str, '%Y-%m-%d %H:%M:%S').replace(tzinfo=BJ).timestamp())


def to_epoch(value):
    if isinstance(value, int):
        return value
    s = str(value)
    if s.isdigit():
        return int(s)
    return int(datetime.fromisoformat(s).timestamp())


def merge_parallel(events):
    out, seen = [], {}
    for e in events:
        key = (e['date'], e['start'], e['end'], e['course'], e['type'])
        if key in seen:
            p = seen[key]
            p['sourceId'] += ',' + e['sourceId']
            p['mergedCount'] = p.get('mergedCount', 1) + 1
            continue
        seen[key] = e
        out.append(e)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--normalized', required=True)
    ap.add_argument('--from', dest='date_from', default='2026-01-01')
    ap.add_argument('--to', dest='date_to', default='2027-12-31')
    ap.add_argument('--as', dest='as_identity', default='user', choices=['user', 'bot'])
    ap.add_argument('--exam-prefix', default='【考试】')
    ap.add_argument('--sample', type=int, default=3, help='抽查多少条日程详情（含提醒/地点）')
    args = ap.parse_args()

    norm = json.load(open(args.normalized, encoding='utf-8'))
    events = [e for e in norm['events'] if e['type'] in ('上课', '考试')]
    merged = merge_parallel(events)
    merged = [e for e in merged if args.date_from <= e['date'] <= args.date_to]

    res = run_cli(['calendar', '+agenda', '--start', args.date_from, '--end', args.date_to,
                   '--as', args.as_identity, '--format', 'json'])
    if not res.get('ok'):
        sys.exit('拉取日程失败：%s' % json.dumps(res, ensure_ascii=False)[:800])
    agenda = res['data']
    mine = [e for e in agenda if MARK in (e.get('description') or '')]
    others = [e for e in agenda if MARK not in (e.get('description') or '')]

    exp = collections.Counter()
    for e in merged:
        title = (args.exam_prefix + e['course']) if e['type'] == '考试' else e['course']
        exp[(title, epoch(e['start']), epoch(e['end']))] += 1
    got = collections.Counter()
    for e in mine:
        got[(e['summary'], to_epoch(e['start_time'].get('timestamp') or e['start_time'].get('datetime')),
             to_epoch(e['end_time'].get('timestamp') or e['end_time'].get('datetime')))] += 1

    missing = exp - got
    extra = got - exp
    dups = [(k, v) for k, v in got.items() if v > 1]

    print('飞书中带源记录ID的日程：%d 条；同区间其他日程（非课表）：%d 条' % (len(mine), len(others)))
    print('期望：%d 条' % len(merged))
    print('缺失：%d  多余：%d  重复：%d' % (sum(missing.values()), sum(extra.values()), len(dups)))
    for k, v in list(missing.items())[:5]:
        print('  缺失', k, v)
    for k, v in list(extra.items())[:5]:
        print('  多余', k, v)
    for k, v in dups[:5]:
        print('  重复', k, v)

    by_type = collections.Counter('考试' if e['summary'].startswith(args.exam_prefix) else '上课' for e in mine)
    print('类型分布：%s' % dict(by_type))
    print('带地点日程：%d / %d' % (sum(1 for e in mine if (e.get('location') or {}).get('name')), len(mine)))
    dates = sorted((e['start_time'].get('datetime') or '')[:10] for e in mine)
    if dates:
        print('日期跨度：%s ~ %s（按本机时区的显示日期）' % (dates[0], dates[-1]))

    # 抽查详情：agenda 列表不含 reminders，需要逐条 +get
    failures = 0
    if mine and args.sample > 0:
        step = max(1, len(mine) // args.sample)
        picks = [mine[i] for i in range(0, len(mine), step)][:args.sample]
        for e in picks:
            one = run_cli(['calendar', '+get', '--event-id', e['event_id'],
                           '--as', args.as_identity, '--format', 'json'])
            d = (one.get('data') or {}) if one.get('ok') else {}
            reminders = d.get('reminders') or []
            print('抽查 %s | 提醒=%s | 地点=%s | 描述长度=%d' % (
                d.get('summary'), reminders, (d.get('location') or {}).get('name'),
                len(d.get('description') or '')))
            if not reminders or not (d.get('description') or ''):
                failures += 1

    ok = not missing and not extra and not dups and failures == 0
    print('结论：%s' % ('PASS' if ok else 'FAIL'))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
