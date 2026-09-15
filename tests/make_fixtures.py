"""
生成测试夹具 (合成 MP4)。

不需要真实的 Telegram 数据 —— 用项目自带的 ffmpeg 造基准片段, 再由各个测试
自行派生出病态样本 (尾部截断 / moov 在尾 / 前缀+尾部 moov / moov 缺失 等)。

用法 (在项目根目录):
    python tests/make_fixtures.py

产物落在 .temp/fixtures/ 下 (不随包发布)。
"""

import os
import subprocess
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

FFMPEG = os.path.join('dist_windows', 'env', 'ffmpeg.exe')
FIX = os.path.join('.temp', 'fixtures')


def _run(args):
    r = subprocess.run(args, capture_output=True)
    if r.returncode != 0:
        print(r.stderr.decode('utf-8', 'replace')[:800])
        raise SystemExit(f'ffmpeg 失败: {" ".join(args[:6])}...')


def main():
    if not os.path.exists(FFMPEG):
        raise SystemExit(f'未找到 ffmpeg: {FFMPEG} (打包版位于 dist_windows/env/)')
    os.makedirs(FIX, exist_ok=True)

    base = [FFMPEG, '-hide_banner', '-loglevel', 'error', '-y']
    small_v = ['-f', 'lavfi', '-i', 'testsrc2=size=320x240:rate=25:duration=4']
    small_a = ['-f', 'lavfi', '-i', 'sine=frequency=440:duration=4']
    small_enc = ['-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
                 '-g', '25', '-c:a', 'aac', '-b:a', '64k']

    print('[1/2] 小片段 (4s, 约 400KB)')
    _run(base + small_v + small_a + small_enc +
         ['-movflags', '+faststart', os.path.join(FIX, 'faststart.mp4')])
    _run(base + small_v + small_a + small_enc + [os.path.join(FIX, 'moov_end.mp4')])
    _run(base + small_v + ['-c:v', 'libx264', '-preset', 'ultrafast',
                           '-pix_fmt', 'yuv420p', '-g', '25',
                           '-movflags', '+frag_keyframe+empty_moov+default_base_moof',
                           os.path.join(FIX, 'fragmented.mp4')])

    print('[2/2] 大片段 (30s, 约 20MB, 用于跨 8MB 分片的重建用例)')
    big_v = ['-f', 'lavfi', '-i', 'testsrc2=size=640x480:rate=25:duration=30']
    big_a = ['-f', 'lavfi', '-i', 'sine=frequency=440:duration=30']
    big_enc = ['-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
               '-g', '50', '-b:v', '8M', '-c:a', 'aac', '-b:a', '128k']
    _run(base + big_v + big_a + big_enc + [os.path.join(FIX, 'big_moov_end.mp4')])
    _run(base + big_v + big_a + big_enc +
         ['-movflags', '+faststart', os.path.join(FIX, 'big_faststart.mp4')])

    print('\n夹具已生成:')
    for name in sorted(os.listdir(FIX)):
        p = os.path.join(FIX, name)
        print(f'  {name:26s} {os.path.getsize(p):>10,} bytes')


if __name__ == '__main__':
    main()
