# V1.16.6 – Optional fallback URL

- URL chính bắt buộc phải đăng nhập được.
- URL dự phòng chỉ được kích hoạt nếu preflight và login thành công.
- `Connection refused` ở localhost/internal URL chỉ tạo cảnh báo `NET-FALLBACK-DISABLED`.
- Mô phỏng tiếp tục hoàn toàn qua URL công khai khi URL chính vẫn hoạt động.
