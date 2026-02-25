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

if ($patched -notmatch "IFLOW_TOOL_CALLS_PATCH") {
  $toolResultPattern = [regex]::new('(?s)\n            # Special handling for tool_result in user messages.*?messages.append\(\{"role": msg.role, "content": processed_content\}\)\n')
  $toolResultBlock = @'

            # IFLOW_TOOL_RESULT_PATCH
            # IFLOW_TOOL_CALLS_PATCH
            # Special handling for tool_result in user messages
            # OpenAI/LiteLLM format expects tool results as role=tool with tool_call_id
            if msg.role == "user" and any(block.type == "tool_result" for block in content if hasattr(block, "type")):
                text_content = ""
                tool_messages = []

                for block in content:
                    if hasattr(block, "type") and block.type == "text":
                        text_content += block.text + "\n"
                    elif hasattr(block, "type") and block.type == "tool_result":
                        tool_call_id = block.tool_use_id if hasattr(block, "tool_use_id") else ""
                        result_content = block.content if hasattr(block, "content") else ""
                        result_text = parse_tool_result_content(result_content)
                        tool_messages.append({"role": "tool", "tool_call_id": tool_call_id, "content": result_text})

                if text_content.strip():
                    messages.append({"role": "user", "content": text_content.strip()})

                messages.extend(tool_messages)
            else:
                # For OpenAI models, convert tool_use blocks into tool_calls
                if anthropic_request.model.startswith("openai/"):
                    text_content = ""
                    tool_calls = []

                    for block in content:
                        if hasattr(block, "type") and block.type == "text":
                            text_content += block.text + "\n"
                        elif hasattr(block, "type") and block.type == "tool_use":
                            tool_id = block.id if hasattr(block, "id") and block.id else f"call_{uuid.uuid4().hex[:24]}"
                            tool_name = block.name if hasattr(block, "name") else ""
                            tool_input = block.input if hasattr(block, "input") else {}
                            tool_calls.append(
                                {
                                    "id": tool_id,
                                    "type": "function",
                                    "function": {"name": tool_name, "arguments": json.dumps(tool_input)},
                                }
                            )
                        elif hasattr(block, "type") and block.type == "tool_result":
                            # Rare in assistant role - keep as text
                            text_content += parse_tool_result_content(block.content if hasattr(block, "content") else "") + "\n"

                    msg_payload = {"role": msg.role, "content": text_content.strip()}
                    if tool_calls:
                        msg_payload["tool_calls"] = tool_calls
                    messages.append(msg_payload)
                else:
                    # Regular handling for other message types
                    processed_content = []
                    for block in content:
                        if hasattr(block, "type"):
                            if block.type == "text":
                                processed_content.append({"type": "text", "text": block.text})
                            elif block.type == "image":
                                processed_content.append({"type": "image", "source": block.source})
                            elif block.type == "tool_use":
                                # Handle tool use blocks if needed
                                processed_content.append(
                                    {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
                                )
                            elif block.type == "tool_result":
                                # Handle different formats of tool result content
                                processed_content_block = {
                                    "type": "tool_result",
                                    "tool_use_id": block.tool_use_id if hasattr(block, "tool_use_id") else "",
                                }

                                # Process the content field properly
                                if hasattr(block, "content"):
                                    if isinstance(block.content, str):
                                        # If it's a simple string, create a text block for it
                                        processed_content_block["content"] = [{"type": "text", "text": block.content}]
                                    elif isinstance(block.content, list):
                                        # If it's already a list of blocks, keep it
                                        processed_content_block["content"] = block.content
                                    else:
                                        # Default fallback
                                        processed_content_block["content"] = [{"type": "text", "text": str(block.content)}]
                                else:
                                    # Default empty content
                                    processed_content_block["content"] = [{"type": "text", "text": ""}]

                                processed_content.append(processed_content_block)

                    messages.append({"role": msg.role, "content": processed_content})

'@

  if ($toolResultPattern.IsMatch($patched)) {
    $patched = $toolResultPattern.Replace($patched, $toolResultBlock)
    $changed = $true
  } else {
    throw "[patch-proxy] failed: could not find tool_result block to patch."
  }
}

if ($changed -and $patched -ne $raw) {
  [System.IO.File]::WriteAllText($filePath, $patched, [System.Text.UTF8Encoding]::new($false))
  Write-Host "[patch-proxy] patched: $filePath"
} else {
  Write-Host "[patch-proxy] already patched."
}
