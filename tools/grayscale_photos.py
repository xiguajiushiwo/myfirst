"""Create lossless grayscale copies of the project's inspection test photos."""

from pathlib import Path

from PIL import Image


SOURCE_DIR = Path(__file__).resolve().parents[1] / "test_photos"
OUTPUT_DIR = SOURCE_DIR / "grayscale"
IMAGE_SUFFIXES = {".bmp", ".jpeg", ".jpg", ".png", ".tif", ".tiff"}


def main() -> None:
    OUTPUT_DIR.mkdir(exist_ok=True)
    sources = sorted(
        path for path in SOURCE_DIR.iterdir()
        if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
    )
    for source in sources:
        destination = OUTPUT_DIR / f"{source.stem}.png"
        with Image.open(source) as image:
            image.convert("L").save(destination, format="PNG", optimize=True)
        print(f"{source.name} -> {destination.name}")

    print(f"created={len(sources)} output={OUTPUT_DIR}")


if __name__ == "__main__":
    main()
