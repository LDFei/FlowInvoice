# dev_async.ps1 —— 一键起 Celery worker + beat（异步模式，docs/06 §2 / README「运行异步模式」）
# 前置：1) .env 设 FLOWINVOICE_ASYNC=1（本脚本不代改配置）；2) docker compose 起 redis（broker）。
# 作用：worker 消费 submissions 队列执行发票处理；beat 驱动周期回收（reclaim_stuck 每 60s 扫卡死任务）。
# 注意：Celery 在 Windows 上不支持 `-B`/`--beat` 内嵌（"-B option does not work on Windows"），
#       必须把 beat 作为独立进程启动 —— 本脚本：beat 以同控制台后台进程拉起，worker 前台跑，
#       worker 结束（Ctrl+C / 崩溃）后自动停掉 beat。分开起两个终端亦可：
#       worker（无 -B）+ `python -m celery -A app.celery_app beat`。
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)   # scripts/ 上一级 = 项目根

$VenvActivate = Join-Path (Get-Location) ".venv\Scripts\Activate.ps1"
if (-not (Test-Path $VenvActivate)) {
    Write-Error "未找到 $VenvActivate —— 请先创建 .venv 并 pip install -r requirements.txt"
    exit 1
}
& $VenvActivate

$Py = (Get-Command python).Source   # 激活后 = .venv\Scripts\python.exe

# -P solo：Windows 无 fork，用 solo 池跑并行重活单元即可（异步任务内另有共享线程池做批内并行）
Write-Host "== FlowInvoice async worker + beat（Ctrl+C 退出）==" -ForegroundColor Cyan

# 先起 beat（后台、共享当前控制台输出 beat 日志；独立进程是 Windows 下唯一可行方式）
$Beat = Start-Process -FilePath $Py -ArgumentList @("-m", "celery", "-A", "app.celery_app", "beat", "--loglevel=INFO") -NoNewWindow -PassThru
try {
    # worker 前台同步运行：Ctrl+C 时与 beat 同收中断，退出后走 finally 兜底停 beat
    & $Py -m celery -A app.celery_app worker -P solo --loglevel=INFO
} finally {
    if (-not $Beat.HasExited) {
        Stop-Process -Id $Beat.Id -Force -ErrorAction SilentlyContinue
        Write-Host "beat 已随 worker 停止。" -ForegroundColor DarkGray
    }
}
