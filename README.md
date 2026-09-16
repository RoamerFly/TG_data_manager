# Telegram 缓存管理器

一个面向 **Telegram Desktop 本地缓存** 的管理工具：扫描 `tdata` 里的缓存碎片，解密还原成图片、视频、音频、贴纸，重建被分片存储的大视频，并把能救回来的部分诚实导出成可播放文件。

全部操作在本地完成，**不联网、不上传、不触碰账号登录态**。

---

## 它能做什么

| 能力 | 说明 |
|---|---|
| 扫描与浏览 | 读取 tdata 缓存目录，支持本地密码（Passcode）；按类型/大小/名称筛选、搜索、分页 |
| 预览 | 图片、视频（含流式 Range）、音频、TGS 动画贴片；未知碎片显示十六进制头部 |
| 大视频重建 | Telegram 把大视频切成 8MB 分片存储，工具按 **字节覆盖率** 判断完整性并重打包，`moov` 前置后即可直接播放 |
| 诚实导出 | 缺数据时**只导出连续覆盖到的那一段**并明确标注时长，不用零填充伪造文件 —— 播放不会中途卡死或黑屏 |
| 回到 Telegram 补全 | 从缓存文件反推真实 `document_id`，跳转到原始消息播放以补全缓存分片；支持相册消息精确定位 |
| 批量操作 | 一键重建全部完整大视频、批量导出、多选删除、重复文件检测、孤儿文件清理 |
| 安全清理 | 清空 Telegram 缓存 / 下载目录 / 应用缓存，两步确认，账号数据与聊天记录绝不被删 |

## 技术要点

工具的价值不在 UI，而在这几处**取证级**的判断：

- **序列化格式**：缓存头的明文结构是 `[u32 part_count] + ([u32 out_offset][u32 size][data]) * N`，`out_offset` 是媒体文件里的绝对偏移（128KB 对齐），且**不连续** —— 先取头、再取尾拿 `moov` 是常态。
- **分片归组口径**：`key_low = document_id(48bit) << 16 | slice_index(16bit)`，同一部视频的标识是 `(key_high, key_low >> 16)`。只按 `key_high` 归组会把不同视频缝到一起。
- **偏移映射**：外部文件 binlog index `i` 覆盖 `[(i-1)*8MiB, i*8MiB)`。
- **完整性的唯一判据**：字节覆盖区间集合是否覆盖 `moov` 必需区段，而不是"分片数量够不够"。
- **导出四道验证门**：容器识别 → box 结构 → ffprobe 时长 → 有界解码，全过才落最终文件名；失败必须返回可读原因（含缺失分片清单）。
- **Telegram 跳转**：`real_doc_id = ((key_high & 0xFFFF) << 48) | (key_low >> 16)`；相册消息需追加 `&single&t=N` 才能定位到其中的第 N 个媒体。

## 运行

### 环境

- Python 3.9+（推荐 3.12）
- Windows（目录与 Telegram Desktop 路径按 Windows 约定）

### 开发模式

```bash
pip install -r requirements.txt
python server.py
# 浏览器打开 http://127.0.0.1:5000
```

Windows 下也可以直接双击 `run.bat`（自动装依赖并启动）。

### 打包成 EXE

```bash
build.bat
```

产物在 `dist_windows/TGCacheManager.exe`。转封装需要的 `ffmpeg.exe` / `ffprobe.exe`
放在 `dist_windows/env/`，打包时不要清理该目录。

## 目录结构

```
server.py               Flask 后端，唯一的导出入口在其中
src/
  scanner.py            tdata 扫描与解密
  deserializer.py       序列化格式解析 → 字节区段
  binlog.py             binlog 解析、分片归组、真实 document_id 还原
  mp4.py                ISOBMFF box 遍历、样本表截断
  coverage.py           字节覆盖区间 —— 完整性的唯一权威定义
  rebuild.py            流式重打包
  export_pipeline.py    四道验证门 + sidecar + 原子提交
  crypto.py             tdata 密钥派生与解密
  exporter.py           转封装 / 缩略图生成
  locations.py          locations + downloads 索引（消息与来源反查）
static/  templates/     前端
tests/                  测试套件
```

设计约定：格式解析、box 层、覆盖率、重建、导出各司其职，**不做跨层猜测**。

## 设置

首次使用需在「设置」中指定：

- **tdata 路径**：如 `D:\Software\Telegram Desktop\tdata`
- **下载目录**（可选）
- **本地密码**（仅当 Telegram 设置了 Passcode 时填写）

> 扫描时若 Telegram Desktop 正在运行，binlog 被锁定，会拿不到真实文件名和跳转信息 —— 界面会给出黄色提示条，关闭 Telegram 后重新扫描即可。

## 隐私与安全

- 所有解析、解密都在本机内存里进行，没有任何数据离开这台电脑。
- 清理功能有硬性红线：与 tdata 存在包含关系、驱动器根目录、用户主目录/桌面/文档/下载等路径一律拒绝。
- `key_datas`、`D877F783D5D3EF8C`（账号/密钥/设置）、聊天记录与登录态**永不被删除**。
- 绑定文件 `telegram_links.json` 含个人来源链接，已在 `.gitignore` 中排除，不会入库。

## 已知限制

- **仅在线播放过、未下载到本地的视频**，Telegram 不会在本地留存消息关联，因此无法自动反查来源消息。这类文件需要在预览弹窗里粘贴一次原始链接完成绑定（绑定按 `document_id` 存储，重新扫描依然有效）。
- 私聊消息 Telegram 不提供 `tg://` 跳转 URL，只能启动客户端。
- 连 `moov`（索引）都没缓存下来的视频，无法生成可播放文件。

## 测试

```bash
python tests/run_all.py            # 全套
python tests/run_all.py mp4 e2e    # 选择套件
```

夹具由脚本合成生成，测试**不需要真实 Telegram 数据**。

## 许可

内部自用工具。使用前请自行备份 `tdata`。
