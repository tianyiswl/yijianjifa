import platform


def test_system_tts_factory_routes_windows(monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    from yijianjifa.features.mixing.tts.base import system_tts_adapter
    from yijianjifa.features.mixing.tts.windows_sapi import WindowsSapiTTS

    assert isinstance(system_tts_adapter(), WindowsSapiTTS)


def test_system_tts_factory_routes_macos(monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    from yijianjifa.features.mixing.tts.base import system_tts_adapter
    from yijianjifa.features.mixing.tts.macos_say import MacOSSayTTS

    assert isinstance(system_tts_adapter(), MacOSSayTTS)


def test_system_tts_factory_rejects_unsupported_platform(monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    from yijianjifa.features.mixing.tts.base import system_tts_adapter

    try:
        system_tts_adapter()
    except RuntimeError as exc:
        assert "Windows and macOS" in str(exc)
    else:
        raise AssertionError("expected unsupported platform error")


def test_macos_voice_names_keep_language_suffix_and_deduplicate(monkeypatch):
    import subprocess
    from yijianjifa.features.mixing.tts.macos_say import MacOSSayTTS
    output='Eddy (中文（中国大陆）)     zh_CN    # 你好\nEddy (中文（台湾）)     zh_TW    # 你好\nEddy (中文（台湾）)     zh_TW    # 你好\nTingting    zh_CN    # 你好\n'
    monkeypatch.setattr(subprocess,'run',lambda *a,**k:subprocess.CompletedProcess(a,0,stdout=output))
    voices=MacOSSayTTS().list_voices()
    assert [v.id for v in voices]==['Eddy (中文（中国大陆）)','Eddy (中文（台湾）)','Tingting']


def test_macos_sends_speech_as_stdin_not_command_options(monkeypatch,tmp_path):
    import subprocess
    from yijianjifa.features.mixing.tts import macos_say
    seen=[]
    def run(args,**kwargs):
        seen.append((args,kwargs));return subprocess.CompletedProcess(args,0,stdout=b'')
    monkeypatch.setattr(subprocess,'run',run)
    monkeypatch.setattr(macos_say,'_probe_duration_ms',lambda p:1000)
    macos_say.MacOSSayTTS().synthesize('-f /private/test',None,1,tmp_path/'out.aiff')
    assert seen[0][1]['input']==b'-f /private/test'
    assert '-f /private/test' not in seen[0][0]
