param(
    [Parameter(Mandatory = $true)][string]$Executable,
    [string]$ExpectedVersion = "1.3.0",
    [string]$Report = "dist/windows/WINDOWS_SMOKE_REPORT.json"
)

# Exercise only the built EXE in a clean Unicode/space-containing directory.
# Remove Python-related PATH/environment entries from each EXE process.
$ErrorActionPreference = "Stop"
$watch = [Diagnostics.Stopwatch]::StartNew()
$originalExe = (Resolve-Path $Executable).Path
$scratch = Join-Path $env:RUNNER_TEMP ("Gateway package 测试 " + [Guid]::NewGuid().ToString("N"))
$root = Join-Path $scratch "installation data"
New-Item -ItemType Directory -Path $scratch -Force | Out-Null
$exe = Join-Path $scratch "IndustrialAIGateway.exe"
Copy-Item -LiteralPath $originalExe -Destination $exe
$server = $null
$occupied = $null
$checks = [ordered]@{}
$result = [ordered]@{
    version = $ExpectedVersion
    os = "Windows x64"
    executable = "IndustrialAIGateway.exe"
    passed = $false
    python_removed_from_child_path = $true
    checks = $checks
}

function Assert-Condition([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
}

function Start-Gateway([string[]]$Arguments) {
    $info = [Diagnostics.ProcessStartInfo]::new()
    $info.FileName = $exe
    $info.WorkingDirectory = $scratch
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    foreach ($argument in $Arguments) { $info.ArgumentList.Add($argument) }
    $info.Environment["PATH"] = "$env:SystemRoot\System32;$env:SystemRoot"
    foreach ($name in @("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV")) {
        $info.Environment.Remove($name) | Out-Null
    }
    return [Diagnostics.Process]::Start($info)
}

function Invoke-Gateway([string[]]$Arguments) {
    $process = Start-Gateway -Arguments $Arguments
    try {
        Assert-Condition ($process.WaitForExit(90000)) "EXE command exceeded 90 seconds."
        Assert-Condition ($process.ExitCode -eq 0) ("EXE command returned " + $process.ExitCode)
    }
    finally { $process.Dispose() }
}

function Get-FreePort {
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
    try {
        $listener.Start()
        return $listener.LocalEndpoint.Port
    }
    finally { $listener.Stop() }
}

function Wait-ForCurrent([int]$Port, [Diagnostics.Process]$Process) {
    $deadline = [DateTime]::UtcNow.AddSeconds(60)
    while ([DateTime]::UtcNow -lt $deadline) {
        Assert-Condition (-not $Process.HasExited) "EXE exited before healthy collection."
        try {
            $health = Invoke-RestMethod "http://127.0.0.1:$Port/api/health" -TimeoutSec 3
            $current = Invoke-RestMethod "http://127.0.0.1:$Port/api/current" -TimeoutSec 3
            if ($health.version -eq $ExpectedVersion -and $current.total -eq 6 -and $current.good -eq 6) {
                return $current
            }
        }
        catch { }
        Start-Sleep -Milliseconds 250
    }
    throw "EXE did not provide six healthy simulation points within 60 seconds."
}

function Wait-ForHistory([int]$Port) {
    $deadline = [DateTime]::UtcNow.AddSeconds(20)
    while ([DateTime]::UtcNow -lt $deadline) {
        $history = Invoke-RestMethod "http://127.0.0.1:$Port/api/history?source=simulation&limit=10" -TimeoutSec 3
        if ($history.total -ge 5) { return $history }
        Start-Sleep -Milliseconds 200
    }
    throw "Simulation baselines were not persisted."
}

try {
    Invoke-Gateway -Arguments @("--root", $root, "--check")
    $checkPath = Join-Path $root "check-result.json"
    Assert-Condition (Test-Path -LiteralPath $checkPath) "Windowed EXE must write check-result.json."
    $check = Get-Content -LiteralPath $checkPath -Raw -Encoding UTF8 | ConvertFrom-Json
    Assert-Condition ($check.ok -and $check.standalone -and $check.read_only) "Standalone check failed."
    Assert-Condition ($check.mode -eq "simulation" -and -not $check.database_exists) "First check must not initialize history."
    Assert-Condition (-not (Test-Path (Join-Path $root "data/history.db"))) "Check created a database."
    $checks.standalone_check_without_history = $true

    $port = Get-FreePort
    $server = Start-Gateway -Arguments @("--root", $root, "--no-browser", "--port", "$port")
    $current = Wait-ForCurrent $port $server
    Assert-Condition ($current.connected -and $current.mode -eq "simulation" -and -not $current.write_enabled) "Unsafe first-run defaults."
    Assert-Condition (@($current.items | Where-Object { $_.quality -ne "Good" -or $null -eq $_.value }).Count -eq 0) "Missing values or bad quality."
    foreach ($type in @("BOOL", "WORD", "DWORD", "FLOAT")) {
        Assert-Condition (@($current.items | Where-Object type -EQ $type).Count -gt 0) "Missing sample type $type."
    }
    $checks.six_good_points_and_four_types = $true
    $health = Invoke-RestMethod "http://127.0.0.1:$port/api/health" -TimeoutSec 3
    Assert-Condition ($health.app -eq "Industrial AI Gateway" -and $health.version -eq $ExpectedVersion) "Unexpected executable version."
    $page = Invoke-WebRequest "http://127.0.0.1:$port/" -TimeoutSec 5
    Assert-Condition ($page.StatusCode -eq 200 -and $page.Content.Contains("PLC")) "Offline UI was not bundled."
    $vue = Invoke-WebRequest "http://127.0.0.1:$port/vendor/vue.global.prod.js" -TimeoutSec 5
    Assert-Condition ($vue.StatusCode -eq 200 -and $vue.RawContentLength -gt 10000) "Offline Vue was not bundled."
    $checks.bundled_offline_interface = $true

    $template = Join-Path $scratch "template.xlsx"
    Invoke-WebRequest "http://127.0.0.1:$port/api/tags/template" -OutFile $template -TimeoutSec 5
    $workbook = [IO.Compression.ZipFile]::OpenRead($template)
    try {
        Assert-Condition ($null -ne $workbook.GetEntry("xl/worksheets/sheet1.xml")) "Invalid bundled Excel export."
    }
    finally { $workbook.Dispose() }
    $checks.excel_template_download = $true

    $live = @($current.items | Where-Object { -not $_.save })[0]
    $body = @{ question = "当前值"; variable = $live.name } | ConvertTo-Json -Compress
    $answer = Invoke-RestMethod "http://127.0.0.1:$port/api/ai/query" -Method Post -ContentType "application/json; charset=utf-8" -Body ([Text.Encoding]::UTF8.GetBytes($body)) -TimeoutSec 5
    Assert-Condition ($answer.provider -eq "local_rules" -and $answer.query_type -eq "current" -and $answer.items.Count -eq 1 -and -not $answer.plc_write_allowed) "On-demand live query failed."
    $checks.live_only_on_demand_query = $true
    $history = Wait-ForHistory $port
    Assert-Condition (Test-Path (Join-Path $root "data/history.db")) "SQLite history was not created."
    $checks.sqlite_history_persisted = $true

    $configPath = Join-Path $root "config/config.json"
    $configurationHash = (Get-FileHash -LiteralPath $configPath -Algorithm SHA256).Hash
    $config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
    Assert-Condition ($config.username -eq "" -and $config.security_string -eq "" -and -not $config.simulation_write_enabled) "Credentials or writes appeared in defaults."
    Invoke-Gateway -Arguments @("--root", $root, "--no-browser", "--port", "$port")
    Assert-Condition (-not $server.HasExited) "Repeated launch stopped the original instance."
    $checks.repeated_launch_reuses_instance = $true
    Invoke-Gateway -Arguments @("--root", $root, "--stop")
    Assert-Condition ($server.WaitForExit(10000) -and $server.ExitCode -eq 0) "CLI stop did not shut down the EXE cleanly."
    Assert-Condition (-not (Test-Path (Join-Path $root "data/gateway.pid"))) "Stop left owner metadata."
    $server.Dispose()
    $server = $null
    $checks.graceful_cli_stop = $true

    # Occupy the default port: normal double-click startup must select another one.
    $occupied = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 8080)
    try { $occupied.Start() }
    catch { $occupied.Stop(); $occupied = $null } # An existing process also occupies 8080.
    $server = Start-Gateway -Arguments @("--root", $root, "--no-browser")
    $metadataPath = Join-Path $root "data/desktop-instance.json"
    $deadline = [DateTime]::UtcNow.AddSeconds(60)
    while (-not (Test-Path $metadataPath) -and [DateTime]::UtcNow -lt $deadline) {
        Assert-Condition (-not $server.HasExited) "Restart exited before opening the interface."
        Start-Sleep -Milliseconds 250
    }
    $metadata = Get-Content -LiteralPath $metadataPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $port = [int]$metadata.port
    Assert-Condition ($port -ge 8081 -and $port -le 8099) "Occupied default port was not skipped."
    $current = Wait-ForCurrent $port $server
    Assert-Condition ((Get-FileHash -LiteralPath $configPath -Algorithm SHA256).Hash -eq $configurationHash) "Restart replaced configuration."
    $retained = Wait-ForHistory $port
    Assert-Condition ($retained.total -ge $history.total) "Restart lost saved history."
    $checks.port_fallback_and_restart_persistence = $true

    $refused = $false
    try { Invoke-RestMethod "http://127.0.0.1:$port/api/shutdown" -Method Post -TimeoutSec 5 | Out-Null }
    catch { $refused = [int]$_.Exception.Response.StatusCode -eq 403 }
    Assert-Condition $refused "UI shutdown must require the local management PIN."
    $pin = (Get-Content -LiteralPath (Join-Path $root "data/operator_pin.txt") -Raw -Encoding UTF8).Trim()
    $shutdown = Invoke-RestMethod "http://127.0.0.1:$port/api/shutdown" -Method Post -Headers @{ "X-Operator-Pin" = $pin } -TimeoutSec 5
    Assert-Condition ($shutdown.status -eq "stopping") "UI shutdown was not accepted."
    Assert-Condition ($server.WaitForExit(45000) -and $server.ExitCode -eq 0) "UI shutdown did not exit cleanly."
    Assert-Condition (-not (Test-Path (Join-Path $root "data/gateway.pid")) -and -not (Test-Path $metadataPath)) "Shutdown left active instance metadata."
    $checks.authorized_ui_shutdown = $true
    $result.passed = $true
    Write-Output "Windows standalone EXE smoke passed: $($checks.Count) checks."
}
finally {
    if ($server -and -not $server.HasExited) {
        try { Invoke-Gateway -Arguments @("--root", $root, "--stop") }
        catch { Write-Warning "EXE cleanup requested; it did not finish. No process was killed." }
    }
    if ($server) { $server.Dispose() }
    if ($occupied) { $occupied.Stop() }
    $watch.Stop()
    $result.elapsed_seconds = [Math]::Round($watch.Elapsed.TotalSeconds, 2)
    $reportPath = [IO.Path]::GetFullPath($Report)
    [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($reportPath)) | Out-Null
    $result | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $reportPath -Encoding UTF8
}
