from pathlib import Path
import struct

import pytest
from PySide6.QtGui import QImage

from yijianjifa.features.image_posts.service import ImagePostService


def picture(path: Path, color=0xFF0066AA):
    path.parent.mkdir(parents=True, exist_ok=True)
    image = QImage(80, 60, QImage.Format.Format_RGB32)
    image.fill(color)
    assert image.save(str(path))
    return path


@pytest.mark.parametrize("extension", ["jpg", "png", "webp", "bmp"])
def test_qt_image_formats_decode_and_normalize(db_engine, app_paths, tmp_path, extension):
    source = picture(tmp_path / f"source.{extension}")
    post = ImagePostService(db_engine, app_paths).import_group([source])
    assert post.images[0].format in {"JPEG", "PNG"}
    assert not QImage(str(post.images[0].path)).isNull()
    assert (post.images[0].width, post.images[0].height) == (80, 60)


def test_import_is_owned_and_naturally_ordered(db_engine, app_paths, tmp_path):
    sources = [picture(tmp_path / f"{number}.png", 0xFF000000 + number) for number in (10, 2, 1)]
    service = ImagePostService(db_engine, app_paths)
    post = service.import_group(sources, display_name="产品A", title="产品标题", body="正文", tags="#产品")
    assert [image.original_name for image in post.images] == ["1.png", "2.png", "10.png"]
    assert [image.position for image in post.images] == [0, 1, 2]
    assert post.title == "产品标题" and post.body == "正文" and post.tags == "#产品"
    assert all(image.path.is_relative_to(app_paths.data_dir / "image_posts") for image in post.images)
    assert all(image.path.exists() and image.thumbnail_path.exists() for image in post.images)
    for source in sources:
        source.unlink()
    assert all(QImage(str(image.path)).size().width() == 80 for image in post.images)
    assert service.get(post.id) == post


def test_corrupt_group_rolls_back_records_and_copies(db_engine, app_paths, tmp_path):
    good = picture(tmp_path / "good.png")
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"not an image")
    service = ImagePostService(db_engine, app_paths)
    with pytest.raises(ValueError, match="bad.png"):
        service.import_group([good, bad])
    assert service.list()[1] == 0
    assert not list((app_paths.data_dir / "image_posts").rglob("*.jpg"))


def test_reorder_remove_and_edit_survive_reload(db_engine, app_paths, tmp_path):
    service = ImagePostService(db_engine, app_paths)
    post = service.import_group([picture(tmp_path / "1.png"), picture(tmp_path / "2.png", 0xFF99FF00)])
    original_hash = post.content_hash
    reversed_post = service.save(post.id, {"image_ids": [image.id for image in reversed(post.images)], "title": "新标题"})
    assert reversed_post.content_hash != original_hash
    assert [image.id for image in reversed_post.images] == [post.images[1].id, post.images[0].id]
    removed_path = post.images[0].path
    updated = service.save(post.id, {"image_ids": [post.images[1].id], "display_name": "新名称"})
    assert len(updated.images) == 1 and updated.title == "新标题"
    assert not removed_path.exists()
    assert ImagePostService(db_engine, app_paths).get(post.id) == updated


def test_save_rejects_cross_post_images_and_empty_group(db_engine, app_paths, tmp_path):
    service = ImagePostService(db_engine, app_paths)
    first = service.import_group([picture(tmp_path / "1.png")])
    second = service.import_group([picture(tmp_path / "2.png")])
    for ids in ([], [second.images[0].id], [first.images[0].id] * 2):
        with pytest.raises(ValueError):
            service.save(first.id, {"image_ids": ids})
    assert len(service.get(first.id).images) == 1


def test_delete_preserves_source_and_other_posts(db_engine, app_paths, tmp_path):
    source = picture(tmp_path / "source.png")
    service = ImagePostService(db_engine, app_paths)
    first = service.import_group([source])
    second = service.import_group([source])
    service.delete([first.id])
    assert service.get(first.id) is None and not first.images[0].path.exists()
    assert source.exists() and second.images[0].path.exists()
    assert service.list()[1] == 1


def test_latest_first_and_search(db_engine, app_paths, tmp_path):
    service = ImagePostService(db_engine, app_paths)
    first = service.import_group([picture(tmp_path / "1.png")], display_name="工厂A")
    second = service.import_group([picture(tmp_path / "2.png")], display_name="工厂B")
    assert [post.id for post in service.list()[0]] == [second.id, first.id]
    assert service.list(query="工厂A")[1] == 1
    assert service.list(offset=1, limit=1)[0][0].id == first.id


def test_symlink_and_size_limit_rejected_before_copy(db_engine, app_paths, tmp_path, monkeypatch):
    import yijianjifa.features.image_posts.service as module
    source = picture(tmp_path / "original.png")
    link = tmp_path / "linked.png"
    link.symlink_to(source)
    service = ImagePostService(db_engine, app_paths)
    with pytest.raises(ValueError, match="符号链接"):
        service.import_group([link])
    monkeypatch.setattr(module, "MAX_IMAGE_BYTES", 10)
    with pytest.raises(ValueError, match="大小"):
        service.import_group([source])
    assert service.list()[1] == 0


def test_add_images_atomic_and_retains_existing_order(db_engine, app_paths, tmp_path):
    service = ImagePostService(db_engine, app_paths)
    post = service.import_group([picture(tmp_path / "1.png")])
    updated = service.add_images(post.id, [picture(tmp_path / "10.png"), picture(tmp_path / "2.png")])
    assert [image.original_name for image in updated.images] == ["1.png", "2.png", "10.png"]
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"bad")
    with pytest.raises(ValueError):
        service.add_images(post.id, [picture(tmp_path / "3.png"), bad])
    assert len(service.get(post.id).images) == 3


def test_exif_orientation_normalized_and_alpha_kept(db_engine, app_paths, tmp_path):
    jpg = picture(tmp_path / "rotated.jpg")
    # EXIF orientation 6: the displayed image must rotate 90 degrees clockwise.
    tiff = b"II" + struct.pack("<HIH", 42, 8, 1) + struct.pack("<HHI", 0x0112, 3, 1) + struct.pack("<H", 6) + b"\x00\x00" + struct.pack("<I", 0)
    exif = b"Exif\x00\x00" + tiff
    raw = jpg.read_bytes()
    jpg.write_bytes(raw[:2] + b"\xff\xe1" + struct.pack(">H", len(exif) + 2) + exif + raw[2:])
    transparent = tmp_path / "transparent.png"
    image = QImage(40, 50, QImage.Format.Format_ARGB32)
    image.fill(0x660077AA)
    assert image.save(str(transparent))
    post = ImagePostService(db_engine, app_paths).import_group([jpg, transparent])
    assert (post.images[0].width, post.images[0].height) == (60, 80)
    assert QImage(str(post.images[0].path)).size().width() == 60
    assert post.images[1].format == "PNG"
    assert QImage(str(post.images[1].path)).hasAlphaChannel()
