"""Kiểm thử tự động cho API. Chạy:  python test_api.py"""
import time
import json
import os
import tempfile
import threading
import unittest
import urllib.request
import urllib.error

_tmp = tempfile.mkdtemp()
os.environ["KHO_DB"] = os.path.join(_tmp, "test.db")
os.environ["KHO_QUIET"] = "1"
import server  # noqa: E402

server.DB_PATH = os.environ["KHO_DB"]


def call(base, method, path, body=None, token=None):
    req = urllib.request.Request(base + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", **({"Authorization": "Bearer " + token} if token else {})})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


class T(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = server.make_server(0)
        cls.base = "http://127.0.0.1:%d" % cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.tok = {}
        for u in ("quanly", "thukho", "qa", "kinhdoanh", "nongdan", "nongdan2"):
            s, r = call(cls.base, "POST", "/api/login", {"username": u, "password": "123456"})
            assert s == 200, r
            cls.tok[u] = r["token"]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def c(self, who, method, path, body=None):
        return call(self.base, method, path, body, self.tok[who])

    def test_01_dang_nhap_sai_va_chua_dang_nhap(self):
        self.assertEqual(call(self.base, "POST", "/api/login", {"username": "quanly", "password": "sai"})[0], 401)
        self.assertEqual(call(self.base, "GET", "/api/lots")[0], 401)

    def test_02_phan_quyen(self):
        s, r = self.c("kinhdoanh", "POST", "/api/receipts", {})
        self.assertEqual(s, 403)
        s, r = self.c("thukho", "POST", "/api/receipts/1/approve")
        self.assertEqual(s, 403)

    def test_03_luong_nhap_kho(self):
        meta = self.c("thukho", "GET", "/api/meta")[1]
        today = meta["today"]
        body = {"sku": "KLT", "supplier_id": 3, "warehouse_id": 2, "qty_kg": 600, "harvest_date": today, "spray_date": None, "phi_days": 0}
        # kho lạnh không hợp với khoai lang tím
        s, r = self.c("thukho", "POST", "/api/receipts", {**body, "warehouse_id": 1})
        self.assertEqual(s, 409)
        s, r = self.c("thukho", "POST", "/api/receipts", {**body, "qty_kg": -5})
        self.assertEqual(s, 400)
        s, r = self.c("thukho", "POST", "/api/receipts", body)
        self.assertEqual(s, 200, r)
        rid = r["id"]
        s, r = self.c("quanly", "POST", "/api/receipts/%d/approve" % rid)
        self.assertEqual(s, 200, r)
        lot_code = r["lot_code"]
        # duyệt lần hai bị chặn
        self.assertEqual(self.c("quanly", "POST", "/api/receipts/%d/approve" % rid)[0], 409)
        lots = self.c("quanly", "GET", "/api/lots?q=" + lot_code)[1]
        self.assertEqual(lots[0]["qa_status"], "CHO_KIEM_DINH")
        self.assertEqual(lots[0]["qty_kg"], 600)
        # lô chưa kiểm định thì chưa khả dụng
        lid = lots[0]["id"]
        s, r = self.c("qa", "POST", "/api/lots/%d/inspect" % lid, {"result": "DAT", "grade": "Loại 1", "residue": 0.01})
        self.assertEqual(s, 200, r)
        hist = self.c("quanly", "GET", "/api/lots/%d" % lid)[1]["history"]
        self.assertEqual(hist[0]["type"], "NHAP")

    def test_04_kiem_dinh_chan_lo_vi_pham(self):
        lots = self.c("qa", "GET", "/api/lots?qa=CHO_KIEM_DINH")[1]
        lot = [l for l in lots if l["sku"] == "SLX"][0]
        s, r = self.c("qa", "POST", "/api/lots/%d/inspect" % lot["id"], {"result": "DAT", "grade": "Loại 1", "residue": 9})
        self.assertEqual(s, 409)
        self.assertIn("vượt giới hạn", r["error"])

    def test_05_dat_hang_fefo_va_xuat_kho(self):
        before = {l["code"]: l for l in self.c("quanly", "GET", "/api/lots?sku=XCL")[1]}
        s, r = self.c("kinhdoanh", "POST", "/api/orders", {"customer_id": 1, "items": [{"sku": "XCL", "qty_kg": 400}]})
        self.assertEqual(s, 200, r)
        oid = r["id"]
        self.assertEqual(self.c("kinhdoanh", "POST", "/api/orders/%d/reserve" % oid)[0], 403)  # Kinh doanh không được giữ hàng
        self.assertEqual(self.c("thukho", "POST", "/api/orders/%d/reserve" % oid)[0], 200)
        o = [x for x in self.c("quanly", "GET", "/api/orders")[1] if x["id"] == oid][0]
        # FEFO: lô hạn sớm hơn (thu hoạch cũ hơn) được lấy trước
        res = o["reservations"]
        self.assertEqual(sum(x["qty_kg"] for x in res), 400)
        self.assertEqual(res[0]["expiry_date"], min(x["expiry_date"] for x in res))
        # kinh doanh không xuất kho được
        self.assertEqual(self.c("kinhdoanh", "POST", "/api/orders/%d/ship" % oid)[0], 403)
        self.assertEqual(self.c("thukho", "POST", "/api/orders/%d/ship" % oid)[0], 200)
        rep = {x["sku"]: x for x in self.c("quanly", "GET", "/api/report")[1]}["XCL"]
        # cân đối: tồn cuối = nhập - xuất + điều chỉnh
        self.assertAlmostEqual(rep["ton_cuoi"], rep["nhap"] - rep["xuat"] + rep["dieu_chinh"])

    def test_06_khong_ban_qua_ton_kho(self):
        s, r = self.c("kinhdoanh", "POST", "/api/orders", {"customer_id": 1, "items": [{"sku": "NRT", "qty_kg": 99999}]})
        self.assertEqual(s, 409)

    def test_07_huy_don_nha_hang(self):
        s, r = self.c("kinhdoanh", "POST", "/api/orders", {"customer_id": 2, "items": [{"sku": "CRT", "qty_kg": 100}]})
        oid = r["id"]
        self.c("thukho", "POST", "/api/orders/%d/reserve" % oid)
        free1 = [x for x in self.c("quanly", "GET", "/api/dashboard")[1]["products"] if x["sku"] == "CRT"][0]["available"]
        self.assertEqual(self.c("kinhdoanh", "POST", "/api/orders/%d/cancel" % oid)[0], 200)
        free2 = [x for x in self.c("quanly", "GET", "/api/dashboard")[1]["products"] if x["sku"] == "CRT"][0]["available"]
        self.assertEqual(free2 - free1, 100)

    def test_08_tranh_chap_dong_thoi_khong_ban_trung(self):
        """Hai đơn cùng tranh nhau phần hàng còn lại: chỉ một đơn được giữ."""
        av = [x for x in self.c("quanly", "GET", "/api/dashboard")[1]["products"] if x["sku"] == "KLT"][0]["available"]
        ids = []
        for cust in (3, 4):
            s, r = self.c("kinhdoanh", "POST", "/api/orders", {"customer_id": cust, "items": [{"sku": "KLT", "qty_kg": av * 0.7}]})
            self.assertEqual(s, 200, r)
            ids.append(r["id"])
        out = []
        ts = [threading.Thread(target=lambda i=i: out.append(self.c("thukho", "POST", "/api/orders/%d/reserve" % i)[0])) for i in ids]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(sorted(out), [200, 409])
        lots = self.c("quanly", "GET", "/api/lots")[1]
        for l in lots:
            self.assertLessEqual(l["reserved_kg"], l["qty_kg"] + 1e-9)

    def test_09_kiem_ke_va_nhat_ky(self):
        lot = [l for l in self.c("quanly", "GET", "/api/lots?q=KLT")[1] if l["free_kg"] >= 10][0]
        s, r = self.c("thukho", "POST", "/api/lots/%d/stocktake" % lot["id"], {"qty_kg": lot["qty_kg"] * 0.5})
        self.assertEqual(s, 403)  # chênh lệch > 2% cần quản lý
        s, r = self.c("quanly", "POST", "/api/lots/%d/stocktake" % lot["id"], {"qty_kg": lot["qty_kg"] - 1})
        self.assertEqual(s, 200, r)
        self.assertTrue(len(self.c("quanly", "GET", "/api/audit")[1]) > 5)


    def test_10_van_chuyen_gps(self):
        os.environ["KHO_DEMO"] = "1"  # chế độ demo: cho phép nút mô phỏng
        trips = self.c("quanly", "GET", "/api/trips")[1]
        self.assertTrue(len(trips) >= 1)  # đơn đã xuất ở các test trước tự tạo chuyến
        cho = [x for x in trips if x["status"] == "CHO_LAY_HANG"]
        t = [x for x in cho if x["leg_type"] == "WAREHOUSE_TO_STORE"][0]
        sim = [x for x in cho if x["id"] != t["id"]][0]
        # khách không có token thì không đọc được; kinh doanh không thấy token
        self.assertNotIn("token", self.c("kinhdoanh", "GET", "/api/trips")[1][0])
        self.assertEqual(call(self.base, "POST", "/api/driver/location", {"t": "sai", "lat": 10, "lon": 106})[0], 404)
        self.assertEqual(call(self.base, "POST", "/api/driver/location", {"t": t["token"], "lat": 999, "lon": 106})[0], 400)
        s, r = call(self.base, "POST", "/api/driver/location", {"t": t["token"], "lat": t["o_lat"], "lon": t["o_lon"], "speed": 10})
        self.assertEqual(s, 200, r)
        info = call(self.base, "GET", "/api/driver/trip?t=" + t["token"])[1]
        self.assertEqual(info["status"], "DANG_VAN_CHUYEN")
        self.assertNotIn("token", info)
        # không trộn điểm GPS thật với điểm mô phỏng trên cùng một chuyến
        self.assertEqual(self.c("thukho", "POST", "/api/trips/%d/simulate" % t["id"])[0], 409)
        for _ in range(12):
            self.c("thukho", "POST", "/api/trips/%d/simulate" % sim["id"])
        done = [x for x in self.c("quanly", "GET", "/api/trips")[1] if x["id"] == sim["id"]][0]
        self.assertEqual(done["status"], "DA_GIAO")
        self.assertEqual(done["progress"], 100)
        self.assertEqual(self.c("kinhdoanh", "POST", "/api/trips/%d/simulate" % t["id"])[0], 403)

    def test_11_truy_xuat_nguon_goc(self):
        t = self.c("quanly", "GET", "/api/lots/3/trace")
        self.assertEqual(t[0], 200)
        t = t[1]
        self.assertEqual(t["lot"]["code"][:3], "XCL")
        self.assertTrue(t["supplier"]["name"])
        self.assertTrue(len(t["ledger"]) >= 1)
        # đơn DH-0001 đã xuất từ lô này và có chuyến xe đi kèm
        ship = [x for x in t["shipments"] if x["order_code"] == "DH-0001"]
        self.assertEqual(len(ship), 1)
        self.assertTrue(ship[0]["trip_code"])
        self.assertEqual(self.c("quanly", "GET", "/api/lots/99999/trace")[0], 404)
        self.assertEqual(call(self.base, "GET", "/api/lots/3/trace")[0], 401)
        # QR thật + trang truy xuất công khai (không cần đăng nhập, có mã xác thực chống dò mã lô)
        self.assertEqual(call(self.base, "GET", "/api/lots/3/qr")[0], 401)
        s, q = self.c("quanly", "GET", "/api/lots/3/qr?base=https://kho.example.vn")
        self.assertEqual(s, 200)
        self.assertEqual(q["url"], "https://kho.example.vn/t/%s?k=%s" % (q["code"], q["token"]))
        self.assertTrue(q["svg"].startswith("<svg") and "<path" in q["svg"])
        self.assertEqual(self.c("quanly", "GET", "/api/lots/3/qr?base=javascript:alert(1)")[0], 200)  # base xấu bị bỏ, dùng địa chỉ máy chủ
        self.assertNotIn("javascript", self.c("quanly", "GET", "/api/lots/3/qr?base=javascript:alert(1)")[1]["url"])
        s, pub = call(self.base, "GET", "/api/public/lot?c=%s&k=%s" % (q["code"], q["token"]))
        self.assertEqual(s, 200)
        self.assertEqual((pub["verdict"], pub["tone"]), ("DAT", "ok"))
        self.assertTrue(pub["farm"]["name"] and pub["qa"]["mrl"] > 0 and pub["spray"]["enough"])
        blob = json.dumps(pub, ensure_ascii=False)  # người mua không được thấy khách hàng, đơn, giá, tồn kho, nhân viên
        for secret in ("DH-", "Co.op", "customer", "qty", "reserved", "price", "Nguyễn Thị Lan", "Trần Văn Minh"):
            self.assertNotIn(secret, blob)
        for bad_k in ("", "deadbeef0000", q["token"][:-1] + ("0" if q["token"][-1] != "0" else "1")):
            self.assertEqual(call(self.base, "GET", "/api/public/lot?c=%s&k=%s" % (q["code"], bad_k))[0], 404)
        self.assertEqual(call(self.base, "GET", "/api/public/lot?c=KHONG-CO-01&k=%s" % q["token"])[0], 404)
        for path in ("/t", "/t/%s?k=%s" % (q["code"], q["token"]), "/tem"):  # trang công khai mở được, có chính sách bảo mật
            with urllib.request.urlopen(self.base + path) as r:
                self.assertEqual(r.status, 200)
                self.assertEqual(r.headers["Referrer-Policy"], "no-referrer")
                self.assertIn("default-src 'self'", r.headers["Content-Security-Policy"])
        with urllib.request.urlopen("%s/qr.svg?c=%s&k=%s&base=https://kho.example.vn" % (self.base, q["code"], q["token"])) as r:
            self.assertEqual(r.headers["Content-Type"].split(";")[0], "image/svg+xml")
            self.assertIn("<path", r.read().decode())
        self.assertEqual(call(self.base, "GET", "/qr.svg?c=%s&k=sai" % q["code"])[0], 404)  # không dùng được làm máy tạo QR tùy ý
        # lô bị thu hồi: trang công khai phải cảnh báo ngay
        s, rc = self.c("qa", "POST", "/api/lots/3/recall", {"reason": "Kiểm thử QR công khai"})
        self.assertEqual(s, 200, rc)
        pub2 = call(self.base, "GET", "/api/public/lot?c=%s&k=%s" % (q["code"], q["token"]))[1]
        self.assertEqual((pub2["verdict"], pub2["tone"]), ("THU_HOI", "bad"))
        self.assertTrue(pub2["recall"] and "reason" not in json.dumps(pub2))

    def test_12_thu_hoi_lo(self):
        def order(kg, cust):
            s, r = self.c("kinhdoanh", "POST", "/api/orders", {"customer_id": cust, "items": [{"sku": "CCB", "qty_kg": kg}]})
            self.assertEqual(s, 200, r)
            self.assertEqual(self.c("thukho", "POST", "/api/orders/%d/reserve" % r["id"])[0], 200)
            return r["id"]
        held_id, ship_id = order(30, 1), order(20, 2)
        self.assertEqual(self.c("thukho", "POST", "/api/orders/%d/ship" % ship_id)[0], 200)
        orders = {o["id"]: o for o in self.c("quanly", "GET", "/api/orders")[1]}
        lot_code = orders[held_id]["reservations"][0]["lot_code"]
        self.assertEqual(orders[ship_id]["reservations"][0]["lot_code"], lot_code)  # cùng một lô theo FEFO
        lot = self.c("quanly", "GET", "/api/lots?all=1&q=" + lot_code)[1][0]
        lid = lot["id"]
        imp = self.c("qa", "GET", "/api/lots/%d/recall-impact" % lid)[1]
        self.assertEqual(sorted(i["phase"] for i in imp["items"]), ["DA_NHA_GIU", "DA_XUAT"])
        # phân quyền và dữ liệu đầu vào
        self.assertEqual(self.c("kinhdoanh", "POST", "/api/lots/%d/recall" % lid, {"reason": "Phát hiện dư lượng"})[0], 403)
        self.assertEqual(self.c("qa", "POST", "/api/lots/%d/recall" % lid, {"reason": "x"})[0], 400)
        s, r = self.c("qa", "POST", "/api/lots/%d/recall" % lid, {"reason": "Phát hiện dư lượng vượt ngưỡng"})
        self.assertEqual(s, 200, r)
        self.assertEqual((r["held_orders"], r["shipped_orders"]), (1, 1))
        self.assertEqual(self.c("qa", "POST", "/api/lots/%d/recall" % lid, {"reason": "Thu hồi lần hai"})[0], 409)
        rid = r["id"]
        after = self.c("quanly", "GET", "/api/lots/%d" % lid)[1]
        self.assertEqual(after["qa_status"], "CACH_LY")
        self.assertEqual(after["reserved_kg"], 0)
        orders = {o["id"]: o for o in self.c("quanly", "GET", "/api/orders")[1]}
        self.assertEqual(orders[held_id]["status"], "MOI")
        self.assertEqual(orders[held_id]["reservations"], [])
        # lô bị thu hồi không còn bán được
        self.assertEqual(self.c("thukho", "POST", "/api/orders/%d/reserve" % held_id)[0], 409)
        # chưa thông báo khách thì chưa đóng được
        self.assertEqual(self.c("qa", "POST", "/api/recalls/%d/close" % rid, {"resolution": "HUY"})[0], 409)
        self.assertEqual(self.c("qa", "POST", "/api/recalls/%d/notify" % rid, {"all": True})[0], 200)
        self.assertEqual(self.c("qa", "POST", "/api/recalls/%d/close" % rid, {"resolution": "sai"})[0], 400)
        self.assertEqual(self.c("qa", "POST", "/api/recalls/%d/close" % rid, {"resolution": "HUY"})[0], 200)
        self.assertEqual(self.c("qa", "POST", "/api/recalls/%d/close" % rid, {"resolution": "HUY"})[0], 409)
        closed = self.c("quanly", "GET", "/api/lots/%d" % lid)[1]
        self.assertEqual(closed["qty_kg"], 0)
        self.assertEqual(closed["history"][0]["type"], "DIEU_CHINH")
        rep = {x["sku"]: x for x in self.c("quanly", "GET", "/api/report")[1]}["CCB"]
        self.assertAlmostEqual(rep["ton_cuoi"], rep["nhap"] - rep["xuat"] + rep["dieu_chinh"])
        qa = self.c("qa", "GET", "/api/qa")[1]
        self.assertTrue(any(x["code"] and x["status"] == "DA_DONG" for x in qa["recalls"]))
        self.assertEqual(self.c("quanly", "GET", "/api/lots/%d/trace" % lid)[1]["recalls"][0]["status"], "DA_DONG")

    def test_13_ke_hoach_va_gia(self):
        import datetime
        today = self.c("quanly", "GET", "/api/meta")[1]["today"]
        day = lambda n: (datetime.date.fromisoformat(today) + datetime.timedelta(days=n)).isoformat()
        body = {"sku": "SLX", "area_ha": 1, "expected_kg": 500, "expected_date": day(4)}
        # chỉ Nông dân được lập kế hoạch; mọi vai trò khác chỉ xem
        for who in ("kinhdoanh", "thukho", "quanly", "qa"):
            self.assertEqual(self.c(who, "POST", "/api/plans", body)[0], 403, who)
        self.assertEqual(self.c("nongdan", "POST", "/api/plans", {**body, "expected_date": day(-1)})[0], 400)
        self.assertEqual(self.c("nongdan", "POST", "/api/plans", {**body, "expected_kg": 0})[0], 400)
        s, r = self.c("nongdan", "POST", "/api/plans", {**body, "supplier_id": 3})  # cố gán sang nông trại khác vẫn bị ép về nông trại của mình
        self.assertEqual(s, 200, r)
        pid = r["id"]
        mine = [x for x in self.c("nongdan", "GET", "/api/plans")[1]["plans"] if x["id"] == pid][0]
        self.assertEqual(mine["supplier_id"], 1)
        up = lambda who, **k: self.c(who, "POST", "/api/plans/%d/update" % pid, k)
        # kinh doanh, thủ kho, QA, quản lý không sửa/dời/hủy/xóa được
        for who in ("kinhdoanh", "thukho", "quanly", "qa"):
            for act in ("edit", "postpone", "cancel", "delete"):
                self.assertEqual(up(who, action=act, expected_date=day(6))[0], 403, (who, act))
        # nông dân khác không đụng được kế hoạch của nông dân này
        self.assertEqual(up("nongdan2", action="cancel")[0], 403)
        # xác nhận chỉ dành cho Quản lý kho / Thủ kho
        self.assertEqual(up("nongdan", action="confirm")[0], 403)
        self.assertEqual(up("kinhdoanh", action="confirm")[0], 403)
        self.assertEqual(up("thukho", action="confirm")[0], 200)
        self.assertEqual(up("quanly", action="confirm")[0], 409)
        # kế hoạch đã xác nhận: không sửa được, chỉ dời ngày (bắt buộc lý do) và phải được xác nhận lại
        self.assertEqual(up("nongdan", action="edit", expected_kg=650)[0], 409)
        self.assertEqual(up("nongdan", action="postpone", expected_date=day(-2), reason="Mưa")[0], 400)
        self.assertEqual(up("nongdan", action="postpone", expected_date=day(7))[0], 400)  # thiếu lý do
        self.assertEqual(up("nongdan", action="postpone", expected_date=day(7), reason="Mưa kéo dài")[0], 200)
        mine = [x for x in self.c("nongdan", "GET", "/api/plans")[1]["plans"] if x["id"] == pid][0]
        self.assertEqual((mine["status"], mine["postponed"], mine["expected_date"]), ("DU_KIEN", 1, day(7)))
        self.assertEqual(up("nongdan", action="edit", expected_kg=650, note="Đã chốt")[0], 200)
        self.assertEqual(up("nongdan", action="delete")[0], 403)  # nông dân không được xóa kế hoạch
        self.assertEqual(up("thukho", action="delete")[0], 403)
        self.assertEqual(up("nongdan", action="confirm")[0], 403)
        self.assertEqual(up("thukho", action="confirm")[0], 200)
        self.assertEqual(up("nongdan", action="cancel")[0], 409)  # đã xác nhận thì không hủy được
        self.assertEqual(up("nongdan", action="edit", expected_kg=640)[0], 409)  # đã xác nhận thì khóa sửa
        self.assertEqual(up("nongdan", action="postpone", expected_date=day(8), reason="Chờ đo độ chín")[0], 200)  # dời ngày: về Dự kiến, chờ xác nhận lại
        self.assertEqual(up("nongdan", action="edit", expected_kg=640)[0], 200)
        self.assertFalse([x for x in self.c("nongdan", "GET", "/api/plans")[1]["plans"] if x["id"] == pid][0]["deletable"])
        self.assertEqual(up("nongdan", action="cancel")[0], 200)  # Dự kiến thì hủy được
        self.assertEqual(up("nongdan", action="cancel")[0], 409)
        # kinh doanh thấy tổng hợp toàn bộ nông dân, có bộ lọc
        allp = self.c("kinhdoanh", "GET", "/api/plans")[1]
        self.assertTrue(len({x["supplier_id"] for x in allp["plans"]}) > 1)
        self.assertTrue(all("farms" in s_ and "total_kg" in s_ for s_ in allp["summary"]))
        one = self.c("kinhdoanh", "GET", "/api/plans?sku=SLX")[1]
        self.assertTrue(all(x["sku"] == "SLX" for x in one["plans"]))
        late = self.c("kinhdoanh", "GET", "/api/plans?from=%s" % day(400))[1]
        self.assertEqual(late["plans"], [])
        self.assertEqual(len(self.c("quanly", "GET", "/api/plans")[1]["balance"]), 6)
        # giá thị trường: chỉ Kinh doanh thêm/sửa/xóa, nông dân và các vai trò khác chỉ xem
        pr = self.c("nongdan", "GET", "/api/prices")[1]
        self.assertEqual(len(pr["summary"]), 6)
        self.assertTrue(all(x["sell_price"] is None for x in pr["summary"]))  # nông dân không thấy giá bán nội bộ
        price = {"sku": "SLX", "kind": "THI_TRUONG", "market": "Chợ A", "price": 40000}
        for who in ("nongdan", "thukho", "quanly", "qa"):
            self.assertEqual(self.c(who, "POST", "/api/prices", price)[0], 403, who)
        self.assertEqual(self.c("kinhdoanh", "POST", "/api/prices", {**price, "price": -5})[0], 400)
        self.assertEqual(self.c("kinhdoanh", "POST", "/api/prices", {**price, "price_date": day(3)})[0], 400)
        self.assertEqual(self.c("kinhdoanh", "POST", "/api/prices", price)[0], 200)
        rid = [x for x in self.c("kinhdoanh", "GET", "/api/prices")[1]["recent"] if x["market"] == "Chợ A"][0]["id"]
        for who in ("nongdan", "thukho", "quanly", "qa"):
            self.assertEqual(self.c(who, "POST", "/api/prices/%d/update" % rid, {"price": 1})[0], 403, who)
            self.assertEqual(self.c(who, "POST", "/api/prices/%d/delete" % rid)[0], 403, who)
        self.assertEqual(self.c("kinhdoanh", "POST", "/api/prices/%d/update" % rid, {"price": 0})[0], 400)
        self.assertEqual(self.c("kinhdoanh", "POST", "/api/prices/%d/update" % rid, {"price": 41500})[0], 200)
        slx = [x for x in self.c("quanly", "GET", "/api/prices")[1]["summary"] if x["sku"] == "SLX"][0]
        self.assertTrue(any(m["market"] == "Chợ A" and m["price"] == 41500 for m in slx["markets"]))
        self.assertEqual(self.c("kinhdoanh", "POST", "/api/prices/%d/delete" % rid)[0], 200)
        self.assertEqual(self.c("kinhdoanh", "POST", "/api/prices/%d/delete" % rid)[0], 404)

    def test_13b_khong_lap_phieu_nhap_thang_tu_ke_hoach(self):
        """Cơ chế cũ (Quản lý kho/Thủ kho lập thẳng phiếu nhập từ kế hoạch) đã bị loại bỏ: luồng mới đi qua phiếu giao hàng (xem test_chain)."""
        import datetime
        today = self.c("quanly", "GET", "/api/meta")[1]["today"]
        day = lambda n: (datetime.date.fromisoformat(today) + datetime.timedelta(days=n)).isoformat()
        pid = self.c("nongdan", "POST", "/api/plans", {"sku": "KLT", "area_ha": 2, "expected_kg": 800, "expected_date": day(1)})[1]["id"]
        rc = {"sku": "KLT", "supplier_id": 1, "warehouse_id": 2, "qty_kg": 760, "harvest_date": today, "phi_days": 0, "plan_id": pid}
        for who in ("thukho", "quanly"):
            self.assertEqual(self.c(who, "POST", "/api/receipts", rc)[0], 409, who)  # Dự kiến
        self.assertEqual(self.c("thukho", "POST", "/api/plans/%d/update" % pid, {"action": "confirm"})[0], 200)
        for who in ("thukho", "quanly"):
            s, r = self.c(who, "POST", "/api/receipts", rc)  # đã xác nhận vẫn không được
            self.assertEqual(s, 409, who)
            self.assertIn("phiếu giao", r["error"])
        plan = [x for x in self.c("thukho", "GET", "/api/plans")[1]["plans"] if x["id"] == pid][0]
        self.assertNotIn("can_receipt", plan)
        # phiếu nhập thủ công (không gắn kế hoạch) vẫn dùng được
        manual = {k: v for k, v in rc.items() if k != "plan_id"}
        self.assertEqual(self.c("thukho", "POST", "/api/receipts", manual)[0], 200)

    def test_14_doi_chieu_yeu_cau(self):
        r = self.c("quanly", "GET", "/api/requirements")[1]
        self.assertEqual(r["summary"]["total"], 289)
        self.assertEqual(len(r["items"]), 289)
        self.assertEqual(sum(r["summary"]["by_status"].values()), 289)
        self.assertEqual(sum(w["total"] for w in r["summary"]["by_who"]), 289)
        self.assertEqual(len(r["summary"]["by_who"]), 26)
        # yêu cầu đã làm thật phải trỏ tới một trang có thật
        pages = {"tong-quan", "ton-kho", "nhap-kho", "kiem-dinh", "don-hang", "van-chuyen", "truy-xuat", "ke-hoach", "bao-cao", "nhat-ky", "nd-chao-ban", "nd-keo", "nd-giao-hang", "nd-cong-no", "rfq", "thu-mua"}
        for i in r["items"]:
            if i["status"] != "HUONG":
                self.assertIn(i["module"], pages, i["id"])
        self.assertEqual(call(self.base, "GET", "/api/requirements")[0], 401)

    def test_15_suc_chua_kho(self):
        caps = {w["id"]: w for w in self.c("thukho", "GET", "/api/capacity")[1]}
        self.assertEqual([caps[i]["capacity_kg"] for i in (1, 2, 3)], [5000000, 8000000, 3500000])
        base = caps[3]["free_kg"]
        body = {"sku": "SLX", "supplier_id": 1, "warehouse_id": 3, "qty_kg": 1000, "harvest_date": time.strftime("%Y-%m-%d"), "phi_days": 0}
        s, r = self.c("thukho", "POST", "/api/receipts", body)
        self.assertEqual(s, 200, r)
        after = {w["id"]: w for w in self.c("thukho", "GET", "/api/capacity")[1]}[3]
        self.assertEqual(after["free_kg"], base - 1000)  # khóa chỗ tạm
        self.assertEqual(after["pending_kg"], caps[3]["pending_kg"] + 1000)
        row = [x for x in self.c("quanly", "GET", "/api/receipts")[1] if x["id"] == r["id"]][0]
        self.assertIn(row["cap"]["level"], ("ok", "warn", "bad"))
        self.assertEqual(self.c("quanly", "POST", "/api/receipts/%d/reject" % r["id"])[0], 200)
        back = {w["id"]: w for w in self.c("thukho", "GET", "/api/capacity")[1]}[3]
        self.assertEqual(back["free_kg"], base)  # từ chối thì hoàn trả
        body["qty_kg"] = base + 1
        s, r = self.c("thukho", "POST", "/api/receipts", body)
        self.assertEqual(s, 409)
        self.assertIn("vượt", r["error"])


    def test_16_phan_quyen_api_nhay_cam(self):
        """Nhật ký, báo cáo tổng hợp, đối chiếu yêu cầu: chỉ Quản lý kho. Duyệt phiếu nhập: chỉ Quản lý kho."""
        for path in ("/api/audit", "/api/report", "/api/requirements"):
            self.assertEqual(self.c("quanly", "GET", path)[0], 200, path)
            for who in ("thukho", "qa", "kinhdoanh", "nongdan", "nongdan2"):
                self.assertEqual(self.c(who, "GET", path)[0], 403, (who, path))
            self.assertEqual(call(self.base, "GET", path)[0], 401, path)
        for who in ("thukho", "qa", "kinhdoanh"):
            self.assertEqual(self.c(who, "POST", "/api/receipts/1/approve")[0], 403, who)
            self.assertEqual(self.c(who, "POST", "/api/receipts/1/reject")[0], 403, who)
            self.assertEqual(self.c(who, "POST", "/api/offers/1/decide", {})[0], 403, who)
        # quyền gửi cho giao diện khớp với API
        perms = {u: set(self.c(u, "GET", "/api/me")[1]["perms"]) for u in ("quanly", "thukho", "qa", "kinhdoanh")}
        self.assertTrue({"baocao", "nhatky", "doichieu"} <= perms["quanly"])
        for who in ("thukho", "qa", "kinhdoanh"):
            self.assertFalse({"baocao", "nhatky", "doichieu"} & perms[who], who)

    def test_17_tong_quan_theo_vai_tro(self):
        full = self.c("quanly", "GET", "/api/dashboard")[1]
        self.assertIn("revenue", full)
        self.assertIn("pending_inspection", full)
        self.assertIn("revenue", self.c("kinhdoanh", "GET", "/api/dashboard")[1])
        for who in ("thukho", "qa"):  # không lộ doanh thu cho vai trò không làm việc với đơn hàng
            d = self.c(who, "GET", "/api/dashboard")[1]
            self.assertNotIn("revenue", d, who)
            self.assertIn("total_kg", d)
        lots = self.c("quanly", "GET", "/api/lots?qa=CHO_KIEM_DINH")[1]
        self.assertEqual(full["pending_inspection"], len([l for l in lots if l["qty_kg"] > 0]))


    def test_18_phan_quyen_van_chuyen(self):
        """Mọi tài khoản thấy Vận chuyển trừ QA/QC; dữ liệu lọc ở backend, không chỉ ẩn ở giao diện."""
        allt = self.c("quanly", "GET", "/api/trips")[1]
        legs = {x["leg_type"] for x in allt}
        self.assertEqual(legs, {"FARM_TO_WAREHOUSE", "WAREHOUSE_TO_STORE"})
        self.assertEqual({x["leg_type"] for x in self.c("thukho", "GET", "/api/trips")[1]}, legs)  # Thủ kho thấy cả 2 chặng
        self.assertTrue(all(x["leg_type"] == "FARM_TO_WAREHOUSE" for x in self.c("quanly", "GET", "/api/trips?leg=in")[1]))
        self.assertTrue(all(x["leg_type"] == "WAREHOUSE_TO_STORE" for x in self.c("quanly", "GET", "/api/trips?leg=out")[1]))
        self.assertEqual(self.c("quanly", "GET", "/api/trips?leg=xyz")[0], 400)
        # QA/QC: không có tab Vận chuyển, API trả 403
        self.assertEqual(self.c("qa", "GET", "/api/trips")[0], 403)
        self.assertEqual(self.c("qa", "GET", "/api/trips/%d" % allt[0]["id"])[0], 403)
        self.assertEqual(self.c("qa", "GET", "/api/trips/%d/qr" % allt[0]["id"])[0], 403)
        # Kinh doanh: xem được cả 2 chặng nhưng không có token, PIN, SĐT tài xế, và không điều phối
        kd = self.c("kinhdoanh", "GET", "/api/trips")[1]
        self.assertEqual({x["leg_type"] for x in kd}, legs)
        for x in kd:
            for k in ("token", "pin", "pin_hash", "driver_phone"):
                self.assertNotIn(k, x)
        one = self.c("kinhdoanh", "GET", "/api/trips/%d" % kd[0]["id"])[1]
        self.assertNotIn("token", one)
        self.assertEqual(self.c("kinhdoanh", "POST", "/api/trips/%d/status" % kd[0]["id"], {"status": "SU_CO"})[0], 403)
        self.assertEqual(self.c("kinhdoanh", "GET", "/api/trips/%d/qr" % kd[0]["id"])[0], 403)
        # Nông dân: chỉ chặng 1 của chính mình; chuyến của người khác trả 404, không thấy chặng 2
        a, b = self.c("nongdan", "GET", "/api/trips")[1], self.c("nongdan2", "GET", "/api/trips")[1]
        self.assertTrue(all(x["leg_type"] == "FARM_TO_WAREHOUSE" for x in a + b))
        self.assertEqual({x["supplier_id"] for x in a} - {1}, set())
        self.assertEqual({x["supplier_id"] for x in b} - {2}, set())
        self.assertTrue(b, "nông dân 2 có chuyến chặng 1 từ phiếu giao mẫu")
        self.assertEqual(self.c("nongdan2", "GET", "/api/trips?leg=out")[1], [])
        mine = b[0]
        self.assertTrue(mine["token"] and mine["pin"])
        for k in ("order_code", "order_id", "dest_address"):
            self.assertNotIn(k, mine)
        self.assertEqual(self.c("nongdan", "GET", "/api/trips/%d" % mine["id"])[0], 404)  # không để lộ chuyến của người khác có tồn tại
        self.assertEqual(self.c("nongdan", "GET", "/api/trips/%d/qr" % mine["id"])[0], 404)
        out = [x for x in allt if x["leg_type"] == "WAREHOUSE_TO_STORE"][0]
        self.assertEqual(self.c("nongdan2", "GET", "/api/trips/%d" % out["id"])[0], 404)
        self.assertEqual(self.c("nongdan2", "POST", "/api/trips/%d/status" % mine["id"], {"status": "DA_GIAO"})[0], 403)
        # QR tài xế: chỉ người điều phối và nông dân của chính chuyến; khác hẳn QR truy xuất công khai
        s, q = self.c("nongdan2", "GET", "/api/trips/%d/qr?base=http://127.0.0.1:8000" % mine["id"])
        self.assertEqual(s, 200, q)
        self.assertIn("<svg", q["svg"])
        self.assertTrue(q["url"].endswith("/tai-xe?t=" + mine["token"]) and "/t/" not in q["url"])
        self.assertEqual(self.c("thukho", "GET", "/api/trips/%d/qr?base=http://127.0.0.1:8000" % mine["id"])[0], 200)
        # tổng quan: QA không có số liệu vận chuyển, Kinh doanh chỉ có xe đang giao cửa hàng
        dq = self.c("qa", "GET", "/api/dashboard")[1]
        self.assertFalse({"active_trips", "active_in", "active_out", "near_arrival"} & set(dq))
        dk = self.c("kinhdoanh", "GET", "/api/dashboard")[1]
        self.assertIn("active_out", dk)
        self.assertFalse({"active_in", "near_arrival", "temp_alerts"} & set(dk))
        dm = self.c("quanly", "GET", "/api/dashboard")[1]
        self.assertEqual(dm["active_trips"], dm["active_in"] + dm["active_out"])

    def test_19_driver_api_khong_dang_nhap(self):
        os.environ["KHO_GPS_MIN_INTERVAL"] = "0"
        cho = [x for x in self.c("quanly", "GET", "/api/trips")[1] if x["status"] in ("CHO_LAY_HANG", "DANG_VAN_CHUYEN")][0]
        # không token, không PIN: không đọc được; sai token 404; PIN sai 401
        self.assertEqual(call(self.base, "GET", "/api/driver/trip")[0], 404)
        self.assertEqual(call(self.base, "POST", "/api/driver/location", {"lat": 10, "lon": 106})[0], 404)
        self.assertEqual(call(self.base, "POST", "/api/driver/status", {"plate": cho["plate"], "pin": "abc", "status": "DA_GIAO"})[0], 401)
        v = call(self.base, "GET", "/api/driver/trip?t=" + cho["token"])[1]
        for k in ("supplier_name", "farm_name", "pin", "driver_phone", "order_code", "token"):  # trang tài xế không lộ nông dân, giá, SĐT
            self.assertNotIn(k, v)
        for x in v["cargo"]:
            self.assertEqual(set(x), {"product", "qty_kg", "code"})
        self.assertEqual(call(self.base, "POST", "/api/driver/status", {"t": cho["token"], "status": "KHONG_CO"})[0], 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
