# Deploy Kronos qua SSH

Chỉnh các biến ở đầu `scripts/deploy.sh`: `SSH_HOST`, `SSH_USER`, `SSH_PORT`,
`SSH_KEY`, `REMOTE_DIR`. Có thể truyền các biến này từ môi trường thay vì sửa file.
Không đặt mật khẩu SSH vào script; dùng SSH key, SSH agent hoặc nhập mật khẩu khi SSH hỏi.
Script chứa cấu hình SSH không được đồng bộ lên server.

Yêu cầu: máy local có Bash, SSH và rsync; server Linux có Bash, rsync,
Python >= 3.10, pip, venv và Internet để tải dependencies/model.
SSH user cần quyền tạo/ghi `REMOTE_DIR`. Ví dụ Ubuntu/Debian cần cài sẵn
`python3 python3-venv python3-pip rsync`. Script không tự cài gói hệ điều hành.

```bash
# Chạy từ thư mục gốc Kronos
bash scripts/deploy.sh --dry-run
bash scripts/deploy.sh
```

`--dry-run` vẫn kết nối SSH nhưng chỉ liệt kê thay đổi; thư mục đích phải tồn tại.
Deploy thật tạo thư mục đích, đồng bộ source (bao gồm source chưa commit),
tạo `.venv`, cài requirements của project và Web UI, kiểm tra dependencies,
và khởi tạo API token riêng trên server nếu chưa có.
Không chép `.env`, token, SSH key, môi trường Python local, log và kết quả dự báo.
Thư mục `data/` được đồng bộ; model weights được tải khi ứng dụng nạp model lần đầu.
Model tải cần Internet và đủ dung lượng; CUDA cần PyTorch/driver phù hợp trên server.

Mặc định script chỉ chuẩn bị source và môi trường, chưa khởi chạy dịch vụ.
Để chỉ cập nhật source, đặt `INSTALL_DEPS=0`. Để restart dịch vụ đã cấu hình trên server,
đặt `POST_DEPLOY_COMMAND="sudo -n systemctl restart kronos-api"`.
SSH user phải có quyền thực hiện lệnh đó mà không cần nhập mật khẩu sudo.
Lỗi đồng bộ, cài dependencies hoặc lệnh sau deploy sẽ trả exit code khác 0.

Không dùng `--delete`: file đã xóa ở local vẫn còn trên server. Source được cập nhật
trực tiếp, không có rollback tự động; nên dừng dịch vụ khi thay đổi lớn.
Không đồng bộ token local; token server nằm tại `webui/.api_token`.
Nếu triển khai API production, dùng WSGI một process và reverse proxy HTTPS theo
`webui/FORECAST_API.md`. Không dùng Flask debug server cho production.
