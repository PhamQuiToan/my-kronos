cd /Users/phamquitoan/github/MTS/KRONOS
source .venv/bin/activate

# **Dự báo Bitcoin:**

python forecast_by_date.py 29/10/2026 M15 BTC


# **Dự báo vàng:**

python forecast_by_date.py 29/10/2026 M15 GOLD

# **Dự báo ngày mai cho cả vàng và Bitcoin (khung M15):**

export FORECAST_UPLOAD_API_KEY="your-api-key"
python forecast_tomorrow.py

Sau khi tạo từng file CSV, script tự upload lên server với category tương ứng.

# **Dự báo và upload đến hết ngày today+3 (mỗi tài sản một CSV chứa cả 3 ngày):**

export FORECAST_UPLOAD_API_KEY="your-api-key"
python forecast_next_3_days.py

# **Chế độ nhập tương tác:**
python forecast_by_date.py

Ngày muốn dự báo (DD/MM/YYYY):
Khung nến (ví dụ M15):
Tài sản (BTC hoặc GOLD/VANG):



# **File kết quả dự báo được lưu theo ngày đích:**
forecast_result/2026-10-29/future_prediction_paxgusdt_2026-10-29_15m.csv
forecast_result/2026-10-29/future_prediction_btcusdt_2026-10-29_15m.csv

# Chỉ giữ lại 5 thư mục ngày kết quả gần nhất trong forecast_result/.



# **Web UI:**
cd /Users/phamquitoan/github/MTS/KRONOS
source .venv/bin/activate
cd webui
python run.py

# **API:**
cd /Users/phamquitoan/github/MTS/KRONOS

# Dùng TOKEN đã lưu ở webui/.api_token
unset KRONOS_API_TOKEN

.venv/bin/python -m flask \
  --app webui.forecast_api:create_app run \
  --host 127.0.0.1 --port 7071

  cloudflared tunnel --url http://127.0.0.1:7071

  cd /Users/phamquitoan/github/MTS/KRONOS

curl --max-time 130 \
  https://something-random.trycloudflare.com/api/forecast \
  -H "Authorization: Bearer $(cat webui/.api_token)" \
  -H "Content-Type: application/json" \
  --data-binary @webui/examples/forecast-request.json