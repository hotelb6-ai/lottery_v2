"""員工尾牙抽獎系統 v2

單檔 Flask 應用。滿足規則：
1. 每個員工只能抽一次
2. 每份獎品只會被派給一位員工

支援兩種資料庫：
- 未設 DATABASE_URL → 本機 SQLite（開發用）
- DATABASE_URL 指向 Postgres → 雲端 Postgres（正式部署，資料持久化）

原子性：
- Postgres：SELECT ... FOR UPDATE SKIP LOCKED + 條件式 UPDATE + UNIQUE 約束
- SQLite  ：BEGIN IMMEDIATE + 條件式 UPDATE + UNIQUE 約束
"""
import csv
import io
import os
import secrets
import time
from base64 import b64encode
from collections import defaultdict, deque
from datetime import datetime
from functools import wraps
from pathlib import Path

import qrcode
from flask import (
    Flask, Response, flash, g, redirect,
    render_template, request, session, url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = Path(__file__).resolve().parent


# ============================================================================
# 資料庫抽象層：同一份程式可以跑 SQLite 或 Postgres
# ============================================================================

DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()
# Render / Heroku 有時給的是 postgres:// 前綴（已 deprecated），normalize
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = 'postgresql://' + DATABASE_URL[len('postgres://'):]

IS_PG = DATABASE_URL.startswith('postgresql://')

if IS_PG:
    import psycopg2  # noqa: F401
    import psycopg2.extras
else:
    import sqlite3
    SQLITE_PATH = Path(os.environ.get('DB_PATH', BASE_DIR / 'lottery.db'))


def _connect_raw():
    if IS_PG:
        conn = psycopg2.connect(DATABASE_URL, sslmode='require',
                                cursor_factory=psycopg2.extras.RealDictCursor)
        conn.autocommit = False
        return conn
    conn = sqlite3.connect(SQLITE_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    return conn


def _translate(sql):
    """SQLite 用 ? 佔位符，Postgres 用 %s。"""
    if IS_PG:
        return sql.replace('?', '%s')
    return sql


class DbConn:
    """薄薄一層 wrapper：兩個 driver 統一 execute / commit / rollback 介面。"""
    def __init__(self, raw):
        self._raw = raw
        self._in_tx = False

    def execute(self, sql, params=()):
        cur = self._raw.cursor()
        cur.execute(_translate(sql), params)
        return cur

    def executemany(self, sql, params_list):
        cur = self._raw.cursor()
        cur.executemany(_translate(sql), params_list)
        return cur

    def commit(self):
        self._raw.commit()
        self._in_tx = False

    def rollback(self):
        self._raw.rollback()
        self._in_tx = False

    def close(self):
        try:
            self._raw.close()
        except Exception:
            pass

    def begin(self):
        """在 SQLite 用 BEGIN IMMEDIATE 提升寫入鎖等級；Postgres 用預設。"""
        if IS_PG:
            # psycopg2 是隱式 begin；autocommit=False 已在 _connect_raw 設好
            self._in_tx = True
        else:
            cur = self._raw.cursor()
            cur.execute('BEGIN IMMEDIATE')
            self._in_tx = True


# ============================================================================
# Flask
# ============================================================================

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY') or secrets.token_hex(32)
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
if os.environ.get('FORCE_HTTPS_COOKIE') == '1':
    app.config['SESSION_COOKIE_SECURE'] = True

DEFAULT_ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', 'admin')
DEFAULT_ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'admin1234')


def get_db():
    if 'db' not in g:
        g.db = DbConn(_connect_raw())
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop('db', None)
    if db is not None:
        db.close()


# ============================================================================
# DDL
# ============================================================================

# SQLite DDL
SQLITE_DDL = '''
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS branches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS employees (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_no TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    department TEXT,
    password_hash TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    has_drawn INTEGER NOT NULL DEFAULT 0,
    drawn_at TEXT,
    branch_id INTEGER REFERENCES branches(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS prizes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    tier TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    branch_id INTEGER REFERENCES branches(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS prize_units (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    prize_id INTEGER NOT NULL,
    unit_code TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'AVAILABLE',
    assigned_employee_id INTEGER,
    assigned_at TEXT,
    FOREIGN KEY(prize_id) REFERENCES prizes(id) ON DELETE CASCADE,
    FOREIGN KEY(assigned_employee_id) REFERENCES employees(id)
);

CREATE TABLE IF NOT EXISTS draws (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER NOT NULL UNIQUE,
    prize_unit_id INTEGER NOT NULL UNIQUE,
    drawn_at TEXT NOT NULL,
    request_id TEXT NOT NULL UNIQUE,
    FOREIGN KEY(employee_id) REFERENCES employees(id),
    FOREIGN KEY(prize_unit_id) REFERENCES prize_units(id)
);

CREATE TABLE IF NOT EXISTS admins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
'''

# Postgres DDL
PG_DDL = '''
CREATE TABLE IF NOT EXISTS branches (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS employees (
    id SERIAL PRIMARY KEY,
    employee_no TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    department TEXT,
    password_hash TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    has_drawn INTEGER NOT NULL DEFAULT 0,
    drawn_at TEXT,
    branch_id INTEGER REFERENCES branches(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS prizes (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    tier TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    branch_id INTEGER REFERENCES branches(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS prize_units (
    id SERIAL PRIMARY KEY,
    prize_id INTEGER NOT NULL REFERENCES prizes(id) ON DELETE CASCADE,
    unit_code TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'AVAILABLE',
    assigned_employee_id INTEGER REFERENCES employees(id),
    assigned_at TEXT
);

CREATE TABLE IF NOT EXISTS draws (
    id SERIAL PRIMARY KEY,
    employee_id INTEGER NOT NULL UNIQUE REFERENCES employees(id),
    prize_unit_id INTEGER NOT NULL UNIQUE REFERENCES prize_units(id),
    drawn_at TEXT NOT NULL,
    request_id TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS admins (
    id SERIAL PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
'''


def _has_column(cur, table, col):
    """回傳指定表是否已有此欄位（跨 SQLite / Postgres）。"""
    if IS_PG:
        cur.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = %s AND column_name = %s",
            (table, col),
        )
        return cur.fetchone() is not None
    cur.execute(f"PRAGMA table_info({table})")
    return any(r['name'] == col for r in cur.fetchall())


def init_db():
    raw = _connect_raw()
    cur = raw.cursor()
    if IS_PG:
        cur.execute(PG_DDL)
    else:
        cur.executescript(SQLITE_DDL)

    # 遷移：如果既有 DB 的 employees / prizes 還沒有 branch_id 欄位，補上
    for table in ('employees', 'prizes'):
        if not _has_column(cur, table, 'branch_id'):
            cur.execute(
                f'ALTER TABLE {table} ADD COLUMN branch_id INTEGER REFERENCES branches(id)'
            )

    # 第一次啟動：建管理員
    cur.execute(_translate('SELECT COUNT(*) AS c FROM admins'))
    row = cur.fetchone()
    admin_count = row['c'] if row else 0
    if admin_count == 0:
        cur.execute(
            _translate('INSERT INTO admins(username, password_hash) VALUES (?, ?)'),
            (DEFAULT_ADMIN_USERNAME, generate_password_hash(DEFAULT_ADMIN_PASSWORD)),
        )

    # 遷移：如果 branches 表還沒任何紀錄，建一個「預設館」，把既有員工/獎品都掛過去
    cur.execute(_translate('SELECT COUNT(*) AS c FROM branches'))
    if cur.fetchone()['c'] == 0:
        cur.execute(
            _translate('INSERT INTO branches(name, is_active, created_at) VALUES (?, 1, ?)'),
            ('預設館', now_str()),
        )
        cur.execute(_translate('SELECT id FROM branches WHERE name = ?'), ('預設館',))
        default_branch_id = cur.fetchone()['id']
        cur.execute(
            _translate('UPDATE employees SET branch_id = ? WHERE branch_id IS NULL'),
            (default_branch_id,),
        )
        cur.execute(
            _translate('UPDATE prizes SET branch_id = ? WHERE branch_id IS NULL'),
            (default_branch_id,),
        )
        # 把預設館設為目前活動館
        cur.execute(
            _translate(
                'INSERT INTO settings(key, value) VALUES (?, ?) '
                'ON CONFLICT (key) DO UPDATE SET value = excluded.value'
            ),
            ('active_branch_id', str(default_branch_id)),
        )

    # 預設 settings
    for key, value in [('draw_open', '1'), ('event_title', '尾牙抽獎')]:
        cur.execute(
            _translate(
                'INSERT INTO settings(key, value) VALUES (?, ?) '
                'ON CONFLICT (key) DO NOTHING'
            ),
            (key, value),
        )

    raw.commit()
    raw.close()


def get_setting(key, default=''):
    row = get_db().execute('SELECT value FROM settings WHERE key = ?', (key,)).fetchone()
    return row['value'] if row else default


def set_setting(key, value):
    db = get_db()
    db.execute(
        'INSERT INTO settings(key, value) VALUES (?, ?) '
        'ON CONFLICT (key) DO UPDATE SET value=excluded.value',
        (key, str(value)),
    )
    db.commit()


def draw_is_open():
    return get_setting('draw_open', '1') == '1'


def active_branch_id():
    """目前活動館的 id；未設定回 None。"""
    v = get_setting('active_branch_id', '')
    try:
        return int(v) if v else None
    except (TypeError, ValueError):
        return None


def get_branch(bid):
    if bid is None:
        return None
    return get_db().execute('SELECT * FROM branches WHERE id = ?', (bid,)).fetchone()


def list_branches(only_active=False):
    if only_active:
        return get_db().execute(
            'SELECT * FROM branches WHERE is_active = 1 ORDER BY id'
        ).fetchall()
    return get_db().execute('SELECT * FROM branches ORDER BY id').fetchall()


def now_str():
    return datetime.now().isoformat(timespec='seconds')


def to_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# ============================================================================
# CSRF
# ============================================================================

def get_csrf_token():
    token = session.get('csrf_token')
    if not token:
        token = secrets.token_hex(32)
        session['csrf_token'] = token
    return token


def check_csrf():
    posted = request.form.get('csrf_token') or request.headers.get('X-CSRF-Token', '')
    expected = session.get('csrf_token', '')
    if not expected or not secrets.compare_digest(posted, expected):
        return False
    return True


@app.before_request
def csrf_guard():
    if request.method == 'POST' and not check_csrf():
        return 'CSRF token 驗證失敗，請重新整理頁面後再試。', 400


@app.context_processor
def inject_globals():
    active_id = active_branch_id()
    return {
        'csrf_token': get_csrf_token(),
        'event_title': get_setting('event_title', '尾牙抽獎'),
        'draw_open': draw_is_open(),
        'active_branch': get_branch(active_id) if active_id else None,
    }


# ============================================================================
# 登入速率限制（記憶體）
# ============================================================================

_login_attempts = defaultdict(deque)
_LOGIN_LIMIT = 10
_LOGIN_WINDOW_SEC = 60


def rate_limit_login(bucket):
    now = time.time()
    dq = _login_attempts[bucket]
    while dq and now - dq[0] > _LOGIN_WINDOW_SEC:
        dq.popleft()
    if len(dq) >= _LOGIN_LIMIT:
        return False
    dq.append(now)
    return True


def client_ip():
    return (request.headers.get('X-Forwarded-For', request.remote_addr or 'unknown')
            .split(',')[0].strip())


# ============================================================================
# Decorators
# ============================================================================

def employee_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get('employee_id'):
            return redirect(url_for('login'))
        return fn(*args, **kwargs)
    return wrapper


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get('admin_id'):
            return redirect(url_for('admin_login'))
        return fn(*args, **kwargs)
    return wrapper


def current_employee():
    eid = session.get('employee_id')
    if not eid:
        return None
    return get_db().execute('SELECT * FROM employees WHERE id = ?', (eid,)).fetchone()


# ============================================================================
# 員工端
# ============================================================================

@app.route('/')
def index():
    if session.get('employee_id'):
        return redirect(url_for('draw_page'))
    return redirect(url_for('login'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if not rate_limit_login(f'emp:{client_ip()}'):
            flash('嘗試次數過多，請稍後再試', 'error')
            return render_template('login.html')
        employee_no = request.form.get('employee_no', '').strip()
        password = request.form.get('password', '')
        emp = get_db().execute(
            'SELECT * FROM employees WHERE employee_no = ? AND is_active = 1',
            (employee_no,),
        ).fetchone()
        if not emp or not check_password_hash(emp['password_hash'], password):
            flash('工號或密碼錯誤', 'error')
            return render_template('login.html')
        session.clear()
        session['employee_id'] = emp['id']
        session['csrf_token'] = secrets.token_hex(32)
        return redirect(url_for('draw_page'))
    return render_template('login.html')


@app.route('/logout')
def logout():
    session.pop('employee_id', None)
    return redirect(url_for('login'))


@app.route('/draw', methods=['GET'])
@employee_required
def draw_page():
    emp = current_employee()
    if not emp:
        return redirect(url_for('login'))
    db = get_db()
    record = db.execute(
        '''
        SELECT d.drawn_at, p.name AS prize_name, p.tier AS prize_tier, pu.unit_code
        FROM draws d
        JOIN prize_units pu ON pu.id = d.prize_unit_id
        JOIN prizes p ON p.id = pu.prize_id
        WHERE d.employee_id = ?
        ''',
        (emp['id'],),
    ).fetchone()

    # 判定「本館活動」的狀態：目前啟用館 + 只算此館的獎品
    ab_id = active_branch_id()
    branch_mismatch = (ab_id is not None and emp['branch_id'] != ab_id)
    emp_branch = get_branch(emp['branch_id']) if emp['branch_id'] else None

    if ab_id is None:
        remaining = 0
    else:
        remaining_row = db.execute(
            "SELECT COUNT(*) AS c FROM prize_units pu "
            "JOIN prizes p ON p.id = pu.prize_id "
            "WHERE pu.status='AVAILABLE' AND p.is_active=1 AND p.branch_id = ?",
            (ab_id,),
        ).fetchone()
        remaining = remaining_row['c']

    return render_template(
        'draw.html', employee=emp, record=record, remaining=remaining,
        branch_mismatch=branch_mismatch, emp_branch=emp_branch,
    )


@app.route('/draw', methods=['POST'])
@employee_required
def do_draw():
    emp = current_employee()
    if not emp:
        return redirect(url_for('login'))

    if not draw_is_open():
        flash('抽獎尚未開放或已結束', 'error')
        return redirect(url_for('draw_page'))

    ab_id = active_branch_id()
    if ab_id is None:
        flash('主辦人尚未指定活動館別，請洽現場工作人員', 'error')
        return redirect(url_for('draw_page'))
    if emp['branch_id'] != ab_id:
        flash('本場活動不是您所屬館別的抽獎，請等待您館的場次', 'error')
        return redirect(url_for('draw_page'))

    db = get_db()
    request_id = secrets.token_hex(16)
    try:
        db.begin()

        # 讀員工，Postgres 順便鎖 row
        if IS_PG:
            e = db.execute(
                'SELECT id, has_drawn, branch_id FROM employees '
                'WHERE id = ? AND is_active = 1 FOR UPDATE',
                (emp['id'],),
            ).fetchone()
        else:
            e = db.execute(
                'SELECT id, has_drawn, branch_id FROM employees WHERE id = ? AND is_active = 1',
                (emp['id'],),
            ).fetchone()
        if not e:
            raise ValueError('員工不存在或已停用')
        if e['has_drawn']:
            raise ValueError('您已經抽過獎了')
        if e['branch_id'] != ab_id:
            raise ValueError('本場活動不是您所屬館別的抽獎')

        # 挑一份可用獎品，只限本館的獎品
        if IS_PG:
            prize = db.execute(
                '''
                SELECT pu.id, pu.unit_code, p.name AS prize_name, p.tier AS prize_tier
                FROM prize_units pu
                JOIN prizes p ON p.id = pu.prize_id
                WHERE pu.status = 'AVAILABLE' AND p.is_active = 1
                  AND p.branch_id = ?
                ORDER BY RANDOM() LIMIT 1
                FOR UPDATE OF pu SKIP LOCKED
                ''',
                (ab_id,),
            ).fetchone()
        else:
            prize = db.execute(
                '''
                SELECT pu.id, pu.unit_code, p.name AS prize_name, p.tier AS prize_tier
                FROM prize_units pu
                JOIN prizes p ON p.id = pu.prize_id
                WHERE pu.status = 'AVAILABLE' AND p.is_active = 1
                  AND p.branch_id = ?
                ORDER BY RANDOM() LIMIT 1
                ''',
                (ab_id,),
            ).fetchone()
        if not prize:
            raise ValueError('本館獎品已抽完，感謝參與')

        now = now_str()
        # 條件式 UPDATE：只在 status 仍是 AVAILABLE 時才更新（雙保險）
        changed = db.execute(
            "UPDATE prize_units SET status='ASSIGNED', assigned_employee_id=?, assigned_at=? "
            "WHERE id=? AND status='AVAILABLE'",
            (e['id'], now, prize['id']),
        ).rowcount
        if changed != 1:
            raise ValueError('這份獎品剛被搶走了，請再試一次')

        db.execute(
            'UPDATE employees SET has_drawn=1, drawn_at=? WHERE id=?',
            (now, e['id']),
        )
        db.execute(
            'INSERT INTO draws(employee_id, prize_unit_id, drawn_at, request_id) '
            'VALUES (?, ?, ?, ?)',
            (e['id'], prize['id'], now, request_id),
        )
        db.commit()
        flash(f"恭喜抽中：{prize['prize_name']}（{prize['prize_tier'] or '普獎'}）", 'success')
    except Exception as ex:
        db.rollback()
        flash(str(ex), 'error')
    return redirect(url_for('draw_page'))


# ============================================================================
# 管理員登入
# ============================================================================

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        if not rate_limit_login(f'adm:{client_ip()}'):
            flash('嘗試次數過多，請稍後再試', 'error')
            return render_template('admin_login.html')
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        row = get_db().execute(
            'SELECT * FROM admins WHERE username = ?', (username,)
        ).fetchone()
        if not row or not check_password_hash(row['password_hash'], password):
            flash('管理員帳號或密碼錯誤', 'error')
            return render_template('admin_login.html')
        session.clear()
        session['admin_id'] = row['id']
        session['admin_name'] = row['username']
        session['csrf_token'] = secrets.token_hex(32)
        return redirect(url_for('admin_dashboard'))
    return render_template('admin_login.html')


@app.route('/admin/logout')
def admin_logout():
    session.pop('admin_id', None)
    session.pop('admin_name', None)
    return redirect(url_for('admin_login'))


# ============================================================================
# 管理員：Dashboard
# ============================================================================

@app.route('/admin/')
@admin_required
def admin_dashboard():
    db = get_db()
    show_all = request.args.get('all') == '1'
    ab_id = active_branch_id()
    filter_branch = (not show_all) and ab_id is not None

    # Summary（依有無「顯示全部」而定）
    if filter_branch:
        summary = {
            'employee_total': db.execute(
                'SELECT COUNT(*) AS c FROM employees WHERE is_active=1 AND branch_id = ?',
                (ab_id,),
            ).fetchone()['c'],
            'drawn_total': db.execute(
                'SELECT COUNT(*) AS c FROM draws d '
                'JOIN employees e ON e.id = d.employee_id WHERE e.branch_id = ?',
                (ab_id,),
            ).fetchone()['c'],
            'prize_total': db.execute(
                'SELECT COUNT(*) AS c FROM prize_units pu '
                'JOIN prizes p ON p.id = pu.prize_id WHERE p.branch_id = ?',
                (ab_id,),
            ).fetchone()['c'],
            'remaining_total': db.execute(
                "SELECT COUNT(*) AS c FROM prize_units pu "
                "JOIN prizes p ON p.id = pu.prize_id "
                "WHERE pu.status='AVAILABLE' AND p.branch_id = ?",
                (ab_id,),
            ).fetchone()['c'],
        }
        records = db.execute(
            '''
            SELECT e.employee_no, e.name, e.department, b.name AS branch_name,
                   d.drawn_at, p.name AS prize_name, p.tier AS prize_tier, pu.unit_code
            FROM draws d
            JOIN employees e ON e.id = d.employee_id
            LEFT JOIN branches b ON b.id = e.branch_id
            JOIN prize_units pu ON pu.id = d.prize_unit_id
            JOIN prizes p ON p.id = pu.prize_id
            WHERE e.branch_id = ?
            ORDER BY d.drawn_at DESC
            ''',
            (ab_id,),
        ).fetchall()
        inventory = db.execute(
            '''
            SELECT p.id, p.name, p.tier, b.name AS branch_name,
                   SUM(CASE WHEN pu.status='AVAILABLE' THEN 1 ELSE 0 END) AS remaining,
                   COUNT(*) AS total
            FROM prize_units pu
            JOIN prizes p ON p.id = pu.prize_id
            LEFT JOIN branches b ON b.id = p.branch_id
            WHERE p.branch_id = ?
            GROUP BY p.id, p.name, p.tier, b.name
            ORDER BY p.id
            ''',
            (ab_id,),
        ).fetchall()
    else:
        summary = {
            'employee_total': db.execute('SELECT COUNT(*) AS c FROM employees WHERE is_active=1').fetchone()['c'],
            'drawn_total': db.execute('SELECT COUNT(*) AS c FROM draws').fetchone()['c'],
            'prize_total': db.execute('SELECT COUNT(*) AS c FROM prize_units').fetchone()['c'],
            'remaining_total': db.execute("SELECT COUNT(*) AS c FROM prize_units WHERE status='AVAILABLE'").fetchone()['c'],
        }
        records = db.execute(
            '''
            SELECT e.employee_no, e.name, e.department, b.name AS branch_name,
                   d.drawn_at, p.name AS prize_name, p.tier AS prize_tier, pu.unit_code
            FROM draws d
            JOIN employees e ON e.id = d.employee_id
            LEFT JOIN branches b ON b.id = e.branch_id
            JOIN prize_units pu ON pu.id = d.prize_unit_id
            JOIN prizes p ON p.id = pu.prize_id
            ORDER BY d.drawn_at DESC
            '''
        ).fetchall()
        inventory = db.execute(
            '''
            SELECT p.id, p.name, p.tier, b.name AS branch_name,
                   SUM(CASE WHEN pu.status='AVAILABLE' THEN 1 ELSE 0 END) AS remaining,
                   COUNT(*) AS total
            FROM prize_units pu
            JOIN prizes p ON p.id = pu.prize_id
            LEFT JOIN branches b ON b.id = p.branch_id
            GROUP BY p.id, p.name, p.tier, b.name
            ORDER BY p.id
            '''
        ).fetchall()
    return render_template(
        'admin/dashboard.html',
        summary=summary, records=records, inventory=inventory,
        show_all=show_all, filter_branch=filter_branch,
    )


# ============================================================================
# 管理員：員工管理
# ============================================================================

@app.route('/admin/employees')
@admin_required
def admin_employees():
    branches = list_branches()
    emps = get_db().execute(
        '''
        SELECT e.*, b.name AS branch_name
        FROM employees e
        LEFT JOIN branches b ON b.id = e.branch_id
        ORDER BY e.is_active DESC, e.branch_id, e.employee_no
        '''
    ).fetchall()
    return render_template('admin/employees.html', employees=emps, branches=branches,
                           active_branch_id=active_branch_id())


@app.route('/admin/employees/new', methods=['POST'])
@admin_required
def admin_employee_new():
    employee_no = request.form.get('employee_no', '').strip()
    name = request.form.get('name', '').strip()
    department = request.form.get('department', '').strip()
    password = request.form.get('password', '').strip()
    branch_id = to_int(request.form.get('branch_id', ''), 0) or None
    if not employee_no or not name or not password:
        flash('工號、姓名、密碼皆必填', 'error')
        return redirect(url_for('admin_employees'))
    if not branch_id:
        flash('請選擇館別', 'error')
        return redirect(url_for('admin_employees'))
    db = get_db()
    try:
        db.execute(
            'INSERT INTO employees(employee_no, name, department, password_hash, branch_id, created_at) '
            'VALUES (?, ?, ?, ?, ?, ?)',
            (employee_no, name, department, generate_password_hash(password),
             branch_id, now_str()),
        )
        db.commit()
        flash(f'已新增：{name}（{employee_no}）', 'success')
    except Exception as e:
        db.rollback()
        if 'unique' in str(e).lower() or 'duplicate' in str(e).lower():
            flash(f'工號 {employee_no} 已存在', 'error')
        else:
            flash(f'新增失敗：{e}', 'error')
    return redirect(url_for('admin_employees'))


@app.route('/admin/employees/bulk', methods=['POST'])
@admin_required
def admin_employee_bulk():
    """一次貼一段 CSV：工號,姓名,部門,密碼（附加：預設館別由表單選定）"""
    raw = request.form.get('csv_data', '').strip()
    branch_id = to_int(request.form.get('branch_id', ''), 0) or None
    if not raw:
        flash('請貼上 CSV 資料', 'error')
        return redirect(url_for('admin_employees'))
    if not branch_id:
        flash('請選擇要匯入到哪一館', 'error')
        return redirect(url_for('admin_employees'))
    ok, dup, bad = 0, 0, 0
    db = get_db()
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        parts = [p.strip() for p in line.split(',')]
        if len(parts) < 4:
            bad += 1
            continue
        employee_no, name, department, password = parts[0], parts[1], parts[2], parts[3]
        if not employee_no or not name or not password:
            bad += 1
            continue
        try:
            db.execute(
                'INSERT INTO employees(employee_no, name, department, password_hash, branch_id, created_at) '
                'VALUES (?, ?, ?, ?, ?, ?)',
                (employee_no, name, department, generate_password_hash(password),
                 branch_id, now_str()),
            )
            db.commit()
            ok += 1
        except Exception as e:
            db.rollback()
            if 'unique' in str(e).lower() or 'duplicate' in str(e).lower():
                dup += 1
            else:
                bad += 1
    flash(f'匯入完成：成功 {ok}、重複 {dup}、格式錯誤 {bad}',
          'success' if ok else 'error')
    return redirect(url_for('admin_employees'))


@app.route('/admin/employees/<int:emp_id>/edit', methods=['POST'])
@admin_required
def admin_employee_edit(emp_id):
    name = request.form.get('name', '').strip()
    department = request.form.get('department', '').strip()
    password = request.form.get('password', '').strip()
    branch_id = to_int(request.form.get('branch_id', ''), 0) or None
    if not name:
        flash('姓名必填', 'error')
        return redirect(url_for('admin_employees'))
    if not branch_id:
        flash('請選擇館別', 'error')
        return redirect(url_for('admin_employees'))
    db = get_db()
    if password:
        db.execute(
            'UPDATE employees SET name=?, department=?, branch_id=?, password_hash=? WHERE id=?',
            (name, department, branch_id, generate_password_hash(password), emp_id),
        )
    else:
        db.execute(
            'UPDATE employees SET name=?, department=?, branch_id=? WHERE id=?',
            (name, department, branch_id, emp_id),
        )
    db.commit()
    flash('已更新', 'success')
    return redirect(url_for('admin_employees'))


@app.route('/admin/employees/<int:emp_id>/toggle', methods=['POST'])
@admin_required
def admin_employee_toggle(emp_id):
    db = get_db()
    db.execute(
        'UPDATE employees SET is_active = 1 - is_active WHERE id = ?', (emp_id,)
    )
    db.commit()
    return redirect(url_for('admin_employees'))


# ============================================================================
# 管理員：獎品管理
# ============================================================================

@app.route('/admin/prizes')
@admin_required
def admin_prizes():
    branches = list_branches()
    prizes = get_db().execute(
        '''
        SELECT p.id, p.name, p.tier, p.is_active, p.created_at, p.branch_id,
               b.name AS branch_name,
               (SELECT COUNT(*) FROM prize_units pu WHERE pu.prize_id = p.id) AS total,
               (SELECT COUNT(*) FROM prize_units pu
                 WHERE pu.prize_id = p.id AND pu.status = 'AVAILABLE') AS remaining
        FROM prizes p
        LEFT JOIN branches b ON b.id = p.branch_id
        ORDER BY p.branch_id, p.id
        '''
    ).fetchall()
    return render_template('admin/prizes.html', prizes=prizes, branches=branches,
                           active_branch_id=active_branch_id())


@app.route('/admin/prizes/new', methods=['POST'])
@admin_required
def admin_prize_new():
    name = request.form.get('name', '').strip()
    tier = request.form.get('tier', '').strip() or '普獎'
    quantity = max(1, to_int(request.form.get('quantity', '1'), 1))
    branch_id = to_int(request.form.get('branch_id', ''), 0) or None
    if not name:
        flash('獎項名稱必填', 'error')
        return redirect(url_for('admin_prizes'))
    if not branch_id:
        flash('請選擇館別', 'error')
        return redirect(url_for('admin_prizes'))
    db = get_db()
    if IS_PG:
        cur = db.execute(
            'INSERT INTO prizes(name, tier, branch_id, created_at) VALUES (?, ?, ?, ?) RETURNING id',
            (name, tier, branch_id, now_str()),
        )
        prize_id = cur.fetchone()['id']
    else:
        cur = db.execute(
            'INSERT INTO prizes(name, tier, branch_id, created_at) VALUES (?, ?, ?, ?)',
            (name, tier, branch_id, now_str()),
        )
        prize_id = cur.lastrowid

    # 一律加隨機後綴，避免同名獎項的 unit_code 撞名
    suffix = secrets.token_hex(2)
    units = [(prize_id, f"{name}-{suffix}-{i:03d}") for i in range(1, quantity + 1)]
    db.executemany(
        'INSERT INTO prize_units(prize_id, unit_code) VALUES (?, ?)', units
    )
    db.commit()
    flash(f'已新增獎品：{name} x {quantity}', 'success')
    return redirect(url_for('admin_prizes'))


@app.route('/admin/prizes/<int:prize_id>/edit', methods=['POST'])
@admin_required
def admin_prize_edit(prize_id):
    name = request.form.get('name', '').strip()
    tier = request.form.get('tier', '').strip() or '普獎'
    branch_id = to_int(request.form.get('branch_id', ''), 0) or None
    if not name:
        flash('獎項名稱必填', 'error')
        return redirect(url_for('admin_prizes'))
    if not branch_id:
        flash('請選擇館別', 'error')
        return redirect(url_for('admin_prizes'))
    db = get_db()
    db.execute(
        'UPDATE prizes SET name=?, tier=?, branch_id=? WHERE id=?',
        (name, tier, branch_id, prize_id),
    )
    db.commit()
    flash('已更新', 'success')
    return redirect(url_for('admin_prizes'))


@app.route('/admin/prizes/<int:prize_id>/add_units', methods=['POST'])
@admin_required
def admin_prize_add_units(prize_id):
    qty = max(1, to_int(request.form.get('quantity', '1'), 1))
    db = get_db()
    prize = db.execute('SELECT name FROM prizes WHERE id = ?', (prize_id,)).fetchone()
    if not prize:
        flash('找不到此獎項', 'error')
        return redirect(url_for('admin_prizes'))
    existing = db.execute(
        'SELECT COUNT(*) AS c FROM prize_units WHERE prize_id = ?', (prize_id,)
    ).fetchone()['c']
    units = [
        (prize_id, f"{prize['name']}-{i:03d}-{secrets.token_hex(2)}")
        for i in range(existing + 1, existing + qty + 1)
    ]
    db.executemany('INSERT INTO prize_units(prize_id, unit_code) VALUES (?, ?)', units)
    db.commit()
    flash(f'已加開 {qty} 份', 'success')
    return redirect(url_for('admin_prizes'))


@app.route('/admin/prizes/<int:prize_id>/toggle', methods=['POST'])
@admin_required
def admin_prize_toggle(prize_id):
    db = get_db()
    db.execute('UPDATE prizes SET is_active = 1 - is_active WHERE id = ?', (prize_id,))
    db.commit()
    return redirect(url_for('admin_prizes'))


@app.route('/admin/prizes/<int:prize_id>/delete', methods=['POST'])
@admin_required
def admin_prize_delete(prize_id):
    db = get_db()
    assigned = db.execute(
        "SELECT COUNT(*) AS c FROM prize_units WHERE prize_id = ? AND status = 'ASSIGNED'",
        (prize_id,),
    ).fetchone()['c']
    if assigned:
        flash('此獎項已有中獎紀錄，無法刪除。可改為停用。', 'error')
        return redirect(url_for('admin_prizes'))
    db.execute('DELETE FROM prizes WHERE id = ?', (prize_id,))
    db.commit()
    flash('已刪除', 'success')
    return redirect(url_for('admin_prizes'))


# ============================================================================
# 管理員：系統設定
# ============================================================================

@app.route('/admin/settings', methods=['GET'])
@admin_required
def admin_settings():
    return render_template('admin/settings.html',
                           branches=list_branches(),
                           active_branch_id=active_branch_id())


@app.route('/admin/settings/set_active_branch', methods=['POST'])
@admin_required
def admin_set_active_branch():
    bid = to_int(request.form.get('branch_id', ''), 0) or None
    if bid is None or not get_branch(bid):
        flash('請選擇館別', 'error')
        return redirect(url_for('admin_settings'))
    set_setting('active_branch_id', str(bid))
    branch = get_branch(bid)
    flash(f'目前活動館別已切換為：{branch["name"]}', 'success')
    return redirect(url_for('admin_settings'))


@app.route('/admin/settings/toggle_draw', methods=['POST'])
@admin_required
def admin_toggle_draw():
    set_setting('draw_open', '0' if draw_is_open() else '1')
    flash('已切換抽獎狀態', 'success')
    return redirect(url_for('admin_settings'))


@app.route('/admin/settings/event_title', methods=['POST'])
@admin_required
def admin_set_event_title():
    title = request.form.get('event_title', '').strip() or '尾牙抽獎'
    set_setting('event_title', title)
    flash('已更新活動名稱', 'success')
    return redirect(url_for('admin_settings'))


@app.route('/admin/settings/reset', methods=['POST'])
@admin_required
def admin_reset():
    confirm = request.form.get('confirm', '')
    scope = request.form.get('scope', 'active')  # 'active' 或 'all'
    if confirm != 'RESET':
        flash('請於欄位輸入 RESET 確認', 'error')
        return redirect(url_for('admin_settings'))
    db = get_db()
    try:
        db.begin()
        if scope == 'all':
            db.execute('DELETE FROM draws')
            db.execute(
                "UPDATE prize_units SET status='AVAILABLE', assigned_employee_id=NULL, assigned_at=NULL"
            )
            db.execute('UPDATE employees SET has_drawn=0, drawn_at=NULL')
            msg = '已重置全部館別的抽獎紀錄'
        else:
            ab_id = active_branch_id()
            if ab_id is None:
                raise ValueError('請先在下方指定目前活動館別再重置')
            # 只刪本館的 draws
            db.execute(
                'DELETE FROM draws WHERE employee_id IN '
                '(SELECT id FROM employees WHERE branch_id = ?)',
                (ab_id,),
            )
            # 只把本館的 prize_units 復原
            db.execute(
                "UPDATE prize_units SET status='AVAILABLE', "
                "assigned_employee_id=NULL, assigned_at=NULL "
                "WHERE prize_id IN (SELECT id FROM prizes WHERE branch_id = ?)",
                (ab_id,),
            )
            db.execute(
                'UPDATE employees SET has_drawn=0, drawn_at=NULL WHERE branch_id = ?',
                (ab_id,),
            )
            branch = get_branch(ab_id)
            msg = f'已重置「{branch["name"] if branch else "本館"}」的抽獎紀錄'
        db.commit()
        flash(msg, 'success')
    except Exception as ex:
        db.rollback()
        flash(f'重置失敗：{ex}', 'error')
    return redirect(url_for('admin_settings'))


# ============================================================================
# 館別 CRUD
# ============================================================================

@app.route('/admin/branches')
@admin_required
def admin_branches():
    branches = get_db().execute(
        '''
        SELECT b.*,
               (SELECT COUNT(*) FROM employees e WHERE e.branch_id = b.id) AS emp_count,
               (SELECT COUNT(*) FROM prizes p WHERE p.branch_id = b.id) AS prize_count
        FROM branches b
        ORDER BY b.id
        '''
    ).fetchall()
    return render_template('admin/branches.html', branches=branches,
                           active_branch_id=active_branch_id())


@app.route('/admin/branches/new', methods=['POST'])
@admin_required
def admin_branch_new():
    name = request.form.get('name', '').strip()
    if not name:
        flash('館別名稱必填', 'error')
        return redirect(url_for('admin_branches'))
    db = get_db()
    try:
        db.execute(
            'INSERT INTO branches(name, is_active, created_at) VALUES (?, 1, ?)',
            (name, now_str()),
        )
        db.commit()
        flash(f'已新增館別：{name}', 'success')
    except Exception as e:
        db.rollback()
        if 'unique' in str(e).lower() or 'duplicate' in str(e).lower():
            flash(f'館別「{name}」已存在', 'error')
        else:
            flash(f'新增失敗：{e}', 'error')
    return redirect(url_for('admin_branches'))


@app.route('/admin/branches/<int:bid>/edit', methods=['POST'])
@admin_required
def admin_branch_edit(bid):
    name = request.form.get('name', '').strip()
    if not name:
        flash('館別名稱必填', 'error')
        return redirect(url_for('admin_branches'))
    db = get_db()
    try:
        db.execute('UPDATE branches SET name=? WHERE id=?', (name, bid))
        db.commit()
        flash('已更新館別名稱', 'success')
    except Exception as e:
        db.rollback()
        flash(f'更新失敗：{e}', 'error')
    return redirect(url_for('admin_branches'))


@app.route('/admin/branches/<int:bid>/activate', methods=['POST'])
@admin_required
def admin_branch_activate(bid):
    branch = get_branch(bid)
    if not branch:
        flash('找不到此館別', 'error')
        return redirect(url_for('admin_branches'))
    set_setting('active_branch_id', str(bid))
    flash(f'目前活動館別已切換為：{branch["name"]}', 'success')
    return redirect(url_for('admin_branches'))


@app.route('/admin/branches/<int:bid>/delete', methods=['POST'])
@admin_required
def admin_branch_delete(bid):
    db = get_db()
    emp_count = db.execute(
        'SELECT COUNT(*) AS c FROM employees WHERE branch_id = ?', (bid,)
    ).fetchone()['c']
    prize_count = db.execute(
        'SELECT COUNT(*) AS c FROM prizes WHERE branch_id = ?', (bid,)
    ).fetchone()['c']
    if emp_count or prize_count:
        flash(f'此館別還有 {emp_count} 位員工、{prize_count} 個獎項，請先移除或改館別再刪', 'error')
        return redirect(url_for('admin_branches'))
    if active_branch_id() == bid:
        set_setting('active_branch_id', '')
    db.execute('DELETE FROM branches WHERE id = ?', (bid,))
    db.commit()
    flash('已刪除館別', 'success')
    return redirect(url_for('admin_branches'))


@app.route('/admin/settings/change_password', methods=['POST'])
@admin_required
def admin_change_password():
    old = request.form.get('old_password', '')
    new = request.form.get('new_password', '')
    if len(new) < 6:
        flash('新密碼長度至少 6 字元', 'error')
        return redirect(url_for('admin_settings'))
    admin_id = session['admin_id']
    db = get_db()
    row = db.execute(
        'SELECT password_hash FROM admins WHERE id = ?', (admin_id,)
    ).fetchone()
    if not row or not check_password_hash(row['password_hash'], old):
        flash('原密碼錯誤', 'error')
        return redirect(url_for('admin_settings'))
    db.execute(
        'UPDATE admins SET password_hash = ? WHERE id = ?',
        (generate_password_hash(new), admin_id),
    )
    db.commit()
    flash('密碼已更新', 'success')
    return redirect(url_for('admin_settings'))


@app.route('/admin/export/winners.csv')
@admin_required
def admin_export_winners():
    rows = get_db().execute(
        '''
        SELECT e.employee_no, e.name, e.department,
               p.name AS prize_name, p.tier AS prize_tier,
               pu.unit_code, d.drawn_at
        FROM draws d
        JOIN employees e ON e.id = d.employee_id
        JOIN prize_units pu ON pu.id = d.prize_unit_id
        JOIN prizes p ON p.id = pu.prize_id
        ORDER BY d.drawn_at
        '''
    ).fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['工號', '姓名', '部門', '獎項', '等級', '獎品編號', '抽獎時間'])
    for r in rows:
        writer.writerow([
            r['employee_no'], r['name'], r['department'] or '',
            r['prize_name'], r['prize_tier'] or '',
            r['unit_code'], r['drawn_at'],
        ])
    csv_bytes = '﻿'.encode('utf-8') + output.getvalue().encode('utf-8')
    return Response(
        csv_bytes,
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename="winners.csv"'},
    )


# ============================================================================
# QR Code
# ============================================================================

@app.route('/admin/qrcode')
@admin_required
def admin_qrcode():
    target = request.args.get('url') or request.host_url.rstrip('/') + url_for('login')
    img = qrcode.make(target)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    qr_b64 = b64encode(buf.getvalue()).decode('ascii')
    return render_template('admin/qrcode.html', target_url=target, qr_b64=qr_b64)


# ============================================================================
# 健康檢查
# ============================================================================

@app.route('/healthz')
def healthz():
    try:
        row = get_db().execute("SELECT 1 AS ok").fetchone()
        return {'ok': True, 'db': 'pg' if IS_PG else 'sqlite', 'result': dict(row)}
    except Exception as e:
        return {'ok': False, 'error': str(e)}, 500


# ============================================================================
# 啟動
# ============================================================================

with app.app_context():
    init_db()


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8000)), debug=False)
