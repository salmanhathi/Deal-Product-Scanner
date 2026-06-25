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

# ── PIN is set via environment variable TDO_PIN (default 1234 for local dev) ──
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
# EXTRACTORS  (same as product_health_monitor app.py)
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
# CORE LOOKUP  — tries primary barcode, falls back to secondary if needed
# ─────────────────────────────────────────────────────────────────────────────

def fetch_one_code(code):
    """Fetch and parse a single TDO product page. Returns dict with found=True/False."""
    code = str(code).strip()
    url = f"https://www.thedealoutlet.com/ae-en/{code}.html"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15,
                         verify=False, allow_redirects=True)
        if r.status_code != 200:
            return None  # signal: not found / error

        soup = BeautifulSoup(r.text, 'html.parser')

        # Detect soft-404 (redirected to homepage or search)
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


def lookup_product(primary_code, fallback_code=None):
    """Try primary barcode; if it fails and fallback exists, try that."""
    primary_code = str(primary_code).strip() if primary_code else ''
    fallback_code = str(fallback_code).strip() if fallback_code else ''

    # Clean up empty / 'nan' values from Excel
    if primary_code.lower() in ('', 'nan', 'none', '-'):
        primary_code = ''
    if fallback_code.lower() in ('', 'nan', 'none', '-'):
        fallback_code = ''

    result = None

    if primary_code:
        result = fetch_one_code(primary_code)

    if result is None and fallback_code and fallback_code != primary_code:
        result = fetch_one_code(fallback_code)
        if result:
            result['used_fallback'] = True
            result['primary_code'] = primary_code

    if result is None:
        code_used = primary_code or fallback_code
        return {
            "found":        False,
            "code":         primary_code,
            "fallbackCode": fallback_code,
            "error":        "Product not found on website",
            "productUrl":   f"https://www.thedealoutlet.com/ae-en/{code_used}.html" if code_used else "",
        }

    result.setdefault('used_fallback', False)
    result.setdefault('primary_code', primary_code)
    result['fallbackCode'] = fallback_code
    return result


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
    # Each item: {primary, fallback}
    items = data.get('items', [])
    if not items:
        return jsonify({"error": "No codes provided"}), 400
    if len(items) > 50:
        return jsonify({"error": "Max 50 items at once"}), 400

    def worker(item):
        return lookup_product(item.get('primary', ''), item.get('fallback', ''))

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(worker, items))

    return jsonify({"results": results})


@app.route('/parse-excel', methods=['POST'])
@login_required
def parse_excel():
    """Accept uploaded .xlsx and return list of {primary, fallback} pairs."""
    if 'file' not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    f = request.files['file']
    if not f.filename.endswith(('.xlsx', '.xls', '.csv')):
        return jsonify({"error": "Please upload an .xlsx, .xls or .csv file"}), 400

    try:
        import openpyxl, io
        wb = openpyxl.load_workbook(io.BytesIO(f.read()), data_only=True)
        ws = wb.active
        items = []
        for row in ws.iter_rows(min_row=2, values_only=True):  # skip header row
            primary  = str(row[0]).strip() if row[0] is not None else ''
            fallback = str(row[1]).strip() if len(row) > 1 and row[1] is not None else ''
            # Skip empty rows and 'nan'
            if primary.lower() in ('', 'nan', 'none') and fallback.lower() in ('', 'nan', 'none'):
                continue
            items.append({"primary": primary, "fallback": fallback})
        return jsonify({"items": items, "count": len(items)})
    except Exception as e:
        return jsonify({"error": f"Failed to read file: {str(e)}"}), 400


if __name__ == '__main__':
    app.run(debug=True, port=5055)
