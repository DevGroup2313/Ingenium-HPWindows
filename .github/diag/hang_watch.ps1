# INGENIUM  |  Copyright (c) 2026 Yannie D. Forest. All rights reserved.
# NO LICENCE IS GRANTED. See LICENCE.txt.
#
# 25 October 2026: whether Windows marks an app's window "Not responding", and for how long. Windows itself decides that
# with IsHungAppWindow (a window that has not taken its messages for about five seconds); this asks it of every visible
# window of the app's processes, five times a second, and records each spell, the app's processor time and its memory.
#   pwsh .github/diag/hang_watch.ps1 <the app's program folder> <seconds> <out.json>
param([string]$Dir, [int]$Seconds, [string]$Out)
Add-Type @"
using System; using System.Collections.Generic; using System.Runtime.InteropServices; using System.Text;
public static class IngHang {
  [DllImport("user32.dll")] public static extern bool IsHungAppWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
  public delegate bool EnumProc(IntPtr h, IntPtr l);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc f, IntPtr l);
  public static List<IntPtr> Of(int[] pids) {
    var set = new HashSet<uint>(); foreach (var p in pids) set.Add((uint)p);
    var o = new List<IntPtr>();
    EnumWindows((h, l) => { uint pid; GetWindowThreadProcessId(h, out pid); if (set.Contains(pid) && IsWindowVisible(h)) o.Add(h); return true; }, IntPtr.Zero);
    return o;
  }
  public static string Title(IntPtr h) { var s = new StringBuilder(300); GetWindowText(h, s, 300); return s.ToString(); }
}
"@
$clock = [Diagnostics.Stopwatch]::StartNew()
$spells = New-Object System.Collections.ArrayList
$now = $null; $samples = 0; $hungSamples = 0; $windowsSeen = 0; $peakMB = 0; $cpu = @(); $titles = @{}
while ($clock.Elapsed.TotalSeconds -lt $Seconds) {
  $procs = @(Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Path -and $_.Path.StartsWith($Dir) })
  $wv = @(Get-Process msedgewebview2 -ErrorAction SilentlyContinue)
  $hung = $false
  if ($procs.Count) {
    $hs = [IngHang]::Of([int[]]($procs | ForEach-Object { $_.Id }))
    if ($hs.Count) { $windowsSeen++ }
    foreach ($h in $hs) {
      $tt = [IngHang]::Title($h); if ($tt) { $titles[$tt] = 1 }
      if ([IngHang]::IsHungAppWindow($h)) { $hung = $true }
    }
    $mb = [math]::Round((($procs + $wv | Measure-Object WorkingSet64 -Sum).Sum) / 1MB)
    if ($mb -gt $peakMB) { $peakMB = $mb }
  }
  $samples++
  $t = [math]::Round($clock.Elapsed.TotalSeconds, 1)
  if ($hung) { $hungSamples++; if (-not $now) { $now = [pscustomobject]@{ from = $t; to = $null; seconds = $null; open = $false } } }
  elseif ($now) { $now.to = $t; $now.seconds = [math]::Round($now.to - $now.from, 1); [void]$spells.Add($now); $now = $null }
  if ($samples % 5 -eq 0) { $cpu += ,@($t, [math]::Round((($procs + $wv | Measure-Object CPU -Sum).Sum), 1)) }
  Start-Sleep -Milliseconds 200
}
if ($now) { $now.to = [math]::Round($clock.Elapsed.TotalSeconds, 1); $now.seconds = [math]::Round($now.to - $now.from, 1); $now.open = $true; [void]$spells.Add($now) }
$res = [ordered]@{
  seconds = $Seconds; samples = $samples; window_seen_samples = $windowsSeen
  not_responding_spells = $spells.Count; not_responding_seconds = [math]::Round(($spells | Measure-Object seconds -Sum).Sum, 1)
  longest_spell_seconds = [math]::Round((($spells | Measure-Object seconds -Maximum).Maximum), 1); spells = $spells
  peak_memory_mb = $peakMB; window_titles = @($titles.Keys); processor_seconds = $cpu
}
$res | ConvertTo-Json -Depth 5 | Out-File -FilePath $Out -Encoding utf8
"Not responding: $($spells.Count) spell(s), $($res.not_responding_seconds) s in all, the longest $($res.longest_spell_seconds) s; memory at most $peakMB MB; window seen in $windowsSeen of $samples looks"
