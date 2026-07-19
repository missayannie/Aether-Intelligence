"""Per-chat file/photo/folder attachments used as AI context.

Files are stored under the chat's attachments folder. Text-extractable files
(text, code, PDF) have their text pulled out and concatenated into a context
block that's injected into the conversation — so it works on BOTH the API and
subscription engines. Images are stored and passed as vision input on the API
path (litellm multimodal); subscription-path vision is a later addition.

Folders: the frontend uploads each file with its relative path as the name, so a
folder becomes many attachments (e.g. "src/utils.py").
"""
from __future__ import annotations

import base64
import mimetypes
from dataclasses import dataclass, asdict

from paths import chat_dir

ATTACH_DIRNAME = "attachments"
TEXT_EXT = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv", ".json", ".yaml",
    ".yml", ".toml", ".ini", ".cfg", ".xml", ".html", ".css", ".py", ".js", ".ts",
    ".tsx", ".jsx", ".rs", ".go", ".java", ".c", ".h", ".cpp", ".cs", ".rb", ".php",
    ".sh", ".ps1", ".sql", ".lua",
}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
MAX_TEXT_CHARS = 20000  # per file, to keep context bounded


@dataclass
class Attachment:
    name: str          # may include a relative path for folder uploads
    kind: str          # "text" | "image" | "pdf" | "other"
    size: int
    chars: int         # extracted text length (0 for images/other)


def _adir(chat_id: str):
    d = chat_dir(chat_id) / ATTACH_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe(name: str) -> str:
    # keep folder structure but strip drive/parent traversal
    return name.replace("\\", "/").lstrip("/").replace("../", "")


def _ext(name: str) -> str:
    i = name.rfind(".")
    return name[i:].lower() if i >= 0 else ""


def store(chat_id: str, name: str, data: bytes) -> Attachment:
    name = _safe(name)
    ext = _ext(name)
    path = _adir(chat_id) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)

    kind, text = "other", ""
    if ext in IMAGE_EXT:
        kind = "image"
    elif ext == ".pdf":
        kind, text = "pdf", _pdf_text(data)
    elif ext in TEXT_EXT or _looks_text(data):
        kind, text = "text", _decode(data)

    if text:
        text = text[:MAX_TEXT_CHARS]
        (_adir(chat_id) / (name + ".extracted.txt")).parent.mkdir(parents=True, exist_ok=True)
        (_adir(chat_id) / (name + ".extracted.txt")).write_text(text, encoding="utf-8")
    return Attachment(name=name, kind=kind, size=len(data), chars=len(text))


def listing(chat_id: str) -> list[dict]:
    d = _adir(chat_id)
    out = []
    for p in sorted(d.rglob("*")):
        if p.is_file() and not p.name.endswith(".extracted.txt"):
            rel = str(p.relative_to(d)).replace("\\", "/")
            ext = _ext(rel)
            kind = "image" if ext in IMAGE_EXT else "pdf" if ext == ".pdf" else (
                "text" if (d / (rel + ".extracted.txt")).exists() else "other")
            extracted = d / (rel + ".extracted.txt")
            chars = extracted.stat().st_size if extracted.exists() else 0
            out.append(asdict(Attachment(name=rel, kind=kind, size=p.stat().st_size, chars=chars)))
    return out


def delete(chat_id: str, name: str) -> None:
    d = _adir(chat_id)
    for p in (d / _safe(name), d / (_safe(name) + ".extracted.txt")):
        if p.exists():
            p.unlink()


def context_block(chat_id: str) -> str:
    """All attachments' extracted text, framed for the model."""
    d = _adir(chat_id)
    parts = []
    for p in sorted(d.rglob("*.extracted.txt")):
        rel = str(p.relative_to(d))[: -len(".extracted.txt")].replace("\\", "/")
        parts.append(f"--- Attached file: {rel} ---\n{p.read_text(encoding='utf-8')}")
    if not parts:
        return ""
    return "The user attached these files as context:\n\n" + "\n\n".join(parts)


def image_data_urls(chat_id: str) -> list[dict]:
    """Images as data-URL content blocks for litellm multimodal (API path)."""
    d = _adir(chat_id)
    blocks = []
    for p in sorted(d.rglob("*")):
        if p.is_file() and _ext(p.name) in IMAGE_EXT:
            mime = mimetypes.guess_type(p.name)[0] or "image/png"
            b64 = base64.b64encode(p.read_bytes()).decode()
            blocks.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
    return blocks


def image_blocks_anthropic(chat_id: str) -> list[dict]:
    """Images as Anthropic-format base64 blocks for the Agent SDK (subscription)."""
    d = _adir(chat_id)
    blocks = []
    for p in sorted(d.rglob("*")):
        if p.is_file() and _ext(p.name) in IMAGE_EXT:
            mime = mimetypes.guess_type(p.name)[0] or "image/png"
            b64 = base64.b64encode(p.read_bytes()).decode()
            blocks.append({
                "type": "image",
                "source": {"type": "base64", "media_type": mime, "data": b64},
            })
    return blocks


def _decode(data: bytes) -> str:
    for enc in ("utf-8", "utf-16", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return ""


def _looks_text(data: bytes) -> bool:
    sample = data[:2048]
    if not sample:
        return False
    if b"\x00" in sample:
        return False
    printable = sum(1 for b in sample if 9 <= b <= 13 or 32 <= b <= 126 or b >= 128)
    return printable / len(sample) > 0.9


def _pdf_text(data: bytes) -> str:
    try:
        import io
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        return "\n".join((pg.extract_text() or "") for pg in reader.pages)
    except Exception:
        return ""
