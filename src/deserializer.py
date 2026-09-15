"""
序列化视频反序列化与大视频重建模块

Telegram Desktop 将大视频 (>10MB) 分块存储在 media_cache 中,
第一个分块使用序列化格式 (slice→part 结构), 用于流式播放.

序列化格式:
  slice_count(4B LE) + [part_count(4B LE) + [out_offset(4B)+part_size(4B)+data] * N] * N

每个 part 的 out_offset 指向最终 MP4 文件中的位置,
parts 可能不连续 (媒体播放器会 seek), 需按 out_offset 排序后重组.

大视频重建:
  当大视频超过 ~10MB 时, header 文件只包含前几个 part + 第一个 slice 去重后的剩余 part,
  后续每个 8MB 数据块以独立的“连续序列化格式”存储 (无头部, 纯数据).

职责划分 (重构后):
  - 本模块只负责**解析格式** (序列化 slice→part、复杂 header 格式、moov 尺寸提取)
  - 重打包/重建一律走 src.rebuild (流式、无空洞、带覆盖率判定)
  - box 解析与样本表截断走 src.mp4
  - 完整性判定走 src.coverage (coverage ⊇ moov 里每个 chunk 的字节区间)

本模块里 `rebuild_large_video` 已废弃 (整片内存 + 零填充 + 只截尾部零块),
`_find_continuous_data_range` 也不再参与任何决策。
"""

import struct
import os
from dataclasses import dataclass
from typing import Optional, Tuple, List, Dict


# 反序列化参数限制
MAX_PARTS_COUNT = 80
MAX_PART_SIZE = 128 * 1024  # 128KB

# 大视频分片常量
K_PART_SIZE = 128 * 1024       # 每个 part 大小
K_IN_SLICE = 8 * 1024 * 1024  # 8 MiB = 64 * 128KB

# 真实"分区块"格式 (见 MediaPart 的注释) 的解析上限
MAX_GROUP_COUNT = 100_000      # 单组 part_count 上限
MAX_BLOCK_SIZE = 64 * 1024 * 1024   # 单个块上限
MIN_FILL_RATIO = 0.90          # 解析后至少要覆盖明文这么多比例才算"是这个格式"


def _find_continuous_data_range(result: bytearray) -> int:
    """
    检查 result 中从位置 0 开始的连续有效数据范围 (遇到第一个全零块即停)。

    已废弃: 不再用于任何决策。

    旧实现只截掉**尾部**的零块, 因此中部的空洞会原样保留; 更糟的是, Telegram
    缓存的视频常常把 moov 放在文件末尾, 一旦 moov 之后有零填充, 尾部截断就会
    把 moov 一起砍掉, 输出彻底无法解复用。

    "哪些字节缺失"应当由 Coverage / mp4.required_ranges 判定, 而不是靠看字节
    是不是 0。此函数仅保留给极少数需要"找到第一个空洞"的旧调用点。
    """
    if not result:
        return 0

    chunk = 4096
    total = len(result)

    for i in range(0, total, chunk):
        block = result[i:i + chunk]
        if block == b'\x00' * len(block):
            # 找到第一个全零块, 向前回查最后一个非零字节的精确位置
            for j in range(i - 1, -1, -1):
                if result[j] != 0:
                    return j + 1
            return 0

    return total


MAX_INMEMORY_VIDEO = 256 * 1024 * 1024     # 256 MiB


def deserialize_video(data: bytes,
                      max_size: int = MAX_INMEMORY_VIDEO) -> Optional[bytes]:
    """
    反序列化普通序列化视频 (slice->part 格式), 重组为完整 MP4 字节。

    仅用于小体积、可以整片放进内存的场景 (缩略图、小视频)。
    超过 max_size 返回 None —— 大视频请走 rebuild.repack_from_extents /
    rebuild.rebuild_large_video_to_file, 那两条路径是流式的、带覆盖率判定的。

    与旧实现的两处关键差别:
      1. file_size 取 max(out_offset + part_size)。
         旧实现取"按 out_offset 排序后最后一个 part 的 起点 + 长度", 一旦 parts
         交错或重叠, 缓冲区就会被开小, 超出范围的 part 被静默丢弃 ——
         文件看着合法却少了数据。
      2. **不再截断尾部零块**。
         旧实现把文件截到"最后一个非零字节"。而 Telegram 缓存的视频常常把 moov
         放在文件末尾, 一旦 moov 之后有零填充就会被一起砍掉, 输出彻底无法解复用。
         哪些字节缺失应当由 Coverage 判定, 而不是靠看字节是不是 0。

    Args:
        data: 解密后的序列化视频数据
        max_size: 允许在内存中重组的最大字节数

    Returns:
        重组后的 MP4 数据; 无法解析或超出 max_size 时返回 None
    """
    if len(data) < 16:
        return None

    parts = _parse_serialized_data(data)
    if not parts:
        return None

    file_size = max(out_offset + part_size for _in, out_offset, part_size in parts)
    if file_size < 16 or file_size > max_size:
        return None

    result = bytearray(file_size)
    for in_offset, out_offset, part_size in parts:
        if in_offset + part_size > len(data):
            continue
        if out_offset + part_size > file_size:
            continue
        result[out_offset:out_offset + part_size] = data[in_offset:in_offset + part_size]

    return bytes(result)


@dataclass(frozen=True)
class MediaPart:
    """缓存文件里的一个数据块。

    out_offset 是这块数据在**最终媒体文件里的绝对偏移**, in_offset 是它在
    缓存明文里的位置。有了 out_offset, "哪些字节有、哪些没有"就是确定的事实,
    不再需要靠 K_IN_SLICE / mtime / binlog 去猜。
    """
    out_offset: int
    size: int
    in_offset: int

    @property
    def out_end(self) -> int:
        return self.out_offset + self.size


def parse_media_parts(data: bytes) -> Optional[List[MediaPart]]:
    """
    解析 Telegram Desktop 媒体缓存的**真实**分区块格式。

    布局 (2026-09-15 用本机真实 tdata 取证确认, 三个不同的大视频样本都能
    一路解析到距文件尾 2 字节处, 且每个块的 out_offset 严格连续):

        plaintext = [u32 part_count] + ([u32 out_offset][u32 size][size 字节数据]) * part_count
                    ^^^ 上面这一整组可以重复出现若干次 ^^^

    典型形态: Telegram 流式下载会先取"开头"再取"结尾"(为了拿 moov), 所以
    第一组常见的是 [offset 0, 128KB] + [文件末尾若干块]; 后续组则是顺序补齐的
    中间块。因此**同一个文件的块并不连续**, 必须逐块按 out_offset 归位。

    Returns:
        [MediaPart, ...] 按 out_offset 升序; 不是这个格式或数据损坏时返回 None
    """
    if len(data) < 12:
        return None

    parts: List[MediaPart] = []
    pos = 0
    n = len(data)

    while pos + 4 <= n:
        count = struct.unpack_from('<I', data, pos)[0]
        pos += 4
        if count == 0 or count > MAX_GROUP_COUNT:
            break
        for _ in range(count):
            if pos + 8 > n:
                return None
            out_offset = struct.unpack_from('<I', data, pos)[0]
            size = struct.unpack_from('<I', data, pos + 4)[0]
            pos += 8
            if size == 0 or size > MAX_BLOCK_SIZE or pos + size > n:
                return None
            parts.append(MediaPart(out_offset, size, pos))
            pos += size
        if pos >= n:
            break

    if not parts:
        return None
    # 兜底校验: 必须吃掉明文的绝大部分, 否则只是误打误撞
    consumed = pos
    if consumed < n * MIN_FILL_RATIO:
        return None
    parts.sort(key=lambda p: (p.out_offset, p.in_offset))
    return parts


def media_part_extents(data: bytes) -> List[Tuple[int, bytes]]:
    """
    把缓存明文解析成 [(out_offset, bytes), ...], 供 rebuild 走覆盖率逻辑。

    同一 out_offset 重复出现时保留先出现的那块 (与旧实现一致)。
    """
    parts = parse_media_parts(data)
    if not parts:
        return []
    merged: Dict[int, bytes] = {}
    for p in parts:
        chunk = data[p.in_offset:p.in_offset + p.size]
        if len(chunk) < p.size:
            continue
        if p.out_offset not in merged:
            merged[p.out_offset] = chunk
    return sorted(merged.items(), key=lambda kv: kv[0])


def _parse_serialized_data(data: bytes) -> List[Tuple[int, int, int]]:
    """
    解析序列化数据, 提取所有 part 的位置信息

    **注意: 本函数实际读的布局与模块 docstring / scanner._check_serialized_video
    里的描述不一致, 这是一个已存在的隐患, 需要有真实样本才能定论。**

      模块 docstring 与 scanner 的说法: slice_count(4B) + [part_count + parts]*N
      本函数的实际读法:               [part_count + parts]*N   (没有前导 slice_count)

    也就是说本函数把文件开头的 4 字节直接当成第一个 part_count。实测:
      - 按"本函数读法"构造的数据 -> 能正确解析出 parts
      - 按"docstring 读法"构造的数据 -> 解析结果为 []

    只有一种能匹配真实的 Telegram 数据。在拿到真实样本比对之前, 这里不做猜测性
    修改, 只把差异记录下来。若最终确认本函数读法是错的, 那么所有"普通序列化视频"
    的重建都会被影响。

    Returns:
        list of (in_offset, out_offset, part_size)
    """
    parts = []
    pos = 0
    slice_idx = 0

    while pos < len(data):
        # 读取 slice header
        if pos + 4 > len(data):
            break

        parts_count = struct.unpack('<I', data[pos:pos + 4])[0]
        pos += 4

        if parts_count == 0 or parts_count > MAX_PARTS_COUNT:
            break

        # 解析每个 part
        for p in range(parts_count):
            if pos + 8 > len(data):
                break

            out_offset = struct.unpack('<I', data[pos:pos + 4])[0]
            part_size = struct.unpack('<I', data[pos + 4:pos + 8])[0]
            pos += 8

            if part_size == 0 or part_size > MAX_PART_SIZE:
                break

            in_offset = pos  # part 数据在序列化文件中的起始位置
            parts.append((in_offset, out_offset, part_size))

            pos += part_size  # 跳过 part 数据
            if pos > len(data) + 64:  # 允许末尾少量额外字节
                break

        slice_idx += 1

        # 安全限制: 最多解析 100 个 slice
        if slice_idx > 100:
            break

    return parts


def get_video_info(data: bytes) -> dict:
    """
    获取序列化视频的基本信息 (不重组文件)

    Returns:
        dict with keys: slice_count, total_parts, estimated_size, ftyp_brand
    """
    info = {
        'slice_count': 0,
        'total_parts': 0,
        'estimated_size': 0,
        'ftyp_brand': '',
        'is_serialized': False,
    }

    if len(data) < 16:
        return info

    # 检查是否是序列化格式
    slice_count = struct.unpack('<I', data[:4])[0]
    if slice_count == 0 or slice_count > MAX_PARTS_COUNT:
        return info

    parts = _parse_serialized_data(data)
    if not parts:
        return info

    info['slice_count'] = slice_count
    info['total_parts'] = len(parts)
    info['is_serialized'] = True

    # 估算大小: 取所有 part 的结束位置最大值 (不能取"起始偏移最大的那个 part")
    info['estimated_size'] = max(out_offset + part_size
                                 for _in, out_offset, part_size in parts)

    # 查找 ftyp 签名
    ftyp_pos = data.find(b'ftyp')
    if ftyp_pos > 0 and ftyp_pos < 64:
        info['ftyp_brand'] = data[ftyp_pos + 4:ftyp_pos + 8].decode('ascii', errors='replace')

    return info


# ==================== 大视频重建 ====================

def _parse_complex_format(data: bytes) -> Tuple[List[Tuple[int, bytes]], bytes]:
    """
    解析复杂序列化格式 (用于大视频 header)

    格式: count(4B LE) + [offset(4B LE) + size(4B LE) + data(size B)] * count

    Returns:
        (parts_list, remaining_data)
        parts_list: [(offset, data), ...]
        remaining_data: header parts 之后的剩余数据 (可能是 first slice remaining)
    """
    parts = []
    if len(data) < 4:
        return parts, b''
    count = struct.unpack_from('<I', data, 0)[0]
    offset = 4
    for _ in range(count):
        if offset + 8 > len(data):
            break
        part_off = struct.unpack_from('<I', data, offset)[0]
        part_size = struct.unpack_from('<I', data, offset + 4)[0]
        offset += 8
        if part_size > K_PART_SIZE * 2 or part_size == 0:
            break
        if offset + part_size > len(data):
            break
        parts.append((part_off, data[offset:offset + part_size]))
        offset += part_size
    return parts, data[offset:]


def _extract_video_size_from_moov(slice0_data: bytes) -> int:
    """
    从重组后的 slice 0 数据中解析 MP4 moov box,
    通过 stco (chunk offset table) 获取视频的真实文件大小.

    返回最后一个 chunk offset + 最后一个 chunk 大小, 
    如果无法解析返回 0.
    """
    import struct as _s

    def _parse_boxes(data, start, end):
        """递归解析 MP4 box, 返回 [(pos, size, type)]"""
        pos = start
        boxes = []
        while pos < end - 8:
            box_size = _s.unpack('>I', data[pos:pos + 4])[0]
            box_type = data[pos + 4:pos + 8]
            if box_size == 0:
                box_size = end - pos
            elif box_size == 1:
                if pos + 16 > end:
                    break
                box_size = _s.unpack('>Q', data[pos + 8:pos + 16])[0]
            if box_size < 8 or pos + box_size > end:
                break
            boxes.append((pos, box_size, box_type))
            if box_type in (b'moov', b'trak', b'mdia', b'minf', b'stbl', b'udta', b'edts'):
                boxes.extend(_parse_boxes(data, pos + 8, pos + box_size))
            pos += box_size
        return boxes

    try:
        boxes = _parse_boxes(slice0_data, 0, len(slice0_data))
        max_end = 0
        for pos, size, btype in boxes:
            if btype == b'stco':
                # stco: version(1B) + flags(3B) + entry_count(4B) + [chunk_offset(4B BE)] * count
                box_data = slice0_data[pos + 8:pos + size]
                if len(box_data) >= 8:
                    entry_count = _s.unpack('>I', box_data[4:8])[0]
                    if entry_count > 0:
                        last_off = _s.unpack('>I', box_data[8 + (entry_count - 1) * 4:12 + (entry_count - 1) * 4])[0]
                        max_end = max(max_end, last_off + 1024)  # +1KB 估算最后 chunk 大小
            elif btype == b'co64':
                # co64: 64-bit chunk offsets
                box_data = slice0_data[pos + 8:pos + size]
                if len(box_data) >= 8:
                    entry_count = _s.unpack('>I', box_data[4:8])[0]
                    if entry_count > 0:
                        last_off = _s.unpack('>Q', box_data[8 + (entry_count - 1) * 8:16 + (entry_count - 1) * 8])[0]
                        max_end = max(max_end, last_off + 1024)
            elif btype == b'mdat':
                # mdat box: pos + size 就是 media data 的结束位置
                max_end = max(max_end, pos + size)
        return max_end
    except Exception:
        return 0


def rebuild_large_video(
    header_data: bytes,
    slice_data_list: List[bytes],
    slice_indices: Optional[List[int]] = None
) -> Optional[bytes]:
    """
    [已废弃] 大视频重建 —— 整片读进内存 + 零填充 + 只截尾部零块。

    请改用 src.rebuild:
      - rebuild_large_video_to_file(header_data, slices, out_path, decrypt_fn)
        流式写出, 峰值内存 = 一个分片
      - repack_from_extents(extents, out_path)
        从内存区段重打包
      - analyze(header_data, slices)
        只读地判定覆盖率与缺口

    保留此函数仅为兼容可能的外部调用; 应用内部已不再使用它。

    旧实现的三个问题:
      1. bytearray(video_size) + bytes(video_data) 造成 2 倍以上峰值内存
      2. 用 0 填充缺失分片区域, 中段空洞原样保留 (只截尾部零块)
      3. 从不校验结果, 于是"满是零洞的文件"被当作成功产物

    格式说明 (仅历史参考):
      header_data 为复杂序列化格式; slice_data_list 为 8MB 分片解密数据;
      slice N 对应视频偏移 N * K_IN_SLICE。
    """
    if not header_data or len(header_data) < 16:
        return None

    # 解析 header 的复杂格式
    parts, remaining = _parse_complex_format(header_data)
    if not parts:
        return None

    # 合并 header parts 和 remaining parts
    all_parts: Dict[int, bytes] = {}
    for off, pdata in parts:
        all_parts[off] = pdata

    if len(remaining) > 100:
        r_parts, _ = _parse_complex_format(remaining)
        for off, pdata in r_parts:
            if off not in all_parts:
                all_parts[off] = pdata

    if not all_parts:
        return None

    # 计算 header parts 覆盖的最大范围 (通常是 slice 0 = 8MB)
    header_max_end = max(off + len(pdata) for off, pdata in all_parts.items())
    if header_max_end > 4 * 1024 * 1024 * 1024:  # 限制 4GB
        return None

    # 构建 slice 0 数据, 然后从 moov 中获取视频真实大小
    slice0_data = bytearray(header_max_end)
    for off, pdata in all_parts.items():
        end = min(off + len(pdata), header_max_end)
        slice0_data[off:end] = pdata[:end - off]

    video_size = _extract_video_size_from_moov(bytes(slice0_data))

    if video_size == 0:
        video_size = header_max_end
    else:
        # 对齐到 K_IN_SLICE 边界
        video_size = ((video_size + K_IN_SLICE - 1) // K_IN_SLICE) * K_IN_SLICE

    if video_size > 4 * 1024 * 1024 * 1024:
        return None

    # 构建完整视频缓冲区
    video_data = bytearray(video_size)

    # 1. 先填充外部 slice 数据 (让 header parts 后覆盖, 保证 ftyp/moov 正确)
    if slice_indices is not None and len(slice_indices) == len(slice_data_list):
        for sdata, slice_idx in zip(slice_data_list, slice_indices):
            slice_start = slice_idx * K_IN_SLICE
            if slice_start >= video_size:
                continue
            fill_end = min(slice_start + len(sdata), video_size)
            video_data[slice_start:fill_end] = sdata[:fill_end - slice_start]
    elif slice_data_list:
        for sn, sdata in enumerate(slice_data_list):
            slice_start = sn * K_IN_SLICE
            if slice_start >= video_size:
                break
            fill_end = min(slice_start + len(sdata), video_size)
            video_data[slice_start:fill_end] = sdata[:fill_end - slice_start]

    # 2. 再填充 header parts (覆盖 slice 数据, 保证 ftyp + moov 正确)
    for off, pdata in all_parts.items():
        end = min(off + len(pdata), video_size)
        video_data[off:end] = pdata[:end - off]

    # 注意: 这里**不再**截断尾部零填充 (旧实现会这么做, 且会把位于文件末尾的 moov
    # 一起砍掉)。缺失分片留下的零洞是"数据缺失"的表达, 应当由覆盖率判定处理,
    # 而不是靠字节是否为 0 来猜。使用旧的返回值意味着你接受一个可能含零洞的文件,
    # 因此该函数已从应用内部移除所有调用点。
    return bytes(video_data)


def get_large_video_info(header_data: bytes) -> dict:
    """
    获取大视频的基本信息 (不写文件、不解密分片)。

    返回键保持向后兼容: estimated_size / parts_count / slices_needed / has_ftyp,
    另加 total_size / moov_present。

    重要变化 —— 删除了旧版的这条规则:
        if estimated_size <= max_end: slices_needed = 0
    它是本 bug 的核心来源: is_large_video_header 判定"是大视频"的条件恰好是
    "存在超过 8MiB 的 part 偏移", 于是 max_end 常常接近 EOF, 使该条件几乎恒真,
    从而把"缺掉整个 mdat 中段"的视频判成"header 自身即完整视频"。

    现在:
      - total_size 由 moov 里最后一个 chunk 的结束位置精确给出 (通过 mp4 解析)
      - slices_needed = ceil(total_size / K_IN_SLICE), 只作为**展示用**的规模指标,
        不再参与任何完整性判定
      - 完整性一律由 Coverage ∩ required_ranges 判定 (见 rebuild.analyze)
    """
    info = {
        'estimated_size': 0,
        'total_size': 0,
        'parts_count': 0,
        'slices_needed': 0,
        'has_ftyp': False,
        'moov_present': False,
    }

    if not header_data or len(header_data) < 16:
        return info

    # ---- 优先: 用 mp4 精确解析 (稀疏视图, 不解密分片) ----
    try:
        from .rebuild import analyze          # 延迟导入, 避免循环依赖
        a = analyze(header_data)
        info['parts_count'] = a.parts_count
        info['has_ftyp'] = a.has_ftyp
        info['moov_present'] = a.moov_present
        if a.total_size > 0:
            info['total_size'] = a.total_size
            info['estimated_size'] = a.total_size
            info['slices_needed'] = (a.total_size + K_IN_SLICE - 1) // K_IN_SLICE
            return info
    except Exception:
        pass

    # ---- 回退: 旧的启发式 (仅在精确解析不可用时使用) ----
    parts, remaining = _parse_complex_format(header_data)
    if not parts:
        return info

    all_parts: Dict[int, bytes] = {}
    for off, pdata in parts:
        all_parts[off] = pdata
    if len(remaining) > 100:
        r_parts, _ = _parse_complex_format(remaining)
        for off, pdata in r_parts:
            if off not in all_parts:
                all_parts[off] = pdata

    if not all_parts:
        return info

    max_end = max(off + len(pdata) for off, pdata in all_parts.items())
    info['parts_count'] = len(all_parts)
    info['has_ftyp'] = header_data.find(b'ftyp') > 0

    slice0_data = bytearray(max_end)
    for off, pdata in all_parts.items():
        end = min(off + len(pdata), max_end)
        slice0_data[off:end] = pdata[:end - off]

    real_size = _extract_video_size_from_moov(bytes(slice0_data))
    total = real_size if real_size > max_end else max_end
    info['total_size'] = total
    info['estimated_size'] = total
    info['slices_needed'] = (total + K_IN_SLICE - 1) // K_IN_SLICE
    return info
