from __future__ import annotations

import hashlib
import json

from yijianjifa.features.mixing.models import Composition


def composition_fingerprint(composition: Composition) -> str:
    payload = [
        {
            "content_hash": segment.asset_content_hash,
            "source_start_ms": segment.source_start_ms,
            "duration_ms": segment.duration_ms,
        }
        for segment in composition.segments
    ]
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
