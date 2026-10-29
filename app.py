"""Найди команду: сервер, правила и данные. Только стандартная библиотека Python 3.9+."""
import contextlib, hashlib, json, os, re, secrets, sqlite3, sys
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("DB_PATH", os.path.join(HERE, "data.db"))
CATEGORIES = ["Технологии", "Образование", "Социальные проекты", "Творчество", "Бизнес", "Другое"]
SCHEMA = """
create table users(id integer primary key, email text unique not null, pw text not null, name text not null,
  bio text not null default '', skills text not null default '', interests text not null default '');
create table sessions(token text primary key, user_id integer not null references users(id));
create table projects(id integer primary key, author_id integer not null references users(id), title text not null,
  description text not null, goal text not null, category text not null, published integer not null default 0,
  open integer not null default 1, created_at text not null);
create table vacancies(id integer primary key, project_id integer not null references projects(id), role text not null,
  skills text not null default '', description text not null default '', seats integer not null check(seats>=1));
create table applications(id integer primary key, user_id integer not null references users(id),
  project_id integer not null references projects(id), vacancy_id integer not null references vacancies(id),
  message text not null, created_at text not null,
  status text not null default 'pending' check(status in ('pending','accepted','rejected','withdrawn')));
create unique index one_pending on applications(user_id, project_id) where status='pending';
create table members(id integer primary key, project_id integer not null references projects(id),
  vacancy_id integer not null references vacancies(id), user_id integer not null references users(id),
  application_id integer not null unique references applications(id), unique(project_id, user_id));
"""


class ApiError(Exception):
    def __init__(self, code, msg):
        self.code, self.msg = code, msg


def bad(msg, code=400):
    raise ApiError(code, msg)


def now(): return datetime.now().isoformat(timespec="seconds")


def connect():
    c = sqlite3.connect(DB, timeout=30, isolation_level=None)  # autocommit, транзакции явные
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c


@contextlib.contextmanager
def tx(c):
    """BEGIN IMMEDIATE: запись сериализуется, проверка мест и вставка атомарны."""
    c.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        c.execute("ROLLBACK"); raise
    c.execute("COMMIT")


# ---------- валидация ----------
def text(v, name, req=True, n=2000):
    if v is None: v = ""
    if not isinstance(v, str): bad(f"{name}: неверный формат")
    v = v.strip()
    if req and not v: bad(f"{name}: не может быть пустым")
    if len(v) > n: bad(f"{name}: слишком длинный текст")
    return v


def skills(v):
    return ", ".join(s.strip() for s in text(v, "Навыки", False, 300).split(",") if s.strip())


def seats(v):
    if isinstance(v, bool) or not isinstance(v, int) or v < 1: bad("Число мест должно быть целым числом не меньше 1")
    return v


# ---------- пользователи ----------
def hash_pw(p, salt=None):
    salt = salt or secrets.token_hex(8)
    return salt + "$" + hashlib.pbkdf2_hmac("sha256", p.encode(), salt.encode(), 60000).hex()


def register(c, d):
    email, name, pw = text(d.get("email"), "Email", n=120).lower(), text(d.get("name"), "Имя", n=60), d.get("password")
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email): bad("Некорректный email")
    if not isinstance(pw, str) or len(pw) < 6: bad("Пароль: минимум 6 символов")
    with tx(c):
        if c.execute("select 1 from users where email=?", (email,)).fetchone(): bad("Email уже занят", 409)
        return c.execute("insert into users(email,pw,name) values(?,?,?)", (email, hash_pw(pw), name)).lastrowid


def login(c, d):
    u = c.execute("select * from users where email=?", (text(d.get("email"), "Email").lower(),)).fetchone()
    pw = d.get("password")
    if not u or not isinstance(pw, str) or not secrets.compare_digest(hash_pw(pw, u["pw"].split("$")[0]), u["pw"]):
        bad("Неверный email или пароль", 401)
    t = secrets.token_hex(24)
    c.execute("insert into sessions values(?,?)", (t, u["id"]))
    return t


def user_of(c, token):
    r = c.execute("select user_id from sessions where token=?", (token or "",)).fetchone()
    return r["user_id"] if r else None


def profile(c, uid, public=False):
    u = c.execute("select id,name,bio,skills,interests,email from users where id=?", (uid,)).fetchone()
    if not u: bad("Пользователь не найден", 404)
    u = dict(u)
    if public: del u["email"]
    return u


def update_me(c, uid, d):
    with tx(c):
        c.execute("update users set name=?,bio=?,skills=?,interests=? where id=?",
                  (text(d.get("name"), "Имя", n=60), text(d.get("bio"), "Описание", False, 600),
                   skills(d.get("skills")), skills(d.get("interests")), uid))


# ---------- проекты и вакансии ----------
def vacs(c, pid):
    return [dict(r) for r in c.execute(
        "select v.*, v.seats-(select count(*) from members m where m.vacancy_id=v.id) free "
        "from vacancies v where project_id=? order by id", (pid,))]


def rstatus(p, vs):
    return "closed" if not p["open"] else ("open" if any(v["free"] > 0 for v in vs) else "full")


def get_project(c, pid, uid=None, must_own=False):
    p = c.execute("select p.*, u.name author_name from projects p join users u on u.id=p.author_id where p.id=?", (pid,)).fetchone()
    if not p or (not p["published"] and p["author_id"] != uid): bad("Проект не найден", 404)
    if must_own and p["author_id"] != uid: bad("Нет прав на этот проект", 403)
    return p


def pfields(d):
    cat = text(d.get("category"), "Категория", n=60)
    if cat not in CATEGORIES: bad("Неизвестная категория")
    return (text(d.get("title"), "Название", n=120), text(d.get("description"), "Описание"),
            text(d.get("goal"), "Цель", n=600), cat)


def vfields(d):
    return text(d.get("role"), "Роль", n=80), skills(d.get("skills")), text(d.get("description"), "Задачи", False), seats(d.get("seats"))


def create_project(c, uid, d):
    f, vl = pfields(d), d.get("vacancies") or []
    if not isinstance(vl, list): bad("Вакансии: неверный формат")
    vl = [vfields(v) for v in vl]
    pub = bool(d.get("publish"))
    if pub and not vl: bad("Для публикации нужна хотя бы одна вакансия")
    with tx(c):
        pid = c.execute("insert into projects(author_id,title,description,goal,category,published,created_at) values(?,?,?,?,?,?,?)",
                        (uid, *f, int(pub), now())).lastrowid
        for v in vl: c.execute("insert into vacancies(project_id,role,skills,description,seats) values(?,?,?,?,?)", (pid, *v))
    return pid


def update_project(c, uid, pid, d):
    f = pfields(d)
    with tx(c):
        get_project(c, pid, uid, True)
        c.execute("update projects set title=?,description=?,goal=?,category=? where id=?", (*f, pid))


def publish(c, uid, pid):
    with tx(c):
        get_project(c, pid, uid, True)
        if not c.execute("select 1 from vacancies where project_id=? and seats>0", (pid,)).fetchone():
            bad("Для публикации нужна хотя бы одна вакансия")
        c.execute("update projects set published=1 where id=?", (pid,))


def set_open(c, uid, pid, is_open):
    if not isinstance(is_open, bool): bad("Неверный формат")
    with tx(c):
        get_project(c, pid, uid, True)
        c.execute("update projects set open=? where id=?", (int(is_open), pid))


def add_vacancy(c, uid, pid, d):
    v = vfields(d)
    with tx(c):
        get_project(c, pid, uid, True)
        return c.execute("insert into vacancies(project_id,role,skills,description,seats) values(?,?,?,?,?)", (pid, *v)).lastrowid


def vacancy_of(c, uid, vid):
    v = c.execute("select * from vacancies where id=?", (vid,)).fetchone()
    if not v: bad("Вакансия не найдена", 404)
    get_project(c, v["project_id"], uid, True)
    return v


def update_vacancy(c, uid, vid, d):
    f = vfields(d)
    with tx(c):
        vacancy_of(c, uid, vid)
        taken = c.execute("select count(*) n from members where vacancy_id=?", (vid,)).fetchone()["n"]
        if f[3] < taken: bad(f"Нельзя уменьшить число мест ниже числа принятых участников ({taken})", 409)
        c.execute("update vacancies set role=?,skills=?,description=?,seats=? where id=?", (*f, vid))


def delete_vacancy(c, uid, vid):
    with tx(c):
        vacancy_of(c, uid, vid)
        if c.execute("select 1 from applications where vacancy_id=? union select 1 from members where vacancy_id=?", (vid, vid)).fetchone():
            bad("Нельзя удалить вакансию со связанными заявками или участниками", 409)
        c.execute("delete from vacancies where id=?", (vid,))


def project_view(c, uid, pid):
    p = get_project(c, pid, uid)
    vs, is_author = vacs(c, pid), uid == p["author_id"]
    members = [dict(r) for r in c.execute(
        "select u.id user_id,u.name,v.role from members m join users u on u.id=m.user_id "
        "join vacancies v on v.id=m.vacancy_id where m.project_id=? order by m.id", (pid,))]
    st, block = rstatus(p, vs), None
    if not uid: block = "login"
    elif is_author: block = "author"
    elif any(m["user_id"] == uid for m in members): block = "member"
    elif c.execute("select 1 from applications where user_id=? and project_id=? and status='pending'", (uid, pid)).fetchone(): block = "pending"
    elif st != "open": block = st
    return {"id": pid, "title": p["title"], "description": p["description"], "goal": p["goal"], "category": p["category"],
            "published": bool(p["published"]), "open": bool(p["open"]), "status": st, "created_at": p["created_at"],
            "author": {"id": p["author_id"], "name": p["author_name"]}, "vacancies": vs, "members": members,
            "is_author": is_author, "apply_block": block}


def catalog(c, q):
    g = lambda k: (q.get(k) or [""])[0].strip().lower()
    text_q, cat, sk, st, sort = g("q"), g("category"), g("skill"), g("status"), g("sort") or "new"
    out = []
    for p in c.execute("select p.*, u.name author_name from projects p join users u on u.id=p.author_id where published=1"):
        vs = vacs(c, p["id"]); s = rstatus(p, vs)
        if text_q and text_q not in (p["title"] + " " + p["description"]).lower(): continue
        if cat and p["category"].lower() != cat: continue
        if sk and not any(sk in v["skills"].lower() for v in vs): continue
        if st and s != st: continue
        d = p["description"]
        out.append({"id": p["id"], "title": p["title"], "short": d if len(d) <= 160 else d[:157] + "…", "category": p["category"],
                    "author": p["author_name"], "status": s, "created_at": p["created_at"],
                    "vacancies": [{"role": v["role"], "free": v["free"]} for v in vs if v["free"] > 0] if p["open"] else []})
    out.sort(key=lambda x: (x["created_at"], x["id"]), reverse=(sort != "old"))
    return out


# ---------- заявки ----------
def apply(c, uid, pid, d):
    msg = text(d.get("message"), "Сообщение", n=1500)
    with tx(c):
        p = get_project(c, pid, uid)
        if not p["published"]: bad("Проект не найден", 404)
        if p["author_id"] == uid: bad("Нельзя подать заявку в собственный проект", 403)
        if not p["open"]: bad("Набор закрыт", 409)
        vid = d.get("vacancy_id")
        if isinstance(vid, bool) or not isinstance(vid, int): bad("Выберите вакансию")
        v = c.execute("select * from vacancies where id=? and project_id=?", (vid, pid)).fetchone()
        if not v: bad("Вакансия не найдена в этом проекте", 404)
        if vacs_free(c, v["id"]) <= 0: bad("В этой вакансии нет свободных мест", 409)
        if c.execute("select 1 from members where project_id=? and user_id=?", (pid, uid)).fetchone(): bad("Вы уже в составе проекта", 409)
        if c.execute("select 1 from applications where project_id=? and user_id=? and status='pending'", (pid, uid)).fetchone():
            bad("У вас уже есть заявка на рассмотрении в этом проекте", 409)
        return c.execute("insert into applications(user_id,project_id,vacancy_id,message,created_at) values(?,?,?,?,?)",
                         (uid, pid, v["id"], msg, now())).lastrowid


def vacs_free(c, vid):
    return c.execute("select v.seats-(select count(*) from members where vacancy_id=v.id) f from vacancies v where id=?", (vid,)).fetchone()["f"]


def decide(c, uid, aid, action):
    with tx(c):
        a = c.execute("select * from applications where id=?", (aid,)).fetchone()
        if not a: bad("Заявка не найдена", 404)
        p = c.execute("select * from projects where id=?", (a["project_id"],)).fetchone()
        if action == "withdraw":
            if a["user_id"] != uid: bad("Это не ваша заявка", 403)
        elif p["author_id"] != uid: bad("Только автор проекта обрабатывает заявки", 403)
        if a["status"] != "pending": bad("Заявка уже обработана", 409)
        if action == "accept":
            if not p["open"]: bad("Набор закрыт", 409)
            if vacs_free(c, a["vacancy_id"]) <= 0: bad("В вакансии нет свободных мест", 409)
            c.execute("insert into members(project_id,vacancy_id,user_id,application_id) values(?,?,?,?)",
                      (a["project_id"], a["vacancy_id"], a["user_id"], aid))
        c.execute("update applications set status=? where id=?", ({"accept": "accepted", "reject": "rejected", "withdraw": "withdrawn"}[action], aid))


def project_apps(c, uid, pid, status):
    get_project(c, pid, uid, True)
    sql = ("select a.id,a.status,a.message,a.created_at,u.id user_id,u.name,u.bio,u.skills,u.interests,v.role "
           "from applications a join users u on u.id=a.user_id join vacancies v on v.id=a.vacancy_id where a.project_id=?")
    args = [pid]
    if status: sql += " and a.status=?"; args.append(status)
    return [dict(r) for r in c.execute(sql + " order by a.id desc", args)]


def my_projects(c, uid):
    out = []
    for p in c.execute("select * from projects where author_id=? order by id desc", (uid,)):
        vs = vacs(c, p["id"])
        out.append({"id": p["id"], "title": p["title"], "published": bool(p["published"]), "status": rstatus(p, vs),
                    "pending": c.execute("select count(*) n from applications where project_id=? and status='pending'", (p["id"],)).fetchone()["n"]})
    return out


def my_apps(c, uid):
    return [dict(r) for r in c.execute(
        "select a.id,a.status,a.message,a.created_at,p.id project_id,p.title,v.role from applications a "
        "join projects p on p.id=a.project_id join vacancies v on v.id=a.vacancy_id where a.user_id=? order by a.id desc", (uid,))]


# ---------- HTTP ----------
I = lambda g: int(g[0])
ROUTES = [
    ("POST", "/register", 0, lambda c, u, g, b, q: {"id": register(c, b)}),
    ("POST", "/login", 0, lambda c, u, g, b, q: {"_cookie": login(c, b)}),
    ("POST", "/logout", 0, lambda c, u, g, b, q: {"_cookie": ""}),
    ("GET", "/me", 0, lambda c, u, g, b, q: {"user": profile(c, u) if u else None}),
    ("PUT", "/me", 1, lambda c, u, g, b, q: update_me(c, u, b)),
    ("GET", r"/users/(\d+)", 0, lambda c, u, g, b, q: {"user": profile(c, I(g), True)}),
    ("GET", "/categories", 0, lambda c, u, g, b, q: {"categories": CATEGORIES}),
    ("GET", "/projects", 0, lambda c, u, g, b, q: {"projects": catalog(c, q)}),
    ("POST", "/projects", 1, lambda c, u, g, b, q: {"id": create_project(c, u, b)}),
    ("GET", r"/projects/(\d+)", 0, lambda c, u, g, b, q: {"project": project_view(c, u, I(g))}),
    ("PUT", r"/projects/(\d+)", 1, lambda c, u, g, b, q: update_project(c, u, I(g), b)),
    ("POST", r"/projects/(\d+)/publish", 1, lambda c, u, g, b, q: publish(c, u, I(g))),
    ("POST", r"/projects/(\d+)/recruiting", 1, lambda c, u, g, b, q: set_open(c, u, I(g), b.get("open"))),
    ("POST", r"/projects/(\d+)/vacancies", 1, lambda c, u, g, b, q: {"id": add_vacancy(c, u, I(g), b)}),
    ("PUT", r"/vacancies/(\d+)", 1, lambda c, u, g, b, q: update_vacancy(c, u, I(g), b)),
    ("DELETE", r"/vacancies/(\d+)", 1, lambda c, u, g, b, q: delete_vacancy(c, u, I(g))),
    ("POST", r"/projects/(\d+)/apply", 1, lambda c, u, g, b, q: {"id": apply(c, u, I(g), b)}),
    ("GET", r"/projects/(\d+)/applications", 1, lambda c, u, g, b, q: {"applications": project_apps(c, u, I(g), (q.get("status") or [""])[0])}),
    ("POST", r"/applications/(\d+)/(accept|reject|withdraw)", 1, lambda c, u, g, b, q: decide(c, u, I(g), g[1])),
    ("GET", "/my/projects", 1, lambda c, u, g, b, q: {"projects": my_projects(c, u)}),
    ("GET", "/my/applications", 1, lambda c, u, g, b, q: {"applications": my_apps(c, u)}),
]
ROUTES = [(m, re.compile("^/api" + p + "$"), a, f) for m, p, a, f in ROUTES]


class Handler(BaseHTTPRequestHandler):
    def send(self, code, body, ctype="application/json; charset=utf-8", cookie=None):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(data)))
        if cookie is not None:
            self.send_header("Set-Cookie", f"sid={cookie}; Path=/; HttpOnly; SameSite=Lax" + ("" if cookie else "; Max-Age=0"))
        self.end_headers(); self.wfile.write(data)

    def handle_any(self, method):
        url = urlparse(self.path)
        if not url.path.startswith("/api"):
            if method == "GET" and url.path == "/":
                return self.send(200, open(os.path.join(HERE, "index.html"), "rb").read(), "text/html; charset=utf-8")
            return self.send(404, {"error": "Не найдено"})
        c = connect()
        try:
            n = int(self.headers.get("Content-Length") or 0)
            try: body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError: body = None
            if not isinstance(body, dict): bad("Некорректный JSON")
            m = re.search(r"sid=(\w+)", self.headers.get("Cookie", ""))
            uid = user_of(c, m.group(1) if m else None)
            for meth, rx, auth, fn in ROUTES:
                g = rx.match(url.path)
                if meth == method and g:
                    if auth and not uid: bad("Требуется вход", 401)
                    res = fn(c, uid, g.groups(), body, parse_qs(url.query)) or {}
                    if method == "POST" and url.path.endswith("/logout"):
                        c.execute("delete from sessions where token=?", (m.group(1) if m else "",))
                    return self.send(200, res, cookie=res.pop("_cookie", None))
            bad("Маршрут не найден", 404)
        except ApiError as e:
            self.send(e.code, {"error": e.msg})
        except sqlite3.IntegrityError:
            self.send(409, {"error": "Конфликт данных"})
        except Exception as e:
            self.send(500, {"error": "Внутренняя ошибка"}); print("ERR", repr(e), file=sys.stderr)
        finally:
            c.close()

    do_GET = lambda s: s.handle_any("GET")
    do_POST = lambda s: s.handle_any("POST")
    do_PUT = lambda s: s.handle_any("PUT")
    do_DELETE = lambda s: s.handle_any("DELETE")

    def log_message(self, *a): pass


# ---------- данные ----------
def init_db():
    for ext in ("", "-wal", "-shm"):
        if os.path.exists(DB + ext): os.remove(DB + ext)
    c = connect(); c.execute("PRAGMA journal_mode=WAL"); c.executescript(SCHEMA); c.close()


def db_empty():
    if not os.path.exists(DB): return True
    try:
        c = connect(); n = c.execute("select count(*) from users").fetchone()[0]; c.close(); return n == 0
    except sqlite3.Error:
        return True


def seed():
    """Демо-данные. Пароль всех аккаунтов: demo1234."""
    init_db(); c = connect(); t0 = datetime(2026, 10, 1, 10)
    U = {}
    for k, em, name, bio, sk, it in [
        ("anna", "anna@demo.ru", "Анна Автор", "Продакт-менеджер, запускаю проекты с нуля.", "менеджмент, аналитика", "экология, образование"),
        ("boris", "boris@demo.ru", "Борис Кандидат", "Backend-разработчик, люблю Python.", "python, sql, docker", "стартапы, open source"),
        ("vera", "vera@demo.ru", "Вера Кандидат", "UX-дизайнер, автор проекта «Карта доступности».", "figma, ux, исследования", "доступная среда, дизайн"),
        ("oleg", "oleg@demo.ru", "Олег Посторонний", "Просто смотрю каталог.", "музыка", "кино")]:
        U[k] = c.execute("insert into users(email,pw,name,bio,skills,interests) values(?,?,?,?,?,?)", (em, hash_pw("demo1234"), name, bio, sk, it)).lastrowid
    n = [0]
    def ts(): n[0] += 1; return (t0 + timedelta(hours=n[0])).isoformat()
    def proj(a, title, desc, goal, cat, op):
        return c.execute("insert into projects(author_id,title,description,goal,category,published,open,created_at) values(?,?,?,?,?,1,?,?)",
                         (U[a], title, desc, goal, cat, op, ts())).lastrowid
    def vac(p, role, sk, desc, s):
        return c.execute("insert into vacancies(project_id,role,skills,description,seats) values(?,?,?,?,?)", (p, role, sk, desc, s)).lastrowid
    def app(u, p, v, msg, st):
        a = c.execute("insert into applications(user_id,project_id,vacancy_id,message,status,created_at) values(?,?,?,?,?,?)", (U[u], p, v, msg, st, ts())).lastrowid
        if st == "accepted": c.execute("insert into members(project_id,vacancy_id,user_id,application_id) values(?,?,?,?)", (p, v, U[u], a))
    p3 = proj("vera", "Карта доступности", "Мобильная карта городских мест с отметками о доступности для маломобильных людей.", "Релиз MVP с 200 проверенными местами.", "Социальные проекты", 1)
    v31, v32 = vac(p3, "Дизайнер", "figma, ux", "Макеты и прототип.", 1), vac(p3, "Разработчик", "python, react", "API и клиент.", 2)
    app("boris", p3, v31, "Готов взять дизайн-ревью и макеты.", "accepted")  # заполненная вакансия
    p2 = proj("anna", "Школьный радиоэфир", "Школьная радиостанция: подкасты, эфиры и выпуски новостей.", "Регулярный эфир раз в неделю.", "Образование", 0)
    v2 = vac(p2, "Звукорежиссёр", "звук, монтаж", "Запись и монтаж выпусков.", 1)  # закрытый набор
    app("vera", p2, v2, "Могу помочь с оформлением выпусков.", "rejected")
    app("boris", p2, v2, "Занимался звуком в школе, хочу вернуться.", "pending")
    p1 = proj("anna", "EcoTracker", "Приложение для учёта личного углеродного следа и челленджей между друзьями.", "Рабочий прототип к концу семестра.", "Технологии", 1)
    d, ds, qa = vac(p1, "Разработчик", "python, sql", "Бэкенд и API.", 2), vac(p1, "Дизайнер", "figma, ux", "Интерфейс приложения.", 1), vac(p1, "Тестировщик", "qa, pytest", "Тест-планы и автотесты.", 1)
    app("boris", p1, d, "Раньше уже отзывал, передумал.", "withdrawn")
    app("boris", p1, qa, "Писал автотесты на pytest.", "pending")  # последнее место, две заявки
    app("vera", p1, qa, "Умею тестировать UX и API.", "pending")
    c.close()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "reset":
        seed(); print("База восстановлена:", DB); sys.exit()
    if db_empty(): seed()  # нет файла, нет таблиц или нет пользователей
    port = int(os.environ.get("PORT", 8000))
    print(f"http://localhost:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
