from yijianjifa.features.accounts.models import CapabilitySnapshot
from yijianjifa.features.commerce.stores import StoreCandidate
from yijianjifa.features.publish.platforms.douyin.commerce import (
    CommercePublishRequest,
    DouyinCommerceAdapter,
)


class FakeCommercePage:
    def __init__(
        self,
        *,
        stores=None,
        music_by_account=None,
        schedule_text=None,
        public_success=False,
        auth_required=False,
    ):
        self.stores = stores or []
        self.music_by_account = music_by_account or {}
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

    def commerce_store_rows(self, account_id, scope, query, region):
        return [row for row in self.stores if row.scope == scope]

    def commerce_store_available(self, account_id, store_id):
        return any(row.id == store_id for row in self.stores)

    def favorite_music_ids(self, account_id):
        return list(self.music_by_account.get(account_id, []))


def store(store_id, *, scope="local"):
    return StoreCandidate(
        id=store_id,
        name=f"店{store_id}",
        region="上海",
        scope=scope,
        commission="confirmed",
    )


def adapter(page, *, location_scope="local_national"):
    return DouyinCommerceAdapter(
        page,
        account_id="A",
        capabilities=CapabilitySnapshot(
            ordinary="available",
            commerce="available",
            location_scope=location_scope,
        ),
    )


def request(**overrides):
    values = dict(
        expected_platform_user_id="user-a",
        video_path="/tmp/v.mp4",
        title="标题",
        body="文案",
        tags="#标签",
        store_id="s1",
        cover_ready=True,
        declaration_required=False,
        declaration=None,
        favorite_music_id=None,
        scheduled_time_text="2026-09-24 09:00",
    )
    values.update(overrides)
    return CommercePublishRequest(**values)


def test_search_national_is_blocked_when_account_scope_is_local_only():
    page = FakeCommercePage(stores=[store("s1", scope="local"), store("n1", scope="national")])
    commerce = adapter(page, location_scope="local")

    try:
        commerce.search_stores(scope="national", query="", region="")
        raised = False
    except ValueError as exc:
        raised = "scope" in str(exc)

    assert raised is True


def test_store_and_favorite_music_are_scoped_to_selected_account():
    page = FakeCommercePage(
        stores=[store("s1")],
        music_by_account={"A": ["a1", "a2"], "B": ["b1"]},
    )
    commerce = adapter(page)

    assert [row.id for row in commerce.search_stores(scope="local", query="", region="上海")] == ["s1"]
    assert commerce.list_favorite_music() == ["a1", "a2"]


def test_missing_or_stale_store_stops_before_final_submit():
    page = FakeCommercePage(stores=[store("s2")])
    evidence = adapter(page).submit_commerce(request(store_id="missing"))

    assert evidence.final_action_triggered is False
    assert evidence.error_code == "commerce_store_unavailable"
    assert not any("final-submit" in selector for selector in page.clicked)


def test_required_declaration_and_bad_cover_stop_before_final_submit():
    page = FakeCommercePage(stores=[store("s1")])
    commerce = adapter(page)

    missing_declaration = commerce.submit_commerce(
        request(declaration_required=True, declaration=None)
    )
    bad_cover = commerce.submit_commerce(request(cover_ready=False))

    assert missing_declaration.error_code == "commerce_declaration_required"
    assert bad_cover.error_code == "commerce_cover_invalid"
    assert missing_declaration.final_action_triggered is False
    assert bad_cover.final_action_triggered is False


def test_favorite_music_from_another_account_stops_before_submit():
    page = FakeCommercePage(
        stores=[store("s1")],
        music_by_account={"A": ["a1"], "B": ["b1"]},
    )
    evidence = adapter(page).submit_commerce(request(favorite_music_id="b1"))
    assert evidence.error_code == "favorite_music_not_in_current_account"
    assert evidence.final_action_triggered is False


def test_submit_reports_schedule_only_with_matching_explicit_evidence():
    page = FakeCommercePage(
        stores=[store("s1")],
        schedule_text="2026-09-24 09:00",
    )
    evidence = adapter(page).submit_commerce(request())

    assert evidence.final_action_triggered is True
    assert evidence.platform_schedule_confirmation == "2026-09-24 09:00"
    assert evidence.public_result is None


def test_auth_required_stops_before_final_submit():
    page = FakeCommercePage(stores=[store("s1")], auth_required=True)
    evidence = adapter(page).submit_commerce(request())
    assert evidence.auth_required is True
    assert evidence.final_action_triggered is False
