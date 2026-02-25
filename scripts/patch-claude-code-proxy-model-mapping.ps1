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

$patched = $raw
$changed = $false

if ($patched -notmatch "IFLOW_THREE_TIER_MAPPING_PATCH") {
  $pattern = [regex]::new("(?s)        # --- Mapping Logic --- START ---.*?        # --- Mapping Logic --- END ---")
  $counter = 0
  $patched = $pattern.Replace(
    $patched,
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
  $changed = $true
}

if ($patched -notmatch "IFLOW_TOOL_BRIDGE_PATCH") {
  $originalModelInject = @'
        original_model = body_json.get("model", "unknown")
        # IFLOW_TOOL_BRIDGE_PATCH
        # Keep original Claude model id so response conversion can preserve tool_use blocks.
        try:
            request.original_model = original_model
        except Exception:
            pass
'@
  $patched = $patched.Replace(
    "        original_model = body_json.get(""model"", ""unknown"")",
    $originalModelInject
  )

  $oldModelBridge = @'
        # Get the clean model name to check capabilities
        clean_model = original_request.model
        if clean_model.startswith("anthropic/"):
            clean_model = clean_model[len("anthropic/") :]
        elif clean_model.startswith("openai/"):
            clean_model = clean_model[len("openai/") :]

        # Check if this is a Claude model (which supports content blocks)
        is_claude_model = clean_model.startswith("claude-")
'@
  $newModelBridge = @'
        # IFLOW_TOOL_BRIDGE_PATCH
        # Use original incoming model id for protocol semantics (tool_use/tool_result),
        # even when internal routing maps to openai/*.
        source_model = getattr(original_request, "original_model", None) or original_request.model
        clean_model = source_model
        if isinstance(clean_model, str) and clean_model.startswith("anthropic/"):
            clean_model = clean_model[len("anthropic/") :]
        elif isinstance(clean_model, str) and clean_model.startswith("openai/"):
            clean_model = clean_model[len("openai/") :]
        elif not isinstance(clean_model, str):
            clean_model = str(clean_model or "")

        source_low = clean_model.strip().lower()
        is_claude_model = source_low.startswith("claude-")
'@
  $patched = $patched.Replace($oldModelBridge, $newModelBridge)
  $changed = $true
}

if ($changed -and $patched -ne $raw) {
  [System.IO.File]::WriteAllText($filePath, $patched, [System.Text.UTF8Encoding]::new($false))
  Write-Host "[patch-proxy] patched: $filePath"
} else {
  Write-Host "[patch-proxy] already patched."
}
