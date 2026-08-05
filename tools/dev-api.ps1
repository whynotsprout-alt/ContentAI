$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"

if (!(Test-Path $VenvPython)) {
  throw "Missing .venv. Run tools/setup.ps1 first."
}

$env:PYTHONPATH = Join-Path $Root "apps/api/src"
$EnvFile = Join-Path $Root ".env"
$DotEnv = @{}
if (Test-Path $EnvFile) {
  Get-Content $EnvFile | ForEach-Object {
    if ($_ -match "^\s*([^#][^=]+?)=(.*)$") {
      $Name = $Matches[1].Trim()
      $Value = $Matches[2].Trim()
      $DotEnv[$Name] = $Value
      [Environment]::SetEnvironmentVariable($Name, $Value, "Process")
    }
  }
}
$Port = if ($DotEnv.ContainsKey("CONTENTAI_API_PORT")) { $DotEnv["CONTENTAI_API_PORT"] } else { "8000" }
$Reload = if ($DotEnv.ContainsKey("CONTENTAI_API_RELOAD")) { $DotEnv["CONTENTAI_API_RELOAD"] } else { "false" }
if ($Reload -eq "true") {
  $ApiSrc = Join-Path $Root "apps/api/src"
  & $VenvPython -m uvicorn contentai.api.app:app --host 127.0.0.1 --port $Port --reload --reload-dir $ApiSrc
} else {
  & $VenvPython -m uvicorn contentai.api.app:app --host 127.0.0.1 --port $Port
}

