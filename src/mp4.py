"""
MP4 / ISO-BMFF box 解析、校验与表格截断

本模块存在的唯一理由: 取代项目中所有 `data.find(b'moov')` / `find(b'ftyp')` /
`find(b'mdat')` 式的子串探测。

在二进制媒体负载里用 find() 找 atom 会频繁误命中 (H.264/H.265 的 mdat 载荷里
出现 ASCII "moov"/"mdat"/"free" 是常态), 一旦命中就会从随机位置开始解析 box,
得到错误的 box 大小与错误的 stco 表 —— 既会漏报损坏, 也会把好文件判坏。

正确做法只能是从偏移 0 开始按 size/type 逐层遍历。本模块就是这件事。

对外能力:
  Reader / Box / iter_boxes / find_box / top_level     —— 通用 box 遍历
  Moov / Track / Chunk / parse_moov                    —— moov 语义解析
  required_ranges                                      —— 完整播放所需的字节区间
  plan_prefix                                          —— 只有连续前缀覆盖时, 可保留多少样本
  truncate_tables                                      —— 截断 stco/stsc/stsz/stts/stss/ctts + 修补时长
  patch_mdat_size                                      —— 把 mdat box 大小改写为实际写出长度
  validate                                             —— 结构自洽性校验 (含 chunk 越界检测)

设计要点:
- `stco` / `co64` 中的 chunk offset 是**从文件起始算的绝对偏移**。
  因此只要把已覆盖的前缀 [0, P) 按原始偏移原样写出 (mdat 起点不变),
  所有满足 offset + size <= P 的 chunk 偏移都无需任何重算, 只需把落在 P 之后的
  样本从各张表里截掉。这使"重写 moov"退化为"截断表格 + 修补时长"。
- 仅依赖标准库。
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

__all__ = [
    'Reader', 'Box', 'iter_boxes', 'find_box', 'top_level', 'read_box',
    'Track', 'Chunk', 'Moov', 'parse_moov', 'parse_moov_bytes',
    'chunk_table', 'required_ranges',
    'PrefixPlan', 'plan_prefix', 'truncate_tables', 'patch_mdat_size',
    'ValidationResult', 'validate',
    'CONTAINER_TYPES', 'is_valid_moov_bytes', 'looks_like_iso_bmff',
]


# ==================== 常量 ====================

# 需要递归进入的容器 box
# 注意: 故意不包含 b'stsd' —— stsd 的子项是 sample entry (avc1/mp4a 等),
# 它们的前 8 字节之后是固定字段而不是子 box, 递归进去会误解析。
CONTAINER_TYPES = frozenset({
    b'moov', b'trak', b'mdia', b'minf', b'stbl', b'edts', b'udta',
    b'mvex', b'moof', b'traf', b'mfra', b'dinf',
})

# 有实际意义的叶子 box
_TABLE_BOXES = frozenset({b'stco', b'co64', b'stsz', b'stz2', b'stts', b'stss', b'ctts', b'stsc'})

MAX_CHUNK_ENTRIES = 20_000_000          # 防御异常 entry_count
MAX_SAMPLE_COUNT = 200_000_000


# ==================== 底层读取 ====================

class Reader:
    """
    统一的随机读取抽象, 支持 bytes / bytearray / memoryview / 文件对象 / 路径。

    不缓存整个文件 —— 只按需 seek+read, 供大文件上的校验使用。
    """

    __slots__ = ('_buf', '_f', '_own', '_size')

    def __init__(self, src):
        self._buf = None
        self._f = None
        self._own = False
        self._size = 0
        if isinstance(src, (bytes, bytearray, memoryview)):
            self._buf = memoryview(src).cast('B')
            self._size = len(self._buf)
        elif hasattr(src, 'read') and hasattr(src, 'seek'):
            self._f = src
        else:
            self._f = open(os.fspath(src), 'rb')
            self._own = True
            self._size = os.fstat(self._f.fileno()).st_size

    # ---- 基本操作 ----

    def size(self) -> int:
        if self._buf is not None:
            return self._size
        if self._size:
            return self._size
        try:
            return os.fstat(self._f.fileno()).st_size
        except (OSError, AttributeError, ValueError):
            cur = self._f.tell()
            self._f.seek(0, 2)
            n = self._f.tell()
            self._f.seek(cur)
            return n

    def read(self, off: int, n: int) -> bytes:
        if n <= 0 or off < 0:
            return b''
        if self._buf is not None:
            end = min(off + n, self._size)
            if off >= end:
                return b''
            return bytes(self._buf[off:end])
        self._f.seek(off)
        return self._f.read(n)

    def close(self) -> None:
        if self._own and self._f is not None:
            try:
                self._f.close()
            except Exception:
                pass
        self._f = None
        self._buf = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _u(buf: bytes, off: int, n: int) -> int:
    """大端无符号整数, 越界返回 -1"""
    if off < 0 or off + n > len(buf):
        return -1
    return int.from_bytes(buf[off:off + n], 'big')


# ==================== Box 遍历 ====================

@dataclass(frozen=True)
class Box:
    """一个 box 的位置与尺寸 (不持有 payload)"""
    start: int
    size: int
    type: bytes
    header: int = 8            # 8 / 16 (largesize) / 24 (largesize+uuid) / 24 (uuid 无 largesize)

    @property
    def end(self) -> int:
        return self.start + self.size

    @property
    def payload_start(self) -> int:
        return self.start + self.header

    @property
    def payload_size(self) -> int:
        return self.size - self.header

    def __repr__(self) -> str:      # pragma: no cover - 调试用
        return f'<Box {self.type.decode("latin-1")} @{self.start} size={self.size}>'


def iter_boxes(reader: Reader,
               start: int,
               end: int,
               fatal: Optional[List[str]] = None,
               notes: Optional[List[str]] = None,
               allow_truncated_tail: bool = False) -> Iterator[Box]:
    """
    顺序遍历 [start, end) 内的同级 box。

    - size == 1  -> 64 位 largesize (header 16; uuid 再 +16)
    - size == 0  -> 延伸到 end (只对最后一个 box 合法)
    - size < header 或越界 -> 记入 fatal 并终止本层遍历
    - 末尾不足 8 字节的残留 -> 记入 notes (非致命, 不影响播放)

    allow_truncated_tail: 当最后一个 box 声明的 size 超出 end 时, 仍然把它按
    裁剪后的长度 yield 出来并记入 notes (而不是判为 fatal)。用于"只能看到文件
    前缀"的场景 —— 例如 mdat 声明的大小覆盖整个文件, 而我们只缓存了前半段,
    此时仍需知道 mdat 的位置与 header 长度。

    不递归; 容器链由调用方 (find_box / parse_moov) 驱动。
    """
    pos = start
    while pos + 8 <= end:
        head = reader.read(pos, 8)
        if len(head) < 8:
            _note(fatal, f'box header truncated at {pos}')
            return
        size = int.from_bytes(head[0:4], 'big')
        btype = head[4:8]
        header = 8

        if size == 1:
            ext = reader.read(pos + 8, 8)
            if len(ext) < 8:
                _note(fatal, f'largesize truncated at {pos}')
                return
            size = int.from_bytes(ext, 'big')
            header = 16
        elif size == 0:
            size = end - pos
            _note(notes, f'box {btype!r} at {pos} has size 0 (extends to end)')

        if btype == b'uuid':
            header += 16

        if size < header or pos + size > end:
            if allow_truncated_tail and size >= header:
                _note(notes, f'box {btype!r} at {pos} 声明 size={size} 超出可用范围, 已裁剪')
                yield Box(pos, end - pos, btype, header)
            else:
                _note(fatal, f'bad box {btype!r} at {pos}: size={size} exceeds [{start},{end})')
            return

        yield Box(pos, size, btype, header)
        pos += size

    if pos < end:
        _note(notes, f'{end - pos} trailing byte(s) after last box')


def _note(lst: Optional[List[str]], msg: str) -> None:
    if lst is not None:
        lst.append(msg)


def find_box(reader: Reader,
             path: Sequence[bytes],
             start: int = 0,
             end: Optional[int] = None,
             fatal: Optional[List[str]] = None,
             notes: Optional[List[str]] = None) -> Optional[Box]:
    """
    按 box 层级路径定位, 例如 (b'moov', b'trak', b'mdia', b'minf', b'stbl')。

    找不到返回 None, 不会猜测、不会回退到子串搜索。
    """
    if end is None:
        end = reader.size()
    cur_lo, cur_hi = start, end
    found: Optional[Box] = None
    for want in path:
        found = None
        for b in iter_boxes(reader, cur_lo, cur_hi, fatal, notes):
            if b.type == want:
                found = b
                break
        if found is None:
            return None
        cur_lo, cur_hi = found.payload_start, found.end
    return found


def top_level(reader: Reader,
              fatal: Optional[List[str]] = None,
              notes: Optional[List[str]] = None) -> List[Box]:
    """返回顶层 box 列表"""
    return list(iter_boxes(reader, 0, reader.size(), fatal, notes))


def read_box(reader: Reader, box: Box) -> bytes:
    """读取整个 box (含 header) 的字节"""
    return reader.read(box.start, box.size)


# 合法的 ISOBMFF 顶层 box 类型 (用于粗略识别 MP4/MOV 容器)
_TOP_LEVEL_MP4_TYPES = frozenset({
    b'ftyp', b'styp', b'moov', b'mdat', b'free', b'skip', b'wide',
    b'sidx', b'ssix', b'moof', b'meta', b'mfra', b'pdin', b'uuid',
})


def looks_like_iso_bmff(src) -> bool:
    """
    粗略判定文件是否是 ISOBMFF (MP4/MOV) 容器。

    存在的意义: WebM/Matroska 以 EBML magic ``1A 45 DF A3`` 开头, 根本不是
    box 结构; 对它们跑 validate() 必然报 "顶层没有任何 box", 于是这一类文件
    会被永久挡在导出门外。验证门需要先判容器, 再决定用哪套规则。
    """
    try:
        reader = Reader(src)
        try:
            head = reader.read(0, 12)
        finally:
            reader.close()
    except Exception:
        return False
    if len(head) < 8:
        return False
    size = int.from_bytes(head[:4], 'big')
    # size: >=8 正常; 1 = 后面跟 64 位 largesize; 0 = 延伸至文件末尾
    if size != 1 and size != 0 and size < 8:
        return False
    return head[4:8] in _TOP_LEVEL_MP4_TYPES


def is_valid_moov_bytes(data: bytes) -> bool:
    """
    在内存字节中判定是否存在结构合法的 moov box。

    这是 `scanner._has_valid_moov` 的正确实现 —— 遍历顶层 box, 不搜索子串。
    """
    if not data or len(data) < 8:
        return False
    reader = Reader(data)
    for b in iter_boxes(reader, 0, len(data)):
        if b.type == b'moov':
            return True
    return False


# ==================== moov 语义解析 ====================

@dataclass(frozen=True)
class Chunk:
    """一个 chunk 的字节区间与样本区间"""
    index: int              # 0-based
    offset: int             # 绝对文件偏移
    size: int               # 该 chunk 全部样本字节数
    first_sample: int       # 0-based
    sample_count: int
    starts_with_sync: bool

    @property
    def end(self) -> int:
        return self.offset + self.size


class Track:
    """
    一条轨道 (trak) 的样本表。

    保持惰性: sample_sizes 只在 stsz.sample_size == 0 / stz2 时才物化。
    """

    __slots__ = (
        'track_id', 'handler', 'timescale', 'duration_ticks',
        'chunk_offsets', 'chunk_samples', 'sample_count',
        'sample_size_const', 'sample_sizes',
        'stts', 'stss', 'ctts',
        'elst_media_time',
        'raw_stbl_tables',
        '_cum_samples',
        '_cum_dts',
        '_cum_sizes',
    )

    def __init__(self) -> None:
        self.track_id = 0
        self.handler = b''
        self.timescale = 0
        self.duration_ticks = 0
        self.chunk_offsets: List[int] = []
        self.chunk_samples: List[int] = []
        self.sample_count = 0
        self.sample_size_const = 0
        self.sample_sizes: Optional[List[int]] = None
        self.stts: List[Tuple[int, int]] = []          # (count, delta)
        self.stss: Optional[List[int]] = None          # 1-based sample number
        self.ctts: Optional[List[Tuple[int, int]]] = None
        self.elst_media_time: Optional[int] = None
        self.raw_stbl_tables: Dict[bytes, bytes] = {}
        self._cum_samples: Optional[List[int]] = None
        self._cum_dts: Optional[List[int]] = None
        self._cum_sizes: Optional[List[int]] = None

    # ---- 时间轴 ----

    def total_ticks(self) -> int:
        return sum(c * d for c, d in self.stts)

    def dts_of_sample(self, index: int) -> int:
        """样本 index 的 DTS (单位: timescale)"""
        return self.ticks_for_samples(index)

    def duration_s(self) -> float:
        if not self.timescale:
            return 0.0
        return self.total_ticks() / float(self.timescale)

    def ticks_for_samples(self, n_samples: int) -> int:
        """前 n_samples 个样本的总时长 (ticks)"""
        if n_samples <= 0:
            return 0
        remaining = n_samples
        acc = 0
        for count, delta in self.stts:
            take = count if count < remaining else remaining
            acc += take * delta
            remaining -= take
            if remaining <= 0:
                break
        return acc

    # ---- 样本 / chunk ----

    def sample_size_at(self, index: int) -> int:
        if self.sample_size_const:
            return self.sample_size_const
        if self.sample_sizes is None:
            return 0
        if index < 0 or index >= len(self.sample_sizes):
            return 0
        return self.sample_sizes[index]

    def _cum_sizes_list(self) -> List[int]:
        if self._cum_sizes is None:
            cum = [0]
            acc = 0
            if self.sample_size_const:
                n = self.sample_count
                # 等差数列, 直接生成
                cum.extend(self.sample_size_const * (i + 1) for i in range(n))
            else:
                for s in (self.sample_sizes or ()):
                    acc += s
                    cum.append(acc)
            self._cum_sizes = cum
        return self._cum_sizes

    def chunk_size(self, index: int) -> int:
        """第 index 个 chunk 的字节数 (该 chunk 全部样本大小之和)"""
        first = self.first_sample_of_chunk(index)
        count = self.chunk_samples[index] if index < len(self.chunk_samples) else 0
        if count <= 0:
            return 0
        if self.sample_size_const:
            return count * self.sample_size_const
        cum = self._cum_sizes_list()
        last = first + count
        if last >= len(cum):
            last = len(cum) - 1
        return cum[last] - cum[first]

    def first_sample_of_chunk(self, index: int) -> int:
        if self._cum_samples is None:
            cum = [0]
            acc = 0
            for c in self.chunk_samples:
                acc += c
                cum.append(acc)
            self._cum_samples = cum
        if index < 0:
            return 0
        if index >= len(self._cum_samples) - 1:
            return self._cum_samples[-1]
        return self._cum_samples[index]

    def chunk_starts_with_sync(self, index: int) -> bool:
        if self.stss is None:
            return True                     # 没有 stss 表示全部样本都是随机访问点
        if not self.stss:
            return False
        first = self.first_sample_of_chunk(index)
        # stss 是 1-based 升序列表
        lo, hi = 0, len(self.stss) - 1
        if self.stss[0] > first + 1:
            return False
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.stss[mid] <= first + 1:
                lo = mid
            else:
                hi = mid - 1
        return self.stss[lo] == first + 1

    def chunk_table(self) -> List[Chunk]:
        out: List[Chunk] = []
        if self._cum_samples is None:
            self.first_sample_of_chunk(0)
        assert self._cum_samples is not None
        n = min(len(self.chunk_offsets), len(self.chunk_samples))
        for i in range(n):
            out.append(Chunk(
                index=i,
                offset=self.chunk_offsets[i],
                size=self.chunk_size(i),
                first_sample=self._cum_samples[i],
                sample_count=self.chunk_samples[i],
                starts_with_sync=self.chunk_starts_with_sync(i),
            ))
        return out

    def validate(self) -> List[str]:
        """返回该轨道的结构问题列表 (空 = 一致)"""
        reasons: List[str] = []
        if not self.chunk_offsets:
            return reasons
        if len(self.chunk_offsets) != len(self.chunk_samples):
            reasons.append(
                f'track {self.track_id}: stco/co64 条目数 {len(self.chunk_offsets)} '
                f'与 stsc 展开的 chunk 数 {len(self.chunk_samples)} 不一致')
        total_from_stsc = sum(self.chunk_samples)
        if self.sample_count and total_from_stsc != self.sample_count:
            reasons.append(
                f'track {self.track_id}: stsc 展开样本数 {total_from_stsc} '
                f'与 stsz/stz2 的 sample_count {self.sample_count} 不一致')
        if self.stts:
            total_from_stts = sum(c for c, _ in self.stts)
            if self.sample_count and total_from_stts != self.sample_count:
                reasons.append(
                    f'track {self.track_id}: stts 样本数 {total_from_stts} '
                    f'与 stsz/stz2 的 sample_count {self.sample_count} 不一致')
        if not self.sample_size_const and self.sample_sizes is not None:
            if self.sample_count and len(self.sample_sizes) != self.sample_count:
                reasons.append(
                    f'track {self.track_id}: 逐样本大小表长度 {len(self.sample_sizes)} '
                    f'与 sample_count {self.sample_count} 不一致')
        if self.stss:
            if self.stss[-1] > self.sample_count:
                reasons.append(
                    f'track {self.track_id}: stss 最后一项 {self.stss[-1]} '
                    f'超出 sample_count {self.sample_count}')
        return reasons


@dataclass
class Moov:
    """解析后的 moov"""
    raw: bytes                                  # 原始 moov 字节
    mvhd_timescale: int = 0
    mvhd_duration: int = 0
    tracks: List[Track] = field(default_factory=list)
    mdat: Optional[Box] = None
    ftyp: Optional[Box] = None
    fragmented: bool = False
    has_edts: bool = False

    def duration_s(self) -> float:
        if self.mvhd_timescale and self.mvhd_duration:
            return self.mvhd_duration / float(self.mvhd_timescale)
        best = 0.0
        for t in self.tracks:
            best = max(best, t.duration_s())
        return best

    def max_chunk_end(self) -> int:
        end = 0
        for t in self.tracks:
            for i in range(min(len(t.chunk_offsets), len(t.chunk_samples))):
                e = t.chunk_offsets[i] + t.chunk_size(i)
                if e > end:
                    end = e
        return end

    def find_track(self, track_id: int) -> Optional[Track]:
        for t in self.tracks:
            if t.track_id == track_id:
                return t
        return None


# ---- moov 内部各 box 的字段解析 ----

def _parse_tkhd(payload: bytes) -> Tuple[int, int]:
    """返回 (track_id, duration)"""
    version = payload[0] if payload else 0
    if version == 1:
        tid = _u(payload, 20, 4)
        dur = _u(payload, 28, 8)
    else:
        tid = _u(payload, 12, 4)
        dur = _u(payload, 20, 4)
    return (tid if tid >= 0 else 0), (dur if dur >= 0 else 0)


def _parse_mdhd(payload: bytes) -> Tuple[int, int]:
    """返回 (timescale, duration)"""
    version = payload[0] if payload else 0
    if version == 1:
        ts = _u(payload, 20, 4)
        dur = _u(payload, 24, 8)
    else:
        ts = _u(payload, 12, 4)
        dur = _u(payload, 16, 4)
    return (ts if ts > 0 else 0), (dur if dur >= 0 else 0)


def _parse_hdlr(payload: bytes) -> bytes:
    h = payload[8:12] if len(payload) >= 12 else b''
    return h


def _parse_stts(payload: bytes) -> List[Tuple[int, int]]:
    n = _u(payload, 4, 4)
    if n < 0 or n > MAX_CHUNK_ENTRIES:
        return []
    out: List[Tuple[int, int]] = []
    off = 8
    for _ in range(n):
        c = _u(payload, off, 4)
        d = _u(payload, off + 4, 4)
        if c < 0 or d < 0:
            break
        out.append((c, d))
        off += 8
    return out


def _parse_stsc(payload: bytes) -> List[Tuple[int, int, int]]:
    n = _u(payload, 4, 4)
    if n < 0 or n > MAX_CHUNK_ENTRIES:
        return []
    out: List[Tuple[int, int, int]] = []
    off = 8
    for _ in range(n):
        fc = _u(payload, off, 4)
        spc = _u(payload, off + 4, 4)
        sdi = _u(payload, off + 8, 4)
        if fc < 0 or spc < 0:
            break
        out.append((fc, spc, sdi))
        off += 12
    return out


def _parse_stsz(payload: bytes) -> Tuple[int, int, Optional[List[int]]]:
    """返回 (sample_size_const, sample_count, sizes_or_None)"""
    const = _u(payload, 4, 4)
    count = _u(payload, 8, 4)
    if count < 0 or count > MAX_SAMPLE_COUNT:
        return 0, 0, None
    if const:
        return const, count, None
    off = 12
    sizes: List[int] = []
    for _ in range(count):
        v = _u(payload, off, 4)
        if v < 0:
            break
        sizes.append(v)
        off += 4
    return 0, count, sizes


def _parse_stz2(payload: bytes) -> Tuple[int, int, Optional[List[int]]]:
    """stz2: reserved(3B)+field_size(1B)"""
    field_size = payload[7] if len(payload) > 7 else 0
    count = _u(payload, 8, 4)
    if count < 0 or count > MAX_SAMPLE_COUNT:
        return 0, 0, None
    sizes: List[int] = []
    if field_size == 16:
        off = 12
        for _ in range(count):
            v = _u(payload, off, 2)
            if v < 0:
                break
            sizes.append(v)
            off += 2
    elif field_size == 8:
        off = 12
        body = payload[off:off + count]
        sizes = list(body)
    elif field_size == 4:
        off = 12
        body = payload[off:off + (count + 1) // 2]
        for i in range(count):
            byte = body[i // 2] if i // 2 < len(body) else 0
            sizes.append((byte >> 4) if (i % 2 == 0) else (byte & 0x0F))
    else:
        return 0, count, None
    if len(sizes) < count:
        return 0, count, sizes
    # 全部相同时退化为常量, 省内存
    if sizes and all(s == sizes[0] for s in sizes):
        return sizes[0], count, None
    return 0, count, sizes


def _parse_stco(payload: bytes, wide: bool) -> List[int]:
    n = _u(payload, 4, 4)
    if n < 0 or n > MAX_CHUNK_ENTRIES:
        return []
    out: List[int] = []
    off = 8
    step = 8 if wide else 4
    for _ in range(n):
        v = _u(payload, off, step)
        if v < 0:
            break
        out.append(v)
        off += step
    return out


def _parse_stss(payload: bytes) -> List[int]:
    n = _u(payload, 4, 4)
    if n < 0 or n > MAX_SAMPLE_COUNT:
        return []
    out: List[int] = []
    off = 8
    for _ in range(n):
        v = _u(payload, off, 4)
        if v < 0:
            break
        out.append(v)
        off += 4
    return out


def _parse_ctts(payload: bytes) -> List[Tuple[int, int]]:
    n = _u(payload, 4, 4)
    if n < 0 or n > MAX_CHUNK_ENTRIES:
        return []
    out: List[Tuple[int, int]] = []
    off = 8
    for _ in range(n):
        c = _u(payload, off, 4)
        raw = payload[off + 4:off + 8]
        if len(raw) < 4:
            break
        v = int.from_bytes(raw, 'big', signed=False)
        if v >= 0x80000000:
            v -= 0x100000000          # ctts v2 为有符号偏移
        out.append((c if c >= 0 else 0, v))
        off += 8
    return out


def _parse_elst_media_time(payload: bytes) -> Optional[int]:
    version = payload[0] if payload else 0
    count = _u(payload, 4, 4)
    if count is None or count <= 0:
        return None
    if version == 1:
        mt = payload[16:24]
        if len(mt) < 8:
            return None
        v = int.from_bytes(mt, 'big', signed=True)
    else:
        mt = payload[8:12]
        if len(mt) < 4:
            return None
        v = int.from_bytes(mt, 'big', signed=True)
    return v if v >= 0 else None          # -1 表示空编辑, 视为无偏移


def _expand_stsc(entries: List[Tuple[int, int, int]], chunk_count: int) -> List[int]:
    """把 stsc 展开为"每个 chunk 的样本数"列表"""
    out = [0] * chunk_count
    for i, (first_chunk, spc, _sdi) in enumerate(entries):
        if first_chunk <= 0:
            continue
        lo = first_chunk - 1
        hi = (entries[i + 1][0] - 1) if (i + 1 < len(entries)) else chunk_count
        if hi > chunk_count:
            hi = chunk_count
        if lo >= chunk_count:
            break
        for c in range(lo, hi):
            out[c] = spc
    return out


def _iter_trak(reader: Reader, trak: Box) -> Track:
    t = Track()
    problems: List[str] = []

    tkhd = find_box(reader, (b'tkhd',), trak.payload_start, trak.end)
    if tkhd is not None:
        t.track_id, t.duration_ticks = _parse_tkhd(reader.read(tkhd.payload_start, tkhd.payload_size))

    mdhd = find_box(reader, (b'mdia', b'mdhd'), trak.payload_start, trak.end)
    if mdhd is not None:
        t.timescale, t.duration_ticks = _parse_mdhd(
            reader.read(mdhd.payload_start, mdhd.payload_size))

    hdlr = find_box(reader, (b'mdia', b'hdlr'), trak.payload_start, trak.end)
    if hdlr is not None:
        t.handler = _parse_hdlr(reader.read(hdlr.payload_start, hdlr.payload_size))

    stbl_start, stbl_end = trak.payload_start, trak.end
    stbl = find_box(reader, (b'mdia', b'minf', b'stbl'), trak.payload_start, trak.end)
    if stbl is not None:
        stbl_start, stbl_end = stbl.payload_start, stbl.end

    for b in iter_boxes(reader, stbl_start, stbl_end, problems):
        payload = reader.read(b.payload_start, b.payload_size)
        if b.type in (b'stco', b'co64'):
            t.raw_stbl_tables[b.type] = payload
            t.chunk_offsets = _parse_stco(payload, wide=(b.type == b'co64'))
        elif b.type == b'stsz':
            t.raw_stbl_tables[b.type] = payload
            t.sample_size_const, t.sample_count, t.sample_sizes = _parse_stsz(payload)
        elif b.type == b'stz2':
            t.raw_stbl_tables[b.type] = payload
            t.sample_size_const, t.sample_count, t.sample_sizes = _parse_stz2(payload)
        elif b.type == b'stsc':
            t.raw_stbl_tables[b.type] = payload
            entries = _parse_stsc(payload)
            n_chunks = len(t.chunk_offsets)
            t.chunk_samples = _expand_stsc(entries, n_chunks) if n_chunks else []
            if not n_chunks and entries:
                # stco 还没读到 (顺序异常), 稍后再展开
                t.raw_stbl_tables[b'__stsc_entries'] = payload
        elif b.type == b'stts':
            t.raw_stbl_tables[b.type] = payload
            t.stts = _parse_stts(payload)
        elif b.type == b'stss':
            t.raw_stbl_tables[b.type] = payload
            t.stss = _parse_stss(payload)
        elif b.type == b'ctts':
            t.raw_stbl_tables[b.type] = payload
            t.ctts = _parse_ctts(payload)

    # stco 在 stsc 之后才读到的兜底
    if not t.chunk_samples and t.chunk_offsets and b'__stsc_entries' in t.raw_stbl_tables:
        t.chunk_samples = _expand_stsc(
            _parse_stsc(t.raw_stbl_tables[b'__stsc_entries']), len(t.chunk_offsets))

    edts = find_box(reader, (b'edts', b'elst'), trak.payload_start, trak.end)
    if edts is not None:
        t.elst_media_time = _parse_elst_media_time(
            reader.read(edts.payload_start, edts.payload_size))

    return t


def parse_moov(reader: Reader,
               fatal: Optional[List[str]] = None,
               notes: Optional[List[str]] = None,
               lenient: bool = False) -> Optional[Moov]:
    """
    从 Reader 中定位并解析 moov。找不到 (或结构非法) 返回 None。

    同时填充 Moov.mdat / Moov.ftyp / Moov.fragmented。

    lenient=True 时, 顶层 box 链允许最后一个 box 的声明大小超出可见范围
    (见 iter_boxes 的 allow_truncated_tail)。重建大视频时, 我们只持有文件前缀
    的稀疏视图, 但 mdat 声明的大小覆盖整个文件 —— 此时仍需要知道 mdat 的位置。
    """
    size = reader.size()
    boxes = list(iter_boxes(reader, 0, size, fatal, notes,
                            allow_truncated_tail=lenient))

    ftyp = next((b for b in boxes if b.type == b'ftyp'), None)
    mdat = next((b for b in boxes if b.type == b'mdat'), None)
    moov_box = next((b for b in boxes if b.type == b'moov'), None)

    if moov_box is None:
        _note(fatal, 'moov box not found at top level')
        return None

    raw = reader.read(moov_box.start, moov_box.size)
    moov = Moov(raw=raw, mdat=mdat, ftyp=ftyp)
    moov.fragmented = any(b.type == b'moof' for b in boxes)

    mvhd = find_box(reader, (b'moov', b'mvhd'), 0, size)
    if mvhd is not None:
        p = reader.read(mvhd.payload_start, mvhd.payload_size)
        version = p[0] if p else 0
        if version == 1:
            moov.mvhd_timescale = _u(p, 20, 4)
            moov.mvhd_duration = _u(p, 24, 8)
        else:
            moov.mvhd_timescale = _u(p, 12, 4)
            moov.mvhd_duration = _u(p, 16, 4)
        if moov.mvhd_timescale < 0:
            moov.mvhd_timescale = 0
        if moov.mvhd_duration < 0:
            moov.mvhd_duration = 0

    # mvex 表示 fragmented
    if find_box(reader, (b'moov', b'mvex'), 0, size) is not None:
        moov.fragmented = True

    for b in iter_boxes(reader, moov_box.payload_start, moov_box.end, fatal, notes):
        if b.type == b'trak':
            trak = _iter_trak(reader, b)
            moov.tracks.append(trak)
            if find_box(reader, (b'edts',), b.payload_start, b.end) is not None:
                moov.has_edts = True

    return moov


def parse_moov_bytes(moov_bytes: bytes) -> Optional[Moov]:
    """从内存中的 moov 字节解析 (raw 会是传入的字节)"""
    return parse_moov(Reader(moov_bytes))


# ==================== 派生查询 ====================

def chunk_table(track: Track) -> List[Chunk]:
    return track.chunk_table()


def required_ranges(moov: Moov, merge: bool = True) -> List[Tuple[int, int]]:
    """
    完整播放该 moov 所需的字节区间 (所有轨道所有 chunk 的并集)。

    这是"完整性"的唯一权威定义: coverage ⊇ required ⇒ 完整。
    """
    spans: List[Tuple[int, int]] = []
    for t in moov.tracks:
        n = min(len(t.chunk_offsets), len(t.chunk_samples))
        for i in range(n):
            off = t.chunk_offsets[i]
            size = t.chunk_size(i)
            if size > 0:
                spans.append((off, off + size))
    if not merge or not spans:
        return spans
    spans.sort()
    out: List[Tuple[int, int]] = [spans[0]]
    for lo, hi in spans[1:]:
        plo, phi = out[-1]
        if lo <= phi:                      # 相邻或重叠都合并
            if hi > phi:
                out[-1] = (plo, hi)
        else:
            out.append((lo, hi))
    return out


# ==================== 前缀计划与表格截断 ====================

@dataclass
class PrefixPlan:
    """"只保留一段连续前缀"的重建计划"""
    p: int                                      # 实际写出的前缀字节数
    per_track_keep: Dict[int, int] = field(default_factory=dict)   # track_id -> 保留的 chunk 数
    samples_kept: Dict[int, int] = field(default_factory=dict)     # track_id -> 保留的样本数
    track_durations: Dict[int, float] = field(default_factory=dict)
    duration_s: float = 0.0
    movie_duration_ticks: int = 0
    truncated: bool = False
    reasons: List[str] = field(default_factory=list)


def plan_prefix(moov: Moov,
                is_covered: Callable[[int, int], bool],
                size_limit: Optional[int] = None) -> PrefixPlan:
    """
    在"只写出一段连续覆盖前缀"的前提下, 计算每条轨道可保留的 chunk 数。

    保留规则: 从 chunk 0 起, 只要 [offset, offset+size) 被完全覆盖就保留;
    遇到第一个未完全覆盖的 chunk 立刻停止 (因为后续 chunk 的字节不在写出的前缀里)。

    返回的 plan.truncated 表示是否真的发生了截断; 未截断时应保持 moov 原样不动。

    Args:
        moov: 解析后的 moov
        is_covered: (lo, hi) -> bool, 判定字节区间是否被完整覆盖
        size_limit: 可选的真实文件大小上限
    """
    plan = PrefixPlan(p=0)
    max_end = 0
    any_truncated = False

    for t in moov.tracks:
        n = min(len(t.chunk_offsets), len(t.chunk_samples))
        keep = 0
        for i in range(n):
            off = t.chunk_offsets[i]
            size = t.chunk_size(i)
            if size > 0:
                if not is_covered(off, off + size):
                    break
                if off + size > max_end:
                    max_end = off + size
            keep = i + 1
        plan.per_track_keep[t.track_id] = keep
        plan.samples_kept[t.track_id] = sum(t.chunk_samples[:keep])
        ticks = t.ticks_for_samples(plan.samples_kept[t.track_id])
        dur = (ticks / float(t.timescale)) if t.timescale else 0.0
        plan.track_durations[t.track_id] = dur
        if keep < n:
            any_truncated = True

    # 实际写出的前缀 = 最后一个被引用的字节 (不写没被任何样本引用的尾巴)
    plan.p = max_end
    if size_limit and size_limit > 0 and (plan.p <= 0 or plan.p > size_limit):
        plan.p = min(max_end, size_limit) if max_end > 0 else 0

    plan.truncated = any_truncated
    plan.duration_s = max(plan.track_durations.values()) if plan.track_durations else moov.duration_s()

    ts = moov.mvhd_timescale or 1000
    plan.movie_duration_ticks = int(round(plan.duration_s * ts))

    if not any(t.chunk_offsets for t in moov.tracks):
        plan.reasons.append('moov 中没有任何 chunk (可能是 fragmented 或空轨道)')
    empty = [tid for tid, k in plan.per_track_keep.items() if k == 0]
    if empty:
        plan.reasons.append(f'以下轨道在前缀内没有任何完整 chunk, 将被置空: {empty}')

    return plan


# ---- moov 树重写 ----

@dataclass
class _Node:
    type: bytes
    payload: bytes = b''
    children: Optional[List['_Node']] = None
    extra_header: bytes = b''            # uuid 的 16 字节 usertype

    def is_container(self) -> bool:
        return self.children is not None


def _parse_tree(reader: Reader, start: int, end: int) -> List['_Node']:
    """
    解析 [start, end) 内的 box 链为可重写的节点树。

    只应作用于 moov 的字节 (moov 里不含 mdat, 所以不会把大块媒体读进内存)。

    关键安全性: 任何一层只要不能**精确覆盖**其容器区间 (存在无法解析的尾部、
    或 box 结构非法), 该层就整体降级为叶子节点并保留原始字节。
    这保证重写过程永远不会凭空丢弃无法理解的字节。
    """
    nodes: List['_Node'] = []
    for b in iter_boxes(reader, start, end):
        extra = reader.read(b.payload_start, 16) if b.type == b'uuid' else b''
        payload_off = b.payload_start + (16 if b.type == b'uuid' else 0)
        kids: Optional[List['_Node']] = None
        if b.type in CONTAINER_TYPES:
            kids = _parse_children(reader, payload_off, b.end)
        if kids is None:
            nodes.append(_Node(
                type=b.type,
                payload=reader.read(payload_off, max(0, b.end - payload_off)),
                extra_header=extra,
            ))
        else:
            nodes.append(_Node(type=b.type, children=kids, extra_header=extra))
    return nodes


def _parse_children(reader: Reader, start: int, end: int) -> Optional[List['_Node']]:
    """
    解析容器的子 box 列表。

    返回 None 表示"这一层不可安全重写", 调用方应降级为叶子节点。
    触发条件: box 结构非法 / 存在残留尾部字节 / 一个子 box 都没有。
    """
    fatal: List[str] = []
    notes: List[str] = []
    kids: List['_Node'] = []
    pos = start
    for b in iter_boxes(reader, start, end, fatal, notes):
        extra = reader.read(b.payload_start, 16) if b.type == b'uuid' else b''
        payload_off = b.payload_start + (16 if b.type == b'uuid' else 0)
        sub: Optional[List['_Node']] = None
        if b.type in CONTAINER_TYPES:
            sub = _parse_children(reader, payload_off, b.end)
        if sub is None:
            kids.append(_Node(type=b.type,
                              payload=reader.read(payload_off, max(0, b.end - payload_off)),
                              extra_header=extra))
        else:
            kids.append(_Node(type=b.type, children=sub, extra_header=extra))
        pos = b.end
    if fatal or not kids or pos != end:
        return None
    return kids


def _serialize_tree(nodes: Iterable['_Node']) -> bytes:
    out = bytearray()
    for n in nodes:
        if n.is_container():
            body = _serialize_tree(n.children or [])
        else:
            body = n.payload
        header_len = 8 + len(n.extra_header)
        total = header_len + len(body)
        if total > 0xFFFFFFFF:
            out += (1).to_bytes(4, 'big') + n.type + total.to_bytes(8, 'big')
        else:
            out += total.to_bytes(4, 'big') + n.type
        out += n.extra_header
        out += body
    return bytes(out)


def _find_child(node: _Node, btype: bytes) -> Optional[_Node]:
    for c in (node.children or ()):
        if c.type == btype:
            return c
    return None


def _path_find(nodes: List[_Node], path: Sequence[bytes]) -> Optional[_Node]:
    cur: Optional[_Node] = None
    level = nodes
    for want in path:
        cur = None
        for n in level:
            if n.type == want:
                cur = n
                break
        if cur is None:
            return None
        level = cur.children or []
    return cur


def _find_by_type(nodes: List[_Node], btype: bytes) -> Optional[_Node]:
    for n in nodes:
        if n.type == btype:
            return n
    return None


def _count_to_counts_list(counts: List[Tuple[int, int]]) -> List[int]:
    out: List[int] = []
    for c, _d in counts:
        if c > 0:
            out.extend([_d] * c)
    return out


def _compress_deltas(deltas: Sequence[int]) -> List[Tuple[int, int]]:
    """把逐样本 delta 序列压回 (count, delta) 的 stts 形式"""
    out: List[Tuple[int, int]] = []
    for d in deltas:
        if out and out[-1][1] == d:
            out[-1] = (out[-1][0] + 1, d)
        else:
            out.append((1, d))
    return out


def _enc_table_box(btype: bytes, entries: Sequence[int], wide: bool, version: int = 0) -> bytes:
    """编码 stco/co64 的 payload"""
    body = bytearray()
    body += bytes([version, 0, 0, 0])
    body += len(entries).to_bytes(4, 'big')
    step = 8 if wide else 4
    for v in entries:
        body += int(v).to_bytes(step, 'big')
    return bytes(body)


def truncate_tables(moov_bytes: bytes, plan: PrefixPlan) -> bytes:
    """
    按计划截断 moov 内的样本表, 并修补各层时长。

    只做两件事, 完全不做偏移运算 (因为 mdat 位置不变):
      1. stco/co64、stsc、stsz/stz2、stts、stss、ctts 截断到保留的样本
      2. mvhd / tkhd / mdhd 的 duration 重算

    若 plan.truncated 为 False, 原样返回 (保持字节级一致)。

    `edts/elst` 在发生截断时会被移除: 编辑表引用的是原始时间轴, 保留它会在
    开头引入一个偏移; 后续 ffmpeg 重封装时会依据实际时间戳重新生成。
    """
    if not plan.truncated:
        return moov_bytes

    reader = Reader(moov_bytes)
    nodes = _parse_tree(reader, 0, len(moov_bytes))
    moov_node = _find_by_type(nodes, b'moov')
    if moov_node is None or moov_node.children is None:
        return moov_bytes

    ts_movie = 0
    mvhd = _find_child(moov_node, b'mvhd')

    for trak in moov_node.children:
        if trak.type != b'trak' or trak.children is None:
            continue
        tkhd = _find_child(trak, b'tkhd')
        track_id = 0
        if tkhd is not None:
            track_id, _ = _parse_tkhd(tkhd.payload)
        keep = plan.per_track_keep.get(track_id, 0)

        # 移除 edts (见函数文档)
        trak.children = [c for c in trak.children if c.type != b'edts']

        stbl = _path_find(trak.children, (b'mdia', b'minf', b'stbl'))
        if stbl is None or stbl.children is None:
            continue

        mdhd = _path_find(trak.children, (b'mdia', b'mdhd'))
        mdhd_timescale = 0
        if mdhd is not None:
            mdhd_timescale, _ = _parse_mdhd(mdhd.payload)

        # --- 先取出各表当前值 ---
        stco_node = _find_child(stbl, b'stco')
        co64_node = _find_child(stbl, b'co64')
        stsc_node = _find_child(stbl, b'stsc')
        stsz_node = _find_child(stbl, b'stsz')
        stz2_node = _find_child(stbl, b'stz2')
        stts_node = _find_child(stbl, b'stts')
        stss_node = _find_child(stbl, b'stss')
        ctts_node = _find_child(stbl, b'ctts')

        offsets = _parse_stco((co64_node or stco_node).payload if (co64_node or stco_node) else b'',
                              wide=co64_node is not None)
        entries = _parse_stsc(stsc_node.payload) if stsc_node else []
        per_chunk = _expand_stsc(entries, len(offsets)) if offsets else []
        const, sample_count, sizes = _parse_stsz(stsz_node.payload) if stsz_node else (0, 0, None)
        if stz2_node is not None:
            const, sample_count, sizes = _parse_stz2(stz2_node.payload)

        # 逐样本大小表若解析失败, 无法安全截断 —— 放弃重写, 保持原样
        if not const and sizes is None and (stsz_node is not None or stz2_node is not None):
            return moov_bytes
        stts = _parse_stts(stts_node.payload) if stts_node else []
        deltas = _count_to_counts_list(stts)

        keep_chunks = min(keep, len(offsets), len(per_chunk)) if offsets else 0
        kept_samples = sum(per_chunk[:keep_chunks])

        # --- stco / co64 ---
        new_offsets = offsets[:keep_chunks]
        if stco_node is not None:
            stco_node.payload = _enc_table_box(b'stco', new_offsets, wide=False)
        if co64_node is not None:
            co64_node.payload = _enc_table_box(b'co64', new_offsets, wide=True)

        # --- stsc: 重算为以 chunk 1 开头的紧凑条目 ---
        if stsc_node is not None:
            runs: List[Tuple[int, int]] = []
            for idx in range(keep_chunks):
                spc = per_chunk[idx]
                if runs and runs[-1][1] == spc:
                    continue
                runs.append((idx + 1, spc))
            body = bytearray()
            body += bytes([0, 0, 0, 0])
            body += len(runs).to_bytes(4, 'big')
            for fc, spc in runs:
                body += int(fc).to_bytes(4, 'big')
                body += int(spc).to_bytes(4, 'big')
                body += (1).to_bytes(4, 'big')       # sample_description_index
            stsc_node.payload = bytes(body)

        # --- stsz / stz2 ---
        if stsz_node is not None and not const and sizes is not None:
            body = bytearray()
            body += bytes([0, 0, 0, 0])
            body += (0).to_bytes(4, 'big')
            body += kept_samples.to_bytes(4, 'big')
            for s in sizes[:kept_samples]:
                body += int(s).to_bytes(4, 'big')
            stsz_node.payload = bytes(body)
        elif stsz_node is not None:
            body = bytearray()
            body += bytes([0, 0, 0, 0])
            body += int(const).to_bytes(4, 'big')
            body += kept_samples.to_bytes(4, 'big')
            stsz_node.payload = bytes(body)
        elif stz2_node is not None and sizes is not None:
            body = bytearray()
            body += bytes([0, 0, 0, 0])
            body += bytes([0, 0, 0, 16])
            body += kept_samples.to_bytes(4, 'big')
            for s in sizes[:kept_samples]:
                body += int(s).to_bytes(2, 'big')
            stz2_node.payload = bytes(body)

        # --- stts ---
        new_deltas = deltas[:kept_samples]
        if stts_node is not None:
            compressed = _compress_deltas(new_deltas)
            body = bytearray()
            body += bytes([0, 0, 0, 0])
            body += len(compressed).to_bytes(4, 'big')
            for c, d in compressed:
                body += int(c).to_bytes(4, 'big')
                body += int(d).to_bytes(4, 'big')
            stts_node.payload = bytes(body)

        # --- stss (1-based 样本号, 只保留 <= kept_samples 的) ---
        if stss_node is not None:
            sync = _parse_stss(stss_node.payload)
            sync = [s for s in sync if 1 <= s <= kept_samples]
            body = bytearray()
            body += bytes([0, 0, 0, 0])
            body += len(sync).to_bytes(4, 'big')
            for s in sync:
                body += int(s).to_bytes(4, 'big')
            stss_node.payload = bytes(body)

        # --- ctts ---
        if ctts_node is not None:
            ctts = _parse_ctts(ctts_node.payload)
            flat = _count_to_counts_list([(c, v) for c, v in ctts])
            flat = flat[:kept_samples]
            runs2: List[Tuple[int, int]] = []
            for v in flat:
                if runs2 and runs2[-1][1] == v:
                    runs2[-1] = (runs2[-1][0] + 1, v)
                else:
                    runs2.append((1, v))
            body = bytearray()
            body += bytes([0, 0, 0, 0])
            body += len(runs2).to_bytes(4, 'big')
            for c, v in runs2:
                body += int(c).to_bytes(4, 'big')
                body += (int(v) & 0xFFFFFFFF).to_bytes(4, 'big')
            ctts_node.payload = bytes(body)

        # --- 轨道时长 (mdhd 用媒体时间轴) ---
        kept_ticks = sum(new_deltas) if new_deltas else 0
        if mdhd is not None:
            mdhd.payload = _rewrite_mdhd_duration(mdhd.payload, kept_ticks)

    # mvhd + tkhd 用电影时间轴
    if mvhd is not None:
        ts_movie = _parse_mvhd_timescale(mvhd.payload)
    for trak in moov_node.children:
        if trak.type != b'trak' or trak.children is None:
            continue
        tkhd = _find_child(trak, b'tkhd')
        if tkhd is None or not ts_movie:
            continue
        tid, _ = _parse_tkhd(tkhd.payload)
        dur_s = plan.track_durations.get(tid, 0.0)
        tkhd.payload = _rewrite_tkhd_duration(tkhd.payload, int(round(dur_s * ts_movie)))
    if mvhd is not None:
        mvhd.payload = _rewrite_mvhd_duration(mvhd.payload, plan.movie_duration_ticks)

    return _serialize_tree(nodes)


# ---- 时长字段就地改写 ----

def _parse_mvhd_timescale(payload: bytes) -> int:
    version = payload[0] if payload else 0
    ts = _u(payload, 20, 4) if version == 1 else _u(payload, 12, 4)
    return ts if ts and ts > 0 else 0


def _put_u32(payload: bytes, off: int, value: int) -> bytes:
    if off < 0 or off + 4 > len(payload):
        return payload
    b = bytearray(payload)
    b[off:off + 4] = (int(value) & 0xFFFFFFFF).to_bytes(4, 'big')
    return bytes(b)


def _put_u64(payload: bytes, off: int, value: int) -> bytes:
    if off < 0 or off + 8 > len(payload):
        return payload
    b = bytearray(payload)
    b[off:off + 8] = (int(value) & 0xFFFFFFFFFFFFFFFF).to_bytes(8, 'big')
    return bytes(b)


def _rewrite_mvhd_duration(payload: bytes, ticks: int) -> bytes:
    version = payload[0] if payload else 0
    if version == 1:
        return _put_u64(payload, 24, ticks)
    return _put_u32(payload, 16, ticks)


def _rewrite_tkhd_duration(payload: bytes, ticks: int) -> bytes:
    version = payload[0] if payload else 0
    if version == 1:
        return _put_u64(payload, 28, ticks)
    return _put_u32(payload, 20, ticks)


def _rewrite_mdhd_duration(payload: bytes, ticks: int) -> bytes:
    version = payload[0] if payload else 0
    if version == 1:
        return _put_u64(payload, 24, ticks)
    return _put_u32(payload, 16, ticks)


def patch_mdat_size(buf: bytearray, box: Box, new_size: int) -> None:
    """
    把 mdat box 的声明大小改写为 new_size (含 header)。

    就地修改 buf。支持 32 位 size 与 64 位 largesize 两种写法。
    """
    if new_size < box.header:
        return
    if box.header == 16:
        # 已经是 largesize
        buf[box.start:box.start + 4] = (1).to_bytes(4, 'big')
        buf[box.start + 8:box.start + 16] = new_size.to_bytes(8, 'big')
        return
    if new_size > 0xFFFFFFFF:
        return                      # 无法用 32 位表达 (需要整体改成 largesize)
    buf[box.start:box.start + 4] = new_size.to_bytes(4, 'big')


# ==================== 校验 ====================

@dataclass
class ValidationResult:
    ok: bool = False
    reasons: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    duration_s: float = 0.0
    file_size: int = 0
    max_chunk_end: int = 0
    has_moov: bool = False
    has_mdat: bool = False
    track_count: int = 0
    fragmented: bool = False

    def summary(self) -> str:
        if self.ok:
            return f'ok, duration={self.duration_s:.2f}s'
        return 'invalid: ' + '; '.join(self.reasons[:4])


def validate(src) -> ValidationResult:
    """
    校验一个 MP4 是否结构自洽、且所有 chunk 都落在文件范围内。

    这是 `stco_exceeds_file` / `_is_valid_mp4_export` 的正确替代:
      - "没有 moov" 是**失败**, 而不是"没问题"
      - 不读整文件, 只读 box 头部与样本表
      - 不搜索子串
    """
    res = ValidationResult()
    reader = Reader(src)
    try:
        res.file_size = reader.size()
        if res.file_size < 16:
            res.reasons.append(f'文件太小 ({res.file_size} bytes)')
            return res

        fatal: List[str] = []
        notes: List[str] = []
        boxes = list(iter_boxes(reader, 0, res.file_size, fatal, notes))
        res.notes.extend(notes)
        if fatal:
            res.reasons.extend(fatal)

        if not boxes:
            res.reasons.append('顶层没有任何 box')
            return res
        if boxes[0].type != b'ftyp':
            res.reasons.append(f'第一个顶层 box 是 {boxes[0].type!r}, 期望 ftyp')

        res.has_mdat = any(b.type == b'mdat' for b in boxes)

        moov_box = next((b for b in boxes if b.type == b'moov'), None)
        if moov_box is None:
            res.reasons.append('缺少 moov box (无法解复用)')
            res.has_moov = False
            return res
        res.has_moov = True

        moov = parse_moov(reader)
        if moov is None:
            res.reasons.append('moov 解析失败')
            return res
        res.fragmented = moov.fragmented
        if moov.fragmented:
            res.reasons.append('fragmented MP4 (moof/mvex) 不受支持')
            return res

        res.track_count = len(moov.tracks)
        if not moov.tracks:
            res.reasons.append('moov 内没有 trak')

        for t in moov.tracks:
            res.reasons.extend(t.validate())

        # 每个 chunk 必须落在文件内 —— 这是真正的 stco 越界检测
        max_end = 0
        for t in moov.tracks:
            n = min(len(t.chunk_offsets), len(t.chunk_samples))
            for i in range(n):
                off = t.chunk_offsets[i]
                size = t.chunk_size(i)
                end = off + size
                if end > max_end:
                    max_end = end
                if end > res.file_size:
                    res.reasons.append(
                        f'track {t.track_id} chunk {i} 越界: '
                        f'{off}+{size}={end} > 文件大小 {res.file_size}')
                    break
        res.max_chunk_end = max_end

        if not res.has_mdat and max_end > 0:
            res.reasons.append('有样本数据但缺少 mdat box')

        res.duration_s = moov.duration_s()
        res.ok = not res.reasons
        return res
    finally:
        reader.close()


def stco_exceeds_size(src) -> bool:
    """
    "chunk 引用超出文件大小" 的布尔判定 (供旧调用点迁移用)。

    与旧实现的区别: 缺少 moov、moov 解析失败、fragmented 都返回 True
    (即"不可信"), 而不是错误地返回 False。
    """
    res = validate(src)
    if not res.ok:
        return True
    return res.max_chunk_end > res.file_size
