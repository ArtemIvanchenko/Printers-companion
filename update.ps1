# Windows wrapper. Bootstraps a private checksummed Python if needed.
param([Parameter(ValueFromRemainingArguments=$true)][string[]]$UpdateArguments)
$ErrorActionPreference = 'Stop'
$UpdateRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$UpdateState = Join-Path $UpdateRoot '.update-state'
New-Item -ItemType Directory -Path $UpdateState -Force | Out-Null

# Compose snapshots contain secrets; this directory is not mounted in API.
$UpdateAcl = Get-Acl $UpdateState
$UpdateAcl.SetAccessRuleProtection($true, $false)
foreach ($UpdateSid in @([System.Security.Principal.WindowsIdentity]::GetCurrent().User,
        [System.Security.Principal.SecurityIdentifier]::new('S-1-5-18'),
        [System.Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'))) {
    $UpdateRule = [System.Security.AccessControl.FileSystemAccessRule]::new($UpdateSid,
        'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
    $UpdateAcl.SetAccessRule($UpdateRule)
}
Set-Acl -Path $UpdateState -AclObject $UpdateAcl

$UpdatePython = $null
$UpdateCandidates = @((Join-Path $UpdateRoot '.venv\Scripts\python.exe'))
foreach ($UpdateCommand in @('python3', 'python')) {
    $UpdateFound = Get-Command $UpdateCommand -ErrorAction SilentlyContinue
    if ($UpdateFound -and $UpdateFound.Source -notmatch 'WindowsApps') { $UpdateCandidates += $UpdateFound.Source }
}
foreach ($UpdateCandidate in $UpdateCandidates) {
    if (Test-Path $UpdateCandidate) {
        try {
            & $UpdateCandidate -c 'import sys; sys.exit(sys.version_info < (3,9))' 2>$null
            if ($LASTEXITCODE -eq 0) { $UpdatePython = $UpdateCandidate; break }
        } catch {}
    }
}
if (-not $UpdatePython) {
    $UpdateArch = if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64' -or $env:PROCESSOR_ARCHITEW6432 -eq 'ARM64') { 'arm64' } else { 'amd64' }
    $UpdateHash = if ($UpdateArch -eq 'arm64') { '88953b39002caf974c081469356caf6220fd4ebcd1bc46aef099a03b0a7e8447' } else { 'ac1a727a71738e11de80b76e975f9b8a258aea6412bfc31696b929d59c6aafd0' }
    $UpdateRuntime = Join-Path $UpdateState "python-3.14.7-$UpdateArch"
    $UpdatePython = Join-Path $UpdateRuntime 'python.exe'
    if (-not (Test-Path $UpdatePython)) {
        Write-Host 'Подготавливаю локальный Python (система и PATH не меняются)...'
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        $UpdateTemporary = Join-Path $UpdateState ([guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $UpdateTemporary | Out-Null
        $UpdateArchive = Join-Path $UpdateTemporary 'runtime.zip'
        Invoke-WebRequest -UseBasicParsing -Uri "https://www.python.org/ftp/python/3.14.7/python-3.14.7-$UpdateArch.zip" -OutFile $UpdateArchive -TimeoutSec 300
        if ((Get-FileHash $UpdateArchive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $UpdateHash) {
            throw 'SHA-256 Python не совпал; выполнение скачанного кода запрещено.'
        }
        $UpdateExtract = Join-Path $UpdateTemporary 'verified'
        Expand-Archive -LiteralPath $UpdateArchive -DestinationPath $UpdateExtract
        if (-not (Test-Path (Join-Path $UpdateExtract 'python.exe'))) { throw 'Архив Python не содержит ожидаемый runtime.' }
        Move-Item -LiteralPath $UpdateExtract -Destination $UpdateRuntime
    }
}
& $UpdatePython (Join-Path $UpdateRoot 'scripts\maintenance\update_runtime.py') --root $UpdateRoot @UpdateArguments
exit $LASTEXITCODE
