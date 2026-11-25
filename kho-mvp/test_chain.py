"""Kiểm thử chuỗi cung ứng 3 bên (Nông dân - Kho - Kinh doanh). Chạy:  python test_chain.py"""
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

_tmp = tempfile.mkdtemp()
os.environ["KHO_DB"] = os.path.join(_tmp, "chain.db")
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


class Chain(unittest.TestCase):
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
        cls.today = server.today()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def c(self, who, method, path, body=None):
        return call(self.base, method, path, body, self.tok[who])

    def ok(self, who, method, path, body=None):
        s, r = self.c(who, method, path, body)
        self.assertEqual(s, 200, r)
        return r

    def test_01_nong_dan_bi_gioi_han_quyen(self):
        for path in ("/api/orders", "/api/lots", "/api/dashboard", "/api/audit", "/api/report"):
            self.assertEqual(self.c("nongdan", "GET", path)[0], 403, path)
        self.assertEqual(self.c("nongdan", "POST", "/api/orders", {})[0], 403)
        self.assertEqual(self.c("kinhdoanh", "POST", "/api/farm/offers", {})[0], 403)  # vai trò khác không dùng được cổng nông dân
        plans = self.ok("nongdan", "GET", "/api/plans")  # nông dân chỉ thấy kế hoạch của nông trại mình và không thấy tồn kho
        self.assertTrue(plans["plans"] and all(x["supplier_id"] == 1 for x in plans["plans"]))
        self.assertEqual(plans["balance"], [])
        meta = self.ok("nongdan", "GET", "/api/meta")
        self.assertEqual(meta["customers"], [])
        self.assertEqual([s["id"] for s in meta["suppliers"]], [1])

    def test_02_chao_ban_qc_nhap_kho_cong_no(self):
        o = self.ok("nongdan", "POST", "/api/farm/offers", {"sku": "SLX", "qty_kg": 1000, "price": 35000, "harvest_date": self.today, "standard": "VietGAP"})
        self.assertEqual(self.c("nongdan", "POST", "/api/offers/%d/decide" % o["id"], {"action": "approve"})[0], 403)  # nông dân không tự duyệt
        self.assertEqual(self.c("thukho", "POST", "/api/offers/%d/decide" % o["id"], {"action": "approve"})[0], 403)
        self.ok("quanly", "POST", "/api/offers/%d/decide" % o["id"], {"action": "approve"})
        self.assertEqual(self.c("quanly", "POST", "/api/offers/%d/decide" % o["id"], {"action": "approve"})[0], 409)
        body = {"source": "o:%d" % o["id"], "qty_kg": 1000, "harvest_date": self.today, "warehouse_id": 3, "phi_days": 7, "vehicle": "49C-123.45"}
        self.assertEqual(self.c("nongdan", "POST", "/api/farm/batches", {**body, "qty_kg": 1500})[0], 409)  # vượt phần còn phải giao
        self.assertEqual(self.c("nongdan", "POST", "/api/farm/batches", {**body, "warehouse_id": 1, "sku": "x"})[0], 200)
        b = [x for x in self.ok("nongdan", "GET", "/api/batches") if x["offer_code"] == o["code"]][0]
        # QR phiếu giao hàng: trước QC hiện trạng thái chờ, mã xác thực sai thì bị từ chối
        qr = self.ok("nongdan", "GET", "/api/batches/%d/qr" % b["id"])
        self.assertIn("<svg", qr["svg"])
        k = server.qr_token(b["code"])
        pub = self.ok("nongdan", "GET", "/api/public/lot?c=%s&k=%s" % (b["code"], k))
        self.assertEqual(pub["verdict"], "CHO_KIEM_DINH")
        self.assertEqual(call(self.base, "GET", "/api/public/lot?c=%s&k=sai" % b["code"])[0], 404)
        before = self.ok("thukho", "GET", "/api/dashboard")["total_kg"]
        # QC: 600 kg Loại 1, 300 kg Loại 2, 80 kg hỏng (tổng 980 kg)
        r = self.ok("thukho", "POST", "/api/batches/%d/receive" % b["id"], {"qty_l1": 600, "qty_l2": 300, "qty_bad": 80, "residue": 0.1, "meets_standard": True})
        self.assertEqual(len(r["lots"]), 2)
        self.assertFalse(r["quarantined"])
        self.assertEqual(r["amount"], round(600 * 35000 + 300 * 35000 * server.L2_FACTOR))
        self.assertEqual(self.c("thukho", "POST", "/api/batches/%d/receive" % b["id"], {"qty_l1": 1})[0], 409)  # không QC hai lần
        self.assertEqual(self.ok("thukho", "GET", "/api/dashboard")["total_kg"] - before, 900)  # chỉ cộng phần đạt, không cộng 80 kg hỏng
        pub = self.ok("nongdan", "GET", "/api/public/lot?c=%s&k=%s" % (b["code"], k))
        self.assertEqual(pub["verdict"], "DAT")  # sau QC, QR phiếu dẫn tới hồ sơ lô thật
        mine = [x for x in self.ok("nongdan", "GET", "/api/batches") if x["id"] == b["id"]][0]
        self.assertEqual(mine["loss_pct"], 10.0)
        self.assertEqual(mine["due"], r["amount"])
        self.assertEqual(self.c("thukho", "POST", "/api/batches/%d/pay" % b["id"])[0], 403)  # chỉ Quản lý kho chốt thanh toán
        self.ok("quanly", "POST", "/api/batches/%d/pay" % b["id"])
        self.assertEqual(self.c("quanly", "POST", "/api/batches/%d/pay" % b["id"])[0], 409)
        mine = [x for x in self.ok("nongdan", "GET", "/api/batches") if x["id"] == b["id"]][0]
        self.assertEqual(mine["due"], 0)
        self.assertEqual(self.ok("nongdan", "GET", "/api/offers")[0]["status"], "DA_GIAO")

    def test_03_qc_vi_pham_bi_cach_ly(self):
        b = [x for x in self.ok("thukho", "GET", "/api/batches") if x["status"] == "CHO_GIAO"][0]
        r = self.ok("thukho", "POST", "/api/batches/%d/receive" % b["id"], {"qty_l1": 100, "residue": 5, "meets_standard": True})
        self.assertTrue(r["quarantined"])  # dư lượng vượt giới hạn: lô tạo ra nhưng bị cách ly, không bán được

    def test_04_rfq_rang_buoc_va_gom_don(self):
        base = {"customer_id": 1, "sku": "XCL", "qty_kg": 1000, "standard": "Bất kỳ", "price_min": 60000, "price_max": 70000, "deposit_pct": 10}
        self.assertEqual(self.c("nongdan", "POST", "/api/rfqs", {**base, "deliver_by": self.today})[0], 403)
        s, r = self.c("kinhdoanh", "POST", "/api/rfqs", {**base, "deliver_by": server.add_days(self.today, 5)})
        self.assertEqual(s, 409)  # xoài cần 45 ngày, giao sau 5 ngày là không thể
        self.assertIn("45", r["error"])
        rf = self.ok("kinhdoanh", "POST", "/api/rfqs", {**base, "deliver_by": server.add_days(self.today, 60)})
        farm = self.ok("nongdan", "GET", "/api/rfqs")
        mine = [x for x in farm if x["id"] == rf["id"]][0]
        self.assertNotIn("customer_id", mine)  # nông dân không thấy danh tính doanh nghiệp
        self.assertEqual(self.c("nongdan", "POST", "/api/farm/rfqs/%d/commit" % rf["id"], {"qty_kg": 400, "price": 90000})[0], 409)  # ngoài khoảng giá
        c1 = self.ok("nongdan", "POST", "/api/farm/rfqs/%d/commit" % rf["id"], {"qty_kg": 600, "price": 65000})
        c2 = self.ok("nongdan2", "POST", "/api/farm/rfqs/%d/commit" % rf["id"], {"qty_kg": 600, "price": 64000})
        self.assertEqual(self.c("nongdan", "POST", "/api/commits/%d/decide" % c1["id"], {"action": "approve"})[0], 403)
        self.ok("kinhdoanh", "POST", "/api/commits/%d/decide" % c1["id"], {"action": "approve"})
        self.assertEqual(self.c("kinhdoanh", "POST", "/api/commits/%d/decide" % c2["id"], {"action": "approve"})[0], 409)  # 600 + 600 > 1000
        self.assertEqual(self.c("nongdan", "POST", "/api/farm/commits/%d/progress" % c2["id"], {"stage": "CHAM_SOC"})[0], 409)  # kèo chưa duyệt
        self.ok("nongdan", "POST", "/api/farm/commits/%d/progress" % c1["id"], {"stage": "CHAM_SOC", "note": "Cây ra hoa đồng loạt"})
        # nông dân hủy kèo đã duyệt thì mất cọc
        r = self.ok("nongdan", "POST", "/api/farm/commits/%d/cancel" % c1["id"])
        self.assertTrue(r["lost_deposit"])
        # kèo chuẩn VietGAP: HTX GlobalGAP không đủ điều kiện, nhưng nông trại VietGAP thì được
        rv = self.ok("kinhdoanh", "POST", "/api/rfqs", {**base, "sku": "CRT", "standard": "GlobalGAP", "deliver_by": server.add_days(self.today, 100)})
        self.assertEqual(self.c("nongdan", "POST", "/api/farm/rfqs/%d/commit" % rv["id"], {"qty_kg": 100, "price": 65000})[0], 409)
        self.assertEqual(self.c("nongdan2", "POST", "/api/farm/rfqs/%d/commit" % rv["id"], {"qty_kg": 100, "price": 65000})[0], 200)

    def test_05_luong_khep_kin_den_tat_toan(self):
        rf = self.ok("kinhdoanh", "POST", "/api/rfqs", {"customer_id": 2, "sku": "XCL", "qty_kg": 500, "standard": "GlobalGAP", "price_min": 60000, "price_max": 70000,
                                                       "deliver_by": server.add_days(self.today, 50), "deposit_pct": 10})
        cm = self.ok("nongdan2", "POST", "/api/farm/rfqs/%d/commit" % rf["id"], {"qty_kg": 500, "price": 66000})
        self.ok("kinhdoanh", "POST", "/api/commits/%d/decide" % cm["id"], {"action": "approve"})
        r = [x for x in self.ok("kinhdoanh", "GET", "/api/rfqs") if x["id"] == rf["id"]][0]
        self.assertEqual(r["status"], "DU_SL")
        self.assertEqual(self.c("kinhdoanh", "POST", "/api/rfqs/%d/receive" % rf["id"])[0], 409)  # chưa xuất kho
        self.ok("nongdan2", "POST", "/api/farm/batches", {"source": "c:%d" % cm["id"], "qty_kg": 500, "harvest_date": self.today, "warehouse_id": 2, "phi_days": 0})
        b = [x for x in self.ok("nongdan2", "GET", "/api/batches") if x["commit_code"] == cm["code"]][0]
        self.ok("thukho", "POST", "/api/batches/%d/receive" % b["id"], {"qty_l1": 480, "qty_l2": 0, "qty_bad": 20, "residue": 0.02, "meets_standard": True})
        pay = self.ok("quanly", "POST", "/api/batches/%d/pay" % b["id"])
        self.assertEqual(pay["refund"], round(500 * 66000 * 0.1))  # hoàn cọc khi giao hàng
        o = self.ok("kinhdoanh", "POST", "/api/rfqs/%d/order" % rf["id"])
        self.ok("thukho", "POST", "/api/orders/%d/reserve" % o["id"])
        self.ok("thukho", "POST", "/api/orders/%d/ship" % o["id"])
        tr = self.ok("kinhdoanh", "GET", "/api/rfqs/%d/trace" % rf["id"])
        self.assertTrue(tr["lots"] and tr["lots"][0]["path"].startswith("/t/"))
        self.ok("kinhdoanh", "POST", "/api/rfqs/%d/receive" % rf["id"])
        r = [x for x in self.ok("kinhdoanh", "GET", "/api/rfqs") if x["id"] == rf["id"]][0]
        self.assertEqual(r["status"], "HOAN_TAT")
        self.assertEqual([x for x in self.ok("kinhdoanh", "GET", "/api/orders") if x["id"] == o["id"]][0]["status"], "DA_NHAN_HANG")  # đồng bộ RFQ -> đơn

    # ---- Hủy kèo theo tiến độ canh tác
    def _kèo(self, farmer, qty=100, days=60, stage=None, cust=3):
        rf = self.ok("kinhdoanh", "POST", "/api/rfqs", {"customer_id": cust, "sku": "XCL", "qty_kg": qty, "standard": "Bất kỳ", "price_min": 60000, "price_max": 70000,
                                                       "deliver_by": server.add_days(self.today, 60), "deposit_pct": 10})
        cm = self.ok(farmer, "POST", "/api/farm/rfqs/%d/commit" % rf["id"], {"qty_kg": qty, "price": 65000})
        self.ok("kinhdoanh", "POST", "/api/commits/%d/decide" % cm["id"], {"action": "approve"})
        c = server.connect()
        c.execute("UPDATE rfqs SET deliver_by=? WHERE id=?", (server.add_days(self.today, days), rf["id"]))
        c.close()
        if stage:
            self.ok(farmer, "POST", "/api/farm/commits/%d/progress" % cm["id"], {"stage": stage})
        return rf, cm

    def _sup(self, sid):
        c = server.connect()
        r = dict(c.execute("SELECT * FROM suppliers WHERE id=?", (sid,)).fetchone())
        c.close()
        return r

    def _reset(self, sid):
        c = server.connect()
        c.execute("UPDATE suppliers SET reputation=100,blocked_until=NULL,cap_until=NULL,cap_pct=NULL WHERE id=?", (sid,))
        c.close()

    def _commit(self, who, rid):
        return [x for x in self.ok(who, "GET", "/api/rfqs") if x["id"] == rid][0]["commits"][0]

    PH = "data:image/jpeg;base64," + __import__("base64").b64encode(b"\xff\xd8\xff\xe0" + b"0" * 200).decode()

    def test_06_huy_ke_o_giai_doan_som(self):
        for stage in ("CHUA_BAT_DAU", "GIEO_TRONG"):
            rf, cm = self._kèo("nongdan2", stage=stage if stage != "CHUA_BAT_DAU" else None, days=5)  # sát hạn giao vẫn tự hủy được
            self.assertEqual(self._commit("nongdan2", rf["id"])["cancel"]["mode"], "direct")
            self.assertTrue(self.ok("nongdan2", "POST", "/api/farm/commits/%d/cancel" % cm["id"])["lost_deposit"])

    def test_07_dang_cham_soc_tu_huy_co_dieu_kien(self):
        rf, cm = self._kèo("nongdan2", stage="CHAM_SOC", days=server.CANCEL_FREE_DAYS + 1)
        self.assertEqual(self._commit("nongdan2", rf["id"])["cancel"]["mode"], "direct")
        self.ok("nongdan2", "POST", "/api/farm/commits/%d/cancel" % cm["id"])
        rf, cm = self._kèo("nongdan2", stage="CHAM_SOC", days=server.CANCEL_FREE_DAYS)  # đúng ngưỡng: chưa "trên" 10 ngày
        self.assertEqual(self._commit("nongdan2", rf["id"])["cancel"]["mode"], "request")
        s, r = self.c("nongdan2", "POST", "/api/farm/commits/%d/cancel" % cm["id"])
        self.assertEqual(s, 409)
        self.assertIn("xin hủy", r["error"])
        self.assertEqual(self._commit("kinhdoanh", rf["id"])["status"], "DA_DUYET")

    def test_08_sap_thu_hoach_khoa_cung(self):
        for stage in ("SAP_THU_HOACH", "DA_THU_HOACH"):
            rf, cm = self._kèo("nongdan2", stage=stage, days=60)  # còn rất lâu vẫn khóa
            self.assertEqual(self._commit("nongdan2", rf["id"])["cancel"]["mode"], "locked")
            self.assertEqual(self.c("nongdan2", "POST", "/api/farm/commits/%d/cancel" % cm["id"])[0], 409)
            s, r = self.c("nongdan2", "POST", "/api/farm/commits/%d/request-cancel" % cm["id"], {"reason": "Sâu bệnh", "detail": "Sâu bệnh lan rộng khắp vườn", "photos": [self.PH]})
            self.assertEqual(s, 409)
            self.assertEqual(self._commit("kinhdoanh", rf["id"])["status"], "DA_DUYET")

    def test_09_khong_lui_tien_do_de_lach_luat(self):
        rf, cm = self._kèo("nongdan2", stage="SAP_THU_HOACH")
        self.assertEqual(self.c("nongdan2", "POST", "/api/farm/commits/%d/progress" % cm["id"], {"stage": "GIEO_TRONG"})[0], 409)
        self.assertEqual(self.c("nongdan2", "POST", "/api/farm/commits/%d/cancel" % cm["id"])[0], 409)

    def test_10_xin_huy_kiem_tra_dau_vao(self):
        rf, cm = self._kèo("nongdan2", stage="CHAM_SOC", days=5)
        url = "/api/farm/commits/%d/request-cancel" % cm["id"]
        ok = {"reason": "Sâu bệnh", "detail": "Sâu đục thân lan ra 60% diện tích", "photos": [self.PH]}
        self.assertEqual(self.c("nongdan2", "POST", url, {**ok, "photos": []})[0], 400)  # thiếu ảnh
        self.assertEqual(self.c("nongdan2", "POST", url, {**ok, "reason": "Chán"})[0], 400)
        self.assertEqual(self.c("nongdan2", "POST", url, {**ok, "detail": "ngắn"})[0], 400)
        self.assertEqual(self.c("nongdan2", "POST", url, {**ok, "photos": ["data:image/jpeg;base64,AAAA"]})[0], 400)  # không phải ảnh thật
        self.assertEqual(self.c("nongdan2", "POST", url, {**ok, "photos": ["http://x/y.jpg"]})[0], 400)
        self.assertEqual(self.c("nongdan", "POST", url, ok)[0], 404)  # kèo của nông trại khác
        self.assertEqual(self.c("kinhdoanh", "POST", url, ok)[0], 403)
        self.ok("nongdan2", "POST", url, ok)
        self.assertEqual(self.c("nongdan2", "POST", url, ok)[0], 409)  # đã gửi rồi
        self.assertEqual(self.c("nongdan2", "POST", "/api/farm/commits/%d/progress" % cm["id"], {"stage": "SAP_THU_HOACH"})[0], 409)
        self.assertEqual(self.c("nongdan2", "POST", "/api/farm/commits/%d/cancel" % cm["id"])[0], 409)
        self.assertEqual(self.c("nongdan2", "POST", "/api/commits/%d/cancel-decision" % cm["id"], {"action": "approve"})[0], 403)  # nông dân không tự duyệt
        self.assertEqual(self.c("nongdan2", "GET", "/api/commits/%d/cancel-request" % cm["id"])[0], 403)
        self.ok("kinhdoanh", "POST", "/api/commits/%d/cancel-decision" % cm["id"], {"action": "reject", "note": "Chưa đủ bằng chứng"})

    def test_11_duyet_huy_phat_muc_1_va_tra_kg_ve_rfq(self):
        self._reset(2)
        rf, cm = self._kèo("nongdan2", qty=100, stage="CHAM_SOC", days=5)
        self.assertEqual(self.ok("kinhdoanh", "GET", "/api/rfqs")[[x["id"] for x in self.ok("kinhdoanh", "GET", "/api/rfqs")].index(rf["id"])]["status"], "DU_SL")
        self.ok("nongdan2", "POST", "/api/farm/commits/%d/request-cancel" % cm["id"], {"reason": "Sâu bệnh", "detail": "Sâu đục thân lan ra 60% diện tích", "photos": [self.PH]})
        r = [x for x in self.ok("kinhdoanh", "GET", "/api/rfqs") if x["id"] == rf["id"]][0]
        self.assertEqual(r["commits"][0]["status"], "CHO_DUYET_HUY")
        self.assertEqual(r["committed_kg"], 100)  # chưa duyệt thì vẫn giữ sản lượng
        q = self.ok("kinhdoanh", "GET", "/api/commits/%d/cancel-request" % cm["id"])
        self.assertEqual(len(q["photos"]), 1)
        self.assertEqual(q["level"], 1)
        r = self.ok("kinhdoanh", "POST", "/api/commits/%d/cancel-decision" % cm["id"], {"action": "approve"})
        self.assertEqual(r["status"], "HUY")
        c = [x for x in self.ok("kinhdoanh", "GET", "/api/rfqs") if x["id"] == rf["id"]][0]
        self.assertEqual((c["commits"][0]["status"], c["commits"][0]["deposit_status"]), ("HUY", "MAT_COC"))
        self.assertEqual((c["status"], c["remaining_kg"]), ("MO", 100))  # số kg thiếu được cộng trả lại, RFQ mở để gom tiếp
        sp = self._sup(2)
        self.assertEqual(sp["reputation"], 90)
        self.assertEqual(sp["blocked_until"], server.add_days(self.today, 3))
        s, e = self.c("nongdan2", "POST", "/api/farm/rfqs/%d/commit" % rf["id"], {"qty_kg": 50, "price": 65000})
        self.assertEqual(s, 409)  # bị khóa nhận kèo mới
        self.assertIn("khóa", e["error"])
        self.assertEqual(self.c("nongdan", "POST", "/api/farm/rfqs/%d/commit" % rf["id"], {"qty_kg": 100, "price": 65000})[0], 200)  # nông dân khác gom được
        self.assertEqual(self.c("kinhdoanh", "POST", "/api/commits/%d/cancel-decision" % cm["id"], {"action": "approve"})[0], 409)  # không duyệt hai lần
        self._reset(2)

    def test_12_mien_tru_phat_hoan_coc(self):
        self._reset(2)
        rf, cm = self._kèo("nongdan2", stage="CHAM_SOC", days=3)
        self.ok("nongdan2", "POST", "/api/farm/commits/%d/request-cancel" % cm["id"], {"reason": "Thiên tai", "detail": "Bão làm đổ toàn bộ giàn trồng", "photos": [self.PH, self.PH]})
        self.ok("kinhdoanh", "POST", "/api/commits/%d/cancel-decision" % cm["id"], {"action": "approve", "waive": True})
        c = self._commit("kinhdoanh", rf["id"])
        self.assertEqual((c["status"], c["deposit_status"]), ("HUY", "HOAN"))
        sp = self._sup(2)
        self.assertEqual((sp["reputation"], sp["blocked_until"]), (100, None))

    def test_13_muc_2_khi_mo_cho_phep_xin_huy_sap_thu_hoach(self):
        self._reset(2)
        server.ALLOW_REQUEST_AT_HARVEST = True
        try:
            rf, cm = self._kèo("nongdan2", qty=100, stage="SAP_THU_HOACH", days=20)
            self.ok("nongdan2", "POST", "/api/farm/commits/%d/request-cancel" % cm["id"], {"reason": "Mất mùa", "detail": "Mưa đá làm mất gần hết sản lượng", "photos": [self.PH]})
            self.ok("kinhdoanh", "POST", "/api/commits/%d/cancel-decision" % cm["id"], {"action": "approve"})
        finally:
            server.ALLOW_REQUEST_AT_HARVEST = False
        sp = self._sup(2)
        self.assertEqual(sp["reputation"], 75)
        self.assertEqual(sp["blocked_until"], server.add_days(self.today, 7))
        self.assertEqual((sp["cap_pct"], sp["cap_until"]), (30, server.add_days(self.today, 30)))
        c = server.connect()
        c.execute("UPDATE suppliers SET blocked_until=NULL WHERE id=2")  # hết 7 ngày khóa, còn giới hạn 30% sản lượng
        c.close()
        rf2 = self.ok("kinhdoanh", "POST", "/api/rfqs", {"customer_id": 3, "sku": "XCL", "qty_kg": 1000, "standard": "Bất kỳ", "price_min": 60000, "price_max": 70000,
                                                        "deliver_by": server.add_days(self.today, 60), "deposit_pct": 10})
        self.assertEqual(self.c("nongdan2", "POST", "/api/farm/rfqs/%d/commit" % rf2["id"], {"qty_kg": 400, "price": 65000})[0], 409)  # 400 > 30% của 1000
        self.assertEqual(self.c("nongdan2", "POST", "/api/farm/rfqs/%d/commit" % rf2["id"], {"qty_kg": 300, "price": 65000})[0], 200)
        self._reset(2)

    def test_14_migrate_csdl_cu(self):
        import sqlite3
        path = os.path.join(_tmp, "old.db")
        old = server.DB_PATH
        server.DB_PATH = path
        try:
            server.init_db()
            c = server.connect()
            c.executescript("PRAGMA foreign_keys=OFF; CREATE TABLE commits_old AS SELECT * FROM commits; DROP TABLE cancel_requests; DROP TABLE commits;"
                            "CREATE TABLE commits(id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, rfq_id INTEGER NOT NULL, supplier_id INTEGER NOT NULL, qty_kg REAL NOT NULL, price INTEGER NOT NULL,"
                            "deposit REAL NOT NULL DEFAULT 0, deposit_status TEXT NOT NULL DEFAULT 'CHO' CHECK(deposit_status IN ('CHO','DA_COC','HOAN','MAT_COC')),"
                            "status TEXT NOT NULL DEFAULT 'CHO_DUYET' CHECK(status IN ('CHO_DUYET','DA_DUYET','TU_CHOI','HUY','DA_GIAO')),"
                            "stage TEXT NOT NULL DEFAULT 'CHUA_BAT_DAU', stage_note TEXT, stage_at TEXT, decided_by INTEGER, created_at TEXT NOT NULL);"
                            "INSERT INTO commits SELECT * FROM commits_old; DROP TABLE commits_old;")
            c.close()
            server.init_db()  # chạy lại trên CSDL cũ: dựng lại bảng, giữ dữ liệu
            c = server.connect()
            self.assertEqual(c.execute("SELECT COUNT(*) FROM commits").fetchone()[0], 2)
            c.execute("UPDATE commits SET status='CHO_DUYET_HUY' WHERE id=1")  # CHECK mới chấp nhận trạng thái mới
            self.assertEqual(c.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertIn("reputation", [r[1] for r in c.execute("PRAGMA table_info(suppliers)")])
            c.close()
        finally:
            server.DB_PATH = old

    # ---- Luồng Kế hoạch (KH) -> Phiếu giao (GH) -> Phiếu nhập (PN) -> Duyệt: đồng bộ tồn kho, phiếu giao, kế hoạch, công nợ
    def _plan(self, kg=1000, sku="SLX", farmer="nongdan", confirm=True):
        pid = self.ok(farmer, "POST", "/api/plans", {"sku": sku, "area_ha": 1, "expected_kg": kg, "expected_date": self.today})["id"]
        if confirm:
            self.ok("thukho", "POST", "/api/plans/%d/update" % pid, {"action": "confirm"})
        return pid

    def _p(self, who, pid):
        return [x for x in self.ok(who, "GET", "/api/plans")["plans"] if x["id"] == pid][0]

    def _gh(self, farmer, pid, **kw):
        body = {"source": "p:%d" % pid, "qty_kg": 1000, "harvest_date": self.today, "spray_date": server.add_days(self.today, -20), "phi_days": 7,
                "warehouse_id": 3, "vehicle": "49C-123.45", **kw}
        return self.c(farmer, "POST", "/api/farm/batches", body)

    def _batch(self, who, bid):
        return [x for x in self.ok(who, "GET", "/api/batches") if x["id"] == bid][0]

    def test_15_ke_hoach_giao_hang_nhap_kho_cong_no(self):
        pid = self._plan(1000, confirm=False)
        self.assertEqual(self._gh("nongdan", pid)[0], 409)  # chưa xác nhận thì chưa có quyền tạo phiếu giao
        self.assertEqual(self.c("thukho", "POST", "/api/plans/%d/update" % pid, {"action": "confirm"})[0], 200)
        p = self._p("nongdan", pid)
        # Đã xác nhận: ẩn Sửa/Hủy, giữ Dời ngày (bắt buộc lý do) và Tạo phiếu giao
        self.assertEqual((p["editable"], p["cancelable"], p["can_postpone"], p["need_reason"], p["can_deliver"]), (False, False, True, True, True))
        self.assertEqual(self.c("nongdan", "POST", "/api/plans/%d/update" % pid, {"action": "edit", "expected_kg": 1})[0], 409)
        self.assertEqual(self.c("nongdan", "POST", "/api/plans/%d/update" % pid, {"action": "cancel"})[0], 409)
        self.assertEqual(self.c("nongdan", "POST", "/api/plans/%d/update" % pid, {"action": "postpone", "expected_date": server.add_days(self.today, 3)})[0], 400)
        self.assertEqual(self.c("nongdan2", "POST", "/api/farm/batches", {"source": "p:%d" % pid, "qty_kg": 10, "harvest_date": self.today, "warehouse_id": 3})[0], 404)  # kế hoạch của nông trại khác
        # Bước 2-3: nông dân lập phiếu giao từ KH (nguồn KH), kế hoạch -> Đang giao hàng và khóa thao tác
        s, r = self._gh("nongdan", pid, qty_kg=980)
        self.assertEqual(s, 200, r)
        bid = r["id"]
        p = self._p("nongdan", pid)
        self.assertEqual(p["status"], "DANG_GIAO")
        self.assertEqual((p["editable"], p["can_postpone"], p["can_deliver"]), (False, False, False))
        self.assertEqual(p["batch_code"], r["code"])
        for act in ("edit", "postpone", "cancel", "harvested"):
            self.assertEqual(self.c("nongdan", "POST", "/api/plans/%d/update" % pid, {"action": act, "expected_date": server.add_days(self.today, 3), "reason": "x", "expected_kg": 5})[0], 409, act)
        self.assertEqual(self._gh("nongdan", pid)[0], 409)  # mỗi kế hoạch chỉ một phiếu giao đang xử lý
        b = self._batch("nongdan", bid)
        self.assertEqual((b["plan_code"], b["source_code"], b["status"], b["offer_code"], b["commit_code"]), (p["code"], p["code"], "CHO_GIAO", None, None))
        # QR sinh được cho nguồn KH, trang công khai báo chờ kho nhận
        self.assertIn("<svg", self.ok("nongdan", "GET", "/api/batches/%d/qr" % bid)["svg"])
        k = server.qr_token(b["code"])
        self.assertEqual(self.ok("nongdan", "GET", "/api/public/lot?c=%s&k=%s" % (b["code"], k))["verdict"], "CHO_KIEM_DINH")
        # luồng QC cũ không dùng cho phiếu nguồn KH; nông dân không tự lập phiếu nhập
        self.assertEqual(self.c("thukho", "POST", "/api/batches/%d/receive" % bid, {"qty_l1": 980, "residue": 0.1, "meets_standard": True})[0], 409)
        self.assertEqual(self.c("nongdan", "POST", "/api/batches/%d/receipt" % bid, {"qty_kg": 980})[0], 403)
        self.assertEqual(self.c("kinhdoanh", "POST", "/api/batches/%d/receipt" % bid, {"qty_kg": 980})[0], 403)
        self.assertEqual(self.c("thukho", "POST", "/api/batches/%d/receipt" % bid, {"qty_kg": 0})[0], 400)
        self.assertEqual(self.c("thukho", "POST", "/api/batches/%d/receipt" % bid, {"qty_kg": 1500})[0], 409)  # cân vượt phiếu giao hơn 10%
        self.ok("kinhdoanh", "POST", "/api/prices", {"sku": "SLX", "kind": "THU_MUA", "market": "Giá thu mua của công ty", "price": 31000, "price_date": self.today})
        before = self.ok("thukho", "GET", "/api/dashboard")["total_kg"]
        # Bước 4: Thủ kho cân 975 kg, lập phiếu nhập (Chờ duyệt). Quản lý từ chối thì phiếu giao quay về Chờ nhập kho để cân lại
        pn1 = self.ok("thukho", "POST", "/api/batches/%d/receipt" % bid, {"qty_kg": 975})
        self.assertEqual(self.c("thukho", "POST", "/api/batches/%d/receipt" % bid, {"qty_kg": 975})[0], 409)  # đã có PN chờ duyệt
        self.assertEqual(self.c("thukho", "POST", "/api/batches/%d/refuse" % bid, {"reason": "x"})[0], 409)
        self.assertEqual(self.c("thukho", "POST", "/api/receipts/%d/approve" % pn1["id"])[0], 403)  # Thủ kho không duyệt
        self.assertEqual(self.ok("thukho", "GET", "/api/dashboard")["total_kg"], before)  # chưa duyệt: tồn chưa đổi
        self.ok("quanly", "POST", "/api/receipts/%d/reject" % pn1["id"])
        self.assertIsNone(self._batch("quanly", bid)["receipt_id"])
        pn = self.ok("thukho", "POST", "/api/batches/%d/receipt" % bid, {"qty_kg": 980})
        # Bước 5: Quản lý kho duyệt -> đồng bộ tồn kho, phiếu giao, kế hoạch, công nợ
        r = self.ok("quanly", "POST", "/api/receipts/%d/approve" % pn["id"])
        self.assertEqual((r["plan_code"], r["unit_price"], r["amount"]), (p["code"], 31000, 980 * 31000))
        self.assertEqual(self.ok("thukho", "GET", "/api/dashboard")["total_kg"] - before, 980)  # 1) tồn kho cộng đúng khối lượng thực nhận
        lot = [l for l in self.ok("thukho", "GET", "/api/lots?q=" + r["lot_code"])][0]
        self.assertEqual((lot["qty_kg"], lot["warehouse_id"]), (980, 3))
        b = self._batch("quanly", bid)
        self.assertEqual((b["status"], b["recv_kg"], b["lot_codes"], b["receipt_code"]), ("DA_NHAP", 980, r["lot_code"], pn["code"]))  # 2) phiếu giao Đã nhập kho
        p = self._p("nongdan", pid)
        self.assertEqual((p["status"], p["actual_kg"], p["lot_code"]), ("DA_NHAP", 980, r["lot_code"]))  # 3) kế hoạch Đã nhập kho, đối soát -20 kg
        self.assertEqual(p["actual_kg"] - p["expected_kg"], -20)
        for who in ("nongdan", "quanly", "thukho"):  # 4) đúng 01 công nợ, Chờ thanh toán, đơn giá thu mua lúc duyệt
            mine = [x for x in self.ok(who, "GET", "/api/batches") if x["plan_id"] == p["id"]]
            self.assertEqual(len(mine), 1, who)
            d = mine[0]
            self.assertEqual((d["debt_status"], d["price"], d["amount"], d["due"], d["receipt_code"], d["plan_code"]),
                             ("CHO_THANH_TOAN", 31000, 980 * 31000, 980 * 31000, pn["code"], p["code"]), who)
        # đổi giá thu mua sau khi duyệt không làm đổi công nợ đã sinh
        self.ok("kinhdoanh", "POST", "/api/prices", {"sku": "SLX", "kind": "THU_MUA", "market": "Giá thu mua của công ty", "price": 99000, "price_date": self.today})
        self.assertEqual(self._batch("nongdan", bid)["amount"], 980 * 31000)
        self.assertEqual(self.c("quanly", "POST", "/api/receipts/%d/approve" % pn["id"])[0], 409)  # không duyệt hai lần
        self.assertEqual(self.c("thukho", "POST", "/api/batches/%d/pay" % bid)[0], 403)  # chỉ Quản lý kho xác nhận thanh toán
        self.assertEqual(self.ok("quanly", "POST", "/api/batches/%d/pay" % bid)["paid"], 980 * 31000)
        self.assertEqual(self.c("quanly", "POST", "/api/batches/%d/pay" % bid)[0], 409)
        self.assertEqual(self._batch("nongdan", bid)["debt_status"], "DA_THANH_TOAN")
        # QR phiếu giao dẫn tới hồ sơ lô thật sau khi nhập kho
        self.assertEqual(self.ok("nongdan", "GET", "/api/public/lot?c=%s&k=%s" % (b["code"], k))["code"], r["lot_code"])
        self.assertTrue(any("Kế hoạch %s" % p["code"] in a["action"] and "thực nhận 980" in a["action"] for a in self.ok("quanly", "GET", "/api/audit")))

    def test_16_vi_pham_cach_ly_tu_choi_nhap(self):
        pid = self._plan(500)
        self.ok("nongdan", "POST", "/api/plans/%d/update" % pid, {"action": "harvested"})
        self.assertEqual(self._p("nongdan", pid)["status"], "DA_THU_HOACH")
        self.assertTrue(self._p("nongdan", pid)["can_deliver"])  # Đã thu hoạch cũng lập được phiếu giao
        s, r = self._gh("nongdan", pid, qty_kg=500, spray_date=server.add_days(self.today, -3), phi_days=7)  # mới phun 3 ngày, cần 7 ngày
        self.assertEqual(s, 200, r)
        bid = r["id"]
        b = self._batch("thukho", bid)
        self.assertEqual((b["phi_ok"], b["gap_days"]), (False, 3))  # giao diện hiện cảnh báo đỏ dựa vào cờ này
        s, e = self.c("thukho", "POST", "/api/batches/%d/receipt" % bid, {"qty_kg": 500})
        self.assertEqual(s, 409)
        self.assertIn("cách ly", e["error"])
        self.assertEqual(self.c("thukho", "POST", "/api/batches/%d/refuse" % bid, {})[0], 400)  # phải có lý do
        self.assertEqual(self.c("nongdan", "POST", "/api/batches/%d/refuse" % bid, {"reason": "x"})[0], 403)
        self.ok("thukho", "POST", "/api/batches/%d/refuse" % bid, {"reason": "Chưa đủ thời gian cách ly"})
        b = self._batch("nongdan", bid)
        self.assertEqual((b["status"], b["qc_note"]), ("TU_CHOI", "Chưa đủ thời gian cách ly"))
        self.assertEqual(self._p("nongdan", pid)["status"], "DA_THU_HOACH")  # kế hoạch trả về Đã thu hoạch
        self.assertEqual(self.c("thukho", "POST", "/api/batches/%d/refuse" % bid, {"reason": "x"})[0], 409)
        self.assertEqual(self._gh("nongdan", pid, qty_kg=500)[0], 200)  # nông dân lập lại phiếu giao hợp lệ

    def test_17_thieu_gia_thu_mua_khong_duyet_duoc(self):
        pid = self._plan(300, sku="CCB")
        bid = self._gh("nongdan", pid, qty_kg=300, warehouse_id=2)[1]["id"]
        pn = self.ok("thukho", "POST", "/api/batches/%d/receipt" % bid, {"qty_kg": 300})
        c = server.connect()
        saved = c.execute("SELECT * FROM market_prices WHERE sku='CCB' AND kind='THU_MUA'").fetchall()
        c.execute("DELETE FROM market_prices WHERE sku='CCB' AND kind='THU_MUA'")
        try:
            s, e = self.c("quanly", "POST", "/api/receipts/%d/approve" % pn["id"])
            self.assertEqual(s, 409)
            self.assertIn("Giá thu mua", e["error"])
            self.assertEqual(self._p("nongdan", pid)["status"], "DANG_GIAO")  # không đồng bộ dở dang
        finally:
            for r in saved:
                c.execute("INSERT INTO market_prices(id,sku,kind,market,price,price_date,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                          (r["id"], r["sku"], r["kind"], r["market"], r["price"], r["price_date"], r["created_by"], r["created_at"]))
            c.close()
        self.assertEqual(self.ok("quanly", "POST", "/api/receipts/%d/approve" % pn["id"])["status"], "DA_DUYET")

    def test_18_migrate_bang_ke_hoach_cu(self):
        path = os.path.join(_tmp, "oldplans.db")
        old = server.DB_PATH
        server.DB_PATH = path
        try:
            server.init_db()
            c = server.connect()
            cols = "id,code,supplier_id,sku,area_ha,expected_kg,expected_date,status,postponed,note,created_by,created_at,lot_id,actual_kg"
            c.executescript("PRAGMA foreign_keys=OFF; CREATE TABLE plans_old AS SELECT %s FROM plans; DROP TABLE plans;" % cols
                            + "CREATE TABLE plans(id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, supplier_id INTEGER NOT NULL, sku TEXT NOT NULL, area_ha REAL NOT NULL,"
                            "expected_kg REAL NOT NULL, expected_date TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'DU_KIEN' CHECK(status IN ('DU_KIEN','XAC_NHAN','DA_NHAP','HUY')),"
                            "postponed INTEGER NOT NULL DEFAULT 0, note TEXT, created_by INTEGER, created_at TEXT NOT NULL, lot_id INTEGER, actual_kg REAL);"
                            "INSERT INTO plans(%s) SELECT %s FROM plans_old; DROP TABLE plans_old;" % (cols, cols))
            n = c.execute("SELECT COUNT(*) FROM plans").fetchone()[0]
            c.close()
            server.init_db()  # chạy lại trên CSDL cũ: dựng lại bảng, giữ dữ liệu, thêm trạng thái mới
            c = server.connect()
            self.assertEqual(c.execute("SELECT COUNT(*) FROM plans").fetchone()[0], n)
            c.execute("UPDATE plans SET status='DANG_GIAO' WHERE id=1")
            c.execute("UPDATE plans SET status='DA_THU_HOACH' WHERE id=2")
            self.assertIn("postpone_reason", [r[1] for r in c.execute("PRAGMA table_info(plans)")])
            for t, col in (("batches", "plan_id"), ("batches", "receipt_id"), ("batches", "recv_kg"), ("receipts", "batch_id")):
                self.assertIn(col, [r[1] for r in c.execute("PRAGMA table_info(%s)" % t)])
            self.assertEqual(c.execute("PRAGMA foreign_key_check").fetchall(), [])
            c.close()
        finally:
            server.DB_PATH = old


    # ---- Vận chuyển 2 chặng (Nông dân -> Kho -> Cửa hàng) và GPS ----
    def _shift(self, tid, minutes):
        c = server.connect()
        c.execute("UPDATE trip_points SET created_at=datetime(created_at,?) WHERE trip_id=?", ("-%d minutes" % minutes, tid))
        c.close()

    def _loc(self, tok, t, f, gap=10, **kw):
        """Gửi điểm GPS nằm ở tỷ lệ f (0..1) của đường thẳng điểm đi -> điểm đến; lùi giờ các điểm cũ để vượt giới hạn tần suất và tốc độ."""
        self._shift(t["id"], gap)
        return call(self.base, "POST", "/api/driver/location", {"t": tok, "lat": t["o_lat"] + (t["d_lat"] - t["o_lat"]) * f, "lon": t["o_lon"] + (t["d_lon"] - t["o_lon"]) * f, **kw})

    def _trip(self, who, tid):
        return self.ok(who, "GET", "/api/trips/%d" % tid)

    def _new_inbound(self, **kw):
        pid = self._plan(1000)
        s, r = self._gh("nongdan", pid, qty_kg=980, vehicle=kw.pop("vehicle", "51D-123.45"), driver_name="Trần Văn Tài", driver_phone="0912 345 678", **kw)
        self.assertEqual(s, 200, r)
        t = [x for x in self.ok("quanly", "GET", "/api/trips?leg=in") if x["batch_id"] == r["id"]][0]
        return pid, r, t

    def test_19_van_chuyen_xuyen_suot_2_chang(self):
        os.environ["KHO_GPS_MIN_INTERVAL"] = "0"
        pid, r, t = self._new_inbound()
        bid = r["id"]
        # chặng 1 tự sinh cùng lúc lập phiếu: đúng điểm đi (nông trại), điểm đến (kho nhận), biển số, tài xế
        self.assertTrue(r["trip_code"].startswith("VC-IN-"))
        self.assertEqual((t["leg_type"], t["status"], t["origin_name"], t["d_name"], t["plate"], t["driver"], t["supplier_id"]),
                         ("FARM_TO_WAREHOUSE", "CHO_LAY_HANG", "Nông trại Rau Sạch Đà Lạt Xanh", "Kho Đà Lạt", "51D-123.45", "Trần Văn Tài", 1))
        self.assertEqual(t["driver_phone"], "0912 345 678")
        self.assertTrue(t["token"] and len(t["pin"]) == 6)
        self.assertEqual(t["cargo"][0]["qty_kg"], 980)
        # lập lại lần 2 không sinh trùng chuyến
        self.assertEqual(self._gh("nongdan", pid)[0], 409)
        self.assertEqual(len([x for x in self.ok("quanly", "GET", "/api/trips?leg=in") if x["batch_id"] == bid]), 1)
        c = server.connect()
        with self.assertRaises(Exception):  # ràng buộc UNIQUE(batch_id) chặn trùng ở mức CSDL
            c.execute("INSERT INTO trips(code,leg_type,batch_id,token,o_lat,o_lon,d_name,d_lat,d_lon,temp_min,temp_max,created_at) VALUES('VC-IN-X','FARM_TO_WAREHOUSE',?,'tk',1,1,'x',2,2,1,2,'2026-01-01 00:00:00')", (bid,))
        c.close()
        tok = t["token"]
        self.assertEqual(call(self.base, "POST", "/api/driver/status", {"t": tok, "status": "DANG_VAN_CHUYEN"})[0], 200)
        # cập nhật GPS chặng 1: tiến độ tăng, "Sắp đến nơi" khi >= 85%
        prog = []
        for f in (0.0, 0.3, 0.6):
            self.assertEqual(self._loc(tok, t, f, speed=10)[0], 200)
            prog.append(self._trip("quanly", t["id"])["progress"])
        self.assertTrue(prog[0] < prog[1] < prog[2] < 85, prog)
        self.assertFalse(self._trip("quanly", t["id"])["near_arrival"])
        self.assertEqual(self._loc(tok, t, 0.92, speed=10)[0], 200)
        cur = self._trip("quanly", t["id"])
        self.assertTrue(cur["near_arrival"] and cur["progress"] >= 85, cur["progress"])
        self.assertEqual(cur["status"], "DANG_VAN_CHUYEN")
        self.assertEqual(cur["gps_timeline"][0]["text"], "Xuất phát tại Nông trại Rau Sạch Đà Lạt Xanh")
        self.assertEqual(self.c("quanly", "GET", "/api/trips")[1][0]["id"], t["id"])  # xếp đầu danh sách: Sắp đến nơi trước tiên
        for who in ("thukho", "quanly"):
            self.assertIn(t["code"], [x["code"] for x in self.ok(who, "GET", "/api/dashboard")["near_arrival"]], who)
        # nhãn trong tab Thu mua QC: xe sắp đến, chưa có cảnh báo nhiệt độ
        self.assertTrue(self._batch("thukho", bid)["trip"]["near_arrival"])
        self.assertFalse(self._batch("thukho", bid)["trip"]["temp_check"])
        # Thủ kho tiếp nhận phiếu -> chặng 1 tự chuyển Đã giao (tài xế chưa bấm)
        self.ok("kinhdoanh", "POST", "/api/prices", {"sku": "SLX", "kind": "THU_MUA", "market": "Giá thu mua của công ty", "price": 31000, "price_date": self.today})
        pn = self.ok("thukho", "POST", "/api/batches/%d/receipt" % bid, {"qty_kg": 980})
        done = self._trip("quanly", t["id"])
        self.assertEqual((done["status"], done["progress"], done["near_arrival"]), ("DA_GIAO", 100, False))
        appr = self.ok("quanly", "POST", "/api/receipts/%d/approve" % pn["id"])
        lid = [l for l in self.ok("thukho", "GET", "/api/lots?q=" + appr["lot_code"])][0]["id"]
        self.ok("qa", "POST", "/api/lots/%d/inspect" % lid, {"result": "DAT", "grade": "Loại 1", "residue": 0.01})  # QA kiểm định đạt thì lô mới bán được
        # chặng 2: xuất kho đơn hàng -> sinh VC-OUT đúng cửa hàng đích, xe do Thủ kho gán
        avail = [p for p in self.ok("thukho", "GET", "/api/dashboard")["products"] if p["sku"] == "SLX"][0]["available"]
        s, o = self.c("kinhdoanh", "POST", "/api/orders", {"customer_id": 1, "items": [{"sku": "SLX", "qty_kg": avail}]})
        self.assertEqual(s, 200, o)
        self.ok("thukho", "POST", "/api/orders/%d/reserve" % o["id"])
        out_before = self.ok("thukho", "GET", "/api/dashboard")["active_out"]
        self.assertEqual(self.c("thukho", "POST", "/api/orders/%d/ship" % o["id"], {"plate": "51D-999.45", "driver": "Lê Văn Xe", "phone": "0988 111 222"})[0], 200)
        out = [x for x in self.ok("kinhdoanh", "GET", "/api/trips?leg=out") if x["order_id"] == o["id"]][0]
        self.assertTrue(out["code"].startswith("VC-OUT-"))
        self.assertEqual((out["leg_type"], out["d_name"], out["plate"], out["driver"], out["status"]),
                         ("WAREHOUSE_TO_STORE", "Siêu thị Co.op Smile Q7", "51D-999.45", "Lê Văn Xe", "CHO_LAY_HANG"))
        for k in ("token", "pin", "driver_phone"):  # Kinh doanh chỉ xem: không thấy liên kết, PIN, SĐT tài xế
            self.assertNotIn(k, out)
        full = [x for x in self.ok("thukho", "GET", "/api/trips?leg=out") if x["id"] == out["id"]][0]
        self.assertTrue(full["token"] and full["pin"] and full["driver_phone"] == "0988 111 222")
        self.assertEqual(self.ok("thukho", "GET", "/api/dashboard")["active_out"], out_before + 1)
        self.assertEqual(self.ok("kinhdoanh", "GET", "/api/dashboard")["active_out"], out_before + 1)
        self.assertNotIn("active_in", self.ok("kinhdoanh", "GET", "/api/dashboard"))
        # cập nhật GPS chặng 2
        self.assertEqual(self._loc(full["token"], full, 0.0, speed=12)[0], 200)
        self.assertEqual(self._loc(full["token"], full, 0.5, gap=90, speed=12)[0], 200)
        mid = self._trip("kinhdoanh", out["id"])
        self.assertTrue(30 <= mid["progress"] <= 70, mid["progress"])
        # truy xuất nguồn gốc trên xe (phía Kinh doanh): lô -> phiếu nhập -> phiếu giao -> nông dân và nông trại
        mine = [x for x in mid["cargo"] if x["lot_code"] == appr["lot_code"]][0]
        og = mine["origin"]
        self.assertEqual((og["farmer"], og["farm_name"], og["batch_code"], og["receipt_code"], og["direct"]),
                         ("Võ Văn Nam", "Nông trại Rau Sạch Đà Lạt Xanh", r["code"], pn["code"], False))
        self.assertEqual(mine["qty_kg"], 980)
        # lô nhập thủ công (không có phiếu giao): hiển thị "Nhập kho trực tiếp", không lỗi
        for x in mid["cargo"]:  # mọi lô trên xe đều truy ngược được, lô không có phiếu giao thì ghi nhập kho trực tiếp
            og2 = x["origin"]
            self.assertEqual(og2["direct"], og2["batch_code"] is None)
            self.assertTrue(og2["direct"] and og2["text"].startswith("Nhập kho trực tiếp, nhà cung cấp: ") or not og2["direct"] and og2["text"] is None)
        c = server.connect()
        lot = c.execute("""SELECT id,code,supplier_id FROM lots WHERE id NOT IN (SELECT lot_id FROM receipts WHERE lot_id IS NOT NULL)
          AND NOT EXISTS (SELECT 1 FROM batches b WHERE (',' || COALESCE(b.lot_codes,'') || ',') LIKE '%,' || lots.code || ',%') LIMIT 1""").fetchone()
        self.assertIsNotNone(lot)  # lô mẫu nhập thủ công
        do = server.lot_origin(c, dict(lot))
        c.close()
        self.assertTrue(do["direct"] and do["text"].startswith("Nhập kho trực tiếp, nhà cung cấp: ") and do["batch_code"] is None)
        # trang truy xuất công khai: có hành trình 2 chặng nhưng không lộ cửa hàng, SĐT tài xế, biển số đầy đủ
        k = server.qr_token(appr["lot_code"])
        pub0 = self.ok("kinhdoanh", "GET", "/api/public/lot?c=%s&k=%s" % (appr["lot_code"], k))
        self.assertEqual([g["leg"] for g in pub0["journey"]], [1])  # QR nhập kho / QR cũ: chỉ đến bước nhập kho, không lộ chặng 2 nào
        ks = server.qr_token(appr["lot_code"], mid["code"])
        self.assertEqual(self.c("kinhdoanh", "GET", "/api/public/lot?c=%s&k=%s&s=%s" % (appr["lot_code"], k, mid["code"]))[0], 404)  # k của lô không dùng được cho tem chuyến
        pub = self.ok("kinhdoanh", "GET", "/api/public/lot?c=%s&k=%s&s=%s" % (appr["lot_code"], ks, mid["code"]))
        self.assertEqual([g["leg"] for g in pub["journey"]], [1, 2])
        txt = json.dumps(pub, ensure_ascii=False)
        for secret in ("Co.op", "Smile", "Nguyễn Thị Thập", "0912 345 678", "0988 111 222", "51D-123.45", "51D-999.45", "Lê Văn Xe", "Trần Văn Tài", full["token"], t["token"]):
            self.assertNotIn(secret, txt, secret)
        self.assertEqual((pub["journey"][0]["plate"], pub["journey"][1]["to"]), ("51D-***.45", "Điểm bán lẻ"))
        self.assertEqual(pub["journey"][0]["from"], "Nông trại Rau Sạch Đà Lạt Xanh")
        self.assertTrue(pub["journey"][0]["stops"])
        # tìm bằng mã phiếu giao cũng có chặng 1
        gh = self.ok("nongdan", "GET", "/api/public/lot?c=%s&k=%s" % (r["code"], server.qr_token(r["code"])))
        self.assertTrue(gh["journey"] and gh["journey"][0]["leg"] == 1)
        # xe giao xong
        self.assertEqual(self.c("thukho", "POST", "/api/trips/%d/status" % out["id"], {"status": "DA_GIAO"})[0], 200)
        self.assertEqual(self.c("thukho", "POST", "/api/trips/%d/status" % out["id"], {"status": "SU_CO"})[0], 409)
        self.assertTrue(any("VC-OUT" in a["action"] or out["code"] in a["action"] for a in self.ok("quanly", "GET", "/api/audit")))

    def test_20_cold_chain_mat_tin_hieu_va_diem_bat_thuong(self):
        os.environ["KHO_GPS_MIN_INTERVAL"] = "0"
        _, r, t = self._new_inbound(vehicle="77Z-000.01")
        tok, hi = t["token"], t["temp_max"] + 5
        self.assertEqual(self._loc(tok, t, 0.1, temp=(t["temp_min"] + t["temp_max"]) / 2)[0], 200)
        self.assertEqual(self._loc(tok, t, 0.2, temp=hi)[0], 200)  # 1 điểm lẻ: chỉ hiện đỏ, không cảnh báo
        cur = self._trip("thukho", t["id"])
        self.assertTrue(cur["temp_alert"])
        self.assertFalse(cur["temp_warning"])
        self.assertFalse(any("CẢNH BÁO NHIỆT ĐỘ" in a["action"] for a in self.ok("quanly", "GET", "/api/audit")))
        self.assertEqual(self._loc(tok, t, 0.3, temp=hi)[0], 200)
        self.assertEqual(self._loc(tok, t, 0.4, temp=hi)[0], 200)  # liên tiếp 3 điểm -> cảnh báo
        cur = self._trip("thukho", t["id"])
        self.assertTrue(cur["temp_warning"] and cur["temp_check"])
        self.assertTrue(any("CẢNH BÁO NHIỆT ĐỘ chuyến %s" % t["code"] in a["action"] for a in self.ok("quanly", "GET", "/api/audit")))
        self.assertIn(t["code"], [x["code"] for x in self.ok("thukho", "GET", "/api/dashboard")["temp_alerts"]])
        # nhãn "Cần kiểm tra nhiệt độ khi nhập": Thủ kho thấy chi tiết, QA chỉ thấy cờ (không có dữ liệu GPS)
        self.assertTrue(self._batch("thukho", r["id"])["trip"]["temp_check"])
        qa = self._batch("qa", r["id"])
        self.assertTrue(qa["cold_alert"])
        self.assertNotIn("trip", qa)
        self.assertEqual(self.c("qa", "GET", "/api/trips")[0], 403)
        self.assertNotIn("temp_alerts", self.ok("qa", "GET", "/api/dashboard"))
        # mất tín hiệu quá 10 phút: gắn nhãn, không bịa vị trí mới
        self.assertFalse(self._trip("quanly", t["id"])["signal_lost"])
        self._shift(t["id"], 11)
        lost = self._trip("quanly", t["id"])
        self.assertTrue(lost["signal_lost"])
        self.assertTrue(lost["last_signal_at"])
        # điểm GPS bất thường bị loại: nhảy xa, tốc độ phi lý, nhịp gửi quá nhanh, tọa độ sai
        self.assertEqual(call(self.base, "POST", "/api/driver/location", {"t": tok, "lat": t["o_lat"] + 2, "lon": t["o_lon"]})[0], 400)
        self.assertEqual(call(self.base, "POST", "/api/driver/location", {"t": tok, "lat": t["o_lat"], "lon": t["o_lon"], "speed": 200})[0], 400)
        self.assertEqual(call(self.base, "POST", "/api/driver/location", {"t": tok, "lat": "x", "lon": 1})[0], 400)
        self.assertEqual(call(self.base, "POST", "/api/driver/location", {"t": tok, "lat": 10, "lon": 106, "temp": 999})[0], 400)
        self.assertEqual(self._trip("quanly", t["id"])["n_points"], 4)
        os.environ["KHO_GPS_MIN_INTERVAL"] = "5"
        try:
            self.assertEqual(call(self.base, "POST", "/api/driver/location", {"t": tok, "lat": t["o_lat"], "lon": t["o_lon"]})[0], 200)
            self.assertEqual(call(self.base, "POST", "/api/driver/location", {"t": tok, "lat": t["o_lat"], "lon": t["o_lon"]})[0], 429)
        finally:
            os.environ["KHO_GPS_MIN_INTERVAL"] = "0"
        # chế độ demo tách khỏi GPS thật
        os.environ["KHO_DEMO"] = "1"
        self.assertEqual(self.c("thukho", "POST", "/api/trips/%d/simulate" % t["id"])[0], 409)
        # Mô phỏng giao hàng của Thủ kho không phụ thuộc KHO_DEMO, nhưng vẫn không trộn với GPS thật; trang tài xế vẫn cần KHO_DEMO
        os.environ["KHO_DEMO"] = "0"
        self.assertEqual(self.c("thukho", "POST", "/api/trips/%d/simulate" % t["id"])[0], 409)
        self.assertEqual(call(self.base, "POST", "/api/driver/simulate", {"t": tok})[0], 403)

    def test_20b_mo_phong_giao_hang_2_chang(self):
        os.environ["KHO_DEMO"] = "0"
        trips = self.ok("thukho", "GET", "/api/trips")
        for leg in (1, 2):
            t = [x for x in trips if x["leg"] == leg and x["status"] == "CHO_LAY_HANG" and not x["n_points"]]
            if not t:
                continue
            t = t[0]
            for _ in range(12):
                self.c("thukho", "POST", "/api/trips/%d/simulate" % t["id"])
            d = self.ok("thukho", "GET", "/api/trips/%d" % t["id"])
            self.assertEqual((d["status"], d["progress"], d["mode"]), ("DA_GIAO", 100, "SIM"))

    def test_20c_truy_xuat_theo_tung_chuyen_xuat(self):
        """1 lô gốc tách làm 2 đơn cho 2 cửa hàng: mỗi QR/chuyến chỉ trả về đúng cửa hàng của chuyến đó."""
        meta = self.ok("kinhdoanh", "GET", "/api/meta")
        cu = [x["name"] for x in meta["customers"]][:2]
        ids = []
        for cid in (meta["customers"][0]["id"], meta["customers"][1]["id"]):
            o = self.ok("kinhdoanh", "POST", "/api/orders", {"customer_id": cid, "items": [{"sku": "KLT", "qty_kg": 10}]})
            self.ok("thukho", "POST", "/api/orders/%d/reserve" % o["id"])
            self.ok("thukho", "POST", "/api/orders/%d/ship" % o["id"], {"plate": "51D-777.%02d" % (len(ids) + 10), "driver": "Tài xế thử"})
            ids.append(o["id"])
        lots = {}
        for oid in ids:
            for r in self.ok("kinhdoanh", "GET", "/api/orders"):
                if r["id"] == oid:
                    for rv in r["reservations"]:
                        lots.setdefault(rv["lot_code"], set()).add(oid)
        code = [k for k, v in lots.items() if len(v) == 2][0]  # lô dùng chung cho cả 2 đơn
        lot = [x for x in self.ok("kinhdoanh", "GET", "/api/lots?all=1&q=" + code) if x["code"] == code][0]
        full = self.ok("kinhdoanh", "GET", "/api/lots/%d/trace" % lot["id"])
        self.assertEqual(len([x for x in full["shipments"] if x["trip_code"]]), 2)
        self.assertEqual(full["journey"], [])  # chưa chọn chuyến thì không gộp timeline
        self.assertFalse([g for g in full["ledger"] if g["type"] == "XUAT"])
        seen = []
        for x in full["shipments"]:
            d = self.ok("kinhdoanh", "GET", "/api/lots/%d/trace?ship=%s" % (lot["id"], x["trip_code"]))
            self.assertEqual([g["leg"] for g in d["journey"]][-1], 2)
            self.assertEqual(len([g for g in d["journey"] if g["leg"] == 2]), 1)
            self.assertEqual(d["journey"][-1]["to"], x["customer"])
            self.assertEqual(len(d["ledger"]) - len(full["ledger"]), 1)
            other = [y for y in full["shipments"] if y["trip_code"] != x["trip_code"]][0]
            self.assertNotIn(other["customer"], json.dumps(d["journey"], ensure_ascii=False)) if other["customer"] != x["customer"] else None
            self.assertNotIn(other["trip_code"], json.dumps({k: v for k, v in d.items() if k != "shipments"}))
            qr = self.ok("kinhdoanh", "GET", "/api/lots/%d/qr?base=http://127.0.0.1:8000&ship=%s" % (lot["id"], x["trip_code"]))
            self.assertIn("s=" + x["trip_code"], qr["url"])
            pub = self.ok("kinhdoanh", "GET", "/api/public/lot?c=%s&k=%s&s=%s" % (code, qr["token"], x["trip_code"]))
            self.assertEqual([g["leg"] for g in pub["journey"]], [g["leg"] for g in d["journey"]])
            self.assertEqual(pub["shipment"], x["trip_code"])
            seen.append(qr["token"])
            # chuyến của lô khác / mã giả không dùng được
            self.assertEqual(self.c("kinhdoanh", "GET", "/api/lots/%d/trace?ship=VC-OUT-000000-99" % lot["id"])[0], 404)
            self.assertEqual(self.c("kinhdoanh", "GET", "/api/public/lot?c=%s&k=%s&s=%s" % (code, qr["token"], other["trip_code"]))[0], 404)  # QR chuyến này không mở được chuyến kia
        self.assertEqual(len(set(seen)), 2)  # mỗi chuyến một mã QR riêng
        # lô cũ (QR không có s) vẫn chạy bình thường, chỉ hiện đến bước nhập kho
        old = self.ok("kinhdoanh", "GET", "/api/public/lot?c=%s&k=%s" % (code, server.qr_token(code)))
        self.assertTrue(all(g["leg"] == 1 for g in old["journey"]))
        self.assertIsNone(old["shipment"])

    def test_20d_don_hang_nhan_hang_va_truy_xuat(self):
        """Tạo đơn -> giữ hàng -> xuất kho -> truy xuất lô và QR của đơn -> xác nhận đã nhận hàng."""
        meta = self.ok("kinhdoanh", "GET", "/api/meta")
        o = self.ok("kinhdoanh", "POST", "/api/orders", {"customer_id": meta["customers"][2]["id"], "items": [{"sku": "KLT", "qty_kg": 5}]})
        oid = o["id"]
        tr = self.ok("kinhdoanh", "GET", "/api/orders/%d/trace" % oid)
        self.assertEqual((tr["status"], tr["lots"]), ("MOI", []))  # chưa giữ hàng: chưa có lô
        self.assertEqual(self.c("kinhdoanh", "POST", "/api/orders/%d/receive" % oid)[0], 409)  # chưa xuất kho
        self.ok("thukho", "POST", "/api/orders/%d/reserve" % oid)
        self.assertEqual(self.c("kinhdoanh", "POST", "/api/orders/%d/receive" % oid)[0], 409)  # mới giữ hàng cũng chưa nhận được
        self.ok("thukho", "POST", "/api/orders/%d/ship" % oid, {"plate": "51D-888.01", "driver": "Tài xế thử"})
        self.assertEqual(self.c("thukho", "POST", "/api/orders/%d/receive" % oid)[0], 403)  # Thủ kho không có quyền quản lý đơn
        self.assertEqual(self.c("nongdan", "GET", "/api/orders/%d/trace" % oid)[0], 403)
        tr = self.ok("kinhdoanh", "GET", "/api/orders/%d/trace" % oid)
        self.assertEqual(tr["status"], "DA_XUAT_KHO")
        self.assertTrue(tr["lots"] and tr["trip_code"])
        l = tr["lots"][0]
        self.assertIn("order=%d" % oid, l["path"])
        q = l["path"].split("?")[1]
        pub = self.ok("kinhdoanh", "GET", "/api/public/lot?c=%s&%s" % (l["code"], q))
        self.assertEqual(len([g for g in pub["journey"] if g["leg"] == 2]), 1)
        self.assertEqual(pub["shipment"], tr["trip_code"])
        # k của đơn này không dùng được cho đơn khác hoặc cho lô gốc
        self.assertEqual(self.c("kinhdoanh", "GET", "/api/public/lot?c=%s&k=%s&order=%d" % (l["code"], q.split("k=")[1], oid + 1))[0], 404)
        self.assertEqual(self.c("kinhdoanh", "GET", "/api/public/lot?c=%s&k=%s&order=%d" % (l["code"], server.qr_token(l["code"]), oid))[0], 404)
        # đơn thứ hai dùng chung lô: QR của đơn 1 không lộ chuyến của đơn 2
        o2 = self.ok("kinhdoanh", "POST", "/api/orders", {"customer_id": meta["customers"][3]["id"], "items": [{"sku": "KLT", "qty_kg": 5}]})
        self.ok("thukho", "POST", "/api/orders/%d/reserve" % o2["id"])
        self.ok("thukho", "POST", "/api/orders/%d/ship" % o2["id"], {"plate": "51D-888.02", "driver": "Tài xế thử"})
        t2 = self.ok("kinhdoanh", "GET", "/api/orders/%d/trace" % o2["id"])["trip_code"]
        again = self.ok("kinhdoanh", "GET", "/api/public/lot?c=%s&%s" % (l["code"], q))
        self.assertEqual((again["shipment"], len([g for g in again["journey"] if g["leg"] == 2])), (tr["trip_code"], 1))
        self.assertNotIn(t2, json.dumps(again))
        # xác nhận đã nhận hàng
        self.ok("kinhdoanh", "POST", "/api/orders/%d/receive" % oid)
        got = [x for x in self.ok("kinhdoanh", "GET", "/api/orders") if x["id"] == oid][0]
        self.assertEqual(got["status"], "DA_NHAN_HANG")
        trip = [x for x in self.ok("quanly", "GET", "/api/trips") if x["code"] == tr["trip_code"]][0]
        self.assertEqual(trip["status"], "DA_GIAO")
        self.assertEqual(self.c("kinhdoanh", "POST", "/api/orders/%d/receive" % oid)[0], 409)  # không nhận hai lần
        self.assertEqual(self.c("kinhdoanh", "POST", "/api/orders/%d/cancel" % oid)[0], 409)
        self.assertTrue(any("đã nhận hàng cho đơn" in a["action"] for a in self.ok("quanly", "GET", "/api/audit")))
        # QR của đơn đã nhận vẫn mở được (truy xuất), đơn chưa nhận giữ nguyên trạng thái
        self.ok("kinhdoanh", "GET", "/api/public/lot?c=%s&%s" % (l["code"], q))
        self.assertEqual([x for x in self.ok("kinhdoanh", "GET", "/api/orders") if x["id"] == o2["id"]][0]["status"], "DA_XUAT_KHO")

    def test_20e_nang_cap_csdl_cu_them_trang_thai_nhan_hang(self):
        import sqlite3, tempfile
        d = tempfile.mkdtemp()
        c = sqlite3.connect(os.path.join(d, "old.db"))
        c.row_factory = sqlite3.Row
        c.executescript(server.SCHEMA.replace("'DA_XUAT_KHO','DA_NHAN_HANG','DA_HUY'", "'DA_XUAT_KHO','DA_HUY'"))
        c.execute("INSERT INTO customers(id,name) VALUES(1,'A')")
        c.execute("INSERT INTO users(id,username,full_name,role,salt,pass_hash) VALUES(1,'x','X','QUAN_LY','s','h')")
        c.execute("INSERT INTO orders(code,customer_id,status,created_by,created_at) VALUES('DH-1',1,'DA_XUAT_KHO',1,'2026-01-01')")
        c.commit()
        server.migrate_orders(c)
        server.migrate_orders(c)  # chạy lại vẫn an toàn
        c.execute("UPDATE orders SET status='DA_NHAN_HANG' WHERE code='DH-1'")
        self.assertEqual(c.execute("SELECT status FROM orders").fetchone()[0], "DA_NHAN_HANG")
        c.close()

    def test_21_phieu_bi_tu_choi_chang_1_su_co(self):
        pid = self._plan(500)
        self.ok("nongdan", "POST", "/api/plans/%d/update" % pid, {"action": "harvested"})
        s, r = self._gh("nongdan", pid, qty_kg=500, spray_date=server.add_days(self.today, -3), phi_days=7)
        t = [x for x in self.ok("quanly", "GET", "/api/trips?leg=in") if x["batch_id"] == r["id"]][0]
        self.assertEqual(t["status"], "CHO_LAY_HANG")
        self.ok("thukho", "POST", "/api/batches/%d/refuse" % r["id"], {"reason": "Chưa đủ thời gian cách ly"})
        t = self._trip("quanly", t["id"])
        self.assertEqual((t["status"], t["status_label"], t["incident_note"]), ("SU_CO", "Sự cố", "Chưa đủ thời gian cách ly"))
        self.assertEqual(t["eta"], None)

    def test_22_tai_xe_tra_cuu_bien_so_pin(self):
        _, r, t = self._new_inbound(vehicle="88A-777.77")
        plate, pin = "88a 777.77", t["pin"]  # biển số không phân biệt hoa thường, dấu cách
        self.assertEqual(call(self.base, "POST", "/api/driver/trips", {"plate": plate, "pin": "000000" if pin != "000000" else "111111"})[0], 401)
        s, d = call(self.base, "POST", "/api/driver/trips", {"plate": plate, "pin": pin})
        self.assertEqual(s, 200, d)
        self.assertEqual([x["code"] for x in d["trips"]], [t["code"]])
        v = d["trips"][0]
        self.assertTrue(v["token"])
        for k in ("supplier_name", "farm_name", "pin", "driver_phone", "order_code", "price", "amount"):
            self.assertNotIn(k, v)
        # chỉ trả các chuyến đang mở của biển số đó (tối đa 2 chặng)
        self.assertEqual(call(self.base, "POST", "/api/driver/status", {"plate": plate, "pin": pin, "trip": t["code"], "status": "DANG_VAN_CHUYEN"})[0], 200)
        self.assertEqual(call(self.base, "POST", "/api/driver/status", {"plate": plate, "pin": "12345", "trip": t["code"], "status": "DA_GIAO"})[0], 401)
        self.assertEqual(call(self.base, "POST", "/api/driver/status", {"plate": plate, "pin": pin, "trip": t["code"], "status": "DA_GIAO"})[0], 200)
        self.assertEqual(call(self.base, "POST", "/api/driver/trips", {"plate": plate, "pin": pin})[0], 401)  # chuyến đã đóng: không còn chuyến mở
        # sai PIN quá 5 lần/10 phút -> khóa tạm, kể cả khi sau đó nhập đúng
        _, r2, t2 = self._new_inbound(vehicle="88A-888.88")
        for _ in range(5):
            self.assertEqual(call(self.base, "POST", "/api/driver/trips", {"plate": "88A-888.88", "pin": "999999" if t2["pin"] != "999999" else "888888"})[0], 401)
        self.assertEqual(call(self.base, "POST", "/api/driver/trips", {"plate": "88A-888.88", "pin": t2["pin"]})[0], 429)
        # GET theo biển số + PIN không còn được hỗ trợ (PIN không được nằm trong đường dẫn)
        self.assertEqual(call(self.base, "GET", "/api/driver/trip?plate=88A-888.88&pin=%s" % t2["pin"])[0], 404)

    def test_23_migrate_chuyen_xe_cu_sang_2_chang(self):
        path = os.path.join(_tmp, "oldtrips.db")
        old = server.DB_PATH
        server.DB_PATH = path
        try:
            server.init_db()
            c = server.connect()
            keep = c.execute("SELECT order_id,token,plate,driver,o_lat,o_lon,d_lat,d_lon,d_name,temp_min,temp_max,created_at FROM trips WHERE leg_type='WAREHOUSE_TO_STORE' ORDER BY id").fetchall()
            c.executescript("""PRAGMA foreign_keys=OFF; DROP TABLE trip_points; DROP TABLE trips;
CREATE TABLE trips(id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, order_id INTEGER NOT NULL REFERENCES orders(id), token TEXT UNIQUE NOT NULL, plate TEXT NOT NULL, driver TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'CHUAN_BI' CHECK(status IN ('CHUAN_BI','DANG_GIAO','DA_GIAO','SU_CO')),
  o_lat REAL NOT NULL, o_lon REAL NOT NULL, d_lat REAL NOT NULL, d_lon REAL NOT NULL, d_name TEXT NOT NULL, temp_min REAL NOT NULL, temp_max REAL NOT NULL, created_at TEXT NOT NULL, ended_at TEXT);
CREATE TABLE trip_points(id INTEGER PRIMARY KEY, trip_id INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE, lat REAL NOT NULL, lon REAL NOT NULL, speed REAL, temp REAL, src TEXT NOT NULL DEFAULT 'GPS', created_at TEXT NOT NULL);
DELETE FROM batches;""")
            self.assertTrue(len(keep) >= 2)
            for i, k in enumerate(keep[:2]):
                c.execute("INSERT INTO trips(code,order_id,token,plate,driver,status,o_lat,o_lon,d_lat,d_lon,d_name,temp_min,temp_max,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          ("CX-%04d" % (i + 1), k[0], k[1], k[2], k[3], ("CHUAN_BI", "DANG_GIAO")[i], *k[4:]))
                c.execute("INSERT INTO trip_points(trip_id,lat,lon,speed,temp,src,created_at) VALUES(?,?,?,?,?,'MO_PHONG',?)", (i + 1, k[4], k[5], 30, 5, k[11]))
            c.close()
            for _ in range(2):  # chạy migration 2 lần vẫn an toàn
                server.init_db()
            c = server.connect()
            rows = c.execute("SELECT code,legacy_code,leg_type,status,origin_name,total_distance_km,token FROM trips WHERE legacy_code IS NOT NULL ORDER BY id").fetchall()
            self.assertEqual([(x["legacy_code"], x["leg_type"], x["status"]) for x in rows],
                             [("CX-0001", "WAREHOUSE_TO_STORE", "CHO_LAY_HANG"), ("CX-0002", "WAREHOUSE_TO_STORE", "DANG_VAN_CHUYEN")])
            self.assertTrue(all(x["code"].startswith("VC-OUT-") and x["origin_name"] and x["total_distance_km"] > 0 for x in rows))
            self.assertEqual([x["token"] for x in rows], [k[1] for k in keep[:2]])  # liên kết tài xế cũ vẫn dùng được
            self.assertEqual(c.execute("SELECT src FROM trip_points LIMIT 1").fetchone()[0], "SIM")
            self.assertEqual(c.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(c.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            tj = server.trip_json(c, c.execute("SELECT * FROM trips WHERE legacy_code='CX-0002'").fetchone())  # chuyến cũ vẫn mở được
            self.assertEqual(tj["status_label"], "Đang vận chuyển")
            c.close()
        finally:
            server.DB_PATH = old


    # ---------------------------------------------------------------- Thu mua trực tiếp (cross-docking) của Kinh doanh
    def _xd_offer(self, qty=400, price=60000, farmer="nongdan2", sku="XCL"):
        return self.ok(farmer, "POST", "/api/farm/offers", {"sku": sku, "qty_kg": qty, "price": price, "harvest_date": self.today, "standard": "Bất kỳ"})

    def _xd_batch(self, oid, qty, farmer="nongdan2", wh=2):
        r = self.ok(farmer, "POST", "/api/farm/batches", {"source": "o:%d" % oid, "qty_kg": qty, "harvest_date": self.today, "warehouse_id": wh, "phi_days": 0, "vehicle": "63C-555.55"})
        return next(b for b in self.ok("quanly", "GET", "/api/batches") if b["id"] == r["id"])

    def _xd_offer_of(self, who, oid):
        return next((o for o in self.ok(who, "GET", "/api/offers") if o["id"] == oid), None)

    def _stock(self, sku):
        c = server.connect()
        try:
            return server.available(c, sku), c.execute("SELECT COALESCE(SUM(qty_kg),0) FROM lots WHERE sku=?", (sku,)).fetchone()[0]
        finally:
            c.close()

    def test_24_kinh_doanh_mua_truc_tiep_qua_kho_giao_thang_cua_hang(self):
        o = self._xd_offer()
        oid = o["id"]
        # đồng bộ hai phía: Kinh doanh và Quản lý kho cùng thấy lô đang chào bán
        self.assertEqual(self._xd_offer_of("kinhdoanh", oid)["status"], "CHO_DUYET")
        self.assertEqual(self._xd_offer_of("quanly", oid)["status"], "CHO_DUYET")
        # phân quyền và kiểm tra đầu vào
        for who in ("nongdan2", "thukho", "qa", "quanly"):
            self.assertEqual(self.c(who, "POST", "/api/offers/%d/buy" % oid, {"customer_id": 3})[0], 403, who)
        self.assertEqual(self.c("kinhdoanh", "POST", "/api/offers/%d/buy" % oid, {})[0], 400)            # chưa chọn cửa hàng
        self.assertEqual(self.c("kinhdoanh", "POST", "/api/offers/%d/buy" % oid, {"customer_id": 99})[0], 400)
        self.assertEqual(self.c("kinhdoanh", "POST", "/api/offers/%d/buy" % oid, {"customer_id": 3, "price": 50000})[0], 409)  # không đề xuất giá khác
        self.assertEqual(self.c("kinhdoanh", "POST", "/api/offers/9999/buy", {"customer_id": 3})[0], 404)
        # hai bên chốt cùng lúc: chỉ một bên thắng
        res = []
        def go(who, path, body):
            res.append(self.c(who, "POST", path, body)[0])
        ts = [threading.Thread(target=go, args=("kinhdoanh", "/api/offers/%d/buy" % oid, {"customer_id": 3, "price": 60000})),
              threading.Thread(target=go, args=("quanly", "/api/offers/%d/decide" % oid, {"action": "approve"}))]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual(sorted(res), [200, 409], res)
        if self._xd_offer_of("quanly", oid)["buyer_type"] != "business":  # kho thắng cuộc đua: ghi nhận rồi kiểm tra Kinh doanh bị chặn, dùng chào bán khác cho phần còn lại
            self.assertEqual(self.c("kinhdoanh", "POST", "/api/offers/%d/buy" % oid, {"customer_id": 3})[0], 409)
            self.assertIsNone(self._xd_offer_of("kinhdoanh", oid))  # lô kho đã duyệt biến mất khỏi danh sách thu mua của Kinh doanh
            o = self._xd_offer(); oid = o["id"]
            self.ok("kinhdoanh", "POST", "/api/offers/%d/buy" % oid, {"customer_id": 3, "price": 60000})
        self.assertEqual(self.c("kinhdoanh", "POST", "/api/offers/%d/buy" % oid, {"customer_id": 3})[0], 409)  # không mua hai lần
        self.assertEqual(self.c("quanly", "POST", "/api/offers/%d/decide" % oid, {"action": "approve"})[0], 409)  # kho không duyệt lại
        ko, qo = self._xd_offer_of("kinhdoanh", oid), self._xd_offer_of("quanly", oid)
        self.assertEqual((ko["status"], ko["mine"], ko["agreed_price"], ko["dest_store"]["name"]), ("CHO_GIAO", True, 60000, "Nhà hàng Hoa Sen"))
        self.assertEqual((qo["buyer_type"], qo["dest_store"]["customer_id"]), ("business", 3))
        self.assertEqual(ko["flow"]["stage"]["code"], "CHO_NONG_DAN")
        fo = self._xd_offer_of("nongdan2", oid)
        self.assertEqual((fo["status"], fo["buyer_type"]), ("CHO_GIAO", "business"))
        for k in ("buyer_id", "dest_store_info", "dest_customer_id"):
            self.assertNotIn(k, fo)  # nông dân không biết cửa hàng của người mua
        # Nông dân lập phiếu giao, chặng 1 sinh ra và Kinh doanh theo dõi được (không có token, PIN)
        b = self._xd_batch(oid, 400)
        self.assertEqual((b["buyer_type"], b["dest_store"]["name"]), ("business", "Nhà hàng Hoa Sen"))
        fl = self._xd_offer_of("kinhdoanh", oid)["flow"]
        self.assertEqual((fl["stage"]["code"], fl["batches"][0]["trip_in"]["status"]), ("CHO_LAY", "CHO_LAY_HANG"))
        tin = next(t for t in self.ok("kinhdoanh", "GET", "/api/trips?leg=in") if t["code"] == fl["batches"][0]["trip_in"]["code"])
        for k in ("token", "pin", "driver_phone"):
            self.assertNotIn(k, tin)
        self.ok("thukho", "POST", "/api/trips/%d/status" % tin["id"], {"status": "DANG_VAN_CHUYEN"})
        self.assertEqual(self._xd_offer_of("kinhdoanh", oid)["flow"]["stage"]["code"], "VC_CHANG1")
        # Kinh doanh chỉ thấy phiếu giao của chính mình; nông dân khác không thấy
        self.assertEqual([x["code"] for x in self.ok("kinhdoanh", "GET", "/api/batches") if x["id"] == b["id"]], [b["code"]])
        # QC đạt: hàng KHÔNG thành tồn kho tự do, tự xuất thẳng chặng 2 về cửa hàng
        av0, tot0 = self._stock("XCL")
        r = self.ok("thukho", "POST", "/api/batches/%d/receive" % b["id"], {"qty_l1": 350, "qty_l2": 30, "qty_bad": 20, "residue": 0.05, "meets_standard": True})
        self.assertTrue(r["crossdock_order"] and not r["quarantined"])
        self.assertEqual(self._stock("XCL"), (av0, tot0))                      # tồn khả dụng và tồn thực không đổi
        od = next(x for x in self.ok("kinhdoanh", "GET", "/api/orders") if x["code"] == r["crossdock_order"])
        self.assertEqual((od["status"], od["customer"], od["crossdock"]["batch_code"]), ("DA_XUAT_KHO", "Nhà hàng Hoa Sen", b["code"]))
        self.assertEqual(round(sum(i["qty_kg"] for i in od["items"])), 380)
        c = server.connect()
        led = c.execute("SELECT type,SUM(qty_kg) q FROM stock_ledger WHERE ref LIKE ? OR ref LIKE ? GROUP BY type", ("Phiếu giao " + b["code"] + "%", "Xuất theo đơn " + od["code"] + "%")).fetchall()
        self.assertEqual({x["type"]: x["q"] for x in led}, {"NHAP": 380, "XUAT": -380})   # ghi sổ cái nhập rồi xuất, tồn lô về 0
        tout = c.execute("SELECT * FROM trips WHERE order_id=?", (od["id"],)).fetchone()
        self.assertEqual((tout["leg_type"], tout["d_name"], tout["status"]), ("WAREHOUSE_TO_STORE", "Nhà hàng Hoa Sen", "CHO_LAY_HANG"))
        self.assertTrue(tout["token"])                                          # tài xế nhận chuyến qua tai-xe.html như bình thường
        c.close()
        self.assertEqual(self._xd_offer_of("kinhdoanh", oid)["flow"]["stage"]["code"], "CHO_GIAO2")
        self.ok("thukho", "POST", "/api/trips/%d/status" % tout["id"], {"status": "DANG_VAN_CHUYEN"})
        self.assertEqual(self._xd_offer_of("kinhdoanh", oid)["flow"]["stage"]["code"], "VC_CHANG2")
        self.ok("thukho", "POST", "/api/trips/%d/status" % tout["id"], {"status": "DA_GIAO"})
        self.assertEqual(self._xd_offer_of("kinhdoanh", oid)["flow"]["stage"]["code"], "DA_DEN")
        self.ok("kinhdoanh", "POST", "/api/orders/%d/receive" % od["id"])
        self.assertEqual(self._xd_offer_of("kinhdoanh", oid)["flow"]["stage"]["code"], "DA_NHAN")
        # truy xuất công khai của lô vẫn có đủ hành trình
        tr = self.ok("kinhdoanh", "GET", "/api/orders/%d/trace" % od["id"])
        self.assertTrue(tr["lots"])
        # công nợ nông dân vẫn do kho thanh toán như luồng chuẩn
        self.assertEqual(self.ok("quanly", "POST", "/api/batches/%d/pay" % b["id"])["paid"], round(350 * 60000 + 30 * 60000 * server.L2_FACTOR))

    def test_25_lo_cach_ly_cho_qa_roi_moi_giao_thang(self):
        o = self._xd_offer(qty=200)
        self.ok("kinhdoanh", "POST", "/api/offers/%d/buy" % o["id"], {"customer_id": 1})
        b = self._xd_batch(o["id"], 150)
        av0, _ = self._stock("XCL")
        r = self.ok("thukho", "POST", "/api/batches/%d/receive" % b["id"], {"qty_l1": 100, "qty_l2": 50, "residue": 5, "meets_standard": True})
        self.assertTrue(r["quarantined"])
        self.assertIsNone(r["crossdock_order"])
        self.assertEqual(self._stock("XCL")[0], av0)                            # lô cách ly không bán được cho ai
        fl = self._xd_offer_of("kinhdoanh", o["id"])["flow"]
        self.assertEqual(fl["stage"]["code"], "CACH_LY")
        self.assertIn("cách ly", fl["batches"][0]["notice"])
        self.assertIn("cách ly", self._xd_offer_of("nongdan2", o["id"])["notice"])
        c = server.connect()
        ids = [x["id"] for x in c.execute("SELECT id FROM lots WHERE code IN (%s) ORDER BY id" % ",".join("?" * len(r["lots"])), r["lots"]).fetchall()]
        c.close()
        self.ok("qa", "POST", "/api/lots/%d/inspect" % ids[0], {"result": "DAT", "residue": 0.1, "grade": "Loại 1"})
        self.assertEqual(self._xd_offer_of("kinhdoanh", o["id"])["flow"]["stage"]["code"], "CACH_LY")  # còn lô thứ hai chưa kết luận
        self.ok("qa", "POST", "/api/lots/%d/inspect" % ids[1], {"result": "DAT", "residue": 0.1, "grade": "Loại 2"})
        fl = self._xd_offer_of("kinhdoanh", o["id"])["flow"]
        self.assertEqual(fl["stage"]["code"], "CHO_GIAO2")
        self.assertEqual(fl["batches"][0]["order"]["status"], "DA_XUAT_KHO")
        self.assertEqual(self._stock("XCL")[0], av0)                            # vẫn không rơi vào tồn kho tự do
        self.assertEqual(len([x for x in self.ok("quanly", "GET", "/api/orders") if x["crossdock"] and x["crossdock"]["batch_code"] == b["code"]]), 1)

    def test_26_qc_khong_dat_thong_bao_ca_hai_ben_va_khong_giao(self):
        o = self._xd_offer(qty=100)
        self.ok("kinhdoanh", "POST", "/api/offers/%d/buy" % o["id"], {"customer_id": 2})
        b = self._xd_batch(o["id"], 100)
        # từ chối nhập (không có hàng đạt)
        r = self.ok("thukho", "POST", "/api/batches/%d/receive" % b["id"], {"qty_l1": 0, "qty_l2": 0, "qty_bad": 100, "meets_standard": False, "note": "Hàng dập nát"})
        self.assertEqual(r["status"], "TU_CHOI")
        fl = self._xd_offer_of("kinhdoanh", o["id"])["flow"]
        self.assertEqual(fl["batches"][0]["stage"]["code"], "QC_KHONG_DAT")
        self.assertTrue(fl["batches"][0]["notice"])
        self.assertTrue(self._xd_offer_of("nongdan2", o["id"])["notice"])
        self.assertEqual(fl["stage"]["code"], "CHO_NONG_DAN")                   # nông dân giao lại được, hàng vẫn dành cho Kinh doanh
        # giao lại, lô cách ly rồi QA kết luận Không đạt: không có đơn giao đi
        b2 = self._xd_batch(o["id"], 100)
        r2 = self.ok("thukho", "POST", "/api/batches/%d/receive" % b2["id"], {"qty_l1": 100, "residue": 5, "meets_standard": True})
        c = server.connect()
        lid = c.execute("SELECT id FROM lots WHERE code=?", (r2["lots"][0],)).fetchone()["id"]
        c.close()
        self.ok("qa", "POST", "/api/lots/%d/inspect" % lid, {"result": "KHONG_DAT", "residue": 5})
        fl = self._xd_offer_of("kinhdoanh", o["id"])["flow"]
        live = [x for x in fl["batches"] if x["code"] == b2["code"]][0]
        self.assertEqual(live["stage"]["code"], "QC_KHONG_DAT")
        self.assertIsNone(live["order"])
        self.assertIn("không đạt QC", self._xd_offer_of("nongdan2", o["id"])["notice"])

    def test_27_luong_kho_tu_mua_van_nhap_ton_kho_va_dong_bo_huy_chao_ban(self):
        o1 = self._xd_offer(qty=300)
        self.ok("quanly", "POST", "/api/offers/%d/decide" % o1["id"], {"action": "approve"})
        self.assertIsNone(self._xd_offer_of("kinhdoanh", o1["id"]))             # kho đã chốt: Kinh doanh không còn thấy để mua
        self.assertEqual(self.c("kinhdoanh", "POST", "/api/offers/%d/buy" % o1["id"], {"customer_id": 1})[0], 409)
        self.assertEqual(self._xd_offer_of("quanly", o1["id"])["buyer_type"], "warehouse")
        b = self._xd_batch(o1["id"], 300)
        self.assertEqual(b["buyer_type"], "warehouse")
        av0, _ = self._stock("XCL")
        r = self.ok("thukho", "POST", "/api/batches/%d/receive" % b["id"], {"qty_l1": 280, "residue": 0.05, "meets_standard": True})
        self.assertIsNone(r["crossdock_order"])
        self.assertEqual(self._stock("XCL")[0], av0 + 280)                      # luồng truyền thống: nhập tồn kho tự do
        self.assertFalse([x for x in self.ok("quanly", "GET", "/api/orders") if x["crossdock"] and x["crossdock"]["batch_code"] == b["code"]])
        # nông dân hủy chào bán thì Kinh doanh không còn thấy; lô đã mua nhưng chưa có phiếu giao thì thấy trạng thái Đã hủy
        o2 = self._xd_offer(qty=120)
        self.assertEqual(self._xd_offer_of("kinhdoanh", o2["id"])["status"], "CHO_DUYET")
        self.ok("nongdan2", "POST", "/api/farm/offers/%d/cancel" % o2["id"])
        self.assertIsNone(self._xd_offer_of("kinhdoanh", o2["id"]))
        self.assertEqual(self._xd_offer_of("quanly", o2["id"])["status"], "HUY")
        self.assertEqual(self.c("kinhdoanh", "POST", "/api/offers/%d/buy" % o2["id"], {"customer_id": 1})[0], 409)
        o3 = self._xd_offer(qty=90)
        self.ok("kinhdoanh", "POST", "/api/offers/%d/buy" % o3["id"], {"customer_id": 1})
        self.ok("nongdan2", "POST", "/api/farm/offers/%d/cancel" % o3["id"])
        self.assertEqual(self._xd_offer_of("kinhdoanh", o3["id"])["flow"]["stage"]["code"], "HUY")
        # Kinh doanh không thấy lô của người khác mua
        self.assertNotIn(o1["id"], [x["id"] for x in self.ok("kinhdoanh", "GET", "/api/offers")])

    def test_28_migrate_csdl_cu_them_cot_thu_mua_truc_tiep(self):
        path = os.path.join(_tmp, "oldxd.db")
        old = server.DB_PATH
        server.DB_PATH = path
        try:
            server.init_db()
            c = server.connect()
            c.executescript("""PRAGMA foreign_keys=OFF;
ALTER TABLE offers DROP COLUMN buyer_type; ALTER TABLE offers DROP COLUMN buyer_id; ALTER TABLE offers DROP COLUMN dest_customer_id;
ALTER TABLE offers DROP COLUMN dest_store_info; ALTER TABLE offers DROP COLUMN bought_at;
ALTER TABLE batches DROP COLUMN xdock_order_id; ALTER TABLE batches DROP COLUMN xdock_note;""")
            c.close()
            for _ in range(2):
                server.init_db()
            c = server.connect()
            self.assertEqual({r[0] for r in c.execute("SELECT buyer_type FROM offers")}, {"warehouse"})  # chào bán cũ mặc định do kho mua
            self.assertEqual(c.execute("PRAGMA foreign_key_check").fetchall(), [])
            c.close()
        finally:
            server.DB_PATH = old


if __name__ == "__main__":
    unittest.main(verbosity=2)
