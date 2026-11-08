#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把规范化课表写成飞书多维表格（Base）：建表 + 26 字段 + 全量记录 + 日历/分组视图；支持复用已有 Base（--base-token/--table-id）。

已验证命令（lark-cli 1.0.88）：
  base +base-create --name --time-zone Asia/Shanghai --table-name 课表与考试 --fields @fields.json
  base +record-batch-create --base-token --table-id --json @records.json
  base +view-create --base-token --table-id --json '[{...}]'
  base +view-set-timebar / +view-set-visible-fields / +view-set-sort / +view-set-group

用法：
  python feishu_base_push.py --normalized normalized.json --base-name "我的课表" --dry-run
  python feishu_base_push.py --normalized normalized.json --base-name "张三 2026-2027-1 课表"
"""
import argparse
import json
import os
import shutil
import subprocess
import sys

# 26 个字段（与已验证的 base_fields.json 一致；课程/星期 的选项按数据自动补全）
FIELDS = [
    {"name": "日程标题", "type": "text"},
    {"name": "类型", "type": "select", "multiple": False,
     "options": [{"name": "上课", "hue": "Blue"}, {"name": "考试", "hue": "Red"}]},
    {"name": "开始时间", "type": "datetime", "style": {"format": "yyyy-MM-dd HH:mm"}},
    {"name": "结束时间", "type": "datetime", "style": {"format": "yyyy-MM-dd HH:mm"}},
    {"name": "课程", "type": "select", "multiple": False, "options": []},
    {"name": "授课内容", "type": "text"},
    {"name": "授课或监考教师", "type": "text"},
    {"name": "校区", "type": "text"},
    {"name": "教学场地", "type": "text"},
    {"name": "教学周", "type": "number", "style": {"type": "plain", "precision": 0}},
    {"name": "星期", "type": "select", "multiple": False, "options": []},
    {"name": "节次", "type": "text"},
    {"name": "教学环节", "type": "text"},
    {"name": "教学形式", "type": "text"},
    {"name": "教学班", "type": "text"},
    {"name": "考核方式", "type": "text"},
    {"name": "课程编号", "type": "text"},
    {"name": "平台课程编号", "type": "text"},
    {"name": "课序号", "type": "text"},
    {"name": "排课人数", "type": "number", "style": {"type": "plain", "precision": 0}},
    {"name": "上课人数", "type": "number", "style": {"type": "plain", "precision": 0}},
    {"name": "座位号", "type": "number", "style": {"type": "plain", "precision": 0}},
    {"name": "源备注", "type": "text"},
    {"name": "数据提示", "type": "text"},
    {"name": "源记录ID", "type": "text"},
    {"name": "学期", "type": "text"},
]
WEEKDAYS = ['星期一', '星期二', '星期三', '星期四', '星期五', '星期六', '星期日']
GRID_VIEW = '全部课次与考试'
GROUP_VIEW = '按课程分组'
CAL_VIEW = '课表日历'


def cli_argv():
    exe = shutil.which('lark-cli')
    if exe:
        return [exe]
    npm_global = os.path.join(os.environ.get('APPDATA', ''), 'npm')
    run_js = os.path.join(npm_global, 'node_modules', '@larksuite', 'cli', 'scripts', 'run.js')
    node = shutil.which('node')
    if os.path.exists(run_js) and node:
        return [node, run_js]
    sys.exit('找不到 lark-cli：请先 npm i -g @larksuite/cli')


def run_cli(args, payload=None):
    proc = subprocess.run(cli_argv() + args, input=payload, capture_output=True, text=True,
                          encoding='utf-8', errors='replace', timeout=180)
    out = (proc.stdout or '').strip()
    try:
        return json.loads(out)
    except Exception:
        return {'ok': False, '_raw': out[-1200:], '_stderr': (proc.stderr or '')[-1200:]}


def build_fields(events):
    fields = json.loads(json.dumps(FIELDS))  # deep copy
    courses = list(dict.fromkeys(e['course'] for e in events))
    for f in fields:
        if f['name'] == '课程':
            f['options'] = [{'name': c} for c in courses]
        if f['name'] == '星期':
            f['options'] = [{'name': d} for d in WEEKDAYS]
    return fields


def build_records(events, semester):
    recs = []
    for e in events:
        rec = {
            '日程标题': e['course'] if e['type'] == '上课' else '[考试] ' + e['course'],
            '类型': [e['type']],
            '开始时间': e['start'][:16],
            '结束时间': e['end'][:16],
            '课程': [e['course']],
            '授课内容': e.get('content') or None,
            '授课或监考教师': e.get('teacher') or None,
            '校区': e.get('campus') or None,
            '教学场地': e.get('location') or None,
            '教学周': e.get('week'),
            '星期': [e.get('weekday')] if e.get('weekday') else [],
            '节次': e.get('periods') or None,
            '教学环节': e.get('phase') or None,
            '教学形式': e.get('form') or None,
            '教学班': e.get('teachingClass') or None,
            '考核方式': e.get('assessment') or None,
            '课程编号': e.get('courseCode') or None,
            '平台课程编号': e.get('platformCourseCode') or None,
            '课序号': e.get('sequence') or None,
            '排课人数': e.get('scheduledStudents'),
            '上课人数': e.get('enrolledStudents') if isinstance(e.get('enrolledStudents'), int) else None,
            '座位号': e.get('seat'),
            '源备注': e.get('sourceRemark') or None,
            '数据提示': e.get('notes') or None,
            '源记录ID': e['sourceId'],
            '学期': semester,
        }
        recs.append(rec)
    return recs


def fetch_existing_ids(base_token, table_id, as_identity):
    """尽力读取已有记录的源记录ID，用于幂等复用；读不到就返回 None（按全量写入）。"""
    res = run_cli(['base', '+record-list', '--base-token', base_token, '--table-id', table_id,
                   '--as', as_identity, '--format', 'json'])
    if not res.get('ok'):
        return None
    data = res.get('data') or {}
    items = data.get('items') or data.get('records') or []
    ids = set()
    for it in items:
        fields = it.get('fields') or it
        val = fields.get('源记录ID')
        if isinstance(val, list):
            val = val[0] if val else None
        if val:
            ids.add(str(val))
    return ids


def main():
    ap = argparse.ArgumentParser(description='课表写入飞书多维表格')
    ap.add_argument('--normalized', required=True)
    ap.add_argument('--base-name', required=True, help='多维表格名称')
    ap.add_argument('--table-name', default='课表与考试')
    ap.add_argument('--time-zone', default='Asia/Shanghai')
    ap.add_argument('--as', dest='as_identity', default='user', choices=['user', 'bot'])
    ap.add_argument('--chunk', type=int, default=100, help='每批写入记录数')
    ap.add_argument('--workdir', default='feishu_base_payload',
                    help='中间文件目录（fields/records JSON 落这里）')
    ap.add_argument('--resources-out', default='lark_base_resources.json')
    ap.add_argument('--base-token', default=None,
                    help='复用已有 Base 的 token（配合 --table-id；给了就不新建 Base）')
    ap.add_argument('--table-id', default=None, help='复用已有 Base 时的 table id')
    ap.add_argument('--dry-run', action='store_true', help='只生成两次 payload 并打印命令')
    args = ap.parse_args()

    norm = json.load(open(args.normalized, encoding='utf-8'))
    events = [e for e in norm['events'] if e['type'] in ('上课', '考试')]
    semester = norm.get('semester') or ''
    os.makedirs(args.workdir, exist_ok=True)
    fields = build_fields(events)
    records = build_records(events, semester)

    fields_path = os.path.join(args.workdir, 'fields.json')
    records_path = os.path.join(args.workdir, 'records.json')
    json.dump(fields, open(fields_path, 'w', encoding='utf-8'), ensure_ascii=False)
    json.dump({'create_records': records}, open(records_path, 'w', encoding='utf-8'),
              ensure_ascii=False)
    print('字段 %d 个（课程选项 %d、星期选项 %d）；记录 %d 条' % (
        len(fields), len(fields[4]['options']), 7, len(records)), flush=True)

    if args.dry_run:
        print('--dry-run：payload 已写到 %s，命令如下' % args.workdir)
        print('  lark-cli base +base-create --name "%s" --time-zone %s --table-name "%s" '
              '--fields @%s --as %s' % (args.base_name, args.time_zone, args.table_name,
                                        fields_path, args.as_identity))
        print('  lark-cli base +record-batch-create --base-token <TOKEN> --table-id <TABLE> '
              '--json @%s --as %s' % (records_path, args.as_identity))
        # 抽样展示一条记录，便于肉眼确认字段映射
        print('  样例记录：' + json.dumps(records[0], ensure_ascii=False))
        return 0

    if args.base_token:
        if not args.table_id:
            sys.exit('--base-token 需要同时给 --table-id')
        base_token, table_id = args.base_token, args.table_id
        base = {'url': '(复用已有 Base)', 'base_token': base_token}
        table = {'id': table_id, 'views': []}
        existing = fetch_existing_ids(base_token, table_id, args.as_identity)
        if existing:
            before = len(records)
            records = [r for r in records
                       if str((r.get('fields') or {}).get('源记录ID')) not in existing]
            print('复用 Base：已存在 %d 条，跳过重复后待写 %d 条'
                  % (before - len(records), len(records)), flush=True)
        else:
            print('复用 Base：未能读取已有记录（按全量写入）', flush=True)
    else:
        created = run_cli(['base', '+base-create', '--name', args.base_name,
                           '--time-zone', args.time_zone, '--table-name', args.table_name,
                           '--fields', '@' + fields_path, '--as', args.as_identity])
        if not created.get('ok'):
            sys.exit('建表失败：%s' % json.dumps(created, ensure_ascii=False)[:1200])
        base = created['data']['base']
        table = created['data']['table']
        base_token, table_id = base['base_token'], table['id']
        print('已创建：%s（table %s）' % (base['url'], table_id), flush=True)

    total = 0
    for start in range(0, len(records), args.chunk):
        chunk = records[start:start + args.chunk]
        part = os.path.join(args.workdir, 'records_%03d.json' % start)
        json.dump({'create_records': chunk}, open(part, 'w', encoding='utf-8'), ensure_ascii=False)
        res = run_cli(['base', '+record-batch-create', '--base-token', base_token,
                       '--table-id', table_id, '--json', '@' + part, '--as', args.as_identity])
        if not res.get('ok'):
            sys.exit('写入第 %d 批失败：%s' % (start, json.dumps(res, ensure_ascii=False)[:1200]))
        n = len((res.get('data') or {}).get('record_id_list') or [])
        total += n
        print('  写入 %d 条（累计 %d）' % (n, total), flush=True)

    views = run_cli(['base', '+view-create', '--base-token', base_token, '--table-id', table_id,
                     '--json', json.dumps([{'name': GROUP_VIEW, 'type': 'grid'},
                                           {'name': CAL_VIEW, 'type': 'calendar'}]),
                     '--as', args.as_identity])
    cal_id = ''
    if views.get('ok'):
        for v in views['data'].get('views', []):
            if v.get('type') == 'calendar':
                cal_id = v['id']
    grid_id = (table.get('views') or [{}])[0].get('id', '')
    if cal_id:
        run_cli(['base', '+view-set-timebar', '--base-token', base_token, '--table-id', table_id,
                 '--view-id', cal_id,
                 '--json', json.dumps({'start_time': '开始时间', 'end_time': '结束时间',
                                       'title': '日程标题'}), '--as', args.as_identity])
    if grid_id:
        run_cli(['base', '+view-rename', '--base-token', base_token, '--table-id', table_id,
                 '--view-id', grid_id, '--name', GRID_VIEW, '--as', args.as_identity])
        run_cli(['base', '+view-set-sort', '--base-token', base_token, '--table-id', table_id,
                 '--view-id', grid_id,
                 '--json', json.dumps({'sort_config': [{'field': '开始时间', 'desc': False}]}),
                 '--as', args.as_identity])
    group_view = next((v['id'] for v in (views.get('data', {}) or {}).get('views', [])
                       if v.get('name') == GROUP_VIEW), '')
    if group_view:
        run_cli(['base', '+view-set-group', '--base-token', base_token, '--table-id', table_id,
                 '--view-id', group_view,
                 '--json', json.dumps({'group_config': [{'field': '课程', 'desc': False}]}),
                 '--as', args.as_identity])

    resources = {'baseToken': base_token, 'baseUrl': base['url'], 'tableId': table_id,
                 'gridViewId': grid_id, 'calendarViewId': cal_id,
                 'calendarUrl': base['url'] + '?table=' + table_id + '&view=' + cal_id,
                 'records': total, 'fields': len(fields)}
    json.dump(resources, open(args.resources_out, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print('完成：%d 条记录；资源信息写到 %s' % (total, args.resources_out), flush=True)
    print('多维表格：%s' % base['url'], flush=True)
    return 0 if total == len(records) else 1


if __name__ == '__main__':
    sys.exit(main())
