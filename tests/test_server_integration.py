"""
server.py 集成自测 —— 用合成 tdata 打通 /api/file, /api/export, /api/preview,
/api/files, /api/exports 全链路。

不需要真实的 Telegram 加密数据: 直接把解密后的字节喂给一个假的 scanner,
并把 binlog 索引手工构造出来。

运行: python .temp/server_selftest.py
"""

import io
import json
import os
import shutil
import struct
import subprocess
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)          # 夹具与 ffmpeg 路径都相对项目根
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)          # 夹具与 ffmpeg 路径都相对项目根

from src.binlog import BinlogRecord  # noqa: E402
from src.coverage import K_IN_SLICE  # noqa: E402
from src.mp4 import Reader, iter_boxes, parse_moov, validate  # noqa: E402
from src.scanner import CacheFile, FileType, ScanResult, TYPE_LABELS, CATEGORY_MAP  # noqa: E402

FIX = os.path.join('.temp', 'fixtures')
WORK = os.path.join('.temp', 'itest')
PASS, FAIL = [], []


def check(name, cond, detail=''):
    (PASS if cond else FAIL).append(name)
    print(('  PASS  ' if cond else '  FAIL  ') + name + (('   ' + str(detail)) if detail else ''))


def build_header(parts):
    body = bytearray(struct.pack('<I', len(parts)))
    for off, data in parts:
        body += struct.pack('<I', off) + struct.pack('<I', len(data)) + data
    return bytes(body)


def build_serialized(parts):
    body = bytearray()
    for i in range(0, len(parts), 79):
        g = parts[i:i + 79]
        body += struct.pack('<I', len(g))
        for off, d in g:
            body += struct.pack('<I', off) + struct.pack('<I', len(d)) + d
    return bytes(body)


shutil.rmtree(WORK, ignore_errors=True)
os.makedirs(os.path.join(WORK, 'media_cache', '1'), exist_ok=True)
os.makedirs(os.path.join(WORK, 'exports'), exist_ok=True)
os.makedirs(os.path.join(WORK, 'thumbs'), exist_ok=True)

BIG = os.path.join(FIX, 'big_moov_end.mp4')
SMALL = os.path.join(FIX, 'faststart.mp4')
if not os.path.exists(BIG):
    print('!! 先运行 tests/test_rebuild.py 生成大样本')
    sys.exit(1)

raw = open(BIG, 'rb').read()
r = Reader(BIG)
moov_box = next(b for b in iter_boxes(r, 0, r.size()) if b.type == b'moov')
moov = parse_moov(r)
R = r.size()
r.close()

# ==================== 合成缓存内容 ====================

HEADER_ID = 'DEADBEEF'
SLICE_IDS = ['11111111', '22222222', '33333333']
SER_ID = 'AAAAAAAA'
IMG_ID = 'BBBBBBBB'
KEY_HIGH = 0x1122334455667788

header_blob = build_header([(0, raw[:128 * 1024])]
                          + [(moov_box.start + i, raw[moov_box.start + i:
                                                     moov_box.start + i + 128 * 1024])
                             for i in range(0, moov_box.size, 128 * 1024)])

blobs = {HEADER_ID: header_blob}

slice_paths = {}
ON_DISK_SLICES = SLICE_IDS[:2]          # 只让分片 0,1 真正存在于磁盘上
for n, sid in enumerate(SLICE_IDS):
    lo = n * K_IN_SLICE
    hi = min(lo + K_IN_SLICE, len(raw))
    blobs[sid] = raw[lo:hi]
    if sid in ON_DISK_SLICES:
        sp = os.path.join(WORK, 'media_cache', '1', sid)
        open(sp, 'wb').write(raw[lo:hi])
        slice_paths[sid] = sp

# 普通序列化视频 (完整)
small_raw = open(SMALL, 'rb').read()
sr = Reader(SMALL)
parts = [(off, small_raw[off:off + 128 * 1024])
         for off in range(0, len(small_raw), 128 * 1024)]
blobs[SER_ID] = build_serialized(parts)

# 一张 PNG
from PIL import Image  # noqa: E402
buf = io.BytesIO()
Image.new('RGB', (32, 32), (200, 30, 30)).save(buf, 'PNG')
blobs[IMG_ID] = buf.getvalue()


class FakeScanner:
    def __init__(self, store):
        self.store = store
        self.local_key = b'\x00' * 256

    def get_decrypted_data(self, file_id):
        return self.store.get(file_id)


import server  # noqa: E402

server.Config.export_dir = os.path.join(WORK, 'exports')
server.Config.thumbnail_dir = os.path.join(WORK, 'thumbs')
server.Config.tdata_path = WORK
server.Config.ensure_dirs()

fake = FakeScanner(blobs)
server.scanner = fake
server.get_scanner = lambda: fake


def _fake_decrypt_by_path(path):
    try:
        with open(path, 'rb') as fh:
            return fh.read()
    except OSError:
        return None


server._decrypt_by_path = _fake_decrypt_by_path


def mkf(file_id, ftype, size, **kw):
    return CacheFile(
        file_id=file_id, file_name=file_id,
        file_path=slice_paths.get(file_id, os.path.join(WORK, file_id)),
        relative_path=file_id, cache_type='media_cache',
        file_size=size, decrypted_size=size,
        file_type=ftype, file_type_label=TYPE_LABELS.get(ftype, ''),
        category=CATEGORY_MAP.get(ftype, 'unknown'),
        is_serialized=(ftype == FileType.SERIALIZED_VIDEO), **kw)


def rec(key_high, key_low, size):
    return BinlogRecord(tag=0, size=size, place=b'\x00' * 7, checksum=0,
                        key_high=key_high, key_low=key_low)


files = [
    mkf(HEADER_ID, FileType.SERIALIZED_VIDEO, len(header_blob), is_large_video=True),
    mkf(SLICE_IDS[0], FileType.VIDEO_SLICE, K_IN_SLICE),
    mkf(SLICE_IDS[1], FileType.VIDEO_SLICE, K_IN_SLICE),
    mkf(SER_ID, FileType.SERIALIZED_VIDEO, len(blobs[SER_ID])),
    mkf(IMG_ID, FileType.PNG, len(blobs[IMG_ID])),
]
server.scan_result = ScanResult(total_files=len(files), decrypted_files=len(files),
                                files=files)
server.file_index = {f.file_id: f for f in files}
server.file_name_index = {f.file_name: f for f in files}

server.binlog_index = {
    # binlog 外部文件 index i 覆盖 [(i-1)*8MiB, i*8MiB) (2026-09-15 修正):
    # index 0 是 header 自身; 外部分片从 1 起。这里 1/2 在盘上(铺 [0,8),
    # [8,16)), 3 只有 binlog 记录而磁盘上不存在 -> 必须被报为缺失分片 2
    HEADER_ID: rec(KEY_HIGH, 0, len(header_blob)),
    SLICE_IDS[0]: rec(KEY_HIGH, 1, K_IN_SLICE),
    SLICE_IDS[1]: rec(KEY_HIGH, 2, K_IN_SLICE),
    SLICE_IDS[2]: rec(KEY_HIGH, 3, K_IN_SLICE),
}

server._enrich_scan(server.scan_result, server.binlog_index)
server._build_large_video_header_index()
server.build_file_index()

client = server.app.test_client()

print(f'  big total_size={parse_moov(Reader(BIG)).max_chunk_end() if False else moov.max_chunk_end()} '
      f'slices_needed={files[0].slices_needed} complete={files[0].is_complete_large_video}')


# ==================== 1. 详情: 缺一个分片必须如实显示 ====================

print('\n[1] /api/file/<id> 覆盖率口径的完整性')

resp = client.get(f'/api/file/{HEADER_ID}')
d = resp.get_json()
check('详情返回 200', resp.status_code == 200)
check('识别为大视频', d.get('is_large_video') is True)
check('判定为不完整', d.get('is_complete_large_video') is False)
check('is_incomplete 为真', d.get('is_incomplete') is True)
lv = d.get('large_video_info') or {}
check('缺分片列表 = [2]', lv.get('missing_indices') == [2], lv.get('missing_indices'))
check('分片详情 3 项', len(lv.get('slice_details') or []) == 3)
check('on_disk 标记正确',
      sorted(sd['slice_index'] for sd in lv.get('slice_details', []) if sd['on_disk']) == [0, 1],
      [(sd['slice_index'], sd['on_disk']) for sd in lv.get('slice_details', [])])
check('estimated_size 是真实文件大小', lv.get('estimated_size') == moov.max_chunk_end(),
      f"{lv.get('estimated_size')} vs {moov.max_chunk_end()}")
check('slices_needed = ceil(total/8MB)', lv.get('slices_needed') == 3,
      lv.get('slices_needed'))
check('给出了可播放时长 (约 23.8s)',
      22.0 < (d.get('playable_duration') or 0) < 25.0,
      d.get('playable_duration'))
check('未在详情里写任何导出文件 (GET 不再有副作用)',
      not any(n.endswith('.mp4') for n in os.listdir(server.Config.export_dir)),
      os.listdir(server.Config.export_dir))

print('\n[1b] 完整视频的详情')
resp = client.get(f'/api/file/{SER_ID}')
d2 = resp.get_json()
check('序列化视频判定为完整', d2.get('is_incomplete') is False, d2.get('incomplete_reasons'))
check('不显示为缺分片', d2.get('is_complete_large_video') is not False)

resp = client.get(f'/api/file/{IMG_ID}')
d3 = resp.get_json()
check('图片不受影响', resp.status_code == 200 and d3.get('category') == 'image')


# ==================== 2. 导出残缺大视频 -> 局部可播片段 ====================

print('\n[2] /api/export/<id> 残缺大视频')

resp = client.post(f'/api/export/{HEADER_ID}')
out = resp.get_json()
print(f'  status={resp.status_code} ok={out.get("ok")} size={out.get("size")} '
      f'dur={out.get("duration")} truncated={out.get("truncated")}')
print(f'  reasons={out.get("reasons")}')
check('导出成功', resp.status_code == 200 and out.get('ok') is True, out.get('error'))
check('标记为局部片段', out.get('truncated') is True)
check('返回 filename (前端契约)', bool(out.get('filename')), out.get('filename'))
check('返回 size (前端契约)', (out.get('size') or 0) > 0, out.get('size'))
check('时长明显短于完整时长', 20.0 < (out.get('duration') or 0) < 28.0, out.get('duration'))

exp = os.path.join(server.Config.export_dir, out.get('filename') or '')
check('最终文件存在', os.path.exists(exp))
check('sidecar 已写出', os.path.exists(exp + '.json'))
check('没有残留 .part / .part.src',
      not any('.part' in n for n in os.listdir(server.Config.export_dir)),
      os.listdir(server.Config.export_dir))

v = validate(exp)
check('产物结构有效', v.ok, v.reasons[:3])
r2 = subprocess.run([os.path.join('dist_windows', 'env', 'ffmpeg.exe'),
                     '-v', 'error', '-i', exp, '-f', 'null', '-'],
                    capture_output=True, timeout=600)
check('产物可完整解码', r2.returncode == 0,
      r2.stderr.decode('utf-8', 'replace')[:200])


# ==================== 3. 预览: 稳定的产物 + Range ====================

print('\n[3] /api/preview/<id> 与 Range')

resp = client.get(f'/api/preview/{HEADER_ID}')
check('预览返回 200', resp.status_code == 200)
check('mimetype 为 video/mp4', resp.mimetype == 'video/mp4', resp.mimetype)
check('声明 Accept-Ranges', resp.headers.get('Accept-Ranges') == 'bytes')
full_len = int(resp.headers.get('Content-Length') or 0)
check('Content-Length 与导出文件一致', full_len == os.path.getsize(exp),
      f'{full_len} vs {os.path.getsize(exp)}')
resp.close()   # Windows: 必须释放句柄, 否则后续 os.replace 会失败

resp2 = client.get(f'/api/preview/{HEADER_ID}', headers={'Range': 'bytes=0-1023'})
r2_status, r2_len = resp2.status_code, len(resp2.data)
r2_cr = resp2.headers.get('Content-Range') or ''
resp2.close()
check('Range 请求返回 206', r2_status == 206, r2_status)
check('Range 返回 1024 字节', r2_len == 1024, r2_len)
check('Content-Range 正确', r2_cr.startswith('bytes 0-1023/'), r2_cr)

# 普通序列化视频的预览产物应被池化
resp3 = client.get(f'/api/preview/{SER_ID}')
s3_status = resp3.status_code
resp3.close()
check('小视频预览返回 200', s3_status == 200, s3_status)
prev_dir = os.path.join(server.Config.export_dir, '.preview')
check('预览产物已池化', os.path.isdir(prev_dir) and len(os.listdir(prev_dir)) >= 1,
      os.listdir(prev_dir) if os.path.isdir(prev_dir) else 'missing')
resp3b = client.get(f'/api/preview/{SER_ID}', headers={'Range': 'bytes=0-511'})
s3b_status = resp3b.status_code
resp3b.close()
check('第二次 Range 仍能命中 (无需重新生成)', s3b_status == 206, s3b_status)


# ==================== 4. 列表: is_rebuilt 与 .part 不被误判 ====================

print('\n[4] /api/files 与 /api/exports')

resp = client.get('/api/files?per_page=100')
listing = resp.get_json()
by_id = {f['file_id']: f for f in listing.get('files', [])}
check('大视频显示为已重建', by_id.get(HEADER_ID, {}).get('is_rebuilt') is True,
      by_id.get(HEADER_ID, {}).get('is_rebuilt'))
check('大视频仍显示不完整', by_id.get(HEADER_ID, {}).get('is_complete_large_video') is False)
check('暴露 covered_bytes', (by_id.get(HEADER_ID, {}).get('covered_bytes') or 0) > 0)
check('暴露 missing_slice_indices',
      by_id.get(HEADER_ID, {}).get('missing_slice_indices') == [2],
      by_id.get(HEADER_ID, {}).get('missing_slice_indices'))

# 伪造一个 .part 与 .json, 确认它们不会被当成导出
open(os.path.join(server.Config.export_dir, 'FFFFFFFF.mp4.part'), 'wb').write(b'x' * 64)
open(os.path.join(server.Config.export_dir, 'FFFFFFFF.mp4.json'), 'w').write('{}')
server._rebuild_exported_ids()
check('.part 不会被登记为已导出', 'FFFFFFFF' not in server._exported_ids)

resp = client.get('/api/exports')
exps = resp.get_json().get('files', [])
names = [e['filename'] for e in exps]
check('导出列表非空', len(exps) >= 1, names)
check('导出列表不含 .part', not any('.part' in n for n in names), names)
check('导出列表不含 .json', not any(n.endswith('.json') for n in names), names)
check('导出列表含 sidecar 元数据', any(e.get('duration') for e in exps), exps[:1])


# ==================== 5. 补全分片后应重新判定为完整 ====================

print('\n[5] 补全分片 -> 增量重算应为完整 (回归"按引用复用"残留)')

# 把缺失的分片补到磁盘上: 内容是位置 2 (即 [16.77MB, 25.17MB) 的字节),
# binlog index=3 -> 摆放到 [(3-1)*8MiB) = 位置 2, 与内容一致
extra_id = '44444444'
lo = 2 * K_IN_SLICE
extra_path = os.path.join(WORK, 'media_cache', '1', extra_id)
open(extra_path, 'wb').write(raw[lo:lo + K_IN_SLICE])
blobs[extra_id] = raw[lo:lo + K_IN_SLICE]
slice_paths[extra_id] = extra_path
nf = mkf(extra_id, FileType.VIDEO_SLICE, K_IN_SLICE)
server.scan_result.files.append(nf)
server.file_index[extra_id] = nf
server.file_name_index[extra_id] = nf
server.binlog_index[extra_id] = rec(KEY_HIGH, 3, K_IN_SLICE)

server._enrich_scan(server.scan_result, server.binlog_index)
hf = server.file_index[HEADER_ID]
print(f'  重新富化后: complete={hf.is_complete_large_video} '
      f'missing={hf.missing_slice_indices} sig={hf.coverage_signature[:10]}')
check('补全后判定为完整', hf.is_complete_large_video is True, hf.missing_reason)
check('缺分片列表清空', hf.missing_slice_indices == [])

# 分片集合变化 -> 旧导出应失效
check('旧导出因指纹变化而失效', server._export_valid_for(HEADER_ID) is False)
server._rebuild_exported_ids()
check('失效后 is_rebuilt 变为 False', HEADER_ID not in server._exported_ids)


# ==================== 6. 完整视频导出应得到完整时长 ====================

print('\n[6] 补全后重新导出 -> 时长等于完整时长')

resp = client.post(f'/api/export/{HEADER_ID}')
out2 = resp.get_json()
print(f'  ok={out2.get("ok")} dur={out2.get("duration")} truncated={out2.get("truncated")} '
      f'reasons={out2.get("reasons")}')
check('导出成功', out2.get('ok') is True, out2.get('error'))
check('不再标记为局部片段', out2.get('truncated') is False)
check('时长等于完整时长 (约 30s)', 29.0 < (out2.get('duration') or 0) < 31.0,
      out2.get('duration'))
exp2 = os.path.join(server.Config.export_dir, out2.get('filename') or '')
check('产物可完整解码', validate(exp2).ok, validate(exp2).reasons[:2])


# ==================== 7. 图片导出 (plain 模式) 同样走原子提交 ====================

print('\n[7] 图片导出')

resp = client.post(f'/api/export/{IMG_ID}')
oi = resp.get_json()
check('图片导出成功', oi.get('ok') is True, oi.get('error'))
img_path = os.path.join(server.Config.export_dir, oi.get('filename') or '')
check('图片产物存在', os.path.exists(img_path))
check('图片也有 sidecar', os.path.exists(img_path + '.json'))
check('图片 is_rebuilt 为真', server._export_valid_for(IMG_ID))


print(f'\n===== {len(PASS)} passed, {len(FAIL)} failed =====')
if FAIL:
    for f in FAIL:
        print('  FAILED:', f)
    sys.exit(1)
