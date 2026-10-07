"""Discover and call the fixed REAPER 7.82 native API catalog."""
import json
import math
from pathlib import Path

CATALOG = json.loads((Path(__file__).resolve().parents[1] / 'native_api.json').read_text(encoding='utf-8'))
MAX_JSON = 2_000_000


def validate_call(function_name, arguments):
    entry = CATALOG['functions'].get(function_name)
    if entry is None:
        raise ValueError(f'Unknown documented API: {function_name}')
    if entry['blocked_reason']:
        raise ValueError(entry['blocked_reason'])
    if not isinstance(arguments, list) or len(arguments) > len(entry['arguments']):
        raise ValueError('arguments must be a positional JSON array matching the documented signature')
    minimum = sum(not a['optional'] for a in entry['arguments'])
    if len(arguments) < minimum:
        raise ValueError(f'{function_name} requires at least {minimum} arguments')
    def finite(value):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError('Non-finite numbers are not supported')
        if isinstance(value, list):
            for item in value:
                finite(item)
        if isinstance(value, dict):
            for item in value.values():
                finite(item)
    finite(arguments)
    if len(json.dumps(arguments)) > MAX_JSON:
        raise ValueError('Request exceeds 2 MB')
    # JSON null loses its array position in the legacy Lua JSON decoder.
    return [{'null': True} if a is None else a for a in arguments]


def register(mcp):
    from reaper_mcp.main import client

    @mcp.tool()
    async def reaper_api_search(query: str = '', offset: int = 0, limit: int = 30) -> dict:
        """Search the documented native REAPER API by name/description; no project changes.

        Discover exact signatures with reaper_api_describe before calling. This catalog
        includes operations not covered by the workflow tools: TakeFX, FX containers,
        input/monitor FX, automation items, MIDI CC, stretch markers, lanes, render
        options, routing, parameter modulation and audio accessors.
        """
        if offset < 0 or not 1 <= limit <= 100 or len(query) > 300:
            raise ValueError('offset >= 0, limit 1..100, query <= 300 characters required')
        found = [e for e in CATALOG['functions'].values()
                 if query.lower() in (e['name'] + ' ' + e['description']).lower()]
        return {'reaper_version': CATALOG['reaper_version'], 'total': len(found),
                'functions': [{k: e[k] for k in ('name', 'signature', 'blocked_reason')}
                              for e in found[offset:offset + limit]]}

    @mcp.tool()
    async def reaper_api_describe(function_name: str) -> dict:
        """Read exact native API argument types, return signature and Cockos documentation.

        Objects returned by calls are opaque session handles, never memory addresses.
        Pass a handle object back unchanged. For a track argument you can instead use
        {"track":0} (zero based) or {"track":-1} (master). ReaProject accepts 0 for
        the current project. Native strings containing binary data use {"bytes_hex":...}.
        reaper.array inputs use {"array_size":1024} or {"array":[0,1,...]} (max 65536).
        The call result includes every return value and the updated array inputs.
        """
        if function_name not in CATALOG['functions']:
            raise ValueError(f'Unknown documented API: {function_name}')
        return CATALOG['functions'][function_name]

    @mcp.tool()
    async def reaper_api_call(function_name: str, arguments: list, undo_description: str = '') -> dict:
        """Call one documented, statically registered native REAPER function.

        Read reaper_api_describe first. This can modify projects or files: use only
        within the user's requested scope. No source code, callbacks or shell commands
        are accepted. Supply a short undo_description for a project edit; leave empty
        for reads. No automatic save. UI/modal/native render calls may wait for a user.
        Handles are valid only in the running bridge session. Explicitly destroy native
        audio accessors/PCM sources when their documented lifecycle requires it.
        """
        if len(undo_description) > 160:
            raise ValueError('undo_description must be <= 160 characters')
        args = validate_call(function_name, arguments)
        return await client.execute('native_api_call', function_name=function_name,
                                    arguments=args, undo_description=undo_description)

    @mcp.tool()
    async def reaper_api_status() -> dict:
        """Read running bridge version, REAPER version and native API availability."""
        return await client.execute('native_api_status')

    @mcp.tool()
    async def fx_inspect_tree(track_index: int = -1, chain: str = 'normal') -> dict:
        """Inspect FX identities, offline/bypass state and nested containers without edits.

        chain: normal, input (regular tracks), or monitoring (master=-1).
        Registry matching is evidence of installation, not proof of successful loading:
        never delete a plugin merely because it is bypassed/offline or has few parameters.
        """
        if track_index < -1 or chain not in ('normal', 'input', 'monitoring'):
            raise ValueError('Invalid track or chain')
        if (chain == 'input' and track_index == -1) or (chain == 'monitoring' and track_index != -1):
            raise ValueError('Input FX require a regular track; monitoring FX require master')
        return await client.execute('native_fx_inspect_tree', track_index=track_index, chain=chain)
