from flask import Flask, render_template, request, jsonify, session
from functools import wraps
import requests
from bs4 import BeautifulSoup
import re, json, os
from concurrent.futures import ThreadPoolExecutor
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'tdo-finder-secret-change-me')

ACCESS_PIN = os.environ.get('TDO_PIN', '1234')

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

PLACEHOLDER_SIGNALS = [
    'noimagelarge', 'noimage', 'no_image',
    '/dw58870029/', 'blank.gif', '1x1', 'pixel',
]

# ─────────────────────────────────────────────────────────────────────────────
# BARCODE MAP  — loaded from uploaded Excel
# Key   = fallback/physical barcode (Column B)
# Value = primary/website barcode   (Column A)
# ─────────────────────────────────────────────────────────────────────────────
barcode_map = {}   # { fallback_code: primary_code }


# ─────────────────────────────────────────────────────────────────────────────
# AUTH
# ─────────────────────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('authenticated'):
            return jsonify({'error': 'Not authenticated', 'auth': False}), 401
        return f(*args, **kwargs)
    return decorated


@app.route('/auth/login', methods=['POST'])
def login():
    data = request.get_json()
    if data.get('pin') == ACCESS_PIN:
        session['authenticated'] = True
        session.permanent = True
        return jsonify({'ok': True})
    return jsonify({'ok': False, 'error': 'Incorrect PIN'}), 401


@app.route('/auth/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'ok': True})


@app.route('/auth/check')
def auth_check():
    return jsonify({'authenticated': bool(session.get('authenticated'))})


# ─────────────────────────────────────────────────────────────────────────────
# EXTRACTORS
# ─────────────────────────────────────────────────────────────────────────────

def extract_prices(soup):
    result = {"sale_price": None, "original_price": None, "saving": None, "has_sale": False}

    sale_el = soup.select_one('h2.sales span.value, .sales span.value')
    if sale_el:
        result["sale_price"] = sale_el.get('content') or re.sub(r'[^\d.]', '', sale_el.get_text())

    orig_el = soup.select_one('span.strike-through.list span.value, .strike-through .value')
    if orig_el:
        result["original_price"] = orig_el.get('content') or re.sub(r'[^\d.]', '', orig_el.get_text())

    saving_el = soup.select_one('.wis_fiyatfark')
    if saving_el:
        result["saving"] = saving_el.get_text(strip=True)

    if result["sale_price"] and result["original_price"]:
        result["has_sale"] = True
        return result

    if not result["sale_price"]:
        for sel in ['[itemprop="price"]', '.price-sales', '.special-price .price',
                    'meta[property="product:price:amount"]']:
            el = soup.select_one(sel)
            if el:
                v = el.get('content') or re.sub(r'[^\d.]', '', el.get_text())
                if v:
                    result["sale_price"] = v
                    break

    if not result["sale_price"]:
        title = soup.title.string if soup.title else ''
        m = re.search(r'for AED\s*([\d,.]+)', title or '')
        if m:
            result["sale_price"] = m.group(1).replace(',', '')

    if not result["original_price"]:
        for sel in ['.price-was', '.regular-price .price', '.old-price .price', '.price__compare']:
            el = soup.select_one(sel)
            if el:
                v = re.sub(r'[^\d.]', '', el.get_text())
                if v:
                    result["original_price"] = v
                    break

    if not result["sale_price"]:
        for script in soup.find_all('script', type='application/ld+json'):
            try:
                data = json.loads(script.string or '')
                if isinstance(data, dict):
                    offers = data.get('offers', {})
                    if isinstance(offers, dict):
                        p = offers.get('price') or offers.get('lowPrice')
                        if p:
                            result["sale_price"] = str(p)
                            break
            except Exception:
                pass

    result["has_sale"] = bool(result["sale_price"] and result["original_price"])
    return result


def extract_images(soup, base_url, code):
    images = []
    seen = set()

    def is_placeholder(src):
        return any(p in src.lower() for p in PLACEHOLDER_SIGNALS)

    def add(url):
        if not url or url.startswith('data:') or '.svg' in url.lower():
            return
        if is_placeholder(url):
            return
        key = url.split('?')[0]
        if key in seen:
            return
        seen.add(key)
        images.append(url)

    def src_of(tag):
        return (tag.get('src') or tag.get('data-src') or tag.get('data-zoom-image')
                or tag.get('data-lazy') or tag.get('data-original') or tag.get('content') or '')

    for tag in soup.select('.product-images-desktop img, .js-img-parent-div img'):
        add(src_of(tag))

    for sel in ['.primary-images img', '.pdp-images img', '.image-container img',
                '[data-image-role="product"]', '.product-gallery img']:
        for tag in soup.select(sel):
            add(src_of(tag))

    og = soup.find('meta', property='og:image')
    og_url = og['content'] if og and og.get('content') else None
    if og_url and not is_placeholder(og_url):
        add(og_url)
        m = re.search(r'/default/([a-z0-9]+)/images/', og_url)
        if m:
            hash_val = m.group(1)
            base = (f"https://www.thedealoutlet.com/dw/image/v2/BGBX_PRD/on/demandware.static"
                    f"/-/Sites-thedealoutlet-master-catalog/default/{hash_val}/images/")
            for n in [1, 2, 3, 4]:
                add(f"{base}{code}_{n}.jpg?sw=800&sh=1200")

    for el in soup.select('[itemprop="image"]'):
        add(src_of(el))
    for sel in ['.carousel img', '.slick-slide img', '.swiper-slide img']:
        for tag in soup.select(sel):
            add(src_of(tag))

    return images


def extract_brand(soup):
    brand_el = soup.select_one('[itemprop="brand"] [itemprop="name"], [itemprop="brand"]')
    if brand_el:
        b = brand_el.get_text(strip=True)
        if b:
            return b
    title_str = soup.title.string.strip() if soup.title else ''
    og = soup.find('meta', property='og:title')
    og_title = og['content'].strip() if og and og.get('content') else ''
    m = re.match(r'^Buy (.+?) for AED', title_str)
    if m and og_title:
        full = m.group(1).strip()
        words_full = full.split()
        words_og = og_title.split()
        for i in range(len(words_full)):
            if words_full[i:i + len(words_og)] == words_og:
                brand = ' '.join(words_full[:i]).strip()
                if brand:
                    return brand
    return None


def extract_title(soup):
    og = soup.find('meta', property='og:title')
    if og and og.get('content'):
        return og['content'].strip()
    h1 = soup.find('h1')
    if h1:
        return h1.get_text(strip=True)
    return soup.title.string.strip() if soup.title else None


def extract_description(soup):
    meta = soup.find('meta', attrs={'name': 'description'})
    if meta and meta.get('content'):
        return meta['content'].strip()
    og = soup.find('meta', property='og:description')
    if og and og.get('content'):
        return og['content'].strip()
    for sel in ['[itemprop="description"]', '.product-description',
                '.pdp-description', '.product__description', '.short-description']:
        el = soup.select_one(sel)
        if el:
            txt = el.get_text(strip=True)
            if txt:
                return txt
    return None


# ─────────────────────────────────────────────────────────────────────────────
# FETCH ONE CODE FROM TDO WEBSITE
# ─────────────────────────────────────────────────────────────────────────────

def fetch_one_code(code):
    """Try fetching a product page by barcode. Returns parsed dict or None if not found."""
    code = str(code).strip()
    url = f"https://www.thedealoutlet.com/ae-en/{code}.html"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15,
                         verify=False, allow_redirects=True)

        if r.status_code != 200:
            return None

        soup = BeautifulSoup(r.text, 'html.parser')

        # Detect soft-404 (redirected to homepage or search page)
        canonical = soup.find('link', rel='canonical')
        product_url = canonical['href'] if canonical and canonical.get('href') else r.url
        if 'search' in product_url or product_url.rstrip('/') == 'https://www.thedealoutlet.com/ae-en':
            return None

        title       = extract_title(soup)
        brand       = extract_brand(soup)
        description = extract_description(soup)
        images      = extract_images(soup, r.url, code)
        prices      = extract_prices(soup)

        sale = float(prices['sale_price']) if prices['sale_price'] else None
        orig = float(prices['original_price']) if prices['original_price'] else None
        discount = round((orig - sale) / orig * 100) if (sale and orig and orig > sale) else None

        return {
            "found":         True,
            "code":          code,
            "name":          title,
            "brand":         brand,
            "salePrice":     sale,
            "originalPrice": orig,
            "discount":      discount,
            "currency":      "AED",
            "description":   description,
            "images":        images,
            "productUrl":    product_url,
        }
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# SMART LOOKUP
# Logic:
#   1. Try the scanned code directly on TDO website
#   2. If not found → check barcode_map: is this a known physical/fallback barcode?
#      If yes → get the primary (website) barcode from the map → try that on TDO
#   3. If still not found → return not found
# ─────────────────────────────────────────────────────────────────────────────

def lookup_product(scanned_code):
    scanned_code = str(scanned_code).strip()

    # Step 1: Try scanned code directly on TDO
    result = fetch_one_code(scanned_code)
    if result:
        result['used_fallback'] = False
        result['scanned_code'] = scanned_code
        return result

    # Step 2: Look up in barcode map — scanned code might be a physical barcode
    primary_code = barcode_map.get(scanned_code)
    if primary_code and primary_code != scanned_code:
        result = fetch_one_code(primary_code)
        if result:
            result['used_fallback'] = True
            result['scanned_code'] = scanned_code   # what the user scanned
            result['code'] = primary_code           # what actually worked
            return result

    # Step 3: Not found anywhere
    return {
        "found":        False,
        "code":         scanned_code,
        "scanned_code": scanned_code,
        "used_fallback": False,
        "error":        "Not found on website and not in barcode map",
        "productUrl":   f"https://www.thedealoutlet.com/ae-en/{scanned_code}.html",
    }


# ─────────────────────────────────────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/lookup', methods=['POST'])
@login_required
def lookup():
    data = request.get_json()
    # Accept a flat list of scanned codes — logic is all server-side now
    codes = data.get('codes', [])
    if not codes:
        return jsonify({"error": "No codes provided"}), 400
    if len(codes) > 50:
        return jsonify({"error": "Max 50 codes at once"}), 400

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(lookup_product, codes))

    return jsonify({"results": results})


@app.route('/upload-excel', methods=['POST'])
@login_required
def upload_excel():
    """
    Upload Excel to load the barcode map.
    Column A = Primary barcode (on TDO website URL)
    Column B = Physical/fallback barcode (on the product itself)
    Row 1 = header (skipped)
    File can have any name.
    """
    global barcode_map
    if 'file' not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    f = request.files['file']
    if not f.filename.lower().endswith(('.xlsx', '.xls', '.csv')):
        return jsonify({"error": "Please upload an .xlsx, .xls or .csv file"}), 400

    try:
        new_map = {}
        filename = f.filename.lower()

        if filename.endswith('.csv'):
            import csv, io
            content = f.read().decode('utf-8-sig')
            reader = csv.reader(io.StringIO(content))
            next(reader, None)  # skip header
            for row in reader:
                primary  = str(row[0]).strip() if len(row) > 0 else ''
                physical = str(row[1]).strip() if len(row) > 1 else ''
                if primary and primary.lower() not in ('nan', 'none'):
                    # Map physical barcode → primary barcode
                    if physical and physical.lower() not in ('nan', 'none', ''):
                        new_map[physical] = primary
                    # Also map primary to itself (direct lookup still works)
                    new_map[primary] = primary
        else:
            import openpyxl, io
            wb = openpyxl.load_workbook(io.BytesIO(f.read()), data_only=True)
            ws = wb.active
            for row in ws.iter_rows(min_row=2, values_only=True):
                primary  = str(row[0]).strip() if row[0] is not None else ''
                physical = str(row[1]).strip() if len(row) > 1 and row[1] is not None else ''
                if primary and primary.lower() not in ('nan', 'none'):
                    if physical and physical.lower() not in ('nan', 'none', ''):
                        new_map[physical] = primary
                    new_map[primary] = primary

        barcode_map = new_map
        return jsonify({
            "ok": True,
            "total_rows": len([v for v in new_map.values()]),
            "mapped_pairs": len([k for k, v in new_map.items() if k != v]),
            "message": f"Loaded {len(new_map)} barcode entries"
        })

    except Exception as e:
        return jsonify({"error": f"Failed to read file: {str(e)}"}), 400


@app.route('/barcode-map-status')
@login_required
def barcode_map_status():
    total = len(set(barcode_map.values()))
    pairs = len([k for k, v in barcode_map.items() if k != v])
    return jsonify({"loaded": total > 0, "products": total, "pairs": pairs})


if __name__ == '__main__':
    app.run(debug=True, port=5055)
