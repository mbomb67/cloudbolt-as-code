# Installs the software updates the server's own Windows Update Agent offers. It runs as a detached SYSTEM scheduled
# task that vuln_wintask.py stages and starts (the Agent refuses a remote session, so it cannot run in the WinRM or
# Run Command session itself). vuln_wintask.build_worker drops these comment lines, the blank lines and the
# indentation before the file is staged, so only the code counts toward the size limit.
# Windows PowerShell 5.1, ASCII only, always exit 0, nothing on stderr. The update source is never changed: no
# ServerSelection, no AddService, nothing downloaded from anywhere but what the Agent itself is pointed at.
#
# Vendor documentation, one page per member used below (Microsoft Learn, Windows Update Agent API):
# https://learn.microsoft.com/en-us/windows/win32/api/wuapi/nn-wuapi-iupdatesession
#   Microsoft.Update.Session; CreateUpdateSearcher, CreateUpdateDownloader, CreateUpdateInstaller.
# https://learn.microsoft.com/en-us/windows/win32/api/wuapi/nf-wuapi-iupdatesearcher-search
#   Search(criteria): IsInstalled, Type, IsHidden are criteria; the returned ISearchResult has ResultCode and Updates
#   https://learn.microsoft.com/en-us/windows/win32/api/wuapi/nn-wuapi-isearchresult
# https://learn.microsoft.com/en-us/windows/win32/api/wuapi/nn-wuapi-iupdate
#   Title, KBArticleIDs, EulaAccepted, AcceptEula(), IsDownloaded, InstallationBehavior.
# https://learn.microsoft.com/en-us/windows/win32/api/wuapi/nn-wuapi-iinstallationbehavior
#   InstallationBehavior.CanRequestUserInput.
# https://learn.microsoft.com/en-us/windows/win32/api/wuapi/nf-wuapi-iupdate-accepteula
#   AcceptEula() returns E_ACCESSDENIED when called from a remote computer; local as SYSTEM it is allowed.
# https://learn.microsoft.com/en-us/windows/win32/api/wuapi/nn-wuapi-iupdatecollection
#   Microsoft.Update.UpdateColl; Add, Count, Item.
# https://learn.microsoft.com/en-us/windows/win32/api/wuapi/nf-wuapi-iupdatedownloader-download
#   The downloader's Updates property is set first; Download() is synchronous and returns an IDownloadResult.
# https://learn.microsoft.com/en-us/windows/win32/api/wuapi/nn-wuapi-iupdateinstaller
#   Updates, AllowSourcePrompts, Install() (synchronous, returns IInstallationResult).
# https://learn.microsoft.com/en-us/windows/win32/api/wuapi/nn-wuapi-iupdateinstaller2
#   ForceQuiet (Windows Installer without user interaction).
# https://learn.microsoft.com/en-us/windows/win32/api/wuapi/nn-wuapi-iinstallationresult
#   ResultCode, HResult, RebootRequired, GetUpdateResult(updateIndex): the index is the update's position in the
#   collection given to the installer. https://learn.microsoft.com/en-us/windows/win32/api/wuapi/nn-wuapi-iupdateinstallationresult
#   (per update: ResultCode, HResult, RebootRequired).
# https://learn.microsoft.com/en-us/windows/win32/api/wuapi/ne-wuapi-operationresultcode
#   orcNotStarted 0, orcInProgress 1, orcSucceeded 2, orcSucceededWithErrors 3, orcFailed 4, orcAborted 5.
# https://learn.microsoft.com/en-us/windows/win32/api/wuapi/nn-wuapi-isysteminformation
#   Microsoft.Update.SystemInfo; RebootRequired.
$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue'
$LogF=Join-Path $PSScriptRoot 'status.log'
$U8=New-Object Text.UTF8Encoding($false)
# Cb appends one CBVULN line to status.log, which the poll script reads. One write per line. Characters outside
# printable ASCII (line breaks too) become spaces and a pipe becomes a slash, so a value never breaks a line.
function Cb($k,$v){$v=([string]$v) -replace '[^\x20-\x7e]',' ' -replace '\|','/';$v=$v.Trim();if($v.Length -gt 900){$v=$v.Substring(0,900)};[IO.File]::AppendAllText($LogF,'CBVULN|'+$k+'|'+$v+"`n",$U8)}
# Rc turns an OperationResultCode into a whole number 0 to 5; anything else counts as 4 (failed).
function Rc($v){try{$n=[int]$v}catch{return 4};if($n -lt 0 -or $n -gt 5){return 4};return $n}
# Hx writes an HRESULT as 0x and eight hexadecimal digits. A failure reports the innermost exception's HResult:
# the outer exception is PowerShell's wrapper around the COM error.
function Hx($v){try{return '0x'+([int]$v).ToString('X8')}catch{return '0x00000000'} }
function Kb($u){$a=@($u.KBArticleIDs);if($a.Count -gt 0){return 'KB'+$a[0]};return '-'}
function Run{
if(Get-Service -Name 'CcmExec' -ErrorAction SilentlyContinue){Cb reason 'This server is patched by Configuration Manager.';return}
$si=New-Object -ComObject Microsoft.Update.SystemInfo
Cb restart_before ([int][bool]$si.RebootRequired)
$ss=New-Object -ComObject Microsoft.Update.Session
Cb phase 'Searching for updates'
$sr=$ss.CreateUpdateSearcher().Search("IsInstalled=0 and Type='Software' and IsHidden=0")
if((Rc $sr.ResultCode) -ge 4){throw 'The search for updates did not complete.'}
$c=New-Object -ComObject Microsoft.Update.UpdateColl
$sk=0
Cb found $sr.Updates.Count
foreach($u in $sr.Updates){
if($u.InstallationBehavior.CanRequestUserInput){$sk++;continue}
if(-not $u.EulaAccepted){try{$u.AcceptEula()}catch{} }
if(-not $u.EulaAccepted){$sk++;continue}
$null=$c.Add($u)
}
Cb skipped $sk
if($c.Count -eq 0){return}
Cb phase 'Downloading updates'
try{$d=$ss.CreateUpdateDownloader();$d.Updates=$c;$null=$d.Download()}catch{Cb note ('The download reported: '+$_.Exception.Message)}
$ins=New-Object -ComObject Microsoft.Update.UpdateColl
$items=@();$nd=0
foreach($u in $c){if($u.IsDownloaded){$null=$ins.Add($u)}else{$nd++;$items+=('4;'+(Kb $u)+';'+$u.Title)} }
if($ins.Count -eq 0){throw 'None of the updates could be downloaded.'}
Cb phase 'Installing updates'
$i=$ss.CreateUpdateInstaller()
$i.Updates=$ins
$i.ForceQuiet=$true
$i.AllowSourcePrompts=$false
$ir=$i.Install()
$up=0;$fl=$nd
for($k=0;$k -lt $ins.Count;$k++){
$q=Rc ($ir.GetUpdateResult($k).ResultCode)
if($q -eq 2){$up++}elseif($q -ge 3){$fl++}
$items+=([string]$q+';'+(Kb $ins.Item($k))+';'+$ins.Item($k).Title)
}
foreach($t in ($items|Select-Object -First 50)){Cb item $t}
$rs=Rc $ir.ResultCode
$rb=[int]([bool]$ir.RebootRequired -or [bool]$si.RebootRequired)
Cb updated $up
Cb failed $fl
Cb result $rs
Cb hresult (Hx $ir.HResult)
Cb restart $rb
if($rs -eq 2 -and $rb -eq 0){
Cb phase 'Checking for more updates'
$m=0
foreach($u in $ss.CreateUpdateSearcher().Search("IsInstalled=0 and Type='Software' and IsHidden=0").Updates){if(-not $u.InstallationBehavior.CanRequestUserInput){$m++} }
Cb more $m
}
}
try{Run}catch{$x=$_.Exception;while($x.InnerException){$x=$x.InnerException};Cb failure ('Windows Update failed: '+$_.Exception.Message);Cb hresult (Hx $x.HResult)}
try{Cb done 1}catch{}
exit 0
