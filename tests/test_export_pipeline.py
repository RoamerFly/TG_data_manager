"""src/export_pipeline.py 自测: 运行 python tests/test_export_pipeline.py"""

import os
import shutil
import sys
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)          # 夹具与 ffmpeg 路径都相对项目根

from src.export_pipeline import (  # noqa: E402
    PIPELINE_VERSION, Sidecar, check_free_space, cleanup_stale, ffmpeg_capabilities,
    finalize, is_current, is_media_filename, part_path, probe, read_sidecar,
    sidecar_path, smoke_test, validate_export,
)
from src.mp4 import Reader, iter_boxes  # noqa: E402

FIX = os.path.join('.temp', 'fixtures')
TMP = os.path.join('.temp', 'pipeline')
PASS, FAIL = [], []


def check(name, cond, detail=''):
    (PASS if cond else FAIL).append(name)
    print(('  PASS  ' if cond else '  FAIL  ') + name + (('   ' + str(detail)) if detail else ''))


shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP, exist_ok=True)

GOOD = os.path.join(FIX, 'faststart.mp4')
MOOV_END = os.path.join(FIX, 'moov_end.mp4')
raw = open(MOOV_END, 'rb').read()
moov_off = raw.rfind(b'moov') - 4

# 造几个坏样本
BAD_NO_MOOV = os.path.join(TMP, 'no_moov.mp4')
open(BAD_NO_MOOV, 'wb').write(raw[:moov_off])
BAD_CUT = os.path.join(TMP, 'cut_tail.mp4')
open(BAD_CUT, 'wb').write(raw[:300000])
BAD_TINY = os.path.join(TMP, 'tiny.mp4')
open(BAD_TINY, 'wb').write(b'\x00' * 64)

print('\n[1] ffmpeg 能力探测')
caps = ffmpeg_capabilities()
print('   ', {k: v for k, v in caps.items() if k != 'path'})
check('ffmpeg 可用', caps.get('available') is True)
check('检测到 libx264 (不再需要静默降级)', caps.get('has_libx264') is True)

print('\n[2] probe / smoke_test')
meta = probe(GOOD)
check('probe 读出时长', bool(meta) and abs(meta['duration'] - 4.0) < 0.1,
      meta and round(meta['duration'], 3))
check('probe 读出 2 条流', bool(meta) and meta['nb_streams'] == 2)
ok, detail = smoke_test(GOOD, 4.0)
check('正常文件冒烟通过', ok, detail[:200])
check('probe 对不存在文件返回 None', probe(os.path.join(TMP, 'nope.mp4')) is None)

print('\n[3] validate_export —— 关键回归: 缺 moov 必须是失败')
v = validate_export(GOOD, 4.0)
check('正常文件: ok', v.ok, v.reasons)
check('正常文件: 时长 4.0s', abs(v.duration_s - 4.0) < 0.05, v.duration_s)

v = validate_export(BAD_NO_MOOV, 4.0)
check('缺 moov: 不是 ok (旧 stco_exceeds_file 会错判为正常)', not v.ok)
check('缺 moov: 原因提到 moov', any('moov' in r for r in v.reasons), v.reasons[:2])

v = validate_export(BAD_CUT, 4.0)
check('尾部截断: 不是 ok', not v.ok, v.reasons[:2])

v = validate_export(BAD_TINY, 4.0)
check('过小文件: 不是 ok', not v.ok, v.reasons[:2])

v = validate_export(GOOD, 99.0)          # 故意给一个错误的期望时长
check('时长不匹配: 不是 ok', not v.ok, v.reasons[:2])

print('\n[4] finalize 原子提交')
part = part_path(os.path.join(TMP, 'movie.mp4'))
shutil.copyfile(GOOD, part)
sc = Sidecar(key_high=0xDEADBEEF, total_size=999999, coverage_signature='sig-A',
             slice_indices=[0, 1], truncated=True)
final = os.path.join(TMP, 'movie.mp4')
verdict = finalize(part, final, sc, expected_duration=4.0)
check('finalize 成功', verdict.ok, verdict.reasons)
check('最终文件出现', os.path.exists(final))
check('part 已被消费', not os.path.exists(part))
check('sidecar 已写出', os.path.exists(sidecar_path(final)))
back = read_sidecar(final)
check('sidecar 版本正确', bool(back) and back.version == PIPELINE_VERSION)
check('sidecar 记录 signature', bool(back) and back.coverage_signature == 'sig-A')
check('sidecar 记录时长', bool(back) and abs(back.duration_s - 4.0) < 0.1,
      back and round(back.duration_s, 3))
check('sidecar 记录解码通过', bool(back) and back.decode_ok)
check('sidecar 记录 playable', bool(back) and back.playable is True)

print('\n[5] is_current 失效判定')
check('签名一致 -> 有效', is_current(final, 'sig-A'))
check('签名不同 -> 失效 (分片集合变了)', not is_current(final, 'sig-B'))
check('无签名参数 -> 只校验文件本身', is_current(final))
time.sleep(1.1)
with open(final, 'ab') as f:
    f.write(b'\x00' * 100)
check('文件被改动后失效', not is_current(final, 'sig-A'))

print('\n[6] finalize 失败路径: 不得留下最终文件名')
bad_part = part_path(os.path.join(TMP, 'bad.mp4'))
shutil.copyfile(BAD_NO_MOOV, bad_part)
bad_final = os.path.join(TMP, 'bad.mp4')
verdict = finalize(bad_part, bad_final, Sidecar(), expected_duration=4.0)
check('坏产物 finalize 失败', not verdict.ok)
check('坏产物: 不产生最终文件', not os.path.exists(bad_final))
check('坏产物: .part 被清理', not os.path.exists(bad_part))
check('坏产物: 不写 sidecar', not os.path.exists(sidecar_path(bad_final)))

print('\n[7] sidecar 往返 / 容错')
s = Sidecar(key_high=12345, total_size=678, slice_indices=[3, 4, 5],
            covered_ranges=[[0, 100], [200, 300]], source_mtimes={'a': 1.5})
s2 = Sidecar.from_json(s.to_json())
check('往返 key_high', s2 and s2.key_high == 12345)
check('往返 slice_indices', s2 and s2.slice_indices == [3, 4, 5])
check('往返 covered_ranges', s2 and s2.covered_ranges == [[0, 100], [200, 300]])
check('坏 JSON -> None', Sidecar.from_json('{{{') is None)
check('未知字段被忽略', Sidecar.from_json('{"version":2,"bogus":1}') is not None)

print('\n[8] 文件名白名单 (保证 .part/.json 不会被当成导出)')
check('.mp4 是媒体', is_media_filename('ABCD.mp4'))
check('.json 不是媒体', not is_media_filename('ABCD.mp4.json'))
check('.part 不是媒体', not is_media_filename('ABCD.mp4.part'))
check('.tmp 不是媒体', not is_media_filename('ABCD.mp4.tmp'))
check('无扩展名不是媒体', not is_media_filename('ABCD'))

print('\n[9] cleanup_stale')
stale_part = os.path.join(TMP, 'old.mp4.part')
open(stale_part, 'wb').write(b'x' * 32)
old = time.time() - 10 * 3600
os.utime(stale_part, (old, old))
fresh_part = os.path.join(TMP, 'fresh.mp4.part')
open(fresh_part, 'wb').write(b'x' * 32)
tmp_file = os.path.join(TMP, 'x.json.tmp')
open(tmp_file, 'wb').write(b'x')
orphan_json = os.path.join(TMP, 'gone.mp4.json')
open(orphan_json, 'wb').write(b'{}')

removed = cleanup_stale(TMP, max_age=6 * 3600)
print('   ', removed)
check('清理过期 .part', not os.path.exists(stale_part))
check('保留未过期 .part', os.path.exists(fresh_part))
check('清理 .tmp', not os.path.exists(tmp_file))
check('清理孤儿 sidecar', not os.path.exists(orphan_json))
check('有媒体的 sidecar 被保留', os.path.exists(sidecar_path(final)))

print('\n[10b] WebM 转封装回归 —— stream_copy 参数顺序')
# 曾经的 bug: `stream_copy(fi, fo, 0, src_size)` 把**已打开的读句柄**当成源路径传进去,
# stream_copy 内部又对第一个参数 open(), 直接 TypeError 让整个导出 500。
# 两个分支 (有 ffmpeg / 无 ffmpeg) 都有这行, 必须都覆盖。
from src import exporter as EX  # noqa: E402

WEBM = os.path.join(TMP, 'clip.webm')
with open(WEBM, 'wb') as _f:
    _f.write(b'\x1a\x45\xdf\xa3' + bytes(range(256)) * 40)   # EBML magic + 垃圾负载
WEBM_OUT_A = os.path.join(TMP, 'clip.a.webm')
WEBM_OUT_B = os.path.join(TMP, 'clip.b.webm')

# (a) ffmpeg 存在: 方案1/2 都会失败, 应落回方案3 的流式复制
try:
    rr_a = EX._remux_file(WEBM, WEBM_OUT_A, want_duration=0.0)
    crashed = False
except Exception as exc:                                  # pragma: no cover
    rr_a, crashed = None, True
    print('   异常:', exc.__class__.__name__, exc)
check('有 ffmpeg: WebM 转封装不抛异常', not crashed)
check('有 ffmpeg: 落回 stream 模式并产出文件',
      bool(rr_a and rr_a.ok) and os.path.exists(WEBM_OUT_A)
      and os.path.getsize(WEBM_OUT_A) == os.path.getsize(WEBM),
      getattr(rr_a, 'mode', None))

# (b) ffmpeg 缺失: 应当直接原样输出, 同样不能崩
_orig_ffmpeg = EX._get_ffmpeg_path
EX._get_ffmpeg_path = lambda: None
try:
    rr_b = EX._remux_file(WEBM, WEBM_OUT_B, want_duration=0.0)
    crashed_b = False
except Exception as exc:                                  # pragma: no cover
    rr_b, crashed_b = None, True
    print('   异常:', exc.__class__.__name__, exc)
finally:
    EX._get_ffmpeg_path = _orig_ffmpeg
check('无 ffmpeg: WebM 转封装不抛异常', not crashed_b)
check('无 ffmpeg: 原样输出且字节一致',
      bool(rr_b and rr_b.ok) and os.path.exists(WEBM_OUT_B)
      and open(WEBM_OUT_B, 'rb').read() == open(WEBM, 'rb').read())

print('\n[10c] 非 ISOBMFF 容器不能被 MP4 规则卡死')
from src.mp4 import looks_like_iso_bmff  # noqa: E402

check('MP4 被识别为 ISOBMFF', looks_like_iso_bmff(GOOD) is True)
check('WebM 不被识别为 ISOBMFF', looks_like_iso_bmff(WEBM) is False)

# 真实 webm 片段 (用自带 ffmpeg 生成, 失败就跳过)
REAL_WEBM = os.path.join(TMP, 'real.webm')
ff = None
try:
    from src.exporter import _get_ffmpeg_path
    ff = _get_ffmpeg_path()
except Exception:
    pass
if ff:
    import subprocess as _sp
    _sp.run([ff, '-v', 'error', '-nostdin', '-f', 'lavfi', '-i',
             'testsrc=size=64x64:rate=10:duration=2', '-c:v', 'libvpx',
             '-b:v', '100k', '-y', REAL_WEBM], capture_output=True, timeout=180)
if os.path.exists(REAL_WEBM) and os.path.getsize(REAL_WEBM) > 0:
    wv = validate_export(REAL_WEBM, None, do_probe=True, do_decode=True)
    check('WebM 通过验证门 (不再报"顶层没有任何 box")', wv.ok,
          '; '.join(wv.reasons)[:160])
    check('WebM 时长由 ffprobe 补全', wv.duration_s > 1.0, round(wv.duration_s, 3))
else:
    print('   (跳过: 当前 ffmpeg 无法生成 webm)')

print('\n[10] 磁盘空间预检')
ok_space, msg = check_free_space(TMP, 1024)
check('常规需求通过', ok_space, msg)
ok_huge, msg_huge = check_free_space(TMP, 1 << 60)
check('超大需求被拒绝', not ok_huge, msg_huge[:80])

print(f'\n===== {len(PASS)} passed, {len(FAIL)} failed =====')
if FAIL:
    for f in FAIL:
        print('  FAILED:', f)
    sys.exit(1)
