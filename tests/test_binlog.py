"""
binlog 归组口径自测 (2026-09-15 真实 tdata 取证后的回归)。

**核心 bug**: 过去认为"key_high 相同 = 同一部媒体"。实测同一个 key_high 下
会混进多部不同的视频 (它们的 slice_index 都算成 0), 于是把别的视频的 8MB
分片缝进当前视频, 产出"结构完整、时长正常、但 ffmpeg 解码报
Invalid NAL unit size"的坏文件 —— 这正是"恢复的视频无法持久播放"的根因。

正确口径: key_low = document_id(48bit) << 16 | slice_index(16bit),
         同一部媒体 = (key_high, key_low >> 16) 相同。

运行: python tests/test_binlog.py
"""

import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

from src.binlog import BinlogRecord, get_slice_records_for_header  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=''):
    (PASS if cond else FAIL).append(name)
    print(('  PASS  ' if cond else '  FAIL  ') + name
          + (('   ' + str(detail)) if detail else ''))


def rec(file_name, key_high, doc_id, slice_index, size=8 << 20):
    """按 key_low = doc_id << 16 | slice_index 组装一条记录"""
    return BinlogRecord(tag=0, size=size, place=b'\x00' * 7, checksum=0,
                        key_high=key_high,
                        key_low=(doc_id << 16) | slice_index)


KH = 0x0000000000015431
DOC_A = 0x69DB000011A5
DOC_B = 0x69DB000011A6      # 另一部视频, 与 A 同 key_high


# ==================== 1. key 字段的拆解 ====================

print('\n[1] key_low 拆解: 高 48 位是文档 id, 低 16 位是 slice 索引')

r = rec('AAAA', KH, DOC_A, 7)
check('slice_index 取低 16 位', r.slice_index == 7, r.slice_index)
check('doc_key = (key_high, key_low>>16)', r.doc_key == (KH, DOC_A), r.doc_key)


# ==================== 2. 关键回归: 不同视频不得混成一组 ====================

print('\n[2] 回归: 同一 key_high 下的不同视频, 分片不得互相串台')

class _Named(BinlogRecord):
    """让 file_name 直接等于给定名字, 便于构造测试索引"""

    def __init__(self, name, kh, doc, si):
        super().__init__(tag=0, size=8 << 20, place=b'\x00' * 7, checksum=0,
                         key_high=kh, key_low=(doc << 16) | si)
        self._name = name

    @property
    def file_name(self) -> str:
        return self._name


# 视频 A: slice 0 (header) + slice 1..3
# 视频 B: 与 A 同 key_high, 但文档 id 不同 —— 旧实现会把它们并进 A
by_name = {
    'A000': (KH, DOC_A, 0), 'A001': (KH, DOC_A, 1), 'A002': (KH, DOC_A, 2),
    'A003': (KH, DOC_A, 3),
    'B000': (KH, DOC_B, 0), 'B001': (KH, DOC_B, 1), 'B002': (KH, DOC_B, 2),
    'B003': (KH, DOC_B, 3), 'B004': (KH, DOC_B, 4),
}
idx = {name: _Named(name, kh, doc, si)
       for name, (kh, doc, si) in by_name.items()}
allnames = set(by_name)

slices_a = get_slice_records_for_header('A000', idx, allnames)
names_a = [fn for _si, fn, _sz in slices_a]
check('视频 A 只拿到自己的 3 个分片', len(slices_a) == 3, names_a)
check('视频 A 不含视频 B 的任何分片',
      not any(n.startswith('B') for n in names_a), names_a)
check('视频 A 的分片索引为 [1,2,3]',
      [si for si, _fn, _sz in slices_a] == [1, 2, 3],
      [si for si, _fn, _sz in slices_a])

slices_b = get_slice_records_for_header('B000', idx, allnames)
names_b = [fn for _si, fn, _sz in slices_b]
check('视频 B 只拿到自己的 4 个分片', len(slices_b) == 4, names_b)
check('视频 B 不含视频 A 的任何分片',
      not any(n.startswith('A') for n in names_b), names_b)

# 旧口径 (只比 key_high) 会把 B 的分片也并进 A —— 证明这个 bug 真实存在
old_way = [n for n in allnames
           if n != 'A000' and idx[n].key_high == idx['A000'].key_high]
stolen = [n for n in old_way if n.startswith('B') and n != 'B000']
check('旧口径会把别的视频的分片也算进来 (证明 bug 真实存在)',
      len(stolen) == 4 and len(old_way) == 8, (len(stolen), len(old_way)))


# ==================== 3. 磁盘上不存在的分片必须被跳过 ====================

print('\n[3] 磁盘上不存在的分片不参与')

slices = get_slice_records_for_header('A000', idx, {'A000', 'A001', 'A003'})
check('只返回磁盘上存在的分片', [fn for _si, fn, _sz in slices] == ['A001', 'A003'],
      slices)
check('索引仍然是真实的 slice 编号', [si for si, _fn, _sz in slices] == [1, 3])


# ==================== 4. 同 slice_index 有多条记录时取磁盘上最大的 ====================

print('\n[4] 同一 slice_index 多条记录 -> 取磁盘上最大的')

idx['A001b'] = _Named('A001b', KH, DOC_A, 1)
idx['A001b'].size = 12 << 20       # 比 A001 大
slices = get_slice_records_for_header('A000', idx, {'A000', 'A001', 'A001b'})
check('同一索引只保留一条', len([s for s in slices if s[0] == 1]) == 1, slices)
check('取 size 最大的那条',
      [fn for si, fn, _sz in slices if si == 1] == ['A001b'], slices)


# ==================== 5. header 自身不会被当成自己的分片 ====================

print('\n[5] header 自身不被计入分片')

slices = get_slice_records_for_header('A000', idx, allnames)
check('结果里没有 header 自己', 'A000' not in [fn for _si, fn, _sz in slices])

print(f'\n===== {len(PASS)} passed, {len(FAIL)} failed =====')
if FAIL:
    for f in FAIL:
        print('  FAILED:', f)
    sys.exit(1)
