"""Exercise the bridge's real JSON codec without a running REAPER instance."""

import json
from pathlib import Path

import pytest

lupa = pytest.importorskip("lupa.lua54")

LUA_SRC = (
    Path(__file__).resolve().parent.parent
    / "reaper_scripts"
    / "reaper_mcp_server.lua"
)


@pytest.fixture
def codec():
    source = LUA_SRC.read_text(encoding="utf-8")
    start = source.index("local MAX_DEPTH =")
    end = source.index("-- Response helpers", start)
    lua = lupa.LuaRuntime(unpack_returned_tuples=True)
    encode, decode = lua.execute(source[start:end] + "\nreturn json_encode, json_decode")
    return lua, encode, decode


@pytest.mark.parametrize("text", [
    "",
    "pitch",
    "plain text " * 10000,
    'quoted "track" and C:\\Audio\\mix.wav',
    "\n\r\t",
    'prefix\nmiddle\\suffix"',
    "Дорожка 日本語 café",
])
@pytest.mark.parametrize("ensure_ascii", [False, True])
def test_strings_round_trip(codec, text, ensure_ascii):
    _, encode, decode = codec
    assert decode(json.dumps(text, ensure_ascii=ensure_ascii)) == text
    assert json.loads(encode(text)) == text


def test_nested_command_and_response_round_trip(codec):
    _, encode, decode = codec
    command = {
        "command": "compose_arrangement",
        "params": {
            "tracks": json.dumps([{
                "track_index": 0,
                "track_name": 'Lead "A"',
                "notes": [{"pitch": 60, "start": 0.125, "end": 1.5}],
            }]),
            "clear_existing": False,
        },
        "id": "test-request",
    }
    parsed = decode(json.dumps(command))
    assert json.loads(encode(parsed)) == command
    assert decode(parsed["params"]["tracks"])[1]["track_name"] == 'Lead "A"'
    response = {"success": True, "data": command["params"], "id": command["id"]}
    assert json.loads(encode(decode(json.dumps(response)))) == response


@pytest.mark.parametrize("value", [
    0, -2147483648, 2147483647, 2147483648, 0.12345678901234567,
    1e-200, -1e200, True, False,
])
def test_scalar_precision_is_preserved(codec, value):
    _, encode, decode = codec
    assert json.loads(encode(value)) == value
    assert decode(json.dumps(value)) == value


def test_nonfinite_numbers_and_nil_encode_as_null(codec):
    _, encode, _ = codec
    for value in (None, float("nan"), float("inf"), float("-inf")):
        assert encode(value) == "null"


@pytest.mark.parametrize(("source", "expected"), [
    (r'""', ""),
    (r'"\/"', "/"),
    (r'"\u0000"', "\0"),
    (r'"\uD83D\uDE00"', "??"),
    (r'"\uZZZZ"', "?"),
    (r'"\b\f\q"', "bfq"),
    ('"unfinished', ""),
    ('"prefix\\nunfinished', "prefix\n"),
])
def test_legacy_string_decoding_is_unchanged(codec, source, expected):
    _, _, decode = codec
    assert decode(source) == expected


def test_legacy_string_encoding_is_unchanged(codec):
    _, encode, _ = codec
    assert encode("a\0b\b\f") == '"ab\b\f"'
    assert json.loads(encode('a\\n\n\r\t"b')) == 'a\\n\n\r\t"b'


def test_encoder_matches_legacy_escaping_for_all_ascii(codec):
    _, encode, _ = codec
    text = "".join(chr(i) for i in range(128))
    escaped = (
        text.replace("\\", "\\\\").replace('"', '\\"')
        .replace("\n", "\\n").replace("\r", "\\r")
        .replace("\t", "\\t").replace("\0", "")
    )
    assert encode(text) == '"' + escaped + '"'


def test_encoder_escapes_strings_in_one_scan(codec):
    lua, encode, _ = codec
    lua.execute("""
      scan_count = 0
      local original = string.gsub
      string.gsub = function(...)
        scan_count = scan_count + 1
        return original(...)
      end
    """)
    text = 'prefix\\\n\r\t"suffix'
    assert json.loads(encode(text)) == text
    assert lua.globals().scan_count == 1


def test_plain_string_decoding_does_not_concatenate_chunks(codec):
    lua, _, decode = codec
    lua.execute("""
      concat_count = 0
      local original = table.concat
      table.concat = function(...)
        concat_count = concat_count + 1
        return original(...)
      end
    """)
    value = decode('{"track": "Piano", "notes": ["C4", "E4"], "empty": ""}')
    assert value["track"] == "Piano"
    assert list(value["notes"].values()) == ["C4", "E4"]
    assert value["empty"] == ""
    assert lua.globals().concat_count == 0
    assert decode(r'"escaped\nstring"') == "escaped\nstring"
    assert lua.globals().concat_count == 1


def test_legacy_null_and_empty_table_handling_is_unchanged(codec):
    _, encode, decode = codec
    assert decode("null") is None
    assert encode(decode("{}")) == "[]"
    assert json.loads(encode(decode('[1, null, 2]'))) == [1, 2]
    assert json.loads(encode(decode('{"missing": null, "present": false}'))) == {
        "present": False,
    }


def test_unicode_and_escaped_object_keys(codec):
    _, encode, decode = codec
    value = {'': 1, 'track"name': 2, 'a\\b': 3, '\n': 4, 'имя': 5}
    assert json.loads(encode(decode(json.dumps(value)))) == value


def test_decoder_size_and_depth_limits(codec):
    _, _, decode = codec
    assert decode(None) is None
    assert decode("") is None
    assert decode('"' + "x" * (20 * 1024 * 1024 - 2) + '"') is not None
    assert decode('"' + "x" * (20 * 1024 * 1024 - 1) + '"') is None
    assert decode("[" * 50 + "0" + "]" * 50) is not None
    assert decode("[" * 51 + "0" + "]" * 51) is None


def test_encoder_distinguishes_cycles_from_shared_tables(codec):
    lua, encode, _ = codec
    cyclic = lua.execute("local t = {}; t.self = t; return t")
    assert json.loads(encode(cyclic)) == {"self": "<circular>"}
    shared = lua.execute("local t = {value = 1}; return {t, t}")
    assert json.loads(encode(shared)) == [{"value": 1}, {"value": 1}]


def test_encoder_depth_limit(codec):
    lua, encode, _ = codec
    nested = lua.execute("""
      local root = {}
      local tail = root
      for i = 1, 51 do tail[1] = {}; tail = tail[1] end
      return root
    """)
    value = json.loads(encode(nested))
    for _ in range(51):
        value = value[0]
    assert value == "<max depth>"


def test_large_note_batch_round_trip(codec):
    _, encode, decode = codec
    notes = [
        {"pitch": 60 + i % 12, "velocity": 100, "start": i / 8, "end": i / 8 + 0.1}
        for i in range(10000)
    ]
    assert json.loads(encode(decode(json.dumps(notes)))) == notes
