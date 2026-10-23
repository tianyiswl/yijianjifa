"""Small, bounded failure context for local mixing receipts."""
from pathlib import Path
import subprocess

from yijianjifa.features.tasks.diagnostics import _redact_text


class MixStepError(RuntimeError):
    def __init__(self, diagnostic):
        self.diagnostic = diagnostic
        material = diagnostic.get('material') or '、'.join(diagnostic.get('materials', []))
        super().__init__(f"{diagnostic['stage']}失败；"
                         + (f'素材：{material}；' if material else '')
                         + f"原因：{diagnostic['reason']}")


def error_diagnostic(exc, stage='制作视频', materials=()):
    """Keep original probe attribution; map owned copies to frozen user names."""
    prior = getattr(exc, 'diagnostic', {})
    prior = prior if isinstance(prior, dict) else {}
    reason = prior.get('reason')
    if not isinstance(reason, str):
        if isinstance(exc, subprocess.CalledProcessError):
            detail = exc.stderr.decode('utf-8', errors='replace') if isinstance(exc.stderr, bytes) else exc.stderr
            reason = f'媒体处理工具执行失败（退出码 {exc.returncode}）' + (f'：{detail}' if detail else '')
        else:
            reason = str(exc) or type(exc).__name__
    aliases = {}
    names = []
    for path, label in materials:
        name = str(label or Path(path).name)
        if name not in names:
            names.append(name)
        aliases[Path(path).name] = name
        for spelling in (str(path), str(path).replace('\\', '/'), str(path).replace('/', '\\')):
            reason = reason.replace(spelling, name)
    for original, label in aliases.items():
        reason = reason.replace(original, label)
    material = prior.get('material', '')
    material = aliases.get(material, material) if isinstance(material, str) else ''
    if not material and len(names) == 1:
        material = names[0]
    recorded = prior.get('materials', names)
    recorded = recorded if isinstance(recorded, list) else names
    return dict(stage=_redact_text(str(prior.get('stage') or stage))[:160],
                material=_redact_text(material)[:255],
                materials=[_redact_text(aliases.get(n, n))[:255] for n in recorded if isinstance(n, str)][:100],
                reason=_redact_text(reason)[-4000:],
                error_type=str(prior.get('error_type') or type(exc).__name__)[:80])
