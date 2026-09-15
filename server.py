"""
Telegram Desktop 缓存数据管理器 — Flask 后端

提供 REST API:
  GET  /api/scan           — 扫描缓存文件
  GET  /api/files          — 获取文件列表 (支持筛选/分页)
  GET  /api/file/<id>      — 获取单个文件详情
  GET  /api/preview/<id>   — 预览文件 (返回原始解密数据)
  GET  /api/thumbnail/<id> — 获取缩略图
  POST /api/export/<id>    — 导出单个文件
  POST /api/export_batch   — 批量导出
  GET  /api/stats          — 获取统计信息
  GET  /api/exports        — 获取已导出文件列表
  DELETE /api/exports/<filename> — 删除导出文件
"""

import os
import sys
import json
import re
import hashlib
import time
import threading
import tempfile
import subprocess
import ctypes
import mimetypes
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from typing import Optional, Dict, List, Tuple

from flask import Flask, request, jsonify, send_file, send_from_directory, Response, render_template

# ==================== 路径处理 (兼容 PyInstaller 打包) ====================

def _get_base_dir():
    """获取程序基目录: 打包后为 exe 同级目录, 开发时为脚本目录"""
    if getattr(sys, 'frozen', False):
        # PyInstaller 打包: exe 所在目录
        return os.path.dirname(sys.executable)
    else:
        # 开发模式: 脚本所在目录
        return os.path.dirname(os.path.abspath(__file__))

def _get_resource_dir():
    """获取资源目录: 打包后为 _MEIPASS 临时解压目录, 开发时为脚本目录"""
    if getattr(sys, 'frozen', False):
        return sys._MEIPASS
    else:
        return os.path.dirname(os.path.abspath(__file__))

# BASE_DIR: exe/data 同级目录 (exports, thumbnails 等可写目录)
# RES_DIR:  资源目录 (templates, static 等只读资源)
BASE_DIR = _get_base_dir()
RES_DIR = _get_resource_dir()
sys.path.insert(0, RES_DIR)

from src.scanner import CacheScanner, CacheFile, ScanResult, FileType, TYPE_LABELS, CATEGORY_MAP, is_image, is_video, is_audio, is_sticker, decompress_tgs, get_hex_preview, detect_fragment_type, is_large_video_header, identify_file_type
from src.crypto import decrypt_tdef_file
from src.exporter import (export_file, generate_thumbnail, get_export_filename,
                          get_video_metadata, _video_thumbnail_from_path)
from src.deserializer import (deserialize_video, get_video_info,
                              get_large_video_info, media_part_extents)
from src.binlog import parse_binlog, build_key_index, get_slices_for_header, get_slice_records_for_header, BinlogRecord
from src.locations import LocationIndex, FileLocation, DownloadRecord

# ---- 重构后新增: 覆盖率 / 流式重建 / 导出管线 ----
from src.coverage import K_IN_SLICE
from src import mp4
from src.mp4 import validate as mp4_validate
from src.rebuild import (
    RebuildResult, SliceSource,
    analyze as analyze_large_video, analyze_extents,
    rebuild_large_video_to_file, repack_from_extents, serialized_extents,
)
from src.export_pipeline import (
    PIPELINE_VERSION, ExportOutcome, cleanup_stale, ffmpeg_capabilities,
    is_current, is_media_filename, part_path, produce, read_sidecar,
    sidecar_path, staging_path,
)

app = Flask(__name__,
            template_folder=os.path.join(RES_DIR, 'templates'),
            static_folder=os.path.join(RES_DIR, 'static'))

# ==================== 全局配置 ====================

class Config:
    """运行时配置"""
    tdata_path: str = r"D:\Software\Telegram Desktop\tdata"
    download_path: str = ''
    export_dir: str = os.path.join(BASE_DIR, "exports")
    thumbnail_dir: str = os.path.join(BASE_DIR, "thumbnails")
    passcode: bytes = b''

    @classmethod
    def auto_detect_download_path(cls) -> str:
        """自动检测 Telegram 下载目录"""
        candidates = [
            os.path.join(os.path.expanduser('~'), 'Downloads', 'Telegram Desktop'),
        ]
        if cls.tdata_path:
            # tdata 同级目录下的 Downloads
            tdata_parent = os.path.dirname(cls.tdata_path)
            candidates.append(os.path.join(tdata_parent, 'Downloads'))
        for c in candidates:
            if os.path.isdir(c):
                return c
        return ''

    @classmethod
    def ensure_dirs(cls):
        os.makedirs(cls.export_dir, exist_ok=True)
        os.makedirs(cls.thumbnail_dir, exist_ok=True)


Config.ensure_dirs()
Config.download_path = Config.auto_detect_download_path()

# ==================== 全局状态 ====================

scanner: Optional[CacheScanner] = None
scan_result = None  # ScanResult 对象
scan_lock = threading.Lock()
# 文件列表操作锁: 保护 scan_result.files 的增删 (防止并发删除)
files_lock = threading.Lock()
# 文件签名表: file_id → (mtime, size)，用于增量扫描
last_file_signatures: Dict[str, tuple] = {}
scan_status = {
    'scanning': False,
    'progress': 0,
    'total': 0,
    'current_file': '',
    'finished': False,
    'error': None,
    'binlog_locked': False,
    'binlog_available': False,
}

# binlog 索引: 文件名 → BinlogRecord
# 用于关联大视频 header 与 8MB slice
binlog_index: Optional[Dict[str, BinlogRecord]] = None

# locations + downloads 索引
# 用于 document_id → {peerId, msgId, tg_url} 映射
location_index: Optional[LocationIndex] = None

# 下载文件缓存 (模块级, scan 时填充)
download_files_cache: List[dict] = []

# 异步任务管理: task_id → task_dict
export_tasks: Dict[str, dict] = {}

# 文件索引: file_id → CacheFile (扫描完成后构建, 避免每次 O(n) 查找)
file_index: Dict[str, CacheFile] = {}
# file_name → CacheFile 索引, 用于按文件名快速查找 (避免 O(n) 遍历)
file_name_index: Dict[str, CacheFile] = {}

# 大视频 header 索引: doc_key (key_high, key_low>>16) → header 文件名
# 由 _build_large_video_header_index 构建, 避免 cache_file_to_dict 内 O(n²) 扫描
# 注意: 不能只按 key_high —— 不同视频会共享 key_high (取证确认)
large_video_header_index: Dict[tuple, str] = {}


def build_file_index():
    """扫描完成后构建 file_id → CacheFile 索引"""
    global file_index, file_name_index
    file_index = {}
    file_name_index = {}
    if scan_result:
        for f in scan_result.files:
            file_index[f.file_id] = f
            file_name_index[f.file_name] = f
    _rebuild_exported_ids()


def find_file_by_id(file_id: str) -> Optional[CacheFile]:
    """在扫描结果中查找文件 (O(1) 索引)"""
    return file_index.get(file_id)


def _has_valid_thumbnail(f: CacheFile) -> bool:
    """检查文件是否有有效缩略图 (文件存在且大小>0)"""
    # 检查 thumbnail_path 属性
    if f.thumbnail_path is not None:
        try:
            if os.path.exists(f.thumbnail_path) and os.path.getsize(f.thumbnail_path) > 0:
                return True
        except Exception:
            pass
    # 检查缩略图目录
    thumb_path = os.path.join(Config.thumbnail_dir, f"{f.file_id}.jpg")
    try:
        return os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0
    except Exception:
        return False


# 导出文件 ID 集合 (扫描后构建, 用于 O(1) 判断是否已导出)
_exported_ids: set = set()

# path -> 已验证过的文件大小 (避免每次请求都重跑 box 校验)
_export_verified: Dict[str, int] = {}
_export_verify_lock = threading.Lock()


def _is_hex_file_id(name: str) -> bool:
    """TG 缓存文件名是大写十六进制 (8~64 位)"""
    if not (8 <= len(name) <= 64):
        return False
    return all(c in '0123456789ABCDEFabcdef' for c in name)


def _current_signature(file_id: str) -> Optional[str]:
    """
    该文件当前的覆盖率指纹 (用于判断导出物是否已过期)。

    非大视频没有覆盖率信息, 返回 None 表示"只校验版本与文件本身"。
    """
    f = file_index.get(file_id)
    if f is None:
        return None
    return f.coverage_signature or None


def _export_path_for(file_id: str) -> Optional[str]:
    """该 file_id 对应的导出文件路径 (按当前文件类型推断扩展名)"""
    f = file_index.get(file_id)
    if f is None:
        return None
    return os.path.join(Config.export_dir, get_export_filename(file_id, f.file_type))


def _export_valid_for(file_id: str) -> bool:
    """
    导出物是否存在、仍然有效、且结构自洽 —— 可以直接拿去播放。

    三层检查:
      1. sidecar 存在且版本 / 覆盖率指纹 / 尺寸 / mtime 全部匹配 (is_current)
      2. 仅对**视频**做结构校验 (真正的 box 遍历, 每个 chunk 都落在文件内)
         —— 图片/音频不是 MP4, 不能拿 MP4 校验去卡它们
      3. 结果按 (路径, 大小) 缓存, 所以播放时的多次 Range 请求不会反复校验
    """
    path = _export_path_for(file_id)
    if not path or not os.path.exists(path):
        return False
    if not is_current(path, _current_signature(file_id)):
        return False

    f = file_index.get(file_id)
    if f is None or not is_video(f.file_type):
        return True                       # 非视频: sidecar 校验通过即可

    try:
        size = os.path.getsize(path)
    except OSError:
        return False
    if _export_verified.get(path) == size:
        return True
    if not _is_valid_mp4_export(path):
        return False
    with _export_verify_lock:
        _export_verified[path] = size
    return True


def _rebuild_exported_ids():
    """
    扫描导出目录, 构建 file_id 集合。

    必须用**媒体扩展名白名单**过滤。目录里还会存在这些非导出文件:
        <id>.mp4.part       重建/转封装的待提交产物
        <id>.mp4.part.src   重建的暂存产物
        <id>.mp4.json       sidecar
        .preview/           预览产物池 (子目录)
    旧实现用 os.path.splitext 取扩展名, 于是 "ABCD.part" 会被当成 "ABCD" ——
    一旦真的引入 .part 文件, 卡片就会在重建尚未完成时显示"已重建"。

    同时要求 sidecar 校验通过 (is_current): 这会让**没有 sidecar 的历史导出**
    以及**分片集合变化前生成的旧导出**自动失效, 不会再被当成有效产物复用。
    """
    global _exported_ids
    _exported_ids = set()
    try:
        names = os.listdir(Config.export_dir)
    except OSError:
        return
    for fname in names:
        if not is_media_filename(fname):
            continue
        stem = os.path.splitext(fname)[0]
        file_id = stem.split('_')[0] if '_' in stem else stem
        if not _is_hex_file_id(file_id):
            continue
        path = os.path.join(Config.export_dir, fname)
        if not is_current(path, _current_signature(file_id)):
            continue
        _exported_ids.add(file_id)


def _export_exists(file_id: str) -> bool:
    """检查导出文件是否存在**且仍然有效** (O(1) 查索引)"""
    return file_id in _exported_ids


def _remove_export_file(file_id: str) -> bool:
    """
    删除 file_id 对应的导出文件及其元数据, 返回是否删除成功。

    除了 <id>.<ext>, 还要清掉 sidecar 与所有 .part 中间产物 ——
    否则残留的 sidecar 会让下一次导出被误判为"已是最新"。
    """
    if not os.path.exists(Config.export_dir):
        return False
    prefix = f"{file_id}."
    deleted = False
    for fname in os.listdir(Config.export_dir):
        if fname.startswith(prefix):
            path = os.path.join(Config.export_dir, fname)
            try:
                if os.path.isfile(path):
                    os.remove(path)
                    deleted = True
                elif os.path.isdir(path):
                    import shutil as _shutil
                    _shutil.rmtree(path, ignore_errors=True)
                    deleted = True
            except Exception:
                pass
    _exported_ids.discard(file_id)
    # 同步清掉校验缓存, 否则同名新文件会被旧的大小记录判为"已验证"
    dir_prefix = os.path.join(Config.export_dir, prefix)
    with _export_verify_lock:
        for key in [k for k in _export_verified if k.startswith(dir_prefix)]:
            _export_verified.pop(key, None)
    return deleted


# ==================== 增量扫描辅助 ====================

def _collect_file_signatures(tdata_path: str) -> Dict[str, tuple]:
    """快速收集磁盘所有缓存文件签名 (只 stat, 不解密)
    返回 {file_id: (mtime, size, fpath, rel_path, cache_type)}
    """
    sigs = {}
    cache_dirs = {
        'cache': os.path.join(tdata_path, "user_data", "cache"),
        'media_cache': os.path.join(tdata_path, "user_data", "media_cache"),
    }
    skip = {'version', 'binlog'}
    for cache_type, cache_dir in cache_dirs.items():
        if not os.path.exists(cache_dir):
            continue
        for root, dirs, files in os.walk(cache_dir):
            for fname in files:
                if fname in skip:
                    continue
                fpath = os.path.join(root, fname)
                try:
                    st = os.stat(fpath)
                    rel_path = os.path.relpath(fpath, cache_dir)
                    sigs[fname] = (st.st_mtime, st.st_size, fpath, rel_path, cache_type)
                except OSError:
                    pass
    return sigs


def _decrypt_and_classify(fpath: str, fname: str, rel_path: str, cache_type: str,
                          fsize: int, local_key: bytes) -> Optional[CacheFile]:
    """解密单个文件并分类, 返回 CacheFile 或 None (失败时)"""
    try:
        decrypted = decrypt_tdef_file(fpath, local_key)
        ftype = identify_file_type(decrypted)
        is_large = False
        slices_needed = -1
        if ftype == FileType.SERIALIZED_VIDEO:
            is_large = is_large_video_header(decrypted)
            if is_large:
                try:
                    lv_info = get_large_video_info(decrypted)
                    slices_needed = lv_info.get('slices_needed', -1)
                except Exception:
                    pass
        elif ftype == FileType.MP4 and len(decrypted) > 16:
            is_large = is_large_video_header(decrypted)
            if is_large:
                ftype = FileType.SERIALIZED_VIDEO
                try:
                    lv_info = get_large_video_info(decrypted)
                    slices_needed = lv_info.get('slices_needed', -1)
                except Exception:
                    pass
        data_hash = hashlib.sha256(decrypted[:4096]).hexdigest()[:16]
        return CacheFile(
            file_id=fname, file_name=fname, file_path=fpath,
            relative_path=rel_path, cache_type=cache_type,
            file_size=fsize, decrypted_size=len(decrypted),
            file_type=ftype,
            file_type_label=TYPE_LABELS.get(ftype, "未知类型"),
            category=CATEGORY_MAP.get(ftype, "unknown"),
            is_serialized=(ftype == FileType.SERIALIZED_VIDEO),
            is_large_video=is_large,
            decrypted_data_hash=data_hash,
            slices_needed=slices_needed,
        )
    except Exception:
        return None


def incremental_scan(s: CacheScanner, progress_callback=None) -> 'ScanResult':
    """增量扫描: 复用未变化文件, 只解密新增/修改文件"""
    global last_file_signatures, scan_result

    start_time = time.time()
    result = ScanResult()

    # 快速收集磁盘文件签名 (只 stat, 不解密)
    current_sigs = _collect_file_signatures(Config.tdata_path)
    result.total_files = len(current_sigs)

    # 构建上次结果的 file_id → CacheFile 映射
    prev_files_by_id: Dict[str, CacheFile] = {}
    if scan_result:
        for f in scan_result.files:
            prev_files_by_id[f.file_id] = f

    # 分类: 未变化 / 新增 / 修改 / 删除
    reused = 0
    to_decrypt = []
    deleted_ids = set(prev_files_by_id.keys()) - set(current_sigs.keys())

    for fname, sig in current_sigs.items():
        mtime, fsize, fpath, rel_path, cache_type = sig
        prev = prev_files_by_id.get(fname)
        prev_sig = last_file_signatures.get(fname)
        if prev and prev_sig and prev_sig[0] == mtime and prev_sig[1] == fsize:
            # 文件未变化: 复用
            result.files.append(prev)
            result.decrypted_files += 1
            cat = prev.category
            result.by_category[cat] = result.by_category.get(cat, 0) + 1
            result.by_type[prev.file_type.value] = result.by_type.get(prev.file_type.value, 0) + 1
            reused += 1
        else:
            # 新增或修改: 需要解密
            to_decrypt.append((fname, fpath, rel_path, cache_type, fsize, mtime))

    # 只解密变化的文件
    for idx, (fname, fpath, rel_path, cache_type, fsize, mtime) in enumerate(to_decrypt):
        if progress_callback:
            progress_callback(reused + idx + 1, result.total_files, fname)
        cf = _decrypt_and_classify(fpath, fname, rel_path, cache_type, fsize, s.local_key)
        if cf:
            result.files.append(cf)
            result.decrypted_files += 1
            cat = cf.category
            result.by_category[cat] = result.by_category.get(cat, 0) + 1
            result.by_type[cf.file_type.value] = result.by_type.get(cf.file_type.value, 0) + 1
        else:
            result.failed_files += 1

    # 处理已删除文件的导出清理 (可选: 不自动删除导出, 保留)
    result.scan_time = time.time() - start_time

    # 更新签名表
    last_file_signatures = {fname: (sig[0], sig[1]) for fname, sig in current_sigs.items()}

    return result


# ==================== 辅助函数 ====================

def get_scanner() -> CacheScanner:
    """获取或创建扫描器实例"""
    global scanner
    if scanner is None:
        scanner = CacheScanner(Config.tdata_path, Config.passcode)
        scanner.load_key()
    return scanner


def cache_file_to_dict(f: CacheFile) -> dict:
    """CacheFile 转 JSON 字典"""
    # 从 relative_path 提取分组信息
    # relative_path 格式: 1\XX\filename 或 filename
    parts = f.relative_path.replace('/', '\\').split('\\')
    bucket = ''
    if len(parts) >= 2 and len(parts[-2]) == 2:
        bucket = parts[-2]  # 如 "0A"
    
    # 检查是否已导出/已重建 (导出文件存在)
    is_rebuilt = _export_exists(f.file_id)

    # 从 binlog 中获取 key_high 与真实 document_id (用于"前往 Telegram 播放")
    key_high = 0
    real_doc_id = 0
    if binlog_index:
        record = binlog_index.get(f.file_name)
        if record:
            key_high = record.key_high
            real_doc_id = record.real_document_id

    # 合成率 (大视频): 已覆盖字节 / 媒体总大小, 保留两位小数。
    # 完整性判定仍以 coverage ⊇ required_ranges 为准 (完整 ⇔ 显示"完整");
    # 合成率用于排序与展示非完整视频的覆盖程度。
    synthesis_rate = None
    if f.is_large_video and f.total_size:
        synthesis_rate = round(
            min(int(f.covered_bytes or 0), int(f.total_size))
            / int(f.total_size) * 100, 2)

    # 分片信息: 查找父 header 和完整性 (使用预构建索引, O(1))
    # 归属口径与覆盖率判定一致: doc_key (key_high, key_low>>16)
    parent_header_id = ''
    parent_is_complete = False
    is_orphan_slice = False
    if f.file_type == FileType.VIDEO_SLICE and binlog_index and scan_result:
        record = binlog_index.get(f.file_name)
        if record:
            # 从 doc_key → header 索引中查找 (避免 O(n²) 扫描)
            header_name = large_video_header_index.get(record.doc_key, '')
            if header_name:
                header_file = file_index.get(header_name)
                if header_file:
                    parent_header_id = header_name
                    parent_is_complete = header_file.is_complete_large_video
        if not parent_header_id:
            is_orphan_slice = True

    # 获取显示名称 (从 locations/downloads 索引)
    display_name = ''
    if location_index and location_index.is_loaded:
        # 大视频 header 或普通序列化视频: 用 binlog key_high 作为 document_id 查找
        if key_high:
            entry = location_index.get_by_document_id(key_high)
            if entry and entry.get('download'):
                display_name = entry['download'].basename
        # 如果没找到, 尝试用文件名匹配 downloads 索引
        if not display_name:
            dl = location_index.get_download_by_filename(f.file_name)
            if dl:
                display_name = dl.basename

    return {
        'file_id': f.file_id,
        'file_name': f.file_name,
        'relative_path': f.relative_path,
        'cache_type': f.cache_type,
        'cache_type_label': '媒体缓存' if f.cache_type == 'media_cache' else '缩略图缓存',
        'bucket': bucket,
        'file_size': f.file_size,
        'decrypted_size': f.decrypted_size,
        'file_type': f.file_type.value,
        'file_type_label': f.file_type_label,
        'category': f.category,
        'is_serialized': f.is_serialized,
        'is_large_video': f.is_large_video,
        'is_complete_large_video': f.is_complete_large_video,
        'is_rebuilt': is_rebuilt,
        'has_thumbnail': _has_valid_thumbnail(f),
        'is_incomplete': False,  # 默认完整，api_file_detail 中按需设置
        'key_high': f'0x{key_high:016X}' if key_high else '',
        'document_id': f'0x{real_doc_id:016X}' if real_doc_id else '',
        'synthesis_rate': synthesis_rate,
        'display_name': display_name,
        'parent_header_id': parent_header_id,
        'parent_is_complete': parent_is_complete,
        'is_orphan_slice': is_orphan_slice,
        # ---- 覆盖率信息 (加性字段; 用于展示缺口与可播时长) ----
        'slices_needed': max(0, int(f.slices_needed or 0)),
        'total_size': int(f.total_size or 0),
        'covered_bytes': int(f.covered_bytes or 0),
        'missing_slice_indices': list(f.missing_slice_indices or []),
        'missing_reason': f.missing_reason or '',
        'moov_present': bool(f.moov_present),
        'playable_duration': round(f.playable_duration_s or 0.0, 3),
    }


# ==================== 视频重建 / 导出 (统一入口) ====================

def _slices_for_header(header_file_id: str, bidx=None,
                       header_data: Optional[bytes] = None) -> List[SliceSource]:
    """
    取该大视频 header 对应的、磁盘上真实存在的 8MB 分片。

    **宁严**: 只认 binlog 精确关联出来的分片。没有 binlog 就返回空列表 ——
    绝不按修改时间聚类去"猜"分片归属, 因为 Telegram 是多任务并行下载、乱序落盘,
    那种猜法会把另一部视频的分片缝进同一个文件, 产出看得到但内容是垃圾的 MP4。

    **2026-09-15 (真实 tdata 取证后修正)**:
    缓存文件自身是 ``[u32 part_count] + [(u32 out_offset, u32 size, data) * N] * G``
    的分区块结构, 每个块自带它在媒体文件里的**绝对** out_offset —— 这部分覆盖
    范围是确定事实, 并且**优先于**外部分片 (见 rebuild._write_prefix 的写出顺序)。

    但 header 单个文件最多只有 ~8MiB, 一部完整缓存的大视频必然还有后续的
    8MiB 裸分片, 所以**不能**因为有自描述区段就丢弃外部分片, 否则 30MB 的视频
    永远只能导出前 8MB。

    过去叠加外部分片会产出坏文件, 根因不在"叠加"本身, 而在归组口径:
    binlog 只按 key_high 归组, 把不同视频混成一组。现已修正为
    (key_high, key_low >> 16) —— 详见 src/binlog.py 的模块注释。

    Args:
        bidx: binlog 索引; 不传则用全局的 binlog_index
            (扫描富化阶段全局还没赋值, 必须显式传入)
        header_data: 可选的 header 明文 (保留参数, 当前不参与决策)
    """
    alias = bidx if bidx is not None else binlog_index
    if not alias or scan_result is None:
        return []
    with files_lock:
        all_fnames = {sf.file_name for sf in scan_result.files}
    try:
        records = get_slice_records_for_header(header_file_id, alias, all_fnames)
    except Exception:
        return []

    out: List[SliceSource] = []
    for idx, fname, binlog_size in records:
        cf = file_name_index.get(fname)
        if cf is not None:
            path = cf.file_path
            real_size = cf.decrypted_size if cf.decrypted_size > 0 else binlog_size
        else:
            path = os.path.join(Config.tdata_path, 'user_data', 'media_cache',
                                '1', fname)
            real_size = binlog_size
        # binlog slice_index -> 媒体文件字节偏移的映射 (2026-09-15 修正):
        #
        #   外部文件 binlog index i 覆盖 [(i-1)*8MiB, i*8MiB)
        #   index 0 是序列化 header 自身的记录 (不会出现在这里)
        #
        # 旧实现用 i*8MiB, 整体错位一格: 完整缓存的视频会被误报成
        # "缺 1 个分片"(且恰好是 index 1), 同时最后一个分片被摆到
        # **文件末尾之外** —— 数据不可能存在于文件之外, 这本身即铁证。
        # 取证: 两部完整缓存的视频 (398.15s/334MB 与 109.67s/97.8MB),
        # 按 (i-1)*8MiB 摆放后覆盖无缝、complete=True, 重打包产物
        # ffmpeg 全量解码 0 错误、时长与完整时长一致 (见 CHANGES.md 第 11 节)。
        place = max(idx - 1, 0)
        out.append(SliceSource(index=place, size=int(real_size),
                               name=fname, path=path))
    return out


def _decrypt_by_path(path: str) -> Optional[bytes]:
    """按路径解密一个缓存文件 (供分片按需解密)"""
    try:
        return decrypt_tdef_file(path, get_scanner().local_key)
    except Exception:
        return None


def _plain_stage(path: str) -> 'RebuildResult':
    """把"直接落盘"的产物包装成 produce() 能理解的 stage 结果"""
    st = RebuildResult(out_path=path, playable=True)
    try:
        st.size = os.path.getsize(path)
    except OSError:
        st.size = 0
    return st


def _fail_stage(path: str, reason: str) -> 'RebuildResult':
    res = RebuildResult(out_path=path)
    res.fail(reason)
    return res


def _expected_export_size_for(f: CacheFile) -> int:
    """导出前预估需要的磁盘空间 (用于 ENOSPC 预检)"""
    if f.is_large_video:
        return max(f.decrypted_size, f.total_size or 0, 8 << 20)
    if is_video(f.file_type):
        return max(f.decrypted_size * 2, 16 << 20)
    return max(f.decrypted_size * 2, 1 << 20)


def _stage_producer(f: CacheFile):
    """
    返回 (stage_fn, remux)。

    stage_fn(stage_path) 负责把"已缓存的区段"写成一份自洽的产物;
    remux=True 表示之后还要用 ffmpeg 把它转封装成 moov 前置的 MP4。
    """
    file_id = f.file_id

    if f.is_large_video:
        def stage_large(path):
            header_data = get_scanner().get_decrypted_data(file_id)
            if not header_data:
                return _fail_stage(path, 'header 解密失败')
            # 真实分区块格式: header 自带每块的绝对 out_offset, 是权威的覆盖来源;
            # 再并上 binlog 精确归组出来的外部 8MiB 分片 (只填 header 没覆盖到的
            # 相邻区域, 中间有洞时连续前缀规则会自动忽略它们)。
            exts = media_part_extents(header_data)
            slices = _slices_for_header(file_id)
            if exts:
                return repack_from_extents(exts, path, slices,
                                           decrypt_fn=_decrypt_by_path)
            return rebuild_large_video_to_file(header_data, slices, path,
                                               decrypt_fn=_decrypt_by_path)
        return stage_large, True

    if f.is_serialized:
        def stage_serialized(path):
            data = get_scanner().get_decrypted_data(file_id)
            if not data:
                return _fail_stage(path, '解密失败')
            # 已是完整 MP4: 直接落盘交给转封装
            if len(data) > 12 and data[4:8] == b'ftyp':
                with open(path, 'wb') as fh:
                    fh.write(data)
                return _plain_stage(path)
            extents = serialized_extents(data)
            if not extents:
                # 反序列化不出区段: 落盘原始数据让 ffmpeg 试一次
                with open(path, 'wb') as fh:
                    fh.write(data)
                return _plain_stage(path)
            return repack_from_extents(extents, path)
        return stage_serialized, True

    if is_video(f.file_type):
        def stage_video(path):
            data = get_scanner().get_decrypted_data(file_id)
            if not data:
                return _fail_stage(path, '解密失败')
            with open(path, 'wb') as fh:
                fh.write(data)
            return _plain_stage(path)
        return stage_video, True

    # 图片 / 音频 / 贴片 / 未知: 不需要转封装, 但同样走原子提交 + sidecar
    def stage_plain(path):
        data = get_scanner().get_decrypted_data(file_id)
        if data is None:
            return _fail_stage(path, '解密失败')
        if not export_file(data, f.file_type, path):
            return _fail_stage(path, '导出失败')
        return _plain_stage(path)
    return stage_plain, False


def _produce_export(file_id: str) -> ExportOutcome:
    """
    导出单个文件的**唯一入口**。所有导出/重建路径都必须走这里, 以保证:
      - 失败时不会留下以最终文件名命名的半成品
      - 失败时不会破坏已存在的好文件
      - 成功时同时写出 sidecar, 供后续失效判定
    """
    out = ExportOutcome()
    f = find_file_by_id(file_id)
    if f is None:
        out.reasons.append('文件未找到')
        return out
    if f.file_type == FileType.VIDEO_SLICE:
        out.reasons.append('视频分片无法独立导出, 请导出对应的大视频')
        return out

    filename = get_export_filename(file_id, f.file_type)
    final_path = os.path.join(Config.export_dir, filename)
    stage_fn, remux = _stage_producer(f)

    key_high = 0
    if binlog_index:
        rec = binlog_index.get(f.file_name)
        if rec:
            key_high = rec.key_high

    out = produce(final_path, stage_fn,
                  key_high=key_high,
                  need_space=_expected_export_size_for(f),
                  remux=remux)
    out.filename = filename
    if out.ok:
        _rebuild_exported_ids()
    return out


def _outcome_json(out: ExportOutcome, file_id: str = '') -> dict:
    """把导出结果转成前端可用的 JSON。

    保持 `ok` / `filename` / `size` 三个键不变 (前端契约), 其余为新增字段。
    """
    return {
        'ok': bool(out.ok),
        'file_id': file_id,
        'filename': out.filename,
        'path': out.final_path,
        'size': int(out.size or 0),
        'duration': round(out.duration_s, 3) if out.duration_s else 0,
        'truncated': bool(out.truncated),
        'total_size': int(out.total_size or 0),
        'covered_bytes': int(out.covered_bytes or 0),
        'missing_blocks': list(out.missing_blocks or [])[:80],
        'reasons': list(out.reasons or []),
    }


def _safe_decrypt(file_id: str) -> Optional[bytes]:
    """解密失败时返回 None 而不是抛异常"""
    try:
        return get_scanner().get_decrypted_data(file_id)
    except Exception:
        return None


def _fill_large_video_info(info: dict, f: CacheFile) -> None:
    """
    填充大视频的细节: 分片清单 / 缺口 / 完整性与可播时长。

    分片清单与缺口全部**从覆盖率推导**, 不做索引算术 (range(0,N) 与 range(1,N+1)
    的差一问题就此消失)。这样也能自然处理: header 自身已覆盖 [0,8MiB)、
    最后一个分片是短块、header 与分片区间重叠等情况。
    """
    slices = _slices_for_header(f.file_id)
    on_disk = {s.index: s for s in slices}
    missing = list(f.missing_slice_indices or [])

    key_high = 0
    if binlog_index:
        rec = binlog_index.get(f.file_name)
        if rec:
            key_high = rec.key_high

    slice_details = []
    for idx in sorted(set(list(on_disk.keys()) + list(missing))):
        s = on_disk.get(idx)
        if s is not None and idx not in missing:
            slice_details.append({
                'slice_index': idx,
                'file_name': s.name,
                'size': int(s.size),
                'on_disk': True,
            })
        else:
            slice_details.append({
                'slice_index': idx,
                'file_name': s.name if s else '',
                'size': int(s.size) if s else 0,
                'on_disk': False,
            })

    lv = dict(info.get('large_video_info') or {})
    lv.update({
        'estimated_size': f.total_size or lv.get('estimated_size', 0),
        'total_size': f.total_size,
        'slices_needed': max(0, int(f.slices_needed or 0)),
        'available_slices': len(slices),
        'key_high': f'0x{key_high:016X}' if key_high else '',
        'slice_details': slice_details,
        'missing_indices': missing,
        'moov_present': bool(f.moov_present),
        'covered_bytes': int(f.covered_bytes or 0),
        'playable_duration': round(f.playable_duration_s or 0.0, 3),
        'complete': bool(f.is_complete_large_video),
    })
    info['large_video_info'] = lv
    info['is_complete_large_video'] = bool(f.is_complete_large_video)
    info['playable_duration'] = round(f.playable_duration_s or 0.0, 3)

    if f.is_complete_large_video:
        info['is_incomplete'] = False
    else:
        info['is_incomplete'] = True
        info['missing_slices'] = len(missing)
        if missing:
            info['file_type_label'] = f'大视频 (缺 {len(missing)} 分片)'
        else:
            info['file_type_label'] = '大视频 (缓存不完整, 无法重建)'
        if f.missing_reason:
            info['incomplete_reasons'] = [f.missing_reason]


def _probe_small_video_meta(f: CacheFile, file_id: str) -> Optional[dict]:
    """非大视频: 落临时文件后用 ffprobe 读时长/分辨率 (绝不写导出目录)"""
    tmp = None
    try:
        data = _safe_decrypt(file_id)
        if not data:
            return None
        if f.is_serialized and not (len(data) > 12 and data[4:8] == b'ftyp'):
            mp4_data = deserialize_video(data)
            if mp4_data is None:
                return None
            data = mp4_data
        if len(data) <= 12 or data[4:8] != b'ftyp':
            return None
        fd, tmp = tempfile.mkstemp(suffix='.mp4', dir=tempfile.gettempdir())
        with os.fdopen(fd, 'wb') as fh:
            fh.write(data)
        return get_video_metadata(tmp)
    except Exception:
        return None
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


# ==================== API 路由 ====================

@app.route('/api/file/<file_id>', methods=['DELETE'])
def api_delete_file(file_id):
    """
    删除缓存文件 (物理删除 tdata 中的原始文件)

    安全机制:
    - 只允许删除 user_data/cache 和 user_data/media_cache 目录下的文件
    - 使用文件路径验证, 防止路径穿越
    - binlog 中的记录不做修改 (Telegram 会自行清理)
    """
    f = find_file_by_id(file_id)
    if f is None:
        return jsonify({'error': '文件未找到'}), 404

    fpath = f.file_path
    # 安全检查: 确保路径在允许的缓存目录内
    allowed_dirs = [
        os.path.abspath(os.path.join(Config.tdata_path, 'user_data', 'cache')),
        os.path.abspath(os.path.join(Config.tdata_path, 'user_data', 'media_cache')),
    ]
    abspath = os.path.abspath(fpath)
    if not any(abspath.startswith(d) for d in allowed_dirs):
        return jsonify({'error': '拒绝删除: 路径不在缓存目录内'}), 400

    if not os.path.exists(abspath):
        return jsonify({'error': '文件不存在 (可能已被 Telegram 清理)'}), 404

    try:
        os.remove(abspath)
        # 从扫描结果中移除 (加锁防止并发删除)
        if scan_result is None:
            return jsonify({'ok': True, 'message': '文件已删除 (无扫描结果可更新)'})
        with files_lock:
            if f in scan_result.files:
                scan_result.files.remove(f)
            # 更新统计
            cat = f.category
            if cat in scan_result.by_category:
                scan_result.by_category[cat] = max(0, scan_result.by_category[cat] - 1)
            t = f.file_type.value
            if t in scan_result.by_type:
                scan_result.by_type[t] = max(0, scan_result.by_type[t] - 1)
            scan_result.decrypted_files = max(0, scan_result.decrypted_files - 1)
            scan_result.total_files = max(0, scan_result.total_files - 1)
            # 同步更新索引
            file_index.pop(file_id, None)
            file_name_index.pop(f.file_name, None)

        # 同时删除关联的缩略图和导出文件
        thumb_path = os.path.join(Config.thumbnail_dir, f"{file_id}.jpg")
        if os.path.exists(thumb_path):
            os.remove(thumb_path)
        _remove_export_file(file_id)

        return jsonify({'ok': True, 'file_id': file_id, 'deleted': True})
    except Exception as e:
        return jsonify({'error': f'删除失败: {e}'}), 500


@app.route('/api/files/batch_delete', methods=['POST'])
def api_batch_delete_files():
    """
    批量删除缓存文件 (物理删除 tdata 中的原始文件)

    安全机制与单个删除相同: 只允许删除 cache 和 media_cache 目录下的文件
    """
    if scan_result is None:
        return jsonify({'error': '尚未扫描'}), 400

    data = request.get_json(silent=True) or {}
    file_ids = data.get('file_ids', [])
    if not file_ids:
        return jsonify({'error': '未选择文件'}), 400

    allowed_dirs = [
        os.path.abspath(os.path.join(Config.tdata_path, 'user_data', 'cache')),
        os.path.abspath(os.path.join(Config.tdata_path, 'user_data', 'media_cache')),
    ]

    success_count = 0
    failed_count = 0
    for file_id in file_ids:
        f = find_file_by_id(file_id)
        if f is None:
            failed_count += 1
            continue
        abspath = os.path.abspath(f.file_path)
        if not any(abspath.startswith(d) for d in allowed_dirs):
            failed_count += 1
            continue
        if not os.path.exists(abspath):
            failed_count += 1
            continue
        try:
            os.remove(abspath)
            # 从扫描结果中移除 (加锁防止并发删除)
            with files_lock:
                if f in scan_result.files:
                    scan_result.files.remove(f)
                    cat = f.category
                    if cat in scan_result.by_category:
                        scan_result.by_category[cat] = max(0, scan_result.by_category[cat] - 1)
                    t = f.file_type.value
                    if t in scan_result.by_type:
                        scan_result.by_type[t] = max(0, scan_result.by_type[t] - 1)
                    scan_result.decrypted_files = max(0, scan_result.decrypted_files - 1)
                    scan_result.total_files = max(0, scan_result.total_files - 1)
                    # 同步更新索引
                    file_index.pop(file_id, None)
                    file_name_index.pop(f.file_name, None)
            # 删除关联的缩略图和导出文件
            thumb_path = os.path.join(Config.thumbnail_dir, f"{file_id}.jpg")
            if os.path.exists(thumb_path):
                os.remove(thumb_path)
            _remove_export_file(file_id)
            success_count += 1
        except Exception:
            failed_count += 1

    return jsonify({
        'ok': True,
        'total': len(file_ids),
        'success': success_count,
        'failed': failed_count,
    })


# ==================== Telegram 来源链接绑定 ====================
# 背景 (2026-09-15, 13 轮取证 .temp/explore_jump*.py): 仅缓存未下载的视频,
# Telegram **不在本地落盘任何消息关联** (docId 邻域无 msgId, 频道名/用户名
# 也不在账号快照里), 无法自动反查 (peer, msg)。
# 解决: 用户把原视频的 t.me 链接粘贴绑定一次, 之后「前往 Telegram 播放」
# 即可精确跳转回去播放, 补全缓存分片。绑定按真实 document_id 存储,
# 重新扫描后依然有效。

TELEGRAM_LINKS_PATH = os.path.join(BASE_DIR, 'telegram_links.json')
_telegram_links_cache: Optional[dict] = None
_telegram_links_lock = threading.Lock()


def _load_telegram_links() -> dict:
    global _telegram_links_cache
    if _telegram_links_cache is None:
        try:
            with open(TELEGRAM_LINKS_PATH, 'r', encoding='utf-8') as fh:
                data = json.load(fh)
            _telegram_links_cache = data if isinstance(data, dict) else {}
        except Exception:
            _telegram_links_cache = {}
        _migrate_album_params()
    return _telegram_links_cache


def _save_telegram_links() -> None:
    if _telegram_links_cache is None:
        return
    tmp = TELEGRAM_LINKS_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(_telegram_links_cache, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, TELEGRAM_LINKS_PATH)


def _telegram_link_keys(f: 'CacheFile') -> List[str]:
    """一个文件的绑定键: 优先真实 document_id (重扫描后仍有效), 兜底文件 id"""
    keys = []
    if binlog_index:
        r = binlog_index.get(f.file_name)
        if r:
            keys.append(f'doc:{r.real_document_id:016X}')
    keys.append(f'file:{f.file_id}')
    return keys


def _get_telegram_link(f: 'CacheFile') -> Optional[dict]:
    links = _load_telegram_links()
    for k in _telegram_link_keys(f):
        v = links.get(k)
        if isinstance(v, dict) and v.get('tg_url'):
            return v
    return None


def _album_suffix(url: str) -> Tuple[str, Optional[int]]:
    """
    提取网页链接里的相册定位参数 (?single&t=N) → ('&single&t=N', N)

    2026-09-15 真机验证 (用户逐个候选肉眼判定, 频道 feihsl 第 25158 条相册消息):
        tg://resolve?domain=feihsl&post=25158               → 只到消息, 显示第 1 个媒体
        tg://resolve?domain=feihsl&post=25158&t=4           → 只到消息 (t 单独无效)
        tg://resolve?domain=feihsl&post=25158&single&t=4    → 正确落到第 4 个媒体
        tg://resolve?domain=feihsl&post=25158&single&media=4 → 只到消息 (media 不是有效参数)
    结论: tg:// 必须 **同时** 带 single 和 t 才会定位到相册内第 N 个媒体,
    固定顺序 &single&t=N。t 是 1-based 的媒体序号 (不是秒数、也不是话题 id)。
    所以 t 只在 single 存在时才透传, 避免把单条消息的 t 误解成话题 id。
    """
    try:
        # keep_blank_values=True 必须带: ?single&t=4 里的 single 没有 '=',
        # 默认会被 parse_qs 直接丢掉
        q = parse_qs(urlparse(url).query, keep_blank_values=True)
    except Exception:
        return '', None
    if 'single' not in q:
        return '', None
    raw = (q.get('t') or [''])[0]
    if not raw.isdigit():
        return '', None
    n = int(raw)
    if n <= 0 or n > 10:  # Telegram 单个相册最多 10 个媒体
        return '', None
    return f'&single&t={n}', n


def _parse_telegram_link(url: str) -> Optional[dict]:
    """
    解析用户提供的 Telegram 链接 → {tg_url, display, album_index}。

    支持:
      https://t.me/<username>/<msg>   → tg://resolve?domain=<username>&post=<msg>
      https://t.me/c/<id>/<msg>       → tg://privatepost?channel=<id>&post=<msg>
      https://t.me/<username>         → tg://resolve?domain=<username> (只开频道)
      tg://resolve?... / tg://privatepost?...  → 原样使用 (自带 single&t= 也保留)

    相册消息 (一条消息里有多个视频): 网页链接末尾的 ?single&t=N 表示"第 N 个媒体",
    由 _album_suffix 转成 &single&t=N 追加到 tg:// 上; 不带就只能停在消息级。
    """
    if not url or not isinstance(url, str):
        return None
    url = url.strip()

    if url.startswith('tg://'):
        rest = url[5:]
        if rest.startswith(('resolve', 'privatepost')):
            out = {'tg_url': url, 'display': url}
            _, idx = _album_suffix('https://x/?' + (url.split('?', 1)[1] if '?' in url else ''))
            if idx:
                out['album_index'] = idx
            return out
        return None

    display = url.split('?')[0].split('#')[0]
    suffix, album_index = _album_suffix(url)

    # 私有频道/群: t.me/c/<id>/<msg>
    m = re.match(r'^(?:https?://)?(?:t\.me|telegram\.me)/c/(\d+)(?:/(\d+))?',
                 url, re.IGNORECASE)
    if m:
        tg = f'tg://privatepost?channel={m.group(1)}'
        if m.group(2):
            tg += f'&post={m.group(2)}'
            tg += suffix
        return {'tg_url': tg, 'display': display, 'album_index': album_index}

    # 公开频道/用户名: t.me/<username>/<msg>
    m = re.match(r'^(?:https?://)?(?:t\.me|telegram\.me)/([A-Za-z0-9_]{3,64})'
                 r'(?:/(\d+))?', url, re.IGNORECASE)
    if m:
        tg = f'tg://resolve?domain={m.group(1)}'
        if m.group(2):
            tg += f'&post={m.group(2)}'
            tg += suffix
        return {'tg_url': tg, 'display': display, 'album_index': album_index}
    return None


def _migrate_album_params() -> None:
    """一次性升级: 早期绑定把 ?single&t=N 丢了, 用 source_url 重新补回相册定位"""
    links = _telegram_links_cache
    if not isinstance(links, dict):
        return
    changed = False
    for v in links.values():
        if not isinstance(v, dict):
            continue
        tg = v.get('tg_url') or ''
        src = v.get('source_url') or ''
        if not src or '&single&t=' in tg:
            continue
        parsed = _parse_telegram_link(src)
        if parsed and parsed['tg_url'] != tg:
            v['tg_url'] = parsed['tg_url']
            if parsed.get('album_index'):
                v['album_index'] = parsed['album_index']
            changed = True
    if changed:
        _save_telegram_links()


def _is_attributed_slice(f: 'CacheFile') -> bool:
    """该文件是否是已归属到大视频 header 的分片 (合并展示时不再单独成行)"""
    if f.file_type != FileType.VIDEO_SLICE or not binlog_index:
        return False
    r = binlog_index.get(f.file_name)
    return bool(r and r.doc_key in large_video_header_index)


@app.route('/api/file/<file_id>/open_in_telegram', methods=['POST'])
def api_open_in_telegram(file_id):
    """
    在 Telegram Desktop 中打开/播放对应的原视频

    跳转策略 (优先级从高到低):
    0. 用户手动绑定的来源链接 (telegram_links.json) —— 仅缓存未下载的视频
       本地无法自动反查 (取证确认), 绑定一次 t.me 链接后即可精确跳转
    1. 从 binlog 记录还原真实 document_id, 查 locations + downloads 索引
       → {peerId, msgId} → tg:// URL
       - Channel/Megagroup/Chat: tg://privatepost?channel=<bareId>&post=<msgId>
       - User: 启动 Telegram (私聊消息无直接跳转 URL)
       还原公式 (2026-09-15 取证, 16/16 交叉验证):
           real_doc_id = ((key_high & 0xFFFF) << 48) | (key_low >> 16)
    2. 仅缓存未下载的视频: locations 里可能有 *media_cache* 条目但无
       downloads 记录 → 本地无消息关联, 只能启动 Telegram
    3. 仅启动 Telegram.exe (无法精确定位)
    """
    import subprocess
    f = find_file_by_id(file_id)
    if f is None:
        return jsonify({'error': '文件未找到'}), 404

    # 查找 Telegram.exe 路径
    telegram_exe = None
    candidates = [
        os.path.join(os.path.dirname(Config.tdata_path), 'Telegram.exe'),
        os.path.join(Config.tdata_path, '..', 'Telegram.exe'),
        r'D:\Software\Telegram Desktop\Telegram.exe',
    ]
    for c in candidates:
        c = os.path.abspath(c)
        if os.path.exists(c):
            telegram_exe = c
            break
    if telegram_exe is None:
        return jsonify({'error': '未找到 Telegram.exe'}), 404

    # 从 binlog 记录还原真实 document_id (2026-09-15 取证公式)
    record = binlog_index.get(f.file_name) if binlog_index else None
    key_high = record.key_high if record else 0
    real_doc_id = record.real_document_id if record else 0

    # 尝试获取 tg:// URL
    tg_url = None
    peer_info = None
    location_status = 'unknown'  # bound / downloaded / cache_only / private_chat / unknown

    # 策略0: 用户绑定的来源链接 (最高优先级)
    bound = _get_telegram_link(f)
    if bound:
        tg_url = bound['tg_url']
        location_status = 'bound'

    if not tg_url and location_index and location_index.is_loaded:
        # 策略1: 用还原的真实 document_id 查找 (locations 存完整 64 位 id)
        entry = None
        if real_doc_id:
            entry = location_index.get_by_document_id(real_doc_id)
        # 兜底: 旧口径 (key_high 直接当 doc_id), 覆盖非标准记录
        if entry is None and key_high:
            entry = location_index.get_by_document_id(key_high)

        if entry:
            d = entry.get('download')
            loc = entry.get('location')
            if d:
                location_status = 'downloaded'
            elif loc is not None:
                location_status = 'cache_only'
            if entry.get('tg_url'):
                tg_url = entry['tg_url']
                if d:
                    peer_info = {
                        'peer_type': d.peer_type,
                        'bare_id': d.bare_id,
                        'msg_id': d.msg_id,
                        'path': d.basename,
                    }
            elif d and d.peer_type == 'User':
                location_status = 'private_chat'

    launched = False
    if tg_url and location_status == 'bound':
        hint = '已跳转到绑定的 Telegram 消息'
    elif tg_url:
        hint = '已跳转到 Telegram 对应消息'
    elif location_status == 'private_chat':
        hint = '该文件来自私聊, Telegram Desktop 不支持私聊消息跳转链接, 已打开 Telegram'
    elif location_status == 'cache_only':
        hint = ('该视频仅缓存未下载, 本地没有它的来源消息记录; '
                '在下方绑定原视频的 t.me 链接后即可精确跳转')
    else:
        hint = '未找到该文件的来源记录; 可在下方绑定原视频的 t.me 链接'

    try:
        if tg_url:
            # 精确跳转: 尝试多种方式打开 tg:// URL
            # 方式1: os.startfile (Windows 协议处理器, TG 已运行时直接跳转)
            try:
                os.startfile(tg_url)
                launched = True
                hint = '已跳转到 Telegram 对应消息'
            except Exception:
                # 方式2: 先启动 TG, 等待就绪后重试 os.startfile
                subprocess.Popen([telegram_exe], creationflags=0x08000000)
                import time as _time
                _time.sleep(3)  # 等待 TG 启动
                try:
                    os.startfile(tg_url)
                    launched = True
                    hint = '已启动 Telegram 并跳转到对应消息'
                except Exception:
                    # 方式3: 用 subprocess 传 URL 给 Telegram.exe
                    subprocess.Popen([telegram_exe, '--', tg_url], creationflags=0x08000000)
                    launched = True
                    hint = '已启动 Telegram, 尝试跳转 (可能需要手动查找)'
        else:
            # 无 tg_url: 仅启动 Telegram (hint 已按 location_status 分类)
            subprocess.Popen([telegram_exe], creationflags=0x08000000)
            launched = True
    except Exception:
        launched = False
        hint = '启动 Telegram 失败, 请检查安装路径'

    return jsonify({
        'ok': launched,
        'telegram_exe': telegram_exe,
        'key_high': f'0x{key_high:016X}' if key_high else '',
        'document_id': f'0x{real_doc_id:016X}' if real_doc_id else '',
        'document_id_dec': real_doc_id,
        'location_status': location_status,
        'tg_url': tg_url,
        'peer_info': peer_info,
        'telegram_link': bound,
        'hint': hint,
    })


@app.route('/api/file/<file_id>/bind_telegram', methods=['POST'])
def api_bind_telegram(file_id):
    """绑定原视频的 t.me 链接 (仅缓存未下载的视频本地无消息关联, 绑定后可精确跳转)"""
    f = find_file_by_id(file_id)
    if f is None:
        return jsonify({'error': '文件未找到'}), 404
    data = request.get_json(silent=True) or {}
    url = (data.get('url') or '').strip()
    parsed = _parse_telegram_link(url)
    if not parsed:
        return jsonify({
            'error': '无法识别的链接。请粘贴形如 https://t.me/频道名/消息号 的地址, '
                     '或 tg:// 开头的链接',
        }), 400
    entry = {
        'tg_url': parsed['tg_url'],
        'source_url': url,
        'display': parsed['display'],
        'bound_at': time.time(),
    }
    if parsed.get('album_index'):
        entry['album_index'] = parsed['album_index']
    with _telegram_links_lock:
        links = _load_telegram_links()
        # 两个键都写: doc: (跨扫描稳定) + file: (无 binlog 时兜底)
        for k in _telegram_link_keys(f):
            links[k] = entry
        _save_telegram_links()
    return jsonify({'ok': True, 'telegram_link': entry})


@app.route('/api/file/<file_id>/bind_telegram', methods=['DELETE'])
def api_unbind_telegram(file_id):
    """解除 Telegram 来源链接绑定"""
    f = find_file_by_id(file_id)
    if f is None:
        return jsonify({'error': '文件未找到'}), 404
    with _telegram_links_lock:
        links = _load_telegram_links()
        removed = False
        for k in _telegram_link_keys(f):
            if k in links:
                links.pop(k)
                removed = True
        if removed:
            _save_telegram_links()
    return jsonify({'ok': True, 'removed': removed})


@app.route('/')
def index():
    """主页面"""
    return render_template('index.html')


@app.route('/api/config', methods=['GET'])
def get_config():
    """获取当前配置"""
    return jsonify({
        'tdata_path': Config.tdata_path,
        'download_path': Config.download_path,
        'export_dir': Config.export_dir,
        'has_passcode': bool(Config.passcode),
        'has_key': scanner is not None or os.path.exists(os.path.join(Config.tdata_path, 'key_datas')),
    })


@app.route('/api/config', methods=['POST'])
def set_config():
    """更新配置"""
    data = request.get_json(silent=True) or {}
    global scanner, scan_result, binlog_index, location_index, download_files_cache, last_file_signatures
    global file_index, file_name_index, large_video_header_index, _exported_ids
    if 'tdata_path' in data:
        path = data['tdata_path']
        key_file = os.path.join(path, 'key_datas')
        if not os.path.exists(key_file):
            return jsonify({'error': f'key_datas 不存在: {path}'}), 400
        Config.tdata_path = path
        # 重置扫描器与所有索引
        scanner = None
        scan_result = None
        binlog_index = None
        location_index = None
        last_file_signatures = {}
        file_index = {}
        file_name_index = {}
        large_video_header_index = {}
        _exported_ids = set()
    if 'download_path' in data:
        path = (data.get('download_path') or '').strip()
        if path and not os.path.isdir(path):
            return jsonify({'error': f'下载目录不存在: {path}'}), 400
        Config.download_path = path
        download_files_cache = []
    if 'passcode' in data and data.get('passcode_change'):
        # 只有显式标记 passcode_change=True 时才修改密码
        # 密码变化时重置扫描器 (密钥派生依赖密码)
        new_pc = data['passcode'].encode('utf-8')
        if new_pc != Config.passcode:
            Config.passcode = new_pc
            scanner = None
            scan_result = None
            binlog_index = None
            location_index = None
            last_file_signatures = {}
            file_index = {}
            file_name_index = {}
            large_video_header_index = {}
            _exported_ids = set()
    return jsonify({'ok': True, 'tdata_path': Config.tdata_path, 'download_path': Config.download_path,
                    'has_passcode': bool(Config.passcode)})


# ==================== 下载文件 API ====================

def _detect_download_file_type(filename: str, first_bytes: bytes = b'') -> str:
    """根据文件名和头部魔数判断下载文件类型"""
    ext = os.path.splitext(filename)[1].lower()

    # 先用头部魔数判断
    if first_bytes:
        if first_bytes[:4] == b'\x89PNG':
            return 'image'
        if first_bytes[:3] == b'\xff\xd8\xff' or first_bytes[:2] == b'\xff\xd8':
            return 'image'
        if first_bytes[:12] == b'RIFF' + b'\x00\x00\x00\x00WEBP':
            return 'image'
        if first_bytes[:6] in (b'GIF87a', b'GIF89a'):
            return 'image'
        if first_bytes[:4] == b'\x00\x00\x01\x00':
            return 'image'  # ICO
        if first_bytes[4:8] == b'ftyp':
            return 'video'
        if first_bytes[:4] == b'\x1aE0' or first_bytes[:4] == b'\x1aE\x00\x00':
            return 'video'  # WebM/Matroska
        if first_bytes[:3] == b'ID3' or first_bytes[:2] == b'\xff\xfb':
            return 'audio'  # MP3
        if first_bytes[:4] == b'OggS':
            return 'audio'
        if first_bytes[:4] == b'fLaC':
            return 'audio'
        if first_bytes[:4] == b'\x66\x4c\x61\x43':
            return 'audio'  # FLAC
        if first_bytes[:4] == b'RIFF' and first_bytes[8:12] == b'WAVE':
            return 'audio'

    # 回退到扩展名
    ext_map = {
        '.jpg': 'image', '.jpeg': 'image', '.png': 'image', '.webp': 'image',
        '.gif': 'image', '.bmp': 'image', '.ico': 'image', '.svg': 'image',
        '.mp4': 'video', '.mov': 'video', '.avi': 'video', '.mkv': 'video',
        '.webm': 'video', '.flv': 'video', '.m4v': 'video',
        '.mp3': 'audio', '.ogg': 'audio', '.wav': 'audio', '.flac': 'audio',
        '.m4a': 'audio', '.aac': 'audio', '.wma': 'audio',
    }
    return ext_map.get(ext, 'unknown')


def _scan_download_files() -> List[dict]:
    """扫描下载目录, 返回文件列表"""
    if not Config.download_path or not os.path.isdir(Config.download_path):
        return []

    results = []
    for fname in os.listdir(Config.download_path):
        fpath = os.path.join(Config.download_path, fname)
        if not os.path.isfile(fpath):
            continue

        try:
            stat = os.stat(fpath)
        except Exception:
            continue

        # 读取前 16 字节用于类型判断
        first_bytes = b''
        try:
            with open(fpath, 'rb') as f:
                first_bytes = f.read(16)
        except Exception:
            pass

        category = _detect_download_file_type(fname, first_bytes)

        results.append({
            'file_id': fname,  # 用文件名作为唯一 ID
            'file_name': fname,
            'file_size': stat.st_size,
            'category': category,
            'modified': stat.st_mtime,
        })

    # 按大小排序
    results.sort(key=lambda x: x['file_size'], reverse=True)
    return results


@app.route('/api/download_files')
def api_download_files():
    """获取下载文件列表 (支持筛选/分页)"""
    global download_files_cache

    # 如果缓存为空, 重新扫描
    if not download_files_cache:
        download_files_cache = _scan_download_files()

    # 筛选参数
    category = request.args.get('category', '')
    search = request.args.get('search', '').lower()
    sort = request.args.get('sort', 'size_desc')

    files = list(download_files_cache)

    if category and category != 'all':
        files = [f for f in files if f['category'] == category]
    if search:
        files = [f for f in files if search in f['file_name'].lower()]

    # 排序
    if sort == 'size_desc':
        files.sort(key=lambda f: f['file_size'], reverse=True)
    elif sort == 'size_asc':
        files.sort(key=lambda f: f['file_size'])
    elif sort == 'name':
        files.sort(key=lambda f: f['file_name'])
    elif sort == 'modified':
        files.sort(key=lambda f: f['modified'], reverse=True)

    total = len(files)
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 50))
    start = (page - 1) * per_page
    end = start + per_page
    page_files = files[start:end]

    # 统计
    by_category = {}
    for f in download_files_cache:
        cat = f['category']
        by_category[cat] = by_category.get(cat, 0) + 1

    return jsonify({
        'files': page_files,
        'total': total,
        'page': page,
        'per_page': per_page,
        'pages': (total + per_page - 1) // per_page,
        'by_category': by_category,
    })


@app.route('/api/download_files/refresh', methods=['POST'])
def api_download_files_refresh():
    """强制重新扫描下载文件列表 (清除缓存)"""
    global download_files_cache
    download_files_cache = []
    download_files_cache = _scan_download_files()
    return jsonify({'ok': True, 'total': len(download_files_cache)})


@app.route('/api/download_preview/<path:filename>')
def api_download_preview(filename):
    """预览/下载文件 (直接 send_file, 支持 Range 请求)"""
    if not Config.download_path:
        return jsonify({'error': '未设置下载目录'}), 400

    fpath = os.path.join(Config.download_path, filename)
    abspath = os.path.abspath(fpath)
    if not abspath.startswith(os.path.abspath(Config.download_path)):
        return jsonify({'error': '非法路径'}), 400

    if not os.path.exists(fpath):
        return jsonify({'error': '文件不存在'}), 404

    # 根据扩展名设置 mimetype
    mimetype, _ = mimetypes.guess_type(filename)
    if not mimetype:
        category = _detect_download_file_type(filename)
        mimetype_map = {'image': 'image/jpeg', 'video': 'video/mp4', 'audio': 'audio/mpeg'}
        mimetype = mimetype_map.get(category, 'application/octet-stream')

    resp = send_file(fpath, mimetype=mimetype, conditional=True)
    resp.headers['Cache-Control'] = 'public, max-age=3600'
    return resp


@app.route('/api/download_thumbnail/<path:filename>')
def api_download_thumbnail(filename):
    """生成下载文件的缩略图"""
    if not Config.download_path:
        return jsonify({'error': '未设置下载目录'}), 400

    fpath = os.path.join(Config.download_path, filename)
    abspath = os.path.abspath(fpath)
    if not abspath.startswith(os.path.abspath(Config.download_path)):
        return jsonify({'error': '非法路径'}), 400

    if not os.path.exists(fpath):
        return jsonify({'error': '文件不存在'}), 404

    category = _detect_download_file_type(filename)

    # 缩略图缓存
    safe_name = filename.replace(os.sep, '_').replace('/', '_')
    thumb_path = os.path.join(Config.thumbnail_dir, f"dl_{safe_name}.jpg")
    if os.path.exists(thumb_path):
        return send_file(thumb_path, mimetype='image/jpeg')

    try:
        if category == 'image':
            with open(fpath, 'rb') as f:
                data = f.read()
            from PIL import Image
            import io as _io
            img = Image.open(_io.BytesIO(data))
            img.thumbnail((200, 200), Image.LANCZOS)
            if img.mode in ('RGBA', 'LA', 'P'):
                img = img.convert('RGBA')
            else:
                img = img.convert('RGB')
            img.save(thumb_path, 'JPEG', quality=85)
            return send_file(thumb_path, mimetype='image/jpeg')

        elif category == 'video':
            # 用 ffmpeg 提取首帧
            from src.exporter import _get_ffmpeg_path, _subprocess_flags
            ffmpeg = _get_ffmpeg_path()
            if ffmpeg:
                si, cf = _subprocess_flags()
                try:
                    subprocess.run(
                        [ffmpeg, '-i', fpath, '-vframes', '1',
                         '-vf', 'scale=200:200:force_original_aspect_ratio=decrease',
                         '-q:v', '2', '-y', thumb_path],
                        capture_output=True, timeout=10,
                        startupinfo=si, creationflags=cf,
                    )
                    if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
                        return send_file(thumb_path, mimetype='image/jpeg')
                except Exception:
                    pass

        # 其他类型不支持缩略图
        return jsonify({'error': '此类型不支持缩略图'}), 400
    except Exception as e:
        return jsonify({'error': f'缩略图生成失败: {e}'}), 500


@app.route('/api/download_files', methods=['DELETE'])
def api_delete_download_file():
    """删除下载文件"""
    if not Config.download_path:
        return jsonify({'error': '未设置下载目录'}), 400

    data = request.get_json(silent=True) or {}
    file_ids = data.get('file_ids', [])

    if not file_ids:
        # 单文件删除 (兼容旧 API)
        file_id = request.args.get('file_id', '')
        if file_id:
            file_ids = [file_id]
        else:
            return jsonify({'error': '未指定文件'}), 400

    results = []
    success_count = 0
    for file_id in file_ids:
        fpath = os.path.join(Config.download_path, file_id)
        abspath = os.path.abspath(fpath)
        if not abspath.startswith(os.path.abspath(Config.download_path)):
            results.append({'file_id': file_id, 'success': False, 'error': '非法路径'})
            continue
        if not os.path.exists(abspath):
            results.append({'file_id': file_id, 'success': False, 'error': '文件不存在'})
            continue
        try:
            os.remove(abspath)
            results.append({'file_id': file_id, 'success': True})
            success_count += 1
        except Exception as e:
            results.append({'file_id': file_id, 'success': False, 'error': str(e)})

    # 从缓存中移除已删除的文件
    global download_files_cache
    deleted_ids = {r['file_id'] for r in results if r['success']}
    if deleted_ids:
        download_files_cache = [f for f in download_files_cache if f['file_id'] not in deleted_ids]

    return jsonify({
        'ok': True,
        'total': len(file_ids),
        'success': success_count,
        'failed': len(file_ids) - success_count,
        'results': results,
    })


@app.route('/api/scan', methods=['GET', 'POST'])
def api_scan():
    """扫描缓存文件"""
    global scan_result, scan_status

    with scan_lock:
        if scan_status['scanning']:
            return jsonify({'error': '扫描正在进行中'}), 409

        scan_status = {
            'scanning': True,
            'progress': 0,
            'total': 0,
            'current_file': '',
            'finished': False,
            'error': None,
            'binlog_locked': False,
            'binlog_available': False,
        }

    def do_scan():
        global scan_result, scan_status, binlog_index, last_file_signatures
        try:
            s = get_scanner()

            def progress_cb(current, total, filename):
                scan_status['progress'] = current
                scan_status['total'] = total
                scan_status['current_file'] = filename

            # 增量扫描: 有上次结果时复用未变化文件, 只解密新增/修改文件
            if scan_result and last_file_signatures:
                result = incremental_scan(s, progress_callback=progress_cb)
            else:
                result = s.scan(progress_callback=progress_cb)
                # 首次全量扫描后收集签名表
                sigs = _collect_file_signatures(Config.tdata_path)
                last_file_signatures.clear()
                last_file_signatures.update({k: (v[0], v[1]) for k, v in sigs.items()})
            scan_result = result
            build_file_index()  # 构建 O(1) 文件索引

            # binlog 可用性提示 (TG 运行时 binlog 被锁定, 会导致真实文件名/分片关联不可用)
            binlog_path = os.path.join(Config.tdata_path, "user_data", "media_cache", "1", "binlog")
            scan_status['binlog_locked'] = False
            scan_status['binlog_available'] = False
            if not os.path.exists(binlog_path):
                scan_status['binlog_locked'] = True  # 无 binlog 文件视为降级

            # 扫描完成后加载 binlog 索引, 并统一做"归类 + 覆盖率完整性判定"
            try:
                binlog_index = _load_binlog_index()
                if binlog_index:
                    _enrich_scan(result, binlog_index)
                    _build_large_video_header_index()  # 构建 header 索引
                    scan_status['binlog_available'] = True
                else:
                    # binlog 不可用 (TG 运行时被锁定): 无法精确关联分片 ->
                    # 大视频会如实显示为"不完整", 而不是靠猜分片数硬标"完整"
                    _enrich_scan(result, None)
                    scan_status['binlog_locked'] = True
            except Exception:
                binlog_index = None
                scan_status['binlog_locked'] = True
                try:
                    _enrich_scan(result, None)
                except Exception:
                    pass

            # 清理崩溃遗留的 .part / 孤儿 sidecar (避免它们被误当成导出)
            try:
                cleanup_stale(Config.export_dir)
            except Exception:
                pass

            # 扫描完成后加载 locations + downloads 索引
            try:
                global location_index
                s2 = get_scanner()
                if s2.local_key:
                    location_index = LocationIndex(Config.tdata_path, s2.local_key)
                    location_index.load()
            except Exception:
                location_index = None

            # 扫描完成后后台为视频生成缩略图 (用于判断视频是否可重建/可播放)
            def _generate_video_thumbnails():
                """后台为所有视频生成缩略图, 成功则设置 thumbnail_path"""
                if not scan_result:
                    return
                from src.scanner import FileType as FT, is_video as _is_video
                # 先在锁内取快照, 避免与删除接口并发修改同一个列表
                with files_lock:
                    files_snap = list(scan_result.files)
                for f in files_snap:
                    # 跳过分片 (缩略图从父 header 生成) 和非视频
                    if f.file_type == FT.VIDEO_SLICE:
                        continue
                    if not _is_video(f.file_type):
                        continue
                    # 已有缩略图则跳过
                    thumb_path = os.path.join(Config.thumbnail_dir, f"{f.file_id}.jpg")
                    if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
                        f.thumbnail_path = thumb_path
                        continue
                    try:
                        if f.is_large_video:
                            # 大视频: 重建到临时文件再抽首帧。
                            # 旧实现把整部视频读进内存 (含分片拼接), 峰值内存极高;
                            # 现在走流式重建, 峰值内存 = 一个分片。
                            tmp_path = None
                            try:
                                fd, tmp_path = tempfile.mkstemp(
                                    suffix='.mp4', dir=tempfile.gettempdir())
                                os.close(fd)
                                hdr = _safe_decrypt(f.file_id) or b''
                                sl = _slices_for_header(f.file_id)
                                exts = media_part_extents(hdr)
                                if exts:
                                    res = repack_from_extents(
                                        exts, tmp_path, sl,
                                        decrypt_fn=_decrypt_by_path)
                                else:
                                    res = rebuild_large_video_to_file(
                                        hdr, sl, tmp_path,
                                        decrypt_fn=_decrypt_by_path)
                                if not getattr(res, 'playable', False):
                                    continue
                                result = _video_thumbnail_from_path(tmp_path, thumb_path, (320, 320))
                            finally:
                                if tmp_path and os.path.exists(tmp_path):
                                    try:
                                        os.remove(tmp_path)
                                    except OSError:
                                        pass
                        else:
                            data = _safe_decrypt(f.file_id)
                            if not data:
                                continue
                            if f.is_serialized and not (len(data) > 12 and data[4:8] == b'ftyp'):
                                mp4_data = deserialize_video(data)
                                if mp4_data is None:
                                    continue
                                data = mp4_data
                            result = generate_thumbnail(data, f.file_type, thumb_path)
                        if result and os.path.exists(result) and os.path.getsize(result) > 0:
                            f.thumbnail_path = result
                        elif os.path.exists(thumb_path) and os.path.getsize(thumb_path) == 0:
                            # 清理空的缩略图文件
                            try:
                                os.remove(thumb_path)
                            except Exception:
                                pass
                    except Exception:
                        pass

            threading.Thread(target=_generate_video_thumbnails, daemon=True).start()

            scan_status['scanning'] = False
            scan_status['finished'] = True
        except Exception as e:
            scan_status['scanning'] = False
            scan_status['finished'] = True
            scan_status['error'] = str(e)

    thread = threading.Thread(target=do_scan, daemon=True)
    thread.start()

    return jsonify({'ok': True, 'message': '扫描已启动'})


@app.route('/api/scan/status')
def scan_status_api():
    """获取扫描进度"""
    return jsonify(scan_status)


@app.route('/api/stats')
def api_stats():
    """获取统计信息"""
    if scan_result is None:
        return jsonify({'error': '尚未扫描, 请先执行扫描'}), 400

    # 按缓存类型分组统计
    by_cache_type = {}
    with files_lock:
        files_snapshot = list(scan_result.files)
    for f in files_snapshot:
        ct = f.cache_type
        if ct not in by_cache_type:
            by_cache_type[ct] = 0
        by_cache_type[ct] += 1

    # 视频分类: 重新计算只含可重建视频的数量 (有缩略图 = 能合成完整视频)
    by_category = dict(scan_result.by_category)
    video_count = 0
    for f in files_snapshot:
        if f.category == 'video' and f.file_type != FileType.VIDEO_SLICE:
            if _has_valid_thumbnail(f):
                video_count += 1
    by_category['video'] = video_count

    return jsonify({
        'total_files': scan_result.total_files,
        'decrypted_files': scan_result.decrypted_files,
        'failed_files': scan_result.failed_files,
        'scan_time': round(scan_result.scan_time, 2),
        'by_category': by_category,
        'by_type': scan_result.by_type,
        'by_cache_type': by_cache_type,
    })


@app.route('/api/duplicates')
def api_duplicates():
    """检测重复缓存文件 (按解密数据 hash 分组)
    返回重复组列表, 每组含相同 hash 的文件列表
    """
    if scan_result is None:
        return jsonify({'error': '尚未扫描'}), 400

    # 按 hash 分组 (排除空 hash)
    groups: Dict[str, list] = {}
    with files_lock:
        files_snapshot = list(scan_result.files)
    for f in files_snapshot:
        h = f.decrypted_data_hash
        if h:
            groups.setdefault(h, []).append(f)

    # 只保留重复组 (>= 2 个文件)
    dup_groups = []
    total_dup_files = 0
    waste_size = 0
    for h, files in groups.items():
        if len(files) < 2:
            continue
        dup_groups.append({
            'hash': h,
            'count': len(files),
            'size': files[0].decrypted_size,
            'waste': files[0].decrypted_size * (len(files) - 1),
            'files': [cache_file_to_dict(f) for f in files],
        })
        total_dup_files += len(files)
        waste_size += files[0].decrypted_size * (len(files) - 1)

    # 按浪费空间降序
    dup_groups.sort(key=lambda g: g['waste'], reverse=True)

    return jsonify({
        'groups': dup_groups,
        'total_groups': len(dup_groups),
        'total_dup_files': total_dup_files,
        'waste_size': waste_size,
    })


@app.route('/api/files')
def api_files():
    """获取文件列表 (支持筛选/分页/分组)"""
    if scan_result is None:
        return jsonify({'error': '尚未扫描'}), 400

    # 筛选参数
    category = request.args.get('category', '')  # image/video/audio/sticker/unknown
    file_type = request.args.get('type', '')      # jpg/png/mp4/...
    cache_type = request.args.get('cache_type', '')  # cache/media_cache
    search = request.args.get('search', '').lower()
    sort = request.args.get('sort', 'size_desc')  # size_desc/size_asc/name/type

    # 分页参数
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 50))

    files = scan_result.files
    # 筛选 (拷贝避免并发修改)
    with files_lock:
        files = list(files)

    # ==================== 分片合并展示 ====================
    # 一个视频 = 一条记录 (以 header 为唯一标识)。已归属到某 header 的分片
    # 不再单独成行 —— 它们已计入该视频的合成率, 单独展示只会让画面充满
    # 千篇一律的 8MB 碎片。孤立分片 (所属 header 不在缓存里) 仍保留展示。
    files = [f for f in files if not _is_attributed_slice(f)]

    if category:
        files = [f for f in files if f.category == category]
        if category == 'video':
            # 视频分类: 只显示有缩略图的视频 (能重建/反序列化的),
            # 排除分片文件 (不是独立视频) 和无缩略图的视频 (无法合成完整视频)
            files = [
                f for f in files
                if f.file_type != FileType.VIDEO_SLICE and _has_valid_thumbnail(f)
            ]
    if file_type:
        files = [f for f in files if f.file_type.value == file_type]
    if cache_type:
        files = [f for f in files if f.cache_type == cache_type]
    if search:
        files = [f for f in files if search in f.file_name.lower()]

    # 排序: 大视频按合成率从大到小 —— 完整的 (合成率 100%/覆盖齐全) 排最前,
    # 其余大视频按合成率降序排在后面; 非大视频保持用户选择的排序方式垫底。
    def _synthesis_rate(f: CacheFile) -> float:
        if f.total_size:
            return min(int(f.covered_bytes or 0), int(f.total_size)) / int(f.total_size)
        return 0.0

    if sort == 'size_desc':
        sub_key = lambda f: -f.decrypted_size  # noqa: E731
    elif sort == 'size_asc':
        sub_key = lambda f: f.decrypted_size  # noqa: E731
    elif sort == 'name':
        sub_key = lambda f: f.file_name  # noqa: E731
    else:  # type
        sub_key = lambda f: f.file_type.value  # noqa: E731

    def _sort_key(f: CacheFile):
        if f.is_large_video:
            grp = 0 if f.is_complete_large_video else 1
            return (grp, -_synthesis_rate(f), sub_key(f))
        return (2, -1.0, sub_key(f))

    files.sort(key=_sort_key)

    total = len(files)
    start = (page - 1) * per_page
    end = start + per_page
    page_files = files[start:end]

    return jsonify({
        'files': [cache_file_to_dict(f) for f in page_files],
        'total': total,
        'page': page,
        'per_page': per_page,
        'pages': (total + per_page - 1) // per_page,
    })


@app.route('/api/file/<file_id>')
def api_file_detail(file_id):
    """获取单个文件详情"""
    f = find_file_by_id(file_id)
    if f is None:
        return jsonify({'error': '文件未找到'}), 404

    info = cache_file_to_dict(f)
    # Telegram 来源链接绑定状态 (预览弹窗据此显示绑定/解绑界面)
    info['telegram_link'] = _get_telegram_link(f)

    # ==================== 完整性判定 ====================
    # 全部基于 box 遍历与覆盖率, 不再用 bytes.find(b'moov') 猜 atom 是否存在。
    # 旧实现还有一处更严重的错位: 它用 range(1, slices_needed+1) 枚举分片,
    # 而 _enrich_scan_with_binlog 用 range(0, slices_needed), 两者差一个 ——
    # 于是"标记为完整"与"详情显示缺一个分片"会同时出现。
    if is_video(f.file_type) and f.file_type != FileType.VIDEO_SLICE:
        if f.is_large_video:
            _fill_large_video_info(info, f)
        elif f.file_type in (FileType.MP4, FileType.MOV):
            data = _safe_decrypt(file_id)
            if data is not None:
                v = mp4_validate(data)
                if v.duration_s:
                    info['playable_duration'] = round(v.duration_s, 3)
                if not v.ok:
                    info['is_incomplete'] = True
                    info['file_type_label'] = '不完整 MP4'
                    info['incomplete_reasons'] = list(v.reasons[:3])
        elif f.is_serialized:
            data = _safe_decrypt(file_id)
            a = None
            if data is not None:
                try:
                    exts = serialized_extents(data)
                    a = analyze_extents(exts, want_duration=True) if exts else None
                except Exception:
                    a = None
            if a is None:
                info['is_incomplete'] = True
                info['file_type_label'] = '不完整视频 (无法解析序列化数据)'
                info['incomplete_reasons'] = ['无法解析序列化数据']
            else:
                info['playable_duration'] = round(a.playable_duration_s, 3)
                info['is_complete_large_video'] = bool(a.complete)
                if not a.ok:
                    info['is_incomplete'] = True
                    info['file_type_label'] = '不完整视频'
                    info['incomplete_reasons'] = list(a.reasons[:3])
                elif not a.complete:
                    info['is_incomplete'] = True
                    info['file_type_label'] = '不完整视频 (缺少部分数据)'
                    info['incomplete_reasons'] = list(a.reasons[:3])

    # 如果是 TGS 动画贴片, 附加解压后的 Lottie JSON
    if f.file_type == FileType.TGS:
        try:
            data = get_scanner().get_decrypted_data(file_id)
            if data:
                lottie_json = decompress_tgs(data)
                if lottie_json:
                    info['lottie_json'] = lottie_json
                    # 提取贴纸名称
                    import json as _json
                    try:
                        lottie = _json.loads(lottie_json)
                        info['sticker_name'] = lottie.get('nm', '')
                    except Exception:
                        pass
        except Exception:
            pass

    # 如果是未知碎片, 附加十六进制预览和碎片分析
    if f.category == 'unknown':
        try:
            data = get_scanner().get_decrypted_data(file_id)
            if data:
                info['hex_preview'] = get_hex_preview(data, 48)
                info['fragment_hint'] = detect_fragment_type(data)
        except Exception:
            pass

    # 如果是序列化视频, 附加反序列化信息 (注意是**合并**而不是覆盖, 否则会把上面
    # 由覆盖率算出的 slice_details / missing_indices / complete 全部冲掉)
    if f.is_serialized:
        data = _safe_decrypt(file_id)
        if data:
            info['video_info'] = get_video_info(data)
            if f.is_large_video:
                lv = dict(info.get('large_video_info') or {})
                for k, v in get_large_video_info(data).items():
                    lv.setdefault(k, v)
                info['large_video_info'] = lv

    # 如果是视频分片, 附加分片信息
    if f.file_type == FileType.VIDEO_SLICE:
        slice_info = {
            'size': f.decrypted_size,
            'is_8mb': f.decrypted_size == K_IN_SLICE,
            'slice_index': -1,
            'parent_header_id': '',
        }
        # 通过 binlog 查找 slice_index 和所属大视频 header
        if binlog_index:
            record = binlog_index.get(f.file_name)
            if record:
                slice_info['slice_index'] = record.slice_index
                slice_info['key_high'] = f'0x{record.key_high:016X}'
                # 优先用预构建索引 (O(1), 按 doc_key 归属);
                # 索引缺失时才退化为一次有界查找
                parent = large_video_header_index.get(record.doc_key, '')
                if not parent:
                    for fname2, r2 in binlog_index.items():
                        if r2.doc_key == record.doc_key and r2.slice_index == 0:
                            parent = fname2
                            break
                if parent and parent in file_index:
                    slice_info['parent_header_id'] = parent
                    pf = file_index.get(parent)
                    slice_info['parent_is_complete'] = bool(
                        pf and pf.is_complete_large_video)
        info['slice_info'] = slice_info

    # ==================== 视频元数据 ====================
    # 重要: 这里**不再**写入导出目录。
    # 旧实现在 GET 详情时就会重建并把结果写到 exports/<file_id>.mp4 (最终文件名),
    # 于是"打开一张卡片"这个只读动作本身就会生成一个可能残缺的导出文件, 之后
    # api_preview 又会优先复用它 —— 这是"坏文件反复被端上桌"的源头之一。
    if f.category == 'video' and f.file_type != FileType.VIDEO_SLICE:
        try:
            meta = None
            export_path = _export_path_for(file_id)
            # 1) 已有导出且仍然有效: 直接读 sidecar 里记录的探测结果
            if export_path and os.path.exists(export_path) and _export_valid_for(file_id):
                sc = read_sidecar(export_path)
                if sc:
                    meta = dict(sc.ffprobe or {})
                    meta['duration'] = sc.duration_s or meta.get('duration', 0)
                    meta['truncated'] = bool(sc.truncated)

            # 2) 大视频: 用覆盖率分析得出的"可播时长", 不为了读时长而写出一部视频
            if meta is None and f.is_large_video:
                meta = {
                    'duration': round(f.playable_duration_s, 3),
                    'playable_duration': round(f.playable_duration_s, 3),
                    'truncated': not f.is_complete_large_video,
                    'complete': bool(f.is_complete_large_video),
                    'total_size': f.total_size,
                    'covered_bytes': f.covered_bytes,
                }

            # 3) 小视频: 落临时文件探测 (不落在导出目录里)
            if meta is None and not f.is_large_video:
                meta = _probe_small_video_meta(f, file_id)

            if meta:
                info['video_meta'] = meta
        except Exception:
            pass

    return jsonify(info)


@app.route('/api/preview/<file_id>')
def api_preview(file_id):
    """
    预览文件。

    - 图片 / TGS: 直接返回解密后的内存数据
    - 视频: 返回一个**稳定的**、支持 Range 的可播文件 (可 seek)
        * 大视频: 复用/生成 exports/ 下的导出产物 (经四道验证门)
        * 其他视频: 池化在 exports/.preview/ 下, 按 (file_id, 覆盖率指纹) 缓存

    与旧实现的关键差别: 旧版每次 Range 请求都会重新解密 + 反序列化 + ffmpeg remux,
    并把结果放在系统临时目录、响应结束时删掉。浏览器播放与拖动进度条会发多个
    Range 请求, 于是每次都重来一遍 —— 慢, 而且 ETag/Last-Modified 每次都变,
    条件请求失效, seek 基本等于重新下载。这正是"能播一下但不持久"的机械原因。
    """
    f = find_file_by_id(file_id)
    if f is None:
        return jsonify({'error': '文件未找到'}), 404

    content_types = {
        FileType.JPEG: 'image/jpeg',
        FileType.PNG: 'image/png',
        FileType.WEBP: 'image/webp',
        FileType.GIF: 'image/gif',
        FileType.BMP: 'image/bmp',
        FileType.PARTIAL_JPEG: 'image/jpeg',
        FileType.MP4: 'video/mp4',
        FileType.MOV: 'video/quicktime',
        FileType.WEBM: 'video/webm',
        FileType.SERIALIZED_VIDEO: 'video/mp4',
        FileType.VIDEO_SLICE: 'application/octet-stream',
        FileType.MP3: 'audio/mpeg',
        FileType.OGG: 'audio/ogg',
        FileType.TGS: 'application/gzip',
        FileType.UNKNOWN_FRAGMENT: 'application/octet-stream',
        FileType.UNKNOWN: 'application/octet-stream',
    }
    content_type = content_types.get(f.file_type, 'application/octet-stream')

    # ---- 大视频: 复用或生成持久化导出产物 ----
    if f.is_large_video:
        if not _export_valid_for(file_id):
            out = _produce_export(file_id)
            if not out.ok:
                return jsonify({
                    'error': '无法生成可播放文件',
                    'reasons': out.reasons[:4],
                    'missing_blocks': out.missing_blocks[:40],
                }), 500
        export_path = _export_path_for(file_id)
        if export_path and os.path.exists(export_path):
            return _serve_file(export_path, content_type)
        return jsonify({'error': '大视频重建失败, 可能缺少分片数据'}), 500

    # ---- 图片 / TGS: 直接返回内存数据 ----
    if is_image(f.file_type):
        data = _safe_decrypt(file_id)
        if data is None:
            return jsonify({'error': '解密失败'}), 500
        if f.file_type == FileType.PARTIAL_JPEG and data[:8] == b'partial:':
            data = data[8:]
        resp = Response(data, mimetype=content_type)
        resp.headers['Content-Length'] = str(len(data))
        resp.headers['Cache-Control'] = 'public, max-age=3600'
        return resp

    if f.file_type == FileType.TGS:
        data = _safe_decrypt(file_id)
        if data is None:
            return jsonify({'error': '解密失败'}), 500
        lottie_json = decompress_tgs(data)
        if not lottie_json:
            return jsonify({'error': 'TGS 解压失败'}), 500
        resp = Response(lottie_json, mimetype='application/json')
        resp.headers['Cache-Control'] = 'public, max-age=3600'
        return resp

    # ---- 其他视频: 池化预览产物 (只做一次解密 + 一次 ffmpeg) ----
    if is_video(f.file_type) and f.file_type != FileType.VIDEO_SLICE:
        export_path = _export_path_for(file_id)
        if export_path and os.path.exists(export_path) and _export_valid_for(file_id):
            return _serve_file(export_path, content_type)

        path, err = _preview_artifact(f)
        if path:
            return _serve_file(path, content_type)
        return jsonify({'error': err or '无法生成可播放的预览'}), 500

    # ---- 音频 / 未知: 直接返回内存数据 ----
    data = _safe_decrypt(file_id)
    if data is None:
        return jsonify({'error': '解密失败'}), 500
    resp = Response(data, mimetype=content_type)
    resp.headers['Content-Length'] = str(len(data))
    resp.headers['Cache-Control'] = 'no-cache'
    return resp


def _placeholder_image():
    """返回 1x1 透明像素 (用于无缩略图的文件, 前端显示 placeholder)"""
    # 1x1 透明 GIF
    pixel = b'GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;'
    return Response(pixel, mimetype='image/gif', headers={'Cache-Control': 'no-store'})


# ==================== 预览产物池 ====================
#
# 目的: 让"同一个视频的多个 Range 请求"只做一次解密 + 一次 ffmpeg。
#
# 旧实现每次请求都把 remux 结果写进系统临时目录, 并在响应结束时删除。
# 浏览器播放与拖动进度条会发多个 Range 请求 -> 每次都重新解密+remux;
# 而且 ETag/Last-Modified 每次都变, 条件请求失效, seek 基本等于重新下载。
# 同时 `resp.call_on_close = _cleanup` 的写法本身也是错的: Werkzeug 的
# `Response.call_on_close` 是"注册回调"的**方法** (签名 call_on_close(func)),
# 不是可读写属性。旧代码取出的 `original_call` 是绑定方法, 零参调用会抛
# TypeError, 同时赋值又把该方法覆盖掉了 —— 清理时机完全不可靠。

PREVIEW_SUBDIR = '.preview'
PREVIEW_MAX_FILES = 24
# path -> 已验证过的文件大小 (避免每个 Range 请求都重跑一次 box 校验)
_preview_ready: Dict[str, int] = {}
_preview_lock = threading.Lock()


def _preview_dir() -> str:
    d = os.path.join(Config.export_dir, PREVIEW_SUBDIR)
    os.makedirs(d, exist_ok=True)
    return d


def _serve_file(path: str, content_type: str, cache: str = 'no-cache'):
    """按 Range 发送一个**稳定存在**的文件 (不再"响应结束即删除")"""
    resp = send_file(path, mimetype=content_type, conditional=True)
    resp.headers['Cache-Control'] = cache
    resp.headers['Accept-Ranges'] = 'bytes'
    return resp


def _preview_artifact(f: CacheFile) -> Tuple[Optional[str], str]:
    """
    为普通视频准备一个稳定的预览产物, 返回 (路径, 错误信息)。

    按 (file_id, 覆盖率指纹) 命名, 只在缺失或失效时重建。
    """
    sig = f.coverage_signature or 'v1'
    target = os.path.join(_preview_dir(), f'{f.file_id}.{sig}.mp4')

    if os.path.exists(target) and os.path.getsize(target) > 0:
        size = os.path.getsize(target)
        if (_preview_ready.get(target) == size
                or not mp4.looks_like_iso_bmff(target)
                or mp4_validate(target).ok):
            with _preview_lock:
                _preview_ready[target] = size
            return target, ''

    stage_fn, remux = _stage_producer(f)
    stage = staging_path(target)
    ready = part_path(target)
    try:
        res = stage_fn(stage)
        if not getattr(res, 'playable', False):
            return None, '; '.join(getattr(res, 'reasons', ()) or ['无法产出可播放片段'])
        if remux:
            from src.exporter import _remux_file
            rr = _remux_file(stage, ready,
                             want_duration=float(getattr(res, 'duration_s', 0) or 0))
            if not rr.ok:
                return None, '转封装失败: ' + '; '.join(rr.reasons[:2] or ['未知原因'])
            os.replace(ready, target)
        else:
            os.replace(stage, target)
        with _preview_lock:
            _preview_ready[target] = os.path.getsize(target)
        _trim_preview_pool()
        return target, ''
    except Exception as exc:
        return None, f'预览生成异常: {exc.__class__.__name__}: {exc}'
    finally:
        for leftover in (stage, ready):
            try:
                if os.path.exists(leftover):
                    os.remove(leftover)
            except OSError:
                pass


def _trim_preview_pool() -> None:
    """限制预览产物数量 (按 mtime 从旧到新删)"""
    try:
        d = _preview_dir()
        items = []
        for name in os.listdir(d):
            p = os.path.join(d, name)
            if os.path.isfile(p):
                items.append((os.path.getmtime(p), p))
        if len(items) <= PREVIEW_MAX_FILES:
            return
        items.sort()
        with _preview_lock:
            for _mt, p in items[:len(items) - PREVIEW_MAX_FILES]:
                try:
                    os.remove(p)
                    _preview_ready.pop(p, None)
                except OSError:
                    pass
    except OSError:
        pass


@app.route('/api/thumbnail/<file_id>')
def api_thumbnail(file_id):
    """获取或生成缩略图"""
    f = find_file_by_id(file_id)
    if f is None:
        return jsonify({'error': '文件未找到'}), 404

    # 只为图片、视频和贴片生成缩略图
    if not is_image(f.file_type) and not is_video(f.file_type) and f.file_type != FileType.TGS:
        return jsonify({'error': '此类型不支持缩略图'}), 400

    # 视频分片: 使用父 header 的缩略图
    if f.file_type == FileType.VIDEO_SLICE:
        info = cache_file_to_dict(f)
        parent_id = info.get('parent_header_id', '')
        if parent_id:
            parent_thumb = os.path.join(Config.thumbnail_dir, f"{parent_id}.jpg")
            if os.path.exists(parent_thumb):
                return send_file(parent_thumb, mimetype='image/jpeg')
            parent_file = find_file_by_id(parent_id)
            if parent_file:
                result = _make_thumbnail(parent_file)
                if result:
                    return send_file(result, mimetype='image/jpeg')
        # 无父 header 或生成失败: 返回占位图 (前端显示 placeholder)
        return _placeholder_image()

    thumb_path = os.path.join(Config.thumbnail_dir, f"{file_id}.jpg")
    if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
        return send_file(thumb_path, mimetype='image/jpeg')

    result = _make_thumbnail(f)
    if result:
        return send_file(result, mimetype='image/jpeg')
    return _placeholder_image()


def _make_thumbnail(f: CacheFile) -> Optional[str]:
    """
    生成缩略图 (视频首帧 / 图片), 返回路径。

    大视频走**流式重建到临时文件**再抽首帧。旧实现用 _rebuild_large_video 把
    整部视频 (含分片拼接) 读进内存, 峰值内存可达 5-6GB —— 这正是大文件场景
    崩溃/超时的来源之一。
    """
    thumb_path = os.path.join(Config.thumbnail_dir, f"{f.file_id}.jpg")
    if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
        f.thumbnail_path = thumb_path
        return thumb_path

    tmp_path = None
    try:
        if f.is_large_video:
            header_data = _safe_decrypt(f.file_id)
            if not header_data:
                return None
            fd, tmp_path = tempfile.mkstemp(suffix='.mp4', dir=tempfile.gettempdir())
            os.close(fd)
            sl = _slices_for_header(f.file_id)
            exts = media_part_extents(header_data)
            if exts:
                res = repack_from_extents(exts, tmp_path, sl,
                                          decrypt_fn=_decrypt_by_path)
            else:
                res = rebuild_large_video_to_file(
                    header_data, sl, tmp_path, decrypt_fn=_decrypt_by_path)
            if not getattr(res, 'playable', False):
                return None
            out = _video_thumbnail_from_path(tmp_path, thumb_path, (320, 320))
        else:
            data = _safe_decrypt(f.file_id)
            if not data:
                return None
            if f.is_serialized and not (len(data) > 12 and data[4:8] == b'ftyp'):
                mp4_data = deserialize_video(data)
                if mp4_data is None:
                    return None
                data = mp4_data
            out = generate_thumbnail(data, f.file_type, thumb_path)

        if out and os.path.exists(out) and os.path.getsize(out) > 0:
            f.thumbnail_path = out
            return out
        if os.path.exists(thumb_path) and os.path.getsize(thumb_path) == 0:
            try:
                os.remove(thumb_path)
            except OSError:
                pass
        return None
    except Exception:
        return None
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


@app.route('/api/export/<file_id>', methods=['POST'])
def api_export(file_id):
    """
    导出单个文件。

    统一走 _produce_export:
        重建/重打包 -> 转封装 -> 四道验证门 -> 原子提交
    失败时返回 500 与可展示的原因, **不会**留下以最终文件名命名的半成品,
    也不会破坏已经存在的好文件。
    成功时的 {ok, filename, size} 与旧版一致 (前端契约不变), 其余为新增字段。
    """
    f = find_file_by_id(file_id)
    if f is None:
        return jsonify({'error': '文件未找到'}), 404

    out = _produce_export(file_id)
    payload = _outcome_json(out, file_id)
    if not out.ok:
        payload['error'] = '；'.join(out.reasons[:3]) or '导出失败'
        return jsonify(payload), 500
    return jsonify(payload)


@app.route('/api/export_batch', methods=['POST'])
def api_export_batch():
    """
    批量导出文件 (同步)。

    每一项都走同一个 _produce_export, 因此失败项会带上具体原因, 而不是像旧版
    那样只要 mp4_data 非空就记 success=True (即使导出的是个零洞文件)。
    """
    payload = request.get_json(silent=True) or {}
    file_ids = payload.get('file_ids', [])
    if not file_ids:
        return jsonify({'error': '未指定文件'}), 400

    results = []
    success_count = 0
    for file_id in file_ids:
        out = _produce_export(file_id)
        item = _outcome_json(out, file_id)
        item['success'] = bool(out.ok)
        if out.ok:
            success_count += 1
        else:
            item['error'] = '；'.join(out.reasons[:3]) or '导出失败'
        results.append(item)

    # 旧版忘了刷新导出索引, 于是批量导出后卡片上的"已重建"标记不更新
    if success_count:
        _rebuild_exported_ids()

    return jsonify({
        'ok': True,
        'total': len(file_ids),
        'success': success_count,
        'failed': len(file_ids) - success_count,
        'results': results,
    })


@app.route('/api/exports')
def api_exports():
    """
    获取已导出文件列表。

    只列**真正的媒体导出**, 用扩展名白名单过滤掉:
      <id>.mp4.part / <id>.mp4.part.src  中间产物
      <id>.mp4.json                      sidecar (单独作为元数据附带)
      .preview/                          预览产物池 (子目录)
    旧实现会把目录里的每个普通文件都列出来, 于是引入中间文件后, 导出管理里
    就会出现一堆 .part / .json 垃圾项。
    """
    if not os.path.exists(Config.export_dir):
        return jsonify({'files': []})

    files = []
    for fname in os.listdir(Config.export_dir):
        if not is_media_filename(fname):
            continue
        fpath = os.path.join(Config.export_dir, fname)
        if not os.path.isfile(fpath):
            continue
        stat = os.stat(fpath)
        # 尝试从 file_id 反查真实文件名 (display_name) 用于展示
        display_name = ''
        stem = os.path.splitext(fname)[0]
        file_id = stem.split('_')[0] if '_' in stem else stem
        cf = find_file_by_id(file_id)
        if cf:
            info = cache_file_to_dict(cf)
            display_name = info.get('display_name', '')

        sc = read_sidecar(fpath)
        entry = {
            'filename': fname,
            'display_name': display_name,
            'size': stat.st_size,
            'modified': stat.st_mtime,
            'file_id': file_id,
            'valid': is_current(fpath, _current_signature(file_id)),
        }
        if sc:
            entry['duration'] = round(sc.duration_s or 0, 3)
            entry['truncated'] = bool(sc.truncated)
            entry['pipeline_version'] = sc.version
        files.append(entry)

    files.sort(key=lambda x: x['modified'], reverse=True)
    return jsonify({'files': files})


@app.route('/api/exports/<path:filename>')
def api_download_export(filename):
    """下载已导出的文件"""
    fpath = os.path.join(Config.export_dir, filename)
    abspath = os.path.abspath(fpath)
    export_dir_abs = os.path.abspath(Config.export_dir)
    if not (abspath == export_dir_abs or abspath.startswith(export_dir_abs + os.sep)):
        return jsonify({'error': '非法路径'}), 400
    if not os.path.exists(fpath):
        return jsonify({'error': '文件不存在'}), 404
    # 用真实文件名作为下载名 (有 display_name 时), 否则用原始文件名
    download_name = filename
    file_id = os.path.splitext(filename)[0]
    cf = find_file_by_id(file_id)
    if cf:
        info = cache_file_to_dict(cf)
        disp = info.get('display_name', '')
        if disp:
            download_name = f"{disp}{os.path.splitext(filename)[1]}"
    return send_file(fpath, as_attachment=True, download_name=download_name)


@app.route('/api/exports/inline/<path:filename>')
def api_inline_export(filename):
    """内联查看已导出的文件 (用于浏览器 video/audio 标签播放, 非 attachment)"""
    fpath = os.path.join(Config.export_dir, filename)
    if not os.path.exists(fpath):
        return jsonify({'error': '文件不存在'}), 404
    # 安全检查
    if not os.path.abspath(fpath).startswith(os.path.abspath(Config.export_dir)):
        return jsonify({'error': '非法路径'}), 400
    resp = send_file(fpath, mimetype='video/mp4', conditional=True)
    resp.headers['Cache-Control'] = 'public, max-age=3600'
    resp.headers['Accept-Ranges'] = 'bytes'
    return resp


@app.route('/api/exports/<path:filename>', methods=['DELETE'])
def api_delete_export(filename):
    """删除已导出的文件"""
    fpath = os.path.join(Config.export_dir, filename)
    if not os.path.exists(fpath):
        return jsonify({'error': '文件不存在'}), 404

    # 安全检查: 确保路径在导出目录内
    if not os.path.abspath(fpath).startswith(os.path.abspath(Config.export_dir)):
        return jsonify({'error': '非法路径'}), 400

    os.remove(fpath)
    _rebuild_exported_ids()
    return jsonify({'ok': True})


@app.route('/api/exports/clear', methods=['POST'])
def api_clear_exports():
    """清空所有导出文件 (含预览产物池)"""
    if not os.path.exists(Config.export_dir):
        return jsonify({'ok': True, 'deleted': 0})

    count = 0
    for fname in os.listdir(Config.export_dir):
        fpath = os.path.join(Config.export_dir, fname)
        if os.path.isfile(fpath):
            try:
                os.remove(fpath)
                count += 1
            except OSError:
                pass
        elif os.path.isdir(fpath) and fname == PREVIEW_SUBDIR:
            import shutil as _shutil
            _shutil.rmtree(fpath, ignore_errors=True)
    with _preview_lock:
        _preview_ready.clear()
    with _export_verify_lock:
        _export_verified.clear()
    _rebuild_exported_ids()
    return jsonify({'ok': True, 'deleted': count})


@app.route('/api/delete_export/<file_id>', methods=['POST'])
def api_delete_export_by_id(file_id):
    """按 file_id 删除对应的导出文件 (用于重新重建前清理旧文件)"""
    deleted = _remove_export_file(file_id)
    if deleted:
        _rebuild_exported_ids()
    return jsonify({'ok': True, 'deleted': deleted})


@app.route('/api/open_folder/<path:filename>', methods=['POST'])
def api_open_folder(filename):
    """
    在资源管理器中打开导出文件所在文件夹并选中该文件
    仅允许导出目录内的文件
    """
    fpath = os.path.join(Config.export_dir, filename)
    abspath = os.path.abspath(fpath)
    export_dir_abs = os.path.abspath(Config.export_dir)
    if not (abspath == export_dir_abs or abspath.startswith(export_dir_abs + os.sep)):
        return jsonify({'error': '非法路径'}), 400
    if not os.path.exists(abspath):
        return jsonify({'error': '文件不存在'}), 404
    try:
        if sys.platform == 'win32':
            subprocess.Popen(['explorer', '/select,', abspath], creationflags=0x08000000)
        else:
            # 非 Windows: 打开所在目录
            subprocess.Popen(['open', os.path.dirname(abspath)])
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': f'打开失败: {e}'}), 500


@app.route('/api/cleanup', methods=['POST'])
def api_cleanup():
    """
    清理孤儿文件:
    - 清理缩略图目录中不属于当前扫描结果的缩略图 (file_id 不在扫描结果中)
    - 清理导出目录中不属于当前扫描结果的导出文件
    返回清理的数量
    """
    if scan_result is None:
        return jsonify({'error': '尚未扫描, 无法判断哪些是孤儿文件'}), 400

    with files_lock:
        valid_ids = {f.file_id for f in list(scan_result.files)}
    # 也保留所有已导出文件名 (可能有 display_name 变体, 按 file_id 前缀匹配)
    thumb_deleted = 0
    export_deleted = 0

    # 清理缩略图
    if os.path.exists(Config.thumbnail_dir):
        for fname in os.listdir(Config.thumbnail_dir):
            if not fname.endswith('.jpg'):
                continue
            # 跳过下载文件缩略图 (dl_ 前缀)
            if fname.startswith('dl_'):
                continue
            file_id = os.path.splitext(fname)[0]
            if file_id not in valid_ids:
                try:
                    os.remove(os.path.join(Config.thumbnail_dir, fname))
                    thumb_deleted += 1
                except Exception:
                    pass

    # 清理导出文件 (只清真正的媒体导出; 中间产物与 sidecar 交给 cleanup_stale)
    if os.path.exists(Config.export_dir):
        for fname in os.listdir(Config.export_dir):
            fpath = os.path.join(Config.export_dir, fname)
            if not os.path.isfile(fpath):
                continue              # 跳过 .preview/ 等子目录
            if not is_media_filename(fname):
                continue              # 跳过 .part / .part.src / .json
            stem = os.path.splitext(fname)[0]
            file_id = stem.split('_')[0] if '_' in stem else stem
            if file_id not in valid_ids:
                try:
                    os.remove(fpath)
                    export_deleted += 1
                except Exception:
                    pass

    # 顺手清掉崩溃遗留的中间产物与孤儿 sidecar
    stale = cleanup_stale(Config.export_dir)
    _rebuild_exported_ids()

    return jsonify({
        'ok': True,
        'thumb_deleted': thumb_deleted,
        'export_deleted': export_deleted,
        'stale_part_deleted': stale.get('part', 0),
        'stale_sidecar_deleted': stale.get('sidecar', 0),
    })


# ==================== 清空缓存 / 下载目录 ====================
#
# 设计原则: **两步走** —— 先 `confirm=false` 预演 (只统计不删), 界面显示数量与
# 体积, 用户二次确认后才 `confirm=true` 真正删除。
#
# 安全红线 (任何情况下都不会跨越, 见 `_resolve_target_dirs`):
#   telegram : 只删 <tdata>/user_data/cache/** 与 <tdata>/user_data/media_cache/**
#              下的普通文件, 且跳过 version / binlog。
#              key_datas、D877F783D5D3EF8C(账号数据/密钥/设置/binlog)、
#              settingss、emoji 等一律不碰 —— 那是真正的用户数据。
#   download : 必须已配置; 拒绝驱动器根目录、用户主目录/桌面/文档/下载,
#              以及与 tdata 有包含关系(任一方向)的路径。
#   app      : 只清本工具自己产出的 exports/ 与 thumbnails/。

# (target_label, tdata 相对路径)
_TG_CACHE_DIRS = (
    ('cache', os.path.join('user_data', 'cache')),
    ('media_cache', os.path.join('user_data', 'media_cache')),
)
# 缓存目录内的结构文件, 删了会让 Telegram 重建缓存索引异常
_TG_CACHE_KEEP_FILES = {'version', 'binlog'}

_CLEAR_TARGET_LABELS = {
    'telegram': 'Telegram 媒体缓存',
    'download': '下载目录',
    'app': '应用缓存',
}


def _unsafe_root_dirs() -> set:
    """绝不允许作为"下载目录"被清空的路径集合"""
    roots = set()
    home = os.path.expanduser('~')
    for p in (home,
              os.path.join(home, 'Desktop'),
              os.path.join(home, 'Documents'),
              os.path.join(home, 'Downloads'),
              os.path.join(home, 'Pictures'),
              os.path.join(home, 'Videos')):
        try:
            roots.add(os.path.abspath(p))
        except Exception:
            pass
    for p in (os.path.abspath(Config.tdata_path), os.path.abspath(BASE_DIR)):
        roots.add(p)
    return roots


def _resolve_target_dirs(target: str):
    """
    把 target 解析成待清空的目录列表。

    Returns: ([(label, abspath), ...], error_message)
    """
    if target == 'telegram':
        tdata = os.path.abspath(Config.tdata_path)
        if not os.path.isdir(tdata):
            return None, f'tdata 目录不存在: {tdata}'
        dirs = []
        for label, rel in _TG_CACHE_DIRS:
            d = os.path.abspath(os.path.join(tdata, rel))
            if os.path.isdir(d):
                dirs.append((label, d))
        if not dirs:
            return None, '未找到 Telegram 缓存目录 (user_data\\cache / user_data\\media_cache)'
        return dirs, None

    if target == 'download':
        dp = (Config.download_path or '').strip()
        if not dp:
            return None, '尚未配置 Telegram 下载目录，请先在上方填写'
        d = os.path.abspath(dp)
        if not os.path.isdir(d):
            return None, f'下载目录不存在: {d}'
        if os.path.splitdrive(d)[1] in ('\\', '/', ''):
            return None, '拒绝清空驱动器根目录'
        tdata = os.path.abspath(Config.tdata_path)
        if d == tdata or tdata.startswith(d + os.sep) or d.startswith(tdata + os.sep):
            return None, '下载目录与 tdata 目录存在包含关系，已拒绝（防止误删账号数据）'
        if d in _unsafe_root_dirs():
            return None, f'拒绝清空受保护目录（主目录/桌面/文档/下载/tdata/程序目录）: {d}'
        return [('download', d)], None

    if target == 'app':
        dirs = []
        for label, p in (('exports', Config.export_dir),
                         ('thumbnails', Config.thumbnail_dir)):
            ap = os.path.abspath(p)
            if os.path.isdir(ap):
                dirs.append((label, ap))
        if not dirs:
            return None, '应用缓存目录不存在'
        return dirs, None

    return None, f'未知目标: {target}'


def _is_under_any(path: str, dirs) -> bool:
    """路径是否位于任一允许目录之内（防 symlink 逃逸）"""
    ap = os.path.abspath(path)
    for _label, d in dirs:
        dd = os.path.abspath(d)
        if ap == dd or ap.startswith(dd + os.sep):
            return True
    return False


def _collect_files(dirs, keep_names=None):
    """收集 dirs 下的普通文件，返回 [(abspath, size)]"""
    out = []
    for _label, d in dirs:
        for root, _subdirs, files in os.walk(d):
            for fname in files:
                if keep_names and fname in keep_names:
                    continue
                fp = os.path.join(root, fname)
                if not os.path.isfile(fp):
                    continue
                if not _is_under_any(fp, dirs):
                    continue
                try:
                    size = os.path.getsize(fp)
                except Exception:
                    size = 0
                out.append((fp, size))
    return out


def _prune_empty_dirs(dirs) -> int:
    """删除清空后残留的空子目录（保留各根目录本身）"""
    removed = 0
    for _label, d in dirs:
        for root, _subdirs, _files in os.walk(d, topdown=False):
            if os.path.abspath(root) == os.path.abspath(d):
                continue
            try:
                if not os.listdir(root):
                    os.rmdir(root)
                    removed += 1
            except Exception:
                pass
    return removed


def _reset_scan_state():
    """清空缓存后，扫描结果已经指向不存在的文件，必须整体作废"""
    global scan_result, file_index, file_name_index, binlog_index
    global location_index, download_files_cache, large_video_header_index
    scan_result = None
    file_index = {}
    file_name_index = {}
    binlog_index = None
    location_index = None
    download_files_cache = []
    large_video_header_index = {}
    last_file_signatures.clear()
    scan_status.update({
        'scanning': False,
        'progress': 0,
        'total': 0,
        'current_file': '',
        'finished': False,
        'error': None,
        'binlog_locked': False,
        'binlog_available': False,
    })
    with _preview_lock:
        _preview_ready.clear()
    with _export_verify_lock:
        _export_verified.clear()
    _rebuild_exported_ids()


@app.route('/api/clear_cache', methods=['POST'])
def api_clear_cache():
    """
    清空缓存 / 下载目录（两步: 先预演后执行）。

    body: {"target": "telegram"|"download"|"app", "confirm": true|false}

    confirm=false: 只统计，返回 file_count / total_bytes / dirs / sample
    confirm=true : 真正删除，返回 deleted / failed / freed_bytes / need_rescan
    """
    data = request.get_json(silent=True) or {}
    target = (data.get('target') or '').strip()
    confirm = bool(data.get('confirm'))

    if target not in _CLEAR_TARGET_LABELS:
        return jsonify({'error': f'未知目标: {target}'}), 400

    dirs, err = _resolve_target_dirs(target)
    if err:
        return jsonify({'error': err, 'target': target}), 400

    keep = _TG_CACHE_KEEP_FILES if target == 'telegram' else None

    if not confirm:
        items = _collect_files(dirs, keep_names=keep)
        total = sum(s for _p, s in items)
        sample = [os.path.basename(p) for p, _s in items[:5]]
        return jsonify({
            'ok': True,
            'target': target,
            'label': _CLEAR_TARGET_LABELS[target],
            'dirs': [{'label': l, 'path': p} for l, p in dirs],
            'file_count': len(items),
            'total_bytes': total,
            'sample': sample,
            'confirm_required': True,
        })

    items = _collect_files(dirs, keep_names=keep)
    deleted = 0
    failed = 0
    freed = 0
    errors = []
    for fp, size in items:
        try:
            os.remove(fp)
            deleted += 1
            freed += size
        except Exception as e:
            failed += 1
            if len(errors) < 5:
                errors.append(f'{os.path.basename(fp)}: {e}')

    dirs_removed = 0
    if target in ('download', 'app'):
        dirs_removed = _prune_empty_dirs(dirs)

    need_rescan = False
    if target == 'telegram':
        _reset_scan_state()
        need_rescan = True
    elif target == 'download':
        download_files_cache.clear()
    elif target == 'app':
        with _preview_lock:
            _preview_ready.clear()
        with _export_verify_lock:
            _export_verified.clear()
        _rebuild_exported_ids()

    return jsonify({
        'ok': True,
        'target': target,
        'label': _CLEAR_TARGET_LABELS[target],
        'deleted': deleted,
        'failed': failed,
        'freed_bytes': freed,
        'dirs_removed': dirs_removed,
        'need_rescan': need_rescan,
        'errors': errors,
    })


@app.route('/api/open_export_folder', methods=['POST'])
def api_open_export_folder():
    """在资源管理器中打开导出目录"""
    try:
        if sys.platform == 'win32':
            subprocess.Popen(['explorer', Config.export_dir], creationflags=0x08000000)
        else:
            subprocess.Popen(['open', Config.export_dir])
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': f'打开失败: {e}'}), 500


@app.route('/api/rebuild_complete_videos', methods=['POST'])
def api_rebuild_complete_videos():
    """
    一键重建所有"分片齐全"的大视频。

    只有覆盖率判定为完整的视频才会被重建, 且结果必须通过四道验证门才会落到
    最终文件名。旧版会把缺中段分片的视频也当成"完整"去重建 (因为
    `estimated_size <= max_end ⇒ slices_needed = 0` 几乎恒真), 结果是批量产出
    满是零洞、无法播放的文件, 并且这些文件还会被登记为"已重建"反复复用。
    """
    if scan_result is None:
        return jsonify({'error': '尚未扫描'}), 400

    with files_lock:
        targets = [f.file_id for f in list(scan_result.files)
                   if f.is_large_video and f.is_complete_large_video]
    if not targets:
        return jsonify({'error': '没有分片完整的大视频可重建'}), 400

    results = []
    success_count = 0
    for file_id in targets:
        out = _produce_export(file_id)
        item = _outcome_json(out, file_id)
        item['success'] = bool(out.ok)
        if out.ok:
            success_count += 1
        else:
            item['error'] = '；'.join(out.reasons[:3]) or '重建失败'
        results.append(item)

    if success_count:
        _rebuild_exported_ids()

    return jsonify({
        'ok': True,
        'total': len(targets),
        'success': success_count,
        'failed': len(targets) - success_count,
        'results': results,
    })


@app.route('/api/export_batch_async', methods=['POST'])
def api_export_batch_async():
    """异步批量导出: 启动后台任务, 返回 task_id"""
    data = request.get_json(silent=True) or {}
    file_ids = data.get('file_ids', [])
    if not file_ids:
        return jsonify({'error': '未指定文件'}), 400

    task_id = hashlib.md5(f"{time.time()}{file_ids}".encode()).hexdigest()[:12]
    export_tasks[task_id] = {
        'status': 'running',       # running / done / cancelled / error
        'total': len(file_ids),
        'progress': 0,
        'success': 0,
        'failed': 0,
        'current_file': '',
        'results': [],
        'cancel_flag': False,
    }

    def run_batch():
        task = export_tasks[task_id]
        for file_id in file_ids:
            if task['cancel_flag']:
                task['status'] = 'cancelled'
                # 取消后也要注册清理 Timer (防止内存泄漏)
                threading.Timer(300, lambda: export_tasks.pop(task_id, None)).start()
                return
            task['current_file'] = file_id
            f = find_file_by_id(file_id)
            if f is None:
                task['results'].append({'file_id': file_id, 'success': False, 'error': '文件未找到'})
                task['progress'] += 1
                task['failed'] += 1
                continue
            # 每一项都走同一个统一入口, 保证"失败不谎报成功、不留下半成品"
            out = _produce_export(file_id)
            item = _outcome_json(out, file_id)
            item['success'] = bool(out.ok)
            if out.ok:
                task['success'] += 1
            else:
                item['error'] = '；'.join(out.reasons[:3]) or '导出失败'
                task['failed'] += 1
            task['results'].append(item)
            task['progress'] += 1
        if task['status'] == 'running':
            task['status'] = 'done'
        # 刷新导出索引
        if task['success'] > 0:
            _rebuild_exported_ids()
        # 任务完成后 5 分钟自动清理, 防止内存泄漏
        threading.Timer(300, lambda: export_tasks.pop(task_id, None)).start()

    thread = threading.Thread(target=run_batch, daemon=True)
    thread.start()
    return jsonify({'ok': True, 'task_id': task_id})


@app.route('/api/task/status/<task_id>')
def api_task_status(task_id):
    """查询异步任务进度"""
    task = export_tasks.get(task_id)
    if task is None:
        return jsonify({'error': '任务不存在'}), 404
    return jsonify({
        'status': task['status'],
        'total': task['total'],
        'progress': task['progress'],
        'success': task['success'],
        'failed': task['failed'],
        'current_file': task['current_file'],
    })


@app.route('/api/task/cancel/<task_id>', methods=['POST'])
def api_task_cancel(task_id):
    """取消异步任务"""
    task = export_tasks.get(task_id)
    if task is None:
        return jsonify({'error': '任务不存在'}), 404
    task['cancel_flag'] = True
    return jsonify({'ok': True})


# ==================== 大视频重建辅助 ====================

def _load_binlog_index() -> Optional[Dict[str, BinlogRecord]]:
    """加载 binlog 索引, 用于关联大视频 header 与 8MB slice"""
    binlog_path = os.path.join(Config.tdata_path, "user_data", "media_cache", "1", "binlog")
    if not os.path.exists(binlog_path):
        return None
    s = get_scanner()
    if not s.local_key:
        return None
    records = parse_binlog(binlog_path, s.local_key)
    return build_key_index(records)


def _reclassify_with_binlog(result, bidx) -> bool:
    """
    用 binlog 把 slice / header 归类正确 (只改归类, 不判定完整性)。

    - 同组存在 slice_index > 0 -> 该组是"大视频"
    - slice_index == 0 的文件是 header (它自己只含 [0,128KB), 其余在外部 slice 0 里)
    - slice_index > 0 的文件是 8MB 分片
    """
    key_high_records = {}
    for f in result.files:
        r = bidx.get(f.file_name)
        if r:
            key_high_records.setdefault(r.key_high, []).append(r)

    large_keys = {kh for kh, items in key_high_records.items()
                  if any(r.slice_index > 0 for r in items)}

    changed = False
    for f in result.files:
        r = bidx.get(f.file_name)
        if not r or r.key_high not in large_keys:
            continue
        if r.slice_index == 0:
            # slice_index == 0 有两类文件: header 本身, 以及覆盖 [0,8MiB) 的
            # **外部** 8MB 分片 (get_slices_for_header 刻意保留 index 0 就是为此)。
            # 不能用 slice_index 区分它们, 否则一个原始数据块会被误标成"大视频",
            # 在界面上变成一个假的、永远重建不出来的条目。
            # 可靠判据: header 是一个很小的映射结构, 不会是整整 8MiB。
            if not f.is_large_video and f.decrypted_size != K_IN_SLICE:
                f.is_large_video = True
                f.is_serialized = True
                if f.file_type != FileType.SERIALIZED_VIDEO:
                    f.file_type = FileType.SERIALIZED_VIDEO
                    f.file_type_label = TYPE_LABELS.get(
                        FileType.SERIALIZED_VIDEO, "序列化视频")
                    f.category = CATEGORY_MAP.get(FileType.SERIALIZED_VIDEO, "video")
                    changed = True
        else:
            if f.file_type != FileType.VIDEO_SLICE:
                f.file_type = FileType.VIDEO_SLICE
                f.file_type_label = TYPE_LABELS.get(FileType.VIDEO_SLICE, "视频分片")
                f.category = CATEGORY_MAP.get(FileType.VIDEO_SLICE, "video")
                f.is_serialized = False
                f.is_large_video = False
                changed = True
    return changed


def _apply_coverage(f: CacheFile, bidx=None) -> None:
    """
    对一个大视频重算覆盖率与完整性 —— 唯一权威的完整性判定。

    完整 ⇔ 覆盖区间 ⊇ moov 里每个 chunk 的 [offset, offset+size)

    旧实现是两套互不一致的推断:
      - 无 binlog: 用"全盘 8MB 文件总数 >= slices_needed"冒充这个视频的分片数
      - 有 binlog: slices_needed == 0 就判完整, 否则 range(0, N) 索引算术
    而 get_large_video_info 旧的 `estimated_size <= max_end ⇒ slices_needed = 0`
    几乎恒为真 (is_large_video_header 的判定条件恰好使 max_end 接近 EOF),
    于是缺掉整个 mdat 中段的视频被判成"完整", 一键重建批量产出零洞文件。
    """
    try:
        header_data = get_scanner().get_decrypted_data(f.file_name)
    except Exception:
        header_data = None

    if not header_data:
        f.is_complete_large_video = False
        f.missing_reason = '无法解密 header'
        return

    try:
        # header 若是真实分区块格式, 覆盖范围已自描述 —— 但单个文件最多约 8MiB,
        # 大视频仍需并上 binlog 精确归组出来的外部 8MiB 分片才判得准。
        exts = media_part_extents(header_data) if header_data else []
        sl = _slices_for_header(f.file_name, bidx)
        if exts:
            a = analyze_extents(exts, sl, decrypt_fn=_decrypt_by_path,
                                want_duration=True)
        else:
            a = analyze_large_video(header_data, sl,
                                    decrypt_fn=_decrypt_by_path,
                                    want_duration=True)
    except Exception as exc:
        f.is_complete_large_video = False
        f.missing_reason = f'分析失败: {exc.__class__.__name__}'
        return

    cov = a.coverage
    f.slices_needed = a.slices_needed
    f.total_size = a.total_size
    f.moov_present = a.moov_present
    f.covered_bytes = cov.covered_bytes() if cov else 0
    f.coverage_signature = cov.signature() if cov else ''
    f.coverage_json = cov.to_json() if cov else ''
    f.missing_slice_indices = list(a.missing_blocks)
    f.playable_duration_s = a.playable_duration_s
    f.is_complete_large_video = bool(a.complete)

    if a.complete:
        f.missing_reason = ''
    elif a.missing_blocks:
        f.missing_reason = f'缺 {len(a.missing_blocks)} 个分片'
    else:
        f.missing_reason = '; '.join(a.reasons[:1]) or '缓存不完整'


def _recount(result) -> None:
    """重新计算分类统计"""
    result.by_category = {}
    result.by_type = {}
    for f in result.files:
        cat = f.category
        result.by_category[cat] = result.by_category.get(cat, 0) + 1
        result.by_type[f.file_type.value] = result.by_type.get(f.file_type.value, 0) + 1


def _enrich_scan(result, bidx=None):
    """
    扫描后的统一富化: 归类 + 覆盖率完整性判定。

    **每次扫描都无条件重算覆盖率** —— 分片文件可以在 header 的 mtime/size 不变的
    情况下增减, 所以完整性不可跨扫描缓存。增量扫描按引用复用 CacheFile 的做法
    会残留旧的 is_complete_large_video 标记, 必须在这里覆写。

    **无 binlog 时不再猜分片归属**: 覆盖率只由 header 自身的区段构成, 于是大视频
    会如实显示为"不完整"。宁可让用户看到缺分片, 也不要产出一个缝了另一部视频
    分片的垃圾文件。
    """
    alias = bidx if bidx is not None else binlog_index
    if alias:
        _reclassify_with_binlog(result, alias)
    for f in result.files:
        if f.is_large_video:
            _apply_coverage(f, alias)
    _recount(result)


def _build_large_video_header_index():
    """构建 doc_key → header 文件名的映射 (用于分片快速找父 header)

    **必须按 doc_key (key_high, key_low>>16) 归属, 不能只按 key_high**:
    不同视频会共享同一个 key_high (2026-09-15 取证实测, 同一 key_high 下混有
    7 个不同视频)。只按 key_high 会把 B 视频的分片归属到 A 视频名下 ——
    分片合并展示后这会直接导致 A 吞掉 B 的分片、B 永远显示缺分片。
    """
    global large_video_header_index
    large_video_header_index = {}
    if not binlog_index or not scan_result:
        return
    for f in scan_result.files:
        if f.is_large_video:
            r = binlog_index.get(f.file_name)
            if r and r.slice_index == 0:
                large_video_header_index[r.doc_key] = f.file_name


def _is_valid_mp4_export(path: str, file_id: str = '') -> bool:
    """
    导出的 MP4 是否有效 —— 交给 src.mp4 做真正的 box 级校验。

    旧实现的问题:
      - 用 `b'moov' in data[:65536]` 判断 moov 是否存在: 子串搜索在媒体负载上会
        误命中, 而且只看前 64KB, moov 在文件末尾时会误判为无效
      - 为了跑 stco 检查把**整个文件读进内存**
      - 缺 moov 的文件会被 stco_exceeds_file 判为"没问题"
    现在改为一次 box 遍历 (只读 box 头部与样本表, 不读整文件), 并额外要求
    sidecar 校验通过 —— 这样"旧版错误逻辑生成的导出"与"分片集合变化前的旧导出"
    都会自动失效。

    Args:
        path: 导出文件路径
        file_id: 可选; 传入时会一并校验覆盖率指纹是否仍然匹配
    """
    if not os.path.exists(path):
        return False
    try:
        # 只有 ISOBMFF (MP4/MOV) 才有 box 可校验; webm/mkv 用 MP4 规则必然假失败
        if mp4.looks_like_iso_bmff(path) and not mp4_validate(path).ok:
            return False
    except Exception:
        return False
    # 有 file_id 时要求 sidecar 存在且仍然有效 (覆盖率指纹 / 尺寸 / mtime)
    if file_id:
        return is_current(path, _current_signature(file_id))
    return True


# ==================== 入口 ====================

def main():
    import argparse
    parser = argparse.ArgumentParser(description='Telegram Desktop 缓存数据管理器')
    parser.add_argument('--tdata', default=Config.tdata_path,
                        help='tdata 目录路径')
    parser.add_argument('--port', type=int, default=5000,
                        help='服务端口')
    parser.add_argument('--host', default='127.0.0.1',
                        help='绑定地址')
    parser.add_argument('--no-browser', action='store_true',
                        help='不自动打开浏览器')
    args = parser.parse_args()

    # 验证 tdata 路径
    key_file = os.path.join(args.tdata, 'key_datas')
    if not os.path.exists(key_file):
        print(f"错误: key_datas 不存在: {key_file}")
        print(f"请确认 Telegram Desktop 数据目录路径是否正确")
        print(f"可在设置页面中修改 tdata 路径")
        if not getattr(sys, 'frozen', False):
            sys.exit(1)
        # 打包后不退出, 用默认路径让用户在 UI 中修改
        print(f"使用默认路径启动, 请在网页设置中修改...")
        args.tdata = Config.tdata_path

    Config.tdata_path = args.tdata

    # 启动时清理崩溃遗留的中间产物 (.part / .part.src) 与孤儿 sidecar
    try:
        Config.ensure_dirs()
        stale = cleanup_stale(Config.export_dir)
        if any(stale.values()):
            print(f"  已清理遗留中间文件: {stale}")
    except Exception:
        pass

    url = f"http://{args.host}:{args.port}"
    print(f"Telegram Desktop 缓存数据管理器")
    print(f"  tdata 路径: {Config.tdata_path}")
    print(f"  导出目录: {Config.export_dir}")
    print(f"  缩略图目录: {Config.thumbnail_dir}")
    print(f"  服务地址: {url}")
    print()

    # 启动服务器 (后台线程)
    from werkzeug.serving import make_server
    server = make_server(args.host, args.port, app, threaded=True)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    print(f"  服务已启动: {url}")

    # 尝试用 pywebview 打开桌面窗口, 失败则回退浏览器
    use_webview = False
    try:
        import webview
        use_webview = True
    except ImportError:
        pass

    if use_webview:
        print("  正在打开桌面窗口...")
        def _open_webview():
            time.sleep(0.5)
            webview.create_window(
                'Telegram 缓存管理器',
                url,
                width=1200,
                height=800,
                min_size=(800, 600),
            )
            webview.start()
            # 窗口关闭后退出程序
            server.shutdown()

        _open_webview()
    else:
        # 回退: 打开浏览器
        print("  (未安装 pywebview, 使用浏览器打开)")
        import webbrowser
        def _open_browser():
            time.sleep(1.0)
            webbrowser.open(url)
        threading.Thread(target=_open_browser, daemon=True).start()
        # 保持运行
        try:
            server_thread.join()
        except KeyboardInterrupt:
            server.shutdown()


if __name__ == '__main__':
    main()
