KBVISION - THỬ GỘP BÁO CÁO CAMERA THÀNH MỘT TIN

Cập nhật:
- Thử gửi toàn bộ báo cáo CAMERA trong một tin Teams.
- Báo cáo HDD vẫn là tin riêng.
- Nếu webhook từ chối tin gộp, chương trình tự quay về cách chia tin cũ (tối đa 18 dòng hoặc 6500 ký tự mỗi phần).
- Chỉ hiển thị lỗi đã xác nhận; không đổi monitor.py hay Task Scheduler.

Cách cập nhật:
1. Sao lưu send_teams_report.py hiện tại.
2. Chép đè send_teams_report.py từ ZIP này vào D:\Python\kbvision_monitor.
3. Từ CMD ở D:\Python\kbvision_monitor chạy:
   py -3.12 -X utf8 send_teams_report.py
4. Kiểm tra Teams: nếu báo cáo CAMERA xuất hiện một tin duy nhất thì thử nghiệm thành công. Nếu webhook từ chối tin gộp, chương trình tự gửi theo cách chia cũ.

Lưu ý: HTTP chấp nhận không đảm bảo giao diện Teams không rút gọn nội dung ở mọi client; hãy kiểm tra phần cuối danh sách trong tin nhắn.
