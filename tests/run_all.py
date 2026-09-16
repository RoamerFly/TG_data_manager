"""
按顺序跑完整套自测。

用法 (在项目根目录):
    python tests/run_all.py            # 全部
    python tests/run_all.py mp4 e2e    # 只跑名字匹配的几项

各项的含义:
  mp4          box 解析 / 校验 / 表格截断 (含 co64、stz2、64位 mdat 变体)
  coverage     覆盖率模型
  deserializer 两个静默损坏 bug 与完整性推断规则的回归
  binlog      分片归组口径 (key_low>>16 = 文档 id) —— 防止串台的回归
  rebuild      流式重建: 形态2 (前缀+尾部 moov) 端到端, 峰值内存, 失败清理
  pipeline     导出管线: 四道验证门 / sidecar / 原子提交 / 失效
  e2e          导出编排 produce() 的完整流程
  server       Flask 接口集成 (合成 tdata, 无需真实 Telegram 数据)
  merge        分片合并展示 / 合成率排序 / doc_key 归属 / t.me 链接绑定
  clear        清空缓存/下载目录: 三步安全红线 (合成 tdata, 不碰真实数据)
  ui           前端 DOM 级验证 (需要 jsdom, 见下)

ui 需要 jsdom:  cd .temp/uicheck && npm install jsdom
且需要先启动 demo 服务:  python .temp/serve_demo.py 5099
"""

import os
import subprocess
import sys
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(PROJECT_ROOT)

PY = sys.executable

ORDER = [
    ('fixtures', [PY, 'tests/make_fixtures.py']),
    ('mp4', [PY, 'tests/test_mp4.py']),
    ('coverage', [PY, 'tests/test_coverage.py']),
    ('deserializer', [PY, 'tests/test_deserializer.py']),
    ('binlog', [PY, 'tests/test_binlog.py']),
    ('rebuild', [PY, 'tests/test_rebuild.py']),
    ('mediaparts', [PY, 'tests/test_media_parts.py']),
    ('pipeline', [PY, 'tests/test_export_pipeline.py']),
    ('e2e', [PY, 'tests/test_e2e_export.py']),
    ('server', [PY, 'tests/test_server_integration.py']),
    ('desktop', [PY, 'tests/test_desktop_shell.py']),
    ('merge', [PY, 'tests/test_merge_and_bind.py']),
    ('clear', [PY, 'tests/test_clear_cache.py']),
]


def main():
    wanted = [a.lower() for a in sys.argv[1:]]
    results = []
    for name, cmd in ORDER:
        if wanted and not any(w in name for w in wanted):
            continue
        print('\n' + '=' * 72)
        print(f'  {name}: {" ".join(cmd)}')
        print('=' * 72)
        started = time.time()
        r = subprocess.run(cmd, capture_output=True)
        out = r.stdout.decode('utf-8', 'replace')
        err = r.stderr.decode('utf-8', 'replace')
        tail = [ln for ln in out.strip().splitlines() if 'passed,' in ln]
        print(out if len(out) < 6000 else out[-6000:])
        if err.strip():
            print('--- stderr ---')
            print(err[-2000:])
        ok = r.returncode == 0
        results.append((name, ok, tail[-1] if tail else '', time.time() - started))

    print('\n' + '=' * 72)
    print('  汇总')
    print('=' * 72)
    for name, ok, tail, dt in results:
        print(f'  {"PASS" if ok else "FAIL"}  {name:14s} {tail:32s} {dt:6.1f}s')
    failed = [n for n, ok, _t, _d in results if not ok]
    if failed:
        print(f'\n失败的项: {", ".join(failed)}')
        return 1
    print('\n全部通过。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
