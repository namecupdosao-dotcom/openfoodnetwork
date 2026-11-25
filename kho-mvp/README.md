# Kho Nông Sản Xanh - MVP (Frontend + Backend + Database thật)

## Chạy trên máy (chỉ cần Python 3.9 trở lên, không cài thêm gói)
    python server.py
    KHO_DEMO=1 python server.py     # chỉ cần cho nút Cập nhật GPS mô phỏng trên trang tài xế (/tai-xe)
Mở http://127.0.0.1:8000  (Windows nếu gõ `python` không được thì dùng `py server.py`)
Xóa file kho.db để nạp lại dữ liệu mẫu.

Tài khoản (mật khẩu 123456): quanly (Quản lý kho), thukho (Thủ kho), qa (QA/QC), kinhdoanh (Kinh doanh), nongdan (Nông dân, Nông trại Rau Sạch Đà Lạt Xanh), nongdan2 (Nông dân, HTX Trái Cây Cái Bè).

## Kiểm thử
    python test_api.py     # 20 bài kiểm thử API (có phân quyền vận chuyển), gồm tranh chấp tồn kho đồng thời, thu hồi lô, kế hoạch, giá
    python test_chain.py   # 23 bài kiểm thử chuỗi 3 bên (có vận chuyển 2 chặng xuyên suốt, cold-chain, PIN tài xế, nâng cấp CSDL cũ), gồm hủy kèo theo tiến độ, phạt, miễn trừ, nâng cấp CSDL cũ

## Kiến trúc
- Frontend: static/index.html (HTML/CSS/JS thuần) gọi REST API bằng fetch.
- Backend: server.py (Python, http.server), xác thực bằng token phiên, phân quyền theo vai trò.
- Database: SQLite (kho.db), có khóa ngoại, ràng buộc CHECK, giao dịch BEGIN IMMEDIATE.

## Hai luồng cốt lõi
1. Nhập kho: Thủ kho lập phiếu -> máy chủ kiểm tra (điều kiện bảo quản, hạn dùng, sức chứa) -> Quản lý kho duyệt -> tạo lô + ghi sổ cái -> QA kiểm định (dư lượng, thời gian cách ly) -> lô Đạt mới được bán.
2. Đặt hàng -> Xuất kho: Kinh doanh tạo đơn (kiểm tồn khả dụng) -> Thủ kho hoặc Quản lý kho bấm Giữ hàng (FEFO) -> Thủ kho xuất kho (trừ tồn, ghi sổ cái) ; hủy đơn sẽ nhả hàng.

## Bảng dữ liệu
users, sessions, products, warehouses, suppliers, customers, lots, receipts, orders, order_items, reservations, stock_ledger, audit_log, trips, trip_points, recalls, recall_items, plans, market_prices, requirements.
Quan hệ chính: suppliers 1-n receipts, receipts 1-1 lots, lots 1-n stock_ledger, orders 1-n order_items, orders n-n lots (qua reservations).

## API chính
POST /api/login · GET /api/me /api/meta /api/dashboard /api/report /api/audit
GET /api/lots, GET /api/lots/{id}, POST /api/lots/{id}/inspect, POST /api/lots/{id}/stocktake
GET|POST /api/receipts, POST /api/receipts/{id}/approve|reject
GET|POST /api/orders, POST /api/orders/{id}/reserve|ship|cancel

GET /api/lots/{id}/trace · GET /api/lots/{id}/qr · GET /api/qa · GET /api/lots/{id}/recall-impact · POST /api/lots/{id}/recall · POST /api/recalls/{id}/notify|close
GET|POST /api/plans, POST /api/plans/{id}/update (confirm|postpone|cancel|received) · GET|POST /api/prices · GET /api/requirements

## Bản gộp (Mục 4): 11 trang, một sản phẩm duy nhất
Tổng quan · Tồn kho · Nhập kho · Kiểm định và thu hồi · Đơn hàng · Vận chuyển (GPS) · Truy xuất nguồn gốc · Kế hoạch và giá thị trường · Báo cáo · Đối chiếu 289 yêu cầu · Nhật ký. Tất cả đều gọi API thật.
- Thu hồi lô: khóa lô, nhả hàng đang giữ (đơn về Mới), liệt kê đơn và khách đã nhận, ghi nhận đã thông báo, đóng thu hồi (hủy hoặc trả nhà cung cấp, tồn về 0 qua sổ cái).
- Truy xuất: nông trại, thu hoạch, phiếu nhập, kiểm định, tồn, đơn hàng, khách, chuyến xe. Có nút in.
- Kế hoạch: lập, xác nhận, dời, hủy, đã nhập kho; cân đối cung cầu 14 ngày. Giá: thị trường theo chợ, giá thu mua, biểu đồ 30 ngày.
- Đối chiếu yêu cầu: data/requirements.json (289 dòng, trạng thái Đã làm thật / Làm một phần / Hướng phát triển) nạp vào bảng requirements mỗi lần khởi động.
- Nâng cấp từ kho.db cũ: chạy server.py là tự tạo bảng mới và nạp dữ liệu mẫu, không mất dữ liệu cũ.
- Quyền giuhang (giữ hàng FEFO): chỉ Quản lý kho và Thủ kho. Kinh doanh tạo và hủy đơn nhưng không thấy nút, và API trả 403.
- Quyền mới: kehoach (Quản lý kho, Thủ kho), gia (Quản lý kho, Kinh doanh). Thu hồi dùng quyền kiểm định (QA, Quản lý kho).

## Vận chuyển 2 chặng (Nông dân -> Kho -> Cửa hàng) và GPS
- **Chặng 1 (VC-IN-yymmdd-nn)**: tự sinh cùng giao dịch khi nông dân bấm Lập phiếu và sinh mã QR (mỗi phiếu tối đa một chuyến, có ràng buộc UNIQUE). Điểm đi là nông trại, điểm đến là kho nhận, biển số và tài xế lấy từ phiếu giao (form có thêm Tên tài xế, SĐT tài xế). Kho tiếp nhận phiếu thì chuyến tự chuyển Đã giao hàng; phiếu bị từ chối thì chuyển Sự cố kèm lý do.
- **Chặng 2 (VC-OUT-yymmdd-nn)**: tự sinh khi Thủ kho hoặc Quản lý kho xuất kho đơn hàng. Khi bấm Xuất kho có hộp chọn xe và tài xế (gợi ý từ danh sách xe, cho sửa). Mỗi đơn một chuyến. Hàng trên xe được truy ngược: lô LO -> phiếu nhập PN -> phiếu giao GH -> nông dân và nông trại; lô nhập thủ công ghi "Nhập kho trực tiếp, nhà cung cấp: ...".
- **Trạng thái**: Chờ lấy hàng, Đang vận chuyển, Đã giao hàng, Sự cố, Đã hủy. "Sắp đến nơi" không lưu, chỉ hiển thị khi đang vận chuyển và tiến độ từ 85%.
- **Số liệu suy ra từ điểm GPS mới nhất** (không lưu trùng): vị trí và tên trạm, tiến độ %, km đã đi, ETA, tốc độ, nhiệt độ, cảnh báo. Tuyến dự kiến lấy từ bảng route_templates theo cặp vùng (không gọi API bản đồ, nên là tuyến gần đúng), không có thì nội suy đường thẳng; quãng đường = haversine x 1,25.
- **Cold-chain**: nhiệt độ vượt ngưỡng của mặt hàng thì hiện đỏ; vượt liên tiếp 3 điểm (hoặc 15 phút) thì sinh cảnh báo, ghi nhật ký, hiện ở Tổng quan của Quản lý kho/Thủ kho và nhãn "Cần kiểm tra nhiệt độ khi nhập" ở tab Thu mua QC (QA chỉ thấy nhãn, không thấy dữ liệu GPS). Quá 10 phút không có điểm mới thì gắn nhãn "Mất tín hiệu GPS", không bịa vị trí.
- **Phân quyền (lọc ở backend)**: Quản lý kho và Thủ kho xem và điều phối cả 2 chặng, thấy liên kết và PIN tài xế. Kinh doanh chỉ xem (không có token, PIN, SĐT tài xế). Nông dân/HTX chỉ thấy chặng 1 của chính mình (chuyến người khác trả 404). QA/QC không có tab Vận chuyển, API trả 403. Tham số `?leg=in|out` lọc theo chặng.
- **Tài xế** (/tai-xe): mở bằng liên kết có token (QR tài xế, khác hoàn toàn QR truy xuất công khai), hoặc nhập biển số + PIN 6 số (PIN suy ra từ biển số, chỉ lưu bản băm; sai quá 5 lần trong 10 phút thì khóa tạm). Server giới hạn 5 giây giữa 2 điểm GPS (đổi bằng biến môi trường KHO_GPS_MIN_INTERVAL), loại điểm có tốc độ trên 120 km/h hoặc nhảy xa bất thường.
- **Trang truy xuất công khai** có khối Hành trình vận chuyển 2 chặng, ẩn tên và địa chỉ cửa hàng (ghi "Điểm bán lẻ"), SĐT tài xế, biển số (che dạng 51D-***.45).
- **Mô phỏng giao hàng** (Thủ kho, Quản lý kho; thay cho nút Bắt đầu chuyến): ở tab Vận chuyển, chặng 1 và chặng 2 đều có nút Mô phỏng giao hàng mở bảng chi tiết (tiến độ trên tuyến, vị trí, tốc độ, nhiệt độ, ETA, các bước bốc hàng - xuất phát - qua trạm - đến nơi, hàng trên xe và nguồn gốc, nhật ký hành trình), chạy tự động hoặc từng nhịp, tốc độ x1/x2/x4. Không cần `KHO_DEMO`; chuyến đã có GPS thật thì không mô phỏng được.
- **Chế độ demo**: nút Cập nhật GPS mô phỏng trên trang tài xế chỉ chạy khi khởi động với `KHO_DEMO=1`; điểm sinh ra có src = SIM và giao diện ghi "Mô phỏng". Không trộn điểm mô phỏng với GPS thật trên cùng một chuyến. Nhiệt độ thùng xe là số liệu mô phỏng nếu thiết bị không gửi `temp`.
- **Nâng cấp CSDL cũ**: init_db tự dựng lại bảng trips (giữ dữ liệu), đổi mã CX-... thành VC-OUT-... (mã cũ nằm trong legacy_code, liên kết tài xế cũ vẫn dùng được), đổi trạng thái CHUAN_BI/DANG_GIAO thành CHO_LAY_HANG/DANG_VAN_CHUYEN, bổ sung chuyến chặng 1 cho các phiếu giao đã có. Chạy lại nhiều lần vẫn an toàn.

## Truy xuất theo từng chuyến xuất kho
- Cùng một lô có thể tách thành nhiều chuyến chặng 2 (mỗi đơn một chuyến). Hành trình luôn là: chặng 1 của lô + MỘT chuyến chặng 2 được chọn; chuyến của cửa hàng khác không bao giờ bị gộp vào.
- QR nhập kho (và QR cũ không có `s`): `/t/<mã lô>?k=...` chỉ hiện đến bước nhập kho (chặng 1), không lộ chặng 2.
- QR tem xuất kho: `/t/<mã lô>?s=<mã chuyến VC-OUT>&k=...`, với `k` = HMAC(mã lô|mã chuyến) nên không dùng được cho chuyến khác; tem in qua `/tem?c=..&k=..&s=..`.
- Nội bộ (kể cả Kinh doanh, nay có mục Truy xuất): gõ mã lô thì chọn từng chuyến (Chuyến #1: Kho → Cửa hàng C, #2: ... D) rồi mới xem hành trình và QR/tem của chuyến đó. API: `GET /api/lots/{id}/trace?ship=<mã chuyến>`, `GET /api/lots/{id}/qr?ship=...`, `GET /api/public/lot?c=&k=&s=`. Ở bảng hàng trên xe của chuyến chặng 2 có liên kết "Truy xuất theo chuyến này".

## Đơn hàng: Đã nhận hàng và Truy xuất theo đơn (Kinh doanh, Quản lý kho)
- Trạng thái mới `DA_NHAN_HANG`. `POST /api/orders/{id}/receive` (quyền donhang) chỉ nhận đơn `DA_XUAT_KHO`, ngược lại 409. Chuyến chặng 2 đang chạy tự chuyển Đã giao, ghi nhật ký, và RFQ liên kết (nếu chưa Hoàn tất) tự chuyển Hoàn tất. Xác nhận ở tab Thu mua RFQ cũng đồng bộ đơn sang Đã nhận hàng.
- `GET /api/orders/{id}/trace`: lô của đơn kèm đường dẫn QR `/t/<lô>?order=<id>&k=...` (k ký theo lô và đơn). Trang công khai chỉ hiện chặng 1 và chuyến chặng 2 của chính đơn đó. Nút "Truy xuất lô và quét QR" và "Đã nhận hàng" nằm ở tab Đơn hàng.
- CSDL cũ: `migrate_orders` dựng lại bảng orders (giữ dữ liệu) để thêm trạng thái mới, chạy lại nhiều lần vẫn an toàn.

## QR thật và trang truy xuất công khai (người mua không cần đăng nhập)
- Mỗi lô có mã QR thật (chuẩn QR Model 2, mức sửa lỗi M) do `qr.py` tự sinh bằng Python thuần, không cần cài thư viện. Đã kiểm tra giải mã bằng OpenCV trên hàng trăm đường dẫn mẫu.
- Trang Truy xuất (nội bộ) hiện mã QR của lô, nút Mở trang công khai, Sao chép liên kết, In tem QR và Quét tem QR bằng camera.
- Người mua quét mã sẽ mở `/t/<mã lô>?k=<mã xác thực>`: trạng thái kiểm định, xuất xứ, ngày thu hoạch, hạn dùng, dư lượng so với giới hạn, thời gian cách ly thuốc, hành trình nông trại - kho - kiểm định, cảnh báo thu hồi. Trang này không hiện khách hàng, đơn hàng, giá, tồn kho, tên nhân viên.
- Mã xác thực `k` = HMAC-SHA256(khóa, mã lô) cắt 12 ký tự: người ngoài không đoán được đường dẫn của lô khác, sửa mã lô trong URL sẽ báo không hợp lệ. Khóa mặc định chỉ để demo, đặt biến môi trường `KHO_QR_SECRET` khi triển khai thật.
- Trang `/t` (không kèm mã) có nút quét QR bằng camera (Chrome trên Android). Trang `/tem?c=...&k=...` in tem nhãn 64x40 mm, chọn số tem.
- Địa chỉ trong mã QR lấy theo đường dẫn bạn đang mở hệ thống. Nếu mở bằng localhost, điện thoại sẽ không quét được. Có thể ép cố định bằng biến `KHO_PUBLIC_URL` (ví dụ https://ten-mien-cua-ban).
- Trên GitHub Codespaces: mở tab Ports, chuột phải cổng 8000, Port Visibility chọn Public. Nếu để Private thì điện thoại sẽ bị yêu cầu đăng nhập GitHub và không xem được trang công khai.
- API công khai: GET /api/public/lot?c=...&k=... · GET /qr.svg?c=...&k=... (chỉ tạo ảnh cho cặp mã hợp lệ). Có tiêu đề bảo mật: Referrer-Policy no-referrer, Content-Security-Policy, Cache-Control no-store.

## Chuỗi cung ứng 3 bên (Mục 5): Nông dân - Kho - Kinh doanh
Giao diện: Nông dân có 4 trang (Chào bán, Kèo thu mua, Giao hàng, Công nợ); Quản lý kho và Thủ kho có thêm trang Thu mua QC; Kinh doanh có thêm trang Thu mua RFQ. Nông dân chỉ gọi được API dành cho nông dân, không thấy đơn hàng, tồn kho, khách hàng.

Luồng A, sàn thu mua + QC kho:
1. Nông dân đăng chào bán (sản phẩm, sản lượng, giá mong muốn, ngày thu hoạch, tiêu chuẩn).
2. Quản lý kho duyệt chốt mua (có thể sửa giá chốt), trạng thái Chờ giao hàng.
3. Nông dân lập phiếu giao hàng (GH-...) và nhận mã QR trước khi đưa hàng lên kho.
4. Thủ kho/Quản lý kho cân đo: Loại 1, Loại 2, hư hỏng, dư lượng, đúng tiêu chuẩn. Chỉ phần Loại 1 + Loại 2 được cộng vào tồn kho (tạo lô, ghi sổ cái). Nếu dư lượng vượt giới hạn, chưa đủ thời gian cách ly hoặc không đúng tiêu chuẩn thì lô vẫn tạo nhưng ở trạng thái Cách ly, chờ QA, chưa bán được.
5. Công nợ nông dân = Loại 1 x giá + Loại 2 x giá x 85%. Quản lý kho bấm Thanh toán. Tỷ lệ hao hụt = (khai báo - thực đạt) / khai báo.

Luồng B, RFQ khép kín:
1. Kinh doanh đăng RFQ (nông sản, sản lượng, VietGAP/GlobalGAP/Hữu cơ, khoảng giá, ngày giao, % cọc). Hệ thống chặn nếu ngày giao sớm hơn thời gian sinh trưởng tối thiểu (hằng số GROW_DAYS trong server.py, là số liệu minh họa).
2. Nông dân nhận kèo một phần hoặc trọn gói (đúng khoảng giá, đủ chứng nhận). Kinh doanh duyệt nhiều kèo cho đến khi đủ sản lượng (RFQ chuyển Đủ sản lượng). Tiền cọc = sản lượng x giá x % cọc, ghi nhận khi duyệt. Hủy kèo đã duyệt thì mất cọc, giao đủ và được thanh toán thì hoàn cọc.
3. Nông dân cập nhật tiến độ canh tác, lập phiếu giao hàng cho kèo, giao về kho. Kho QC như luồng A.
4. Kinh doanh bấm Tạo đơn xuất kho, Thủ kho/Quản lý kho giữ hàng (FEFO) rồi xuất kho ở trang Đơn hàng, Kinh doanh xác nhận nhận hàng và tất toán. Nút Truy xuất lô mở trang công khai giống khi quét QR.

QR phiếu giao hàng: dạng /t/GH-... Trước khi kho nhập thì trang công khai báo Đang chờ kho nhận và kiểm định, sau khi nhập thì tự chuyển sang hồ sơ của lô thật.
API mới: GET /api/offers /api/batches /api/rfqs, GET /api/batches/{id}/qr, GET /api/rfqs/{id}/trace, POST /api/farm/offers /api/farm/batches /api/farm/rfqs/{id}/commit /api/farm/commits/{id}/progress|cancel, POST /api/offers/{id}/decide, /api/batches/{id}/receive|pay, /api/rfqs, /api/rfqs/{id}/order|receive, /api/commits/{id}/decide.
Nâng cấp từ kho.db cũ: chạy server.py là tự dựng lại bảng users (thêm vai trò Nông dân), tạo bảng mới và nạp dữ liệu mẫu, không mất dữ liệu cũ.
Chưa có: đặt cọc qua cổng thanh toán thật (cọc chỉ ghi nhận số liệu), hợp đồng điện tử, thông báo đẩy, khấu trừ tạm ứng, tách công nợ bán hàng cho doanh nghiệp (hiện chỉ xác nhận tất toán theo giá trị đơn).

## Gợi ý sức chứa kho
- Sức chứa tối đa (hằng số WH_CAPACITY trong server.py): Kho lạnh Củ Chi 5.000.000 kg, Kho mát Cái Bè 8.000.000 kg, Kho Đà Lạt 3.500.000 kg. Chạy server.py sẽ tự cập nhật cho kho.db cũ.
- Sức chứa khả dụng = tối đa - tồn kho hiện tại - tổng phiếu nhập Chờ duyệt của kho đó. Lập phiếu là khóa chỗ tạm (vượt thì trả 409); Từ chối phiếu thì chỗ được hoàn lại. Nhận hàng nông dân (QC) cũng tính phần chỗ đang khóa.
- API: GET /api/capacity. Trang Tồn kho có thẻ và thanh sức chứa từng kho; form Lập phiếu nhập hiện badge xanh/vàng (trên 85%)/đỏ kèm gợi ý kho khác; phiếu Chờ duyệt có nhãn sức chứa kho nhận.

## Giới hạn (nên nói rõ khi báo cáo)
Chưa có: quên mật khẩu, 2FA, ứng dụng di động riêng, thanh toán, hóa đơn, quét QR để nhập/xuất kho, tem cho pallet/thùng, kết nối máy in tem, chữ ký số và chống sao chép tem QR, điều chuyển kho, cảm biến nhiệt độ kho, cổng riêng cho khách hàng (nông dân đã có cổng từ Mục 5). Giá thị trường và kế hoạch mẫu là dữ liệu do nhóm tự tạo. Mật khẩu băm PBKDF2; chưa dùng HTTPS (cần reverse proxy khi triển khai thật).

## Giao diện (Mục 3)
- Thương hiệu: logo và favicon là file SVG (static/logo.svg, static/favicon.svg), dễ thay sau này. Màu: xanh chanh #C8F03C, mực xanh đậm #12261B, xanh lá #0B7A3E.
- Font Be Vietnam Pro (tải từ Google Fonts, có font hệ thống dự phòng khi mất mạng).
- Có chế độ sáng/tối, trên điện thoại menu nằm ở đáy màn hình và bảng chuyển thành thẻ.
- Sửa tên môn, nhóm, phiên bản ở đầu đoạn script trong static/index.html (biến INFO).

## Hủy kèo thu mua theo tiến độ canh tác (nông dân)
| Tiến độ canh tác | Nút trên tài khoản nông dân |
|---|---|
| Chưa bắt đầu, Đã gieo trồng | **Hủy kèo** (tự hủy, kèo đã duyệt thì mất cọc như trước) |
| Đang chăm sóc | Còn cách hạn giao (Giao trước) **trên 10 ngày**: **Hủy kèo** tự hủy. Từ 10 ngày trở xuống: **Xin hủy kèo** (doanh nghiệp duyệt, phạt Mức 1) |
| Sắp thu hoạch, Đã thu hoạch | Nút Hủy kèo **bị vô hiệu hóa** (khóa cứng), API trả 409 |

- Xin hủy kèo: nông dân chọn lý do (Sâu bệnh, Thiên tai, Mất mùa, Lý do khác), mô tả chi tiết (tối thiểu 10 ký tự) và đính kèm 1 đến 5 ảnh (bắt buộc; trình duyệt tự nén về tối đa 1280 px, server kiểm tra định dạng và dung lượng). Kèo chuyển sang **Chờ duyệt hủy**, trang Thu mua RFQ của doanh nghiệp hiện cảnh báo KHẨN và nút Xem yêu cầu hủy. Trong lúc chờ, kèo vẫn giữ sản lượng, nông dân không cập nhật tiến độ hay hủy tiếp được.
- Doanh nghiệp **Duyệt hủy**: kèo thành Đã hủy, số kg thiếu tự cộng lại vào RFQ (RFQ Đủ sản lượng mở lại để gom nông dân khác), áp dụng phạt. **Từ chối hủy**: kèo trở về Đã duyệt, nông dân phải thực hiện tiếp. Tích **Miễn trừ phạt** (thiên tai, dịch bệnh bất khả kháng): hoàn cọc, không trừ điểm, không khóa.
- Phạt Mức 1 (Đang chăm sóc): mất 100% cọc, trừ 10 điểm uy tín, khóa nhận kèo mới 3 ngày.
- Phạt Mức 2 (Sắp thu hoạch): mất 100% cọc, trừ 25 điểm, khóa 7 ngày, chỉ được nhận tối đa 30% sản lượng mỗi RFQ trong 30 ngày. Mức này mặc định **không dùng** vì Sắp thu hoạch đang khóa cứng. Đặt `ALLOW_REQUEST_AT_HARVEST = True` trong server.py để cho Xin hủy kèo ở giai đoạn này.
- Doanh nghiệp thấy điểm uy tín và tỷ lệ hủy kèo của nông dân ngay trên từng kèo khi xét duyệt.
- Tiến độ canh tác chỉ được cập nhật tiến lên, không lùi (tránh lùi tiến độ để lách luật hủy).
- Chỉnh ngưỡng và mức phạt ở đầu phần chuỗi cung ứng trong server.py: `CANCEL_FREE_DAYS`, `CANCEL_PENALTY`, `ALLOW_REQUEST_AT_HARVEST`.
- API mới: `POST /api/farm/commits/{id}/request-cancel`, `GET /api/commits/{id}/cancel-request`, `POST /api/commits/{id}/cancel-decision`. CSDL cũ tự nâng cấp khi chạy server (dựng lại bảng commits, thêm bảng cancel_requests và cột uy tín cho suppliers).


## Luồng Kế hoạch (KH) -> Giao hàng (GH) -> Nhập kho
1. Nông dân lập kế hoạch (Dự kiến: sửa, dời, hủy). Thủ kho/Quản lý kho Xác nhận. Đã xác nhận: ẩn Sửa/Hủy, còn Dời ngày (bắt buộc lý do), Đã thu hoạch, Tạo phiếu giao.
2. Nông dân Tạo phiếu giao từ KH (tab Giao hàng, nguồn `p:<id>`): GH ở trạng thái Chờ nhập kho, KH chuyển Đang giao hàng và bị khóa. QR dùng chung qr.py, tem, trang truy xuất.
3. Thủ kho (trang Thu mua QC) tiếp nhận: hệ thống kiểm tra (ngày thu hoạch - ngày phun cuối) >= thời gian cách ly. Vi phạm thì chỉ được Từ chối nhập (GH = Không nhập, KH về Đã thu hoạch). Đạt thì nhập cân thực tế và Tạo phiếu nhập (Chờ duyệt).
4. Quản lý kho duyệt phiếu nhập: tạo lô (tồn kho), GH = Đã nhập kho, KH = Đã nhập kho (thực nhận, chênh so với dự kiến), công nợ Chờ thanh toán = thực nhận x Giá thu mua mới nhất lúc duyệt. Thiếu Giá thu mua thì không duyệt được. Quản lý kho Xác nhận thanh toán.
Phiếu nhập không còn lập thẳng từ kế hoạch (POST /api/receipts với plan_id trả 409). API mới: POST /api/batches/{id}/receipt, POST /api/batches/{id}/refuse, POST /api/plans/{id}/update action=harvested.

## Thu mua trực tiếp của Kinh doanh (cross-docking): Nông dân → Kho QC → Cửa hàng
- Menu mới **Thu mua nông sản** (Kinh doanh, quyền `thumua`): chỉ liệt kê lô nông dân chào bán (đang chào bán + các lô mình đã mua), chọn cửa hàng nhận rồi bấm **Mua lô hàng**. Mua theo đúng giá chào bán, chốt ngay, không cần nông dân duyệt lại (hệ thống chưa có thương lượng giá; gửi giá khác bị chặn 409).
- Chống mua trùng: `POST /api/offers/{id}/buy` và duyệt của kho (`/decide`) cùng chạy trong giao dịch và chỉ thắng khi chào bán còn `CHO_DUYET`, bên thua nhận 409. Hai trang (Thu mua QC của kho, Thu mua nông sản của Kinh doanh) tự tải lại 5 giây/lần khi dữ liệu đổi (không vẽ lại lúc đang nhập liệu hay mở bảng chi tiết).
- Dữ liệu: `offers.buyer_type` (`warehouse` mặc định | `business`), `buyer_id`, `dest_customer_id`, `dest_store_info` (JSON chụp tên/địa chỉ cửa hàng), `bought_at`; `batches.xdock_order_id`, `batches.xdock_note`. CSDL cũ tự nâng cấp.
- Sau khi mua: chào bán sang Chờ giao hàng, nông dân lập phiếu giao + chặng 1 như thường lệ. Thủ kho QC: nếu **Đạt**, hệ thống trong cùng giao dịch tạo đơn `DH-xxxx` cho cửa hàng người mua, giữ thẳng các lô vừa nhập và xuất kho (sổ cái NHẬP rồi XUẤT, tồn lô về 0, không lúc nào là tồn kho tự do) rồi sinh chuyến chặng 2 (tài xế dùng tai-xe.html như cũ). Lô bị cách ly thì chờ QA, khi QA kết luận Đạt cho mọi lô của phiếu mới tự xuất thẳng; không đạt thì không giao và ghi thông báo `xdock_note` cho Nông dân, Kinh doanh, Kho (nông dân giao lại hoặc hủy theo quy trình cũ).
- Kinh doanh theo dõi ngay trên menu mới: 4 bước (Nông dân→Kho, QC, Kho→Cửa hàng, Đã nhận), tiến độ và ETA của 2 chặng; bấm **Đã nhận hàng** khi xe tới. Công nợ nông dân vẫn do Quản lý kho thanh toán như luồng chuẩn. Luồng Quản lý kho tự mua (nhập tồn kho) không đổi.
- Kiểm thử: `test_chain.py` thêm test_24 đến test_28 (vòng đời đầy đủ, tranh chấp mua đồng thời, lô cách ly, QC không đạt, luồng kho tự mua, nâng cấp CSDL cũ).
