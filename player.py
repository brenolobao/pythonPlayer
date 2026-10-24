import os
import io
import sys
import json
import math
import time
import queue
import random
import base64
import colorsys
import threading
import warnings

warnings.filterwarnings("ignore")
os.environ["PYGAME_HIDE_SUPPORT_PROMPT"] = "1"

import tkinter as tk
from tkinter import filedialog, font as tkfont

import numpy as np
import pygame
from mutagen import File as MutagenFile
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageChops, ImageOps, ImageTk

RS = Image.Resampling
TR = Image.Transform

SUPPORTED = (".mp3", ".wav", ".ogg", ".flac")
STATE_FILE = os.path.join(os.path.expanduser("~"), ".ambient_player.json")
NB = 28
FRAME_MS = 33


# ----------------------------------------------------------------- helpers
def clamp(x, a=0.0, b=1.0):
    return a if x < a else b if x > b else x


def ease_out(t):
    t = clamp(t)
    return 1 - (1 - t) ** 3


def ease_io(t):
    t = clamp(t)
    return t * t * (3 - 2 * t)


def mixc(a, b, t):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def hexc(c):
    return "#%02x%02x%02x" % tuple(int(clamp(v, 0, 255)) for v in c[:3])


def fmt(s):
    s = max(0, int(s))
    if s >= 3600:
        return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"
    return f"{s // 60}:{s % 60:02d}"


class Spring:
    def __init__(self, x=0.0, k=170.0, d=22.0):
        self.x = x
        self.v = 0.0
        self.t = x
        self.k = k
        self.d = d

    def step(self, dt):
        n = max(1, int(dt / 0.008) + 1)
        h = dt / n
        for _ in range(n):
            a = self.k * (self.t - self.x) - self.d * self.v
            self.v += a * h
            self.x += self.v * h
        return self.x


# ------------------------------------------------------------ image tools
def rounded_mask(size, radius, ss=4):
    w, h = size
    m = Image.new("L", (w * ss, h * ss), 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, w * ss - 1, h * ss - 1), radius=radius * ss, fill=255)
    return m.resize(size, RS.LANCZOS)


def placeholder_cover(seed, size=720):
    rnd = random.Random(seed)
    h = rnd.random()

    def hsv(hh, s, v):
        r, g, b = colorsys.hsv_to_rgb(hh % 1.0, s, v)
        return np.array([r * 255, g * 255, b * 255], dtype=np.float32)

    c1 = hsv(h, 0.62, 0.96)
    c2 = hsv(h + 0.14 + rnd.random() * 0.08, 0.78, 0.50)
    y, x = np.mgrid[0:size, 0:size].astype(np.float32)
    t = ((x + y) / (2 * size))[..., None]
    arr = c1 * (1 - t) + c2 * t
    img = Image.fromarray(arr.astype("uint8"), "RGB").convert("RGBA")
    ss = 2
    layer = Image.new("RGBA", (size * ss, size * ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    S = size * ss
    col = (255, 255, 255, 225)
    rx, ry = 0.095 * S, 0.072 * S
    for cx, cy in ((0.36 * S, 0.68 * S), (0.62 * S, 0.62 * S)):
        d.ellipse((cx - rx, cy - ry, cx + rx, cy + ry), fill=col)
    sw = 0.022 * S
    d.rectangle((0.45 * S - sw, 0.30 * S, 0.45 * S + sw, 0.68 * S), fill=col)
    d.rectangle((0.71 * S - sw, 0.24 * S, 0.71 * S + sw, 0.62 * S), fill=col)
    d.polygon([(0.45 * S - sw, 0.30 * S), (0.71 * S + sw, 0.24 * S),
               (0.71 * S + sw, 0.335 * S), (0.45 * S - sw, 0.395 * S)], fill=col)
    layer = layer.resize((size, size), RS.LANCZOS)
    img.alpha_composite(layer)
    return img.convert("RGB")


def extract_cover(path):
    try:
        f = MutagenFile(path)
        if f is None:
            return None
        data = None
        tags = f.tags
        if tags is not None and hasattr(tags, "getall"):
            pics = tags.getall("APIC")
            if pics:
                data = pics[0].data
        if data is None and getattr(f, "pictures", None):
            data = f.pictures[0].data
        if data is None and tags is not None:
            try:
                if "covr" in tags:
                    data = bytes(tags["covr"][0])
                elif "metadata_block_picture" in tags:
                    from mutagen.flac import Picture
                    data = Picture(base64.b64decode(tags["metadata_block_picture"][0])).data
            except Exception:
                data = None
        if data:
            return Image.open(io.BytesIO(data)).convert("RGB")
    except Exception:
        pass
    return None


def pick_accent(cover):
    im = cover.resize((64, 64), RS.BOX).quantize(colors=8, method=0)
    pal = im.getpalette()
    best, bs = None, -1.0
    for cnt, idx in im.getcolors():
        r, g, b = pal[idx * 3: idx * 3 + 3]
        h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        score = math.sqrt(cnt / 4096.0) * (0.25 + s) * (0.3 + v)
        if score > bs:
            bs, best = score, (h, s, v)
    h, s, v = best
    if s < 0.14:
        s, v = 0.0, 0.96
    else:
        s, v = clamp(max(s, 0.40) * 1.1, 0, 0.92), max(v, 0.92)

    def rgb(hh):
        r, g, b = colorsys.hsv_to_rgb(hh % 1.0, s, v)
        return (int(r * 255), int(g * 255), int(b * 255))

    return rgb(h), rgb(h + 0.09 if s > 0 else h)


def build_art(cover, acc, S=720):
    w, h = cover.size
    m = min(w, h)
    sq = cover.crop(((w - m) // 2, (h - m) // 2, (w - m) // 2 + m, (h - m) // 2 + m)).resize((S, S), RS.LANCZOS)
    art = sq.convert("RGBA")
    art.putalpha(rounded_mask((S, S), int(S * 0.06)))
    P = int(S * 0.25)
    C = S + 2 * P
    r = int(S * 0.06)
    glow = Image.new("RGBA", (C, C), (0, 0, 0, 0))
    ImageDraw.Draw(glow).rounded_rectangle((P + S * 0.06, P + S * 0.09, P + S * 0.94, P + S * 1.02),
                                           radius=r, fill=acc + (140,))
    glow = glow.filter(ImageFilter.GaussianBlur(S * 0.075))
    shadow = Image.new("RGBA", (C, C), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle((P + S * 0.02, P + S * 0.04, P + S * 0.98, P + S * 1.0),
                                             radius=r, fill=(0, 0, 0, 175))
    shadow = shadow.filter(ImageFilter.GaussianBlur(S * 0.05))
    combo = Image.new("RGBA", (C, C), (0, 0, 0, 0))
    combo.alpha_composite(glow)
    combo.alpha_composite(shadow)
    combo.alpha_composite(art, (P, P))
    return combo


def build_visual(cover):
    cover = cover.convert("RGB")
    acc, acc2 = pick_accent(cover)
    art = build_art(cover, acc)
    small = cover.resize((16, 16), RS.BOX)
    small = ImageEnhance.Color(small).enhance(1.6)
    base = small.resize((384, 384), RS.BICUBIC).filter(ImageFilter.GaussianBlur(38))
    lum = max(1, base.convert("L").resize((1, 1), RS.BOX).getpixel((0, 0)))
    base = ImageEnhance.Brightness(base).enhance(min(0.62, 95.0 / lum))
    base = ImageEnhance.Color(base).enhance(1.2)
    tint = Image.new("RGB", base.size, tuple(int(c * 0.38) for c in acc))
    base = Image.blend(base, tint, 0.28)
    L1 = base
    L2 = ImageOps.mirror(base).rotate(90)
    bgc = base.resize((1, 1), RS.BOX).getpixel((0, 0))
    return {"art": art, "L1": L1, "L2": L2, "acc": acc, "acc2": acc2, "bgc": tuple(bgc)}


def make_vignette(ow, oh):
    y, x = np.mgrid[0:oh, 0:ow].astype(np.float32)
    nx = (x / ow - 0.5) * 2
    ny = (y / oh - 0.5) * 2
    d = np.sqrt(nx * nx * 0.8 + ny * ny * 0.9)
    v = 1 - 0.5 * np.clip(d - 0.35, 0, 1) ** 1.5
    v = v * (1 - 0.22 * np.clip((ny + 0.1) / 1.1, 0, 1))
    arr = (np.clip(v, 0, 1) * 255).astype("uint8")
    return Image.fromarray(arr, "L").convert("RGB")


# ------------------------------------------------------------------ icons
_IC = {}


def _glyph(d, name, u, c):
    w = 2.0 * u

    def L(pts, width=w):
        pp = [(x * u, y * u) for x, y in pts]
        d.line(pp, fill=c, width=int(width), joint="curve")
        r = width / 2
        for x, y in (pp[0], pp[-1]):
            d.ellipse((x - r, y - r, x + r, y + r), fill=c)

    def P(pts):
        d.polygon([(x * u, y * u) for x, y in pts], fill=c)

    def R(x0, y0, x1, y1, r):
        d.rounded_rectangle((x0 * u, y0 * u, x1 * u, y1 * u), radius=r * u, fill=c)

    def tri(pts):
        P(pts)
        pp = [(x * u, y * u) for x, y in pts] + [(pts[0][0] * u, pts[0][1] * u)]
        d.line(pp, fill=c, width=int(1.8 * u), joint="curve")

    if name == "play":
        tri([(8.4, 6.2), (18.2, 12), (8.4, 17.8)])
    elif name == "pause":
        R(6, 4.5, 10.5, 19.5, 1.6)
        R(13.5, 4.5, 18, 19.5, 1.6)
    elif name == "next":
        tri([(5.4, 6.2), (15.2, 12), (5.4, 17.8)])
        R(16.8, 5, 19.8, 19, 1.3)
    elif name == "prev":
        tri([(18.6, 6.2), (8.8, 12), (18.6, 17.8)])
        R(4.2, 5, 7.2, 19, 1.3)
    elif name == "shuffle":
        L([(3, 17), (8, 17), (15.5, 7), (19, 7)])
        L([(3, 7), (8, 7), (10.2, 9.8)])
        L([(13, 14.2), (15.5, 17), (19, 17)])
        P([(18, 3.4), (22.2, 7), (18, 10.6)])
        P([(18, 13.4), (22.2, 17), (18, 20.6)])
    elif name in ("repeat", "repeat1"):
        d.rounded_rectangle((3 * u, 6 * u, 21 * u, 18 * u), radius=4 * u, outline=c, width=int(2 * u))
        d.rectangle((11 * u, 3 * u, 17 * u, 9 * u), fill=(0, 0, 0, 0))
        d.rectangle((7 * u, 15 * u, 13 * u, 21 * u), fill=(0, 0, 0, 0))
        P([(11, 3.2), (11, 8.8), (16, 6)])
        P([(13, 15.2), (13, 20.8), (8, 18)])
        if name == "repeat1":
            L([(10.6, 10.6), (12.2, 9.3), (12.2, 14.8)], 1.6 * u)
    elif name.startswith("vol"):
        P([(3.5, 9.5), (7.5, 9.5), (12.5, 5), (12.5, 19), (7.5, 14.5), (3.5, 14.5)])
        lv = int(name[3])
        if lv == 0:
            L([(16, 9.5), (21.5, 14.5)])
            L([(21.5, 9.5), (16, 14.5)])
        else:
            for i, r in enumerate((3.4, 6.4, 9.4)[:lv]):
                d.arc(((12.5 - r) * u, (12 - r) * u, (12.5 + r) * u, (12 + r) * u),
                      start=-48, end=48, fill=c, width=int(1.8 * u))
    elif name == "queue":
        L([(9, 7), (21, 7)])
        L([(9, 12), (21, 12)])
        L([(9, 17), (16, 17)])
        for y in (7, 12, 17):
            d.ellipse((3.3 * u, (y - 1.3) * u, 5.9 * u, (y + 1.3) * u), fill=c)
    elif name == "full":
        L([(4, 9), (4, 4), (9, 4)])
        L([(15, 4), (20, 4), (20, 9)])
        L([(20, 15), (20, 20), (15, 20)])
        L([(9, 20), (4, 20), (4, 15)])
    elif name == "close":
        L([(6, 6), (18, 18)])
        L([(18, 6), (6, 18)])
    elif name == "search":
        d.ellipse((4.5 * u, 4.5 * u, 15 * u, 15 * u), outline=c, width=int(2 * u))
        L([(14, 14), (19.5, 19.5)])


def get_icon(name, size, color=(255, 255, 255), a8=8, circle=None):
    key = (name, size, color, a8, circle)
    im = _IC.get(key)
    if im is not None:
        return im
    if len(_IC) > 800:
        _IC.clear()
    ss = 4
    S = size * ss
    layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    if circle:
        ImageDraw.Draw(layer).ellipse((0, 0, S - 1, S - 1), fill=circle)
        g = int(S * 0.54)
        glyph = Image.new("RGBA", (g, g), (0, 0, 0, 0))
        _glyph(ImageDraw.Draw(glyph), name, g / 24.0, tuple(color) + (255,))
        ox = (S - g) // 2 + (int(g * 0.04) if name == "play" else 0)
        layer.alpha_composite(glyph, (ox, (S - g) // 2))
    else:
        _glyph(ImageDraw.Draw(layer), name, S / 24.0, tuple(color) + (255,))
    layer = layer.resize((size, size), RS.LANCZOS)
    if a8 < 8:
        a = layer.getchannel("A").point(lambda v: v * a8 // 8)
        layer.putalpha(a)
    ph = ImageTk.PhotoImage(layer)
    _IC[key] = ph
    return ph


def get_round(w, h, r, fill, outline=None):
    w, h, r = max(2, int(w)), max(2, int(h)), int(r)
    key = ("r", w, h, r, fill, outline)
    im = _IC.get(key)
    if im is not None:
        return im
    if len(_IC) > 800:
        _IC.clear()
    ss = 3
    layer = Image.new("RGBA", (w * ss, h * ss), (0, 0, 0, 0))
    kw = {"outline": outline, "width": ss} if outline else {}
    ImageDraw.Draw(layer).rounded_rectangle((0, 0, w * ss - 1, h * ss - 1), radius=r * ss, fill=fill, **kw)
    layer = layer.resize((w, h), RS.LANCZOS)
    ph = ImageTk.PhotoImage(layer)
    _IC[key] = ph
    return ph


def render_bar(w, frac, thick, knob, a8):
    pad, H, ss = 12, 28, 3
    im = Image.new("RGBA", ((w + 2 * pad) * ss, H * ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    cy = H * ss / 2
    th = thick * ss
    x0, x1 = pad * ss, (pad + w) * ss
    d.rounded_rectangle((x0, cy - th / 2, x1, cy + th / 2), radius=th / 2, fill=(255, 255, 255, 62))
    fx = x0 + (x1 - x0) * frac
    if frac > 0:
        d.rounded_rectangle((x0, cy - th / 2, max(fx, x0 + th), cy + th / 2), radius=th / 2, fill=(255, 255, 255, 240))
    if knob > 0.6:
        r = knob * ss
        d.ellipse((fx - r, cy - r, fx + r, cy + r), fill=(255, 255, 255, 255))
    im = im.resize((w + 2 * pad, H), RS.LANCZOS)
    if a8 < 8:
        im.putalpha(im.getchannel("A").point(lambda v: v * a8 // 8))
    return ImageTk.PhotoImage(im)


# ----------------------------------------------------------------- button
class Btn:
    def __init__(self, app, name, cb, kind="icon", label="", w=40, h=40):
        self.name, self.cb, self.kind, self.label = name, cb, kind, label
        self.cx = self.cy = 0.0
        self.w, self.h = w, h
        self.hov = Spring(0, 320, 26)
        self.prs = Spring(0, 520, 30)
        self.img = app.cv.create_image(0, 0, anchor="center", state="hidden")
        self.txt = None
        if kind in ("pill", "field"):
            self.txt = app.cv.create_text(0, 0, text=label, anchor="center" if kind == "pill" else "w",
                                          fill="#ffffff", font=app.F["pill" if kind == "pill" else "field"],
                                          state="hidden")
        self.on = False
        self.icon = name
        self.size = 32
        self.color = (255, 255, 255)
        self.alpha = 1.0
        self.circle = False
        self.active = False

    def hit(self, x, y, pad=6):
        return self.on and self.alpha > 0.3 and abs(x - self.cx) <= self.w / 2 + pad and abs(y - self.cy) <= self.h / 2 + pad


# -------------------------------------------------------------------- app
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Player")
        self.geometry("1180x720")
        self.minsize(860, 560)
        self.configure(bg="#08080b")
        try:
            pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=2048)
        except pygame.error:
            pass
        self.cv = tk.Canvas(self, bg="#08080b", highlightthickness=0, bd=0)
        self.cv.pack(fill="both", expand=True)
        self._cfg, self._pos = {}, {}
        self._ell = {}
        self._init_fonts()
        self._init_state()
        self._init_items()
        self._bind_events()
        self._dark_titlebar()
        self._load_state()
        self.last_t = time.perf_counter()
        self.after(30, self._frame)
        self.protocol("WM_DELETE_WINDOW", self._close)

    # ------------------------------------------------------------ setup
    def _init_fonts(self):
        fams = set(tkfont.families())
        self.fam = None
        for f in ("SF Pro Display", "Segoe UI Variable Display", "Segoe UI Variable Text",
                  "Segoe UI", "Helvetica Neue", "Inter", "Arial"):
            if f in fams:
                self.fam = f
                break
        if not self.fam:
            self.fam = tkfont.nametofont("TkDefaultFont").actual()["family"]

        def mk(px, w="normal"):
            return tkfont.Font(family=self.fam, size=-int(px), weight=w)

        self.F = dict(label=mk(13, "bold"), t0=mk(36, "bold"), t1=mk(31, "bold"), t2=mk(26, "bold"),
                      artist=mk(22), time=mk(12), pill=mk(13, "bold"), dt=mk(22, "bold"), ds=mk(13),
                      rt=mk(15, "bold"), rs=mk(13), rd=mk(13), field=mk(14), empty=mk(15))
        self._fpx = None

    def _sync_fonts(self, tpx, apx):
        if self._fpx == (tpx, apx):
            return
        self._fpx = (tpx, apx)
        self.F["t0"].configure(size=-tpx)
        self.F["t1"].configure(size=-int(tpx * 0.86))
        self.F["t2"].configure(size=-int(tpx * 0.74))
        self.F["artist"].configure(size=-apx)
        self._ell.clear()

    def _init_state(self):
        self.W, self.H = 1180, 720
        self.time = 0.0
        self.playlist, self.visible = [], []
        self.current = -1
        self.length, self.offset = 0.0, 0.0
        self.playing = self.paused = False
        self.shuffle, self.repeat = False, 0
        self.volume, self.premute = 0.7, 0.7
        self.fs = False
        self.fail_count = 0
        pygame.mixer.music.set_volume(self.volume)

        self.samples, self.sr, self.an_path, self.an_token = None, 44100, None, 0
        self.win = np.hanning(2048).astype(np.float32)
        self.tilt = np.linspace(0, 16, NB).astype(np.float32)
        self._make_edges(44100)
        self.bands = np.zeros(NB, dtype=np.float32)
        self.bass = 0.0

        self.vis_q, self.add_q = queue.Queue(), queue.Queue()
        self.vis_token, self.vis_path = 0, None
        self.vis = build_visual(placeholder_cover("ambient"))
        self.vis_prev, self.vis_t = None, 1.0
        self.acc, self.acc2, self.bgc = self.vis["acc"], self.vis["acc2"], self.vis["bgc"]

        self.art_sp = Spring(0.86, 150, 15)
        self.pop = Spring(0.0, 130, 9)
        self.bar_h = Spring(0.0, 260, 24)
        self.vol_h = Spring(0.0, 260, 24)
        self.dp = Spring(0.0, 150, 24.5)
        self.ui_a = 1.0
        self.last_move = time.time()
        self.mx = self.my = -1
        self.down = None
        self.drag = None
        self.drag_frac = 0.0
        self.hot = None

        self.txt_old = {"title": "", "artist": ""}
        self.txt_new = {"title": "Nada tocando", "artist": "Adicione músicas para começar"}
        self.txt_t = 1.0

        self.search_mode, self.query = False, ""
        self.scroll = self.scroll_t = 0.0
        self.hover_row = -1
        self.restore_path = None
        self.pl_info = ""

        self.viz_key = None
        self.art_key = None
        self.bg_key = None
        self.bar_keys = {}
        self.L = {}
        self.ow = self.oh = 0
        self.bg_photo = None
        self.vig = None
        self.last_bg = 0.0
        self.panel_size = None

    def _make_edges(self, sr):
        binhz = sr / 2048.0
        e = np.geomspace(45, 13000, NB + 1) / binhz
        e = np.clip(e.astype(int), 1, 1023)
        for i in range(1, len(e)):
            if e[i] <= e[i - 1]:
                e[i] = min(1023, e[i - 1] + 1)
        self.edges = e

    def _init_items(self):
        cv = self.cv
        self.bg_item = cv.create_image(0, 0, anchor="nw")
        self.viz_item = cv.create_image(0, 0, anchor="s")
        self.art_item = cv.create_image(0, 0, anchor="center")
        self.t_label = cv.create_text(0, 0, anchor="nw", text="", font=self.F["label"], fill="#fff")
        self.t_title = cv.create_text(0, 0, anchor="nw", text="", font=self.F["t0"], fill="#fff")
        self.t_artist = cv.create_text(0, 0, anchor="nw", text="", font=self.F["artist"], fill="#fff")
        self.t_left = cv.create_text(0, 0, anchor="nw", text="", font=self.F["time"], fill="#fff")
        self.t_right = cv.create_text(0, 0, anchor="ne", text="", font=self.F["time"], fill="#fff")
        self.bar_item = cv.create_image(0, 0, anchor="w")
        self.vol_item = cv.create_image(0, 0, anchor="w")

        self.b_shuffle = Btn(self, "shuffle", self.toggle_shuffle)
        self.b_prev = Btn(self, "prev", self.prev)
        self.b_play = Btn(self, "play", self.toggle_play)
        self.b_next = Btn(self, "next", self.next)
        self.b_repeat = Btn(self, "repeat", self.toggle_repeat)
        self.b_mute = Btn(self, "vol3", self.toggle_mute)
        self.b_queue = Btn(self, "queue", self.toggle_drawer)
        self.b_full = Btn(self, "full", self.toggle_fullscreen)
        self.mbtns = [self.b_shuffle, self.b_prev, self.b_play, self.b_next, self.b_repeat,
                      self.b_mute, self.b_queue, self.b_full]

        self.d_panel = cv.create_image(0, 0, anchor="nw", state="hidden")
        self.d_hl_hover = cv.create_image(0, 0, anchor="nw", state="hidden")
        self.d_hl_cur = cv.create_image(0, 0, anchor="nw", state="hidden")
        self.d_title = cv.create_text(0, 0, anchor="nw", text="Fila", font=self.F["dt"], fill="#fff", state="hidden")
        self.d_sub = cv.create_text(0, 0, anchor="nw", text="", font=self.F["ds"], fill="#fff", state="hidden")
        self.d_empty = cv.create_text(0, 0, anchor="center", text="", font=self.F["empty"], fill="#fff",
                                      justify="center", state="hidden")
        self.d_sicon = cv.create_image(0, 0, anchor="center", state="hidden")
        self.d_eq = [cv.create_line(0, 0, 0, 0, width=3, capstyle="round", state="hidden") for _ in range(3)]
        self.b_add = Btn(self, "add", self.add_files, "pill", "Arquivos", 96, 32)
        self.b_folder = Btn(self, "folder", self.add_folder, "pill", "Pasta", 76, 32)
        self.b_clear = Btn(self, "clear", self.clear, "pill", "Limpar", 80, 32)
        self.b_close = Btn(self, "close", self.toggle_drawer)
        self.b_field = Btn(self, "field", self.enter_search, "field", "", 300, 38)
        self.dbtns = [self.b_add, self.b_folder, self.b_clear, self.b_close, self.b_field]
        self.btns = self.mbtns + self.dbtns
        self.rows = []

    def _bind_events(self):
        cv = self.cv
        cv.bind("<Configure>", self._on_resize)
        cv.bind("<Motion>", self._on_motion)
        cv.bind("<ButtonPress-1>", self._on_press)
        cv.bind("<ButtonRelease-1>", self._on_release)
        cv.bind("<Double-Button-1>", self._on_double)
        cv.bind("<Button-3>", self._on_rclick)
        cv.bind("<Leave>", lambda e: setattr(self, "mx", -1) or setattr(self, "my", -1))
        cv.bind("<MouseWheel>", lambda e: self._on_wheel(e.x, e.y, 1 if e.delta > 0 else -1))
        cv.bind("<Button-4>", lambda e: self._on_wheel(e.x, e.y, 1))
        cv.bind("<Button-5>", lambda e: self._on_wheel(e.x, e.y, -1))
        self.bind("<Key>", self._on_key)

    def _dark_titlebar(self):
        try:
            import ctypes
            self.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            for attr, val in ((20, 1), (19, 1), (35, 0x0b0808)):
                ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(ctypes.c_int(val)), 4)
        except Exception:
            pass

    # -------------------------------------------------------- canvas utils
    def cfg(self, item, **kw):
        c = self._cfg.setdefault(item, {})
        ch = {k: v for k, v in kw.items() if c.get(k) is not v and c.get(k) != v}
        if ch:
            self.cv.itemconfigure(item, **ch)
            c.update(ch)

    def put(self, item, x, y):
        p = (round(x * 2) / 2, round(y * 2) / 2)
        if self._pos.get(item) != p:
            self.cv.coords(item, *p)
            self._pos[item] = p

    def hide(self, *items):
        for it in items:
            self.cfg(it, state="hidden")

    def ellipsize(self, text, font_key, maxw):
        key = (text, font_key, int(maxw))
        r = self._ell.get(key)
        if r is not None:
            return r
        font = self.F[font_key]
        if font.measure(text) <= maxw:
            r = text
        else:
            lo, hi = 0, len(text)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if font.measure(text[:mid] + "…") <= maxw:
                    lo = mid
                else:
                    hi = mid - 1
            r = text[:lo].rstrip() + "…"
        if len(self._ell) > 600:
            self._ell.clear()
        self._ell[key] = r
        return r

    # -------------------------------------------------------- persistence
    def _save_state(self):
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump({"paths": [t["path"] for t in self.playlist], "current": self.current,
                           "volume": self.volume, "shuffle": self.shuffle, "repeat": self.repeat}, f)
        except Exception:
            pass

    def _load_state(self):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            self.dp.t = 1.0
            return
        self.volume = clamp(float(d.get("volume", 0.7)))
        self.premute = self.volume or 0.7
        pygame.mixer.music.set_volume(self.volume)
        self.shuffle = bool(d.get("shuffle", False))
        self.repeat = int(d.get("repeat", 0)) % 3
        paths = [p for p in d.get("paths", []) if os.path.exists(p)]
        cur = d.get("current", -1)
        if paths:
            if 0 <= cur < len(d.get("paths", [])) and d["paths"][cur] in paths:
                self.restore_path = d["paths"][cur]
            self._add_paths(paths)
        else:
            self.dp.t = 1.0

    def _close(self):
        self._save_state()
        try:
            pygame.mixer.quit()
        except Exception:
            pass
        self.destroy()

    # ----------------------------------------------------------- playlist
    def _meta(self, path):
        title = os.path.splitext(os.path.basename(path))[0]
        artist, album, length = "Artista desconhecido", "", 0.0
        try:
            a = MutagenFile(path, easy=True)
            if a is not None:
                length = float(a.info.length)
                if a.get("title"):
                    title = a["title"][0]
                if a.get("artist"):
                    artist = a["artist"][0]
                if a.get("album"):
                    album = a["album"][0]
        except Exception:
            pass
        return {"path": path, "title": title, "artist": artist, "album": album, "length": length}

    def _add_paths(self, paths):
        existing = {t["path"] for t in self.playlist}
        todo = [p for p in paths if p.lower().endswith(SUPPORTED) and p not in existing]
        if not todo:
            return

        def work():
            self.add_q.put([self._meta(p) for p in todo])

        threading.Thread(target=work, daemon=True).start()

    def add_files(self):
        files = filedialog.askopenfilenames(parent=self, title="Adicionar músicas",
                                            filetypes=[("Áudio", "*.mp3 *.wav *.ogg *.flac"), ("Todos", "*.*")])
        if files:
            self._add_paths(list(files))

    def add_folder(self):
        folder = filedialog.askdirectory(parent=self, title="Adicionar pasta")
        if not folder:
            return
        paths = []
        for root, _, files in os.walk(folder):
            for f in sorted(files):
                if f.lower().endswith(SUPPORTED):
                    paths.append(os.path.join(root, f))
        self._add_paths(paths)

    def clear(self):
        self.stop()
        self.playlist.clear()
        self.current = -1
        self.vis_path = None
        self.vis_token += 1
        self._refilter()
        self._set_text()
        self._save_state()

    def remove(self, idx):
        if not (0 <= idx < len(self.playlist)):
            return
        was_cur = idx == self.current
        was_playing = self.playing
        del self.playlist[idx]
        if idx < self.current:
            self.current -= 1
        elif was_cur:
            self.stop()
            if self.playlist:
                self.current = min(idx, len(self.playlist) - 1)
                if was_playing:
                    self.play_index(self.current)
                else:
                    self._request_visual(self.current)
            else:
                self.current = -1
                self._set_text()
        self._refilter()
        self._save_state()

    def _refilter(self):
        q = self.query.lower().strip()
        self.visible = [i for i, t in enumerate(self.playlist)
                        if not q or q in t["title"].lower() or q in t["artist"].lower() or q in t["album"].lower()]
        total = sum(t["length"] for t in self.playlist)
        n = len(self.playlist)
        mins = int(total // 60)
        dur = f"{mins // 60} h {mins % 60} min" if mins >= 60 else f"{mins} min"
        self.pl_info = f"{n} {'música' if n == 1 else 'músicas'} · {dur}" if n else "Vazia"
        self.scroll_t = 0.0 if q else self.scroll_t

    def _poll_queues(self):
        try:
            while True:
                lst = self.add_q.get_nowait()
                self.playlist.extend(lst)
                self._refilter()
                if self.current == -1 and self.playlist:
                    self.current = 0
                    if self.restore_path:
                        for i, t in enumerate(self.playlist):
                            if t["path"] == self.restore_path:
                                self.current = i
                                break
                    self.length = self.playlist[self.current]["length"]
                    self._request_visual(self.current)
                    self._scroll_to_current()
                self._save_state()
        except queue.Empty:
            pass
        try:
            while True:
                token, vis = self.vis_q.get_nowait()
                if token == self.vis_token:
                    self._apply_visual(vis)
        except queue.Empty:
            pass

    # ------------------------------------------------------------- visuals
    def _request_visual(self, i):
        if not (0 <= i < len(self.playlist)):
            return
        path = self.playlist[i]["path"]
        if path == self.vis_path:
            return
        self.vis_path = path
        self.vis_token += 1
        token = self.vis_token

        def work():
            try:
                cover = extract_cover(path) or placeholder_cover(path)
                vis = build_visual(cover)
                self.vis_q.put((token, vis))
            except Exception:
                pass

        threading.Thread(target=work, daemon=True).start()

    def _apply_visual(self, vis):
        self.vis_prev = self.vis
        self.vis = vis
        self.vis_t = 0.0
        self.pop.x = -0.1
        self.pop.t = 0.0
        self.art_key = None
        self._set_text()

    def _current_text(self):
        if 0 <= self.current < len(self.playlist):
            t = self.playlist[self.current]
            return {"title": t["title"], "artist": t["artist"]}
        return {"title": "Nada tocando", "artist": "Adicione músicas para começar"}

    def _set_text(self):
        new = self._current_text()
        if new != self.txt_new:
            self.txt_old = self.txt_new
            self.txt_new = new
            self.txt_t = 0.0

    # --------------------------------------------------------------- audio
    def _request_analysis(self, path):
        if path == self.an_path and self.samples is not None:
            return
        self.samples = None
        self.an_path = path
        self.an_token += 1
        token = self.an_token

        def work():
            try:
                if os.path.getsize(path) > 70 * 1024 * 1024:
                    return
                snd = pygame.mixer.Sound(path)
                raw = snd.get_raw()
                freq, _, ch = pygame.mixer.get_init()
                arr = np.frombuffer(raw, dtype=np.int16)
                if ch == 2:
                    arr = ((arr[0::2].astype(np.int32) + arr[1::2].astype(np.int32)) // 2).astype(np.int16)
                if token == self.an_token:
                    self.sr = freq
                    self._make_edges(freq)
                    self.samples = arr
                    if self.length <= 0:
                        self.length = len(arr) / float(freq)
            except Exception:
                pass

        threading.Thread(target=work, daemon=True).start()

    def _fft(self, pos):
        s = self.samples
        if s is None:
            return None
        i = int(max(0.0, pos - 0.04) * self.sr)
        seg = s[i:i + 2048]
        if len(seg) < 2048:
            seg = np.pad(seg, (0, 2048 - len(seg)))
        sp = np.abs(np.fft.rfft(seg.astype(np.float32) * self.win)) / (32768.0 * 512.0)
        out = np.empty(NB, dtype=np.float32)
        e = self.edges
        for k in range(NB):
            part = sp[e[k]:e[k + 1]]
            out[k] = part.max() * 0.6 + part.mean() * 0.4
        db = 20 * np.log10(out + 1e-7) + self.tilt
        v = np.clip((db + 74) / 56.0, 0, 1)
        return v ** 1.3

    def _fake(self, t):
        i = np.arange(NB, dtype=np.float32)
        e = 0.55 + 0.45 * math.sin(t * 3.1) ** 2
        v = (0.5 + 0.5 * np.sin(t * (1.1 + 0.23 * i) + i * 1.9)) * (0.85 - 0.55 * i / NB) * e
        return np.clip(v * 0.9, 0, 1).astype(np.float32)

    def _update_audio(self, dt):
        if self.playing and not self.paused:
            tgt = self._fft(self._position())
            if tgt is None:
                tgt = self._fake(self.time)
        else:
            tgt = np.zeros(NB, dtype=np.float32)
        up, dn = 1 - math.exp(-dt * 26), 1 - math.exp(-dt * 6)
        k = np.where(tgt > self.bands, up, dn)
        self.bands += (tgt - self.bands) * k
        self.bass += (float(self.bands[:4].mean()) - self.bass) * (1 - math.exp(-dt * 12))

    # ------------------------------------------------------------ playback
    def _start_stream(self, start=0.0):
        t = self.playlist[self.current]
        try:
            pygame.mixer.music.load(t["path"])
            try:
                pygame.mixer.music.play(start=start)
                self.offset = start
            except pygame.error:
                pygame.mixer.music.play()
                self.offset = 0.0
            pygame.mixer.music.set_volume(self.volume)
            return True
        except pygame.error:
            return False

    def play_index(self, i, start=0.0):
        if not (0 <= i < len(self.playlist)):
            return
        self.current = i
        t = self.playlist[i]
        self.length = t["length"]
        if not self._start_stream(start):
            self.playing = self.paused = False
            self.fail_count += 1
            if self.fail_count < len(self.playlist):
                self.next(auto=True)
            return
        self.fail_count = 0
        self.playing, self.paused = True, False
        self._request_visual(i)
        self._request_analysis(t["path"])
        self._scroll_to_current()
        self.b_play.prs.x = 1.0
        self._save_state()

    def _position(self):
        if not self.playing:
            return 0.0
        return self.offset + max(0, pygame.mixer.music.get_pos()) / 1000.0

    def seek(self, pos):
        if self.current < 0:
            return
        pos = clamp(pos, 0, max(0.0, self.length - 0.4))
        was_paused = self.paused
        if self._start_stream(pos):
            self.playing = True
            self.paused = False
            if was_paused:
                pygame.mixer.music.pause()
                self.paused = True
            if self.samples is None:
                self._request_analysis(self.playlist[self.current]["path"])

    def toggle_play(self):
        if not self.playlist:
            self.dp.t = 1.0
            return
        if self.current < 0:
            self.play_index(random.randrange(len(self.playlist)) if self.shuffle else 0)
        elif not self.playing:
            self.play_index(self.current)
        elif self.paused:
            pygame.mixer.music.unpause()
            self.paused = False
        else:
            pygame.mixer.music.pause()
            self.paused = True
        self.b_play.prs.x = 1.0

    def stop(self):
        try:
            pygame.mixer.music.stop()
        except pygame.error:
            pass
        self.playing = self.paused = False
        self.offset = 0.0

    def next(self, auto=False):
        n = len(self.playlist)
        if not n:
            return
        if self.shuffle and n > 1:
            nxt = random.choice([i for i in range(n) if i != self.current])
        else:
            nxt = self.current + 1
            if nxt >= n:
                if self.repeat == 1 or not auto:
                    nxt = 0
                else:
                    self.stop()
                    return
        self.play_index(nxt)

    def prev(self):
        n = len(self.playlist)
        if not n:
            return
        if self._position() > 3:
            self.seek(0)
        else:
            self.play_index((self.current - 1) % n)

    def skip(self, secs):
        if self.current >= 0 and self.playing:
            self.seek(self._position() + secs)

    def toggle_shuffle(self):
        self.shuffle = not self.shuffle
        self._save_state()

    def toggle_repeat(self):
        self.repeat = (self.repeat + 1) % 3
        self._save_state()

    def _set_volume(self, v):
        self.volume = clamp(v)
        pygame.mixer.music.set_volume(self.volume)

    def toggle_mute(self):
        if self.volume > 0:
            self.premute = self.volume
            self._set_volume(0)
        else:
            self._set_volume(self.premute or 0.5)

    def toggle_drawer(self):
        self.dp.t = 0.0 if self.dp.t > 0.5 else 1.0
        if self.dp.t == 0.0:
            self.search_mode = False

    def toggle_fullscreen(self):
        self.fs = not self.fs
        self.attributes("-fullscreen", self.fs)

    def enter_search(self):
        self.search_mode = True
        self.dp.t = 1.0

    def _scroll_to_current(self):
        if self.current in self.visible:
            j = self.visible.index(self.current)
            self.scroll_t = max(0.0, j * 62 - 150)

    # -------------------------------------------------------------- input
    def _on_resize(self, e):
        if e.width < 50 or e.height < 50:
            return
        self.W, self.H = e.width, e.height
        self.ow, self.oh = max(120, self.W // 4), max(80, self.H // 4)
        self.bg_photo = ImageTk.PhotoImage(Image.new("RGB", (self.W, self.H), (8, 8, 11)))
        self.cv.itemconfigure(self.bg_item, image=self.bg_photo)
        self.vig = make_vignette(self.ow, self.oh)
        self.viz_key = self.art_key = self.bg_key = None
        self.last_bg = 0.0
        self.panel_size = None

    def _on_motion(self, e):
        self.mx, self.my = e.x, e.y
        self.last_move = time.time()
        if self.drag == "seek":
            x, y, w, _ = self.hb
            self.drag_frac = clamp((e.x - x) / w)
        elif self.drag == "vol":
            x, y, w, _ = self.hv
            self._set_volume((e.x - x) / w)

    def _hot_btn(self):
        for b in reversed(self.btns):
            if b.hit(self.mx, self.my):
                return b
        return None

    def _row_at(self, x, y):
        g = getattr(self, "list_geo", None)
        if not g or self.dp.x < 0.5:
            return -1
        px, lt, lb = g
        if x < px or not (lt <= y <= lb):
            return -1
        j = int((y - lt + self.scroll) // 62)
        return j if 0 <= j < len(self.visible) else -1

    def _on_press(self, e):
        self.mx, self.my = e.x, e.y
        self.last_move = time.time()
        b = self._hot_btn()
        if b:
            self.down = b
            return
        if self.ui_a > 0.5:
            x, y, w, _ = self.hb
            if x - 12 <= e.x <= x + w + 12 and abs(e.y - y) <= 16:
                self.drag = "seek"
                self.drag_frac = clamp((e.x - x) / w)
                return
            x, y, w, _ = self.hv
            if x - 12 <= e.x <= x + w + 12 and abs(e.y - y) <= 14:
                self.drag = "vol"
                self._set_volume((e.x - x) / w)
                return
        j = self._row_at(e.x, e.y)
        if j >= 0:
            self.play_index(self.visible[j])

    def _on_release(self, e):
        if self.down is not None:
            b, self.down = self.down, None
            if b.hit(e.x, e.y):
                b.cb()
        if self.drag == "seek":
            self.drag = None
            if self.length > 0 and self.current >= 0:
                self.seek(self.drag_frac * self.length)
        else:
            self.drag = None

    def _on_double(self, e):
        L = self.L
        if L and L["ax"] <= e.x <= L["ax"] + L["s"] and L["ay"] <= e.y <= L["ay"] + L["s"]:
            self.toggle_fullscreen()

    def _on_rclick(self, e):
        j = self._row_at(e.x, e.y)
        if j >= 0:
            self.remove(self.visible[j])

    def _on_wheel(self, x, y, d):
        g = getattr(self, "list_geo", None)
        if g and self.dp.x > 0.5 and x >= g[0]:
            self.scroll_t -= d * 124
        else:
            self._set_volume(self.volume + d * 0.04)
        self.last_move = time.time()

    def _on_key(self, e):
        self.last_move = time.time()
        ks = e.keysym
        if self.search_mode:
            if ks == "Escape":
                self.search_mode = False
                self.query = ""
                self._refilter()
            elif ks == "Return":
                self.search_mode = False
            elif ks == "BackSpace":
                self.query = self.query[:-1]
                self._refilter()
            elif e.char and e.char.isprintable():
                self.query += e.char
                self._refilter()
            return "break"
        k = ks.lower()
        if k == "space":
            self.toggle_play()
        elif k == "right":
            self.skip(5)
        elif k == "left":
            self.skip(-5)
        elif k == "up":
            self._set_volume(self.volume + 0.05)
        elif k == "down":
            self._set_volume(self.volume - 0.05)
        elif k == "n":
            self.next()
        elif k == "p":
            self.prev()
        elif k == "s":
            self.toggle_shuffle()
        elif k == "r":
            self.toggle_repeat()
        elif k == "m":
            self.toggle_mute()
        elif k in ("q", "tab"):
            self.toggle_drawer()
        elif k in ("f", "f11"):
            self.toggle_fullscreen()
        elif k in ("slash",) or (k == "f" and e.state & 4):
            self.enter_search()
        elif k == "o":
            self.add_files()
        elif k == "escape":
            if self.fs:
                self.toggle_fullscreen()
            elif self.dp.t > 0.5:
                self.toggle_drawer()
        return "break"

    # --------------------------------------------------------- main loop
    def _frame(self):
        t0 = time.perf_counter()
        dt = min(0.066, max(0.001, t0 - self.last_t))
        self.last_t = t0
        self.time += dt
        try:
            self._tick(dt)
        except Exception:
            import traceback
            traceback.print_exc()
        el = (time.perf_counter() - t0) * 1000
        self.after(max(1, int(FRAME_MS - el)), self._frame)

    def _tick(self, dt):
        self._poll_queues()

        if self.playing and not self.paused and not pygame.mixer.music.get_busy():
            self.playing = False
            if self.repeat == 2:
                self.play_index(self.current)
            else:
                self.next(auto=True)

        moving = self.playing and not self.paused
        self.art_sp.t = 1.0 if moving else 0.86
        for s in (self.art_sp, self.pop, self.dp):
            s.step(dt)
        if self.vis_t < 1.0:
            self.vis_t = min(1.0, self.vis_t + dt / 1.1)
            if self.vis_t >= 1.0:
                self.vis_prev = None
        e = ease_io(self.vis_t)
        if self.vis_prev is not None and self.vis_t < 1.0:
            self.acc = mixc(self.vis_prev["acc"], self.vis["acc"], e)
            self.acc2 = mixc(self.vis_prev["acc2"], self.vis["acc2"], e)
            self.bgc = mixc(self.vis_prev["bgc"], self.vis["bgc"], e)
        else:
            self.acc, self.acc2, self.bgc = self.vis["acc"], self.vis["acc2"], self.vis["bgc"]
        self.panel_c = mixc(self.bgc, (10, 10, 14), 0.62)

        idle = time.time() - self.last_move
        want_hide = moving and idle > 6 and self.dp.t < 0.5 and not self.drag and not self.search_mode
        self.ui_a += ((0.0 if want_hide else 1.0) - self.ui_a) * (1 - math.exp(-dt * 5))
        self.ui_a = clamp(self.ui_a)

        self._update_audio(dt)
        self.L = self._layout()
        self._draw_bg()
        self._draw_viz()
        self._draw_art()
        self._draw_text(dt)
        self._draw_controls(dt)
        self._draw_drawer(dt)
        self._update_buttons(dt)
        self._update_cursor(want_hide)

    def _layout(self):
        W, H = self.W, self.H
        k = H / 720.0
        pad = max(24, int(H * 0.07))
        dw = int(min(420, max(320, W * 0.31)))
        p = clamp(self.dp.x, 0, 1.0)
        avail = W - (dw + 16) * p
        gap = int(48 * max(0.7, k))
        s = int(min(H * 0.58, (avail - 2 * pad - gap) * 0.5))
        s = max(s, 150)
        colw = int(min(500 * max(0.7, k), max(avail - 2 * pad - gap - s, 240)))
        total = s + gap + colw
        ax = (avail - total) / 2
        ay = H * 0.47 - s / 2
        return {"k": k, "dw": dw, "s": s, "colw": colw, "ax": ax, "ay": ay, "xc": ax + s + gap}

    # ---------------------------------------------------------- bg / viz
    def _bg_frame(self, vis, t, ow, oh):
        Z = 230.0
        vw, vh = Z, Z * oh / ow
        if vh > Z:
            vw, vh = vw * Z / vh, Z

        def tf(layer, ph, sp):
            th = 0.22 * math.sin(t * 0.05 * sp + ph)
            cx = 192 + 34 * math.sin(t * 0.09 * sp + ph * 1.7)
            cy = 192 + 26 * math.cos(t * 0.07 * sp + ph * 0.6)
            z = 1 + 0.10 * math.sin(t * 0.06 * sp + ph * 2.3)
            w_, h_ = vw * z, vh * z
            c, s = math.cos(th), math.sin(th)
            co = (w_ / ow * c, -(h_ / oh) * s, cx - 0.5 * w_ * c + 0.5 * h_ * s,
                  w_ / ow * s, (h_ / oh) * c, cy - 0.5 * w_ * s - 0.5 * h_ * c)
            return layer.transform((ow, oh), TR.AFFINE, co, RS.BILINEAR)

        A = tf(vis["L1"], 0.0, 1.0)
        B = tf(vis["L2"], 2.1, 0.8)
        return Image.blend(A, B, 0.5 + 0.3 * math.sin(t * 0.11))

    def _draw_bg(self):
        if self.bg_photo is None or self.vig is None:
            return
        now = self.time
        if self.vis_t >= 1.0 and now - self.last_bg < 0.075:
            return
        self.last_bg = now
        ow, oh = self.ow, self.oh
        f = self._bg_frame(self.vis, now, ow, oh)
        if self.vis_prev is not None and self.vis_t < 1.0:
            f = Image.blend(self._bg_frame(self.vis_prev, now, ow, oh), f, ease_io(self.vis_t))
        f = ImageChops.multiply(f, self.vig)
        self.bg_photo.paste(f.resize((self.W, self.H), RS.BILINEAR))

    def _draw_viz(self):
        W, H = self.W, self.H
        b = self.bands
        key = (tuple((b * 36).astype(int)), W, H, self.acc, self.acc2)
        if key == self.viz_key:
            return
        self.viz_key = key
        vh = int(H * 0.2)
        span = int(min(W * 0.9, 1500))
        n = NB * 2
        slot = span / n
        bw = max(4.0, slot * 0.56)
        ss = 2
        img_h = vh + int(bw)
        im = Image.new("RGBA", (span * ss, img_h * ss), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        for i in range(n):
            kb = NB - 1 - i if i < NB else i - NB
            v = float(b[kb])
            h = bw + v * (vh * 0.92 - bw)
            x0 = (i * slot + (slot - bw) / 2) * ss
            x1 = x0 + bw * ss
            y1 = (vh + bw * 0.5) * ss
            y0 = y1 - (h + bw * 0.5) * ss
            u = abs(i - (n - 1) / 2) / ((n - 1) / 2)
            col = mixc(self.acc, self.acc2, u)
            d.rounded_rectangle((x0, y0, x1, y1), radius=bw * ss / 2, fill=col + (int(105 + 120 * v),))
        im = im.resize((span, img_h), RS.BOX)
        ph = ImageTk.PhotoImage(im)
        self.cv.itemconfigure(self.viz_item, image=ph)
        self.viz_photo = ph
        self.put(self.viz_item, W / 2, H + bw)

    def _draw_art(self):
        L = self.L
        s = L["s"]
        sc = self.art_sp.x * (1 + 0.035 * self.bass) + self.pop.x
        cs = max(16, int(s * 1.5 * sc))
        self.put(self.art_item, L["ax"] + s / 2, L["ay"] + s / 2)
        key = (cs, round(self.vis_t, 2), id(self.vis))
        if key == self.art_key:
            return
        self.art_key = key
        img = self.vis["art"].resize((cs, cs), RS.BILINEAR)
        if self.vis_prev is not None and self.vis_t < 1.0:
            old = self.vis_prev["art"].resize((cs, cs), RS.BILINEAR)
            img = Image.blend(old, img, ease_io(self.vis_t))
        ph = ImageTk.PhotoImage(img)
        self.cv.itemconfigure(self.art_item, image=ph)
        self.art_photo = ph

    # --------------------------------------------------------------- text
    def _draw_text(self, dt):
        L = self.L
        s, xc, colw, ay = L["s"], L["xc"], L["colw"], L["ay"]
        ua = self.ui_a
        if self.txt_t < 1.0:
            self.txt_t = min(1.0, self.txt_t + dt / 0.9)
            t = self.txt_t
            if t < 0.35:
                a, yo, src = 1 - ease_out(t / 0.35), -10 * ease_out(t / 0.35), self.txt_old
            else:
                u = ease_out((t - 0.35) / 0.65)
                a, yo, src = u, 14 * (1 - u), self.txt_new
        else:
            a, yo, src = 1.0, 0.0, self.txt_new

        tpx = int(clamp(s * 0.085, 22, 54))
        apx = max(14, int(tpx * 0.62))
        self._sync_fonts(tpx, apx)
        shift = (1 - ua) * s * 0.18
        y0 = ay + s * 0.10 + shift + yo

        label = "Pausado" if self.paused else ("Tocando agora" if self.playing else "Pronto")
        self.cfg(self.t_label, text=label.upper() if False else label, fill=hexc(mixc(self.bgc, mixc(self.acc, (255, 255, 255), 0.35), 1.0)))
        self.put(self.t_label, xc, ay + s * 0.035 + shift)

        title, artist = src["title"], src["artist"]
        key = "t2"
        for kk in ("t0", "t1", "t2"):
            if self.F[kk].measure(title) <= colw:
                key = kk
                break
        self.cfg(self.t_title, text=self.ellipsize(title, key, colw), font=self.F[key],
                 fill=hexc(mixc(self.bgc, (255, 255, 255), a)))
        self.put(self.t_title, xc, y0)
        self.cfg(self.t_artist, text=self.ellipsize(artist, "artist", colw),
                 fill=hexc(mixc(self.bgc, mixc(self.bgc, (255, 255, 255), 0.72), a)))
        self.put(self.t_artist, xc, y0 + tpx * 1.45)

    # ----------------------------------------------------------- controls
    def _bar_image(self, tag, w, frac, thick, knob, a8):
        key = (w, int(frac * w * 2), int(thick * 2), int(knob), a8)
        if self.bar_keys.get(tag) != key:
            self.bar_keys[tag] = key
            setattr(self, "ph_" + tag, render_bar(w, frac, thick, knob, a8))
        return getattr(self, "ph_" + tag)

    def _draw_controls(self, dt):
        L = self.L
        s, xc, colw, ay = L["s"], L["xc"], L["colw"], L["ay"]
        ua = self.ui_a
        a8 = int(round(ua * 8))
        vis_on = a8 > 0
        yo = (1 - ua) * 12
        by = ay + s * 0.62 + yo
        pos = self._position()
        length = self.length
        if self.drag == "seek":
            frac = self.drag_frac
            shown = frac * length
        else:
            frac = clamp(pos / length) if length > 0 else 0.0
            shown = pos
        hovering = self.mx >= 0 and xc - 12 <= self.mx <= xc + colw + 12 and abs(self.my - by) <= 16 and ua > 0.5
        self.bar_h.t = 1.0 if (hovering or self.drag == "seek") else 0.0
        self.bar_h.step(dt)
        thick = 5 + 4 * self.bar_h.x
        knob = 7 * self.bar_h.x
        self.hb = (xc, by, colw, 0)
        if vis_on:
            ph = self._bar_image("bar", int(colw), frac, thick, knob, a8)
            self.cfg(self.bar_item, image=ph, state="normal")
            self.put(self.bar_item, xc - 12, by)
            tcol = hexc(mixc(self.bgc, mixc(self.bgc, (255, 255, 255), 0.7), ua))
            self.cfg(self.t_left, state="normal", text=fmt(shown), fill=tcol)
            self.cfg(self.t_right, state="normal", text="-" + fmt(max(0, length - shown)) if length > 0 else "-:--", fill=tcol)
            self.put(self.t_left, xc, by + 14)
            self.put(self.t_right, xc + colw, by + 14)
        else:
            self.hide(self.bar_item, self.t_left, self.t_right)

        k = L["k"]
        small = int(clamp(s * 0.07, 22, 34))
        mid = int(clamp(s * 0.105, 30, 48))
        big = int(clamp(s * 0.17, 54, 84))
        cy = ay + s * 0.79 + yo
        sp = min(colw / 5.0, 96 * max(0.8, k))
        cxm = xc + colw / 2
        light = mixc(self.bgc, (255, 255, 255), 0.62)
        accl = mixc(self.acc, (255, 255, 255), 0.25)
        spec = [(self.b_shuffle, -2, small, "shuffle", accl if self.shuffle else light),
                (self.b_prev, -1, mid, "prev", (255, 255, 255)),
                (self.b_play, 0, big, "pause" if (self.playing and not self.paused) else "play", (255, 255, 255)),
                (self.b_next, 1, mid, "next", (255, 255, 255)),
                (self.b_repeat, 2, small, "repeat1" if self.repeat == 2 else "repeat", accl if self.repeat else light)]
        for b, off, size, icon, col in spec:
            b.cx, b.cy = cxm + off * sp, cy
            b.size, b.icon, b.color = size, icon, col
            b.w = b.h = size * (1.3 if b is not self.b_play else 1.0)
            b.alpha, b.on = ua, vis_on
            b.circle = b is self.b_play

        vy = ay + s * 0.97 + yo
        vw = int(colw * 0.46)
        lv = 0 if self.volume <= 0.001 else 1 if self.volume < 0.34 else 2 if self.volume < 0.67 else 3
        self.b_mute.cx, self.b_mute.cy = xc + 14, vy
        self.b_mute.size, self.b_mute.icon, self.b_mute.color = 26, f"vol{lv}", light
        self.b_mute.w = self.b_mute.h = 34
        self.b_mute.alpha, self.b_mute.on = ua, vis_on
        vx = xc + 40
        self.hv = (vx, vy, vw, 0)
        vh_ = (self.mx >= 0 and vx - 12 <= self.mx <= vx + vw + 12 and abs(self.my - vy) <= 14 and ua > 0.5)
        self.vol_h.t = 1.0 if (vh_ or self.drag == "vol") else 0.0
        self.vol_h.step(dt)
        if vis_on:
            ph = self._bar_image("vol", vw, self.volume, 4 + 2 * self.vol_h.x, 6 * self.vol_h.x, a8)
            self.cfg(self.vol_item, image=ph, state="normal")
            self.put(self.vol_item, vx - 12, vy)
        else:
            self.hide(self.vol_item)
        for b, off, name in ((self.b_full, 0, "full"), (self.b_queue, 1, "queue")):
            b.cx, b.cy = xc + colw - 16 - off * 46, vy
            b.size, b.icon = 26, name
            b.color = accl if (b is self.b_queue and self.dp.t > 0.5) else light
            b.w = b.h = 34
            b.alpha, b.on = ua, vis_on
            b.circle = False

    # ------------------------------------------------------------- drawer
    def _row_items(self, n):
        while len(self.rows) <= n:
            cv = self.cv
            self.rows.append({
                "num": cv.create_text(0, 0, anchor="center", font=self.F["rs"], fill="#fff", state="hidden"),
                "title": cv.create_text(0, 0, anchor="w", font=self.F["rt"], fill="#fff", state="hidden"),
                "sub": cv.create_text(0, 0, anchor="w", font=self.F["rs"], fill="#fff", state="hidden"),
                "dur": cv.create_text(0, 0, anchor="e", font=self.F["rd"], fill="#fff", state="hidden"),
            })
        return self.rows[n]

    def _draw_drawer(self, dt):
        W, H = self.W, self.H
        dw = self.L["dw"]
        dp = self.dp.x
        show = dp > 0.01
        for b in self.dbtns:
            b.on = show
            b.alpha = 1.0
        d_items = [self.d_panel, self.d_hl_hover, self.d_hl_cur, self.d_title, self.d_sub, self.d_empty,
                   self.d_sicon] + self.d_eq
        if not show:
            self.hide(*d_items)
            for r in self.rows:
                self.hide(*r.values())
            self.list_geo = None
            return
        px = W - (dw + 16) * clamp(dp, 0, 1.02)
        ph_h = H - 32
        if self.panel_size != (dw, ph_h):
            self.panel_size = (dw, ph_h)
            self.panel_img = get_round(dw, ph_h, 30, (16, 16, 22, 178), (255, 255, 255, 34))
        self.cfg(self.d_panel, image=self.panel_img, state="normal")
        self.put(self.d_panel, px, 16)

        pc = self.panel_c
        white = (255, 255, 255)
        muted = mixc(pc, white, 0.6)
        self.cfg(self.d_title, state="normal", fill=hexc(white))
        self.put(self.d_title, px + 24, 34)
        self.cfg(self.d_sub, state="normal", text=self.pl_info, fill=hexc(muted))
        self.put(self.d_sub, px + 24, 66)
        self.b_close.cx, self.b_close.cy, self.b_close.size = px + dw - 36, 48, 20
        self.b_close.w = self.b_close.h = 34
        self.b_close.color = mixc(pc, white, 0.75)
        self.b_close.icon = "close"
        x = px + 24
        for b in (self.b_add, self.b_folder, self.b_clear):
            b.w = self.F["pill"].measure(b.label) + 32
            b.h = 32
            b.cx, b.cy = x + b.w / 2, 112
            x += b.w + 8
        fw = dw - 48
        self.b_field.w, self.b_field.h = fw, 38
        self.b_field.cx, self.b_field.cy = px + 24 + fw / 2, 160
        caret = "|" if (self.search_mode and int(self.time * 2) % 2 == 0) else " "
        ftxt = (self.query + caret) if self.search_mode else (self.query or "Buscar  ( / )")
        ftxt = self.ellipsize(ftxt, "field", fw - 60)
        self.cfg(self.b_field.txt, text=ftxt, state="normal",
                 fill=hexc(white if (self.search_mode or self.query) else muted))
        self.put(self.b_field.txt, px + 24 + 40, 160)
        self.cfg(self.d_sicon, image=get_icon("search", 18, mixc(pc, white, 0.7)), state="normal")
        self.put(self.d_sicon, px + 24 + 22, 160)

        lt, lb = 192, H - 30
        self.list_geo = (px, lt, lb)
        rowh = 62
        n = len(self.visible)
        listH = lb - lt
        maxs = max(0, n * rowh - listH)
        self.scroll_t = clamp(self.scroll_t, 0, maxs)
        self.scroll += (self.scroll_t - self.scroll) * (1 - math.exp(-dt * 12))
        self.hover_row = self._row_at(self.mx, self.my) if self.mx >= 0 else -1

        if n == 0:
            msg = "Nenhuma música\nAdicione arquivos ou uma pasta" if not self.playlist else "Nada encontrado"
            self.cfg(self.d_empty, text=msg, state="normal", fill=hexc(muted))
            self.put(self.d_empty, px + dw / 2, (lt + lb) / 2)
        else:
            self.hide(self.d_empty)

        first = max(0, int(self.scroll // rowh))
        cnt = int(listH // rowh) + 3
        used = 0
        hov_done = cur_done = False
        eq_done = False
        accl = mixc(self.acc, white, 0.3)
        for j in range(first, min(n, first + cnt)):
            idx = self.visible[j]
            tr = self.playlist[idx]
            y0 = lt + j * rowh - self.scroll
            cyr = y0 + rowh / 2
            f = clamp(min(cyr - lt, lb - cyr) / 30.0)
            r = self._row_items(used)
            used += 1
            if f <= 0.02:
                self.hide(*r.values())
                continue
            is_cur = idx == self.current
            if j == self.hover_row and not hov_done and f > 0.5:
                self.cfg(self.d_hl_hover, image=get_round(dw - 24, rowh - 8, 16, (255, 255, 255, 30)), state="normal")
                self.put(self.d_hl_hover, px + 12, y0 + 4)
                hov_done = True
            if is_cur and f > 0.5:
                self.cfg(self.d_hl_cur, image=get_round(dw - 24, rowh - 8, 16, self.acc + (46,)), state="normal")
                self.put(self.d_hl_cur, px + 12, y0 + 4)
                cur_done = True
            tcol = mixc(pc, accl if is_cur else white, f)
            scol = mixc(pc, muted, f)
            maxw = dw - 24 - 22 - 44 - 56
            self.cfg(r["title"], state="normal", text=self.ellipsize(tr["title"], "rt", maxw), fill=hexc(tcol))
            self.cfg(r["sub"], state="normal", text=self.ellipsize(tr["artist"], "rs", maxw), fill=hexc(scol))
            self.cfg(r["dur"], state="normal", text=fmt(tr["length"]), fill=hexc(scol))
            self.put(r["title"], px + 24 + 40, y0 + rowh * 0.38)
            self.put(r["sub"], px + 24 + 40, y0 + rowh * 0.68)
            self.put(r["dur"], px + dw - 28, cyr)
            if is_cur:
                self.hide(r["num"])
                if f > 0.5:
                    eq_done = True
                    live = self.playing and not self.paused
                    for e_i, line in enumerate(self.d_eq):
                        v = float(self.bands[(2, 7, 12)[e_i]]) if live else 0.0
                        h = 5 + 15 * v
                        xx = px + 24 + 10 + e_i * 7
                        self.cfg(line, state="normal", fill=hexc(accl))
                        self.cv.coords(line, xx, cyr + h / 2, xx, cyr - h / 2)
            else:
                self.cfg(r["num"], state="normal", text=str(idx + 1), fill=hexc(scol))
                self.put(r["num"], px + 24 + 17, cyr)
        for k in range(used, len(self.rows)):
            self.hide(*self.rows[k].values())
        if not hov_done:
            self.hide(self.d_hl_hover)
        if not cur_done:
            self.hide(self.d_hl_cur)
        if not eq_done:
            self.hide(*self.d_eq)

    # ------------------------------------------------------------ buttons
    def _update_buttons(self, dt):
        self.hot = self._hot_btn() if self.mx >= 0 else None
        for b in self.btns:
            b.hov.t = 1.0 if (self.hot is b and (self.down in (None, b))) else 0.0
            b.prs.t = 1.0 if self.down is b and self.hot is b else 0.0
            b.hov.step(dt)
            b.prs.step(dt)
            self._render_btn(b)

    def _render_btn(self, b):
        if not b.on or b.alpha < 0.03:
            self.hide(b.img)
            if b.txt:
                self.hide(b.txt)
            return
        sc = 1 + 0.12 * b.hov.x - 0.15 * b.prs.x
        if b.kind == "icon":
            px = max(8, int(round(b.size * sc / 2)) * 2)
            circ = None
            if b.circle:
                circ = (255, 255, 255, 34 + int(round(b.hov.x * 5)) * 8)
            img = get_icon(b.icon, px, tuple(b.color), int(round(b.alpha * 8)), circ)
        elif b.kind == "pill":
            lvl = 26 + int(round(b.hov.x * 6)) * 7
            img = get_round(b.w * sc, b.h * sc, b.h * sc / 2, (255, 255, 255, lvl))
        else:
            lvl = (46 if self.search_mode else 24) + int(round(b.hov.x * 4)) * 5
            img = get_round(b.w, b.h, b.h / 2, (255, 255, 255, lvl))
        self.cfg(b.img, image=img, state="normal")
        self.put(b.img, b.cx, b.cy)
        if b.kind == "pill":
            self.cfg(b.txt, state="normal", fill="#ffffff")
            self.put(b.txt, b.cx, b.cy)

    def _update_cursor(self, hidden):
        over_bar = (self.ui_a > 0.5 and self.mx >= 0 and
                    (self.bar_h.t > 0.5 or self.vol_h.t > 0.5))
        row = self.hover_row >= 0 and self.dp.x > 0.5
        if hidden and self.ui_a < 0.15:
            c = "none"
        elif self.hot or over_bar or row:
            c = "hand2"
        else:
            c = ""
        self.cfg_cursor(c)

    def cfg_cursor(self, c):
        if getattr(self, "_cursor", None) != c:
            self._cursor = c
            self.cv.configure(cursor=c)


if __name__ == "__main__":
    App().mainloop()
