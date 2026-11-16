# go2rtc_ctl.ps1 - release a webcam from go2rtc for exclusive capture and give it back.
# Must run elevated (go2rtc + its ffmpeg live in session 0 under the GoRTC scheduled task).
#   release : comment out the go2rtc streams that open -Device, restart go2rtc
#   restore : put the original go2rtc.yaml back, restart go2rtc
#   status  : print whether a release is active
param(
    [Parameter(Mandatory)][ValidateSet('release', 'restore', 'status')][string]$Action,
    [string]$Device = '',
    [string]$DeviceB64 = '',   # UTF-8 base64 of the device name (non-ASCII survives UAC hops)
    [string]$Yaml = 'C:\go2rtc\go2rtc.yaml',
    [string]$Task = 'GoRTC',
    [string]$Log = ''
)
$ErrorActionPreference = 'Stop'
if ($Log) { Start-Transcript -Path $Log -Force | Out-Null }
$orig = "$Yaml.astrocam-orig"
if ($DeviceB64) { $Device = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($DeviceB64)) }
$utf8 = New-Object System.Text.UTF8Encoding($false)

function Restart-Go2rtc {
    Stop-ScheduledTask -TaskName $Task -ErrorAction SilentlyContinue
    $dir = Split-Path $Yaml
    # go2rtc's exec children survive the task stop; kill everything started from the go2rtc folder in session 0
    Get-CimInstance Win32_Process | Where-Object {
        $_.SessionId -eq 0 -and $_.ExecutablePath -and $_.ExecutablePath.StartsWith($dir, 'OrdinalIgnoreCase')
    } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2
    Start-ScheduledTask -TaskName $Task
}

try {
    switch ($Action) {
        'status' {
            if (Test-Path $orig) { 'RELEASED' } else { 'NORMAL' }
        }
        'release' {
            if (-not $Device) { throw 'Device is required' }
            $created = $false
            if (-not (Test-Path $orig)) { Copy-Item $Yaml $orig; $created = $true }
            $lines = [IO.File]::ReadAllLines($orig, $utf8)
            $out = New-Object System.Collections.Generic.List[string]
            $i = 0; $hits = @()
            while ($i -lt $lines.Count) {
                $l = $lines[$i]
                # a stream key "  name:" followed by "    - source" lines
                if ($l -match '^  ([A-Za-z0-9_\-]+):\s*$') {
                    $name = $Matches[1]; $j = $i + 1; $block = @()
                    while ($j -lt $lines.Count -and $lines[$j] -match '^    ') { $block += $lines[$j]; $j++ }
                    if ($block.Count -and (($block -join "`n").Contains($Device))) {
                        $hits += $name
                        $out.Add("#astrocam# $l"); $block | ForEach-Object { $out.Add("#astrocam# $_") }
                        $i = $j; continue
                    }
                }
                $out.Add($l); $i++
            }
            if (-not $hits) { "No go2rtc stream uses '$Device'; nothing to release."; if ($created) { Remove-Item $orig }; break }
            [IO.File]::WriteAllLines($Yaml, $out, $utf8)
            Restart-Go2rtc
            "Released streams: $($hits -join ', ')"
        }
        'restore' {
            if (Test-Path $orig) {
                Copy-Item $orig $Yaml -Force
                Remove-Item $orig
                Restart-Go2rtc
                'Restored original go2rtc.yaml and restarted go2rtc'
            } else { 'Nothing to restore' }
        }
    }
} finally {
    if ($Log) { Stop-Transcript | Out-Null }
}


