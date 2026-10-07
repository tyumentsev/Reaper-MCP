# REAPER 7.82 native API transport (fork 0.9.0)

This fork retains the upstream workflow tools and adds five tools:

- `reaper_api_search(query, offset, limit)` searches the pinned Cockos catalog.
- `reaper_api_describe(function_name)` returns the exact Lua signature, argument types and a link to the Cockos documentation.
- `reaper_api_call(function_name, arguments, undo_description)` calls a statically registered function.
- `reaper_api_status()` checks the running bridge and native function availability.
- `fx_inspect_tree(track_index, chain)` inspects ordinary/input/monitor FX, including nested containers.

The catalog contains **732 documented native Lua functions from REAPER 7.82**.
**724 are registered for dispatch**, subject to availability in the running host.
This is API transport coverage, not a claim that every function, third-party plugin,
GUI workflow or operating-system combination has been integration tested. Lua-only
helpers/callbacks (`defer`, graphics event loops, and similar facilities) are not native
RPC functions. SWS, ReaImGui and other extension APIs require separate catalogs.

## Using the tools

Search and describe before invoking a function. Arguments are positional and follow
the **Lua** signature, not the C or Python signature. Required arguments cannot be
omitted. Optional nulls retain their position. Read requests need no undo label.
For an edit, supply `undo_description`; project edits execute in a named undo block.
No automatic project save is performed. File exports and device changes may not be
undoable even when they are native API calls. Modal dialogs can require user input.

Examples (arguments to `reaper_api_call`):

```json
{"function_name":"GetMediaTrackInfo_Value","arguments":[{"track":-1},"D_VOL"]}
{"function_name":"SetMediaTrackInfo_Value","arguments":[{"track":0},"B_PHASE",1],"undo_description":"Invert track polarity"}
{"function_name":"TrackFX_GetNamedConfigParm","arguments":[{"track":0},0,"container_count"]}
{"function_name":"TrackFX_GetNamedConfigParm","arguments":[{"track":0},0,"fx_ident"]}
{"function_name":"GetSetProjectInfo","arguments":[0,"RENDER_SRATE",48000,true],"undo_description":"Set render sample rate"}
```

Return values are positional records, preserving false, zero and nil:
`{"returns":[{"index":1,"value":...}],"arrays":...}` inside the bridge response's `data`.
Object results are `{ "handle": "session:counter", "type": "MediaItem" }`.
Pass that object back unchanged. Native memory addresses are not accepted. Handles
are scoped to the bridge process; restarting it invalidates them. Supported REAPER
pointer types are checked with `ValidatePtr` before use. `{"track":0}` selects a
zero-based current-project track; `{"track":-1}` selects the master. Project `0`
means the current project. Other objects are obtained through APIs such as `GetMediaItem`,
`GetActiveTake`, `GetTrackEnvelope` and `EnumProjects`.

Binary strings (MIDI event buffers, SysEx, etc.) are encoded as `{"bytes_hex":"..."}`
when they contain NUL bytes or invalid UTF-8; the same representation is accepted on
input. `reaper.array` arguments use `{"array_size":1024}` or `{"array":[0,0,...]}`.
Modified arrays are returned with their argument index. Arrays are limited to 65,536
samples/values and requests to 2 MB; read audio in chunks. Audio Accessors read the
signal described by Cockos' accessor API; they are not a live post-master audio stream.
Destroy native accessors/sources after use according to their documented lifecycle.

## Coverage of the workflow gaps

| Workflow | Native API path now exposed |
|---|---|
| Plugin diagnostics | `fx_inspect_tree`, `TrackFX_GetOffline`, `TrackFX_GetNamedConfigParm`, `EnumInstalledFX` |
| Nested containers | `TrackFX_GetNamedConfigParm`: `container_count`, `container_item.X`, parameter mappings |
| Parallel FX | `TrackFX_SetNamedConfigParm`: `parallel` |
| Input/monitor FX | Native FX indices with `0x1000000`; `TrackFX_GetRecCount` |
| Take FX | `TakeFX_*` functions with a take handle |
| Parameter modulation | `TrackFX_SetNamedConfigParm`: `param.X.lfo.*`, `acs.*`, `plink.*`, `learn.*` |
| Routing and pins | `CreateTrackSend`, `SetTrackSendInfo_Value`, `TrackFX_SetPinMappings` |
| Polarity/width/pan law | `SetMediaTrackInfo_Value`: `B_PHASE`, `D_WIDTH`, `D_PANLAW`, `I_PANMODE` |
| Automation items | `InsertAutomationItem`, `GetSetAutomationItemInfo`, envelope `*Ex` APIs |
| Master/take/send envelopes | Native envelope handles, `GetFXEnvelope`, `GetTakeEnvelopeByName`, send envelope API |
| Stretch markers | `GetTakeStretchMarker`, `SetTakeStretchMarker`, `SetTakeStretchMarkerSlope` |
| Fixed lanes/Razor edits | Track/item native property APIs, `P_RAZOREDITS_EXT` |
| CC/SysEx | `MIDI_GetCC`, `MIDI_SetCC`, `MIDI_GetTextSysexEvt`, `MIDI_GetAllEvts` |
| Render settings | `GetSetProjectInfo`, `GetSetProjectInfo_String` |
| Audio buffers | `CreateTrackAudioAccessor`, `CreateTakeAudioAccessor`, `GetAudioAccessorSamples` |
| New 7.82 theme API | `EnumThemeColors` |

Registry matching in the FX inspector does **not** prove that a plugin has loaded.
Bypassed/offline FX and plugins with no exposed parameters must not be deleted on
that evidence alone. The inspector intentionally reports observations, not an
unreliable blanket `missing=true` judgment.

## Explicit exclusions and lifecycle

`ExecProcess`, `AddRemoveReaScript`, `ReaScriptError`, `PreventUIRefresh`, and the four
`Undo_BeginBlock[2]`/`Undo_EndBlock[2]` functions are catalogued but not callable.
The bridge owns undo/UI-refresh boundaries; external process execution and arbitrary
script registration are not part of this transport. Existing `script_run` remains
limited to REAPER's Scripts folder. There is no `load`, `loadstring`, or supplied
Lua source execution. All callable native names are generated into a static table.
Native project/file operations are powerful: callers must follow the user's scope.

The new bridge uses `%TEMP%/reaper_mcp_v09` (`$TMPDIR` or `/tmp` elsewhere).
Both Python and Lua must be upgraded together. The previous bridge uses another
directory, allowing a staged switch without sending new requests to an old handler.

## Reproducibility

Download the official 7.82 reference and run:

```sh
python scripts/generate_api_catalog.py /path/to/reascripthelp.html
pytest tests -q
ruff check .
```

The catalog records the reference SHA-256 and source URL. Generated Lua and the JSON
catalog are committed; runtime installation does not scrape documentation or generate
code. The runtime fragment is embedded into the single installed Lua script.
