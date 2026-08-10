"""Tworzy neutralne mapy demonstracyjne bez odwzorowania realnego obiektu."""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
WIDTH = 1600
HEIGHT = 900


def font(size: int, *, bold: bool = False) -> ImageFont.ImageFont:
    candidates = (
        "C:/Windows/Fonts/segoeuib.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf",
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
    )
    for candidate in candidates:
        if Path(candidate).is_file():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default()


def create_map(path: Path, title: str, accent: str) -> None:
    image = Image.new("RGB", (WIDTH, HEIGHT), "#f3f4f6")
    draw = ImageDraw.Draw(image)
    draw.rectangle((25, 25, WIDTH - 25, HEIGHT - 25), fill="white", outline="#64748b", width=4)
    draw.rectangle((25, 25, WIDTH - 25, 105), fill=accent)
    draw.text((60, 48), title, font=font(32, bold=True), fill="white")
    draw.text(
        (60, 118),
        "FIKCYJNY OBIEKT TESTOWY — BRAK ODWZOROWANIA RZECZYWISTEJ INFRASTRUKTURY",
        font=font(17, bold=True),
        fill="#334155",
    )

    zones = (
        ((70, 180, 490, 500), "STREFA DEMO A", "#dbeafe"),
        ((540, 180, 1030, 500), "STREFA DEMO B", "#dcfce7"),
        ((1080, 180, 1530, 500), "STREFA DEMO C", "#fef3c7"),
        ((220, 570, 1380, 810), "STREFA TECHNICZNA DEMO", "#f3e8ff"),
    )
    for coordinates, label, color in zones:
        draw.rounded_rectangle(coordinates, radius=18, fill=color, outline="#94a3b8", width=3)
        draw.text(
            (coordinates[0] + 24, coordinates[1] + 22),
            label,
            font=font(22, bold=True),
            fill="#1e293b",
        )

    draw.line((510, 150, 510, 535), fill="#cbd5e1", width=5)
    draw.line((1055, 150, 1055, 535), fill="#cbd5e1", width=5)
    draw.text((60, 845), "Mapa zawiera wyłącznie dane fikcyjne.", font=font(16), fill="#475569")
    image.save(path, format="PNG", optimize=True)


def main() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    create_map(DATA / "camera_map.png", "MAPA DEMO — KAMERY", "#991b1b")
    create_map(DATA / "infrastructure_map.png", "MAPA DEMO — INFRASTRUKTURA", "#1e3a8a")


if __name__ == "__main__":
    main()
