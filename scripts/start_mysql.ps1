$mysqlExe = 'C:\Program Files\MySQL\MySQL Server 8.4\bin\mysqld.exe'
$defaultsFile = 'C:\workspace\AIpaddle\config\mysql.ini'

if (-not (Get-Process mysqld -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $mysqlExe })) {
    Start-Process -FilePath $mysqlExe -ArgumentList "--defaults-file=$defaultsFile" -WorkingDirectory (Split-Path $mysqlExe) -WindowStyle Hidden
}
