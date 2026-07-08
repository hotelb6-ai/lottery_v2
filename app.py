"""員工尾牙抽獎系統 v2

單檔 Flask 應用。滿足規則：
1. 每個員工只能抽一次
2. 每份獎品只會被派給一位員工

原子性由 SQLite BEGIN IMMEDIATE + UNIQUE 約束共同保證。
"""
import csv
import io
import os
import secrets
import sqlite3
import time
from base64 import b64encode
from collections import defaultdict, deque
from datetime import datetime
from functools import wraps
from pathlib import Path

import qrcode
from flask import (
    Flask, Response, flash, g, jsonify, redirect,
    render_template, request, session, url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get('DB_PATH', BASE_DIR / 'lottery.db'))

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY') or secrets.token_hex(32)
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
if os.environ.get('FORCE_HTTPS_COOKIE') == '1':
    app.config['SESSION_COOKIE_SECURE'] = True

DEFAULT_ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', 'admin')
DEFAULT_ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'admin1234')


# ----------------------------- DB ------------------------------------------

def get_db():
    if 'db' not in g:
        conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys = ON')
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop('db', None)
    if conn is not None:
        conn.close()


def init_db():
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.executescript(
        '''
        PRAGMA journal_mode=WAL;
        PRAGMA foreign_keys = ON;

        CREATE TABLE IF NOT EXISTS employees (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_no TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            department TEXT,
            password_hash TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            has_drawn INTEGER NOT NULL DEFAULT 0,
            drawn_at TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS prizes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            tier TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
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
    )

    admin_row = cur.execute('SELECT COUNT(*) FROM admins').fetchone()[0]
    if admin_row == 0:
        cur.execute(
            'INSERT INTO admins(username, password_hash) VALUES (?, ?)',
            (DEFAULT_ADMIN_USERNAME, generate_password_hash(DEFAULT_ADMIN_PASSWORD)),
        )

    for key, value in [('draw_open', '1'), ('event_title', '尾牙抽獎')]:
        cur.execute(
            'INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)',
            (key, value),
        )

    conn.commit()
    conn.close()


def get_setting(key, default=''):
    row = get_db().execute('SELECT value FROM settings WHERE key = ?', (key,)).fetchone()
    return row['value'] if row else default


def set_setting(key, value):
    get_db().execute(
        'INSERT INTO settings(key, value) VALUES (?, ?) '
        'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
        (key, str(value)),
    )


def draw_is_open():
    return get_setting('draw_open', '1') == '1'


def now_str():
    return datetime.now().isoformat(timespec='seconds')


# ---------------------------- CSRF -----------------------------------------

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
    return {
        'csrf_token': get_csrf_token(),
        'event_title': get_setting('event_title', '尾牙抽獎'),
        'draw_open': draw_is_open(),
    }


# ---------------------- 登入速率限制（記憶體） -------------------------------

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
    return request.headers.get('X-Forwarded-For', request.remote_addr or 'unknown').split(',')[0].strip()


# ---------------------------- Decorators -----------------------------------

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


# =============================================================================
# 員工端
# =============================================================================

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
    conn = get_db()
    record = conn.execute(
        '''
        SELECT d.drawn_at, p.name AS prize_name, p.tier AS prize_tier, pu.unit_code
        FROM draws d
        JOIN prize_units pu ON pu.id = d.prize_unit_id
        JOIN prizes p ON p.id = pu.prize_id
        WHERE d.employee_id = ?
        ''',
        (emp['id'],),
    ).fetchone()
    remaining = conn.execute(
        "SELECT COUNT(*) FROM prize_units WHERE status = 'AVAILABLE'"
    ).fetchone()[0]
    return render_template('draw.html', employee=emp, record=record, remaining=remaining)


@app.route('/draw', methods=['POST'])
@employee_required
def do_draw():
    emp = current_employee()
    if not emp:
        return redirect(url_for('login'))

    if not draw_is_open():
        flash('抽獎尚未開放或已結束', 'error')
        return redirect(url_for('draw_page'))

    conn = get_db()
    cur = conn.cursor()
    request_id = secrets.token_hex(16)
    try:
        cur.execute('BEGIN IMMEDIATE')
        e = cur.execute(
            'SELECT id, has_drawn FROM employees WHERE id = ? AND is_active = 1',
            (emp['id'],),
        ).fetchone()
        if not e:
            raise ValueError('員工不存在或已停用')
        if e['has_drawn']:
            raise ValueError('您已經抽過獎了')

        prize = cur.execute(
            '''
            SELECT pu.id, pu.unit_code, p.name AS prize_name, p.tier AS prize_tier
            FROM prize_units pu
            JOIN prizes p ON p.id = pu.prize_id
            WHERE pu.status = 'AVAILABLE' AND p.is_active = 1
            ORDER BY RANDOM() LIMIT 1
            '''
        ).fetchone()
        if not prize:
            raise ValueError('獎品已抽完，感謝參與')

        now = now_str()
        # 條件式 UPDATE：只在 status 仍是 AVAILABLE 時才更新
        changed = cur.execute(
            "UPDATE prize_units SET status='ASSIGNED', assigned_employee_id=?, assigned_at=? "
            "WHERE id=? AND status='AVAILABLE'",
            (e['id'], now, prize['id']),
        ).rowcount
        if changed != 1:
            raise ValueError('這份獎品剛被搶走了，請再試一次')

        cur.execute(
            'UPDATE employees SET has_drawn=1, drawn_at=? WHERE id=?',
            (now, e['id']),
        )
        cur.execute(
            'INSERT INTO draws(employee_id, prize_unit_id, drawn_at, request_id) '
            'VALUES (?, ?, ?, ?)',
            (e['id'], prize['id'], now, request_id),
        )
        conn.commit()
        flash(f"恭喜抽中：{prize['prize_name']}（{prize['prize_tier'] or '普獎'}）", 'success')
    except Exception as ex:
        conn.rollback()
        flash(str(ex), 'error')
    return redirect(url_for('draw_page'))


# =============================================================================
# 管理員登入
# =============================================================================

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


# =============================================================================
# 管理員：Dashboard
# =============================================================================

@app.route('/admin/')
@admin_required
def admin_dashboard():
    conn = get_db()
    summary = {
        'employee_total': conn.execute('SELECT COUNT(*) FROM employees WHERE is_active=1').fetchone()[0],
        'drawn_total': conn.execute('SELECT COUNT(*) FROM draws').fetchone()[0],
        'prize_total': conn.execute('SELECT COUNT(*) FROM prize_units').fetchone()[0],
        'remaining_total': conn.execute("SELECT COUNT(*) FROM prize_units WHERE status='AVAILABLE'").fetchone()[0],
    }
    records = conn.execute(
        '''
        SELECT e.employee_no, e.name, e.department,
               d.drawn_at, p.name AS prize_name, p.tier AS prize_tier, pu.unit_code
        FROM draws d
        JOIN employees e ON e.id = d.employee_id
        JOIN prize_units pu ON pu.id = d.prize_unit_id
        JOIN prizes p ON p.id = pu.prize_id
        ORDER BY d.drawn_at DESC
        '''
    ).fetchall()
    inventory = conn.execute(
        '''
        SELECT p.id, p.name, p.tier,
               SUM(CASE WHEN pu.status='AVAILABLE' THEN 1 ELSE 0 END) AS remaining,
               COUNT(*) AS total
        FROM prize_units pu
        JOIN prizes p ON p.id = pu.prize_id
        GROUP BY p.id, p.name, p.tier
        ORDER BY p.id
        '''
    ).fetchall()
    return render_template('admin/dashboard.html',
                           summary=summary, records=records, inventory=inventory)


# =============================================================================
# 管理員：員工管理
# =============================================================================

@app.route('/admin/employees')
@admin_required
def admin_employees():
    emps = get_db().execute(
        'SELECT * FROM employees ORDER BY is_active DESC, employee_no'
    ).fetchall()
    return render_template('admin/employees.html', employees=emps)


@app.route('/admin/employees/new', methods=['POST'])
@admin_required
def admin_employee_new():
    employee_no = request.form.get('employee_no', '').strip()
    name = request.form.get('name', '').strip()
    department = request.form.get('department', '').strip()
    password = request.form.get('password', '').strip()
    if not employee_no or not name or not password:
        flash('工號、姓名、密碼皆必填', 'error')
        return redirect(url_for('admin_employees'))
    try:
        get_db().execute(
            'INSERT INTO employees(employee_no, name, department, password_hash, created_at) '
            'VALUES (?, ?, ?, ?, ?)',
            (employee_no, name, department, generate_password_hash(password), now_str()),
        )
        flash(f'已新增：{name}（{employee_no}）', 'success')
    except sqlite3.IntegrityError:
        flash(f'工號 {employee_no} 已存在', 'error')
    return redirect(url_for('admin_employees'))


@app.route('/admin/employees/bulk', methods=['POST'])
@admin_required
def admin_employee_bulk():
    """一次貼一段 CSV：工號,姓名,部門,密碼"""
    raw = request.form.get('csv_data', '').strip()
    if not raw:
        flash('請貼上 CSV 資料', 'error')
        return redirect(url_for('admin_employees'))
    ok, dup, bad = 0, 0, 0
    conn = get_db()
    for lineno, line in enumerate(raw.splitlines(), 1):
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
            conn.execute(
                'INSERT INTO employees(employee_no, name, department, password_hash, created_at) '
                'VALUES (?, ?, ?, ?, ?)',
                (employee_no, name, department, generate_password_hash(password), now_str()),
            )
            ok += 1
        except sqlite3.IntegrityError:
            dup += 1
    flash(f'匯入完成：成功 {ok}、重複 {dup}、格式錯誤 {bad}',
          'success' if ok else 'error')
    return redirect(url_for('admin_employees'))


@app.route('/admin/employees/<int:emp_id>/edit', methods=['POST'])
@admin_required
def admin_employee_edit(emp_id):
    name = request.form.get('name', '').strip()
    department = request.form.get('department', '').strip()
    password = request.form.get('password', '').strip()
    if not name:
        flash('姓名必填', 'error')
        return redirect(url_for('admin_employees'))
    if password:
        get_db().execute(
            'UPDATE employees SET name=?, department=?, password_hash=? WHERE id=?',
            (name, department, generate_password_hash(password), emp_id),
        )
    else:
        get_db().execute(
            'UPDATE employees SET name=?, department=? WHERE id=?',
            (name, department, emp_id),
        )
    flash('已更新', 'success')
    return redirect(url_for('admin_employees'))


@app.route('/admin/employees/<int:emp_id>/toggle', methods=['POST'])
@admin_required
def admin_employee_toggle(emp_id):
    get_db().execute(
        'UPDATE employees SET is_active = 1 - is_active WHERE id = ?', (emp_id,)
    )
    return redirect(url_for('admin_employees'))


# =============================================================================
# 管理員：獎品管理
# =============================================================================

@app.route('/admin/prizes')
@admin_required
def admin_prizes():
    prizes = get_db().execute(
        '''
        SELECT p.*,
               (SELECT COUNT(*) FROM prize_units pu WHERE pu.prize_id = p.id) AS total,
               (SELECT COUNT(*) FROM prize_units pu
                 WHERE pu.prize_id = p.id AND pu.status = 'AVAILABLE') AS remaining
        FROM prizes p
        ORDER BY p.id
        '''
    ).fetchall()
    return render_template('admin/prizes.html', prizes=prizes)


@app.route('/admin/prizes/new', methods=['POST'])
@admin_required
def admin_prize_new():
    name = request.form.get('name', '').strip()
    tier = request.form.get('tier', '').strip() or '普獎'
    try:
        quantity = max(1, int(request.form.get('quantity', '1')))
    except ValueError:
        quantity = 1
    if not name:
        flash('獎項名稱必填', 'error')
        return redirect(url_for('admin_prizes'))
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        'INSERT INTO prizes(name, tier, created_at) VALUES (?, ?, ?)',
        (name, tier, now_str()),
    )
    prize_id = cur.lastrowid
    units = [(prize_id, f"{name}-{i:03d}") for i in range(1, quantity + 1)]
    try:
        cur.executemany(
            'INSERT INTO prize_units(prize_id, unit_code) VALUES (?, ?)', units
        )
    except sqlite3.IntegrityError:
        # unit_code 撞名，補上 hex 後綴
        cur.executemany(
            'INSERT INTO prize_units(prize_id, unit_code) VALUES (?, ?)',
            [(prize_id, f"{name}-{i:03d}-{secrets.token_hex(3)}") for i in range(1, quantity + 1)],
        )
    flash(f'已新增獎品：{name} x {quantity}', 'success')
    return redirect(url_for('admin_prizes'))


@app.route('/admin/prizes/<int:prize_id>/edit', methods=['POST'])
@admin_required
def admin_prize_edit(prize_id):
    name = request.form.get('name', '').strip()
    tier = request.form.get('tier', '').strip() or '普獎'
    if not name:
        flash('獎項名稱必填', 'error')
        return redirect(url_for('admin_prizes'))
    get_db().execute(
        'UPDATE prizes SET name=?, tier=? WHERE id=?',
        (name, tier, prize_id),
    )
    flash('已更新', 'success')
    return redirect(url_for('admin_prizes'))


@app.route('/admin/prizes/<int:prize_id>/add_units', methods=['POST'])
@admin_required
def admin_prize_add_units(prize_id):
    try:
        qty = max(1, int(request.form.get('quantity', '1')))
    except ValueError:
        qty = 1
    conn = get_db()
    prize = conn.execute('SELECT name FROM prizes WHERE id = ?', (prize_id,)).fetchone()
    if not prize:
        flash('找不到此獎項', 'error')
        return redirect(url_for('admin_prizes'))
    existing = conn.execute(
        'SELECT COUNT(*) FROM prize_units WHERE prize_id = ?', (prize_id,)
    ).fetchone()[0]
    units = [
        (prize_id, f"{prize['name']}-{i:03d}-{secrets.token_hex(2)}")
        for i in range(existing + 1, existing + qty + 1)
    ]
    conn.executemany(
        'INSERT INTO prize_units(prize_id, unit_code) VALUES (?, ?)', units
    )
    flash(f'已加開 {qty} 份', 'success')
    return redirect(url_for('admin_prizes'))


@app.route('/admin/prizes/<int:prize_id>/toggle', methods=['POST'])
@admin_required
def admin_prize_toggle(prize_id):
    get_db().execute(
        'UPDATE prizes SET is_active = 1 - is_active WHERE id = ?', (prize_id,)
    )
    return redirect(url_for('admin_prizes'))


@app.route('/admin/prizes/<int:prize_id>/delete', methods=['POST'])
@admin_required
def admin_prize_delete(prize_id):
    conn = get_db()
    assigned = conn.execute(
        "SELECT COUNT(*) FROM prize_units WHERE prize_id = ? AND status = 'ASSIGNED'",
        (prize_id,),
    ).fetchone()[0]
    if assigned:
        flash('此獎項已有中獎紀錄，無法刪除。可改為停用。', 'error')
        return redirect(url_for('admin_prizes'))
    conn.execute('DELETE FROM prizes WHERE id = ?', (prize_id,))
    flash('已刪除', 'success')
    return redirect(url_for('admin_prizes'))


# =============================================================================
# 管理員：系統設定
# =============================================================================

@app.route('/admin/settings', methods=['GET'])
@admin_required
def admin_settings():
    return render_template('admin/settings.html')


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
    if confirm != 'RESET':
        flash('請於欄位輸入 RESET 確認', 'error')
        return redirect(url_for('admin_settings'))
    conn = get_db()
    conn.execute('BEGIN IMMEDIATE')
    try:
        conn.execute('DELETE FROM draws')
        conn.execute(
            "UPDATE prize_units SET status='AVAILABLE', assigned_employee_id=NULL, assigned_at=NULL"
        )
        conn.execute('UPDATE employees SET has_drawn=0, drawn_at=NULL')
        conn.commit()
        flash('已重置所有抽獎紀錄', 'success')
    except Exception as ex:
        conn.rollback()
        flash(f'重置失敗：{ex}', 'error')
    return redirect(url_for('admin_settings'))


@app.route('/admin/settings/change_password', methods=['POST'])
@admin_required
def admin_change_password():
    old = request.form.get('old_password', '')
    new = request.form.get('new_password', '')
    if len(new) < 6:
        flash('新密碼長度至少 6 字元', 'error')
        return redirect(url_for('admin_settings'))
    admin_id = session['admin_id']
    row = get_db().execute(
        'SELECT password_hash FROM admins WHERE id = ?', (admin_id,)
    ).fetchone()
    if not row or not check_password_hash(row['password_hash'], old):
        flash('原密碼錯誤', 'error')
        return redirect(url_for('admin_settings'))
    get_db().execute(
        'UPDATE admins SET password_hash = ? WHERE id = ?',
        (generate_password_hash(new), admin_id),
    )
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


# =============================================================================
# QR Code
# =============================================================================

@app.route('/admin/qrcode')
@admin_required
def admin_qrcode():
    target = request.args.get('url') or request.host_url.rstrip('/') + url_for('login')
    img = qrcode.make(target)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    qr_b64 = b64encode(buf.getvalue()).decode('ascii')
    return render_template('admin/qrcode.html', target_url=target, qr_b64=qr_b64)


# =============================================================================
# 啟動
# =============================================================================

with app.app_context():
    init_db()


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8000)), debug=False)
