
from __future__ import annotations
from config import Config

_OCR_ONLY_CHANNEL_IDS = frozenset((-1002817093108, -1004395536862))

_QQ88_PROMO_MARKERS = (
    "voucher", "mã km", "ma km", "tổng nạp", "tổng cược", "kim cương",
    "quà tặng", "ưu đãi", "uu dai",
)

def _is_ocr_allowed_channel(chat_id, event=None) -> bool:
    try:
        cid = int(chat_id)
    except (ValueError, TypeError):
        return False
    if cid in _OCR_ONLY_CHANNEL_IDS:
        return True
    if cid in getattr(Config, "QQ88_OCR_CHAT_IDS", ()):

        return bool(getattr(Config, "QQ88_OCR_ENABLED", True)) and event is not None and _is_qq88_code_post(event)
    if not bool(getattr(Config, "HI88_OCR_ENABLED", False)) or event is None:
        return False
    if cid not in set(getattr(Config, "OCR_MEDIA_FALLBACK_CHANNEL_IDS", ())):
        return False
    cfg = getattr(Config, "CHANNEL_CONFIG", {}).get(cid, {})
    return "hi88-freecode.pages.dev" in str(cfg.get("url", "")) and _has_hi88_code_link(event)

def _has_hi88_code_link(event) -> bool:

    message = getattr(event, "message", None)
    texts = [
        getattr(message, "text", "") or "",
        getattr(message, "message", "") or "",
    ]
    haystack = " ".join(texts).lower()
    if "hi88-freecode.pages.dev" in haystack:
        return True
    for entity in getattr(message, "entities", None) or []:
        url = getattr(entity, "url", "") or ""
        if "hi88-freecode.pages.dev" in url.lower():
            return True
    return False

def _qq88_message_text(event) -> str:
    message = getattr(event, "message", None)
    texts = [getattr(message, "text", "") or "", getattr(message, "message", "") or ""]
    return " ".join(texts).lower()

def _has_qq88_code_link(event) -> bool:

    if "tangquaqq88.com" in _qq88_message_text(event):
        return True
    message = getattr(event, "message", None)
    for entity in getattr(message, "entities", None) or []:
        if "tangquaqq88.com" in (getattr(entity, "url", "") or "").lower():
            return True
    return False

def _is_qq88_code_post(event) -> bool:

    if not _has_qq88_code_link(event):
        return False
    haystack = _qq88_message_text(event)
    return not any(marker in haystack for marker in _QQ88_PROMO_MARKERS)

def _qq88_ocr_shape_ok(candidate: str) -> bool:

    value = str(candidate or "").strip()
    expected = {max(4, int(length)) for length in getattr(
        Config, "QQ88_OCR_CODE_LENGTHS", (getattr(Config, "QQ88_OCR_CODE_LENGTH", 10), 12)
    )}
    if len(value) not in expected or not value.isascii() or not value.isalnum():
        return False
    if bool(getattr(Config, "QQ88_OCR_REQUIRE_MIXED_CASE", True)):
        return any(ch.islower() for ch in value) and any(ch.isupper() for ch in value)
    return True


def _hi88_code_shape_ok(candidate: str, lengths=(10, 12)) -> bool:
    value = str(candidate or "").strip()
    return (
        len(value) in {int(length) for length in lengths}
        and value.isascii() and value.isalnum()
        and any(ch.isalpha() for ch in value)
        and (any(ch.isdigit() for ch in value)
             or (any(ch.islower() for ch in value) and any(ch.isupper() for ch in value)))
    )
