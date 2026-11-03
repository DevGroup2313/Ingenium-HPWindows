# INGENIUM  |  Copyright (c) 2026 Yannie D. Forest. All rights reserved.
# NO LICENCE IS GRANTED. See LICENCE.txt.
#
# 6.7.1 (app_care_671.py in Ingenium-Mac-Mini source/build/desktop): checks, on Windows, with the installed app:
#   1. a page whose process ends (as a crash, running out of memory or Task Manager would end it) is loaded again by
#      itself, and the window keeps answering;
#   2. Windows ending the session (what a shut down, restart or sign out sends) ends the run as closing the window does,
#      so the next start does not count it as a run that did not finish (full graphics stay on, nothing is moved);
#   3. runs whose page came up and that were ended by force (every step before this one ends the app that way) leave the
#      stored work where it is;
#   4. two starts whose page never came up move the stored work aside, whole, under a name of its own, and say so.
#   pwsh .github/diag/care_check.ps1 <the app's program folder> <its exe> <folder for pictures>
param([string]$Dir, [string]$Exe, [string]$Out)
$ErrorActionPreference = 'Continue'
$pass = 0; $fail = 0
function Ok([bool]$c, [string]$m, $d) { if ($c) { "PASS $m"; $script:pass++ } else { "FAIL $m  $d"; $script:fail++ } }
function ReadText([string]$path) { try { $fs = [IO.File]::Open($path, 'Open', 'Read', 'ReadWrite'); try { (New-Object IO.StreamReader($fs)).ReadToEnd() } finally { $fs.Dispose() } } catch { '' } }
function StopApp { Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Path -and $_.Path.StartsWith($Dir) } | Stop-Process -Force -ErrorAction SilentlyContinue; Start-Sleep -Seconds 4 }
function WaitFor([int]$from, [string]$pattern, [int]$seconds) {
  for ($i = 0; $i -lt $seconds; $i++) { $t = ReadText $script:log; if ($t.Length -gt $from -and $t.Substring($from) -match $pattern) { return $true }; Start-Sleep -Seconds 1 }
  return $false
}
function Since([int]$from) { $t = ReadText $script:log; if ($t.Length -gt $from) { $t.Substring($from) } else { '' } }
Add-Type @"
using System; using System.Collections.Generic; using System.Runtime.InteropServices; using System.Text;
public static class IngCare {
  [DllImport("user32.dll")] public static extern bool IsHungAppWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetClassName(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern IntPtr SendMessageTimeout(IntPtr h, uint msg, IntPtr w, IntPtr l, uint flags, uint ms, out IntPtr r);
  public delegate bool EnumProc(IntPtr h, IntPtr l);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc f, IntPtr l);
  public static List<IntPtr> Of(int pid, string classPrefix, bool visibleOnly) {
    var o = new List<IntPtr>();
    EnumWindows((h, l) => { uint p; GetWindowThreadProcessId(h, out p);
      if (p == (uint)pid && (!visibleOnly || IsWindowVisible(h))) { var s = new StringBuilder(256); GetClassName(h, s, 256);
        if (classPrefix == null || s.ToString().StartsWith(classPrefix)) o.Add(h); } return true; }, IntPtr.Zero);
    return o;
  }
  public static int EndSession(IntPtr h) {
    IntPtr r; // ENDSESSION_LOGOFF, as a sign out sends it
    SendMessageTimeout(h, 0x0011, IntPtr.Zero, new IntPtr(unchecked((int)0x80000000)), 2, 10000, out r);
    SendMessageTimeout(h, 0x0016, new IntPtr(1), new IntPtr(unchecked((int)0x80000000)), 2, 10000, out r);
    return (int)r;
  }
}
"@
$script:log = Join-Path $Dir 'ingenium_crash.log'
$flags = Join-Path $env:USERPROFILE 'Library\Application Support\Ingenium'
$sessionFlag = Join-Path $flags 'ingenium_session.flag'
$startFlag = Join-Path $flags 'ingenium_start.flag'
$work = Join-Path $env:LOCALAPPDATA 'Ingenium'
$store = Join-Path $work 'storage'
$safe = Join-Path $work 'ingenium_safe_graphics.txt'
Remove-Item Env:WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS -ErrorAction SilentlyContinue

# 3. every step before this one ended the app by force after its page came up
StopApp
$all = ReadText $script:log
$kept = @(Get-ChildItem $work -Directory -ErrorAction SilentlyContinue | Where-Object { $_.Name -like 'storage.kept-*' -or $_.Name -like 'storage.crashed-*' })
Ok (($kept.Count -eq 0) -and ($all -notmatch 'moved aside')) "runs whose page came up, each ended by force, left the stored work where it is" (($kept | ForEach-Object Name) -join ', ')

# 1. the page's process ends
Remove-Item $sessionFlag, $startFlag, $safe -Force -ErrorAction SilentlyContinue
$from = (ReadText $script:log).Length
$env:WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS = '--remote-debugging-port=9335'
$p = Start-Process -FilePath $Exe -WorkingDirectory $Dir -PassThru
$up = WaitFor $from 'load: verified, page booted' 150
$care = WaitFor $from 'care: listening' 20
Ok ($up -and $care) "the app starts, its page comes up, and it listens for the page's processes (log: $((Since $from) -split "`n" | Where-Object { $_ -match 'care:|graphics:' } | ForEach-Object { $_.Trim() }))" (Since $from)
Start-Sleep -Seconds 5
$r = @(Get-CimInstance Win32_Process -Filter "Name='msedgewebview2.exe'" | Where-Object { $_.CommandLine -match '--type=renderer' })
"Ending $($r.Count) page process(es) by force"
$from2 = (ReadText $script:log).Length
foreach ($x in $r) { Stop-Process -Id $x.ProcessId -Force -ErrorAction SilentlyContinue }
$back = WaitFor $from2 'page: its process stopped[^\n]*loaded again' 30
$loaded = WaitFor $from2 'load: page reported loaded' 30
Ok ($back -and $loaded) "a page whose process ended is loaded again by itself" (Since $from2)
Start-Sleep -Seconds 8
$hw = @([IngCare]::Of($p.Id, $null, $true))
$hung = @($hw | Where-Object { [IngCare]::IsHungAppWindow($_) })
Ok ((-not $p.HasExited) -and $hw.Count -ge 1 -and $hung.Count -eq 0) "the app keeps running and its window answers ($($hw.Count) window(s), $($hung.Count) not responding)"
try { python .github/diag/cdp_probe.py 9335 (Join-Path $Out 'screen-care-after-reload.png') | Select-String -Pattern '"ingReady"|"booted"|picture:' | ForEach-Object { "  page after the new start: $($_.Line.Trim())" } } catch { "  (the page could not be asked: $($_.Exception.Message))" }

# 2. Windows ends the session
$bw = @([IngCare]::Of($p.Id, '.NET-BroadcastEventWindow', $false))
"Windows' session messages go to $($bw.Count) window(s) of the app"
$from3 = (ReadText $script:log).Length
foreach ($h in $bw) { $null = [IngCare]::EndSession($h) }
$said = WaitFor $from3 'session: Windows is ending the session' 15
Start-Sleep -Seconds 2
Ok ($said -and -not (Test-Path $sessionFlag)) "Windows ending the session ends the run as closing the window does (the run's mark is gone: $(-not (Test-Path $sessionFlag)))" (Since $from3)
StopApp  # as Windows ends what is still open
Remove-Item Env:WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS -ErrorAction SilentlyContinue
$from4 = (ReadText $script:log).Length
$p = Start-Process -FilePath $Exe -WorkingDirectory $Dir -PassThru
$up = WaitFor $from4 'load: verified, page booted' 150
$s4 = Since $from4
Ok ($up -and $s4 -match 'graphics: full' -and $s4 -notmatch 'did not finish|ended badly|moved aside') "the next start counts it as a run that finished: full graphics, nothing moved" (($s4 -split "`n" | Where-Object { $_ -match 'graphics:|start:' } | ForEach-Object { $_.Trim() }) -join ' | ')
StopApp

# 4. two starts whose page never came up
$mark = Join-Path $store 'care-check-marker.txt'
New-Item -ItemType Directory -Force $store | Out-Null
Set-Content -Path $mark -Value 'kept by the care check'
New-Item -ItemType Directory -Force $flags | Out-Null
Set-Content -Path $startFlag -Value '2' -NoNewline
$from5 = (ReadText $script:log).Length
$p = Start-Process -FilePath $Exe -WorkingDirectory $Dir -PassThru
$moved = WaitFor $from5 'moved aside, whole' 60
$up = WaitFor $from5 'load: verified, page booted' 150
$k = @(Get-ChildItem $work -Directory -ErrorAction SilentlyContinue | Where-Object { $_.Name -like 'storage.kept-*' })
$inside = $k.Count -eq 1 -and (Test-Path (Join-Path $k[0].FullName 'care-check-marker.txt'))
Ok ($moved -and $inside -and $up -and -not (Test-Path $mark)) "after two starts whose page never came up, the work is moved aside whole ($(($k | ForEach-Object Name) -join ', ')), and the app starts fresh" (Since $from5)
Ok (-not (Test-Path $startFlag)) "once the page comes up, the start is no longer counted"
StopApp
"`n$pass passed, $fail failed"
if ($fail) { exit 1 }
