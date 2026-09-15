"""
src/mp4.py 自测: 用真实 ffmpeg 产物 + 手工构造的病态样本验证解析/截断/校验。

运行: python tests/test_mp4.py
"""

import os
import shutil
import struct
import subprocess
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)          # 夹具与 ffmpeg 路径都相对项目根

from src.mp4 import (  # noqa: E402
    Box, Reader, iter_boxes, parse_moov, patch_mdat_size, plan_prefix,
    stco_exceeds_size, truncate_tables, validate,
)

FFMPEG = os.path.join('dist_windows', 'env', 'ffmpeg.exe')
FFPROBE = os.path.join('dist_windows', 'env', 'ffprobe.exe')
FIX = os.path.join('.temp', 'fixtures')

PASS = []
FAIL = []


def check(name, cond, detail=''):
    (PASS if cond else FAIL).append(name)
    print(('  PASS  ' if cond else '  FAIL  ') + name + (('   ' + detail) if detail else ''))


def ffprobe_duration(path):
    try:
        out = subprocess.run(
            [FFPROBE, '-v', 'quiet', '-print_format', 'json', '-show_format', path],
            capture_output=True, timeout=30)
        import json
        return float(json.loads(out.stdout.decode('utf-8', 'replace'))['format']['duration'])
    except Exception:
        return None


def ffmpeg_decode_ok(path):
    r = subprocess.run([FFMPEG, '-v', 'error', '-i', path, '-f', 'null', '-'],
                       capture_output=True, timeout=120)
    return r.returncode == 0, r.stderr.decode('utf-8', 'replace').strip()


# ==================== 1. 真实文件上的 validate ====================

print('\n[1] validate on real ffmpeg output')

for name, expect_ok in (('faststart', True), ('moov_end', True), ('fragmented', False)):
    p = os.path.join(FIX, name + '.mp4')
    v = validate(p)
    check(f'{name}: ok={v.ok} (期望 {expect_ok})', v.ok == expect_ok,
          f'dur={v.duration_s:.3f} reasons={v.reasons[:2]}')

# 缺 moov -> 必须判失败 (旧 stco_exceeds_file 会错误地返回 False="没问题")
src = os.path.join(FIX, 'moov_end.mp4')
raw = open(src, 'rb').read()
moov_off = raw.rfind(b'moov') - 4
no_moov = raw[:moov_off]
v = validate(no_moov)
check('缺少 moov: ok=False', not v.ok, str(v.reasons[:1]))
check('缺少 moov: stco_exceeds_size()==True (不可信)', stco_exceeds_size(no_moov) is True)

# 尾部截断 -> chunk 越界必须被抓住
trunc = raw[:moov_off] + raw[moov_off:moov_off + 200]      # moov 只剩 200 字节
v = validate(trunc)
check('moov 被截断: ok=False', not v.ok, str(v.reasons[:1]))

cut = raw[:300000]                                          # 砍掉尾部 mdat + moov
v = validate(cut)
check('mdat 中途截断: ok=False', not v.ok, str(v.reasons[:1]))

# 完整文件不应被误判
check('完整 moov_end: stco_exceeds_size()==False', stco_exceeds_size(src) is False)


# ==================== 2. 形态 2 复现与修复 (核心) ====================
# 缓存形态: 头部前缀 [0,P) + 尾部独立 moov 区间 —— 这正是当前代码判为"完整"
# 却重建出满是零洞文件的情形。

print('\n[2] 形态 2: 前缀 + 尾部独立 moov  →  重打包为可播放的局部片段')

r = Reader(src)
file_size = r.size()
moov = parse_moov(r)
mdat = moov.mdat
assert mdat is not None
print(f'  mdat=[{mdat.start},{mdat.end})  moov=[{moov.raw and 0 or 0}]  file_size={file_size}')

P = 200_000                       # 头部只覆盖 200 KB
moov_lo = file_size - len(moov.raw)
coverage = [(0, P), (moov_lo, file_size)]


def is_covered(lo, hi, _cov=coverage):
    return any(l <= lo and hi <= h for l, h in _cov)


plan = plan_prefix(moov, is_covered, size_limit=file_size)
print(f'  plan: p={plan.p} truncated={plan.truncated} duration={plan.duration_s:.3f}s '
      f'keep={plan.per_track_keep} samples={plan.samples_kept}')
check('形态2: 判定为截断', plan.truncated)
check('形态2: plan.p <= P', plan.p <= P, f'p={plan.p}')

# 组装: [0, plan.p) 原样 + 截断后的 moov
buf = bytearray(r.read(0, plan.p))
patch_mdat_size(buf, mdat, plan.p - mdat.start)
new_moov = truncate_tables(moov.raw, plan)
out_path = os.path.join(FIX, 'repacked_prefix.mp4')
with open(out_path, 'wb') as f:
    f.write(buf)
    f.write(new_moov)

r.close()

# 2a. 结构校验
v = validate(out_path)
check('重打包产物 validate ok', v.ok, str(v.reasons[:3]))
check('重打包产物时长 ≈ plan.duration_s',
      abs(v.duration_s - plan.duration_s) < 0.05,
      f'{v.duration_s:.3f} vs {plan.duration_s:.3f}')

# 2b. 确实没有零洞 (前 4KB 之外不应出现 >=8KB 的连续零块)
data = open(out_path, 'rb').read()
holes = 0
run = 0
for i in range(0, len(data), 4096):
    if data[i:i + 4096] == b'\x00' * 4096:
        run += 1
        if run >= 2:
            holes += 1
    else:
        run = 0
check('重打包产物不含 >=8KB 零洞', holes == 0, f'零洞数={holes}')

# 2c. ffmpeg 能解复用 + faststart 后能完整解码
ok, err = ffmpeg_decode_ok(out_path)
check('ffmpeg 能完整解码重打包产物 (退出码 0)', ok, err[:200])

dur = ffprobe_duration(out_path)
check('ffprobe 时长与计算值一致',
      dur is not None and abs(dur - plan.duration_s) < 0.15,
      f'ffprobe={dur} computed={plan.duration_s:.3f}')

fast = os.path.join(FIX, 'repacked_faststart.mp4')
subprocess.run([FFMPEG, '-hide_banner', '-loglevel', 'error', '-y', '-i', out_path,
                '-c', 'copy', '-movflags', '+faststart', fast], check=False)
v2 = validate(fast)
check('faststart 重封装后 validate ok', v2.ok, str(v2.reasons[:3]))
ok2, err2 = ffmpeg_decode_ok(fast)
check('faststart 产物完整解码', ok2, err2[:200])


# ==================== 3. 完整视频: moov 必须字节级不变 ====================

print('\n[3] 完整覆盖时不应改动 moov')

r = Reader(src)
moov = parse_moov(r)
plan_full = plan_prefix(moov, lambda lo, hi: True, size_limit=r.size())
check('完整覆盖: truncated=False', not plan_full.truncated)
same = truncate_tables(moov.raw, plan_full) == moov.raw
check('完整覆盖: moov 字节级不变', same)
check('完整覆盖: plan.p <= 文件大小', plan_full.p <= r.size(),
      f'p={plan_full.p} size={r.size()}')
r.close()


# ==================== 4. 手工构造的变体 (co64 / stz2 / largesize mdat) ====================

print('\n[4] 手工构造变体: co64 / stz2 / 64位 mdat')


def _box(t, payload):
    return struct.pack('>I', 8 + len(payload)) + t + payload


def extract_video_stsd(path):
    """
    从真实 ffmpeg 产物里取出视频轨的 stsd box 字节。

    手工构造的夹具必须带上真实的 avc1 样本描述, 否则 ffprobe/ffmpeg 会因为
    "invalid STSD entries" 直接拒绝打开, 无法验证容器层行为。
    """
    from src.mp4 import Box, find_box
    r = Reader(path)
    box = find_box(r, (b'moov', b'trak', b'mdia', b'minf', b'stbl', b'stsd'))
    data = r.read(box.start, box.size)
    r.close()
    return data


REAL_STSD = extract_video_stsd(os.path.join(FIX, 'faststart.mp4'))


def build_min_mp4(samples=(100, 200, 300, 400), use_co64=False, use_stz2=False,
                  mdat_largesize=False, extra_bytes=64):
    """构造一个最小但结构自洽的单轨 MP4 (每 chunk 一个样本)"""
    n = len(samples)
    payload_size = sum(samples) + extra_bytes
    if mdat_largesize:
        mdat = struct.pack('>I', 1) + b'mdat' + struct.pack('>Q', 16 + payload_size) + b'\xAB' * payload_size
    else:
        mdat = struct.pack('>I', 8 + payload_size) + b'mdat' + b'\xAB' * payload_size

    ftyp = _box(b'ftyp', b'isom' + struct.pack('>I', 512) + b'isomiso2avc1mp41')

    # sample offsets: 紧跟在 mdat 数据区之后是不对的, 这里直接指到 mdat 载荷内
    mdat_payload_start = 16 if mdat_largesize else 8
    base = len(ftyp) + mdat_payload_start
    offs = []
    cur = base
    for s in samples:
        offs.append(cur)
        cur += s

    stts = _box(b'stts', b'\x00\x00\x00\x00' + struct.pack('>I', 1)
                + struct.pack('>II', n, 40))
    stsc = _box(b'stsc', b'\x00\x00\x00\x00' + struct.pack('>I', 1)
                + struct.pack('>III', 1, 1, 1))
    if use_stz2:
        body = b'\x00\x00\x00\x00' + bytes([0, 0, 0, 16]) + struct.pack('>I', n)
        for s in samples:
            body += struct.pack('>H', s)
        stsz = _box(b'stz2', body)
    else:
        body = b'\x00\x00\x00\x00' + struct.pack('>I', 0) + struct.pack('>I', n)
        for s in samples:
            body += struct.pack('>I', s)
        stsz = _box(b'stsz', body)
    if use_co64:
        body = b'\x00\x00\x00\x00' + struct.pack('>I', n)
        for o in offs:
            body += struct.pack('>Q', o)
        stco = _box(b'co64', body)
    else:
        body = b'\x00\x00\x00\x00' + struct.pack('>I', n)
        for o in offs:
            body += struct.pack('>I', o)
        stco = _box(b'stco', body)
    stss = _box(b'stss', b'\x00\x00\x00\x00' + struct.pack('>I', 1) + struct.pack('>I', 1))
    stbl = _box(b'stbl', REAL_STSD + stsz + stsc + stco + stts + stss)
    dinf = _box(b'dinf', b'')
    minf = _box(b'minf', dinf + stbl)
    hdlr = _box(b'hdlr', b'\x00\x00\x00\x00' + struct.pack('>I', 0) + b'vide' + b'\x00' * 12)
    mdhd = _box(b'mdhd', b'\x00\x00\x00\x00' + struct.pack('>IIII', 0, 0, 1000, n * 40) + b'\x00\x00')
    mdia = _box(b'mdia', mdhd + hdlr + minf)
    tkhd = _box(b'tkhd', b'\x00\x00\x00\x07' + struct.pack('>IIIII', 0, 0, 1, 0, n * 40)
                + b'\x00' * 60)
    trak = _box(b'trak', tkhd + mdia)
    mvhd = _box(b'mvhd', b'\x00\x00\x00\x00' + struct.pack('>IIII', 0, 0, 1000, n * 40) + b'\x00' * 80)
    moov = _box(b'moov', mvhd + trak)
    return ftyp + mdat + moov


for label, kw in (
    ('co64', dict(use_co64=True)),
    ('stz2', dict(use_stz2=True)),
    ('largesize_mdat', dict(mdat_largesize=True)),
    ('plain', dict()),
):
    path = os.path.join(FIX, f'min_{label}.mp4')
    with open(path, 'wb') as f:
        f.write(build_min_mp4(**kw))
    v = validate(path)
    check(f'min_{label}: validate ok', v.ok, str(v.reasons[:2]))

    # 截断到只剩前 2 个样本的字节, 用 plan+truncate 重写
    raw = open(path, 'rb').read()
    r2 = Reader(raw)
    m2 = parse_moov(r2)
    # 取第 2 个 chunk 的结束位置作为覆盖边界
    t0 = m2.tracks[0]
    if len(t0.chunk_offsets) >= 2:
        cut_at = t0.chunk_offsets[1] + t0.chunk_size(1)
    else:
        cut_at = r2.size()
    plan2 = plan_prefix(m2, lambda lo, hi, c=cut_at: hi <= c, size_limit=r2.size())
    new_moov = truncate_tables(m2.raw, plan2)
    out = bytearray(r2.read(0, plan2.p))
    patch_mdat_size(out, m2.mdat, plan2.p - m2.mdat.start)
    out += new_moov
    out_path2 = os.path.join(FIX, f'min_{label}_trimmed.mp4')
    with open(out_path2, 'wb') as f:
        f.write(out)
    r2.close()

    v2 = validate(out_path2)
    check(f'min_{label}: 截断后 validate ok', v2.ok, str(v2.reasons[:2]))
    check(f'min_{label}: 截断后 samples=2',
          plan2.samples_kept.get(t0.track_id) == 2,
          f"kept={plan2.samples_kept}")
    check(f'min_{label}: 截断后时长 80ms',
          abs(v2.duration_s - 0.08) < 1e-6, f'{v2.duration_s}')
    # ffprobe / ffmpeg 的容器层验证 (-c copy 不解码, 所以只要容器自洽就能通过)
    check(f'min_{label}: ffprobe 可解析', ffprobe_duration(out_path2) is not None)
    remux = os.path.join(FIX, f'min_{label}_remux.mp4')
    rc = subprocess.run([FFMPEG, '-hide_banner', '-loglevel', 'error', '-y',
                         '-i', out_path2, '-c', 'copy', '-movflags', '+faststart', remux],
                        capture_output=True)
    check(f'min_{label}: ffmpeg 能打开并 remux (退出码 0)', rc.returncode == 0,
          rc.stderr.decode('utf-8', 'replace')[:160])
    # 夹具的 mdat 是 0xAB 填充, ffmpeg 找不到合法 NAL 会**丢掉整条流**,
    # 产出一个没有 trak 的空 MP4 且退出码仍为 0。
    # 这正是"退出码 0 不等于成功"的陷阱 —— 校验必须抓住它。
    v3 = validate(remux)
    if v3.ok:
        check(f'min_{label}: remux 产物有效', True, f'dur={v3.duration_s:.3f}')
    else:
        check(f'min_{label}: ffmpeg 产出的无流文件被校验拒绝 (退出码陷阱)',
              'trak' in ' '.join(v3.reasons), str(v3.reasons[:2]))


# ==================== 5. 无覆盖 -> 拒绝 ====================

print('\n[5] 前缀外无任何覆盖时不得写出文件')

r = Reader(src)
moov = parse_moov(r)
plan_none = plan_prefix(moov, lambda lo, hi: False, size_limit=r.size())
check('全无覆盖: p == 0', plan_none.p == 0, f'p={plan_none.p}')
check('全无覆盖: 所有轨道 keep == 0',
      all(k == 0 for k in plan_none.per_track_keep.values()),
      str(plan_none.per_track_keep))
r.close()


print(f'\n===== {len(PASS)} passed, {len(FAIL)} failed =====')
if FAIL:
    for f in FAIL:
        print('  FAILED:', f)
    sys.exit(1)
