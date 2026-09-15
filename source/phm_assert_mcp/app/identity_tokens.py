"""Extract literal code constraints, not identities or business intent."""
import re
import unicodedata

_CODE = re.compile(r"(?<![A-Za-z0-9_.-])[A-Za-z][A-Za-z0-9_-]{3,255}(?![A-Za-z0-9_.-])")


def code_tokens(text):
    text = unicodedata.normalize("NFKC", str(text or ""))
    return list(dict.fromkeys(m[0] for m in _CODE.finditer(text)
        if any(c.isdigit() for c in m[0]) and not m[0].upper().startswith("FLT-")
        and not re.search(r"(?:型号|标准号?|版本|协议)\s*[:：为是]?\s*$", text[max(0, m.start()-12):m.start()])))[:32]
