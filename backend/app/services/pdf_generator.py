"""
Conversor DOCX → PDF usando PyMuPDF (fitz).

PyMuPDF >= 1.22 suporta abertura de DOCX direto da memória
e conversão para PDF sem necessidade de LibreOffice ou dependências externas.
"""
import io
import logging

_log = logging.getLogger(__name__)


def docx_to_pdf_bytes(docx_bytes: bytes) -> bytes:
    """
    Recebe bytes de um arquivo .docx e retorna bytes de um .pdf.
    Lança ValueError se a conversão falhar.
    """
    try:
        import fitz  # PyMuPDF
    except ImportError as e:
        raise ValueError("PyMuPDF não instalado — pip install pymupdf") from e

    try:
        doc = fitz.open(stream=docx_bytes, filetype="docx")
        pdf_bytes: bytes = doc.convert_to_pdf()
        doc.close()
        _log.debug("DOCX→PDF: %d bytes → %d bytes", len(docx_bytes), len(pdf_bytes))
        return pdf_bytes
    except Exception as exc:
        raise ValueError(f"Falha ao converter DOCX para PDF: {exc}") from exc
