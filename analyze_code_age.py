"""Phân tích log: tuổi mã vs tỷ lệ thành công, và nhịp xuất hiện 429.

Dùng:  python analyze_code_age.py logs/bot_activity.log [logs/other.log ...]
Đọc các dòng "[CODE-AGE]" (tuổi mã lúc bắt đầu nhập + kết quả cuối) và
"[Submit-Pacer]" (số mã đã gửi kể từ lần throttle trước). Chỉ đọc, không sửa gì.
"""
import re
import sys
from collections import defaultdict

AGE_RE = re.compile(
    r"\[CODE-AGE\] domain=(?P<domain>\S+)(?: account=\S+)? code=\S+ "
    r"age_at_dispatch=(?P<age>[\d.?]+)s submit=(?P<submit>[\d.]+)s "
    r"outcome=(?P<outcome>\w+) max_age=(?P<max>\d+)s"
)
THROTTLE_RE = re.compile(
    r"\[Submit-Pacer\] domain=(?P<domain>\S+) .*?cooldown=(?P<cd>[\d.]+)s.*?"
    r"sent_since_last_throttle=(?P<sent>\d+) since_last_throttle=(?P<gap>\S+)"
)
SUCCESS = {"SUCCESS_POINTS", "SUCCESS_NO_POINTS"}
BUCKETS = [(0, 5), (5, 10), (10, 20), (20, 30), (30, 40), (40, 60), (60, 120), (120, 10**9)]


def bucket_of(age):
    for lo, hi in BUCKETS:
        if lo <= age < hi:
            return (lo, hi)
    return BUCKETS[-1]


def main(paths):
    ages = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))  # domain -> bucket -> outcome -> n
    unknown_age = defaultdict(int)
    dropped = defaultdict(int)
    throttles = defaultdict(list)
    for path in paths:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = AGE_RE.search(line)
                if m:
                    if m["age"] == "?":
                        unknown_age[m["domain"]] += 1
                        continue
                    if m["outcome"] == "EXPIRED_IN_QUEUE":
                        # Bị bỏ vì quá max_age trước khi nhập: luôn "không thành công" theo
                        # định nghĩa, nên đếm riêng để không làm méo tỷ lệ theo tuổi.
                        dropped[m["domain"]] += 1
                        continue
                    ages[m["domain"]][bucket_of(float(m["age"]))][m["outcome"]] += 1
                    continue
                t = THROTTLE_RE.search(line)
                if t:
                    throttles[t["domain"]].append((int(t["sent"]), t["gap"], float(t["cd"])))

    # PATCH_MARKER_2026-10-25-v8-bugfix (#1: log chi co EXPIRED_IN_QUEUE thi dropped co du lieu
    # nhung ages/throttles rong -> gate cu thoat sai. Them dropped va
    # unknown_age vao gate, va dua unknown_age vao vong in.)
    if not ages and not throttles and not dropped and not unknown_age:
        print("Không thấy dòng [CODE-AGE] / [Submit-Pacer] — chạy bot một thời gian rồi thử lại.")
        return
    for domain in sorted(set(ages) | set(dropped) | set(unknown_age)):
        per_bucket = ages.get(domain, {})
        print(f"\n== {domain}: tuổi mã lúc bắt đầu nhập vs kết quả ==")
        print(f"{'tuổi (s)':>10} {'số mã':>7} {'thành công':>11} {'tỷ lệ':>7}   kết quả khác")
        for b in BUCKETS:
            if b not in per_bucket:
                continue
            outs = per_bucket[b]
            total = sum(outs.values())
            ok = sum(n for o, n in outs.items() if o in SUCCESS)
            others = ", ".join(f"{o}={n}" for o, n in sorted(outs.items()) if o not in SUCCESS)
            hi = "∞" if b[1] >= 10**9 else str(b[1])
            print(f"{b[0]:>5}-{hi:<4} {total:>7} {ok:>11} {ok / total:>6.0%}   {others}")
        if unknown_age[domain]:
            print(f"(bỏ qua {unknown_age[domain]} mã không có mốc thời gian)")
        if dropped[domain]:
            print(f"({dropped[domain]} mã bị bỏ vì quá max_age trước khi nhập — không tính vào bảng trên)")
    for domain, items in sorted(throttles.items()):
        print(f"\n== {domain}: {len(items)} lần bị throttle (429) ==")
        for sent, gap, cd in items:
            print(f"  gửi {sent:>3} mã kể từ lần trước | cách lần trước {gap:>7} | cooldown {cd:.1f}s")
        print("  Lưu ý: sent=0 thường là nhiều request bay cùng lúc cùng nhận 429, không phải nhiều đợt riêng.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    main(sys.argv[1:])
