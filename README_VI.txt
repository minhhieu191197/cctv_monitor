KBVision Teams compact report update
====================================

Muc tieu:
- Giu nguyen monitor.py, config.json, device.xml va nvr_password.dpapi hien co.
- Thay send_teams_report.py bang ban compact trong goi nay.
- Dung run_daily_retention.bat lam Action trong Task Scheduler.
- Gui Teams moi ngay, ke ca ngay khong co canh bao.
- CSV giu 30 ngay; HTML giu 7 ngay; scheduled logs giu 7 ngay.

Cach cap nhat an toan:
1. Sao luu thu muc kbvision_monitor hien tai.
2. Chep send_teams_report.py va run_daily_retention.bat tu goi nay vao thu muc dang chay.
3. Khong thay monitor.py, config.json, device.xml, nvr_password.dpapi.
4. Kiem tra bien moi truong KBV_TEAMS_WEBHOOK duoc dat cho tai khoan Windows tmhieu.
5. Chay thu run_daily_retention.bat.
6. Xac nhan co CSV moi, tin nhan Teams moi, va logs\scheduled_YYYYMMDD.log co dong BAT END.
7. Task Scheduler > Actions phai tro den run_daily_retention.bat trong dung thu muc.

Luu y:
- CSV chi luu tren may Windows, khong dinh kem vao Teams.
- Link webhook la bi mat; khong chia se trong chat.
- Python duong dan trong BAT dang theo may da xac nhan: Python312.
- Task Scheduler chay bang tai khoan tmhieu de doc bien webhook va kho mat khau DPAPI.
