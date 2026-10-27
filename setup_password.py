#!/usr/bin/env python3
"""Store the common NVR password once, encrypted with current-user Windows DPAPI."""
import getpass
import os
import sys
from pathlib import Path
from credential_store import protect_password

HERE = Path(__file__).resolve().parent
TARGET = HERE / "nvr_password.dpapi"

def main():
    if os.name != "nt":
        print("Chạy script này trên máy Windows sẽ dùng để chạy monitor.py.")
        return 2
    first = getpass.getpass("Nhập mật khẩu chung của đầu ghi (chỉ thiết lập một lần): ")
    if not first:
        print("Mật khẩu trống; không lưu.")
        return 2
    second = getpass.getpass("Nhập lại mật khẩu để xác nhận: ")
    if first != second:
        print("Hai lần nhập không khớp; chưa lưu.")
        return 2
    try:
        TARGET.write_bytes(protect_password(first))
    except Exception as exc:
        print(f"Không lưu được mật khẩu mã hóa: {exc}")
        return 2
    print(f"Đã lưu mật khẩu mã hóa tại: {TARGET.name}")
    print("Monitor.py sẽ dùng kho này, không hỏi mật khẩu mỗi lần chạy.")
    print("Kho chỉ giải mã được dưới tài khoản Windows hiện tại trên máy này.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
