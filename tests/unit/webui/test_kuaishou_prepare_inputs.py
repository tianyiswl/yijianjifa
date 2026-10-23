"""Exercise the real prepare method with creator-input event contracts, no IO."""
from types import SimpleNamespace

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from yijianjifa.webui.kuaishou_multi_publish import KuaishouMultiPublishAdapter
from yijianjifa.webui import kuaishou_publish_form as contract


class CreatorInputs:
    """Readonly-until-click date and topic type set only by Shift+Digit3."""

    def __init__(self):
        self.events = []
        self.caption = ''
        self.tags = []
        self.topic_type = False
        self.topic_text = ''
        self.mode = '立即发布'
        self.time_text = '2026-10-07 23:14:53'
        self.pending_time = self.time_text
        self.date_readonly = True
        self.editor = Editor(self)
        self.date = DateInput(self)
        self.confirm = DateConfirmation(self)
        self.keyboard = Keyboard(self)

    def locator(self, selector):
        if selector == contract.EDITOR:
            return self.editor
        if selector == contract.DATE_INPUT:
            return self.date
        if selector == contract.DATE_CONFIRM:
            return self.confirm
        raise AssertionError('Unexpected input selector')

    def get_by_text(self, mode, *, exact):
        assert exact and mode in {'立即发布', '定时发布'}
        return ModeInput(self, mode)

    def observed(self):
        return {'caption': self.caption, 'tags': list(self.tags), 'mode': self.mode,
                'time_text': self.time_text if self.mode == '定时发布' else '',
                'upload_complete': True, 'editor_count': 1, 'button_count': 1,
                'button_enabled': True,'declaration_count':1,'declaration_variant':'legacy',
                'declaration_label':'','declaration_item_count':0}


class Editor:
    def __init__(self, creator):
        self.creator = creator

    def click(self):
        self.creator.events.append(('editor_click', None))

    def press(self, key):
        c = self.creator
        c.events.append(('editor_press', key))
        if key == 'ControlOrMeta+A':
            c.caption = ''
            c.tags.clear()
            c.topic_type = False
        elif key == 'Shift+Digit3':
            c.topic_type = True
            c.topic_text = ''
            c.caption += '#'
        elif key == 'Space':
            if c.topic_type and c.topic_text:
                c.tags.append(c.topic_text)
            c.topic_type = False
            c.caption += ' '
        else:
            raise AssertionError('Unexpected editor key')


class Keyboard:
    def __init__(self, creator):
        self.creator = creator

    def insert_text(self, value):
        c = self.creator
        c.events.append(('insert_text', value))
        c.caption += value
        if c.topic_type:
            c.topic_text += value

    def type(self, value, **kwargs):
        # A literal '#' inserted without Shift+Digit3 does not establish the
        # creator's topic state. The old path must fail the readback test.
        self.creator.events.append(('type_text', value))
        self.creator.caption += value


class ModeInput:
    def __init__(self, creator, mode):
        self.creator = creator
        self.mode = mode

    def count(self):
        return 1

    def click(self):
        self.creator.events.append(('mode_click', self.mode))
        self.creator.mode = self.mode


class DateInput:
    def __init__(self, creator):
        self.creator = creator

    def count(self):
        return 1

    def click(self):
        self.creator.events.append(('date_click', None))
        self.creator.date_readonly = False

    def fill(self, value):
        c = self.creator
        if c.date_readonly:
            raise PlaywrightTimeoutError('Date input is readonly until its normal click enters typing mode')
        c.events.append(('date_fill', value))
        c.pending_time = value

    def press(self, key):
        c = self.creator
        c.events.append(('date_press', key))

    def input_value(self):
        return self.creator.time_text


class DateConfirmation:
    def __init__(self, creator):
        self.creator = creator

    def count(self):
        return 1

    def click(self):
        c = self.creator
        assert c.date_readonly is False
        c.events.append(('date_confirm', None))
        c.time_text = c.pending_time
        c.date_readonly = True


def session_and_adapter(monkeypatch):
    frame = CreatorInputs()
    session = SimpleNamespace(form_filled=False, final=False, page=SimpleNamespace(keyboard=frame.keyboard))
    adapter = object.__new__(KuaishouMultiPublishAdapter)
    monkeypatch.setattr(adapter, '_observed_form', lambda s, current: current.observed())
    return session, adapter, frame


def test_date_input_enters_typing_mode_and_commits_through_visible_confirmation(monkeypatch):
    session, adapter, frame = session_and_adapter(monkeypatch)
    form = {'text': '合成标题', 'caption': '合成标题', 'tags': [],
            'mode': '定时发布', 'time_text': '2026-10-08 12:00:00',
            'declaration':{'choice':'none','name':'','source':None}}
    assert adapter._prepare_form(session, frame, form) is False
    assert session.form_filled is True
    assert adapter._prepare_form(session, frame, form) is True
    assert [event for event in frame.events if event[0].startswith('date_')] == [
        ('date_click', None), ('date_fill', form['time_text']), ('date_confirm', None),
    ]
    assert session.final is False


def test_date_confirmation_without_matching_committed_value_stops_before_final(monkeypatch):
    session, adapter, frame = session_and_adapter(monkeypatch)
    form = {'text': '合成标题', 'caption': '合成标题', 'tags': [],
            'mode': '定时发布', 'time_text': '2026-10-08 12:00:00',
            'declaration':{'choice':'none','name':'','source':None}}
    monkeypatch.setattr(frame.confirm, 'click', lambda: frame.events.append(('date_confirm', None)))
    with pytest.raises(ValueError, match='^schedule input did not commit$'):
        adapter._prepare_form(session, frame, form)
    assert session.form_filled is False and session.final is False
    assert frame.time_text != form['time_text']


def test_topic_type_is_set_by_shift_digit3_before_each_chinese_topic(monkeypatch):
    session, adapter, frame = session_and_adapter(monkeypatch)
    form = {'text': '合成标题', 'caption': '合成标题 #功能测试 #剪辑测试',
            'tags': ['功能测试', '剪辑测试'], 'mode': '立即发布', 'time_text': '',
            'declaration':{'choice':'none','name':'','source':None}}
    assert adapter._prepare_form(session, frame, form) is False
    assert adapter._prepare_form(session, frame, form) is True
    assert frame.tags == form['tags']
    assert sum(event == ('editor_press', 'Shift+Digit3') for event in frame.events) == 2
    for tag in form['tags']:
        at = frame.events.index(('insert_text', tag))
        assert frame.events[at - 1] == ('editor_press', 'Shift+Digit3')
        assert frame.events[at + 1] == ('editor_press', 'Space')
    assert not any(event[0].startswith('date_') for event in frame.events)
