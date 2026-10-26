# Run Cargo from a normal PowerShell session with MSVC available when installed.
$ErrorActionPreference = 'Stop'
if ($IsWindows -and -not (Get-Command link.exe -ErrorAction SilentlyContinue)) {
    $taskVsWhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
    if (Test-Path -LiteralPath $taskVsWhere) {
        $taskVsInstall = & $taskVsWhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
        if ($taskVsInstall) {
            $taskDevShell = Join-Path $taskVsInstall 'Common7\Tools\Microsoft.VisualStudio.DevShell.dll'
            Import-Module -Name $taskDevShell
            Enter-VsDevShell -VsInstallPath $taskVsInstall -SkipAutomaticLocation -DevCmdArguments '-arch=x64 -host_arch=x64' | Out-Null
        }
    }
}
& cargo @args
exit $LASTEXITCODE
