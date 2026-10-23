"""Collect installed Python dependency license notices for the local release."""
from __future__ import annotations
import argparse
import importlib.metadata as metadata
import json
from pathlib import Path
import shutil


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    target = args.destination.resolve()
    target.mkdir(parents=True, exist_ok=True)
    inventory = []
    for dist in sorted(metadata.distributions(), key=lambda item: item.metadata.get('Name', '').lower()):
        name = dist.metadata.get('Name', 'unknown')
        if name.lower() == 'comfybridge':
            continue
        safe = ''.join(c for c in name if c.isalnum() or c in '-_.')
        directory = target / safe
        directory.mkdir(exist_ok=True)
        notices = []
        for file in dist.files or []:
            basename = Path(str(file)).name
            if any(token in basename.upper() for token in ('LICENSE', 'COPYING', 'NOTICE')):
                source = Path(dist.locate_file(file))
                if source.is_file() and source.stat().st_size <= 2 * 1024 * 1024:
                    filename = f'{len(notices):02d}-{basename}'
                    shutil.copyfile(source, directory / filename)
                    notices.append(filename)
        inventory.append({'name': name, 'version': dist.version,
                          'license_expression': dist.metadata.get('License-Expression'),
                          'license': dist.metadata.get('License'),
                          'home_page': dist.metadata.get('Home-page'),
                          'project_urls': dist.metadata.get_all('Project-URL', []),
                          'notice_files': notices})
    (target / 'inventory.json').write_text(json.dumps(inventory, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    # Python's own PSF license is present in uv-managed interpreter installs.
    import sys
    for root in (Path(sys.base_prefix), Path(sys.base_prefix).parent):
        for name in ('LICENSE.txt', 'LICENSE'):
            source = root / name
            if source.is_file():
                shutil.copyfile(source, target / 'PYTHON-LICENSE.txt')
                return


if __name__ == '__main__':
    main()
