$ports = 8000, 8001, 8811, 8812

foreach ($port in $ports) {
    $connections = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    $processIds = $connections | Select-Object -ExpandProperty OwningProcess -Unique
    foreach ($processId in $processIds) {
        $process = Get-Process -Id $processId -ErrorAction SilentlyContinue
        if ($process) {
            Write-Host "停止端口 $port：$($process.ProcessName) PID=$processId"
            Stop-Process -Id $processId -Force
        }
    }
}
