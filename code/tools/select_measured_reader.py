"""Select the measured Awiros reader for both image upload and live video."""
import argparse
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]


def select(root, device):
    root = Path(root)
    profile = 'camera-fp16-gpu.json' if device == 'gpu' else 'camera-layout-1280.json'
    camera = json.loads((root / 'config' / profile).read_text())
    settings = json.loads((root / 'config.json').read_text())
    settings.update(camera['plate_overrides'])
    # Both applications use an explicit device; never silently claim GPU results.
    settings.update(plate_device='0' if device == 'gpu' else 'cpu',
                    ocr_device='0' if device == 'gpu' else 'cpu')
    for path in [root / 'config.json', root / 'config' / 'camera.json']:
        backup = path.with_suffix(path.suffix + '.before-measured-reader')
        if not backup.exists():
            shutil.copy2(path, backup)
    (root / 'config.json').write_text(json.dumps(settings, indent=2) + '\n')
    (root / 'config' / 'camera.json').write_text(json.dumps(camera, indent=2) + '\n')
    return profile


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--device', choices=['cpu', 'gpu'], default='cpu')
    args = parser.parse_args()
    print('Selected', select(ROOT, args.device), 'for image upload and live video.')
    print('Restart the application. Calibrate camera gate coordinates before use.')
    print('Crop OCR accuracy does not establish automatic crossing-log accuracy.')
