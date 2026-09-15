"""
导出管线: 四道验证门 + 原子提交 + sidecar

旧管线的问题 (见 plan §2):
  - 导出直接写最终文件名; 残缺产物被登记为"已重建"并长期复用
  - _remux_video 所有分支都 return True; export_with_info 忽略返回值直接置 success
  - 唯一质检是 `out_size > len(data) * 0.3`, 而 `-c copy` 会把零洞原样复制
  - 没有任何"真的能解码吗"的检验

新管线保证:
  写 <id>.<ext>.part
    -> 第1道: box 结构校验 (src.mp4.validate, 含 chunk 越界)
    -> 第2道: ffprobe 时长与计算值一致
    -> 第3道: 有界解码冒烟 (开头 + 结尾各 20 秒)
    -> 写 sidecar <id>.<ext>.json
    -> os.replace(.part -> 最终名)
  四道全过才可能出现最终文件名。任何一步失败都返回结构化原因, 绝不谎报成功。

sidecar 让导出物可失效:
  - 版本号变化 (PIPELINE_VERSION) -> 旧产物视为过期
  - coverage_signature 变化 (分片集合变了) -> 旧产物视为过期
  - 尺寸/mtime 与记录不符 -> 视为过期
因此用户后来补全了分片, 旧的残缺导出不会再被端上桌。

仅依赖标准库 + 本项目模块。
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import mp4
from .mp4 import validate as mp4_validate

__all__ = [
    'PIPELINE_VERSION', 'PART_SUFFIX', 'SIDECAR_SUFFIX', 'MEDIA_EXTS',
    'Sidecar', 'ExportVerdict', 'ExportOutcome', 'produce',
    'probe', 'smoke_test', 'validate_export', 'finalize', 'is_current',
    'read_sidecar', 'write_sidecar', 'sidecar_path', 'part_path', 'staging_path',
    'is_media_filename', 'cleanup_stale', 'check_free_space',
    'ffmpeg_available', 'ffmpeg_capabilities', 'reset_capability_cache',
    'can_reencode',
]

PIPELINE_VERSION = 2
PART_SUFFIX = '.part'
SIDECAR_SUFFIX = '.json'

# 允许出现在导出目录里的媒体扩展名 (白名单 —— 必须能过滤掉 .part/.json/.tmp)
MEDIA_EXTS = frozenset({
    '.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp',
    '.mp4', '.mov', '.webm', '.mkv',
    '.mp3', '.ogg', '.m4a', '.opus',
    '.tgs', '.bin',
})

SMOKE_WINDOW = 20.0          # 解码冒烟每段时长 (秒)
DEFAULT_STALE_AGE = 6 * 3600

_caps_cache: Optional[Dict[str, Any]] = None


# ==================== ffmpeg 定位与能力探测 ====================

def _ffmpeg_path() -> Optional[str]:
    from .exporter import _get_ffmpeg_path
    return _get_ffmpeg_path()


def _ffprobe_path() -> Optional[str]:
    from .exporter import _get_ffprobe_path
    return _get_ffprobe_path()


def _subprocess_flags():
    from .exporter import _subprocess_flags
    return _subprocess_flags()


def ffmpeg_available() -> bool:
    return _ffmpeg_path() is not None


def ffmpeg_capabilities(refresh: bool = False) -> Dict[str, Any]:
    """
    探测 ffmpeg 能力。

    用于取代"硬编码 libx264 然后静默降级"的做法: 如果构建里没有 libx264,
    重编码路径应当被明确跳过并给出原因, 而不是悄悄写出一个坏文件。
    """
    global _caps_cache
    if _caps_cache is not None and not refresh:
        return _caps_cache

    caps: Dict[str, Any] = {'available': False, 'has_libx264': False, 'has_aac': False,
                            'version': '', 'path': ''}
    ffmpeg = _ffmpeg_path()
    if not ffmpeg:
        _caps_cache = caps
        return caps

    caps['available'] = True
    caps['path'] = ffmpeg
    si, cf = _subprocess_flags()
    try:
        r = subprocess.run([ffmpeg, '-hide_banner', '-version'],
                           capture_output=True, timeout=20,
                           startupinfo=si, creationflags=cf)
        first = r.stdout.decode('utf-8', 'replace').splitlines()[:1]
        caps['version'] = first[0] if first else ''
        e = subprocess.run([ffmpeg, '-hide_banner', '-encoders'],
                           capture_output=True, timeout=30,
                           startupinfo=si, creationflags=cf)
        text = e.stdout.decode('utf-8', 'replace')
        caps['has_libx264'] = 'libx264' in text
        caps['has_aac'] = ' aac ' in text or '\taac' in text or ' aac\n' in text
    except Exception as exc:
        caps['error'] = str(exc)

    _caps_cache = caps
    return caps


def reset_capability_cache() -> None:
    global _caps_cache
    _caps_cache = None


def can_reencode() -> bool:
    caps = ffmpeg_capabilities()
    return bool(caps.get('available') and caps.get('has_libx264'))


# ==================== 探测与冒烟 ====================

def probe(path: str, timeout: int = 60) -> Optional[Dict[str, Any]]:
    """ffprobe 读取容器/流信息; 不可用或失败返回 None"""
    ffprobe = _ffprobe_path()
    if not ffprobe or not os.path.exists(path):
        return None
    si, cf = _subprocess_flags()
    try:
        r = subprocess.run(
            [ffprobe, '-v', 'quiet', '-print_format', 'json',
             '-show_format', '-show_streams', path],
            capture_output=True, timeout=timeout, startupinfo=si, creationflags=cf)
        if r.returncode != 0:
            return None
        info = json.loads(r.stdout.decode('utf-8', 'replace'))
        fmt = info.get('format') or {}
        out: Dict[str, Any] = {
            'duration': float(fmt.get('duration') or 0),
            'size': int(fmt.get('size') or 0),
            'format_name': fmt.get('format_long_name') or fmt.get('format_name') or '',
            'nb_streams': int(fmt.get('nb_streams') or 0),
            'streams': [],
        }
        for st in info.get('streams') or []:
            out['streams'].append({
                'codec_type': st.get('codec_type') or '',
                'codec_name': st.get('codec_name') or '',
                'width': int(st.get('width') or 0),
                'height': int(st.get('height') or 0),
                'duration': float(st.get('duration') or 0),
            })
        if not out['duration']:
            for st in out['streams']:
                if st.get('duration'):
                    out['duration'] = max(out['duration'], st['duration'])
        return out
    except Exception:
        return None


def smoke_test(path: str, duration_s: float = 0.0,
               timeout: int = 300) -> Tuple[bool, str]:
    """
    有界解码冒烟: 抽开头与结尾各 SMOKE_WINDOW 秒实际解码。

    这是唯一能抓住"容器自洽但内容不可播"的检查。
    退出码非 0, 或出现 "Invalid data found when processing input" 即判失败。

    注意: ffmpeg 返回 0 **不等于** 成功 (例如它可能把整条流丢掉却仍然返回 0),
    所以这里只在 ffmpeg 不可用时降级放行, 其余一律以退出码与错误文本为准。
    """
    ffmpeg = _ffmpeg_path()
    if not ffmpeg or not os.path.exists(path):
        return True, 'skipped (ffmpeg 不可用)'

    si, cf = _subprocess_flags()
    windows: List[Tuple[float, float]] = [(0.0, SMOKE_WINDOW)]
    if duration_s and duration_s > SMOKE_WINDOW * 1.5:
        windows.append((max(0.0, duration_s - SMOKE_WINDOW), SMOKE_WINDOW))

    for start, length in windows:
        cmd = [ffmpeg, '-v', 'error', '-nostdin']
        if start > 0:
            cmd += ['-ss', f'{start:.3f}']
        cmd += ['-i', path, '-t', f'{length:.3f}', '-f', 'null', '-']
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=timeout,
                               startupinfo=si, creationflags=cf)
        except subprocess.TimeoutExpired:
            return False, f'解码冒烟超时 (起始 {start:.0f}s)'
        err = r.stderr.decode('utf-8', 'replace').strip()
        if r.returncode != 0:
            return False, f'解码失败 (起始 {start:.0f}s, 退出码 {r.returncode}): {err[:300]}'
        if 'Invalid data found when processing input' in err:
            return False, f'解码报 Invalid data (起始 {start:.0f}s): {err[:300]}'
    return True, ''


# ==================== sidecar ====================

@dataclass
class Sidecar:
    version: int = PIPELINE_VERSION
    key_high: int = 0
    total_size: int = 0
    covered_ranges: List[List[int]] = field(default_factory=list)
    slice_indices: List[int] = field(default_factory=list)
    source_mtimes: Dict[str, float] = field(default_factory=dict)
    coverage_signature: str = ''
    output_size: int = 0
    output_mtime: float = 0.0
    duration_s: float = 0.0
    computed_duration_s: float = 0.0
    truncated: bool = False
    playable: bool = True
    decode_ok: bool = False
    decode_detail: str = ''
    ffprobe: Dict[str, Any] = field(default_factory=dict)
    reasons: List[str] = field(default_factory=list)
    created_at: float = 0.0

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, separators=(',', ':'))

    @classmethod
    def from_json(cls, text: str) -> Optional['Sidecar']:
        try:
            payload = json.loads(text)
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in payload.items() if k in known})


def sidecar_path(media_path: str) -> str:
    return media_path + SIDECAR_SUFFIX


def part_path(final_path: str) -> str:
    """待提交路径 (通过全部验证后才 os.replace 成 final_path)"""
    return final_path + PART_SUFFIX


def staging_path(final_path: str) -> str:
    """
    重建/重打包的暂存路径。

    转封装不能就地读写同一个文件, 所以需要两个不同的中间文件名:
      final_path + '.part.src'  <- 重建产物 (临时, 提交后删除)
      final_path + '.part'      <- 转封装产物 (进入 finalize)
    两者都不以媒体扩展名结尾, 因此不会被当成导出。
    """
    return final_path + '.part.src'


def write_sidecar(media_path: str, sc: Sidecar) -> bool:
    """原子写 sidecar"""
    target = sidecar_path(media_path)
    tmp = target + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            f.write(sc.to_json())
        os.replace(tmp, target)
        return True
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def read_sidecar(media_path: str) -> Optional[Sidecar]:
    try:
        with open(sidecar_path(media_path), 'r', encoding='utf-8') as f:
            return Sidecar.from_json(f.read())
    except (OSError, ValueError):
        return None


def is_current(media_path: str, signature: Optional[str] = None) -> bool:
    """
    该导出物是否仍然有效 (版本 / 覆盖率指纹 / 尺寸 / mtime 全部匹配)。

    signature 为 None 表示调用方没有覆盖率信息 (非大视频), 此时只校验版本与文件本身。
    """
    if not os.path.exists(media_path):
        return False
    sc = read_sidecar(media_path)
    if sc is None or sc.version != PIPELINE_VERSION:
        return False
    if signature and sc.coverage_signature and sc.coverage_signature != signature:
        return False
    try:
        st = os.stat(media_path)
    except OSError:
        return False
    if sc.output_size and sc.output_size != st.st_size:
        return False
    if sc.output_mtime and int(sc.output_mtime) != int(st.st_mtime):
        return False
    return True


def is_media_filename(name: str) -> bool:
    return os.path.splitext(name)[1].lower() in MEDIA_EXTS


# ==================== 验证门 ====================

@dataclass
class ExportVerdict:
    ok: bool = False
    reasons: List[str] = field(default_factory=list)
    duration_s: float = 0.0
    size: int = 0
    ffprobe: Dict[str, Any] = field(default_factory=dict)
    decode_ok: bool = False
    decode_detail: str = ''


def validate_export(part: str,
                    expected_duration: Optional[float] = None,
                    do_probe: bool = True,
                    do_decode: bool = True) -> ExportVerdict:
    """
    四道门里的前三道 (第四道是 os.replace 本身的原子性)。

    Returns:
        ExportVerdict; ok=False 时 reasons 里是可展示给用户的原因
    """
    v = ExportVerdict()
    if not os.path.exists(part):
        v.reasons.append('产物文件不存在')
        return v

    v.size = os.path.getsize(part)
    if v.size < 1024:
        v.reasons.append(f'产物过小 ({v.size} 字节)')
        return v

    # 门 1: box 结构 —— 只对 ISOBMFF 生效。
    # WebM/Matroska 没有 box 概念, 拿 MP4 规则去套必然失败, 会让所有 webm 永远导不出来。
    if mp4.looks_like_iso_bmff(part):
        box = mp4_validate(part)
        if not box.ok:
            v.reasons.extend(box.reasons)
            return v
        v.duration_s = box.duration_s

    # 门 2: ffprobe 时长与计算值一致
    if do_probe:
        meta = probe(part)
        if meta is None:
            v.reasons.append('ffprobe 无法解析该文件')
            return v
        v.ffprobe = meta
        # 非 ISOBMFF (WebM 等) 没有 box 可算时长, 用 ffprobe 的补上
        if not v.duration_s and meta.get('duration'):
            v.duration_s = float(meta['duration'])
        if meta.get('nb_streams', 0) <= 0:
            v.reasons.append('产物中没有任何流 (ffmpeg 可能丢弃了全部轨道)')
            return v
        if expected_duration and meta.get('duration'):
            tol = max(2.0, 0.05 * expected_duration)
            if abs(meta['duration'] - expected_duration) > tol:
                v.reasons.append(
                    f'ffprobe 时长 {meta["duration"]:.3f}s 与计算值 '
                    f'{expected_duration:.3f}s 相差超过 {tol:.1f}s')

    # 门 3: 有界解码冒烟
    if do_decode:
        ok, detail = smoke_test(part, v.duration_s or (expected_duration or 0))
        v.decode_ok = ok
        v.decode_detail = detail
        if not ok:
            v.reasons.append(detail)

    v.ok = not v.reasons
    return v


# ==================== 提交 ====================

def check_free_space(directory: str, needed: int) -> Tuple[bool, str]:
    """导出前预检磁盘空间, 避免 ENOSPC 只留下一个半成品 .part"""
    try:
        import shutil
        usage = shutil.disk_usage(directory)
        if needed > 0 and usage.free < needed + (64 << 20):
            return False, (f'磁盘空间不足: 需要约 {needed / 1048576:.0f} MB, '
                           f'可用 {usage.free / 1048576:.0f} MB')
        return True, ''
    except Exception:
        return True, ''          # 探测失败不阻塞


def finalize(part: str,
             final: str,
             sidecar: Optional[Sidecar] = None,
             expected_duration: Optional[float] = None,
             do_probe: bool = True,
             do_decode: bool = True,
             plain: bool = False) -> ExportVerdict:
    """
    验证 .part -> 写 sidecar -> 原子改名到最终文件名。

    只有全部通过才会出现最终文件名; 失败时删除 .part 并保留原有文件不动。

    plain=True 用于非视频 (图片/音频/贴片/bin): 跳过 MP4 结构、时长与解码三道门,
    只要求产物存在且非空 —— 但仍然走 sidecar + 原子改名, 因此
    "已重建"标记与"旧产物失效"对所有类型一致生效。
    """
    if plain:
        verdict = ExportVerdict()
        if not os.path.exists(part):
            verdict.reasons.append('产物文件不存在')
        elif os.path.getsize(part) <= 0:
            verdict.reasons.append('产物文件为空')
        else:
            verdict.size = os.path.getsize(part)
            verdict.ok = True
    else:
        verdict = validate_export(part, expected_duration, do_probe, do_decode)

    if not verdict.ok:
        try:
            os.remove(part)
        except OSError:
            pass
        return verdict

    st = os.stat(part)
    if sidecar is None:
        sidecar = Sidecar()
    sidecar.version = PIPELINE_VERSION
    sidecar.output_size = st.st_size
    sidecar.output_mtime = st.st_mtime
    sidecar.duration_s = verdict.duration_s
    if expected_duration:
        sidecar.computed_duration_s = expected_duration
    sidecar.decode_ok = verdict.decode_ok
    sidecar.decode_detail = verdict.decode_detail
    sidecar.ffprobe = {k: v for k, v in verdict.ffprobe.items() if k != 'streams'}
    sidecar.created_at = time.time()
    if not sidecar.reasons:
        sidecar.playable = True

    # 原子改名。
    # Windows 上如果目标文件正被占用 (例如浏览器还在流式播放同一个导出文件),
    # os.replace 会抛 WinError 5/32。这里做几次短重试, 失败后如实报错, 绝不留下
    # 以最终名命名的半成品。
    last_exc: Optional[OSError] = None
    for attempt in range(5):
        try:
            os.replace(part, final)
            last_exc = None
            break
        except OSError as exc:
            last_exc = exc
            time.sleep(0.2 * (attempt + 1))
    if last_exc is not None:
        verdict.ok = False
        verdict.reasons.append(
            f'提交失败 (目标文件可能正被占用): {last_exc}')
        try:
            os.remove(part)
        except OSError:
            pass
        return verdict

    write_sidecar(final, sidecar)
    return verdict


# ==================== 导出编排 ====================

@dataclass
class ExportOutcome:
    """一次导出的完整结果 (供 API 与 UI 使用)"""
    ok: bool = False
    final_path: str = ''
    filename: str = ''
    size: int = 0
    duration_s: float = 0.0
    total_size: int = 0
    truncated: bool = False
    coverage_signature: str = ''
    covered_bytes: int = 0
    missing_blocks: List[int] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)
    probe: Dict[str, Any] = field(default_factory=dict)
    stage: Any = None


def produce(final_path: str,
            stage_fn: Callable[[str], Any],
            *,
            key_high: int = 0,
            coverage: Any = None,
            slice_indices: Sequence[int] = (),
            source_mtimes: Optional[Dict[str, float]] = None,
            need_space: int = 0,
            do_probe: bool = True,
            do_decode: bool = True,
            remux: bool = True) -> ExportOutcome:
    """
    导出编排: 重建 -> [转封装] -> 四道验证门 -> 原子提交。

    两条不可妥协的规则:
      1. 最终文件名只可能通过 os.replace 产生 —— 失败时既有的好文件不受影响,
         也不会留下任何以最终名命名的半成品。
      2. 任何一步失败都返回 ok=False 与可展示的原因, 绝不谎报成功。

    Args:
        final_path: 最终导出路径 (exports/<file_id>.<ext>)
        stage_fn: 接收"暂存路径", 返回一个带 playable/size/duration_s/reasons
            属性的对象 (src.rebuild.RebuildResult)
        coverage: src.coverage.Coverage, 写入 sidecar 用于后续失效判定;
            不传则取 stage 结果里的 coverage
        remux: True=视频, 需要 ffmpeg 转封装为 moov 前置;
            False=图片/音频等, 跳过转封装与 MP4 校验
        need_space: 预估需要的字节数 (用于磁盘预检)
        do_probe / do_decode: 关闭第二/第三道门 (测试用)
    """
    out = ExportOutcome(final_path=final_path, filename=os.path.basename(final_path))
    directory = os.path.dirname(final_path) or '.'
    stage_path = staging_path(final_path)
    ready_path = part_path(final_path)

    try:
        os.makedirs(directory, exist_ok=True)
    except OSError as exc:
        out.reasons.append(f'无法创建导出目录: {exc}')
        return out

    ok_space, space_msg = check_free_space(directory, need_space)
    if not ok_space:
        out.reasons.append(space_msg)
        return out

    # ---- 1. 重建 / 重打包到暂存路径 ----
    for stale in (stage_path, ready_path):
        _safe_remove(stale)

    try:
        stage = stage_fn(stage_path)
    except Exception as exc:
        _safe_remove(stage_path)
        out.reasons.append(f'重建异常: {exc.__class__.__name__}: {exc}')
        return out

    out.stage = stage
    stage_playable = bool(getattr(stage, 'playable', False))
    stage_reasons = list(getattr(stage, 'reasons', ()) or ())
    stage_duration = float(getattr(stage, 'duration_s', 0.0) or 0.0)
    out.truncated = bool(getattr(stage, 'truncated', False))
    out.total_size = int(getattr(stage, 'total_size', 0) or 0)
    out.missing_blocks = list(getattr(stage, 'missing_blocks', ()) or ())

    # 覆盖率与分片清单优先取调用方传入的; 没传就用重建结果里带出来的
    cov = coverage if coverage is not None else getattr(stage, 'coverage', None)
    if not slice_indices:
        slice_indices = list(getattr(stage, 'slice_indices', ()) or ())
    if source_mtimes is None:
        source_mtimes = dict(getattr(stage, 'source_mtimes', {}) or {})

    if not stage_playable:
        _safe_remove(stage_path)
        out.reasons = stage_reasons or ['重建失败: 无法产出可用产物']
        return out

    if not os.path.exists(stage_path) or os.path.getsize(stage_path) < 1:
        _safe_remove(stage_path)
        out.reasons = ['重建产物不存在或为空']
        return out

    # ---- 2. 转封装 (moov 前置) ----
    if remux:
        from .exporter import _remux_file
        rr = _remux_file(stage_path, ready_path, want_duration=stage_duration)
        if not rr.ok:
            _safe_remove(stage_path)
            _safe_remove(ready_path)
            out.reasons = ['转封装失败: ' + r for r in (rr.reasons or ['未知原因'])]
            return out
        candidate = ready_path
    else:
        candidate = stage_path

    # ---- 3. 四道验证门 + 原子提交 ----
    sc = Sidecar(
        key_high=int(key_high or 0),
        total_size=out.total_size,
        covered_ranges=[list(e.as_tuple()) for e in cov.extents] if cov else [],
        slice_indices=list(slice_indices or ()),
        source_mtimes=dict(source_mtimes or {}),
        coverage_signature=cov.signature() if cov else '',
        truncated=out.truncated,
    )
    verdict = finalize(candidate, final_path, sc,
                       expected_duration=stage_duration if remux else None,
                       do_probe=do_probe, do_decode=do_decode,
                       plain=not remux)
    _safe_remove(stage_path)
    _safe_remove(ready_path)

    out.probe = dict(verdict.ffprobe or {})
    if not verdict.ok:
        out.reasons = list(verdict.reasons) or ['导出校验未通过']
        return out

    out.ok = True
    out.size = verdict.size
    out.duration_s = verdict.duration_s
    out.size = verdict.size or (os.path.getsize(final_path) if os.path.exists(final_path) else 0)
    out.coverage_signature = sc.coverage_signature
    out.covered_bytes = int(cov.covered_bytes()) if cov else 0
    if out.truncated:
        out.reasons = [f'缓存不完整, 已导出可播放的局部片段 ({out.duration_s:.1f}s)']
    return out


def _safe_remove(path: str) -> None:
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


# ==================== 清理 ====================

def cleanup_stale(directory: str, max_age: int = DEFAULT_STALE_AGE) -> Dict[str, int]:
    """
    清理崩溃遗留物:
      - 名字里含 '.part' 的暂存文件 (超过 max_age 时)
      - 孤儿 sidecar (媒体文件已不存在)
      - *.tmp
    Returns: {'part': n, 'sidecar': n, 'tmp': n}
    """
    removed = {'part': 0, 'sidecar': 0, 'tmp': 0}
    if not os.path.isdir(directory):
        return removed
    now = time.time()
    try:
        names = os.listdir(directory)
    except OSError:
        return removed

    for name in names:
        path = os.path.join(directory, name)
        if not os.path.isfile(path):
            continue
        try:
            if name.endswith('.tmp'):
                os.remove(path)
                removed['tmp'] += 1
                continue
            # 用 in 而不是 endswith —— 存在 X.mp4.part 与 X.mp4.part.src 两种中间文件
            if PART_SUFFIX in name:
                age = now - os.path.getmtime(path)
                if age > max_age:
                    os.remove(path)
                    removed['part'] += 1
                continue
            if name.endswith(SIDECAR_SUFFIX):
                media = path[:-len(SIDECAR_SUFFIX)]
                if not os.path.exists(media):
                    os.remove(path)
                    removed['sidecar'] += 1
        except OSError:
            pass
    return removed
