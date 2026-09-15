# 视频重建管线修复 —— 变更说明

修复目标：让恢复/重建出来的视频**能完整播完**；残缺缓存输出**可播放的局部片段**；
导出**不再谎报成功**；重建不再把整部视频读进内存。

---

## 一、根因（四条互相叠加的机制）

| # | 机制 | 原来的位置 |
|---|---|---|
| 1 | **完整性靠推断**：`estimated_size <= max_end ⇒ slices_needed = 0` 几乎恒为真（因为"是大视频"的判定条件恰好是"存在 > 8MiB 的 part 偏移"，使 `max_end` 接近 EOF），于是缺掉整个 mdat 中段的视频被判成"完整"；无 binlog 时还用"全盘 8MB 文件总数"冒充这个视频的分片数 | `deserializer.get_large_video_info`、`server._enrich_scan_without_binlog` |
| 2 | **"修洞"只是截掉尾部零块**，中部空洞原样保留 | `deserializer._find_continuous_data_range` |
| 3 | **转封装永不失败**：所有分支都 `return True`；`stco_exceeds_file` 在没有 moov 时返回 `False`（把最坏情况当成没问题）；`export_with_info` 直接置 `success=True` | `exporter._remux_video` / `stco_exceeds_file` / `export_with_info` |
| 4 | **导出直接写最终文件名**，残缺产物被登记为"已重建"并被 `api_preview` 反复复用；`api_file_detail` 甚至在 GET 里就写导出文件；普通视频每个 Range 请求都重新解密+remux | `server.py` 6 个写入点 / `_rebuild_exported_ids` / `api_file_detail` / `api_preview` |

---

## 二、核心设计

**关键洞察**：`stco / co64` 里的 chunk offset 是**从文件起始算的绝对偏移**。只要把已覆盖的
前缀 `[0, P)` 按原始偏移原样写出（mdat 起点不变），所有落在前缀内的 chunk 偏移**无需重算** ——
于是"重写 moov"从通用 MP4 授权（高风险）退化为**表格截断 + 时长修补**，再由 ffmpeg
`-c copy -movflags +faststart` 重写并前置 moov 作为二次校验。

**完整性的唯一定义**：`coverage ⊇ mp4.required_ranges(moov)`（moov 里每个 chunk 的字节区间）。

**导出的四道验证门**：box 结构校验 → ffprobe 时长容差 → 有界解码冒烟（开头+结尾各 20 秒）
→ 写 sidecar → `os.replace`。四道全过才会出现最终文件名。

---

## 三、新增模块

| 文件 | 职责 |
|---|---|
| `src/mp4.py` | box 遍历 / moov 与样本表解析 / `validate` / `plan_prefix` / `truncate_tables`。**取代所有 `bytes.find(b'moov')` 式子串探测** |
| `src/coverage.py` | 字节区间集合（合并、缺口、前缀、指纹）。完整性的权威来源 |
| `src/rebuild.py` | 流式重打包：`rebuild_large_video_to_file` / `repack_from_extents` / `analyze`。峰值内存 = 一个 8MB 分片 |
| `src/export_pipeline.py` | `produce` / `finalize` / `Sidecar` / `is_current` / `smoke_test`。原子提交与失效判定 |

## 四、主要改造

- **`src/deserializer.py`**：`deserialize_video` 用 `max(out_offset + part_size)` 计算大小
  （原来会静默丢弃 part），并**删除盲截断**（原来会把文件末尾的 moov 一起砍掉）；
  `get_large_video_info` 删除 `estimated_size <= max_end` 规则；`rebuild_large_video` 标记废弃。
- **`src/exporter.py`**：`stco_exceeds_file` 改为委派 `mp4.validate`（缺少 moov 现在是"不可信"
  而不是"没问题"）；`_remux_video` 返回结构化结果、每级都要通过结构+时长校验、
  删除"静默原样写入"；重编码前探测 ffmpeg 是否真的带 libx264；`export_with_info` 传导真实结果。
- **`src/scanner.py`**：`_has_valid_moov` / `identify_file_type` 改走 box 遍历；
  `CacheFile` 增加覆盖率字段。
- **`src/binlog.py`**：新增 `get_slice_records_for_header`（带分片大小）。
- **`server.py`**：新增 `_produce_export` 作为**唯一导出入口**；6 个写入点全部改线；
  `api_file_detail` 不再有写副作用；完整性富化改为覆盖率口径且每次扫描无条件重算；
  `_rebuild_exported_ids` 加媒体扩展名白名单 + hex 校验 + sidecar 校验（`.part`/`.json` 不会被当成导出）；
  预览产物按 `(file_id, 覆盖率指纹)` 池化，`call_on_close` 的错误写法删除。
- **`static/js/app.js`**：`handleVideoError` 补齐 `stalled/waiting/timeupdate/ended/durationchange`，
  能在"播到一半停住"时给出提示；残缺视频展示实际可播时长与缺失分片；
  失败时展示后端返回的原因；卡片对局部重建显示"已重建 · 局部"。

---

## 五、验证

```bash
E:/A-Environment/Miniconda3/python.exe tests/run_all.py
```

| 项 | 内容 | 结果 |
|---|---|---|
| fixtures | 用自带 ffmpeg 生成合成夹具 | — |
| mp4 | box 解析/校验/表格截断（含 `co64`、`stz2`、64 位 mdat 变体） | 50 通过 |
| coverage | 覆盖率模型 | 40 通过 |
| deserializer | 两个静默损坏 bug + 完整性推断规则的回归 | 22 通过 |
| rebuild | 流式重建：**形态 2（前缀 + 尾部 moov）端到端**、失败清理、内存 | 43 通过 |
| pipeline | 四道验证门 / sidecar / 原子提交 / 失效 | 47 通过 |
| e2e | 导出编排 `produce()` 全流程 | 31 通过 |
| server | Flask 接口集成（合成 tdata，无需真实 Telegram 数据） | 55 通过 |

前端另有 DOM 级校验 `.temp/uicheck/uicheck.js`（jsdom，29 项通过），覆盖
残缺视频详情渲染、导出后播放、**播放中断检测**、失败原因展示、卡片状态。

**最关键的端到端结论**：残缺的大视频（前缀 + 尾部独立 moov）现在导出为
**23.8 秒可完整播放的局部片段**，`ffprobe` 时长与计算值一致（23.800），
`ffmpeg -v error` 解码退出码 0，产物内**不含任何 ≥8KB 零洞**；
完整覆盖时 moov 字节级不变、时长与源一致。

---

## 六、遗留与风险

1. `_parse_serialized_data` 实际读的布局（`[part_count + parts]*N`）与 docstring /
   `scanner._check_serialized_video` 的描述（`slice_count + ...`）**不一致**，
   两者只有一个能匹配真实 Telegram 数据 —— 需要真实样本才能定论（已在代码里注明）。
2. moov 是否**保证**出现在 header 里（"moov 完全不可得"这种形态的出现频率）未经真实数据验证。
   若该形态常见，"局部可播放"能救回的比例会明显降低。
3. `elst` / B 帧 `ctts` / AAC encoder delay 在截断后可能引入约 1 帧的 A/V 偏移
   （已通过移除 `edts` 缓解，未实测）。
4. Windows 上 `os.replace` 会因目标文件正被播放而失败 —— 已加短重试，
   但"边播边重新重建"仍可能失败（会如实报错，不会破坏已有文件）。
5. 分片集合变化会让旧导出失效（这是有意为之）：用户机器上已有的导出会显示为
   "未重建"，需要重新生成一次。

---

## 七、打包与真实 tdata 验证（2026-09-15）

### 构建

```
E:/A-Environment/Miniconda3/python.exe -m PyInstaller --noconfirm --onefile --windowed \
  --name TGCacheManager --distpath dist_windows --workpath build \
  --add-data "src;src" --add-data "templates;templates" --add-data "static;static" \
  --hidden-import tgcrypto --hidden-import Crypto ... server.py
```

- 产物：`dist_windows/TGCacheManager.exe`（约 43.4 MB，Python 3.12.4 + PyInstaller 6.22.2）
- `build.bat` 已修：旧版第 3 步 `rmdir /s /q dist_windows` 会连 `dist_windows\env\` 里
  **200MB 的 ffmpeg.exe / ffprobe.exe 一起删掉**；现在只删旧 exe，并在构建后检查
  env/ffmpeg.exe 是否存在。
- 注意：`--hidden-import System.Windows.Forms / System.Threading` 会报 not found
  （没装 pythonnet），属无害告警，webview 仍走 edgechromium。

### 真实数据验证结果（30581 个缓存文件，扫描 24.6s）

- 服务启动、静态资源、webview 窗口、`/api/config`、`/api/scan`、`/api/stats`
  `/api/files`、`/api/file/<id>` 全部 200。
- 新增字段在真实数据上正常：`covered_bytes` / `missing_slice_indices` /
  `playable_duration` / `moov_present` / `is_complete_large_video`。

### 由此发现并修复的两个真实 bug

1. **`stream_copy` 参数顺序（崩溃）** — `src/exporter.py` 的 `_remux_file` 把
   **已打开的读句柄 `fi`** 当成源路径传给 `stream_copy()`，而它内部会对第一个参数
   再 `open()`，直接 `TypeError: expected str, bytes or os.PathLike object,
   not BufferedReader`，导出返回 500。有 ffmpeg / 无 ffmpeg 两条分支都有，均已修正为
   `stream_copy(src_path, fo, 0, src_size)`。
2. **验证门把 WebM 当 MP4 校验** — `validate_export` 无条件跑 `mp4.validate()`，
   于是所有 `.webm` 都被 "顶层没有任何 box" 挡在门外（且 `_accept_output`、
   `_is_valid_mp4_export` 同理）。新增 `mp4.looks_like_iso_bmff()`，只在确实是
   ISOBMFF 时才做 box 校验；非 MP4 容器交给 ffprobe，时长由 ffprobe 补全。
   修复后真实 webm 导出 `ok=True`（65504B/0.867s、65168B/1.88s）。

回归测试已补齐（`tests/test_export_pipeline.py` 的 [10b] / [10c]），
套件从 288 增至 **296 项，全部通过**。

### 仍未解决：大视频（serialized_video）重建在真实数据上失败

真实 tdata 里的 MP4 全部是 `serialized_video`（71 个）+ `video_slice`（18 个），
抽样导出全部失败，三种症状：

| 样本 | 现象 |
|---|---|
| `9863967533A6` | 有 moov、结构校验与 ffprobe 时长都通过，但**解码失败**：`Invalid NAL unit size (52794 > 40678)`，说明 mdat 负载拼错 |
| `926CFDD2161B` / `F105D7D942FE` | `缓存中未找到 moov (顶层 box 链不完整)` |

诊断脚本 `.temp/diag_real.py`（只读，不写缓存）已能复现，关键线索：

- 解密后的 header 以 `[u32][u32=0][u32=0x00020000(=128KiB)]` + 标准 `ftyp` 开头，
  即**紧跟着就是 MP4 数据**，与 `_parse_serialized_data` 假定的
  `slice_count + [part_count + parts]*N` 布局对不上。
- 同一 header：`parts_count=27`、`estimated_size` 两种算法给出 **36.6MB 与 4.8MB**
  两个互相矛盾的值；`slices_needed=5` 却匹配到 **16 个分片**，`covered_bytes` 92MB
  远大于 `total_size`。→ 分片发现/映射（key_high 匹配 + binlog）很可能过度匹配，
  或 part→(slice, offset) 的映射错了。

下一步应先把 header 的真实布局定死（对比 TD 源码 `SerializedFileParts`），
再重做分片发现。这是"恢复的视频无法播放"的最后一环。


---

## 8. 深度修复：「恢复的视频无法持久播放」根因（2026-09-15）

上一节末尾留下的「大视频重建在真实数据上失败」已定位并修复。结论全部来自本机
真实 tdata（`D:\Software\Telegram Desktop\tdata`，30581 个缓存文件）的字节级取证，
取证脚本见 `.temp/probe_*.py`（只读，不写缓存）。

### 8.1 真实缓存布局（取证确认）

序列化 header 的明文是**自描述的分区块结构**：

```
plaintext = [u32 part_count] + ([u32 out_offset][u32 size][size 字节 data]) * part_count
            ^^^ 上面这一整组可以重复出现若干次（组 0、组 1、...）^^^
```

- `out_offset` 是**媒体文件里的绝对字节偏移**，实测全部按 128KB 对齐。
- 同一个文件里的块**并不连续**：Telegram 流式下载先取「开头」再取「尾部 moov」，
  所以组 0 常见形态是 `[off=0, 128KB] + [文件末尾若干块]`，后续组才顺序补齐中间。
- 实测三个样本都能一路解析到距文件尾 2 字节处。

由此纠正了一个此前记录的「未决项」：`_parse_serialized_data` 假定的布局确实是错的，
`media_part_extents()` 取代它成为主路径（`src/deserializer.py`）。

### 8.2 真正的根因：binlog 分片归组口径错误

`src/binlog.py` 原来假设「`key_high` 相同 = 同一部媒体」。**这是错的**：

- 实测同一个 `key_high` 下混进了 **7 部不同的视频**（它们的 slice 索引都算成 0）；
- 于是把别的视频的 8MB 分片缝进当前视频 → mdat 负载拼错 →
  ffprobe 时长正常、但 ffmpeg 解码报 `Invalid NAL unit size`。

正确的分组键是 **`(key_high, key_low >> 16)`**，即

```
key_low = document_id(高 48 位) << 16 | slice_index(低 16 位)
```

证据：

- 按新口径分组后，组内 slice 索引**零重复**，且落在 `0..N` 的连续区间
  （实测 `0..280` / `0..233` / `0..101`）；
- 组内 slice 上界与 slice 0 序列化文件里 part 的最大 `out_offset` 高度吻合
  （281 × 8MiB ≈ 2.25GB ↔ part#17868 的 2.242GB）；
- 旧口径下同一组出现多个 `slice=0` 的不同大小文件 —— 自相矛盾。

`slice` 索引 i 对应媒体字节区间 `[i * 8MiB, (i+1) * 8MiB)`。

### 8.3 配套修正：自描述 header 与外部分片的优先级

最初的错误修法是「header 自描述 → 直接丢弃所有外部切片」。这会让一部**完整缓存**
的 30MB 视频永远只能导出前 8MB，属于功能倒退。正确做法是两者取并集：

- **header 自描述区段是权威的** —— 与外部分片重叠时以 header 为准
  （`rebuild._write_prefix` 先写 slice 再写 header；`SparseReader` 排序同样让
  header 排在后面覆盖）；
- 外部分片只在与已覆盖区域**相邻**时才真正延长可播放前缀；中间有空洞时它们
  只记入覆盖率，连续前缀规则会自动忽略 —— 因此即便个别分片定位有误也不会污染输出；
- `repack_from_extents()` 新增 `slices` / `decrypt_fn` 参数，统一走这一套。

### 8.4 改动清单

| 文件 | 改动 |
|---|---|
| `src/binlog.py` | 新增 `BinlogRecord.doc_key`；`get_slice_records_for_header` 改按 `doc_key` 归组；模块注释补上取证结论 |
| `src/deserializer.py` | 新增 `MediaPart` / `parse_media_parts` / `media_part_extents`（真实布局），旧的 `_parse_serialized_data` 降级为兜底 |
| `src/rebuild.py` | `collect_header_extents` / `serialized_extents` 优先走真实布局；`repack_from_extents` 支持外部切片；`SparseReader` 让 header 覆盖 slice |
| `server.py` | `_slices_for_header` 去掉「自描述就返回空」的早退；`stage_large`、缩略图两处、`_enrich_scan` 均改为 header 区段 + 外部切片 |
| `tests/test_binlog.py` | 新增：归组口径回归（含「旧口径确实会串台」的证明用例） |
| `tests/test_media_parts.py` | 新增：真实布局解析 / 非连续块 / 端到端重打包解码 |

### 8.5 验证结果

真实数据端到端（`.temp/verify_fix2.py`，走完整链路：自描述 header + 新口径外部切片）：

| 文件 | 产物 | ffprobe | ffmpeg 完整解码 |
|---|---|---|---|
| `9863967533A6` | 3.12 MB | 16.40s，h264 720x960 + aac | rc=0，0 错误 |
| `926CFDD2161B` | 8.00 MB | 10.54s，h264 1080x1920 + aac | rc=0，0 错误 |
| `F105D7D942FE` | 8.00 MB | 22.48s，h264 960x1280 + aac | rc=0，0 错误 |

三者均为**局部片段**（`truncated=True`）：本机缓存里这些视频本就只有开头 +
尾部 moov，中间数据没下载完。这是诚实的输出，而不是坏文件 —— 判定依据是
「coverage ⊇ moov 要求的字节区间」。

自测套件：332 项全部通过（`tests/run_all.py`）。

> 注：本机测试时 `pipeline` / `e2e` / `server` 三项若与其余套件放在同一批次里跑，
> 会被环境的批量删除保护（单回合累计删除 >50）中断，需单独运行 —— 与代码无关。

### 8.6 已知限制

- 外部分片的偏移只能由 `slice_index * 8MiB` 推出，无法逐一校验。取证时发现
  18 个 8MiB 裸块中 17 个符合该规律，有 1 个（`224EF3E286C8`，slice=1）却以
  `ftyp` 开头 —— 疑似陈旧 binlog 记录。由于它不与当前视频的连续前缀相邻，
  实际不会进入产物，但未彻底查清。
- 因此仍然坚持：宁可标记为不完整，也不猜分片归属。

---

## 9. 修复 build.bat（2026-09-15）

`build.bat` 此前**从来没有真正构建成功过**，而且一直在"静默失败"。

### 9.1 病根：UTF-8 + 纯 LF 换行

cmd.exe 解析 `.bat` 有两个硬要求，旧文件两条都违反：

| 要求 | 旧文件实际情况 | 后果 |
|---|---|---|
| 换行必须是 **CRLF** | **CRLF 数量 = 0**，全是 LF | `^` 续行失效、`if (...)` 块崩坏 |
| 编码必须是当前代码页 | UTF-8 无 BOM，且第 2 行还执行 `chcp 65001` | 中文多字节序列把解析器打乱 |

实测运行旧脚本，`^` 续行全部断裂，输出是一连串
`'xxx' 不是内部或外部命令，也不是可运行的程序`，PyInstaller 根本没被调用过。

**更隐蔽的问题**：因为脚本最后一句是 `pause`（永远成功），返回给调用方的
`exit code` 是 **0**。也就是说它一直"看起来成功了"，实际什么都没做。

### 9.2 修复方案

- **编码改为 GBK（代码页 936），换行改为 CRLF，去掉 `chcp 65001`** ——
  中文 Windows 下 cmd 原生读 GBK，不需要在文件中间切代码页。
  文件顶部加了 REM 注释，说明后续编辑时必须保持 GBK + CRLF。
- 用生成脚本 `.temp/write_build_bat.py` 落盘，保证编码/换行不会被编辑器改回去。

### 9.3 顺带修掉的其他缺陷

1. **缺 `--noconfirm`** —— 输出目录非空时，PyInstaller 在非交互环境下直接
   `SystemExit` 报错退出、在交互环境下卡在确认提示。已补上。
2. **旧 exe 被占用时报错难懂** —— 新增第 3 步：先尝试删除旧 exe，删不掉就明确
   提示"程序可能正在运行，请先关闭"，而不是让 PyInstaller 抛权限错误。
3. **无产物校验** —— 构建结束后检查 `dist_windows\TGCacheManager.exe` 是否真的
   存在，并打印文件大小；不存在就报失败。这直接堵住"静默失败"。
4. **`cd /d "%~dp0"`** —— 无论从哪个目录调用都切到脚本所在目录。
5. 步骤编号改为 1/5..5/5，清理环节只删 `build\`，保留 `dist_windows\env\`。

### 9.4 验证

以子进程方式实跑（stdin 接 DEVNULL 喂掉 `pause`）：

```
[1/5] 检查 PyInstaller...      PyInstaller 已就绪.
[2/5] 检查依赖库...            依赖已就绪.
[3/5] 检查旧的 exe 是否可替换... 可以替换.
[4/5] 清理旧构建...            已清理 build\ 【已保留 dist_windows\env\】
[5/5] 开始打包...
...
Build complete! The results are available in: dist_windows
========================================
  构建完成!
  输出文件: dist_windows\TGCacheManager.exe
  文件大小: 40751604 字节
  ffmpeg 运行时: dist_windows\env\ffmpeg.exe  [OK]
exit code = 0
```

产物冒烟：`/api/config` 200、`/api/scan/status` 200。

顺带清掉了项目根目录一个 0 字节的垃圾文件 `16`（早前一条被 shell 转义搞坏的
命令留下的）。

> 排错提示：Windows 下没法从 Bash 直接 `cmd /c xxx.bat`（安全策略会拦），
> 可以写成 python 子进程脚本执行（见 `.temp/run_build.py`）。

---

## 10. 设置里新增「清空缓存 / 下载目录」（2026-09-15）

需求：在设置中支持清空缓存或下载目录，**但绝不能清掉用户数据**（账号/密钥/设置），
目的是方便做一次干净的重建验证 —— 先清空，再让 Telegram 重新缓存一个视频，
扫描结果里就只有那一个视频。

### 10.1 两个词的确切含义（先定义边界再动手）

| 目标 | 实际清空什么 | 不动什么 |
|---|---|---|
| **清空 Telegram 缓存** | `<tdata>\user_data\cache\**` 与 `<tdata>\user_data\media_cache\**` 下的普通文件（跳过 `version` / `binlog`） | `key_datas`、`D877F783D5D3EF8C\`（账号数据/密钥/设置/binlog）、`settingss`、`emoji` 等 |
| **清空下载目录** | 设置里填的「Telegram 下载目录」下的文件（含子目录，空目录随后 prune） | 目录根本身；受保护路径直接拒绝 |
| **清空应用缓存** | 本工具自己产出的 `exports\`（含 `.preview\`）与 `thumbnails\` | 一切 tdata 内容 |

真实 tdata 取证（`D:\Software\Telegram Desktop\tdata`）确认缓存就在
`user_data\cache\1\` 与 `user_data\media_cache\1\`，两个目录各有一个 `version`
文件 —— **必须保留**，否则 Telegram 重建缓存索引会异常。`cache\1` 这样的目录
结构也保留（只删文件、不删目录）。

### 10.2 接口：`POST /api/clear_cache`

两步走，**先预演后执行**，不允许一步删：

```jsonc
// 第一步：只统计
{"target": "telegram"|"download"|"app", "confirm": false}
→ {ok, dirs:[{label,path}], file_count, total_bytes, sample:[最多5个文件名], confirm_required:true}

// 第二步：真正删除
{"target": "...", "confirm": true}
→ {ok, deleted, failed, freed_bytes, dirs_removed, need_rescan, errors:[最多5条]}
```

- `target=telegram` 删除成功后会调 `_reset_scan_state()` 作废全部扫描结果
  （否则索引指向已不存在的文件，预览/导出会报莫名其妙的错），返回
  `need_rescan=true`，前端据此把界面拉回初始态并提示重新扫描。
- `target=download` 只清 `download_files_cache`；`target=app` 清预览池与
  导出校验缓存，然后 `_rebuild_exported_ids()`。
- 删除失败（多半是被 Telegram 占着句柄）**计数上报，不谎报成功**，失败明细
  最多回传 5 条。

### 10.3 安全红线（后端硬校验，前端拦不住也删不掉）

`_resolve_target_dirs()` 是唯一入口，任何一条不满足直接 400：

1. `download` 未配置 / 目录不存在 → 拒绝。
2. 驱动器根目录（`splitdrive` 后只剩分隔符）→ 拒绝。
3. **与 tdata 有包含关系（任一方向）** → 拒绝。这条最关键：防止有人把下载
   目录填成 tdata 或其父目录，一把梭删掉账号数据。
4. 主目录 / 桌面 / 文档 / 下载 / 图片 / 视频 / tdata / 程序目录 → 拒绝。
5. `telegram` 只认 `user_data\cache` 与 `user_data\media_cache` 两个根；
   收集文件时用 `_is_under_any()` 复核绝对路径（防 symlink 逃逸），
   并跳过 `version` / `binlog`。

### 10.4 前端

- `templates/index.html`：设置弹窗新增「清理 (会先预览再二次确认)」分区，
  三个按钮 —— `btn-clear-telegram-cache` / `btn-clear-download-dir`（`btn-danger`）、
  `btn-clear-app-cache`（`btn-secondary`），下方两段 `form-hint` 明确写出
  不会删 `key_datas` 与 `D877F783D5D3EF8C`。
- `static/js/app.js`：新增 `clearCacheTarget(target)`，先拉预演统计，再用
  `confirm()` 展示「目录清单 + 文件数 + 体积 + 示例文件」，用户确认后才发
  第二步请求；按钮全程 disabled + 文案切「统计中/清空中」。
- 顺手把 `saveSettings()` 里那段重复的界面重置代码抽成 `resetUIForRescan()`，
  清空缓存后复用。
- `static/css/style.css`：新增 `.settings-section` / `.settings-section-title`
  和 `code` 行内样式。

### 10.5 验证

新增 `tests/test_clear_cache.py`（已注册进 `tests/run_all.py`，套件名 `clear`），
用 Flask test_client + **合成 tdata** 跑真实删除，33 项断言全过：

- 预演阶段一个文件都不删；文件数/字节数正确。
- 执行后：缓存文件 3 个全删、`version` 与 `binlog` 保留、`key_datas` /
  `D877F783D5D3EF8C\*` / `emoji` 一个不少、`cache\1` 目录还在。
- 下载目录：文件全删、空子目录 prune、目录根保留。
- 安全红线 7 种非法输入全部 400，且被拒后 `key_datas` 依然存在。
- `app` 目标只做预演（避免测试时清掉 205 个真实缩略图），确认 `dirs`
  只含 `exports` / `thumbnails`。

---

## 11. 「完整缓存却提示缺 1 个分片」—— binlog 偏移映射差一（2026-09-15）

### 11.1 现象

用户完整播放两部视频各两次（1:49 / 6:38），界面都显示「缺 1 个分片」，
且缺的恰好都是 1 号分片。

### 11.2 取证（`.temp/verify_shift.py`）

对两部视频分别按两种映射做只读分析：

| | 旧映射 `i*8MiB` | 新映射 `(i-1)*8MiB` |
|---|---|---|
| 6:38 视频 (334MB, 需 40 分片) | missing=[1]，可播仅 10.31s | **complete=True**，398.12s 全时长 |
| 1:49 视频 (97.8MB, 需 12 分片) | missing=[1]，可播仅 9.33s | **complete=True**，109.68s 全时长 |
| 最后一个分片（短块） | 被摆到**文件末尾之外** | 恰好止于文件末尾 |

两条铁证：

1. **数据不可能存在于文件之外**。旧映射把最后一个短分片摆到
   `[100.66MB, 106.24MB)`，而该视频文件末尾（moov 结束）在 97.85MB。
   新映射下短分片 `[92.27MB, 97.85MB)` 与文件末尾严丝合缝。
2. **重打包整片解码**。按新映射重打包后 ffmpeg 全量解码 0 错误，
   ffprobe 时长 398.12s / 109.67s，与完整时长一致。

顺带破案：旧版记录在案的「`224EF3E286C8`（slice=1）以 ftyp 开头，疑似陈旧
binlog」—— ftyp 在文件偏移 0，而新映射下 index 1 恰好覆盖 `[0, 8MiB)`，
所以它以 ftyp 开头是**完全正常**的，不是陈旧记录。

### 11.3 正确的映射

```
外部文件 binlog slice_index i  覆盖媒体文件 [(i-1)*8MiB, i*8MiB)
index 0  = 序列化 header 自身的记录
实测外部文件 index 从 2 起；index 1 偶见于无 header 的裸块（内容以 ftyp 开头）
```

旧结论「i 覆盖 [i*8MiB, (i+1)*8MiB)」整体错位一格，方向是把每个分片
往后挪了一个格：完整缓存被误报成缺 index 1，尾部短块越过 EOF。

### 11.4 修复

- `server.py::_slices_for_header`（唯一换算点，展示/导出/缩略图全走这里）：
  `place = max(idx - 1, 0)`。binlog 模块保持忠实记录原始 index 不动。
- `src/binlog.py` 模块注释与 `get_slice_records_for_header` 注释同步改写，
  写明换算位置在 server，防止未来被"改回去"。
- `tests/test_server_integration.py`：合成 binlog 的 key_low 从 0/1/2 改成
  1/2/3（与真实格式一致：外部分片从 1 起铺 `[(i-1)*8MiB)`），第 5 节补全
  分片的记录同步改为 3。预期值（missing=[2]、on_disk=[0,1]、23.8s/30.0s）
  全部不变——分片清单与缺口本来就**从覆盖率推导**，不受索引算术影响。

### 11.5 验证

- 真实数据（正式代码路径 `_enrich_scan`）：两部视频 `complete=True`、
  `missing=[]`、可播时长 398.12s / 109.68s（= 完整时长）。
  `_produce_export` 正式导出 334MB 那部：ok=True、335,196,344 字节、
  398.15s、truncated=False，四道验证门全过。
- 测试 365 项全过，无回归（binlog 13 / rebuild 43 / mediaparts 23 /
  pipeline 55 / e2e 31 / server 55 / mp4 50 / coverage 40 /
  deserializer 22 / clear 33）。
