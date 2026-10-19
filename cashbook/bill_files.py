"""
What a bill really is, read from its bytes: never from its name or the type
the phone claims.

Photos are decoded and written out again, so whatever rode along in the file
(EXIF and GPS, a script glued to the end, a second file hidden inside) is left
behind. A PDF cannot be rewritten safely, so it is read whole and refused when
anything in it acts on its own: scripts, launch actions, embedded files, forms
that submit somewhere.
"""
import io
import warnings
from dataclasses import dataclass

from PIL import Image, ImageOps
from pypdf import PdfReader
from pypdf.generic import ArrayObject, DictionaryObject, IndirectObject, NameObject

JPEG, PNG, PDF = 'image/jpeg', 'image/png', 'application/pdf'
EXTENSIONS = {JPEG: 'jpg', PNG: 'png', PDF: 'pdf'}

# Refusal codes the app turns into words
TYPE = 'bill_type'
TOO_LARGE = 'bill_too_large'
UNSAFE = 'bill_unsafe'
CORRUPT = 'bill_corrupt'

# A phone camera makes about 12 to 50 million pixels; a bomb claims billions
MAX_PIXELS = 60_000_000
# Long edge a stored photo keeps: small print on a till roll still reads
LONG_EDGE = 3000
JPEG_QUALITY = 85
MAX_PAGES = 100
# Bytes allowed after the last %%EOF: a newline or two, never a second file
TRAILING_SLACK = 1024
# Objects read before giving up on a PDF built to keep the server busy
MAX_PDF_OBJECTS = 200_000

# Keys and action types that make a PDF do something when opened or clicked
ACTIVE_KEYS = {
    '/JavaScript', '/JS', '/Launch', '/EmbeddedFile', '/EmbeddedFiles', '/RichMedia', '/XFA', '/AA',
    '/SubmitForm', '/ImportData', '/GoToE', '/GoToR', '/Movie', '/Sound',
}
ACTIVE_ACTIONS = {
    '/JavaScript', '/Launch', '/SubmitForm', '/ImportData', '/GoToE', '/GoToR', '/RichMediaExecute',
    '/Movie', '/Sound', '/Rendition',
}


class BillRejected(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class InspectedBill:
    content: bytes
    content_type: str
    page_count: int | None = None
    width: int | None = None
    height: int | None = None

    @property
    def extension(self):
        return EXTENSIONS[self.content_type]

    @property
    def size(self):
        return len(self.content)


def read_upload(upload, max_bytes):
    """The whole file, refusing one larger than `max_bytes` before reading more than that."""
    if upload.size is not None and upload.size > max_bytes:
        raise BillRejected(TOO_LARGE, 'This file is larger than your plan allows for one bill.')
    upload.seek(0)
    data = upload.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise BillRejected(TOO_LARGE, 'This file is larger than your plan allows for one bill.')
    if not data:
        raise BillRejected(CORRUPT, 'This file is empty.')
    return data


def sniff(data):
    """The type the first bytes prove, or None."""
    if data.startswith(b'\xff\xd8\xff'):
        return JPEG
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return PNG
    if data.startswith(b'%PDF-'):
        return PDF
    return None


def inspect(data):
    """
    The bill as it will be stored. Raises BillRejected for anything that is
    not a whole, harmless JPEG, PNG or PDF.
    """
    kind = sniff(data)
    if kind is None:
        raise BillRejected(TYPE, 'Only photos (JPEG or PNG) and PDFs can be attached.')
    if kind == PDF:
        return _pdf(data)
    return _photo(data, kind)


def _photo(data, kind):
    expected = {'JPEG', 'MPO'} if kind == JPEG else {'PNG'}  # MPO: a JPEG some cameras write
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        try:
            with Image.open(io.BytesIO(data)) as probe:
                if probe.format not in expected:
                    raise BillRejected(TYPE, 'This file is not the photo it says it is.')
                # Read from the header, before a single pixel is decoded
                width, height = probe.size
                if width * height > MAX_PIXELS:
                    raise BillRejected(UNSAFE, 'This photo is too large to open safely.')
                probe.verify()
            with Image.open(io.BytesIO(data)) as image:
                image.load()
                photo = ImageOps.exif_transpose(image)
                photo.thumbnail((LONG_EDGE, LONG_EDGE))
                return _encode(photo)
        except BillRejected:
            raise
        except (Image.DecompressionBombError, Image.DecompressionBombWarning):
            raise BillRejected(UNSAFE, 'This photo is too large to open safely.')
        except Exception:
            raise BillRejected(CORRUPT, 'This photo is damaged and cannot be opened.')


def _encode(photo):
    """A fresh file holding only the pixels. PNG only when it has see-through parts."""
    out = io.BytesIO()
    transparent = photo.mode in ('RGBA', 'LA') or (photo.mode == 'P' and 'transparency' in photo.info)
    # Pillow writes some of what it read back out (a JPEG's comments, say): keep nothing but the pixels
    photo = photo.copy()
    photo.info = {key: value for key, value in photo.info.items() if key == 'transparency'}
    if transparent:
        photo.save(out, 'PNG', optimize=True)
        content_type = PNG
    else:
        if photo.mode not in ('RGB', 'L'):
            photo = photo.convert('RGB')
        photo.save(out, 'JPEG', quality=JPEG_QUALITY, optimize=True, progressive=True)
        content_type = JPEG
    return InspectedBill(out.getvalue(), content_type, width=photo.width, height=photo.height)


def _pdf(data):
    end = data.rfind(b'%%EOF')
    if end < 0:
        raise BillRejected(CORRUPT, 'This PDF is cut short and cannot be opened.')
    if len(data[end + 5:].strip()) > TRAILING_SLACK:
        raise BillRejected(UNSAFE, 'This PDF has another file hidden in it.')
    try:
        reader = PdfReader(io.BytesIO(data), strict=True)
        if reader.is_encrypted:
            raise BillRejected(UNSAFE, 'This PDF is locked with a password. Attach one without it.')
        pages = len(reader.pages)
        if not pages:
            raise BillRejected(CORRUPT, 'This PDF has no pages.')
        if pages > MAX_PAGES:
            raise BillRejected(TOO_LARGE, f'A PDF bill may have up to {MAX_PAGES} pages.')
        _refuse_active_content(reader)
        first = reader.pages[0].mediabox
        width, height = round(float(first.width)), round(float(first.height))
    except BillRejected:
        raise
    except Exception:
        raise BillRejected(CORRUPT, 'This PDF is damaged and cannot be opened.')
    return InspectedBill(data, PDF, page_count=pages, width=width, height=height)


def _refuse_active_content(reader):
    """
    Look at every object: the ones reachable from the trailer, and the ones
    listed in the cross-reference table that nothing points at, which some
    readers still run.
    """
    seen = set()
    pending = [reader.trailer]
    for generation, numbers in reader.xref.items():
        pending.extend(IndirectObject(number, generation, reader) for number in numbers)
    pending.extend(IndirectObject(number, 0, reader) for number in reader.xref_objStm)

    read = 0
    while pending:
        value = pending.pop()
        if isinstance(value, IndirectObject):
            key = (value.idnum, value.generation)
            if key in seen:
                continue
            seen.add(key)
            value = value.get_object()
        read += 1
        if read > MAX_PDF_OBJECTS:
            raise BillRejected(UNSAFE, 'This PDF is too complex to check.')
        if isinstance(value, DictionaryObject):
            for name, item in value.items():
                if name in ACTIVE_KEYS:
                    raise BillRejected(UNSAFE, 'This PDF contains scripts or attachments, so it cannot be kept.')
                if name == '/S' and isinstance(item, NameObject) and item in ACTIVE_ACTIONS:
                    raise BillRejected(UNSAFE, 'This PDF contains scripts or attachments, so it cannot be kept.')
                pending.append(item)
        elif isinstance(value, ArrayObject):
            pending.extend(value)
