"""Export the selected launcher artwork as a Windows multiresolution ICO."""
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "assets/branding/whale-maid-pixel.png"
TARGET = ROOT / "assets/branding/windows/whale-maid.ico"
SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)


def main():
    with Image.open(SOURCE) as original:
        # Only format/size conversion; retain the approved artwork and alpha.
        artwork = original.convert("RGBA")
        if artwork.width != artwork.height:
            raise ValueError("Launcher artwork must be square")
        if artwork.getchannel("A").getextrema()[0] != 0:
            raise ValueError("Launcher artwork must retain its transparent background")
        TARGET.parent.mkdir(parents=True, exist_ok=True)
        artwork.save(TARGET, format="ICO", sizes=[(size, size) for size in SIZES])
    with Image.open(TARGET) as icon:
        if icon.ico.sizes() != {(size, size) for size in SIZES}:
            raise RuntimeError("ICO export lost a requested resolution")
    print(f"Exported {len(SIZES)} icon resolutions to {TARGET}")


if __name__ == "__main__":
    main()
