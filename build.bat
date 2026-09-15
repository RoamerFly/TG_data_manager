@echo off
setlocal
REM ============================================================
REM  Telegram Desktop 缓存数据管理器 - Windows 构建脚本
REM
REM  编码要求: 本文件必须是 GBK(代码页 936) + CRLF 换行。
REM    - 存成 UTF-8 会让中文变乱码; 存成 LF 换行会让 ^ 续行和 if (...) 块失效。
REM    - 用记事本/VS Code 另存时请显式选择 "GBK" 编码与 "CRLF" 换行符。
REM  输出: dist_windows\TGCacheManager.exe
REM ============================================================

cd /d "%~dp0"

set "PYTHON=E:\A-Environment\Miniconda3\python.exe"
if not exist "%PYTHON%" set "PYTHON=python"

echo ========================================
echo   Telegram 缓存管理器 - 构建脚本
echo ========================================
echo.

REM ---- 第1步: 检查/安装 PyInstaller ----
echo [1/5] 检查 PyInstaller...
"%PYTHON%" -c "import PyInstaller" >nul 2>&1
if errorlevel 1 (
    echo       未找到 PyInstaller, 正在安装...
    "%PYTHON%" -m pip install pyinstaller
    if errorlevel 1 (
        echo       PyInstaller 安装失败!
        pause
        exit /b 1
    )
)
echo       PyInstaller 已就绪.

REM ---- 第2步: 检查依赖库 ----
echo.
echo [2/5] 检查依赖库...
"%PYTHON%" -c "import flask, tgcrypto, Crypto, PIL" >nul 2>&1
if errorlevel 1 (
    echo       正在安装依赖...
    "%PYTHON%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo       依赖安装失败!
        pause
        exit /b 1
    )
)
echo       依赖已就绪.

REM ---- 第3步: 旧的 exe 不能被占用 ----
echo.
echo [3/5] 检查旧的 exe 是否可替换...
if exist "dist_windows\TGCacheManager.exe" (
    del /f /q "dist_windows\TGCacheManager.exe" >nul 2>&1
    if exist "dist_windows\TGCacheManager.exe" (
        echo.
        echo   无法删除 dist_windows\TGCacheManager.exe
        echo   它很可能正在运行。请先关闭程序窗口, 或在任务管理器里
        echo   结束 TGCacheManager.exe 后再重新构建。
        echo.
        pause
        exit /b 1
    )
)
echo       可以替换.

REM ---- 第4步: 清理旧构建 ----
REM 注意: dist_windows\env\ 里放着 ffmpeg.exe / ffprobe.exe, 属于外部下载的
REM       运行时依赖, 绝不能随构建一起删掉。这里只清 build\。
echo.
echo [4/5] 清理旧构建...
if exist build rmdir /s /q build
if exist build (
    echo       build\ 清理失败, 可能文件被占用。跳过清理继续构建。
) else (
    echo       已清理 build\ 【已保留 dist_windows\env\】
)

REM ---- 第5步: PyInstaller 打包 ----
REM --noconfirm: 输出目录非空时直接覆盖, 不弹确认。
REM              缺了这个, 非交互环境下 PyInstaller 会直接报错退出。
echo.
echo [5/5] 开始打包, 请稍候 (约 1-2 分钟)...
echo.
"%PYTHON%" -m PyInstaller ^
    --noconfirm ^
    --onefile ^
    --windowed ^
    --name "TGCacheManager" ^
    --distpath dist_windows ^
    --workpath build ^
    --add-data "src;src" ^
    --add-data "templates;templates" ^
    --add-data "static;static" ^
    --hidden-import "tgcrypto" ^
    --hidden-import "Crypto" ^
    --hidden-import "Crypto.Cipher.AES" ^
    --hidden-import "Crypto.Util.Counter" ^
    --hidden-import "PIL" ^
    --hidden-import "PIL.Image" ^
    --hidden-import "PIL.ImageFile" ^
    --hidden-import "flask" ^
    --hidden-import "flask.json" ^
    --hidden-import "webview" ^
    --hidden-import "webview.platforms.edgechromium" ^
    --hidden-import "webview.platforms.winforms" ^
    --hidden-import "clr" ^
    --hidden-import "System.Windows.Forms" ^
    --hidden-import "System.Threading" ^
    server.py

if errorlevel 1 (
    echo.
    echo ========================================
    echo   构建失败!
    echo ========================================
    pause
    exit /b 1
)

REM ---- 产物校验 (防止"看着成功其实没产出") ----
if not exist "dist_windows\TGCacheManager.exe" (
    echo.
    echo   构建流程走完了, 但 dist_windows\TGCacheManager.exe 不存在!
    echo   请检查上面的 PyInstaller 输出。
    pause
    exit /b 1
)

REM 清理中间文件
if exist build rmdir /s /q build

echo.
echo ========================================
echo   构建完成!
echo ========================================
echo.
echo   输出文件: dist_windows\TGCacheManager.exe
for %%I in ("dist_windows\TGCacheManager.exe") do echo   文件大小: %%~zI 字节
echo.

REM ---- ffmpeg 运行时依赖检查 ----
if not exist "dist_windows\env\ffmpeg.exe" (
    echo   [警告] 未找到 dist_windows\env\ffmpeg.exe
    echo          视频重建/转封装将不可用, 只能导出原始字节。
    echo          请把 ffmpeg.exe 与 ffprobe.exe 放到 dist_windows\env\ 下。
) else (
    echo   ffmpeg 运行时: dist_windows\env\ffmpeg.exe  [OK]
)
echo.
echo   使用方式: 双击 TGCacheManager.exe 即可启动
echo   浏览器会自动打开 http://127.0.0.1:5000
echo   导出文件保存在 exe 同级的 exports\ 目录
echo.
pause
endlocal
