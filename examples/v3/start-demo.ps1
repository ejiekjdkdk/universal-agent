param(
    [ValidateSet("both", "demo_a", "demo_b")] [string] $Demo = "both",
    [string] $AgentRoot = (Join-Path $PSScriptRoot "../.."),
    [string] $Projects = $PSScriptRoot,
    [string] $State,
    [string] $Python = "python",
    [int] $Port = 8765
)
$ErrorActionPreference = "Stop"
$core = (Resolve-Path -LiteralPath $AgentRoot).Path
if (-not $env:AGENT_ACCESS_TOKEN) {
    $env:AGENT_ACCESS_TOKEN = [Guid]::NewGuid().ToString("N")
    Write-Host "Local demo access token (paste into the workbench): $env:AGENT_ACCESS_TOKEN"
}
if (-not $State) { $State = Join-Path $core "var/v3/$Demo" }
$env:AGENT_SKILL_ROOT = Join-Path ([IO.Path]::GetFullPath($State)) "skills"
$env:AGENT_DATABASE = Join-Path ([IO.Path]::GetFullPath($State)) "tasks.db"
$env:AGENT_PORT = "$Port"

$bindings = @{
    demo_a = @{ business = "demo_records"; skill = "record-lookup"; tools = @("read_record") }
    demo_b = @{ business = "demo_fragments"; skill = "fragment-summary"; tools = @("summarize_fragments", "retrieve_knowledge") }
}
$ctl = Join-Path $core "scripts/skillctl.py"
$selected = if ($Demo -eq "both") { "demo_a", "demo_b" } else { @($Demo) }
$scratchParent = (Resolve-Path -LiteralPath ([IO.Path]::GetTempPath())).Path
$scratchName = "agent-v3-" + [Guid]::NewGuid().ToString("N")
$scratch = (New-Item -ItemType Directory -Path (Join-Path $scratchParent $scratchName)).FullName
try {
    $env:AGENT_SCENARIOS = (New-Item -ItemType Directory -Path (Join-Path $scratch "scenarios")).FullName
    $env:AGENT_PRINCIPALS_FILE = Join-Path $scratch "principals.json"
    $reader = $null
    foreach ($id in $selected) {
        $project = (Resolve-Path -LiteralPath (Join-Path $Projects $id)).Path
        $binding = $bindings[$id]
        $source = Join-Path $project "scenarios/$($binding.business)"
        Copy-Item -LiteralPath $source -Destination $env:AGENT_SCENARIOS -Recurse
        $role = @(Get-Content -LiteralPath (Join-Path $project "principals.json") -Encoding UTF8 -Raw | ConvertFrom-Json)[0]
        if ($null -eq $reader) {
            $reader = $role
        } else {
            foreach ($field in "tenant_id", "org_id", "user_id", "token_env") {
                if ($reader.$field -cne $role.$field) { throw "Demo reader identities differ." }
            }
            $reader.grants | Add-Member -NotePropertyName $binding.business -NotePropertyValue $role.grants.($binding.business)
        }
        & $Python $ctl --root $env:AGENT_SKILL_ROOT import (Join-Path $project "skills/$($binding.skill)")
        if ($LASTEXITCODE -ne 0) { throw "Skill import failed." }
        $toolArgs = foreach ($name in $binding.tools) { "--tool"; $name }
        & $Python $ctl --root $env:AGENT_SKILL_ROOT publish $binding.business $binding.skill 1.0.0 --scenarios $env:AGENT_SCENARIOS @toolArgs
        if ($LASTEXITCODE -ne 0) { throw "Skill publication failed." }
    }
    $roles = ConvertTo-Json -InputObject @($reader) -Depth 8
    [IO.File]::WriteAllText($env:AGENT_PRINCIPALS_FILE, $roles, [Text.UTF8Encoding]::new($false))
    Write-Host "Workbench: http://127.0.0.1:$Port/"
    & $Python (Join-Path $core "run.py")
    exit $LASTEXITCODE
} finally {
    $target = Get-Item -LiteralPath $scratch
    if ($target.Parent.FullName -eq $scratchParent.TrimEnd('\','/') -and $target.Name -ceq $scratchName -and
        -not ($target.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        Remove-Item -LiteralPath $target.FullName -Recurse -Force
    }
}
