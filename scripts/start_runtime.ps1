param([switch]$Check)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
try {
    $registered = [IO.File]::ReadAllText((Join-Path $root '.runtime-python')).TrimEnd([char[]]"`r`n")
    if ($registered -notmatch '^[A-Za-z]:\\' -or
        [IO.Path]::GetFileName($registered) -ine 'python.exe' -or
        -not [IO.File]::Exists($registered)) {
        throw 'Invalid interpreter registration'
    }
} catch {
    [Console]::Error.WriteLine('Register a verified isolated Python first: "PATH\TO\env\Scripts\python.exe" -I scripts\start_runtime.py --register')
    exit 1
}
$mode = if ($Check) { '--check' } else { '--launch' }
& $registered -I (Join-Path $PSScriptRoot 'start_runtime.py') $mode
exit $LASTEXITCODE
