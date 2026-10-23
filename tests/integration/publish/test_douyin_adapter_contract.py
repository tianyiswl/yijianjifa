from yijianjifa.features.publish.platforms.douyin.normal import DouyinNormalAdapter, NormalPublishRequest


class FakePage:
    def __init__(self, *, schedule_text=None, public_success=False, auth_required=False):
        self.schedule_text = schedule_text
        self.public_success = public_success
        self.auth_required = auth_required
        self.clicked = []
        self.filled = {}

    def set_input_files(self, selector, path):
        self.filled[selector] = path

    def fill(self, selector, value):
        self.filled[selector] = value

    def click(self, selector):
        self.clicked.append(selector)

    def is_visible(self, selector):
        if selector.endswith("auth-required"):
            return self.auth_required
        if selector.endswith("public-success"):
            return bool(self.clicked) and self.public_success
        if selector.endswith("schedule-confirmation"):
            return bool(self.clicked) and self.schedule_text is not None
        return False

    def text_content(self, selector):
        if selector.endswith("identity"):
            return "user-a"
        if selector.endswith("schedule-confirmation"):
            return self.schedule_text
        return None


def test_adapter_only_reports_platform_scheduled_with_matching_explicit_evidence():
    page = FakePage(schedule_text="2026-09-24 09:00")
    adapter = DouyinNormalAdapter(page)
    request = NormalPublishRequest(expected_platform_user_id="user-a",
        video_path="/tmp/v.mp4",
        title="标题",
        body="文案",
        tags="#标签",
        scheduled_time_text="2026-09-24 09:00",
    )
    ev = adapter.submit_normal(request)
    assert ev.final_action_triggered is True
    assert ev.platform_schedule_confirmation == "2026-09-24 09:00"


def test_adapter_does_not_invent_platform_schedule_without_matching_evidence():
    page = FakePage(schedule_text="2026-09-24 12:00")
    adapter = DouyinNormalAdapter(page)
    request = NormalPublishRequest(expected_platform_user_id="user-a",
        video_path="/tmp/v.mp4",
        title="标题",
        body="文案",
        tags="#标签",
        scheduled_time_text="2026-09-24 09:00",
    )
    ev = adapter.submit_normal(request)
    assert ev.final_action_triggered is True
    assert ev.platform_schedule_confirmation is None
    assert ev.public_result is None


def test_adapter_auth_required_stops_before_final_action():
    page = FakePage(auth_required=True)
    adapter = DouyinNormalAdapter(page)
    request = NormalPublishRequest(expected_platform_user_id="user-a", video_path="/tmp/v.mp4", title="", body="", tags="")
    ev = adapter.submit_normal(request)
    assert ev.auth_required is True
    assert ev.final_action_triggered is False
    assert not any("final-submit" in selector for selector in page.clicked)


def test_adapter_public_success_is_separate_from_platform_schedule():
    page = FakePage(public_success=True)
    adapter = DouyinNormalAdapter(page)
    request = NormalPublishRequest(expected_platform_user_id="user-a", video_path="/tmp/v.mp4", title="", body="", tags="")
    ev = adapter.submit_normal(request)
    assert ev.public_result == "published"
    assert ev.platform_schedule_confirmation is None
