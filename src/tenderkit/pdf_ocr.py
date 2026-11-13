"""Local Polish OCR for selected PDF pages with no extracted text."""
import importlib.util
import math
import os
from pathlib import Path
import sys
import tempfile
import time

from tenderkit.queue import worker_lock
from tenderkit.read_document import MAX_RESPONSE, TextReadError, _bounded_text

OCR_TIMEOUT = 30
DPI = 200
MAX_PIXELS = 16_000_000
# Version the cache when changing the recognition settings or format.
CACHE_VERSION = 'pol-200-v1'


def _recognize(path, page, deadline):
    if importlib.util.find_spec('pymupdf') is None:
        raise TextReadError('Install the OCR extra: pip install "tenderkit[ocr]"')
    with tempfile.TemporaryDirectory() as scratch:
        target = Path(scratch) / 'text.txt'
        command = [sys.executable, '-m', 'tenderkit.pdf_ocr', str(path), str(page), str(target)]
        return _bounded_text(command, target, deadline=deadline, limit=4 * MAX_RESPONSE,
                             operation='PDF OCR (requires Polish pol.traineddata in TESSDATA_PREFIX)')


def _cached_text(path, page, cache, deadline):
    if cache is None:
        return _recognize(path, page, deadline)
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / f'{CACHE_VERSION}-{page}.txt'
    with worker_lock(target, suffix='.lock') as locked:
        if not locked:
            raise TextReadError('OCR is already running for this page; retry shortly')
        if target.is_file():
            with target.open('rb') as stream:
                data = stream.read(4 * MAX_RESPONSE + 1)
            if len(data) > 4 * MAX_RESPONSE:
                raise TextReadError('Cached OCR text exceeds output limit')
            return data.decode('utf-8', errors='replace')
        text = _recognize(path, page, deadline)
        with tempfile.NamedTemporaryFile(dir=cache, suffix='.tmp', delete=False) as stream:
            staged = Path(stream.name)
        try:
            staged.write_text(text, encoding='utf-8', newline='')
            staged.replace(target)
        finally:
            staged.unlink(missing_ok=True)
        return text


def recognize_pages(path, selected, first_page, cache):
    deadline = time.monotonic() + OCR_TIMEOUT
    for number, item in enumerate(selected, first_page):
        if not item['text_layer']:
            item['text'] = _cached_text(path, number, cache, deadline)
            item['text_source'] = 'ocr'


def _worker(path, number, target):
    import pymupdf

    with pymupdf.open(path) as document:
        page = document[number - 1]
        width, height = (math.ceil(n * DPI / 72) for n in (page.rect.width, page.rect.height))
        if not (0 < width <= MAX_PIXELS and 0 < height <= MAX_PIXELS and width * height <= MAX_PIXELS):
            raise ValueError('PDF page exceeds OCR raster limit')
        pixmap = page.get_pixmap(dpi=DPI, colorspace=pymupdf.csRGB, alpha=False)
        data = pixmap.pdfocr_tobytes(language='pol', tessdata=os.environ.get('TESSDATA_PREFIX'))
        with pymupdf.open('pdf', data) as result:
            Path(target).write_text(result[0].get_text(), encoding='utf-8', newline='')


if __name__ == '__main__':
    _worker(sys.argv[1], int(sys.argv[2]), sys.argv[3])
