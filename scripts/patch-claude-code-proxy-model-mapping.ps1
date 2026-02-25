param()

$ErrorActionPreference = "Stop"

function Resolve-FastApiFile {
  $candidates = @(
    (Join-Path $env:APPDATA "Python\Python311\site-packages\server\fastapi.py"),
    (Join-Path $env:APPDATA "Python\Python312\site-packages\server\fastapi.py"),
    (Join-Path $env:APPDATA "Python\Python313\site-packages\server\fastapi.py"),
    (Join-Path $env:APPDATA "Python\Python314\site-packages\server\fastapi.py")
  )
  foreach ($candidate in $candidates) {
    if (Test-Path $candidate) { return $candidate }
  }
  return $null
}

$filePath = Resolve-FastApiFile
if (-not $filePath) {
  Write-Host "[patch-proxy] skip: fastapi.py not found."
  exit 0
}

$raw = Get-Content -Raw -Encoding UTF8 $filePath
if ($raw -match "IFLOW_THREE_TIER_MAPPING_PATCH") {
  Write-Host "[patch-proxy] already patched."
  exit 0
}

$mappingBlock = @'
        # --- Mapping Logic --- START ---
        mapped = False
        low = clean_v.lower()
        middle_model = os.environ.get("MIDDLE_MODEL", "").strip()

        # IFLOW_THREE_TIER_MAPPING_PATCH
        # Claude model families -> iFlow 3-tier mapping
        if "opus" in low:
            new_model = f"openai/{BIG_MODEL}"
            mapped = True
        elif "sonnet" in low:
            new_model = f"openai/{middle_model or BIG_MODEL}"
            mapped = True
        elif "haiku" in low:
            new_model = f"openai/{SMALL_MODEL}"
            mapped = True
        elif low.startswith("claude-"):
            new_model = f"openai/{middle_model or BIG_MODEL}"
            mapped = True

        # Add prefixes to non-mapped models if they match known lists
        elif not mapped:
            if clean_v in GEMINI_MODELS and not v.startswith("gemini/"):
                new_model = f"gemini/{clean_v}"
                mapped = True  # Technically mapped to add prefix
            elif clean_v in OPENAI_MODELS and not v.startswith("openai/"):
                new_model = f"openai/{clean_v}"
                mapped = True  # Technically mapped to add prefix
        # --- Mapping Logic --- END ---
'@

$pattern = [regex]::new("(?s)        # --- Mapping Logic --- START ---.*?        # --- Mapping Logic --- END ---")
$counter = 0
$patched = $pattern.Replace(
  $raw,
  {
    param($m)
    $script:counter += 1
    if ($script:counter -le 2) { return $mappingBlock }
    return $m.Value
  }
)

if ($counter -lt 2) {
  throw "[patch-proxy] failed: expected 2 mapping blocks, found $counter."
}

[System.IO.File]::WriteAllText($filePath, $patched, [System.Text.UTF8Encoding]::new($false))
Write-Host "[patch-proxy] patched: $filePath"
