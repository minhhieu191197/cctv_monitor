#!/usr/bin/env python3
# KBVision/Dahua-compatible NVR monitor V20.
# Reports camera online/offline totals and only the selected physical-HDD fields.
# HDD data reuses the proven StorageDeviceManager.getDeviceInfos RPC from the record sample;
# recording-range metadata from that sample is intentionally not used.

import csv
import hashlib
import html
import json
import os
import re
import smtplib
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

import requests
from requests.auth import HTTPDigestAuth

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
DEVICES_PATH = HERE / "devices.csv"
PASSWORD_STORE_PATH = HERE / "nvr_password.dpapi"
XML_DEVICE_CANDIDATES = (HERE / "device.xml", HERE / "devices.xml")
OUT_DIR = HERE / "reports"
OUT_DIR.mkdir(exist_ok=True)

HEALTHY_STATES = {
    "running", "normal", "success", "read/write", "readwrite", "ready",
    "working", "online", "ok", "healthy", "in use", "inuse",
}
ERROR_STATES = {
    "error", "failed", "failure", "fault", "abnormal", "offline", "bad",
    "unhealthy", "not working", "notworking", "not exist", "missing",
}


def md5_upper(value: str) -> str:
    return hashlib.md5(value.encode("utf-8")).hexdigest().upper()


def post_json(session, url, payload, timeout):
    response = session.post(url, json=payload, timeout=timeout)
    response.raise_for_status()
    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError(f"RPC trả về dữ liệu không phải JSON (HTTP {response.status_code}).") from exc


def login_rpc(base_url, username, password, timeout):
    """Dahua-compatible RPC2 challenge login; never log credentials/session values."""
    session = requests.Session()
    session.headers.update({"Content-Type": "application/json"})
    login_url = base_url.rstrip("/") + "/RPC2_Login"

    challenge = post_json(session, login_url, {
        "method": "global.login",
        "params": {"userName": username, "password": "", "clientType": "Web3.0"},
        "id": 1,
    }, timeout)

    params = challenge.get("params") or {}
    realm = params.get("realm")
    random_value = params.get("random")
    temporary_session = challenge.get("session")
    encryption = params.get("encryption", "Default")
    if not realm or not random_value or not temporary_session:
        session.close()
        raise RuntimeError("RPC login không trả đủ realm/random/session.")
    if encryption not in ("Default", None, ""):
        session.close()
        raise RuntimeError(f"RPC dùng kiểu mã hóa chưa hỗ trợ: {encryption!r}.")

    first_hash = md5_upper(f"{username}:{realm}:{password}")
    response_hash = md5_upper(f"{username}:{random_value}:{first_hash}")
    login = post_json(session, login_url, {
        "method": "global.login",
        "params": {
            "userName": username,
            "password": response_hash,
            "clientType": "Web3.0",
            "authorityType": "Default",
            "passwordType": "Default",
        },
        "id": 2,
        "session": temporary_session,
    }, timeout)

    if login.get("result") is not True or not login.get("session"):
        session.close()
        error = (login.get("error") or {}).get("message", "không rõ nguyên nhân")
        raise RuntimeError(f"Đầu ghi từ chối đăng nhập RPC: {error}")
    return session, login["session"]


def rpc_call(session, base_url, session_id, method, params, request_id, timeout):
    return post_json(session, base_url.rstrip("/") + "/RPC2", {
        "method": method,
        "params": params,
        "id": request_id,
        "session": session_id,
    }, timeout)


def read_camera_states(session, base_url, session_id, timeout):
    response = rpc_call(
        session, base_url, session_id,
        "LogicDeviceManager.getCameraState",
        {"uniqueChannels": [-1]}, 3, timeout,
    )
    if response.get("result") is not True:
        error = (response.get("error") or {}).get("message", "không rõ nguyên nhân")
        raise RuntimeError(f"Không đọc được trạng thái camera: {error}")
    states = (response.get("params") or {}).get("states")
    if not isinstance(states, list):
        raise RuntimeError("Phản hồi trạng thái camera không có danh sách states.")
    return states


def read_configured_camera_channels(ip, port, scheme, username, password, timeout):
    """Use RemoteDevice config as the source of truth for configured IP cameras."""
    session = requests.Session()
    session.auth = HTTPDigestAuth(username, password)
    session.headers.update({"User-Agent": "KBVision-Monitor/20"})
    url = f"{scheme}://{ip}:{port}/cgi-bin/configManager.cgi"
    try:
        response = session.get(url, params={"action": "getConfig", "name": "RemoteDevice"}, timeout=timeout)
        response.raise_for_status()
        text = response.text
        if not text.strip() or text.strip().startswith("Error"):
            raise RuntimeError("RemoteDevice không trả cấu hình hợp lệ")
        fields = {}
        for line in text.splitlines():
            m = re.match(r"(?:table\.)?RemoteDevice\[(\d+)\]\.(Enable|Address|IP|Name)=(.*)", line.strip(), re.I)
            if m:
                idx, key, value = int(m.group(1)), m.group(2).lower(), m.group(3).strip()
                fields.setdefault(idx, {})[key] = value
        if not fields:
            # Some firmware uses INFO_n rather than table.RemoteDevice[n].
            for idx in set(re.findall(r"INFO_(\d+)\.(?:Address|Enable)=", text, re.I)):
                block = fields.setdefault(int(idx), {})
                for key in ("Address", "Enable", "IP", "Name"):
                    m = re.search(rf"INFO_{idx}\.{key}=(.*)", text, re.I)
                    if m:
                        block[key.lower()] = m.group(1).strip()
        if not fields:
            raise RuntimeError("Không tìm thấy danh sách RemoteDevice trong cấu hình")
        configured = set()
        for idx, item in fields.items():
            enabled = item.get("enable", "true").lower() == "true"
            address = item.get("address", item.get("ip", "")).strip()
            if enabled and address and address not in ("192.168.0.0", "0.0.0.0", "-"):
                # RemoteDevice indexes and RPC channel identifiers are zero-based on this NVR family.
                configured.add(idx)
        return configured
    finally:
        session.close()


def normalize_disk(disk):
    """Normalize one physical HDD object from StorageDeviceManager.getDeviceInfos."""
    capacity_raw = disk.get("Capacity")
    try:
        capacity_bytes = float(capacity_raw) if capacity_raw not in (None, "") else 0.0
    except (TypeError, ValueError):
        capacity_bytes = 0.0
    state = str(disk.get("State") or "").strip()
    name = str(disk.get("Name") or "").strip()
    model = str(disk.get("Module") or "").strip()

    # Treat explicit health/error flags as authoritative when firmware provides them.
    explicit_error = False
    for key in ("IsError", "Error", "HasError", "Fault"):
        value = disk.get(key)
        if isinstance(value, bool) and value:
            explicit_error = True
        elif isinstance(value, (int, float)) and value == 1:
            explicit_error = True
        elif isinstance(value, str) and value.strip().lower() in ("true", "yes", "1", "error", "failed"):
            explicit_error = True

    state_key = state.casefold().replace("_", " ").strip()
    health_key = str(disk.get("Health") or disk.get("HealthStatus") or disk.get("SmartStatus") or "").casefold().strip()
    if explicit_error or state_key in ERROR_STATES or health_key in ERROR_STATES:
        status = "LỖI HDD"
    elif state_key in HEALTHY_STATES and (not health_key or health_key in HEALTHY_STATES):
        status = "BÌNH THƯỜNG"
    elif health_key in ERROR_STATES:
        status = "LỖI HDD"
    else:
        status = "CHƯA XÁC MINH"

    return {
        "name": name,
        "model": model,
        "capacity_bytes": capacity_bytes,
        "capacity_tb": round(capacity_bytes / 1_000_000_000_000, 3) if capacity_bytes > 0 else None,
        "state": state,
        "status": status,
    }


def read_physical_hdds(session, base_url, session_id, timeout):
    """Use the working sample's RPC method/volume, not CGI partition totals."""
    response = rpc_call(
        session, base_url, session_id,
        "StorageDeviceManager.getDeviceInfos",
        {"volume": "InvidualPhysicalVolume"}, 5, timeout,
    )
    if response.get("result") is not True:
        error = (response.get("error") or {}).get("message", "không rõ nguyên nhân")
        raise RuntimeError(f"StorageDeviceManager.getDeviceInfos thất bại: {error}")
    raw_devices = (response.get("params") or {}).get("device")
    if raw_devices is None:
        raise RuntimeError("RPC HDD không trả về params.device; không thể xác minh ổ cứng.")
    if isinstance(raw_devices, dict):
        raw_devices = [raw_devices]
    # Optional diagnostic mode: log only the HDD API payload (no credentials/session IDs).
    # Enable with PowerShell: $env:KBV_DEBUG_HDD="1"
    if os.environ.get("KBV_DEBUG_HDD", "").strip() == "1":
        print("\n[HDD DEBUG] StorageDeviceManager.getDeviceInfos raw params.device:")
        print(json.dumps(raw_devices, ensure_ascii=False, indent=2, default=str))
    if not isinstance(raw_devices, list):
        raise RuntimeError("RPC HDD trả về params.device sai định dạng.")

    disks = [normalize_disk(item) for item in raw_devices if isinstance(item, dict)]
    # Sort by device name when available, so HDD 1/2 ordering remains stable.
    disks.sort(key=lambda d: (not bool(d["name"]), d["name"].casefold()))
    return disks


def probe_device(device, timeout):
    name = (device.get("name") or device.get("ip") or "Unknown NVR").strip()
    ip = (device.get("ip") or "").strip()
    port = (device.get("port") or "80").strip()
    scheme = (device.get("scheme") or "http").strip()
    username = (device.get("username") or "").strip()
    password = device.get("password") or ""
    base_url = f"{scheme}://{ip}:{port}"

    session, session_id = login_rpc(base_url, username, password, timeout)
    try:
        states = read_camera_states(session, base_url, session_id, timeout)
        # Get configured camera slots separately; never infer configuration from max channels.
        try:
            configured_channels = read_configured_camera_channels(
                ip, port, scheme, username, password, timeout
            )
            camera_config_ok = True
            camera_config_error = ""
        except Exception as exc:
            configured_channels = set()
            camera_config_ok = False
            camera_config_error = str(exc)[:300]
        # HDD query failure must not throw away valid camera results.
        try:
            disks = read_physical_hdds(session, base_url, session_id, timeout)
            hdd_error = ""
            hdd_query_ok = True
        except Exception as exc:
            disks = []
            hdd_error = str(exc)[:300]
            hdd_query_ok = False
        return {
            "name": name,
            "ip": ip,
            "reachable": True,
            "states": states,
            "configured_channels": configured_channels,
            "camera_config_ok": camera_config_ok,
            "camera_config_error": camera_config_error,
            "disks": disks,
            "hdd_query_ok": hdd_query_ok,
            "hdd_error": hdd_error,
            "error": "",
        }
    finally:
        try:
            rpc_call(session, base_url, session_id, "global.logout", None, 99, timeout)
        except Exception:
            pass
        session.close()


def camera_summary(states, configured_channels):
    """Count only slots enabled and addressed in RemoteDevice configuration."""
    by_channel = {}
    for item in states:
        try:
            channel = int(item.get("channel"))
        except (ValueError, TypeError):
            continue
        by_channel[channel] = str(item.get("connectionState") or "").strip()
    installed = []
    for channel in sorted(configured_channels):
        state = by_channel.get(channel, "")
        channel_number = channel + 1
        installed.append({"channel_number": channel_number, "state": state})
    online = sum(1 for item in installed if item["state"].casefold() == "connected")
    offline_items = [item for item in installed if item["state"].casefold() != "connected"]
    offline_channels = [item["channel_number"] for item in offline_items]
    return len(installed), online, len(offline_items), offline_channels


def compact_channels(channels):
    """Compact a channel list such as [7, 8, 9, 10] to CH7–CH10."""
    nums = sorted({int(n) for n in channels if n is not None})
    if not nums:
        return "chưa xác định kênh"
    ranges = []
    start = prev = nums[0]
    for number in nums[1:]:
        if number == prev + 1:
            prev = number
            continue
        ranges.append((start, prev))
        start = prev = number
    ranges.append((start, prev))
    parts = []
    for first, last in ranges:
        if first == last:
            parts.append(f"CH{first}")
        elif last == first + 1:
            parts.extend([f"CH{first}", f"CH{last}"])
        else:
            parts.append(f"CH{first}–CH{last}")
    return ", ".join(parts)


def disk_capacity_text(disk):
    if not disk or not disk.get("capacity_tb"):
        return "CHƯA XÁC MINH"
    return f"{disk['capacity_tb']:.3f} TB"


def make_report_row(result, checked_at, max_disks):
    """One compact row per NVR; no recording-range fields from the record sample."""
    row = {
        "thoi_gian_kiem_tra": checked_at,
        "cua_hang_dau_ghi": result["name"],
        "ip_dau_ghi": result["ip"],
        "tong_camera": "",
        "camera_online": "",
        "camera_offline": "",
    }
    for i in range(1, max_disks + 1):
        row[f"hdd{i}_dung_luong"] = "CHƯA XÁC MINH"
        row[f"hdd{i}_trang_thai"] = "CHƯA XÁC MINH"

    actions = []
    if not result["reachable"]:
        row["ket_qua_can_xu_ly"] = "MẤT KẾT NỐI ĐẦU GHI"
        return row

    if not result.get("camera_config_ok", False):
        row["tong_camera"] = "CHƯA XÁC MINH"
        row["camera_online"] = "CHƯA XÁC MINH"
        row["camera_offline"] = "CHƯA XÁC MINH"
        actions.append("CAM CHƯA XÁC MINH")
    else:
        total, online, offline, offline_channels = camera_summary(
            result["states"], result["configured_channels"]
        )
        row["tong_camera"] = total
        row["camera_online"] = online
        row["camera_offline"] = offline
        if offline:
            channel_text = compact_channels(offline_channels)
            actions.append(f"CAM OFFLINE: {channel_text}")

    disks = result["disks"]
    if not result["hdd_query_ok"]:
        actions.append("HDD CHƯA XÁC MINH")
    elif not disks:
        # A successful query with zero physical disks is a real attention item.
        for i in range(1, max_disks + 1):
            row[f"hdd{i}_dung_luong"] = "KHÔNG PHÁT HIỆN"
            row[f"hdd{i}_trang_thai"] = "KHÔNG PHÁT HIỆN"
        actions.append("HDD KHÔNG PHÁT HIỆN")
    else:
        for i in range(1, max_disks + 1):
            if i <= len(disks):
                disk = disks[i - 1]
                row[f"hdd{i}_dung_luong"] = disk_capacity_text(disk)
                row[f"hdd{i}_trang_thai"] = disk["status"]
                disk_label = disk.get("name") or f"HDD{i}"
                if disk["status"] == "LỖI HDD":
                    actions.append(f"HDD{i} LỖI")
                elif disk["status"] == "CHƯA XÁC MINH":
                    actions.append(f"HDD{i} CHƯA XÁC MINH")
            else:
                # Some NVRs legitimately have only one installed HDD. Do not flag
                # the unpopulated second bay as a failure just because it is absent.
                row[f"hdd{i}_dung_luong"] = "KHÔNG LẮP"
                row[f"hdd{i}_trang_thai"] = "KHÔNG LẮP"

    row["ket_qua_can_xu_ly"] = "; ".join(actions) if actions else "OK"
    return row


def html_escape(value):
    return html.escape(str(value), quote=True)


def send_email(cfg, html_body, csv_path, html_path, now):
    email_cfg = cfg["email"]
    password_env = email_cfg.get("password_env", "KBV_SMTP_PASSWORD")
    smtp_password = os.environ.get(password_env, "")
    if not smtp_password:
        print(f"Email chưa gửi: biến môi trường {password_env} chưa được cấu hình.")
        return
    message = EmailMessage()
    message["Subject"] = f"{email_cfg.get('subject_prefix', 'KBVision Camera Report')} - {now.strftime('%Y-%m-%d')}"
    message["From"] = email_cfg["sender"]
    message["To"] = ", ".join(email_cfg["recipients"])
    message.set_content("KBVision: báo cáo camera/HDD. Xem bảng HTML và CSV đính kèm.")
    message.add_alternative(html_body, subtype="html")
    for path in (csv_path, html_path):
        maintype, subtype = ("text", "csv") if path.suffix.lower() == ".csv" else ("text", "html")
        message.add_attachment(path.read_bytes(), maintype=maintype, subtype=subtype, filename=path.name)
    try:
        with smtplib.SMTP(email_cfg.get("server", "smtp.office365.com"), int(email_cfg.get("port", 587)), timeout=20) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            smtp.login(email_cfg["sender"], smtp_password)
            smtp.send_message(message)
        print(f"Đã gửi email tới {', '.join(email_cfg['recipients'])}")
    except Exception as exc:
        print(f"Gửi email thất bại: {exc}")
        print("Nếu SMTP AUTH bị tắt, cần dùng Power Automate hoặc Microsoft Graph.")



def load_common_password(cfg):
    """Load a shared password protected with Windows DPAPI; never prompt here."""
    configured_path = cfg.get("password_store_file", "nvr_password.dpapi")
    password_path = (HERE / configured_path).resolve()
    if not password_path.exists():
        raise RuntimeError(
            f"Chưa có kho mật khẩu mã hóa ({password_path.name}). "
            "Chạy py setup_password.py một lần để lưu mật khẩu chung; "
            "monitor.py sẽ không hỏi lại khi chạy hằng ngày."
        )
    try:
        from credential_store import unprotect_password
        value = unprotect_password(password_path.read_bytes())
    except Exception as exc:
        raise RuntimeError(
            "Không giải mã được kho mật khẩu Windows DPAPI. "
            "Hãy chạy setup_password.py dưới đúng tài khoản Windows đang chạy monitor.py."
        ) from exc
    if not value:
        raise RuntimeError("Kho mật khẩu trống; chạy setup_password.py để lưu lại.")
    return value


def _enabled(value):
    return (value or "1").strip().lower() not in ("0", "false", "no", "off")


def load_devices(cfg):
    """Load NVR inventory from KBiVMS XML (preferred) or CSV fallback.

    KBiVMS XML password attributes are app-encrypted blobs and are NOT passed as
    RPC passwords. The common API password comes from a Windows-DPAPI vault.
    XML port (usually 8888) is not the web/RPC HTTP port; use config http_port.
    """
    source = str(cfg.get("device_source", "auto")).strip().lower()
    xml_path = next((p for p in XML_DEVICE_CANDIDATES if p.exists()), None)
    if source not in ("auto", "xml", "csv"):
        raise RuntimeError("config.device_source chỉ nhận auto, xml hoặc csv.")
    use_xml = source == "xml" or (source == "auto" and xml_path is not None)
    if use_xml and xml_path is None:
        raise RuntimeError("Không tìm thấy device.xml hoặc devices.xml trong thư mục chương trình.")

    if use_xml:
        try:
            xml_root = ET.parse(xml_path).getroot()
        except Exception as exc:
            raise RuntimeError(f"Không đọc được {xml_path.name}: {exc}") from exc
        test_names = cfg.get("test_device_names", [])
        allowed = {str(n).strip().casefold() for n in test_names if str(n).strip()}
        devices = []
        xml_names = set()
        for item in xml_root.iter("Device"):
            name = (item.attrib.get("name") or "").strip()
            ip = (item.attrib.get("domain") or item.attrib.get("ip") or "").strip()
            username = (item.attrib.get("username") or cfg.get("default_username", "admin")).strip()
            if not name or not ip:
                continue
            xml_names.add(name.casefold())
            if allowed and name.casefold() not in allowed:
                continue
            devices.append({
                "name": name,
                "ip": ip,
                "port": str(cfg.get("http_port", 80)),
                "scheme": str(cfg.get("http_scheme", "http")),
                "username": username or "admin",
                # The XML password is KBiVMS-encrypted and is deliberately not
                # treated as plaintext or sent to the NVR API.
                "password": "",
                "enabled": "1",
            })
        if allowed:
            missing = sorted(allowed - xml_names)
            if missing:
                print("Cảnh báo: không thấy các đầu ghi trong device.xml: " + ", ".join(missing))
        if not devices:
            if allowed:
                raise RuntimeError("Không khớp đầu ghi nào trong test_device_names với device.xml.")
            raise RuntimeError(f"Không tìm thấy mục Device hợp lệ trong {xml_path.name}.")
        print(f"Nguồn thiết bị: {xml_path.name} ({len(devices)} đầu ghi được chọn).")
    else:
        if not DEVICES_PATH.exists():
            raise RuntimeError("Không tìm thấy device.xml/devices.xml hoặc devices.csv.")
        with DEVICES_PATH.open("r", newline="", encoding="utf-8-sig") as file:
            devices = [row for row in csv.DictReader(file) if _enabled(row.get("enabled", "1"))]
        if not devices:
            raise RuntimeError("Không có đầu ghi nào được bật trong devices.csv.")
        print(f"Nguồn thiết bị: devices.csv ({len(devices)} đầu ghi được chọn).")

    for index, device in enumerate(devices, start=1):
        device["name"] = (device.get("name") or "").strip()
        device["ip"] = (device.get("ip") or device.get("domain") or "").strip()
        if not device["name"]:
            raise RuntimeError(f"Đầu ghi thứ {index} không có tên.")
        if not device["ip"]:
            raise RuntimeError(f"Đầu ghi {device['name']} không có IP.")
        device["username"] = (device.get("username") or cfg.get("default_username", "admin")).strip() or "admin"
        device["scheme"] = (device.get("scheme") or cfg.get("http_scheme", "http")).strip() or "http"
        device["port"] = str(device.get("port") or cfg.get("http_port", 80)).strip()
        device["password"] = (device.get("password") or "").strip()

    # XML uses KBiVMS-encrypted password strings; they cannot be treated as the
    # plaintext password expected by the NVR RPC authentication. Use local vault.
    if any(not device["password"] for device in devices):
        common_password = load_common_password(cfg)
        for device in devices:
            if not device["password"]:
                device["password"] = common_password
    return devices

def main():
    if not CONFIG_PATH.exists():
        print("Thiếu config.json.")
        sys.exit(2)
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    timeout = int(cfg.get("timeout_seconds", 12))
    delay = float(cfg.get("delay_between_devices_seconds", 0.25))

    try:
        devices = load_devices(cfg)
    except Exception as exc:
        print(f"Không thể nạp cấu hình đầu ghi: {exc}")
        sys.exit(2)

    checked_at_dt = datetime.now().astimezone()
    checked_at = checked_at_dt.isoformat(timespec="seconds")
    stamp = checked_at_dt.strftime("%Y%m%d_%H%M%S")
    results = []

    for index, device in enumerate(devices, 1):
        label = device.get("name") or device.get("ip") or f"NVR-{index}"
        print(f"[{index}/{len(devices)}] Đang kiểm tra {label} ({device.get('ip', '')})...", flush=True)
        try:
            result = probe_device(device, timeout)
            if result.get("camera_config_ok"):
                total, online, offline, offline_channels = camera_summary(
                    result["states"], result["configured_channels"]
                )
                cam_msg = f"Camera cấu hình={total}, online={online}, offline={offline}"
            else:
                cam_msg = "Camera=CHƯA XÁC MINH (không đọc được RemoteDevice)"
            disk_msg = f"HDD vật lý={len(result['disks'])}" if result["hdd_query_ok"] else "HDD=CHƯA XÁC MINH"
            print(f"    {cam_msg}; {disk_msg}", flush=True)
        except Exception as exc:
            result = {
                "name": label,
                "ip": device.get("ip", ""),
                "reachable": False,
                "states": [],
                "configured_channels": set(),
                "camera_config_ok": False,
                "disks": [],
                "hdd_query_ok": False,
                "hdd_error": "",
                "error": str(exc)[:300],
            }
            print(f"    LỖI truy vấn đầu ghi: {exc}", flush=True)
        results.append(result)
        time.sleep(delay)

    # At least HDD1/HDD2 are always present in the report; add more columns if the
    # fleet includes recorders with more than two physical disks.
    max_disks = max([2] + [len(r.get("disks", [])) for r in results])
    report_rows = [make_report_row(result, checked_at, max_disks) for result in results]

    report_columns = [
        "thoi_gian_kiem_tra", "cua_hang_dau_ghi", "ip_dau_ghi",
        "tong_camera", "camera_online", "camera_offline",
    ]
    for i in range(1, max_disks + 1):
        report_columns.extend([f"hdd{i}_dung_luong", f"hdd{i}_trang_thai"])
    report_columns.append("ket_qua_can_xu_ly")

    report_path = OUT_DIR / f"kbvision_report_{stamp}.csv"
    header_labels = {
        "thoi_gian_kiem_tra": "Thời gian kiểm tra",
        "cua_hang_dau_ghi": "Cửa hàng/đầu ghi",
        "ip_dau_ghi": "IP đầu ghi",
        "tong_camera": "Tổng camera",
        "camera_online": "Online",
        "camera_offline": "Offline",
        "ket_qua_can_xu_ly": "Cần xử lý",
    }
    for i in range(1, max_disks + 1):
        header_labels[f"hdd{i}_dung_luong"] = f"HDD {i} dung lượng vật lý API (TB)"
        header_labels[f"hdd{i}_trang_thai"] = f"HDD {i} trạng thái"
    with report_path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.writer(file)
        writer.writerow([header_labels.get(key, key) for key in report_columns])
        for row in report_rows:
            writer.writerow([row.get(key, "") for key in report_columns])

    total_nvrs = len(report_rows)
    all_installed = sum(int(row["tong_camera"] or 0) for row in report_rows)
    all_online = sum(int(row["camera_online"] or 0) for row in report_rows)
    all_offline = sum(int(row["camera_offline"] or 0) for row in report_rows)
    all_disk_states = [row[f"hdd{i}_trang_thai"] for row in report_rows for i in range(1, max_disks + 1)]
    hdd_errors = sum(1 for state in all_disk_states if state == "LỖI HDD")
    hdd_unknown = sum(1 for state in all_disk_states if state in ("CHƯA XÁC MINH", "KHÔNG PHÁT HIỆN"))
    nvr_errors = sum(1 for result in results if not result["reachable"])
    problem_rows = [row for row in report_rows if row["ket_qua_can_xu_ly"] != "OK"]

    table_columns = [
        ("cua_hang_dau_ghi", "Cửa hàng"),
        ("ip_dau_ghi", "IP đầu ghi"),
        ("tong_camera", "Cam"),
        ("camera_online", "On"),
        ("camera_offline", "Off"),
    ]
    for i in range(1, max_disks + 1):
        table_columns.extend([
            (f"hdd{i}_dung_luong", f"HDD{i} API TB"),
            (f"hdd{i}_trang_thai", f"HDD{i} TT"),
        ])
    table_columns.append(("ket_qua_can_xu_ly", "Cần xử lý"))
    html_header = "".join(f"<th>{html_escape(label)}</th>" for _, label in table_columns)
    if problem_rows:
        html_rows = "".join(
            "<tr>" + "".join(f"<td>{html_escape(row.get(key, ''))}</td>" for key, _ in table_columns) + "</tr>"
            for row in problem_rows
        )
    else:
        html_rows = f"<tr><td colspan='{len(table_columns)}'>Không có đầu ghi cần xử lý.</td></tr>"

    html_body = f"""<!doctype html><html><head><meta charset='utf-8'><style>
    body{{font-family:Arial,sans-serif;color:#222;margin:16px}}
    h3{{margin:0 0 8px 0}}
    .summary{{margin:0 0 12px 0;font-size:13px}}
    table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #ddd;padding:6px;text-align:left;font-size:12px}} th{{background:#f3f4f6}}
    </style></head><body>
    <h3>KBVision – Báo cáo camera/HDD</h3>
    <p class='summary'>{checked_at_dt.strftime('%d/%m/%Y %H:%M')} | Đầu ghi {total_nvrs} | Cam {all_installed} | On {all_online} | Off {all_offline} | HDD lỗi {hdd_errors} | Mất kết nối {nvr_errors}</p>
    <table><tr>{html_header}</tr>{html_rows}</table>
    </body></html>"""
    html_path = OUT_DIR / f"daily_report_{stamp}.html"
    html_path.write_text(html_body, encoding="utf-8")

    print(f"Tóm tắt: Đầu ghi {total_nvrs} | Cam {all_installed} | On {all_online} | Off {all_offline} | HDD lỗi {hdd_errors} | Mất kết nối {nvr_errors}")
    print(f"CSV: {report_path}")
    print(f"HTML: {html_path}")

    email_cfg = cfg.get("email", {})
    if email_cfg.get("enabled", False):
        send_email(cfg, html_body, report_path, html_path, checked_at_dt)


if __name__ == "__main__":
    main()
