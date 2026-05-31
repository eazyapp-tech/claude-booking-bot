"""Format property document KB content for broker agent injection."""


def format_property_docs(docs: list[dict], max_chars: int = 8000) -> str:
    """
    Format a list of document dicts (from get_property_documents_text) into a
    compact string suitable for injection into the broker agent system prompt.

    Each dict has keys: property_id, filename, text.
    """
    if not docs:
        return ""

    parts = []
    total = 0
    for doc in docs:
        header = f"[Document: {doc['filename']} | Property: {doc['property_id']}]\n"
        body = doc.get("text", "").strip()
        chunk = header + body
        remaining = max_chars - total
        if remaining <= len(header) + 50:
            break
        if len(chunk) > remaining:
            chunk = chunk[:remaining]
        parts.append(chunk)
        total += len(chunk)
        if total >= max_chars:
            break

    if not parts:
        return ""

    from core.untrusted import fence
    return fence("\n\n".join(parts), "brand-uploaded property knowledge-base documents")
