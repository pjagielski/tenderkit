"""Bounded text extraction. OOXML uses stdlib; PDF runs an installed CLI in a child process."""
from pathlib import Path, PurePosixPath
import os
import shutil
import subprocess
import sys
import tempfile
import time
from xml.etree import ElementTree as ET
from zipfile import ZipFile, BadZipFile

MAX_INPUT = 20 * 1024 * 1024
MAX_TEXT = 8 * 1024 * 1024
MAX_XML = 32 * 1024 * 1024
MAX_RESPONSE = 60000
CHUNK_CHARS = 12000
W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
S = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'


class TextReadError(ValueError):
    pass


def _chunks(text, label):
    return [dict(text=text[i:i+CHUNK_CHARS], label=label) for i in range(0, len(text), CHUNK_CHARS)] or [dict(text='', label=label)]


def _xml(archive, name):
    data = archive.read(name)
    if b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
        raise TextReadError('XML declarations are not supported')
    return ET.fromstring(data)


def _docx(archive):
    root = _xml(archive, 'word/document.xml')
    pages, text, size = [], [], 0
    for paragraph in root.iter(W + 'p'):
        for node in paragraph.iter():
            if node.tag == W + 't':
                size += len(node.text or '')
                if size > MAX_TEXT:
                    raise TextReadError('Extracted text exceeds limit')
                text.append(node.text or '')
            elif node.tag == W + 'tab':
                text.append('\t')
            elif node.tag == W + 'lastRenderedPageBreak' or node.tag == W + 'br' and node.get(W + 'type') == 'page':
                pages.extend(_chunks(''.join(text), 'document'))
                text = []
            elif node.tag == W + 'br':
                text.append('\n')
        text.append('\n')
    pages.extend(_chunks(''.join(text), 'document'))
    return pages


def _xlsx(archive):
    shared = []
    if 'xl/sharedStrings.xml' in archive.namelist():
        shared = [''.join(node.itertext()) for node in _xml(archive, 'xl/sharedStrings.xml').findall(S + 'si')]
    workbook = _xml(archive, 'xl/workbook.xml')
    relationships = {node.get('Id'): node for node in _xml(archive, 'xl/_rels/workbook.xml.rels')}
    relation_id = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id'
    pages, size = [], 0
    for sheet in workbook.findall(S + 'sheets/' + S + 'sheet'):
        relation = relationships.get(sheet.get(relation_id))
        if relation is None or relation.get('TargetMode') == 'External':
            raise TextReadError('Unsupported worksheet relationship')
        target = relation.get('Target', '')
        path = PurePosixPath(target.lstrip('/')) if target.startswith('/') else PurePosixPath('xl') / target
        if '..' in path.parts or not str(path).startswith('xl/'):
            raise TextReadError('Invalid worksheet path')
        root = _xml(archive, str(path))
        lines = []
        for row in root.iter(S + 'row'):
            cells = []
            for cell in row.findall(S + 'c'):
                raw = cell.findtext(S + 'v') or ''
                kind = cell.get('t')
                if kind == 's':
                    index = int(raw)
                    if not 0 <= index < len(shared):
                        raise TextReadError('Invalid shared-string reference')
                    raw = shared[index]
                elif kind == 'inlineStr':
                    raw = ''.join(n.text or '' for n in cell.iter(S + 't'))
                elif cell.find(S + 'f') is not None:
                    # Keep formula text and cached value as data; never evaluate it.
                    raw = '=' + (cell.findtext(S + 'f') or '') + (' [cached: ' + raw + ']' if raw else '')
                size += len(raw) + len(cell.get('r') or '?') + 3
                if size > MAX_TEXT:
                    raise TextReadError('Extracted text exceeds limit')
                cells.append((cell.get('r') or '?') + ': ' + raw)
            lines.append('\t'.join(cells))
        pages.extend(_chunks('\n'.join(lines), sheet.get('name') or 'sheet'))
    return pages or [dict(text='', label='workbook')]


def _pdf(path):
    with tempfile.TemporaryDirectory() as scratch:
        target = Path(scratch) / 'text.txt'
        executable = os.environ.get('TENDERKIT_PDFTOTEXT') or shutil.which('pdftotext')
        if executable:
            command = [executable, '-enc', 'UTF-8', '-layout', str(path), str(target)]
            engine = 'pdftotext'
        else:
            executable = shutil.which('pymupdf') or str(Path(sys.executable).with_name('pymupdf.exe' if os.name == 'nt' else 'pymupdf'))
            if not Path(executable).is_file():
                raise TextReadError('PDF extraction needs pdftotext or the optional documents extra (PyMuPDF CLI)')
            command = [executable, 'gettext', '-mode', 'simple', '-output', str(target), str(path)]
            engine = 'pymupdf-cli'
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        deadline = time.monotonic() + 30
        try:
            while process.poll() is None:
                if time.monotonic() > deadline or target.exists() and target.stat().st_size > MAX_TEXT:
                    raise TextReadError('PDF text extraction exceeded time or output limit')
                time.sleep(0.05)
            if process.returncode or not target.is_file():
                raise TextReadError('PDF text extraction failed')
            if target.stat().st_size > MAX_TEXT:
                raise TextReadError('Extracted text exceeds limit')
            text = target.read_text(encoding='utf-8', errors='replace')
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
        pages = text.split('\f')
        if text.endswith('\f'):
            pages.pop()
        return [dict(text=p, label='PDF page') for p in pages or ['']], engine


def read_document(path, *, page=1, pages=1):
    if type(page) is not int or page < 1 or type(pages) is not int or not 1 <= pages <= 5:
        raise ValueError('Page must be positive; request 1 to 5 pages')
    path = Path(path)
    if path.stat().st_size > MAX_INPUT:
        raise TextReadError('Text extraction input exceeds 20 MB limit')
    kind = path.suffix.lower().lstrip('.')
    engine, pagination = 'stdlib-ooxml', 'logical chunks (12000 characters, separated by explicit page breaks or worksheets)'
    try:
        if kind == 'pdf':
            extracted, engine = _pdf(path)
            pagination = 'physical PDF pages'
        elif kind in ('docx', 'xlsx'):
            with ZipFile(path) as archive:
                infos = archive.infolist()
                if len(infos) > 2000 or sum(i.file_size for i in infos) > MAX_XML:
                    raise TextReadError('Office archive exceeds extraction limits')
                extracted = _docx(archive) if kind == 'docx' else _xlsx(archive)
                if sum(len(p['text'].encode('utf-8')) for p in extracted) > MAX_TEXT:
                    raise TextReadError('Extracted text exceeds limit')
        else:
            raise TextReadError('Supported formats: PDF, DOCX, XLSX (no archive expansion or OCR)')
    except (BadZipFile, ET.ParseError, KeyError, IndexError) as exc:
        raise TextReadError('Invalid or unsupported document structure') from exc
    if page > len(extracted):
        raise ValueError('Page outside document')
    selected = extracted[page-1:page-1+pages]
    if sum(len(p['text']) for p in selected) > MAX_RESPONSE:
        raise TextReadError('Selected pages exceed response text limit')
    return dict(format=kind, engine=engine, pagination=pagination, total_pages=len(extracted),
        pages=[dict(item, page=page+i) for i, item in enumerate(selected)],
        next_page=page+len(selected) if page+len(selected) <= len(extracted) else None,
        text_available=any(item['text'].strip() for item in selected))
