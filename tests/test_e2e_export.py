"""
端到端导出管线自测 —— 走的是 server.py 将要使用的同一条 produce() 流程。

覆盖两条真实路径:
  A. 大视频: header + 8MB 分片 -> rebuild -> remux -> finalize
  B. 普通序列化视频: 序列化 map -> repack_from_extents -> remux -> finalize

并验证: 残缺视频输出局部可播片段、完整视频忠实复制、失败不留产物、sidecar 失效。

运行: python .temp/e2e_export_selftest.py
"""

import os
import shutil
import struct
import subprocess
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)          # 夹具与 ffmpeg 路径都相对项目根

from src.coverage import K_IN_SLICE  # noqa: E402
from src.export_pipeline import (  # noqa: E402
    is_current, part_path, produce, read_sidecar, sidecar_path, staging_path,
)
from src.mp4 import Reader, iter_boxes, parse_moov, validate  # noqa: E402
from src.rebuild import (  # noqa: E402
    SliceSource, rebuild_large_video_to_file, repack_from_extents,
    serialized_extents,
)

FFMPEG = os.path.join('dist_windows', 'env', 'ffmpeg.exe')
FFPROBE = os.path.join('dist_windows', 'env', 'ffprobe.exe')
FIX = os.path.join('.temp', 'fixtures')
OUT = os.path.join('.temp', 'e2e')
K_PART_MAX = 128 * 1024
PASS, FAIL = [], []


def check(name, cond, detail=''):
    (PASS if cond else FAIL).append(name)
    print(('  PASS  ' if cond else '  FAIL  ') + name + (('   ' + str(detail)) if detail else ''))


def ffprobe_duration(path):
    import json
    try:
        out = subprocess.run([FFPROBE, '-v', 'quiet', '-print_format', 'json',
                              '-show_format', path], capture_output=True, timeout=60)
        return float(json.loads(out.stdout.decode('utf-8', 'replace'))['format']['duration'])
    except Exception:
        return None


def decode_ok(path):
    r = subprocess.run([FFMPEG, '-v', 'error', '-i', path, '-f', 'null', '-'],
                       capture_output=True, timeout=600)
    return r.returncode == 0, r.stderr.decode('utf-8', 'replace').strip()


def build_header(parts):
    body = bytearray(struct.pack('<I', len(parts)))
    for off, data in parts:
        body += struct.pack('<I', off) + struct.pack('<I', len(data)) + data
    return bytes(body)


def build_serialized(parts):
    """按 _parse_serialized_data 的实际读法: [part_count + parts]*N"""
    body = bytearray()
    for i in range(0, len(parts), 79):
        g = parts[i:i + 79]
        body += struct.pack('<I', len(g))
        for off, d in g:
            body += struct.pack('<I', off) + struct.pack('<I', len(d)) + d
    return bytes(body)


shutil.rmtree(OUT, ignore_errors=True)
os.makedirs(OUT, exist_ok=True)

BIG = os.path.join(FIX, 'big_moov_end.mp4')
if not os.path.exists(BIG):
    print('!! 先运行 tests/test_rebuild.py 生成大样本')
    sys.exit(1)

raw = open(BIG, 'rb').read()
r = Reader(BIG)
moov_box = next(b for b in iter_boxes(r, 0, r.size()) if b.type == b'moov')
moov = parse_moov(r)
real_end = moov.max_chunk_end()
full_dur = ffprobe_duration(BIG)
r.close()


# ==================== A. 大视频: 残缺 -> 局部可播片段 ====================

print('\n[A] 大视频 (前缀 + 尾部 moov, 缺中间分片) -> 导出局部可播片段')

header = build_header([(0, raw[:K_PART_MAX])]
                      + [(moov_box.start + i, raw[moov_box.start + i:moov_box.start + i + K_PART_MAX])
                         for i in range(0, moov_box.size, K_PART_MAX)])

slices = []
for i in range(2):                       # 只有分片 0,1 -> 必然残缺
    lo = i * K_IN_SLICE
    slices.append(SliceSource(index=i, size=K_IN_SLICE, name=f'slice{i}',
                              data=raw[lo:lo + K_IN_SLICE]))

final_a = os.path.join(OUT, 'ABCDEF.mp4')
out_a = produce(final_a, lambda p: rebuild_large_video_to_file(header, slices, p),
                key_high=0x1234, need_space=32 << 20)
print(f'  ok={out_a.ok} size={out_a.size} dur={out_a.duration_s:.2f} truncated={out_a.truncated}')
print(f'  reasons={out_a.reasons}')
check('A: 导出成功', out_a.ok, out_a.reasons)
check('A: 判定为局部片段', out_a.truncated)
check('A: 时长明显短于完整时长', out_a.duration_s < full_dur * 0.95,
      f'{out_a.duration_s:.2f} vs {full_dur:.2f}')
check('A: 最终文件存在', os.path.exists(final_a))
check('A: 中间文件已清理',
      not os.path.exists(part_path(final_a)) and not os.path.exists(staging_path(final_a)))
check('A: 写入 sidecar', os.path.exists(sidecar_path(final_a)))

if out_a.ok:
    v = validate(final_a)
    check('A: 产物 validate ok', v.ok, v.reasons[:3])
    check('A: 产物 moov 已前置 (faststart)',
          v.ok and (next(b for b in iter_boxes(Reader(final_a), 0, os.path.getsize(final_a))
                         if b.type in (b'moov', b'mdat')).type == b'moov'),
          '第一个数据 box')
    ok, err = decode_ok(final_a)
    check('A: ffmpeg 完整解码', ok, err[:300])
    d = ffprobe_duration(final_a)
    check('A: ffprobe 时长 ≈ 实际可播时长',
          d is not None and abs(d - out_a.duration_s) < 0.3,
          f'ffprobe={d} computed={out_a.duration_s:.3f}')
    sc = read_sidecar(final_a)
    check('A: sidecar 记录 truncated', bool(sc) and sc.truncated is True)
    check('A: sidecar 记录 key_high', bool(sc) and sc.key_high == 0x1234)
    check('A: is_current 为真', is_current(final_a))


# ==================== B. 普通序列化视频 ====================

print('\n[B] 普通序列化视频 (前缀 + 尾部 moov) -> repack_from_extents -> 导出')

parts = [(0, raw[:K_PART_MAX])]
for i in range(0, moov_box.size, K_PART_MAX):
    off = moov_box.start + i
    parts.append((off, raw[off:off + K_PART_MAX]))
serialized = build_serialized(parts)
extents = serialized_extents(serialized)
check('B: 解析出区段', len(extents) >= 2, f'{len(extents)} 段')

final_b = os.path.join(OUT, 'FEEDFACE.mp4')
out_b = produce(final_b, lambda p: repack_from_extents(extents, p),
                slice_indices=[], need_space=32 << 20)
print(f'  ok={out_b.ok} size={out_b.size} dur={out_b.duration_s:.2f} truncated={out_b.truncated}')
print(f'  reasons={out_b.reasons}')
check('B: 导出成功', out_b.ok, out_b.reasons)
check('B: 最终文件存在', os.path.exists(final_b))
if out_b.ok:
    v = validate(final_b)
    check('B: 产物 validate ok', v.ok, v.reasons[:3])
    ok, err = decode_ok(final_b)
    check('B: ffmpeg 完整解码', ok, err[:300])


# ==================== C. 完整覆盖 -> 忠实复制且时长不变 ====================

print('\n[C] 全部分片齐全 -> 时长等于完整时长')

all_slices = []
for i in range((len(raw) + K_IN_SLICE - 1) // K_IN_SLICE):
    lo = i * K_IN_SLICE
    all_slices.append(SliceSource(index=i, size=min(K_IN_SLICE, len(raw) - lo),
                                  name=f'slice{i}', data=raw[lo:lo + K_IN_SLICE]))

final_c = os.path.join(OUT, '00C0FFEE.mp4')
out_c = produce(final_c, lambda p: rebuild_large_video_to_file(header, all_slices, p),
                need_space=64 << 20)
print(f'  ok={out_c.ok} dur={out_c.duration_s:.2f} truncated={out_c.truncated} '
      f'reasons={out_c.reasons}')
check('C: 导出成功', out_c.ok, out_c.reasons)
check('C: 未截断', not out_c.truncated)
check('C: 时长等于完整时长', abs(out_c.duration_s - full_dur) < 0.1,
      f'{out_c.duration_s:.3f} vs {full_dur:.3f}')
if out_c.ok:
    ok, err = decode_ok(final_c)
    check('C: ffmpeg 完整解码', ok, err[:300])


# ==================== D. 失败路径: 不留最终文件, 不破坏已有好文件 ====================

print('\n[D] 缺 moov -> 失败且不得留下/破坏产物')

final_d = os.path.join(OUT, 'BADBAD00.mp4')
# 先放一个"已有的好文件" (用 A 的产物冒充)
shutil.copyfile(final_a, final_d)
before = os.path.getsize(final_d)

header_no_moov = build_header([(0, raw[:K_PART_MAX])])
out_d = produce(final_d, lambda p: rebuild_large_video_to_file(header_no_moov, slices, p))
print(f'  ok={out_d.ok} reasons={out_d.reasons}')
check('D: 判定失败', not out_d.ok)
check('D: 失败原因提到 moov', any('moov' in x for x in out_d.reasons), out_d.reasons[:2])
check('D: 既有好文件未被破坏', os.path.exists(final_d) and os.path.getsize(final_d) == before)
check('D: 未留下中间文件',
      not os.path.exists(part_path(final_d)) and not os.path.exists(staging_path(final_d)))
check('D: 失败没有为该文件写入 sidecar', not os.path.exists(sidecar_path(final_d)))
check('D: 失败后 is_current 仍为假 (不会把失败当成功)', not is_current(final_d))

print('\n[E] 磁盘空间不足 -> 提前失败, 不开始写')

final_e = os.path.join(OUT, '0BADF00D.mp4')
out_e = produce(final_e, lambda p: rebuild_large_video_to_file(header, all_slices, p),
                need_space=1 << 60)
print(f'  ok={out_e.ok} reasons={out_e.reasons}')
check('E: 判定失败', not out_e.ok)
check('E: 原因提到磁盘空间', any('磁盘' in x for x in out_e.reasons), out_e.reasons[:2])
check('E: 未产生任何文件',
      not os.path.exists(final_e) and not os.path.exists(staging_path(final_e)))


print(f'\n===== {len(PASS)} passed, {len(FAIL)} failed =====')
if FAIL:
    for f in FAIL:
        print('  FAILED:', f)
    sys.exit(1)
