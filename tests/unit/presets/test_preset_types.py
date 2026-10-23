import pytest
from pydantic import ValidationError

from yijianjifa.features.presets.models import CommercePreset, MixPreset, PresetValidationError
from yijianjifa.features.presets.service import PresetService
from yijianjifa.features.library.copy_service import CategoryRepository


def test_deleted_category_marks_mix_preset_needs_repair_without_fallback(db_engine):
    categories = CategoryRepository(db_engine)
    old = categories.create("video", "旧产品")
    service = PresetService(db_engine, categories=categories)
    p = service.create_mix(name="旧产品方案", material_category_ids=[old.id])

    categories.delete(old.id)
    result = service.materialize(p.id)

    assert result.state == "needs_repair"
    assert result.material_category_ids == (old.id,)
    assert result.material_scope == "categories"


def test_legacy_multi_account_commerce_preset_is_rejected():
    with pytest.raises((PresetValidationError, ValidationError)):
        CommercePreset.model_validate({"name": "legacy", "account_ids": ["A", "B"], "schedule_rule": {}})


def test_commerce_preset_has_one_scalar_account_id():
    p = CommercePreset(name="团购", account_id="A1")
    assert p.account_id == "A1"
    assert "account_ids" not in p.model_dump()
