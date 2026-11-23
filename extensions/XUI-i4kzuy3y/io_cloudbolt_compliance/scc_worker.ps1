$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$Work = $PSScriptRoot
$LogF = Join-Path $Work 'status.log'
$U8 = New-Object Text.UTF8Encoding($false)
$T0 = Get-Date
$PFiles = $env:ProgramW6432
if (!$PFiles) { $PFiles = $env:ProgramFiles }
$Root = Join-Path $PFiles 'CloudBolt\Compliance\scc'
$Acl = @('/inheritance:r', '/grant:r', '*S-1-5-18:(OI)(CI)F', '*S-1-5-32-544:(OI)(CI)F')
$St = @{ ready = 0; ver = ''; os = ''; exit = ''; score = ''; ds = ''; prof = ''; title = ''; problem = ''; up = @{}; kv = '' }

function E($k, $v) {
  $t = ([string]$v) -replace '[\r\n\t]+', ' ' -replace '[A-Za-z][A-Za-z0-9+.-]*://\S+', '<url>'
  [IO.File]::AppendAllText($LogF, "OSCAP|$k|$t`n", $U8)
}
function N($v, $n) {
  $t = ([string]$v) -replace '[\r\n\t~|]+', ' '
  if ($t.Length -gt $n) { $t.Substring(0, $n) } else { $t }
}
function P($m) { $St.problem = [string]$m; E problem (N $m 280) }
function Sm($f) {
  $d = [int]((Get-Date) - $T0).TotalSeconds
  E summary ("ready=$($St.ready)~ver=$(N $St.ver 40)~os=$(N $St.os 120)~mode=$($Cfg.mode)~exit=$($St.exit)~score=$($St.score)~dur=$d~ds=$(N $St.ds 120)~prof=$(N $St.prof 200)~title=$(N $St.title 200)~problem=$(N $St.problem 300)~st_report=$($St.up['report.html'])~st_results=$($St.up['results.xml.gz'])~st_arf=$($St.up['arf.xml.gz'])~final=$f$($St.kv)")
}

function Fs {
  $v = [string]$Cfg.ver
  if ($v -and $v -ne 'latest') {
    $x = "$Root\$v\scc_$v\cscc.exe"
    if (Test-Path $x) { return $x }
    return $null
  }
  $best = $null
  foreach ($d in (Get-ChildItem $Root -Directory -ErrorAction SilentlyContinue)) {
    if ($d.Name -like '*.partial') { continue }
    $x = "$($d.FullName)\scc_$($d.Name)\cscc.exe"
    if (Test-Path $x) {
      try { $n = [version]$d.Name } catch { continue }
      if (!$best -or $n -gt $best.n) { $best = @{ n = $n; p = $x } }
    }
  }
  if ($best) { $best.p } else { $null }
}

function Dl($u, $o) {
  if ($u -notmatch '^https://') { throw 'The address must use https.' }
  if (Get-Command curl.exe -ErrorAction SilentlyContinue) {
    & curl.exe -sSLf --proto '=https' --proto-redir '=https' --max-redirs 5 --retry 2 -o $o $u
    if ($LASTEXITCODE -ne 0) { Remove-Item $o -Force -ErrorAction SilentlyContinue }
  }
  if (!(Test-Path $o)) {
    $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 1800 -MaximumRedirection 5 -Uri $u -OutFile $o -PassThru
    if ($r.BaseResponse.ResponseUri.Scheme -ne 'https') { Remove-Item $o -Force; throw 'The download was redirected to an address that is not https.' }
  }
}

function Inst {
  if (!$Cfg.url) { P 'No SCC download address is configured.'; return $false }
  $v = [string]$Cfg.ver
  if (!(Test-Path $Root)) {
    New-Item -ItemType Directory -Force -Path $Root | Out-Null
    $null = icacls $Root @Acl
  }
  if (!$v -or $v -eq 'latest') {
    E progress 'Looking up the latest SCC version'
    try {
      $rel = "$Root\release.xml"
      Dl 'https://raw.githubusercontent.com/niwc-atlantic/scap-scc/main/scc_release.xml' $rel
      $x = [IO.File]::ReadAllText($rel)
      Remove-Item $rel -Force -ErrorAction SilentlyContinue
    } catch { P ('Could not read the SCC release list: ' + $_.Exception.Message); return $false }
    $m = [regex]::Match($x, '<version>\s*([0-9][0-9.]*)\s*</version>')
    if (!$m.Success) { P 'The SCC release list has no version.'; return $false }
    $v = $m.Groups[1].Value
  }
  $url = ([string]$Cfg.url).Replace('{version}', $v)
  $part = "$Root\$v.partial"
  $ok = $false
  try {
    Remove-Item $part -Recurse -Force -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Force -Path $part | Out-Null
    $zip = "$part\scc.zip"
    E progress "Downloading SCC $v (about 108 MB)"
    try { Dl $url $zip } catch { P ('The SCC download failed: ' + $_.Exception.Message); return $false }
    if ($Cfg.sha) {
      if ((Get-FileHash $zip -Algorithm SHA256).Hash -ne ([string]$Cfg.sha).Trim()) {
        P 'The downloaded SCC file does not match the expected SHA-256.'
        return $false
      }
    }
    E progress 'Extracting SCC'
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $tmp = "$part\x"
    [IO.Compression.ZipFile]::ExtractToDirectory($zip, $tmp)
    $inner = Get-ChildItem $tmp -Recurse -File -Filter '*_Windows.zip' | Select-Object -First 1
    if (!$inner) { P 'The SCC download holds no Windows package.'; return $false }
    [IO.Compression.ZipFile]::ExtractToDirectory($inner.FullName, $part)
    Remove-Item $tmp, $zip -Recurse -Force
    if (!(Test-Path "$part\scc_$v\cscc.exe")) { P "SCC $v was extracted but cscc.exe is not in it."; return $false }
    Remove-Item "$Root\$v" -Recurse -Force -ErrorAction SilentlyContinue
    Rename-Item $part $v
    $ok = $true
  } finally {
    if (!$ok) { Remove-Item $part -Recurse -Force -ErrorAction SilentlyContinue }
  }
  E installed 1
  return $true
}

function Bm($path) {
  $rs = New-Object Xml.XmlReaderSettings
  $rs.DtdProcessing = 'Ignore'
  $rd = [Xml.XmlReader]::Create($path, $rs)
  try {
    while ($rd.Read()) {
      if ($rd.NodeType -ne 'Element') { continue }
      if ($rd.LocalName -eq 'rule-result') { break }
      if ($rd.LocalName -eq 'Benchmark') {
        $x = $rd.GetAttribute('id')
        if ($x) { return ($x -replace '^xccdf_.*?_benchmark_', '') }
      }
    }
  } finally { $rd.Close() }
  ''
}

function Cf {
  if ($Cfg.bench) {
    $x = New-Object Xml.XmlDocument
    $x.Load("$Sd\options.xml")
    foreach ($e in $x.options.contents.content) {
      if ($e.benchmarkID -eq $Cfg.bench) { return (Join-Path $Sd $e.filepath) }
    }
    return $null
  }
  if ($St.os -match 'Server\s+(2016|2019|2022|2025)') { $k = "Windows_Server_$($Matches[1])" }
  elseif ($St.os -match 'Windows\s+(10|11)\b') { $k = "Windows_$($Matches[1])" }
  else { return $null }
  Get-ChildItem "$Sd\Resources\Content\SCAP12_Content" -Filter "U_MS_${k}_V*.xml" | Select-Object -First 1 -ExpandProperty FullName
}

function Pf($f) {
  $t = [IO.File]::ReadAllText($f)
  $b = [regex]::Match($t, '<(?:\w+:)?Benchmark\s[^>]*?\bid="([^"]+)"').Groups[1].Value
  $ps = foreach ($m in [regex]::Matches($t, '<(?:\w+:)?Profile\s+id="([^"]+)"[^>]*>\s*<(?:\w+:)?title[^>]*>([^<]*)')) {
    @{ full = $m.Groups[1].Value; short = ($m.Groups[1].Value -replace '^xccdf_.*?_profile_', ''); title = [Net.WebUtility]::HtmlDecode($m.Groups[2].Value).Trim() }
  }
  @{ id = ($b -replace '^xccdf_.*?_benchmark_', ''); profiles = @($ps) }
}

function Rn($id, $full) {
  New-Item -ItemType Directory -Force -Path "$Work\scc" | Out-Null
  $x = New-Object Xml.XmlDocument
  $x.Load("$Sd\options.xml")
  foreach ($e in $x.options.contents.content) {
    $on = if ($e.benchmarkID -eq $id) { '1' } else { '0' }
    $e.enabled = $on
    $e.review = $on
    if ($on -eq '1') { $e.selectedProfile = $full }
  }
  $set = @{ keepXCCDFXML = '1'; keepARFXML = '1'; allSettingsHTMLReport = '1'; nonComplianceHTMLReport = '0'; saveDISACKL = '0'; saveDISACKLB = '0'; enableSummaryViewer = '0' }
  foreach ($k in $set.Keys) {
    $n = $x.options.SelectSingleNode($k)
    if ($n) { $n.InnerText = $set[$k] }
  }
  [IO.File]::WriteAllText("$Work\options.xml", $x.OuterXml, $U8)
  New-Item -ItemType Directory -Force -Path "$Work\scc" | Out-Null
  $pr = Start-Process -FilePath $Cs -ArgumentList "-o `"$Work\options.xml`" -u `"$Work\scc`"" -WorkingDirectory $Sd -NoNewWindow -Wait -PassThru -RedirectStandardOutput "$Work\o.txt" -RedirectStandardError "$Work\e.txt"
  $pr.ExitCode
}

function Px($path) {
  $c = @{}; $sv = @{}; $fl = New-Object Collections.ArrayList
  $n = 0; $sc = ''; $inr = $false; $sev = 'unknown'; $rid = ''
  $rs = New-Object Xml.XmlReaderSettings
  $rs.DtdProcessing = 'Ignore'
  $rd = [Xml.XmlReader]::Create($path, $rs)
  try {
    while (-not $rd.EOF) {
      if ($rd.NodeType -ne 'Element') { [void]$rd.Read(); continue }
      switch ($rd.LocalName) {
        'rule-result' {
          $rid = $rd.GetAttribute('version')
          if (!$rid) { $rid = $rd.GetAttribute('idref') -replace '^xccdf_.*?_rule_', '' -replace '_rule$', '' }
          $sev = $rd.GetAttribute('severity')
          if ('high', 'medium', 'low' -notcontains $sev) { $sev = 'unknown' }
          $inr = $true
          [void]$rd.Read()
        }
        'result' {
          if ($inr) {
            $v = $rd.ReadElementContentAsString().Trim()
            $inr = $false
            $c[$v] = 1 + [int]$c[$v]
            $n++
            if ($v -eq 'fail') { $sv[$sev] = 1 + [int]$sv[$sev]; [void]$fl.Add("$sev $rid") }
          } else { [void]$rd.Read() }
        }
        'score' {
          $sys = $rd.GetAttribute('system')
          $t = $rd.ReadElementContentAsString().Trim()
          if (!$sc -and $sys -eq 'urn:xccdf:scoring:default') { $sc = $t }
        }
        default { [void]$rd.Read() }
      }
    }
  } finally { $rd.Close() }
  $kv = (('pass', 'fail', 'error', 'unknown', 'notapplicable', 'notchecked', 'notselected', 'informational', 'fixed' | ForEach-Object { "$_=$([int]$c[$_])" }) + ('high', 'medium', 'low', 'unknown' | ForEach-Object { "sev_$_=$([int]$sv[$_])" }) + "rules=$n") -join '~'
  if (!$sc) {
    $pa = [int]$c['pass']; $fa = [int]$c['fail']
    if ($pa + $fa -gt 0) { $sc = ([math]::Round($pa / ($pa + $fa) * 100, 2)).ToString([Globalization.CultureInfo]::InvariantCulture) }
  }
  $fo = foreach ($s in 'high', 'medium', 'low', 'unknown') { $fl | Where-Object { $_.StartsWith("$s ") } }
  @{ n = $n; kv = $kv; score = $sc; failed = @($fo | Select-Object -First 1000); total = $fl.Count; fail = [int]$c['fail'] }
}

function Gz($src, $dst) {
  $i = [IO.File]::OpenRead($src)
  $o = [IO.File]::Create($dst)
  $g = New-Object IO.Compression.GZipStream($o, [IO.Compression.CompressionMode]::Compress)
  $i.CopyTo($g)
  $g.Close(); $o.Close(); $i.Close()
}

function Up($n, $u) {
  if (!$u) { return }
  $f = "$Work\out\$n"
  $ok = 0
  if (!(Test-Path $f)) { E stored "$n|no|the file was not produced" }
  elseif ((Get-Item $f).Length -gt 52428800) { E stored "$n|no|the file is larger than 50 MB" }
  else {
    try {
      $q = [Net.HttpWebRequest]::Create($u)
      $q.Method = 'PUT'
      $q.Timeout = 300000
      $q.ReadWriteTimeout = 300000
      $q.AllowWriteStreamBuffering = $false
      if ($Cfg.tok) { $q.Headers.Add('X-Oscap-Token', $Cfg.tok) }
      $fs = [IO.File]::OpenRead($f)
      $q.ContentLength = $fs.Length
      $w = $q.GetRequestStream()
      $fs.CopyTo($w)
      $w.Close(); $fs.Close()
      $q.GetResponse().Close()
      $ok = 1
      E stored "$n|yes|"
    } catch {
      $m = $_.Exception.Message
      if ($m -match 'trust') { $m = 'the server does not trust the certificate of the upload address; turn on Skip certificate check in the Compliance settings' }
      E stored "$n|no|$(N $m 200)"
    }
  }
  $St.up[$n] = $ok
}

function Main {
  E engine scc
  $St.os = (Get-CimInstance Win32_OperatingSystem).Caption.Trim()
  E os_name $St.os
  [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
  $Cs = Fs
  if (!$Cs) {
    if ($Cfg.inst -ne '1') { P 'SCC is not installed on this server. Choose Install to download it (about 108 MB).'; return }
    if (!(Inst)) { return }
    $Cs = Fs
    if (!$Cs) { P 'SCC was installed but cscc.exe was not found.'; return }
  }
  $Sd = Split-Path $Cs -Parent
  $St.ver = Split-Path (Split-Path $Sd -Parent) -Leaf
  E oscap_version $St.ver
  $f = Cf
  if (!$f) {
    if ($Cfg.bench) { P "The benchmark $($Cfg.bench) is not installed in SCC." } else { P "SCC has no benchmark for $($St.os)." }
    return
  }
  $i = Pf $f
  $St.ready = 1
  E ready 1
  if ($Cfg.mode -ne 'scan') {
    E datastream "$($i.id)|1"
    foreach ($p in $i.profiles) { E profile "$($i.id)|$($p.short)|$(N $p.title 200)" }
    return
  }
  $want = ([string]$Cfg.prof) -replace '^xccdf_.*?_profile_', ''
  $pr = $null
  if ($want) {
    $pr = $i.profiles | Where-Object { $_.short -eq $want } | Select-Object -First 1
    if (!$pr) { $St.ds = $i.id; P "Profile $want is not in $($i.id)"; return }
  } else {
    foreach ($w in (([string]$Cfg.defp) -split ',')) {
      $w = $w.Trim()
      if (!$w) { continue }
      $pr = $i.profiles | Where-Object { $_.short -eq $w } | Select-Object -First 1
      if ($pr) { break }
    }
    if (!$pr) { $pr = $i.profiles | Select-Object -First 1 }
  }
  $St.ds = $i.id
  if (!$pr) { P "No profiles were found in $($i.id)"; return }
  $St.prof = $pr.short; $St.title = $pr.title
  E scan_datastream $i.id
  E scan_profile $pr.short
  E scan_profile_title $pr.title
  E progress 'Running the SCC scan'
  $code = Rn $i.id $pr.full
  $roots = @("$Work\scc", "$env:USERPROFILE\SCC")
  $find = { param($flt) Get-ChildItem $roots -Recurse -File -Filter $flt -ErrorAction SilentlyContinue | Where-Object { $_.LastWriteTime -ge $T0 } | Sort-Object LastWriteTime -Descending }
  $all = @(& $find '*XCCDF-Results*.xml')
  $xs = $null
  $seen = @{}
  foreach ($x in $all) {
    $bid = Bm $x.FullName
    $seen[$bid] = 1
    if (!$xs -and (($bid -eq $i.id) -or (!$bid -and $all.Count -eq 1))) { $xs = $x }
  }
  if ($seen.Count -gt 1) { E progress 'SCC scanned more content than requested' }
  if (!$xs) {
    $out = (Get-Content "$Work\o.txt" -Tail 3 -ErrorAction SilentlyContinue) -join ' '
    $log = (& $find '*Error_Log*.txt') | Select-Object -First 1
    $lt = if ($log) { (Get-Content $log.FullName -Tail 3) -join ' ' }
    $St.exit = 1; E exit_code 1
    P "SCC (exit $code) produced no results for $($i.id). $out $lt"
    return
  }
  E progress 'Reading the results'
  $r = Px $xs.FullName
  if (!$r.n) { $St.exit = 1; E exit_code 1; P 'The SCC results hold no rule results.'; return }
  $St.exit = if ($r.fail) { 2 } else { 0 }
  $St.score = $r.score
  $St.kv = '~' + $r.kv
  E exit_code $St.exit
  Sm 0
  foreach ($l in $r.failed) { E failed $l }
  E failed_total $r.total
  E progress 'Storing the result files'
  $od = "$Work\out"
  New-Item -ItemType Directory -Force -Path $od | Out-Null
  $cn = $xs.BaseName -replace '^.*XCCDF-Results_', ''
  $h = (& $find "*All-Settings*$cn.html") | Select-Object -First 1
  if ($h) { Copy-Item $h.FullName "$od\report.html" }
  Gz $xs.FullName "$od\results.xml.gz"
  $a = (& $find "*_ARF_$cn.xml") | Select-Object -First 1
  if ($a) { Gz $a.FullName "$od\arf.xml.gz" }
  if ($Cfg.ins -eq '1') {
    Add-Type 'using System.Net;using System.Security.Cryptography.X509Certificates;public class TrustAll:ICertificatePolicy{public bool CheckValidationResult(ServicePoint s,X509Certificate c,WebRequest r,int p){return true;}}'
    [Net.ServicePointManager]::CertificatePolicy = New-Object TrustAll
  }
  Up 'report.html' $Cfg.upr
  Up 'results.xml.gz' $Cfg.upz
  Up 'arf.xml.gz' $Cfg.upa
}

try { Main }
catch { P ('Unexpected error: ' + $_.Exception.Message) }
finally {
  E duration ([int]((Get-Date) - $T0).TotalSeconds)
  Sm 1
  E done 1
}
