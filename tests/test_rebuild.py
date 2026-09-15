"""
src/rebuild.py 端到端自测

用一个真实的大 MP4 合成 Telegram 的缓存形态:
  - header = 复杂序列化格式 (count + [out_offset + size + data]*N)
      * part [0, 128KB)          —— 头部
      * part [moov_start, end)   —— 尾部独立 moov 区段 (TG 为流式播放而预取)
  - 8MB 分片 = 直接从原文件对应偏移切出来

然后验证重建产物: validate / ffprobe 时长 / ffmpeg 完整解码 / faststart 重封装。

运行: python tests/test_rebuild.py
"""

import os
import struct
import subprocess
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)          # 夹具与 ffmpeg 路径都相对项目根

from src.coverage import K_IN_SLICE  # noqa: E402
from src.mp4 import Reader, iter_boxes, parse_moov, validate  # noqa: E402
from src.rebuild import SliceSource, rebuild_large_video_to_file  # noqa: E402

FFMPEG = os.path.join('dist_windows', 'env', 'ffmpeg.exe')
FFPROBE = os.path.join('dist_windows', 'env', 'ffprobe.exe')
FIX = os.path.join('.temp', 'fixtures')
OUT = os.path.join('.temp', 'out')

K_PART_MAX = 128 * 1024
PASS, FAIL = [], []


def check(name, cond, detail=''):
    (PASS if cond else FAIL).append(name)
    print(('  PASS  ' if cond else '  FAIL  ') + name + (('   ' + str(detail)) if detail else ''))


def safe_validate(path):
    """产物可能不存在 (重建失败时) —— 不要让测试脚本崩掉"""
    if not os.path.exists(path):
        return None
    return validate(path)


def guard_product(path, label):
    """产物存在才继续做后续检查"""
    exists = os.path.exists(path)
    check(f'{label}: 产物文件存在', exists)
    return exists


def ffprobe_duration(path):
    import json
    try:
        out = subprocess.run([FFPROBE, '-v', 'quiet', '-print_format', 'json',
                              '-show_format', path], capture_output=True, timeout=60)
        return float(json.loads(out.stdout.decode('utf-8', 'replace'))['format']['duration'])
    except Exception:
        return None


def decode_ok(path, extra=None):
    cmd = [FFMPEG, '-v', 'error', '-i', path]
    if extra:
        cmd += extra
    cmd += ['-f', 'null', '-']
    r = subprocess.run(cmd, capture_output=True, timeout=600)
    return r.returncode == 0, r.stderr.decode('utf-8', 'replace').strip()


def build_header(parts):
    """复杂序列化格式: count(4B LE) + [off(4B LE) + size(4B LE) + data]*count"""
    body = bytearray()
    body += struct.pack('<I', len(parts))
    for off, data in parts:
        body += struct.pack('<I', off)
        body += struct.pack('<I', len(data))
        body += data
    return bytes(body)


def split_parts(off, data, chunk=K_PART_MAX):
    """把一个区段切成 <= 256KB 的 parts (_parse_complex_format 的上限)"""
    out = []
    for i in range(0, len(data), chunk):
        out.append((off + i, data[i:i + chunk]))
    return out


# ==================== 生成大样本 ====================

os.makedirs(FIX, exist_ok=True)
os.makedirs(OUT, exist_ok=True)
BIG = os.path.join(FIX, 'big_moov_end.mp4')
BIG_FS = os.path.join(FIX, 'big_faststart.mp4')

if not os.path.exists(BIG):
    print('生成 30MB 测试视频 ...')
    common = ['-hide_banner', '-loglevel', 'error', '-y',
              '-f', 'lavfi', '-i', 'testsrc2=size=640x480:rate=25:duration=30',
              '-f', 'lavfi', '-i', 'sine=frequency=440:duration=30',
              '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
              '-g', '50', '-b:v', '8M', '-c:a', 'aac', '-b:a', '128k']
    subprocess.run([FFMPEG] + common + [BIG], check=True)
    subprocess.run([FFMPEG] + common + ['-movflags', '+faststart', BIG_FS], check=True)

print(f'  {BIG}: {os.path.getsize(BIG)} bytes')
print(f'  {BIG_FS}: {os.path.getsize(BIG_FS)} bytes')

raw = open(BIG, 'rb').read()
r = Reader(BIG)
size = r.size()
boxes = list(iter_boxes(r, 0, size))
mdat = next(b for b in boxes if b.type == b'mdat')
moov_box = next(b for b in boxes if b.type == b'moov')
moov = parse_moov(r)
full_dur = ffprobe_duration(BIG)
print(f'  mdat=[{mdat.start},{mdat.end})  moov=[{moov_box.start},{moov_box.end})  '
      f'full_dur={full_dur:.2f}s  total_size={size}')
print(f'  tracks={[(t.track_id, t.handler.decode(), len(t.chunk_offsets)) for t in moov.tracks]}')
r.close()

COVER = 20 * 1024 * 1024          # 假设下载到 20MB (分片 0,1 完整)


def make_slices(n_slices, src=raw):
    out = []
    for i in range(n_slices):
        lo = i * K_IN_SLICE
        hi = min(lo + K_IN_SLICE, len(src))
        if lo >= len(src):
            break
        out.append(SliceSource(index=i, size=hi - lo, name=f'slice{i}',
                               data=src[lo:hi]))
    return out


# ==================== 1. 形态 2: 头部前缀 + 尾部独立 moov ====================

print('\n[1] 形态 2 (前缀 + 尾部独立 moov): 只应输出前缀可播片段')

n_slices = min(2, COVER // K_IN_SLICE)
slices = make_slices(n_slices)
header = build_header(
    split_parts(0, raw[:K_PART_MAX])
    + split_parts(moov_box.start, raw[moov_box.start:moov_box.end])
)
print(f'  header={len(header)} bytes, {n_slices} 个 8MB 分片')

out1 = os.path.join(OUT, 'shape2.mp4')
res = rebuild_large_video_to_file(header, slices, out1)
print(f'  playable={res.playable} p={res.p} total={res.total_size} '
      f'truncated={res.truncated} dur={res.duration_s:.2f}s')
print(f'  keep={res.per_track_keep} samples={res.samples_kept} '
      f'missing_blocks={res.missing_blocks[:6]}')
print(f'  reasons={res.reasons}')
check('形态2: 重建成功', res.playable, res.reasons)
check('形态2: 写出前缀 <= 16MB (只含 2 个分片)',
      res.p <= 16 * 1024 * 1024, f'p={res.p}')
check('形态2: 判定为截断', res.truncated)
check('形态2: 时长明显小于完整时长', res.duration_s < full_dur * 0.9,
      f'{res.duration_s:.2f} vs {full_dur:.2f}')
check('形态2: 产物文件存在', os.path.exists(out1))

if guard_product(out1, '形态2'):
    v = validate(out1)
    check('形态2: validate ok', v.ok, v.reasons[:3])
    check('形态2: 时长与计算值一致', abs(v.duration_s - res.duration_s) < 0.1,
          f'{v.duration_s:.3f} vs {res.duration_s:.3f}')

    data = open(out1, 'rb').read()
    holes = sum(1 for i in range(0, len(data) - 8192, 4096)
                if data[i:i + 4096] == b'\x00' * 4096
                and data[i + 4096:i + 8192] == b'\x00' * 4096)
    check('形态2: 产物不含 >=8KB 零洞', holes == 0, f'holes={holes}')

    ok, err = decode_ok(out1)
    check('形态2: ffmpeg 完整解码', ok, err[:300])
    d = ffprobe_duration(out1)
    check('形态2: ffprobe 时长与计算一致',
          d is not None and abs(d - res.duration_s) < 0.2,
          f'ffprobe={d} computed={res.duration_s:.3f}')

    fs1 = os.path.join(OUT, 'shape2_faststart.mp4')
    subprocess.run([FFMPEG, '-hide_banner', '-loglevel', 'error', '-y', '-i', out1,
                    '-c', 'copy', '-movflags', '+faststart', fs1], check=False)
    vfs = safe_validate(fs1)
    check('形态2: faststart 重封装后 validate ok',
          bool(vfs and vfs.ok), vfs.reasons[:3] if vfs else 'no file')
    ok2, err2 = decode_ok(fs1)
    check('形态2: faststart 产物完整解码', ok2, err2[:300])


# ==================== 2. 完整覆盖: 应为忠实复制 ====================

print('\n[2] 全部分片 + moov 齐全 -> 忠实复制 (moov 字节级不变)')

all_slices = make_slices((len(raw) + K_IN_SLICE - 1) // K_IN_SLICE + 1)
out2 = os.path.join(OUT, 'complete.mp4')
res2 = rebuild_large_video_to_file(header, all_slices, out2)
check('完整: 重建成功', res2.playable, res2.reasons)
check('完整: 未截断', not res2.truncated)
check('完整: 时长 == 完整时长',
      abs(res2.duration_s - full_dur) < 0.1,
      f'{res2.duration_s:.3f} vs {full_dur:.3f}')
if guard_product(out2, '完整'):
    v2 = validate(out2)
    check('完整: validate ok', v2.ok, v2.reasons[:3])
    ok3, err3 = decode_ok(out2)
    check('完整: ffmpeg 完整解码', ok3, err3[:300])
    check('完整: 产物 mdat 与源一致 (前 128KB 之外逐字节相等)',
          open(out2, 'rb').read()[K_PART_MAX:res2.p] == raw[K_PART_MAX:res2.p])


# ==================== 3. faststart (moov 在前) + 只覆盖前缀 ====================

print('\n[3] faststart (moov 在文件前) + 只覆盖前缀 -> 不得出现两个 moov')

rfs = Reader(BIG_FS)
n_fs = rfs.size()
fs_boxes = list(iter_boxes(rfs, 0, n_fs))
fs_moov = next(b for b in fs_boxes if b.type == b'moov')
fs_mdat = next(b for b in fs_boxes if b.type == b'mdat')
print(f'  moov=[{fs_moov.start},{fs_moov.end})  mdat=[{fs_mdat.start},{fs_mdat.end})')
raw_fs = open(BIG_FS, 'rb').read()
header_fs = build_header(split_parts(0, raw_fs[:K_PART_MAX]))
slices_fs = make_slices(2, src=raw_fs)
out3 = os.path.join(OUT, 'faststart_prefix.mp4')
res3 = rebuild_large_video_to_file(header_fs, slices_fs, out3)
print(f'  playable={res3.playable} p={res3.p} truncated={res3.truncated} '
      f'dur={res3.duration_s:.2f}  reasons={res3.reasons}')
check('faststart: 重建成功', res3.playable, res3.reasons)
if guard_product(out3, 'faststart'):
    v3 = validate(out3)
    check('faststart: validate ok', v3.ok, v3.reasons[:3])
    n_moov = sum(1 for b in iter_boxes(Reader(out3), 0, os.path.getsize(out3))
                 if b.type == b'moov')
    check('faststart: 产物中只有一个 moov', n_moov == 1, f'moov 数量={n_moov}')
    ok4, err4 = decode_ok(out3)
    check('faststart: ffmpeg 完整解码', ok4, err4[:300])
    check('faststart: 时长等于前缀覆盖时长 (明显小于完整)',
          res3.duration_s < full_dur * 0.95, f'{res3.duration_s:.2f}')
rfs.close()


# ==================== 4. moov 缺失 -> 必须失败且不留文件 ====================

print('\n[4] moov 未缓存 -> 必须失败且清理产物')

header_no_moov = build_header(split_parts(0, raw[:K_PART_MAX]))
out4 = os.path.join(OUT, 'no_moov.mp4')
res4 = rebuild_large_video_to_file(header_no_moov, make_slices(2), out4)
print(f'  playable={res4.playable} reasons={res4.reasons}')
check('缺 moov: 判定失败', not res4.playable)
check('缺 moov: 失败原因明确', any('moov' in x for x in res4.reasons), res4.reasons)
check('缺 moov: 不留下产物文件', not os.path.exists(out4))


# ==================== 5. 只有尾部 moov / 只有头部 128KB ====================

print('\n[5a] 只有尾部 moov 区段 (无文件起始覆盖) -> 必须失败')

header_only_moov = build_header(split_parts(moov_box.start, raw[moov_box.start:moov_box.end]))
out5a = os.path.join(OUT, 'only_moov.mp4')
res5a = rebuild_large_video_to_file(header_only_moov, [], out5a)
print(f'  playable={res5a.playable} reasons={res5a.reasons}')
check('只有 moov: 判定失败', not res5a.playable, res5a.reasons)
check('只有 moov: 不留下产物文件', not os.path.exists(out5a))

print('\n[5b] 只有头部 128KB (+ moov) -> 允许产出极短片段, 但不得引用未覆盖字节')

out5b = os.path.join(OUT, 'head_only.mp4')
res5b = rebuild_large_video_to_file(header, [], out5b)
print(f'  playable={res5b.playable} p={res5b.p} dur={res5b.duration_s:.3f}s '
      f'reasons={res5b.reasons}')
check('只有头部: 输出前缀不超过已覆盖的 128KB',
      res5b.p <= K_PART_MAX, f'p={res5b.p}')
check('只有头部: 时长极短 (<1s)', res5b.duration_s < 1.0, f'{res5b.duration_s:.3f}s')
if guard_product(out5b, '只有头部'):
    v5b = validate(out5b)
    check('只有头部: validate ok', v5b.ok, v5b.reasons[:3])
    d5b = open(out5b, 'rb').read()
    holes5b = sum(1 for i in range(0, len(d5b) - 8192, 4096)
                  if d5b[i:i + 4096] == b'\x00' * 4096
                  and d5b[i + 4096:i + 8192] == b'\x00' * 4096)
    check('只有头部: 不含零洞', holes5b == 0, f'holes={holes5b}')
    ok5b, err5b = decode_ok(out5b)
    check('只有头部: ffmpeg 可解码该极短片段', ok5b, err5b[:200])


# ==================== 6. 中间分片缺失 -> 只能用到空洞之前 ====================

print('\n[6] 缺中间分片: 只能输出到空洞之前, 不得跨洞')

slices_gap = make_slices(1) + make_slices(4)[3:]     # 只有 0, 3
out6 = os.path.join(OUT, 'gap.mp4')
res6 = rebuild_large_video_to_file(header, slices_gap, out6)
print(f'  playable={res6.playable} p={res6.p} truncated={res6.truncated} '
      f'dur={res6.duration_s:.2f}s reasons={res6.reasons}')
check('缺中间分片: 重建成功', res6.playable, res6.reasons)
check('缺中间分片: 只写出第一个分片范围', res6.p <= K_IN_SLICE, f'p={res6.p}')
check('缺中间分片: 产出的时长只覆盖前缀',
      res6.duration_s < full_dur * 0.5, f'{res6.duration_s:.2f}')
if guard_product(out6, '缺中间分片'):
    v6 = validate(out6)
    check('缺中间分片: validate ok', v6.ok, v6.reasons[:3])
    ok6, err6 = decode_ok(out6)
    check('缺中间分片: ffmpeg 完整解码', ok6, err6[:300])


print(f'\n===== {len(PASS)} passed, {len(FAIL)} failed =====')
if FAIL:
    for f in FAIL:
        print('  FAILED:', f)
    sys.exit(1)
