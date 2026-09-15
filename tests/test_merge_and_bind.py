"""
合并展示 + Telegram 来源绑定 自测 (2026-09-15)。

覆盖:
1. 分片合并展示: 已归属到 header 的分片不再单独成行; 孤立分片保留
2. 归属按 doc_key (回归: 只按 key_high 会把 B 视频的分片错归到 A)
3. 合成率 (covered/total, 两位小数) 与排序: 完整最前, 其余按合成率降序
4. t.me 链接解析 / 绑定 / 解绑 / 详情字段

运行: python tests/test_merge_and_bind.py (由 run_all.py 调度)
"""

import io
import json
import os
import shutil
import struct
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)          # 夹具与 ffmpeg 路径都相对项目根

from src.binlog import BinlogRecord  # noqa: E402
from src.coverage import K_IN_SLICE  # noqa: E402
from src.mp4 import Reader, iter_boxes, parse_moov  # noqa: E402
from src.scanner import CacheFile, FileType, ScanResult, TYPE_LABELS, CATEGORY_MAP  # noqa: E402

FIX = os.path.join('.temp', 'fixtures')
WORK = os.path.join('.temp', 'mtest')
PASS, FAIL = [], []


def check(name, cond, detail=''):
    (PASS if cond else FAIL).append(name)
    print(('  PASS  ' if cond else '  FAIL  ') + name + (('   ' + str(detail)) if detail else ''))


def build_header(parts):
    body = bytearray(struct.pack('<I', len(parts)))
    for off, data in parts:
        body += struct.pack('<I', off) + struct.pack('<I', len(data)) + data
    return bytes(body)


shutil.rmtree(WORK, ignore_errors=True)
os.makedirs(os.path.join(WORK, 'media_cache', '1'), exist_ok=True)
os.makedirs(os.path.join(WORK, 'exports'), exist_ok=True)
os.makedirs(os.path.join(WORK, 'thumbs'), exist_ok=True)

BIG = os.path.join(FIX, 'big_moov_end.mp4')
if not os.path.exists(BIG):
    print('!! 先运行 tests/make_fixtures.py 生成夹具')
    sys.exit(1)

raw = open(BIG, 'rb').read()
r = Reader(BIG)
moov_box = next(b for b in iter_boxes(r, 0, r.size()) if b.type == b'moov')
r.close()

# ==================== 合成两个共享 key_high 的大视频 + 一个孤立分片 ====================
# 关键: 两个视频的 doc_key 不同 (doc48 不同), 但 key_high 相同 ——
# 旧的 key_high 归属会把 B 的分片错归到 A。

KEY_HIGH = 0xAABBCCDD11223344
DOC_A = 0x1111          # 视频 A 的 doc48
DOC_B = 0x2222          # 视频 B 的 doc48

H_A, S_A1, S_A2, S_A3 = 'AAAA1111', 'AAAA2222', 'AAAA3333', 'AAAA4444'  # A: 3/3 分片 -> 完整
H_B, S_B1 = 'BBBB1111', 'BBBB2222'                      # B: header + 1/3 分片 -> 不完整
ORPHAN = 'CCCC9999'                                      # 孤立分片: 无 header
IMG_ID = 'DDEE0000'


def kl(doc48, slice_idx):
    return (doc48 << 16) | slice_idx


header_blob = build_header([(0, raw[:128 * 1024])]
                          + [(moov_box.start + i, raw[moov_box.start + i:
                                                     moov_box.start + i + 128 * 1024])
                             for i in range(0, moov_box.size, 128 * 1024)])

blobs = {H_A: header_blob, H_B: header_blob}

slice_files = {}


def put_slice(fid, doc48, idx):
    lo = (idx - 1) * K_IN_SLICE
    hi = min(lo + K_IN_SLICE, len(raw))
    sp = os.path.join(WORK, 'media_cache', '1', fid)
    open(sp, 'wb').write(raw[lo:hi])
    slice_files[fid] = sp


put_slice(S_A1, DOC_A, 1)
put_slice(S_A2, DOC_A, 2)
put_slice(S_A3, DOC_A, 3)
put_slice(S_B1, DOC_B, 1)
put_slice(ORPHAN, 0x3333, 1)   # 孤立分片: doc 0x3333 没有 header

# 一张 PNG
from PIL import Image  # noqa: E402
buf = io.BytesIO()
Image.new('RGB', (32, 32), (30, 30, 200)).save(buf, 'PNG')
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

# 隔离 telegram_links 存储 (不要碰项目根的真实绑定文件)
server.TELEGRAM_LINKS_PATH = os.path.join(WORK, 'telegram_links.json')
server._telegram_links_cache = None

fake = FakeScanner(blobs)
server.scanner = fake
server.get_scanner = lambda: fake
server._decrypt_by_path = lambda p: open(p, 'rb').read() if os.path.exists(p) else None


def mkf(file_id, ftype, size, **kw):
    return CacheFile(
        file_id=file_id, file_name=file_id,
        file_path=slice_files.get(file_id, os.path.join(WORK, file_id)),
        relative_path=file_id, cache_type='media_cache',
        file_size=size, decrypted_size=size,
        file_type=ftype, file_type_label=TYPE_LABELS.get(ftype, ''),
        category=CATEGORY_MAP.get(ftype, 'unknown'),
        is_serialized=(ftype == FileType.SERIALIZED_VIDEO), **kw)


def rec(key_high, key_low, size):
    return BinlogRecord(tag=0, size=size, place=b'\x00' * 7, checksum=0,
                        key_high=key_high, key_low=key_low)


files = [
    mkf(H_A, FileType.SERIALIZED_VIDEO, len(header_blob), is_large_video=True),
    mkf(S_A1, FileType.VIDEO_SLICE, K_IN_SLICE),
    mkf(S_A2, FileType.VIDEO_SLICE, K_IN_SLICE),
    mkf(S_A3, FileType.VIDEO_SLICE, K_IN_SLICE),
    mkf(H_B, FileType.SERIALIZED_VIDEO, len(header_blob), is_large_video=True),
    mkf(S_B1, FileType.VIDEO_SLICE, K_IN_SLICE),
    mkf(ORPHAN, FileType.VIDEO_SLICE, K_IN_SLICE),
    mkf(IMG_ID, FileType.PNG, len(blobs[IMG_ID])),
]
server.scan_result = ScanResult(total_files=len(files), decrypted_files=len(files),
                                files=files)
server.file_index = {f.file_id: f for f in files}
server.file_name_index = {f.file_name: f for f in files}

server.binlog_index = {
    H_A: rec(KEY_HIGH, kl(DOC_A, 0), len(header_blob)),
    S_A1: rec(KEY_HIGH, kl(DOC_A, 1), K_IN_SLICE),
    S_A2: rec(KEY_HIGH, kl(DOC_A, 2), K_IN_SLICE),
    S_A3: rec(KEY_HIGH, kl(DOC_A, 3), K_IN_SLICE),
    H_B: rec(KEY_HIGH, kl(DOC_B, 0), len(header_blob)),
    S_B1: rec(KEY_HIGH, kl(DOC_B, 1), K_IN_SLICE),
    ORPHAN: rec(KEY_HIGH, kl(0x3333, 1), K_IN_SLICE),
}

server._enrich_scan(server.scan_result, server.binlog_index)
server._build_large_video_header_index()
server.build_file_index()

client = server.app.test_client()

print(f'  A: complete={files[0].is_complete_large_video} covered={files[0].covered_bytes} '
      f'total={files[0].total_size}')
print(f'  B: complete={files[4].is_complete_large_video} covered={files[4].covered_bytes} '
      f'total={files[4].total_size}')


# ==================== 1. 归属按 doc_key (回归: key_high 串台) ====================

print('\n[1] 分片归属必须按 doc_key (共享 key_high 不得串台)')

resp = client.get(f'/api/file/{S_B1}')
sb = resp.get_json().get('slice_info') or {}
check('B 的分片归属到 B 的 header', sb.get('parent_header_id') == H_B,
      sb.get('parent_header_id'))
check('B 的分片不会错归到 A', sb.get('parent_header_id') != H_A,
      sb.get('parent_header_id'))

resp = client.get(f'/api/file/{S_A1}')
sa = resp.get_json().get('slice_info') or {}
check('A 的分片归属到 A 的 header', sa.get('parent_header_id') == H_A,
      sa.get('parent_header_id'))


# ==================== 2. 合并展示: 归属分片不单独成行 ====================

print('\n[2] /api/files 合并展示')

resp = client.get('/api/files?per_page=100')
fl = resp.get_json()
ids = [f['file_id'] for f in fl['files']]

check('A 的分片不再单独成行', S_A1 not in ids and S_A2 not in ids, ids)
check('B 的分片不再单独成行', S_B1 not in ids, ids)
check('两个 header 都在列表里', H_A in ids and H_B in ids, ids)
check('孤立分片保留展示 (没有 header 可归属)', ORPHAN in ids, ids)
check('图片不受影响', IMG_ID in ids, ids)
check('列表恰好 4 项 (2 header + 孤立分片 + 图片)', len(ids) == 4, ids)

# 搜索也要遵守合并 (分片名搜不到, 视频本身可搜到)
resp = client.get('/api/files?per_page=100&search=aaaa')
ids_s = [f['file_id'] for f in resp.get_json()['files']]
check('搜索分片文件名不再命中分片', S_A1 not in ids_s and S_A2 not in ids_s, ids_s)
check('搜索仍能找到视频本身', H_A in ids_s, ids_s)


# ==================== 3. 合成率与排序 ====================

print('\n[3] 合成率与排序 (完整最前, 其余按合成率降序)')

resp = client.get(f'/api/file/{H_A}')
da = resp.get_json()
resp = client.get(f'/api/file/{H_B}')
db = resp.get_json()

check('完整视频合成率 = 100', da.get('synthesis_rate') == 100.0,
      da.get('synthesis_rate'))
check('不完整视频合成率 < 100', 0 < (db.get('synthesis_rate') or 0) < 100,
      db.get('synthesis_rate'))
check('合成率保留两位小数',
      db.get('synthesis_rate') == round(db.get('synthesis_rate'), 2),
      db.get('synthesis_rate'))
check('普通图片没有合成率', client.get(f'/api/file/{IMG_ID}').get_json()
      .get('synthesis_rate') is None)

resp = client.get('/api/files?per_page=100&sort=size_desc')
ids_sorted = [f['file_id'] for f in resp.get_json()['files']]
# 期望: A(完整, grp0) -> B(不完整, grp1) -> 图片/孤立分片 (grp2)
check('完整的 A 排在最前', ids_sorted[0] == H_A, ids_sorted)
check('B 紧随其后 (唯一的不完整大视频)', ids_sorted[1] == H_B, ids_sorted)
check('非大视频排在后面', set(ids_sorted[2:]) == {ORPHAN, IMG_ID}, ids_sorted)


# ==================== 4. t.me 链接解析 ====================

print('\n[4] _parse_telegram_link')

P = server._parse_telegram_link

cases = [
    ('https://t.me/feihsl/25158?single&t=4', 'tg://resolve?domain=feihsl&post=25158'),
    ('https://t.me/feihsl/25158', 'tg://resolve?domain=feihsl&post=25158'),
    ('t.me/feihsl/25158', 'tg://resolve?domain=feihsl&post=25158'),
    ('https://telegram.me/some_channel/123', 'tg://resolve?domain=some_channel&post=123'),
    ('https://t.me/feihsl', 'tg://resolve?domain=feihsl'),
    ('https://t.me/c/1234567/25158', 'tg://privatepost?channel=1234567&post=25158'),
    ('https://t.me/c/1234567', 'tg://privatepost?channel=1234567'),
    ('tg://resolve?domain=feihsl&post=25158', 'tg://resolve?domain=feihsl&post=25158'),
    ('tg://privatepost?channel=111&post=222', 'tg://privatepost?channel=111&post=222'),
]
for url, want in cases:
    got = P(url)
    check(f'解析 {url[:40]}', bool(got) and got['tg_url'] == want,
          got and got['tg_url'])

bad = ['', 'https://example.com/x', 'https://t.me/', 'http://evil.com/t.me/a/1',
       'tg://', 'tg://unknown?x=1', 'https://v.qq.com/x/1', '随便什么']
for url in bad:
    check(f'拒绝 {url[:30]!r}', P(url) is None, P(url))


# ==================== 5. 绑定 / 解绑 / 详情字段 ====================

print('\n[5] bind_telegram API')

resp = client.post(f'/api/file/{H_B}/bind_telegram',
                   json={'url': 'not a link'})
check('非法链接返回 400', resp.status_code == 400, resp.status_code)

resp = client.post(f'/api/file/{H_B}/bind_telegram',
                   json={'url': 'https://t.me/feihsl/25158?single&t=4'})
out = resp.get_json()
check('绑定成功', resp.status_code == 200 and out.get('ok') is True, out)
check('返回 tg_url', out.get('telegram_link', {}).get('tg_url')
      == 'tg://resolve?domain=feihsl&post=25158', out)

# 详情能看到绑定
resp = client.get(f'/api/file/{H_B}')
tl = resp.get_json().get('telegram_link') or {}
check('详情暴露 telegram_link', tl.get('tg_url') == 'tg://resolve?domain=feihsl&post=25158', tl)
check('详情暴露 document_id', bool(resp.get_json().get('document_id')),
      resp.get_json().get('document_id'))

# 绑定落盘 (按 doc: 键, 跨扫描有效)
links = json.load(open(server.TELEGRAM_LINKS_PATH, encoding='utf-8'))
doc_key = f'doc:{server.binlog_index[H_B].real_document_id:016X}'
check('绑定按真实 document_id 落盘', doc_key in links, list(links))
check('绑定文件写到了隔离目录',
      server.TELEGRAM_LINKS_PATH.startswith(WORK), server.TELEGRAM_LINKS_PATH)

# 模拟重扫描: 换 file_id 但 doc 不变, 绑定仍应命中
server._telegram_links_cache = None   # 强制重读盘
moved = mkf('NEWBB1111', FileType.SERIALIZED_VIDEO, len(header_blob), is_large_video=True)
server.binlog_index['NEWBB1111'] = server.binlog_index[H_B]
check('换文件名后 (同 doc) 绑定仍命中',
      bool(server._get_telegram_link(moved)), server._get_telegram_link(moved))

# 解绑
resp = client.delete(f'/api/file/{H_B}/bind_telegram')
check('解绑成功', resp.status_code == 200 and resp.get_json().get('removed') is True,
      resp.get_json())
resp = client.get(f'/api/file/{H_B}')
check('解绑后详情无 telegram_link',
      not (resp.get_json().get('telegram_link') or {}).get('tg_url'),
      resp.get_json().get('telegram_link'))

print(f'\n===== {len(PASS)} passed, {len(FAIL)} failed =====')
if FAIL:
    for f in FAIL:
        print('  FAILED:', f)
    sys.exit(1)
