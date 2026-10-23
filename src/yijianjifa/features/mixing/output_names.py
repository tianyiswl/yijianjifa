"""Stable output names shared by rendering and crash recovery."""
from datetime import datetime
import re


def new_output_stamp():
    return datetime.now().astimezone().strftime('%Y%m%d_%H%M%S_%f')


def output_filename(snapshot, index):
    stamp = snapshot.get('output_name_stamp')
    if stamp is None:  # Preserve paths of already frozen tasks.
        return f'{index + 1:03d}.mp4'
    if not isinstance(stamp, str) or not re.fullmatch(r'\d{8}_\d{6}_\d{6}', stamp):
        raise ValueError('invalid output timestamp')
    return f'{stamp}_{index + 1:03d}.mp4'
