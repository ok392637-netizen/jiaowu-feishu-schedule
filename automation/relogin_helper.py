# -*- coding: utf-8 -*-
"""打开「重新登录」窗口：先释放自动化 Edge 的 profile 锁，再启动可见 Edge 停在登录页。

建议用「统一认证登录」并勾选 rememberMe（7 天免登录），这样重启后还能自动续上。
"""
import json
import os
import subprocess
import time

import cdp_fetch

HERE = os.path.dirname(os.path.abspath(__file__))
FLAG = os.path.join(HERE, 'login-needed.flag')


def main():
    cdp_fetch.stop_instance()
    with open(FLAG, 'w', encoding='utf-8') as fh:
        json.dump({'ts': time.time(), 'reason': 'manual-relogin'}, fh)
    args = [cdp_fetch.edge_exe(),
            '--user-data-dir=%s' % cdp_fetch.PROFILE,
            '--profile-directory=Default', '--no-first-run', '--no-default-browser-check',
            '--new-window', cdp_fetch.BASE_URL + '/new/welcome.page?ui=new']
    subprocess.Popen(args, cwd=HERE)
    print('已打开登录窗口：登录成功后关闭窗口即可（keepalive 会在 15 分钟内接管续命）')


if __name__ == '__main__':
    main()
