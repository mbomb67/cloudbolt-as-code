# Reads the Windows build and the installed update ids; installs and changes nothing.
# vuln_jobs.build_windows_script drops these comment lines and the indentation before the text is sent:
# WinRM sends the whole script on one command line, so every character counts.
# Windows PowerShell 5.1, ASCII only, stdout only, always exit 0.
$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue'
# Cb prints one CBVULN line: characters outside printable ASCII (line breaks too) become spaces, a pipe becomes a slash.
function Cb($k,$v){$v=([string]$v) -replace '[^\x20-\x7e]',' ' -replace '\|','/';$v=$v.Trim();if($v.Length -gt 3000){$v=$v.Substring(0,3000)};'CBVULN|'+$k+'|'+$v}
try{
Cb os ([Environment]::OSVersion.VersionString)
$o=Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion'
Cb current_build $o.CurrentBuild
Cb ubr $o.UBR
Cb installation_type $o.InstallationType
Cb edition_id $o.EditionID
Cb display_version $o.DisplayVersion
Cb product_name $o.ProductName
}catch{Cb reason ('Reading the Windows version failed: '+$_.Exception.Message)}
# Each source of update ids has its own try: the build above is reported either way.
# Get-HotFix (Win32_QuickFixEngineering) is known to miss updates, so the servicing packages are read too.
$h=@()
try{$h+=@(Get-HotFix|ForEach-Object{$_.HotFixID})}catch{Cb note ('Get-HotFix failed: '+$_.Exception.Message)}
try{$h+=@(Get-ChildItem 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\Packages' -Name|ForEach-Object{[regex]::Match($_,'KB\d{4,9}').Value})}catch{Cb note ('The servicing packages could not be read: '+$_.Exception.Message)}
try{
$h=@($h|Where-Object{$_ -match '^KB\d{4,9}$'}|Sort-Object -Unique -Descending)
$n=$h.Count;$s=''
# At most 400 ids, in lines that end at a comma: Cb cuts a value at 3000 characters, never in the middle of an id.
foreach($i in ($h|Select-Object -First 400)){if($s.Length+$i.Length+1 -gt 2900){Cb hotfixes $s;$s=''};if($s){$s+=','};$s+=$i}
if($s){Cb hotfixes $s}
if($n -gt 400){Cb note ('Only 400 of '+$n+' installed update ids are listed.')}
}catch{Cb note ('The installed update list could not be read: '+$_.Exception.Message)}
Cb done 1
exit 0
