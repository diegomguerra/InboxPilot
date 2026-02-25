import re
import html

ZERO_WIDTH_CHARS = '\u200b\u200c\u200d\ufeff\u00ad\u2060\u180e'
ZWNJ_PATTERN = re.compile(r'&(?:zwnj|zwj|#8203|#8204|#8205|#65279);', re.IGNORECASE)


def normalize_email_text(text: str) -> str:
    """Clean email text: unescape HTML entities, remove zero-width chars, collapse spaces."""
    if not text:
        return ""
    
    result = html.unescape(text)
    
    result = ZWNJ_PATTERN.sub('', result)
    
    for char in ZERO_WIDTH_CHARS:
        result = result.replace(char, '')
    
    result = re.sub(r'[ \t]+', ' ', result)
    result = re.sub(r'\n{3,}', '\n\n', result)
    
    return result.strip()


def html_to_text(html_content: str) -> str:
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", "", html_content)
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", "", text)
    text = re.sub(r"(?is)<!--.*?-->", "", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&#\d+;", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_text(text: str) -> str:
    if not text:
        return ""
    return html.unescape(text).strip()


def parse_email_address(from_header: str) -> str:
    if not from_header:
        return ""
    match = re.search(r'<([^>]+)>', from_header)
    if match:
        return match.group(1)
    match = re.search(r'[\w\.-]+@[\w\.-]+', from_header)
    if match:
        return match.group(0)
    return from_header.strip()


def clean_text(raw: str) -> str:
    if not raw:
        return ""
    result = html.unescape(raw)
    result = ZWNJ_PATTERN.sub('', result)
    for char in ZERO_WIDTH_CHARS:
        result = result.replace(char, '')
    result = re.sub(r"(?is)<script[^>]*>.*?</script>", "", result)
    result = re.sub(r"(?is)<style[^>]*>.*?</style>", "", result)
    result = re.sub(r"(?s)<[^>]+>", " ", result)
    result = re.sub(r'[ \t]+', ' ', result)
    result = re.sub(r'\n{3,}', '\n\n', result)
    return result.strip()


def truncate_text(text: str, max_chars: int) -> str:
    if not text or len(text) <= max_chars:
        return text or ""
    return text[:max_chars].rsplit(' ', 1)[0] + "..."


def build_email_llm_context(email_data: dict, max_body_chars: int = 12000) -> str:
    from_addr = email_data.get("from", email_data.get("from_addr", ""))
    subject = email_data.get("subject", "")
    date = email_data.get("date", "")
    snippet = email_data.get("snippet", "")
    body = email_data.get("body", email_data.get("body_text", ""))
    body_clean = clean_text(body)
    body_truncated = truncate_text(body_clean, max_body_chars)
    lines = [
        f"De: {from_addr}",
        f"Assunto: {subject}",
        f"Data: {date}",
    ]
    if snippet:
        lines.append(f"Preview: {snippet}")
    lines.append(f"\nCorpo:\n{body_truncated}")
    return "\n".join(lines)
