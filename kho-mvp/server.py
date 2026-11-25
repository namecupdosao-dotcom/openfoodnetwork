#!/usr/bin/env python3
"""Hệ thống Quản lý Kho & Chuỗi cung ứng nông sản - MVP (Backend + SQLite).

Chạy:  python server.py            (mở http://127.0.0.1:8000)
Chỉ dùng thư viện chuẩn của Python 3.9+, không cần cài thêm gói nào.
"""
import datetime
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

import qr as qrlib

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("KHO_DB", os.path.join(BASE, "kho.db"))
STATIC = os.path.join(BASE, "static")

SCHEMA = """
CREATE TABLE users(
  id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, full_name TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('QUAN_LY','THU_KHO','QA','KINH_DOANH','NONG_DAN')),
  salt TEXT NOT NULL, pass_hash TEXT NOT NULL, supplier_id INTEGER REFERENCES suppliers(id));
CREATE TABLE sessions(
  token TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), created_at TEXT NOT NULL);
CREATE TABLE products(
  sku TEXT PRIMARY KEY, name TEXT NOT NULL, unit TEXT NOT NULL DEFAULT 'kg',
  shelf_days INTEGER NOT NULL CHECK(shelf_days>0), temp_min REAL NOT NULL, temp_max REAL NOT NULL,
  price INTEGER NOT NULL CHECK(price>=0), min_stock REAL NOT NULL DEFAULT 0, mrl REAL NOT NULL);
CREATE TABLE warehouses(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, city TEXT NOT NULL,
  capacity_kg REAL NOT NULL CHECK(capacity_kg>0), temp_min REAL NOT NULL, temp_max REAL NOT NULL);
CREATE TABLE suppliers(id INTEGER PRIMARY KEY, name TEXT NOT NULL, area TEXT NOT NULL, cert TEXT NOT NULL);
CREATE TABLE customers(id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE lots(
  id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, sku TEXT NOT NULL REFERENCES products(sku),
  supplier_id INTEGER NOT NULL REFERENCES suppliers(id), warehouse_id INTEGER NOT NULL REFERENCES warehouses(id),
  harvest_date TEXT NOT NULL, expiry_date TEXT NOT NULL, spray_date TEXT, phi_days INTEGER NOT NULL DEFAULT 0,
  qty_kg REAL NOT NULL CHECK(qty_kg>=0), reserved_kg REAL NOT NULL DEFAULT 0 CHECK(reserved_kg>=0),
  qa_status TEXT NOT NULL DEFAULT 'CHO_KIEM_DINH' CHECK(qa_status IN ('CHO_KIEM_DINH','DAT','CACH_LY','KHONG_DAT')),
  grade TEXT, residue REAL, created_at TEXT NOT NULL,
  CHECK(reserved_kg<=qty_kg));
CREATE TABLE receipts(
  id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, sku TEXT NOT NULL REFERENCES products(sku),
  supplier_id INTEGER NOT NULL REFERENCES suppliers(id), warehouse_id INTEGER NOT NULL REFERENCES warehouses(id),
  qty_kg REAL NOT NULL CHECK(qty_kg>0), harvest_date TEXT NOT NULL, spray_date TEXT, phi_days INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'CHO_DUYET' CHECK(status IN ('CHO_DUYET','DA_DUYET','TU_CHOI')),
  lot_id INTEGER REFERENCES lots(id), created_by INTEGER NOT NULL REFERENCES users(id),
  decided_by INTEGER REFERENCES users(id), created_at TEXT NOT NULL, plan_id INTEGER);
CREATE TABLE orders(
  id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, customer_id INTEGER NOT NULL REFERENCES customers(id),
  status TEXT NOT NULL DEFAULT 'MOI' CHECK(status IN ('MOI','DA_GIU_HANG','DA_XUAT_KHO','DA_NHAN_HANG','DA_HUY')),
  created_by INTEGER NOT NULL REFERENCES users(id), created_at TEXT NOT NULL);
CREATE TABLE order_items(
  id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
  sku TEXT NOT NULL REFERENCES products(sku), qty_kg REAL NOT NULL CHECK(qty_kg>0));
CREATE TABLE reservations(
  id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
  lot_id INTEGER NOT NULL REFERENCES lots(id), qty_kg REAL NOT NULL CHECK(qty_kg>0));
CREATE TABLE stock_ledger(
  id INTEGER PRIMARY KEY, lot_id INTEGER NOT NULL REFERENCES lots(id),
  type TEXT NOT NULL CHECK(type IN ('NHAP','XUAT','DIEU_CHINH')), qty_kg REAL NOT NULL,
  ref TEXT NOT NULL, user_id INTEGER REFERENCES users(id), created_at TEXT NOT NULL);
CREATE TABLE audit_log(
  id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users(id), action TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX idx_lots_sku_exp ON lots(sku, expiry_date);
CREATE INDEX idx_ledger_lot ON stock_ledger(lot_id);
"""

TRIP_DDL = """CREATE TABLE %s(
  id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL,
  leg_type TEXT NOT NULL DEFAULT 'WAREHOUSE_TO_STORE' CHECK(leg_type IN ('FARM_TO_WAREHOUSE','WAREHOUSE_TO_STORE')),
  batch_id INTEGER REFERENCES batches(id), order_id INTEGER REFERENCES orders(id), supplier_id INTEGER REFERENCES suppliers(id),
  token TEXT UNIQUE NOT NULL, pin_hash TEXT, plate TEXT NOT NULL DEFAULT '', driver TEXT NOT NULL DEFAULT '', driver_phone TEXT,
  status TEXT NOT NULL DEFAULT 'CHO_LAY_HANG' CHECK(status IN ('CHO_LAY_HANG','DANG_VAN_CHUYEN','DA_GIAO','SU_CO','HUY')),
  origin_name TEXT, origin_address TEXT, o_lat REAL NOT NULL, o_lon REAL NOT NULL,
  d_name TEXT NOT NULL, dest_address TEXT, d_lat REAL NOT NULL, d_lon REAL NOT NULL,
  total_distance_km REAL NOT NULL DEFAULT 0, temp_min REAL NOT NULL, temp_max REAL NOT NULL,
  incident_note TEXT, temp_warned_at TEXT, legacy_code TEXT,
  created_at TEXT NOT NULL, started_at TEXT, ended_at TEXT,
  CHECK((leg_type='FARM_TO_WAREHOUSE' AND batch_id IS NOT NULL AND order_id IS NULL)
     OR (leg_type='WAREHOUSE_TO_STORE' AND order_id IS NOT NULL AND batch_id IS NULL)));"""
TRIP_SCHEMA = TRIP_DDL % "IF NOT EXISTS trips" + """
CREATE TABLE IF NOT EXISTS trip_points(
  id INTEGER PRIMARY KEY, trip_id INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
  lat REAL NOT NULL, lon REAL NOT NULL, speed REAL, temp REAL, src TEXT NOT NULL DEFAULT 'GPS', created_at TEXT NOT NULL,
  location_name TEXT, distance_km REAL);
CREATE INDEX IF NOT EXISTS idx_points_trip ON trip_points(trip_id,id);
CREATE TABLE IF NOT EXISTS route_templates(
  id INTEGER PRIMARY KEY, from_region TEXT NOT NULL, to_region TEXT NOT NULL, seq INTEGER NOT NULL,
  name TEXT NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL);
"""
PLANS_DDL = """CREATE TABLE %s(
  id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
  sku TEXT NOT NULL REFERENCES products(sku), area_ha REAL NOT NULL CHECK(area_ha>0), expected_kg REAL NOT NULL CHECK(expected_kg>0),
  expected_date TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'DU_KIEN' CHECK(status IN ('DU_KIEN','XAC_NHAN','DA_THU_HOACH','DANG_GIAO','DA_NHAP','HUY')),
  postponed INTEGER NOT NULL DEFAULT 0, note TEXT, created_by INTEGER REFERENCES users(id), created_at TEXT NOT NULL,
  lot_id INTEGER REFERENCES lots(id), actual_kg REAL, postpone_reason TEXT);"""
PLAN_COLS = "id,code,supplier_id,sku,area_ha,expected_kg,expected_date,status,postponed,note,created_by,created_at,lot_id,actual_kg,postpone_reason"
EXTRA_SCHEMA = """
CREATE TABLE IF NOT EXISTS recalls(
  id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, lot_id INTEGER NOT NULL REFERENCES lots(id), reason TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'DANG_XU_LY' CHECK(status IN ('DANG_XU_LY','DA_DONG')),
  resolution TEXT CHECK(resolution IN ('HUY','TRA_NCC')), qty_at_recall REAL NOT NULL,
  created_by INTEGER REFERENCES users(id), created_at TEXT NOT NULL, closed_at TEXT);
CREATE TABLE IF NOT EXISTS recall_items(
  id INTEGER PRIMARY KEY, recall_id INTEGER NOT NULL REFERENCES recalls(id) ON DELETE CASCADE,
  order_id INTEGER NOT NULL REFERENCES orders(id), customer_id INTEGER NOT NULL REFERENCES customers(id), qty_kg REAL NOT NULL,
  phase TEXT NOT NULL CHECK(phase IN ('DA_XUAT','DA_NHA_GIU')), notified_at TEXT);
""" + PLANS_DDL % "IF NOT EXISTS plans" + """
CREATE TABLE IF NOT EXISTS market_prices(
  id INTEGER PRIMARY KEY, sku TEXT NOT NULL REFERENCES products(sku),
  kind TEXT NOT NULL CHECK(kind IN ('THI_TRUONG','THU_MUA')), market TEXT NOT NULL,
  price INTEGER NOT NULL CHECK(price>0), price_date TEXT NOT NULL, created_by INTEGER REFERENCES users(id), created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_prices ON market_prices(sku, price_date);
CREATE TABLE IF NOT EXISTS requirements(
  id INTEGER PRIMARY KEY, who TEXT NOT NULL, name TEXT NOT NULL, pri TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('DA_LAM','MOT_PHAN','HUONG')), module TEXT, note TEXT);
"""
MARKETS = ["Chợ đầu mối Bình Điền", "Chợ đầu mối Thủ Đức"]
REQ_FILE = os.path.join(BASE, "data", "requirements.json")
WH_COORD = {1: (10.9722, 106.4936), 2: (10.3419, 106.0432), 3: (11.9404, 108.4583)}
CUST_COORD = {1: (10.7296, 106.7217), 2: (10.7275, 106.7050), 3: (10.7769, 106.6954), 4: (10.8231, 106.6297)}
# Điểm đi của chặng 1 (nông trại) và thông tin cửa hàng của chặng 2: nạp vào CSDL khi còn trống (chỉ để demo)
FARM_SEED = {1: ("Nông trại Rau Sạch Đà Lạt Xanh", "Xã Xuân Thọ, TP. Đà Lạt, Lâm Đồng", 11.9760, 108.4930),
             2: ("HTX Trái Cây Cái Bè", "Xã Hòa Khánh, huyện Cái Bè, Tiền Giang", 10.3300, 106.0000),
             3: ("HTX Nông nghiệp Củ Chi", "Xã Tân Thạnh Đông, huyện Củ Chi, TP.HCM", 11.0130, 106.4220),
             4: ("Trang trại Đơn Dương", "Thị trấn Thạnh Mỹ, huyện Đơn Dương, Lâm Đồng", 11.7840, 108.5460)}
CUST_ADDR = {1: "Đường Nguyễn Thị Thập, Quận 7, TP.HCM", 2: "Khu Phú Mỹ Hưng, Quận 7, TP.HCM", 3: "Trung tâm Quận 1, TP.HCM",
             4: "Quận Tân Bình, TP.HCM"}
# Vùng (để chọn tuyến mẫu theo cặp vùng) và các trạm trung gian. Không có API bản đồ nên đây là tuyến gần đúng phục vụ demo.
REGIONS = {"DALAT": (11.9404, 108.4583), "CAIBE": (10.3419, 106.0432), "CUCHI": (10.9722, 106.4936), "HCM": (10.7769, 106.6954)}
REGION_RADIUS_KM = 60
_QL20 = [("Đức Trọng", 11.7389, 108.3706), ("Di Linh", 11.5760, 108.0830), ("Bảo Lộc", 11.5480, 107.8070), ("Madagui", 11.4600, 107.5900),
         ("Tân Phú", 11.2670, 107.4330), ("Định Quán", 11.1950, 107.3500), ("Dầu Giây", 10.9400, 107.1700), ("Biên Hòa", 10.9570, 106.8430)]
_TO_CAIBE = [("Bến Lức", 10.6370, 106.4890), ("Tân An", 10.5320, 106.4140), ("Cai Lậy", 10.4050, 106.1180)]
ROUTE_SEED = {
    ("DALAT", "CUCHI"): _QL20 + [("Hóc Môn", 10.8830, 106.5920)],
    ("DALAT", "HCM"): _QL20 + [("Thủ Đức", 10.8500, 106.7600)],
    ("DALAT", "CAIBE"): _QL20 + [("Bình Chánh", 10.6900, 106.5700)] + _TO_CAIBE,
    ("CUCHI", "HCM"): [("Hóc Môn", 10.8830, 106.5920)],
    ("CUCHI", "CAIBE"): [("Hóc Môn", 10.8830, 106.5920), ("Bình Chánh", 10.6900, 106.5700)] + _TO_CAIBE,
    ("CAIBE", "HCM"): [("Cai Lậy", 10.4050, 106.1180), ("Tân An", 10.5320, 106.4140), ("Bến Lức", 10.6370, 106.4890), ("Bình Chánh", 10.6900, 106.5700)],
}
PLATES = ["51D-123.45", "51C-778.90", "60C-456.12", "72C-310.88"]
DRIVERS = ["Nguyễn Văn Hải", "Trần Quốc Bảo", "Lê Minh Tuấn", "Phạm Văn Long"]

# Sức chứa tối đa (kg): Củ Chi 5.000 tấn, Cái Bè 8.000 tấn, Đà Lạt 3.500 tấn
WH_CAPACITY = {1: 5000000, 2: 8000000, 3: 3500000}
WARN_PCT = 85

ROLE_PERMS = {
    # Kế hoạch thu hoạch: chỉ Nông dân được thêm/sửa/dời/hủy/xóa (quyền "kehoach"); Quản lý kho và Thủ kho xác nhận kế hoạch.
    # Hàng từ kế hoạch về kho qua phiếu giao (GH) do nông dân lập; Thủ kho tiếp nhận và lập phiếu nhập, Quản lý kho duyệt.
    # Giá thị trường: chỉ Kinh doanh được thêm/sửa/xóa (quyền "gia"); các vai trò còn lại chỉ xem.
    # "baocao", "nhatky", "doichieu": nhóm Quản trị hệ thống (báo cáo tổng hợp, nhật ký hoạt động, đối chiếu yêu cầu) chỉ dành cho Quản lý kho.
    "QUAN_LY": {"nhap", "duyet", "xuat", "giuhang", "kiemdinh", "donhang", "xacnhan", "baocao", "nhatky", "doichieu"},
    "THU_KHO": {"nhap", "xuat", "giuhang", "xacnhan"},
    "QA": {"kiemdinh"},
    "KINH_DOANH": {"donhang", "gia", "rfq", "thumua"},
    "NONG_DAN": {"nongdan", "kehoach"},
}
ROLE_NAMES = {"QUAN_LY": "Quản lý kho", "THU_KHO": "Thủ kho", "QA": "Nhân viên QA/QC", "KINH_DOANH": "Kinh doanh", "NONG_DAN": "Nông dân"}
PERM_NAMES = {"nhap": "lập phiếu nhập kho", "duyet": "phê duyệt phiếu nhập", "xuat": "xuất kho", "giuhang": "giữ hàng (FEFO)",
              "kiemdinh": "kiểm định, thu hồi lô", "donhang": "quản lý đơn hàng",
              "kehoach": "lập, sửa, dời ngày, hủy kế hoạch thu hoạch (dành cho nông dân)", "xacnhan": "xác nhận kế hoạch thu hoạch",
              "gia": "thêm, sửa, xóa giá thị trường (dành cho Kinh doanh)",
              "nongdan": "dùng chức năng dành cho nông dân", "thumua": "mua trực tiếp lô chào bán của nông dân (giao thẳng về cửa hàng)", "rfq": "đăng yêu cầu thu mua và duyệt kèo",
              "baocao": "xem báo cáo nhập - xuất - tồn tổng hợp", "nhatky": "xem nhật ký hoạt động hệ thống",
              "doichieu": "xem đối chiếu yêu cầu hệ thống"}


class ApiError(Exception):
    def __init__(self, status, msg):
        super().__init__(msg)
        self.status, self.msg = status, msg


def today():
    return os.environ.get("KHO_TODAY") or datetime.date.today().isoformat()


def add_days(d, n):
    return (datetime.date.fromisoformat(d) + datetime.timedelta(days=n)).isoformat()


def now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def hash_pw(pw, salt):
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 120_000).hex()


def connect():
    c = sqlite3.connect(DB_PATH, timeout=15, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("PRAGMA journal_mode=WAL")
    return c


def audit(c, uid, action):
    c.execute("INSERT INTO audit_log(user_id,action,created_at) VALUES(?,?,?)", (uid, action, now()))


def ledger(c, lot_id, typ, qty, ref, uid):
    c.execute("INSERT INTO stock_ledger(lot_id,type,qty_kg,ref,user_id,created_at) VALUES(?,?,?,?,?,?)",
              (lot_id, typ, qty, ref, uid, now()))


# ---------------------------------------------------------------- dữ liệu mẫu
def init_db():
    if os.path.exists(DB_PATH):
        c = connect()
        migrate(c)
        c.close()
        return
    c = connect()
    c.executescript(SCHEMA)
    for u, n, r in [("quanly", "Nguyễn Thị Lan", "QUAN_LY"), ("thukho", "Trần Văn Minh", "THU_KHO"),
                    ("qa", "Lê Thu Hà", "QA"), ("kinhdoanh", "Phạm Quốc Bảo", "KINH_DOANH")]:
        salt = secrets.token_hex(16)
        c.execute("INSERT INTO users(username,full_name,role,salt,pass_hash) VALUES(?,?,?,?,?)",
                  (u, n, r, salt, hash_pw("123456", salt)))
    prods = [("SLX", "Súp lơ xanh Đà Lạt", 7, 2, 6, 38000, 200, 0.5), ("XCL", "Xoài cát Hòa Lộc", 10, 10, 13, 65000, 500, 0.5),
             ("NRT", "Nấm rơm tươi", 4, 10, 14, 72000, 100, 0.2), ("CRT", "Cà rốt Đà Lạt", 21, 2, 6, 22000, 300, 0.3),
             ("KLT", "Khoai lang tím", 30, 12, 16, 28000, 200, 0.1), ("CCB", "Cà chua bi", 9, 8, 12, 48000, 100, 0.5)]
    c.executemany("INSERT INTO products(sku,name,shelf_days,temp_min,temp_max,price,min_stock,mrl) VALUES(?,?,?,?,?,?,?,?)", prods)
    c.executemany("INSERT INTO warehouses(id,name,city,capacity_kg,temp_min,temp_max) VALUES(?,?,?,?,?,?)",
                  [(1, "Kho lạnh Củ Chi", "TP.HCM", WH_CAPACITY[1], 2, 8), (2, "Kho mát Cái Bè", "Tiền Giang", WH_CAPACITY[2], 10, 15),
                   (3, "Kho Đà Lạt", "Lâm Đồng", WH_CAPACITY[3], 2, 6)])
    c.executemany("INSERT INTO suppliers(id,name,area,cert) VALUES(?,?,?,?)",
                  [(1, "Nông trại Rau Sạch Đà Lạt Xanh", "Đà Lạt, Lâm Đồng", "VietGAP"), (2, "HTX Trái Cây Cái Bè", "Cái Bè, Tiền Giang", "GlobalGAP"),
                   (3, "HTX Nông nghiệp Củ Chi", "Củ Chi, TP.HCM", "VietGAP"), (4, "Trang trại Đơn Dương", "Đơn Dương, Lâm Đồng", "Hữu cơ")])
    c.executemany("INSERT INTO customers(id,name) VALUES(?,?)",
                  [(1, "Siêu thị Co.op Smile Q7"), (2, "Cửa hàng Rau Củ Quả Phú Mỹ Hưng"), (3, "Nhà hàng Hoa Sen"), (4, "Đại lý Nông sản Sạch Online")])
    t = today()
    # (sku, supplier, kho, ngày thu hoạch lùi, kg, qa, hạng, dư lượng, ngày phun lùi, phi)
    seed = [("SLX", 1, 3, 2, 400, "DAT", "Loại 1", 0.12, 15, 7), ("SLX", 4, 3, 5, 120, "DAT", "Loại 1", 0.08, 18, 7),
            ("XCL", 2, 2, 3, 1000, "DAT", "Loại 1", 0.05, 20, 10), ("XCL", 2, 2, 9, 300, "DAT", "Loại 2", 0.07, 25, 10),
            ("NRT", 3, 2, 1, 100, "DAT", "Loại 1", 0.01, 0, 0), ("CRT", 1, 1, 13, 700, "DAT", "Loại 1", 0.10, 30, 7),
            ("KLT", 3, 2, 11, 500, "DAT", "Loại 1", 0.02, 33, 7), ("CCB", 1, 2, 1, 100, "DAT", "Loại 1", 0.15, 12, 7),
            ("CRT", 1, 1, 1, 300, "CACH_LY", None, 0.41, 3, 7), ("SLX", 1, 3, 0, 200, "CHO_KIEM_DINH", None, None, 13, 7)]
    n = {}
    for sku, sup, wh, back, kg, qa, grade, res, sp, phi in seed:
        h = add_days(t, -back)
        n[sku] = n.get(sku, 0) + 1
        code = "%s-%s-%02d" % (sku, h.replace("-", "")[2:], n[sku])
        cur = c.execute("INSERT INTO lots(code,sku,supplier_id,warehouse_id,harvest_date,expiry_date,spray_date,phi_days,qty_kg,qa_status,grade,residue,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (code, sku, sup, wh, h, add_days(h, [p[2] for p in prods if p[0] == sku][0]), add_days(t, -sp) if sp else None, phi, kg, qa, grade, res, now()))
        ledger(c, cur.lastrowid, "NHAP", kg, "Phiếu nhập khởi tạo", 2)
    # vài đơn hàng đã xuất để báo cáo có số liệu
    for code, cust, sku, kg, lot in [("DH-0001", 1, "XCL", 200, 3), ("DH-0002", 2, "SLX", 100, 1)]:
        o = c.execute("INSERT INTO orders(code,customer_id,status,created_by,created_at) VALUES(?,?,?,?,?)", (code, cust, "DA_XUAT_KHO", 4, now())).lastrowid
        c.execute("INSERT INTO order_items(order_id,sku,qty_kg) VALUES(?,?,?)", (o, sku, kg))
        c.execute("INSERT INTO reservations(order_id,lot_id,qty_kg) VALUES(?,?,?)", (o, lot, kg))
        c.execute("UPDATE lots SET qty_kg=qty_kg-? WHERE id=?", (kg, lot))
        ledger(c, lot, "XUAT", -kg, "Xuất theo đơn " + code, 2)
    audit(c, None, "Khởi tạo cơ sở dữ liệu và nạp dữ liệu mẫu")
    migrate(c)
    c.close()


# ---------------------------------------------------------------- vận chuyển / GPS
import math
import random


def haversine(a, b, c2, d):
    r = 6371.0
    p1, p2 = math.radians(a), math.radians(c2)
    dp, dl = p2 - p1, math.radians(d - b)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


ROAD_FACTOR = 1.25          # quãng đường bộ = chim bay x 1,25
NEAR_PCT = 85               # tiến độ từ 85% trở lên: hiển thị "Sắp đến nơi" (suy ra, không lưu)
LOST_MIN = 10               # quá 10 phút không có điểm GPS mới: "Mất tín hiệu GPS"
TEMP_STREAK, TEMP_MINUTES = 3, 15   # nhiệt độ vượt ngưỡng liên tiếp 3 điểm (hoặc 15 phút) thì sinh cảnh báo
GPS_MIN_GAP = 5             # giây tối thiểu giữa 2 điểm GPS của cùng một chuyến (đổi bằng biến môi trường KHO_GPS_MIN_INTERVAL)
MAX_SPEED, MAX_JUMP_KMH = 120, 150  # tốc độ hợp lý tối đa và ngưỡng loại điểm nhảy bất thường
PIN_FAILS, PIN_WINDOW = 5, 600      # sai PIN quá 5 lần trong 10 phút thì khóa tạm biển số đó


def region_of(lat, lon):
    best = None
    for name, (la, lo) in REGIONS.items():
        d = haversine(lat, lon, la, lo)
        if d <= REGION_RADIUS_KM and (best is None or d < best[0]):
            best = (d, name)
    return best[1] if best else None


def plate_key(p):
    return re.sub(r"[^0-9A-Z]", "", str(p or "").upper())


def mask_plate(p):
    """51D-123.45 -> 51D-***.45 (trang công khai không để lộ biển số đầy đủ)."""
    p = str(p or "").strip()
    if not p:
        return ""
    return (p.split("-")[0] + "-***" + p[-3:]) if "-" in p and len(p) > 6 else "***" + p[-2:]


def clean_phone(v):
    return re.sub(r"[^0-9+ ]", "", str(v or "")).strip()[:15]


def driver_pin(plate, token=""):
    """Mã PIN 6 số của tài xế, suy ra từ biển số (cùng một xe thì cùng PIN cho cả 2 chặng). Chỉ lưu bản băm trong bảng trips."""
    k = plate_key(plate) or str(token)
    return "%06d" % (int(hmac.new(QR_SECRET.encode(), ("pin:" + k).encode(), hashlib.sha256).hexdigest()[:12], 16) % 1_000_000)


def pin_hash(pin, token):
    return hashlib.sha256(("%s:%s" % (pin, token)).encode()).hexdigest()


def parse_ts(s):
    try:
        return datetime.datetime.strptime(str(s)[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def hhmm(s):
    return str(s or "")[11:16]


def route_path(c, t):
    """Tuyến dự kiến: điểm đi -> các trạm của tuyến mẫu (nếu có theo cặp vùng) -> điểm đến. Không có tuyến mẫu thì nội suy đường thẳng."""
    a, b = (t["o_lat"], t["o_lon"]), (t["d_lat"], t["d_lon"])
    pts = [{"name": t.get("origin_name") or "Điểm đi", "lat": a[0], "lon": a[1], "kind": "origin"}]
    ra, rb = region_of(*a), region_of(*b)
    mids = []
    if ra and rb and ra != rb:
        q = c.execute("SELECT name,lat,lon FROM route_templates WHERE from_region=? AND to_region=? ORDER BY seq", (ra, rb)).fetchall()
        if not q:
            q = c.execute("SELECT name,lat,lon FROM route_templates WHERE from_region=? AND to_region=? ORDER BY seq DESC", (rb, ra)).fetchall()
        mids = [{"name": x["name"], "lat": x["lat"], "lon": x["lon"], "kind": "stop"} for x in q]
    if not mids:  # nội suy đường thẳng: 3 mốc trung gian không tên (không dùng để gọi tên trạm)
        mids = [{"name": None, "lat": a[0] + (b[0] - a[0]) * f, "lon": a[1] + (b[1] - a[1]) * f, "kind": "mid"} for f in (0.25, 0.5, 0.75)]
    pts += mids + [{"name": t["d_name"], "lat": b[0], "lon": b[1], "kind": "dest"}]
    cum = 0.0
    for i, p in enumerate(pts):
        if i:
            cum += haversine(pts[i - 1]["lat"], pts[i - 1]["lon"], p["lat"], p["lon"])
        p["cum"] = cum
    return pts


def path_road_km(path):
    return round(path[-1]["cum"] * ROAD_FACTOR, 1)


def project_on_path(path, lat, lon):
    """Quãng đường (đường chim bay cộng dồn) từ điểm đi tới hình chiếu của (lat, lon) lên tuyến."""
    best = None
    for i in range(len(path) - 1):
        a, b = path[i], path[i + 1]
        kx, ky = 111.320 * math.cos(math.radians(a["lat"])), 110.574
        bx, by = (b["lon"] - a["lon"]) * kx, (b["lat"] - a["lat"]) * ky
        px, py = (lon - a["lon"]) * kx, (lat - a["lat"]) * ky
        l2 = bx * bx + by * by
        u = 0.0 if l2 == 0 else max(0.0, min(1.0, (px * bx + py * by) / l2))
        dx, dy = px - u * bx, py - u * by
        d2 = dx * dx + dy * dy
        if best is None or d2 < best[0]:
            best = (d2, a["cum"] + u * (b["cum"] - a["cum"]))
    return best[1] if best else 0.0


def point_at(path, dist):
    """Tọa độ tại quãng đường dist (km chim bay cộng dồn) dọc tuyến."""
    dist = max(0.0, min(dist, path[-1]["cum"]))
    for i in range(len(path) - 1):
        a, b = path[i], path[i + 1]
        if dist <= b["cum"] or i == len(path) - 2:
            span = b["cum"] - a["cum"]
            f = 0.0 if span <= 0 else (dist - a["cum"]) / span
            return a["lat"] + (b["lat"] - a["lat"]) * f, a["lon"] + (b["lon"] - a["lon"]) * f
    return path[-1]["lat"], path[-1]["lon"]


def locate(path, lat, lon):
    """Tên trạm gần nhất (<= 12 km với trạm, <= 3 km với điểm đi/đến). None nếu đang ở giữa đường."""
    best = None
    for p in path:
        if not p["name"]:
            continue
        d = haversine(lat, lon, p["lat"], p["lon"])
        lim = 3.0 if p["kind"] in ("origin", "dest") else 12.0
        if d <= lim and (best is None or d < best[0]):
            best = (d, p["name"], p["kind"])
    return best[1] if best else None


def sim_temp(t):
    mid = (t["temp_min"] + t["temp_max"]) / 2
    return round(mid + random.uniform(-0.4, 0.4) * (t["temp_max"] - t["temp_min"]), 1)


def temp_run(pts, tmin, tmax):
    """Chuỗi điểm liên tiếp CUỐI CÙNG có nhiệt độ vượt ngưỡng: (số điểm, thời điểm bắt đầu, số phút kéo dài)."""
    n, since, first, last = 0, None, None, None
    for p in reversed(pts):
        if p["temp"] is None or tmin <= p["temp"] <= tmax:
            break
        n += 1
        first = parse_ts(p["created_at"])
        since = p["created_at"]
        if last is None:
            last = first
    mins = (last - first).total_seconds() / 60 if n and first and last else 0
    return n, since, mins


def is_temp_warning(n, mins):
    return n >= TEMP_STREAK or (n >= 2 and mins >= TEMP_MINUTES)


def add_point(c, t, lat, lon, speed, src, temp=None, uid=None):
    """Lưu một điểm GPS, tính tên trạm và quãng đã đi. Sinh cảnh báo nhiệt độ khi vượt ngưỡng kéo dài."""
    td = dict(t)
    path = route_path(c, td)
    total = path[-1]["cum"] or 1
    trav = project_on_path(path, lat, lon)
    c.execute("INSERT INTO trip_points(trip_id,lat,lon,speed,temp,src,created_at,location_name,distance_km) VALUES(?,?,?,?,?,?,?,?,?)",
              (t["id"], lat, lon, speed, sim_temp(td) if temp is None else temp, src, now(), locate(path, lat, lon),
               round(min(trav, total) * ROAD_FACTOR, 1)))
    if not td.get("temp_warned_at"):
        recent = rows(c, "SELECT temp,created_at FROM trip_points WHERE trip_id=? ORDER BY id DESC LIMIT 30", (t["id"],))[::-1]
        n, since, mins = temp_run(recent, td["temp_min"], td["temp_max"])
        if is_temp_warning(n, mins):
            c.execute("UPDATE trips SET temp_warned_at=? WHERE id=?", (now(), t["id"]))
            audit(c, uid, "CẢNH BÁO NHIỆT ĐỘ chuyến %s: thùng xe vượt ngưỡng %g–%g°C liên tiếp %d điểm" % (td["code"], td["temp_min"], td["temp_max"], n))


def next_trip_code(c, leg_type, day):
    prefix = "VC-IN-" if leg_type == "FARM_TO_WAREHOUSE" else "VC-OUT-"
    base = prefix + day.replace("-", "")[2:]
    n = c.execute("SELECT COUNT(*) FROM trips WHERE code LIKE ?", (base + "-%",)).fetchone()[0] + 1
    while c.execute("SELECT 1 FROM trips WHERE code=?", ("%s-%02d" % (base, n),)).fetchone():
        n += 1
    return "%s-%02d" % (base, n)


def insert_trip(c, leg_type, ref, ends, vehicle, temps, created=None, uid=None, supplier_id=None):
    """Ghi chuyến mới. ends = (tên đi, địa chỉ đi, lat, lon, tên đến, địa chỉ đến, lat, lon). Phải gọi trong giao dịch của nghiệp vụ gọi nó."""
    created = created or now()
    token = secrets.token_urlsafe(9)
    plate = str(vehicle.get("plate") or "").strip()[:20]
    pin = driver_pin(plate, token)
    t = {"o_lat": ends[2], "o_lon": ends[3], "d_lat": ends[6], "d_lon": ends[7], "origin_name": ends[0], "d_name": ends[4]}
    total = path_road_km(route_path(c, t))
    code = next_trip_code(c, leg_type, created[:10])
    cur = c.execute("""INSERT INTO trips(code,leg_type,batch_id,order_id,supplier_id,token,pin_hash,plate,driver,driver_phone,origin_name,origin_address,
        o_lat,o_lon,d_name,dest_address,d_lat,d_lon,total_distance_km,temp_min,temp_max,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (code, leg_type, ref if leg_type == "FARM_TO_WAREHOUSE" else None, ref if leg_type == "WAREHOUSE_TO_STORE" else None, supplier_id,
                     token, pin_hash(pin, token), plate, str(vehicle.get("driver") or "").strip()[:40], clean_phone(vehicle.get("phone")) or None,
                     ends[0], ends[1], ends[2], ends[3], ends[4], ends[5], ends[6], ends[7], total, temps[0], temps[1], created))
    audit(c, uid, "Tạo chuyến %s (%s)" % (code, "Chặng 1 nông trại - kho" if leg_type == "FARM_TO_WAREHOUSE" else "Chặng 2 kho - cửa hàng"))
    return cur.lastrowid, code


def wh_ends(c, wh_id):
    w = c.execute("SELECT name,city FROM warehouses WHERE id=?", (wh_id,)).fetchone()
    co = WH_COORD.get(wh_id, (10.9722, 106.4936))
    return (w["name"] if w else "Kho", (w["city"] if w else ""), co[0], co[1])


def create_trip_in(c, bid, uid=None):
    """Chặng 1 (VC-IN): sinh cùng giao dịch với phiếu giao hàng. Mỗi phiếu tối đa một chuyến."""
    ex = c.execute("SELECT id FROM trips WHERE batch_id=?", (bid,)).fetchone()
    if ex:
        return ex["id"]
    bt = c.execute("""SELECT b.*,p.temp_min,p.temp_max,s.name AS sname,s.area,s.farm_name,s.farm_address,s.farm_lat,s.farm_lon
      FROM batches b JOIN products p ON p.sku=b.sku JOIN suppliers s ON s.id=b.supplier_id WHERE b.id=?""", (bid,)).fetchone()
    w = wh_ends(c, bt["warehouse_id"])
    if bt["farm_lat"] is not None and bt["farm_lon"] is not None:
        o = (bt["farm_name"] or bt["sname"], bt["farm_address"] or bt["area"], bt["farm_lat"], bt["farm_lon"])
    else:  # nông trại chưa khai tọa độ: lấy vùng của nhà cung cấp, đặt cách kho nhận một đoạn để chuyến vẫn có quãng đường
        o = (bt["sname"], bt["area"], w[2] + 0.25, w[3] + 0.25)
    tid, _ = insert_trip(c, "FARM_TO_WAREHOUSE", bid, o + w[:2] + w[2:], {"plate": bt["vehicle"], "driver": bt["driver_name"], "phone": bt["driver_phone"]},
                         (bt["temp_min"], bt["temp_max"]), uid=uid, supplier_id=bt["supplier_id"])
    return tid


def create_trip_out(c, order, wh_id, plate=None, driver=None, phone=None, uid=None, created=None):
    """Chặng 2 (VC-OUT): sinh khi xuất kho đơn hàng. Mỗi đơn một chuyến."""
    ex = c.execute("SELECT id FROM trips WHERE order_id=?", (order["id"],)).fetchone()
    if ex:
        return ex["id"]
    cu = c.execute("SELECT * FROM customers WHERE id=?", (order["customer_id"],)).fetchone()
    co = (cu["lat"], cu["lon"]) if cu["lat"] is not None else CUST_COORD.get(order["customer_id"], (10.7769, 106.7009))
    p = c.execute("SELECT MIN(p.temp_min) AS lo,MAX(p.temp_max) AS hi FROM order_items i JOIN products p ON p.sku=i.sku WHERE i.order_id=?", (order["id"],)).fetchone()
    n = c.execute("SELECT COUNT(*) FROM trips").fetchone()[0] + 1
    if not plate:
        plate, driver = PLATES[n % 4], driver or DRIVERS[n % 4]
    w = wh_ends(c, wh_id)
    tid, _ = insert_trip(c, "WAREHOUSE_TO_STORE", order["id"], w + (cu["name"], cu["address"] or "", co[0], co[1]), {"plate": plate, "driver": driver, "phone": phone},
                         (p["lo"] if p and p["lo"] is not None else 2, p["hi"] if p and p["hi"] is not None else 8), uid=uid, created=created)
    return tid


def seed_transport_data(c):
    """Nạp tọa độ nông trại, địa chỉ cửa hàng và tuyến mẫu nếu còn trống (chạy lại nhiều lần vẫn an toàn)."""
    for sid, (nm, addr, la, lo) in FARM_SEED.items():
        c.execute("UPDATE suppliers SET farm_name=COALESCE(farm_name,?),farm_address=COALESCE(farm_address,?),farm_lat=COALESCE(farm_lat,?),farm_lon=COALESCE(farm_lon,?) WHERE id=?",
                  (nm, addr, la, lo, sid))
    for cid, (la, lo) in CUST_COORD.items():
        c.execute("UPDATE customers SET address=COALESCE(address,?),lat=COALESCE(lat,?),lon=COALESCE(lon,?) WHERE id=?", (CUST_ADDR.get(cid), la, lo, cid))
    if not c.execute("SELECT 1 FROM route_templates LIMIT 1").fetchone():
        for (a, b), stops in ROUTE_SEED.items():
            for i, (nm, la, lo) in enumerate(stops):
                c.execute("INSERT INTO route_templates(from_region,to_region,seq,name,lat,lon) VALUES(?,?,?,?,?,?)", (a, b, i, nm, la, lo))


def backfill_points(c, t, status):
    """Dữ liệu minh họa cho chuyến đã kết thúc từ trước (điểm mô phỏng chạy dọc tuyến)."""
    t = dict(t)
    path = route_path(c, t)
    for i in range(11):
        la, lo = point_at(path, path[-1]["cum"] * i / 10)
        add_point(c, t, la, lo, 38 if 0 < i < 10 else 0, "SIM")
    c.execute("UPDATE trips SET status=?,ended_at=?,started_at=COALESCE(started_at,created_at) WHERE id=?", (status, now(), t["id"]))


def migrate_transport(c):
    """Nâng cấp CSDL cũ sang vận chuyển 2 chặng. Chạy lại nhiều lần vẫn an toàn, không mất dữ liệu cũ."""
    add_column(c, "suppliers", "farm_name", "farm_name TEXT")
    add_column(c, "suppliers", "farm_address", "farm_address TEXT")
    add_column(c, "suppliers", "farm_lat", "farm_lat REAL")
    add_column(c, "suppliers", "farm_lon", "farm_lon REAL")
    add_column(c, "customers", "address", "address TEXT")
    add_column(c, "customers", "lat", "lat REAL")
    add_column(c, "customers", "lon", "lon REAL")
    add_column(c, "batches", "driver_name", "driver_name TEXT")
    add_column(c, "batches", "driver_phone", "driver_phone TEXT")
    add_column(c, "trip_points", "location_name", "location_name TEXT")
    add_column(c, "trip_points", "distance_km", "distance_km REAL")
    cols = [r["name"] for r in c.execute("PRAGMA table_info(trips)").fetchall()]
    if "leg_type" not in cols:  # CSDL cũ: SQLite không sửa được CHECK và NOT NULL, dựng lại bảng trips (giữ nguyên dữ liệu và khóa liên kết)
        c.execute("PRAGMA foreign_keys=OFF")
        c.executescript(TRIP_DDL % "trips_new" + """
INSERT INTO trips_new(id,code,leg_type,order_id,token,plate,driver,status,o_lat,o_lon,d_lat,d_lon,d_name,temp_min,temp_max,created_at,ended_at,legacy_code)
SELECT id,code,'WAREHOUSE_TO_STORE',order_id,token,plate,driver,
  CASE status WHEN 'CHUAN_BI' THEN 'CHO_LAY_HANG' WHEN 'DANG_GIAO' THEN 'DANG_VAN_CHUYEN' ELSE status END,
  o_lat,o_lon,d_lat,d_lon,d_name,temp_min,temp_max,created_at,ended_at,code FROM trips;
DROP TABLE trips; ALTER TABLE trips_new RENAME TO trips;""")
        c.execute("PRAGMA foreign_keys=ON")
    c.execute("UPDATE trip_points SET src='SIM' WHERE src='MO_PHONG'")
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_trips_batch ON trips(batch_id) WHERE batch_id IS NOT NULL")
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_trips_order ON trips(order_id) WHERE order_id IS NOT NULL")
    c.execute("CREATE INDEX IF NOT EXISTS idx_trips_supplier ON trips(supplier_id)")
    seed_transport_data(c)
    # chuyến cũ (đều gắn đơn hàng): điền điểm đi/đến, quãng đường, PIN, và đổi mã sang VC-OUT (mã cũ nằm trong legacy_code)
    for t in c.execute("SELECT * FROM trips WHERE leg_type='WAREHOUSE_TO_STORE' AND (origin_name IS NULL OR pin_hash IS NULL OR total_distance_km=0 OR code NOT LIKE 'VC-%') ORDER BY id").fetchall():
        o = c.execute("SELECT * FROM orders WHERE id=?", (t["order_id"],)).fetchone()
        wh = c.execute("SELECT l.warehouse_id FROM reservations r JOIN lots l ON l.id=r.lot_id WHERE r.order_id=? LIMIT 1", (t["order_id"],)).fetchone()
        w = wh_ends(c, wh["warehouse_id"] if wh else 1)
        cu = c.execute("SELECT address FROM customers WHERE id=?", (o["customer_id"],)).fetchone() if o else None
        d = dict(t)
        d["origin_name"] = t["origin_name"] or w[0]
        total = path_road_km(route_path(c, d))
        code = t["code"] if t["code"].startswith("VC-") else next_trip_code(c, "WAREHOUSE_TO_STORE", t["created_at"][:10])
        c.execute("UPDATE trips SET code=?,origin_name=?,origin_address=COALESCE(origin_address,?),dest_address=COALESCE(dest_address,?),total_distance_km=?,pin_hash=COALESCE(pin_hash,?) WHERE id=?",
                  (code, d["origin_name"], w[1], cu["address"] if cu else None, total, pin_hash(driver_pin(t["plate"], t["token"]), t["token"]), t["id"]))
    # đơn đã xuất kho mà chưa có chuyến: tạo chuyến đã giao để có dữ liệu minh họa
    for o in c.execute("SELECT * FROM orders WHERE status IN ('DA_XUAT_KHO','DA_NHAN_HANG') AND id NOT IN (SELECT order_id FROM trips WHERE order_id IS NOT NULL)").fetchall():
        w = c.execute("SELECT l.warehouse_id FROM reservations r JOIN lots l ON l.id=r.lot_id WHERE r.order_id=? LIMIT 1", (o["id"],)).fetchone()
        tid = create_trip_out(c, o, w["warehouse_id"] if w else 1)
        backfill_points(c, c.execute("SELECT * FROM trips WHERE id=?", (tid,)).fetchone(), "DA_GIAO")
    # phiếu giao hàng chưa có chặng 1: chờ giao -> Chờ lấy hàng; đã nhập -> Đã giao; không nhập -> Sự cố
    c.execute("UPDATE batches SET driver_name='Nguyễn Văn Sáu',driver_phone='0901 234 567' WHERE vehicle='63C-112.34' AND driver_name IS NULL")
    for b in c.execute("SELECT id,status,qc_note FROM batches WHERE id NOT IN (SELECT batch_id FROM trips WHERE batch_id IS NOT NULL) ORDER BY id").fetchall():
        tid = create_trip_in(c, b["id"])
        t = c.execute("SELECT * FROM trips WHERE id=?", (tid,)).fetchone()
        if b["status"] == "DA_NHAP":
            backfill_points(c, t, "DA_GIAO")
        elif b["status"] == "TU_CHOI":
            c.execute("UPDATE trips SET status='SU_CO',incident_note=? WHERE id=?", (b["qc_note"] or "Phiếu giao không được nhập kho", tid))
    c.execute("UPDATE trips SET supplier_id=(SELECT supplier_id FROM batches WHERE batches.id=trips.batch_id) WHERE supplier_id IS NULL AND batch_id IS NOT NULL")


def seed_extra(c):
    """Bảng mới của bản gộp: tạo nếu chưa có, nạp dữ liệu mẫu nếu còn trống."""
    c.executescript(EXTRA_SCHEMA)
    t = today()
    if not c.execute("SELECT 1 FROM market_prices LIMIT 1").fetchone():
        rnd = random.Random(15)  # cố định để lần nào nạp cũng ra cùng một bộ số
        for p in c.execute("SELECT sku,price FROM products").fetchall():
            for mk, bias in zip(MARKETS, (0.97, 1.03)):
                v = p["price"] * bias
                for back in range(14, -1, -1):
                    v = max(p["price"] * 0.7, v * (1 + rnd.uniform(-0.03, 0.032)))
                    c.execute("INSERT INTO market_prices(sku,kind,market,price,price_date,created_at) VALUES(?,?,?,?,?,?)",
                              (p["sku"], "THI_TRUONG", mk, int(round(v, -2)), add_days(t, -back), now()))
            for back in (14, 7, 0):
                c.execute("INSERT INTO market_prices(sku,kind,market,price,price_date,created_at) VALUES(?,?,?,?,?,?)",
                          (p["sku"], "THU_MUA", "Giá thu mua của công ty", int(round(p["price"] * (0.66 + rnd.uniform(0, 0.04)), -2)), add_days(t, -back), now()))
    if not c.execute("SELECT 1 FROM plans LIMIT 1").fetchone():
        for i, (sup, sku, ha, kg, d, st, note) in enumerate([
                (1, "SLX", 1.5, 1800, 3, "XAC_NHAN", "Đợt thu hoạch đầu mùa, thời tiết thuận lợi"),
                (2, "XCL", 4.0, 3500, 6, "DU_KIEN", "Chờ đo độ chín trước khi chốt ngày"),
                (3, "NRT", 0.2, 400, 1, "XAC_NHAN", "Nhà lưới Củ Chi"),
                (4, "CRT", 2.0, 2600, 9, "DU_KIEN", "Phụ thuộc lượng mưa tuần tới"),
                (3, "KLT", 3.0, 3000, 12, "DU_KIEN", "")], 1):
            c.execute("INSERT INTO plans(code,supplier_id,sku,area_ha,expected_kg,expected_date,status,note,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                      ("KH-%04d" % i, sup, sku, ha, kg, add_days(t, d), st, note, 2, now()))
    try:  # bảng đối chiếu 289 yêu cầu là dữ liệu tham chiếu, luôn đồng bộ theo file
        with open(REQ_FILE, encoding="utf-8") as f:
            reqs = json.load(f)
        c.executemany("INSERT OR REPLACE INTO requirements(id,who,name,pri,status,module,note) VALUES(:id,:who,:name,:pri,:status,:module,:note)", reqs)
    except (OSError, ValueError) as e:
        sys.stderr.write("Không nạp được requirements.json: %r\n" % (e,))


def add_column(c, table, col, ddl):
    if col not in [r["name"] for r in c.execute("PRAGMA table_info(%s)" % table).fetchall()]:
        c.execute("ALTER TABLE %s ADD COLUMN %s" % (table, ddl))


ORDERS_DDL = """CREATE TABLE orders_new(
  id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, customer_id INTEGER NOT NULL REFERENCES customers(id),
  status TEXT NOT NULL DEFAULT 'MOI' CHECK(status IN ('MOI','DA_GIU_HANG','DA_XUAT_KHO','DA_NHAN_HANG','DA_HUY')),
  created_by INTEGER NOT NULL REFERENCES users(id), created_at TEXT NOT NULL);
"""


def migrate_orders(c):
    """CSDL cũ: SQLite không sửa được CHECK, dựng lại bảng orders (giữ nguyên dữ liệu và mã) để thêm trạng thái Đã nhận hàng."""
    if "DA_NHAN_HANG" in c.execute("SELECT sql FROM sqlite_master WHERE name='orders'").fetchone()[0]:
        return
    c.execute("PRAGMA foreign_keys=OFF")
    c.executescript(ORDERS_DDL + "INSERT INTO orders_new(id,code,customer_id,status,created_by,created_at) SELECT id,code,customer_id,status,created_by,created_at FROM orders;"
                    " DROP TABLE orders; ALTER TABLE orders_new RENAME TO orders;")
    c.execute("PRAGMA foreign_keys=ON")


def migrate(c):
    migrate_orders(c)
    c.executescript(TRIP_SCHEMA)
    for wid, cap in WH_CAPACITY.items():  # đồng bộ sức chứa mới cho CSDL cũ
        c.execute("UPDATE warehouses SET capacity_kg=? WHERE id=?", (cap, wid))
    seed_extra(c)
    add_column(c, "receipts", "plan_id", "plan_id INTEGER REFERENCES plans(id)")  # phiếu nhập sinh ra từ kế hoạch nào
    add_column(c, "plans", "lot_id", "lot_id INTEGER REFERENCES lots(id)")         # lô thực tế đã nhập (sau khi QC đạt)
    add_column(c, "plans", "actual_kg", "actual_kg REAL")
    add_column(c, "plans", "postpone_reason", "postpone_reason TEXT")                # lý do dời ngày (bắt buộc khi kế hoạch đã xác nhận)
    if "DANG_GIAO" not in c.execute("SELECT sql FROM sqlite_master WHERE name='plans'").fetchone()[0]:
        # CSDL cũ: SQLite không sửa được CHECK, dựng lại bảng plans (giữ nguyên dữ liệu) để thêm trạng thái Đã thu hoạch, Đang giao hàng
        c.execute("PRAGMA foreign_keys=OFF")
        c.executescript(PLANS_DDL % "plans_new" + "INSERT INTO plans_new(%s) SELECT %s FROM plans; DROP TABLE plans; ALTER TABLE plans_new RENAME TO plans;" % (PLAN_COLS, PLAN_COLS))
        c.execute("PRAGMA foreign_keys=ON")
    add_column(c, "receipts", "batch_id", "batch_id INTEGER REFERENCES batches(id)")  # phiếu nhập sinh ra từ phiếu giao hàng nào
    ensure_chain(c)
    add_column(c, "batches", "plan_id", "plan_id INTEGER REFERENCES plans(id)")       # nguồn Kế hoạch thu hoạch (KH-...)
    add_column(c, "batches", "receipt_id", "receipt_id INTEGER REFERENCES receipts(id)")  # phiếu nhập kho đang/đã xử lý
    add_column(c, "batches", "recv_kg", "recv_kg REAL")                               # khối lượng thực nhận (nguồn Kế hoạch)
    # Luồng thu mua trực tiếp (cross-docking): Kinh doanh mua lô chào bán của nông dân, hàng qua kho kiểm định rồi giao thẳng về cửa hàng
    add_column(c, "offers", "buyer_type", "buyer_type TEXT NOT NULL DEFAULT 'warehouse'")  # 'warehouse' | 'business'
    add_column(c, "offers", "buyer_id", "buyer_id INTEGER REFERENCES users(id)")            # tài khoản Kinh doanh đã chốt mua
    add_column(c, "offers", "dest_customer_id", "dest_customer_id INTEGER REFERENCES customers(id)")
    add_column(c, "offers", "dest_store_info", "dest_store_info TEXT")                      # JSON {customer_id,name,address} chốt tại lúc mua
    add_column(c, "offers", "bought_at", "bought_at TEXT")
    add_column(c, "batches", "xdock_order_id", "xdock_order_id INTEGER REFERENCES orders(id)")  # đơn xuất thẳng chặng 2 sinh tự động khi đạt QC
    add_column(c, "batches", "xdock_note", "xdock_note TEXT")                                # thông báo QC không đạt / cách ly cho Nông dân và Kinh doanh
    migrate_transport(c)


LEG_VN = {"FARM_TO_WAREHOUSE": "Chặng 1: Nông trại → Kho", "WAREHOUSE_TO_STORE": "Chặng 2: Kho → Cửa hàng"}
TRIP_VN = {"CHO_LAY_HANG": "Chờ lấy hàng", "DANG_VAN_CHUYEN": "Đang vận chuyển", "DA_GIAO": "Đã giao hàng", "SU_CO": "Sự cố", "HUY": "Đã hủy"}
OPEN_TRIP = ("CHO_LAY_HANG", "DANG_VAN_CHUYEN")


def lot_origin(c, lot):
    """Truy ngược một lô: LO -> Phiếu nhập PN -> Phiếu giao GH -> Nông dân và Nông trại. Lô nhập thủ công thì ghi rõ nhập kho trực tiếp."""
    rc = c.execute("SELECT code,batch_id FROM receipts WHERE lot_id=? AND status='DA_DUYET'", (lot["id"],)).fetchone()
    bt = c.execute("SELECT * FROM batches WHERE id=?", (rc["batch_id"],)).fetchone() if rc and rc["batch_id"] else None
    if not bt:  # luồng chào bán/kèo: kho cân đo và tạo lô trực tiếp từ phiếu giao, mã lô nằm trong lot_codes
        bt = c.execute("SELECT * FROM batches WHERE (',' || COALESCE(lot_codes,'') || ',') LIKE ?", ("%," + lot["code"] + ",%",)).fetchone()
    sup = c.execute("SELECT * FROM suppliers WHERE id=?", (bt["supplier_id"] if bt else lot["supplier_id"],)).fetchone()
    farm = (sup["farm_name"] or sup["name"]) if sup else ""
    out = {"receipt_code": rc["code"] if rc else None, "supplier": sup["name"] if sup else "", "farm_name": farm,
           "farm_area": sup["area"] if sup else "", "batch_id": bt["id"] if bt else None, "batch_code": bt["code"] if bt else None, "direct": bt is None}
    u = c.execute("SELECT full_name FROM users WHERE role='NONG_DAN' AND supplier_id=? ORDER BY id LIMIT 1", (sup["id"],)).fetchone() if sup else None
    out["farmer"] = u["full_name"] if u else (sup["name"] if sup else "")
    out["text"] = ("Nhập kho trực tiếp, nhà cung cấp: " + out["supplier"]) if bt is None else None
    return out


def trip_cargo(c, t):
    if t["leg_type"] == "FARM_TO_WAREHOUSE":
        return rows(c, "SELECT b.code AS batch_code,p.name AS product,b.qty_kg FROM batches b JOIN products p ON p.sku=b.sku WHERE b.id=?", (t["batch_id"],))
    out = []
    for r in c.execute("""SELECT l.id,l.code,l.supplier_id,p.name AS product,SUM(r.qty_kg) AS qty_kg FROM reservations r JOIN lots l ON l.id=r.lot_id
      JOIN products p ON p.sku=l.sku WHERE r.order_id=? GROUP BY l.id ORDER BY l.id""", (t["order_id"],)).fetchall():
        x = {"lot_code": r["code"], "product": r["product"], "qty_kg": r["qty_kg"]}
        x["origin"] = lot_origin(c, {"id": r["id"], "code": r["code"], "supplier_id": r["supplier_id"]})
        out.append(x)
    return out


def trip_timeline(c, t, pts, path, hide_dest=False):
    """Các mốc [HH:MM] kèm tên trạm theo thứ tự thời gian: Xuất phát -> Qua trạm ... -> Đến ..."""
    dest = "Điểm bán lẻ" if hide_dest else t["d_name"]
    out, seen = [], set()
    for i, p in enumerate(pts):
        nm = p.get("location_name") or locate(path, p["lat"], p["lon"])
        if i == 0:
            first = ("Xuất phát tại " + t["origin_name"]) if nm == t["origin_name"] else ("Bắt đầu theo dõi" + ((" tại " + nm) if nm else ""))
            out.append({"time": hhmm(p["created_at"]), "text": first, "lat": p["lat"], "lon": p["lon"]})
            if nm:
                seen.add(nm)
            continue
        if nm and nm not in seen and nm != t["d_name"]:
            seen.add(nm)
            out.append({"time": hhmm(p["created_at"]), "text": "Qua trạm " + nm, "lat": p["lat"], "lon": p["lon"]})
    if t["status"] == "DA_GIAO" and t.get("ended_at"):
        out.append({"time": hhmm(t["ended_at"]), "text": "Đến " + dest, "lat": t["d_lat"], "lon": t["d_lon"]})
    return out


def trip_json(c, t, points=False, secret=False):
    """Chuyến xe kèm số liệu SUY RA từ điểm GPS mới nhất (tiến độ, ETA, nhiệt độ, mất tín hiệu). Không lưu trùng vào bảng trips."""
    t = dict(t)
    pts = rows(c, "SELECT lat,lon,speed,temp,src,location_name,distance_km,created_at FROM trip_points WHERE trip_id=? ORDER BY id", (t["id"],))
    last = pts[-1] if pts else None
    path = route_path(c, t)
    total = t["total_distance_km"] or path_road_km(path)
    trav = min(total, project_on_path(path, last["lat"], last["lon"]) * ROAD_FACTOR) if last else 0.0
    done = t["status"] == "DA_GIAO"
    t["distance_traveled_km"] = round(total if done else trav, 1)
    t["total_distance_km"] = t["total_km"] = round(total, 1)
    t["left_km"] = round(max(0.0, total - t["distance_traveled_km"]), 1)
    t["progress"] = t["progress_percent"] = 100 if done else max(0, min(99, round(trav / total * 100))) if total else 0
    t["near_arrival"] = t["near"] = t["status"] == "DANG_VAN_CHUYEN" and t["progress"] >= NEAR_PCT
    t["leg"] = 1 if t["leg_type"] == "FARM_TO_WAREHOUSE" else 2
    t["leg_label"], t["status_label"] = LEG_VN[t["leg_type"]], TRIP_VN[t["status"]]
    t["origin_lat"], t["origin_lon"], t["dest_lat"], t["dest_lon"], t["dest_name"] = t["o_lat"], t["o_lon"], t["d_lat"], t["d_lon"], t["d_name"]
    t["last"] = last
    t["n_points"] = len(pts)
    t["mode"] = "SIM" if any(p["src"] == "SIM" for p in pts) else ("GPS" if pts else None)
    t["speed_kmh"] = last["speed"] if last else None
    t["container_temp_c"] = last["temp"] if last else None
    t["current_lat"], t["current_lng"] = (last["lat"], last["lon"]) if last else (None, None)
    t["current_location_name"] = ((last["location_name"] or locate(path, last["lat"], last["lon"]) or "Đang di chuyển") if last else None)
    t["temp_alert"] = bool(last and last["temp"] is not None and not (t["temp_min"] <= last["temp"] <= t["temp_max"]))
    n, since, mins = temp_run(pts, t["temp_min"], t["temp_max"])
    t["temp_warning"] = is_temp_warning(n, mins)
    t["temp_warning_since"] = since if t["temp_warning"] else None
    t["temp_check"] = bool(t["temp_warned_at"]) or t["temp_warning"]  # nhãn "Cần kiểm tra nhiệt độ khi nhập"
    t["temp_exceed_points"] = sum(1 for p in pts if p["temp"] is not None and not (t["temp_min"] <= p["temp"] <= t["temp_max"]))
    # mất tín hiệu: chuyến đang chạy mà quá 10 phút không có điểm mới (không bịa vị trí mới)
    ref = parse_ts(last["created_at"]) if last else parse_ts(t["started_at"] or t["created_at"])
    t["signal_lost"] = bool(t["status"] == "DANG_VAN_CHUYEN" and ref and (datetime.datetime.now() - ref).total_seconds() > LOST_MIN * 60)
    t["last_signal_at"] = last["created_at"] if last else None
    # ETA: quãng còn lại / tốc độ trung bình 5 điểm gần nhất (tối thiểu 20 km/h); chưa xuất phát thì giờ dự kiến xuất phát + thời gian tuyến
    sp = [p["speed"] for p in pts[-5:] if p["speed"]]
    avg = max(sum(sp) / len(sp) if sp else 40.0, 20.0)
    if done:
        t["eta"] = t["ended_at"]
    elif t["status"] in ("SU_CO", "HUY"):
        t["eta"] = None
    elif t["status"] == "DANG_VAN_CHUYEN":
        t["eta"] = (datetime.datetime.now() + datetime.timedelta(hours=t["left_km"] / avg)).strftime("%Y-%m-%d %H:%M")
    else:
        start = max(datetime.datetime.now(), parse_ts(t["created_at"]) or datetime.datetime.now())
        t["eta"] = (start + datetime.timedelta(hours=total / 40.0)).strftime("%Y-%m-%d %H:%M")
    t["gps_timeline"] = trip_timeline(c, t, pts, path)
    t["route_waypoints"] = [{"name": p["name"], "lat": p["lat"], "lon": p["lon"], "kind": p["kind"], "km": round(p["cum"] * ROAD_FACTOR, 1)} for p in path]
    t["cargo"] = trip_cargo(c, t)
    if t["order_id"]:
        o = c.execute("SELECT code FROM orders WHERE id=?", (t["order_id"],)).fetchone()
        t["order_code"] = o["code"] if o else ""
        t["ref_code"] = t["order_code"]
    else:
        b = c.execute("SELECT b.code,s.name AS sname,s.farm_name FROM batches b JOIN suppliers s ON s.id=b.supplier_id WHERE b.id=?", (t["batch_id"],)).fetchone()
        t["order_code"] = ""
        t["batch_code"] = t["ref_code"] = b["code"] if b else ""
        t["supplier_name"], t["farm_name"] = (b["sname"], b["farm_name"] or b["sname"]) if b else ("", "")
    t["pin_set"] = bool(t["pin_hash"])
    if secret:
        t["pin"] = driver_pin(t["plate"], t["token"])
    else:
        t.pop("token", None)
    t.pop("pin_hash", None)
    if points:
        t["points"] = pts
    return t


def trip_sort_key(t):
    rank = 0 if t["near_arrival"] else {"DANG_VAN_CHUYEN": 1, "CHO_LAY_HANG": 2}.get(t["status"], 3)
    return (rank, -t["id"])


def trip_view(c, t, u, points=False):
    """Dữ liệu chuyến theo vai trò. Quản lý kho, Thủ kho: đầy đủ kèm token và PIN. Kinh doanh: chỉ xem, không có token, PIN, SĐT tài xế.
    Nông dân: chỉ chặng 1 của chính mình, không có thông tin đơn hàng hay khách."""
    role = u["role"]
    if role == "QA":
        raise ApiError(403, "Vai trò \"%s\" không xem được vận chuyển" % ROLE_NAMES[role])
    full = role in ("QUAN_LY", "THU_KHO") or (role == "NONG_DAN" and t["leg_type"] == "FARM_TO_WAREHOUSE")
    d = trip_json(c, t, points=points, secret=full)
    if role == "KINH_DOANH":
        d.pop("driver_phone", None)
    if role == "NONG_DAN":
        for k in ("order_id", "order_code", "dest_address"):
            d.pop(k, None)
        d["ref_code"] = d.get("batch_code", "")
    return d


def visible_trips(c, u, leg=None):
    if u["role"] == "QA":
        raise ApiError(403, "Vai trò \"%s\" không xem được vận chuyển" % ROLE_NAMES[u["role"]])
    where, args = [], []
    if u["role"] == "NONG_DAN":
        where += ["leg_type='FARM_TO_WAREHOUSE'", "supplier_id=?"]
        args.append(need_farmer(u))
    if leg:
        if leg not in ("in", "out"):
            raise ApiError(400, "Tham số leg chỉ nhận in hoặc out")
        where.append("leg_type=?")
        args.append("FARM_TO_WAREHOUSE" if leg == "in" else "WAREHOUSE_TO_STORE")
    return c.execute("SELECT * FROM trips" + (" WHERE " + " AND ".join(where) if where else ""), args).fetchall()


def trip_by_id(c, tid):
    t = c.execute("SELECT * FROM trips WHERE id=?", (tid,)).fetchone()
    if not t:
        raise ApiError(404, "Không tìm thấy chuyến xe")
    return t


def trip_for_user(c, u, tid):
    t = trip_by_id(c, tid)
    if u["role"] == "QA":
        raise ApiError(403, "Vai trò \"%s\" không xem được vận chuyển" % ROLE_NAMES[u["role"]])
    if u["role"] == "NONG_DAN" and (t["leg_type"] != "FARM_TO_WAREHOUSE" or t["supplier_id"] != need_farmer(u)):
        raise ApiError(404, "Không tìm thấy chuyến xe")  # không để lộ chuyến của người khác có tồn tại
    return t


def set_trip_status(c, t, status, uid=None, note=None):
    status = {"DANG_GIAO": "DANG_VAN_CHUYEN"}.get(status, status)  # tên cũ của trạng thái
    if status not in ("DANG_VAN_CHUYEN", "DA_GIAO", "SU_CO"):
        raise ApiError(400, "Trạng thái không hợp lệ")
    if t["status"] == "DA_GIAO":
        raise ApiError(409, "Chuyến đã giao xong")
    if t["status"] == "HUY":
        raise ApiError(409, "Chuyến đã bị hủy")
    if t["status"] == status:
        raise ApiError(409, "Chuyến đang ở trạng thái %s" % TRIP_VN[status])
    c.execute("UPDATE trips SET status=?,ended_at=?,started_at=CASE WHEN ?='DANG_VAN_CHUYEN' THEN COALESCE(started_at,?) ELSE started_at END,incident_note=? WHERE id=?",
              (status, now() if status == "DA_GIAO" else None, status, now(), (str(note or "").strip()[:150] or None) if status == "SU_CO" else None, t["id"]))
    audit(c, uid, "Chuyến %s chuyển sang %s" % (t["code"], TRIP_VN[status]) + ((": " + str(note).strip()[:150]) if status == "SU_CO" and note else ""))


def close_inbound(c, bid, ok, reason=None, uid=None):
    """Kho tiếp nhận phiếu -> chuyến chặng 1 tự chuyển Đã giao (nếu tài xế chưa bấm). Phiếu bị từ chối -> Sự cố kèm lý do."""
    t = c.execute("SELECT * FROM trips WHERE batch_id=?", (bid,)).fetchone()
    if not t or t["status"] == "HUY":
        return
    if ok:
        if t["status"] != "DA_GIAO":
            c.execute("UPDATE trips SET status='DA_GIAO',ended_at=?,incident_note=NULL WHERE id=?", (now(), t["id"]))
            audit(c, uid, "Chuyến %s tự chuyển Đã giao do kho đã tiếp nhận phiếu giao" % t["code"])
    elif t["status"] != "SU_CO":
        c.execute("UPDATE trips SET status='SU_CO',incident_note=? WHERE id=?", (str(reason or "Phiếu giao không được nhập kho")[:150], t["id"]))
        audit(c, uid, "Chuyến %s chuyển Sự cố: phiếu giao bị từ chối (%s)" % (t["code"], str(reason or "")[:150]))


def sim_step(c, t, uid=None, need_demo=False):
    """Mô phỏng một nhịp di chuyển dọc tuyến, không trộn với điểm GPS thật. Thủ kho/Quản lý kho dùng được ở nút Mô phỏng giao hàng; trang tài xế vẫn cần KHO_DEMO=1."""
    if need_demo and os.environ.get("KHO_DEMO") != "1":
        raise ApiError(403, "Chế độ mô phỏng đang tắt. Khởi động máy chủ với biến môi trường KHO_DEMO=1 để dùng")
    if t["status"] in ("DA_GIAO", "SU_CO", "HUY"):
        raise ApiError(409, "Chuyến không còn chạy")
    if c.execute("SELECT 1 FROM trip_points WHERE trip_id=? AND src='GPS' LIMIT 1", (t["id"],)).fetchone():
        raise ApiError(409, "Chuyến này đang dùng GPS thật, không trộn với dữ liệu mô phỏng")
    td = dict(t)
    path = route_path(c, td)
    k = c.execute("SELECT COUNT(*) FROM trip_points WHERE trip_id=?", (t["id"],)).fetchone()[0]
    f = min(1.0, k / 10.0)
    la, lo = point_at(path, path[-1]["cum"] * f)
    if 0 < f < 1:
        la += random.uniform(-1, 1) * 0.0004
        lo += random.uniform(-1, 1) * 0.0004
    add_point(c, td, la, lo, 0 if f in (0, 1) else round(random.uniform(35, 55), 0), "SIM", uid=uid)
    if t["status"] == "CHO_LAY_HANG":
        c.execute("UPDATE trips SET status='DANG_VAN_CHUYEN',started_at=? WHERE id=?", (now(), t["id"]))
        audit(c, uid, "Chuyến %s bắt đầu (mô phỏng)" % t["code"])
    if f >= 1:
        c.execute("UPDATE trips SET status='DA_GIAO',ended_at=? WHERE id=?", (now(), t["id"]))
        audit(c, uid, "Chuyến %s đã đến nơi (mô phỏng)" % t["code"])


# ---- tài xế: token hoặc cặp biển số + PIN
_PIN_FAILS = {}
_PIN_LOCK = threading.Lock()


def pin_throttle(key, fail):
    """Sai PIN quá 5 lần trong 10 phút thì khóa tạm biển số đó (chống dò). fail=None: chỉ kiểm tra khóa; True: ghi nhận một lần sai; False: xóa."""
    with _PIN_LOCK:
        t0 = time.time()
        lst = [x for x in _PIN_FAILS.get(key, []) if t0 - x < PIN_WINDOW]
        if fail is None and len(lst) >= PIN_FAILS:
            raise ApiError(429, "Nhập sai quá nhiều lần, đã khóa tạm 10 phút. Hãy thử lại sau")
        if fail is False:
            lst = []
        elif fail:
            lst.append(t0)
        if lst:
            _PIN_FAILS[key] = lst
        else:
            _PIN_FAILS.pop(key, None)


def driver_trips_by_pin(c, plate, pin):
    key = plate_key(plate)
    if not key:
        raise ApiError(401, "Biển số hoặc mã PIN không đúng")
    pin_throttle(key, None)
    if not re.fullmatch(r"\d{6}", str(pin or "")):
        pin_throttle(key, True)
        raise ApiError(401, "Biển số hoặc mã PIN không đúng")
    ok = [t for t in c.execute("SELECT * FROM trips WHERE status IN ('CHO_LAY_HANG','DANG_VAN_CHUYEN') ORDER BY id").fetchall()
          if plate_key(t["plate"]) == key and t["pin_hash"] and hmac.compare_digest(pin_hash(str(pin), t["token"]), t["pin_hash"])]
    if not ok:
        pin_throttle(key, True)
        raise ApiError(401, "Biển số hoặc mã PIN không đúng, hoặc xe không có chuyến đang mở")
    pin_throttle(key, False)
    return ok[:2]  # tối đa 2 chặng (VC-IN và VC-OUT)


def driver_trip(c, src):
    """Tài xế chỉ được dùng token của chuyến, hoặc biển số + PIN (kèm mã chuyến nếu xe có 2 chuyến đang mở)."""
    token = src.get("t")
    if token:
        t = c.execute("SELECT * FROM trips WHERE token=?", (str(token),)).fetchone()
        if not t:
            raise ApiError(404, "Liên kết chuyến xe không hợp lệ")
        return t
    if src.get("plate") or src.get("pin"):
        ts = driver_trips_by_pin(c, src.get("plate"), src.get("pin"))
        want = str(src.get("trip") or "")
        if want:
            ts = [x for x in ts if x["code"] == want]
        if len(ts) != 1:
            raise ApiError(409, "Xe có 2 chuyến đang mở, hãy chỉ rõ mã chuyến")
        return ts[0]
    raise ApiError(404, "Liên kết chuyến xe không hợp lệ")


def driver_view(c, t, token=False):
    """Trang tài xế: không có tên nông dân/cửa hàng ngoài tên điểm đến, không có giá, doanh thu."""
    d = trip_json(c, t, secret=False)
    keep = ("code", "leg_type", "leg", "leg_label", "status", "status_label", "plate", "driver", "ref_code", "origin_name", "d_name", "total_km", "left_km",
            "distance_traveled_km", "progress", "eta", "speed_kmh", "container_temp_c", "temp_min", "temp_max", "temp_alert", "temp_warning", "signal_lost",
            "last_signal_at", "mode", "near_arrival", "current_location_name")
    out = {k: d[k] for k in keep}
    out["cargo"] = [{"product": x["product"], "qty_kg": x["qty_kg"], "code": x.get("lot_code") or x.get("batch_code")} for x in d["cargo"]]
    out["demo"] = os.environ.get("KHO_DEMO") == "1"
    if token:
        out["token"] = t["token"]
    return out


def gps_check(c, t, lat, lon, speed_kmh):
    """Giới hạn tần suất (>= 5 giây giữa 2 điểm) và loại điểm bất thường (quá 120 km/h hoặc nhảy xa)."""
    if speed_kmh is not None and speed_kmh > MAX_SPEED:
        raise ApiError(400, "Tốc độ %d km/h không hợp lý, điểm GPS bị bỏ qua" % speed_kmh)
    last = c.execute("SELECT lat,lon,created_at FROM trip_points WHERE trip_id=? ORDER BY id DESC LIMIT 1", (t["id"],)).fetchone()
    if not last:
        return
    gap = float(os.environ.get("KHO_GPS_MIN_INTERVAL", GPS_MIN_GAP))
    ts = parse_ts(last["created_at"])
    dt = (datetime.datetime.now() - ts).total_seconds() if ts else 1e9
    if dt < gap:
        raise ApiError(429, "Gửi vị trí quá nhanh, vui lòng chờ ít nhất %g giây giữa 2 lần" % gap)
    dist = haversine(last["lat"], last["lon"], lat, lon)
    if dist > 0.5 and dist / max(dt, 1.0) * 3600 > MAX_JUMP_KMH:
        raise ApiError(400, "Điểm GPS nhảy bất thường (%.1f km trong %d giây), đã bỏ qua" % (dist, dt))


def mask_leg(c, t, hide=True):
    """Chặng vận chuyển cho trang truy xuất công khai: không có tên/địa chỉ cửa hàng, SĐT tài xế, biển số đầy đủ."""
    t = dict(t)
    pts = rows(c, "SELECT lat,lon,temp,src,location_name,created_at FROM trip_points WHERE trip_id=? ORDER BY id", (t["id"],))
    path = route_path(c, t)
    out_leg = t["leg_type"] == "WAREHOUSE_TO_STORE"
    exceed = sum(1 for p in pts if p["temp"] is not None and not (t["temp_min"] <= p["temp"] <= t["temp_max"]))
    return {"leg": 2 if out_leg else 1, "label": ("Chặng 2: Kho → Điểm bán lẻ" if hide else "Chặng 2: Kho → Cửa hàng") if out_leg else "Chặng 1: Nông trại → Kho",
            "from": t["origin_name"], "to": "Điểm bán lẻ" if (out_leg and hide) else t["d_name"], "status": t["status"], "status_label": TRIP_VN[t["status"]],
            "plate": mask_plate(t["plate"]), "distance_km": t["total_distance_km"], "started_at": (pts[0]["created_at"] if pts else t["started_at"]),
            "ended_at": t["ended_at"], "stops": [{"time": x["time"], "text": x["text"]} for x in trip_timeline(c, t, pts, path, hide_dest=out_leg and hide)],
            "temp": {"min": t["temp_min"], "max": t["temp_max"], "points": len(pts), "exceeded": exceed, "alert": bool(t["temp_warned_at"])},
            "source": ("SIM" if any(p["src"] == "SIM" for p in pts) else "GPS") if pts else None}


def lot_out_trip(c, lot_id, ship=None, order=None):
    """Chuyến chặng 2 đã xuất đúng lô này, chọn theo mã chuyến (VC-OUT-...) hoặc theo id đơn hàng; None nếu không thuộc lô hoặc đã hủy."""
    if order:
        if not str(order).isdigit():
            return None
        return c.execute("""SELECT t.* FROM trips t WHERE t.order_id=? AND t.leg_type='WAREHOUSE_TO_STORE' AND t.status<>'HUY'
          AND t.order_id IN (SELECT order_id FROM reservations WHERE lot_id=?)""", (int(order), lot_id)).fetchone()
    if not ship:
        return None
    return c.execute("""SELECT t.* FROM trips t WHERE t.code=? AND t.leg_type='WAREHOUSE_TO_STORE' AND t.status<>'HUY'
      AND t.order_id IN (SELECT order_id FROM reservations WHERE lot_id=?)""", (str(ship), lot_id)).fetchone()


def public_journey(c, lot=None, batch_id=None, ship=None, hide=True, order=None):
    """Hành trình theo NGỮ CẢNH: luôn có chặng 1 của lô; chặng 2 chỉ là CHUYẾN `ship` (nếu có), tuyệt đối không gộp các chuyến khác của cùng lô."""
    legs = []
    bid = batch_id
    if lot is not None and bid is None:
        bid = lot_origin(c, lot)["batch_id"]
    if bid:
        t = c.execute("SELECT * FROM trips WHERE batch_id=? AND status<>'HUY'", (bid,)).fetchone()
        if t:
            legs.append(mask_leg(c, t, hide))
    if lot is not None and (ship or order):
        t = lot_out_trip(c, lot["id"], ship, order)
        if t:
            legs.append(mask_leg(c, t, hide))
    return legs


# ---------------------------------------------------------------- nghiệp vụ
def rows(c, sql, args=()):
    return [dict(r) for r in c.execute(sql, args).fetchall()]


def available(c, sku):
    return c.execute("SELECT COALESCE(SUM(qty_kg-reserved_kg),0) FROM lots WHERE sku=? AND qa_status='DAT' AND expiry_date>=?", (sku, today())).fetchone()[0]


def num(v, name, positive=True):
    try:
        v = float(v)
    except (TypeError, ValueError):
        raise ApiError(400, "%s không hợp lệ" % name)
    if positive and v <= 0:
        raise ApiError(400, "%s phải lớn hơn 0" % name)
    return v


def valid_date(v, name):
    try:
        datetime.date.fromisoformat(v)
        return v
    except (TypeError, ValueError):
        raise ApiError(400, "%s không hợp lệ (định dạng YYYY-MM-DD)" % name)


def tx(c):
    c.execute("BEGIN IMMEDIATE")


def capacity_info(c, wid):
    """Sức chứa khả dụng = tối đa - tồn kho hiện tại - tổng khối lượng phiếu Chờ duyệt (khóa chỗ tạm)."""
    w = c.execute("SELECT * FROM warehouses WHERE id=?", (wid,)).fetchone()
    used = c.execute("SELECT COALESCE(SUM(qty_kg),0) FROM lots WHERE warehouse_id=?", (wid,)).fetchone()[0]
    pend = c.execute("SELECT COALESCE(SUM(qty_kg),0) FROM receipts WHERE warehouse_id=? AND status='CHO_DUYET'", (wid,)).fetchone()[0]
    cap = w["capacity_kg"]
    free = cap - used - pend
    return {"id": wid, "name": w["name"], "city": w["city"], "capacity_kg": cap, "used_kg": used, "pending_kg": pend,
            "free_kg": free, "used_pct": round(used / cap * 100, 2), "committed_pct": round((used + pend) / cap * 100, 2),
            "free_pct": round(free / cap * 100, 2), "temp_min": w["temp_min"], "temp_max": w["temp_max"]}


def buy_price(c, sku):
    """Đơn giá thu mua mới nhất của công ty (cột Giá thu mua ở tab Giá thị trường) tại thời điểm gọi."""
    return c.execute("SELECT price,price_date FROM market_prices WHERE sku=? AND kind='THU_MUA' AND price_date<=? ORDER BY price_date DESC,id DESC LIMIT 1",
                     (sku, today())).fetchone()


def create_receipt(c, u, b):
    if b.get("plan_id"):
        raise ApiError(409, "Không lập phiếu nhập trực tiếp từ kế hoạch thu hoạch. Nông dân lập phiếu giao hàng (GH) từ kế hoạch, "
                            "Thủ kho tiếp nhận phiếu giao rồi mới lập phiếu nhập kho")
    sku = b.get("sku")
    p = c.execute("SELECT * FROM products WHERE sku=?", (sku,)).fetchone()
    if not p:
        raise ApiError(400, "Mặt hàng không tồn tại")
    w = c.execute("SELECT * FROM warehouses WHERE id=?", (b.get("warehouse_id"),)).fetchone()
    if not w:
        raise ApiError(400, "Kho không tồn tại")
    if not c.execute("SELECT 1 FROM suppliers WHERE id=?", (b.get("supplier_id"),)).fetchone():
        raise ApiError(400, "Nhà cung cấp không tồn tại")
    qty = num(b.get("qty_kg"), "Khối lượng")
    h = valid_date(b.get("harvest_date"), "Ngày thu hoạch")
    if h > today():
        raise ApiError(400, "Ngày thu hoạch không được sau ngày hôm nay")
    if add_days(h, p["shelf_days"]) < today():
        raise ApiError(400, "Hàng đã quá hạn sử dụng, không thể nhập kho")
    if w["temp_max"] < p["temp_min"] or w["temp_min"] > p["temp_max"]:
        raise ApiError(409, "%s (%g–%g°C) không phù hợp bảo quản %s (%g–%g°C)" % (w["name"], w["temp_min"], w["temp_max"], p["name"], p["temp_min"], p["temp_max"]))
    spray = b.get("spray_date") or None
    if spray:
        valid_date(spray, "Ngày phun thuốc")
    phi = int(num(b.get("phi_days") or 0, "Thời gian cách ly", False))
    tx(c)
    try:
        ci = capacity_info(c, w["id"])
        if qty > ci["free_kg"] + 1e-9:
            raise ApiError(409, "%s không đủ sức chứa: vượt %.0f kg (còn trống %.0f kg). Hãy chuyển sang kho khác còn trống" % (w["name"], qty - ci["free_kg"], max(ci["free_kg"], 0)))
        n = c.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] + 1
        code = "PN-%s-%03d" % (today().replace("-", "")[2:], n)
        cur = c.execute("INSERT INTO receipts(code,sku,supplier_id,warehouse_id,qty_kg,harvest_date,spray_date,phi_days,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (code, sku, b["supplier_id"], w["id"], qty, h, spray, phi, u["id"], now()))
        audit(c, u["id"], "Lập phiếu nhập %s (%g kg %s)" % (code, qty, p["name"]))
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise
    return {"id": cur.lastrowid, "code": code}


def approve_receipt(c, u, rid, approve):
    tx(c)
    try:
        r = c.execute("SELECT * FROM receipts WHERE id=?", (rid,)).fetchone()
        if not r:
            raise ApiError(404, "Không tìm thấy phiếu nhập")
        if r["status"] != "CHO_DUYET":
            raise ApiError(409, "Phiếu đã được xử lý trước đó")
        if not approve:
            c.execute("UPDATE receipts SET status='TU_CHOI',decided_by=? WHERE id=?", (u["id"], rid))
            if r["batch_id"]:  # phiếu giao quay về Chờ nhập kho để Thủ kho cân kiểm lại và lập phiếu nhập mới
                c.execute("UPDATE batches SET receipt_id=NULL WHERE id=? AND receipt_id=?", (r["batch_id"], rid))
            audit(c, u["id"], "Từ chối phiếu nhập " + r["code"])
            c.execute("COMMIT")
            return {"status": "TU_CHOI"}
        w = c.execute("SELECT * FROM warehouses WHERE id=?", (r["warehouse_id"],)).fetchone()
        used = c.execute("SELECT COALESCE(SUM(qty_kg),0) FROM lots WHERE warehouse_id=?", (w["id"],)).fetchone()[0]
        if used + r["qty_kg"] > w["capacity_kg"]:
            raise ApiError(409, "%s không đủ sức chứa (còn trống %.0f kg)" % (w["name"], w["capacity_kg"] - used))
        bt = pl = None
        if r["batch_id"]:  # phiếu nhập sinh từ phiếu giao hàng nguồn Kế hoạch: kiểm tra và lấy đơn giá thu mua TẠI THỜI ĐIỂM DUYỆT
            bt = c.execute("SELECT * FROM batches WHERE id=?", (r["batch_id"],)).fetchone()
            if not bt or bt["status"] != "CHO_GIAO" or bt["receipt_id"] != rid:
                raise ApiError(409, "Phiếu giao hàng liên kết không còn ở trạng thái Chờ nhập kho")
            pl = c.execute("SELECT * FROM plans WHERE id=?", (bt["plan_id"],)).fetchone()
            if not pl or pl["status"] != "DANG_GIAO":
                raise ApiError(409, "Kế hoạch liên kết không còn ở trạng thái Đang giao hàng")
            buy = buy_price(c, r["sku"])
            if not buy:
                raise ApiError(409, "Chưa có Giá thu mua của mặt hàng %s. Hãy bổ sung ở tab Giá thị trường trước khi duyệt phiếu nhập" % r["sku"])
        p = c.execute("SELECT * FROM products WHERE sku=?", (r["sku"],)).fetchone()
        seq = c.execute("SELECT COUNT(*) FROM lots WHERE sku=? AND harvest_date=?", (r["sku"], r["harvest_date"])).fetchone()[0] + 1
        code = "%s-%s-%02d" % (r["sku"], r["harvest_date"].replace("-", "")[2:], seq)
        while c.execute("SELECT 1 FROM lots WHERE code=?", (code,)).fetchone():
            seq += 1
            code = "%s-%s-%02d" % (r["sku"], r["harvest_date"].replace("-", "")[2:], seq)
        lid = c.execute("INSERT INTO lots(code,sku,supplier_id,warehouse_id,harvest_date,expiry_date,spray_date,phi_days,qty_kg,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (code, r["sku"], r["supplier_id"], r["warehouse_id"], r["harvest_date"], add_days(r["harvest_date"], p["shelf_days"]), r["spray_date"], r["phi_days"], r["qty_kg"], now())).lastrowid
        c.execute("UPDATE receipts SET status='DA_DUYET',decided_by=?,lot_id=? WHERE id=?", (u["id"], lid, rid))
        ledger(c, lid, "NHAP", r["qty_kg"], "Phiếu nhập " + r["code"], u["id"])
        audit(c, u["id"], "Duyệt phiếu nhập %s, tạo lô %s" % (r["code"], code))
        out = {"status": "DA_DUYET", "lot_code": code}
        if bt:  # đồng bộ 4 phân hệ: tồn kho (lô vừa tạo) - phiếu giao - kế hoạch - công nợ
            amount = round(r["qty_kg"] * buy["price"])
            c.execute("UPDATE batches SET status='DA_NHAP',recv_kg=?,price=?,amount=?,lot_codes=?,meets_standard=1,qc_by=?,qc_at=? WHERE id=?",
                      (r["qty_kg"], buy["price"], amount, code, r["created_by"], now(), bt["id"]))
            c.execute("UPDATE plans SET status='DA_NHAP',lot_id=?,actual_kg=? WHERE id=?", (lid, r["qty_kg"], pl["id"]))
            diff = r["qty_kg"] - pl["expected_kg"]
            audit(c, u["id"], "Kế hoạch %s đã nhập kho qua phiếu giao %s: lô %s thực nhận %g kg (%s so với dự kiến)" % (
                pl["code"], bt["code"], code, r["qty_kg"], ("%+g kg" % diff) if abs(diff) >= 1e-9 else "đúng"))
            audit(c, u["id"], "Công nợ %s: %g kg x %s đ/kg (giá thu mua %s) = %s đ, chờ thanh toán" % (
                r["code"], r["qty_kg"], format(buy["price"], ","), buy["price_date"], format(amount, ",")))
            out.update({"batch_code": bt["code"], "plan_code": pl["code"], "unit_price": buy["price"], "amount": amount})
        c.execute("COMMIT")
        return out
    except Exception:
        c.execute("ROLLBACK") if c.in_transaction else None
        raise


def inspect_lot(c, u, lid, b):
    result = b.get("result")
    if result not in ("DAT", "CACH_LY", "KHONG_DAT"):
        raise ApiError(400, "Kết luận không hợp lệ")
    res = num(b.get("residue"), "Dư lượng", False)
    tx(c)
    try:
        l = c.execute("SELECT l.*,p.mrl,p.name FROM lots l JOIN products p ON p.sku=l.sku WHERE l.id=?", (lid,)).fetchone()
        if not l:
            raise ApiError(404, "Không tìm thấy lô")
        if l["reserved_kg"] > 0 and result != "DAT":
            raise ApiError(409, "Lô đang được giữ cho đơn hàng, hãy hủy giữ hàng trước khi cách ly")
        if result == "DAT":
            if res > l["mrl"]:
                raise ApiError(409, "Dư lượng %.2f vượt giới hạn %.2f mg/kg, không thể kết luận Đạt" % (res, l["mrl"]))
            if l["spray_date"] and (datetime.date.fromisoformat(l["harvest_date"]) - datetime.date.fromisoformat(l["spray_date"])).days < l["phi_days"]:
                raise ApiError(409, "Chưa đủ thời gian cách ly thuốc BVTV (%d ngày), không thể kết luận Đạt" % l["phi_days"])
        grade = b.get("grade") if result == "DAT" else None
        c.execute("UPDATE lots SET qa_status=?,grade=?,residue=? WHERE id=?", (result, grade, res, lid))
        audit(c, u["id"], "Kiểm định lô %s: %s" % (l["code"], result))
        xb = xdock_batch_of_lot(c, l["code"])
        if xb:  # lô thuộc đơn Kinh doanh mua trực tiếp: QA kết luận xong thì tự điều phối giao thẳng về cửa hàng
            xdock_try(c, u, xb)
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK") if c.in_transaction else None
        raise
    return {"ok": True}


def create_order(c, u, b):
    if not c.execute("SELECT 1 FROM customers WHERE id=?", (b.get("customer_id"),)).fetchone():
        raise ApiError(400, "Khách hàng không tồn tại")
    items = b.get("items") or []
    if not items:
        raise ApiError(400, "Đơn hàng cần ít nhất một mặt hàng")
    clean = []
    for it in items:
        if not c.execute("SELECT 1 FROM products WHERE sku=?", (it.get("sku"),)).fetchone():
            raise ApiError(400, "Mặt hàng không tồn tại")
        q = num(it.get("qty_kg"), "Số lượng")
        av = available(c, it["sku"])
        if q > av:
            raise ApiError(409, "Chỉ còn %.0f kg khả dụng của %s, không đủ %.0f kg" % (av, it["sku"], q))
        clean.append((it["sku"], q))
    tx(c)
    try:
        n = c.execute("SELECT COUNT(*) FROM orders").fetchone()[0] + 1
        code = "DH-%04d" % n
        oid = c.execute("INSERT INTO orders(code,customer_id,created_by,created_at) VALUES(?,?,?,?)", (code, b["customer_id"], u["id"], now())).lastrowid
        for sku, q in clean:
            c.execute("INSERT INTO order_items(order_id,sku,qty_kg) VALUES(?,?,?)", (oid, sku, q))
        audit(c, u["id"], "Tạo đơn hàng " + code)
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise
    return {"id": oid, "code": code}


def reserve_order(c, u, oid):
    tx(c)
    try:
        o = c.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        if not o:
            raise ApiError(404, "Không tìm thấy đơn hàng")
        if o["status"] != "MOI":
            raise ApiError(409, "Đơn không ở trạng thái Mới")
        plan = []
        for it in c.execute("SELECT * FROM order_items WHERE order_id=?", (oid,)).fetchall():
            need = it["qty_kg"]
            # FEFO: lô còn hạn, đã đạt kiểm định, hết hạn trước thì lấy trước
            for l in c.execute("SELECT * FROM lots WHERE sku=? AND qa_status='DAT' AND expiry_date>=? AND qty_kg-reserved_kg>0 ORDER BY expiry_date, id", (it["sku"], today())).fetchall():
                if need <= 0:
                    break
                take = min(need, l["qty_kg"] - l["reserved_kg"])
                plan.append((l["id"], take))
                need -= take
            if need > 1e-9:
                raise ApiError(409, "Không đủ hàng khả dụng cho %s (thiếu %.0f kg)" % (it["sku"], need))
        for lid, q in plan:
            c.execute("UPDATE lots SET reserved_kg=reserved_kg+? WHERE id=?", (q, lid))
            c.execute("INSERT INTO reservations(order_id,lot_id,qty_kg) VALUES(?,?,?)", (oid, lid, q))
        c.execute("UPDATE orders SET status='DA_GIU_HANG' WHERE id=?", (oid,))
        audit(c, u["id"], "Giữ hàng cho đơn %s theo FEFO" % o["code"])
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK") if c.in_transaction else None
        raise
    return {"status": "DA_GIU_HANG"}


def release(c, oid):
    for r in c.execute("SELECT * FROM reservations WHERE order_id=?", (oid,)).fetchall():
        c.execute("UPDATE lots SET reserved_kg=reserved_kg-? WHERE id=?", (r["qty_kg"], r["lot_id"]))
    c.execute("DELETE FROM reservations WHERE order_id=?", (oid,))


def cancel_order(c, u, oid):
    tx(c)
    try:
        o = c.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        if not o:
            raise ApiError(404, "Không tìm thấy đơn hàng")
        if o["status"] not in ("MOI", "DA_GIU_HANG"):
            raise ApiError(409, "Chỉ hủy được đơn chưa xuất kho")
        release(c, oid)
        c.execute("UPDATE orders SET status='DA_HUY' WHERE id=?", (oid,))
        audit(c, u["id"], "Hủy đơn %s, nhả hàng đã giữ" % o["code"])
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK") if c.in_transaction else None
        raise
    return {"status": "DA_HUY"}


def do_ship(c, u, o, plate=None, driver=None, phone=None, note=None):
    """Xuất kho đơn đã giữ hàng + tạo chuyến chặng 2. Phải gọi trong giao dịch của bên gọi."""
    oid = o["id"]
    for r in c.execute("SELECT r.*,l.qa_status,l.expiry_date,l.code FROM reservations r JOIN lots l ON l.id=r.lot_id WHERE order_id=?", (oid,)).fetchall():
        if r["qa_status"] != "DAT" or r["expiry_date"] < today():
            raise ApiError(409, "Lô %s không còn đủ điều kiện xuất kho. Hãy hủy đơn và giữ hàng lại" % r["code"])
        c.execute("UPDATE lots SET qty_kg=qty_kg-?,reserved_kg=reserved_kg-? WHERE id=?", (r["qty_kg"], r["qty_kg"], r["lot_id"]))
        ledger(c, r["lot_id"], "XUAT", -r["qty_kg"], "Xuất theo đơn " + o["code"] + (note or ""), u["id"])
    wh = c.execute("SELECT l.warehouse_id FROM reservations r JOIN lots l ON l.id=r.lot_id WHERE r.order_id=? LIMIT 1", (oid,)).fetchone()
    c.execute("UPDATE orders SET status='DA_XUAT_KHO' WHERE id=?", (oid,))
    create_trip_out(c, o, wh["warehouse_id"] if wh else 1, plate or None, driver or None, phone or None, uid=u["id"])


def ship_order(c, u, oid, b=None):
    b = b or {}
    plate, driver, phone = str(b.get("plate") or "").strip()[:20], str(b.get("driver") or "").strip()[:40], clean_phone(b.get("phone"))
    tx(c)
    try:
        o = c.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        if not o:
            raise ApiError(404, "Không tìm thấy đơn hàng")
        if o["status"] != "DA_GIU_HANG":
            raise ApiError(409, "Đơn phải được giữ hàng trước khi xuất kho")
        do_ship(c, u, o, plate, driver, phone)
        audit(c, u["id"], "Xuất kho đơn %s và tạo chuyến xe chặng 2" % o["code"])
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK") if c.in_transaction else None
        raise
    return {"status": "DA_XUAT_KHO"}


def xdock_try(c, u, bid):
    """Cross-docking: lô của phiếu giao thuộc chào bán do Kinh doanh đã mua. Khi MỌI lô của phiếu đã có kết luận QC:
    lô Đạt được giữ thẳng cho một đơn của cửa hàng người mua và xuất kho chặng 2 ngay trong cùng giao dịch (không bao giờ thành tồn kho tự do);
    lô Không đạt thì ghi thông báo cho Nông dân và Kinh doanh. Gọi trong giao dịch. Trả về mã đơn hoặc None."""
    bt = c.execute("""SELECT b.*,o.buyer_type,o.buyer_id,o.dest_customer_id,o.code AS offer_code FROM batches b JOIN offers o ON o.id=b.offer_id WHERE b.id=?""", (bid,)).fetchone()
    if not bt or bt["buyer_type"] != "business" or bt["xdock_order_id"]:
        return None
    codes = [x for x in (bt["lot_codes"] or "").split(",") if x]
    lots = [dict(r) for r in c.execute("SELECT * FROM lots WHERE code IN (%s)" % ",".join("?" * len(codes)), codes).fetchall()] if codes else []
    if not lots or any(l["qa_status"] in ("CHO_KIEM_DINH", "CACH_LY") for l in lots):
        return None  # còn lô đang cách ly, chờ QA kết luận rồi mới quyết định
    good = [l for l in lots if l["qa_status"] == "DAT" and l["qty_kg"] - l["reserved_kg"] > 1e-9 and l["expiry_date"] >= today()]
    bad = [l["code"] for l in lots if l not in good]
    if not good:
        note = "Lô %s không đạt QC, không giao về cửa hàng. Hãy xử lý trả hàng, thương lượng lại hoặc giao lô khác." % ", ".join(bad)
        c.execute("UPDATE batches SET xdock_note=? WHERE id=?", (note, bid))
        audit(c, u["id"], "Cross-dock phiếu giao %s: %s Đã thông báo Nông dân và Kinh doanh" % (bt["code"], note))
        return None
    cu = c.execute("SELECT * FROM customers WHERE id=?", (bt["dest_customer_id"],)).fetchone()
    if not cu:
        raise ApiError(409, "Chào bán %s chưa có cửa hàng nhận hàng hợp lệ" % bt["offer_code"])
    oid = c.execute("INSERT INTO orders(code,customer_id,status,created_by,created_at) VALUES(?,?,?,?,?)",
                    ("DH-%04d" % (c.execute("SELECT COUNT(*) FROM orders").fetchone()[0] + 1), cu["id"], "DA_GIU_HANG", bt["buyer_id"] or u["id"], now())).lastrowid
    by_sku = {}
    for l in good:
        q = l["qty_kg"] - l["reserved_kg"]
        by_sku[l["sku"]] = by_sku.get(l["sku"], 0) + q
        c.execute("UPDATE lots SET reserved_kg=reserved_kg+? WHERE id=?", (q, l["id"]))
        c.execute("INSERT INTO reservations(order_id,lot_id,qty_kg) VALUES(?,?,?)", (oid, l["id"], q))
    for sku, q in by_sku.items():
        c.execute("INSERT INTO order_items(order_id,sku,qty_kg) VALUES(?,?,?)", (oid, sku, q))
    o = c.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    do_ship(c, u, o, note=" (cross-dock từ phiếu giao %s)" % bt["code"])
    note = ("Lô %s không đạt QC nên không giao đi." % ", ".join(bad)) if bad else None
    c.execute("UPDATE batches SET xdock_order_id=?,xdock_note=? WHERE id=?", (oid, note, bid))
    audit(c, u["id"], "Cross-dock: phiếu giao %s đạt QC, tự tạo đơn %s và xuất thẳng chặng 2 tới %s%s" % (bt["code"], o["code"], cu["name"], (". " + note) if note else ""))
    return o["code"]


def xdock_batch_of_lot(c, lot_code):
    r = c.execute("SELECT b.id FROM batches b JOIN offers o ON o.id=b.offer_id WHERE o.buyer_type='business' AND ','||b.lot_codes||',' LIKE ?", ("%," + lot_code + ",%",)).fetchone()
    return r["id"] if r else None


def stocktake(c, u, lid, b):
    q = num(b.get("qty_kg"), "Số lượng", False)
    if q < 0:
        raise ApiError(400, "Số lượng không hợp lệ")
    tx(c)
    try:
        l = c.execute("SELECT * FROM lots WHERE id=?", (lid,)).fetchone()
        if not l:
            raise ApiError(404, "Không tìm thấy lô")
        diff = q - l["qty_kg"]
        if diff == 0:
            raise ApiError(409, "Số liệu khớp, không có chênh lệch")
        if abs(diff) / max(l["qty_kg"], 1) > 0.02 and u["role"] != "QUAN_LY":
            raise ApiError(403, "Chênh lệch trên 2% cần Quản lý kho phê duyệt")
        if q < l["reserved_kg"]:
            raise ApiError(409, "Số lượng thấp hơn lượng đang giữ cho đơn hàng (%.0f kg)" % l["reserved_kg"])
        c.execute("UPDATE lots SET qty_kg=? WHERE id=?", (q, lid))
        ledger(c, lid, "DIEU_CHINH", diff, "Kiểm kê: thực tế %g kg" % q, u["id"])
        audit(c, u["id"], "Kiểm kê lô %s, chênh lệch %+g kg" % (l["code"], diff))
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK") if c.in_transaction else None
        raise
    return {"diff": diff}


LOT_SQL = """SELECT l.*,p.name AS product_name,w.name AS warehouse_name,s.name AS supplier_name,s.cert,
  (l.qty_kg-l.reserved_kg) AS free_kg,
  CAST(julianday(l.expiry_date)-julianday(?) AS INTEGER) AS days_left
  FROM lots l JOIN products p ON p.sku=l.sku JOIN warehouses w ON w.id=l.warehouse_id JOIN suppliers s ON s.id=l.supplier_id"""


def transport_summary(c):
    """Số xe đang chạy theo từng chặng, chuyến chặng 1 sắp đến nơi và cảnh báo nhiệt độ (cho Tổng quan)."""
    act = [trip_json(c, t) for t in c.execute("SELECT * FROM trips WHERE status IN ('CHO_LAY_HANG','DANG_VAN_CHUYEN')").fetchall()]
    short = lambda j: {"id": j["id"], "code": j["code"], "leg": j["leg"], "plate": j["plate"], "progress": j["progress"], "eta": j["eta"],
                       "to": j["d_name"], "product": (j["cargo"][0]["product"] if j["cargo"] else ""), "kg": sum(x["qty_kg"] for x in j["cargo"])}
    return {"active_trips": len(act), "active_in": sum(1 for j in act if j["leg"] == 1), "active_out": sum(1 for j in act if j["leg"] == 2),
            "near_arrival": [short(j) for j in act if j["leg"] == 1 and j["near_arrival"]],
            "temp_alerts": [short(j) for j in act if j["temp_warning"]],
            "lost_signal": [short(j) for j in act if j["signal_lost"]]}


def dashboard(c):
    t = today()
    prods = rows(c, "SELECT sku,name,min_stock FROM products")
    for p in prods:
        p["available"] = available(c, p["sku"])
    wh = rows(c, "SELECT w.*,COALESCE((SELECT SUM(qty_kg) FROM lots WHERE warehouse_id=w.id),0) AS used FROM warehouses w")
    return {
        "total_kg": c.execute("SELECT COALESCE(SUM(qty_kg),0) FROM lots").fetchone()[0],
        "lots": c.execute("SELECT COUNT(*) FROM lots WHERE qty_kg>0").fetchone()[0],
        "expiring": rows(c, LOT_SQL + " WHERE l.qty_kg>0 AND l.qa_status='DAT' AND l.expiry_date<=date(?,'+3 day') ORDER BY l.expiry_date", (t, t)),
        "held": rows(c, LOT_SQL + " WHERE l.qty_kg>0 AND l.qa_status IN ('CACH_LY','KHONG_DAT','CHO_KIEM_DINH')", (t,)),
        **transport_summary(c),
        "pending_receipts": c.execute("SELECT COUNT(*) FROM receipts WHERE status='CHO_DUYET'").fetchone()[0],
        "open_recalls": c.execute("SELECT COUNT(*) FROM recalls WHERE status='DANG_XU_LY'").fetchone()[0],
        "pending_inspection": c.execute("SELECT COUNT(*) FROM lots WHERE qty_kg>0 AND qa_status='CHO_KIEM_DINH'").fetchone()[0],
        "open_orders": c.execute("SELECT COUNT(*) FROM orders WHERE status IN ('MOI','DA_GIU_HANG')").fetchone()[0],
        "revenue": c.execute("SELECT COALESCE(SUM(i.qty_kg*p.price),0) FROM orders o JOIN order_items i ON i.order_id=o.id JOIN products p ON p.sku=i.sku WHERE o.status IN ('DA_XUAT_KHO','DA_NHAN_HANG')").fetchone()[0],
        "products": prods, "warehouses": wh, "today": t}


# ---------------------------------------------------------------- truy xuất, kiểm định, thu hồi
def trace_lot(c, lid, ship=None):
    """Truy xuất nội bộ. Không có `ship`: chỉ nguồn gốc + danh sách chuyến xuất để chọn (không gộp timeline). Có `ship` (mã chuyến VC-OUT): chỉ hành trình chặng 1 + chuyến đó."""
    r = rows(c, LOT_SQL + " WHERE l.id=?", (today(), lid))
    if not r:
        raise ApiError(404, "Không tìm thấy lô")
    lot = r[0]
    sup = c.execute("SELECT * FROM suppliers WHERE id=?", (lot["supplier_id"],)).fetchone()
    rc = rows(c, """SELECT r.code,r.qty_kg,r.created_at,cu.full_name AS creator,du.full_name AS approver
      FROM receipts r JOIN users cu ON cu.id=r.created_by LEFT JOIN users du ON du.id=r.decided_by WHERE r.lot_id=?""", (lid,))
    shipments = rows(c, """SELECT o.id AS order_id,o.code AS order_code,o.status,cu.name AS customer,SUM(r.qty_kg) AS qty_kg,t.code AS trip_code,t.status AS trip_status
          FROM reservations r JOIN orders o ON o.id=r.order_id JOIN customers cu ON cu.id=o.customer_id LEFT JOIN trips t ON t.order_id=o.id AND t.status<>'HUY'
          WHERE r.lot_id=? GROUP BY o.id ORDER BY o.id""", (lid,))
    for i, x in enumerate(shipments, 1):
        x["no"] = i
    cur = None
    if ship:
        cur = next((x for x in shipments if x["trip_code"] == str(ship)), None)
        if not cur or not lot_out_trip(c, lid, ship):
            raise ApiError(404, "Chuyến xuất kho này không thuộc lô đang xem")
    led = rows(c, "SELECT g.type,g.qty_kg,g.ref,g.created_at,u.full_name FROM stock_ledger g LEFT JOIN users u ON u.id=g.user_id WHERE g.lot_id=? ORDER BY g.id", (lid,))
    led = [g for g in led if g["type"] != "XUAT" or (cur and g["ref"] == "Xuất theo đơn " + cur["order_code"])]  # sổ cái chỉ giữ dòng xuất của chính chuyến đang xem
    return {
        "lot": lot, "supplier": dict(sup), "receipt": rc[0] if rc else None, "ledger": led,
        "shipments": shipments, "selected": cur,
        "journey": public_journey(c, lot={"id": lid, "code": lot["code"], "supplier_id": lot["supplier_id"]}, ship=ship, hide=False),
        "recalls": rows(c, "SELECT id,code,reason,status,resolution,created_at FROM recalls WHERE lot_id=? ORDER BY id DESC", (lid,)),
    }


# ---------------------------------------------------------------- QR và truy xuất công khai
# Mã xác thực (k) = HMAC-SHA256(khóa, mã lô) cắt 12 ký tự hex. Người ngoài không đoán được đường dẫn của lô khác,
# nên không dò được từng mã lô để xem hàng loạt. Khóa mặc định chỉ để demo: khi triển khai thật hãy đặt KHO_QR_SECRET.
QR_SECRET = os.environ.get("KHO_QR_SECRET", "kho-nong-san-xanh-demo")
PUBLIC_VERDICTS = {
    "THU_HOI": ("bad", "Lô này đã bị thu hồi", "Vui lòng không sử dụng và liên hệ nơi mua hàng để được hỗ trợ."),
    "HET_HAN": ("bad", "Lô đã quá hạn sử dụng", "Không nên sử dụng sản phẩm này."),
    "DAT": ("ok", "Đạt kiểm định", "Lô đã qua kiểm tra dư lượng thuốc bảo vệ thực vật và thời gian cách ly."),
    "CHO_KIEM_DINH": ("wait", "Đang chờ kiểm định", "Lô chưa có kết luận chất lượng cuối cùng."),
    "CACH_LY": ("bad", "Đang cách ly để kiểm tra thêm", "Lô chưa được phép bán ra thị trường."),
    "KHONG_DAT": ("bad", "Không đạt kiểm định", "Lô không đủ điều kiện bán ra thị trường."),
}


def qr_token(code, ship=None):
    """Mã xác thực của QR. QR tem xuất kho gắn thêm mã chuyến: HMAC(mã lô | mã chuyến), nên không dùng được cho chuyến khác."""
    msg = code if not ship else "%s|%s" % (code, ship)
    return hmac.new(QR_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()[:12]


def qr_valid(code, k, ship=None):
    return bool(code) and bool(k) and hmac.compare_digest(qr_token(str(code), ship or None), str(k))


def clean_base(v):
    """Chỉ nhận dạng http(s)://tên-miền[:cổng], bỏ mọi thứ khác (chống chèn đường dẫn lạ vào mã QR)."""
    v = str(v or "").strip().rstrip("/")
    return v if re.fullmatch(r"https?://[A-Za-z0-9.\-]+(:\d{1,5})?", v) else ""


def ctx_key(ship=None, order=None):
    """Ngữ cảnh gắn vào QR: mã chuyến (s) hoặc id đơn hàng (order); dùng để ký mã xác thực."""
    if order:
        return "order:%s" % order
    return ship or None


def public_url(base, code, ship=None, order=None):
    if order:
        return "%s/t/%s?order=%s&k=%s" % (base, quote(code, safe=""), quote(str(order), safe=""), qr_token(code, ctx_key(order=order)))
    if ship:
        return "%s/t/%s?s=%s&k=%s" % (base, quote(code, safe=""), quote(ship, safe=""), qr_token(code, ship))
    return "%s/t/%s?k=%s" % (base, quote(code, safe=""), qr_token(code))


def lot_qr(c, lid, base, ship=None):
    l = c.execute("SELECT code FROM lots WHERE id=?", (lid,)).fetchone()
    if not l:
        raise ApiError(404, "Không tìm thấy lô")
    if ship and not lot_out_trip(c, lid, ship):
        raise ApiError(404, "Chuyến xuất kho này không thuộc lô đang xem")
    base = clean_base(os.environ.get("KHO_PUBLIC_URL")) or clean_base(base)
    if not base:
        raise ApiError(400, "Không xác định được địa chỉ công khai của máy chủ")
    url = public_url(base, l["code"], ship)
    host = urlparse(base).hostname or ""
    return {"code": l["code"], "ship": ship or None, "token": qr_token(l["code"], ship), "url": url,
            "svg": qrlib.make_svg(url, label="Mã QR truy xuất lô " + l["code"] + ((" chuyến " + ship) if ship else "")),
            "is_local": host in ("localhost", "127.0.0.1", "::1") or host.endswith(".local")}


def public_lot(c, code, k, ship=None, order=None):
    """Dữ liệu cho người mua: chỉ thông tin sản phẩm. Không có khách hàng, đơn hàng, giá, tồn kho, tên nhân viên.
    Có `ship` (QR tem xuất kho): hành trình = chặng 1 + đúng chuyến đó. Không có (QR nhập kho/QR cũ): chỉ chặng 1."""
    ship = (str(ship).strip() if ship else None) or None
    order = (str(order).strip() if order else None) or None
    if not qr_valid(code, k, ctx_key(ship, order)):
        raise ApiError(404, "Mã QR không hợp lệ hoặc đã bị chỉnh sửa")
    if code.startswith("GH-"):
        return public_slip(c, code)
    r = c.execute("""SELECT l.id,l.code,l.harvest_date,l.expiry_date,l.spray_date,l.phi_days,l.qa_status,l.grade,l.residue,
      p.name AS product_name,p.temp_min,p.temp_max,p.mrl,w.name AS warehouse_name,w.city AS warehouse_city,
      s.name AS supplier_name,s.area AS supplier_area,s.cert,CAST(julianday(l.expiry_date)-julianday(?) AS INTEGER) AS days_left
      FROM lots l JOIN products p ON p.sku=l.sku JOIN warehouses w ON w.id=l.warehouse_id JOIN suppliers s ON s.id=l.supplier_id
      WHERE l.code=?""", (today(), code)).fetchone()
    if not r:
        raise ApiError(404, "Không tìm thấy lô hàng")
    l = dict(r)
    rc = c.execute("SELECT substr(created_at,1,10) AS d FROM receipts WHERE lot_id=? AND status='DA_DUYET'", (l["id"],)).fetchone()
    rcl = c.execute("SELECT status,substr(created_at,1,10) AS d FROM recalls WHERE lot_id=? ORDER BY id DESC", (l["id"],)).fetchall()
    gap = None
    if l["spray_date"]:
        gap = (datetime.date.fromisoformat(l["harvest_date"]) - datetime.date.fromisoformat(l["spray_date"])).days
    if rcl:
        verdict = "THU_HOI"
    elif l["qa_status"] == "DAT" and l["days_left"] < 0:
        verdict = "HET_HAN"
    else:
        verdict = l["qa_status"]
    tone, title, text = PUBLIC_VERDICTS[verdict]
    return {
        "code": l["code"], "product": l["product_name"], "verdict": verdict, "tone": tone, "title": title, "text": text,
        "farm": {"name": l["supplier_name"], "area": l["supplier_area"], "cert": l["cert"]},
        "harvest_date": l["harvest_date"], "expiry_date": l["expiry_date"], "days_left": l["days_left"],
        "spray": {"date": l["spray_date"], "phi_days": l["phi_days"], "gap_days": gap, "enough": None if gap is None else gap >= l["phi_days"]},
        "qa": {"status": l["qa_status"], "grade": l["grade"], "residue": l["residue"], "mrl": l["mrl"]},
        "storage": {"name": l["warehouse_name"], "city": l["warehouse_city"], "received": rc["d"] if rc else None,
                    "temp_min": l["temp_min"], "temp_max": l["temp_max"]},
        "recall": {"date": rcl[0]["d"], "closed": rcl[0]["status"] == "DA_DONG"} if rcl else None,
        "journey": public_journey(c, lot={"id": l["id"], "code": l["code"], "supplier_id": c.execute("SELECT supplier_id FROM lots WHERE id=?", (l["id"],)).fetchone()[0]}, ship=ship, order=order),
        "shipment": (lambda t_: t_["code"] if t_ else None)(lot_out_trip(c, l["id"], ship, order)),
        "today": today(),
    }


def recalls_json(c):
    out = rows(c, """SELECT r.*,l.code AS lot_code,p.name AS product_name,u.full_name AS creator
      FROM recalls r JOIN lots l ON l.id=r.lot_id JOIN products p ON p.sku=l.sku LEFT JOIN users u ON u.id=r.created_by ORDER BY r.id DESC""")
    for r in out:
        r["items"] = rows(c, """SELECT i.*,o.code AS order_code,cu.name AS customer FROM recall_items i
          JOIN orders o ON o.id=i.order_id JOIN customers cu ON cu.id=i.customer_id WHERE i.recall_id=? ORDER BY i.id""", (r["id"],))
    return out


def qa_overview(c):
    t = today()
    sup = rows(c, """SELECT s.id,s.name,s.cert,COUNT(l.id) AS total,
      COALESCE(SUM(l.qa_status='DAT'),0) AS dat,COALESCE(SUM(l.qa_status='CACH_LY'),0) AS cach_ly,
      COALESCE(SUM(l.qa_status='KHONG_DAT'),0) AS khong_dat,COALESCE(SUM(l.qa_status='CHO_KIEM_DINH'),0) AS cho
      FROM suppliers s LEFT JOIN lots l ON l.supplier_id=s.id GROUP BY s.id ORDER BY s.id""")
    for s_ in sup:
        done = s_["dat"] + s_["cach_ly"] + s_["khong_dat"]
        s_["pass_rate"] = round(100.0 * s_["dat"] / done, 1) if done else None
    return {
        "queue": rows(c, LOT_SQL + " WHERE l.qa_status='CHO_KIEM_DINH' AND l.qty_kg>0 ORDER BY l.harvest_date, l.id", (t,)),
        "held": rows(c, LOT_SQL + " WHERE l.qa_status IN ('CACH_LY','KHONG_DAT') AND l.qty_kg>0 ORDER BY l.expiry_date, l.id", (t,)),
        "suppliers": sup, "recalls": recalls_json(c)}


def recall_impact(c, lid):
    lot = rows(c, LOT_SQL + " WHERE l.id=?", (today(), lid))
    if not lot:
        raise ApiError(404, "Không tìm thấy lô")
    items = rows(c, """SELECT o.id AS order_id,o.code AS order_code,o.status,o.customer_id,cu.name AS customer,SUM(r.qty_kg) AS qty_kg
      FROM reservations r JOIN orders o ON o.id=r.order_id JOIN customers cu ON cu.id=o.customer_id
      WHERE r.lot_id=? AND o.status IN ('DA_GIU_HANG','DA_XUAT_KHO','DA_NHAN_HANG') GROUP BY o.id ORDER BY o.id""", (lid,))
    for i in items:
        i["phase"] = "DA_XUAT" if i["status"] in ("DA_XUAT_KHO", "DA_NHAN_HANG") else "DA_NHA_GIU"
    return {"lot": lot[0], "items": items,
            "open_recall": c.execute("SELECT code FROM recalls WHERE lot_id=? AND status='DANG_XU_LY'", (lid,)).fetchone() is not None}


def do_recall(c, u, lid, b):
    reason = str(b.get("reason") or "").strip()
    if len(reason) < 5:
        raise ApiError(400, "Hãy ghi rõ lý do thu hồi (ít nhất 5 ký tự)")
    tx(c)
    try:
        l = c.execute("SELECT * FROM lots WHERE id=?", (lid,)).fetchone()
        if not l:
            raise ApiError(404, "Không tìm thấy lô")
        if c.execute("SELECT 1 FROM recalls WHERE lot_id=? AND status='DANG_XU_LY'", (lid,)).fetchone():
            raise ApiError(409, "Lô này đang có đợt thu hồi chưa đóng")
        n = c.execute("SELECT COUNT(*) FROM recalls").fetchone()[0] + 1
        code = "TH-%04d" % n
        rid = c.execute("INSERT INTO recalls(code,lot_id,reason,qty_at_recall,created_by,created_at) VALUES(?,?,?,?,?,?)",
                        (code, lid, reason, l["qty_kg"], u["id"], now())).lastrowid
        c.execute("UPDATE lots SET qa_status='CACH_LY' WHERE id=?", (lid,))
        held = shipped = 0
        for i in recall_impact_rows(c, lid):
            if i["status"] == "DA_GIU_HANG":
                release(c, i["order_id"])  # nhả toàn bộ hàng đã giữ của đơn, đơn quay về Mới để giữ lại bằng lô khác
                c.execute("UPDATE orders SET status='MOI' WHERE id=?", (i["order_id"],))
                held += 1
            else:
                shipped += 1
            c.execute("INSERT INTO recall_items(recall_id,order_id,customer_id,qty_kg,phase) VALUES(?,?,?,?,?)",
                      (rid, i["order_id"], i["customer_id"], i["qty_kg"], "DA_XUAT" if i["status"] in ("DA_XUAT_KHO", "DA_NHAN_HANG") else "DA_NHA_GIU"))
        audit(c, u["id"], "Thu hồi lô %s (%s): %s. Nhả %d đơn đang giữ, %d đơn đã xuất cần thông báo khách" % (l["code"], code, reason, held, shipped))
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK") if c.in_transaction else None
        raise
    return {"id": rid, "code": code, "held_orders": held, "shipped_orders": shipped}


def recall_impact_rows(c, lid):
    return rows(c, """SELECT o.id AS order_id,o.status,o.customer_id,SUM(r.qty_kg) AS qty_kg FROM reservations r JOIN orders o ON o.id=r.order_id
      WHERE r.lot_id=? AND o.status IN ('DA_GIU_HANG','DA_XUAT_KHO','DA_NHAN_HANG') GROUP BY o.id ORDER BY o.id""", (lid,))


def recall_notify(c, u, rid, b):
    tx(c)
    try:
        r = c.execute("SELECT * FROM recalls WHERE id=?", (rid,)).fetchone()
        if not r:
            raise ApiError(404, "Không tìm thấy đợt thu hồi")
        if r["status"] != "DANG_XU_LY":
            raise ApiError(409, "Đợt thu hồi đã đóng")
        if b.get("all"):
            n = c.execute("UPDATE recall_items SET notified_at=? WHERE recall_id=? AND phase='DA_XUAT' AND notified_at IS NULL", (now(), rid)).rowcount
        else:
            n = c.execute("UPDATE recall_items SET notified_at=? WHERE id=? AND recall_id=? AND phase='DA_XUAT' AND notified_at IS NULL", (now(), b.get("item_id"), rid)).rowcount
        if not n:
            raise ApiError(409, "Không có khách nào cần ghi nhận thêm")
        audit(c, u["id"], "Ghi nhận đã thông báo thu hồi %s cho %d đơn hàng" % (r["code"], n))
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK") if c.in_transaction else None
        raise
    return {"notified": n}


def recall_close(c, u, rid, b):
    res = b.get("resolution")
    if res not in ("HUY", "TRA_NCC"):
        raise ApiError(400, "Hướng xử lý không hợp lệ")
    tx(c)
    try:
        r = c.execute("SELECT * FROM recalls WHERE id=?", (rid,)).fetchone()
        if not r:
            raise ApiError(404, "Không tìm thấy đợt thu hồi")
        if r["status"] != "DANG_XU_LY":
            raise ApiError(409, "Đợt thu hồi đã đóng")
        left = c.execute("SELECT COUNT(*) FROM recall_items WHERE recall_id=? AND phase='DA_XUAT' AND notified_at IS NULL", (rid,)).fetchone()[0]
        if left:
            raise ApiError(409, "Còn %d đơn đã xuất chưa ghi nhận thông báo khách, chưa thể đóng thu hồi" % left)
        l = c.execute("SELECT * FROM lots WHERE id=?", (r["lot_id"],)).fetchone()
        if l["reserved_kg"] > 0:
            raise ApiError(409, "Lô vẫn còn hàng đang giữ cho đơn khác")
        if l["qty_kg"] > 0:
            c.execute("UPDATE lots SET qty_kg=0 WHERE id=?", (l["id"],))
            ledger(c, l["id"], "DIEU_CHINH", -l["qty_kg"], "Thu hồi %s: %s" % (r["code"], "hủy bỏ" if res == "HUY" else "trả nhà cung cấp"), u["id"])
        c.execute("UPDATE recalls SET status='DA_DONG',resolution=?,closed_at=? WHERE id=?", (res, now(), rid))
        audit(c, u["id"], "Đóng thu hồi %s, xử lý: %s (%g kg)" % (r["code"], "hủy bỏ" if res == "HUY" else "trả nhà cung cấp", l["qty_kg"]))
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK") if c.in_transaction else None
        raise
    return {"status": "DA_DONG"}


# ---------------------------------------------------------------- kế hoạch thu hoạch, cân đối cung cầu, giá
PLAN_STATUS_VN = {"DU_KIEN": "Dự kiến", "XAC_NHAN": "Đã xác nhận", "DA_THU_HOACH": "Đã thu hoạch", "DANG_GIAO": "Đang giao hàng",
                  "DA_NHAP": "Đã nhập kho", "HUY": "Đã hủy"}
# Vòng đời: Dự kiến -> Đã xác nhận -> Đã thu hoạch -> Đang giao hàng -> Đã nhập kho (Đã xác nhận hoặc Đã thu hoạch đều lập được phiếu giao)
PLAN_DELIVERABLE = ("XAC_NHAN", "DA_THU_HOACH")


def own_plan(c, u, pid):
    p = c.execute("SELECT * FROM plans WHERE id=?", (pid,)).fetchone()
    if not p:
        raise ApiError(404, "Không tìm thấy kế hoạch")
    if u["role"] != "NONG_DAN" or p["supplier_id"] != u.get("supplier_id"):
        raise ApiError(403, "Bạn chỉ được thao tác trên kế hoạch thu hoạch của chính mình")
    return p


def plan_fields(c, b, p=None):
    sku = b.get("sku", p["sku"] if p else None)
    pr = c.execute("SELECT * FROM products WHERE sku=?", (sku,)).fetchone()
    if not pr:
        raise ApiError(400, "Mặt hàng không tồn tại")
    area = num(b.get("area_ha", p["area_ha"] if p else None), "Diện tích")
    kg = num(b.get("expected_kg", p["expected_kg"] if p else None), "Sản lượng dự kiến")
    d = valid_date(b.get("expected_date", p["expected_date"] if p else None), "Ngày thu hoạch dự kiến")
    if d < today() and not (p and d == p["expected_date"]):
        raise ApiError(400, "Ngày thu hoạch dự kiến không được ở quá khứ")
    note = str(b.get("note", p["note"] if p else "") or "").strip()[:200]
    return pr, area, kg, d, note


def create_plan(c, u, b):
    sid = need_farmer(u)  # kế hoạch luôn thuộc về nông trại của tài khoản đang đăng nhập
    p, area, kg, d, note = plan_fields(c, b)
    tx(c)
    try:
        code = "KH-%04d" % (c.execute("SELECT COALESCE(MAX(id),0) FROM plans").fetchone()[0] + 1)
        pid = c.execute("INSERT INTO plans(code,supplier_id,sku,area_ha,expected_kg,expected_date,note,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (code, sid, p["sku"], area, kg, d, note, u["id"], now())).lastrowid
        audit(c, u["id"], "Lập kế hoạch thu hoạch %s (%g kg %s, dự kiến %s)" % (code, kg, p["name"], d))
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK") if c.in_transaction else None
        raise
    return {"id": pid, "code": code}


def update_plan(c, u, pid, b):
    act_ = b.get("action")
    if act_ not in ("confirm", "edit", "postpone", "cancel", "delete", "harvested"):
        raise ApiError(400, "Thao tác không hợp lệ")
    tx(c)
    try:
        if act_ == "confirm":  # Bước 1: Quản lý kho / Thủ kho chốt lịch thu hoạch với nông dân
            if "xacnhan" not in ROLE_PERMS[u["role"]]:
                raise ApiError(403, "Vai trò \"%s\" không có quyền %s" % (ROLE_NAMES[u["role"]], PERM_NAMES["xacnhan"]))
            p = c.execute("SELECT * FROM plans WHERE id=?", (pid,)).fetchone()
            if not p:
                raise ApiError(404, "Không tìm thấy kế hoạch")
            if p["status"] != "DU_KIEN":
                raise ApiError(409, "Chỉ xác nhận được kế hoạch đang ở trạng thái Dự kiến")
            c.execute("UPDATE plans SET status='XAC_NHAN' WHERE id=?", (pid,))
            msg = "Xác nhận kế hoạch %s" % p["code"]
        else:  # các thao tác còn lại chỉ nông dân, trên kế hoạch của chính mình
            if "kehoach" not in ROLE_PERMS[u["role"]]:
                raise ApiError(403, "Vai trò \"%s\" chỉ được xem kế hoạch thu hoạch, không được %s" % (ROLE_NAMES[u["role"]], {"edit": "sửa", "postpone": "dời ngày", "cancel": "hủy", "delete": "xóa", "harvested": "cập nhật thu hoạch"}[act_]))
            p = own_plan(c, u, pid)
            if p["status"] in ("DA_NHAP", "HUY") and act_ != "delete":
                raise ApiError(409, "Kế hoạch đã kết thúc, không thể thay đổi")
            if p["status"] == "DANG_GIAO" and act_ != "delete":
                raise ApiError(409, "Hàng của kế hoạch này đang giao về kho (đã lập phiếu giao hàng), không thể thay đổi")
            if act_ == "edit":  # đã xác nhận thì khóa sửa: muốn đổi lịch phải dời ngày kèm lý do
                if p["status"] != "DU_KIEN":
                    raise ApiError(409, "Kế hoạch đã được kho xác nhận, không thể sửa. Nếu đổi lịch hãy dùng Dời ngày kèm lý do")
                pr, area, kg, d, note = plan_fields(c, b, p)
                c.execute("UPDATE plans SET sku=?,area_ha=?,expected_kg=?,expected_date=?,note=? WHERE id=?", (pr["sku"], area, kg, d, note, pid))
                msg = "Sửa kế hoạch %s: %g kg %s, dự kiến %s" % (p["code"], kg, pr["name"], d)
            elif act_ == "postpone":
                if p["status"] not in ("DU_KIEN", "XAC_NHAN"):
                    raise ApiError(409, "Kế hoạch đã thu hoạch, không thể dời ngày. Hãy lập phiếu giao hàng")
                d = valid_date(b.get("expected_date"), "Ngày thu hoạch mới")
                if d < today() or d == p["expected_date"]:
                    raise ApiError(400, "Ngày mới phải từ hôm nay trở đi và khác ngày cũ")
                why = str(b.get("reason") or "").strip()[:150]
                if p["status"] == "XAC_NHAN" and not why:
                    raise ApiError(400, "Kế hoạch đã xác nhận: hãy nhập lý do dời ngày")
                back = " (cần kho xác nhận lại)" if p["status"] == "XAC_NHAN" else ""
                c.execute("UPDATE plans SET expected_date=?,postponed=postponed+1,status='DU_KIEN',postpone_reason=? WHERE id=?", (d, why or p["postpone_reason"], pid))
                msg = "Dời kế hoạch %s từ %s sang %s%s%s" % (p["code"], p["expected_date"], d, (": " + why) if why else "", back)
            elif act_ == "harvested":  # Đã xác nhận -> Đã thu hoạch (mở khóa lập phiếu giao như trạng thái Đã xác nhận)
                if p["status"] != "XAC_NHAN":
                    raise ApiError(409, "Chỉ đánh dấu Đã thu hoạch cho kế hoạch đã được kho xác nhận")
                c.execute("UPDATE plans SET status='DA_THU_HOACH' WHERE id=?", (pid,))
                msg = "Kế hoạch %s đã thu hoạch" % p["code"]
            elif act_ == "cancel":
                if p["status"] != "DU_KIEN":
                    raise ApiError(409, "Chỉ hủy được kế hoạch đang ở trạng thái Dự kiến. Kế hoạch đã xác nhận hoặc đã nhập kho không thể hủy")
                why = str(b.get("reason") or "").strip()[:150]
                c.execute("UPDATE plans SET status='HUY',note=? WHERE id=?", (("Hủy: " + why) if why else (p["note"] or ""), pid))
                msg = "Hủy kế hoạch %s%s" % (p["code"], (": " + why) if why else "")
            else:  # delete: không cho phép, chỉ được hủy
                raise ApiError(403, "Nông dân không được xóa kế hoạch thu hoạch. Chỉ hủy được kế hoạch đang ở trạng thái Dự kiến")
        audit(c, u["id"], msg)
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK") if c.in_transaction else None
        raise
    return {"ok": True}


def plans_json(c, u, q):
    """Danh sách kế hoạch kèm giai đoạn thực tế, bộ lọc và tổng hợp theo mặt hàng. Nông dân chỉ thấy kế hoạch của mình."""
    where, args = ["1=1"], []
    if u["role"] == "NONG_DAN":
        where.append("p.supplier_id=?")
        args.append(u.get("supplier_id") or -1)
    elif q.get("supplier"):
        where.append("p.supplier_id=?")
        args.append(q["supplier"])
    if q.get("sku"):
        where.append("p.sku=?")
        args.append(q["sku"])
    if q.get("from"):
        where.append("p.expected_date>=?")
        args.append(valid_date(q["from"], "Ngày bắt đầu"))
    if q.get("to"):
        where.append("p.expected_date<=?")
        args.append(valid_date(q["to"], "Ngày kết thúc"))
    plans = rows(c, """SELECT p.*,s.name AS supplier_name,pr.name AS product_name,
        bt.code AS batch_code,bt.status AS batch_status,r.code AS receipt_code,r.status AS receipt_status,r.qty_kg AS receipt_kg,
        l.code AS lot_code,l.qa_status AS lot_qa
      FROM plans p JOIN suppliers s ON s.id=p.supplier_id JOIN products pr ON pr.sku=p.sku
      LEFT JOIN batches bt ON bt.id=(SELECT MAX(id) FROM batches WHERE plan_id=p.id AND status<>'TU_CHOI')
      LEFT JOIN receipts r ON r.id=bt.receipt_id
      LEFT JOIN lots l ON l.id=COALESCE(p.lot_id,r.lot_id)
      WHERE """ + " AND ".join(where) + """
      ORDER BY CASE WHEN p.status IN ('DU_KIEN','XAC_NHAN','DA_THU_HOACH','DANG_GIAO') THEN 0 ELSE 1 END, p.expected_date, p.id""", args)
    farmer = u["role"] == "NONG_DAN"
    for p in plans:
        p["stage"] = p["status"]
        p["editable"] = farmer and p["status"] == "DU_KIEN"                               # Sửa: chỉ khi Dự kiến
        p["cancelable"] = farmer and p["status"] == "DU_KIEN"                             # Hủy: chỉ khi Dự kiến
        p["can_postpone"] = farmer and p["status"] in ("DU_KIEN", "XAC_NHAN")             # Dời ngày (bắt buộc lý do khi Đã xác nhận)
        p["need_reason"] = p["status"] == "XAC_NHAN"
        p["can_harvest"] = farmer and p["status"] == "XAC_NHAN"
        p["can_deliver"] = farmer and p["status"] in PLAN_DELIVERABLE                    # Tạo phiếu giao
        p["deletable"] = False
        p["can_confirm"] = p["status"] == "DU_KIEN"
    agg = {}
    for p in plans:
        if p["status"] in ("DU_KIEN", "XAC_NHAN", "DA_THU_HOACH", "DANG_GIAO"):
            a = agg.setdefault(p["sku"], {"sku": p["sku"], "name": p["product_name"], "total_kg": 0, "plans": 0, "farms": set(), "first_date": p["expected_date"], "confirmed_kg": 0})
            a["total_kg"] += p["expected_kg"]
            a["plans"] += 1
            a["farms"].add(p["supplier_id"])
            a["first_date"] = min(a["first_date"], p["expected_date"])
            if p["status"] != "DU_KIEN":
                a["confirmed_kg"] += p["expected_kg"]
    summary = sorted(({**a, "farms": len(a["farms"])} for a in agg.values()), key=lambda a: -a["total_kg"])
    return {"plans": plans, "summary": summary, "balance": [] if farmer else supply_balance(c), "du_factor": DU_FACTOR,
            "scope": "own" if farmer else "all"}


DU_FACTOR = 8  # dư thừa khi tồn dự kiến vượt 8 lần mức tối thiểu


def supply_balance(c):
    t, end = today(), add_days(today(), 14)
    out = []
    for p in rows(c, "SELECT sku,name,min_stock FROM products ORDER BY sku"):
        av = available(c, p["sku"])
        inc = c.execute("SELECT COALESCE(SUM(expected_kg),0) FROM plans WHERE sku=? AND ((status IN ('DU_KIEN','XAC_NHAN') AND expected_date BETWEEN ? AND ?) OR status IN ('DA_THU_HOACH','DANG_GIAO'))", (p["sku"], t, end)).fetchone()[0]
        dem = c.execute("SELECT COALESCE(SUM(i.qty_kg),0) FROM order_items i JOIN orders o ON o.id=i.order_id WHERE i.sku=? AND o.status='MOI'", (p["sku"],)).fetchone()[0]
        proj = av + inc - dem
        st = "THIEU" if proj < p["min_stock"] else "DU" if proj > DU_FACTOR * p["min_stock"] else "CAN_BANG"
        out.append({**p, "available": av, "incoming": inc, "demand": dem, "projected": proj, "status": st})
    return out


def price_overview(c):
    t = today()
    summary = []
    for p in rows(c, "SELECT sku,name,price FROM products ORDER BY sku"):
        series = rows(c, "SELECT price_date AS d,CAST(ROUND(AVG(price)) AS INTEGER) AS p FROM market_prices WHERE sku=? AND kind='THI_TRUONG' AND price_date>=? AND price_date<=? GROUP BY price_date ORDER BY price_date",
                      (p["sku"], add_days(t, -30), t))
        last = series[-1] if series else None
        old = [x for x in series if x["d"] <= add_days(last["d"], -7)] if last else []
        buy = c.execute("SELECT price,price_date FROM market_prices WHERE sku=? AND kind='THU_MUA' ORDER BY price_date DESC,id DESC LIMIT 1", (p["sku"],)).fetchone()
        mk = rows(c, """SELECT m.market,m.price,m.price_date FROM market_prices m WHERE m.sku=? AND m.kind='THI_TRUONG'
          AND m.id=(SELECT id FROM market_prices WHERE sku=m.sku AND kind=m.kind AND market=m.market ORDER BY price_date DESC,id DESC LIMIT 1) ORDER BY m.market""", (p["sku"],))
        summary.append({"sku": p["sku"], "name": p["name"], "sell_price": p["price"], "market_avg": last["p"] if last else None,
                        "market_date": last["d"] if last else None,
                        "change7": round(100.0 * (last["p"] - old[-1]["p"]) / old[-1]["p"], 1) if last and old else None,
                        "buy_price": buy["price"] if buy else None, "buy_date": buy["price_date"] if buy else None,
                        "markets": mk, "series": series})
    recent = rows(c, """SELECT m.id,m.sku,p.name,m.kind,m.market,m.price,m.price_date,u.full_name AS who FROM market_prices m JOIN products p ON p.sku=m.sku
      LEFT JOIN users u ON u.id=m.created_by ORDER BY m.price_date DESC, m.id DESC LIMIT 30""")
    return {"summary": summary, "recent": recent, "markets": MARKETS}


def add_price(c, u, b):
    if not c.execute("SELECT 1 FROM products WHERE sku=?", (b.get("sku"),)).fetchone():
        raise ApiError(400, "Mặt hàng không tồn tại")
    if b.get("kind") not in ("THI_TRUONG", "THU_MUA"):
        raise ApiError(400, "Loại giá không hợp lệ")
    mk = str(b.get("market") or "").strip()[:80]
    if not mk:
        raise ApiError(400, "Hãy nhập tên chợ hoặc nguồn giá")
    price = int(num(b.get("price"), "Giá"))
    d = valid_date(b.get("price_date") or today(), "Ngày áp dụng")
    if d > today():
        raise ApiError(400, "Ngày áp dụng không được ở tương lai")
    tx(c)
    try:
        c.execute("INSERT INTO market_prices(sku,kind,market,price,price_date,created_by,created_at) VALUES(?,?,?,?,?,?,?)", (b["sku"], b["kind"], mk, price, d, u["id"], now()))
        audit(c, u["id"], "Cập nhật giá %s %s tại %s: %d ₫/kg" % ("thị trường" if b["kind"] == "THI_TRUONG" else "thu mua", b["sku"], mk, price))
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK") if c.in_transaction else None
        raise
    return {"ok": True}


def edit_price(c, u, pid, b):
    tx(c)
    try:
        r = c.execute("SELECT * FROM market_prices WHERE id=?", (pid,)).fetchone()
        if not r:
            raise ApiError(404, "Không tìm thấy dòng giá")
        mk = str(b.get("market", r["market"]) or "").strip()[:80]
        if not mk:
            raise ApiError(400, "Hãy nhập tên chợ hoặc nguồn giá")
        price = int(num(b.get("price", r["price"]), "Giá"))
        d = valid_date(b.get("price_date", r["price_date"]), "Ngày áp dụng")
        if d > today():
            raise ApiError(400, "Ngày áp dụng không được ở tương lai")
        c.execute("UPDATE market_prices SET market=?,price=?,price_date=? WHERE id=?", (mk, price, d, pid))
        audit(c, u["id"], "Sửa giá %s %s tại %s: %d ₫ → %d ₫/kg" % ("thị trường" if r["kind"] == "THI_TRUONG" else "thu mua", r["sku"], mk, r["price"], price))
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK") if c.in_transaction else None
        raise
    return {"ok": True}


def delete_price(c, u, pid):
    tx(c)
    try:
        r = c.execute("SELECT * FROM market_prices WHERE id=?", (pid,)).fetchone()
        if not r:
            raise ApiError(404, "Không tìm thấy dòng giá")
        c.execute("DELETE FROM market_prices WHERE id=?", (pid,))
        audit(c, u["id"], "Xóa giá %s %s tại %s (%d ₫/kg, ngày %s)" % ("thị trường" if r["kind"] == "THI_TRUONG" else "thu mua", r["sku"], r["market"], r["price"], r["price_date"]))
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK") if c.in_transaction else None
        raise
    return {"ok": True}


def req_overview(c):
    items = rows(c, "SELECT * FROM requirements ORDER BY id")
    by_status, by_pri, who = {}, {}, {}
    for r in items:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        by_pri.setdefault(r["pri"], {}).setdefault(r["status"], 0)
        by_pri[r["pri"]][r["status"]] += 1
        w = who.setdefault(r["who"], {"who": r["who"], "total": 0, "DA_LAM": 0, "MOT_PHAN": 0, "HUONG": 0})
        w["total"] += 1
        w[r["status"]] += 1
    return {"summary": {"total": len(items), "by_status": by_status, "by_pri": by_pri, "by_who": list(who.values())}, "items": items}


# ---------------------------------------------------------------- HTTP
# ---------------------------------------------------------------- chuỗi cung ứng 3 bên: Kinh doanh <-> Kho <-> Nông dân
from contextlib import contextmanager

STANDARDS = ["Bất kỳ", "VietGAP", "GlobalGAP", "Hữu cơ"]
# Thời gian tối thiểu từ lúc chốt kèo đến lúc có hàng (ngày). Số liệu minh họa của nhóm, chỉnh theo thực tế.
GROW_DAYS = {"SLX": 75, "XCL": 45, "NRT": 14, "CRT": 85, "KLT": 100, "CCB": 70}
L2_FACTOR = 0.85  # giá Loại 2 bằng 85% giá thỏa thuận
STAGES = {"CHUA_BAT_DAU": "Chưa bắt đầu", "GIEO_TRONG": "Đã gieo trồng", "CHAM_SOC": "Đang chăm sóc",
          "SAP_THU_HOACH": "Sắp thu hoạch", "DA_THU_HOACH": "Đã thu hoạch"}
FARM_USERS = [("nongdan", "Võ Văn Nam", 1), ("nongdan2", "Huỳnh Thị Hoa", 2)]

# ---- Hủy kèo theo tiến độ canh tác (số liệu minh họa của nhóm, chỉnh tại đây)
STAGE_ORDER = list(STAGES)                 # chỉ được cập nhật tiến độ tiến lên, không lùi lại (tránh lách luật hủy kèo)
CANCEL_FREE_DAYS = 10                      # "Đang chăm sóc": tự hủy được nếu còn cách hạn giao (Giao trước) trên số ngày này (7–10 ngày)
ALLOW_REQUEST_AT_HARVEST = False           # "Sắp thu hoạch": False = khóa cứng; True = cho Xin hủy kèo và phạt Mức 2
CANCEL_REASONS = ["Sâu bệnh", "Thiên tai", "Mất mùa", "Lý do khác"]
CANCEL_PENALTY = {                         # mức phạt khi Doanh nghiệp duyệt yêu cầu hủy (không miễn trừ)
    "CHAM_SOC":      {"level": 1, "rep": 10, "lock_days": 3, "cap_pct": None, "cap_days": 0},
    "SAP_THU_HOACH": {"level": 2, "rep": 25, "lock_days": 7, "cap_pct": 30, "cap_days": 30},
}
MAX_PHOTOS, MAX_PHOTO_BYTES = 5, 1_500_000
MAX_BODY = 9_000_000                      # giới hạn mỗi yêu cầu POST (đủ cho 5 ảnh)

CHAIN_SCHEMA = """
CREATE TABLE IF NOT EXISTS offers(
  id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
  sku TEXT NOT NULL REFERENCES products(sku), qty_kg REAL NOT NULL CHECK(qty_kg>0), price INTEGER NOT NULL CHECK(price>0),
  harvest_date TEXT NOT NULL, standard TEXT NOT NULL DEFAULT 'Bất kỳ', note TEXT,
  status TEXT NOT NULL DEFAULT 'CHO_DUYET' CHECK(status IN ('CHO_DUYET','CHO_GIAO','DA_GIAO','TU_CHOI','HUY')),
  agreed_price INTEGER, decided_by INTEGER REFERENCES users(id), created_by INTEGER REFERENCES users(id), created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rfqs(
  id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, customer_id INTEGER NOT NULL REFERENCES customers(id),
  sku TEXT NOT NULL REFERENCES products(sku), qty_kg REAL NOT NULL CHECK(qty_kg>0), standard TEXT NOT NULL DEFAULT 'Bất kỳ',
  price_min INTEGER NOT NULL, price_max INTEGER NOT NULL, deliver_by TEXT NOT NULL, deposit_pct REAL NOT NULL DEFAULT 10,
  note TEXT, status TEXT NOT NULL DEFAULT 'MO' CHECK(status IN ('MO','DU_SL','HOAN_TAT','HUY')),
  order_id INTEGER REFERENCES orders(id), settled_at TEXT, created_by INTEGER REFERENCES users(id), created_at TEXT NOT NULL,
  CHECK(price_min<=price_max));
CREATE TABLE IF NOT EXISTS commits(
  id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, rfq_id INTEGER NOT NULL REFERENCES rfqs(id),
  supplier_id INTEGER NOT NULL REFERENCES suppliers(id), qty_kg REAL NOT NULL CHECK(qty_kg>0), price INTEGER NOT NULL,
  deposit REAL NOT NULL DEFAULT 0, deposit_status TEXT NOT NULL DEFAULT 'CHO' CHECK(deposit_status IN ('CHO','DA_COC','HOAN','MAT_COC')),
  status TEXT NOT NULL DEFAULT 'CHO_DUYET' CHECK(status IN ('CHO_DUYET','DA_DUYET','CHO_DUYET_HUY','TU_CHOI','HUY','DA_GIAO')),
  stage TEXT NOT NULL DEFAULT 'CHUA_BAT_DAU', stage_note TEXT, stage_at TEXT, decided_by INTEGER REFERENCES users(id), created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS batches(
  id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
  sku TEXT NOT NULL REFERENCES products(sku), offer_id INTEGER REFERENCES offers(id), commit_id INTEGER REFERENCES commits(id),
  warehouse_id INTEGER NOT NULL REFERENCES warehouses(id), qty_kg REAL NOT NULL CHECK(qty_kg>0), harvest_date TEXT NOT NULL,
  spray_date TEXT, phi_days INTEGER NOT NULL DEFAULT 0, vehicle TEXT, price INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'CHO_GIAO' CHECK(status IN ('CHO_GIAO','DA_NHAP','TU_CHOI')),
  qty_l1 REAL, qty_l2 REAL, qty_bad REAL, residue REAL, meets_standard INTEGER, qc_note TEXT, amount REAL, lot_codes TEXT,
  qc_by INTEGER REFERENCES users(id), qc_at TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cancel_requests(
  id INTEGER PRIMARY KEY, commit_id INTEGER NOT NULL REFERENCES commits(id), reason TEXT NOT NULL, detail TEXT NOT NULL, photos TEXT NOT NULL,
  stage TEXT NOT NULL, level INTEGER NOT NULL, days_left INTEGER,
  status TEXT NOT NULL DEFAULT 'CHO' CHECK(status IN ('CHO','DUYET','TU_CHOI')), waived INTEGER NOT NULL DEFAULT 0, penalty TEXT, decision_note TEXT,
  decided_by INTEGER REFERENCES users(id), decided_at TEXT, created_by INTEGER REFERENCES users(id), created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS payments(
  id INTEGER PRIMARY KEY, batch_id INTEGER NOT NULL REFERENCES batches(id), kind TEXT NOT NULL CHECK(kind IN ('THANH_TOAN','HOAN_COC')),
  amount REAL NOT NULL, note TEXT, created_by INTEGER REFERENCES users(id), created_at TEXT NOT NULL);
"""

USERS_DDL = """CREATE TABLE %s(
  id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, full_name TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('QUAN_LY','THU_KHO','QA','KINH_DOANH','NONG_DAN')),
  salt TEXT NOT NULL, pass_hash TEXT NOT NULL, supplier_id INTEGER REFERENCES suppliers(id));"""


@contextmanager
def txn(c):
    tx(c)
    try:
        yield
        c.execute("COMMIT")
    except BaseException:
        if c.in_transaction:
            c.execute("ROLLBACK")
        raise


def ensure_chain(c):
    """Nâng cấp CSDL: thêm vai trò Nông dân, các bảng mới, tài khoản và dữ liệu mẫu. Chạy lại nhiều lần vẫn an toàn."""
    sql = c.execute("SELECT sql FROM sqlite_master WHERE name='users'").fetchone()[0]
    if "NONG_DAN" not in sql:  # SQLite không sửa được CHECK, nên dựng lại bảng users (giữ nguyên dữ liệu)
        c.execute("PRAGMA foreign_keys=OFF")
        c.executescript(USERS_DDL % "users_new" + "INSERT INTO users_new(id,username,full_name,role,salt,pass_hash) "
                        "SELECT id,username,full_name,role,salt,pass_hash FROM users; DROP TABLE users; ALTER TABLE users_new RENAME TO users;")
        c.execute("PRAGMA foreign_keys=ON")
    c.executescript(CHAIN_SCHEMA)
    if "CHO_DUYET_HUY" not in c.execute("SELECT sql FROM sqlite_master WHERE name='commits'").fetchone()[0]:
        # CSDL cũ: SQLite không sửa được CHECK, dựng lại bảng commits (giữ nguyên dữ liệu) để thêm trạng thái "Chờ duyệt hủy"
        ddl = re.search(r"CREATE TABLE IF NOT EXISTS commits\(.*?\);\n(?=CREATE)", CHAIN_SCHEMA, re.S).group(0).replace("IF NOT EXISTS commits(", "commits_new(", 1)
        c.execute("PRAGMA foreign_keys=OFF")
        c.executescript(ddl + "INSERT INTO commits_new SELECT * FROM commits; DROP TABLE commits; ALTER TABLE commits_new RENAME TO commits;")
        c.execute("PRAGMA foreign_keys=ON")
    add_column(c, "suppliers", "reputation", "reputation INTEGER NOT NULL DEFAULT 100")  # điểm uy tín hồ sơ nông trại
    add_column(c, "suppliers", "blocked_until", "blocked_until TEXT")                      # khóa nhận kèo mới đến ngày này
    add_column(c, "suppliers", "cap_until", "cap_until TEXT")                              # giới hạn sản lượng đến ngày này
    add_column(c, "suppliers", "cap_pct", "cap_pct REAL")                                  # tối đa % sản lượng mỗi RFQ trong thời gian giới hạn
    for u, n, sid in FARM_USERS:
        if not c.execute("SELECT 1 FROM users WHERE username=?", (u,)).fetchone():
            salt = secrets.token_hex(16)
            c.execute("INSERT INTO users(username,full_name,role,salt,pass_hash,supplier_id) VALUES(?,?,?,?,?,?)", (u, n, "NONG_DAN", salt, hash_pw("123456", salt), sid))
    if c.execute("SELECT 1 FROM offers LIMIT 1").fetchone() or c.execute("SELECT 1 FROM rfqs LIMIT 1").fetchone():
        return
    t = today()
    f1, f2 = [c.execute("SELECT id FROM users WHERE username=?", (u,)).fetchone()[0] for u in ("nongdan", "nongdan2")]
    kd = c.execute("SELECT id FROM users WHERE username='kinhdoanh'").fetchone()
    kd = kd[0] if kd else 1
    c.execute("INSERT INTO offers(code,supplier_id,sku,qty_kg,price,harvest_date,standard,note,status,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
              ("CB-0001", 1, "SLX", 500, 36000, add_days(t, 2), "VietGAP", "Súp lơ đợt đầu, hái sáng sớm", "CHO_DUYET", f1, now()))
    c.execute("INSERT INTO offers(code,supplier_id,sku,qty_kg,price,agreed_price,harvest_date,standard,status,decided_by,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
              ("CB-0002", 2, "XCL", 800, 62000, 62000, add_days(t, -1), "GlobalGAP", "CHO_GIAO", 1, f2, now()))
    c.execute("INSERT INTO batches(code,supplier_id,sku,offer_id,warehouse_id,qty_kg,harvest_date,spray_date,phi_days,vehicle,price,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
              ("GH-%s-001" % t.replace("-", "")[2:], 2, "XCL", 2, 2, 800, add_days(t, -1), add_days(t, -25), 10, "63C-112.34", 62000, now()))
    c.execute("INSERT INTO rfqs(code,customer_id,sku,qty_kg,standard,price_min,price_max,deliver_by,deposit_pct,note,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
              ("RFQ-0001", 1, "CRT", 1500, "VietGAP", 20000, 26000, add_days(t, 95), 10, "Giao đủ trong một đợt, đóng túi 1 kg", kd, now()))
    c.execute("INSERT INTO commits(code,rfq_id,supplier_id,qty_kg,price,deposit,deposit_status,status,stage,stage_note,stage_at,decided_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
              ("CK-0001", 1, 1, 800, 23000, 800 * 23000 * 0.1, "DA_COC", "DA_DUYET", "CHAM_SOC", "Cây 3 tuần tuổi, phát triển tốt", now(), kd, now()))
    c.execute("INSERT INTO commits(code,rfq_id,supplier_id,qty_kg,price,deposit,created_at) VALUES(?,?,?,?,?,?,?)",
              ("CK-0002", 1, 3, 500, 24000, 500 * 24000 * 0.1, now()))
    audit(c, None, "Nạp dữ liệu mẫu cho chuỗi cung ứng 3 bên")


def need_farmer(u):
    if not u.get("supplier_id"):
        raise ApiError(403, "Tài khoản nông dân chưa gắn với nhà cung cấp nào")
    return u["supplier_id"]


def std_ok(cert, std):
    return std in ("", "Bất kỳ") or cert == std or cert == "Hữu cơ"


def next_code(c, prefix, table):
    return "%s-%04d" % (prefix, c.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0] + 1)


def product_of(c, sku):
    p = c.execute("SELECT * FROM products WHERE sku=?", (sku,)).fetchone()
    if not p:
        raise ApiError(400, "Mặt hàng không tồn tại")
    return p


# ---- Nông dân: chào bán
def create_offer(c, u, b):
    sid = need_farmer(u)
    product_of(c, b.get("sku"))
    qty, price = num(b.get("qty_kg"), "Sản lượng"), int(num(b.get("price"), "Giá mong muốn"))
    h = valid_date(b.get("harvest_date"), "Ngày thu hoạch")
    if h < add_days(today(), -7) or h > add_days(today(), 180):
        raise ApiError(400, "Ngày thu hoạch phải trong khoảng từ 7 ngày trước đến 180 ngày tới")
    std = b.get("standard") or "Bất kỳ"
    if std not in STANDARDS:
        raise ApiError(400, "Tiêu chuẩn chất lượng không hợp lệ")
    with txn(c):
        code = next_code(c, "CB", "offers")
        cur = c.execute("INSERT INTO offers(code,supplier_id,sku,qty_kg,price,harvest_date,standard,note,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (code, sid, b["sku"], qty, price, h, std, str(b.get("note") or "")[:200], u["id"], now()))
        audit(c, u["id"], "Nông dân đăng chào bán %s (%g kg %s)" % (code, qty, b["sku"]))
    return {"id": cur.lastrowid, "code": code}


def decide_offer(c, u, oid, b):
    act = b.get("action")
    if act not in ("approve", "reject"):
        raise ApiError(400, "Hành động không hợp lệ")
    with txn(c):
        o = c.execute("SELECT * FROM offers WHERE id=?", (oid,)).fetchone()
        if not o:
            raise ApiError(404, "Không tìm thấy chào bán")
        if o["status"] != "CHO_DUYET":
            raise ApiError(409, "Doanh nghiệp (Kinh doanh) đã chốt mua lô chào bán này, kho không duyệt lại được" if o["buyer_type"] == "business"
                           else "Chào bán đã được xử lý trước đó")
        price = int(num(b.get("agreed_price") or o["price"], "Giá chốt"))
        c.execute("UPDATE offers SET status=?,agreed_price=?,decided_by=? WHERE id=?",
                  ("CHO_GIAO" if act == "approve" else "TU_CHOI", price if act == "approve" else None, u["id"], oid))
        audit(c, u["id"], "%s chào bán %s" % ("Duyệt chốt mua" if act == "approve" else "Từ chối", o["code"]))
    return {"status": "CHO_GIAO" if act == "approve" else "TU_CHOI"}


def cancel_offer(c, u, oid):
    sid = need_farmer(u)
    with txn(c):
        o = c.execute("SELECT * FROM offers WHERE id=? AND supplier_id=?", (oid, sid)).fetchone()
        if not o:
            raise ApiError(404, "Không tìm thấy chào bán của bạn")
        if o["status"] not in ("CHO_DUYET", "CHO_GIAO"):
            raise ApiError(409, "Chào bán này không thể hủy nữa")
        if c.execute("SELECT 1 FROM batches WHERE offer_id=? AND status<>'TU_CHOI'", (oid,)).fetchone():
            raise ApiError(409, "Đã có phiếu giao hàng cho chào bán này, không thể hủy")
        c.execute("UPDATE offers SET status='HUY' WHERE id=?", (oid,))
        audit(c, u["id"], "Nông dân hủy chào bán " + o["code"])
    return {"status": "HUY"}


OFFER_SQL = "SELECT o.*,p.name AS product_name,s.name AS supplier_name,s.cert FROM offers o JOIN products p ON p.sku=o.sku JOIN suppliers s ON s.id=o.supplier_id"
TRIP_BRIEF = ("id", "code", "status", "status_label", "progress", "eta", "current_location_name", "plate", "driver", "distance_traveled_km", "total_km",
              "near_arrival", "signal_lost", "temp_check")


def trip_brief(c, t, u):
    j = trip_view(c, t, u)
    return {k: j.get(k) for k in TRIP_BRIEF}


def batch_stage(c, u, b):
    """Trạng thái hành trình của một phiếu giao thuộc đơn Kinh doanh mua trực tiếp: (mã, nhãn, màu)."""
    tin = c.execute("SELECT * FROM trips WHERE batch_id=?", (b["id"],)).fetchone()
    if b["status"] == "TU_CHOI":
        return ("QC_KHONG_DAT", "QC không đạt, hàng bị từ chối nhập kho", "bad"), tin and trip_brief(c, tin, u), None, None
    if b["status"] == "CHO_GIAO":
        st = tin["status"] if tin else "CHO_LAY_HANG"
        stage = {"CHO_LAY_HANG": ("CHO_LAY", "Chặng 1: chờ nông dân xuất phát", "info"), "DANG_VAN_CHUYEN": ("VC_CHANG1", "Chặng 1: nông dân đang vận chuyển đến kho", "info"),
                 "DA_GIAO": ("CHO_QC", "Hàng đã đến kho, chờ kiểm định QC", "warn"), "SU_CO": ("SU_CO", "Chặng 1 gặp sự cố", "bad")}.get(st, ("CHO_LAY", "Chặng 1: chờ nông dân xuất phát", "info"))
        return stage, tin and trip_brief(c, tin, u), None, None
    order = c.execute("SELECT id,code,status FROM orders WHERE id=?", (b["xdock_order_id"],)).fetchone() if b["xdock_order_id"] else None
    tout = c.execute("SELECT * FROM trips WHERE order_id=? AND status<>'HUY'", (order["id"],)).fetchone() if order else None
    if not order:
        lots = [r["qa_status"] for r in c.execute("SELECT qa_status FROM lots WHERE code IN (%s)" % ",".join("?" * len((b["lot_codes"] or "x").split(","))), (b["lot_codes"] or "x").split(",")).fetchall()]
        if any(x in ("CACH_LY", "CHO_KIEM_DINH") for x in lots):
            stage = ("CACH_LY", "Lô bị cách ly, chờ QA kết luận", "warn")
        elif lots and all(x == "KHONG_DAT" for x in lots):
            stage = ("QC_KHONG_DAT", "QC không đạt, không giao về cửa hàng", "bad")
        else:
            stage = ("DANG_XU_LY", "Đang điều phối giao hàng", "info")
        return stage, tin and trip_brief(c, tin, u), None, None
    if order["status"] == "DA_NHAN_HANG":
        stage = ("DA_NHAN", "Đã nhận hàng tại cửa hàng", "ok")
    else:
        st = tout["status"] if tout else "CHO_LAY_HANG"
        stage = {"CHO_LAY_HANG": ("CHO_GIAO2", "Chặng 2: kho đã xuất, chờ tài xế nhận chuyến", "info"), "DANG_VAN_CHUYEN": ("VC_CHANG2", "Chặng 2: đang giao từ kho về cửa hàng", "info"),
                 "DA_GIAO": ("DA_DEN", "Hàng đã đến cửa hàng, chờ bạn xác nhận nhận hàng", "warn"), "SU_CO": ("SU_CO", "Chặng 2 gặp sự cố", "bad")}.get(st, ("CHO_GIAO2", "Chặng 2: kho đã xuất", "info"))
    return stage, tin and trip_brief(c, tin, u), dict(order), tout and trip_brief(c, tout, u)


def offer_flow(c, u, o):
    """Tiến trình thu mua trực tiếp của một chào bán (dùng cho tài khoản Kinh doanh đã mua)."""
    items = []
    for b in c.execute("SELECT * FROM batches WHERE offer_id=? ORDER BY id", (o["id"],)).fetchall():
        stage, tin, order, tout = batch_stage(c, u, b)
        lots = [dict(r) for r in c.execute("SELECT code,grade,qa_status FROM lots WHERE code IN (%s)" % ",".join("?" * len((b["lot_codes"] or "x").split(","))), (b["lot_codes"] or "x").split(",")).fetchall()]
        items.append({"code": b["code"], "status": b["status"], "qty_kg": b["qty_kg"], "qty_l1": b["qty_l1"], "qty_l2": b["qty_l2"], "qty_bad": b["qty_bad"],
                      "qc_note": b["qc_note"], "notice": b["xdock_note"], "stage": {"code": stage[0], "label": stage[1], "tone": stage[2]},
                      "trip_in": tin, "order": order, "trip_out": tout, "lots": lots})
    live = [x for x in items if x["status"] != "TU_CHOI"]
    if o["status"] == "HUY":
        stage = {"code": "HUY", "label": "Nông dân đã hủy chào bán", "tone": "mute"}
    elif live:
        stage = live[-1]["stage"]
    else:
        stage = {"code": "CHO_NONG_DAN", "label": "Chờ nông dân lập phiếu giao hàng", "tone": "info"}
    return {"stage": stage, "batches": items}


def offers_json(c, u):
    if u["role"] == "NONG_DAN":
        rs = rows(c, OFFER_SQL + " WHERE o.supplier_id=? ORDER BY o.id DESC", (need_farmer(u),))
        for o in rs:  # nông dân biết hàng đã được doanh nghiệp thu mua và có thông báo QC, nhưng không thấy danh tính cửa hàng
            for k in ("buyer_id", "dest_customer_id", "dest_store_info", "decided_by", "created_by"):
                o.pop(k, None)
            nt = c.execute("SELECT xdock_note FROM batches WHERE offer_id=? AND xdock_note IS NOT NULL ORDER BY id DESC LIMIT 1", (o["id"],)).fetchone()
            o["notice"] = nt["xdock_note"] if nt and o["buyer_type"] == "business" else None
        return rs
    if u["role"] == "KINH_DOANH":
        # Chỉ thấy lô còn đang chào bán (chưa ai chốt) và các lô do chính mình đã mua. Lô kho đã duyệt hoặc nông dân hủy sẽ biến mất ở lần tải sau.
        rs = rows(c, OFFER_SQL + " WHERE o.status='CHO_DUYET' OR (o.buyer_type='business' AND o.buyer_id=?) ORDER BY CASE o.status WHEN 'CHO_DUYET' THEN 0 ELSE 1 END, o.id DESC", (u["id"],))
        for o in rs:
            o["mine"] = o["buyer_type"] == "business" and o["buyer_id"] == u["id"]
            o["dest_store"] = json.loads(o["dest_store_info"]) if o["mine"] and o["dest_store_info"] else None
            o["flow"] = offer_flow(c, u, o) if o["mine"] else None
            for k in ("buyer_id", "dest_customer_id", "dest_store_info", "decided_by", "created_by"):
                o.pop(k, None)
        return rs
    rs = rows(c, OFFER_SQL + " ORDER BY CASE o.status WHEN 'CHO_DUYET' THEN 0 WHEN 'CHO_GIAO' THEN 1 ELSE 2 END, o.id DESC")
    for o in rs:
        o["dest_store"] = json.loads(o["dest_store_info"]) if o["dest_store_info"] else None
    return rs


def buy_offer(c, u, oid, b):
    """Kinh doanh chốt mua lô chào bán của nông dân theo đúng giá chào bán (không thương lượng giá). Chỉ một bên chốt được: kho hoặc Kinh doanh."""
    cu = c.execute("SELECT * FROM customers WHERE id=?", (b.get("customer_id"),)).fetchone()
    if not cu:
        raise ApiError(400, "Hãy chọn cửa hàng nhận hàng")
    with txn(c):
        o = c.execute("SELECT * FROM offers WHERE id=?", (oid,)).fetchone()
        if not o:
            raise ApiError(404, "Không tìm thấy chào bán")
        if o["status"] != "CHO_DUYET":
            raise ApiError(409, "Lô hàng này không còn chào bán: đã có bên chốt mua hoặc nông dân đã hủy")
        if b.get("price") not in (None, "") and int(num(b.get("price"), "Giá mua")) != o["price"]:
            raise ApiError(409, "Giá chào bán đã thay đổi hoặc không khớp (%s ₫/kg). Hệ thống chỉ hỗ trợ mua theo đúng giá chào bán của nông dân" % format(o["price"], ","))
        info = json.dumps({"customer_id": cu["id"], "name": cu["name"], "address": cu["address"] or ""}, ensure_ascii=False)
        if c.execute("UPDATE offers SET status='CHO_GIAO',agreed_price=price,buyer_type='business',buyer_id=?,dest_customer_id=?,dest_store_info=?,bought_at=?,decided_by=? "
                     "WHERE id=? AND status='CHO_DUYET'", (u["id"], cu["id"], info, now(), u["id"], oid)).rowcount != 1:
            raise ApiError(409, "Lô hàng này vừa được bên khác chốt mua")
        audit(c, u["id"], "Kinh doanh chốt mua trực tiếp chào bán %s (%g kg) giá %s ₫/kg, giao thẳng về %s sau khi kho kiểm định" % (o["code"], o["qty_kg"], format(o["price"], ","), cu["name"]))
    return {"status": "CHO_GIAO", "code": o["code"], "agreed_price": o["price"], "dest_store": cu["name"]}


# ---- Nông dân: phiếu giao hàng (batch) và QR
def src_left(c, kind, sid_):
    col = "offer_id" if kind == "o" else "commit_id"
    return c.execute("SELECT COALESCE(SUM(qty_kg),0) FROM batches WHERE %s=? AND status<>'TU_CHOI'" % col, (sid_,)).fetchone()[0]


def create_batch(c, u, b):
    sid = need_farmer(u)
    kind, _, ident = str(b.get("source") or "").partition(":")
    if not ident.isdigit() or kind not in ("o", "c", "p"):
        raise ApiError(400, "Hãy chọn kế hoạch, chào bán hoặc kèo thu mua cần giao")
    ident = int(ident)
    if kind == "p":  # nguồn Kế hoạch thu hoạch (KH-...): đơn giá chưa chốt, kho áp Giá thu mua tại thời điểm duyệt nhập kho
        s = c.execute("SELECT * FROM plans WHERE id=? AND supplier_id=?", (ident, sid)).fetchone()
        if not s:
            raise ApiError(404, "Không tìm thấy kế hoạch thu hoạch của nông trại bạn")
        if s["status"] not in PLAN_DELIVERABLE:
            raise ApiError(409, "Kế hoạch %s đang ở trạng thái %s. Chỉ lập phiếu giao từ kế hoạch Đã xác nhận hoặc Đã thu hoạch" % (s["code"], PLAN_STATUS_VN.get(s["status"], s["status"])))
        price = 0
    elif kind == "o":
        s = c.execute("SELECT * FROM offers WHERE id=? AND supplier_id=?", (ident, sid)).fetchone()
        if not s or s["status"] != "CHO_GIAO":
            raise ApiError(409, "Chào bán chưa được kho duyệt chốt mua, hoặc đã giao xong")
        price = s["agreed_price"] or s["price"]
    else:
        s = c.execute("SELECT cm.*,r.sku FROM commits cm JOIN rfqs r ON r.id=cm.rfq_id WHERE cm.id=? AND cm.supplier_id=?", (ident, sid)).fetchone()
        if not s or s["status"] != "DA_DUYET":
            raise ApiError(409, "Kèo chưa được doanh nghiệp duyệt, hoặc đã giao xong")
        price = s["price"]
    qty = num(b.get("qty_kg"), "Khối lượng giao")
    if kind != "p":
        left = s["qty_kg"] - src_left(c, kind, ident)
        if qty > left + 1e-9:
            raise ApiError(409, "Vượt quá phần còn phải giao (%.0f kg)" % left)
    p = product_of(c, s["sku"])
    h = valid_date(b.get("harvest_date"), "Ngày thu hoạch")
    if h > today():
        raise ApiError(400, "Ngày thu hoạch không được sau ngày hôm nay")
    if add_days(h, p["shelf_days"]) < today():
        raise ApiError(400, "Hàng đã quá hạn sử dụng, không thể giao")
    w = c.execute("SELECT * FROM warehouses WHERE id=?", (b.get("warehouse_id"),)).fetchone()
    if not w:
        raise ApiError(400, "Kho nhận không tồn tại")
    if w["temp_max"] < p["temp_min"] or w["temp_min"] > p["temp_max"]:
        raise ApiError(409, "%s (%g–%g°C) không phù hợp bảo quản %s (%g–%g°C)" % (w["name"], w["temp_min"], w["temp_max"], p["name"], p["temp_min"], p["temp_max"]))
    spray = b.get("spray_date") or None
    if spray:
        valid_date(spray, "Ngày phun thuốc")
    phi = int(num(b.get("phi_days") or 0, "Thời gian cách ly", False))
    with txn(c):
        if kind == "p":  # khóa kế hoạch ngay trong giao dịch: mỗi kế hoạch chỉ có một phiếu giao đang xử lý
            if c.execute("UPDATE plans SET status='DANG_GIAO' WHERE id=? AND status IN ('XAC_NHAN','DA_THU_HOACH')", (ident,)).rowcount != 1:
                raise ApiError(409, "Kế hoạch đã có phiếu giao hàng đang xử lý")
        n = c.execute("SELECT COUNT(*) FROM batches").fetchone()[0] + 1
        code = "GH-%s-%03d" % (today().replace("-", "")[2:], n)
        cur = c.execute("INSERT INTO batches(code,supplier_id,sku,offer_id,commit_id,plan_id,warehouse_id,qty_kg,harvest_date,spray_date,phi_days,vehicle,price,created_at,driver_name,driver_phone) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (code, sid, s["sku"], ident if kind == "o" else None, ident if kind == "c" else None, ident if kind == "p" else None, w["id"], qty, h, spray, phi,
                         str(b.get("vehicle") or "").strip()[:20], price, now(), str(b.get("driver_name") or "").strip()[:40] or None, clean_phone(b.get("driver_phone")) or None))
        create_trip_in(c, cur.lastrowid, u["id"])
        audit(c, u["id"], "Nông dân lập phiếu giao hàng %s (%g kg %s)%s" % (code, qty, s["sku"], (" từ kế hoạch " + s["code"]) if kind == "p" else ""))
    t = c.execute("SELECT code FROM trips WHERE batch_id=?", (cur.lastrowid,)).fetchone()
    return {"id": cur.lastrowid, "code": code, "trip_code": t["code"] if t else None}


BATCH_SQL = """SELECT b.*,p.name AS product_name,s.name AS supplier_name,w.name AS warehouse_name,o.code AS offer_code,
  cm.code AS commit_code,cm.deposit,cm.deposit_status,r.code AS rfq_code,r.standard AS rfq_standard,
  pl.code AS plan_code,pl.expected_kg AS plan_expected_kg,rc.code AS receipt_code,rc.status AS receipt_status,rc.qty_kg AS receipt_kg,
  o.buyer_type AS buyer_type,o.buyer_id AS buyer_id,o.dest_store_info AS dest_store_info,xo.code AS xdock_order_code,xo.status AS xdock_order_status,
  COALESCE((SELECT SUM(amount) FROM payments WHERE batch_id=b.id AND kind='THANH_TOAN'),0) AS paid
  FROM batches b JOIN products p ON p.sku=b.sku JOIN suppliers s ON s.id=b.supplier_id JOIN warehouses w ON w.id=b.warehouse_id
  LEFT JOIN offers o ON o.id=b.offer_id LEFT JOIN commits cm ON cm.id=b.commit_id LEFT JOIN rfqs r ON r.id=cm.rfq_id
  LEFT JOIN plans pl ON pl.id=b.plan_id LEFT JOIN receipts rc ON rc.id=b.receipt_id LEFT JOIN orders xo ON xo.id=b.xdock_order_id"""


def batches_json(c, u):
    if u["role"] == "NONG_DAN":
        bs = rows(c, BATCH_SQL + " WHERE b.supplier_id=? ORDER BY b.id DESC", (need_farmer(u),))
    elif u["role"] == "KINH_DOANH":  # Kinh doanh chỉ thấy phiếu giao của các lô mình đã mua trực tiếp
        bs = rows(c, BATCH_SQL + " WHERE o.buyer_type='business' AND o.buyer_id=? ORDER BY b.id DESC", (u["id"],))
    else:
        bs = rows(c, BATCH_SQL + " ORDER BY CASE b.status WHEN 'CHO_GIAO' THEN 0 ELSE 1 END, b.id DESC")
    for x in bs:
        x["dest_store"] = json.loads(x["dest_store_info"]) if x["dest_store_info"] and u["role"] != "NONG_DAN" else None
        x.pop("dest_store_info", None)
        if u["role"] != "QUAN_LY" and u["role"] != "THU_KHO":
            x.pop("buyer_id", None)
        x["payments"] = rows(c, "SELECT * FROM payments WHERE batch_id=? ORDER BY id", (x["id"],))
        ok = (x["qty_l1"] or 0) + (x["qty_l2"] or 0)
        x["loss_pct"] = round(max(0.0, (x["qty_kg"] - ok) / x["qty_kg"] * 100), 1) if x["status"] != "CHO_GIAO" and not x["plan_id"] else None
        x["due"] = round((x["amount"] or 0) - x["paid"]) if x["status"] == "DA_NHAP" else 0
        x["source_code"] = x["plan_code"] or x["offer_code"] or x["commit_code"]
        x["debt_status"] = None if x["status"] != "DA_NHAP" else ("CHO_THANH_TOAN" if x["due"] > 0 else "DA_THANH_TOAN")
        # điều kiện cách ly thuốc BVTV: (ngày thu hoạch - ngày phun cuối) >= thời gian cách ly
        x["gap_days"] = (datetime.date.fromisoformat(x["harvest_date"]) - datetime.date.fromisoformat(x["spray_date"])).days if x["spray_date"] else None
        x["phi_ok"] = x["gap_days"] is None or x["gap_days"] >= x["phi_days"]
        x["plan_flow"] = bool(x["plan_id"])
        tr = c.execute("SELECT * FROM trips WHERE batch_id=?", (x["id"],)).fetchone()
        if tr:
            j = trip_json(c, tr)
            if u["role"] == "QA":  # QA không có tab Vận chuyển nhưng vẫn biết rủi ro nhiệt độ để lưu ý khi kiểm định
                x["cold_alert"] = j["temp_check"]
            else:
                x["trip"] = {k: j[k] for k in ("id", "code", "status", "status_label", "progress", "eta", "temp_check", "signal_lost", "near_arrival",
                                               "current_location_name", "plate", "driver", "distance_traveled_km", "total_km")}
    return bs


def batch_qr(c, u, bid, base):
    sql, args = "SELECT code,supplier_id FROM batches WHERE id=?", (bid,)
    r = c.execute(sql, args).fetchone()
    if not r or (u["role"] == "NONG_DAN" and r["supplier_id"] != u.get("supplier_id")):
        raise ApiError(404, "Không tìm thấy phiếu giao hàng")
    base = clean_base(os.environ.get("KHO_PUBLIC_URL")) or clean_base(base)
    if not base:
        raise ApiError(400, "Không xác định được địa chỉ công khai của máy chủ")
    url = public_url(base, r["code"])
    host = urlparse(base).hostname or ""
    return {"code": r["code"], "url": url, "svg": qrlib.make_svg(url, label="Mã QR phiếu giao hàng " + r["code"]),
            "is_local": host in ("localhost", "127.0.0.1", "::1") or host.endswith(".local")}


# ---- Kho: kiểm định QC, nhập kho, công nợ
def lot_code(c, sku, harvest):
    seq = c.execute("SELECT COUNT(*) FROM lots WHERE sku=? AND harvest_date=?", (sku, harvest)).fetchone()[0] + 1
    while True:
        code = "%s-%s-%02d" % (sku, harvest.replace("-", "")[2:], seq)
        if not c.execute("SELECT 1 FROM lots WHERE code=?", (code,)).fetchone():
            return code
        seq += 1


def receive_batch(c, u, bid, b):
    l1, l2 = num(b.get("qty_l1") or 0, "Khối lượng Loại 1", False), num(b.get("qty_l2") or 0, "Khối lượng Loại 2", False)
    bad = num(b.get("qty_bad") or 0, "Khối lượng hư hỏng", False)
    if min(l1, l2, bad) < 0:
        raise ApiError(400, "Khối lượng không được âm")
    res = num(b.get("residue") if b.get("residue") not in (None, "") else 0, "Dư lượng", False)
    meets = bool(b.get("meets_standard"))
    with txn(c):
        bt = c.execute("SELECT b.*,p.mrl,p.shelf_days,p.name AS pname,s.cert FROM batches b JOIN products p ON p.sku=b.sku JOIN suppliers s ON s.id=b.supplier_id WHERE b.id=?", (bid,)).fetchone()
        if not bt:
            raise ApiError(404, "Không tìm thấy phiếu giao hàng")
        if bt["status"] != "CHO_GIAO":
            raise ApiError(409, "Phiếu này đã được kiểm định trước đó")
        if bt["plan_id"]:
            raise ApiError(409, "Phiếu giao nguồn Kế hoạch (%s) đi theo luồng Tạo phiếu nhập kho rồi Quản lý kho duyệt" % bt["code"])
        good = l1 + l2
        if good + bad > bt["qty_kg"] * 1.1 + 1e-9:
            raise ApiError(409, "Tổng khối lượng cân (%.0f kg) vượt quá phiếu giao (%.0f kg) hơn 10%%, hãy kiểm tra lại" % (good + bad, bt["qty_kg"]))
        if add_days(bt["harvest_date"], bt["shelf_days"]) < today() and good > 0:
            raise ApiError(409, "Hàng đã quá hạn sử dụng, chỉ có thể ghi nhận là hư hỏng")
        w = c.execute("SELECT * FROM warehouses WHERE id=?", (bt["warehouse_id"],)).fetchone()
        used = c.execute("SELECT COALESCE(SUM(qty_kg),0) FROM lots WHERE warehouse_id=?", (w["id"],)).fetchone()[0] + c.execute("SELECT COALESCE(SUM(qty_kg),0) FROM receipts WHERE warehouse_id=? AND status='CHO_DUYET'", (w["id"],)).fetchone()[0]
        if used + good > w["capacity_kg"]:
            raise ApiError(409, "%s không đủ sức chứa (còn trống %.0f kg)" % (w["name"], w["capacity_kg"] - used))
        phi_ok = not bt["spray_date"] or (datetime.date.fromisoformat(bt["harvest_date"]) - datetime.date.fromisoformat(bt["spray_date"])).days >= bt["phi_days"]
        ok = meets and res <= bt["mrl"] and phi_ok
        codes = []
        for grade, q in (("Loại 1", l1), ("Loại 2", l2)):
            if q <= 0:
                continue
            code = lot_code(c, bt["sku"], bt["harvest_date"])
            lid = c.execute("INSERT INTO lots(code,sku,supplier_id,warehouse_id,harvest_date,expiry_date,spray_date,phi_days,qty_kg,qa_status,grade,residue,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (code, bt["sku"], bt["supplier_id"], bt["warehouse_id"], bt["harvest_date"], add_days(bt["harvest_date"], bt["shelf_days"]),
                             bt["spray_date"], bt["phi_days"], q, "DAT" if ok else "CACH_LY", grade, res, now())).lastrowid
            ledger(c, lid, "NHAP", q, "Phiếu giao %s (%s)" % (bt["code"], grade), u["id"])
            codes.append(code)
        amount = round(l1 * bt["price"] + l2 * bt["price"] * L2_FACTOR)
        c.execute("UPDATE batches SET status=?,qty_l1=?,qty_l2=?,qty_bad=?,residue=?,meets_standard=?,qc_note=?,amount=?,lot_codes=?,qc_by=?,qc_at=? WHERE id=?",
                  ("DA_NHAP" if good > 0 else "TU_CHOI", l1, l2, bad, res, int(meets), str(b.get("note") or "")[:200], amount, ",".join(codes), u["id"], now(), bid))
        if bt["offer_id"] and good > 0:
            o = c.execute("SELECT qty_kg FROM offers WHERE id=?", (bt["offer_id"],)).fetchone()
            if src_left(c, "o", bt["offer_id"]) >= o["qty_kg"] - 1e-9:
                c.execute("UPDATE offers SET status='DA_GIAO' WHERE id=?", (bt["offer_id"],))
        close_inbound(c, bt["id"], good > 0, str(b.get("note") or "").strip() or "Không có khối lượng đạt, phiếu giao không được nhập kho", u["id"])
        if bt["offer_id"] and c.execute("SELECT 1 FROM offers WHERE id=? AND buyer_type='business'", (bt["offer_id"],)).fetchone():
            if good <= 0:
                c.execute("UPDATE batches SET xdock_note=? WHERE id=?", ("Hàng không đạt QC, kho từ chối nhập. Nông dân giao lại hoặc hủy chào bán.", bid))
            elif not ok:
                why = "không đúng tiêu chuẩn đã ghi nhận" if not meets else "dư lượng vượt giới hạn" if res > bt["mrl"] else "chưa đủ thời gian cách ly"
                c.execute("UPDATE batches SET xdock_note=? WHERE id=?", ("Lô bị cách ly vì %s, chờ QA kết luận. Chỉ giao về cửa hàng khi QA kết luận Đạt." % why, bid))
            else:
                xdock_try(c, u, bid)  # đạt QC: tự tạo đơn xuất thẳng chặng 2 về cửa hàng của Kinh doanh
        audit(c, u["id"], "QC phiếu giao %s: Loại 1 %g kg, Loại 2 %g kg, hỏng %g kg%s" % (bt["code"], l1, l2, bad, "" if ok else " (lô bị cách ly chờ QA)"))
    xo = c.execute("SELECT o.code FROM batches b JOIN orders o ON o.id=b.xdock_order_id WHERE b.id=?", (bid,)).fetchone()
    return {"status": "DA_NHAP" if good > 0 else "TU_CHOI", "lots": codes, "amount": amount, "quarantined": (not ok) and good > 0,
            "crossdock_order": xo["code"] if xo else None,
            "reason": None if ok else ("không đúng tiêu chuẩn đã ghi nhận" if not meets else "dư lượng vượt giới hạn" if res > bt["mrl"] else "chưa đủ thời gian cách ly")}


def plan_batch(c, bid):
    bt = c.execute("SELECT b.*,p.name AS pname,p.shelf_days FROM batches b JOIN products p ON p.sku=b.sku WHERE b.id=?", (bid,)).fetchone()
    if not bt:
        raise ApiError(404, "Không tìm thấy phiếu giao hàng")
    if not bt["plan_id"]:
        raise ApiError(409, "Phiếu giao này thuộc luồng chào bán hoặc kèo thu mua, hãy dùng chức năng Cân đo và kiểm định")
    if bt["status"] != "CHO_GIAO":
        raise ApiError(409, "Phiếu giao hàng đã được xử lý trước đó")
    return bt


def batch_receipt(c, u, bid, b):
    """Bước 4: Thủ kho tiếp nhận phiếu giao nguồn Kế hoạch, nhập khối lượng cân thực tế và lập Phiếu nhập kho (Chờ duyệt)."""
    qty = num(b.get("qty_kg"), "Khối lượng cân thực tế")
    tx(c)
    try:
        bt = plan_batch(c, bid)
        if bt["receipt_id"]:
            raise ApiError(409, "Phiếu giao này đã có phiếu nhập kho đang chờ Quản lý kho duyệt")
        gap = (datetime.date.fromisoformat(bt["harvest_date"]) - datetime.date.fromisoformat(bt["spray_date"])).days if bt["spray_date"] else None
        if gap is not None and gap < bt["phi_days"]:
            raise ApiError(409, "Vi phạm thời gian cách ly: thu hoạch sau khi phun thuốc %d ngày, yêu cầu tối thiểu %d ngày. Hãy từ chối nhập lô này" % (gap, bt["phi_days"]))
        if add_days(bt["harvest_date"], bt["shelf_days"]) < today():
            raise ApiError(409, "Hàng đã quá hạn sử dụng, không thể nhập kho. Hãy từ chối nhập lô này")
        if qty > bt["qty_kg"] * 1.1 + 1e-9:
            raise ApiError(409, "Khối lượng cân (%.0f kg) vượt phiếu giao (%.0f kg) hơn 10%%, hãy cân kiểm tra lại" % (qty, bt["qty_kg"]))
        pl = c.execute("SELECT * FROM plans WHERE id=?", (bt["plan_id"],)).fetchone()
        if not pl or pl["status"] != "DANG_GIAO":
            raise ApiError(409, "Kế hoạch liên kết không còn ở trạng thái Đang giao hàng")
        ci = capacity_info(c, bt["warehouse_id"])
        if qty > ci["free_kg"] + 1e-9:
            raise ApiError(409, "%s không đủ sức chứa: vượt %.0f kg (còn trống %.0f kg)" % (ci["name"], qty - ci["free_kg"], max(ci["free_kg"], 0)))
        n = c.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] + 1
        code = "PN-%s-%03d" % (today().replace("-", "")[2:], n)
        cur = c.execute("INSERT INTO receipts(code,sku,supplier_id,warehouse_id,qty_kg,harvest_date,spray_date,phi_days,created_by,created_at,plan_id,batch_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (code, bt["sku"], bt["supplier_id"], bt["warehouse_id"], qty, bt["harvest_date"], bt["spray_date"], bt["phi_days"], u["id"], now(), bt["plan_id"], bt["id"]))
        c.execute("UPDATE batches SET receipt_id=? WHERE id=?", (cur.lastrowid, bt["id"]))
        close_inbound(c, bt["id"], True, uid=u["id"])
        audit(c, u["id"], "Tiếp nhận phiếu giao %s, lập phiếu nhập %s (%g kg %s, dự kiến %g kg)" % (bt["code"], code, qty, bt["pname"], pl["expected_kg"]))
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK") if c.in_transaction else None
        raise
    return {"id": cur.lastrowid, "code": code}


def batch_refuse(c, u, bid, b):
    """Bước 4 (nhánh từ chối): phiếu giao -> Không nhập, kế hoạch trả về Đã thu hoạch để nông dân xử lý lại."""
    why = str(b.get("reason") or "").strip()[:150]
    if not why:
        raise ApiError(400, "Hãy nhập lý do từ chối nhập")
    tx(c)
    try:
        bt = plan_batch(c, bid)
        if bt["receipt_id"]:
            raise ApiError(409, "Phiếu giao đã có phiếu nhập chờ duyệt. Hãy nhờ Quản lý kho từ chối phiếu nhập trước")
        c.execute("UPDATE batches SET status='TU_CHOI',qc_note=?,qc_by=?,qc_at=? WHERE id=?", (why, u["id"], now(), bid))
        c.execute("UPDATE plans SET status='DA_THU_HOACH' WHERE id=? AND status='DANG_GIAO'", (bt["plan_id"],))
        close_inbound(c, bt["id"], False, why, u["id"])
        audit(c, u["id"], "Từ chối nhập phiếu giao %s: %s. Kế hoạch trở về Đã thu hoạch" % (bt["code"], why))
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK") if c.in_transaction else None
        raise
    return {"status": "TU_CHOI"}


def pay_batch(c, u, bid):
    with txn(c):
        bt = c.execute("SELECT * FROM batches WHERE id=?", (bid,)).fetchone()
        if not bt or bt["status"] != "DA_NHAP":
            raise ApiError(409, "Chỉ thanh toán được phiếu đã nhập kho")
        paid = c.execute("SELECT COALESCE(SUM(amount),0) FROM payments WHERE batch_id=? AND kind='THANH_TOAN'", (bid,)).fetchone()[0]
        due = round(bt["amount"] - paid)
        if due <= 0:
            raise ApiError(409, "Phiếu này đã thanh toán đủ")
        c.execute("INSERT INTO payments(batch_id,kind,amount,note,created_by,created_at) VALUES(?,?,?,?,?,?)", (bid, "THANH_TOAN", due, "Thanh toán theo số lượng thực nhập", u["id"], now()))
        refund = 0
        if bt["commit_id"]:
            cm = c.execute("SELECT * FROM commits WHERE id=?", (bt["commit_id"],)).fetchone()
            if cm["deposit_status"] == "DA_COC":
                refund = cm["deposit"]
                c.execute("INSERT INTO payments(batch_id,kind,amount,note,created_by,created_at) VALUES(?,?,?,?,?,?)", (bid, "HOAN_COC", refund, "Hoàn tiền cọc giữ cam kết " + cm["code"], u["id"], now()))
                c.execute("UPDATE commits SET deposit_status='HOAN' WHERE id=?", (cm["id"],))
            if src_left(c, "c", cm["id"]) >= cm["qty_kg"] - 1e-9:
                c.execute("UPDATE commits SET status='DA_GIAO' WHERE id=?", (cm["id"],))
        audit(c, u["id"], "Thanh toán công nợ phiếu %s: %s đ" % (bt["code"], format(due, ",")))
    return {"paid": due, "refund": refund}


# ---- Kinh doanh: RFQ, duyệt kèo, đơn xuất kho, nhận hàng
def create_rfq(c, u, b):
    p = product_of(c, b.get("sku"))
    if not c.execute("SELECT 1 FROM customers WHERE id=?", (b.get("customer_id"),)).fetchone():
        raise ApiError(400, "Doanh nghiệp không tồn tại")
    qty = num(b.get("qty_kg"), "Sản lượng cần mua")
    pmin, pmax = int(num(b.get("price_min"), "Giá thấp nhất")), int(num(b.get("price_max"), "Giá cao nhất"))
    if pmin > pmax:
        raise ApiError(400, "Giá thấp nhất không được lớn hơn giá cao nhất")
    std = b.get("standard") or "Bất kỳ"
    if std not in STANDARDS:
        raise ApiError(400, "Tiêu chuẩn không hợp lệ")
    d = valid_date(b.get("deliver_by"), "Ngày cần giao")
    need = GROW_DAYS.get(b["sku"], 30)
    if d < add_days(today(), need):
        raise ApiError(409, "%s cần tối thiểu %d ngày từ lúc gieo trồng đến thu hoạch, ngày giao sớm nhất là %s" % (p["name"], need, add_days(today(), need)))
    dep = num(b.get("deposit_pct") if b.get("deposit_pct") not in (None, "") else 10, "Tỷ lệ cọc", False)
    if not 0 <= dep <= 50:
        raise ApiError(400, "Tỷ lệ cọc phải từ 0 đến 50%")
    with txn(c):
        code = next_code(c, "RFQ", "rfqs")
        cur = c.execute("INSERT INTO rfqs(code,customer_id,sku,qty_kg,standard,price_min,price_max,deliver_by,deposit_pct,note,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (code, b["customer_id"], b["sku"], qty, std, pmin, pmax, d, dep, str(b.get("note") or "")[:200], u["id"], now()))
        audit(c, u["id"], "Đăng yêu cầu thu mua %s (%g kg %s)" % (code, qty, b["sku"]))
    return {"id": cur.lastrowid, "code": code}


def rfq_committed(c, rid):
    return c.execute("SELECT COALESCE(SUM(qty_kg),0) FROM commits WHERE rfq_id=? AND status IN ('DA_DUYET','CHO_DUYET_HUY','DA_GIAO')", (rid,)).fetchone()[0]


def commit_rfq(c, u, rid, b):
    sid = need_farmer(u)
    qty = num(b.get("qty_kg"), "Sản lượng nhận")
    price = int(num(b.get("price"), "Giá bán"))
    with txn(c):
        r = c.execute("SELECT * FROM rfqs WHERE id=?", (rid,)).fetchone()
        if not r or r["status"] != "MO":
            raise ApiError(409, "Yêu cầu thu mua này không còn mở")
        s = c.execute("SELECT * FROM suppliers WHERE id=?", (sid,)).fetchone()
        if s["blocked_until"] and today() < s["blocked_until"]:
            raise ApiError(409, "Nông trại của bạn đang bị khóa quyền nhận kèo mới đến hết ngày %s do hủy kèo muộn" % add_days(s["blocked_until"], -1))
        if s["cap_until"] and today() < s["cap_until"] and s["cap_pct"]:
            cap = r["qty_kg"] * s["cap_pct"] / 100
            have = c.execute("SELECT COALESCE(SUM(qty_kg),0) FROM commits WHERE rfq_id=? AND supplier_id=? AND status IN ('CHO_DUYET','DA_DUYET','CHO_DUYET_HUY','DA_GIAO')", (rid, sid)).fetchone()[0]
            if have + qty > cap + 1e-9:
                raise ApiError(409, "Do hủy kèo muộn, đến hết ngày %s bạn chỉ được nhận tối đa %g%% sản lượng mỗi yêu cầu (%.0f kg, bạn đã nhận %.0f kg)" % (add_days(s["cap_until"], -1), s["cap_pct"], cap, have))
        if not std_ok(s["cert"], r["standard"]):
            raise ApiError(409, "Yêu cầu cần chứng nhận %s, nông trại của bạn đang có %s" % (r["standard"], s["cert"]))
        left = r["qty_kg"] - rfq_committed(c, rid)
        if qty > left + 1e-9:
            raise ApiError(409, "Chỉ còn thiếu %.0f kg, bạn không thể nhận %.0f kg" % (left, qty))
        if not r["price_min"] <= price <= r["price_max"]:
            raise ApiError(409, "Giá phải trong khoảng %s–%s đ/kg" % (format(r["price_min"], ","), format(r["price_max"], ",")))
        if c.execute("SELECT 1 FROM commits WHERE rfq_id=? AND supplier_id=? AND status='CHO_DUYET'", (rid, sid)).fetchone():
            raise ApiError(409, "Bạn đang có một kèo chờ duyệt cho yêu cầu này")
        code = next_code(c, "CK", "commits")
        cur = c.execute("INSERT INTO commits(code,rfq_id,supplier_id,qty_kg,price,deposit,created_at) VALUES(?,?,?,?,?,?,?)",
                        (code, rid, sid, qty, price, round(qty * price * r["deposit_pct"] / 100), now()))
        audit(c, u["id"], "Nông dân nhận kèo %s cho %s (%g kg)" % (code, r["code"], qty))
    return {"id": cur.lastrowid, "code": code}


def decide_commit(c, u, cid, b):
    act = b.get("action")
    if act not in ("approve", "reject"):
        raise ApiError(400, "Hành động không hợp lệ")
    with txn(c):
        cm = c.execute("SELECT * FROM commits WHERE id=?", (cid,)).fetchone()
        if not cm:
            raise ApiError(404, "Không tìm thấy kèo")
        if cm["status"] != "CHO_DUYET":
            raise ApiError(409, "Kèo đã được xử lý trước đó")
        r = c.execute("SELECT * FROM rfqs WHERE id=?", (cm["rfq_id"],)).fetchone()
        if act == "reject":
            c.execute("UPDATE commits SET status='TU_CHOI',decided_by=? WHERE id=?", (u["id"], cid))
        else:
            if r["status"] != "MO" or cm["qty_kg"] > r["qty_kg"] - rfq_committed(c, r["id"]) + 1e-9:
                raise ApiError(409, "Yêu cầu thu mua đã đủ sản lượng, không thể duyệt thêm kèo này")
            c.execute("UPDATE commits SET status='DA_DUYET',deposit_status='DA_COC',decided_by=?,stage_at=? WHERE id=?", (u["id"], now(), cid))
            if rfq_committed(c, r["id"]) >= r["qty_kg"] - 1e-9:
                c.execute("UPDATE rfqs SET status='DU_SL' WHERE id=?", (r["id"],))
        audit(c, u["id"], "%s kèo %s của %s" % ("Duyệt" if act == "approve" else "Từ chối", cm["code"], r["code"]))
    return {"status": "DA_DUYET" if act == "approve" else "TU_CHOI"}


def set_progress(c, u, cid, b):
    sid = need_farmer(u)
    if b.get("stage") not in STAGES:
        raise ApiError(400, "Giai đoạn không hợp lệ")
    with txn(c):
        cm = c.execute("SELECT * FROM commits WHERE id=? AND supplier_id=?", (cid, sid)).fetchone()
        if cm and cm["status"] == "CHO_DUYET_HUY":
            raise ApiError(409, "Kèo đang chờ doanh nghiệp duyệt hủy, chưa thể cập nhật tiến độ")
        if not cm or cm["status"] != "DA_DUYET":
            raise ApiError(409, "Chỉ cập nhật tiến độ cho kèo đã được duyệt")
        if STAGE_ORDER.index(b["stage"]) < STAGE_ORDER.index(cm["stage"]):
            raise ApiError(409, "Tiến độ canh tác chỉ được cập nhật tiến lên, không lùi về \"%s\"" % STAGES[b["stage"]])
        c.execute("UPDATE commits SET stage=?,stage_note=?,stage_at=? WHERE id=?", (b["stage"], str(b.get("note") or "")[:200], now(), cid))
        audit(c, u["id"], "Cập nhật tiến độ %s: %s" % (cm["code"], STAGES[b["stage"]]))
    return {"stage": b["stage"]}


def days_left(deliver_by):
    return (datetime.date.fromisoformat(deliver_by) - datetime.date.fromisoformat(today())).days


def cancel_policy(c, cm, r):
    """Quyết định nông dân được hủy kèo bằng cách nào, tùy trạng thái và tiến độ canh tác.
    mode: direct (tự hủy) | request (xin hủy, doanh nghiệp duyệt, bị phạt) | pending (đang chờ duyệt) | locked (khóa cứng) | none (không còn hủy được)."""
    if cm["status"] == "CHO_DUYET":
        return {"mode": "direct", "reason": ""}
    if cm["status"] == "CHO_DUYET_HUY":
        return {"mode": "pending", "reason": "Đang chờ doanh nghiệp duyệt yêu cầu hủy"}
    if cm["status"] != "DA_DUYET":
        return {"mode": "none", "reason": ""}
    if c.execute("SELECT 1 FROM batches WHERE commit_id=? AND status<>'TU_CHOI'", (cm["id"],)).fetchone():
        return {"mode": "locked", "reason": "Đã có phiếu giao hàng cho kèo này, không thể hủy"}
    st, left = cm["stage"], days_left(r["deliver_by"])
    if st in ("CHUA_BAT_DAU", "GIEO_TRONG"):
        return {"mode": "direct", "reason": "", "days_left": left}
    if st == "CHAM_SOC":
        if left > CANCEL_FREE_DAYS:
            return {"mode": "direct", "reason": "", "days_left": left}
        return {"mode": "request", "level": 1, "days_left": left, "penalty": CANCEL_PENALTY["CHAM_SOC"],
                "reason": "Còn %d ngày đến hạn giao (cần trên %d ngày mới được tự hủy). Bạn chỉ có thể xin hủy và chờ doanh nghiệp duyệt" % (max(left, 0), CANCEL_FREE_DAYS)}
    if st == "SAP_THU_HOACH" and ALLOW_REQUEST_AT_HARVEST:
        return {"mode": "request", "level": 2, "days_left": left, "penalty": CANCEL_PENALTY["SAP_THU_HOACH"], "reason": "Kèo sắp thu hoạch, chỉ được xin hủy (phạt Mức 2)"}
    return {"mode": "locked", "reason": "Kèo ở giai đoạn \"%s\" nên không thể hủy" % STAGES.get(st, st)}


def cancel_commit(c, u, cid):
    sid = need_farmer(u)
    with txn(c):
        cm = c.execute("SELECT * FROM commits WHERE id=? AND supplier_id=?", (cid, sid)).fetchone()
        if not cm or cm["status"] not in ("CHO_DUYET", "DA_DUYET", "CHO_DUYET_HUY"):
            raise ApiError(409, "Không thể hủy kèo này")
        r = c.execute("SELECT * FROM rfqs WHERE id=?", (cm["rfq_id"],)).fetchone()
        pol = cancel_policy(c, cm, r)
        if pol["mode"] != "direct":
            raise ApiError(409, pol["reason"] or "Không thể tự hủy kèo này")
        lost = cm["status"] == "DA_DUYET"
        c.execute("UPDATE commits SET status='HUY',deposit_status=? WHERE id=?", ("MAT_COC" if lost else cm["deposit_status"], cid))
        if lost:
            c.execute("UPDATE rfqs SET status='MO' WHERE id=? AND status='DU_SL'", (cm["rfq_id"],))
        audit(c, u["id"], "Nông dân hủy kèo %s%s" % (cm["code"], " (mất tiền cọc)" if lost else ""))
    return {"status": "HUY", "lost_deposit": lost}


def check_photos(photos):
    import base64
    if not isinstance(photos, list) or not photos:
        raise ApiError(400, "Bắt buộc đính kèm ảnh chụp thực tế tại vườn")
    if len(photos) > MAX_PHOTOS:
        raise ApiError(400, "Tối đa %d ảnh" % MAX_PHOTOS)
    magic = {"image/jpeg": b"\xff\xd8\xff", "image/png": b"\x89PNG", "image/webp": b"RIFF"}
    for p in photos:
        m = re.match(r"^data:(image/(?:jpeg|png|webp));base64,([A-Za-z0-9+/=]+)$", p if isinstance(p, str) else "")
        if not m:
            raise ApiError(400, "Ảnh không hợp lệ (chỉ nhận JPG, PNG, WebP)")
        try:
            raw = base64.b64decode(m.group(2), validate=True)
        except ValueError:
            raise ApiError(400, "Ảnh không hợp lệ")
        if len(raw) > MAX_PHOTO_BYTES:
            raise ApiError(400, "Mỗi ảnh tối đa %.1f MB" % (MAX_PHOTO_BYTES / 1e6))
        if not raw.startswith(magic[m.group(1)]):
            raise ApiError(400, "Nội dung ảnh không đúng định dạng")
    return photos


def request_cancel(c, u, cid, b):
    sid = need_farmer(u)
    reason, detail = b.get("reason"), str(b.get("detail") or "").strip()
    if reason not in CANCEL_REASONS:
        raise ApiError(400, "Hãy chọn lý do hủy: " + ", ".join(CANCEL_REASONS))
    if len(detail) < 10:
        raise ApiError(400, "Hãy mô tả chi tiết tình trạng (ít nhất 10 ký tự)")
    photos = check_photos(b.get("photos"))
    with txn(c):
        cm = c.execute("SELECT * FROM commits WHERE id=? AND supplier_id=?", (cid, sid)).fetchone()
        if not cm:
            raise ApiError(404, "Không tìm thấy kèo")
        r = c.execute("SELECT * FROM rfqs WHERE id=?", (cm["rfq_id"],)).fetchone()
        pol = cancel_policy(c, cm, r)
        if pol["mode"] == "pending":
            raise ApiError(409, "Kèo này đã gửi yêu cầu hủy, đang chờ doanh nghiệp duyệt")
        if pol["mode"] == "direct" and cm["status"] == "DA_DUYET":
            raise ApiError(409, "Kèo này được tự hủy, hãy dùng nút Hủy kèo")
        if pol["mode"] != "request":
            raise ApiError(409, pol["reason"] or "Kèo này không thể xin hủy")
        c.execute("INSERT INTO cancel_requests(commit_id,reason,detail,photos,stage,level,days_left,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                  (cid, reason, detail[:1000], json.dumps(photos), cm["stage"], pol["level"], pol["days_left"], u["id"], now()))
        c.execute("UPDATE commits SET status='CHO_DUYET_HUY' WHERE id=?", (cid,))
        audit(c, u["id"], "KHẨN: Nông dân xin hủy kèo %s (%s, giai đoạn %s), chờ doanh nghiệp duyệt" % (cm["code"], reason, STAGES[cm["stage"]]))
    return {"status": "CHO_DUYET_HUY"}


def cancel_request_json(c, cid, photos=True):
    q = c.execute("SELECT * FROM cancel_requests WHERE commit_id=? ORDER BY id DESC LIMIT 1", (cid,)).fetchone()
    if not q:
        return None
    d = dict(q)
    d["photo_count"] = len(json.loads(d["photos"]))
    d["photos"] = json.loads(d["photos"]) if photos else []
    d["penalty"] = json.loads(d["penalty"]) if d["penalty"] else CANCEL_PENALTY.get("CHAM_SOC" if d["level"] == 1 else "SAP_THU_HOACH")
    return d


def decide_cancel(c, u, cid, b):
    act = b.get("action")
    if act not in ("approve", "reject"):
        raise ApiError(400, "Hành động không hợp lệ")
    waive = bool(b.get("waive")) and act == "approve"
    note = str(b.get("note") or "").strip()[:200]
    with txn(c):
        cm = c.execute("SELECT * FROM commits WHERE id=?", (cid,)).fetchone()
        if not cm:
            raise ApiError(404, "Không tìm thấy kèo")
        if cm["status"] != "CHO_DUYET_HUY":
            raise ApiError(409, "Kèo này không có yêu cầu hủy đang chờ duyệt")
        q = c.execute("SELECT * FROM cancel_requests WHERE commit_id=? AND status='CHO' ORDER BY id DESC LIMIT 1", (cid,)).fetchone()
        pen = CANCEL_PENALTY["CHAM_SOC" if q["level"] == 1 else "SAP_THU_HOACH"]
        if act == "reject":
            c.execute("UPDATE commits SET status='DA_DUYET' WHERE id=?", (cid,))
            c.execute("UPDATE cancel_requests SET status='TU_CHOI',decision_note=?,decided_by=?,decided_at=? WHERE id=?", (note, u["id"], now(), q["id"]))
            audit(c, u["id"], "Từ chối yêu cầu hủy kèo %s, nông dân phải tiếp tục thực hiện" % cm["code"])
            return {"status": "DA_DUYET"}
        c.execute("UPDATE commits SET status='HUY',deposit_status=? WHERE id=?", ("HOAN" if waive else "MAT_COC", cid))
        # số kg thiếu tự động cộng lại cho RFQ (rfq_committed chỉ đếm kèo đã duyệt), RFQ đủ sản lượng thì mở lại để gom tiếp
        c.execute("UPDATE rfqs SET status='MO' WHERE id=? AND status='DU_SL'", (cm["rfq_id"],))
        applied = {"waived": True} if waive else {"deposit_lost": cm["deposit"], "rep": pen["rep"], "lock_days": pen["lock_days"], "cap_pct": pen["cap_pct"], "cap_days": pen["cap_days"]}
        if not waive:
            c.execute("UPDATE suppliers SET reputation=MAX(0,reputation-?),blocked_until=? WHERE id=?", (pen["rep"], add_days(today(), pen["lock_days"]), cm["supplier_id"]))
            if pen["cap_pct"]:
                c.execute("UPDATE suppliers SET cap_pct=?,cap_until=? WHERE id=?", (pen["cap_pct"], add_days(today(), pen["cap_days"]), cm["supplier_id"]))
        c.execute("UPDATE cancel_requests SET status='DUYET',waived=?,penalty=?,decision_note=?,decided_by=?,decided_at=? WHERE id=?",
                  (int(waive), json.dumps(applied), note, u["id"], now(), q["id"]))
        audit(c, u["id"], "Duyệt hủy kèo %s%s" % (cm["code"], ": miễn trừ phạt, hoàn cọc" if waive else ": mất cọc, trừ %d điểm uy tín, khóa nhận kèo %d ngày%s" % (
            pen["rep"], pen["lock_days"], ", giới hạn %g%% sản lượng %d ngày" % (pen["cap_pct"], pen["cap_days"]) if pen["cap_pct"] else "")))
    return {"status": "HUY", "waived": waive, "applied": applied}


def rfqs_json(c, u):
    farmer = u["role"] == "NONG_DAN"
    sid = need_farmer(u) if farmer else None
    rs = rows(c, """SELECT r.*,cu.name AS customer,p.name AS product_name,o.code AS order_code,o.status AS order_status
      FROM rfqs r JOIN customers cu ON cu.id=r.customer_id JOIN products p ON p.sku=r.sku LEFT JOIN orders o ON o.id=r.order_id ORDER BY r.id DESC""")
    out = []
    for r in rs:
        cms = rows(c, "SELECT cm.*,s.name AS supplier_name,s.cert,s.reputation,s.blocked_until,s.cap_until,s.cap_pct FROM commits cm JOIN suppliers s ON s.id=cm.supplier_id WHERE rfq_id=? ORDER BY cm.id", (r["id"],))
        for x in cms:
            x["cancel"] = cancel_policy(c, x, r)
            qq = cancel_request_json(c, x["id"], photos=False)
            x["cancel_request"] = {k: qq[k] for k in ("reason", "detail", "photo_count", "level", "status", "waived", "created_at", "decision_note")} if qq else None
            hist = c.execute("SELECT COUNT(*) n, SUM(status='HUY' AND deposit_status='MAT_COC') lost FROM commits WHERE supplier_id=? AND deposit_status<>'CHO'", (x["supplier_id"],)).fetchone()
            x["supplier_stats"] = {"total": hist["n"], "cancelled": hist["lost"] or 0}
        r["committed_kg"] = sum(x["qty_kg"] for x in cms if x["status"] in ("DA_DUYET", "CHO_DUYET_HUY", "DA_GIAO"))
        r["pending_kg"] = sum(x["qty_kg"] for x in cms if x["status"] == "CHO_DUYET")
        r["remaining_kg"] = max(0, r["qty_kg"] - r["committed_kg"])
        r["grow_days"] = GROW_DAYS.get(r["sku"], 30)
        if farmer:
            cms = [x for x in cms if x["supplier_id"] == sid]
            if r["status"] != "MO" and not cms:
                continue
            for k in ("customer_id", "created_by", "order_id", "order_code", "order_status"):
                r.pop(k, None)
            r["customer"] = "Doanh nghiệp thu mua"
        r["commits"] = cms
        out.append(r)
    return out


def rfq_order(c, u, rid):
    r = c.execute("SELECT * FROM rfqs WHERE id=?", (rid,)).fetchone()
    if not r:
        raise ApiError(404, "Không tìm thấy yêu cầu thu mua")
    if r["status"] != "DU_SL" or r["order_id"]:
        raise ApiError(409, "Chỉ tạo đơn khi yêu cầu đã đủ sản lượng và chưa có đơn")
    o = create_order(c, u, {"customer_id": r["customer_id"], "items": [{"sku": r["sku"], "qty_kg": r["qty_kg"]}]})
    c.execute("UPDATE rfqs SET order_id=? WHERE id=?", (o["id"], rid))
    audit(c, u["id"], "Tạo đơn %s từ %s, chờ kho giữ hàng (FEFO)" % (o["code"], r["code"]))
    return o


def rfq_receive(c, u, rid):
    with txn(c):
        r = c.execute("SELECT r.*,o.status AS ost FROM rfqs r LEFT JOIN orders o ON o.id=r.order_id WHERE r.id=?", (rid,)).fetchone()
        if not r or r["status"] != "DU_SL":
            raise ApiError(409, "Yêu cầu này chưa sẵn sàng để xác nhận nhận hàng")
        if r["ost"] != "DA_XUAT_KHO":
            raise ApiError(409, "Kho chưa xuất hàng cho đơn này")
        total = c.execute("SELECT COALESCE(SUM(i.qty_kg*p.price),0) FROM order_items i JOIN products p ON p.sku=i.sku WHERE i.order_id=?", (r["order_id"],)).fetchone()[0]
        c.execute("UPDATE rfqs SET status='HOAN_TAT',settled_at=? WHERE id=?", (now(), rid))
        audit(c, u["id"], "Xác nhận nhận hàng và tất toán %s (%s đ)" % (r["code"], format(round(total), ",")))
        mark_order_received(c, u, r["order_id"], audit_msg=False)  # đồng bộ: đơn tương ứng cũng thành Đã nhận hàng
    return {"total": total}


def mark_order_received(c, u, oid, audit_msg=True):
    """Đơn DA_XUAT_KHO -> DA_NHAN_HANG; chuyến chặng 2 đang chạy thì chuyển Đã giao. Gọi trong giao dịch."""
    o = c.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    c.execute("UPDATE orders SET status='DA_NHAN_HANG' WHERE id=?", (oid,))
    t = c.execute("SELECT * FROM trips WHERE order_id=? AND leg_type='WAREHOUSE_TO_STORE'", (oid,)).fetchone()
    if t and t["status"] in OPEN_TRIP:
        c.execute("UPDATE trips SET status='DA_GIAO',ended_at=? WHERE id=?", (now(), t["id"]))
        audit(c, u["id"], "Chuyến %s chuyển Đã giao do khách xác nhận đã nhận hàng" % t["code"])
    if audit_msg:
        audit(c, u["id"], "Xác nhận đã nhận hàng cho đơn %s" % o["code"])


def order_receive(c, u, oid):
    with txn(c):
        o = c.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        if not o:
            raise ApiError(404, "Không tìm thấy đơn hàng")
        if o["status"] != "DA_XUAT_KHO":
            raise ApiError(409, "Chỉ xác nhận nhận hàng cho đơn đã xuất kho" if o["status"] != "DA_NHAN_HANG" else "Đơn này đã được xác nhận nhận hàng")
        mark_order_received(c, u, oid)
        rf = c.execute("SELECT * FROM rfqs WHERE order_id=? AND status<>'HOAN_TAT'", (oid,)).fetchone()
        if rf and rf["status"] == "DU_SL":
            total = c.execute("SELECT COALESCE(SUM(i.qty_kg*p.price),0) FROM order_items i JOIN products p ON p.sku=i.sku WHERE i.order_id=?", (oid,)).fetchone()[0]
            c.execute("UPDATE rfqs SET status='HOAN_TAT',settled_at=? WHERE id=?", (now(), rf["id"]))
            audit(c, u["id"], "Xác nhận nhận hàng và tất toán %s (%s đ) cùng đơn %s" % (rf["code"], format(round(total), ","), o["code"]))
    return {"status": "DA_NHAN_HANG"}


def order_trace(c, oid):
    """Lô của một đơn hàng. Đường dẫn QR gắn `order=<id>`: trang công khai chỉ hiện chặng 1 + chuyến chặng 2 của chính đơn này."""
    o = c.execute("SELECT o.id,o.code,o.status,cu.name AS customer_name FROM orders o JOIN customers cu ON cu.id=o.customer_id WHERE o.id=?", (oid,)).fetchone()
    if not o:
        raise ApiError(404, "Không tìm thấy đơn hàng")
    lots = []
    for x in rows(c, """SELECT l.code,SUM(rs.qty_kg) AS qty_kg,l.qa_status,l.grade,l.harvest_date,s.name AS supplier_name,s.cert
      FROM reservations rs JOIN lots l ON l.id=rs.lot_id JOIN suppliers s ON s.id=l.supplier_id WHERE rs.order_id=? GROUP BY l.id ORDER BY l.id""", (oid,)):
        x["path"] = public_url("", x["code"], order=oid)
        lots.append(x)
    trip = c.execute("SELECT code,status FROM trips WHERE order_id=? AND status<>'HUY'", (oid,)).fetchone()
    return {"id": o["id"], "code": o["code"], "status": o["status"], "customer_name": o["customer_name"], "trip_code": trip["code"] if trip else None, "lots": lots}


def rfq_trace(c, rid):
    r = c.execute("SELECT * FROM rfqs WHERE id=?", (rid,)).fetchone()
    if not r:
        raise ApiError(404, "Không tìm thấy yêu cầu thu mua")
    codes = []
    for x in rows(c, "SELECT b.lot_codes FROM batches b JOIN commits cm ON cm.id=b.commit_id WHERE cm.rfq_id=? AND b.lot_codes<>''", (rid,)):
        codes += x["lot_codes"].split(",")
    if r["order_id"]:
        codes += [x["code"] for x in rows(c, "SELECT l.code FROM reservations rs JOIN lots l ON l.id=rs.lot_id WHERE rs.order_id=?", (r["order_id"],))]
    lots = []
    ot = c.execute("SELECT code FROM trips WHERE order_id=? AND leg_type='WAREHOUSE_TO_STORE' AND status<>'HUY'", (r["order_id"],)).fetchone() if r["order_id"] else None
    ship = ot["code"] if ot else None
    reserved = {x["code"] for x in rows(c, "SELECT l.code FROM reservations rs JOIN lots l ON l.id=rs.lot_id WHERE rs.order_id=?", (r["order_id"],))} if r["order_id"] else set()
    for code in dict.fromkeys(codes):
        x = c.execute("SELECT l.code,l.qty_kg,l.qa_status,l.grade,l.harvest_date,s.name AS supplier_name,s.cert FROM lots l JOIN suppliers s ON s.id=l.supplier_id WHERE l.code=?", (code,)).fetchone()
        if x:
            x = dict(x)
            x["path"] = public_url("", code, ship if code in reserved else None)  # lô đi theo đơn của RFQ này: QR gắn đúng chuyến xuất
            lots.append(x)
    return {"code": r["code"], "lots": lots}


FARM_OK = set()  # các hàm API mà tài khoản nông dân được gọi ngoài nhóm quyền "nongdan" (gán sau khi khai báo route)


def public_slip(c, code):
    """Mã QR trên phiếu giao hàng (GH-...): trước khi kho nhập thì báo đang chờ kiểm định, sau khi nhập thì chuyển sang hồ sơ lô thật."""
    bt = c.execute("""SELECT b.*,p.name AS pname,p.temp_min,p.temp_max,p.mrl,s.name AS sname,s.area,s.cert,w.name AS wname,w.city
      FROM batches b JOIN products p ON p.sku=b.sku JOIN suppliers s ON s.id=b.supplier_id JOIN warehouses w ON w.id=b.warehouse_id WHERE b.code=?""", (code,)).fetchone()
    if not bt:
        raise ApiError(404, "Không tìm thấy phiếu giao hàng")
    first = (bt["lot_codes"] or "").split(",")[0]
    if first:
        return public_lot(c, first, qr_token(first))
    gap = None
    if bt["spray_date"]:
        gap = (datetime.date.fromisoformat(bt["harvest_date"]) - datetime.date.fromisoformat(bt["spray_date"])).days
    return {"code": bt["code"], "product": bt["pname"], "verdict": "CHO_KIEM_DINH", "tone": "wait",
            "title": "Đang chờ kho nhận và kiểm định" if bt["status"] == "CHO_GIAO" else "Phiếu không được nhập kho",
            "text": "Nông dân đã lập phiếu giao hàng, kho chưa cân đo và kiểm định chất lượng." if bt["status"] == "CHO_GIAO" else "Kho không nhập lô hàng này.",
            "farm": {"name": bt["sname"], "area": bt["area"], "cert": bt["cert"]}, "harvest_date": bt["harvest_date"], "expiry_date": None, "days_left": -1,
            "spray": {"date": bt["spray_date"], "phi_days": bt["phi_days"], "gap_days": gap, "enough": None if gap is None else gap >= bt["phi_days"]},
            "qa": {"status": "CHO_KIEM_DINH", "grade": None, "residue": None, "mrl": bt["mrl"]},
            "storage": {"name": bt["wname"], "city": bt["city"], "received": None, "temp_min": bt["temp_min"], "temp_max": bt["temp_max"]},
            "recall": None, "journey": public_journey(c, batch_id=bt["id"]), "today": today()}


ROUTES = []


def route(method, pattern, perm=None, auth=True):
    def deco(fn):
        ROUTES.append((method, re.compile("^" + pattern + "$"), perm, auth, fn))
        return fn
    return deco


@route("POST", "/api/login", auth=False)
def r_login(c, u, b, m, q):
    r = c.execute("SELECT * FROM users WHERE username=?", (str(b.get("username", "")).strip(),)).fetchone()
    if not r or not secrets.compare_digest(hash_pw(str(b.get("password", "")), r["salt"]), r["pass_hash"]):
        raise ApiError(401, "Sai tên đăng nhập hoặc mật khẩu")
    tok = secrets.token_hex(24)
    c.execute("INSERT INTO sessions(token,user_id,created_at) VALUES(?,?,?)", (tok, r["id"], now()))
    audit(c, r["id"], "Đăng nhập")
    return {"token": tok, "user": {"id": r["id"], "username": r["username"], "full_name": r["full_name"], "role": r["role"], "role_name": ROLE_NAMES[r["role"]], "perms": sorted(ROLE_PERMS[r["role"]])}}


@route("POST", "/api/logout")
def r_logout(c, u, b, m, q):
    c.execute("DELETE FROM sessions WHERE user_id=?", (u["id"],))
    return {"ok": True}


@route("GET", "/api/me")
def r_me(c, u, b, m, q):
    return {"id": u["id"], "username": u["username"], "full_name": u["full_name"], "role": u["role"], "role_name": ROLE_NAMES[u["role"]], "perms": sorted(ROLE_PERMS[u["role"]])}


@route("GET", "/api/meta")
def r_meta(c, u, b, m, q):
    meta = {"products": rows(c, "SELECT * FROM products"), "warehouses": rows(c, "SELECT * FROM warehouses"),
            "suppliers": rows(c, "SELECT * FROM suppliers"), "customers": rows(c, "SELECT * FROM customers"), "today": today(),
            "standards": STANDARDS, "stages": STAGES, "grow_days": GROW_DAYS,
            "cancel_free_days": CANCEL_FREE_DAYS, "cancel_reasons": CANCEL_REASONS, "max_photos": MAX_PHOTOS}
    meta["demo"] = os.environ.get("KHO_DEMO") == "1"
    if "xuat" in ROLE_PERMS[u["role"]]:  # danh sách xe gợi ý khi xuất kho (cho sửa)
        meta["fleet"] = [{"plate": a, "driver": b_} for a, b_ in zip(PLATES, DRIVERS)]
    if u["role"] == "NONG_DAN":  # nông dân chỉ thấy hồ sơ của mình, không thấy danh sách khách hàng
        meta["customers"] = []
        meta["suppliers"] = [s for s in meta["suppliers"] if s["id"] == u.get("supplier_id")]
    return meta


@route("GET", "/api/dashboard")
def r_dash(c, u, b, m, q):
    d = dashboard(c)
    if "donhang" not in ROLE_PERMS[u["role"]]:
        d.pop("revenue", None)  # doanh thu chỉ trả cho vai trò làm việc với đơn hàng (Quản lý kho, Kinh doanh)
    tkeys = ("active_trips", "active_in", "active_out", "near_arrival", "temp_alerts", "lost_signal")
    if u["role"] == "QA":  # QA không có phần Vận chuyển
        for k in tkeys:
            d.pop(k, None)
    elif u["role"] == "KINH_DOANH":  # Kinh doanh chỉ theo dõi xe đang giao tới cửa hàng
        for k in ("active_trips", "active_in", "near_arrival", "temp_alerts", "lost_signal"):
            d.pop(k, None)
    return d


@route("GET", "/api/lots")
def r_lots(c, u, b, m, q):
    sql, args = LOT_SQL + " WHERE 1=1", [today()]
    if q.get("q"):
        sql += " AND (l.code LIKE ? OR p.name LIKE ?)"
        args += ["%" + q["q"] + "%"] * 2
    if q.get("warehouse_id"):
        sql += " AND l.warehouse_id=?"
        args.append(q["warehouse_id"])
    if q.get("qa"):
        sql += " AND l.qa_status=?"
        args.append(q["qa"])
    if not q.get("all"):
        sql += " AND l.qty_kg>0"
    return rows(c, sql + " ORDER BY l.expiry_date, l.id", args)


@route("GET", r"/api/lots/(\d+)")
def r_lot(c, u, b, m, q):
    lot = rows(c, LOT_SQL + " WHERE l.id=?", (today(), m.group(1)))
    if not lot:
        raise ApiError(404, "Không tìm thấy lô")
    lot = lot[0]
    lot["history"] = rows(c, "SELECT g.*,u.full_name FROM stock_ledger g LEFT JOIN users u ON u.id=g.user_id WHERE lot_id=? ORDER BY g.id DESC", (lot["id"],))
    lot["orders"] = rows(c, "SELECT o.code,o.status,cu.name AS customer,r.qty_kg FROM reservations r JOIN orders o ON o.id=r.order_id JOIN customers cu ON cu.id=o.customer_id WHERE r.lot_id=?", (lot["id"],))
    return lot


@route("POST", r"/api/lots/(\d+)/inspect", perm="kiemdinh")
def r_inspect(c, u, b, m, q):
    return inspect_lot(c, u, int(m.group(1)), b)


@route("POST", r"/api/lots/(\d+)/stocktake", perm="xuat")
def r_stock(c, u, b, m, q):
    return stocktake(c, u, int(m.group(1)), b)


@route("GET", "/api/capacity")
def r_capacity(c, u, b, m, q):
    return [capacity_info(c, w["id"]) for w in c.execute("SELECT id FROM warehouses ORDER BY id").fetchall()]


@route("GET", "/api/receipts")
def r_receipts(c, u, b, m, q):
    rs = rows(c, """SELECT r.*,p.name AS product_name,s.name AS supplier_name,w.name AS warehouse_name,l.code AS lot_code,l.qa_status AS lot_qa,pl.code AS plan_code,bt.code AS batch_code,u.full_name AS creator
      FROM receipts r JOIN products p ON p.sku=r.sku JOIN suppliers s ON s.id=r.supplier_id JOIN warehouses w ON w.id=r.warehouse_id
      LEFT JOIN lots l ON l.id=r.lot_id LEFT JOIN plans pl ON pl.id=r.plan_id LEFT JOIN batches bt ON bt.id=r.batch_id JOIN users u ON u.id=r.created_by ORDER BY r.id DESC""")
    caps = {w["id"]: capacity_info(c, w["id"]) for w in c.execute("SELECT id FROM warehouses").fetchall()}
    for r in rs:
        if r["status"] == "CHO_DUYET":  # nhãn gợi ý cho Quản lý kho: kho nhận sau khi duyệt phiếu này
            ci = caps[r["warehouse_id"]]
            r["cap"] = {"capacity_kg": ci["capacity_kg"], "free_kg": ci["free_kg"], "committed_pct": ci["committed_pct"],
                        "level": "bad" if ci["free_kg"] < 0 else ("warn" if ci["committed_pct"] > WARN_PCT else "ok")}
    return rs


@route("POST", "/api/receipts", perm="nhap")
def r_receipt_new(c, u, b, m, q):
    return create_receipt(c, u, b)


@route("POST", r"/api/receipts/(\d+)/approve", perm="duyet")
def r_receipt_ok(c, u, b, m, q):
    return approve_receipt(c, u, int(m.group(1)), True)


@route("POST", r"/api/receipts/(\d+)/reject", perm="duyet")
def r_receipt_no(c, u, b, m, q):
    return approve_receipt(c, u, int(m.group(1)), False)


@route("GET", "/api/orders")
def r_orders(c, u, b, m, q):
    os_ = rows(c, "SELECT o.*,cu.name AS customer FROM orders o JOIN customers cu ON cu.id=o.customer_id ORDER BY o.id DESC")
    for o in os_:
        o["items"] = rows(c, "SELECT i.*,p.name AS product_name,p.price FROM order_items i JOIN products p ON p.sku=i.sku WHERE order_id=?", (o["id"],))
        o["reservations"] = rows(c, "SELECT r.qty_kg,l.code AS lot_code,l.expiry_date FROM reservations r JOIN lots l ON l.id=r.lot_id WHERE order_id=? ORDER BY l.expiry_date", (o["id"],))
        o["total"] = sum(i["qty_kg"] * i["price"] for i in o["items"])
        xd = c.execute("SELECT b.code AS batch_code,f.code AS offer_code FROM batches b JOIN offers f ON f.id=b.offer_id WHERE b.xdock_order_id=?", (o["id"],)).fetchone()
        o["crossdock"] = dict(xd) if xd else None  # đơn tự sinh từ luồng thu mua trực tiếp của Kinh doanh
    return os_


@route("POST", "/api/orders", perm="donhang")
def r_order_new(c, u, b, m, q):
    return create_order(c, u, b)


@route("POST", r"/api/orders/(\d+)/reserve", perm="giuhang")
def r_order_reserve(c, u, b, m, q):
    return reserve_order(c, u, int(m.group(1)))


@route("POST", r"/api/orders/(\d+)/receive", perm="donhang")
def r_order_receive(c, u, b, m, q):
    return order_receive(c, u, int(m.group(1)))


@route("GET", r"/api/orders/(\d+)/trace", perm="donhang")
def r_order_trace(c, u, b, m, q):
    return order_trace(c, int(m.group(1)))


@route("POST", r"/api/orders/(\d+)/cancel", perm="donhang")
def r_order_cancel(c, u, b, m, q):
    return cancel_order(c, u, int(m.group(1)))


@route("POST", r"/api/orders/(\d+)/ship", perm="xuat")
def r_order_ship(c, u, b, m, q):
    return ship_order(c, u, int(m.group(1)), b)


@route("GET", "/api/report", perm="baocao")
def r_report(c, u, b, m, q):
    return rows(c, """SELECT p.sku,p.name,
      COALESCE(SUM(CASE WHEN g.type='NHAP' THEN g.qty_kg END),0) AS nhap,
      COALESCE(-SUM(CASE WHEN g.type='XUAT' THEN g.qty_kg END),0) AS xuat,
      COALESCE(SUM(CASE WHEN g.type='DIEU_CHINH' THEN g.qty_kg END),0) AS dieu_chinh,
      (SELECT COALESCE(SUM(qty_kg),0) FROM lots WHERE sku=p.sku) AS ton_cuoi
      FROM products p LEFT JOIN lots l ON l.sku=p.sku LEFT JOIN stock_ledger g ON g.lot_id=l.id GROUP BY p.sku ORDER BY p.sku""")


@route("GET", "/api/audit", perm="nhatky")
def r_audit(c, u, b, m, q):
    return rows(c, "SELECT a.created_at,a.action,COALESCE(u.full_name,'Hệ thống') AS who,u.role FROM audit_log a LEFT JOIN users u ON u.id=a.user_id ORDER BY a.id DESC LIMIT 100")


@route("GET", "/api/trips")
def r_trips(c, u, b, m, q):
    out = [trip_view(c, t, u, points=True) for t in visible_trips(c, u, q.get("leg"))]
    out.sort(key=trip_sort_key)  # Sắp đến nơi -> Đang vận chuyển -> Chờ lấy hàng -> đã kết thúc
    return out


@route("GET", r"/api/trips/(\d+)")
def r_trip_one(c, u, b, m, q):
    return trip_view(c, trip_for_user(c, u, int(m.group(1))), u, points=True)


@route("GET", r"/api/trips/(\d+)/qr")
def r_trip_qr(c, u, b, m, q):
    """QR/liên kết dành riêng cho tài xế (có token gửi GPS). Khác hoàn toàn với QR truy xuất công khai."""
    t = trip_for_user(c, u, int(m.group(1)))
    if u["role"] not in ("QUAN_LY", "THU_KHO", "NONG_DAN"):
        raise ApiError(403, "Vai trò \"%s\" không xem được liên kết tài xế" % ROLE_NAMES[u["role"]])
    base = clean_base(os.environ.get("KHO_PUBLIC_URL")) or clean_base(q.get("base")) or q.get("_base")
    if not base:
        raise ApiError(400, "Không xác định được địa chỉ công khai của máy chủ")
    url = "%s/tai-xe?t=%s" % (base, t["token"])
    host = urlparse(base).hostname or ""
    return {"code": t["code"], "url": url, "pin": driver_pin(t["plate"], t["token"]), "plate": t["plate"],
            "svg": qrlib.make_svg(url, label="Mã QR tài xế chuyến " + t["code"]),
            "is_local": host in ("localhost", "127.0.0.1", "::1") or host.endswith(".local")}


@route("POST", r"/api/trips/(\d+)/simulate", perm="xuat")
def r_trip_sim(c, u, b, m, q):
    """Mô phỏng một nhịp di chuyển (chỉ khi KHO_DEMO=1, dùng khi không có xe thật, ví dụ lúc báo cáo)."""
    with txn(c):
        sim_step(c, trip_by_id(c, int(m.group(1))), u["id"])
    return {"ok": True}


@route("POST", r"/api/trips/(\d+)/status", perm="xuat")
def r_trip_status(c, u, b, m, q):
    with txn(c):
        set_trip_status(c, trip_by_id(c, int(m.group(1))), b.get("status"), u["id"], b.get("note"))
    return {"ok": True}


@route("GET", "/api/driver/trip", auth=False)
def r_driver_trip(c, u, b, m, q):
    return driver_view(c, driver_trip(c, {"t": q.get("t")}))


@route("POST", "/api/driver/trips", auth=False)
def r_driver_trips(c, u, b, m, q):
    """Tra cứu theo biển số + PIN 6 số (không dùng GET để PIN không nằm trong đường dẫn/nhật ký)."""
    return {"trips": [driver_view(c, t, token=True) for t in driver_trips_by_pin(c, b.get("plate"), b.get("pin"))]}


@route("POST", "/api/driver/location", auth=False)
def r_driver_loc(c, u, b, m, q):
    with txn(c):
        t = driver_trip(c, b)
        if t["status"] in ("DA_GIAO", "HUY"):
            raise ApiError(409, "Chuyến đã giao xong" if t["status"] == "DA_GIAO" else "Chuyến đã bị hủy")
        try:
            lat, lon = float(b.get("lat")), float(b.get("lon"))
        except (TypeError, ValueError):
            raise ApiError(400, "Tọa độ không hợp lệ")
        if not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
            raise ApiError(400, "Tọa độ ngoài phạm vi")
        sp = b.get("speed")
        sp = round(float(sp) * 3.6, 0) if isinstance(sp, (int, float)) and not isinstance(sp, bool) and math.isfinite(sp) else None  # m/s -> km/h
        temp = b.get("temp")
        if temp is not None:
            try:
                temp = float(temp)
            except (TypeError, ValueError):
                raise ApiError(400, "Nhiệt độ không hợp lệ")
            if not (math.isfinite(temp) and -40 <= temp <= 60):
                raise ApiError(400, "Nhiệt độ ngoài phạm vi")
        if c.execute("SELECT 1 FROM trip_points WHERE trip_id=? AND src='SIM' LIMIT 1", (t["id"],)).fetchone():
            raise ApiError(409, "Chuyến này đang chạy mô phỏng, không trộn với GPS thật")
        gps_check(c, t, lat, lon, sp)
        add_point(c, t, lat, lon, sp, "GPS", temp=temp)
        if t["status"] == "CHO_LAY_HANG":
            c.execute("UPDATE trips SET status='DANG_VAN_CHUYEN',started_at=? WHERE id=?", (now(), t["id"]))
            audit(c, None, "Tài xế bắt đầu chuyến %s (GPS)" % t["code"])
    return {"ok": True}


@route("POST", "/api/driver/status", auth=False)
def r_driver_status(c, u, b, m, q):
    with txn(c):
        set_trip_status(c, driver_trip(c, b), b.get("status"), None, b.get("note"))
    return {"ok": True}


@route("POST", "/api/driver/simulate", auth=False)
def r_driver_sim(c, u, b, m, q):
    """Nút Cập nhật điểm GPS tiếp theo trên trang tài xế: chỉ chạy ở chế độ demo (KHO_DEMO=1)."""
    with txn(c):
        sim_step(c, driver_trip(c, b), need_demo=True)
    return {"ok": True}


@route("GET", r"/api/lots/(\d+)/trace")
def r_trace(c, u, b, m, q):
    return trace_lot(c, int(m.group(1)), q.get("ship") or None)


@route("GET", r"/api/lots/(\d+)/qr")
def r_lot_qr(c, u, b, m, q):
    return lot_qr(c, int(m.group(1)), clean_base(q.get("base")) or q.get("_base"), q.get("ship") or None)  # base lạ bị bỏ, quay về địa chỉ máy chủ


@route("GET", "/api/public/lot", auth=False)
def r_public_lot(c, u, b, m, q):
    return public_lot(c, q.get("c"), q.get("k"), q.get("s"), q.get("order"))


@route("GET", "/api/qa")
def r_qa(c, u, b, m, q):
    return qa_overview(c)


@route("GET", r"/api/lots/(\d+)/recall-impact")
def r_recall_impact(c, u, b, m, q):
    return recall_impact(c, int(m.group(1)))


@route("POST", r"/api/lots/(\d+)/recall", perm="kiemdinh")
def r_recall(c, u, b, m, q):
    return do_recall(c, u, int(m.group(1)), b)


@route("POST", r"/api/recalls/(\d+)/notify", perm="kiemdinh")
def r_recall_notify(c, u, b, m, q):
    return recall_notify(c, u, int(m.group(1)), b)


@route("POST", r"/api/recalls/(\d+)/close", perm="kiemdinh")
def r_recall_close(c, u, b, m, q):
    return recall_close(c, u, int(m.group(1)), b)


@route("GET", "/api/plans")
def r_plans(c, u, b, m, q):
    return plans_json(c, u, q)


@route("POST", "/api/plans", perm="kehoach")
def r_plan_new(c, u, b, m, q):
    return create_plan(c, u, b)


@route("POST", r"/api/plans/(\d+)/update")
def r_plan_update(c, u, b, m, q):  # quyền được kiểm tra theo từng thao tác bên trong update_plan
    return update_plan(c, u, int(m.group(1)), b)


@route("GET", "/api/prices")
def r_prices(c, u, b, m, q):
    d = price_overview(c)
    if u["role"] == "NONG_DAN":  # nông dân xem giá tham khảo, không thấy giá bán nội bộ và người nhập
        for s_ in d["summary"]:
            s_["sell_price"] = None
        for r_ in d["recent"]:
            r_["who"] = None
    return d


@route("POST", "/api/prices", perm="gia")
def r_price_new(c, u, b, m, q):
    return add_price(c, u, b)


@route("POST", r"/api/prices/(\d+)/update", perm="gia")
def r_price_edit(c, u, b, m, q):
    return edit_price(c, u, int(m.group(1)), b)


@route("POST", r"/api/prices/(\d+)/delete", perm="gia")
def r_price_del(c, u, b, m, q):
    return delete_price(c, u, int(m.group(1)))


@route("GET", "/api/requirements", perm="doichieu")
def r_requirements(c, u, b, m, q):
    return req_overview(c)


# ---------------------------------------------------------------- API chuỗi cung ứng 3 bên
@route("GET", "/api/offers")
def r_offers(c, u, b, m, q):
    return offers_json(c, u)


@route("POST", "/api/farm/offers", perm="nongdan")
def r_offer_new(c, u, b, m, q):
    return create_offer(c, u, b)


@route("POST", r"/api/farm/offers/(\d+)/cancel", perm="nongdan")
def r_offer_cancel(c, u, b, m, q):
    return cancel_offer(c, u, int(m.group(1)))


@route("POST", r"/api/offers/(\d+)/buy", perm="thumua")
def r_offer_buy(c, u, b, m, q):
    return buy_offer(c, u, int(m.group(1)), b)


@route("POST", r"/api/offers/(\d+)/decide", perm="duyet")
def r_offer_decide(c, u, b, m, q):
    return decide_offer(c, u, int(m.group(1)), b)


@route("GET", "/api/batches")
def r_batches(c, u, b, m, q):
    return batches_json(c, u)


@route("POST", "/api/farm/batches", perm="nongdan")
def r_batch_new(c, u, b, m, q):
    return create_batch(c, u, b)


@route("GET", r"/api/batches/(\d+)/qr")
def r_batch_qr(c, u, b, m, q):
    return batch_qr(c, u, int(m.group(1)), q.get("_base"))


@route("POST", r"/api/batches/(\d+)/receive", perm="nhap")
def r_batch_receive(c, u, b, m, q):
    return receive_batch(c, u, int(m.group(1)), b)


@route("POST", r"/api/batches/(\d+)/receipt", perm="nhap")
def r_batch_receipt(c, u, b, m, q):
    return batch_receipt(c, u, int(m.group(1)), b)


@route("POST", r"/api/batches/(\d+)/refuse", perm="nhap")
def r_batch_refuse(c, u, b, m, q):
    return batch_refuse(c, u, int(m.group(1)), b)


@route("POST", r"/api/batches/(\d+)/pay", perm="duyet")
def r_batch_pay(c, u, b, m, q):
    return pay_batch(c, u, int(m.group(1)))


@route("GET", "/api/rfqs")
def r_rfqs(c, u, b, m, q):
    return rfqs_json(c, u)


@route("POST", "/api/rfqs", perm="rfq")
def r_rfq_new(c, u, b, m, q):
    return create_rfq(c, u, b)


@route("POST", r"/api/farm/rfqs/(\d+)/commit", perm="nongdan")
def r_rfq_commit(c, u, b, m, q):
    return commit_rfq(c, u, int(m.group(1)), b)


@route("POST", r"/api/commits/(\d+)/decide", perm="rfq")
def r_commit_decide(c, u, b, m, q):
    return decide_commit(c, u, int(m.group(1)), b)


@route("POST", r"/api/farm/commits/(\d+)/progress", perm="nongdan")
def r_commit_progress(c, u, b, m, q):
    return set_progress(c, u, int(m.group(1)), b)


@route("POST", r"/api/farm/commits/(\d+)/cancel", perm="nongdan")
def r_commit_cancel(c, u, b, m, q):
    return cancel_commit(c, u, int(m.group(1)))


@route("POST", r"/api/farm/commits/(\d+)/request-cancel", perm="nongdan")
def r_commit_request_cancel(c, u, b, m, q):
    return request_cancel(c, u, int(m.group(1)), b)


@route("GET", r"/api/commits/(\d+)/cancel-request", perm="rfq")
def r_commit_cancel_request(c, u, b, m, q):
    d = cancel_request_json(c, int(m.group(1)))
    if not d:
        raise ApiError(404, "Kèo này chưa có yêu cầu hủy")
    cm = c.execute("SELECT cm.code,cm.qty_kg,cm.deposit,cm.stage,s.name AS supplier_name,s.reputation FROM commits cm JOIN suppliers s ON s.id=cm.supplier_id WHERE cm.id=?", (int(m.group(1)),)).fetchone()
    return {**d, "commit": dict(cm)}


@route("POST", r"/api/commits/(\d+)/cancel-decision", perm="rfq")
def r_commit_cancel_decision(c, u, b, m, q):
    return decide_cancel(c, u, int(m.group(1)), b)


@route("POST", r"/api/rfqs/(\d+)/order", perm="rfq")
def r_rfq_order(c, u, b, m, q):
    return rfq_order(c, u, int(m.group(1)))


@route("POST", r"/api/rfqs/(\d+)/receive", perm="rfq")
def r_rfq_receive(c, u, b, m, q):
    return rfq_receive(c, u, int(m.group(1)))


@route("GET", r"/api/rfqs/(\d+)/trace")
def r_rfq_trace(c, u, b, m, q):
    return rfq_trace(c, int(m.group(1)))


FARM_OK.update({r_me, r_logout, r_meta, r_prices, r_offers, r_batches, r_batch_qr, r_rfqs, r_plans, r_plan_new, r_plan_update, r_trips, r_trip_one, r_trip_qr})


class Handler(BaseHTTPRequestHandler):
    server_version = "KhoMVP/1.0"

    def log_message(self, fmt, *args):
        if os.environ.get("KHO_QUIET") != "1":
            sys.stderr.write("%s %s\n" % (self.command, self.path))

    def send_json(self, status, obj):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def guess_base(self):
        """Địa chỉ công khai mà người dùng đang truy cập (hỗ trợ proxy của Codespaces/Render qua X-Forwarded-*)."""
        proto = (self.headers.get("X-Forwarded-Proto") or "http").split(",")[0].strip()
        host = (self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or "").split(",")[0].strip()
        return clean_base("%s://%s" % (proto, host))

    def send_svg(self, svg):
        data = svg.encode()
        self.send_response(200)
        self.send_header("Content-Type", "image/svg+xml; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=3600")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def handle_any(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path == "/qr.svg":  # ảnh QR công khai, chỉ tạo được cho cặp (mã lô, mã xác thực) hợp lệ
            code, k, ship, order = q.get("c"), q.get("k"), (q.get("s") or None), (q.get("order") or None)
            base = clean_base(os.environ.get("KHO_PUBLIC_URL")) or clean_base(q.get("base")) or self.guess_base()
            if not qr_valid(code, k, ctx_key(ship, order)) or not base:
                return self.send_json(404, {"error": "Mã QR không hợp lệ"})
            return self.send_svg(qrlib.make_svg(public_url(base, code, ship, order), label="Mã QR truy xuất lô " + code))
        if not url.path.startswith("/api/"):
            return self.static(url.path)
        q["_base"] = self.guess_base()  # gán sau cùng để client không giả mạo được
        body = {}
        if self.command == "POST":
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                return self.send_json(413, {"error": "Dữ liệu gửi lên quá lớn (tối đa %d MB)" % (MAX_BODY // 1_000_000)})
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self.send_json(400, {"error": "Dữ liệu gửi lên không hợp lệ"})
        c = connect()
        try:
            for method, rx, perm, auth, fn in ROUTES:
                m = rx.match(url.path)
                if m and method == self.command:
                    user = None
                    if auth:
                        tok = (self.headers.get("Authorization") or "").replace("Bearer ", "")
                        user = c.execute("SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=?", (tok,)).fetchone()
                        if not user:
                            raise ApiError(401, "Chưa đăng nhập hoặc phiên đã hết hạn")
                        user = dict(user)
                        if perm and perm not in ROLE_PERMS[user["role"]]:
                            raise ApiError(403, "Vai trò \"%s\" không có quyền %s" % (ROLE_NAMES[user["role"]], PERM_NAMES[perm]))
                        if user["role"] == "NONG_DAN" and perm != "nongdan" and fn not in FARM_OK:
                            raise ApiError(403, "Tài khoản nông dân chỉ dùng được các chức năng dành cho nông dân")
                    return self.send_json(200, fn(c, user, body, m, q))
            raise ApiError(404, "Không tìm thấy đường dẫn")
        except ApiError as e:
            self.send_json(e.status, {"error": e.msg})
        except sqlite3.IntegrityError as e:
            self.send_json(409, {"error": "Vi phạm ràng buộc dữ liệu: %s" % e})
        except Exception as e:  # noqa
            sys.stderr.write("LỖI: %r\n" % (e,))
            self.send_json(500, {"error": "Lỗi hệ thống"})
        finally:
            c.close()

    def static(self, path):
        public = path == "/t" or path.startswith("/t/")  # trang truy xuất công khai: /t/<mã lô>?k=<mã xác thực>
        path = {"/": "/index.html", "": "/index.html", "/tai-xe": "/tai-xe.html", "/tem": "/tem.html"}.get(path, path)
        if public:
            path = "/truy-xuat.html"
        f = os.path.normpath(os.path.join(STATIC, path.lstrip("/")))
        if not f.startswith(STATIC) or not os.path.isfile(f):
            return self.send_json(404, {"error": "Không tìm thấy"})
        with open(f, "rb") as fh:
            data = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", (mimetypes.guess_type(f)[0] or "text/plain") + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        if public or path == "/tem.html":
            self.send_header("Referrer-Policy", "no-referrer")  # không để lộ mã xác thực trong URL cho bên thứ ba
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                             "font-src https://fonts.gstatic.com; img-src 'self' data:; script-src 'self' 'unsafe-inline'; connect-src 'self'; "
                             "frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = handle_any


def make_server(port):
    init_db()
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    srv = make_server(port)
    print("Máy chủ đang chạy tại http://127.0.0.1:%d  (Ctrl+C để dừng)" % port)
    print("Tài khoản mẫu (mật khẩu 123456): quanly, thukho, qa, kinhdoanh, nongdan, nongdan2")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
