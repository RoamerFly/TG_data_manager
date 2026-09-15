"""
视频重打包与分析

两块能力:
  1. 重建 (rebuild_*) —— 把已缓存的区段重打包成一个**可播放的局部片段**
  2. 分析 (analyze)   —— 只读地算出覆盖率、缺口与可播放时长, 供扫描与详情页使用

替代 src/deserializer.rebuild_large_video 的"整片内存 + 零填充 + 尾部截断"做法。

旧做法的问题:
  1. bytearray(video_size) 把整部视频读进内存 (1.5GB 视频峰值 5-6GB)
  2. 缺失分片的位置留成 0, 然后只把**尾部**的零块截掉 —— 中部空洞原样保留
  3. 从不校验结果, 所以"满是零洞的文件"会被当成成功产物

新做法 (见 plan §3.1):
  - 覆盖率是从已缓存区段算出来的**事实**
  - 只写出从 0 开始的**连续覆盖前缀** [0, P), 逐段写、无空洞, 峰值内存 = 1 个分片
  - moov 用稀疏视图按 box 声明大小跳着定位, 无需整文件
  - 样本表按 P 截断 (src.mp4.truncate_tables), 不做任何偏移搬运
    —— 因为 mdat 起点不变, chunk 的绝对偏移依然有效

moov 摆放有两条分支 (见 _plan_moov_placement):
  - moov 在 [0, P) 之外 (非 faststart): 直接追加截断后的 moov
  - moov 在 [0, P) 之内 (faststart): 完整覆盖时原位保留; 需要截断时把原 moov 区域
    改写成等尺寸 free box, 再把截断后的 moov 追加到末尾 —— 避免出现两个 moov

仅依赖标准库 + 本项目模块。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .coverage import Coverage, K_IN_SLICE
from .deserializer import (_parse_complex_format, _parse_serialized_data,
                           media_part_extents)
from .mp4 import (
    Box, Moov, PrefixPlan, iter_boxes, parse_moov, patch_mdat_size, plan_prefix,
    required_ranges, truncate_tables,
)

__all__ = [
    'SliceSource', 'RebuildResult', 'LargeVideoAnalysis',
    'rebuild_large_video_to_file', 'repack_from_extents',
    'analyze', 'analyze_extents', 'collect_header_extents', 'serialized_extents',
    'SparseReader',
]

WRITE_BUF = 1 << 20          # 1 MiB


# ==================== 输入描述 ====================

@dataclass
class SliceSource:
    """一个 8 MiB 分片"""
    index: int
    size: int
    name: str = ''
    path: str = ''
    data: Optional[bytes] = None        # 已解密 (测试或小文件直接给)

    @property
    def lo(self) -> int:
        return self.index * K_IN_SLICE

    @property
    def hi(self) -> int:
        return self.lo + max(0, self.size)


@dataclass
class RebuildResult:
    """重建结果 —— 覆盖率与判定通过返回值离开函数, 不再"返回 bytes 然后猜" """
    out_path: str = ''
    size: int = 0
    playable: bool = False
    p: int = 0                                   # 写出的连续前缀字节数
    duration_s: float = 0.0
    total_size: int = 0                          # moov 声明的完整文件大小
    truncated: bool = False
    coverage: Optional[Coverage] = None
    per_track_keep: Dict[int, int] = field(default_factory=dict)
    samples_kept: Dict[int, int] = field(default_factory=dict)
    slice_indices: List[int] = field(default_factory=list)
    source_mtimes: Dict[str, float] = field(default_factory=dict)
    missing_blocks: List[int] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)

    def fail(self, reason: str) -> 'RebuildResult':
        self.reasons.append(reason)
        self.playable = False
        return self

    @property
    def ok(self) -> bool:
        return self.playable and not self.reasons


@dataclass
class LargeVideoAnalysis:
    """
    只读分析结果 (不解密分片、不写文件)。

    `complete` 是唯一权威的完整性判定: moov 里每个 chunk 的字节区间都被覆盖。
    """
    ok: bool = False
    error: str = ''
    moov_present: bool = False
    moov_range: Optional[Tuple[int, int]] = None
    ftyp_present: bool = False
    mdat_present: bool = False
    track_count: int = 0
    parts_count: int = 0
    has_ftyp: bool = False
    total_size: int = 0
    duration_s: float = 0.0
    coverage: Optional[Coverage] = None
    covered_prefix_end: int = 0
    required_count: int = 0
    complete: bool = False
    missing_blocks: List[int] = field(default_factory=list)
    missing_ranges: List[Tuple[int, int]] = field(default_factory=list)
    playable_prefix_bytes: int = 0
    playable_duration_s: float = 0.0
    reasons: List[str] = field(default_factory=list)

    @property
    def slices_needed(self) -> int:
        """展示用: 完整视频需要多少个 8MB 分片"""
        if self.total_size <= 0:
            return 0
        return (self.total_size + K_IN_SLICE - 1) // K_IN_SLICE


# ==================== 稀疏只读视图 ====================

class _Piece:
    """一个可用数据段 (已解密, 或需要按需解密)"""
    __slots__ = ('lo', 'hi', 'data', 'path', 'name', 'kind')

    def __init__(self, lo: int, hi: int, kind: str,
                 data: Optional[bytes] = None, path: str = '', name: str = ''):
        self.lo = lo
        self.hi = hi
        self.kind = kind
        self.data = data
        self.path = path
        self.name = name


class SparseReader:
    """
    把若干已覆盖区段组装成一个只读的稀疏视图。

    未覆盖区域读出为全 0 —— 因此调用方**必须**先用 Coverage 确认要读的范围
    确实被覆盖, 否则会读到伪造的 0 字节。

    提供与 src.mp4.Reader 相同的 size()/read() 接口, 可直接喂给 parse_moov(),
    这样无需把整个文件读进内存, 也能按 box 声明的大小跳着走到尾部的 moov。
    """

    __slots__ = ('_pieces', '_size', '_decrypt', '_cache_path', '_cache_data')

    def __init__(self, pieces: Sequence[_Piece], size: int,
                 decrypt_fn: Optional[Callable[[str], Optional[bytes]]] = None):
        # header 排在后面 = 读到的数据以 header 为准 (与 _write_prefix 一致:
        # header 是 Telegram 自己认定的文件头, 优先于外部分片里的同区段数据)
        self._pieces = sorted(pieces, key=lambda p: (p.lo, p.hi, p.kind == 'header'))
        self._size = max(0, int(size))
        self._decrypt = decrypt_fn
        self._cache_path: Optional[str] = None
        self._cache_data: Optional[bytes] = None

    def size(self) -> int:
        return self._size

    def resolve(self, pc: _Piece) -> Optional[bytes]:
        """取出该段的解密数据 (分片按需解密, 只缓存最近一个以限制内存)"""
        if pc.data is not None:
            return pc.data
        if not pc.path or self._decrypt is None:
            return None
        if self._cache_path == pc.path and self._cache_data is not None:
            return self._cache_data
        data = self._decrypt(pc.path)
        self._cache_path = pc.path
        self._cache_data = data
        return data

    def read(self, off: int, n: int) -> bytes:
        if n <= 0 or off < 0 or off >= self._size:
            return b''
        n = min(n, self._size - off)
        out = bytearray(n)                      # 0 填充
        end = off + n
        for pc in self._pieces:
            if pc.hi <= off:
                continue
            if pc.lo >= end:
                break
            a = max(off, pc.lo)
            b = min(end, pc.hi)
            if b <= a:
                continue
            data = self.resolve(pc)
            if data is None:
                continue
            src_a = a - pc.lo
            out[a - off:b - off] = data[src_a:src_a + (b - a)]
        return bytes(out)


# ==================== 区段来源解析 ====================

def collect_header_extents(header_data: bytes) -> List[Tuple[int, bytes]]:
    """
    解析大视频 header 的分区块格式, 返回 [(out_offset, data), ...]。

    **2026-09-15 用真实 tdata 取证后重写**: 过去这里用的 `_parse_complex_format`
    假定的布局是错的, 解析出来的 out_offset 全是媒体负载里的随机字节, 于是
    重建出来的 MP4 看着结构完整、时长也对, 但 mdat 负载是拼错的 —— 这正是
    "恢复的视频无法持久播放"的根因。现在优先走 `media_part_extents`
    (真实布局, 每块自带绝对 out_offset), 只有它解析不出来时才回退旧实现
    以兼容历史数据。
    """
    real = media_part_extents(header_data)
    if real:
        return real

    parts, remaining = _parse_complex_format(header_data)
    merged: Dict[int, bytes] = {}
    for off, data in parts:
        merged[off] = data
    if len(remaining) > 100:
        r_parts, _ = _parse_complex_format(remaining)
        for off, data in r_parts:
            if off not in merged:
                merged[off] = data
    return sorted(merged.items(), key=lambda kv: kv[0])


def serialized_extents(data: bytes) -> List[Tuple[int, bytes]]:
    """
    解析普通序列化视频, 返回 [(out_offset, data), ...]。

    与 collect_header_extents 的区别只是输入格式不同, 之后走同一套重打包逻辑。
    同样优先使用真实布局 (media_part_extents), 失败才回退旧的 slice->part 读法。
    """
    real = media_part_extents(data)
    if real:
        return real

    triples = _parse_serialized_data(data)
    merged: Dict[int, bytes] = {}
    for in_off, out_off, size in triples:
        chunk = data[in_off:in_off + size]
        if len(chunk) < size:
            continue
        merged[out_off] = chunk
    return sorted(merged.items(), key=lambda kv: kv[0])


def _build_pieces(extents: Iterable[Tuple[int, bytes]],
                  slices: Sequence[SliceSource]) -> Tuple[List[_Piece], Coverage]:
    pieces: List[_Piece] = []
    cov = Coverage()
    for off, data in extents:
        pieces.append(_Piece(off, off + len(data), 'header', data=data))
        cov.add(off, off + len(data), 'header')
    for s in slices:
        if s.size <= 0:
            continue
        pieces.append(_Piece(s.lo, s.hi, 'slice', data=s.data,
                             path=s.path, name=s.name))
        cov.add(s.lo, s.hi, 'slice', s.name)
    return pieces, cov


# ==================== moov 摆放计划 ====================

@dataclass
class _MoovPlacement:
    append: bool = True          # 是否在末尾追加截断后的 moov
    strip: bool = False          # 是否把原位 moov 改写成等尺寸 free box
    skip: bool = False           # 原位已自洽, 什么都不做
    reason: str = ''


def _plan_moov_placement(moov_box: Box, plan: PrefixPlan) -> _MoovPlacement:
    """
    - moov 在 [0, P) 之外   -> 直接追加
    - moov 在 [0, P) 之内且无需截断 -> 原位保留 (字节级忠实复制原文件)
    - moov 在 [0, P) 之内且需要截断 -> 原 moov 区域改写成 free, 截断后的 moov 追加到末尾
    """
    if moov_box.start >= plan.p:
        return _MoovPlacement(append=True)
    if not plan.truncated:
        return _MoovPlacement(append=False, skip=True)
    if moov_box.end > plan.p:
        return _MoovPlacement(reason='moov 跨越可写边界, 无法安全重排')
    return _MoovPlacement(append=True, strip=True)


# ==================== 共用: 定位 moov ====================

def _locate_moov(sparse: SparseReader, cov: Coverage
                 ) -> Tuple[Optional[Box], Optional[Moov], str]:
    """返回 (moov_box, moov, 错误原因)"""
    size = sparse.size()
    moov_box: Optional[Box] = None
    for b in iter_boxes(sparse, 0, size, allow_truncated_tail=True):
        if b.type == b'moov':
            moov_box = b
            break
    if moov_box is None:
        return None, None, '缓存中未找到 moov (顶层 box 链不完整)'
    if not cov.contains(moov_box.start, moov_box.end):
        return moov_box, None, (f'moov 未完整缓存: 需要 [{moov_box.start},{moov_box.end}), '
                                f'实际覆盖 {cov.describe()}')
    moov = parse_moov(sparse, lenient=True)
    if moov is None:
        return moov_box, None, 'moov 解析失败'
    if moov.fragmented:
        return moov_box, None, 'fragmented MP4 (moof/mvex) 暂不支持'
    if not moov.tracks:
        return moov_box, None, 'moov 内没有可用的轨道'
    if moov.ftyp is None:
        return moov_box, None, '缓存中未找到 ftyp'
    if not cov.contains(moov.ftyp.start, moov.ftyp.end):
        return moov_box, None, 'ftyp 未完整缓存, 无法构成合法 MP4'
    if moov.mdat is None:
        return moov_box, None, '缓存中未找到 mdat'
    return moov_box, moov, ''


# ==================== 分析 (只读) ====================

def analyze(header_data: bytes,
            slices: Sequence[SliceSource] = (),
            decrypt_fn: Optional[Callable[[str], Optional[bytes]]] = None,
            want_duration: bool = False) -> LargeVideoAnalysis:
    """
    只读分析大视频 header: 覆盖率 / 缺口 / 完整性 / 可播放时长。不写任何文件。

    完整性判定 = coverage ⊇ moov 里每个 chunk 的字节区间 (mp4.required_ranges)。
    这是唯一权威的定义 —— 不再使用 estimated_size <= max_end 之类的推断。
    """
    a = LargeVideoAnalysis()
    if not header_data or len(header_data) < 16:
        a.error = 'header 数据为空或过短'
        return a
    try:
        extents = collect_header_extents(header_data)
    except Exception as exc:
        a.error = f'header 解析异常: {exc}'
        return a
    if not extents:
        a.error = 'header 解析失败 (未解析出任何 part)'
        return a
    return analyze_extents(extents, slices, decrypt_fn, want_duration)


def analyze_extents(extents: Sequence[Tuple[int, bytes]],
                    slices: Sequence[SliceSource] = (),
                    decrypt_fn: Optional[Callable[[str], Optional[bytes]]] = None,
                    want_duration: bool = False) -> LargeVideoAnalysis:
    """
    与 analyze 相同, 但直接接受已解析的区段 [(out_offset, data), ...]。

    普通序列化视频 (serialized_extents 的结果) 与复杂 header 格式走同一套判定。
    """
    a = LargeVideoAnalysis()
    if not extents:
        a.error = '没有任何已缓存的区段'
        return a

    a.parts_count = len(extents)
    a.has_ftyp = any(data.find(b'ftyp') >= 0 for _off, data in extents)

    pieces, cov = _build_pieces(extents, slices)
    a.coverage = cov
    if not cov:
        a.error = '没有任何已缓存的区段'
        return a

    sparse = SparseReader(pieces, cov.total_end, decrypt_fn)
    moov_box, moov, err = _locate_moov(sparse, cov)

    a.moov_present = moov is not None
    if moov_box is not None:
        a.moov_range = (moov_box.start, moov_box.end)
    a.ftyp_present = bool(moov and moov.ftyp)
    a.mdat_present = bool(moov and moov.mdat)

    if moov is None:
        # 没有可用的 moov -> 无法计算需求区间, 只能判定"不完整"
        a.complete = False
        a.covered_prefix_end = cov.covered_prefix_end()
        a.reasons.append(err or 'moov 不可用')
        if err:
            a.error = err
        return a

    a.track_count = len(moov.tracks)
    a.total_size = moov.max_chunk_end() or cov.total_end
    a.duration_s = moov.duration_s()

    req = required_ranges(moov)
    a.required_count = len(req)
    a.missing_ranges = cov.missing_of(req)
    a.complete = not a.missing_ranges
    a.covered_prefix_end = cov.covered_prefix_end()
    a.missing_blocks = cov.missing_blocks(a.total_size, K_IN_SLICE)

    if want_duration:
        p_cov = a.covered_prefix_end
        if p_cov > 0 and moov.ftyp is not None and moov.ftyp.end <= p_cov:
            plan = plan_prefix(moov, lambda lo, hi: hi <= p_cov, size_limit=a.total_size)
            a.playable_prefix_bytes = plan.p
            a.playable_duration_s = plan.duration_s
            a.reasons.extend(plan.reasons)
        if a.playable_prefix_bytes <= 0:
            a.reasons.append('连续覆盖前缀内没有任何完整样本')

    a.ok = True
    if not a.complete and not a.reasons:
        a.reasons.append(
            f'覆盖不足: 缺 {len(a.missing_blocks)} 个分片'
            if a.missing_blocks else '覆盖不足: 部分样本字节未缓存')
    return a


# ==================== 重建 ====================

def rebuild_large_video_to_file(
    header_data: bytes,
    slices: Sequence[SliceSource],
    out_path: str,
    decrypt_fn: Optional[Callable[[str], Optional[bytes]]] = None,
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
) -> RebuildResult:
    """
    把大视频 header + 已存在的 8MB 分片重打包为一个**可播放的局部片段**。

    Args:
        header_data: header 文件的解密数据 (复杂序列化格式)
        slices: 磁盘上真实存在的分片 (必须来自 binlog 关联, 不做任何猜测)
        out_path: 输出路径 (应为 exports 目录下的 *.part)
        decrypt_fn: 路径 -> 解密数据; 缺失时无法解密分片
        progress_cb: (done, total, label)

    Returns:
        RebuildResult; playable=False 时 out_path 会被清理掉
    """
    res = RebuildResult(out_path=out_path)
    if not header_data or len(header_data) < 16:
        return res.fail('header 数据为空或过短')
    try:
        extents = collect_header_extents(header_data)
        if not extents:
            return res.fail('header 解析失败 (未解析出任何 part)')
        pieces, cov = _build_pieces(extents, slices)
        if not cov:
            return _cleanup(res.fail('没有任何已缓存的区段'))
        _fill_source_meta(res, slices)
        return _repack(res, pieces, cov, out_path, decrypt_fn, progress_cb)
    except Exception as exc:
        res.fail(f'重建异常: {exc.__class__.__name__}: {exc}')
        return _cleanup(res)


def repack_from_extents(
    extents: Sequence[Tuple[int, bytes]],
    out_path: str,
    slices: Sequence[SliceSource] = (),
    decrypt_fn: Optional[Callable[[str], Optional[bytes]]] = None,
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
) -> RebuildResult:
    """
    从内存中的区段 (普通序列化视频反序列化后的结果) 重打包。

    与 rebuild_large_video_to_file 共用同一套覆盖率/截断/校验逻辑, 因此
    "缓存不全"的小视频也能得到诚实的局部片段, 而不是带零洞的坏文件。

    Args:
        extents: 内存区段 [(out_offset, data), ...]。大视频场景下这是 header
            自描述解析出来的结果, **它是权威的**: 与 slices 重叠时以它为准。
        slices: 额外的外部 8MiB 分片 (binlog 关联且磁盘上真实存在)。只有在
            与已覆盖区域**相邻**时才会真正延长可播放前缀; 中间有空洞时它们
            只是记入覆盖率, 不会污染输出 (连续前缀规则会自动忽略)。
    """
    res = RebuildResult(out_path=out_path)
    if not extents:
        return res.fail('没有任何已缓存的区段')
    try:
        pieces, cov = _build_pieces(extents, slices)
        _fill_source_meta(res, slices)
        return _repack(res, pieces, cov, out_path, decrypt_fn, progress_cb)
    except Exception as exc:
        res.fail(f'重打包异常: {exc.__class__.__name__}: {exc}')
        return _cleanup(res)


def _fill_source_meta(res: RebuildResult, slices: Sequence[SliceSource]) -> None:
    res.slice_indices = sorted(s.index for s in slices if s.size > 0)
    for s in slices:
        if s.size > 0 and s.path:
            try:
                res.source_mtimes[s.name or s.path] = os.path.getmtime(s.path)
            except OSError:
                pass


def _cleanup(res: RebuildResult) -> RebuildResult:
    """任何失败都不得留下半成品"""
    if res.out_path and os.path.exists(res.out_path):
        try:
            os.remove(res.out_path)
        except OSError:
            pass
    res.size = 0
    return res


def _repack(res: RebuildResult,
            pieces: List[_Piece],
            cov: Coverage,
            out_path: str,
            decrypt_fn: Optional[Callable[[str], Optional[bytes]]],
            progress_cb: Optional[Callable[[int, int, str], None]]) -> RebuildResult:
    res.coverage = cov

    # ---- 1. 用稀疏视图定位并解析 moov ----
    sparse = SparseReader(pieces, cov.total_end, decrypt_fn)
    moov_box, moov, err = _locate_moov(sparse, cov)
    if moov is None:
        return _cleanup(res.fail(err))

    total_size = moov.max_chunk_end() or cov.total_end
    res.total_size = total_size
    res.missing_blocks = cov.missing_blocks(total_size, K_IN_SLICE)

    # ---- 2. 只在"连续覆盖前缀"内取样本 ----
    # 必须用 covered_prefix_end 约束: 否则会出现"chunk A 落在头部区段、
    # chunk B 落在尾部区段, 中间是空洞"的情形 —— 那样无法写出连续文件。
    assert moov_box is not None
    p_cov = cov.covered_prefix_end()
    if p_cov <= 0:
        return _cleanup(res.fail('从文件起始处没有任何连续覆盖, 无法重建'))
    if moov.ftyp is not None and moov.ftyp.end > p_cov:
        return _cleanup(res.fail('连续覆盖前缀不足以包含 ftyp'))

    plan: PrefixPlan = plan_prefix(moov, lambda lo, hi: hi <= p_cov,
                                   size_limit=total_size)
    res.per_track_keep = dict(plan.per_track_keep)
    res.samples_kept = dict(plan.samples_kept)
    res.truncated = plan.truncated
    res.duration_s = plan.duration_s
    res.p = plan.p
    res.reasons.extend(plan.reasons)

    if plan.p <= 0:
        return _cleanup(res.fail('连续覆盖前缀内没有任何完整样本 (媒体数据全部缺失)'))
    if plan.p > p_cov:
        return _cleanup(res.fail(f'内部错误: 计划写出 {plan.p} 超出连续覆盖 {p_cov}'))
    if moov.mdat is not None and moov.mdat.start >= plan.p:
        return _cleanup(res.fail('连续覆盖前缀内没有任何媒体数据'))

    placement = _plan_moov_placement(moov_box, plan)
    if placement.reason:
        return _cleanup(res.fail(placement.reason))

    # ---- 3. 写出前缀 ----
    os.makedirs(os.path.dirname(out_path) or '.', exist_ok=True)
    if not _write_prefix(out_path, pieces, plan.p, sparse, progress_cb):
        return _cleanup(res.fail('写出前缀失败: 覆盖存在空洞 (内部错误)'))

    # ---- 4. 改写 mdat 声明大小 ----
    if moov.mdat is not None:
        _patch_mdat_in_file(out_path, moov.mdat, plan.p - moov.mdat.start)

    # ---- 5. moov 摆放 ----
    new_moov = truncate_tables(moov.raw, plan)
    final_size = plan.p

    if placement.strip:
        # 原位 moov 改写成等尺寸 free box (必须在 header pass 之后做)
        with open(out_path, 'r+b') as f:
            f.seek(moov_box.start)
            f.write(moov_box.size.to_bytes(4, 'big'))
            f.write(b'free')
            remain = moov_box.size - 8
            zero = b'\x00' * min(remain, WRITE_BUF)
            while remain > 0:
                n = min(remain, len(zero))
                f.write(zero[:n])
                remain -= n

    if placement.append:
        with open(out_path, 'r+b') as f:
            f.seek(plan.p)
            f.write(new_moov)
            final_size = plan.p + len(new_moov)
            f.truncate(final_size)

    res.size = final_size
    res.playable = True
    return res


def _patch_mdat_in_file(path: str, mdat: Box, new_size: int) -> bool:
    """把文件里 mdat box 的声明大小改写为 new_size (含 header)"""
    if new_size < mdat.header:
        return False
    try:
        with open(path, 'r+b') as f:
            f.seek(mdat.start)
            head = bytearray(f.read(mdat.header))
            if len(head) < mdat.header:
                return False
            patch_mdat_size(head, Box(0, mdat.header, b'mdat', mdat.header), new_size)
            f.seek(mdat.start)
            f.write(bytes(head))
        return True
    except OSError:
        return False


def _write_prefix(
    out_path: str,
    pieces: Sequence[_Piece],
    limit: int,
    sparse: SparseReader,
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
) -> bool:
    """
    写出 [0, limit)。

    两个 pass: 先写分片, 再覆盖 header parts —— 因为 header 的 [0,128KB) 是
    Telegram 自己认定的文件头, 应当优先于分片中的同区段数据。
    逐段写出, 峰值内存 = 一个分片 + 一个 1MiB 缓冲。
    """
    ordered = sorted(pieces, key=lambda p: p.lo)
    with open(out_path, 'wb') as f:
        for kind_pass in ('slice', 'header'):
            targets = [p for p in ordered if p.kind == kind_pass]
            total = len(targets)
            for i, pc in enumerate(targets):
                lo = pc.lo
                hi = min(pc.hi, limit)
                if hi > lo:
                    data = sparse.resolve(pc)
                    if data is None:
                        return False
                    seg = data[:hi - lo]
                    f.seek(lo)
                    for off in range(0, len(seg), WRITE_BUF):
                        f.write(seg[off:off + WRITE_BUF])
                if progress_cb:
                    progress_cb(i + 1, total, pc.name or pc.kind)
    return True
