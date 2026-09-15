"""
Telegram Desktop 缓存扫描与文件类型识别模块

负责扫描 user_data/cache 和 user_data/media_cache 目录,
解密所有 TDEF 文件, 通过 magic bytes 识别文件类型
"""

import os
import struct
import hashlib
import zlib
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Dict, Tuple
from .crypto import decrypt_tdef_file, extract_local_key


# ==================== 文件类型定义 ====================

class FileType(Enum):
    """缓存文件解密后的实际媒体类型"""
    JPEG = "jpg"
    PNG = "png"
    WEBP = "webp"
    GIF = "gif"
    BMP = "bmp"
    MP4 = "mp4"
    MOV = "mov"
    WEBM = "webm"
    MP3 = "mp3"
    OGG = "ogg"
    TGS = "tgs"          # 动画贴片 (gzip/Lottie JSON)
    SERIALIZED_VIDEO = "serialized_video"  # 序列化视频 (需反序列化)
    VIDEO_SLICE = "video_slice"            # 8MB 视频分片 (大视频数据块)
    PARTIAL_JPEG = "partial_jpeg"          # partial: 前缀的 JPEG
    UNKNOWN_FRAGMENT = "unknown_fragment"  # 未知碎片 (可能是未完成下载)
    UNKNOWN = "unknown"


# magic bytes 签名表 (按优先级排序)
FILE_SIGNATURES = [
    # [签名, 偏移, 类型]
    (b'\xff\xd8\xff', 0, FileType.JPEG),
    (b'\x89PNG', 0, FileType.PNG),
    (b'RIFF', 0, FileType.WEBP),       # WebP 也以 RIFF 开头
    (b'GIF8', 0, FileType.GIF),
    (b'BM', 0, FileType.BMP),
    (b'\x1a\x45\xdf\xa3', 0, FileType.WEBM),  # EBML/WebM
    (b'\xff\xfb', 0, FileType.MP3),
    (b'\xff\xf3', 0, FileType.MP3),
    (b'\xff\xf2', 0, FileType.MP3),
    (b'ID3', 0, FileType.MP3),
    (b'OggS', 0, FileType.OGG),
    (b'\x1f\x8b\x08', 0, FileType.TGS),  # gzip (动画贴片)
]

# 大视频分片大小 (8 MiB = 64 * 128KB)
K_IN_SLICE = 8388608

# ftyp box 检查 (MP4/MOV)
MP4_BRANDS = {b'isom', b'mp42', b'qt  ', b'qt  ', b'M4V ', b'iso2', b'avc1', b'mp41'}


def _has_valid_moov(data: bytes) -> bool:
    """
    检查数据中是否包含结构合法的 moov atom。

    旧实现是 `data.find(b'moov') >= 0` —— 子串搜索在二进制媒体负载上会频繁
    误命中 (H.264/H.265 的 mdat 里出现 ASCII "moov" 是常态), 于是损坏文件
    会被判成"有 moov"。现在改为真正遍历顶层 box。
    """
    from .mp4 import is_valid_moov_bytes
    return is_valid_moov_bytes(data)


def _probe_top_level_boxes(data: bytes) -> Tuple[List[int], bool, bool]:
    """
    遍历顶层 box, 返回 (ftyp 的起始偏移列表, 是否有 moov, 是否有 mdat)。

    全部基于 box 的 size/type, 不做任何子串搜索。
    """
    from .mp4 import Reader, iter_boxes
    ftyp_offsets: List[int] = []
    has_moov = False
    has_mdat = False
    try:
        reader = Reader(data)
        for b in iter_boxes(reader, 0, len(data)):
            if b.type == b'ftyp':
                ftyp_offsets.append(b.start)
            elif b.type == b'moov':
                has_moov = True
            elif b.type == b'mdat':
                has_mdat = True
    except Exception:
        pass
    return ftyp_offsets, has_moov, has_mdat


def identify_file_type(data: bytes) -> FileType:
    """
    通过 magic bytes 识别解密后的文件类型

    检查顺序 (严格按此顺序, 避免误判):
    1. partial: 前缀 (渐进式加载的 JPEG 预览)
    2. 标准 magic bytes 签名 (WebM 等)
    3. 8MB 视频分片 (精确大小匹配, 必须在 ftyp 检查之前)
    4. MP4 ftyp box at offset 4 + moov 验证 (标准 MP4)
    5. 序列化视频 (slice/part 结构, ftyp 在非标准偏移)
    6. 深层签名扫描 (偏移 0-4096)

    关于 atom 判定: ftyp/moov/mdat 的存在性由**顶层 box 遍历**得出, 不再用
    `data.find(b'moov')` 之类子串搜索 (随机字节很容易命中)。只有在分类阶段,
    为了识别"元数据前缀 + ftyp"的序列化布局, 才在前 256 字节内做一次有界查找,
    并且它只影响"归为哪一类", 不参与任何正确性判定。
    """
    if len(data) < 4:
        return FileType.UNKNOWN

    # 1. 检查 partial: 前缀
    if len(data) > 10 and data[:8] == b'partial:':
        payload = data[8:]
        if payload[:3] == b'\xff\xd8\xff':
            return FileType.PARTIAL_JPEG

    # 2. 检查标准签名
    for sig, offset, ftype in FILE_SIGNATURES:
        if len(data) >= len(sig) + offset and data[offset:offset + len(sig)] == sig:
            # WebP 需要额外确认: RIFF....WEBP
            if ftype == FileType.WEBP:
                if len(data) > 12 and data[8:12] == b'WEBP':
                    return FileType.WEBP
                else:
                    continue  # RIFF 但不是 WebP, 继续检查
            return ftype

    # 3. 检查 8MB 视频分片 (必须在 ftyp 检查之前!)
    #    8MB 纯数据块可能碰巧在 offset 4 包含 ftyp, 但没有 moov, 不是有效 MP4
    if len(data) == K_IN_SLICE:
        return FileType.VIDEO_SLICE

    ftyp_offsets, has_moov, has_mdat = _probe_top_level_boxes(data)
    ftyp_at_standard = 4 in ftyp_offsets

    # 4. 检查 MP4 ftyp box (标准位置: 偏移4) + 验证 moov 存在
    if len(data) > 12 and ftyp_at_standard:
        # 标准位置 ftyp: 需要验证是否真正包含 moov
        # (8MB 分片或损坏文件可能只有 ftyp+free, 没有 moov)
        if has_moov:
            return FileType.MP4
        # ftyp 在标准位置但无 moov: 可能是序列化视频头部或不完整缓存
        if _check_serialized_video(data) is not None:
            return FileType.SERIALIZED_VIDEO
        # 无 moov 也无序列化结构: 仍算 MP4 (可能是不完整缓存)
        return FileType.MP4

    # 5. 检查序列化视频 (ftyp 在非标准偏移, 带序列化头部)
    #    格式: count(4B) + padding + [size(4B) + ftyp...]
    #    这些文件的 ftyp 前有序列化元数据, 不是标准 MP4
    if any(4 < off < 64 for off in ftyp_offsets):
        return FileType.SERIALIZED_VIDEO

    # 5b. 检查序列化视频 (slice/part 结构, 无 ftyp)
    if len(data) > 16:
        ftype = _check_serialized_video(data)
        if ftype is not None:
            return ftype

    # 6. 搜索已知签名 (深层扫描, 偏移 0-4096)
    for sig, offset, ftype in FILE_SIGNATURES:
        pos = data.find(sig, 0, 4096)
        if pos > 0 and pos < 4096:
            if ftype == FileType.WEBP and data[pos+8:pos+12] != b'WEBP':
                continue
            return ftype

    return FileType.UNKNOWN


def _check_serialized_video(data: bytes) -> Optional[FileType]:
    """
    检查是否是序列化视频文件

    支持两种 Telegram 序列化格式:
    1. slice→part 结构: slice_count(4B LE) + [part_count(4B LE) + [offset(4B)+size(4B)+data] * N] * N
    2. 简化序列化: slice_count(4B LE) + padding(4B) + chunk_size(4B, 通常 0x00020000) + data

    内部对"是否含视频签名"的判定走顶层 box 遍历; 对"ftyp 是否在非标准偏移"
    的判定用一次前 256 字节的有界查找 (序列化元数据本身就只占开头几十字节),
    它只影响分类结果。
    """
    MAX_PARTS_COUNT = 80
    MAX_PART_SIZE = 128 * 1024

    if len(data) < 12:
        return None

    slice_count = struct.unpack('<I', data[:4])[0]
    if slice_count == 0 or slice_count > MAX_PARTS_COUNT:
        return None

    def _has_video_signature() -> bool:
        _ftyp, has_moov, has_mdat = _probe_top_level_boxes(data)
        if has_moov or has_mdat:
            return True
        hint = data.find(b'ftyp', 0, 256)
        return 0 < hint < 256

    # 模式2: 简化序列化格式 — data[8:12] == 0x00020000 (131072, 固定块大小)
    if data[8:12] == b'\x00\x00\x02\x00':
        return FileType.SERIALIZED_VIDEO if _has_video_signature() else None

    # 模式3: 另一种变体 — 01 00 00 00 28 00 00 00 (slice_count=1, part_count=40)
    if data[:8] == b'\x01\x00\x00\x00\x28\x00\x00\x00':
        return FileType.SERIALIZED_VIDEO if _has_video_signature() else None

    # 模式1: 标准 slice→part 结构验证
    pos = 4
    for _ in range(slice_count):
        if pos + 4 > len(data):
            return None
        part_count = struct.unpack('<I', data[pos:pos + 4])[0]
        pos += 4
        if part_count == 0 or part_count > MAX_PARTS_COUNT:
            return None

        for _ in range(part_count):
            if pos + 8 > len(data):
                return None
            out_offset = struct.unpack('<I', data[pos:pos + 4])[0]
            part_size = struct.unpack('<I', data[pos + 4:pos + 8])[0]
            pos += 8
            if part_size == 0 or part_size > MAX_PART_SIZE:
                return None
            pos += part_size  # 跳过 part 数据
            if pos > len(data) + 16:  # 允许末尾有一些额外字节
                return None
        break  # 只检查第一个 slice

    # 结构合理且有视频签名 -> 序列化视频
    if _has_video_signature():
        return FileType.SERIALIZED_VIDEO

    # 结构合理但没有任何视频签名 → 可能是纯数据分片, 不判定为序列化视频
    return None


# ==================== 文件类型分类 ====================

def is_large_video_header(data: bytes) -> bool:
    """检测序列化视频是否是大视频头部 (含全局偏移 > 8MB)"""
    try:
        if len(data) < 12:
            return False
        count = struct.unpack('<I', data[:4])[0]
        if count == 0 or count > 200:
            return False
        offset = 4
        for _ in range(count):
            if offset + 8 > len(data):
                break
            part_off = struct.unpack('<I', data[offset:offset + 4])[0]
            part_size = struct.unpack('<I', data[offset + 4:offset + 8])[0]
            offset += 8
            if part_size == 0 or part_size > 256 * 1024:
                break
            if part_off > K_IN_SLICE:
                return True
            offset += part_size
            if offset > len(data):
                break
        return False
    except Exception:
        return False


def is_image(ftype: FileType) -> bool:
    """是否是图片类型"""
    return ftype in (FileType.JPEG, FileType.PNG, FileType.WEBP, FileType.GIF, FileType.BMP, FileType.PARTIAL_JPEG)

def is_video(ftype: FileType) -> bool:
    """是否是视频类型"""
    return ftype in (FileType.MP4, FileType.MOV, FileType.WEBM, FileType.SERIALIZED_VIDEO, FileType.VIDEO_SLICE)

def is_audio(ftype: FileType) -> bool:
    """是否是音频类型"""
    return ftype in (FileType.MP3, FileType.OGG)

def is_sticker(ftype: FileType) -> bool:
    """是否是动画贴片"""
    return ftype == FileType.TGS


def decompress_tgs(data: bytes) -> Optional[str]:
    """
    解压 TGS 动画贴纸数据, 返回 Lottie JSON 字符串
    
    Telegram 的 TGS 文件使用非标准 gzip 头,
    标准 gzip.decompress() 会失败, 需用 zlib wbits=31 解压
    """
    try:
        decompressed = zlib.decompress(data, wbits=31)
        return decompressed.decode('utf-8', errors='replace')
    except Exception:
        try:
            # 尝试手动跳过 gzip 头, 用 raw deflate 解压
            if len(data) < 10:
                return None
            flg = data[3]
            header_len = 10
            if flg & 0x08:  # FNAME
                null_pos = data.index(0, 10)
                header_len = null_pos + 1
            raw_deflate = data[header_len:]
            if len(raw_deflate) > 8:
                raw_deflate = raw_deflate[:-8]  # 去掉 CRC32 + ISIZE
            decompressed = zlib.decompress(raw_deflate, wbits=-15)
            return decompressed.decode('utf-8', errors='replace')
        except Exception:
            return None


def get_hex_preview(data: bytes, length: int = 32) -> str:
    """获取文件头部的十六进制预览字符串"""
    return data[:length].hex(' ')


def detect_fragment_type(data: bytes) -> str:
    """
    检测未知碎片中可能包含的内容类型
    返回人类可读的描述
    """
    fragments = []
    # 搜索常见签名 (深层扫描)
    if data.find(b'ftyp') >= 0:
        fragments.append("MP4 碎片")
    if data.find(b'moov') >= 0:
        fragments.append("moov 元数据")
    if data.find(b'mdat') >= 0:
        fragments.append("mdat 数据")
    if data.find(b'\xff\xd8\xff') >= 0:
        fragments.append("JPEG 碎片")
    if data.find(b'\x89PNG') >= 0:
        fragments.append("PNG 碎片")
    if data.find(b'ID3') >= 0:
        fragments.append("MP3 碎片")
    if data.find(b'OggS') >= 0:
        fragments.append("OGG 碎片")
    
    if fragments:
        return "可能包含: " + "、".join(fragments)
    return "无已知文件签名"


# ==================== 扫描结果数据结构 ====================

@dataclass
class CacheFile:
    """单个缓存文件信息"""
    file_id: str               # 唯一ID (文件名的 hex)
    file_name: str             # 原始文件名
    file_path: str             # 完整路径
    relative_path: str         # 相对于 cache/media_cache 的路径
    cache_type: str            # "cache" 或 "media_cache"
    file_size: int             # 加密文件大小 (字节)
    decrypted_size: int        # 解密后大小 (字节)
    file_type: FileType        # 文件类型
    file_type_label: str       # 文件类型显示名称
    category: str              # 大类: image/video/audio/sticker/unknown
    is_serialized: bool                # 是否是序列化视频
    is_large_video: bool = False       # 是否是大视频头部 (需 8MB 分片补全)
    is_complete_large_video: bool = False  # 大视频且所有分片齐全 (可一键重建)
    slices_needed: int = -1            # 大视频需要的分片数 (-1=未解析, 0=未知, >0=需要外部slice)
    decrypted_data_hash: str = ""      # 解密数据的 SHA256 (用于去重)
    # ---- 覆盖率 / 完整性 (重构后新增; 由 server 的扫描富化阶段填充) ----
    coverage_json: str = ""            # Coverage 的 JSON (区间 + 指纹)
    coverage_signature: str = ""       # 覆盖率指纹 (用于导出发失效)
    covered_bytes: int = 0             # 已覆盖字节数
    total_size: int = 0                # moov 声明的完整文件大小
    missing_slice_indices: List[int] = field(default_factory=list)
    moov_present: bool = False
    playable_duration_s: float = 0.0   # 连续覆盖前缀能播出的时长
    missing_reason: str = ""           # 判定为不完整时的原因 (用于 UI 展示)
    # 缩略图相关 (运行时生成, 不持久化)
    thumbnail_path: Optional[str] = None


@dataclass
class ScanResult:
    """扫描结果"""
    total_files: int = 0
    decrypted_files: int = 0
    failed_files: int = 0
    by_category: Dict[str, int] = field(default_factory=dict)
    by_type: Dict[str, int] = field(default_factory=dict)
    files: List[CacheFile] = field(default_factory=list)
    scan_time: float = 0.0


# ==================== 类型标签映射 ====================

TYPE_LABELS = {
    FileType.JPEG: "JPEG 图片",
    FileType.PNG: "PNG 图片",
    FileType.WEBP: "WebP 图片",
    FileType.GIF: "GIF 动图",
    FileType.BMP: "BMP 图片",
    FileType.MP4: "MP4 视频",
    FileType.MOV: "MOV 视频",
    FileType.WEBM: "WebM 视频",
    FileType.MP3: "MP3 音频",
    FileType.OGG: "OGG 音频",
    FileType.TGS: "动画贴片",
    FileType.SERIALIZED_VIDEO: "序列化视频",
    FileType.VIDEO_SLICE: "视频分片",
    FileType.PARTIAL_JPEG: "预览图片",
    FileType.UNKNOWN_FRAGMENT: "未知碎片",
    FileType.UNKNOWN: "未知类型",
}

CATEGORY_MAP = {
    FileType.JPEG: "image",
    FileType.PNG: "image",
    FileType.WEBP: "image",
    FileType.GIF: "image",
    FileType.BMP: "image",
    FileType.PARTIAL_JPEG: "image",
    FileType.MP4: "video",
    FileType.MOV: "video",
    FileType.WEBM: "video",
    FileType.SERIALIZED_VIDEO: "video",
    FileType.VIDEO_SLICE: "slice",
    FileType.MP3: "audio",
    FileType.OGG: "audio",
    FileType.TGS: "sticker",
    FileType.UNKNOWN_FRAGMENT: "unknown",
    FileType.UNKNOWN: "unknown",
}


# ==================== 缓存扫描器 ====================

class CacheScanner:
    """扫描并解密 Telegram Desktop 缓存文件"""

    # 跳过的特殊文件
    SKIP_FILES = {'version', 'binlog'}

    def __init__(self, tdata_path: str, passcode: bytes = b''):
        """
        Args:
            tdata_path: tdata 目录路径
            passcode: 本地密码 (无密码时为空)
        """
        self.tdata_path = tdata_path
        self.passcode = passcode
        self.local_key: Optional[bytes] = None
        self._key_loaded = False

    def load_key(self) -> bool:
        """加载 LocalKey"""
        key_datas_path = os.path.join(self.tdata_path, "key_datas")
        if not os.path.exists(key_datas_path):
            raise FileNotFoundError(f"key_datas not found: {key_datas_path}")
        self.local_key = extract_local_key(key_datas_path, self.passcode)
        self._key_loaded = True
        return True

    def scan(self, progress_callback=None) -> ScanResult:
        """
        扫描所有缓存文件

        Args:
            progress_callback: 可选回调函数 callback(current, total, filename)
        Returns:
            ScanResult 扫描结果
        """
        if not self._key_loaded:
            self.load_key()

        import time
        start_time = time.time()

        result = ScanResult()

        # 收集所有缓存文件路径
        cache_dirs = {
            'cache': os.path.join(self.tdata_path, "user_data", "cache"),
            'media_cache': os.path.join(self.tdata_path, "user_data", "media_cache"),
        }

        all_files = []
        for cache_type, cache_dir in cache_dirs.items():
            if not os.path.exists(cache_dir):
                continue
            for root, dirs, files in os.walk(cache_dir):
                for fname in files:
                    if fname in self.SKIP_FILES:
                        continue
                    fpath = os.path.join(root, fname)
                    rel_path = os.path.relpath(fpath, cache_dir)
                    all_files.append((fpath, fname, rel_path, cache_type))

        result.total_files = len(all_files)

        for idx, (fpath, fname, rel_path, cache_type) in enumerate(all_files):
            if progress_callback:
                progress_callback(idx + 1, result.total_files, fname)

            try:
                fsize = os.path.getsize(fpath)
                decrypted = decrypt_tdef_file(fpath, self.local_key)
                ftype = identify_file_type(decrypted)

                # 检测大视频头部 (序列化视频 + 全局偏移)
                # 也检测被识别为 MP4 但实际是复杂序列化格式的文件
                is_large = False
                if ftype == FileType.SERIALIZED_VIDEO:
                    is_large = is_large_video_header(decrypted)
                elif ftype == FileType.MP4 and len(decrypted) > 16:
                    # MP4 可能是包含 ftyp 的大视频 header (复杂序列化格式)
                    is_large = is_large_video_header(decrypted)
                    if is_large:
                        # 重新标记为序列化视频
                        ftype = FileType.SERIALIZED_VIDEO

                # 计算解密数据 hash (用于去重)
                data_hash = hashlib.sha256(decrypted[:4096]).hexdigest()[:16]

                cache_file = CacheFile(
                    file_id=fname,
                    file_name=fname,
                    file_path=fpath,
                    relative_path=rel_path,
                    cache_type=cache_type,
                    file_size=fsize,
                    decrypted_size=len(decrypted),
                    file_type=ftype,
                    file_type_label=TYPE_LABELS.get(ftype, "未知类型"),
                    category=CATEGORY_MAP.get(ftype, "unknown"),
                    is_serialized=(ftype == FileType.SERIALIZED_VIDEO),
                    is_large_video=is_large,
                    decrypted_data_hash=data_hash,
                )
                result.files.append(cache_file)
                result.decrypted_files += 1

                # 统计
                cat = cache_file.category
                result.by_category[cat] = result.by_category.get(cat, 0) + 1
                result.by_type[ftype.value] = result.by_type.get(ftype.value, 0) + 1

            except Exception as e:
                result.failed_files += 1

        result.scan_time = time.time() - start_time
        return result

    def get_decrypted_data(self, file_id: str) -> Optional[bytes]:
        """获取指定文件的解密数据"""
        if not self._key_loaded:
            self.load_key()
        # file_id 就是文件名
        fpath = os.path.join(self.tdata_path, "user_data", "cache", file_id)
        if not os.path.exists(fpath):
            # 尝试 media_cache
            fpath = os.path.join(self.tdata_path, "user_data", "media_cache", file_id)
            if not os.path.exists(fpath):
                # 递归搜索
                for root, dirs, files in os.walk(os.path.join(self.tdata_path, "user_data")):
                    if file_id in files:
                        fpath = os.path.join(root, file_id)
                        break
                else:
                    return None
        return decrypt_tdef_file(fpath, self.local_key)
