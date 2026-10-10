"""Windowless scheduled entry point; preserve runner logs and exit status."""
import os
from pathlib import Path
import subprocess
import sys

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    config = args.config or root / 'archive-config.json'  # legacy tasks only
    powershell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    return subprocess.call(
        [str(powershell), '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
         '-File', str(root / 'run-archive.ps1'), '-ConfigPath', str(config.resolve())],
        cwd=root, creationflags=subprocess.CREATE_NO_WINDOW,
    )


if __name__ == '__main__':
    sys.exit(main())
