"""
真实"分区块"缓存格式 (src.deserializer.parse_media_parts) 自测。

这个格式是 2026-09-15 用本机真实 tdata 取证得到的:
    plaintext = [u32 part_count] + ([u32 out_offset][u32 size][data]) * part_count
                上面这一整组可以重复若干次

旧实现假定的布局是错的, 后果是 mdat 负载被拼错 —— "恢复的视频看着能打开、
时长也对, 但播到一半就崩"。本文件把真实布局钉死, 防止再退化。

运行: python tests/test_media_parts.py
"""
import os
import sys
import struct
import subprocess

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

from src.deserializer import (  # noqa: E402
    MediaPart, parse_media_parts, media_part_extents,
)
from src import rebuild as RB                                    # noqa: E402
from src.exporter import _get_ffmpeg_path, _get_ffprobe_path     # noqa: E402

FIX = os.path.join('.temp', 'fixtures')
TMP = os.path.join('.temp', 'mediaparts')
PASS, FAIL = [], []


def check(name, cond, detail=''):
    (PASS if cond else FAIL).append(name)
    print(('  PASS  ' if cond else '  FAIL  ') + name + (('   ' + str(detail)) if detail else ''))


os.makedirs(TMP, exist_ok=True)


# ==================== 构造真实布局 ====================

def build_cache_file(groups, part_size=131072, trailer=b'\x00\x00'):
    """groups: [[(out_offset, data), ...], ...] -> 序列化成缓存明文"""
    out = bytearray()
    for parts in groups:
        out += struct.pack('<I', len(parts))
        for off, data in parts:
            out += struct.pack('<II', off, len(data))
            out += data
    out += trailer
    return bytes(out)


def split_into_parts(media: bytes, part_size: int):
    return [(i * part_size, media[i * part_size:(i + 1) * part_size])
            for i in range((len(media) + part_size - 1) // part_size)]


print('\n[1] 基本解析与往返')
media = b''.join(bytes([(i * 7 + j) % 256 for j in range(131072)])
                 for i in range(4))
parts_spec = [[(0, media[0:131072]), (131072, media[131072:262144])],
              [(262144, media[262144:393216]), (393216, media[393216:])]]
blob = build_cache_file(parts_spec)
got = parse_media_parts(blob)
check('解析出 4 块', bool(got) and len(got) == 4, got and len(got))
if got:
    check('块按顺序排列', [p.out_offset for p in got] == [0, 131072, 262144, 393216])
    ok = all(blob[p.in_offset:p.in_offset + p.size]
             == media[p.out_offset:p.out_offset + p.size] for p in got)
    check('每块内容与目标偏移一致', ok)
    check('覆盖完整媒体', max(p.out_end for p in got) == len(media))

print('\n[2] 非连续块 (Telegram 先取头再取尾) —— 不能假设块是连续的')
head = media[0:131072]
tail = media[393216:]
blob = build_cache_file([[(0, head), (393216, tail)]])
got = parse_media_parts(blob)
check('解析出 2 块', bool(got) and len(got) == 2)
if got:
    check('偏移正确保留 (0 与 393216)', [p.out_offset for p in got] == [0, 393216])
    exts = media_part_extents(blob)
    check('区段数为 2', len(exts) == 2)
    check('区段数据正确', exts[0][1] == head and exts[1][1] == tail)

print('\n[3] 拒绝不是这个格式的数据')
check('随机字节返回 None', parse_media_parts(os.urandom(4096)) is None)
check('过短返回 None', parse_media_parts(b'\x01\x02\x03') is None)
# count 大到不可能 -> 不能误判
bad = struct.pack('<I', 0xFFFFFF) + b'\x00' * 64
check('异常 part_count 返回 None', parse_media_parts(bad) is None)
# 声明 size 超出文件 -> 不能被接受
bad2 = struct.pack('<II', 1, 1 << 30) + b'\x00' * 32
check('size 越界返回 None', parse_media_parts(bad2) is None)

print('\n[4] 端到端: 真实布局 + 完整重打包链路')
SRC = os.path.join(FIX, 'big_moov_end.mp4')
if os.path.exists(SRC):
    raw = open(SRC, 'rb').read()
    PS = 131072
    all_parts = split_into_parts(raw, PS)
    # 只保留前 24 块 + 最后 2 块 (moov 在末尾, 所以尾部必须有)
    kept = all_parts[:24] + all_parts[-2:]
    blob = build_cache_file([kept])
    exts = media_part_extents(blob)
    check('解析出 26 个区段', len(exts) == 26, len(exts))

    a = RB.analyze_extents(exts, (), None, want_duration=True)
    check('moov 可定位', a.moov_present is True, a.error)
    check('轨道数正确', a.track_count >= 1, a.track_count)
    check('判定为不完整', a.complete is False)
    check('给出可播放前缀', a.playable_prefix_bytes > 0 and a.playable_duration_s > 0,
          f'{a.playable_prefix_bytes}B/{a.playable_duration_s:.2f}s')

    out_path = os.path.join(TMP, 'rebuilt.mp4')
    r = RB.repack_from_extents(exts, out_path)
    check('重打包成功', r.ok is True, r.reasons[:2])
    if r.ok:
        check('产物存在且非空', os.path.getsize(out_path) > 0)
        ffprobe = _get_ffprobe_path()
        ffmpeg = _get_ffmpeg_path()
        if ffprobe:
            pr = subprocess.run([ffprobe, '-v', 'error', '-print_format', 'json',
                                 '-show_format', '-i', out_path],
                                capture_output=True, timeout=120)
            import json
            try:
                dur = json.loads(pr.stdout.decode())['format']['duration']
            except Exception:
                dur = None
            check('ffprobe 能读出时长',
                  dur is not None and float(dur) > 0, dur)
        if ffmpeg:
            dec = subprocess.run([ffmpeg, '-v', 'error', '-nostdin', '-i', out_path,
                                  '-f', 'null', '-'], capture_output=True, timeout=600)
            err = dec.stderr.decode('utf-8', 'replace').strip()
            check('ffmpeg 完整解码无错误', dec.returncode == 0 and not err,
                  err.splitlines()[:2])
else:
    print('   (跳过: 缺少夹具 big_moov_end.mp4)')

print('\n[5] 关键回归: 旧的错误布局不能把真实数据解析成噪声')
# 真实数据里 part0 的 size 恒为 131072; 旧实现会把前 4 字节当 part_count 后
# 继续把媒体负载当帧头, 得到的 out_offset 是随机字节 —— 这里确保新实现
# 拿到的偏移全部是 128KB 对齐的合理值
if os.path.exists(SRC):
    got = parse_media_parts(blob)
    aligned = all(p.out_offset % PS == 0 for p in got)
    check('所有 out_offset 都按 128KB 对齐', aligned,
          [p.out_offset for p in got[:4]])
    sizes_ok = all(0 < p.size <= PS for p in got)
    check('所有块大小 <= 128KB', sizes_ok)

print(f'\n===== {len(PASS)} passed, {len(FAIL)} failed =====')
if FAIL:
    for f in FAIL:
        print('  FAILED:', f)
    sys.exit(1)
