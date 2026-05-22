"""
Gerador de DOCX — produz o documento Word final a partir do ArtigoGerado.
Suporta formato Vancouver (padrão) e estrutura para TCC (ABNT em breve).
"""
import os
import asyncio
from pathlib import Path
from docx import Document
from docx.shared import Inches, Pt, Cm, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.style import WD_STYLE_TYPE
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

from app.config import settings
from app.models.schemas import ArtigoGerado, CKO


def _set_margins(doc: Document, top=3, bottom=2, left=3, right=2):
    for section in doc.sections:
        section.top_margin = Cm(top)
        section.bottom_margin = Cm(bottom)
        section.left_margin = Cm(left)
        section.right_margin = Cm(right)


def _add_page_number(doc: Document):
    section = doc.sections[0]
    footer = section.footer
    para = footer.paragraphs[0]
    para.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    run = para.add_run()
    fldChar = OxmlElement("w:fldChar")
    fldChar.set(qn("w:fldCharType"), "begin")
    instrText = OxmlElement("w:instrText")
    instrText.text = "PAGE"
    fldChar2 = OxmlElement("w:fldChar")
    fldChar2.set(qn("w:fldCharType"), "end")
    run._r.append(fldChar)
    run._r.append(instrText)
    run._r.append(fldChar2)
    run.font.size = Pt(10)


def _configurar_estilos(doc: Document):
    styles = doc.styles

    # Normal
    normal = styles["Normal"]
    normal.font.name = "Times New Roman"
    normal.font.size = Pt(12)
    normal.paragraph_format.space_after = Pt(0)
    normal.paragraph_format.space_before = Pt(0)
    normal.paragraph_format.line_spacing = Pt(18)  # 1.5
    normal.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY

    # Heading 1 — seções principais
    h1 = styles["Heading 1"]
    h1.font.name = "Times New Roman"
    h1.font.size = Pt(12)
    h1.font.bold = True
    h1.font.color.rgb = RGBColor(0, 0, 0)
    h1.paragraph_format.space_before = Pt(12)
    h1.paragraph_format.space_after = Pt(6)
    h1.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.LEFT

    # Heading 2 — subseções
    h2 = styles["Heading 2"]
    h2.font.name = "Times New Roman"
    h2.font.size = Pt(12)
    h2.font.bold = False
    h2.font.italic = True
    h2.font.color.rgb = RGBColor(0, 0, 0)
    h2.paragraph_format.space_before = Pt(6)
    h2.paragraph_format.space_after = Pt(3)


def _add_heading(doc: Document, text: str, level: int = 1):
    p = doc.add_heading(text.upper() if level == 1 else text, level=level)
    return p


def _add_paragraph(doc: Document, text: str, first_line_indent: bool = True):
    p = doc.add_paragraph(text)
    p.style = doc.styles["Normal"]
    p.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    p.paragraph_format.line_spacing = Pt(18)
    if first_line_indent:
        p.paragraph_format.first_line_indent = Cm(1.25)
    return p


def _add_title_page(doc: Document, artigo: ArtigoGerado, cko: CKO):
    # Título
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run(artigo.titulo)
    run.font.name = "Times New Roman"
    run.font.size = Pt(14)
    run.font.bold = True
    p.paragraph_format.space_after = Pt(18)

    # Palavras-chave
    doc.add_paragraph()
    kw_para = doc.add_paragraph()
    kw_para.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    kw_run = kw_para.add_run("Palavras-chave: ")
    kw_run.font.bold = True
    kw_run.font.size = Pt(12)
    kw_run.font.name = "Times New Roman"
    kw_val = kw_para.add_run("; ".join(artigo.palavras_chave) + ".")
    kw_val.font.size = Pt(12)
    kw_val.font.name = "Times New Roman"

    doc.add_paragraph()


def _add_resumo(doc: Document, artigo: ArtigoGerado):
    _add_heading(doc, "Resumo")
    res = artigo.resumo

    for label, texto in [
        ("Introdução", res.introducao),
        ("Apresentação do Caso", res.caso),
        ("Discussão", res.discussao),
        ("Conclusão", res.conclusao),
    ]:
        p = doc.add_paragraph()
        p.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        p.paragraph_format.line_spacing = Pt(12)  # simples
        bold_run = p.add_run(f"{label}: ")
        bold_run.font.bold = True
        bold_run.font.name = "Times New Roman"
        bold_run.font.size = Pt(12)
        text_run = p.add_run(texto)
        text_run.font.name = "Times New Roman"
        text_run.font.size = Pt(12)

    doc.add_paragraph()


def _add_secao(doc: Document, titulo: str, paragrafos: list[str]):
    _add_heading(doc, titulo)
    for texto in paragrafos:
        _add_paragraph(doc, texto)
    doc.add_paragraph()


def _add_referencias(doc: Document, artigo: ArtigoGerado):
    _add_heading(doc, "Referências")
    for ref in artigo.referencias:
        p = doc.add_paragraph()
        p.style = doc.styles["Normal"]
        p.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        p.paragraph_format.line_spacing = Pt(12)  # simples
        p.paragraph_format.space_after = Pt(6)
        p.paragraph_format.first_line_indent = Cm(0)
        p.paragraph_format.left_indent = Cm(0)
        run = p.add_run(f"{ref.numero}. {ref.formatada}")
        run.font.name = "Times New Roman"
        run.font.size = Pt(12)


def _add_disclaimer(doc: Document):
    doc.add_page_break()
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_before = Pt(12)
    run = p.add_run(
        "AVISO: Este documento foi gerado com auxílio de inteligência artificial (RCCS v1.0). "
        "O autor é integralmente responsável pela revisão do conteúdo antes de qualquer submissão. "
        "A plataforma não garante aceitação em periódicos científicos."
    )
    run.font.name = "Times New Roman"
    run.font.size = Pt(10)
    run.font.italic = True
    run.font.color.rgb = RGBColor(128, 128, 128)


def _add_imagens(doc: Document, cko: CKO, sessao_id: str):
    """Insert figures after Caso Clínico with CARE-compliant captions."""
    if not cko.imagens:
        return

    imagens_sorted = sorted(cko.imagens, key=lambda x: x.numero_figura)
    images_dir = Path("images_output") / sessao_id

    for img in imagens_sorted:
        if not img.filename:
            continue
        img_path = images_dir / img.filename
        if not img_path.exists():
            continue

        try:
            # Insert image centered
            p_img = doc.add_paragraph()
            p_img.alignment = WD_ALIGN_PARAGRAPH.CENTER
            run = p_img.add_run()
            run.add_picture(str(img_path), width=Inches(5.0))

            # CARE caption: "Figura X – legenda."
            p_cap = doc.add_paragraph()
            p_cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
            p_cap.paragraph_format.space_after = Pt(12)
            run_cap = p_cap.add_run(f"Figura {img.numero_figura} \u2013 {img.legenda}")
            run_cap.font.name = "Times New Roman"
            run_cap.font.size = Pt(11)
            run_cap.font.italic = True
        except Exception as exc:
            # A missing or corrupt image must not break the whole document
            import logging
            logging.getLogger(__name__).warning(
                "Falha ao inserir figura %s: %s", img.filename, exc
            )


async def gerar_docx(sessao_id: str, artigo: ArtigoGerado, cko: CKO) -> str:
    output_dir = Path(settings.docx_output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    filepath = output_dir / f"{sessao_id}.docx"

    # Run synchronous python-docx in executor to avoid blocking
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _build_docx, filepath, artigo, cko, sessao_id)

    return str(filepath)


def _build_docx(filepath: Path, artigo: ArtigoGerado, cko: CKO, sessao_id: str):
    doc = Document()
    _set_margins(doc)
    _configurar_estilos(doc)
    _add_page_number(doc)

    _add_title_page(doc, artigo, cko)
    _add_resumo(doc, artigo)
    _add_secao(doc, "Introdução", artigo.introducao)
    _add_secao(doc, "Apresentação do Caso", artigo.caso_clinico)
    _add_imagens(doc, cko, sessao_id)  # figures after case presentation
    _add_secao(doc, "Discussão", artigo.discussao)
    _add_secao(doc, "Conclusão", artigo.conclusao)
    _add_referencias(doc, artigo)
    _add_disclaimer(doc)

    doc.save(str(filepath))
