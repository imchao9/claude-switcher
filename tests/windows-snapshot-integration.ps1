# Windows 加密快照隔离回归。仅使用临时账号，不访问真实 Claude 数据或网络。
$ErrorActionPreference = 'Stop'
$core = Join-Path (Split-Path -Parent $PSScriptRoot) 'windows\claude-switch.ps1'
$root = Join-Path ([IO.Path]::GetTempPath()) ('cs-snapshot-' + [guid]::NewGuid().ToString('N'))
$env:APPDATA = Join-Path $root 'appdata'
$env:LOCALAPPDATA = Join-Path $root 'local'
$homeDir = Join-Path $root 'home'
$accounts = Join-Path $homeDir '.claude-switcher\accounts'
New-Item -ItemType Directory -Path "$accounts\A\sub" -Force | Out-Null
Set-Content -LiteralPath "$accounts\A\sub\marker.txt" -Value 'account-a' -Encoding UTF8 -NoNewline
New-Item -ItemType Directory -Path $env:APPDATA,$env:LOCALAPPDATA -Force | Out-Null
Set-Content -LiteralPath "$homeDir\.claude-switcher\current.txt" -Value 'B' -Encoding ascii -NoNewline
$password = 'test-password-123'
$failures = 0

function Invoke-Snapshot([string[]]$Arguments, [string]$Secret, [string]$HomePath = $homeDir) {
    $env:USERPROFILE = $HomePath
    $out = $Secret | & powershell -NoProfile -ExecutionPolicy Bypass -File $core @Arguments 2>&1
    return @{ output = (@($out) -join "`n"); code = $LASTEXITCODE }
}
function Assert([string]$Name, [bool]$Condition, [string]$Detail = '') {
    if ($Condition) { "PASS  $Name" } else { "FAIL  $Name $Detail"; $script:failures++ }
}

try {
    $image = Join-Path $root 'account-a.csnap'
    $r = Invoke-Snapshot @('snapshot-export','A',$image,'--password-stdin') $password
    Assert '加密导出成功' ($r.code -eq 0 -and (Test-Path $image)) $r.output

    $home2 = Join-Path $root 'home2'
    $r = Invoke-Snapshot @('snapshot-import',$image,'A','--password-stdin') $password $home2
    Assert '往返导入成功' ($r.code -eq 0) $r.output
    Assert '快照内容一致' ((Get-Content "$home2\.claude-switcher\accounts\A\sub\marker.txt" -Raw -Encoding UTF8).Trim() -eq 'account-a')
    Assert '元数据不进入账号目录' (-not (Test-Path "$home2\.claude-switcher\accounts\A\.claude-switcher-snapshot.json"))

    $home3 = Join-Path $root 'home3'
    $r = Invoke-Snapshot @('snapshot-import',$image,'A','--password-stdin') 'wrong-password' $home3
    Assert '错误密码拒绝且不建槽位' ($r.code -ne 0 -and -not (Test-Path "$home3\.claude-switcher\accounts\A")) $r.output

    $home4 = Join-Path $root 'home4'
    $r = Invoke-Snapshot @('snapshot-import',$image,'B','--password-stdin') $password $home4
    Assert '账号不匹配拒绝' ($r.code -ne 0 -and -not (Test-Path "$home4\.claude-switcher\accounts\B")) $r.output

    $shortImage = Join-Path $root 'short.csnap'
    $r = Invoke-Snapshot @('snapshot-export','A',$shortImage,'--password-stdin') 'short'
    Assert '短密码拒绝' ($r.code -ne 0 -and -not (Test-Path $shortImage)) $r.output

    Set-Content -LiteralPath "$homeDir\.claude-switcher\current.txt" -Value 'A' -Encoding ascii -NoNewline
    $currentImage = Join-Path $root 'current.csnap'
    $r = Invoke-Snapshot @('snapshot-export','A',$currentImage,'--password-stdin') $password
    Assert '当前账号禁止导出' ($r.code -ne 0 -and -not (Test-Path $currentImage)) $r.output

    $tampered = Join-Path $root 'tampered.csnap'
    Copy-Item -LiteralPath $image -Destination $tampered
    $stream = [IO.File]::Open($tampered,[IO.FileMode]::Open,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)
    try {
        # 写固定的 0 可能没有改变密文；翻转一位确保每次都产生真实篡改。
        $stream.Position = $stream.Length - 1
        $originalByte = $stream.ReadByte()
        $stream.Position = $stream.Length - 1
        $stream.WriteByte([byte]($originalByte -bxor 1))
        $stream.Flush()
        $stream.Position = $stream.Length - 1
        Assert '篡改确实改变密文字节' ($stream.ReadByte() -ne $originalByte)
    } finally { $stream.Dispose() }
    $home5 = Join-Path $root 'home5'
    $r = Invoke-Snapshot @('snapshot-import',$tampered,'A','--password-stdin') $password $home5
    Assert '篡改认证失败且不建槽位' ($r.code -ne 0 -and -not (Test-Path "$home5\.claude-switcher\accounts\A")) $r.output
} finally {
    Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue
}
if ($failures) { "FAILED: $failures 项"; exit 1 }
'OK: 全部通过'
