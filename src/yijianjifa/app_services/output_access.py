"""Output directory chosen by the native dialog, never a page-supplied path."""
from pathlib import Path
import tempfile

from yijianjifa.core.settings import GlobalSettings


class OutputAccess:
    def __init__(self, engine, paths):
        self.settings = GlobalSettings(engine, default_output_directory=paths.data_dir / '成品')

    def read(self):
        return {'directory': str(self.settings.output_directory)}

    def choose(self, selected):
        if not selected:
            return {**self.read(), 'cancelled': True}
        if not isinstance(selected, (list, tuple)) or len(selected) != 1:
            raise ValueError('请选择一个成品文件夹。')
        path = Path(selected[0]).resolve()
        if not path.is_dir():
            raise ValueError('请选择已有文件夹。')
        try:
            with tempfile.TemporaryFile(dir=path, prefix='.p011-write-check-') as probe:
                probe.write(b'p011'); probe.flush()
        except OSError:
            raise ValueError('无法写入这个文件夹，请检查权限。') from None
        self.settings.output_directory = path
        return {**self.read(), 'cancelled': False}
