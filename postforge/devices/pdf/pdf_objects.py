# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

"""
Native PDF object types and writer.

Provides typed wrappers for PDF primitives, object numbering, and complete
file serialization (header, xref table, trailer).
"""

import re


# ---------------------------------------------------------------------------
# PDF primitive types
# ---------------------------------------------------------------------------

class PdfName:
    """PDF name object, e.g. /Type, /Font."""

    __slots__ = ('_name',)

    # Escape bytes outside printable ASCII (0x21-0x7E), '#', and PDF
    # delimiter characters: ( ) < > [ ] { } / %
    _NEEDS_ESCAPE = re.compile(rb'[^!-~]|[#()/<>\[\]{}%]')

    def __init__(self, name: str) -> None:
        # Store with leading slash: '/Type'
        self._name = name

    def serialize(self) -> bytes:
        # The leading '/' is the PDF name introducer — only escape the body
        body = self._name[1:].encode('latin-1')
        def _escape(m: re.Match) -> bytes:
            return b'#%02X' % m.group(0)[0]
        return b'/' + self._NEEDS_ESCAPE.sub(_escape, body)

    def __repr__(self) -> str:
        return f'PdfName({self._name!r})'


class PdfNumber:
    """PDF numeric object (integer or real)."""

    __slots__ = ('_value',)

    def __init__(self, value: int | float) -> None:
        self._value = value

    def serialize(self) -> bytes:
        v = self._value
        if isinstance(v, int):
            return str(v).encode('ascii')
        # Use integer form when the value is whole
        iv = int(v)
        if v == iv:
            return str(iv).encode('ascii')
        # Round to 6 decimal places for floats
        return f'{v:.6f}'.rstrip('0').rstrip('.').encode('ascii')

    def __repr__(self) -> str:
        return f'PdfNumber({self._value!r})'


class PdfBool:
    """PDF boolean object."""

    __slots__ = ('_value',)

    def __init__(self, value: bool) -> None:
        self._value = value

    def serialize(self) -> bytes:
        return b'true' if self._value else b'false'

    def __repr__(self) -> str:
        return f'PdfBool({self._value!r})'


class PdfString:
    """PDF string object — literal `(text)` or hex `<hex>` form."""

    __slots__ = ('_value',)

    def __init__(self, value: str | bytes) -> None:
        self._value = value

    def serialize(self) -> bytes:
        v = self._value
        if isinstance(v, str):
            raw = v.encode('latin-1', errors='replace')
        else:
            raw = v
        # Use literal form if all bytes are safe (printable ASCII, no
        # unbalanced parens).  Otherwise use hex form.
        if self._is_safe_literal(raw):
            escaped = (raw.replace(b'\\', b'\\\\')
                          .replace(b'(', b'\\(')
                          .replace(b')', b'\\)'))
            return b'(' + escaped + b')'
        return b'<' + raw.hex().encode('ascii') + b'>'

    @staticmethod
    def _is_safe_literal(data: bytes) -> bool:
        for b in data:
            if b < 0x20 and b not in (0x09, 0x0A, 0x0D):
                return False
        return True

    def __repr__(self) -> str:
        return f'PdfString({self._value!r})'


class PdfRef:
    """Indirect reference to a PDF object: `N 0 R`."""

    __slots__ = ('obj_num',)

    def __init__(self, obj_num: int) -> None:
        self.obj_num = obj_num

    def serialize(self) -> bytes:
        return f'{self.obj_num} 0 R'.encode('ascii')

    def __repr__(self) -> str:
        return f'PdfRef({self.obj_num})'


class PdfArray:
    """PDF array object: `[item1 item2 ...]`."""

    __slots__ = ('_items',)

    def __init__(self, items: list | None = None) -> None:
        self._items: list = items if items is not None else []

    def append(self, item: object) -> None:
        self._items.append(item)

    def __len__(self) -> int:
        return len(self._items)

    def __bool__(self) -> bool:
        return len(self._items) > 0

    def serialize(self) -> bytes:
        parts = [_serialize(item) for item in self._items]
        return b'[' + b' '.join(parts) + b']'

    def __repr__(self) -> str:
        return f'PdfArray({self._items!r})'


class PdfDict:
    """PDF dictionary object: `<< /Key value ... >>`."""

    __slots__ = ('_entries',)

    def __init__(self) -> None:
        self._entries: dict[str, object] = {}

    def __setitem__(self, key: str, value: object) -> None:
        self._entries[key] = value

    def __getitem__(self, key: str) -> object:
        return self._entries[key]

    def __contains__(self, key: str) -> bool:
        return key in self._entries

    def __len__(self) -> int:
        return len(self._entries)

    def __bool__(self) -> bool:
        return len(self._entries) > 0

    def serialize(self) -> bytes:
        parts = [b'<<']
        for key, value in self._entries.items():
            # Keys are stored as strings like '/Type'
            key_obj = PdfName(key)
            parts.append(key_obj.serialize() + b' ' + _serialize(value))
        parts.append(b'>>')
        return b'\n'.join(parts)

    def __repr__(self) -> str:
        return f'PdfDict({self._entries!r})'


class PdfStream:
    """PDF stream object: dictionary + binary data.

    /Length is computed automatically at serialization — never set manually.
    """

    __slots__ = ('_dict', '_data')

    def __init__(self, data: bytes) -> None:
        self._dict = PdfDict()
        self._data = data

    def __setitem__(self, key: str, value: object) -> None:
        self._dict[key] = value

    def __contains__(self, key: str) -> bool:
        return key in self._dict

    def serialize(self) -> bytes:
        # Set /Length automatically
        self._dict['/Length'] = PdfNumber(len(self._data))
        dict_bytes = self._dict.serialize()
        return dict_bytes + b'\nstream\n' + self._data + b'\nendstream'

    def __repr__(self) -> str:
        return f'PdfStream({len(self._data)} bytes)'


# ---------------------------------------------------------------------------
# Serialization helper
# ---------------------------------------------------------------------------

def _serialize(obj: object) -> bytes:
    """Serialize any PDF object to bytes."""
    if hasattr(obj, 'serialize'):
        return obj.serialize()
    # Fallback for raw Python types
    if isinstance(obj, bool):
        return b'true' if obj else b'false'
    if isinstance(obj, int):
        return str(obj).encode('ascii')
    if isinstance(obj, float):
        return PdfNumber(obj).serialize()
    if isinstance(obj, str):
        return PdfString(obj).serialize()
    if isinstance(obj, bytes):
        return b'<' + obj.hex().encode('ascii') + b'>'
    raise TypeError(f'Cannot serialize {type(obj).__name__} to PDF')


# ---------------------------------------------------------------------------
# PDF Writer
# ---------------------------------------------------------------------------

class PdfWriter:
    """Builds and writes a complete PDF 1.5 file.

    Objects are registered with add_object() and pages with add_page().
    The write() method serializes the complete file with header, body,
    xref table, and trailer.
    """

    def __init__(self) -> None:
        self._objects: list[object] = []  # indexed by obj_num - 1
        self._pages: list[PdfDict] = []

    def add_object(self, obj: object) -> PdfRef:
        """Register a PDF object and return its indirect reference."""
        self._objects.append(obj)
        return PdfRef(len(self._objects))  # 1-based

    def add_page(self, width: float, height: float) -> PdfDict:
        """Create a page dict with MediaBox and return it.

        The page is also registered as an indirect object.
        """
        page = PdfDict()
        page['/Type'] = PdfName('/Page')
        page['/MediaBox'] = PdfArray([
            PdfNumber(0), PdfNumber(0),
            PdfNumber(width), PdfNumber(height),
        ])
        self._objects.append(page)
        self._pages.append(page)
        return page

    def write(self, f: object) -> None:
        """Serialize the complete PDF to a file-like object.

        Object ordering: all user objects first, then Pages tree, then
        Catalog. This ensures page dicts are never the final object in
        the file, fixing poppler text-selection bugs.
        """
        # Build Pages tree node
        pages_node = PdfDict()
        pages_node['/Type'] = PdfName('/Pages')
        page_refs: list[PdfRef] = []
        for page in self._pages:
            # Find this page's obj_num
            idx = self._objects.index(page)
            page_refs.append(PdfRef(idx + 1))
        pages_node['/Kids'] = PdfArray(page_refs)
        pages_node['/Count'] = PdfNumber(len(self._pages))
        self._objects.append(pages_node)
        pages_ref = PdfRef(len(self._objects))

        # Patch /Parent on all page dicts
        for page in self._pages:
            page['/Parent'] = pages_ref

        # Build Catalog
        catalog = PdfDict()
        catalog['/Type'] = PdfName('/Catalog')
        catalog['/Pages'] = pages_ref
        self._objects.append(catalog)
        catalog_ref = PdfRef(len(self._objects))

        # Write header
        f.write(b'%PDF-1.5\n')
        f.write(b'%\xe2\xe3\xcf\xd3\n')  # binary comment

        # Write all objects, recording byte offsets
        offsets: list[int] = []
        for i, obj in enumerate(self._objects):
            offsets.append(f.tell())
            obj_num = i + 1
            f.write(f'{obj_num} 0 obj\n'.encode('ascii'))
            f.write(_serialize(obj))
            f.write(b'\nendobj\n')

        # Write xref table
        xref_offset = f.tell()
        n_objects = len(self._objects) + 1  # +1 for free entry 0
        f.write(b'xref\n')
        f.write(f'0 {n_objects}\n'.encode('ascii'))
        f.write(b'0000000000 65535 f \n')
        for offset in offsets:
            f.write(f'{offset:010d} 00000 n \n'.encode('ascii'))

        # Write trailer
        f.write(b'trailer\n')
        trailer = PdfDict()
        trailer['/Size'] = PdfNumber(n_objects)
        trailer['/Root'] = catalog_ref
        f.write(trailer.serialize())
        f.write(b'\nstartxref\n')
        f.write(f'{xref_offset}\n'.encode('ascii'))
        f.write(b'%%EOF\n')
