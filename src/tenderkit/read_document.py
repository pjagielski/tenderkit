"""Bounded text extraction. OOXML uses stdlib, legacy XLS the optional xlrd; PDF and legacy DOC run an installed
CLI in a child process."""
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


def _bounded_text(command, target, *, deadline, limit, operation, capture=False):
    """Run a converter that writes text to target (or, with capture, to stdout redirected to target) within limits."""
    if time.monotonic() >= deadline:
        raise TextReadError(f'{operation} exceeded time or output limit')
    output = open(target, 'wb') if capture else None
    try:
        process = subprocess.Popen(command, stdout=output or subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    except OSError as exc:
        if output:
            output.close()
        raise TextReadError(f'{operation} failed: {exc}') from exc
    try:
        while process.poll() is None:
            if time.monotonic() > deadline or target.exists() and target.stat().st_size > limit:
                raise TextReadError(f'{operation} exceeded time or output limit')
            time.sleep(0.05)
        if process.returncode or not target.is_file():
            raise TextReadError(f'{operation} failed')
        if target.stat().st_size > limit:
            raise TextReadError(f'{operation} exceeded time or output limit')
        return target.read_text(encoding='utf-8', errors='replace')
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        if output:
            output.close()


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
        text = _bounded_text(command, target, deadline=time.monotonic() + 30,
                             limit=MAX_TEXT, operation='PDF text extraction')
        pages = text.split('\f')
        if text.endswith('\f'):
            pages.pop()
        return [dict(text=p, label='PDF page') for p in pages or ['']], engine


def _xls_value(xlrd, cell, datemode):
    if cell.ctype == xlrd.XL_CELL_NUMBER:
        return str(int(cell.value)) if float(cell.value).is_integer() else repr(cell.value)
    if cell.ctype == xlrd.XL_CELL_DATE:
        try:
            value = xlrd.xldate.xldate_as_datetime(cell.value, datemode)
        except (ValueError, OverflowError, xlrd.xldate.XLDateError):
            return repr(cell.value)
        return value.date().isoformat() if value.time() == value.min.time() else value.isoformat(sep=' ')
    if cell.ctype == xlrd.XL_CELL_BOOLEAN:
        return 'TRUE' if cell.value else 'FALSE'
    if cell.ctype == xlrd.XL_CELL_ERROR:
        return xlrd.error_text_from_code.get(cell.value, '#ERROR')
    return str(cell.value)


def _xls(path):
    """Legacy Excel 97-2003 sheets in the same cell-reference layout as XLSX. xlrd exposes no formula text, so
    formula cells give their cached value."""
    try:
        import xlrd
    except ImportError as exc:
        raise TextReadError('XLS extraction needs the optional documents extra (xlrd): '
                            'pip install "tenderkit[documents]"') from exc
    try:
        book = xlrd.open_workbook(str(path), on_demand=True)
    except Exception as exc:  # malformed input raises XLRDError, struct.error, AssertionError and others
        raise TextReadError('Invalid or unsupported XLS workbook') from exc
    pages, size = [], 0
    try:
        for index in range(book.nsheets):
            sheet = book.sheet_by_index(index)
            lines = []
            for row in range(sheet.nrows):
                cells = []
                for column in range(sheet.ncols):
                    cell = sheet.cell(row, column)
                    if cell.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
                        continue
                    raw, ref = _xls_value(xlrd, cell, book.datemode), xlrd.cellname(row, column)
                    size += len(raw) + len(ref) + 3
                    if size > MAX_TEXT:
                        raise TextReadError('Extracted text exceeds limit')
                    cells.append(ref + ': ' + raw)
                lines.append('\t'.join(cells))
            pages.extend(_chunks('\n'.join(lines), sheet.name or 'sheet'))
            book.unload_sheet(index)
    finally:
        book.release_resources()
    return pages or [dict(text='', label='workbook')]


def _soffice():
    found = os.environ.get('TENDERKIT_SOFFICE') or shutil.which('soffice') or shutil.which('libreoffice')
    if found:
        return found
    for candidate in (Path(os.environ.get('ProgramFiles', r'C:\Program Files')) / 'LibreOffice' / 'program' / 'soffice.exe',
                      Path('/Applications/LibreOffice.app/Contents/MacOS/soffice')):
        if candidate.is_file():
            return str(candidate)
    return None


def _doc_pages(text):
    text = text.lstrip('\ufeff')
    pages = [chunk for part in text.split('\f') for chunk in _chunks(part, 'document')]
    if len(pages) > 1 and not pages[-1]['text'].strip():
        pages.pop()
    return pages


def _doc(path):
    """Legacy Word 97-2003 text through antiword or, failing that, LibreOffice; both run as bounded child processes."""
    antiword = os.environ.get('TENDERKIT_ANTIWORD') or shutil.which('antiword')
    soffice = _soffice()
    if not antiword and not soffice:
        raise TextReadError('DOC extraction needs antiword or LibreOffice (soffice) on PATH, '
                            'or TENDERKIT_ANTIWORD / TENDERKIT_SOFFICE naming one')
    with tempfile.TemporaryDirectory() as scratch:
        scratch = Path(scratch)
        if antiword:
            try:
                text = _bounded_text([antiword, '-m', 'UTF-8.txt', '-w', '0', str(path)], scratch / 'antiword.txt',
                                     deadline=time.monotonic() + 30, limit=MAX_TEXT, operation='DOC text extraction',
                                     capture=True)
                return _doc_pages(text), 'antiword'
            except TextReadError:
                # antiword reads only Word 97-2003 binaries; RTF or older Word saved as .doc may still convert below.
                if not soffice:
                    raise
        # A private copy with a short name, since LibreOffice names its output after the input, and a private profile,
        # so the conversion never attaches to or waits for a LibreOffice the user has open.
        source = scratch / 'document.doc'
        shutil.copyfile(path, source)
        text = _bounded_text([soffice, '--headless', '--norestore', '-env:UserInstallation=' + (scratch / 'profile').as_uri(),
                              '--convert-to', 'txt:Text (encoded):UTF8', '--outdir', str(scratch / 'out'), str(source)],
                             scratch / 'out' / 'document.txt', deadline=time.monotonic() + 90, limit=MAX_TEXT,
                             operation='DOC conversion with LibreOffice')
        return _doc_pages(text), 'libreoffice'


def _sniff(path, kind):
    """The real format of a legacy-named file: portals often serve DOCX/XLSX under .doc/.xls."""
    if kind in ('doc', 'xls'):
        with open(path, 'rb') as stream:
            if stream.read(4) == b'PK\x03\x04':
                return kind + 'x'
    return kind


def read_document(path, *, page=1, pages=1, ocr=False, ocr_cache=None):
    if type(page) is not int or page < 1 or type(pages) is not int or not 1 <= pages <= 5:
        raise ValueError('Page must be positive; request 1 to 5 pages')
    path = Path(path)
    if path.stat().st_size > MAX_INPUT:
        raise TextReadError('Text extraction input exceeds 20 MB limit')
    kind = _sniff(path, path.suffix.lower().lstrip('.'))
    if ocr and kind != 'pdf':
        raise TextReadError('OCR is available only for PDFs')
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
        elif kind == 'xls':
            extracted, engine = _xls(path), 'xlrd'
            pagination = 'logical chunks (12000 characters, separated by worksheets)'
        elif kind == 'doc':
            extracted, engine = _doc(path)
            pagination = 'logical chunks (12000 characters, separated by page breaks the converter reports)'
        else:
            raise TextReadError('Supported formats: PDF, DOCX, XLSX, DOC, XLS')
    except (BadZipFile, ET.ParseError, KeyError, IndexError) as exc:
        raise TextReadError('Invalid or unsupported document structure') from exc
    if page > len(extracted):
        raise ValueError('Page outside document')
    selected = extracted[page-1:page-1+pages]
    if kind == 'pdf':
        for item in selected:
            item['text_layer'] = bool(item['text'].strip())
            item['text_source'] = 'embedded' if item['text_layer'] else None
        if ocr and any(not item['text_layer'] for item in selected):
            from tenderkit.pdf_ocr import recognize_pages
            recognize_pages(path, selected, page, ocr_cache)
    if sum(len(p['text']) for p in selected) > MAX_RESPONSE:
        raise TextReadError('Selected pages exceed response text limit')
    return dict(format=kind, engine=engine, pagination=pagination, total_pages=len(extracted),
        pages=[dict(item, page=page+i) for i, item in enumerate(selected)],
        next_page=page+len(selected) if page+len(selected) <= len(extracted) else None,
        text_available=any(item['text'].strip() for item in selected))
