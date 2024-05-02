from itertools import repeat
from typing import List, Optional, Dict

import ocrmypdf
import pypdfium2 as pdfium
import io
from concurrent.futures import ThreadPoolExecutor

from surya.ocr import run_recognition

from marker.ocr.heuristics import should_ocr_page, no_text_found, detect_bad_ocr
from marker.pdf.images import render_image
from marker.schema.page import Page
from marker.schema.block import Block, Line, Span
from marker.settings import settings
from marker.pdf.extract_text import get_text_blocks


def run_ocr(doc, pages: List[Page], langs: List[str], rec_model, parallel_factor) -> (List[Page], Dict):
    ocr_pages = 0
    ocr_success = 0
    ocr_failed = 0
    no_text = no_text_found(pages)
    ocr_idxs = []
    for pnum, page in enumerate(pages):
        ocr_needed = should_ocr_page(page, no_text)
        if ocr_needed:
            ocr_idxs.append(pnum)
            ocr_pages += 1

    ocr_method = settings.OCR_ENGINE_INTERNAL
    if ocr_method == "surya":
        new_pages = surya_recognition(doc, ocr_idxs, langs, rec_model, pages)
    else:
        new_pages = tesseract_recognition(doc, ocr_idxs, langs)

    for orig_idx, page in zip(ocr_idxs, new_pages):
        if detect_bad_ocr(page.prelim_text) or len(page.prelim_text) == 0:
            ocr_failed += 1
        else:
            ocr_success += 1
            pages[orig_idx] = page

    return pages, {"ocr_pages": ocr_pages, "ocr_failed": ocr_failed, "ocr_success": ocr_success}


def surya_recognition(doc, page_idxs, langs: List[str], rec_model, pages: List[Page]) -> List[Optional[Page]]:
    images = [render_image(doc[pnum], dpi=settings.SURYA_OCR_DPI) for pnum in page_idxs]
    processor = rec_model.processor
    selected_pages = [p for i, p in enumerate(pages) if i in page_idxs]

    surya_langs = [langs] * len(page_idxs)
    detection_results = [p.text_lines.bboxes for p in selected_pages]
    polygons = [[b.polygon for b in bboxes] for bboxes in detection_results]

    results = run_recognition(images, surya_langs, rec_model, processor, polygons=polygons)

    new_pages = []
    for (page_idx, result, old_page) in zip(page_idxs, results, selected_pages):
        text_lines = old_page.text_lines
        ocr_results = result.text_lines
        blocks = []
        for i, line in enumerate(ocr_results):
            block = Block(
                bbox=line.bbox,
                pnum=page_idx,
                lines=[Line(
                    bbox=line.bbox,
                    spans=[Span(
                        text=line.text,
                        bbox=line.bbox,
                        span_id=f"{page_idx}_{i}",
                        font="",
                        font_weight=0,
                        font_size=0,
                    )
                    ]
                )]
            )
            blocks.append(block)
        page = Page(
            blocks=blocks,
            pnum=page_idx,
            bbox=result.image_bbox,
            rotation=0,
            text_lines=text_lines,
            ocr_method="surya"
        )
        new_pages.append(page)
    return new_pages


def tesseract_recognition(doc, page_idxs, langs: List[str]) -> List[Optional[Page]]:
    pdf_pages = generate_single_page_pdfs(doc, page_idxs)
    with ThreadPoolExecutor(max_workers=settings.OCR_PARALLEL_WORKERS) as executor:
        pages = list(executor.map(_tesseract_recognition, pdf_pages, repeat(langs, len(pdf_pages))))

    return pages


def generate_single_page_pdfs(doc, page_idxs) -> List[io.BytesIO]:
    pdf_pages = []
    for page_idx in page_idxs:
        blank_doc = pdfium.PdfDocument.new()
        blank_doc.import_pages(doc, pages=[page_idx])
        assert len(blank_doc) == 1, "Failed to import page"

        in_pdf = io.BytesIO()
        blank_doc.save(in_pdf)
        in_pdf.seek(0)
        pdf_pages.append(in_pdf)
    return pdf_pages


def _tesseract_recognition(in_pdf, langs: List[str]) -> Optional[Page]:
    out_pdf = io.BytesIO()

    ocrmypdf.ocr(
        in_pdf,
        out_pdf,
        language=langs[0],
        output_type="pdf",
        redo_ocr=None if settings.OCR_ALL_PAGES else True,
        force_ocr=True if settings.OCR_ALL_PAGES else None,
        progress_bar=False,
        optimize=False,
        fast_web_view=1e6,
        skip_big=15,  # skip images larger than 15 megapixels
        tesseract_timeout=settings.TESSERACT_TIMEOUT,
        tesseract_non_ocr_timeout=settings.TESSERACT_TIMEOUT,
    )

    new_doc = pdfium.PdfDocument(out_pdf.getvalue())

    blocks, _ = get_text_blocks(new_doc, max_pages=1)
    page = blocks[0]
    page.ocr_method = "tesseract"
    return page