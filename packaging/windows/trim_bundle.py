"""Trim only byte-identical reviewed inputs in a newly built Windows folder.

No installed application, user profile, model, media, or browser cache is touched.
Dependency drift fails before any removal; unknown files remain in the bundle.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path, PurePosixPath
import re

ALLOWED_QT_DLLS = frozenset('''Qt63DAnimation.dll Qt63DCore.dll Qt63DExtras.dll Qt63DInput.dll Qt63DLogic.dll Qt63DQuick.dll Qt63DQuickAnimation.dll Qt63DQuickExtras.dll Qt63DQuickInput.dll Qt63DQuickLogic.dll Qt63DQuickRender.dll Qt63DQuickScene2D.dll Qt63DQuickScene3D.dll Qt63DRender.dll Qt6Charts.dll Qt6ChartsQml.dll Qt6Concurrent.dll Qt6DataVisualization.dll Qt6DataVisualizationQml.dll Qt6Graphs.dll Qt6LabsAnimation.dll Qt6LabsFolderListModel.dll Qt6LabsPlatform.dll Qt6LabsQmlModels.dll Qt6LabsSettings.dll Qt6LabsSharedImage.dll Qt6LabsStyleKit.dll Qt6LabsStyleKitImpl.dll Qt6LabsSynchronizer.dll Qt6LabsWavefrontMesh.dll Qt6Location.dll Qt6OpenGLWidgets.dll Qt6PdfQuick.dll Qt6PositioningQuick.dll Qt6QmlCore.dll Qt6QmlDesignSupport.dll Qt6QmlLocalStorage.dll Qt6QmlNetwork.dll Qt6QmlXmlListModel.dll Qt6Quick3D.dll Qt6Quick3DAssetImport.dll Qt6Quick3DAssetUtils.dll Qt6Quick3DEffects.dll Qt6Quick3DHelpers.dll Qt6Quick3DHelpersImpl.dll Qt6Quick3DParticleEffects.dll Qt6Quick3DParticles.dll Qt6Quick3DPhysics.dll Qt6Quick3DPhysicsHelpers.dll Qt6Quick3DRuntimeRender.dll Qt6Quick3DSpatialAudio.dll Qt6Quick3DUtils.dll Qt6Quick3DXr.dll Qt6QuickControls2.dll Qt6QuickControls2Basic.dll Qt6QuickControls2BasicStyleImpl.dll Qt6QuickControls2FluentWinUI3.dll Qt6QuickControls2FluentWinUI3StyleImpl.dll Qt6QuickControls2Fusion.dll Qt6QuickControls2FusionStyleImpl.dll Qt6QuickControls2Imagine.dll Qt6QuickControls2ImagineStyleImpl.dll Qt6QuickControls2Impl.dll Qt6QuickControls2Material.dll Qt6QuickControls2MaterialStyleImpl.dll Qt6QuickControls2Native.dll Qt6QuickControls2Universal.dll Qt6QuickControls2UniversalStyleImpl.dll Qt6QuickControls2Windows.dll Qt6QuickControls2WindowsStyleImpl.dll Qt6QuickDialogs2.dll Qt6QuickDialogs2QuickImpl.dll Qt6QuickDialogs2Utils.dll Qt6QuickEffects.dll Qt6QuickLayouts.dll Qt6QuickNativeStyle.dll Qt6QuickParticles.dll Qt6QuickShapes.dll Qt6QuickShapesDesignHelpers.dll Qt6QuickTemplates2.dll Qt6QuickTest.dll Qt6QuickTimeline.dll Qt6QuickTimelineBlendTrees.dll Qt6QuickVectorImage.dll Qt6QuickVectorImageGenerator.dll Qt6QuickVectorImageHelpers.dll Qt6RemoteObjects.dll Qt6RemoteObjectsQml.dll Qt6Scxml.dll Qt6ScxmlQml.dll Qt6Sensors.dll Qt6SensorsQuick.dll Qt6SerialPort.dll Qt6SpatialAudio.dll Qt6Sql.dll Qt6StateMachine.dll Qt6StateMachineQml.dll Qt6Test.dll Qt6TextToSpeech.dll Qt6VirtualKeyboard.dll Qt6VirtualKeyboardQml.dll Qt6VirtualKeyboardSettings.dll Qt6WebChannelQuick.dll Qt6WebEngineQuick.dll Qt6WebEngineQuickDelegatesQml.dll Qt6WebSockets.dll Qt6WebView.dll Qt6WebViewQuick.dll'''.split())


def allowed(relative):
    if '\\' in relative or '..' in PurePosixPath(relative).parts or PurePosixPath(relative).is_absolute():
        return False
    if relative.startswith('_internal/PySide6/qml/'):
        return not relative.startswith('_internal/PySide6/qml/QtMultimedia/')
    qt = '_internal/PySide6/'
    if relative.startswith(qt) and '/' not in relative[len(qt):]:
        return relative[len(qt):] in ALLOWED_QT_DLLS
    if relative.startswith(qt + 'translations/') or re.fullmatch(r'_internal/playwright-browsers/chromium-\d+/chrome-win64/locales/[^/]+\.pak', relative):
        return not re.search(r'(?:^|[_-])(en|zh)(?:[_-]|\.|$)', PurePosixPath(relative).name, re.I)
    return False


def trim(root, manifest, report):
    root = Path(root).resolve(strict=True)
    rows = json.loads(Path(manifest).read_text(encoding='utf-8'))
    if rows.get('schema_version') != 1 or not rows.get('files'):
        raise ValueError('Invalid reviewed manifest')
    targets = []; seen = set()
    # Validate the entire set, including content hashes, before unlinking anything.
    for item in rows['files']:
        relative = item['relative']; path = root / relative
        if relative in seen or not allowed(relative) or not path.resolve(strict=True).is_relative_to(root):
            raise ValueError('Unreviewed or unsafe trim destination: ' + relative)
        seen.add(relative)
        current = path
        while current != root:
            if current.is_symlink(): raise ValueError('Symlink in trim destination')
            current = current.parent
        if not path.is_file() or path.stat().st_size != item['size']:
            raise ValueError('Dependency size drift: ' + relative)
        with path.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != item['sha256']:
                raise ValueError('Dependency content drift: ' + relative)
        targets.append((path, item))
    before = sum(p.stat().st_size for p in root.rglob('*') if p.is_file())
    groups = Counter()
    for path, item in targets:
        path.unlink()
        groups[item.get('reason', 'reviewed')] += item['size']
    result = {'schema_version': 1, 'reviewed_source': rows.get('reviewed_source'),
              'before_bytes': before, 'after_bytes': before - sum(groups.values()),
              'removed_bytes': sum(groups.values()), 'removed_files': len(targets), 'groups': dict(groups)}
    Path(report).write_text(json.dumps(result, indent=2)+'\n', encoding='utf-8')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, default=Path(__file__).with_name('trim-manifest.json'))
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(trim(args.root, args.manifest, args.report)), flush=True)
