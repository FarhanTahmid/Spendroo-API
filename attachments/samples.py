"""
Real and hostile files for the attachment tests: photos and PDFs made on the spot,
so no binary fixtures live in the repository.
"""
import io
import random
import struct
import zlib

from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image
from pypdf import PdfWriter
from pypdf.generic import ArrayObject, DictionaryObject, NameObject, NumberObject, TextStringObject

MB = 1024 * 1024


def upload(data, name='bill.jpg', content_type='image/jpeg'):
    return SimpleUploadedFile(name, data, content_type=content_type)


def jpeg(width=400, height=300, *, size=None, gps=False, color=(200, 180, 150)):
    """A real JPEG. `size` pads it with comment segments to exactly that many bytes."""
    image = Image.new('RGB', (width, height), color)
    out = io.BytesIO()
    exif = None
    if gps:
        exif = Image.Exif()
        exif[0x8825] = {1: 'N', 2: (23.0, 48.0, 0.0), 3: 'E', 4: (90.0, 24.0, 0.0)}
        exif[0x010F] = 'PhoneMaker'
    image.save(out, 'JPEG', quality=90, **({'exif': exif} if exif is not None else {}))
    data = out.getvalue()
    return data if size is None else _pad_jpeg(data, size)


def noisy_jpeg(width=1200, height=900):
    """A photo that does not compress away, as a camera's does."""
    rng = random.Random(7)
    image = Image.frombytes('RGB', (width, height), bytes(rng.getrandbits(8) for _ in range(width * height * 3)))
    out = io.BytesIO()
    image.save(out, 'JPEG', quality=95)
    return out.getvalue()


def _pad_jpeg(data, size):
    # A comment segment is a 4-byte head and up to 65533 bytes of text
    missing = size - len(data)
    if missing < 0 or 0 < missing < 4:
        raise ValueError('cannot pad the photo to exactly that size')
    segments = []
    while missing > 0:
        chunk = min(missing, 65537)
        if 0 < missing - chunk < 4:  # leave a remainder a whole segment can fill
            chunk -= 4
        segments.append(b'\xff\xfe' + struct.pack('>H', chunk - 2) + b'C' * (chunk - 4))
        missing -= chunk
    return data[:2] + b''.join(segments) + data[2:]


def png(width=40, height=30, *, alpha=False, text=None):
    image = Image.new('RGBA' if alpha else 'RGB', (width, height), (10, 20, 30, 128) if alpha else (10, 20, 30))
    out = io.BytesIO()
    if text is None:
        image.save(out, 'PNG')
    else:
        from PIL.PngImagePlugin import PngInfo
        info = PngInfo()
        info.add_text('Comment', text)
        image.save(out, 'PNG', pnginfo=info)
    return out.getvalue()


def png_bomb(width=100_000, height=100_000):
    """A tiny PNG whose header claims ten billion pixels."""
    data = bytearray(png(1, 1))
    # IHDR data starts at byte 16; its CRC covers the type and data (bytes 12..29)
    data[16:24] = struct.pack('>II', width, height)
    data[29:33] = struct.pack('>I', zlib.crc32(bytes(data[12:29])) & 0xFFFFFFFF)
    return bytes(data)


def pdf(pages=1):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=595, height=842)
    return _write(writer)


def pdf_with_script():
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    writer.add_js('app.alert("hello");')
    return _write(writer)


def pdf_with_open_action():
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    action = DictionaryObject({
        NameObject('/S'): NameObject('/JavaScript'),
        NameObject('/Q'): TextStringObject('app.launchURL("http://example.com")'),
    })
    writer._root_object[NameObject('/OpenAction')] = writer._add_object(action)
    return _write(writer)


def pdf_with_launch():
    writer = PdfWriter()
    page = writer.add_blank_page(width=595, height=842)
    link = DictionaryObject({
        NameObject('/Type'): NameObject('/Annot'),
        NameObject('/Subtype'): NameObject('/Link'),
        NameObject('/Rect'): ArrayObject([NumberObject(0), NumberObject(0), NumberObject(100), NumberObject(100)]),
        NameObject('/A'): DictionaryObject({
            NameObject('/S'): NameObject('/Launch'),
            NameObject('/F'): TextStringObject('cmd.exe'),
        }),
    })
    page[NameObject('/Annots')] = ArrayObject([writer._add_object(link)])
    return _write(writer)


def pdf_with_attachment():
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    writer.add_attachment('invoice.exe', b'MZ' + b'\x00' * 64)
    return _write(writer)


def pdf_encrypted():
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    writer.encrypt('secret')
    return _write(writer)


def pdf_hidden_script():
    """A script object nothing points at, listed only in the cross-reference table."""
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    writer._add_object(DictionaryObject({
        NameObject('/S'): NameObject('/JavaScript'),
        NameObject('/JS'): TextStringObject('app.alert(1)'),
    }))
    return _write(writer)


def _write(writer):
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


ZIP = b'PK\x03\x04' + b'\x14\x00' * 8 + b'payload.html<script>alert(1)</script>' * 80
HTML = b'<!doctype html><html><script>fetch("/steal")</script></html>'
EXE = b'MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00\xff\xff' + b'\x00' * 200
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>'
