
from __future__ import annotations
import re
from telethon.tl.types import MessageEntitySpoiler, MessageEntityCustomEmoji
from config import Config
from code_identity import normalize_code
from code_validator import CodeValidator
from logger_setup import logger
from code_extraction import (extract_codes_from_message, extract_tokens_from_line,
                             unique_keep_order, validate_candidate, _iter_spoiler_texts)

_KJC_SPECIAL_CHANNEL_IDS = Config.KJC_SPECIAL_CHANNEL_IDS

_KJC_BROADCAST_DOMAINS = Config.KJC_BROADCAST_DOMAINS

_KJC_BROADCAST_URLS = Config.KJC_BROADCAST_URLS

_KJC_LABEL_RE = re.compile(
    r"(?:NHẬN\s+CODE|NHAN\s+CODE|CODE)\s*(?:NGAY|NGÀY)?\s*[:\-–—]?\s*"
    r"(MM88|RR88|XX88|GG88)",
    re.IGNORECASE,
)

_KJC_SITE_RE = {
    key: re.compile(rf"\b{re.escape(key)}\b", re.IGNORECASE)
    for key in ("MM88", "RR88", "XX88", "GG88")
}

def is_kjc_special_channel(chat_id) -> bool:
    try:
        return int(chat_id) in _KJC_SPECIAL_CHANNEL_IDS
    except Exception:
        return False


def extract_kjc_spoiler_codes(event) -> list[str]:
    if not is_kjc_special_channel(getattr(event, "chat_id", None)):
        return []

    message = getattr(event, "message", None)
    if message is None:
        return []

    full_text = (
        getattr(message, "message", None)
        or getattr(message, "text", None)
        or ""
    )

    if not full_text:
        return []

    entities = getattr(message, "entities", None) or []
    codes = []
    seen_codes = set()
    seen_candidates = set()
    for spoiler_text in _iter_spoiler_texts(full_text, entities):
        for line in spoiler_text.splitlines():
            line = line.strip()
            if not line:
                continue

            candidates = extract_tokens_from_line(line)
            if not candidates:
                candidates = [line]

            for candidate in candidates:
                cleaned = CodeValidator.clean_code(candidate)
                normalized_candidate = normalize_code(cleaned, "xx88code.com")
                if not cleaned or normalized_candidate in seen_candidates:
                    continue
                seen_candidates.add(normalized_candidate)

                try:
                    result = validate_candidate(
                        cleaned,
                        "https://xx88code.com",
                        source="kjc_spoiler",
                    )
                except Exception as exc:
                    logger.warning(
                        "⚠️ [KJC] Không validate được '%s': %s",
                        cleaned,
                        exc,
                    )
                    continue

                if not result.get("valid"):
                    logger.warning(
                        "🚫 [KJC] Bỏ code không hợp lệ: %r",
                        cleaned,
                    )
                    continue

                code = result.get("clean_code") or cleaned
                code_key = normalize_code(code, "xx88code.com")
                if code_key not in seen_codes:
                    seen_codes.add(code_key)
                    codes.append(code)
                    logger.info("🎯 [KJC-SPOILER] phát hiện: %s", code)

    try:
        fallback = extract_codes_from_message(
            event,
            full_text,
            "https://xx88code.com",
            channel_name="KJC",
            include_text_after_spoiler=True,
            spoiler_codes=[],
        )
        return unique_keep_order(codes + (fallback or []), "xx88code.com")
    except Exception as exc:
        logger.debug("⚠️ [KJC] text fallback lỗi: %s", exc)
        return unique_keep_order(codes, "xx88code.com")

def detect_kjc_labeled_domains(event, raw_text: str = "") -> tuple[str, ...]:

    message = getattr(event, "message", None)
    text = " ".join(
        part for part in (
            raw_text,
            getattr(message, "message", None) if message else "",
            getattr(message, "text", None) if message else "",
        ) if part
    ).upper()

    aliases = {
        "MM88": "livemm88.net",
        "RR88": "liverr88.net",
        "XX88": "xx88code.com",
        "GG88": "gg88live.tv",
    }

    label_match = _KJC_LABEL_RE.search(text)
    if label_match:
        return (aliases[label_match.group(1).upper()],)

    found = tuple(dict.fromkeys(aliases[k] for k in aliases if _KJC_SITE_RE[k].search(text)))
    return found if len(found) == 1 else ()

def build_kjc_broadcast_items(
    codes: list[str], channel_name: str, target_domains: tuple[str, ...] = ()
) -> list[dict]:
    items = []
    domains = target_domains or _KJC_BROADCAST_DOMAINS
    active_domains = set(getattr(Config, "ACTIVE_DOMAINS", ()) or ())
    if active_domains:
        domains = tuple(domain for domain in domains if domain in active_domains)
    for code in unique_keep_order(codes, "xx88code.com"):
        for domain in domains:
            items.append(
                {
                    "code": normalize_code(code, domain),
                    "channel_name": channel_name,
                    "target_url": _KJC_BROADCAST_URLS[domain],
                    "domain": domain,
                    "source": "kjc_spoiler",
                }
            )
    return items


# PATCH_MARKER_2026-10-26-parallel-llwin-local-labels
_LOCAL_SITE_ALIASES = {
    "XX88": ("xx88code.com", "https://xx88code.com"),
    "MM88": ("livemm88.net", "https://livemm88.net/nhap-code"),
    "RR88": ("liverr88.net", "https://liverr88.net/nhap-code"),
    "GG88": ("gg88live.tv", "https://gg88live.tv/nhap-code"),
    "HI88": ("hi88-freecode.pages.dev", "https://hi88-freecode.pages.dev/"),
    "V79": ("v79code.com", "https://v79code.com/"),
    "LLWIN": ("llwincode.com", "https://llwincode.com/"),
}
_LOCAL_SITE_LABEL = re.compile(
    r"(?<![\w/\.])(?:XX88|MM88|RR88|GG88|HI88|V79|LLWIN)(?![\w.])", re.I
)


def _sticker_document_ids(message):
    ids = []
    sticker = getattr(message, "sticker", None)
    if sticker:
        document = getattr(sticker, "document", None)
        document_id = (
            getattr(document, "id", None)
            or getattr(sticker, "document_id", None)
            or getattr(sticker, "id", None)
        )
        if document_id is not None:
            ids.append(document_id)

    media = getattr(message, "media", None)
    document = getattr(media, "document", None)
    attributes = getattr(document, "attributes", ()) or ()
    is_sticker = bool(sticker) or any(
        type(attribute).__name__ == "DocumentAttributeSticker"
        for attribute in attributes
    )
    if is_sticker and getattr(document, "id", None) is not None:
        ids.append(document.id)
    return tuple(dict.fromkeys(ids))


def _entity_char_span(text, entity):
    """Telegram offsets count UTF-16 units, including emoji surrogate pairs."""
    raw = text.encode("utf-16-le")
    start, length = int(entity.offset), int(entity.length)
    if start < 0 or length <= 0 or (start+length)*2 > len(raw):
        return None
    return (len(raw[:start*2].decode("utf-16-le", errors="ignore")),
            len(raw[:(start+length)*2].decode("utf-16-le", errors="ignore")))


def extract_kjc_routed_items(event, channel_name="") -> list[dict]:
    """Associate each spoiler with its local text/custom-emoji site label.

    A row may contain several labelled code groups. A label-only preceding
    line can introduce the next row. Footer brands and unrelated article text
    never supply a message-wide default, and there is no broadcast fallback.
    """
    if not is_kjc_special_channel(getattr(event, "chat_id", None)):
        return []
    message = getattr(event, "message", None)
    text = (getattr(message, "message", None) or getattr(message, "text", None) or "")
    if not text:
        return []
    entities = getattr(message, "entities", None) or []
    spoilers, emoji, barriers = [], [], []
    mapping = {
        **{str(key): value for key, value in getattr(Config, "KJC_CUSTOM_EMOJI_DOMAINS", {}).items()},
        **{str(key): value for key, value in getattr(Config, "KJC_STICKER_DOMAINS", {}).items()},
        **{str(key): value for key, value in getattr(Config, "KJC_BRAND_MAPPING", {}).items()},
    }
    separators = {str(value) for value in getattr(Config, "KJC_CODE_SEPARATOR_EMOJI_IDS", ())}
    known_document_found = False
    unknown_document_ids = set()
    mapped_emoji_domains = set()
    for entity in entities:
        if not isinstance(entity, (MessageEntitySpoiler, MessageEntityCustomEmoji)):
            continue
        try:
            span = _entity_char_span(text, entity)
        except (AttributeError, TypeError, ValueError, UnicodeError):
            continue
        if span is None:
            continue
        if isinstance(entity, MessageEntitySpoiler):
            spoilers.append(span)
        else:
            try:
                brand_id = str(entity.document_id)
            except AttributeError:
                continue
            domain = mapping.get(brand_id)
            emoji.append((*span, domain))
            if domain is not None:
                known_document_found = True
                mapped_emoji_domains.add(domain)
            elif brand_id not in separators:
                known_document_found = True
                unknown_document_ids.add(brand_id)
                barriers.append(span)

    sticker_domains = set()
    for raw_id in _sticker_document_ids(message):
        brand_id = str(raw_id)
        domain = mapping.get(brand_id)
        if domain is None:
            known_document_found = True
            unknown_document_ids.add(brand_id)
            barriers.append((0, len(text)))
        else:
            known_document_found = True
            sticker_domains.add(domain)
    for brand_id in sorted(unknown_document_ids):
        logger.warning("UNKNOWN_BRAND_EMOJI ID=%s", brand_id)
    if len(sticker_domains) > 1:
        logger.warning("⚠️ [KJC] Conflicting sticker brand IDs; skipping message")
        return []

    mapped_emoji = [(a, b, domain) for a, b, domain in emoji if domain]
    # Telegram's sticker document has no text offset, so use its brand only
    # for spoilers without a nearer custom-emoji brand label.
    sticker_domain = next(iter(sticker_domains), None)
    spoilers = sorted(set(spoilers))
    if not spoilers:
        return []

    def in_spoiler(start, end):
        return any(start < b and end > a for a, b in spoilers)

    labels = [(a, b, domain) for a, b, domain in mapped_emoji if not in_spoiler(a, b)]
    preserve_llwin_text_fallback = (
        mapped_emoji_domains == {"llwincode.com"}
        and not unknown_document_ids
        and not sticker_domains
    )
    if not known_document_found or preserve_llwin_text_fallback:
        labels.extend((m.start(), m.end(), _LOCAL_SITE_ALIASES[m.group().upper()][0])
                      for m in _LOCAL_SITE_LABEL.finditer(text) if not in_spoiler(m.start(), m.end()))
    labels.sort()
    urls = {domain: url for domain, url in _LOCAL_SITE_ALIASES.values()}
    active = set(getattr(Config, "ACTIVE_DOMAINS", ()) or ())
    items, seen = [], set()
    for start, end in spoilers:
        lo, hi = text.rfind("\n", 0, start)+1, text.find("\n", end)
        hi = len(text) if hi < 0 else hi
        # Separators outside spoilers create independent labelled cells.
        row_start = lo
        for mark in re.finditer(r"[|;]", text[lo:hi]):
            pos = row_start+mark.start()
            if in_spoiler(pos, pos+1):
                continue
            if pos < start:
                lo = pos+1
            elif pos >= end:
                hi = min(hi, pos)
        row_labels = [label for label in labels if lo <= label[0] and label[1] <= hi]
        row_codes = [(a, b) for a, b in spoilers if lo <= a and b <= hi]
        chosen = None
        if row_labels:
            domains = {label[2] for label in row_labels}
            if sticker_domain is not None and domains != {sticker_domain}:
                continue
            if len(row_codes) == 1 and len(domains) > 1:
                continue  # Conflicting labels surrounding one code are ambiguous.
            prefix = row_labels[0][0] < row_codes[0][0]
            options = ([label for label in row_labels if label[1] <= start] if prefix
                       else [label for label in row_labels if label[0] >= end])
            if options:
                chosen = options[-1] if prefix else options[0]
        elif lo > 0:
            prev_end = lo-1
            prev_start = text.rfind("\n", 0, prev_end)+1
            previous = [label for label in labels if prev_start <= label[0] and label[1] <= prev_end]
            if previous and len({label[2] for label in previous}) == 1:
                residue = list(text[prev_start:prev_end])
                for a, b in [(a, b) for a, b, _ in previous]+[(a, b) for a, b, _ in emoji]:
                    for i in range(max(a, prev_start), min(b, prev_end)):
                        residue[i-prev_start] = " "
                rest = re.sub(r"NHẬN|NHAN|NHẬP|NHAP|CODE|NGAY", "", "".join(residue), flags=re.I)
                if not any(c.isalnum() for c in rest) and not any(prev_start <= a < prev_end for a, b in spoilers):
                    chosen = previous[-1]
        if chosen is None:
            if sticker_domain is not None and not any(
                lo <= label[0] and label[1] <= hi for label in labels
            ) and not any(
                barrier_start < hi and barrier_end > lo
                for barrier_start, barrier_end in barriers
            ):
                chosen = (0, len(text), sticker_domain)
            else:
                continue
        a, b, domain = chosen
        if domain not in urls or active and domain not in active:
            continue
        between_start, between_end = (b, start) if b <= start else (end, a)
        if any(between_start <= x and y <= between_end and not in_spoiler(x, y)
               for x, y in barriers):
            continue  # Unknown logo is not silently treated as the preceding brand.
        target = urls[domain]
        for line in text[start:end].splitlines():
            for candidate in extract_tokens_from_line(line) or [line.strip()]:
                if not candidate:
                    continue
                result = validate_candidate(candidate, target, source="kjc_spoiler")
                code = normalize_code(result.get("clean_code") or "", domain)
                if result.get("valid") and code and (domain, code) not in seen:
                    seen.add((domain, code))
                    items.append({"code": code, "channel_name": channel_name,
                                  "target_url": target, "domain": domain,
                                  "source": "kjc_spoiler", "source_kind": "spoiler"})
    return items
