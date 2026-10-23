"""Console-free child processes for desktop media and speech tools."""
import subprocess
import sys


def hidden_process_options():
    # Literal keeps imports portable: subprocess.CREATE_NO_WINDOW exists only on Windows.
    return {'creationflags': 0x08000000} if sys.platform == 'win32' else {}


def run_hidden(*args, **kwargs):
    options = hidden_process_options()
    if options:
        kwargs['creationflags'] = kwargs.get('creationflags', 0) | options['creationflags']
    return subprocess.run(*args, **kwargs)
