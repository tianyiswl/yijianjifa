"""Stable output names shared by rendering and crash recovery."""
from datetime import datetime
import re


def new_output_stamp():
    return datetime.now().astimezone().strftime('%m%d_%H%M%S')


def output_filename(snapshot, index):
    stamp = snapshot.get('output_name_stamp')
    if stamp is None:  # Preserve paths of already frozen tasks.
        return f'{index + 1:03d}.mp4'
    if isinstance(stamp, str):
        if re.fullmatch(r'\d{4}_\d{6}', stamp):
            return f'{stamp}_{index + 1:02d}.mp4'
        if re.fullmatch(r'\d{8}_\d{6}_\d{6}', stamp):
            return f'{stamp}_{index + 1:03d}.mp4'
    raise ValueError('invalid output timestamp')
