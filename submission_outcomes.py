from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from dashboard import update_dashboard
from logger_setup import logger
from code_identity import normalize_code


from media_helpers import take_result_screenshot

SUCCESS_KW = [
    "THÀNH CÔNG", "THANH CONG", "SUCCESS", "COMPLETED",
    "ĐÃ NHẬN", "DA NHAN", "RECEIVED", "ADDED", "AWARDED",
    "CONGRATULATIONS", "APPROVED", "ACCEPTED",
]

FAILED_KW = [
    "SAI", "LỖI", "LOI",
    "ĐÃ SỬ", "DA SU", "ĐÃ DÙNG",
    "FAILED", "ERROR", "INVALID",
    "KHÔNG ĐÚNG", "KHÔNG TỒN TẠI", "KHÔNG HỢP LỆ",
    "HẾT HẠN", "ĐÃ HẾT", "EXPIRED",
    "CODE ĐÃ SỬ DỤNG HẾT", "CODE DA SU DUNG HET",
    "CODE ĐÃ HẾT HẠN", "CODE DA HET HAN",
    "MÃ ĐÃ ĐƯỢC SỬ DỤNG", "MA DA DUOC SU DUNG",
    "ĐÃ ĐƯỢC SỬ DỤNG", "DA DUOC SU DUNG",
        "ĐÃ CÓ NGƯỜI SỬ DỤNG", "DA CO NGUOI SU DUNG",
    "KHÔNG CÒN HỢP LỆ", "KHONG CON HOP LE",
    "MÃ ĐÃ HẾT HẠN", "MA DA HET HAN",
    "CODE USED UP", "CODE HAS EXPIRED",
    "NOT FOUND", "NOT EXIST", "KHÔNG TÌM THẤY",
    "CODE NOT USED", "CODE_NOT_USED",
    "CODE_NOT_ACTIVE", "CODE NOT ACTIVE",
    "THAT BAI", "THẤT BẠI",
    "REJECTED", "DECLINED",
]


ACCOUNT_LIMIT_KW = (
    "ĐẠT GIỚI HẠN", "DAT GIOI HAN", "ĐÃ ĐẠT GIỚI HẠN", "DA DAT GIOI HAN",
    "GIỚI HẠN NHẬN", "GIOI HAN NHAN", "GIỚI HẠN LƯỢT", "GIOI HAN LUOT",
    "VƯỢT QUÁ GIỚI HẠN", "VUOT QUA GIOI HAN", "QUÁ SỐ LẦN", "QUA SO LAN",
    "QUÁ SỐ LƯỢT", "QUA SO LUOT", "HẾT SỐ LẦN", "HET SO LAN",
    "HẾT LƯỢT", "HET LUOT", "KHÔNG CÒN LƯỢT", "KHONG CON LUOT",
    "TỐI ĐA LƯỢT", "TOI DA LUOT", "SỐ LẦN NHẬP TỐI ĐA", "SO LAN NHAP TOI DA",
    "ĐÃ NHẬP TỐI ĐA", "DA NHAP TOI DA", "TÀI KHOẢN ĐÃ NHẬN",

    "KHÔNG ĐỦ ĐIỀU KIỆN", "KHONG DU DIEU KIEN", "CHƯA ĐỦ ĐIỀU KIỆN", "CHUA DU DIEU KIEN",
    "LIMIT REACHED", "DAILY LIMIT", "ACCOUNT LIMIT", "MAXIMUM CLAIM",
    "MAX ATTEMPTS", "EXCEEDED THE LIMIT", "LIMIT EXCEEDED",
)


def is_account_limit_text(text: str) -> bool:
    upper = str(text or "").upper()
    return any(kw in upper for kw in ACCOUNT_LIMIT_KW)


TOO_MANY_KW = [
    "TOO MANY",
    "RATE LIMIT",
    "QUÁ NHIỀU",
    "THÊM SAU",
    "THỬ LẠI SAU",
    # XX8 business-level throttle trả HTTP 200; KHÔNG được coi AMBIGUOUS.
    "KHÔNG THAO TÁC LIÊN TỤC",
    "KHONG THAO TAC LIEN TUC",
    "THAO TÁC QUÁ NHANH",
    "THAO TAC QUA NHANH",
]


_HTTP_429_RE = re.compile(r"(?<![\d.,])429(?![\d.,]?\d)(?!\s*(?:ĐIỂM|DIEM|XU|COIN|POINT))")

NEGATIVE_SUCCESS_KW = (
    "NOT ACCEPTED", "NOT ADDED", "NOT APPROVED", "UNSUCCESSFUL",
    "KHÔNG THÀNH CÔNG", "KHONG THANH CONG",
)

POINT_KW = ["ĐIỂM", "XU", "COIN", "POINT"]


_CAPTCHA_WORDS = r"(?:CAPTCHA|TURNSTILE|CLOUDFLARE|TOKEN)"
_EXPIRY_WORDS = (
    r"(?:HẾT HẠN|HET HAN|EXPIRED|EXPIRE|TIMEOUT|TIMED OUT|TIME OUT|"
    r"HẾT THỜI GIAN|HET THOI GIAN|QUÁ HẠN|QUA HAN)"
)
_CAPTCHA_EXPIRED_RE = re.compile(
    rf"{_CAPTCHA_WORDS}[^.\n]{{0,40}}{_EXPIRY_WORDS}"
    rf"|{_EXPIRY_WORDS}[^.\n]{{0,40}}{_CAPTCHA_WORDS}"
    r"|CAPTCHA_EXPIRED|CAPTCHA_TIMEOUT"
)


_CAPTCHA_INVALID_RE = re.compile(
    r"CAPTCHA_INVALID|CAPTCHA_FAILED|INVALID[_\s-]*CAPTCHA|"
    r"CAPTCHA\s+(?:XÁC\s+THỰC\s+)?KHÔNG\s+HỢP\s+LỆ|"
    r"CAPTCHA\s+(?:XAC\s+THUC\s+)?KHONG\s+HOP\s+LE|"
    r"TOKEN[^.\n]{0,30}(?:INVALID|KHÔNG\s+HỢP\s+LỆ|KHONG\s+HOP\s+LE)"
)

_SHORT_KW = {"SAI", "LOI", "LỖI", "XU", "ĐIỂM", "DIEM"}
_SHORT_KW_PATTERNS = {
    kw: re.compile(rf"(?:^|[^\w]){re.escape(kw)}(?:[^\w]|$)")
    for kw in _SHORT_KW
}


def _kw_matches(text_upper: str, keyword: str) -> bool:
    pattern = _SHORT_KW_PATTERNS.get(keyword)
    if pattern is not None:
        return bool(pattern.search(text_upper))
    return keyword in text_upper


_CONNECTIVITY_KW = (
    "KHÔNG THỂ KẾT NỐI ĐẾN SERVER", "KHONG THE KET NOI DEN SERVER",
    "KHÔNG THỂ KẾT NỐI TỚI SERVER", "KHONG THE KET NOI TOI SERVER",
    "KHÔNG THỂ KẾT NỐI ĐẾN MÁY CHỦ", "KHONG THE KET NOI DEN MAY CHU",
    "FAILED TO FETCH", "NETWORK ERROR", "NETWORKERROR",
    "ERR_CONNECTION_RESET", "ERR_CONNECTION_TIMED_OUT",
)


class ResultStatus(str, Enum):
    CONNECTIVITY_ERROR = "CONNECTIVITY_ERROR"
    SUCCESS_POINTS = "SUCCESS_POINTS"
    SUCCESS_NO_POINTS = "SUCCESS_NO_POINTS"
    FAILED = "FAILED"
    AMBIGUOUS = "AMBIGUOUS"
    NO_RESULT = "NO_RESULT"
    RATE_LIMITED = "RATE_LIMITED"
    ACCOUNT_LIMIT = "ACCOUNT_LIMIT"
    CAPTCHA_EXPIRED = "CAPTCHA_EXPIRED"


@dataclass(frozen=True)
class Outcome:
    status: ResultStatus
    result: dict


def classify_result(raw_text: str) -> ResultStatus:
    text = raw_text or ""
    stripped = text.strip()
    upper = text.upper()

    if any(term in upper for term in _CONNECTIVITY_KW):
        return ResultStatus.CONNECTIVITY_ERROR

    instructional = (
        "NHẬP TÀI KHOẢN VÀ MÃ CODE ĐỂ NHẬN ĐIỂM",
        "NHAP TAI KHOAN VA MA CODE DE NHAN DIEM",
    )
    if upper.strip().rstrip(".! ") in instructional:
        return ResultStatus.NO_RESULT

    if is_account_limit_text(upper) and not any(
        _kw_matches(upper, kw) for kw in SUCCESS_KW
    ):
        return ResultStatus.ACCOUNT_LIMIT

    is_rate_limited = any(kw in upper for kw in TOO_MANY_KW) or bool(_HTTP_429_RE.search(upper))
    if is_rate_limited:
        return ResultStatus.RATE_LIMITED

    if _CAPTCHA_EXPIRED_RE.search(upper) or _CAPTCHA_INVALID_RE.search(upper):
        return ResultStatus.CAPTCHA_EXPIRED

    if any(marker in upper for marker in NEGATIVE_SUCCESS_KW):
        return ResultStatus.FAILED

    is_success = any(_kw_matches(upper, kw) for kw in SUCCESS_KW)
    is_failed = any(_kw_matches(upper, kw) for kw in FAILED_KW)

    if is_success and not is_failed:
        has_points = any(_kw_matches(upper, kw) for kw in POINT_KW)
        return ResultStatus.SUCCESS_POINTS if has_points else ResultStatus.SUCCESS_NO_POINTS

    if len(stripped) < 3:
        return ResultStatus.NO_RESULT

    if is_failed:
        return ResultStatus.FAILED

    return ResultStatus.AMBIGUOUS


async def _build_no_result_debug_info(
    page, code: str, user: str, domain: str, clicked: bool,
    pre_click_text: str, result_timeout_s: float, is_hi88: bool,
) -> dict:

    debug_info: dict[str, Any] = {
        "code": code,
        "user": user,
        "domain": domain,
        "clicked_submit_button": clicked,
        "pre_click_text_len": len(pre_click_text or ""),
        "result_timeout_s": result_timeout_s,
        "page_url": None,
        "post_click_text_snippet": "",
    }
    try:
        debug_info["page_url"] = page.url
    except Exception:
        pass
    try:
        post_text = await page.evaluate("() => document.body.innerText || ''")
        debug_info["post_click_text_snippet"] = (post_text or "")[:800]
    except Exception:
        pass
    if is_hi88:
        try:
            debug_info["hi88_watcher_captures"] = await page.evaluate(
                "() => window.__hi88Captures || []"
            )
        except Exception:
            debug_info["hi88_watcher_captures"] = []
    return debug_info


def _safe_append_history(callbacks: Optional[dict], **kwargs) -> None:

    if not callbacks:
        return
    fn = callbacks.get("append_history")
    if not fn:
        return
    try:
        fn(**kwargs)
    except Exception as e:
        logger.debug(f"⚠️ append_history callback error: {e}")


async def record_outcome(
    *,
    systems: dict,
    page,
    user: str,
    code: str,
    target_url: str,
    domain: str,
    elapsed: float,
    raw_text: str = "",
    result_text: str = "",
    status: "ResultStatus | str | None" = None,
    key: str | None = None,

    callbacks: Optional[dict] = None,
    debug_info: Optional[dict] = None,
    clicked: bool = True,
    pre_click_text: str = "",
    result_timeout_s: float = 0.0,
    is_hi88: bool = False,
    **_ignored_kwargs: Any,
) -> Outcome:
    code = normalize_code(code, domain)
    db = systems["db"]
    perf_mon = systems["performance_monitor"]

    final_raw_text = raw_text or result_text or ""
    key = key or f"{domain}|{user}"

    if status is None:
        status = classify_result(final_raw_text)
    elif not isinstance(status, ResultStatus):
        try:
            status = ResultStatus(status)
        except ValueError:

            logger.debug(f"⚠️ [{key}] status lạ '{status}' — tự phân loại lại từ raw_text")
            status = classify_result(final_raw_text)

    result_text = final_raw_text
    loop = asyncio.get_running_loop()
    db_executor = systems.get("db_executor")

    take_screenshot_fn = (callbacks or {}).get("take_screenshot") or take_result_screenshot

    if status == ResultStatus.RATE_LIMITED:
        from config import Config
        backoff_delay = max(
            0.1,
            float(getattr(Config, "RATE_LIMIT_BACKOFF_SECONDS", 5.0)),
        )

        logger.warning(
            f"🚫 [{user}|{domain}] Too Many Requests — defer {backoff_delay:.1f}s ngoài browser lock"
        )
        update_dashboard(
            domain=domain, account=user, code=code, status="RATE_LIMITED",
            rtt_ms=elapsed * 1000, raw_response=result_text,
        )
        _safe_append_history(
            callbacks, event_type="RESULT", code=code, target_url=target_url,
            account=user, status="RATE_LIMITED", submit_elapsed=elapsed,
            message=result_text[:100],
        )
        await loop.run_in_executor(
            db_executor, db.record_submission, code, user, target_url,
            "RATE_LIMITED", result_text[:100],
        )
        perf_mon.record_task("submit_code", elapsed, False)
        outcome = Outcome(
            status=status,
            result={
                "success": False,
                "rate_limited": True,
                "retry_after_seconds": backoff_delay,
                "failure_kind": "RATE_LIMITED",
                "message": result_text[:120],
            },
        )

    elif status == ResultStatus.ACCOUNT_LIMIT:
        logger.warning(f"⚠️ [{user}|{domain}] Site báo đạt giới hạn — vẫn giữ account trong vòng xoay — {result_text[:60]}")
        update_dashboard(
            domain=domain, account=user, code=code, status="ĐẠT GIỚI HẠN",
            rtt_ms=elapsed * 1000, raw_response=result_text,
        )
        _safe_append_history(
            callbacks, event_type="RESULT", code=code, target_url=target_url,
            account=user, status="ACCOUNT_LIMIT", submit_elapsed=elapsed,
            message=result_text[:100],
        )
        perf_mon.record_task("submit_code", elapsed, False)
        outcome = Outcome(
            status=status,
            result={
                "success": False, "message": result_text[:100],
                "account_limit": True, "is_account_blocked": False, "is_wrong_code": False,
            },
        )

    elif status in (ResultStatus.SUCCESS_POINTS, ResultStatus.SUCCESS_NO_POINTS):
        has_points = status == ResultStatus.SUCCESS_POINTS
        logger.info(f"✅ [{user}|{domain}] SUCCESS code={code} ({elapsed:.2f}s) — {result_text[:60]}")
        update_dashboard(
            domain=domain, account=user, code=code, status="THÀNH CÔNG",
            rtt_ms=elapsed * 1000, raw_response=result_text,
        )
        _safe_append_history(
            callbacks, event_type="RESULT", code=code, target_url=target_url,
            account=user, status="SUCCESS", submit_elapsed=elapsed, message=result_text[:100],
        )
        await loop.run_in_executor(
            db_executor, db.record_submission, code, user, target_url, "SUCCESS", result_text[:100],
        )
        perf_mon.record_task("submit_code", elapsed, True)
        outcome = Outcome(
            status=status,
            result={"success": True, "has_points": has_points, "message": result_text[:100]},
        )

    elif status == ResultStatus.NO_RESULT:
        info = debug_info or await _build_no_result_debug_info(
            page, code, user, domain, clicked, pre_click_text, result_timeout_s, is_hi88,
        )
        screenshot = await take_screenshot_fn(
            page, user, code, target_url, "UNKNOWN", debug_info=info,
        )
        logger.warning(f"⚠️ [{user}] NO RESULT after {elapsed:.2f}s")
        update_dashboard(
            domain=domain, account=user, code=code, status="UNKNOWN",
            rtt_ms=elapsed * 1000, raw_response="No popup",
        )
        _safe_append_history(
            callbacks, event_type="RESULT", code=code, target_url=target_url,
            account=user, status="UNKNOWN", submit_elapsed=elapsed, message="No popup",
            screenshot=screenshot,
        )
        await loop.run_in_executor(
            db_executor, db.record_submission, code, user, target_url, "UNKNOWN", "No popup",
        )
        perf_mon.record_task("submit_code", elapsed, False)
        outcome = Outcome(status=status, result={
            "success": False, "message": "No popup", "status": "NO_RESULT",
            "failure_kind": "UNKNOWN_AFTER_SUBMIT",
        })

    elif status == ResultStatus.CAPTCHA_EXPIRED:

        logger.warning(
            f"⌛ [{user}|{domain}] CAPTCHA/Turnstile hết hạn ({elapsed:.2f}s) — "
            f"KHÔNG huỷ code: {result_text[:60]}"
        )
        update_dashboard(
            domain=domain, account=user, code=code, status="CAPTCHA_EXPIRED",
            rtt_ms=elapsed * 1000, raw_response=result_text,
        )
        _safe_append_history(
            callbacks, event_type="RESULT", code=code, target_url=target_url,
            account=user, status="CAPTCHA_EXPIRED", submit_elapsed=elapsed,
            message=result_text[:100], screenshot="",
        )
        await loop.run_in_executor(
            db_executor, db.record_submission, code, user, target_url, "UNKNOWN", result_text[:100],
        )
        perf_mon.record_task("submit_code", elapsed, False)
        outcome = Outcome(
            status=status,
            result={
                "success": False, "message": result_text[:100],
                "is_wrong_code": False, "failure_kind": "CAPTCHA_EXPIRED",
            },
        )

    elif status == ResultStatus.CONNECTIVITY_ERROR:

        logger.warning(
            "🌐 [%s|%s] SITE_CONNECTION_ERROR (%.2fs) — "
            "chưa xác nhận kết quả server; KHÔNG submit lại tự động: %s",
            domain, user, elapsed, result_text[:120],
        )
        update_dashboard(
            domain=domain, account=user, code=code, status="LỖI KẾT NỐI",
            rtt_ms=elapsed * 1000, raw_response=result_text,
        )
        _safe_append_history(
            callbacks, event_type="RESULT", code=code, target_url=target_url,
            account=user, status="NETWORK_UNCONFIRMED", submit_elapsed=elapsed,
            message=result_text[:100],
        )
        await loop.run_in_executor(
            db_executor, db.record_submission,
            code, user, target_url, "UNKNOWN", result_text[:100],
        )
        perf_mon.record_task("submit_code", elapsed, False)
        outcome = Outcome(
            status=status,
            result={
                "success": False, "message": result_text[:100],
                "status": "AMBIGUOUS", "failure_kind": "UNKNOWN_AFTER_SUBMIT",
                "is_wrong_code": False, "network_error": True,
            },
        )

    elif status == ResultStatus.FAILED:

        try:
            from config import Config
            capture_failed = bool(getattr(Config, "SCREENSHOT_ON_FAILED", False))
        except Exception:
            capture_failed = False
        screenshot = (
            await take_screenshot_fn(page, user, code, target_url, "FAILED")
            if capture_failed else ""
        )
        logger.warning(f"❌ [{user}|{domain}] FAILED code={code} ({elapsed:.2f}s) — {result_text[:60]}")
        update_dashboard(
            domain=domain, account=user, code=code, status="THẤT BẠI",
            rtt_ms=elapsed * 1000, raw_response=result_text,
        )
        _safe_append_history(
            callbacks, event_type="RESULT", code=code, target_url=target_url,
            account=user, status="FAILED", submit_elapsed=elapsed, message=result_text[:100],
            screenshot=screenshot,
        )
        await loop.run_in_executor(
            db_executor, db.record_submission, code, user, target_url, "FAILED", result_text[:100],
        )
        perf_mon.record_task("submit_code", elapsed, False)
        outcome = Outcome(
            status=status,
            result={"success": False, "message": result_text[:100], "is_wrong_code": True},
        )

    else:

        screenshot = await take_screenshot_fn(page, user, code, target_url, "AMBIGUOUS")
        logger.warning(
            f"❓ [{user}|{domain}] Kết quả MƠ HỒ code={code} ({elapsed:.2f}s), không rõ đúng/sai — "
            f"KHÔNG huỷ code, để retry: {result_text[:80]}"
        )
        update_dashboard(
            domain=domain, account=user, code=code, status="UNKNOWN",
            rtt_ms=elapsed * 1000, raw_response=result_text,
        )
        _safe_append_history(
            callbacks, event_type="RESULT", code=code, target_url=target_url,
            account=user, status="AMBIGUOUS", submit_elapsed=elapsed, message=result_text[:100],
            screenshot=screenshot,
        )
        await loop.run_in_executor(
            db_executor, db.record_submission, code, user, target_url, "UNKNOWN", result_text[:100],
        )
        perf_mon.record_task("submit_code", elapsed, False)
        outcome = Outcome(
            status=ResultStatus.AMBIGUOUS,
            result={
                "success": False, "message": result_text[:100],
                "is_wrong_code": False, "status": "AMBIGUOUS",
                "failure_kind": "UNKNOWN_AFTER_SUBMIT",
            },
        )

    clean_page_fn = (callbacks or {}).get("clean_page")
    if clean_page_fn is not None:
        try:
            await clean_page_fn(page, domain, target_url, key)
        except Exception as e:
            logger.debug(f"⚠️ [{key}] clean_page callback lỗi (bỏ qua, không ảnh hưởng kết quả): {e}")

    return outcome
