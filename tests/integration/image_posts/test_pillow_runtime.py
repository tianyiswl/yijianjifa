"""Packaged Mac image storage works when Qt is deliberately unavailable."""
import json
import os
from pathlib import Path
import subprocess
import sys


def test_image_storage_without_any_qt_module(tmp_path):
    script = r'''
import json, sys
from pathlib import Path
sys.modules['PySide6'] = None
from PIL import Image
from yijianjifa.features.image_posts import service as module
from yijianjifa.features.image_posts.files import ImagePostFiles
from yijianjifa.os_integration.paths import AppPaths
from yijianjifa.storage.db import create_engine_for, migrate_to_head
root = Path(sys.argv[1]); data = root / 'data'; data.mkdir()
paths = AppPaths(data, data/'cache', data/'logs', data/'managed')
engine = create_engine_for(data/'test.db'); migrate_to_head(engine)
core = module.ImagePostService(engine, paths)
formats = []
for ext, fmt in [('jpg','JPEG'), ('png','PNG'), ('webp','WEBP'), ('bmp','BMP')]:
    source = root / ('source.'+ext)
    Image.new('RGB', (80,60), '#336699').save(source, format=fmt)
    post = core.import_group([source])
    with Image.open(post.images[0].path) as result:
        assert result.size == (80,60) and result.format in {'JPEG','PNG'}
    with Image.open(post.images[0].thumbnail_path) as thumbnail:
        assert thumbnail.format == 'PNG' and max(thumbnail.size) <= 420
    formats.append(ext)
source = root/'rotated.jpg'
exif = Image.Exif(); exif[274] = 6
Image.new('RGB', (80,60), '#998800').save(source, exif=exif)
rotated = core.import_group([source]).images[0]
assert (rotated.width, rotated.height) == (60,80)
with Image.open(rotated.path) as result:
    assert result.size == (60,80) and result.getexif().get(274,1) == 1
source = root/'alpha.png'
Image.new('RGBA', (50,60), (10,20,30,40)).save(source)
alpha = core.import_group([source]).images[0]
assert alpha.format == 'PNG'
with Image.open(alpha.path) as result:
    assert result.getpixel((0,0))[3] == 40
source = root/'palette.png'
Image.new('P', (50,60), 0).save(source, transparency=0)
palette = core.import_group([source]).images[0]
with Image.open(palette.path) as result:
    assert result.mode == 'RGBA' and result.getpixel((0,0))[3] == 0
bad = root/'bad.png'; bad.write_bytes(b'not an image')
before = core.list()[1]
try: core.import_group([root/'source.jpg',bad])
except ValueError as error: assert 'bad.png' in str(error)
else: raise AssertionError('broken image was accepted')
assert core.list()[1] == before
animated = root/'animated.webp'
Image.new('RGB', (30,30), 'red').save(animated, save_all=True, append_images=[Image.new('RGB',(30,30),'blue')], duration=100)
try: core.import_group([animated])
except ValueError as error: assert '静态' in str(error)
else: raise AssertionError('animated image was accepted')
module.MAX_IMAGE_PIXELS = 10
try: core.import_group([root/'source.jpg'])
except ValueError as error: assert '像素' in str(error)
else: raise AssertionError('pixel limit was ignored')
module.MAX_IMAGE_PIXELS = 40_000_000
module.MAX_IMAGE_BYTES = 10
try: core.import_group([root/'source.jpg'])
except ValueError as error: assert '大小' in str(error)
else: raise AssertionError('file limit was ignored')
assert not any(name.startswith('PySide6.') for name in sys.modules)
print(json.dumps({'formats':formats,'exif':True,'alpha':True,'palette':True,'corrupt':True,'static':True,'limits':True}))
'''
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path)],
                            text=True, capture_output=True,
                            env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[3] / 'src')})
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {'formats': ['jpg', 'png', 'webp', 'bmp'], 'exif': True,
                                       'alpha': True, 'palette': True, 'corrupt': True, 'static': True, 'limits': True}
