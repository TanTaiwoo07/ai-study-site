@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo.
echo   论文精读助手
echo   ------------------------------------------
echo   正在启动本地服务，稍后浏览器会自动打开
echo   用完直接关掉这个窗口即可
echo.

where python >nul 2>nul
if %errorlevel%==0 (
    python serve.py
    goto :end
)

where py >nul 2>nul
if %errorlevel%==0 (
    py serve.py
    goto :end
)

echo   [错误] 没找到 Python。
echo.
echo   请先安装 Python，或者直接双击 reader.html
echo   （直接打开时「一键打开论文」用不了，只能手动选文件）
echo.
pause

:end
