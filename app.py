import os, re, json, csv, io, sqlite3
from functools import wraps
from concurrent.futures import ThreadPoolExecutor

import requests
from bs4 import BeautifulSoup
from flask import Flask, render_template, request, jsonify, session, g
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'tdo-secret-change-me')
ACCESS_PIN     = os.environ.get('TDO_PIN', '1234')

# ── DATABASE ───────────────────────────────────────────────────────────────────
DB_PATH = os.path.join(os.path.dirname(__file__), 'barcodes.db')

def db_connect():
    """Always returns a fresh direct SQLite connection. Caller must close it."""
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    return con

def get_db():
    """Per-request connection stored on Flask g."""
    if not hasattr(g, '_db'):
        g._db = db_connect()
    return g._db

@app.teardown_appcontext
def close_db(exc):
    db = getattr(g, '_db', None)
    if db:
        db.close()

def init_db():
    con = db_connect()
    con.execute("""
        CREATE TABLE IF NOT EXISTS barcodes (
            physical_code TEXT NOT NULL PRIMARY KEY,
            primary_code  TEXT NOT NULL,
            updated_at    TEXT DEFAULT (datetime('now'))
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_primary ON barcodes(primary_code)")
    con.commit()
    con.close()

init_db()

# ── DB HELPERS ─────────────────────────────────────────────────────────────────
def clean(v) -> str:
    if v is None: return ''
    s = str(v).strip()
    return '' if s.lower() in ('nan', 'none', '-', 'null', '') else s

def lookup_primary(physical: str):
    """Thread-safe — uses its own direct connection, not Flask g."""
    con = db_connect()
    row = con.execute(
        "SELECT primary_code FROM barcodes WHERE physical_code=? LIMIT 1",
        (physical.strip(),)
    ).fetchone()
    con.close()
    return row['primary_code'] if row else None

def upsert_barcode(physical: str, primary: str):
    """Thread-safe — uses its own direct connection."""
    con = db_connect()
    con.execute("""
        INSERT INTO barcodes (physical_code, primary_code, updated_at)
        VALUES (?, ?, datetime('now'))
        ON CONFLICT(physical_code) DO UPDATE SET
            primary_code=excluded.primary_code,
            updated_at=datetime('now')
    """, (physical.strip(), primary.strip()))
    con.commit()
    con.close()

def get_stats(con=None):
    """Return {total, mapped} — uses given con or opens its own."""
    close_after = False
    if con is None:
        con = db_connect()
        close_after = True
    row = con.execute("""
        SELECT COUNT(*) as total,
               SUM(CASE WHEN physical_code != primary_code THEN 1 ELSE 0 END) as mapped
        FROM barcodes
    """).fetchone()
    if close_after:
        con.close()
    return {"total": row["total"] or 0, "mapped": row["mapped"] or 0}

# ── TDO SCRAPERS (same as product_health_monitor) ─────────────────────────────
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
PLACEHOLDER = ['noimagelarge', 'noimage', 'no_image', '/dw58870029/', 'blank.gif', '1x1', 'pixel']

def extract_prices(soup):
    r = {"sale_price": None, "original_price": None}
    el = soup.select_one('h2.sales span.value, .sales span.value')
    if el: r["sale_price"] = el.get('content') or re.sub(r'[^\d.]', '', el.get_text())
    el = soup.select_one('span.strike-through.list span.value, .strike-through .value')
    if el: r["original_price"] = el.get('content') or re.sub(r'[^\d.]', '', el.get_text())
    if r["sale_price"] and r["original_price"]: return r
    if not r["sale_price"]:
        for sel in ['[itemprop="price"]', '.price-sales', '.special-price .price',
                    'meta[property="product:price:amount"]']:
            el = soup.select_one(sel)
            if el:
                v = el.get('content') or re.sub(r'[^\d.]', '', el.get_text())
                if v: r["sale_price"] = v; break
    if not r["sale_price"]:
        t = soup.title.string if soup.title else ''
        m = re.search(r'for AED\s*([\d,.]+)', t or '')
        if m: r["sale_price"] = m.group(1).replace(',', '')
    if not r["original_price"]:
        for sel in ['.price-was', '.regular-price .price', '.old-price .price', '.price__compare']:
            el = soup.select_one(sel)
            if el:
                v = re.sub(r'[^\d.]', '', el.get_text())
                if v: r["original_price"] = v; break
    if not r["sale_price"]:
        for s in soup.find_all('script', type='application/ld+json'):
            try:
                d = json.loads(s.string or '')
                offers = d.get('offers', {}) if isinstance(d, dict) else {}
                p = offers.get('price') or offers.get('lowPrice')
                if p: r["sale_price"] = str(p); break
            except Exception:
                pass
    return r

def extract_images(soup, code):
    seen, imgs = [], []
    def add(url):
        if not url or url.startswith('data:') or '.svg' in url.lower(): return
        if any(p in url.lower() for p in PLACEHOLDER): return
        k = url.split('?')[0]
        if k in seen: return
        seen.append(k); imgs.append(url)
    def src(tag):
        return (tag.get('src') or tag.get('data-src') or tag.get('data-zoom-image')
                or tag.get('data-lazy') or tag.get('data-original') or tag.get('content') or '')
    for tag in soup.select('.product-images-desktop img, .js-img-parent-div img'): add(src(tag))
    for sel in ['.primary-images img', '.pdp-images img', '.image-container img',
                '[data-image-role="product"]', '.product-gallery img']:
        for tag in soup.select(sel): add(src(tag))
    og = soup.find('meta', property='og:image')
    og_url = og['content'] if og and og.get('content') else None
    if og_url and not any(p in og_url.lower() for p in PLACEHOLDER):
        add(og_url)
        m = re.search(r'/default/([a-z0-9]+)/images/', og_url)
        if m:
            base = (f"https://www.thedealoutlet.com/dw/image/v2/BGBX_PRD/on/demandware.static"
                    f"/-/Sites-thedealoutlet-master-catalog/default/{m.group(1)}/images/")
            for n in [1, 2, 3, 4]: add(f"{base}{code}_{n}.jpg?sw=800&sh=1200")
    for el in soup.select('[itemprop="image"]'): add(src(el))
    for sel in ['.carousel img', '.slick-slide img', '.swiper-slide img']:
        for tag in soup.select(sel): add(src(tag))
    return imgs

def extract_brand(soup):
    el = soup.select_one('[itemprop="brand"] [itemprop="name"], [itemprop="brand"]')
    if el:
        b = el.get_text(strip=True)
        if b: return b
    t = soup.title.string.strip() if soup.title else ''
    og = soup.find('meta', property='og:title')
    og_t = og['content'].strip() if og and og.get('content') else ''
    m = re.match(r'^Buy (.+?) for AED', t)
    if m and og_t:
        full = m.group(1).strip()
        wf, wo = full.split(), og_t.split()
        for i in range(len(wf)):
            if wf[i:i + len(wo)] == wo:
                b = ' '.join(wf[:i]).strip()
                if b: return b
    return None

def extract_title(soup):
    og = soup.find('meta', property='og:title')
    if og and og.get('content'): return og['content'].strip()
    h1 = soup.find('h1')
    if h1: return h1.get_text(strip=True)
    return soup.title.string.strip() if soup.title else None

def extract_description(soup):
    m = soup.find('meta', attrs={'name': 'description'})
    if m and m.get('content'): return m['content'].strip()
    og = soup.find('meta', property='og:description')
    if og and og.get('content'): return og['content'].strip()
    for sel in ['[itemprop="description"]', '.product-description',
                '.pdp-description', '.product__description', '.short-description']:
        el = soup.select_one(sel)
        if el:
            t = el.get_text(strip=True)
            if t: return t
    return None

def fetch_from_tdo(code: str):
    url = f"https://www.thedealoutlet.com/ae-en/{code}.html"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15, verify=False, allow_redirects=True)
        if r.status_code != 200: return None
        soup = BeautifulSoup(r.text, 'html.parser')
        canon = soup.find('link', rel='canonical')
        prod_url = canon['href'] if canon and canon.get('href') else r.url
        if ('search' in prod_url or prod_url.rstrip('/') in (
                'https://www.thedealoutlet.com/ae-en',
                'https://www.thedealoutlet.com')): return None
        prices = extract_prices(soup)
        sale = float(prices['sale_price']) if prices['sale_price'] else None
        orig = float(prices['original_price']) if prices['original_price'] else None
        disc = round((orig - sale) / orig * 100) if sale and orig and orig > sale else None
        return {
            "found": True, "code": code,
            "name": extract_title(soup), "brand": extract_brand(soup),
            "salePrice": sale, "originalPrice": orig, "discount": disc, "currency": "AED",
            "description": extract_description(soup),
            "images": extract_images(soup, code), "productUrl": prod_url,
        }
    except Exception as e:
        app.logger.warning(f"fetch_from_tdo({code}): {e}")
        return None

# ── SMART LOOKUP ───────────────────────────────────────────────────────────────
def lookup_product(physical: str) -> dict:
    physical = physical.strip()

    # Step 1 — check DB for primary barcode
    primary = lookup_primary(physical)
    if primary:
        result = fetch_from_tdo(primary)
        if result:
            result['physical_code'] = physical
            result['primary_code']  = primary
            result['used_db']       = (physical != primary)
            return result
        return {
            "found": False, "code": physical, "physical_code": physical,
            "primary_code": primary, "used_db": True,
            "error": f"Primary barcode {primary} found in database but not on TDO website.",
            "productUrl": f"https://www.thedealoutlet.com/ae-en/{primary}.html",
        }

    # Step 2 — try the scanned code directly (maybe it IS the primary)
    result = fetch_from_tdo(physical)
    if result:
        result['physical_code'] = physical
        result['primary_code']  = physical
        result['used_db']       = False
        return result

    # Step 3 — not found anywhere, ask user to add mapping
    return {
        "found": False, "code": physical, "physical_code": physical,
        "primary_code": None, "used_db": False, "needs_primary": True,
        "error": "Barcode not in database and not found directly on TDO website.",
        "productUrl": f"https://www.thedealoutlet.com/ae-en/{physical}.html",
    }

# ── AUTH ───────────────────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def dec(*a, **kw):
        if not session.get('authenticated'):
            return jsonify({'error': 'Not authenticated', 'auth': False}), 401
        return f(*a, **kw)
    return dec

@app.route('/auth/login', methods=['POST'])
def auth_login():
    d = request.get_json(force=True)
    if d.get('pin') == ACCESS_PIN:
        session['authenticated'] = True
        session.permanent = True
        return jsonify({'ok': True})
    return jsonify({'ok': False, 'error': 'Incorrect PIN'}), 401

@app.route('/auth/logout', methods=['POST'])
def auth_logout():
    session.clear()
    return jsonify({'ok': True})

@app.route('/auth/check')
def auth_check():
    return jsonify({'authenticated': bool(session.get('authenticated'))})

# ── MAIN ROUTES ────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/admin')
def admin():
    if not session.get('authenticated'):
        return render_template('index.html')
    return render_template('admin.html')

@app.route('/lookup', methods=['POST'])
@login_required
def lookup():
    data  = request.get_json(force=True)
    codes = [str(c).strip() for c in data.get('codes', []) if str(c).strip()]
    if not codes:          return jsonify({"error": "No codes provided"}), 400
    if len(codes) > 50:    return jsonify({"error": "Max 50 codes per request"}), 400
    with ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(lookup_product, codes))
    return jsonify({"results": results})

@app.route('/add-mapping', methods=['POST'])
@login_required
def add_mapping():
    d        = request.get_json(force=True)
    physical = clean(d.get('physical', ''))
    primary  = clean(d.get('primary', ''))
    if not physical or not primary:
        return jsonify({"error": "Both barcodes required"}), 400
    upsert_barcode(physical, primary)
    result = fetch_from_tdo(primary)
    if result:
        result['physical_code'] = physical
        result['primary_code']  = primary
        result['used_db']       = True
        result['just_added']    = True
    else:
        result = {
            "found": False, "code": primary, "physical_code": physical,
            "primary_code": primary, "used_db": True,
            "error": "Mapping saved to database but product not found on TDO website.",
            "productUrl": f"https://www.thedealoutlet.com/ae-en/{primary}.html",
        }
    return jsonify({"ok": True, "result": result})

@app.route('/import-excel', methods=['POST'])
@login_required
def import_excel():
    if 'file' not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    f     = request.files['file']
    fname = f.filename.lower()
    if not fname.endswith(('.xlsx', '.xls', '.csv')):
        return jsonify({"error": "Please upload a .xlsx, .xls or .csv file"}), 400
    try:
        rows       = []
        file_bytes = f.read()

        if fname.endswith('.csv'):
            content = file_bytes.decode('utf-8-sig')
            for i, row in enumerate(csv.reader(io.StringIO(content))):
                if i == 0: continue   # skip header
                primary  = clean(row[0] if len(row) > 0 else '')
                physical = clean(row[1] if len(row) > 1 else '')
                if primary:
                    rows.append((physical or primary, primary))
        else:
            import openpyxl
            # read_only + data_only = fast streaming, low memory, handles 100k+ rows
            wb = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
            ws = wb.active
            first = True
            for row in ws.iter_rows(values_only=True):
                if first: first = False; continue   # skip header
                primary  = clean(row[0] if len(row) > 0 else None)
                physical = clean(row[1] if len(row) > 1 else None)
                if primary:
                    rows.append((physical or primary, primary))
            wb.close()

        if not rows:
            return jsonify({"error": "No data found. Check Column A has barcodes and row 1 is a header."}), 400

        # Direct connection — keeps import completely separate from request lifecycle
        con = db_connect()
        con.executemany("""
            INSERT INTO barcodes (physical_code, primary_code, updated_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(physical_code) DO UPDATE SET
                primary_code = excluded.primary_code,
                updated_at   = datetime('now')
        """, rows)
        con.commit()
        stats = get_stats(con)
        con.close()

        return jsonify({
            "ok":       True,
            "imported": len(rows),
            "db_total": stats['total'],
            "db_mapped": stats['mapped'],
            "message":  f"Imported {len(rows):,} rows — database now has {stats['total']:,} barcodes.",
        })
    except Exception as e:
        app.logger.exception("import_excel error")
        return jsonify({"error": str(e)}), 400

@app.route('/db-stats')
@login_required
def db_stats_route():
    s = get_stats()
    return jsonify({"total": s['total'], "mapped": s['mapped']})

@app.route('/db-debug', methods=['POST'])
@login_required
def db_debug():
    """Admin test tool — check what DB returns for any barcode."""
    d       = request.get_json(force=True)
    scanned = clean(d.get('code', ''))
    primary = lookup_primary(scanned)
    stats   = get_stats()
    return jsonify({
        "scanned":  scanned,
        "in_map":   primary is not None,
        "primary":  primary,
        "map_size": stats['total'],
    })

# Keep old endpoint name working too (admin.html calls /map-debug)
@app.route('/map-debug', methods=['POST'])
@login_required
def map_debug():
    return db_debug()

@app.route('/db-clear', methods=['POST'])
@login_required
def db_clear():
    db = get_db()
    db.execute('DELETE FROM barcodes')
    db.commit()
    return jsonify({'ok': True})

if __name__ == '__main__':
    app.run(debug=True, port=5055)
