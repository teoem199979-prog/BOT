
from __future__ import annotations
import re
from config import Config
from code_identity import normalize_code
from code_validator import CodeValidator, validate_code_cached
from logger_setup import logger
from telethon.tl.types import MessageEntitySpoiler

_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

_WWW_RE = re.compile(r"www\.\S+", re.IGNORECASE)

_TME_RE = re.compile(r"t\.me/\S+", re.IGNORECASE)

_DOMAIN_RE = re.compile(r"\b[a-zA-Z0-9.-]+\.(?:com|net|org|vn|app|info)\b", re.IGNORECASE)

_HASHTAG_RE = re.compile(r"#\S+")

_CURRENCY_RE = re.compile(r"\d[\d.,]{3,}\s*(?:VND|VNĐ)\b", re.IGNORECASE)

_CODE_MARKER_RE = re.compile(
    r"NHẬN\s+CODE(?:\s+NGAY)?|NHAN\s+CODE(?:\s+NGAY)?|"
    r"NHẬP\s+CODE|NHAP\s+CODE|PHÁT\s+CODE|PHAT\s+CODE|"
    r"CODE\s+FREE|FREE\s+CODE|GIFT\s*CODE|GIFTCODE|"
    r"TẶNG\s+CODE|TANG\s+CODE",
    re.IGNORECASE,
)

_NOISE_RE = re.compile(
    r"HTTP|WWW|\.COM|FACEBOOK|TELEGRAM|TIKTOK|ZALO|CSKH|BOT|CHECK\s+LINK|LINK",
    re.IGNORECASE,
)

# PATCH_MARKER_2026-10-25-v8-bugfix (#8: SPECIAL_CODE_CHARS_30 BAT BUOC phai qua re.escape truoc
# khi chen vao character class. Chuoi mac dinh da chua ^, ], \ va
# nhieu ky tu dac biet khac — bo re.escape thi regex break hoac doi
# nghia (] dong class som, ^ thanh phu dinh). Khong sua thanh phan
# nay neu khong co test di kem.)
_TOKEN_CHARS = rf"A-Za-z0-9{re.escape(getattr(Config, 'SPECIAL_CODE_CHARS_30', ''))}"
_TOKEN_RE = re.compile(
    rf"(?<![{_TOKEN_CHARS}])[{_TOKEN_CHARS}]"
    rf"{{{getattr(Config, 'CODE_MIN_LENGTH', 6)},{getattr(Config, 'CODE_MAX_LENGTH', 15) + 30}}}"
    rf"(?![{_TOKEN_CHARS}])"
)

_ALNUM_TOKEN_RE = re.compile(r"(?<![a-zA-Z0-9])[a-zA-Z0-9]{6,15}(?![a-zA-Z0-9])")

_VIETNAMESE_DIACRITIC_RE = re.compile(r"[àáảãạăằắẳẵặâầấẩẫậèéẻẽẹêềếểễệìíỉĩịòóỏõọôồốổỗộơờớởỡợùúủũụưừứửữựỳýỷỹỵđ]", re.IGNORECASE)

_PLAIN_TEXT_ORDER_PHONE_CONTEXT_RE = re.compile(
    r"(?:mã\s*(?:đơn|don|dh)|đơn\s*hàng|don\s*hang|order|invoice|"
    r"transaction|tracking|mã\s*giao\s*dịch|ma\s*giao\s*dich|"
    r"sđt|sdt|điện\s*thoại|dien\s*thoai|phone|hotline|liên\s*hệ|lien\s*he)",
    re.IGNORECASE,
)

def _iter_spoiler_texts(text: str, entities):

    if not text or not entities:
        return
    try:
        raw = text.encode("utf-16-le")
    except Exception:
        return
    for entity in entities:
        if not isinstance(entity, MessageEntitySpoiler):
            continue
        try:
            start = max(0, int(entity.offset)) * 2
            end = start + max(0, int(entity.length)) * 2
            spoiler_text = raw[start:end].decode("utf-16-le", errors="ignore").strip()
        except (AttributeError, TypeError, ValueError):
            continue
        if spoiler_text:
            yield spoiler_text

def validate_candidate(code: str, target_url: str, source: str = "normal"):
    # PATCH_MARKER_2026-10-26-v9-core-ocr-sqlite: preserve legacy validator fallback.
    try:
        return validate_code_cached(code, target_url, source=source)
    except TypeError:
        return CodeValidator.validate_code(code, target_url)

def get_filter_group_name(target_url: str) -> str:
    name, _ = CodeValidator.get_filter_group(target_url)
    return name

def unique_keep_order(items, target_url: str = ""):
    seen, result = set(), []
    for it in items:
        c = normalize_code(CodeValidator.clean_code(it), target_url)
        if not c:
            continue
        u = c
        if u not in seen:
            seen.add(u)
            result.append(c)
    return result

def remove_noise_from_text(text: str) -> str:
    t = text or ""
    t = _URL_RE.sub(" ", t)
    t = _WWW_RE.sub(" ", t)
    t = _DOMAIN_RE.sub(" ", t)
    t = _HASHTAG_RE.sub(" ", t)
    return t.replace("：", ":").replace("|", " ").replace("•", " ")

def line_has_code_marker(line: str) -> bool:
    return bool(_CODE_MARKER_RE.search(line or ""))

def line_is_noise(line: str) -> bool:
    u = line.strip()
    if not u:
        return True
    if _NOISE_RE.search(u):
        return True
    if _CURRENCY_RE.search(u):
        return True
    return False

def _line_has_vietnamese(line: str) -> bool:
    if not line:
        return False
    if _VIETNAMESE_DIACRITIC_RE.search(line):
        return True
    lower = line.lower()
    return any(w in lower for w in CodeValidator.VIETNAMESE_TEXT_WORDS)

def extract_tokens_from_line(line: str):
    mn = getattr(Config, "CODE_MIN_LENGTH", 6)
    mx = getattr(Config, "CODE_MAX_LENGTH", 15)
    return [c for c in _TOKEN_RE.findall(line or "") if mn <= len(CodeValidator.clean_code(c)) <= mx]

def extract_spoiler_codes(event, target_url: str):
    codes = []
    message = getattr(event, "message", None)
    if message is None:
        return codes
    entities = getattr(message, "entities", None) or []
    full = getattr(message, "message", None) or getattr(message, "text", None) or ""
    if not full:
        return codes
    signature = tuple((entity.offset, entity.length) for entity in entities
                      if isinstance(entity, MessageEntitySpoiler))
    if not signature:
        return codes
    cache_key = (target_url, full, signature)
    cached = getattr(event, "_autobot_spoiler_cache", None)
    if cached is not None and cached[0] == cache_key:
        return list(cached[1])
    try:
        seen_codes = set()
        seen_candidates = set()
        for sp in _iter_spoiler_texts(full, entities):
            for line in (sp.splitlines() if "\n" in sp else [sp]):
                line = line.strip()
                if not line:
                    continue
                for tok in (extract_tokens_from_line(line) or [line]):
                    candidate_key = normalize_code(CodeValidator.clean_code(tok), target_url)
                    if not candidate_key or candidate_key in seen_candidates:
                        continue
                    seen_candidates.add(candidate_key)
                    v = validate_candidate(tok, target_url, source="spoiler")
                    clean_code = v.get("clean_code")
                    normalized = normalize_code(clean_code, target_url)
                    if v.get("valid") and normalized and normalized not in seen_codes:
                        seen_codes.add(normalized)
                        codes.append(normalized)
                        logger.info(f"🔒 Spoiler: {v['clean_code']}")
                    elif not v.get("valid"):
                        logger.info(f"🔒 Spoiler bị loại: {tok!r} | {v.get('reason', '')}")
    except Exception as e:
        logger.warning(f"⚠️ spoiler: {e}")
    result = unique_keep_order(codes, target_url)
    try:
        event._autobot_spoiler_cache = (cache_key, tuple(result))
    except (AttributeError, TypeError):
        pass
    return result

def extract_marker_near_codes(text: str, target_url: str):
    lines = [l.strip() for l in remove_noise_from_text(text).splitlines()]
    codes = []
    _, group_config = CodeValidator.get_filter_group(target_url)
    marker_scan_lines = max(
        0,
        int(group_config.get("marker_scan_lines", 3) or 0),
    )
    for i, line in enumerate(lines):
        if not line_has_code_marker(line):
            continue
        scan = [line] if line else []
        for off in range(1, marker_scan_lines + 1):
            if i + off < len(lines):
                scan.append(lines[i + off])
        for sl in scan:
            if line_is_noise(sl):
                continue
            for tok in extract_tokens_from_line(sl):
                v = validate_candidate(CodeValidator.clean_code(tok), target_url, source="marker")
                if v["valid"]:
                    codes.append(v["clean_code"])
                    logger.info(f"🎯 Marker: {v['clean_code']}")
    return unique_keep_order(codes, target_url)

def _plain_text_token_is_safe(token: str, line: str) -> bool:

    clean = CodeValidator.clean_code(token)
    if not clean or not any(ch.isalpha() for ch in clean):

        return False

    digit_count = sum(ch.isdigit() for ch in clean)
    if digit_count >= 7 or (len(clean) >= 10 and digit_count / len(clean) >= 0.7):
        return False

    context = line[max(0, line.find(token) - 40): line.find(token) + len(token) + 40]
    if _PLAIN_TEXT_ORDER_PHONE_CONTEXT_RE.search(context):
        return False

    return True

def extract_plain_text_codes(text: str, target_url: str):

    if not text:
        return []

    codes = []

    hi88_requires_digit = get_filter_group_name(target_url) == "hi88"
    for raw_line in remove_noise_from_text(text).splitlines():
        line = raw_line.strip()
        if not line or line_has_code_marker(line) or line_is_noise(line):
            continue
        for token in extract_tokens_from_line(line):
            if not _plain_text_token_is_safe(token, line):
                continue
            if hi88_requires_digit and not any(ch.isdigit() for ch in token):
                continue
            result = validate_candidate(
                CodeValidator.clean_code(token), target_url, source="plain_text"
            )
            if result.get("valid"):
                codes.append(result.get("clean_code") or token)
                logger.info("🎯 Plain-text: %s", result.get("clean_code") or token)
    return unique_keep_order(codes, target_url)

def extract_hi88_near_link_codes(text: str, target_url: str) -> list:
    if not text:
        return []
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    codes = []
    for i, line in enumerate(lines):
        if "hi88-freecode.pages.dev" not in line.lower():
            continue
        nearby = []
        for off in range(1, 4):
            if i - off >= 0:
                nearby.append(lines[i - off])
            if i + off < len(lines):
                nearby.append(lines[i + off])
        for candidate_line in nearby:
            if line_is_noise(candidate_line):
                continue
            if _line_has_vietnamese(candidate_line):
                continue
            for tok in extract_tokens_from_line(candidate_line):
                v = validate_candidate(tok, target_url, source="marker")
                if v["valid"]:
                    codes.append(v["clean_code"])
                    logger.info(f"🎯 [HI88-LINK] {v['clean_code']}")
    return unique_keep_order(codes, target_url)

def extract_codes_by_regex(text: str, site_type: str = "qq88") -> list:
    if not text:
        return []
    codes = []
    if site_type == "qq88":
        BL = {"QQ88", "CODE", "DANGNHAP", "GAMEBAI", "NOHU", "CASINO", "REVIEWPHIM", "TINTUC", "KHUYENMAI", "GIFTCODE", "FREECODE", "CAMERA", "TROLL", "BONGDA", "THETHAO", "MINIGAME"}
        for m in _ALNUM_TOKEN_RE.findall(text):
            if any(k in m.upper() for k in BL):
                continue
            hl = any(c.isalpha() for c in m)
            hd = any(c.isdigit() for c in m)
            hll = any(c.islower() for c in m)
            hu = any(c.isupper() for c in m)
            if hl and (hd or (hll and hu)):
                codes.append(m)
    return list(dict.fromkeys(codes))

def extract_codes_from_message(
    event,
    raw_text: str,
    target_url: str,
    channel_name: str = "",
    include_text_after_spoiler: bool = False,
    spoiler_codes: list[str] | None = None,
):

    group = get_filter_group_name(target_url)
    logger.debug(f"[EXTRACT] group={group} | url={target_url}")

    spoiler = (
        extract_spoiler_codes(event, target_url)
        if spoiler_codes is None
        else list(spoiler_codes)
    )
    if spoiler:
        logger.warning(f"🎯 [SPOILER] {len(spoiler)}: {spoiler}")

        merge_spoiler_text = group in {"qq88", "hi88"}
        if merge_spoiler_text:
            collected = list(spoiler)
        elif getattr(Config, "SPOILER_FAST_PATH", True):
            return unique_keep_order(spoiler, target_url)
        elif not include_text_after_spoiler:
            return unique_keep_order(spoiler, target_url)
        else:
            collected = list(spoiler)
    else:
        collected = []

    if group == "qq88":
        caption = (
            getattr(event.message, "message", None)
            or getattr(event.message, "text", None)
            or raw_text
            or ""
        ).strip().lower()
        has_media = bool(getattr(event, "media", None))
        has_text = bool((raw_text or caption).strip())
        if collected:
            logger.info("✅ [QQ88] spoiler/text đã nhận; không yêu cầu link khi đã có mã hợp lệ")
        elif has_text:
            logger.info("✅ [QQ88] text")
        elif has_media:
            if "tangquaqq88.com" in caption:
                logger.info("⏭️ [QQ88] chỉ có caption/link, không có spoiler code → bỏ qua, không OCR")
            else:
                logger.info("⏭️ [QQ88] no link → skip")
                return []
        else:
            logger.info("⏭️ [QQ88] nothing → skip")
            return []

    if group == "hi88":
        hc = extract_hi88_near_link_codes(raw_text, target_url)
        if hc:
            logger.warning(f"🎯 [HI88-LINK] {hc}")
            collected.extend(hc)

    mc = extract_marker_near_codes(raw_text, target_url)
    if mc:
        logger.warning(f"🎯 [MARKER] {len(mc)}: {mc}")
        collected.extend(mc)

    if not collected or group in {"qq88", "hi88"}:
        pc = extract_plain_text_codes(raw_text, target_url)
        if pc:
            logger.warning(f"🎯 [PLAIN-TEXT] {len(pc)}: {pc}")
            collected.extend(pc)

    if collected:
        merged = unique_keep_order(collected, target_url)
        logger.warning(f"🎯 [EXTRACT-MERGED] {len(merged)}: {merged}")
        return merged

    if group == "qq88" and "tangquaqq88.com" in raw_text.lower():
        cleaned = _URL_RE.sub("", raw_text)
        cleaned = _TME_RE.sub("", cleaned)
        rq = []
        for raw_code in extract_codes_by_regex(cleaned, "qq88"):

            result = validate_candidate(raw_code, target_url, source="regex")
            if result.get("valid"):
                rq.append(result.get("clean_code") or raw_code)
        if rq:
            logger.info(f"🎯 [QQ88-REGEX] {rq}")
            return unique_keep_order(rq, target_url)

    return []
