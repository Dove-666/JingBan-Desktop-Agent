param(
    [string]$SdkPath = 'E:\Android\Sdk',
    [string]$JdkPath = 'E:\Biancheng',
    [string]$SigningDirectory = 'E:\Android\keys',
    [switch]$SkipTests
)
$ErrorActionPreference = 'Stop'
$projectDirectory = $PSScriptRoot
if (-not (Test-Path -LiteralPath (Join-Path $JdkPath 'bin\java.exe'))) { throw 'Select an installed JDK with -JdkPath.' }
if (-not (Test-Path -LiteralPath (Join-Path $SdkPath 'platforms\android-35\android.jar'))) { throw 'Android SDK platform 35 is required.' }
New-Item -ItemType Directory -Force -Path $SigningDirectory | Out-Null
Add-Type -AssemblyName System.Security
$keyPath = Join-Path $SigningDirectory 'aura-companion-release.p12'
$secretPath = Join-Path $SigningDirectory 'aura-companion-password.dpapi'
if ((Test-Path -LiteralPath $keyPath) -xor (Test-Path -LiteralPath $secretPath)) {
    throw 'The signing key and its protected credential must be restored together; neither will be overwritten.'
}
$previousJava = $env:JAVA_HOME
$previousSdk = $env:ANDROID_HOME
$previousUserHome = $env:ANDROID_USER_HOME
$previousAvdHome = $env:ANDROID_AVD_HOME
$previousKey = $env:AURA_ANDROID_KEYSTORE
$previousPassword = $env:AURA_ANDROID_STORE_PASSWORD
try {
    $env:JAVA_HOME = $JdkPath
    $env:ANDROID_HOME = $SdkPath
    $env:ANDROID_USER_HOME = 'E:\Android\user'
    $env:ANDROID_AVD_HOME = 'E:\Android\avd'
    $env:AURA_ANDROID_KEYSTORE = $keyPath
    if (-not (Test-Path -LiteralPath $secretPath)) {
        $randomBytes = New-Object byte[] 32
        $randomSource = [System.Security.Cryptography.RandomNumberGenerator]::Create()
        $randomSource.GetBytes($randomBytes)
        $randomSource.Dispose()
        $passwordBytes = [Text.Encoding]::UTF8.GetBytes([Convert]::ToBase64String($randomBytes))
        [Array]::Clear($randomBytes, 0, $randomBytes.Length)
        $protected = [Security.Cryptography.ProtectedData]::Protect($passwordBytes, $null, [Security.Cryptography.DataProtectionScope]::CurrentUser)
        [IO.File]::WriteAllBytes($secretPath, $protected)
        [Array]::Clear($passwordBytes, 0, $passwordBytes.Length)
    }
    $passwordBytes = [Security.Cryptography.ProtectedData]::Unprotect([IO.File]::ReadAllBytes($secretPath), $null, [Security.Cryptography.DataProtectionScope]::CurrentUser)
    $env:AURA_ANDROID_STORE_PASSWORD = [Text.Encoding]::UTF8.GetString($passwordBytes)
    [Array]::Clear($passwordBytes, 0, $passwordBytes.Length)
    if (-not (Test-Path -LiteralPath $keyPath)) {
        & (Join-Path $JdkPath 'bin\keytool.exe') -genkeypair -keystore $keyPath -storetype PKCS12 -alias aura-companion -keyalg RSA -keysize 3072 -validity 10000 -dname 'CN=AURA Companion, OU=Personal Development, O=AURA' -storepass:env AURA_ANDROID_STORE_PASSWORD -keypass:env AURA_ANDROID_STORE_PASSWORD
        if ($LASTEXITCODE -ne 0) { throw 'Could not create the local APK signing key.' }
    }
    Push-Location $projectDirectory
    try {
        if (-not $SkipTests) {
            & .\gradlew.bat :app:testDebugUnitTest :app:lintRelease --console=plain
            if ($LASTEXITCODE -ne 0) { throw 'Android verification failed.' }
        }
        & .\gradlew.bat :app:assembleRelease --console=plain
        if ($LASTEXITCODE -ne 0) { throw 'APK build failed.' }
        $delivery = Join-Path $projectDirectory 'deliverables'
        New-Item -ItemType Directory -Force -Path $delivery | Out-Null
        $releaseMetadata = Get-Content -Encoding utf8 -Raw -LiteralPath (Join-Path $projectDirectory 'app\build\outputs\apk\release\output-metadata.json') | ConvertFrom-Json
        $releaseVersion = $releaseMetadata.elements[0].versionName
        if ($releaseVersion -notmatch '^\d+\.\d+\.\d+$') { throw 'Unexpected release version metadata.' }
        foreach ($architecture in @('arm64-v8a', 'armeabi-v7a', 'x86_64')) {
            Copy-Item -LiteralPath (Join-Path $projectDirectory "app\build\outputs\apk\release\app-$architecture-release.apk") -Destination (Join-Path $delivery "AURA-$releaseVersion-$architecture.apk") -Force
        }
        Copy-Item -LiteralPath (Join-Path $projectDirectory 'app\build\outputs\apk\release\app-universal-release.apk') -Destination (Join-Path $delivery "AURA-$releaseVersion-universal.apk") -Force
        Get-ChildItem -LiteralPath $delivery -Filter "AURA-$releaseVersion-*.apk" | Select-Object Name,Length
    } finally { Pop-Location }
} finally {
    $env:JAVA_HOME = $previousJava
    $env:ANDROID_HOME = $previousSdk
    $env:ANDROID_USER_HOME = $previousUserHome
    $env:ANDROID_AVD_HOME = $previousAvdHome
    $env:AURA_ANDROID_KEYSTORE = $previousKey
    $env:AURA_ANDROID_STORE_PASSWORD = $previousPassword
}
