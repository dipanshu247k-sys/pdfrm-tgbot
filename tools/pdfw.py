import argparse
import io
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

import img2pdf
from PIL import Image

WATERMARK_OPACITY = 0.3


def natural_key(value: str):
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", value)]


def resolve_pdfimages_binary(script_dir: Path) -> Path:
    config_path = script_dir / "pdfimages-path.json"
    if config_path.exists():
        import json

        try:
            data = json.loads(config_path.read_text(encoding="utf-8-sig"))
            configured = data.get("pdfimages") or data.get("pdfimages_exe")
            if configured:
                configured_path = Path(configured).expanduser().resolve()
                if configured_path.exists() and configured_path.is_file():
                    return configured_path
        except (json.JSONDecodeError, OSError):
            pass

    in_path = shutil.which("pdfimages")
    if not in_path:
        raise FileNotFoundError(
            "pdfimages binary not found. Install poppler-utils or configure tools/pdfimages-path.json"
        )
    return Path(in_path)


def apply_watermark(pdf_path: Path, watermark_image: Path) -> None:
    try:
        import fitz
    except ImportError as exc:
        raise RuntimeError("PyMuPDF is required when using -wmark.") from exc

    try:
        with Image.open(watermark_image) as source_image:
            rgba_image = source_image.convert("RGBA")
            alpha = rgba_image.getchannel("A")
            opacity_scale = max(0, min(255, int(round(WATERMARK_OPACITY * 255))))
            alpha = alpha.point(lambda value: (value * opacity_scale) // 255)
            rgba_image.putalpha(alpha)

            watermark_width, watermark_height = rgba_image.size
            buffer = io.BytesIO()
            rgba_image.save(buffer, format="PNG")
            watermark_png_bytes = buffer.getvalue()
    except Exception as exc:
        raise ValueError(f"Failed to process watermark image: {watermark_image}") from exc

    temp_output_path = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix="pdfw-wm-", suffix=".pdf", dir=pdf_path.parent, delete=False
        ) as tmp_file:
            temp_output_path = Path(tmp_file.name)

        with fitz.open(str(pdf_path)) as target_pdf:
            xref = 0
            for page in target_pdf:
                page_rect = page.rect
                scale = min(
                    page_rect.width / float(watermark_width),
                    page_rect.height / float(watermark_height),
                )
                draw_width = float(watermark_width) * scale
                draw_height = float(watermark_height) * scale
                offset_x = (page_rect.width - draw_width) / 2.0
                offset_y = (page_rect.height - draw_height) / 2.0
                watermark_rect = fitz.Rect(
                    offset_x,
                    offset_y,
                    offset_x + draw_width,
                    offset_y + draw_height,
                )
                xref = page.insert_image(
                    watermark_rect,
                    stream=watermark_png_bytes,
                    overlay=True,
                    keep_proportion=False,
                    xref=xref,
                )
            target_pdf.save(str(temp_output_path))

        temp_output_path.replace(pdf_path)
    except Exception as exc:
        raise OSError(f"Failed to apply watermark to {pdf_path}: {exc}") from exc
    finally:
        if temp_output_path and temp_output_path.exists():
            try:
                temp_output_path.unlink()
            except OSError:
                pass


def convert_pdf(
    source_pdf: Path,
    output_pdf: Path,
    script_dir: Path,
    pdfimages_bin: Path,
    watermark_image: Optional[Path] = None,
) -> int:
    with tempfile.TemporaryDirectory(prefix="pdfw-") as tmp_dir:
        tmp_path = Path(tmp_dir)
        out_prefix = tmp_path / "w"

        try:
            subprocess.run(
                [str(pdfimages_bin), "-j", str(source_pdf), str(out_prefix)],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            stderr_text = (exc.stderr or "").strip()
            print(
                f"pdfimages failed for {source_pdf} with exit code {exc.returncode}"
                + (f": {stderr_text}" if stderr_text else ""),
                file=sys.stderr,
            )
            return 1

        dedup_script = script_dir / "dedup.py"
        if not dedup_script.exists():
            print(f"Missing helper script: {dedup_script}", file=sys.stderr)
            return 1

        try:
            subprocess.run([sys.executable, str(dedup_script), str(tmp_path)], check=True)
        except subprocess.CalledProcessError as exc:
            print(
                f"dedup.py failed for {source_pdf} with exit code {exc.returncode}",
                file=sys.stderr,
            )
            return 1

        images = sorted(
            [p for p in tmp_path.iterdir() if p.is_file() and p.name.startswith("w-")],
            key=lambda p: natural_key(p.name),
        )

        if not images:
            print(f"No extracted images were found for {source_pdf}.", file=sys.stderr)
            return 1

        output_pdf.parent.mkdir(parents=True, exist_ok=True)
        try:
            with output_pdf.open("wb") as output_file:
                output_file.write(img2pdf.convert([str(img) for img in images]))
        except (OSError, ValueError) as exc:
            print(f"Failed to create output PDF {output_pdf}: {exc}", file=sys.stderr)
            return 1

        if watermark_image is not None:
            try:
                apply_watermark(output_pdf, watermark_image)
            except (RuntimeError, ValueError, OSError) as exc:
                print(f"Failed to apply watermark to {output_pdf}: {exc}", file=sys.stderr)
                return 1

    print(f"Created: {output_pdf}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Convert a PDF into a *_pdfw.pdf output")
    parser.add_argument("source_pdf", help="Path to source PDF file")
    parser.add_argument("-o", "--output", help="Optional output PDF path")
    parser.add_argument("-wmark", dest="watermark_image", help="Optional watermark image path")
    args = parser.parse_args()

    source_pdf = Path(args.source_pdf).expanduser().resolve()
    if not source_pdf.exists() or not source_pdf.is_file() or source_pdf.suffix.lower() != ".pdf":
        print(f"Input file is not a valid PDF: {source_pdf}", file=sys.stderr)
        return 1

    output_pdf = (
        Path(args.output).expanduser().resolve()
        if args.output
        else source_pdf.with_name(f"{source_pdf.stem}_pdfw.pdf")
    )

    watermark_image = None
    if args.watermark_image:
        watermark_image = Path(args.watermark_image).expanduser().resolve()
        if not watermark_image.exists() or not watermark_image.is_file():
            print(f"Watermark image not found: {watermark_image}", file=sys.stderr)
            return 1

    script_dir = Path(__file__).resolve().parent
    try:
        pdfimages_bin = resolve_pdfimages_binary(script_dir)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    return convert_pdf(source_pdf, output_pdf, script_dir, pdfimages_bin, watermark_image)


if __name__ == "__main__":
    raise SystemExit(main())
