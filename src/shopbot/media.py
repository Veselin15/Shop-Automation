"""Сваляне и подготовка на снимките за качване в обявата."""

from __future__ import annotations

import hashlib
import io
import logging
from pathlib import Path

from PIL import Image
from playwright.async_api import BrowserContext

log = logging.getLogger(__name__)

MAX_EDGE = 1600
JPEG_QUALITY = 88
MIN_BYTES = 4_000          # под това е плейсхолдър/иконка, не продуктова снимка
MIN_EDGE = 300


class ImageError(RuntimeError):
    pass


def _target_path(root: Path, product_id: str, index: int, url: str) -> Path:
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
    folder = root / product_id
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{index:02d}_{digest}.jpg"


def _process(raw: bytes) -> bytes:
    """Нормализира към JPEG без EXIF и с разумен размер.

    Bazar.bg отхвърля прекалено големи файлове, а EXIF-ът от CDN-а на източника
    носи метаданни, които няма смисъл да пътуват до обявата.
    """
    with Image.open(io.BytesIO(raw)) as img:
        if min(img.size) < MIN_EDGE:
            raise ImageError(f"снимката е твърде малка: {img.size}")
        img = img.convert("RGB")
        img.thumbnail((MAX_EDGE, MAX_EDGE), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        return buf.getvalue()


async def download_images(
    context: BrowserContext,
    urls: list[str],
    product_id: str,
    root: Path,
    limit: int = 6,
) -> list[Path]:
    """Сваля през контекста на браузъра, за да носи сесийните бисквитки.

    CDN-ът на източника често отказва гола заявка без Referer/сесия.
    """
    saved: list[Path] = []
    seen: set[str] = set()

    for url in urls:
        if len(saved) >= limit:
            break
        if url in seen:
            continue
        seen.add(url)

        dest = _target_path(root, product_id, len(saved), url)
        if dest.exists() and dest.stat().st_size > MIN_BYTES:
            saved.append(dest)
            continue

        try:
            resp = await context.request.get(url, timeout=30_000)
            if not resp.ok:
                log.warning("снимка %s -> HTTP %s", url, resp.status)
                continue
            raw = await resp.body()
            if len(raw) < MIN_BYTES:
                log.debug("снимка %s е само %d байта, пропускам", url, len(raw))
                continue
            dest.write_bytes(_process(raw))
            saved.append(dest)
        except ImageError as exc:
            log.debug("снимка %s отпада: %s", url, exc)
        except Exception as exc:
            log.warning("снимка %s не се свали: %s", url, exc)

    return saved


def cleanup_product_images(root: Path, product_id: str) -> None:
    folder = root / product_id
    if not folder.exists():
        return
    for f in folder.iterdir():
        f.unlink(missing_ok=True)
    folder.rmdir()
