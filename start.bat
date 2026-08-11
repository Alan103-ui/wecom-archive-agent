@echo off
chcp 65001 >nul
cd /d %~dp0

if not exist ".venv\Scripts\python.exe" (
    echo [1/2] 首次运行，正在创建虚拟环境...
    python -m venv .venv
    .venv\Scripts\python.exe -m pip install --upgrade pip -q
    echo [2/2] 安装依赖（首次约 2-5 分钟）...
    .venv\Scripts\python.exe -m pip install -r requirements.txt -i https://mirrors.cloud.tencent.com/pypi/simple
)

if not exist ".env" (
    if exist ".env.example" (
        copy /y ".env.example" ".env" >nul
        echo 已从 .env.example 生成 .env，默认 mock 演示模式
    )
)

echo.
echo ============================================
echo  企业微信会话存档智能体
echo  管理页面: http://127.0.0.1:8002
echo  接口文档: http://127.0.0.1:8002/docs
echo  按 Ctrl+C 停止
echo ============================================
echo.

.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8002
pause
