import json
from pathlib import Path

import lupa
import pytest

from reaper_mcp.tools.native_api_tools import CATALOG, validate_call

ROOT = Path(__file__).resolve().parents[1]


def runtime():
    lua = lupa.LuaRuntime(unpack_returned_tuples=True)
    lua.execute('''
      reaper = {
        genGuid=function() return "test-session" end,
        APIExists=function() return true end,
        GetAppVersion=function() return "7.82/x64" end,
        EnumProjects=function() return 0, "" end,
        Undo_BeginBlock2=function() undo_open=true end,
        Undo_EndBlock2=function() undo_open=false end,
        ValidatePtr=function() return false end
      }
      native_catalog = {
        Echo={args={{kind="string"},{kind="integer"}}, returns={"string","integer"},
              fn=function(a,b) return a,b end},
        Fails={args={}, returns={},fn=function() error("native failure") end},
        Pointer={args={{kind="MediaTrack"}}, returns={},fn=function() error("must not run") end},
        Optional={args={{kind="optional",optional=true},{kind="integer"}},returns={"integer"},
                  fn=function(a,b) return b end},
        Audio={args={{kind="reaper.array"}},returns={},fn=function(a) a.data[1]=0.5 end}
      }
      reaper.new_array=function(input)
        local data={}
        if type(input)=="number" then for i=1,input do data[i]=0 end else data=input end
        return {data=data,table=function() return data end}
      end
    ''')
    source = (ROOT / 'reaper_scripts/native_api_runtime.inc.lua').read_text(encoding='utf-8')
    return lua, lua.execute(source + '\nreturn native_api')


def table(lua, value):
    if isinstance(value, dict):
        return lua.table_from({k: table(lua, v) for k, v in value.items()})
    if isinstance(value, list):
        return lua.table_from([table(lua, v) for v in value])
    return value


def test_catalog_is_complete_for_pinned_reference():
    assert CATALOG['reaper_version'] == '7.82'
    assert len(CATALOG['functions']) == 732
    for name in ['TakeFX_AddByName', 'InsertAutomationItem', 'MIDI_GetCC',
                 'GetAudioAccessorSamples', 'SetTakeStretchMarker', 'EnumThemeColors']:
        assert not CATALOG['functions'][name]['blocked_reason']


@pytest.mark.parametrize('name,args', [('ExecProcess', ['cmd.exe', 0]), ('loadstring', ['x']),
                                      ('GetTrack', []), ('GetTrack', [0, float('nan')])])
def test_python_validation_rejects_invalid_calls(name, args):
    with pytest.raises(ValueError):
        validate_call(name, args)


def test_optional_null_preserves_position():
    lua, api = runtime()
    result = api.native_api_call(table(lua, {'function_name':'Optional', 'arguments':[{'null':True},7]}))
    assert result['returns'][1]['value'] == 7


def test_binary_result_roundtrips_losslessly():
    lua, api = runtime()
    result = api.native_api_call(table(lua, {'function_name':'Echo','arguments':[{'bytes_hex':'00ff0a'},42]}))
    assert result['returns'][1]['value']['bytes_hex'] == '00ff0a'
    assert result['returns'][2]['value'] == 42


def test_type_validation_precedes_native_call():
    lua, api = runtime()
    result, error = api.native_api_call(table(lua, {'function_name':'Echo','arguments':['x',1.5]}))
    assert result is None and 'integer' in error


def test_forged_handle_is_rejected():
    lua, api = runtime()
    result, error = api.native_api_call(table(lua, {'function_name':'Pointer','arguments':[{'handle':'forged'}]}))
    assert result is None and 'handle' in error


def test_undo_block_closes_when_native_api_raises():
    lua, api = runtime()
    result, error = api.native_api_call(table(lua, {'function_name':'Fails','arguments':[], 'undo_description':'test'}))
    assert result is None and 'native failure' in error
    assert lua.globals().undo_open is False


def test_audio_buffer_returns_mutated_samples():
    lua, api = runtime()
    result = api.native_api_call(table(lua, {'function_name':'Audio','arguments':[{'array_size':4}]}))
    assert result['arrays'][1]['values'][1] == 0.5
    assert len(result['arrays'][1]['values']) == 4


def test_generated_catalog_and_embedded_runtime_are_current():
    source = (ROOT / 'reaper_scripts/reaper_mcp_server.lua').read_text(encoding='utf-8')
    fragment = (ROOT / 'reaper_scripts/native_api_runtime.inc.lua').read_text(encoding='utf-8')
    assert fragment in source
    for name, spec in CATALOG['functions'].items():
        if not spec['blocked_reason']:
            assert f'[{json.dumps(name)}] = {{fn=function(...) return reaper.{name}(' in source
