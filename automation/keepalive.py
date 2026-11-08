# -*- coding: utf-8 -*-
"""每 10 分钟给教务会话续命；登录失效时每天最多提醒一次。

由 Windows 计划任务（见 install_tasks.ps1）调用，静默运行。
"""
import json
import os
import subprocess
import sys
import time

import cdp_fetch

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, 'keepalive.log')
STATUS = os.path.join(HERE, 'keepalive_status.json')
FLAG = os.path.join(HERE, 'login-needed.flag')
FLAG_PAUSE_MIN = 15
NOTIFY_DAYS = 1


def log(msg):
    line = '%s  %s' % (time.strftime('%Y-%m-%d %H:%M:%S'), msg)
    print(line, flush=True)
    try:
        with open(LOG, 'a', encoding='utf-8') as fh:
            fh.write(line + '\n')
    except Exception:
        pass


def load(path, default):
    try:
        with open(path, encoding='utf-8') as fh:
            return json.load(fh)
    except Exception:
        return default


def save(path, data):
    try:
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
    except Exception:
        pass


def notify(text):
    """有配 notifyUserId 就用飞书 bot 私聊提醒；没有就只写日志。返回是否真的发出。"""
    cfg = cdp_fetch.CFG
    uid = cfg.get('notifyUserId')
    if not uid or uid.startswith('ou_xxx'):
        return False
    import shutil
    exe = shutil.which('lark-cli') or shutil.which('lark-cli.cmd')
    if exe:
        cmd = [exe]
    else:
        cmd = [cdp_fetch._node(), os.path.join(os.environ.get('APPDATA', ''),
               'npm', 'node_modules', '@larksuite', 'cli', 'scripts', 'run.js')]
    try:
        proc = subprocess.run(cmd + ['im', '+messages-send', '--as', 'bot', '--user-id', uid,
                                     '--text', text], capture_output=True, text=True,
                              encoding='utf-8', errors='replace', timeout=60)
    except Exception as e:
        log('通知失败：%s' % e)
        return False
    if proc.returncode != 0:
        log('通知失败（exit=%s）：%s' % (proc.returncode, (proc.stderr or '')[-200:]))
        return False
    return True


def flag_paused():
    f = load(FLAG, None)
    if not f:
        return False
    try:
        return (time.time() - float(f.get('ts', 0))) / 60 < FLAG_PAUSE_MIN
    except Exception:
        return False


def main():
    status = load(STATUS, {'notifiedOn': None})
    if flag_paused():
        log('用户正在重新登录（flag 未过期），本轮跳过')
        return 0
    ok, payload = cdp_fetch.fetch()
    if not ok and payload == cdp_fetch.LOGIN_IN_PROGRESS:
        log('用户正在重新登录，本轮跳过')
        return 0
    if ok:
        if os.path.exists(FLAG):
            try:
                os.remove(FLAG)
            except Exception:
                pass
        status.update({'ok': True, 'at': time.strftime('%Y-%m-%dT%H:%M:%S'), 'records': len(payload)})
        save(STATUS, status)
        log('续命成功：%d 条' % len(payload))
        return 0

    if payload == cdp_fetch.LOGIN_EXPIRED:
        cdp_fetch.stop_instance()
        today = time.strftime('%Y-%m-%d')
        save(FLAG, {'ts': time.time(), 'reason': 'login-expired', 'at': today})
        status.update({'ok': False, 'loginExpired': True, 'at': time.strftime('%Y-%m-%dT%H:%M:%S')})
        if status.get('notifiedOn') != today:
            sent = notify('教务登录已过期，课表同步已暂停。\n'
                          '请双击运行自动化目录下的「重新登录课表账号.cmd」，'
                          '用统一认证登录并勾选「7 天内无需再次登录」，登录后关闭窗口即可。')
            if sent:
                status['notifiedOn'] = today
                log('已通知用户重新登录')
            else:
                log('通知未发出，下一轮会重试')
        save(STATUS, status)
        log('登录过期，已释放自动化 Edge 让用户登录')
        return 1

    status.update({'ok': False, 'error': str(payload)[:300], 'at': time.strftime('%Y-%m-%dT%H:%M:%S')})
    save(STATUS, status)
    log('续命失败：%s' % str(payload)[:300])
    return 1


if __name__ == '__main__':
    sys.exit(main())
