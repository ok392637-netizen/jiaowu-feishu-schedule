# -*- coding: utf-8 -*-
"""用「专用 Edge profile + 常驻无头 Edge + CDP」取教务课表。

为什么要常驻而不是每次启动：教务是服务端会话，空闲超时就会掉登录。
保持一个 --headless=new 的 Edge 常驻，配合 keepalive.py 每 10 分钟发一次真实请求续命，
登录态可以一直有效；只有进程死掉/机器重启才需要重新登录。

用法：
    python cdp_fetch.py            # 自测：抓一次并打印条数
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
NODE_SCRIPT = os.path.join(HERE, 'cdp_grab.mjs')
LOG = os.path.join(HERE, 'edge-auto.log')
RESULT = os.path.join(HERE, 'cdp_result.json')
FLAG = os.path.join(HERE, 'login-needed.flag')
LOCK = os.path.join(HERE, 'cdp_fetch.lock')
LOGIN_EXPIRED = 'LOGIN_EXPIRED'
LOGIN_IN_PROGRESS = 'LOGIN_IN_PROGRESS'


def load_config():
    try:
        with open(os.path.join(HERE, 'config.json'), encoding='utf-8') as fh:
            return json.load(fh)
    except Exception:
        return {}


CFG = load_config()
PROFILE = os.environ.get('JIAOWU_PROFILE') or os.path.join(HERE, 'edge-auto')
PID_FILE = os.path.join(HERE, 'edge-auto.pid')
PORT = int(CFG.get('cdpPort') or os.environ.get('JIAOWU_CDP_PORT') or 9240)
BASE_URL = (CFG.get('baseUrl') or os.environ.get('JIAOWU_BASE_URL') or 'https://jw.example.edu.cn').rstrip('/')

EDGE_CANDIDATES = [
    os.path.join(os.environ.get('PROGRAMFILES(X86)', r'C:\Program Files (x86)'),
                 r'Microsoft\Edge\Application\msedge.exe'),
    os.path.join(os.environ.get('PROGRAMFILES', r'C:\Program Files'),
                 r'Microsoft\Edge\Application\msedge.exe'),
    os.path.join(os.environ.get('LOCALAPPDATA', ''), r'Microsoft\Edge\Application\msedge.exe'),
]


def edge_exe():
    for p in EDGE_CANDIDATES:
        if p and os.path.exists(p):
            return p
    for p in ('msedge', 'msedge.exe'):
        found = _which(p)
        if found:
            return found
    raise RuntimeError('找不到 msedge.exe，请安装 Edge 或修改 EDGE_CANDIDATES')


def _which(name):
    for d in os.environ.get('PATH', '').split(os.pathsep):
        cand = os.path.join(d, name)
        if os.path.exists(cand):
            return cand
    return None


def _node():
    return _which('node') or _which('node.exe') or 'node'


def cdp_alive(port=None, timeout=2):
    port = port or PORT
    try:
        with urllib.request.urlopen('http://127.0.0.1:%d/json/version' % port, timeout=timeout) as r:
            return bool(json.loads(r.read().decode('utf-8')).get('Browser'))
    except Exception:
        return False


def login_in_progress(max_min=15):
    """用户正在手动登录时（relogin_helper 会写 flag），不要去抢 profile。"""
    try:
        with open(FLAG, encoding='utf-8') as fh:
            f = json.load(fh)
        return (time.time() - float(f.get('ts', 0))) / 60 < max_min
    except Exception:
        return False


def _acquire_lock(max_age=300):
    """进程级锁，避免 keepalive 与 sync 同时写同一个结果文件。"""
    try:
        if time.time() - os.stat(LOCK).st_mtime < max_age:
            return False
        os.remove(LOCK)
    except FileNotFoundError:
        pass
    except Exception:
        return False
    try:
        fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        return False


def _release_lock():
    try:
        os.remove(LOCK)
    except Exception:
        pass


def _kill_pid(pid):
    try:
        subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'], capture_output=True, timeout=30)
    except Exception:
        pass


def stop_instance():
    """只杀自动化 profile 的 Edge 进程，不动用户自己的浏览器。"""
    if os.path.exists(PID_FILE):
        try:
            _kill_pid(int(open(PID_FILE).read().strip()))
        except Exception:
            pass
        try:
            os.remove(PID_FILE)
        except Exception:
            pass
    marker = os.path.basename(PROFILE)
    script = ("Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
              "Where-Object { $_.CommandLine -like '*%s*' } | "
              "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" % marker)
    try:
        subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', script],
                       capture_output=True, timeout=40)
    except Exception:
        pass
    time.sleep(1)


def start_instance(wait=25):
    if cdp_alive():
        return True
    os.makedirs(PROFILE, exist_ok=True)
    stop_instance()
    args = [edge_exe(), '--headless=new', '--remote-debugging-port=%d' % PORT,
            '--user-data-dir=%s' % PROFILE, '--profile-directory=Default',
            '--no-first-run', '--no-default-browser-check', '--disable-gpu',
            '--disable-background-networking', '--disable-sync', 'about:blank']
    logfh = open(LOG, 'a', encoding='utf-8')
    proc = subprocess.Popen(args, stdout=logfh, stderr=logfh, cwd=HERE,
                            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                            close_fds=True)
    try:
        with open(PID_FILE, 'w') as fh:
            fh.write(str(proc.pid))
    except Exception:
        pass
    deadline = time.time() + wait
    while time.time() < deadline:
        if cdp_alive():
            return True
        time.sleep(0.5)
    return False


def _fetch_once(d1=None, d2=None, timeout=180):
    """返回 (ok, data_or_error)。登录失效时 error == LOGIN_EXPIRED。"""
    d1 = d1 or CFG.get('d1') or '2026-08-01 00:00:00'
    d2 = d2 or CFG.get('d2') or '2027-03-01 00:00:00'
    if login_in_progress():
        return False, LOGIN_IN_PROGRESS
    if not os.path.isdir(PROFILE):
        return False, '没有自动化 profile：%s（先运行 relogin_helper.py 登录一次）' % PROFILE
    if not start_instance():
        return False, '无头 Edge 起不来（CDP 端口 %d 无响应）' % PORT
    result_path = os.path.join(HERE, 'cdp_result.%d.json' % os.getpid())
    try:
        subprocess.run([_node(), NODE_SCRIPT, str(PORT), result_path, d1, d2, BASE_URL],
                       capture_output=True, text=True, encoding='utf-8', errors='replace',
                       timeout=timeout, cwd=HERE)
    except subprocess.TimeoutExpired:
        return False, 'CDP 抓取超时'
    try:
        if not os.path.exists(result_path):
            return False, 'CDP 脚本没有产出结果'
        with open(result_path, encoding='utf-8') as fh:
            result = json.load(fh)
        os.remove(result_path)
    except Exception as e:
        return False, 'CDP 结果解析失败：%s' % e
    if result.get('fetchOk') and isinstance(result.get('data'), list):
        return True, result['data']
    info = str(result.get('fetchInfo') or '')
    if result.get('loginHtml') or 'DOCTYPE html' in info or '"html":true' in info.replace(' ', ''):
        return False, LOGIN_EXPIRED
    return False, '抓取失败：%s' % (info[:300] or result.get('error') or '未知')


def fetch(d1=None, d2=None, timeout=180):
    """带锁与“登录进行中”保护的入口。"""
    if login_in_progress():
        return False, LOGIN_IN_PROGRESS
    if not _acquire_lock():
        return False, '另一轮抓取进行中（lock 未释放），稍后再试'
    try:
        return _fetch_once(d1, d2, timeout)
    finally:
        _release_lock()


if __name__ == '__main__':
    t0 = time.time()
    ok, payload = fetch()
    if not ok:
        print('FAIL:', payload)
        sys.exit(1)
    by = {}
    for x in payload:
        key = x.get('lx')
        by[key] = by.get(key, 0) + 1
    print('OK records=%d types=%s elapsed=%.1fs' % (len(payload), by, time.time() - t0))
