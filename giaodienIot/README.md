# Đèn đường 3D — Digital Twin ba cột

Cảnh Three.js mô phỏng một đoạn đường với ba cột ở đầu, giữa và cuối tuyến; **chỉ quan sát** ba kênh `lamp-01`, `lamp-02`, `lamp-03`. AUTO chung, ngưỡng lux và ON/OFF riêng vẫn dùng widget có sẵn của dự án `C:\IOT\BTL-IOT-Nhom-4`; cảnh 3D không gửi lệnh tới thiết bị.

## Phân biệt DEMO và hệ thống thật

- `index.html`: demo độc lập, tạo dữ liệu giả ngay trong trình duyệt. Số mA/V/lux và trạng thái lệnh **không phải telemetry thực**. Trang demo tập trung vào mô hình và bảng điều khiển giả lập. Demo dùng điện áp minh họa xấp xỉ 5 V; ON/OFF riêng chỉ dùng khi cả ba ở MANUAL. AUTO demo bật khi `lux < T−50`, tắt khi `lux > T`, giữ trạng thái ở hai biên, vùng trễ hoặc khi lux lỗi.
- `node-red/ui_template.html`: bản nguồn widget hiển thị snapshot nhận từ đầu ra đã xác minh của Python; không chứa thông tin đăng nhập, HMAC key hay MQTT client. Giữ bản nguồn này cho đến khi xác nhận widget đã được import/Deploy trên Node-RED đang chạy.
- `node-red/flows_ready.json`: **fragment chưa nối dữ liệu**, dành cho việc gắn widget thủ công. Không có inject/mock 3 giây, không có điều khiển production. **Không import cùng bản flow chính** vì sẽ có hai widget 3D. Có thể bỏ fragment nếu không dùng cách gắn tay; file flow chính trên đĩa chưa chứng minh Node-RED đã Deploy.
- `three.min.js`: Three.js r128 local. Ba bản lưu `.txt` của mô hình lớp học cũ đã được loại bỏ vì không tham gia chạy hoặc triển khai.

## Triển khai vào Node-RED hiện hữu

1. Sao lưu flow Node-RED đang hoạt động trước khi import. Không thay thế tab/điều khiển hiện có bằng `flows_ready.json`.
2. Bật `httpStatic` trong `settings.js` của Node-RED trỏ đến một thư mục public do bạn quản lý, ví dụ `C:/Users/Admin/.node-red/public/`; khởi động lại Node-RED nếu thay cài đặt này. Đặt `three.min.js` tại `public/static/vendor/three.min.js`, `classroom_twin_3d.js` tại `public/static/classroom_twin_3d.js`. Kiểm tra hai URL `/static/vendor/three.min.js` và `/static/classroom_twin_3d.js` trả về script trước khi deploy.
3. Flow ưu tiên: `C:\IOT\BTL-IOT-Nhom-4\node_red_flow.json` chứa widget 3D trong Dashboard chính. `mqtt-verified` → `fn-snapshot` → output 17 → cảnh 3D; 16 output đầu và các widget AUTO/slider/ON-OFF vẫn giữ nguyên. Khi import, chọn phương thức không tạo bản sao tab cũ; kiểm tra ID/nối dây và broker trước khi Deploy.
4. Nếu cần gắn thủ công vào Dashboard đang chạy, chỉ lấy nội dung `ui_template.html`, tạo một `ui_template` trong tab hiện hữu và nối một nhánh từ snapshot verified (`city/digital-twin/verified/telemetry` qua function hiện tại). Fragment `flows_ready.json` chỉ để tham khảo/import độc lập khi muốn tạo tab mới; bản thân nó không hiển thị dữ liệu cho đến khi nối nguồn verified.
5. Chỉ Deploy flow live sau khi kiểm tra và được phép triển khai. Trình duyệt không được kết nối topic lệnh hay chứa secret. Khi giao diện tải mà chưa có snapshot, ba đèn sẽ hiển thị chưa rõ; khi không nhận bản tin mới trong khoảng 16 giây sẽ ghi dữ liệu cũ thay vì báo ONLINE.

## Schema snapshot thực

`digital_twin_server.py:build_snapshot()` phát object dạng sau; các giá trị trong ví dụ chỉ minh họa **schema**, không phải bản ghi thực:

```json
{
  "verifiedAt": 1790000000000,
  "complete": true,
  "lux": 85.0,
  "luxValid": true,
  "autoOffLux": 150,
  "gatewayPending": false,
  "gatewayStatus": "IDLE",
  "gatewayReportedMode": null,
  "gatewayReportedThreshold": null,
  "lamps": {
    "lamp-01": {"available": true, "stale": false, "sourceTs": 1790000000000, "state": "ON", "mode": "AUTO", "currentMa": 410.0, "voltageV": 5.0, "sensorValid": true, "autoOffLux": 150},
    "lamp-02": {"available": true, "stale": false, "sourceTs": 1790000000000, "state": "ON", "mode": "AUTO", "currentMa": 410.0, "voltageV": 5.0, "sensorValid": true, "autoOffLux": 150},
    "lamp-03": {"available": true, "stale": false, "sourceTs": 1790000000000, "state": "OFF", "mode": "AUTO", "currentMa": 0.0, "voltageV": 5.0, "sensorValid": true, "autoOffLux": 150}
  }
}
```

Snapshot không có trường `timestamp` cấp cao hoặc timestamp dạng chuỗi cho từng đèn: dùng `verifiedAt` và `sourceTs`. Khi `available:false`/`stale:true`, giao diện giữ trạng thái đã xác minh cuối và hiển thị một cảnh báo duy nhất trong bảng chi tiết; khi chưa từng thấy đèn thì không đoán ON/OFF. Cột đèn trên cảnh là hình ảnh đại diện kênh, không thay đổi `diagram.json` của Wokwi. Khi sensor invalid, dòng/áp không được xem là số đo hợp lệ.

## Kiểm tra

Mở `index.html` để xem demo giả lập; trang kiểm thử mock riêng đã được loại khỏi bộ file tối thiểu. Với Node-RED, sau khi được phép Deploy, thử ngắt luồng snapshot và xác nhận giao diện báo dữ liệu cũ; lệnh thật phải đi qua Python ký → ESP32 ACK/telemetry → snapshot verified. Kết quả demo hoặc import flow đơn thuần không phải xác nhận kiểm thử xuyên suốt hay phần cứng thật.
