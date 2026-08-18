[CmdletBinding()]
param(
    [Parameter()]
    [string] $Config = (
        Join-Path $PSScriptRoot "..\configs\lineage.example.yml"
    ),

    [Parameter()]
    [string[]] $Job,

    [Parameter()]
    [switch] $Plan,

    [Parameter()]
    [switch] $Force,

    [Parameter()]
    [switch] $FailFast
)

$arguments = @(
    "-m",
    "lineage_utility",
    $(if ($Plan) { "plan" } else { "run" }),
    "--config",
    (Resolve-Path -LiteralPath $Config)
)

foreach ($jobName in $Job) {
    $arguments += @("--job", $jobName)
}
if ($Force -and -not $Plan) {
    $arguments += "--force"
}
if ($FailFast -and -not $Plan) {
    $arguments += "--fail-fast"
}

& python @arguments
exit $LASTEXITCODE

