"""Frozen package checks must exercise the same browser channel as publishing."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from yijianjifa.webui import bundle_check


def driver_fixture(monkeypatch, *, fail_js=False):
    import playwright.sync_api

    launches = []
    contexts = []

    class Context:
        def __init__(self):
            self.closed = False

        def route(self, pattern, handler):
            assert pattern == '**/*'

        def new_page(self):
            return SimpleNamespace(set_content=lambda html: None,
                                   evaluate=lambda script: not fail_js)

        def close(self):
            self.closed = True

    def launch(profile, **options):
        assert Path(profile).is_absolute()
        assert options['channel'] == 'chromium'
        context = Context()
        contexts.append(context)
        launches.append(options)
        return context

    class Driver:
        def __enter__(self):
            return SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))

        def __exit__(self, *_):
            return False

    monkeypatch.setattr(playwright.sync_api, 'sync_playwright', Driver)
    return launches, contexts


def test_frozen_browser_check_exercises_background_and_visible(monkeypatch, tmp_path):
    launches, contexts = driver_fixture(monkeypatch)
    check = getattr(bundle_check, 'check_browsers', None)
    assert callable(check), 'Frozen browser startup check is missing'
    result = check(tmp_path/'profiles')
    assert result['background'] is True and result['visible'] is True
    assert [options['headless'] for options in launches] == [True, False]
    assert all(context.closed for context in contexts)


def test_frozen_browser_check_fails_when_page_js_is_unusable(monkeypatch, tmp_path):
    _, contexts = driver_fixture(monkeypatch, fail_js=True)
    check = getattr(bundle_check, 'check_browsers', None)
    assert callable(check), 'Frozen browser startup check is missing'
    with pytest.raises(RuntimeError, match='browser'):
        check(tmp_path/'profiles')
    assert len(contexts) == 1 and contexts[0].closed
