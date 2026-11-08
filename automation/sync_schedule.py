# -*- coding: utf-8 -*-
"""课表每日同步：教务 → 飞书日历（新增/变更/删除）+ 刷新 Excel/CSV/ICS 交付物。

用法：
    python sync_schedule.py --dry-run        # 只看差异，不写飞书
    python sync_schedule.py                  # 正式同步
    python sync_schedule.py --no-notify      # 不同步发飞书私聊
"""
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_SCRIPTS = os.environ.get('JIAOWU_SKILL_SCRIPTS') or os.path.join(os.path.dirname(HERE), 'skill', 'scripts')
CONFIG = os.path.join(HERE, 'config.json')
STATE = os.path.join(HERE, 'state.json')
STATUS = os.path.join(HERE, 'sync_status.json')
LOG = os.path.join(HERE, 'sync.log')
RAW_DIR = os.path.join(HERE, 'raw')
WORK_DIR = os.path.join(HERE, 'work')
OUT_DIR = os.path.join(HERE, 'outputs')


def log(msg):
    line = '%s  %s' % (time.strftime('%Y-%m-%d %H:%M:%S'), msg)
    print(line, flush=True)
    try:
        with open(LOG, 'a', encoding='utf-8') as fh:
            fh.write(line + '\n')
    except Exception:
        pass


def load_cfg():
    with open(CONFIG, encoding='utf-8') as fh:
        return json.load(fh)


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _node():
    for d in os.environ.get('PATH', '').split(os.pathsep):
        for name in ('node.exe', 'node'):
            p = os.path.join(d, name)
            if os.path.exists(p):
                return p
    return 'node'


def lark(args, stdin=None, timeout=120):
    """优先用 PATH 里的 lark-cli；退回 node + 全局包里的 run.js。"""
    exe = shutil.which('lark-cli') or shutil.which('lark-cli.cmd')
    if exe:
        cmd = [exe] + args
    else:
        cmd = [_node(), os.path.join(os.environ.get('APPDATA', ''), 'npm', 'node_modules',
                                     '@larksuite', 'cli', 'scripts', 'run.js')] + args
    try:
        proc = subprocess.run(cmd, input=stdin, capture_output=True, text=True,
                              encoding='utf-8', errors='replace', timeout=timeout)
    except Exception as e:
        return {'ok': False, '_error': str(e)}
    out = (proc.stdout or '').strip()
    try:
        body = json.loads(out)
    except Exception:
        body = {'ok': False, '_raw': out[-800:], '_stderr': (proc.stderr or '')[-800:]}
    body.setdefault('_exit', proc.returncode)
    return body


def fingerprint(event):
    keys = ['type', 'course', 'date', 'start', 'end', 'week', 'weekday', 'periods', 'content',
            'teacher', 'campus', 'location', 'phase', 'form', 'teachingClass', 'assessment',
            'courseCode', 'platformCourseCode', 'sequence', 'scheduledStudents', 'seat',
            'sourceRemark', 'notes']
    payload = {k: event.get(k) for k in keys}
    return hashlib.sha1(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()[:16]


def load_state(cfg):
    if os.path.exists(STATE):
        try:
            with open(STATE, encoding='utf-8') as fh:
                st = json.load(fh)
            st.setdefault('events', {})
            if st.get('calendarId') and st['calendarId'] != cfg.get('calendarId'):
                log('警告：state.json 里的 calendarId 与 config 不一致，按新日历重建状态')
                st = {'calendarId': cfg.get('calendarId'), 'events': {}}
            return st
        except Exception:
            pass
    return {'calendarId': cfg.get('calendarId'), 'events': {}}


def save_state(state):
    state['updatedAt'] = time.strftime('%Y-%m-%dT%H:%M:%S')
    with open(STATE, 'w', encoding='utf-8') as fh:
        json.dump(state, fh, ensure_ascii=False, indent=1)


def save_status(status):
    with open(STATUS, 'w', encoding='utf-8') as fh:
        json.dump(status, fh, ensure_ascii=False, indent=1)


def notify(cfg, text):
    uid = cfg.get('notifyUserId')
    if not uid or uid.startswith('ou_xxx'):
        return
    res = lark(['im', '+messages-send', '--as', 'bot', '--user-id', uid, '--text', text], timeout=60)
    if not res.get('ok'):
        log('通知发送失败：%s' % json.dumps(res, ensure_ascii=False)[:300])


def run_artifacts(cfg, normalized_path):
    report = os.path.join(OUT_DIR, '自检报告.txt')
    os.makedirs(OUT_DIR, exist_ok=True)
    proc = subprocess.run([sys.executable, os.path.join(SKILL_SCRIPTS, 'build_artifacts.py'),
                           '--normalized', normalized_path, '--outdir', OUT_DIR,
                           '--stem', cfg.get('stem') or '课表', '--prefix', cfg.get('prefix') or '',
                           '--report', report],
                          capture_output=True, text=True, encoding='utf-8', errors='replace',
                          timeout=300)
    if proc.returncode != 0:
        return False, ((proc.stdout or '')[-600:] + (proc.stderr or '')[-600:])
    dl = cfg.get('downloadsDir')
    if dl and os.path.isdir(dl):
        for suffix in ('_详细课表.xlsx', '_飞书导入.csv', '_北京时间.ics'):
            src = os.path.join(OUT_DIR, (cfg.get('stem') or '课表') + suffix)
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(dl, os.path.basename(src)))
    return True, 'ok'


def main():
    args = set(sys.argv[1:])
    dry = '--dry-run' in args
    cfg = load_cfg()
    for d in (RAW_DIR, WORK_DIR):
        os.makedirs(d, exist_ok=True)
    status = {'startedAt': time.strftime('%Y-%m-%dT%H:%M:%S'), 'ok': False, 'dryRun': dry,
              'changes': {'create': 0, 'update': 0, 'delete': 0}, 'errors': []}
    try:
        push = load_module('feishu_calendar_push', os.path.join(SKILL_SCRIPTS, 'feishu_calendar_push.py'))
        cdp_fetch = load_module('cdp_fetch', os.path.join(HERE, 'cdp_fetch.py'))

        # 飞书授权心跳：让 refresh token 滚动续期
        auth = lark(['calendar', 'calendars', 'primary', '--as', 'user', '--format', 'json'])
        if not auth.get('ok'):
            raise RuntimeError('飞书授权已过期（%s）' % json.dumps(auth, ensure_ascii=False)[:200])

        if cdp_fetch.login_in_progress():
            log('检测到正在手动重新登录，本轮暂停')
            status['ok'] = True
            status['paused'] = 'login-in-progress'
            save_status(status)
            return 0
        log('开始同步：抓取教务课表 …')
        ok, payload = cdp_fetch.fetch(cfg.get('d1'), cfg.get('d2'))
        if not ok and payload == cdp_fetch.LOGIN_IN_PROGRESS:
            log('检测到正在手动重新登录，本轮暂停')
            status['ok'] = True
            status['paused'] = 'login-in-progress'
            save_status(status)
            return 0
        if not ok:
            if payload == cdp_fetch.LOGIN_EXPIRED:
                raise RuntimeError('教务登录已过期（需要重新登录）')
            raise RuntimeError('抓取失败：%s' % payload)
        flag = os.path.join(HERE, 'login-needed.flag')
        if os.path.exists(flag):
            try:
                os.remove(flag)
            except Exception:
                pass
        stamp = time.strftime('%Y%m%d-%H%M')
        raw_path = os.path.join(RAW_DIR, 'calendar_raw_%s.json' % stamp)
        with open(raw_path, 'w', encoding='utf-8') as fh:
            json.dump(payload, fh, ensure_ascii=False)
        by = {}
        for x in payload:
            by[x.get('lx')] = by.get(x.get('lx'), 0) + 1
        log('抓取成功：%d 条 %s' % (len(payload), by))
        status['rawRecords'] = len(payload)

        norm_path = os.path.join(WORK_DIR, 'normalized_new.json')
        proc = subprocess.run([sys.executable, os.path.join(SKILL_SCRIPTS, 'jw_normalize.py'),
                               '--raw', raw_path, '--out', norm_path],
                              capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=180)
        if proc.returncode != 0:
            raise RuntimeError('规范化失败：%s' % (proc.stderr or proc.stdout)[-600:])
        with open(norm_path, encoding='utf-8') as fh:
            norm = json.load(fh)
        merged = push.merge_parallel([e for e in norm['events'] if e['type'] in ('上课', '考试')])
        current = {e['sourceId']: e for e in merged}
        status['events'] = len(current)
        log('规范化完成：%d 条日历事件（合并平行排课后）' % len(current))

        state = load_state(cfg)
        prev = state['events']
        creates = [k for k in current if k not in prev]
        updates = [k for k in current if k in prev and fingerprint(current[k]) != prev[k]['fp']]
        deletes = [k for k in prev if k not in current]
        log('差异：新增 %d、变更 %d、删除 %d' % (len(creates), len(updates), len(deletes)))
        status['diff'] = {'create': len(creates), 'update': len(updates), 'delete': len(deletes)}
        if len(deletes) > int(cfg.get('maxDeletes', 5)) and '--allow-mass-delete' not in args:
            raise RuntimeError('待删除 %d 条，超过安全阈值（疑似抓取异常），已中止' % len(deletes))

        if dry:
            for k in creates:
                e = current[k]
                log('  [dry] 新增 %s %s %s' % (k, e['date'], e['course']))
            for k in updates:
                log('  [dry] 变更 %s %s' % (k, current[k]['course']))
            for k in deletes:
                log('  [dry] 删除 %s %s' % (k, prev[k].get('summary')))
            status['ok'] = True
            status['finishedAt'] = time.strftime('%Y-%m-%dT%H:%M:%S')
            save_status(status)
            return 0

        calendar_id = cfg['calendarId']
        changed_lines = []
        for k in creates:
            e = current[k]
            body = push.build_body(e)
            res = lark(['calendar', 'events', 'create', '--calendar-id', calendar_id,
                        '--idempotency-key', push.idempotency_key(k), '--data', '-',
                        '--as', 'user', '--format', 'json'], stdin=json.dumps(body, ensure_ascii=False))
            ev = ((res.get('data') or {}).get('event') or {}) if res.get('ok') else {}
            if ev.get('event_id'):
                prev[k] = {'event_id': ev['event_id'], 'fp': fingerprint(e), 'summary': e['course'],
                           'start': e['start'], 'end': e['end']}
                status['changes']['create'] += 1
                changed_lines.append('新增：%s %s %s' % (e['date'], e['start'][11:16], e['course']))
                log('  created %s %s' % (k, ev['event_id']))
            else:
                status['errors'].append('create %s: %s' % (k, json.dumps(res, ensure_ascii=False)[:300]))

        for k in updates:
            e = current[k]
            body = push.build_body(e)
            body.pop('reminders', None)
            res = lark(['calendar', 'events', 'patch', '--calendar-id', calendar_id,
                        '--event-id', prev[k]['event_id'], '--data', '-', '--as', 'user',
                        '--format', 'json'], stdin=json.dumps(body, ensure_ascii=False))
            if res.get('ok'):
                prev[k].update({'fp': fingerprint(e), 'summary': e['course'],
                                'start': e['start'], 'end': e['end']})
                status['changes']['update'] += 1
                changed_lines.append('变更：%s %s %s' % (e['date'], e['start'][11:16], e['course']))
                log('  updated %s' % k)
            else:
                status['errors'].append('update %s: %s' % (k, json.dumps(res, ensure_ascii=False)[:300]))

        for k in deletes:
            res = lark(['calendar', 'events', 'delete', '--calendar-id', calendar_id,
                        '--event-id', prev[k]['event_id'], '--as', 'user', '--format', 'json'])
            if res.get('ok'):
                changed_lines.append('删除：%s %s' % (prev[k].get('summary'), prev[k].get('start', '')[:10]))
                del prev[k]
                status['changes']['delete'] += 1
                log('  deleted %s' % k)
            else:
                status['errors'].append('delete %s: %s' % (k, json.dumps(res, ensure_ascii=False)[:300]))

        state['events'] = prev
        save_state(state)

        if '--no-artifacts' not in args:
            ok_art, detail = run_artifacts(cfg, norm_path)
            log('交付物生成：%s' % ('OK' if ok_art else 'FAIL ' + detail[:300]))
            status['artifacts'] = ok_art
            if not ok_art:
                status['errors'].append('artifacts: ' + detail[:300])

        status['ok'] = not status['errors']
        status['finishedAt'] = time.strftime('%Y-%m-%dT%H:%M:%S')
        save_status(status)

        total = sum(status['changes'].values())
        if '--no-notify' not in args and cfg.get('notifyUserId') and not str(cfg['notifyUserId']).startswith('ou_xxx'):
            if total:
                head = '课表同步：新增 %d / 变更 %d / 删除 %d' % (
                    status['changes']['create'], status['changes']['update'], status['changes']['delete'])
                notify(cfg, head + '\n' + '\n'.join(changed_lines[:10]))
            elif status['errors']:
                notify(cfg, '课表同步有错误：\n' + '\n'.join(status['errors'][:5]))
        log('同步完成：%s' % json.dumps(status['changes'], ensure_ascii=False))
        return 0 if status['ok'] else 1
    except Exception as e:
        status['errors'].append('%s: %s' % (type(e).__name__, e))
        status['trace'] = traceback.format_exc()[-1200:]
        status['finishedAt'] = time.strftime('%Y-%m-%dT%H:%M:%S')
        save_status(status)
        log('失败：%s' % e)
        if '--no-notify' not in args and not dry:
            today = time.strftime('%Y-%m-%d')
            st = {}
            if os.path.exists(STATE):
                try:
                    st = json.load(open(STATE, encoding='utf-8'))
                except Exception:
                    st = {}
            if st.get('lastFailureNotify') != today:
                hint = ''
                if '教务登录' in str(e):
                    hint = '\n双击自动化目录的「重新登录课表账号.cmd」登录一次即可。'
                notify(cfg, '课表自动同步失败（%s）：%s%s' % (today, e, hint))
                st['lastFailureNotify'] = today
                try:
                    save_state(st)
                except Exception:
                    pass
        return 1


if __name__ == '__main__':
    sys.exit(main())
