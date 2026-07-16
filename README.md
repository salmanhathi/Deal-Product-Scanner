# The Deal Product Details Finder

Scan or enter a product barcode → get full details from thedealoutlet.com instantly.
PIN-protected so only your team can access it.

---

## Run Locally

```bash
pip install -r requirements.txt
TDO_PIN=1234 SECRET_KEY=any-secret python app.py
```
Open: http://localhost:5055

---

## Deploy to Render (free, team access)

1. Push this folder to a **GitHub repo** (can be private)
2. Go to https://render.com → New → Web Service
3. Connect your GitHub repo
4. Settings:
   - **Build command:** `pip install -r requirements.txt`
   - **Start command:** `gunicorn app:app`
   - **Instance type:** Free
5. Add **Environment Variables** in Render dashboard:
   - `TDO_PIN` = your chosen PIN (e.g. `7391`)
   - `SECRET_KEY` = any long random string (e.g. `tdo-xk29sla-secret-2024`)
6. Click **Deploy** → Render gives you a public URL like `https://tdo-finder.onrender.com`

Share that URL + PIN with your team.

---

## Control Access

- **Change PIN:** Update `TDO_PIN` in Render → Environment Variables → Redeploy
- **Turn off:** Render dashboard → Suspend Service (one click, instant)
- **Turn back on:** Resume Service (one click)
- **Revoke access:** Change the PIN — existing sessions expire automatically

---

## Excel Format

Upload an Excel file with:
| Column A (Primary Barcode) | Column B (Fallback Barcode) |
|---|---|
| 020416208650 | 104227337532 |
| ABC-456 | |

Row 1 = header (skipped). Column B is optional.
If primary barcode is not found, the app automatically tries the fallback.

---

## Features

- 📷 **Camera scan** — scan barcodes with phone camera
- 🔢 **Manual type** — enter primary + optional fallback barcode
- 📦 **Multi lookup** — paste many codes or upload Excel
- ⚡ **Fallback barcodes** — auto-tries second barcode if first fails
- 🔒 **PIN protection** — team access, you control the PIN
- 🖼️ **Image carousel** — swipe through all product images
- 💾 **Recent scans** — saved in browser for quick re-lookup
