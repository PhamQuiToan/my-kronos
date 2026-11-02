# Deploy Kronos qua SSH

Fine-tune nền sau deploy: làm theo [webui/LEARNING.md](../webui/LEARNING.md).
Script loại trừ `learning_state/` để giữ dữ liệu, registry và checkpoint trên
server. Cần bật API collection và cài systemd timer riêng; deploy không tự train.

Khi chạy từ terminal, script hỏi lần lượt SSH host/IP, user, port, đường dẫn
private key và thư mục deploy trên server. Nhấn Enter để dùng giá trị mặc định
hiển thị trong ngoặc vuông. Đường dẫn key hỗ trợ dạng `~/.ssh/id_ed25519`.
Thông tin nhập chỉ dùng cho lần chạy hiện tại, không lưu vào file.
Có thể truyền các biến `SSH_HOST`, `SSH_USER`, `SSH_PORT`, `SSH_KEY`, `REMOTE_DIR`
từ môi trường để đặt mặc định. Dùng `--non-interactive` để bỏ qua câu hỏi khi tự động hóa.
Không đặt mật khẩu SSH vào script; dùng SSH key, SSH agent hoặc nhập mật khẩu khi SSH hỏi.
Script chứa cấu hình SSH không được đồng bộ lên server.

Script hỗ trợ openrsync/rsync 2.6.9 có sẵn trên macOS, không dùng
`--protect-args` vì phiên bản này không hỗ trợ. Nếu deploy thất bại, thông báo
cuối chỉ rõ bước SSH/tạo thư mục, đồng bộ source hay cài dependencies.
Thông báo `Đã đồng bộ source lên server.` xác nhận bước rsync thành công;
lỗi cài dependencies sau đó không có nghĩa là source chưa được chép.

Yêu cầu: máy local có Bash, SSH và rsync; server Linux có Bash, rsync,
Python >= 3.10, pip, venv và Internet để tải dependencies/model.
SSH user cần quyền tạo/ghi `REMOTE_DIR`. Ví dụ Ubuntu/Debian cần cài sẵn
`python3 python3-venv python3-pip rsync`. Script không tự cài gói hệ điều hành.

```bash
# Chạy từ thư mục gốc Kronos
bash scripts/deploy.sh --dry-run
bash scripts/deploy.sh

# Chạy tự động, không hỏi cấu hình
SSH_HOST=203.0.113.10 SSH_USER=ubuntu bash scripts/deploy.sh --non-interactive
```

`--dry-run` vẫn kết nối SSH nhưng chỉ liệt kê thay đổi; thư mục đích phải tồn tại.
Deploy thật tạo thư mục đích, đồng bộ source (bao gồm source chưa commit),
tạo `.venv`, cài requirements của project và Web UI, kiểm tra dependencies,
và khởi tạo API token riêng trên server nếu chưa có.
Không chép `.env`, token, SSH key, môi trường Python local, log và kết quả dự báo.
Thư mục `data/` được đồng bộ; model weights được tải khi ứng dụng nạp model lần đầu.
Model tải cần Internet và đủ dung lượng; CUDA cần PyTorch/driver phù hợp trên server.

Mặc định script dùng user `root`, đồng bộ source, cài dependencies rồi chạy
`systemctl restart kronos-api && systemctl is-active --quiet kronos-api`.
Service `kronos-api` phải được cài trước và chạy code trong `REMOTE_DIR`.
`is-active` kiểm tra trạng thái systemd, chưa kiểm chứng HTTP hoặc inference.
Đặt `INSTALL_DEPS=0` để bỏ cài dependencies; lệnh restart vẫn chạy.
Để chỉ đồng bộ source, đặt cả `INSTALL_DEPS=0 POST_DEPLOY_COMMAND=''`.
Với user không phải root, cấu hình
`POST_DEPLOY_COMMAND='sudo -n systemctl restart kronos-api && sudo -n systemctl is-active --quiet kronos-api'`.
SSH user phải có quyền sudo không cần nhập mật khẩu cho các lệnh đó.
Chỉ chạy POST_DEPLOY_COMMAND sau khi đồng bộ và cài dependencies thành công;
`--dry-run` không chạy lệnh này. Có thể ghi đè bằng biến môi trường.
Lỗi đồng bộ, cài dependencies hoặc lệnh sau deploy sẽ trả exit code khác 0.

Không dùng `--delete`: file đã xóa ở local vẫn còn trên server. Source được cập nhật
trực tiếp, không có rollback tự động; nên dừng dịch vụ khi thay đổi lớn.
Không đồng bộ token local; token server nằm tại `webui/.api_token`.
Nếu triển khai API production, dùng WSGI một process và reverse proxy HTTPS theo
`webui/FORECAST_API.md`. Không dùng Flask debug server cho production.
