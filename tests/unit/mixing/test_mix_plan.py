from yijianjifa.features.mixing.models import MixPlan


def test_quick_mix_defaults_are_product_defaults():
    p = MixPlan.quick()
    assert p.material_scope == "all"
    assert p.copy_scope == "all"
    assert p.quantity == 10
    assert p.resolution == "720p"
    assert p.frame_fit == "preserve_blur"
    assert p.transition.mode == "none"
    assert p.background_music.mode == "none"
