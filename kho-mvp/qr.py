"""Bộ tạo mã QR bằng Python thuần (không cần cài thư viện).

Chuẩn: QR Code Model 2, chế độ Byte (UTF-8), mức sửa lỗi M (khôi phục được ~15% dữ liệu hỏng),
phiên bản 1-10 (tối đa 213 byte), tự chọn mặt nạ có điểm phạt thấp nhất theo đúng quy tắc ISO/IEC 18004.
Dùng:  svg = make_svg("https://.../t/XCL-261120-01?k=abc")
"""

# Mức M: số byte sửa lỗi mỗi khối và số khối, theo phiên bản (chỉ mục 0 bỏ trống)
_ECC_PER_BLOCK = (None, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26)
_NUM_BLOCKS = (None, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5)
MAX_VERSION = 10
_FORMAT_BITS_M = 0  # mã mức sửa lỗi M trong thông tin định dạng


class QRTooLong(ValueError):
    pass


def _raw_modules(ver):
    n = (16 * ver + 128) * ver + 64
    if ver >= 2:
        a = ver // 7 + 2
        n -= (25 * a - 10) * a - 55
        if ver >= 7:
            n -= 36
    return n


def _data_codewords(ver):
    return _raw_modules(ver) // 8 - _ECC_PER_BLOCK[ver] * _NUM_BLOCKS[ver]


def _gf_mul(x, y):
    z = 0
    for i in range(7, -1, -1):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree):
    res = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            res[j] = _gf_mul(res[j], root)
            if j + 1 < degree:
                res[j] ^= res[j + 1]
        root = _gf_mul(root, 2)
    return res


def _rs_remainder(data, divisor):
    res = [0] * len(divisor)
    for b in data:
        factor = b ^ res.pop(0)
        res.append(0)
        for i, coef in enumerate(divisor):
            res[i] ^= _gf_mul(coef, factor)
    return res


def _build_codewords(payload, ver):
    cap = _data_codewords(ver)
    bits = [0, 1, 0, 0]  # chế độ Byte
    cc_bits = 16 if ver >= 10 else 8
    bits += [(len(payload) >> i) & 1 for i in range(cc_bits - 1, -1, -1)]
    for byte in payload:
        bits += [(byte >> i) & 1 for i in range(7, -1, -1)]
    bits += [0] * min(4, cap * 8 - len(bits))
    bits += [0] * (-len(bits) % 8)
    data = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = 0xEC
    while len(data) < cap:
        data.append(pad)
        pad ^= 0xEC ^ 0x11
    nb, ecc_len = _NUM_BLOCKS[ver], _ECC_PER_BLOCK[ver]
    raw = _raw_modules(ver) // 8
    n_short = nb - raw % nb
    short_len = raw // nb
    divisor = _rs_divisor(ecc_len)
    blocks, k = [], 0
    for i in range(nb):
        dl = short_len - ecc_len + (0 if i < n_short else 1)
        dat = data[k:k + dl]
        k += dl
        ecc = _rs_remainder(dat, divisor)
        if i < n_short:
            dat.append(0)
        blocks.append(dat + ecc)
    out = []
    for i in range(len(blocks[0])):
        for j, blk in enumerate(blocks):
            if i != short_len - ecc_len or j >= n_short:
                out.append(blk[i])
    return out


class _Grid:
    def __init__(self, ver):
        self.ver = ver
        self.n = ver * 4 + 17
        self.m = [[False] * self.n for _ in range(self.n)]
        self.fn = [[False] * self.n for _ in range(self.n)]
        self._draw_function_patterns()

    def _set(self, x, y, dark):
        self.m[y][x] = bool(dark)
        self.fn[y][x] = True

    def _finder(self, cx, cy):
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                x, y = cx + dx, cy + dy
                if 0 <= x < self.n and 0 <= y < self.n:
                    self._set(x, y, max(abs(dx), abs(dy)) not in (2, 4))

    def _align(self, cx, cy):
        for dy in range(-2, 3):
            for dx in range(-2, 3):
                self._set(cx + dx, cy + dy, max(abs(dx), abs(dy)) != 1)

    def _align_positions(self):
        if self.ver == 1:
            return []
        a = self.ver // 7 + 2
        step = (self.ver * 4 + a * 2 + 1) // (a * 2 - 2) * 2
        return list(reversed([self.n - 7 - i * step for i in range(a - 1)] + [6]))

    def _draw_function_patterns(self):
        n = self.n
        for i in range(n):
            self._set(6, i, i % 2 == 0)
            self._set(i, 6, i % 2 == 0)
        self._finder(3, 3)
        self._finder(n - 4, 3)
        self._finder(3, n - 4)
        pos = self._align_positions()
        for i, x in enumerate(pos):
            for j, y in enumerate(pos):
                if not ((i == 0 and j == 0) or (i == 0 and j == len(pos) - 1) or (i == len(pos) - 1 and j == 0)):
                    self._align(x, y)
        self.draw_format(0)
        if self.ver >= 7:
            rem = self.ver
            for _ in range(12):
                rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
            bits = self.ver << 12 | rem
            for i in range(18):
                b = (bits >> i) & 1
                a, c = n - 11 + i % 3, i // 3
                self._set(a, c, b)
                self._set(c, a, b)

    def draw_format(self, mask):
        n = self.n
        data = _FORMAT_BITS_M << 3 | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412
        g = lambda i: (bits >> i) & 1
        for i in range(0, 6):
            self._set(8, i, g(i))
        self._set(8, 7, g(6))
        self._set(8, 8, g(7))
        self._set(7, 8, g(8))
        for i in range(9, 15):
            self._set(14 - i, 8, g(i))
        for i in range(0, 8):
            self._set(n - 1 - i, 8, g(i))
        for i in range(8, 15):
            self._set(8, n - 15 + i, g(i))
        self._set(8, n - 8, True)

    def place(self, codewords):
        n, i = self.n, 0
        total = len(codewords) * 8
        for right in range(n - 1, 0, -2):
            if right <= 6:
                right -= 1
            for vert in range(n):
                for j in range(2):
                    x = right - j
                    y = (n - 1 - vert) if ((right + 1) & 2) == 0 else vert
                    if not self.fn[y][x] and i < total:
                        self.m[y][x] = ((codewords[i >> 3] >> (7 - (i & 7))) & 1) == 1
                        i += 1

    def mask(self, k):
        f = _MASKS[k]
        for y in range(self.n):
            for x in range(self.n):
                if not self.fn[y][x] and f(x, y):
                    self.m[y][x] = not self.m[y][x]

    def penalty(self):
        n, m = self.n, self.m
        score = 0
        lines = [row[:] for row in m] + [[m[y][x] for y in range(n)] for x in range(n)]
        for ln in lines:  # N1 (dãy cùng màu) và N3 (mẫu giống ô định vị)
            run = 1
            for i in range(1, n):
                if ln[i] == ln[i - 1]:
                    run += 1
                else:
                    if run >= 5:
                        score += run - 2
                    run = 1
            if run >= 5:
                score += run - 2
            s = "".join("1" if v else "0" for v in ln)
            for pat in ("10111010000", "00001011101"):
                start = s.find(pat)
                while start != -1:
                    score += 40
                    start = s.find(pat, start + 1)
        for y in range(n - 1):  # N2 (khối 2x2 cùng màu)
            for x in range(n - 1):
                if m[y][x] == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                    score += 3
        dark = sum(v for row in m for v in row)
        total = n * n
        k = -(-abs(dark * 20 - total * 10) // total) - 1  # N4 (tỉ lệ ô đen)
        return score + max(k, 0) * 10


_MASKS = (
    lambda x, y: (x + y) % 2 == 0,
    lambda x, y: y % 2 == 0,
    lambda x, y: x % 3 == 0,
    lambda x, y: (x + y) % 3 == 0,
    lambda x, y: (x // 3 + y // 2) % 2 == 0,
    lambda x, y: x * y % 2 + x * y % 3 == 0,
    lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
    lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
)


def encode(text):
    """Trả về ma trận bool (True = ô đen) của mã QR chứa `text`."""
    payload = text.encode("utf-8")
    for ver in range(1, MAX_VERSION + 1):
        cc_bits = 16 if ver >= 10 else 8
        if 4 + cc_bits + 8 * len(payload) <= _data_codewords(ver) * 8:
            break
    else:
        raise QRTooLong("Nội dung quá dài cho mã QR (tối đa 213 byte)")
    codewords = _build_codewords(payload, ver)
    g = _Grid(ver)
    g.place(codewords)
    best, best_pen = 0, None
    for k in range(8):
        g.mask(k)
        g.draw_format(k)
        p = g.penalty()
        if best_pen is None or p < best_pen:
            best, best_pen = k, p
        g.mask(k)  # hoàn tác mặt nạ
    g.mask(best)
    g.draw_format(best)
    return g.m


def to_svg(modules, border=4, dark="#000", light="#fff", label="Mã QR"):
    n = len(modules)
    size = n + border * 2
    parts = []
    for y, row in enumerate(modules):
        x = 0
        while x < n:
            if row[x]:
                s = x
                while x < n and row[x]:
                    x += 1
                parts.append("M%d,%dh%dv1h-%dz" % (s + border, y + border, x - s, x - s))
            else:
                x += 1
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" role="img" aria-label="%s" shape-rendering="crispEdges">'
            '<rect width="100%%" height="100%%" fill="%s"/><path d="%s" fill="%s"/></svg>') % (size, size, label, light, "".join(parts), dark)


def make_svg(text, **kw):
    return to_svg(encode(text), **kw)
