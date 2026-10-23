import json
from unittest.mock import patch
from yijianjifa.app_services.desktop_api import DesktopApi


def call(params, trusted=True):
    api = DesktopApi.__new__(DesktopApi)
    return api.dispatch(json.dumps(dict(v=1, request_id='help-test', operation='help.open', params=params)), trusted_origin=trusted)


def test_help_opens_only_fixed_public_entries():
    with patch('webbrowser.open', return_value=True) as opened:
        assert call({'target': 'guide'})['ok']
        opened.assert_called_with('https://39.108.82.213/operator/guide', new=2)
        assert call({'target': 'download'})['ok']
        opened.assert_called_with('https://github.com/tianyiswl/yijianjifa', new=2)
        for params in ({'target': 'https://evil.example'}, {'target': 'guide', 'url': 'file:///etc/passwd'}, {'target': []}):
            assert not call(params)['ok']
        assert not call({'target': 'guide'}, trusted=False)['ok']
        assert opened.call_count == 2


def test_browser_failure_has_actionable_message():
    with patch('webbrowser.open', return_value=False):
        result = call({'target': 'guide'})
        assert not result['ok']
        assert '默认浏览器' in result['error']['message']
