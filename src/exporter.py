"""
文件导出模块

负责将解密后的缓存文件导出为标准格式:
- 图片 (JPEG/WebP/PNG/GIF/BMP/partial_JPEG) → PNG
- 视频 (MP4/WebM/序列化视频) → MP4 (用 ffmpeg remux 将 moov 移前)
- 音频 → 原格式
- 动画贴片 (TGS) → 原格式 (.tgs)
- 未知类型 → .bin

同时负责缩略图生成 (图片缩略图 + 视频首帧缩略图)
"""

import os
import sys
import io
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from .scanner import FileType, is_image, is_video, is_audio, is_sticker
from .deserializer import deserialize_video


def has_zero_holes(data: bytes) -> bool:
    """
    [仅作提示] 检查视频数据中是否有零填充空洞 (大于 8KB 的连续零区域)。

    已不再用于任何决策 —— 判据过于粗糙: 跳过前 64KB、只认 4KB 对齐的纯零块,
    小洞/非对齐洞完全看不见; 而"有洞"本身也不能靠看字节是不是 0 来判断。

    洞的权威判定请用 Coverage: `coverage.missing_of(mp4.required_ranges(moov))`。
    保留此函数仅为兼容可能的外部调用。
    """
    if not data or len(data) < 8192:
        return False

    chunk = 4096
    consecutive_zero = 0
    skip_start = min(65536, len(data))
    for i in range(skip_start, len(data), chunk):
        block = data[i:i + chunk]
        if len(block) < chunk:
            break
        if block == b'\x00' * chunk:
            consecutive_zero += 1
            if consecutive_zero >= 2:  # 连续 2 个 4KB 全零块 = 8KB 空洞
                return True
        else:
            consecutive_zero = 0
    return False


def stco_exceeds_file(data) -> bool:
    """
    检查 MP4 的 chunk offset 是否超出文件范围, 或文件本身不可信。

    **行为变更 (重要)**: 旧实现在找不到 moov 时返回 False, 即把最坏的情况
    (文件根本没有索引、完全无法解复用) 当成了"没问题"。现在改为:

        缺少 moov / moov 解析失败 / fragmented / 结构非法  ->  返回 True (不可信)

    也就是说"返回 False"现在意味着"已确认每个 chunk 都落在文件内"。

    实现委托给 src.mp4.stco_exceeds_size —— 不再用 bytes.find(b'moov') 定位
    moov (媒体负载里出现 ASCII "moov" 是常态, 会从随机位置开始解析)。

    Args:
        data: 文件字节, 或文件路径
    """
    from .mp4 import stco_exceeds_size
    return stco_exceeds_size(data)


def stream_copy(src, dst_fd, lo: int, hi: int, buf_size: int = 1 << 20) -> int:
    """
    把文件 (或字节) 的 [lo, hi) 拷贝到已打开的二进制写句柄。

    用于避免"把整部视频读进内存"—— 逐块复制, 峰值内存 = buf_size。
    Returns: 实际写出的字节数
    """
    written = 0
    if hi <= lo:
        return 0
    if isinstance(src, (bytes, bytearray, memoryview)):
        mv = memoryview(src)
        for off in range(lo, hi, buf_size):
            chunk = mv[off:min(off + buf_size, hi)]
            dst_fd.write(chunk)
            written += len(chunk)
        return written
    with open(src, 'rb') as f:
        f.seek(lo)
        remaining = hi - lo
        while remaining > 0:
            chunk = f.read(min(buf_size, remaining))
            if not chunk:
                break
            dst_fd.write(chunk)
            written += len(chunk)
            remaining -= len(chunk)
    return written


# ffmpeg 路径 (优先使用程序目录下 env/ffmpeg.exe)
def _get_ffmpeg_path() -> Optional[str]:
    """获取 ffmpeg 可执行文件路径"""
    # 确定 exe 所在目录
    if getattr(sys, 'frozen', False):
        exe_dir = os.path.dirname(sys.executable)
    else:
        exe_dir = os.path.dirname(os.path.abspath(__file__))
        # 开发模式下 src/ 的上级目录
        parent = os.path.dirname(exe_dir)
        if os.path.exists(os.path.join(parent, 'dist_windows')):
            exe_dir = os.path.join(parent, 'dist_windows')

    # 1. 优先: env/ffmpeg.exe (用户下载的最新版)
    env_ffmpeg = os.path.join(exe_dir, 'env', 'ffmpeg.exe')
    if os.path.exists(env_ffmpeg):
        return env_ffmpeg

    # 2. 打包后同目录 ffmpeg.exe
    bundled = os.path.join(exe_dir, 'ffmpeg.exe')
    if os.path.exists(bundled):
        return bundled

    # 3. 开发环境已知路径
    dev_path = r'E:\Shell\FFmpeg\ffmpeg-master-latest-win64-gpl-shared\bin\ffmpeg.exe'
    if os.path.exists(dev_path):
        return dev_path

    # 4. 尝试 PATH
    import shutil
    found = shutil.which('ffmpeg')
    if found:
        return found
    return None


def _get_ffprobe_path() -> Optional[str]:
    """获取 ffprobe 可执行文件路径 (与 ffmpeg 同目录)"""
    ffmpeg = _get_ffmpeg_path()
    if not ffmpeg:
        return None
    # ffprobe 与 ffmpeg 同目录
    ffprobe = os.path.join(os.path.dirname(ffmpeg), 'ffprobe.exe')
    if os.path.exists(ffprobe):
        return ffprobe
    # 回退: PATH
    import shutil
    found = shutil.which('ffprobe')
    return found


def get_video_metadata(video_path: str, timeout: int = 15) -> Optional[dict]:
    """
    用 ffprobe 获取视频的时长/分辨率/码率等元数据

    Returns:
        dict with keys: duration, width, height, bit_rate, format_name
        ffprobe 不可用或失败时返回 None
    """
    ffprobe = _get_ffprobe_path()
    if not ffprobe:
        return None

    si, cf = _subprocess_flags()
    try:
        result = subprocess.run(
            [ffprobe, '-v', 'quiet', '-print_format', 'json',
             '-show_streams', '-show_format', '-i', video_path],
            capture_output=True, timeout=timeout,
            startupinfo=si, creationflags=cf,
        )
        if result.returncode != 0:
            return None
        import json as _json
        info = _json.loads(result.stdout.decode('utf-8', errors='replace'))
        meta = {'duration': 0.0, 'width': 0, 'height': 0, 'bit_rate': 0, 'format_name': ''}
        # format 层时长/码率
        fmt = info.get('format', {}) or {}
        meta['duration'] = float(fmt.get('duration', 0) or 0)
        meta['bit_rate'] = int(float(fmt.get('bit_rate', 0) or 0))
        meta['format_name'] = fmt.get('format_long_name', '') or fmt.get('format_name', '')
        # stream 层分辨率
        for st in info.get('streams', []) or []:
            if st.get('codec_type') == 'video' and st.get('width'):
                meta['width'] = int(st['width'])
                meta['height'] = int(st['height'])
                if not meta.get('duration'):
                    meta['duration'] = float(st.get('duration', 0) or 0)
                if not meta.get('bit_rate'):
                    meta['bit_rate'] = int(float(st.get('bit_rate', 0) or 0))
                break
        return meta
    except Exception:
        return None


def _subprocess_flags():
    """获取 Windows 下隐藏命令行窗口的参数"""
    si = None
    cf = 0
    if sys.platform == 'win32':
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        cf = subprocess.CREATE_NO_WINDOW
    return si, cf


# ==================== 导出格式映射 ====================

EXPORT_EXTENSIONS = {
    FileType.JPEG: "png",
    FileType.PNG: "png",
    FileType.WEBP: "png",
    FileType.GIF: "gif",
    FileType.BMP: "png",
    FileType.PARTIAL_JPEG: "png",
    FileType.MP4: "mp4",
    FileType.MOV: "mov",
    FileType.WEBM: "webm",
    FileType.SERIALIZED_VIDEO: "mp4",
    FileType.VIDEO_SLICE: "bin",
    FileType.MP3: "mp3",
    FileType.OGG: "ogg",
    FileType.TGS: "tgs",
    FileType.UNKNOWN_FRAGMENT: "bin",
    FileType.UNKNOWN: "bin",
}


def get_export_extension(ftype: FileType) -> str:
    """获取导出文件扩展名"""
    return EXPORT_EXTENSIONS.get(ftype, "bin")


def get_export_filename(file_id: str, ftype: FileType, index: int = 0) -> str:
    """生成导出文件名"""
    ext = get_export_extension(ftype)
    if index > 0:
        return f"{file_id}_{index}.{ext}"
    return f"{file_id}.{ext}"

# ==================== 图片导出 ====================

def export_image(data: bytes, ftype: FileType, out_path: str, to_png: bool = True) -> bool:
    """
    导出图片文件

    Args:
        data: 解密后的原始图片数据
        ftype: 文件类型
        out_path: 输出路径
        to_png: 是否转换为 PNG (GIF 保持原格式)
    Returns:
        成功返回 True
    """
    # GIF 保持原格式
    if ftype == FileType.GIF:
        with open(out_path, 'wb') as f:
            f.write(data)
        return True

    # partial JPEG: 去掉 "partial:" 前缀
    if ftype == FileType.PARTIAL_JPEG:
        if data[:8] == b'partial:':
            data = data[8:]

    if not to_png:
        # 保留原始格式
        with open(out_path, 'wb') as f:
            f.write(data)
        return True

    # 转 PNG
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(data))
        img.save(out_path, 'PNG')
        return True
    except Exception:
        # Pillow 转换失败, 保留原始数据
        with open(out_path, 'wb') as f:
            f.write(data)
        return True


# ==================== 视频导出 ====================

def _update_stco_co64(moov_data: bytes, delta: int) -> bytes:
    """
    更新 moov atom 内所有 stco/co64 box 的 chunk offset.
    
    当 moov 被移动到文件前面时, mdat 的位置会改变, 
    stco/co64 中的 chunk offset 需要加上 delta (moov 新位置 - moov 原位置).
    """
    import struct as _s

    def _patch_stco(data, start, end, delta):
        """在 [start, end) 范围内查找并修正 stco/co64 box"""
        pos = start
        patches = []
        while pos + 8 <= end:
            box_size = _s.unpack('>I', data[pos:pos + 4])[0]
            box_type = data[pos + 4:pos + 8]
            if box_size == 0:
                break
            if box_size == 1:  # 64-bit size
                if pos + 16 > end:
                    break
                box_size = _s.unpack('>Q', data[pos + 8:pos + 16])[0]
            if box_size < 8 or pos + box_size > end:
                break
            
            if box_type == b'stco':
                # stco: version(1B) + flags(3B) + entry_count(4B) + [chunk_offset(4B BE)] * count
                box_data = data[pos + 8:pos + box_size]
                if len(box_data) >= 8:
                    entry_count = _s.unpack('>I', box_data[4:8])[0]
                    for i in range(entry_count):
                        off_pos = 8 + i * 4
                        if off_pos + 4 <= len(box_data):
                            old_val = _s.unpack('>I', box_data[off_pos:off_pos + 4])[0]
                            new_val = old_val + delta
                            if 0 < new_val < 0xFFFFFFFF:
                                patches.append((pos + 8 + off_pos, _s.pack('>I', new_val)))
            elif box_type == b'co64':
                # co64: 64-bit chunk offsets
                box_data = data[pos + 8:pos + box_size]
                if len(box_data) >= 8:
                    entry_count = _s.unpack('>I', box_data[4:8])[0]
                    for i in range(entry_count):
                        off_pos = 8 + i * 8
                        if off_pos + 8 <= len(box_data):
                            old_val = _s.unpack('>Q', box_data[off_pos:off_pos + 8])[0]
                            new_val = old_val + delta
                            patches.append((pos + 8 + off_pos, _s.pack('>Q', new_val)))
            
            # 递归进入容器 box
            if box_type in (b'moov', b'trak', b'mdia', b'minf', b'stbl', b'udta', b'edts'):
                patches.extend(_patch_stco(data, pos + 8, pos + box_size, delta))
            
            pos += box_size
        return patches
    
    if delta == 0:
        return moov_data
    
    data = bytearray(moov_data)
    patches = _patch_stco(data, 0, len(data), delta)
    for offset, new_bytes in patches:
        data[offset:offset + len(new_bytes)] = new_bytes
    
    return bytes(data)


def _move_moov_front(data: bytes) -> bytes:
    """
    手动将 MP4 的 moov atom 移到文件前面 (不重编码, 仅原子级搬运).
    同时更新 stco/co64 中的 chunk offset 以反映新的 mdat 位置.
    用于 ffmpeg 不可用或 remux 失败时的回退方案.

    MP4 结构: [ftyp] [free/mdat] [moov] → 目标: [ftyp] [moov] [free/mdat]
    """
    if not data or len(data) < 16:
        return data

    # 查找 ftyp atom
    ftyp_end = 0
    if data[4:8] == b'ftyp':
        ftyp_size = int.from_bytes(data[0:4], 'big')
        if ftyp_size > 0 and ftyp_size < len(data):
            ftyp_end = ftyp_size
    if ftyp_end == 0:
        return data  # 无 ftyp, 无法处理

    # 查找 moov atom (可能在文件末尾)
    moov_offset = -1
    moov_size = 0
    offset = 0
    while offset + 8 <= len(data):
        atom_size = int.from_bytes(data[offset:offset + 4], 'big')
        atom_type = data[offset + 4:offset + 8]
        if atom_size == 0:
            break
        if atom_size < 8 or offset + atom_size > len(data):
            # 可能是 mdat 中的数据, 跳到末尾
            break
        if atom_type == b'moov':
            moov_offset = offset
            moov_size = atom_size
            break
        offset += atom_size

    if moov_offset < 0 or moov_offset <= ftyp_end:
        return data  # 无 moov 或 moov 已在前面

    # 提取各部分
    ftyp_data = data[:ftyp_end]
    moov_data = data[moov_offset:moov_offset + moov_size]
    # 中间部分 (ftyp 和 moov 之间的其他 atom, 如 mdat/free)
    middle_data = data[ftyp_end:moov_offset]
    # moov 之后的数据 (通常无)
    after_data = data[moov_offset + moov_size:]

    # 计算 delta: moov 从 moov_offset 移到 ftyp_end, 
    # mdat 等中间数据从 ftyp_end 移到 ftyp_end + moov_size
    # 所以 chunk offset 需要加上 (moov_size) 
    # (因为 mdat 从 ftyp_end 变到 ftyp_end + moov_size, 偏移增加了 moov_size)
    delta = moov_size
    
    # 更新 stco/co64 偏移
    moov_data = _update_stco_co64(moov_data, delta)
    
    # 重组: ftyp + moov + middle + after
    return ftyp_data + moov_data + middle_data + after_data


@dataclass
class RemuxResult:
    """转封装结果 —— 调用方第一次能知道到底成没成"""
    ok: bool = False
    mode: str = ''            # 'copy' | 'reencode' | 'moov_move' | 'stream' | 'raw' | ''
    size: int = 0
    duration_s: float = 0.0
    reasons: List[str] = field(default_factory=list)

    def __bool__(self) -> bool:          # 兼容旧的 `if _remux_video(...)` 写法
        return self.ok


def _accept_output(out_path: str, want_duration: float) -> Tuple[bool, str, float]:
    """
    校验一次转封装的产物是否真的可用。

    旧实现只看 `out_size > len(data) * 0.3`, 而 `-c copy` 会把零洞原样复制 ——
    大小完全正常, 内容却是坏的。这里改成真正的结构 + 时长校验。
    """
    from .mp4 import validate as mp4_validate, looks_like_iso_bmff

    if not os.path.exists(out_path):
        return False, '未产生输出文件', 0.0
    size = os.path.getsize(out_path)
    if size <= 0:
        return False, '输出文件为空', 0.0

    # WebM/Matroska 不是 box 结构, 拿 MP4 规则套必然失败 —— 交给 ffprobe
    if not looks_like_iso_bmff(out_path):
        from .export_pipeline import probe
        meta = probe(out_path)
        if meta is None:
            return False, '输出既不是 MP4 也无法被 ffprobe 解析', 0.0
        if meta.get('nb_streams', 0) <= 0:
            return False, '输出中没有任何流', 0.0
        return True, 'ok', float(meta.get('duration') or 0.0)

    info = mp4_validate(out_path)
    if not info.ok:
        # 结构不可信: 允许 ffprobe 复核一次 (某些容器并非 MP4)
        return False, '输出结构校验失败: ' + '; '.join(info.reasons[:2]), info.duration_s

    if not info.has_mdat or info.track_count <= 0:
        return False, '输出没有可用的流或媒体数据', info.duration_s

    if want_duration > 1.0:
        tol = max(1.0, 0.02 * want_duration)
        if abs(info.duration_s - want_duration) > tol:
            return False, (f'输出时长 {info.duration_s:.2f}s 与输入的 '
                           f'{want_duration:.2f}s 相差超过 {tol:.2f}s'), info.duration_s
    return True, '', info.duration_s


def _remux_file(src_path: str,
                out_path: str,
                force_reencode: bool = False,
                want_duration: Optional[float] = None) -> RemuxResult:
    """
    把已经落在磁盘上的视频转封装为"moov 前置、可渐进播放"的单文件 MP4。

    三级方案, 每级都要通过真正的结构/时长校验才算成功:
      1. `-c copy -movflags +faststart`  (不重编码, 快)
      2. `libx264 + aac` 重编码           (容器需要重建时; 需 ffmpeg 带 libx264)
      3. 手动搬运 moov + 修正 stco/co64   (不依赖 ffmpeg)

    与旧实现的关键差别:
      - 不再有"直接写入原始数据"这一层静默降级; 全部失败就返回 ok=False
      - 重编码前先探测 ffmpeg 是否真的带 libx264, 缺失时明确跳过并给出原因
      - tier 1 的接受条件从"大小比例"改为"结构 + 时长校验"

    Args:
        want_duration: 输入应有的时长 (秒); 用于校验输出时长。0 表示未知。
    """
    res = RemuxResult()
    if not os.path.exists(src_path):
        res.reasons.append('输入文件不存在')
        return res

    src_size = os.path.getsize(src_path)
    if src_size <= 0:
        res.reasons.append('输入文件为空')
        return res

    if want_duration is None:
        from .mp4 import validate as mp4_validate
        want_duration = mp4_validate(src_path).duration_s or 0.0

    head = b''
    with open(src_path, 'rb') as f:
        head = f.read(4)
    is_webm = head == b'\x1a\x45\xdf\xa3'
    out_dir = os.path.dirname(out_path) or '.'
    os.makedirs(out_dir, exist_ok=True)

    ffmpeg = _get_ffmpeg_path()
    if not ffmpeg:
        # 无 ffmpeg: MP4 走方案 3 (手动搬运 moov), WebM 直接流式复制
        if is_webm:
            with open(out_path, 'wb') as fo:
                stream_copy(src_path, fo, 0, src_size)
            res.ok, res.size, res.mode = True, os.path.getsize(out_path), 'stream'
            res.reasons.append('未找到 ffmpeg, 已原样输出 (WebM 无需 faststart)')
            return res
        moved_path = out_path + '.moved'
        try:
            with open(src_path, 'rb') as f:
                data = f.read()
            moved = _move_moov_front(data)
            with open(moved_path, 'wb') as f:
                f.write(moved)
        except Exception as exc:
            res.reasons.append(f'手动搬运 moov 失败: {exc}')
            return res
        ok, why, dur = _accept_output(moved_path, want_duration)
        if ok:
            os.replace(moved_path, out_path)
            res.ok, res.size, res.mode, res.duration_s = True, os.path.getsize(out_path), 'moov_move', dur
        else:
            try:
                os.remove(moved_path)
            except OSError:
                pass
            res.reasons.append(f'未找到 ffmpeg, 且手动搬运 moov 后仍不可用: {why}')
        return res

    si, cf = _subprocess_flags()

    # ---- 方案 1: -c copy ----
    if not force_reencode:
        cmd1 = [ffmpeg, '-v', 'error', '-nostdin', '-fflags', '+discardcorrupt',
                '-i', src_path, '-c', 'copy']
        if not is_webm:
            cmd1 += ['-movflags', '+faststart']
        cmd1 += ['-y', out_path]
        try:
            subprocess.run(cmd1, capture_output=True, timeout=1800,
                           startupinfo=si, creationflags=cf)
        except subprocess.TimeoutExpired:
            res.reasons.append('方案1 (-c copy) 超时')
        else:
            ok, why, dur = _accept_output(out_path, want_duration)
            if ok:
                res.ok, res.size, res.mode, res.duration_s = True, os.path.getsize(out_path), 'copy', dur
                return res
            res.reasons.append(f'方案1 (-c copy) 产物不合格: {why}')
            try:
                os.remove(out_path)
            except OSError:
                pass
    else:
        res.reasons.append('方案1 被跳过 (要求强制重编码)')

    # ---- 方案 2: 重编码 ----
    from .export_pipeline import can_reencode, ffmpeg_capabilities
    if not can_reencode():
        caps = ffmpeg_capabilities()
        res.reasons.append(
            '方案2 (重编码) 被跳过: 当前 ffmpeg 不具备 libx264'
            + (f' (版本: {caps.get("version", "?")})' if caps.get('available') else ' (未找到 ffmpeg)'))
    else:
        cmd2 = [ffmpeg, '-v', 'error', '-nostdin', '-fflags', '+discardcorrupt',
                '-i', src_path,
                '-c:v', 'libx264', '-preset', 'fast', '-crf', '23',
                '-c:a', 'aac', '-b:a', '128k',
                '-max_interleave_delta', '0']
        if not is_webm:
            cmd2 += ['-movflags', '+faststart']
        cmd2 += ['-y', out_path]
        try:
            subprocess.run(cmd2, capture_output=True, timeout=3600,
                           startupinfo=si, creationflags=cf)
        except subprocess.TimeoutExpired:
            res.reasons.append('方案2 (重编码) 超时')
        else:
            ok, why, dur = _accept_output(out_path, want_duration)
            if ok:
                res.ok, res.size, res.mode, res.duration_s = True, os.path.getsize(out_path), 'reencode', dur
                return res
            res.reasons.append(f'方案2 (重编码) 产物不合格: {why}')
            try:
                os.remove(out_path)
            except OSError:
                pass

    # ---- 方案 3: 手动搬运 moov ----
    if is_webm:
        # 注意: stream_copy 的第一个参数是**源路径**(或 bytes), 第二个才是已打开的写句柄。
        # 早期版本误把打开的读句柄 fi 传进去, 导致 TypeError 让整个导出 500。
        with open(out_path, 'wb') as fo:
            stream_copy(src_path, fo, 0, src_size)
        res.ok, res.size, res.mode = True, os.path.getsize(out_path), 'stream'
        res.reasons.append('WebM 无 moov, 已原样输出')
        return res

    moved_path = out_path + '.moved'
    try:
        with open(src_path, 'rb') as f:
            data = f.read()
        moved = _move_moov_front(data)
        with open(moved_path, 'wb') as f:
            f.write(moved)
    except Exception as exc:
        res.reasons.append(f'方案3 (手动搬运 moov) 失败: {exc}')
        return res

    ok, why, dur = _accept_output(moved_path, want_duration)
    if ok:
        os.replace(moved_path, out_path)
        res.ok, res.size, res.mode, res.duration_s = True, os.path.getsize(out_path), 'moov_move', dur
        return res
    res.reasons.append(f'方案3 (手动搬运 moov) 产物不合格: {why}')
    try:
        os.remove(moved_path)
    except OSError:
        pass

    # 全部失败 —— 明确失败, 绝不静默写出原始数据
    res.ok = False
    res.reasons.append('三种转封装方案均未产出可用文件')
    return res


def _remux_video(data: bytes, out_path: str, force_reencode: bool = False) -> RemuxResult:
    """
    转封装内存中的视频数据。

    仅为兼容仍持有整片字节的旧调用点而保留 (内部会先落到临时文件再交给
    _remux_file)。新代码应当直接使用文件路径, 避免把整部视频读进内存。
    """
    out_dir = os.path.dirname(out_path) or '.'
    os.makedirs(out_dir, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(suffix='.mp4', dir=out_dir)
    try:
        with os.fdopen(tmp_fd, 'wb') as f:
            f.write(data)
        return _remux_file(tmp_path, out_path, force_reencode=force_reencode)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def export_video(data: bytes, ftype: FileType, out_path: str) -> bool:
    """
    导出视频文件 (内存数据版本)。

    仅为兼容旧调用点保留; 新代码应当走 src.export_pipeline.finalize 的文件路径,
    以获得"校验后才提交"的保证。

    注意: 这里不再需要调用方预先判断 force_reencode —— _remux_file 会先尝试
    `-c copy` 并**校验产物**, 不合格才回退到重编码, 自我纠正。
    """
    if ftype == FileType.SERIALIZED_VIDEO:
        if not (len(data) > 12 and data[4:8] == b'ftyp'):
            mp4_data = deserialize_video(data)
            if mp4_data is None:
                return False
            data = mp4_data

    return bool(_remux_video(data, out_path))


# ==================== 通用导出 ====================

def export_file(data: bytes, ftype: FileType, out_path: str) -> bool:
    """
    通用导出函数, 根据文件类型选择合适的导出方式

    Args:
        data: 解密后的原始数据
        ftype: 文件类型
    Returns:
        成功返回 True
    """
    if is_image(ftype):
        return export_image(data, ftype, out_path, to_png=True)
    elif is_video(ftype):
        return export_video(data, ftype, out_path)
    else:
        # 音频/贴片/未知: 直接写入原始数据
        with open(out_path, 'wb') as f:
            f.write(data)
        return True


# ==================== 缩略图生成 ====================

def generate_thumbnail(data: bytes, ftype: FileType, thumb_path: str,
                       max_size: Tuple[int, int] = (200, 200)) -> Optional[str]:
    """
    生成缩略图

    Args:
        data: 解密后的原始数据
        ftype: 文件类型
        thumb_path: 缩略图输出路径
        max_size: 最大尺寸 (width, height)
    Returns:
        成功返回缩略图路径, 失败返回 None
    """
    try:
        if is_image(ftype):
            return _generate_image_thumbnail(data, ftype, thumb_path, max_size)
        elif is_video(ftype):
            return _generate_video_thumbnail(data, ftype, thumb_path, max_size)
        elif ftype == FileType.TGS:
            return _generate_tgs_thumbnail(data, thumb_path, max_size)
    except Exception:
        return None
    return None


def _generate_tgs_thumbnail(data: bytes, thumb_path: str,
                             max_size: Tuple[int, int]) -> Optional[str]:
    """
    生成 TGS 动画贴片的缩略图
    TGS 是 gzip 压缩的 Lottie JSON, 无法直接生成位图缩略图
    生成一个带贴纸图标的占位缩略图
    """
    from .scanner import decompress_tgs
    import json

    lottie_json = decompress_tgs(data)
    if not lottie_json:
        return None

    try:
        lottie = json.loads(lottie_json)
        name = lottie.get('nm', 'TGS')
        w = lottie.get('w', 512)
        h = lottie.get('h', 512)

        from PIL import Image, ImageDraw, ImageFont
        # 生成带贴纸名称的占位图
        img = Image.new('RGB', max_size, (240, 240, 248))
        draw = ImageDraw.Draw(img)

        # 绘制贴纸图标 (简化版)
        cx, cy = max_size[0] // 2, max_size[1] // 2 - 10
        r = min(max_size) // 4
        draw.ellipse([cx-r, cy-r, cx+r, cy+r], fill=(100, 120, 255))

        # 绘制名称文字
        try:
            font = ImageFont.truetype("arial.ttf", 12)
        except Exception:
            font = ImageFont.load_default()
        text = name[:12]
        try:
            bbox = draw.textbbox((0, 0), text, font=font)
            tw = bbox[2] - bbox[0]
        except Exception:
            tw = len(text) * 7
        draw.text((cx - tw // 2, cy + r + 5), text, fill=(80, 80, 100), font=font)

        img.save(thumb_path, 'JPEG', quality=85)
        return thumb_path
    except Exception:
        return None


def _generate_image_thumbnail(data: bytes, ftype: FileType, thumb_path: str,
                               max_size: Tuple[int, int]) -> Optional[str]:
    """生成图片缩略图"""
    from PIL import Image

    # partial JPEG: 去掉前缀
    if ftype == FileType.PARTIAL_JPEG:
        if data[:8] == b'partial:':
            data = data[8:]

    img = Image.open(io.BytesIO(data))
    img.thumbnail(max_size, Image.LANCZOS)

    # 转换模式以兼容 PNG
    if img.mode in ('RGBA', 'LA', 'P'):
        img = img.convert('RGBA')
    else:
        img = img.convert('RGB')

    img.save(thumb_path, 'JPEG', quality=85)
    return thumb_path


def _generate_video_thumbnail(data: bytes, ftype: FileType, thumb_path: str,
                               max_size: Tuple[int, int]) -> Optional[str]:
    """
    生成视频缩略图 (内存数据版本)

    只为小体积数据保留; 大视频请用 _generate_video_thumbnail_from_path,
    避免把整部视频读进内存。
    """
    if ftype == FileType.SERIALIZED_VIDEO:
        if not (len(data) > 12 and data[4:8] == b'ftyp'):
            mp4_data = deserialize_video(data)
            if mp4_data is None:
                return None
            data = mp4_data

    ext = get_export_extension(ftype)
    with tempfile.NamedTemporaryFile(suffix=f'.{ext}', delete=False) as tmp:
        tmp.write(data)
        tmp_path = tmp.name
    try:
        return _video_thumbnail_from_path(tmp_path, thumb_path, max_size)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def _video_thumbnail_from_path(src_path: str, thumb_path: str,
                                max_size: Tuple[int, int]) -> Optional[str]:
    """从磁盘上的视频文件提取首帧缩略图"""
    ffmpeg = _get_ffmpeg_path()
    if not ffmpeg or not os.path.exists(src_path):
        return None

    si, cf = _subprocess_flags()
    try:
        subprocess.run(
            [ffmpeg, '-v', 'error', '-nostdin', '-i', src_path, '-vframes', '1',
             '-vf', f'scale={max_size[0]}:{max_size[1]}:force_original_aspect_ratio=decrease',
             '-q:v', '2', '-y', thumb_path],
            capture_output=True, timeout=30,
            startupinfo=si, creationflags=cf,
        )
        if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
            return thumb_path
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    except Exception:
        pass
    return None


# ==================== 批量导出 ====================

def export_with_info(data: bytes, ftype: FileType, out_path: str) -> dict:
    """
    导出文件并返回详细信息

    与旧实现的关键差别: `success` 现在真的反映转封装结果。
    旧版在 `_remux_video` 的返回值被忽略的情况下直接把 success 置为 True,
    于是"转封装全失败"也会上报成功。

    Returns:
        dict with keys: success, out_path, size, width, height, reasons
    """
    result = {
        'success': False,
        'out_path': out_path,
        'size': 0,
        'width': 0,
        'height': 0,
        'reasons': [],
    }

    try:
        if is_image(ftype):
            img_data = data
            if ftype == FileType.PARTIAL_JPEG and data[:8] == b'partial:':
                img_data = data[8:]

            from PIL import Image
            img = Image.open(io.BytesIO(img_data))
            result['width'] = img.width
            result['height'] = img.height
            img.save(out_path, 'PNG')
            result['success'] = True
            result['size'] = os.path.getsize(out_path)

        elif is_video(ftype):
            if ftype == FileType.SERIALIZED_VIDEO:
                if not (len(data) > 12 and data[4:8] == b'ftyp'):
                    mp4_data = deserialize_video(data)
                    if mp4_data is None:
                        result['reasons'].append(
                            '序列化视频反序列化失败, 或体积过大 (请走流式重建路径)')
                        return result
                    data = mp4_data
            rr = _remux_video(data, out_path)
            result['success'] = rr.ok
            result['reasons'] = list(rr.reasons)
            if rr.ok:
                result['size'] = rr.size
        else:
            with open(out_path, 'wb') as f:
                f.write(data)
            result['success'] = True
            result['size'] = os.path.getsize(out_path)

    except Exception as exc:
        result['reasons'].append(f'{exc.__class__.__name__}: {exc}')

    return result
