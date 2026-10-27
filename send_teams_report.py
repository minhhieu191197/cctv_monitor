#!/usr/bin/env python3
"""Send confirmed Camera and HDD errors to Teams; omit unverified states."""
import csv, json, os, sys, urllib.request
from pathlib import Path
from datetime import datetime

HERE = Path(__file__).resolve().parent
REPORTS = HERE / "reports"
MAX_LINES = 18
MAX_CHARS = 6500


def norm(value):
    return (value or "").strip().casefold()


def getval(row, *names):
    lookup = {norm(k): (v or "").strip() for k, v in row.items()}
    for name in names:
        if norm(name) in lookup:
            return lookup[norm(name)]
    return ""


def _send_payload(url, title, lines, suffix=""):
    card = {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard",
                "version": "1.2",
                "body": [
                    {"type": "TextBlock", "text": title + suffix,
                     "weight": "Bolder", "size": "Medium", "wrap": True},
                    {"type": "TextBlock", "text": "\n".join(lines), "wrap": True}
                ]
            }
        }]
    }
    data = json.dumps(card, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data,
        headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
    with urllib.request.urlopen(req, timeout=20) as resp:
        if not 200 <= resp.status < 300:
            raise RuntimeError(f"Teams webhook HTTP {resp.status}")
    print(f"Teams sent: {title}{suffix}; HTTP {resp.status}; payload={len(data)} bytes")


def post_card(url, title, lines):
    # Try one message first. If the webhook rejects it, automatically fall back
    # to the previous 18-line chunks so the daily report is still delivered.
    try:
        _send_payload(url, title, lines)
        print(f"Single-message test passed for: {title}")
        return
    except Exception as exc:
        print(f"Single-message attempt failed for {title}: {type(exc).__name__}: {exc}")
        print("Falling back to the previous split-message format.")

    chunks, current = [], []
    for line in lines:
        candidate = current + [line]
        if current and (len(candidate) > MAX_LINES or len("\n".join(candidate)) > MAX_CHARS):
            chunks.append(current)
            current = [line]
        else:
            current = candidate
    if current or not chunks:
        chunks.append(current)
    for idx, chunk in enumerate(chunks, 1):
        suffix = f" ({idx}/{len(chunks)})" if len(chunks) > 1 else ""
        _send_payload(url, title, chunk, suffix)


def main():
    url = os.environ.get("KBV_TEAMS_WEBHOOK", "").strip()
    if not url:
        print("ERROR: KBV_TEAMS_WEBHOOK environment variable is not set.")
        return 2

    files = sorted(REPORTS.glob("kbvision_report_*.csv"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        print("ERROR: No kbvision_report_*.csv found in reports.")
        return 2
    report = files[0]
    with report.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        print(f"ERROR: CSV has no data rows: {report.name}")
        return 2

    total_nvr = len(rows)
    configured = online = offline = 0
    camera_issues, hdd_issues = [], []
    hdd_errors = 0

    # Only explicit, confirmed error states are included in issue lists.
    confirmed_hdd_errors = {"lỗi hdd", "hdd lỗi", "hdd1 lỗi", "hdd2 lỗi",
                            "error", "failed", "fault", "abnormal"}
    ignored_hdd_states = {"", "bình thường", "normal", "ok", "chưa xác minh",
                          "không phát hiện", "không lắp", "unknown", "not detected",
                          "not installed", "n/a", "na"}

    for row in rows:
        store = getval(row, "Cửa hàng/đầu ghi", "cua_hang_dau_ghi") or "NVR chưa rõ"
        ip = getval(row, "IP đầu ghi", "ip_dau_ghi")
        label = f"{store} ({ip})" if ip else store

        total = getval(row, "Tổng camera", "tong_camera")
        on = getval(row, "Online", "camera_online")
        off = getval(row, "Offline", "camera_offline")
        try:
            if total and on and off:
                configured += int(float(total))
                online += int(float(on))
                row_offline = int(float(off))
                offline += row_offline
                if row_offline > 0:
                    camera_issues.append(f"• {label}: {row_offline} camera offline")
        except (ValueError, TypeError):
            pass  # Missing camera metrics are not reported as confirmed camera errors.

        issue = getval(row, "Cần xử lý", "ket_qua_can_xu_ly")
        issue_n = norm(issue)
        if issue_n and issue_n != "ok":
            # Explicit connection loss is actionable; don't repeat offline issues already listed.
            if "mất kết nối đầu ghi" in issue_n or "mat ket noi dau ghi" in issue_n:
                if not any(line.startswith(f"• {label}:") for line in camera_issues):
                    camera_issues.append(f"• {label}: MẤT KẾT NỐI ĐẦU GHI")
            elif "cam offline" in issue_n or "camera offline" in issue_n:
                if not any(line.startswith(f"• {label}:") for line in camera_issues):
                    camera_issues.append(f"• {label}: {issue}")
            elif "hdd" in issue_n and any(word in issue_n for word in ("lỗi", "loi", "error", "failed", "fault")):
                # HDD errors belong only in the HDD report.
                pass
            # Other statuses (including unknown/unverified) are intentionally omitted.

        for key, value in row.items():
            k, v = norm(key), norm(value)
            if "hdd" not in k or not ("trạng thái" in k or "trang_thai" in k):
                continue
            if v in confirmed_hdd_errors:
                hdd_errors += 1
                disk = key.replace(" trạng thái", "").replace("_trạng_thái", "").replace("_trang_thai", "").strip()
                hdd_issues.append(f"• {label}: {disk} báo LỖI HDD")
            elif v in ignored_hdd_states:
                continue
            # Any other state is omitted unless it is an explicit recognized error.

    now = datetime.now().astimezone()
    percent = online / configured * 100 if configured else 0
    camera_lines = [
        f"Kiểm tra: {now:%d/%m/%Y %H:%M}",
        f"Đầu ghi trong báo cáo: {total_nvr}",
        f"Camera online: {online}/{configured} ({percent:.1f}%)",
        f"Camera offline đã xác nhận: {offline}",
        f"CSV lưu cục bộ: {report.name}", "",
        "DANH SÁCH LỖI CAMERA ĐÃ XÁC NHẬN"
    ] + (camera_issues or ["Không phát hiện lỗi camera đã xác nhận."])

    hdd_lines = [
        f"Kiểm tra: {now:%d/%m/%Y %H:%M}",
        f"Đầu ghi trong báo cáo: {total_nvr}",
        f"HDD lỗi đã xác nhận qua API: {hdd_errors}",
        f"CSV lưu cục bộ: {report.name}", "",
        "DANH SÁCH LỖI HDD ĐÃ XÁC NHẬN"
    ] + (hdd_issues or ["Không phát hiện lỗi HDD đã xác nhận."])

    try:
        post_card(url, f"KBVISION — CAMERA ({offline} offline đã xác nhận)", camera_lines)
        post_card(url, f"KBVISION — HDD ({hdd_errors} lỗi đã xác nhận)", hdd_lines)
    except Exception as exc:
        print(f"ERROR: Teams send failed: {type(exc).__name__}: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
