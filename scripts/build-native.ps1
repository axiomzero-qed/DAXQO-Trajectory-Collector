param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('standard', 'xm')]
    [string]$Platform,
    [int]$VersionCode = 8,
    [Parameter(Mandatory = $true)]
    [string]$Source,
    [string]$ToolsRoot = $env:DAXQO_ANDROID_TOOLS
)

$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
$stageAndroid = Join-Path $project 'dist\native-v05\android'
if (!$ToolsRoot -or !(Test-Path -LiteralPath $ToolsRoot)) { throw 'Pass -ToolsRoot or set DAXQO_ANDROID_TOOLS.' }
$gradleRelative = 'gradle-8.10.2\bin\gradle.bat'
$sdkRelative = 'android-sdk'
$jdkRelative = 'temurin-17'
$substExe = Join-Path $env:WINDIR 'System32\subst.exe'
$mappedDrives = [System.Collections.Generic.List[string]]::new()
$pushed = $false

function Get-FreeAsciiDrives([int]$Count) {
    $used = @([System.IO.DriveInfo]::GetDrives() | ForEach-Object { $_.Name.Substring(0, 1).ToUpperInvariant() })
    $available = @('R','S','T','U','V','W','X','Y','Z' | Where-Object { $_ -notin $used })
    if ($available.Count -lt $Count) { throw 'Two free temporary drive letters are required.' }
    return $available[0..($Count - 1)]
}

try {
    $prepareArgs = @((Join-Path $PSScriptRoot 'prepare_native.py'), '--version-code', "$VersionCode")
    if ($Source) { $prepareArgs += @('--source', $Source) }
    & python @prepareArgs
    if ($LASTEXITCODE -ne 0) { throw 'Could not prepare the v0.5 staged source.' }

    $letters = Get-FreeAsciiDrives 2
    $toolDrive = "$($letters[0]):"
    $sourceDrive = "$($letters[1]):"
    & $substExe $toolDrive $toolsRoot
    if ($LASTEXITCODE -ne 0) { throw 'Could not map the local Android tools path.' }
    $mappedDrives.Add($toolDrive)
    & $substExe $sourceDrive $stageAndroid
    if ($LASTEXITCODE -ne 0) { throw 'Could not map the staged Android project path.' }
    $mappedDrives.Add($sourceDrive)

    $mappedToolsRoot = "$toolDrive\"
    $mappedSourceRoot = "$sourceDrive\"
    $env:JAVA_HOME = Join-Path $mappedToolsRoot $jdkRelative
    $env:ANDROID_HOME = Join-Path $mappedToolsRoot $sdkRelative
    $env:ANDROID_SDK_ROOT = $env:ANDROID_HOME
    $env:PATH = (Join-Path $env:JAVA_HOME 'bin') + ';' + $env:PATH
    $escapedSdk = $env:ANDROID_HOME.Replace('\', '\\')
    "sdk.dir=$escapedSdk" | Set-Content -LiteralPath (Join-Path $mappedSourceRoot 'local.properties') -Encoding ASCII

    Push-Location $mappedSourceRoot
    $pushed = $true
    $gradle = Join-Path $mappedToolsRoot $gradleRelative
    & $gradle --no-daemon "-Pplatform=$Platform" -PtrajectoryBuild=true assembleDebug lintDebug
    if ($LASTEXITCODE -ne 0) { throw "Gradle assembleDebug/lintDebug failed for $Platform." }

    $apk = Join-Path $mappedSourceRoot 'app\build\outputs\apk\debug\app-debug.apk'
    if (!(Test-Path -LiteralPath $apk)) { throw 'Gradle did not produce the expected debug APK.' }
    $aapt = Join-Path $mappedToolsRoot 'android-sdk\build-tools\35.0.0\aapt.exe'
    $badging = & $aapt dump badging $apk
    if ($LASTEXITCODE -ne 0) { throw 'Could not inspect the built APK.' }
    $expectedPackage = if ($Platform -eq 'xm') { 'io.daxqo.xm' } else { 'io.daxqo.app' }
    $packageLine = $badging | Where-Object { $_ -match '^package:' } | Select-Object -First 1
    if ($packageLine -notmatch "name='$expectedPackage'") { throw "Unexpected APK package for $Platform." }
    if ($packageLine -notmatch "versionCode='$VersionCode'") { throw 'APK versionCode does not match the requested value.' }
    if ($packageLine -notmatch "versionName='0\.5\.0(?:-xm)?'") { throw 'APK does not retain the v0.5 version name.' }

    $destinationName = if ($Platform -eq 'xm') { 'daxqo-xm-collector-v05.apk' } else { 'daxqo-standard-collector-v05.apk' }
    $destination = Join-Path $project "dist\$destinationName"
    Copy-Item -LiteralPath $apk -Destination $destination -Force
    Write-Host "Verified APK: $destination"
    Write-Host $packageLine
} finally {
    if ($pushed) { Pop-Location }
    for ($index = $mappedDrives.Count - 1; $index -ge 0; $index--) {
        & $substExe $mappedDrives[$index] /D | Out-Null
    }
}
