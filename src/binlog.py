"""
Telegram Desktop 缓存 binlog 解析模块

解析 media_cache/binlog 文件, 提取 PlaceId → Cache Key 的映射关系.
用于将 8MB 视频分片正确关联到对应的大视频 header.

binlog 记录格式 (来自 lib_storage 源码):
  - BasicHeader: 16 字节 (format:1B + flags:3B + systemTime:4B + reserved1:4B + reserved2:4B)
  - StoreWithTime: 48 字节 (type:1B + tag:1B + size:3B + place:7B + checksum:4B + key:16B + time:12B)
  - MultiStore: 16B header + N * 48B StoreWithTime
  - MultiRemove: 16B header + N * 16B Key
  - MultiAccess: 16B header (time replaces reserved) + N * 16B Key

Cache Key 结构:
  Key { uint64 high; uint64 low; }

  **2026-09-15 用本机真实 tdata 取证修正** —— 过去的“key_high 相同即同一部
  视频”是错的, 实测同一个 key_high 下会混进 7 个不同的视频 (它们的 slice
  索引都算成 0)。正确的分组键是 (key_high, key_low >> 16):

      key_low = document_id(高 48 位) << 16 | slice_index(低 16 位)

  证据:
    - 按 (key_high, key_low >> 16) 分组后, 组内 slice 索引**零重复**, 且是
      0..N 的连续区间 (实测出现 0..280 / 0..233 / 0..101);
    - 组内 slice 索引的上界与 slice 0 序列化文件里 part 的最大 out_offset
      高度吻合 (281 个分片 x 8MiB ≈ 2.25GB, 对应 part#17868 的 2.242GB);
    - 而按 key_high 分组时, 同一组里出现多个 slice=0 的不同大小文件。
  把不同视频的分片缝在一起, 就是“恢复出来的视频看得到但解码报
  Invalid NAL unit size”的直接来源。

  slice 索引与媒体偏移的对应 (**2026-09-15 修正, 有整片解码证据**):
      外部文件 binlog index i 覆盖媒体文件的 [(i-1)*8MiB, i*8MiB)。
      index 0 是序列化 header 自身的记录; 实测外部文件的 index 从 2 起
      (index 1 偶见于无 header 的裸块, 内容以 ftyp 开头, 即 [0,8MiB))。
      旧的 "i*8MiB" 映射整体错位一格, 会把完整缓存误报成"缺 1 个分片"。
      注意: 偏移换算不在这个模块做 (binlog 忠实记录原始 index),
      在 server._slices_for_header 转成 SliceSource 时统一减一。

PlaceId → 文件路径映射 (C++ PlaceFromId):
  nibble 顺序反转: 先输出低位 nibble, 再输出高位 nibble
  如 0x12 → "21", 0xAB → "BA"
  格式: XX/YYZZAABBCCDDEE (大写, 7 字节 → 2+12 字符)
"""

import os
import struct
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass

from .crypto import decrypt_tdef_file, extract_local_key


@dataclass
class BinlogRecord:
    """binlog 中的一条 Store 记录"""
    tag: int           # 用户标签 (uint8)
    size: int          # 数据大小 (3 字节, 最大 16MB)
    place: bytes       # PlaceId (7 字节)
    checksum: int      # XXH32 校验和
    key_high: int      # Key 高 64 位
    key_low: int       # Key 低 64 位

    @property
    def slice_index(self) -> int:
        """从 key_low 提取 slice 索引 (最后 2 字节, little-endian)"""
        return self.key_low & 0xFFFF

    @property
    def doc_key(self) -> Tuple[int, int]:
        """
        同一部媒体的标识 = (key_high, key_low >> 16)。

        只比较 key_high 会把不同媒体混成一组 (已取证证伪), 必须带上
        key_low 的高 48 位 (document_id)。
        """
        return (self.key_high, self.key_low >> 16)

    @property
    def real_document_id(self) -> int:
        """
        还原真实的 64 位 document_id (用于查 locations/downloads 索引)。

        **2026-09-15 取证确认, 经 locations 的 16 条 DocumentFileLocation
        交叉验证 16/16 全部命中**:

            real_document_id = ((key_high & 0xFFFF) << 48) | (key_low >> 16)

        即缓存 binlog 的 key_high 低 16 位其实是真实文档 ID 的**高 16 位**,
        key_low 的高 48 位是真实文档 ID 的低 48 位。locations 文件里存的
        是完整 64 位 id, 直接拿 key_high 去查永远查不到。

        反向验证公式 (从 locations 的真实 id 预测缓存键):
            pred_key_high = (1 << 16) | (real_id >> 48)
            pred_doc48    = real_id & 0xFFFFFFFFFFFF
        """
        return ((self.key_high & 0xFFFF) << 48) | (self.key_low >> 16)

    @property
    def file_name(self) -> str:
        """PlaceId → 文件名 (nibble 反转, 大写)"""
        return _place_to_filename(self.place)


def _place_to_filename(place: bytes) -> str:
    """
    PlaceId (7 字节) → 文件名

    C++ PlaceFromId 的 push 函数先输出低位 nibble, 再输出高位 nibble:
      pushDigit(value & 0x0F)  → 低位
      pushDigit(value >> 4)    → 高位

    示例: [0x12, 0x34, 0x56, 0x78, 0x9A, 0xBC, 0xDE]
      → "21/436587A9CBED" (不是 "12/3456789ABCDE")
    """
    hex_chars = []
    for i, b in enumerate(place):
        low = b & 0x0F
        high = (b >> 4) & 0x0F
        hex_chars.append(f"{low:X}{high:X}")
        if i == 0:
            hex_chars.append('/')
    return ''.join(hex_chars)[3:]  # 去掉前2字符目录和'/'


def parse_binlog(binlog_path: str, local_key: bytes) -> List[BinlogRecord]:
    """
    解析 binlog 文件, 返回所有 Store/MultiStore 记录

    Args:
        binlog_path: binlog 文件路径
        local_key: LocalKey (256 字节)
    Returns:
        BinlogRecord 列表
    """
    decrypted = decrypt_tdef_file(binlog_path, local_key)

    # 跳过 16B BasicHeader
    offset = 16
    records = []

    while offset < len(decrypted) - 1:
        rt = decrypted[offset]

        if rt == 0x01:  # StoreWithTime (48 字节)
            if offset + 48 > len(decrypted):
                break
            r = decrypted[offset:offset + 48]
            records.append(BinlogRecord(
                tag=r[1],
                size=r[2] | (r[3] << 8) | (r[4] << 16),
                place=r[5:12],
                checksum=struct.unpack('<I', r[12:16])[0],
                key_high=struct.unpack('<Q', r[16:24])[0],
                key_low=struct.unpack('<Q', r[24:32])[0],
            ))
            offset += 48

        elif rt == 0x02:  # MultiStore (16B header + N * 48B)
            count = decrypted[offset + 1] | (decrypted[offset + 2] << 8) | (decrypted[offset + 3] << 16)
            total = 16 + count * 48
            if offset + total > len(decrypted):
                break
            for i in range(count):
                po = offset + 16 + i * 48
                r = decrypted[po:po + 48]
                records.append(BinlogRecord(
                    tag=r[1],
                    size=r[2] | (r[3] << 8) | (r[4] << 16),
                    place=r[5:12],
                    checksum=struct.unpack('<I', r[12:16])[0],
                    key_high=struct.unpack('<Q', r[16:24])[0],
                    key_low=struct.unpack('<Q', r[24:32])[0],
                ))
            offset += total

        elif rt == 0x03:  # MultiRemove (16B header + N * 16B Key)
            count = decrypted[offset + 1] | (decrypted[offset + 2] << 8) | (decrypted[offset + 3] << 16)
            offset += 16 + count * 16

        elif rt == 0x04:  # MultiAccess (16B header + N * 16B Key)
            count = decrypted[offset + 1] | (decrypted[offset + 2] << 8) | (decrypted[offset + 3] << 16)
            offset += 16 + count * 16

        else:
            offset += 1

    return records


def build_key_index(records: List[BinlogRecord]) -> Dict[str, BinlogRecord]:
    """
    构建 文件名 → BinlogRecord 的映射

    Args:
        records: binlog 解析结果
    Returns:
        {file_name: BinlogRecord} 字典
    """
    index = {}
    for r in records:
        fname = r.file_name
        index[fname] = r
    return index


def group_by_video_key(records: List[BinlogRecord]) -> Dict[int, List[BinlogRecord]]:
    """
    按 key_high 分组 (同一大视频的所有分片)

    Returns:
        {key_high: [BinlogRecord, ...]} 字典
    """
    from collections import defaultdict
    groups = defaultdict(list)
    for r in records:
        groups[r.key_high].append(r)
    return dict(groups)


def get_slice_records_for_header(
    header_file_name: str,
    binlog_index: Dict[str, BinlogRecord],
    all_file_names: set
) -> List[Tuple[int, str, int]]:
    """
    获取属于同一大视频的所有 8MB 分片, 连同它们的大小。

    与 get_slices_for_header 的区别: 额外返回 size, 让覆盖率模型不必再 stat 文件
    (binlog 记录的 size 就是该分片的解密后大小)。

    **归组口径 (2026-09-15 修正)**: 只认 (key_high, key_low >> 16) 完全相同的
    记录 —— 即同一部媒体。仅比较 key_high 会混入别的视频。

    Returns:
        [(slice_index, file_name, size), ...] 按 slice_index 排序
    """
    header_record = binlog_index.get(header_file_name)
    if not header_record:
        return []

    target_doc = header_record.doc_key

    by_slice: Dict[int, List[Tuple[str, BinlogRecord]]] = {}
    for fname, r in binlog_index.items():
        if r.doc_key != target_doc:
            continue
        if fname == header_file_name:
            continue
        si = r.slice_index
        # 不跳过任何 index: 归组只管"这是不是同一部媒体的分片",
        # 摆放位置 ([(i-1)*8MiB)) 由 server._slices_for_header 统一换算。
        # index 0 若出现 (非 header 自身), 视为覆盖 [0,8MiB) 的另一份拷贝,
        # 与 header 重叠的部分以 header 为准, 无害。
        by_slice.setdefault(si, []).append((fname, r))

    out: List[Tuple[int, str, int]] = []
    for si in sorted(by_slice):
        on_disk = [(fn, r) for fn, r in by_slice[si] if fn in all_file_names]
        if not on_disk:
            continue
        # 多个都在磁盘上时, 选 size 最大的 (通常是最新版本)
        best_name, best_rec = max(on_disk, key=lambda x: x[1].size)
        out.append((si, best_name, int(best_rec.size)))
    return out


def get_slices_for_header(
    header_file_name: str,
    binlog_index: Dict[str, BinlogRecord],
    all_file_names: set
) -> List[Tuple[int, str]]:
    """
    获取属于同一大视频的所有 8MB 分片

    通过 binlog Key 关联: 找到 header 的 key_high,
    然后返回所有 key_high 相同且 slice_index >= 0 的文件

    同一 slice_index 可能有多条 binlog 记录 (旧版本被 compact/remove 后留下),
    优先选择磁盘上实际存在的文件。如果都不在磁盘上则跳过。

    Args:
        header_file_name: 大视频 header 的文件名
        binlog_index: 文件名 → BinlogRecord 映射
        all_file_names: 所有已扫描文件的文件名集合
    Returns:
        [(slice_index, file_name), ...] 按 slice_index 排序
    """
    return [(si, fn) for si, fn, _size in
            get_slice_records_for_header(header_file_name, binlog_index, all_file_names)]
