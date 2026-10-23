"""Upload probe contract. Only the host supplies observations and the fixed file."""
from pathlib import Path
import hashlib
from .models import CapabilitySnapshot

FIXTURE_SHA256 = 'd87ff718eed1660d0df005eeb78626848ba29b956a66d1d38ac4055a9cc9ba2a'
SCOPES = {'pending', 'local', 'national', 'local_national'}


def fixture_path():
    path = Path(__file__).parents[2] / 'webui/assets/capability-test.mp4'
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != FIXTURE_SHA256:
        raise ValueError('capability fixture unavailable')
    return path


def interpret_observation(value, expected_uid):
    """Missing controls are not negative evidence without a completed upload/form.

    Scope labels must come from the location selector, not arbitrary page text.
    The native adapter must verify identity before upload and after observation.
    """
    if type(value) is not dict or set(value) != {'uid_before','uid_after','upload_complete','form_ready','commerce_visible','scope_options'}:
        raise ValueError('invalid capability observation')
    if value['uid_before'] != expected_uid or value['uid_after'] != expected_uid:
        raise ValueError('capability identity mismatch')
    for key in ('upload_complete','form_ready','commerce_visible'):
        if type(value[key]) is not bool:raise ValueError('invalid observation flag')
    options=value['scope_options']
    if type(options) is not list or any(type(s) is not str or s not in {'local','national'} for s in options):
        raise ValueError('invalid location options')
    if not value['upload_complete'] or not value['form_ready']:
        return CapabilitySnapshot()
    scopes=set(options)
    scope='local_national' if scopes=={'local','national'} else next(iter(scopes),'pending')
    return CapabilitySnapshot(ordinary='available',commerce='available' if value['commerce_visible'] else 'not_found',location_scope=scope)
