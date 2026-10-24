param([string]$ToolsRoot = $env:DAXQO_ANDROID_TOOLS)
$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $MyInvocation.MyCommand.Path
if (!$ToolsRoot) { throw 'Pass -ToolsRoot or set DAXQO_ANDROID_TOOLS.' }
$tools = $ToolsRoot
$env:JAVA_HOME = Join-Path $tools 'temurin-17'
$env:ANDROID_HOME = Join-Path $tools 'android-sdk'
$env:ANDROID_SDK_ROOT = $env:ANDROID_HOME
$gradle = Join-Path $tools 'gradle-8.10.2\bin\gradle.bat'
$apk = Join-Path $project 'app\build\outputs\apk\debug\app-debug.apk'
$dist = Join-Path $project 'dist'
New-Item -ItemType Directory -Force -Path $dist | Out-Null
Push-Location $project
try {
    & $gradle --offline --no-daemon assembleDebug
    if ($LASTEXITCODE -ne 0) { throw "Gradle build failed with exit code $LASTEXITCODE" }
    Copy-Item -LiteralPath $apk -Destination (Join-Path $dist 'fixture.apk') -Force
} finally {
    Pop-Location
}
