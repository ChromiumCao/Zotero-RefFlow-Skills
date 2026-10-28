"""Optional PDF text readers; never install packages or infer OCR text."""
from __future__ import annotations

import importlib
from pathlib import Path
import shutil
import subprocess
import warnings


def read_pages(path, pages=None):
    path = Path(path)
    with path.open("rb") as handle:
        if not handle.read(8).startswith(b"%PDF-"):
            raise ValueError("Not a PDF file")
    wanted = list(dict.fromkeys(pages)) if pages is not None else None
    if wanted is not None and (not wanted or any(type(n) is not int or n < 1 for n in wanted)):
        raise ValueError("Positive PDF file page indexes required")
    tool = shutil.which("pdftotext")
    if tool:
        result = []
        for page in wanted or [None]:
            args = [tool, "-layout", "-enc", "UTF-8"]
            if page is not None:
                args += ["-f", str(page), "-l", str(page)]
            proc = subprocess.run(args + [str(path), "-"], text=True, encoding="utf-8",
                                  capture_output=True, timeout=60, check=False)
            if proc.returncode:
                raise ValueError("PDF extraction failed (encrypted or damaged PDF): " + proc.stderr[:300])
            texts = proc.stdout.split("\f")
            if texts and not texts[-1].strip():
                texts.pop()
            result.extend((page or index, text) for index, text in enumerate(texts, 1))
    else:
        reader_class = None
        for module in ("pypdf", "PyPDF2"):
            try:
                reader_class = importlib.import_module(module).PdfReader
                break
            except ImportError:
                continue
        if reader_class is None:
            raise RuntimeError("PDF reader unavailable: use pdftotext, an already installed pypdf/PyPDF2, or verified text export")
        try:
            reader = reader_class(str(path))
            if reader.is_encrypted and not reader.decrypt(""):
                raise ValueError("Encrypted PDF requires an authorized decrypted copy")
            indices = wanted or list(range(1, len(reader.pages) + 1))
            if any(n > len(reader.pages) for n in indices):
                raise ValueError("Requested PDF page is outside the document")
            with warnings.catch_warnings(record=True) as messages:
                warnings.simplefilter("always")
                result = [(n, reader.pages[n - 1].extract_text() or "") for n in indices]
            if any("not implemented" in str(message.message).lower() for message in messages):
                raise ValueError("PDF reader cannot decode this font; use compatible pdftotext/pypdf or verified text export")
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("PDF text extraction failed: " + str(exc)) from exc
    if not result or any(not text.strip() for _, text in result):
        raise ValueError("PDF contains a page without extractable text; resolve scans/OCR before importing or pruning")
    if any(len(text) > 100000 for _, text in result):
        raise ValueError("PDF page exceeds extraction limit; select narrower evidence")
    if any(any(ord(char) < 32 and char not in "\t\r\n" for char in text) for _, text in result):
        raise ValueError("PDF reader returned control characters/undecoded text; use a compatible reader or verified text export")
    return result
