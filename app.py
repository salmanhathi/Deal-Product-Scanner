import os, re, json, csv, io
from functools import wraps
from concurrent.futures import ThreadPoolExecutor

import requests
from bs4 import BeautifulSoup
from flask import Flask, render_template, request, jsonify, session
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)
app.secret_key  = os.environ.get('SECRET_KEY', 'tdo-secret-key-change-me')
ACCESS_PIN      = os.environ.get('TDO_PIN', '1234')

# ── barcode map persisted to disk so it survives restarts ──────────────────
MAP_FILE = os.path.join(os.path.dirname(__file__), 'barcode_map.json')

def load_map() -> dict:
    try:
        with open(MAP_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def save_map(m: dict):
    with open(MAP_FILE, 'w') as f:
        json.dump(m, f)

barcode_map: dict = load_map()   # { any_barcode: primary_barcode }

# ──────────────────────────────────────────────────────────────────────────────
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

PLACEHOLDER = ['noimagelarge','noimage','no_image','/dw58870029/','blank.gif','1x1','pixel']

def clean(v) -> str:
    """Normalise a cell value to a plain string, strip whitespace. Returns '' for empties."""
    if v is None:
        return ''
    s = str(v).strip()
    return '' if s.lower() in ('nan','none','-','null','') else s

# ── extractors (identical to product_health_monitor) ──────────────────────────

def extract_prices(soup):
    r = {"sale_price": None, "original_price": None}

    el = soup.select_one('h2.sales span.value, .sales span.value')
    if el:
        r["sale_price"] = el.get('content') or re.sub(r'[^\d.]', '', el.get_text())

    el = soup.select_one('span.strike-through.list span.value, .strike-through .value')
    if el:
        r["original_price"] = el.get('content') or re.sub(r'[^\d.]', '', el.get_text())

    if r["sale_price"] and r["original_price"]:
        return r

    if not r["sale_price"]:
        for sel in ['[itemprop="price"]','.price-sales','.special-price .price',
                    'meta[property="product:price:amount"]']:
            el = soup.select_one(sel)
            if el:
                v = el.get('content') or re.sub(r'[^\d.]','',el.get_text())
                if v: r["sale_price"] = v; break

    if not r["sale_price"]:
        t = soup.title.string if soup.title else ''
        m = re.search(r'for AED\s*([\d,.]+)', t or '')
        if m: r["sale_price"] = m.group(1).replace(',','')

    if not r["original_price"]:
        for sel in ['.price-was','.regular-price .price','.old-price .price','.price__compare']:
            el = soup.select_one(sel)
            if el:
                v = re.sub(r'[^\d.]','',el.get_text())
                if v: r["original_price"] = v; break

    if not r["sale_price"]:
        for s in soup.find_all('script', type='application/ld+json'):
            try:
                d = json.loads(s.string or '')
                offers = d.get('offers',{}) if isinstance(d,dict) else {}
                p = offers.get('price') or offers.get('lowPrice')
                if p: r["sale_price"] = str(p); break
            except Exception:
                pass
    return r


def extract_images(soup, code):
    seen, imgs = set(), []

    def add(url):
        if not url or url.startswith('data:') or '.svg' in url.lower(): return
        if any(p in url.lower() for p in PLACEHOLDER): return
        k = url.split('?')[0]
        if k in seen: return
        seen.add(k); imgs.append(url)

    def src(tag):
        return (tag.get('src') or tag.get('data-src') or tag.get('data-zoom-image')
                or tag.get('data-lazy') or tag.get('data-original') or tag.get('content') or '')

    for tag in soup.select('.product-images-desktop img, .js-img-parent-div img'): add(src(tag))
    for sel in ['.primary-images img','.pdp-images img','.image-container img',
                '[data-image-role="product"]','.product-gallery img']:
        for tag in soup.select(sel): add(src(tag))

    og = soup.find('meta', property='og:image')
    og_url = og['content'] if og and og.get('content') else None
    if og_url and not any(p in og_url.lower() for p in PLACEHOLDER):
        add(og_url)
        m = re.search(r'/default/([a-z0-9]+)/images/', og_url)
        if m:
            base = (f"https://www.thedealoutlet.com/dw/image/v2/BGBX_PRD/on/demandware.static"
                    f"/-/Sites-thedealoutlet-master-catalog/default/{m.group(1)}/images/")
            for n in [1,2,3,4]: add(f"{base}{code}_{n}.jpg?sw=800&sh=1200")

    for el in soup.select('[itemprop="image"]'): add(src(el))
    for sel in ['.carousel img','.slick-slide img','.swiper-slide img']:
        for tag in soup.select(sel): add(src(tag))
    return imgs


def extract_brand(soup):
    el = soup.select_one('[itemprop="brand"] [itemprop="name"],[itemprop="brand"]')
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
            if wf[i:i+len(wo)] == wo:
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
    m = soup.find('meta', attrs={'name':'description'})
    if m and m.get('content'): return m['content'].strip()
    og = soup.find('meta', property='og:description')
    if og and og.get('content'): return og['content'].strip()
    for sel in ['[itemprop="description"]','.product-description',
                '.pdp-description','.product__description','.short-description']:
        el = soup.select_one(sel)
        if el:
            t = el.get_text(strip=True)
            if t: return t
    return None

# ── core TDO fetch ─────────────────────────────────────────────────────────────

def fetch_from_tdo(code: str) -> dict | None:
    """Fetch https://www.thedealoutlet.com/ae-en/{code}.html
    Returns parsed product dict, or None if not found / error."""
    url = f"https://www.thedealoutlet.com/ae-en/{code}.html"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15, verify=False, allow_redirects=True)
        if r.status_code != 200:
            return None
        soup = BeautifulSoup(r.text, 'html.parser')
        canon = soup.find('link', rel='canonical')
        prod_url = canon['href'] if canon and canon.get('href') else r.url
        # Soft-404 detection
        if ('search' in prod_url or
                prod_url.rstrip('/') in ('https://www.thedealoutlet.com/ae-en',
                                         'https://www.thedealoutlet.com')):
            return None

        prices = extract_prices(soup)
        sale   = float(prices['sale_price'])    if prices['sale_price']    else None
        orig   = float(prices['original_price']) if prices['original_price'] else None
        disc   = round((orig-sale)/orig*100)     if sale and orig and orig>sale else None

        return {
            "found":         True,
            "code":          code,
            "name":          extract_title(soup),
            "brand":         extract_brand(soup),
            "salePrice":     sale,
            "originalPrice": orig,
            "discount":      disc,
            "currency":      "AED",
            "description":   extract_description(soup),
            "images":        extract_images(soup, code),
            "productUrl":    prod_url,
        }
    except Exception as e:
        app.logger.warning(f"fetch_from_tdo({code}) error: {e}")
        return None

# ── smart lookup ───────────────────────────────────────────────────────────────

def lookup_product(scanned: str) -> dict:
    """
    1. Try scanned code directly on TDO website.
    2. If not found → check barcode_map:
         barcode_map[scanned] = primary_code (from Excel col A)
       Try that primary code on TDO.
    3. Return not-found with helpful message.
    """
    scanned = str(scanned).strip()
    app.logger.info(f"Looking up: '{scanned}'  |  map size: {len(barcode_map)}")

    # Step 1 — direct website lookup
    result = fetch_from_tdo(scanned)
    if result:
        result['scanned_code']  = scanned
        result['used_fallback'] = False
        return result

    # Step 2 — check barcode map for a primary code
    primary = barcode_map.get(scanned)
    app.logger.info(f"Not found directly. Map lookup '{scanned}' → '{primary}'")

    if primary and primary != scanned:
        result = fetch_from_tdo(primary)
        if result:
            result['scanned_code']  = scanned
            result['used_fallback'] = True
            result['primary_code']  = primary
            return result

    # Step 3 — nothing worked
    in_map = scanned in barcode_map
    return {
        "found":         False,
        "code":          scanned,
        "scanned_code":  scanned,
        "used_fallback": False,
        "error": (
            f"Physical barcode {scanned} is in your map (primary: {barcode_map[scanned]}) "
            f"but that product was also not found on the website."
            if in_map else
            f"Barcode {scanned} not found on website and not in your uploaded barcode map."
        ),
        "productUrl": f"https://www.thedealoutlet.com/ae-en/{scanned}.html",
    }

# ── auth ───────────────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('authenticated'):
            return jsonify({'error': 'Not authenticated', 'auth': False}), 401
        return f(*args, **kwargs)
    return decorated

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

# ── routes ─────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/lookup', methods=['POST'])
@login_required
def lookup():
    data  = request.get_json(force=True)
    codes = [str(c).strip() for c in data.get('codes', []) if str(c).strip()]
    if not codes:
        return jsonify({"error": "No codes provided"}), 400
    if len(codes) > 50:
        return jsonify({"error": "Max 50 codes per request"}), 400
    with ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(lookup_product, codes))
    return jsonify({"results": results})


@app.route('/upload-excel', methods=['POST'])
@login_required
def upload_excel():
    """
    Load barcode map from Excel / CSV.
    Column A = Primary barcode  (appears in TDO website URL)
    Column B = Physical barcode (printed on product box / label)
    Row 1    = header — always skipped.
    File name can be anything.
    Map is saved to disk so it survives server restarts.
    """
    global barcode_map

    if 'file' not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    f = request.files['file']
    if not f.filename.lower().endswith(('.xlsx', '.xls', '.csv')):
        return jsonify({"error": "Upload a .xlsx, .xls or .csv file"}), 400

    try:
        new_map: dict[str, str] = {}

        def add_row(primary_raw, physical_raw):
            primary  = clean(primary_raw)
            physical = clean(physical_raw)
            if not primary:
                return
            # primary maps to itself (so direct scans of website barcode always work)
            new_map[primary] = primary
            # physical barcode maps to primary
            if physical and physical != primary:
                new_map[physical] = primary

        fname = f.filename.lower()
        if fname.endswith('.csv'):
            content = f.read().decode('utf-8-sig')
            reader  = csv.reader(io.StringIO(content))
            for i, row in enumerate(reader):
                if i == 0: continue   # skip header
                add_row(row[0] if len(row) > 0 else '', row[1] if len(row) > 1 else '')
        else:
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(f.read()), data_only=True)
            ws = wb.active
            for i, row in enumerate(ws.iter_rows(min_row=1, values_only=True)):
                if i == 0: continue   # skip header
                add_row(row[0] if len(row) > 0 else None,
                        row[1] if len(row) > 1 else None)

        barcode_map = new_map
        save_map(barcode_map)   # persist to disk

        primaries = sorted(set(new_map.values()))
        pairs     = sum(1 for k, v in new_map.items() if k != v)

        app.logger.info(f"Barcode map loaded: {len(primaries)} products, {pairs} pairs")
        app.logger.info(f"Sample: {dict(list(new_map.items())[:8])}")

        return jsonify({
            "ok":            True,
            "products":      len(primaries),
            "pairs":         pairs,
            "primary_codes": primaries,
            "message":       f"{len(primaries)} products · {pairs} physical↔primary pairs",
        })

    except Exception as e:
        app.logger.exception("Excel parse error")
        return jsonify({"error": f"Failed to read file: {e}"}), 400


@app.route('/map-status')
@login_required
def map_status():
    primaries = len(set(barcode_map.values()))
    pairs     = sum(1 for k, v in barcode_map.items() if k != v)
    return jsonify({"loaded": primaries > 0, "products": primaries, "pairs": pairs})


@app.route('/map-debug', methods=['POST'])
@login_required
def map_debug():
    """Dev helper — check what the map returns for a barcode."""
    d       = request.get_json(force=True)
    scanned = clean(d.get('code', ''))
    return jsonify({
        "scanned":    scanned,
        "in_map":     scanned in barcode_map,
        "primary":    barcode_map.get(scanned),
        "map_size":   len(barcode_map),
        "sample":     dict(list(barcode_map.items())[:10]),
    })


if __name__ == '__main__':
    app.run(debug=True, port=5055)
