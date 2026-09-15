"""
src/deserializer.py 自测: 两个静默损坏 bug + 完整性推断规则的回归

运行: python tests/test_deserializer.py
"""

import os
import struct
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)          # 夹具与 ffmpeg 路径都相对项目根

from src.coverage import K_IN_SLICE  # noqa: E402
from src.deserializer import deserialize_video, get_large_video_info  # noqa: E402
from src.mp4 import Reader, iter_boxes, parse_moov  # noqa: E402
from src.rebuild import collect_header_extents  # noqa: E402

FIX = os.path.join('.temp', 'fixtures')
PASS, FAIL = [], []


def check(name, cond, detail=''):
    (PASS if cond else FAIL).append(name)
    print(('  PASS  ' if cond else '  FAIL  ') + name + (('   ' + str(detail)) if detail else ''))


def build_serialized(parts):
    """
    构造 slice→part 格式。

    注意: 这里按 `_parse_serialized_data` 的**实际读法**构造 ——
    [part_count + [out_off + size + data]*N] 重复 N 次, 没有前导 slice_count。
    (模块 docstring 与 scanner._check_serialized_video 说的是
     `slice_count + [part_count + parts]*N`, 二者不一致; 见文件末尾说明。)
    """
    body = bytearray()
    groups = [parts[i:i + 79] for i in range(0, len(parts), 79)]
    for g in groups:
        body += struct.pack('<I', len(g))         # part_count
        for out_off, data in g:
            body += struct.pack('<I', out_off)
            body += struct.pack('<I', len(data))
            body += data
    return bytes(body)


def build_header(parts):
    """复杂格式: count + [off + size + data]*count"""
    body = bytearray()
    body += struct.pack('<I', len(parts))
    for off, data in parts:
        body += struct.pack('<I', off)
        body += struct.pack('<I', len(data))
        body += data
    return bytes(body)


print('\n[1] file_size 必须取 max(out_offset + part_size)')

# part 1: out=0, size=200  (结束于 200)
# part 2: out=100, size=10 (结束于 110) —— 起始偏移更大但结束更早
# 旧实现按 out_offset 排序取"最后一个 part 的起点+长度" -> 110,
# 于是 out=0 的 part 因 0+200 > 110 被静默丢弃, 返回一个 110 字节的残缺缓冲区。
data = build_serialized([(0, b'A' * 200), (100, b'B' * 10)])
out = deserialize_video(data)
check('返回非空', out is not None)
check('缓冲区长度 = max 结束位置 (200)', out is not None and len(out) == 200,
      f'len={len(out) if out else None}')
check('out=0 的 part 未被丢弃', out is not None and out[0:100] == b'A' * 100)
check('重叠 part 后者覆盖前者', out is not None and out[100:110] == b'B' * 10)


print('\n[2] 不得截断尾部零块 (旧实现会砍掉 moov)')

# 覆盖 [0, 20000), 最后 4096 字节是全零 —— 模拟尾部 free box / moov 尾部填充。
# 旧实现会截到"最后一个非零字节"(约 15904), 于是 moov 声明的 box 大小超过文件,
# 输出彻底无法解复用。
payload = b'C' * (20000 - 4096) + b'\x00' * 4096
data = build_serialized([(0, payload)])
out = deserialize_video(data)
check('缓冲区未被截断 (长度仍为 20000)', out is not None and len(out) == 20000,
      f'len={len(out) if out else None}')
# 对照: 旧的"截到最后一个非零字节"逻辑会得到 15904
from src.deserializer import _find_continuous_data_range  # noqa: E402
legacy_len = _find_continuous_data_range(bytearray(payload))
check('旧截断逻辑确实会砍掉尾部 (证明该 bug 真实存在)', legacy_len < 20000,
      f'旧逻辑会截到 {legacy_len}')


print('\n[3] 完整序列化 map 应逐字节还原原文件')

BIG = os.path.join(FIX, 'big_moov_end.mp4')
if os.path.exists(BIG):
    raw = open(BIG, 'rb').read()
    # 切成 <=128KB 的 part, 全部收进一个 slice
    parts = []
    step = 128 * 1024
    for off in range(0, len(raw), step):
        parts.append((off, raw[off:off + step]))
    serialized = build_serialized(parts)
    out = deserialize_video(serialized, max_size=1 << 30)
    check('大文件往返成功', out is not None)
    check('往返逐字节一致', out == raw,
          f'len {len(out) if out else None} vs {len(raw)}')
else:
    check('big_moov_end.mp4 存在 (先跑 rebuild_selftest)', False)


print('\n[4] 体积上限: 超限返回 None (避免把整部视频读进内存)')

data = build_serialized([(0, b'D' * 5000)])
check('小体积通过', deserialize_video(data, max_size=10000) is not None)
check('超限返回 None', deserialize_video(data, max_size=1000) is None)


print('\n[5] get_large_video_info: 不得再把"缺中段"判成 slices_needed = 0')

if os.path.exists(BIG):
    r = Reader(BIG)
    size = r.size()
    moov_box = next(b for b in iter_boxes(r, 0, size) if b.type == b'moov')
    moov = parse_moov(r)
    real_end = moov.max_chunk_end()
    r.close()

    # 形态 2: header 带 [0,128KB) 与尾部 moov 区段
    head = raw[:128 * 1024]
    moov_bytes = raw[moov_box.start:moov_box.end]
    header = build_header([(0, head), (moov_box.start, moov_bytes)])

    info = get_large_video_info(header)
    expect_slices = (real_end + K_IN_SLICE - 1) // K_IN_SLICE
    print(f'  total_size={info["total_size"]} slices_needed={info["slices_needed"]} '
          f'(期望 {expect_slices}) moov_present={info["moov_present"]}')

    check('moov 被识别', info['moov_present'] is True)
    check('total_size 精确等于 moov 声明的结束位置',
          info['total_size'] == real_end, f'{info["total_size"]} vs {real_end}')
    check('slices_needed 是 ceil(total/8MB), 不再是 0',
          info['slices_needed'] == expect_slices and info['slices_needed'] > 0,
          info['slices_needed'])
    check('estimated_size 与 total_size 一致',
          info['estimated_size'] == info['total_size'])
    check('has_ftyp 为真', info['has_ftyp'] is True)
    check('parts_count 记录 header part 数', info['parts_count'] == 2,
          info['parts_count'])

    print('\n[6] 旧的 estimated_size <= max_end 规则已删除')
    # max_end 在这里约等于 128KB 与 moov 结束位置里的较大者 = moov 结束位置 = real_end
    # 旧规则会因 estimated_size(≈real_end) <= max_end(≈real_end) 而返回 0
    from src.rebuild import analyze  # noqa: E402
    parts = collect_header_extents(header)
    max_end = max(off + len(d) for off, d in parts)
    print(f'  max_end={max_end}  (旧规则会判定 estimated<=max_end -> 0 分片)')
    check('旧规则的条件确实成立 (证明该 bug 真实存在)', real_end <= max_end,
          f'real_end={real_end} max_end={max_end}')
    check('新实现给出非零 slices_needed', info['slices_needed'] > 0)

    a = analyze(header)
    check('analyze: moov 可用', a.moov_present)
    check('analyze: 判定为不完整 (只有 header 时)',
          a.complete is False, a.reasons[:2])
    check('analyze: 需求区间非空', a.required_count > 0, a.required_count)
    check('analyze: 缺口存在', len(a.missing_ranges) > 0,
          f'{len(a.missing_ranges)} 段缺失')
else:
    check('big_moov_end.mp4 存在 (先跑 rebuild_selftest)', False)


print(f'\n===== {len(PASS)} passed, {len(FAIL)} failed =====')
if FAIL:
    for f in FAIL:
        print('  FAILED:', f)
    sys.exit(1)
