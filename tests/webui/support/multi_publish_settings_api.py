import sys,json,tempfile
from pathlib import Path
from yijianjifa.os_integration.paths import AppPaths
from yijianjifa.storage.db import create_engine_for,migrate_to_head
from yijianjifa.app_services.desktop_api import DesktopApi
root=Path(sys.argv[1]);root.mkdir(exist_ok=True)
paths=AppPaths(root/'data',root/'cache',root/'logs',root/'media')
for p in (paths.data_dir,paths.cache_dir,paths.logs_dir,paths.managed_media_dir):p.mkdir(exist_ok=True)
e=create_engine_for(root/'app.db');migrate_to_head(e);api=DesktopApi(e,paths)
print(json.dumps(api.dispatch(sys.stdin.read(),trusted_origin=True)))
api.close(1)
