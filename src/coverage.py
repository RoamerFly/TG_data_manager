"""
字节覆盖率模型

替代项目中所有"猜完整性"的启发式:
  - deserializer.get_large_video_info 的 `estimated_size <= max_end ⇒ slices_needed = 0`
  - server._enrich_scan_without_binlog 的"用全盘 8MB 文件总数冒充这个视频的分片数"
  - server._enrich_scan_with_binlog 的 `range(0, N)` 索引算术
  - server._rebuild_large_video 的"按 mtime 30 秒聚类猜分片归属"

权威定义只有一条:
    完整 ⇔ 覆盖区间集合 ⊇ moov 中每个 chunk 的 [offset, offset+size)

覆盖率是"事实", 不是"推断": 它只由 header 里 parts 的 out_offset 与
磁盘上真实存在的分片构成, 任何没有被这两者证明覆盖的字节都算缺失。

仅依赖标准库。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    'K_IN_SLICE', 'Extent', 'Coverage',
    'build_large_video_coverage', 'coverage_from_pairs', 'format_bytes',
]

# 大视频分片大小 (8 MiB)
K_IN_SLICE = 8 * 1024 * 1024

# JSON 中保留的原始来源条目上限 (超出后只保留合并区间, 不再记录 ref)
MAX_RAW_ENTRIES = 8192


@dataclass(frozen=True)
class Extent:
    """一个半开字节区间 [lo, hi)"""
    lo: int
    hi: int
    kind: str = ''          # 'header' | 'slice' | 'file' | ''
    ref: str = ''           # 来源标识 (分片文件名 / header 文件名)

    @property
    def size(self) -> int:
        return max(0, self.hi - self.lo)

    def as_tuple(self) -> Tuple[int, int]:
        return (self.lo, self.hi)

    def __repr__(self) -> str:      # pragma: no cover
        return f'<Extent {self.lo}-{self.hi} {self.kind}:{self.ref}>'


class Coverage:
    """
    合并后的字节区间集合。

    语义:
      - `extents` 返回规范化 (排序 + 合并重叠/相邻) 后的区间列表
      - 所有查询都基于规范化结果, 与 add() 的顺序无关
    """

    __slots__ = ('_raw', '_merged')

    def __init__(self, extents: Optional[Iterable[Extent]] = None) -> None:
        self._raw: List[Extent] = []
        self._merged: Optional[List[Extent]] = None
        if extents:
            for e in extents:
                self.add(e.lo, e.hi, e.kind, e.ref)

    # ---- 构建 ----

    def add(self, lo: int, hi: int, kind: str = '', ref: str = '') -> 'Coverage':
        """加入一个区间。hi <= lo 时忽略。"""
        if hi > lo and lo >= 0:
            self._raw.append(Extent(int(lo), int(hi), kind, ref))
            self._merged = None
        return self

    def merge(self, other: 'Coverage') -> 'Coverage':
        for e in other.raw:
            self.add(e.lo, e.hi, e.kind, e.ref)
        return self

    @property
    def raw(self) -> List[Extent]:
        return list(self._raw)

    @property
    def extents(self) -> List[Extent]:
        if self._merged is None:
            self._merged = self._compute_merged()
        return self._merged

    def _compute_merged(self) -> List[Extent]:
        if not self._raw:
            return []
        ordered = sorted(self._raw, key=lambda e: (e.lo, e.hi))
        out: List[Extent] = [ordered[0]]
        for e in ordered[1:]:
            last = out[-1]
            if e.lo <= last.hi:                     # 重叠或相邻都合并
                if e.hi > last.hi:
                    out[-1] = Extent(last.lo, e.hi, last.kind or e.kind, last.ref or e.ref)
            else:
                out.append(e)
        return out

    def normalized(self) -> 'Coverage':
        c = Coverage()
        for e in self.extents:
            c._raw.append(e)
        c._merged = list(c._raw)
        return c

    # ---- 查询 ----

    def __bool__(self) -> bool:
        return bool(self._raw)

    def __len__(self) -> int:
        return len(self.extents)

    @property
    def total_end(self) -> int:
        """覆盖到的最远字节"""
        ex = self.extents
        return ex[-1].hi if ex else 0

    def covered_bytes(self) -> int:
        return sum(e.size for e in self.extents)

    def contains(self, lo: int, hi: int) -> bool:
        """[lo, hi) 是否被完整覆盖 (必须落在同一个合并区间内)"""
        if hi <= lo:
            return True
        for e in self.extents:
            if e.lo <= lo and hi <= e.hi:
                return True
            if e.lo > lo:
                break
        return False

    def clip(self, lo: int, hi: int) -> Optional[Extent]:
        """返回 [lo, hi) 与覆盖区间的交集 (取第一个命中的合并区间)"""
        for e in self.extents:
            if e.lo >= hi:
                break
            if e.hi <= lo:
                continue
            return Extent(max(lo, e.lo), min(hi, e.hi), e.kind, e.ref)
        return None

    def covered_prefix_end(self) -> int:
        """[0, N) 被完整覆盖时的最大 N (从 0 起的连续覆盖长度)"""
        ex = self.extents
        if not ex or ex[0].lo > 0:
            return 0
        end = ex[0].hi
        for e in ex[1:]:
            if e.lo <= end:
                if e.hi > end:
                    end = e.hi
            else:
                break
        return end

    def first_hole_from(self, start: int = 0) -> Optional[int]:
        """
        从 start 起第一个未被覆盖的字节位置; 若 [start, total_end) 全被覆盖返回 None。
        """
        pos = start
        for e in self.extents:
            if e.hi <= pos:
                continue
            if e.lo > pos:
                return pos
            pos = e.hi
        return None

    def gaps_within(self, total: int) -> List[Tuple[int, int]]:
        """[0, total) 内未被覆盖的区间"""
        out: List[Tuple[int, int]] = []
        pos = 0
        for e in self.extents:
            if e.lo > pos:
                out.append((pos, min(e.lo, total)))
            pos = max(pos, e.hi)
            if pos >= total:
                break
        if pos < total:
            out.append((pos, total))
        return [(a, b) for a, b in out if b > a]

    def missing_regions(self, total: int, block: int = K_IN_SLICE) -> List[Tuple[int, int]]:
        """
        按固定块大小统计未被完整覆盖的块区间。

        仅用于**展示**。完整性判定必须用 covers_all(required_ranges) ——
        因为最后一分片常常是短块, 而 total 本身也是从 moov 估算出来的,
        纯块算术会把合法的短尾块误报为缺失。
        """
        out: List[Tuple[int, int]] = []
        if total <= 0 or block <= 0:
            return out
        n = (total + block - 1) // block
        for i in range(n):
            lo = i * block
            hi = min((i + 1) * block, total)
            if not self.contains(lo, hi):
                out.append((lo, hi))
        return out

    def missing_blocks(self, total: int, block: int = K_IN_SLICE) -> List[int]:
        """未被完整覆盖的块索引 (用于 UI 的分片缺口展示)"""
        out: List[int] = []
        if total <= 0 or block <= 0:
            return out
        n = (total + block - 1) // block
        for i in range(n):
            lo = i * block
            hi = min((i + 1) * block, total)
            if not self.contains(lo, hi):
                out.append(i)
        return out

    def present_blocks(self, total: int, block: int = K_IN_SLICE) -> List[int]:
        return [i for i in range((total + block - 1) // block)
                if self.contains(i * block, min((i + 1) * block, total))]

    def covers_all(self, ranges: Sequence[Tuple[int, int]]) -> bool:
        """是否覆盖给定的全部字节区间 (完整性判定的核心)"""
        return all(self.contains(lo, hi) for lo, hi in ranges)

    def missing_of(self, ranges: Sequence[Tuple[int, int]]) -> List[Tuple[int, int]]:
        return [(lo, hi) for lo, hi in ranges if not self.contains(lo, hi)]

    # ---- 身份 ----

    def signature(self) -> str:
        """
        覆盖率的稳定指纹。

        同时包含合并区间与原始来源条目, 所以:
          - 分片集合变化 → 指纹变化 (导出作废)
          - 分片重命名/替换 → 指纹变化
        与 add() 的调用顺序无关。
        """
        h = hashlib.sha1()
        for e in self.extents:
            h.update(b'E%d:%d;' % (e.lo, e.hi))
        for e in sorted(self._raw, key=lambda x: (x.lo, x.hi, x.kind, x.ref)):
            h.update(b'R%d:%d:%s:%s;' % (e.lo, e.hi, e.kind.encode('utf-8', 'replace'),
                                         e.ref.encode('utf-8', 'replace')))
        return h.hexdigest()

    # ---- 序列化 ----

    def to_json(self) -> str:
        payload = {
            'e': [[e.lo, e.hi] for e in self.extents],
            'k': self.signature(),
        }
        if len(self._raw) <= MAX_RAW_ENTRIES:
            payload['r'] = [[e.lo, e.hi, e.kind, e.ref] for e in self._raw]
        return json.dumps(payload, separators=(',', ':'), ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> 'Coverage':
        c = cls()
        if not text:
            return c
        try:
            payload = json.loads(text)
        except Exception:
            return c
        for item in payload.get('r') or []:
            if len(item) >= 2:
                c._raw.append(Extent(int(item[0]), int(item[1]),
                                     item[2] if len(item) > 2 else '',
                                     item[3] if len(item) > 3 else ''))
        if not c._raw:
            for item in payload.get('e') or []:
                if len(item) >= 2:
                    c._raw.append(Extent(int(item[0]), int(item[1])))
        return c

    def describe(self) -> str:
        if not self._raw:
            return '无任何已缓存区段'
        parts = []
        for e in self.extents[:6]:
            parts.append(f'{format_bytes(e.lo)}-{format_bytes(e.hi)}')
        more = '' if len(self.extents) <= 6 else f' …(+{len(self.extents) - 6})'
        return f'{len(self.extents)} 段, 共 {format_bytes(self.covered_bytes())}: ' \
               + ', '.join(parts) + more


# ==================== 便捷构造 ====================

def coverage_from_pairs(pairs: Iterable[Tuple[int, int]], kind: str = '',
                        ref: str = '') -> Coverage:
    c = Coverage()
    for lo, hi in pairs:
        c.add(lo, hi, kind, ref)
    return c


def build_large_video_coverage(
    header_parts: Iterable[Tuple[int, int]],
    slices: Iterable[Tuple[int, int, str]],
    slice_size: int = K_IN_SLICE,
    header_name: str = '',
) -> Coverage:
    """
    由 header 的 parts 与已存在的 8MB 分片构造覆盖率。

    Args:
        header_parts: [(out_offset, size), ...]
            来自 deserializer._parse_complex_format 的 parts (含 remaining 段解析结果)。
            out_offset 是**原始媒体文件里的绝对偏移**。
        slices: [(slice_index, size, file_name), ...]
            由 binlog 关联出的、磁盘上真实存在的分片。
            slice_index = N 表示该分片覆盖 [N * slice_size, N * slice_size + size)。
        slice_size: 分片大小 (8 MiB)
        header_name: header 文件名 (写入 ref, 便于诊断)

    Returns:
        Coverage
    """
    cov = Coverage()
    for off, size in header_parts:
        if size and size > 0:
            cov.add(int(off), int(off) + int(size), 'header', header_name)
    for item in slices:
        if len(item) >= 2:
            idx = int(item[0])
            size = int(item[1])
            name = item[2] if len(item) > 2 else ''
            if size > 0 and idx >= 0:
                lo = idx * slice_size
                cov.add(lo, lo + size, 'slice', name)
    return cov


def format_bytes(n: int) -> str:
    if n < 0:
        n = 0
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024 or unit == 'TB':
            if unit == 'B':
                return f'{int(n)}{unit}'
            return f'{n:.1f}{unit}'
        n /= 1024.0
    return f'{n:.1f}TB'
