# -*- coding: utf-8 -*-
"""
Telegram 跳转候选验证脚本（交互式）
==================================

运行目录: D:\\Agent\\Project\\TG_data_manager
运行命令: E:/A-Environment/Miniconda3/python.exe verify_tg_jump.py

背景:
  上一版脚本用 subprocess 参数列表调 cmd /c start, Python 只对含空格的参数加引号,
  于是 tg:// 里的 & 被 cmd 当成命令分隔符, 实际只执行了
  "tg://resolve?domain=feihsl" -> 只进频道不定位消息。
  本版一律手工给 URL 加双引号, 并用 shell=True 保证经过 cmd 解析。

待验证的核心问题:
  原链接 https://t.me/feihsl/25158?single&t=4 里 t=4 = 相册内第 4 个媒体。
  tg:// 能否表达"相册内第 N 个"? 逐个候选试, 由你肉眼判定。

判定标准:
  Telegram 跳到 feihsl 频道第 25158 条消息, 并且**显示的是相册里第 4 个视频**。
"""

import json
import os
import subprocess
import sys

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

ROOT = os.path.dirname(os.path.abspath(__file__))
LINKS_PATH = os.path.join(ROOT, 'telegram_links.json')

BASE_POST = 'tg://resolve?domain=feihsl&post=25158'
HTTPS_SRC = 'https://t.me/feihsl/25158?single&t=4'

CANDIDATES = [
    ('当前应用内使用的写法（只到消息级）', BASE_POST),
    ('tg:// 加 t=4（试能否定位相册内第 4 个）', BASE_POST + '&t=4'),
    ('tg:// 加 single + t=4', BASE_POST + '&single&t=4'),
    ('tg:// 用 media 参数写法', BASE_POST + '&single&media=4'),
    ('原始 https 链接（走浏览器 / 网页版）', HTTPS_SRC),
]

ANSWER_MAP = {
    'y': '正确定位到第 4 个视频',
    'm': '只跳到消息或频道，不是第 4 个视频',
    'n': '没反应 / 报错',
}


def open_url(url):
    """经 cmd start 打开，URL 手工加引号，避免 & 被当成命令分隔符"""
    cmd = 'start "" "%s"' % url
    return subprocess.Popen(cmd, shell=True)


def main():
    print('=' * 64)
    print('Telegram 跳转候选验证')
    print('=' * 64)

    if os.path.exists(LINKS_PATH):
        try:
            with open(LINKS_PATH, 'r', encoding='utf-8') as fh:
                bindings = json.load(fh)
            print('\n当前绑定:')
            for k, v in bindings.items():
                print('  %-22s tg_url=%s' % (k, v.get('tg_url', '')))
                if v.get('source_url'):
                    print('  %-22s source=%s' % ('', v['source_url']))
        except Exception as e:
            print('  绑定文件读取失败: %r' % (e,))
    else:
        print('\n未找到 telegram_links.json，使用内置候选')

    print('\n会依次打开 %d 个候选。每个打开后请看 Telegram 窗口：' % len(CANDIDATES))
    print('  跳到的是不是 feihsl 频道 25158 那条消息里的【第 4 个视频】？')
    print()
    input('准备好了按回车开始（先确认 Telegram 窗口看得见）...')

    results = []
    for idx, (desc, url) in enumerate(CANDIDATES, 1):
        print('\n' + '-' * 64)
        print('候选 %d/%d: %s' % (idx, len(CANDIDATES), desc))
        print('  URL: %s' % url)
        try:
            choice = input('  回车=打开它, s=跳过, q=退出 > ').strip().lower()
        except EOFError:
            break
        if choice == 'q':
            print('  已退出')
            break
        if choice == 's':
            results.append((desc, url, '已跳过'))
            continue

        open_url(url)
        while True:
            ans = input('  结果? y=正确定位到第4个视频 / m=只到消息或频道 / n=没反应 > ').strip().lower()
            if ans in ANSWER_MAP:
                results.append((desc, url, ANSWER_MAP[ans]))
                break
            print('  只接受 y / m / n')

    print('\n' + '=' * 64)
    print('汇总')
    print('=' * 64)
    for desc, url, res in results:
        flag = ' <== 可用' if res.startswith('正确') else ''
        print('  [%s] %s' % (res, desc))
        print('        %s%s' % (url, flag))

    print('\n把上面这段汇总贴给我，我会据此改 server.py 的链接构造，')
    print('让「前往 Telegram 播放」直接定位到相册里对应的那一个视频。')
    print()


if __name__ == '__main__':
    main()
