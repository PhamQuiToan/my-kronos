# Thu thập nến và fine-tune nền

API phục vụ dự báo độc lập với worker training. Tính năng mặc định tắt.
Không train trong HTTP request; systemd timer kiểm tra mỗi ngày lúc 03:00 UTC.
BTC/GOLD và mỗi timeframe có dữ liệu, model hiện hành và lịch sử riêng.

## Bật trên server

Deploy source mới, cài dependencies (bao gồm PyYAML):

```bash
cd /opt/kronos
.venv/bin/python -m pip install -r requirements.txt -r webui/requirements.txt
sudo systemctl edit kronos-api
```

Thêm vào phần override:

```ini
[Service]
Environment=KRONOS_LEARNING_ENABLED=1
Environment=KRONOS_LEARNING_DIR=/opt/kronos/webui/learning_state
```

Sau đó:

```bash
sudo systemctl daemon-reload
sudo systemctl restart kronos-api
sudo cp scripts/systemd/kronos-learning.service /etc/systemd/system/
sudo cp scripts/systemd/kronos-learning.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now kronos-learning.timer
```

Các unit mẫu dùng root và /opt/kronos theo server hiện tại. Nếu đổi user/path,
đổi cả API và worker; cả hai phải dùng chung thư mục state có quyền đọc/ghi.
Worker mặc định CPU, hai thread toán học và CPUQuota=100% (tối đa một CPU);
model base cần đáng kể RAM và training có thể kéo dài. Worker riêng vẫn chia sẻ
RAM với API; cần đủ RAM cho cả hai. Có thể đổi KRONOS_TRAIN_DEVICE sang cuda khi
có GPU phù hợp. Không cài hoặc khởi chạy training trên server chỉ bằng deploy.sh.

## Thu thập dữ liệu request

Khi `KRONOS_LEARNING_ENABLED=1`, API tự lưu nến của mọi request đã xác thực và
vượt kiểm tra, không phụ thuộc trường `learn` (được giữ lại nhưng bỏ qua để
tương thích client cũ). Bật tính năng này chỉ khi mọi request gửi vào API đều
chứa dữ liệu thị trường thực; server không thể phân biệt OHLCV thật với dữ liệu
demo, tổng hợp hoặc output dự báo. Không gửi dữ liệu mẫu từ
`webui/examples/forecast-request.json` khi đang bật thu thập. Mỗi symbol/timeframe
cần dùng một nguồn dữ liệu thống nhất; không trộn giá vàng hoặc volume từ các
broker khác nhau vào cùng cặp.

Timestamp được hiểu là thời điểm MỞ nến UTC. Chỉ nến có timestamp + timeframe
<= giờ server mới được lưu. Nến đang mở vẫn được dùng cho forecast nếu client gửi,
nhưng không được thu thập để train. Khi nến đó đóng, client cần gửi lại giá cuối.
Nến trùng (symbol, timeframe, timestamp) bị bỏ qua; bản đóng đầu tiên được giữ
nguyên, không ghi đè bằng retry hoặc dữ liệu thay đổi sau này. Không lưu forecast
output để training. Trường `learn` không bật/tắt thu thập; khi tính năng learning
bật, lỗi lưu DB trả 503 và có thể retry an toàn.

Dữ liệu lưu trước inference nên nến vẫn được lưu nếu inference bận/lỗi/timeout.
Lưu trữ dùng SQLite transaction; input sai hoặc token sai không được thu thập.
Worker kiểm tra mỗi ngày lúc 03:00 UTC; khi đạt ngưỡng dưới đây, training và
đánh giá sẽ chạy trong lượt kiểm tra tiếp theo, không chạy trong HTTP request.

## Chọn model

- Ít nhất 2.048 nến của một symbol/timeframe, và ít nhất 512 nến mới kể từ lần
  đánh giá hoàn tất. Một lần kiểm tra theo lịch có thể không chạy training.
- Snapshot tối đa 20.000 nến mới nhất. Với BTC, chỉ dùng đoạn liên tục cuối cùng;
  với GOLD cho phép khoảng nghỉ là bội số timeframe như API.
- Giữ riêng 256 nến cuối để kiểm tra; cả tập này phải mới hơn lần đánh giá trước.
  Phần còn lại chia 80% train / 20% validation theo thời gian. Không trộn ngẫu nhiên
  giữa các tập. Chọn checkpoint tốt nhất bằng validation, không dùng test để train.
- Fine-tune predictor từ Kronos-base, giữ tokenizer gốc; 2 epoch, batch size 4,
  learning rate 1e-6, lookback 128, horizon 24. Mỗi job bắt đầu từ base để tránh
  vô tình dùng dữ liệu test cũ đã được học trong một model tích lũy.
- Chạy dự báo rolling trên 240 nến trong tập test, theo từng nhóm 24 nến, cùng seed
  và tham số cho candidate và model hiện hành. Context chỉ gồm nến trước target.
- Chỉ promote nếu close MAE hữu hạn, giảm hơn 2% so với model hiện hành và thấp
  hơn baseline giữ nguyên giá đóng cuối cùng. Đây là gate trên một tập kiểm tra,
  không đảm bảo tốt hơn cho mọi horizon/thị trường hoặc lợi nhuận giao dịch.
- Registry đổi atomically sau khi checkpoint tải lại và dự báo kiểm tra thành công.
  Request tiếp theo tự nạp model tương ứng; response `model` cho biết model thực
  sự dùng. Request đang chạy tiếp tục dùng phiên bản đã chọn. Nếu load bản mới lỗi,
  API dùng base và ghi event=model_load_fallback, không dùng model của cặp khác.
- Model không đạt gate được giữ để kiểm tra nhưng không phục vụ. Training lỗi giữ
  nguyên model hiện hành và dữ liệu đủ điều kiện để thử lại vào lần kiểm tra sau.

## Theo dõi, chạy ngay và rollback

```bash
sudo systemctl list-timers kronos-learning.timer
sudo journalctl -u kronos-learning.service -f
# Chạy một lần bằng service (không chạy trong HTTP request)
sudo systemctl start --no-block kronos-learning.service
# Xem model từng cặp
cd /opt/kronos
.venv/bin/python -m webui.learning_worker status
# Hoặc chạy thủ công một cặp, dùng cùng KRONOS_LEARNING_DIR nếu đã đổi
.venv/bin/python -m webui.learning_worker run --symbol GOLD --timeframe M15
# Quay về model trước đó (có thể là base)
.venv/bin/python -m webui.learning_worker rollback --symbol GOLD --timeframe M15
```

Worker dùng khóa file chung, không cho chạy chồng job hoặc rollback khi đang train.
Rollback đổi registry; API áp dụng ở request kế tiếp. Tắt timer nếu muốn ngăn các
lần promote tiếp theo: `sudo systemctl disable --now kronos-learning.timer`.
Lệnh đó không dừng job đang chạy; để hủy job đang chạy dùng
`sudo systemctl stop kronos-learning.service` trước rollback.

`webui/learning_state/versions/<version>/` chứa config, CSV snapshot, training.log,
report.json (metrics, mốc thời gian, kết quả gate), checkpoint. SQLite chứa nến,
active/previous và kết quả lần đánh giá cuối. Thư mục state không được Git hoặc
rsync deploy chép/ghi đè. Nếu đặt thư mục tùy chỉnh, nên đặt ngoài thư mục source.
Chưa tự xóa nến/checkpoint cũ: theo dõi dung lượng, backup DB cùng checkpoint;
không xóa model đang active hoặc previous. Để ngừng dùng model học và thu thập,
đặt KRONOS_LEARNING_ENABLED=0 và restart API (sẽ dùng base).

Tests offline:

```bash
.venv/bin/python -m unittest discover -s tests -p test_learning.py -v
.venv/bin/python -m unittest discover -s tests -p test_forecast_api.py -v
```
