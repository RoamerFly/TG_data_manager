"""src/coverage.py 自测: 运行 python tests/test_coverage.py"""

import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)          # 夹具与 ffmpeg 路径都相对项目根

from src.coverage import (  # noqa: E402
    K_IN_SLICE, Coverage, build_large_video_coverage, coverage_from_pairs,
)

PASS, FAIL = [], []


def check(name, cond, detail=''):
    (PASS if cond else FAIL).append(name)
    print(('  PASS  ' if cond else '  FAIL  ') + name + (('   ' + str(detail)) if detail else ''))


MB = 1024 * 1024

print('\n[1] 合并与查询')
c = Coverage()
c.add(0, 100).add(100, 200)          # 相邻 -> 合并
c.add(500, 600)
check('相邻区间合并', [e.as_tuple() for e in c.extents] == [(0, 200), (500, 600)],
      [e.as_tuple() for e in c.extents])
check('重叠区间合并', [e.as_tuple() for e in Coverage().add(0, 150).add(100, 300).extents]
      == [(0, 300)])
check('顺序无关', Coverage().add(500, 600).add(0, 200).extents ==
      Coverage().add(0, 200).add(500, 600).extents)
check('零长度忽略', len(Coverage().add(10, 10)) == 0)
check('contains 命中', c.contains(50, 80))
check('contains 跨段为假', not c.contains(150, 550))
check('contains 出界为假', not c.contains(150, 250))
check('covered_bytes', c.covered_bytes() == 300, c.covered_bytes())
check('total_end', c.total_end == 600)

print('\n[2] 连续前缀与空洞')
check('前缀=200', coverage_from_pairs([(0, 200), (500, 600)]).covered_prefix_end() == 200)
check('无 0 起点 -> 前缀 0', coverage_from_pairs([(50, 100)]).covered_prefix_end() == 0)
check('空洞定位', coverage_from_pairs([(0, 200), (500, 600)]).first_hole_from(0) == 200)
check('无空洞时返回 None', coverage_from_pairs([(0, 200)]).first_hole_from(0) is None)
check('空洞起点正确', coverage_from_pairs([(0, 200), (500, 600)]).first_hole_from(300) == 300)
check('overlap 提升前缀', coverage_from_pairs([(0, 100), (50, 300)]).covered_prefix_end() == 300)

print('\n[3] gaps / missing')
cov = coverage_from_pairs([(0, 2 * MB), (5 * MB, 6 * MB)])
check('gaps_within(8MB)', cov.gaps_within(8 * MB) == [(2 * MB, 5 * MB), (6 * MB, 8 * MB)],
      cov.gaps_within(8 * MB))
check('missing_blocks(8MB, 1MB块)',
      cov.missing_blocks(8 * MB, block=MB) == [2, 3, 4, 6, 7],
      cov.missing_blocks(8 * MB, block=MB))
check('present_blocks', cov.present_blocks(8 * MB, block=MB) == [0, 1, 5])

print('\n[4] 完整性 = 覆盖 ⊆ 需求区间 (权威判定)')
req = [(0, 1000), (2000, 3000)]
check('全覆盖 -> True', coverage_from_pairs([(0, 1000), (2000, 3000)]).covers_all(req))
check('中间缺一块 -> False',
      not coverage_from_pairs([(0, 1000), (2500, 3000)]).covers_all(req))
check('缺尾 -> False', not coverage_from_pairs([(0, 1000)]).covers_all(req))
check('多出无关区段不影响', coverage_from_pairs([(0, 5000)]).covers_all(req))
check('missing_of 定位', coverage_from_pairs([(0, 1000)]).missing_of(req) == [(2000, 3000)])
check('需求区间为空 -> True', Coverage().covers_all([]))

print('\n[5] signature 稳定性与失效')
a = Coverage().add(0, 100, 'header', 'H').add(100, 200, 'slice', 'S1')
b = Coverage().add(100, 200, 'slice', 'S1').add(0, 100, 'header', 'H')
check('顺序不同签名相同', a.signature() == b.signature())
c2 = Coverage().add(0, 100, 'header', 'H')
check('分片变少 -> 签名变化', a.signature() != c2.signature())
c3 = Coverage().add(0, 100, 'header', 'H').add(100, 200, 'slice', 'S2')
check('分片被替换 -> 签名变化', a.signature() != c3.signature())
check('签名长度 40', len(a.signature()) == 40)

print('\n[6] JSON 往返')
j = a.to_json()
back = Coverage.from_json(j)
check('往返区间一致', [e.as_tuple() for e in back.extents] == [e.as_tuple() for e in a.extents])
check('往返签名一致', back.signature() == a.signature())
check('往返保留 ref', sorted((e.kind, e.ref) for e in back.raw)
      == sorted((e.kind, e.ref) for e in a.raw))
check('空串容错', len(Coverage.from_json('')) == 0)
check('坏 JSON 容错', len(Coverage.from_json('{not json')) == 0)

print('\n[7] build_large_video_coverage')
# header: 带 [0,128KB) 与尾部 moov part; 分片 0,1,3 存在, 2 缺失
S = K_IN_SLICE
parts = [(0, 128 * 1024), (400 * MB, 64 * 1024)]
slices = [(0, S, 'slice0'), (1, S, 'slice1'), (3, S, 'slice3')]
cv = build_large_video_coverage(parts, slices)
check('header 段存在', cv.contains(0, 128 * 1024))
check('moov 段存在', cv.contains(400 * MB, 400 * MB + 64 * 1024))
check('分片 3 存在', cv.contains(3 * S, 4 * S))
check('分片 2 缺失', not cv.contains(2 * S, 3 * S))
check('连续前缀到 2*S', cv.covered_prefix_end() == 2 * S, cv.covered_prefix_end())
check('可写前缀内不含洞', cv.first_hole_from(0) == 2 * S)

print('\n[8] describe / format')
print('   ', cv.describe())
check('describe 不为空', bool(cv.describe()))

print(f'\n===== {len(PASS)} passed, {len(FAIL)} failed =====')
if FAIL:
    for f in FAIL:
        print('  FAILED:', f)
    sys.exit(1)
