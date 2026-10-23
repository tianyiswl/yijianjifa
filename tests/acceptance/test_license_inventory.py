import importlib.util
from pathlib import Path


def test_license_inventory_excludes_debug_and_source_files_named_copying():
    path=Path(__file__).resolve().parents[2]/'tools/mac_license_inventory.py'
    spec=importlib.util.spec_from_file_location('mac_license_inventory',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    accepted=['pkg.dist-info/LICENSE','pkg.dist-info/LICENCE.python','COPYING.GPLv2','LICENSE-BSD2.md','COPYRIGHT.txt']
    rejected=['PyObjCTest/copying.cpython-312-darwin.so.dSYM/Contents/Info.plist','copying.cpython-312-darwin.so','copying.py','copying.m','licenses/README.rst']
    for name in accepted:assert module.is_license_file(Path(name)),name
    for name in rejected:assert not module.is_license_file(Path(name)),name
