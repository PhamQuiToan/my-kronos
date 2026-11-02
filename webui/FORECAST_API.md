# Client-candle forecast API

`POST /api/forecast` nhận nến từ client, gọi `KronosPredictor.predict` và trả JSON.
Không đọc CSV, không sinh dữ liệu dự báo giả. Endpoint cũng được đăng ký trong
`webui/app.py`. Để chạy dịch vụ chỉ có API forecast được bảo vệ, dùng app riêng:

```bash
# Từ thư mục gốc repository, dùng môi trường đã cài requirements của dự án và webui.
.venv/bin/python -m webui.api_token
unset KRONOS_API_TOKEN  # Dùng token lưu trong webui/.api_token
export KRONOS_API_DEVICE=cpu
.venv/bin/python -m flask --app webui.forecast_api:create_app --debug run --host 127.0.0.1 --port 7071
```

Ở chế độ debug, API ghi body request đã xác thực vào log ở mức `DEBUG`, kèm `request_id`
để đối chiếu với response. Log không ghi header `Authorization`; body có thể chứa
dữ liệu đầu vào nhạy cảm, vì vậy chỉ bật debug khi phát triển cục bộ, không bật ở production.

TOKEN được tạo ngẫu nhiên và lưu trong `webui/.api_token`, quyền file `0600`
(chỉ chủ sở hữu đọc/ghi). File được loại khỏi Git; không chia sẻ hoặc commit file này.
Lệnh khởi tạo không ghi đè token đã có và không in secret ra terminal.
API đọc file theo từng request, nên đổi token không cần restart:

```bash
.venv/bin/python -m webui.api_token --rotate
```

`KRONOS_API_TOKEN` nếu được đặt sẽ ưu tiên hơn file, kể cả khi giá trị rỗng
(khi đó API từ chối phục vụ). Nếu file thiếu, không đọc được hoặc không hợp lệ và
không có biến môi trường, API trả 503. Không tự sinh token khi nhận request.
Có thể xem token trong file để cấu hình client; không lưu token vào URL hay log. Production dùng WSGI server
với **một process**, phía sau reverse proxy HTTPS; Flask development server chỉ dùng
để chạy thử. App riêng không bật CORS. Các API Web UI cũ vẫn giữ cơ chế truy cập
hiện có; TOKEN mới chỉ bảo vệ `/api/forecast`.

```bash
KRONOS_CLIENT_TOKEN="$(cat webui/.api_token)"
curl --fail-with-body http://127.0.0.1:7071/api/forecast \
  -H "Authorization: Bearer ${KRONOS_CLIENT_TOKEN}" \
  -H 'Content-Type: application/json' \
  --data-binary @webui/examples/forecast-request.json
```

File request mẫu dùng giá minh họa; thay bằng dữ liệu thị trường thực khi sử dụng.

## Request

Fine-tune nền tùy chọn: xem [LEARNING.md](LEARNING.md) để bật thu thập nến thật,
worker theo lịch, đánh giá và rollback. Request hỗ trợ `learn` (boolean, mặc định
false); chỉ đặt true cho dữ liệu thị trường thực. Model response có thể là đường
dẫn checkpoint riêng theo symbol/timeframe khi tính năng này được bật.

### Log khi gọi API

Mỗi lần gọi endpoint được ghi vào `webui/logs/forecast_api.log`, kể cả khi không
bật debug. Log có dòng bắt đầu và kết thúc, `request_id`, HTTP status, thời gian
xử lý, mã lỗi và lý do từ chối (`reason`). File tự tạo khi có request đầu tiên;
xoay vòng ở 5 MiB và giữ tối đa 3 bản cũ. Thư mục log đã được bỏ qua bởi Git.
File log ghi toàn bộ body JSON của request đã xác thực, trong giới hạn 512 KiB,
ở dòng `event=input body=...`, kể cả khi dữ liệu nến không hợp lệ và không bật debug.
Body được escape thành chuỗi JSON trên một dòng, giữ nguyên thứ tự nến để chẩn đoán.
Request bị từ chối trước bước đọc body (ví dụ sai token, vượt quota hoặc quá lớn)
chỉ có log trạng thái. Không ghi header Authorization; body có thể chứa dữ liệu
nhạy cảm nên không gửi secret trong body hoặc chia sẻ log công khai.

```bash
tail -f webui/logs/forecast_api.log
```

Sau khi cập nhật code, khởi động lại server nếu không dùng tự động reload.

- `symbol`: chính xác `BTC` hoặc `GOLD`. Đây là nhãn tài sản; cùng dùng model tổng quát,
  không tự chọn model chuyên biệt theo symbol.
- `timeframe`: `M1`, `M5`, `M15`, `M30`, `H1`, `H4`, `D1`.
- `candles`: 2–512 nến. Dùng toàn bộ nến, không cắt hoặc sắp xếp lại. Hai nến là
  mức tối thiểu kỹ thuật; nên cung cấp khoảng 400 nến để phù hợp cách dùng hiện có.
- Mỗi nến có đúng `timestamp`, `open`, `high`, `low`, `close`, `volume`.
  Timestamp là chuỗi ISO 8601 có timezone, được chuẩn hóa UTC. Giá phải dương,
  volume không âm, OHLC đúng biên, số hữu hạn với trị tuyệt đối tối đa 1e15.
  Không chấp nhận chuỗi số hay boolean.
- Timestamp tăng dần, không trùng. Với BTC, khoảng cách chính xác một timeframe.
  Với GOLD, cho phép gap là bội số nguyên của timeframe; không chèn nến giả.
  Timestamp dự báo GOLD bỏ qua các khung giờ UTC thiếu ở ít nhất hai ngày trong
  các gap dưới 24 giờ, nếu không có nến nào trong input giao dịch ở khung giờ đó.
  Đây là suy luận từ input, không phải lịch sàn: không tự bỏ cuối tuần, ngày lễ
  hoặc xử lý thay đổi giờ nghỉ theo DST. Gap chỉ xuất hiện một lần không được
  dùng để suy ra giờ nghỉ hằng ngày.
- `pred_len`: số nguyên 1–120, mặc định 24.
- `temperature`: 0.01–2, mặc định 1.0.
- `top_p`: 0.01–1, mặc định 0.9.
- `sample_count`: số nguyên 1–5, mặc định 1.
- Không chấp nhận field lạ. Payload tối đa 512 KiB.

## Model và giới hạn vận hành

Model mặc định `NeoQuasar/Kronos-base` (hoặc checkpoint đã qua gate khi bật learning), tokenizer
`NeoQuasar/Kronos-Tokenizer-base`, context 512, device mặc định CPU.
`KRONOS_API_DEVICE` có thể chỉ định device được PyTorch hỗ trợ.
Model được lazy-load và cache riêng cho API, không bị đổi bởi `/api/load-model`
của Web UI. Lần đầu cần có weights trong cache hoặc kết nối Hugging Face.
Pipeline hiện có tự tính input `amount` từ volume × giá OHLC trung bình khi thiếu;
API không thêm giá trị output giả. Model thật hiện trả OHLCV và amount.

Mỗi process có một worker, không xếp hàng request forecast. Khi đang chạy, request
khác nhận 503. Deadline mặc định 120 giây (bao gồm nạp model), cấu hình nội bộ bằng
Flask config `FORECAST_TIMEOUT_SECONDS`. Timeout trả 504 nhưng không thể ngắt an toàn
thread PyTorch; khóa vẫn giữ cho đến khi tác vụ kết thúc. Nếu tác vụ treo, cần restart
process. Model tải lỗi được thử lại trong request sau.

Giới hạn chung 10 request đã xác thực / 60 giây / process, tính cả request sai dữ liệu.
Không dùng IP hoặc X-Forwarded-For để tránh giả mạo và tăng bộ nhớ không giới hạn.
401 không đưa vào quota này; reverse proxy nên giới hạn cả traffic chưa xác thực.
Nếu scale nhiều process/host, cần rate limit chung ở gateway và tính ngân sách model
cho từng process; limiter và model cache trong bộ nhớ không chia sẻ giữa các process.

## Response

Ví dụ cấu trúc (giá dự báo minh họa, kết quả thật phụ thuộc model):

```json
{
  "request_id": "<server-generated-id>",
  "symbol": "BTC",
  "timeframe": "M15",
  "model": "NeoQuasar/Kronos-base",
  "generated_at": "2026-10-30T00:16:00+00:00",
  "last_input_timestamp": "2026-10-30T00:15:00+00:00",
  "parameters": {"lookback": 2, "pred_len": 1, "temperature": 1.0, "top_p": 0.9, "sample_count": 1},
  "forecast": [
    {"timestamp": "2026-10-30T00:30:00+00:00", "open": 101.0, "high": 110.0, "low": 90.0, "close": 102.0, "volume": 10.0, "amount": 1010.0}
  ]
}
```

Timestamp forecast bắt đầu sau nến cuối một timeframe, bỏ qua giờ nghỉ GOLD
suy ra từ input như mô tả trên. Chỉ trả các cột OHLCV/amount
thực sự có trong kết quả model; không thêm confidence. Model output không hữu hạn
hoặc sai số hàng/cột được coi là lỗi. Mọi response endpoint có `X-Request-ID` và
`Cache-Control: no-store`.

```json
{"request_id": "<server-generated-id>", "error": {"code": "unauthorized", "message": "Valid Bearer token required"}}
```

Status: 400 input sai; 401 thiếu/sai Bearer TOKEN; 413 quá kích thước; 415 sai
Content-Type; 429 vượt quota (`Retry-After: 60`); 503 chưa cấu hình TOKEN, worker
bận hoặc lỗi nạp/chạy model; 504 timeout; 500 lỗi nội bộ ngoài dự kiến.
Token được so sánh constant-time trên SHA-256 digest; không ghi token hoặc exception
chứa dữ liệu nhạy cảm vào log. Đối chiếu lỗi bằng request_id.

## Kiểm thử

```bash
.venv/bin/python -m unittest discover -s tests -p test_forecast_api.py -v
```

Test mock lớp model, không tải weights hay gọi mạng. Bao gồm BTC/GOLD, dữ liệu đầu
vào đúng, cache model, auth, dữ liệu sai, payload, JSON, rate limit, lỗi model,
output lỗi và timeout không xếp hàng thêm công việc.
