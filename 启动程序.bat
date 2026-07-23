@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0code"

where py >nul 2>nul
if %errorlevel%==0 (
    set "PYTHON_CMD=py -3"
) else (
    set "PYTHON_CMD=python"
)

if not exist ".venv\Scripts\python.exe" (
    echo [1/3] 正在创建独立运行环境...
    %PYTHON_CMD% -m venv .venv
    if errorlevel 1 goto :python_error

    echo [2/3] 正在安装程序依赖，首次运行需要几分钟...
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 goto :install_error
    ".venv\Scripts\python.exe" -m pip install -r visualization\requirements.txt
    if errorlevel 1 goto :install_error
)

echo [3/3] 正在启动多智能体教案磨课系统...
".venv\Scripts\python.exe" -m streamlit run visualization\app.py
goto :end

:python_error
echo.
echo 未找到可用的 Python 3，请安装 Python 3.11 或更高版本并加入 PATH。
pause
goto :end

:install_error
echo.
echo 依赖安装失败，请检查网络后重新双击启动程序。
pause

:end
endlocal
