import json
from io import BytesIO
from xml.etree import ElementTree
from zipfile import ZipFile

from sqlalchemy import delete, select
from sqlalchemy import text as sql_text

from .models import Chunk, Document
from .storage import Storage


class DocumentError(ValueError):
    pass


def parse_document(name, data):
    extension = name.rsplit(".", 1)[-1].lower()
    if extension in {"txt", "md"}:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise DocumentError("Text documents must use UTF-8 encoding") from exc
        pages = [("section 1", text)]
    elif extension == "pdf":
        from pypdf import PdfReader

        reader = PdfReader(BytesIO(data))
        if reader.is_encrypted:
            raise DocumentError("Encrypted PDFs are not supported")
        if len(reader.pages) > 1000:
            raise DocumentError("PDF exceeds the 1000 page parsing limit")
        pages = [
            (f"page {i + 1}", page.extract_text() or "") for i, page in enumerate(reader.pages)
        ]
    elif extension == "docx":
        with ZipFile(BytesIO(data)) as archive:
            info = archive.getinfo("word/document.xml")
            if (
                info.file_size > 20 * 1024 * 1024
                or sum(i.file_size for i in archive.infolist()) > 100 * 1024 * 1024
            ):
                raise DocumentError("DOCX expanded content is too large")
            root = ElementTree.fromstring(archive.read(info))
        namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        paragraphs = ["".join(p.itertext()) for p in root.iter(namespace + "p")]
        pages = [("section 1", "\n".join(paragraphs))]
    else:
        raise DocumentError("Unsupported document format")
    if not any(text.strip() for _, text in pages):
        raise DocumentError("No extractable text; scanned PDFs require OCR outside this release")
    if sum(len(text) for _, text in pages) > 4_000_000:
        raise DocumentError("Extracted text exceeds the parsing limit")
    return pages


def chunks(pages, size=1600, overlap=200):
    for locator, text in pages:
        text = text.replace("\x00", "")
        for offset in range(0, len(text), size - overlap):
            part = text[offset : offset + size].strip()
            if part:
                yield locator, part


def ingest(document_id, factory, settings, lease_owner=None):
    with factory() as session:
        document = session.get(Document, document_id)
        if document is None or document.status == "ready":
            return
        key, name, user_id = document.storage_key, document.name, document.user_id
    try:
        pages = parse_document(name, Storage(settings).get(key))
        parts = list(chunks(pages))
        from .retrieval import embed_texts

        vectors = embed_texts([text for _, text in parts], settings)
        with factory.begin() as session:
            from .api import serialize

            serialize(session)
            document = session.get(Document, document_id)
            if document is None or document.lease_owner != lease_owner:
                return
            session.execute(delete(Chunk).where(Chunk.document_id == document_id))
            session.add_all(
                Chunk(
                    document_id=document_id,
                    user_id=user_id,
                    ordinal=i,
                    locator=locator,
                    text=text,
                    embedding=vectors[i],
                )
                for i, (locator, text) in enumerate(parts)
            )
            session.flush()
            if session.bind.dialect.name == "postgresql":
                indexed = list(
                    session.scalars(
                        select(Chunk).where(
                            Chunk.document_id == document_id, Chunk.user_id == user_id
                        )
                    )
                )
                for chunk in indexed:
                    if len(chunk.embedding) != 384:
                        raise DocumentError("Embedding dimension must be 384")
                    session.execute(
                        sql_text(
                            "UPDATE chunks SET embedding_vector = CAST(:vector AS vector) WHERE id = :id AND user_id = :user_id"
                        ),
                        {"vector": json.dumps(chunk.embedding), "id": chunk.id, "user_id": user_id},
                    )
            document.status = "ready"
            document.error = None
            document.lease_until = None
            document.lease_owner = None
    except Exception:
        # Do not store exception details that may include private content or credentials.
        with factory.begin() as session:
            from .api import serialize

            serialize(session)
            document = session.get(Document, document_id)
            if document is not None and document.lease_owner == lease_owner:
                document.status = "failed"
                document.error = "Unable to extract/index this document; verify supported format, UTF-8 text and model availability"
                document.lease_until = None
                document.lease_owner = None
