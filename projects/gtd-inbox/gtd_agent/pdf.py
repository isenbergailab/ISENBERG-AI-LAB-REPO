"""Text from PDFs, read locally with pypdf, the one package the agent uses beyond the standard library.

pypdf is optional: without it PDFs are not read, and doctor and each proposal say how to install it. The limits
keep one odd file from stalling the agent: size, pages and characters.
"""
from __future__ import annotations

import io

MAX_BYTES = 20 * 1024 * 1024
MAX_PAGES = 50
INSTALL = "py -m pip install pypdf"


class PdfError(Exception):
    """Why a PDF was not read, short enough for a proposal or TODAY."""


def version() -> str | None:
    """pypdf's version, or None when it is not installed."""
    try:
        import pypdf
    except ImportError:
        return None
    return str(getattr(pypdf, "__version__", "?"))


def is_pdf(filename: str, content_type: str = "") -> bool:
    return content_type.lower() == "application/pdf" or filename.lower().endswith(".pdf")


def pdf_text(data: bytes, max_chars: int) -> str:
    """The text of a PDF's first MAX_PAGES pages, cut at max_chars. Raises PdfError."""
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise PdfError(f"pypdf is not installed ({INSTALL})") from exc
    if len(data) > MAX_BYTES:
        raise PdfError(f"it is over {MAX_BYTES // (1024 * 1024)} MB")
    parts: list[str] = []
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise PdfError("it is password-protected")
        size = 0
        for index in range(min(len(reader.pages), MAX_PAGES)):
            text = reader.pages[index].extract_text() or ""
            parts.append(text)
            size += len(text)
            if size >= max_chars:
                break
    except PdfError:
        raise
    except Exception as exc:  # noqa: BLE001 - pypdf raises many kinds of errors on broken files
        raise PdfError(f"it could not be read ({type(exc).__name__})") from exc
    text = "\n".join(parts).replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        raise PdfError("it has no text, maybe a scan")
    return text[:max_chars]
