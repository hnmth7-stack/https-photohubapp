
"""PhotoHub - web đặt lịch chụp ảnh (Flask + SQLite).
Chạy:  pip install flask  ->  python app.py  ->  mở http://localhost:5000
  /         Khách hàng: xem photographer, lọc, xem ảnh, liên hệ & đặt lịch
  /studio   Photographer: đăng nhập bằng khóa riêng, quản lý ảnh (link bất kỳ) & đơn
  /admin    Admin: đăng nhập bằng ADMIN_KEY, xem lượt truy cập / đơn đặt / số ảnh
"""
import os, sqlite3, hashlib, hmac, secrets, uuid
from datetime import date, datetime, timedelta
from functools import wraps
from flask import (Flask, g, request, session, redirect, url_for,
                   render_template, flash, abort)
from jinja2 import DictLoader

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "photohub.db")
ADMIN_KEY = os.environ.get("ADMIN_KEY", "photohub-admin-2026")  # ĐỔI khi triển khai thật
SLOTS = ["08:00", "09:00", "10:00", "11:00", "13:00", "14:00", "15:00", "16:00", "17:00"]
ST = {"pending": "Chờ xác nhận", "confirmed": "Đã nhận", "rejected": "Từ chối", "done": "Hoàn thành"}

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "doi-khoa-bi-mat-nay")
app.jinja_env.globals.update(ST=ST, SLOTS=SLOTS)
app.jinja_env.filters["vnd"] = lambda v: f"{int(v):,}".replace(",", ".") + "đ"


def h(s): return hashlib.sha256(s.encode()).hexdigest()


# ---------------------------------------------------------------- DATABASE
SCHEMA = """
CREATE TABLE IF NOT EXISTS pg(id INTEGER PRIMARY KEY, name TEXT, kind TEXT, style TEXT, loc TEXT,
  price INT, bio TEXT, key_hash TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS img(id INTEGER PRIMARY KEY, pid INT, url TEXT, cap TEXT);
CREATE TABLE IF NOT EXISTS bk(id INTEGER PRIMARY KEY, pid INT, name TEXT, phone TEXT, email TEXT,
  day TEXT, slot TEXT, concept TEXT, msg TEXT, status TEXT DEFAULT 'pending', created TEXT);
CREATE TABLE IF NOT EXISTS visit(id INTEGER PRIMARY KEY, vid TEXT, path TEXT, day TEXT);
"""
U = lambda i: f"https://images.unsplash.com/photo-{i}?w=800&q=80"
SEED = [
    ("Minh An Portfolio", "Photographer", "Nàng thơ", "Sài Gòn", 1500000,
     "Chân dung nàng thơ ngoài trời, ánh sáng hoàng hôn dịu nhẹ.", "minhan-2026",
     ["1517841905240-472988babdf9", "1524504388940-b1c1722653e1", "1494790108377-be9c29b29330"]),
    ("Linh Chi Studio", "Studio", "Hàn Quốc", "Sài Gòn", 1800000,
     "Studio concept Hàn Quốc, couple & chân dung, có sẵn phông và đạo cụ.", "linhchi-2026",
     ["1534528741775-53994a69daeb", "1519741497674-611481863552", "1516035069371-29a1b244cc32"]),
    ("Hoàng Long Art", "Photographer", "Vintage", "Hà Nội", 1200000,
     "Phong cách vintage/film, phố cổ Hà Nội.", "hoanglong-2026",
     ["1507003211169-0a1dd7228f2d", "1519741497674-611481863552"]),
    ("MUA Ngọc Trâm", "Makeup Artist", "Nàng thơ", "Sài Gòn", 500000,
     "Makeup & làm tóc cho buổi chụp, tông trong trẻo tự nhiên.", "ngoctram-2026",
     ["1494790108377-be9c29b29330", "1517841905240-472988babdf9"]),
]


def init_db():
    c = sqlite3.connect(DB)
    c.executescript(SCHEMA)
    if not c.execute("SELECT 1 FROM pg").fetchone():
        print("Khóa đăng nhập photographer demo (/studio):")
        for n, k, s, l, pr, bio, key, imgs in SEED:
            cur = c.execute("INSERT INTO pg(name,kind,style,loc,price,bio,key_hash) VALUES(?,?,?,?,?,?,?)",
                            (n, k, s, l, pr, bio, h(key)))
            for i in imgs:
                c.execute("INSERT INTO img(pid,url,cap) VALUES(?,?,?)", (cur.lastrowid, U(i), s))
            print(f"  {n}: {key}")
    c.commit(); c.close()


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB); g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_):
    d = g.pop("db", None)
    if d: d.close()


def q(sql, a=(), one=False):
    r = db().execute(sql, a).fetchall()
    return (r[0] if r else None) if one else r


def ex(sql, a=()):
    cur = db().execute(sql, a); db().commit(); return cur.lastrowid


def sc(sql, a=()): return q(sql, a, one=True)[0]


# ---------------------------------------------------------------- TRACKING + AUTH
@app.before_request
def track():
    p = request.path
    g.new_vid = None
    if request.method == "GET" and request.endpoint not in (None, "static") \
            and not p.startswith(("/admin", "/studio")):
        vid = request.cookies.get("vid")
        if not vid:
            vid = g.new_vid = uuid.uuid4().hex
        ex("INSERT INTO visit(vid,path,day) VALUES(?,?,?)", (vid, p, date.today().isoformat()))


@app.after_request
def set_vid(r):
    if g.get("new_vid"):
        r.set_cookie("vid", g.new_vid, max_age=31536000, httponly=True, samesite="Lax")
    return r


def need(role):
    def deco(f):
        @wraps(f)
        def w(*a, **k):
            if not session.get(role):
                return redirect(url_for("admin_login" if role == "admin" else "studio_login"))
            return f(*a, **k)
        return w
    return deco


def ok_url(u): return u.startswith(("http://", "https://")) and len(u) < 600


# ---------------------------------------------------------------- KHÁCH HÀNG
@app.route("/")
def index():
    style, loc, mx = request.args.get("style", ""), request.args.get("loc", ""), request.args.get("max", type=int)
    sql = ("SELECT p.*, (SELECT url FROM img WHERE pid=p.id ORDER BY id LIMIT 1) cover,"
           " (SELECT COUNT(*) FROM img WHERE pid=p.id) n_img FROM pg p WHERE 1=1")
    a = []
    if style: sql += " AND style=?"; a.append(style)
    if loc: sql += " AND loc=?"; a.append(loc)
    if mx: sql += " AND price<=?"; a.append(mx)
    return render_template("index.html", items=q(sql, a),
                           styles=[r[0] for r in q("SELECT DISTINCT style FROM pg")],
                           locs=[r[0] for r in q("SELECT DISTINCT loc FROM pg")],
                           f=dict(style=style, loc=loc, max=mx or ""))


@app.route("/p/<int:pid>")
def profile(pid):
    p = q("SELECT * FROM pg WHERE id=?", (pid,), one=True) or abort(404)
    return render_template("profile.html", p=p, imgs=q("SELECT * FROM img WHERE pid=? ORDER BY id DESC", (pid,)),
                           today=date.today().isoformat())


@app.route("/p/<int:pid>/book", methods=["POST"])
def book(pid):
    q("SELECT 1 FROM pg WHERE id=?", (pid,), one=True) or abort(404)
    f = {k: request.form.get(k, "").strip() for k in ("name", "phone", "email", "day", "slot", "concept", "msg")}
    try:
        okday = datetime.strptime(f["day"], "%Y-%m-%d").date() >= date.today()
    except ValueError:
        okday = False
    if not (f["name"] and len(f["phone"]) >= 8 and okday and f["slot"] in SLOTS):
        flash("Vui lòng nhập đủ họ tên, số điện thoại hợp lệ, ngày (từ hôm nay) và giờ chụp.", "err")
    elif q("SELECT 1 FROM bk WHERE pid=? AND day=? AND slot=? AND status IN ('pending','confirmed')",
           (pid, f["day"], f["slot"]), one=True):
        flash("Khung giờ này đã có người đặt, vui lòng chọn giờ khác.", "err")
    else:
        ex("INSERT INTO bk(pid,name,phone,email,day,slot,concept,msg,created) VALUES(?,?,?,?,?,?,?,?,?)",
           (pid, f["name"][:80], f["phone"][:20], f["email"][:120], f["day"], f["slot"],
            f["concept"][:120], f["msg"][:1000], datetime.now().strftime("%Y-%m-%d %H:%M")))
        flash("🎉 Đã gửi yêu cầu! Photographer sẽ liên hệ với bạn qua số điện thoại/email sớm nhất.", "ok")
    return redirect(url_for("profile", pid=pid) + "#dat-lich")


# ---------------------------------------------------------------- PHOTOGRAPHER
@app.route("/studio/login", methods=["GET", "POST"])
def studio_login():
    if request.method == "POST":
        p = q("SELECT id FROM pg WHERE key_hash=?", (h(request.form.get("key", "").strip()),), one=True)
        if p:
            session.clear(); session["pg"] = p["id"]
            return redirect(url_for("studio"))
        flash("Khóa không đúng.", "err")
    return render_template("studio_login.html")


@app.route("/studio/register", methods=["POST"])
def studio_register():
    f = request.form
    name = f.get("name", "").strip()
    if not name:
        flash("Vui lòng nhập tên.", "err"); return redirect(url_for("studio_login"))
    key = "pg-" + secrets.token_urlsafe(9)
    pid = ex("INSERT INTO pg(name,kind,style,loc,price,bio,key_hash) VALUES(?,?,?,?,?,?,?)",
             (name[:80], f.get("kind", "Photographer"), f.get("style", "").strip()[:40] or "Tự do",
              f.get("loc", "").strip()[:40] or "Sài Gòn", max(0, f.get("price", type=int) or 0), "", h(key)))
    session.clear(); session["pg"] = pid
    flash(f"Tạo hồ sơ thành công! KHÓA ĐĂNG NHẬP CỦA BẠN: {key} — hãy lưu lại, hệ thống chỉ hiển thị một lần.", "key")
    return redirect(url_for("studio"))


@app.route("/studio")
@need("pg")
def studio():
    pid = session["pg"]
    return render_template("studio.html", p=q("SELECT * FROM pg WHERE id=?", (pid,), one=True),
                           imgs=q("SELECT * FROM img WHERE pid=? ORDER BY id DESC", (pid,)),
                           bks=q("SELECT * FROM bk WHERE pid=? ORDER BY day DESC, slot", (pid,)))


@app.route("/studio/profile", methods=["POST"])
@need("pg")
def studio_profile():
    f = request.form
    ex("UPDATE pg SET name=?,kind=?,style=?,loc=?,price=?,bio=? WHERE id=?",
       (f.get("name", "")[:80], f.get("kind", "Photographer"), f.get("style", "")[:40], f.get("loc", "")[:40],
        max(0, f.get("price", type=int) or 0), f.get("bio", "")[:800], session["pg"]))
    flash("Đã cập nhật hồ sơ.", "ok")
    return redirect(url_for("studio"))


@app.route("/studio/image", methods=["POST"])
@need("pg")
def studio_img_add():
    urls = [u.strip() for u in request.form.get("urls", "").splitlines() if u.strip()]
    good = [u for u in urls if ok_url(u)]
    for u in good:
        ex("INSERT INTO img(pid,url,cap) VALUES(?,?,?)", (session["pg"], u, request.form.get("cap", "")[:100]))
    flash(f"Đã thêm {len(good)} ảnh." + (f" Bỏ qua {len(urls)-len(good)} link không hợp lệ." if len(good) < len(urls) else ""),
          "ok" if good else "err")
    return redirect(url_for("studio"))


@app.route("/studio/image/<int:iid>/del", methods=["POST"])
@need("pg")
def studio_img_del(iid):
    ex("DELETE FROM img WHERE id=? AND pid=?", (iid, session["pg"]))
    return redirect(url_for("studio"))


@app.route("/studio/booking/<int:bid>/<act>", methods=["POST"])
@need("pg")
def studio_booking(bid, act):
    new = {"confirm": "confirmed", "reject": "rejected", "done": "done"}.get(act) or abort(400)
    ex("UPDATE bk SET status=? WHERE id=? AND pid=?", (new, bid, session["pg"]))
    return redirect(url_for("studio") + "#don")


# ---------------------------------------------------------------- ADMIN
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        if hmac.compare_digest(request.form.get("key", ""), ADMIN_KEY):
            session.clear(); session["admin"] = True
            return redirect(url_for("admin"))
        flash("Sai mã admin.", "err")
    return render_template("admin_login.html")


@app.route("/admin")
@need("admin")
def admin():
    t = date.today().isoformat()
    stats = dict(views=sc("SELECT COUNT(*) FROM visit"), uniq=sc("SELECT COUNT(DISTINCT vid) FROM visit"),
                 tv=sc("SELECT COUNT(*) FROM visit WHERE day=?", (t,)),
                 tu=sc("SELECT COUNT(DISTINCT vid) FROM visit WHERE day=?", (t,)),
                 bk=sc("SELECT COUNT(*) FROM bk"), img=sc("SELECT COUNT(*) FROM img"), pg=sc("SELECT COUNT(*) FROM pg"))
    rows = {r["day"]: r for r in q("SELECT day, COUNT(*) v, COUNT(DISTINCT vid) u FROM visit GROUP BY day")}
    days = [(date.today() - timedelta(i)).isoformat() for i in range(6, -1, -1)]
    chart = [(d, rows[d]["v"] if d in rows else 0, rows[d]["u"] if d in rows else 0) for d in days]
    return render_template(
        "admin.html", s=stats, chart=chart, mx=max([c[1] for c in chart] + [1]),
        by=q("SELECT status, COUNT(*) n FROM bk GROUP BY status"),
        pgs=q("SELECT p.*, (SELECT COUNT(*) FROM img WHERE pid=p.id) n_img,"
              " (SELECT COUNT(*) FROM bk WHERE pid=p.id) n_bk,"
              " (SELECT COUNT(*) FROM visit WHERE path='/p/'||p.id) n_view FROM pg p"),
        bks=q("SELECT b.*, p.name pname FROM bk b JOIN pg p ON p.id=b.pid ORDER BY b.id DESC LIMIT 100"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")


@app.errorhandler(404)
def nf(_):
    return render_template("base.html"), 404


# ---------------------------------------------------------------- GIAO DIỆN (templates)
CSS = """
:root{--b:#3E2723;--p:#F4C9D6;--a:#8D6E63;--bg:#FAF6F0}*{box-sizing:border-box}
body{margin:0;font-family:system-ui,Arial,sans-serif;background:var(--bg);color:var(--b)}
.nav{display:flex;justify-content:space-between;align-items:center;padding:14px 5vw;background:#fff;position:sticky;top:0;z-index:5;box-shadow:0 2px 8px #0001}
.logo{font-weight:800;font-size:22px;color:var(--b);text-decoration:none}nav a{margin-left:18px;color:var(--a);text-decoration:none;font-weight:600}
main{max-width:1150px;margin:auto;padding:24px 5vw}.hero{text-align:center;padding:40px 0 10px}.hero h1{font-size:clamp(28px,5vw,44px);margin:0 0 8px}
.filters{display:flex;gap:10px;flex-wrap:wrap;justify-content:center;margin:20px 0}
input,select,textarea{padding:10px 12px;border:1px solid #d9c9c0;border-radius:8px;font:inherit;background:#fff;width:100%}
.filters input,.filters select{width:auto}label{font-weight:600;font-size:14px;display:block;margin:10px 0 4px}
.btn{background:var(--b);color:#fff;border:0;border-radius:8px;padding:10px 18px;font-weight:700;cursor:pointer;text-decoration:none;display:inline-block}
.btn:hover{background:var(--p);color:var(--b)}.btn.sm{padding:6px 12px;font-size:13px}.btn.red{background:#b3261e}.btn.red:hover{background:#f3b5b1}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:20px}
.card{background:#fff;border-radius:14px;overflow:hidden;box-shadow:0 4px 10px #0001;transition:.2s;text-decoration:none;color:inherit;display:block}
.card:hover{transform:translateY(-4px)}.card img{width:100%;height:210px;object-fit:cover;display:block;background:var(--p)}.card .in{padding:14px}
.badge{background:var(--p);padding:3px 10px;border-radius:12px;font-size:12px;font-weight:700;display:inline-block}
.box{background:#fff;border-radius:14px;padding:20px;box-shadow:0 4px 10px #0001;margin-bottom:18px}
.two{display:grid;grid-template-columns:2fr 1fr;gap:24px;align-items:start}@media(max-width:800px){.two{grid-template-columns:1fr}}
.gal{columns:3 200px;gap:10px}.gal img{width:100%;margin-bottom:10px;border-radius:10px;cursor:zoom-in;display:block}
.flash{max-width:1100px;margin:12px auto;padding:12px 16px;border-radius:8px;background:#e8f5e9}.flash.err{background:#fdecea}.flash.key{background:#fff3cd;word-break:break-all}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:14px;margin-bottom:18px}
.stat{background:#fff;border-left:5px solid var(--p);border-radius:10px;padding:16px;box-shadow:0 2px 6px #0001}.stat b{font-size:28px;display:block}
.bars{display:flex;align-items:flex-end;gap:10px;height:150px}.bars div{flex:1;text-align:center;font-size:11px}.bars i{display:block;background:var(--a);border-radius:4px 4px 0 0}
.tw{overflow-x:auto}table{width:100%;border-collapse:collapse;font-size:14px}th,td{padding:8px;border-bottom:1px solid #eee;text-align:left;vertical-align:top}
.mini{display:flex;flex-wrap:wrap;gap:10px}.mini div{position:relative}.mini img{width:110px;height:110px;object-fit:cover;border-radius:8px;display:block}
.lb{position:fixed;inset:0;background:#000d;display:none;align-items:center;justify-content:center;z-index:9}.lb img{max-width:92vw;max-height:92vh;border-radius:8px}
footer{text-align:center;padding:30px;color:var(--a)}.narrow{max-width:420px;margin:30px auto}
"""
JS = """
const FB='data:image/svg+xml,'+encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="400" height="300"><rect width="400" height="300" fill="#F4C9D6"/><text x="200" y="165" text-anchor="middle" font-size="48">📷</text></svg>');
document.querySelectorAll('img').forEach(i=>{i.onerror=()=>{i.onerror=null;i.src=FB}});
const lb=document.getElementById('lb');
document.querySelectorAll('.gal img').forEach(i=>i.onclick=()=>{lb.querySelector('img').src=i.src;lb.style.display='flex'});
lb.onclick=()=>lb.style.display='none';
"""
BASE_T = """<!doctype html><html lang="vi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{% block title %}PhotoHub - Đặt lịch chụp ảnh{% endblock %}</title>
<style>""" + CSS + """</style></head><body>
<header class="nav"><a class="logo" href="/">📸 PhotoHub</a><nav><a href="/">Khám phá</a><a href="/studio">Dành cho Photographer</a>
{% if session.admin %}<a href="/admin">Admin</a>{% endif %}{% if session.pg or session.admin %}<a href="/logout">Đăng xuất</a>{% endif %}</nav></header>
{% for c,m in get_flashed_messages(with_categories=true) %}<div class="flash {{c}}">{{m}}</div>{% endfor %}
<main>{% block body %}<div class="box"><h2>Không tìm thấy trang</h2><a class="btn" href="/">Về trang chủ</a></div>{% endblock %}</main>
<footer>© PhotoHub · Kết nối khách hàng & photographer</footer>
<div class="lb" id="lb"><img alt=""></div><script>""" + JS + """</script></body></html>"""

INDEX_T = """{% extends "base.html" %}{% block body %}
<section class="hero"><h1>Tìm photographer cho khoảnh khắc của bạn ✨</h1><p>Xem portfolio thật, chọn phong cách, đặt lịch chỉ trong vài phút.</p></section>
<form class="filters" method="get">
<select name="style"><option value="">Mọi phong cách</option>{% for s in styles %}<option {{'selected' if s==f.style}}>{{s}}</option>{% endfor %}</select>
<select name="loc"><option value="">Mọi địa điểm</option>{% for s in locs %}<option {{'selected' if s==f.loc}}>{{s}}</option>{% endfor %}</select>
<input type="number" name="max" value="{{f.max}}" placeholder="Ngân sách tối đa (đ)" step="100000" min="0">
<button class="btn">Tìm kiếm</button></form>
{% if not items %}<div class="box">Chưa có photographer phù hợp. Thử đổi bộ lọc nhé.</div>{% endif %}
<div class="grid">{% for p in items %}<a class="card" href="/p/{{p.id}}"><img src="{{p.cover or ''}}" alt="{{p.name}}">
<div class="in"><span class="badge">{{p.kind}}</span> <span class="badge">{{p.style}}</span><h3 style="margin:8px 0 4px">{{p.name}}</h3>
<div>📍 {{p.loc}} · 🖼️ {{p.n_img}} ảnh</div><b>Từ {{p.price|vnd}}</b></div></a>{% endfor %}</div>{% endblock %}"""

PROFILE_T = """{% extends "base.html" %}{% block title %}{{p.name}} - PhotoHub{% endblock %}{% block body %}
<div class="box"><span class="badge">{{p.kind}}</span> <span class="badge">{{p.style}}</span>
<h1 style="margin:8px 0">{{p.name}}</h1><div>📍 {{p.loc}} · Từ <b>{{p.price|vnd}}</b>/buổi</div><p>{{p.bio}}</p></div>
<div class="two"><div><h2>Portfolio ({{imgs|length}} ảnh)</h2>
{% if imgs %}<div class="gal">{% for i in imgs %}<img src="{{i.url}}" alt="{{i.cap}}" loading="lazy">{% endfor %}</div>{% else %}<div class="box">Chưa có ảnh.</div>{% endif %}</div>
<form class="box" id="dat-lich" method="post" action="/p/{{p.id}}/book" style="position:sticky;top:80px">
<h3 style="margin-top:0">📅 Liên hệ & đặt lịch</h3>
<label>Họ tên *</label><input name="name" required><label>Số điện thoại *</label><input name="phone" required inputmode="tel">
<label>Email</label><input name="email" type="email"><label>Ngày chụp *</label><input name="day" type="date" min="{{today}}" required>
<label>Giờ chụp *</label><select name="slot">{% for s in SLOTS %}<option>{{s}}</option>{% endfor %}</select>
<label>Concept mong muốn</label><input name="concept" placeholder="VD: nàng thơ hoàng hôn">
<label>Lời nhắn</label><textarea name="msg" rows="3"></textarea><br><button class="btn" style="width:100%">Gửi yêu cầu</button></form></div>{% endblock %}"""

STUDIO_LOGIN_T = """{% extends "base.html" %}{% block body %}<div class="narrow">
<form class="box" method="post"><h2 style="margin-top:0">📸 Đăng nhập Photographer</h2><label>Khóa đăng nhập</label>
<input name="key" type="password" required placeholder="Khóa riêng của bạn"><br><br><button class="btn">Đăng nhập</button></form>
<form class="box" method="post" action="/studio/register"><h3 style="margin-top:0">Chưa có hồ sơ? Đăng ký</h3>
<label>Tên / Studio *</label><input name="name" required><label>Loại</label><select name="kind"><option>Photographer</option><option>Studio</option><option>Makeup Artist</option></select>
<label>Phong cách</label><input name="style" placeholder="Vintage, Hàn Quốc..."><label>Địa điểm</label><input name="loc" placeholder="Sài Gòn">
<label>Giá từ (đ)</label><input name="price" type="number" min="0" step="50000"><br><br><button class="btn">Tạo hồ sơ & nhận khóa</button></form></div>{% endblock %}"""

STUDIO_T = """{% extends "base.html" %}{% block body %}
<h1>Xin chào, {{p.name}}</h1><div class="two"><div>
<div class="box" id="don"><h2 style="margin-top:0">🔔 Đơn đặt lịch ({{bks|length}})</h2>{% if not bks %}<p>Chưa có đơn nào.</p>{% endif %}
<div class="tw"><table>{% for b in bks %}<tr><td><b>{{b.name}}</b><br>📞 {{b.phone}}<br>{{b.email}}</td>
<td>{{b.day}} · {{b.slot}}<br><i>{{b.concept}}</i><br>{{b.msg}}</td><td><span class="badge">{{ST[b.status]}}</span></td>
<td>{% if b.status=='pending' %}<form method="post" action="/studio/booking/{{b.id}}/confirm" style="display:inline"><button class="btn sm">Nhận</button></form>
<form method="post" action="/studio/booking/{{b.id}}/reject" style="display:inline"><button class="btn sm red">Từ chối</button></form>
{% elif b.status=='confirmed' %}<form method="post" action="/studio/booking/{{b.id}}/done"><button class="btn sm">Hoàn thành</button></form>{% endif %}</td></tr>{% endfor %}</table></div></div>
<div class="box"><h2 style="margin-top:0">🖼️ Ảnh portfolio ({{imgs|length}})</h2><div class="mini">{% for i in imgs %}<div><img src="{{i.url}}" alt="">
<form method="post" action="/studio/image/{{i.id}}/del"><button class="btn sm red" style="width:100%">Xóa</button></form></div>{% endfor %}</div></div></div>
<div><form class="box" method="post" action="/studio/image"><h3 style="margin-top:0">➕ Thêm ảnh từ link</h3>
<label>Mỗi dòng một link ảnh (https://...)</label><textarea name="urls" rows="4" required></textarea><label>Chú thích</label><input name="cap"><br><br><button class="btn">Thêm ảnh</button></form>
<form class="box" method="post" action="/studio/profile"><h3 style="margin-top:0">✏️ Hồ sơ</h3>
<label>Tên</label><input name="name" value="{{p.name}}" required><label>Loại</label><select name="kind">{% for k in ['Photographer','Studio','Makeup Artist'] %}<option {{'selected' if k==p.kind}}>{{k}}</option>{% endfor %}</select>
<label>Phong cách</label><input name="style" value="{{p.style}}"><label>Địa điểm</label><input name="loc" value="{{p.loc}}"><label>Giá từ (đ)</label><input name="price" type="number" value="{{p.price}}">
<label>Giới thiệu</label><textarea name="bio" rows="4">{{p.bio}}</textarea><br><button class="btn">Lưu</button> <a href="/p/{{p.id}}">Xem trang công khai</a></form></div></div>{% endblock %}"""

ADMIN_LOGIN_T = """{% extends "base.html" %}{% block body %}<form class="box narrow" method="post"><h2 style="margin-top:0">🔑 Admin</h2>
<label>Mã admin</label><input name="key" type="password" required autofocus><br><br><button class="btn">Đăng nhập</button></form>{% endblock %}"""

ADMIN_T = """{% extends "base.html" %}{% block body %}<h1>📊 Bảng điều khiển Admin</h1>
<div class="stats"><div class="stat">Tổng lượt truy cập<b>{{s.views}}</b></div><div class="stat">Người truy cập (duy nhất)<b>{{s.uniq}}</b></div>
<div class="stat">Hôm nay: lượt / người<b>{{s.tv}} / {{s.tu}}</b></div><div class="stat">Tổng đơn đã đặt<b>{{s.bk}}</b></div>
<div class="stat">Tổng số hình ảnh<b>{{s.img}}</b></div><div class="stat">Photographer<b>{{s.pg}}</b></div></div>
<div class="two"><div class="box"><h3 style="margin-top:0">Lượt truy cập 7 ngày qua</h3><div class="bars">{% for c in chart %}
<div><i style="height:{{c[1]*100//mx}}px"></i>{{c[1]}}<br>{{c[0][5:]}}</div>{% endfor %}</div></div>
<div class="box"><h3 style="margin-top:0">Đơn theo trạng thái</h3>{% for r in by %}<p><span class="badge">{{ST[r.status]}}</span> {{r.n}}</p>{% else %}<p>Chưa có đơn.</p>{% endfor %}</div></div>
<div class="box tw"><h3 style="margin-top:0">Photographer</h3><table><tr><th>Tên</th><th>Loại</th><th>Ảnh</th><th>Đơn</th><th>Lượt xem trang</th></tr>
{% for p in pgs %}<tr><td><a href="/p/{{p.id}}">{{p.name}}</a></td><td>{{p.kind}}</td><td>{{p.n_img}}</td><td>{{p.n_bk}}</td><td>{{p.n_view}}</td></tr>{% endfor %}</table></div>
<div class="box tw"><h3 style="margin-top:0">Đơn đặt gần đây</h3><table><tr><th>#</th><th>Khách</th><th>Photographer</th><th>Lịch chụp</th><th>Trạng thái</th><th>Tạo lúc</th></tr>
{% for b in bks %}<tr><td>{{b.id}}</td><td>{{b.name}}<br>{{b.phone}}</td><td>{{b.pname}}</td><td>{{b.day}} {{b.slot}}</td><td>{{ST[b.status]}}</td><td>{{b.created}}</td></tr>{% endfor %}</table></div>{% endblock %}"""

app.jinja_loader = DictLoader({
    "base.html": BASE_T, "index.html": INDEX_T, "profile.html": PROFILE_T, "studio_login.html": STUDIO_LOGIN_T,
    "studio.html": STUDIO_T, "admin_login.html": ADMIN_LOGIN_T, "admin.html": ADMIN_T})

init_db()

if __name__ == "__main__":
    print(f"\nAdmin: http://localhost:5000/admin  (mã: {ADMIN_KEY})")
    app.run(host="0.0.0.0", port=5000, debug=False)
