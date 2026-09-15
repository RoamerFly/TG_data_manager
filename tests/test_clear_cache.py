# -*- coding: utf-8 -*-
"""
验证 /api/clear_cache 的三个目标与安全红线。

用 Flask test_client 在进程内跑, 不启动真实服务, 也不需要真实 Telegram 数据。
关键: 用一份**合成 tdata** 做真实删除, 确认
  - 只删 user_data/cache 与 user_data/media_cache 下的缓存文件
  - version / binlog 保留
  - key_datas 与 D877F783D5D3EF8C (账号数据) 一个不少
"""
import os
import shutil
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

import server  # noqa: E402
from server import Config, app  # noqa: E402

ROOT = os.path.join(PROJECT_ROOT, '.temp', 'clear_test')
TDATA = os.path.join(ROOT, 'tdata')
DL = os.path.join(ROOT, 'downloads')

fails = []


def check(cond, msg):
    if cond:
        print('  [OK] ' + msg)
    else:
        print('  [FAIL] ' + msg)
        fails.append(msg)


def w(path, data=b'x'):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(data)
    return path


def build_fixture():
    if os.path.isdir(ROOT):
        shutil.rmtree(ROOT, ignore_errors=True)
    os.makedirs(ROOT, exist_ok=True)
    # --- 用户数据 (绝不能被删) ---
    w(os.path.join(TDATA, 'key_datas'), b'KEY' * 100)
    w(os.path.join(TDATA, 'settingss'), b'settings')
    w(os.path.join(TDATA, 'D877F783D5D3EF8C', 'config'), b'account-config')
    w(os.path.join(TDATA, 'D877F783D5D3EF8C', 'settings'), b'account-settings')
    w(os.path.join(TDATA, 'D877F783D5D3EF8C', 'binlog'), b'binlog')
    w(os.path.join(TDATA, 'emoji', 'a.tgs'), b'emoji')
    # --- 缓存 (应该被删) ---
    w(os.path.join(TDATA, 'user_data', 'cache', 'version'), b'1')
    w(os.path.join(TDATA, 'user_data', 'cache', '1', 'AAA.bin'), b'A' * 1000)
    w(os.path.join(TDATA, 'user_data', 'cache', '1', 'BBB.bin'), b'B' * 2000)
    w(os.path.join(TDATA, 'user_data', 'media_cache', 'version'), b'1')
    w(os.path.join(TDATA, 'user_data', 'media_cache', '1', 'CCC.bin'), b'C' * 3000)
    w(os.path.join(TDATA, 'user_data', 'media_cache', '1', 'binlog'), b'bl')
    # --- 下载目录 ---
    w(os.path.join(DL, 'sub', 'photo.jpg'), b'P' * 500)
    w(os.path.join(DL, 'doc.pdf'), b'D' * 700)


def post(client, **body):
    return client.post('/api/clear_cache', json=body)


def main():
    build_fixture()
    Config.tdata_path = TDATA
    Config.download_path = DL
    client = app.test_client()

    print('== 1. 预演 telegram (confirm=false, 不应删除任何文件) ==')
    r = post(client, target='telegram', confirm=False)
    d = r.get_json()
    check(r.status_code == 200, '预演返回 200')
    check(d.get('file_count') == 3, f"文件数 3 (实际 {d.get('file_count')})")
    check(d.get('total_bytes') == 6000, f"字节 6000 (实际 {d.get('total_bytes')})")
    check(d.get('confirm_required') is True, '带 confirm_required 标记')
    check(os.path.exists(os.path.join(TDATA, 'user_data', 'cache', '1', 'AAA.bin')),
          '预演阶段缓存文件仍在')

    print('== 2. 执行 telegram (confirm=true) ==')
    r = post(client, target='telegram', confirm=True)
    d = r.get_json()
    check(d.get('deleted') == 3, f"删除 3 个 (实际 {d.get('deleted')})")
    check(d.get('freed_bytes') == 6000, '释放 6000 字节')
    check(d.get('need_rescan') is True, 'need_rescan=True (提示重新扫描)')

    print('== 3. 用户数据完整性 ==')
    for rel in ['key_datas', 'settingss',
                os.path.join('D877F783D5D3EF8C', 'config'),
                os.path.join('D877F783D5D3EF8C', 'settings'),
                os.path.join('D877F783D5D3EF8C', 'binlog'),
                os.path.join('emoji', 'a.tgs'),
                os.path.join('user_data', 'cache', 'version'),
                os.path.join('user_data', 'media_cache', 'version'),
                os.path.join('user_data', 'media_cache', '1', 'binlog')]:
        check(os.path.exists(os.path.join(TDATA, rel)), f'保留: {rel}')
    for rel in [os.path.join('user_data', 'cache', '1', 'AAA.bin'),
                os.path.join('user_data', 'cache', '1', 'BBB.bin'),
                os.path.join('user_data', 'media_cache', '1', 'CCC.bin')]:
        check(not os.path.exists(os.path.join(TDATA, rel)), f'已删: {rel}')
    check(os.path.isdir(os.path.join(TDATA, 'user_data', 'cache', '1')),
          'cache/1 目录结构保留 (Telegram 需要)')

    print('== 4. 清空下载目录 ==')
    r = post(client, target='download', confirm=True)
    d = r.get_json()
    check(d.get('deleted') == 2, f"删除 2 个 (实际 {d.get('deleted')})")
    check(not os.path.exists(os.path.join(DL, 'doc.pdf')), 'doc.pdf 已删')
    check(not os.path.exists(os.path.join(DL, 'sub', 'photo.jpg')), 'sub/photo.jpg 已删')
    check(os.path.isdir(DL), '下载目录根保留')

    print('== 5. 安全红线 ==')
    Config.download_path = TDATA                       # 下载目录 == tdata
    r = post(client, target='download', confirm=True)
    check(r.status_code == 400, '下载目录 = tdata → 400')
    check(os.path.exists(os.path.join(TDATA, 'key_datas')), '被拒后 key_datas 仍在')

    Config.download_path = os.path.dirname(TDATA)      # 下载目录包含 tdata
    r = post(client, target='download', confirm=True)
    check(r.status_code == 400, '下载目录包含 tdata → 400')
    check(os.path.exists(os.path.join(TDATA, 'key_datas')), '被拒后 key_datas 仍在')

    Config.download_path = os.path.expanduser('~')
    r = post(client, target='download', confirm=True)
    check(r.status_code == 400, '下载目录 = 用户主目录 → 400')

    Config.download_path = os.path.join(os.path.expanduser('~'), 'Desktop')
    r = post(client, target='download', confirm=True)
    check(r.status_code == 400, '下载目录 = 桌面 → 400')

    Config.download_path = ''
    r = post(client, target='download', confirm=True)
    check(r.status_code == 400, '下载目录未配置 → 400')

    r = post(client, target='no_such_target', confirm=False)
    check(r.status_code == 400, '未知 target → 400')

    print('== 6. app 目标预演 (不实际删除, 避免清掉真实缩略图) ==')
    r = post(client, target='app', confirm=False)
    d = r.get_json()
    check(r.status_code == 200, 'app 预演 200')
    check(d.get('label') == '应用缓存', 'label 正确')
    labels = {x['label'] for x in d.get('dirs', [])}
    check(labels == {'exports', 'thumbnails'}, f'dirs 只含 exports/thumbnails (实际 {labels})')

    shutil.rmtree(ROOT, ignore_errors=True)
    print()
    if fails:
        print('FAILED: %d 项' % len(fails))
        for m in fails:
            print('  - ' + m)
        return 1
    print('ALL PASSED')
    return 0


if __name__ == '__main__':
    sys.exit(main())
