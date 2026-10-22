# Digital Twin ba đèn đường — ESP32, MQTT, Node-RED và ThingsBoard

Project dùng **một ESP32** để điều khiển ba đèn ON/OFF độc lập. Một BH1750 đo ánh sáng chung; ba INA219 đo riêng điện áp/dòng của ba nhánh đèn. Python là cổng Digital Twin chịu trách nhiệm xác minh HMAC, chống replay, lưu SQLite, ký lệnh và chỉ chuyển tiếp dữ liệu đã xác minh tới Node-RED/ThingsBoard.

## 1. Kiến trúc

```text
BH1750 + INA219 x3 + nút x3
            │ I²C/GPIO
            ▼
       ESP32 gateway
       │ telemetry + HMAC
       │ command/ACK + HMAC
       ▼
 Private MQTT broker ◄──── Node-RED command request
       │
       ▼
 digital_twin_server.py
   ├─ xác minh HMAC + sequence/timestamp
   ├─ lưu digital_twin.db
   ├─ publish city/digital-twin/verified/telemetry ──► Node-RED Dashboard
   └─ MQTT/TLS riêng, một chiều ──► 1 ThingsBoard device
```

Node-RED không đọc topic telemetry thô. ThingsBoard không điều khiển đèn trong phiên bản này; lệnh chỉ đi qua Digital Twin nội bộ.

## 2. BOM và giả định phần cứng

- 1 ESP32 DevKit loại 30 chân tương thích `esp32dev`.
- 1 BH1750, địa chỉ mặc định `0x23`.
- 3 INA219, địa chỉ `0x40`, `0x41`, `0x44`.
- 3 kênh công suất/driver phù hợp với tải đèn, 3 nút nhấn và nguồn phù hợp.
- Dây I²C có pull-up 3,3 V nếu module không tích hợp sẵn; tất cả phần logic phải chung GND.

`diagram.json` là **mô phỏng chức năng**, không phải sơ đồ cấp nguồn triển khai. Wokwi hiện không có BH1750/INA219 trong danh sách phần cứng hỗ trợ chính thức, nên profile `wokwi` dùng một LDR và ba biến trở làm adapter. Profile `esp32dev` dùng đúng thư viện BH1750/INA219 và không đọc ADC giả.

### GPIO

| Chức năng | GPIO |
|---|---:|
| I²C SDA / SCL | 21 / 22 |
| Lamp 01 / 02 / 03 output | 25 / 26 / 27 |
| Nút Lamp 01 / 02 / 03 | 32 / 33 / 16 |
| Wokwi-only: lux | 34 |
| Wokwi-only: dòng L1/L2/L3 | 35 / 36 / 39 |

Các chân strap 0, 2, 4, 5, 12, 15 không được dùng cho điều khiển đèn. GPIO 34–39 chỉ xuất hiện ở adapter mô phỏng dưới dạng input.

### Nguồn và INA219

ESP32/I²C dùng logic 3,3 V và chung GND. Mỗi INA219 phải mắc high-side nối tiếp với **một nhánh tải riêng** và được cấu hình địa chỉ khác nhau. Không cấp dòng đèn trực tiếp từ GPIO; GPIO chỉ điều khiển driver/MOSFET. Chưa thể chốt chân module D4184, điện áp tải và giới hạn dòng khi chưa có đúng datasheet/ảnh module thực tế. Hãy kiểm tra shunt, mức logic và tản nhiệt trước khi cấp tải thật.

| Khối | Điện áp dự kiến | Ghi chú |
|---|---|---|
| ESP32 qua USB-C | 5 V từ laptop | Điện áp logic GPIO là 3,3 V |
| BH1750/INA219 phía I²C | 3,3 V | SDA/SCL không được kéo lên 5 V |
| Ba tải đèn | Chưa chốt | Phải theo đúng LED/driver thực tế, không lấy dòng tải trực tiếp từ GPIO |
| GND | Chung | ESP32, cảm biến, driver và nguồn tải phải có mốc tham chiếu phù hợp |

Với breakout INA219 kiểu phổ biến có jumper `A0/A1`, cấu hình dự kiến là:

| Nhánh | Địa chỉ | Jumper thường gặp |
|---|---:|---|
| Lamp 01 | `0x40` | A0 mở, A1 mở |
| Lamp 02 | `0x41` | nối A0 |
| Lamp 03 | `0x44` | nối A1 |

Cách jumper có thể khác theo nhà sản xuất; phải đối chiếu silkscreen/datasheet của đúng module và xác nhận bằng log quét I²C trước khi dùng.

## 3. Điều khiển

- Thanh kéo đặt ngưỡng tắt `T` trong khoảng 100–1000 lux, bước 10, mặc định 150. AUTO bật đèn khi lux `< T − 50`, tắt khi lux `> T`; bằng đúng hai biên hoặc ở giữa thì giữ nguyên. Đây là ngưỡng ánh sáng môi trường, **không phải dimming/PWM**.
- Bật AUTO chung đưa cả ba đèn sang AUTO và áp dụng T ngay; tắt AUTO chung đưa cả ba về MANUAL và giữ nguyên ON/OFF hiện tại. Ngưỡng được lưu trong NVS của ESP32 và khôi phục sau reboot; mode và trạng thái đèn sau reboot khởi tạo lại ở AUTO/OFF trước khi đọc cảm biến.
- BH1750 lỗi/mất: giữ trạng thái hiện tại, phát `luxValid=false`; không tự đoán lux.
- Ba switch riêng gửi ON/OFF khi cả ba đèn đã báo MANUAL và dữ liệu còn mới; lệnh cá nhân bị backend/firmware từ chối nếu AUTO còn bật. Dashboard hiển thị trạng thái xác nhận thay vì coi vị trí switch vừa bấm là kết quả.
- Ba nút vật lý vẫn có thể chuyển riêng từng đèn sang MANUAL; khi đó giao diện có thể báo trạng thái chung không xác định (MIXED). Bấm AUTO chung để đưa cả ba về AUTO hoặc tắt AUTO chung để tất cả về MANUAL.
- Lệnh chung đi qua Python, được ký HMAC bằng khóa lamp-01 của gateway một ESP32, rồi gửi đến `city/streetlight/gateway/command`; ACK được ký và xác minh trước khi gỡ trạng thái chờ. Không đặt khóa thật trong README hoặc repository.
- Hệ thống chỉ biểu diễn ON/OFF (`brightness` chỉ là 0 hoặc 100), không tuyên bố dimming giả.

Firmware quét I²C khi khởi động và báo từng địa chỉ trên Serial Monitor. INA219 thiếu sẽ tạo `sensorValid=false` và giá trị điện bị gửi `null`, không thay bằng số giả.

## 4. Secrets

Không commit secret/token. Tạo các file cục bộ:

```powershell
Copy-Item .env.example .env
Copy-Item include\secrets.example.h include\secrets.h
```

Ba HMAC secret trong hai file phải giống nhau. Với deployment, MQTT nội bộ cần broker riêng có ACL/tài khoản; cấu hình mặc định là `127.0.0.1`, không phải public broker.

Profile Wokwi dùng ba secret demo trong `platformio.ini`. Khi chạy backend cho Wokwi, `.env` phải dùng đúng ba giá trị demo đó hoặc bạn thay đồng bộ cả hai nơi.

## 5. Cài đặt và khởi chạy (Dành cho máy mới / người dùng mới)

### 5.1. Cài đặt môi trường lần đầu (Chỉ làm 1 lần)

1. **Yêu cầu phần mềm:**
   - **Python 3.10+** (tích chọn *Add Python to PATH* khi cài).
   - **Node.js (LTS)** (để chạy Node-RED).
   - **VS Code** có 2 tiện ích: **PlatformIO IDE** và **Wokwi Simulator**.

2. **Cài đặt thư viện và môi trường ảo:**
   Mở PowerShell tại thư mục dự án:
   ```powershell
   # Tạo virtualenv Python và cài đặt thư viện (server + broker aMQTT)
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   pip install amqtt

   # Cài đặt Node-RED dashboard
   npm --prefix .node-red-data install node-red-dashboard

   # Tạo file cấu hình từ file mẫu
   Copy-Item .env.example .env
   Copy-Item include\secrets.example.h include\secrets.h
   ```

3. **Cấu hình file `.env`:**
   Mở file `.env` và thiết lập:
   - Mặc định chạy local: `IOT_MQTT_BROKER=127.0.0.1`, `IOT_MQTT_PORT=1883`, `TB_ENABLED=false`.
   - Nếu đẩy lên **ThingsBoard Cloud**: đổi `TB_ENABLED=true`, `TB_HOST=mqtt.thingsboard.cloud`, `TB_PORT=8883`, `TB_DEVICE_TOKEN=<Token_Device_Cua_Ban>`.

---

### 5.2. Thứ tự khởi chạy dự án (Mở 4 Terminal riêng biệt)

#### Terminal 1 — Bật MQTT Broker nội bộ (aMQTT)
```powershell
cd C:\IOT\BTL-IOT-Nhom-4
.\.venv\Scripts\Activate.ps1
amqtt -c .venv\Lib\site-packages\amqtt\scripts\default_broker.yaml
```
> Khi khởi động thành công sẽ có log: `Listener 'default' bind to 0.0.0.0:1883`.

#### Terminal 2 — Bật Python Digital Twin Server
```powershell
cd C:\IOT\BTL-IOT-Nhom-4
.\.venv\Scripts\Activate.ps1
python digital_twin_server.py
```
> Khi khởi động thành công sẽ có log: `DIGITAL TWIN SERVER DA KHOI DONG`.

#### Terminal 3 — Build firmware & Chạy mô phỏng Wokwi (ESP32)
1. Build firmware:
   ```powershell
   cd C:\IOT\BTL-IOT-Nhom-4
   pio run -e wokwi
   ```
2. Trong VS Code:
   - Nhấn `F1` → chọn **`Wokwi: Enable Private Wokwi IoT Gateway`** (bắt buộc để Wokwi thông mạng tới máy local).
   - Nhấn `F1` → chọn **`Wokwi: Start Simulator`** (hoặc mở file `diagram.json` và bấm nút Play).
3. Sau 5 giây, Terminal 2 (Python) sẽ bắt đầu in các dòng `Da xac minh telemetry lamp-01 / lamp-02 / lamp-03`.

*(Nếu nạp cho bo mạch phần cứng thật: chạy `pio run -e esp32dev --target upload`)*.

#### Terminal 4 — Bật Giao diện Web Dashboard Local (Node-RED)
```powershell
cd C:\IOT\BTL-IOT-Nhom-4
npx node-red --userDir .node-red-data node_red_flow.json
```
Sau khi khởi động xong, mở trình duyệt truy cập:
- **Dashboard điều khiển & 3D Local:** `http://127.0.0.1:1880/ui`
- **Trình chỉnh sửa Node-RED flow (nếu cần):** `http://127.0.0.1:1880`

## 6. ThingsBoard Cloud — một device duy nhất

1. Tạo một device đại diện gateway ba đèn và lấy **Device access token**.
2. Điền `TB_ENABLED=true`, `TB_HOST`, `TB_PORT=8883`, `TB_DEVICE_TOKEN` trong `.env`.
3. Token chỉ là MQTT username; không đặt token trong code/log.
4. Backend mở client MQTT/TLS riêng và publish `v1/devices/me/telemetry` sau khi đủ snapshot của ba đèn.

Payload cloud gồm lux chung và các key `lamp01_*`, `lamp02_*`, `lamp03_*`, kèm `sourceTs` và `stale` để không biến dữ liệu cũ thành dữ liệu mới. Nếu cloud mất kết nối, SQLite và Dashboard nội bộ vẫn chạy; dữ liệu cloud được giữ trong hàng đợi hữu hạn, có reconnect/backoff.

Tài liệu chính thức: [ThingsBoard Cloud MQTT connection](https://thingsboard.io/docs/paas/reference/mqtt-api/getting-connected/) và [telemetry format](https://thingsboard.io/docs/paas/reference/mqtt-api/telemetry/).

## 7. Bảo mật và kiểm thử

- Telemetry: HMAC-SHA256 + `sequence` tăng + timestamp không quay lui.
- Command: Python tạo `commandId`, `issuedAt`, `expiresAt`, HMAC; ESP32 kiểm tra device, thời hạn, replay và chữ ký.
- ACK: ESP32 ký HMAC; Python chỉ hoàn tất command còn `PENDING`, đúng device và đúng chữ ký.
- Payload sai bị ghi `security_events` và không đi Node-RED/ThingsBoard.

Chạy kiểm thử:

```powershell
python -m unittest discover -s tests -v
pio test -e native
python -m py_compile digital_twin_server.py
python -m json.tool diagram.json > $null
python -m json.tool node_red_flow.json > $null
```

## 8. Giới hạn đã biết

- Wokwi LDR/biến trở chỉ kiểm tra luồng logic, không xác nhận độ chính xác BH1750/INA219.
- Build thành công không chứng minh wiring, nguồn, địa chỉ I²C, Wi-Fi, broker hay ThingsBoard thực tế hoạt động.
- Hàng đợi ThingsBoard nằm trong RAM và hữu hạn; nếu cần đảm bảo store-and-forward qua khởi động lại, cần thêm outbox bền vững trong SQLite.
- Các ngưỡng dòng trong `.env` phải được hiệu chuẩn theo tải. Cảnh báo điện áp mặc định bị tắt cho đến khi đặt cả `IOT_VOLTAGE_MIN` và `IOT_VOLTAGE_MAX`.
- Cần xác nhận datasheet chính xác của ESP32 30 chân, module công suất và tải trước khi lắp phần cứng.

## 9. Nhật ký thay đổi

Quy ước: sau mỗi lần cập nhật project, thêm một mục mới vào phần này và **không xóa lịch sử cũ**. Mỗi mục phải ghi rõ thời gian theo múi giờ `Asia/Bangkok` (`UTC+07:00`), nội dung đã thay đổi, lý do thay đổi và kết quả kiểm tra liên quan.

### 2026-10-20 21:47:07 +07:00

- **Nội dung:** Sửa kiểm tra anti-replay để cho phép ESP32 khởi động lại với sequence thấp hơn khi timestamp mới vẫn tăng; gói có timestamp cũ hoặc trùng vẫn bị từ chối.
  **Lý do:** SQLite có sequence `1800000000` từ phiên trước, còn Wokwi mới khởi động với sequence `1792507xxx`, khiến mọi telemetry hợp lệ bị đánh dấu `REPLAY_SEQUENCE`.
- **Nội dung:** Thêm kiểm thử `test_sequence_reset_after_reboot_requires_newer_timestamp`.
  **Lý do:** Bảo đảm hỗ trợ reboot nhưng không làm mất khả năng chặn phát lại telemetry cũ.
- **Nội dung:** Đặt debounce nút là 5 ms riêng cho build `IOT_SIMULATION`; phần cứng thật tiếp tục dùng 25 ms.
  **Lý do:** Wokwi thường chạy chậm hơn thời gian thực nên thao tác click ngắn có thể không vượt qua cửa sổ debounce của firmware.
- **Kết quả:** `diagram.json` hợp lệ; build `wokwi` thành công; 10/10 kiểm thử Python đạt; Digital Twin chấp nhận telemetry mới của cả ba đèn; Node-RED Dashboard phản hồi HTTP 200. Lệnh thử `AUTO_OFF` cho gateway và `lamp-03 = ON / MANUAL` đều nhận ACK `APPLIED`; Wokwi hiển thị Lamp 03 sáng và telemetry trả về `state=ON`.

### 2026-10-20 21:39:24 +07:00

- **Nội dung:** Đổi dây nút `Lamp 03 Manual` trong `diagram.json` từ `esp:D16` sang `esp:RX2`; firmware vẫn sử dụng GPIO16.
  **Lý do:** ESP32 DevKit V1 trong Wokwi đặt tên chân GPIO16 là `RX2`. Tên `D16` không khớp chân của linh kiện mô phỏng nên nút Lamp 03 không truyền được mức LOW tới firmware.
- **Nội dung:** Khởi động lại broker MQTT, Digital Twin server, Node-RED và Wokwi sau khi build firmware.
  **Lý do:** Các tiến trình cục bộ đã dừng nên telemetry chưa đi qua Digital Twin.
- **Kết quả:** Được cập nhật sau khi hoàn tất build và kiểm thử luồng nút–telemetry–Digital Twin.

### 2026-10-20 21:36:29 +07:00

- **Nội dung:** Chuẩn hóa `sequence` và `timestamp` đọc từ SQLite về kiểu `int` trong `last_sequence_and_timestamp()`.
  **Lý do:** Database được tạo bởi phiên bản cũ khai báo `timestamp` là `TEXT`, khiến Digital Twin lỗi khi so sánh timestamp số của telemetry với chuỗi trong SQLite.
- **Nội dung:** Thêm cấu hình `IOT_SIMULATION_CLOCK` và dùng miền thời gian của thiết bị khi chạy Wokwi; đồng thời tính trạng thái stale và thời hạn ACK phù hợp với mô phỏng.
  **Lý do:** Wokwi có thể chạy chậm hơn thời gian thực, làm telemetry bị báo `CLOCK_SKEW` và command hợp lệ bị đánh dấu hết hạn.
- **Nội dung:** Bổ sung `IOT_SIMULATION_CLOCK=false` vào `.env.example`.
  **Lý do:** Tách rõ cấu hình demo Wokwi khỏi cấu hình triển khai thật; môi trường thật vẫn kiểm tra độ lệch đồng hồ nghiêm ngặt.
- **Nội dung:** Cài broker aMQTT trong `.venv`, cài Node-RED và `node-red-dashboard` cục bộ trong `.node-red-data`, sau đó chạy broker, Digital Twin, Wokwi và Dashboard.
  **Lý do:** Máy chưa có broker MQTT và Node-RED nên ESP32 không thể gửi telemetry hoặc nhận lệnh.
- **Kết quả:** 6/6 kiểm thử Python đạt; telemetry của cả ba đèn được xác minh và lưu; lệnh thử `lamp-01 = ON / MANUAL` nhận ACK `APPLIED`; Dashboard phản hồi HTTP 200 tại `http://127.0.0.1:1880/ui`.

### 2026-10-20 21:53:02 +07:00

- **Nội dung:** Chuyển nút `Lamp 03 Manual` từ GPIO16/RX2 sang GPIO18/D18 trong cả firmware (`src/main.cpp`) và sơ đồ (`diagram.json`).
  **Lý do:** GPIO16 đồng thời là chân UART RX2 trong mô hình Wokwi, có thể giữ trạng thái đầu vào không ổn định làm nút đỏ bật được nhưng không nhận lần nhấn tiếp theo để tắt.
- **Nội dung:** Thêm log Serial `"[lamp-03] Nut local: ON/OFF"` (áp dụng tương tự cho cả ba đèn) ngay khi nút local được firmware chấp nhận.
  **Lý do:** Cho phép phân biệt lỗi nút/dây với lỗi MQTT hoặc Digital Twin khi kiểm tra.
- **Kết quả:** `diagram.json` đọc hợp lệ; build firmware môi trường `wokwi` thành công; 10/10 kiểm thử Python và 6/6 kiểm thử native đạt. Lệnh `OFF` thử nghiệm cho `lamp-03` đi qua Digital Twin và nhận trạng thái `APPLIED`; telemetry sau lệnh báo `OFF`. Cần khởi động lại Wokwi để nạp firmware và dây D18 mới trước khi kiểm tra nút local.

### 2026-10-21 10:03:49 +07:00

- **Nội dung:** Sao lưu `node_red_flow.json` và `.node-red-data/settings.js` vào `.backups/20261021-095528/` trước khi triển khai giao diện 3D.
  **Lý do:** Giữ bản khôi phục độc lập trước khi thay đổi cấu hình phục vụ file tĩnh của Node-RED.
- **Nội dung:** Tạo vùng public riêng `.node-red-data/public/` và chỉ đưa vào hai asset cần thiết: `static/vendor/three.min.js` và `static/classroom_twin_3d.js`; không phục vụ trực tiếp toàn bộ thư mục `giaodienIot`.
  **Lý do:** Giữ nguyên hai URL mà widget 3D đang dùng nhưng tránh công khai file demo và các file triển khai không cần thiết.
- **Nội dung:** Bật `httpStatic: 'C:/IOT/BTL-IOT-Nhom-4/.node-red-data/public/'` trong `.node-red-data/settings.js`, sau đó khởi động lại Node-RED bằng đúng `node_red_flow.json` và user directory của project.
  **Lý do:** Cho phép `ui_template` tải Three.js và mã cảnh 3D từ Node-RED mà không cần sửa đồng thời widget hoặc flow.
- **Nội dung:** Giữ cảnh 3D ở chế độ **chỉ quan sát**; không thêm nút điều khiển thứ hai. Các lệnh vẫn đi qua AUTO, thanh ngưỡng và ba switch L1-L3 hiện có tới topic `city/dashboard/command/request`.
  **Lý do:** Tránh hai nguồn điều khiển cạnh tranh và giữ thống nhất cơ chế chờ ACK, kiểm tra telemetry mới và định dạng command hiện tại.
- **Kết quả:** Node-RED Dashboard, `three.min.js` và `classroom_twin_3d.js` đều phản hồi HTTP `200`; hai asset có kích thước lần lượt `603445` và `37079` byte. Flow live có 31 node, vẫn nối output 17 của `fn-snapshot` tới `ui-tpl-classroom-3d` và nhận topic `city/digital-twin/verified/telemetry`.
- **Kết quả:** Dashboard 3D hiển thị đủ ba đèn `ONLINE` với snapshot đã xác minh. Thử switch L1 `ON` rồi `OFF` đều nhận ACK `APPLIED`, telemetry và cảnh 3D đổi tương ứng `5.00 V/ON` rồi `0.00 V/OFF`. Thử `AUTO_ON` rồi `AUTO_OFF` cũng nhận `APPLIED`; sau kiểm tra đã khôi phục ba đèn về `OFF / MANUAL`.
- **Kết quả:** `node --check .node-red-data/settings.js` đạt; 10/10 kiểm thử Python đạt; build PlatformIO `esp32dev` thành công (`RAM 14.0%`, `Flash 62.3%`).

### 2026-10-21 10:31:31 +07:00

- **Nội dung:** Đưa group `Đèn đường 3D (chỉ quan sát)` lên vị trí đầu tiên của Dashboard, tăng chiều rộng group và widget từ 12 lên 24 cột. Chuyển `Môi trường và AUTO`, `Lamp 01`, `Lamp 02`, `Lamp 03` xuống hàng kế tiếp với thứ tự lần lượt 2–5 và giữ mỗi group rộng 6 cột.
  **Lý do:** Cảnh 3D cần chiếm trọn một hàng ngang, còn toàn bộ bảng đo và điều khiển phải nằm bên dưới để dễ quan sát và thao tác.
- **Nội dung:** Sao lưu flow và nguồn widget trước khi sửa tại `.backups/20261021-102828-layout/`, sau đó khởi động lại Node-RED để nạp bố cục mới.
  **Lý do:** Có thể khôi phục nhanh bố cục cũ nếu cần đối chiếu.
- **Kết quả:** `node_red_flow.json` hợp lệ; Node-RED phản hồi HTTP `200`, kết nối lại MQTT và flow live vẫn có 31 node. Flow live xác nhận group 3D `order=1`, `width=24`; widget 3D `width=24`, trong khi bốn group đo/điều khiển nằm sau và rộng 6 cột. Không thay đổi topic, logic telemetry hoặc luồng command/ACK.

### 2026-10-22 14:30:00 +07:00

- **Nội dung:** Cập nhật mục 5 trong `README.md` thành hướng dẫn chi tiết dành cho người dùng mới / môi trường mới (setup virtualenv Python, cài đặt aMQTT, cấu hình file `.env`, lệnh khởi chạy 4 Terminal theo đúng thứ tự broker -> Python -> Wokwi -> Node-RED).
  **Lý do:** Giúp bất kỳ ai tải repository về đều có thể tự cài đặt và khởi chạy toàn bộ hệ thống (cả web local và đẩy dữ liệu ThingsBoard Cloud) mà không bị thiếu bước.
- **Kết quả:** Đã kiểm chứng thực tế: aMQTT chạy cổng 1883, Python xác minh telemetry 3 đèn từ Wokwi đạt 100%, Node-RED phản hồi cổng 1880.

### 2026-10-22 14:54:48 +07:00

- **Nội dung:** Bật cầu nối ThingsBoard Cloud trong `.env` với MQTT/TLS tại `mqtt.thingsboard.cloud:8883`; access token chỉ được lưu trong `.env`, không ghi vào mã nguồn, README hoặc log. Để trống `TB_CA_CERT` để dùng kho CA tin cậy của Windows.
  **Lý do:** Gửi snapshot telemetry đã xác minh của ba đèn từ Digital Twin lên device ThingsBoard Cloud bằng kết nối được mã hóa.
- **Nội dung:** Sửa hostname mẫu/hướng dẫn từ `thingsboard.cloud` thành `mqtt.thingsboard.cloud`, bổ sung log `PUBACK` trong `digital_twin_server.py`, và thêm `.backups/` vào `.gitignore`.
  **Lý do:** Dùng đúng endpoint MQTT Cloud, giúp xác nhận gói QoS 1 đã được broker nhận và tránh vô tình đưa các bản sao lưu vào Git.
- **Nội dung:** Khởi động lại Digital Twin Server và kiểm tra toàn bộ tuyến `private MQTT -> HMAC/anti-replay -> Digital Twin snapshot -> ThingsBoard Cloud` bằng ba telemetry có chữ ký hợp lệ.
  **Lý do:** Xác nhận triển khai thực tế, không chỉ kiểm tra cú pháp hoặc cấu hình tĩnh.
- **Kết quả:** ThingsBoard trả `connectRc=0`, publish trả `rc=0` và nhận `PUBACK`; Digital Twin xác minh đủ `lamp-01`, `lamp-02`, `lamp-03` rồi nhận `PUBACK mid=1` từ Cloud. MQTT nội bộ cổng 1883 và Node-RED cổng 1880 đều truy cập được; kiểm tra cú pháp đạt và 10/10 kiểm thử Python đạt.

### 2026-10-22 16:10:33 +07:00

- **Nội dung:** Khởi tạo Git repository với nhánh `main` và tải mã nguồn dự án lên `https://github.com/Greenbutter2510/IOT-digital-twin-n-ng` bằng commit đầu tiên `2326117`.
  **Lý do:** Lưu trữ tập trung firmware ESP32, Digital Twin server, Node-RED flow, giao diện 3D, cấu hình mẫu, tài liệu và kiểm thử để có thể sao chép/triển khai trên máy khác.
- **Nội dung:** Mở rộng `.gitignore` để loại trừ `.env`, `include/secrets.h`, access token, database, log, `.venv`, `.pio`, `node_modules`, cache Python, bản sao lưu và credential/runtime Node-RED.
  **Lý do:** Không đưa bí mật hoặc file sinh tự động lên repository công khai, đồng thời giữ repository gọn và có thể tái tạo từ các file cấu hình mẫu.
- **Kết quả:** Push nhánh `main` thành công; kiểm tra staging không tìm thấy device token hoặc Wokwi license. 10/10 kiểm thử Python đạt, `diagram.json` và `node_red_flow.json` hợp lệ, `settings.js` vượt qua `node --check`.
