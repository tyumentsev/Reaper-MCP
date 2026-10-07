-- This source fragment is embedded into reaper_mcp_server.lua at build time.
-- No dynamic Lua source execution or arbitrary function-name lookup.
local native_api = {}
local native_handles = {}
local native_next_handle = 0
local native_session = reaper.genGuid()
local native_pointer_kinds = {
  MediaTrack=true, MediaItem=true, MediaItem_Take=true, TrackEnvelope=true,
  ReaProject=true, PCM_source=true, AudioAccessor=true, ProjectMarker=true,
}

local function native_finite(n)
  return type(n) == "number" and n == n and n ~= math.huge and n ~= -math.huge
end

local function native_decode(value, spec)
  local kind = spec.kind
  if value == nil or (type(value) == "table" and value.null == true) then
    if spec.optional then return nil end
    error("Required argument " .. kind .. " cannot be null")
  end
  if kind == "integer" or kind == "number" then
    if not native_finite(value) or (kind == "integer" and math.floor(value) ~= value) then
      error("Expected finite " .. kind)
    end
    return value
  elseif kind == "boolean" then
    if type(value) ~= "boolean" then error("Expected boolean") end
    return value
  elseif kind == "string" then
    if type(value) == "table" and type(value.bytes_hex) == "string" then
      local hex = value.bytes_hex
      if #hex > 2000000 or #hex % 2 ~= 0 or hex:find("[^0-9a-fA-F]") then error("Invalid bytes_hex") end
      return (hex:gsub("..", function(pair) return string.char(tonumber(pair, 16)) end))
    end
    if type(value) ~= "string" or #value > 2000000 then error("Expected string <= 2 MB") end
    return value
  elseif kind == "ReaProject" and value == 0 then
    return 0
  elseif kind == "MediaTrack" and type(value) == "table" and value.track ~= nil then
    local idx = value.track
    if not native_finite(idx) or math.floor(idx) ~= idx or idx < -1 then error("Invalid track selector") end
    local tr = idx == -1 and reaper.GetMasterTrack(0) or reaper.GetTrack(0, idx)
    if not tr then error("Track does not exist") end
    return tr
  elseif kind == "reaper.array" then
    if type(value) ~= "table" then error("Expected array descriptor") end
    local size = value.array_size or (type(value.array) == "table" and #value.array)
    if not native_finite(size) or math.floor(size) ~= size or size < 1 or size > 65536 then
      error("Array size must be 1..65536")
    end
    if value.array then
      for _, n in ipairs(value.array) do if not native_finite(n) then error("Array must contain finite numbers") end end
      return reaper.new_array(value.array)
    end
    return reaper.new_array(size)
  end
  if type(value) ~= "table" or type(value.handle) ~= "string" then error("Expected " .. kind .. " session handle") end
  local entry = native_handles[value.handle]
  if not entry or (kind ~= "identifier" and entry.kind ~= kind) then error("Unknown, expired or incorrectly typed handle") end
  if native_pointer_kinds[entry.kind] and not reaper.ValidatePtr(entry.value, entry.kind .. "*") then
    native_handles[value.handle] = nil
    error("REAPER object was deleted or its project was closed")
  end
  return entry.value
end

local function native_encode(value, kind)
  if value == nil then return {null=true} end
  if type(value) == "userdata" then
    native_next_handle = native_next_handle + 1
    if native_next_handle > 100000 then error("Handle budget exhausted; restart bridge") end
    local id = native_session .. ":" .. native_next_handle
    native_handles[id] = {value=value, kind=kind}
    return {handle=id, type=kind}
  end
  if type(value) == "string" and (value:find("%z") or not utf8.len(value)) then
    return {bytes_hex=(value:gsub(".", function(c) return string.format("%02x", string.byte(c)) end))}
  end
  if type(value) == "number" and not native_finite(value) then return {number=tostring(value)} end
  if type(value) == "string" or type(value) == "number" or type(value) == "boolean" then return value end
  error("Unsupported native result type: " .. type(value))
end

function native_api.native_api_status()
  local available, unavailable = 0, {}
  for name in pairs(native_catalog) do
    if reaper.APIExists(name) then available = available + 1 else unavailable[#unavailable+1] = name end
  end
  table.sort(unavailable)
  return {bridge_version="0.9.0", reaper_version=reaper.GetAppVersion(),
          catalog_version="7.82", available_count=available, unavailable=unavailable, session=native_session}
end

function native_api.native_api_call(p)
  local spec = native_catalog[p.function_name]
  if not spec then return nil, "Unknown or excluded native API function" end
  if not reaper.APIExists(p.function_name) then return nil, "API not available in this REAPER build" end
  if type(p.arguments) ~= "table" or #p.arguments > #spec.args then return nil, "Invalid argument count" end
  local args, arrays = {}, {}
  for i, arg_spec in ipairs(spec.args) do
    local ok, value = pcall(native_decode, p.arguments[i], arg_spec)
    if not ok then return nil, "Argument " .. i .. ": " .. tostring(value) end
    args[i] = value
    if arg_spec.kind == "reaper.array" and value then arrays[#arrays+1] = {index=i, value=value} end
  end
  if p.function_name == "DeleteTrack" and args[1] == reaper.GetMasterTrack(0) then
    return nil, "The master track cannot be deleted"
  end
  if p.function_name == "GetAudioAccessorSamples" then
    local descriptor = p.arguments[6]
    local capacity = descriptor.array_size or #descriptor.array
    if args[3] < 1 or args[5] < 0 or args[3] * args[5] > capacity then
      return nil, "Audio sample buffer is smaller than channels * samples per channel"
    end
  end
  local label = p.undo_description or ""
  if type(label) ~= "string" or #label > 640 then return nil, "Invalid undo description" end
  local project_at_start = reaper.EnumProjects(-1, "")
  if label ~= "" then reaper.Undo_BeginBlock2(project_at_start) end
  local result = table.pack(pcall(spec.fn, table.unpack(args, 1, #spec.args)))
  if label ~= "" then reaper.Undo_EndBlock2(project_at_start, "MCP API: " .. label, -1) end
  if not result[1] then return nil, tostring(result[2]) end
  local values, buffers = {}, {}
  for i=2,result.n do
    local ok, encoded = pcall(native_encode, result[i], spec.returns[i-1] or "opaque")
    if not ok then return nil, tostring(encoded) end
    values[#values+1] = {index=i-1, value=encoded}
  end
  for _, arr in ipairs(arrays) do buffers[#buffers+1] = {index=arr.index, values=arr.value.table()} end
  return {function_name=p.function_name, returns=values, arrays=buffers}
end

function native_api.native_fx_inspect_tree(p)
  local ok, tr = pcall(native_decode, {track=p.track_index}, {kind="MediaTrack"})
  if not ok then return nil, tostring(tr) end
  local chain = p.chain or "normal"
  if chain ~= "normal" and chain ~= "input" and chain ~= "monitoring" then return nil, "Invalid FX chain" end
  if (chain == "input" and p.track_index == -1) or (chain == "monitoring" and p.track_index ~= -1) then
    return nil, "Input/monitoring chain does not match track"
  end
  local installed_names, installed_ids = {}, {}
  local n = 0
  while true do
    local exists, name, ident = reaper.EnumInstalledFX(n)
    if not exists then break end
    installed_names[name] = true
    installed_ids[ident] = true
    n = n + 1
  end
  local count = chain == "normal" and reaper.TrackFX_GetCount(tr) or reaper.TrackFX_GetRecCount(tr)
  local base = chain == "normal" and 0 or 0x1000000
  local results, visited = {}, {}
  local function named(idx, key)
    local found, value = reaper.TrackFX_GetNamedConfigParm(tr, idx, key)
    return found and value or ""
  end
  local function visit(idx, parent)
    if visited[idx] or #results >= 1000 then return end
    visited[idx] = true
    local _, label = reaper.TrackFX_GetFXName(tr, idx, "")
    local original, ident = named(idx,"fx_name"), named(idx,"fx_ident")
    local children = tonumber(named(idx,"container_count")) or 0
    results[#results+1] = {
      index=idx, parent=parent, name=label, original_name=original, identifier=ident,
      guid=reaper.TrackFX_GetFXGUID(tr,idx), fx_type=named(idx,"fx_type"),
      enabled=reaper.TrackFX_GetEnabled(tr,idx), offline=reaper.TrackFX_GetOffline(tr,idx),
      parameter_count=reaper.TrackFX_GetNumParams(tr,idx), container_count=children,
      parallel=named(idx,"parallel"), latency_samples=named(idx,"pdc"),
      registry_match=(installed_names[original] or installed_ids[ident]) == true,
      load_status="not_inferred_from_registry",
    }
    for child=0,children-1 do
      local child_idx=tonumber(named(idx,"container_item." .. child))
      if child_idx then visit(child_idx,idx) end
    end
  end
  for i=0,count-1 do visit(base+i,-1) end
  return {track_index=p.track_index, chain=chain, fx=results, truncated=#results>=1000}
end
