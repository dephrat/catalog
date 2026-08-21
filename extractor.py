import io

# Safety bound only — keeps a pathological document out of memory.
# The prompt-facing limit is tagger.ATTACHMENT_CAP, applied by the caller,
# so truncation is reported accurately instead of being silently pre-applied.
EXTRACT_LIMIT = 50000

def extract_text(name, content_type, raw):
    """Extract text from decoded attachment bytes.

    Providers hand over already-decoded bytes, so this stays free of any
    provider-specific encoding (Graph uses base64, Gmail base64url).
    Returns the extracted text, or None.
    """
    if not raw:
        return None

    name = (name or "").lower()
    content_type = (content_type or "").lower()

    # PDF
    if "pdf" in content_type or name.endswith(".pdf"):
        return extract_pdf(raw)

    # Word
    if "wordprocessingml" in content_type or name.endswith(".docx"):
        return extract_docx(raw)

    return None

def extract_pdf(raw):
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(raw))
        text = ""
        for page in reader.pages:
            text += page.extract_text() or ""
        return text[:EXTRACT_LIMIT].strip() or None
    except Exception as e:
        print(f"PDF extraction error: {e}")
        return None

def extract_docx(raw):
    try:
        from docx import Document
        doc = Document(io.BytesIO(raw))
        text = "\n".join([p.text for p in doc.paragraphs if p.text.strip()])
        return text[:EXTRACT_LIMIT].strip() or None
    except Exception as e:
        print(f"DOCX extraction error: {e}")
        return None