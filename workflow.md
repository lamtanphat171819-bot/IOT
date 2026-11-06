# Quy trình thu thập dữ liệu và thử nghiệm ML

Tài liệu này bổ sung quy trình **ngoại tuyến** để ghi telemetry MQTT thành CSV, đánh giá mô hình dự đoán độ ẩm đất sau 10 phút và thử nghiệm phân loại trạng thái. Dashboard có hiển thị trạng thái baseline từ ngưỡng điều khiển; mô hình ML không được nạp vào dashboard/firmware, không gửi lệnh MQTT và không điều khiển thiết bị.

## 1. Chuẩn bị

- Dùng Python 3.10 trở lên và mở PowerShell tại thư mục gốc dự án.
- Tạo môi trường ảo rồi cài các gói cần cho collector và huấn luyện:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

- ESP32/Wokwi hiện phát telemetry lên `broker.hivemq.com:1883`, topic `qhuy-greenhouse/data`. Collector mặc định dùng đúng broker/topic này. Có thể truyền `--broker`, `--port` và `--topic` để dùng broker riêng.
- Thiết bị phải đang kết nối MQTT và gửi JSON telemetry hợp lệ. Collector ghi timestamp UTC lúc máy thu nhận từng bản tin; timestamp này không phải đồng hồ đo được trên ESP32.

## 2. Thu thập dữ liệu

```powershell
python tools/collect_telemetry.py
```

Mặc định dữ liệu được nối thêm vào `data/telemetry.csv`; thư mục và tiêu đề CSV được tạo tự động. Dừng collector bằng `Ctrl+C`. Chỉ chạy một collector cho mỗi tệp. Có thể chọn đường dẫn khác:

```powershell
python tools/collect_telemetry.py --output data\greenhouse.csv
```

Mỗi dòng chứa timestamp UTC, nhiệt độ, độ ẩm không khí, độ ẩm đất, ánh sáng, trạng thái thiết bị/chế độ và các trường lịch tưới. Bản tin sai định dạng, thiếu trường hoặc ngoài miền hợp lệ sẽ bị bỏ qua kèm thông báo lỗi. Mất kết nối, máy ngủ, hoặc broker công cộng có thể làm mất mẫu; CSV không tự tái tạo dữ liệu thiếu.

Thu thập **dữ liệu thật đại diện cho điều kiện cần dự đoán**: các chu kỳ khô/ướt, tưới, thay đổi môi trường, và nhiều ngày nếu có thể. Mô phỏng biến trở/Wokwi hữu ích để kiểm tra luồng dữ liệu, nhưng không chứng minh dự báo hữu dụng trong nhà kính thật. Mặc định huấn luyện cần ít nhất 100 mẫu huấn luyện và 20 mẫu kiểm định sau khi tạo nhãn; với lấy mẫu mỗi phút, cần dữ liệu liên tục đáng kể (thường nhiều giờ, và nên thu thập lâu hơn).

## 3. Huấn luyện và kiểm định theo thời gian

```powershell
python tools/train_soil_moisture.py data\telemetry.csv
```

Script kiểm tra tiêu đề, timestamp có múi giờ, giá trị số hữu hạn/trong miền hợp lý và timestamp trùng lặp. Nhãn là độ ẩm đất tại mẫu gần nhất với thời điểm **10 phút sau** mỗi hàng đầu vào, tối đa lệch mặc định 30 giây. Để tránh hàng telemetry gần như trùng nhau chi phối kết quả, mặc định chỉ chọn một ví dụ mỗi 60 giây. Các mẫu không có nhãn tương lai trong dung sai sẽ bị loại.

80% ví dụ sớm hơn được dùng làm train; phần sau dùng làm validation theo thứ tự thời gian. Các ví dụ train có nhãn chạm hoặc vượt điểm bắt đầu validation bị loại để tạo khoảng cách tránh rò rỉ nhãn qua ranh giới. Mặc định cần tối thiểu 100 ví dụ train và 20 ví dụ validation sau bước này. Nếu thiếu, script dừng với lỗi rõ ràng thay vì huấn luyện trên dữ liệu quá ít.

Tham số có thể điều chỉnh khi chu kỳ đo khác, nhưng cần chọn theo tần suất thực tế của dữ liệu:

```powershell
python tools/train_soil_moisture.py data\telemetry.csv --horizon-minutes 10 --tolerance-seconds 30 --sample-interval-seconds 60
```

Không nới dung sai chỉ để ép tạo thêm nhãn: nhãn lệch khỏi 10 phút sẽ làm thay đổi câu hỏi dự đoán. Mô hình Random Forest dùng các cảm biến hiện tại và trạng thái thiết bị; nó chỉ được fit trên phần train và được đánh giá trên phần thời gian muộn hơn.

## 4. Phân loại trạng thái môi trường (thử nghiệm offline)

Dashboard hiển thị đồng thời các trạng thái theo ngưỡng hiện tại: **nhiệt độ cao** (`temp > 32 °C`), **đất khô** (`soil < 35%`) và **thiếu sáng** (`light < 30%`). Nếu không điều kiện nào đúng, giao diện hiển thị “Ổn định theo ngưỡng”. Đây là bộ luật baseline minh bạch, **không phải dự đoán của mô hình ML**. Độ ẩm không khí chưa được phân loại vì project chưa định nghĩa ngưỡng vận hành cho chỉ số này. Ngưỡng đất/ánh sáng hiện là phần trăm quy ước, chưa hiệu chuẩn.

Script dưới đây huấn luyện một bộ phân loại đa nhãn Random Forest trên các mẫu telemetry. Nhãn được sinh tự động từ đúng các ngưỡng baseline nói trên để minh họa quy trình ML:

```powershell
python tools\train_environment_classifier.py data\telemetry.csv
```

Tập validation là 20% mẫu mới nhất theo thời gian. Mỗi trạng thái cần có cả ví dụ dương và âm ở cả train lẫn validation; nếu dữ liệu hiện tại chưa bao phủ đủ điều kiện, script sẽ dừng và báo nhãn còn thiếu. Kết quả chỉ đo khả năng mô hình **bắt chước nhãn do luật tạo ra**, không xác nhận các ngưỡng là đúng cho cây trồng và không chứng minh mô hình có lợi hơn luật. Mẫu liên tiếp gần như trùng nhau cũng không tương đương nhiều tình huống độc lập. Muốn đánh giá ML có ý nghĩa hơn, cần chuyên gia/người dùng gắn nhãn độc lập theo mục tiêu canh tác và kiểm định trên các giai đoạn/điều kiện chưa dùng khi huấn luyện.

Mô hình này chạy offline, không được dashboard tải vào và không điều khiển thiết bị. Không dùng trạng thái giao diện hoặc dự đoán thử nghiệm để tự động bật bơm/quạt/đèn.

## 5. Đọc kết quả

- **MAE**: sai số tuyệt đối trung bình theo điểm phần trăm độ ẩm đất; càng thấp càng tốt.
- **RMSE**: phạt sai số lớn nhiều hơn MAE; xem cả hai để nhận biết dự báo lệch mạnh.
- **Persistence**: dự báo độ ẩm sau 10 phút bằng chính độ ẩm hiện tại. Đây là baseline đơn giản; mô hình chỉ đáng xem xét nếu cải thiện ổn định trên dữ liệu kiểm định độc lập.
- Tỷ lệ “better/worse” chỉ so sánh MAE của đúng lần chia thời gian này, không phải bằng chứng tổng quát. Hãy lưu các lần đo/điều kiện thử nghiệm và đánh giá lại trên giai đoạn mới.

Không có ngưỡng MAE nào được đảm bảo hữu ích cho mọi loại đất/cảm biến. Cần hiệu chuẩn cảm biến, kiểm tra dữ liệu thật, so sánh trên nhiều giai đoạn tách biệt và xác nhận sai số có ý nghĩa đối với mục đích sử dụng trước khi đưa ra quyết định.

## 6. Giới hạn và an toàn

- Dự báo chỉ là thử nghiệm ngoại tuyến, không được triển khai vào firmware/dashboard và không điều khiển bơm, quạt, đèn hay servo.
- Telemetry hiện không có timestamp đo tại thiết bị; thời gian nhận phụ thuộc đồng hồ máy thu và mạng. Mất mạng làm dữ liệu thưa, nên không thể đảm bảo nhãn chính xác theo thời gian đo.
- Độ ẩm đất là phần trăm quy ước từ ADC, cần hiệu chuẩn theo cảm biến, đất và cách lắp đặt. Dữ liệu biến trở Wokwi là dữ liệu mô phỏng, không đại diện cảm biến thực.
- Dữ liệu liên tiếp có tương quan; một lần chia thời gian và nhiều mẫu không thay cho kiểm định lặp lại theo mùa vụ/điều kiện khác nhau. Mô hình ngoài vùng dữ liệu thu thập có thể dự báo sai.
- Broker mặc định là MQTT công cộng không xác thực, topic có thể bị nghe/giả mạo và topic lệnh firmware hiện cũng công khai. Chỉ dùng để học/mô phỏng; không dùng broker/topic này cho tải thật. Với phần cứng thật, ưu tiên broker riêng có TLS/xác thực và cơ chế điều khiển an toàn độc lập.
- Không để mô hình hoặc collector thay thế giám sát, bảo vệ điện/nước, giới hạn thời gian bơm, hoặc biện pháp an toàn phần cứng.
