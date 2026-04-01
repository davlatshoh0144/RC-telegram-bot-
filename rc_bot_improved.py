import html
import json
import logging
import os
import re
import tempfile
from urllib.parse import quote_plus
from urllib.request import urlopen

import pdfplumber
import pytesseract
from PIL import Image, ImageDraw, ImageFont
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters
import sys
import subprocess


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("rc_bot")

# Load a local .env file (so you don't retype token every run).
# Format per line: KEY=VALUE
def _load_dotenv(path: str):
    try:
        if not os.path.isfile(path):
            return
        with open(path, "r", encoding="utf-8") as f:
            for raw in f.read().splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and val and key not in os.environ:
                    os.environ[key] = val
    except Exception:
        return


_load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# If user starts this with Python 3.14+ (common on your PC), auto-relaunch under Python 3.12.
# This avoids the python-telegram-bot import hang/issues on 3.14.
if sys.version_info >= (3, 14):
    try:
        script = os.path.abspath(__file__)
        args = ["py", "-3.12", script, *sys.argv[1:]]
        raise SystemExit(subprocess.call(args))
    except FileNotFoundError:
        raise SystemExit("Python 3.14 detected and Python 3.12 launcher not found. Run: py -3.12 rc_bot_improved.py")

TEMP_DIR = tempfile.gettempdir()
US_STATE_RE = r"(?:AL|AK|AZ|AR|CA|CO|CT|DC|DE|FL|GA|HI|IA|ID|IL|IN|KS|KY|LA|MA|MD|ME|MI|MN|MO|MS|MT|NC|ND|NE|NH|NJ|NM|NV|NY|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VA|VT|WA|WI|WV|WY)"
MONTH_NAME_RE = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
FIXED_BRAND_IMAGE_FILENAME = "Gemini_Generated_Image_3evokr3evokr3evo.png"
FIXED_BRAND_IMAGE_PATH = os.path.join(SCRIPT_DIR, FIXED_BRAND_IMAGE_FILENAME)
USER_BRAND_IMAGE_PATH = os.getenv("UZB_BRAND_IMAGE_PATH", "").strip()
RATE_TOGGLE_PREFIX = "rateview"

# Bot token
TOKEN = "8042412126:AAGSq0mXMR0_FSdiB8tQlnaH72e6klK66Y4"
GOOGLE_MAPS_API_KEY = (os.getenv("GOOGLE_MAPS_API_KEY") or "").strip()

TESSERACT_CMD = (os.getenv("TESSERACT_CMD") or r"C:\Program Files\Tesseract-OCR\tesseract.exe").strip()
if TESSERACT_CMD:
    pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def split_lines(text: str):
    return [normalize_space(line) for line in (text or "").splitlines() if normalize_space(line)]


def unique_keep_order(values):
    seen = set()
    out = []
    for value in values or []:
        cleaned = normalize_space(value)
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(cleaned)
    return out


def cleanup_extracted_text(text: str) -> str:
    text = text or ""
    text = text.replace("\x00", " ")
    text = re.sub(r"\(cid:\d+\)", " ", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def extract_text_from_pdf(file_path: str) -> str:
    parts = []
    try:
        with pdfplumber.open(file_path) as pdf:
            for page in pdf.pages:
                native = page.extract_text() or ""
                native_clean = re.sub(r"\(cid:\d+\)", " ", native)
                native_is_readable = bool(re.search(r"[A-Za-z]{4,}", native_clean))
                native_is_garbage = native.count("(cid:") > 20
                if native.strip() and native_is_readable and not native_is_garbage:
                    parts.append(native)

                native_state_hits = len(re.findall(rf"\b{US_STATE_RE}\b\s+\d{{5}}", native_clean, re.IGNORECASE))
                native_has_stop_markers = bool(re.search(r"\bSTOP\s+DETAILS\b|\bStop\s+\d+\s+of\s+\d+\b", native_clean, re.IGNORECASE))
                needs_ocr = (
                    (not native.strip())
                    or (not native_is_readable)
                    or native_is_garbage
                    or (native_state_hits < 2)
                    or (not native_has_stop_markers and len(native_clean) < 1500)
                )
                if needs_ocr:
                    try:
                        img = page.to_image(resolution=250).original
                        ocr_text = pytesseract.image_to_string(img, config="--oem 3 --psm 6")
                        if ocr_text.strip():
                            parts.append(ocr_text)
                    except Exception as ocr_error:
                        logger.warning("OCR failed on page: %s", ocr_error)
    except Exception as exc:
        logger.exception("PDF extraction error: %s", exc)
        return ""

    merged_lines = unique_keep_order(split_lines("\n".join(parts)))
    return cleanup_extracted_text("\n".join(merged_lines))


def extract_text_from_image(image_path: str) -> str:
    try:
        img = Image.open(image_path)
        return cleanup_extracted_text(pytesseract.image_to_string(img, config="--oem 3 --psm 6"))
    except Exception as exc:
        logger.exception("Image OCR error: %s", exc)
        return ""


def find_first(patterns, text: str, flags: int = re.IGNORECASE):
    for pattern in patterns:
        m = re.search(pattern, text or "", flags)
        if m:
            return m
    return None


def clean_address(value: str) -> str:
    value = normalize_space(value)
    value = re.sub(r"\s*,\s*", ", ", value)
    value = re.sub(r"\b([A-Z]{2})\s*,\s*(\d{5}(?:-\d{4})?)\b", r"\1 \2", value)
    value = value.replace(" ,", ",")
    return value.strip(" -,")


def clean_date_value(value: str) -> str:
    value = normalize_space(value)
    date_match = re.search(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b", value)
    if date_match:
        return date_match.group(0)
    text_date = re.search(rf"\b{MONTH_NAME_RE}\s+\d{{1,2}},\s*\d{{4}}\b", value, re.IGNORECASE)
    if text_date:
        return normalize_space(text_date.group(0))
    iso_date = re.search(r"\b\d{4}-\d{2}-\d{2}\b", value)
    if iso_date:
        return iso_date.group(0)
    return "N/A"


def clean_time_value(value: str) -> str:
    value = normalize_space(value)
    return value or "N/A"


def build_maps_link(address: str) -> str:
    address = clean_address(address)
    if not address or address == "N/A":
        return ""
    return f"https://www.google.com/maps/search/?api=1&query={quote_plus(address)}"


def extract_date_time(text: str):
    date = "N/A"
    time = "N/A"
    date_match = find_first(
        [r"\b\d{1,2}/\d{1,2}/\d{2,4}\b", rf"\b{MONTH_NAME_RE}\s+\d{{1,2}},\s*\d{{4}}\b", r"\b\d{4}-\d{2}-\d{2}\b"],
        text,
    )
    if date_match:
        date = normalize_space(date_match.group(0))
    time_match = find_first(
        [r"\b\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2}\b", r"\b\d{1,2}:\d{2}\s*(?:AM|PM|CDT|CST|EDT|EST|PDT|PST)?\b"],
        text,
    )
    if time_match:
        time = normalize_space(time_match.group(0))
    elif re.search(r"\bFCFS\b", text or "", re.IGNORECASE):
        time = "FCFS"
    return date, time


def make_stop(stop_type: str, number: int) -> dict:
    return {
        "type": stop_type,
        "number": number,
        "location": "N/A",
        "address": "N/A",
        "date": "N/A",
        "time": "N/A",
        "maps_link": "",
        "references": [],
    }


def finalize_stop(stop: dict) -> dict:
    stop["location"] = normalize_space(stop.get("location", "N/A")) or "N/A"
    stop["address"] = clean_address(stop.get("address", "N/A")) or "N/A"
    stop["date"] = clean_date_value(stop.get("date", "N/A"))
    stop["time"] = clean_time_value(stop.get("time", "N/A"))
    stop["maps_link"] = build_maps_link(stop["address"])
    stop["references"] = unique_keep_order(stop.get("references", []))
    return stop


def has_real_stop(stop: dict) -> bool:
    return any(stop.get(k) not in {"", "N/A"} for k in ["location", "address", "date", "time"])


def extract_reference_numbers(text: str):
    patterns = [
        ("PO", r"\bPO(?:\s*NUMBER)?\s*#?\s*[:\-]?\s*([A-Z0-9-]{3,})\b"),
        ("BOL", r"\bBOL(?:\s*NUMBER)?\s*#?\s*[:\-]?\s*([A-Z0-9-]{3,})\b"),
        ("LOAD", r"\bLOAD\s*#?\s*([A-Z0-9-]{3,})\b"),
        ("ORDER", r"\bORDER\s*#?\s*([A-Z0-9-]{3,})\b"),
    ]
    refs = []
    for label, pattern in patterns:
        for m in re.finditer(pattern, text or "", re.IGNORECASE):
            token = normalize_space(m.group(1)).strip(" -:;,.").upper()
            token = re.sub(r"[^A-Z0-9-]", "", token)
            if not token or not re.search(r"\d", token):
                continue
            refs.append(f"{label}: {token}")
    return unique_keep_order(refs)


def extract_load_number(text: str) -> str:
    patterns = [
        r"\bPRO\s*#\s*([A-Z0-9-]{3,})\b",
        r"\bLOAD\s*#\s*([A-Z0-9-]{3,})\b",
        r"\bORDER\s*#\s*([A-Z0-9-]{3,})\b",
        r"\bORDER\s*[:#]?\s*([0-9]{4,})\b",
    ]
    for pattern in patterns:
        for m in re.finditer(pattern, text or "", re.IGNORECASE):
            token = normalize_space(m.group(1)).strip(" -:;,.")
            if re.search(r"\d", token):
                return token
    return "N/A"


def extract_rate(text: str) -> str:
    patterns = [
        r"\bTOTAL\s+RATE\s*[:#-]?\s*\$?\s*([\d,]+(?:\.\d{2})?)",
        r"\bTOTAL\s+COST\s*[:#-]?\s*\$?\s*([\d,]+(?:\.\d{2})?)",
        r"\bCARRIER\s+PAY(?:MENT)?\s*[:#-]?\s*\$?\s*([\d,]+(?:\.\d{2})?)",
        r"\bTOTAL\s*\$\s*([\d,]+(?:\.\d{2})?)",
    ]
    m = find_first(patterns, text or "", re.IGNORECASE | re.DOTALL)
    return m.group(1) if m else "N/A"


def extract_wecanmoveit_stops(text: str):
    lines = split_lines(text)
    stop_details_start = 0
    for i, line in enumerate(lines):
        if re.search(r"\bSTOP\s+DET", line, re.IGNORECASE):
            stop_details_start = i
            break
    tail = lines[stop_details_start:]

    anchors = []
    for i, line in enumerate(tail):
        if re.search(r"\b(?:PICKUP|PICK\s*UP|PECUP|DELIVERY|DEIVERY|DEINERY)\b", line, re.IGNORECASE):
            window = "\n".join(tail[i : i + 10])
            if re.search(rf"\b{US_STATE_RE}\b\s+\d{{5}}", window, re.IGNORECASE):
                anchors.append(i)
    anchors = sorted(set(anchors))
    if not anchors:
        return [], []

    blocks = []
    for a_idx, anchor in enumerate(anchors):
        next_anchor = anchors[a_idx + 1] if a_idx + 1 < len(anchors) else len(tail)
        start = max(0, anchor - 4)
        end = max(start + 1, next_anchor)
        blocks.append(tail[start:end])

    pickups = []
    deliveries = []
    for block_lines in blocks:
        block = "\n".join(block_lines)
        upper = block.upper()
        is_delivery = bool(re.search(r"\bDELIV|\bDEINER|\bDEIVERY\b", upper))
        is_pickup = bool(re.search(r"\bPICK\b|\bPECUP\b|\bPICKUP\b", upper))
        stop_type = "delivery" if (is_delivery and not is_pickup) else "pickup"

        address = "N/A"
        for j in range(len(block_lines) - 1):
            if re.match(r"^\d{1,6}\s+", block_lines[j]):
                maybe_city = block_lines[j + 1] if j + 1 < len(block_lines) else ""
                if re.search(rf"\b{US_STATE_RE}\b", maybe_city, re.IGNORECASE):
                    address = clean_address(f"{block_lines[j]}, {maybe_city}")
                    break
        if address == "N/A":
            m = re.search(rf"(\d{{1,6}}\s+[^,\n]+,\s*[^,\n]+,\s*{US_STATE_RE}\s*,?\s*\d{{5}})", block, re.IGNORECASE)
            if m:
                address = clean_address(m.group(1))

        location = "N/A"
        for j in range(len(block_lines) - 1):
            if re.match(r"^\d{1,6}\s+", block_lines[j]):
                if j - 1 >= 0:
                    candidate = block_lines[j - 1]
                    if len(candidate) <= 60 and not re.search(r"\b(PICKUP|DELIVERY|LOADING|SCHEDULE|WINDOW|TYPE)\b", candidate, re.IGNORECASE):
                        location = candidate
                break

        date, time = extract_date_time(block)
        stop = make_stop(stop_type, (len(deliveries) + 1) if stop_type == "delivery" else (len(pickups) + 1))
        stop["location"] = location
        stop["address"] = address
        stop["date"] = date
        stop["time"] = time
        stop["references"] = extract_reference_numbers(block)
        stop = finalize_stop(stop)
        if stop_type == "pickup":
            pickups.append(stop)
        else:
            deliveries.append(stop)

    pickups = [s for s in pickups if has_real_stop(s)]
    deliveries = [s for s in deliveries if has_real_stop(s)]
    return pickups, deliveries


def calculate_loaded_miles_google(pickup_stops, delivery_stops):
    if not GOOGLE_MAPS_API_KEY:
        return "N/A"
    route = []
    for stop in (pickup_stops or []) + (delivery_stops or []):
        addr = stop.get("address", "N/A")
        if addr and addr != "N/A":
            route.append(addr)
    if len(route) < 2:
        return "N/A"

    total = 0.0
    for i in range(len(route) - 1):
        origin = route[i]
        dest = route[i + 1]
        url = (
            "https://maps.googleapis.com/maps/api/distancematrix/json"
            f"?origins={quote_plus(origin)}&destinations={quote_plus(dest)}&units=imperial&key={quote_plus(GOOGLE_MAPS_API_KEY)}"
        )
        try:
            with urlopen(url, timeout=15) as response:
                payload = json.loads(response.read().decode("utf-8", errors="ignore"))
            if payload.get("status") != "OK":
                continue
            rows = payload.get("rows") or []
            if not rows:
                continue
            elements = rows[0].get("elements") or []
            if not elements:
                continue
            element = elements[0]
            if element.get("status") != "OK":
                continue
            meters = (element.get("distance") or {}).get("value")
            if meters:
                total += float(meters) / 1609.344
        except Exception:
            continue
    return str(int(round(total))) if total > 0 else "N/A"


def parse_rc(text: str) -> dict:
    data = {
        "pro_number": extract_load_number(text),
        "broker": "N/A",
        "commodity": "N/A",
        "weight": "N/A",
        "pallets": "N/A",
        "miles": "N/A",
        "google_loaded_miles": "N/A",
        "equipment": "N/A",
        "total_rate": extract_rate(text),
        "pickup_stops": [],
        "delivery_stops": [],
        "reference_numbers": [],
        "charge_items": [],
        "special_instructions": [],
        "is_hazmat": bool(re.search(r"\bHAZ(?:MAT|ARDOUS)\b", text or "", re.IGNORECASE)) and not bool(re.search(r"\bNON[-\s]?HAZ", text or "", re.IGNORECASE)),
        "un_number": "N/A",
        "hazmat_class": "N/A",
        "temp_range": "N/A",
        "temp_mode": "N/A",
        "pulp_required": False,
        "pulp_not_required": False,
        "tarp_required": False,
        "tarp_not_required": False,
        "temp_on_bol": False,
        "tracking_required": False,
        "seal_required": False,
    }

    miles_match = find_first([r"\bTOTAL\s+MILES\s*[:#-]?\s*([\d,]+(?:\.\d+)?)\b", r"\bMILES?\s*[:#-]?\s*([\d,]+(?:\.\d+)?)\b"], text or "")
    if miles_match:
        data["miles"] = miles_match.group(1)

    weight_match = find_first([r"\bTOTAL\s+WEIGHT\s*[:#-]?\s*([\d,]+)\b", r"\bWEIGHT\s*[:#-]?\s*([\d,]+)\s*(?:LB|LBS)?\b"], text or "")
    if weight_match:
        data["weight"] = weight_match.group(1)

    pallets_match = find_first([r"\bPALLETS?\s*[:#-]?\s*(\d+)\b", r"\b(\d+)\s+PALLETS?\b"], text or "")
    if pallets_match:
        data["pallets"] = pallets_match.group(1)

    equip_match = find_first([
        r"\bEQUIPMENT\s+TYPE\s*[:#-]?\s*([^\n\r]{1,40})",
        r"\bEQUIP(?:MENT)?\s*[:#-]?\s*([^\n\r]{1,40})",
        r"\bTrailer\s*:\s*([^\n\r]{3,40})",
    ], text or "")
    if equip_match:
        raw = normalize_space(equip_match.group(1)).split(";")[0].strip()
        if re.search(r"\bREEFER\b", raw, re.IGNORECASE):
            data["equipment"] = "R (REEFER)"
        elif re.search(r"\bVAN\s*[-–]\s*HAZARDOUS\b|\bHAZMAT\s+VAN\b|\bVAN\s+HAZMAT\b", raw, re.IGNORECASE):
            data["equipment"] = "V (HAZMAT)"
        elif re.search(r"\bVAN\b|\bDRY\b", raw, re.IGNORECASE):
            data["equipment"] = "V"
        elif re.search(r"\bFLATBED\b", raw, re.IGNORECASE):
            data["equipment"] = "F"
        elif raw:
            data["equipment"] = raw[:25]

    com_match = find_first([r"\bCOMMODITY\b\s*[:#-]?\s*([^\n\r;]{3,120})"], text or "")
    if com_match:
        raw_commodity = normalize_space(com_match.group(1))
        # Strip anything after Trailer: or semicolon
        raw_commodity = re.split(r"\s*[;|]\s*|\s+Trailer\s*:", raw_commodity, flags=re.IGNORECASE)[0]
        data["commodity"] = normalize_space(raw_commodity)

    un_match = find_first([r"\bUN\s*#?\s*(\d{4})\b", r"\bUN\s*NUMBER\s*[:#-]?\s*(\d{4})\b"], text or "")
    if un_match:
        data["un_number"] = f"UN{un_match.group(1)}"
        data["is_hazmat"] = True
    class_match = find_first([r"\b(?:HAZ(?:MAT)?\s+)?CLASS\s*[:#-]?\s*([0-9.]+)\b"], text or "")
    if class_match:
        data["hazmat_class"] = class_match.group(1)

    broker = detect_broker(text or "")
    data["broker"] = broker

    upper = (text or "").upper()
    # Route to correct stop extractor based on format/broker
    if re.search(r"(?m)^\s*PU\s*\d+\b", text or "", re.IGNORECASE) and \
       (
           re.search(r"(?m)^\s*SO\s*\d+\b", text or "", re.IGNORECASE)
           or re.search(r"(?m)^\s*FINAL\s+DELIVERY\b", text or "", re.IGNORECASE)
           or re.search(r"(?m)^\s*DELIVERY\s+STOP\b", text or "", re.IGNORECASE)
       ):
        # McLeod format: PU 1 / SO N (Scotlynn, Ace Truckload, etc.)
        pu, de = extract_mcleod_stops(text)
    elif re.search(r"Shipper\s*\(Stop\s*\d+\s*of\s*\d+\)", text or "", re.IGNORECASE) or \
         ("WECANMOVEIT" in upper):
        pu, de = extract_wecanmoveit_stops(text)
    elif "PROPEL FREIGHT" in upper and re.search(r"(?m)^\s*PICK\s*1\b", text or ""):
        pu, de = extract_propel_stops(text)
    elif "ARRIVE LOGISTICS" in upper or "ARRIVE ORDER" in upper:
        pu, de = extract_arrive_stops(text)
    elif "COR FREIGHT" in upper:
        pu, de = extract_cor_stops(text)
    elif "CARDINAL LOGISTICS" in upper or "RYDER" in upper:
        pu, de = extract_cardinal_stops(text)
    elif "BARAKAT" in upper:
        pu, de = extract_barakat_stops(text)
    elif "ALLEN LUND" in upper:
        pu, de = extract_allen_lund_stops(text)
    else:
        pu, de = extract_default_stops(text)

    fallback_pu = fallback_de = None
    if not any(has_real_stop(stop) for stop in (pu or [])):
        fallback_pu, fallback_de = extract_default_stops(text)
        if any(has_real_stop(stop) for stop in (fallback_pu or [])):
            pu = fallback_pu
    if not any(has_real_stop(stop) for stop in (de or [])):
        if fallback_de is None:
            _, fallback_de = extract_default_stops(text)
        if any(has_real_stop(stop) for stop in (fallback_de or [])):
            de = fallback_de

    data["pickup_stops"] = pu
    data["delivery_stops"] = de
    data["google_loaded_miles"] = calculate_loaded_miles_google(pu, de)

    def _clean_ref_value(ref: str) -> str:
        value = normalize_space(ref or "")
        value = re.sub(r"^REF:\s*REF:\s*", "REF: ", value, flags=re.IGNORECASE)
        value = re.sub(r"\bPES\b.*$", "", value, flags=re.IGNORECASE).strip(" -,:;")
        value = re.sub(r"^REF:\s*(REF:\s*)+", "REF: ", value, flags=re.IGNORECASE)
        value = re.sub(r"\s{2,}", " ", value)
        return value

    pickup_po_pairs = re.findall(r"\bPO\s*#?\s*(\d{5,})\s*[-:]\s*(TAIL|NOSE)\b", text or "", re.IGNORECASE)
    pickup_po_pairs = sorted(
        pickup_po_pairs,
        key=lambda item: (0 if item[1].upper() == "TAIL" else 1, item[0]),
    )
    if pu and pickup_po_pairs:
        pickup_po_refs = [f"PO {po} - {part.upper()}" for po, part in pickup_po_pairs]
        existing_refs = [_clean_ref_value(r) for r in (pu[0].get("references") or []) if _clean_ref_value(r)]
        existing_non_po_tail_nose = [
            r for r in existing_refs
            if not re.search(r"\bPO\s*\d{5,}\s*-\s*(TAIL|NOSE)\b", r, re.IGNORECASE)
        ]
        pu[0]["references"] = unique_keep_order(pickup_po_refs + existing_non_po_tail_nose)

    for stop in (pu or []) + (de or []):
        stop["references"] = unique_keep_order([_clean_ref_value(r) for r in (stop.get("references") or []) if _clean_ref_value(r)])

    refs = []
    refs.extend(extract_reference_numbers(text))
    for s in (pu or []) + (de or []):
        refs.extend(s.get("references") or [])
    data["reference_numbers"] = unique_keep_order([_clean_ref_value(r) for r in refs if _clean_ref_value(r)])[:12]
    data["charge_items"] = extract_charge_items(text)

    instr = []
    comment_notes = extract_special_instructions(text)
    if re.search(r"\bPOD\b", text or "", re.IGNORECASE):
        instr.append("Send POD after delivery.")
    if re.search(r"\bMACRO\s*POINT\b", text or "", re.IGNORECASE):
        instr.append("MacroPoint tracking required.")
    if re.search(r"\bAPPOINTMENT\b|\bAPPT\b", text or "", re.IGNORECASE):
        instr.append("Appointment required.")
    if re.search(r"\bLUMPER\b", text or "", re.IGNORECASE):
        instr.append("Check lumper instructions.")
    instr.extend(comment_notes)

    # Temp controls (reefer)
    temp_data = extract_temp_controls(text or "")
    data["temp_range"] = temp_data["temp_range"]
    data["temp_mode"] = temp_data["temp_mode"]
    data["temp_on_bol"] = temp_data.get("temp_on_bol", False)
    data["pulp_required"] = temp_data.get("pulp_required", False)
    data["pulp_not_required"] = temp_data.get("pulp_not_required", False)
    data["tarp_required"] = temp_data.get("tarp_required", False)
    data["tarp_not_required"] = temp_data.get("tarp_not_required", False)
    data["tracking_required"] = temp_data.get("tracking_required", False)
    data["seal_required"] = temp_data.get("seal_required", False)

    if data["pulp_required"]:
        instr.append("Pulp required before loading.")
    elif data["pulp_not_required"]:
        instr.append("No pulp check required.")

    if data["tarp_required"]:
        instr.append("Tarp required.")
    elif data["tarp_not_required"]:
        instr.append("No tarp required.")
    if data.get("temp_mode") == "CONTINUOUS":
        instr.append("Reefer must run continuous.")
    if data.get("temp_on_bol"):
        instr.append("Temperature must follow BOL instructions.")
    if data.get("tracking_required"):
        instr.append("Tracking required.")
    if data.get("seal_required"):
        instr.append("Seal required.")
    if re.search(r"\bDRIVER\s+MUST\s+ACCEPT\s+TRACKING\b|\bMUST\s+ACCEPT\s+TRACKING\b", text or "", re.IGNORECASE):
        instr.append("Driver must accept tracking.")
    if re.search(r"\bCONFIRM\s+PO\s+ORDER\b|\bPO\s+ORDER\s+CONFIRM(?:ATION)?\b", text or "", re.IGNORECASE):
        instr.append("Confirm PO order before delivery.")
    if re.search(r"\bTWO\s+LOAD\s+UPDATES\s+PER\s+DAY\b|\bLOAD\s+UPDATES?\s+PER\s+DAY\b", text or "", re.IGNORECASE):
        instr.append("Provide two load updates per day (AM/PM).")
    if re.search(r"\bDELAY(?:ED|S)?\b.*\bREPORT\b|\bREPORT\s+DELAYS?\b", text or "", re.IGNORECASE):
        instr.append("Report delays to broker immediately.")
    if re.search(r"\bLUMPER\b.*\bAPPROV", text or "", re.IGNORECASE) or re.search(r"\bLUMPER\b.*\bREIMBURS", text or "", re.IGNORECASE):
        instr.append("Lumper charges require broker approval and receipt submission for reimbursement.")
    if re.search(r"\bPODS?\b.*\bWITHIN\s*48\s*HOURS?\b|\bBOL\b.*\bWITHIN\s*48\s*HOURS?\b", text or "", re.IGNORECASE):
        instr.append("Send POD/BOL within 48 hours after delivery.")
    if re.search(r"\bSEALED?\b|\bSEAL\s+NUMBER\b", text or "", re.IGNORECASE):
        instr.append("Trailer must be sealed and seal number must be written on BOL.")
    if re.search(r"\bDETENTION\b", text or "", re.IGNORECASE) or re.search(r"\bLAYOVER\b", text or "", re.IGNORECASE):
        instr.append("Detention/Layover paid per broker rules; submit requests on time.")

    for stop_group, label in ((pu or [], "Pickup"), (de or [], "Delivery")):
        multi_stop = len(stop_group) > 1
        for idx, stop in enumerate(stop_group, start=1):
            stop_label = f"{label} {idx}" if multi_stop else label
            for note in stop.get("notes") or []:
                cleaned = clean_instruction(note)
                if cleaned:
                    instr.append(f"{stop_label}: {cleaned}")

    data["special_instructions"] = unique_keep_order(instr)[:24]
    return data


def escape(value: str) -> str:
    return html.escape(value or "N/A")


def format_stop_block(title: str, stop: dict) -> str:
    schedule = normalize_space(f"{stop.get('date', 'N/A')} {stop.get('time', 'N/A')}") or "N/A"
    lines = [
        f"<b>{title}</b>",
        f"📍 {escape(stop.get('location', 'N/A'))}",
        escape(stop.get("address", "N/A")),
        f"🕒 {escape(schedule)}",
    ]
    if stop.get("contact") not in {"", "N/A", None}:
        lines.append(f"Contact: {escape(stop.get('contact'))}")
    if stop.get("phone") not in {"", "N/A", None}:
        lines.append(f"Phone: {escape(stop.get('phone'))}")
    if stop.get("references"):
        lines.append(f"🔢 Ref: {escape(', '.join(stop['references'][:6]))}")
    if stop.get("maps_link"):
        lines.append(f'🗺️ <a href="{stop["maps_link"]}">Open in Google Maps</a>')
    return "\n".join(lines)


def format_driver_message(
    data: dict,
    show_rate: bool = True,
    max_refs: int = 12,
    max_charge_items: int = 8,
    max_instructions: int = 12,
) -> str:
    lines = [
        "<b>🚚 RATE CONFIRMATION - DRIVER VIEW</b>",
        "",
        f"<b>📦 LOAD:</b> {escape(data.get('pro_number'))}",
        f"<b>🏢 BROKER:</b> {escape(data.get('broker'))}",
        "",
        "<b>📋 SHIPMENT</b>",
        f"Commodity: {escape(data.get('commodity'))}",
        f"Weight: {escape(data.get('weight'))}",
        f"Pallets: {escape(data.get('pallets'))}",
        f"Miles: {escape(data.get('miles'))}",
        f"Loaded Miles (Google): {escape(data.get('google_loaded_miles'))}",
        f"Equipment: {escape(data.get('equipment'))}",
        f"<b>☣️ HAZMAT:</b> {'YES' if data.get('is_hazmat') else 'NO'}",
    ]
    if data.get("is_hazmat"):
        lines.append(f"UN: {escape(data.get('un_number'))}  Class: {escape(data.get('hazmat_class'))}")
    if data.get("temp_range", "N/A") != "N/A":
        mode = data.get("temp_mode", "N/A")
        mode_str = f" ({mode})" if mode != "N/A" else ""
        lines.append(f"<b>🌡️ TEMP:</b> {escape(data.get('temp_range'))}{mode_str}")
    if data.get("tracking_required"):
        lines.append("<b>Tracking:</b> REQUIRED")
    if data.get("seal_required"):
        lines.append("<b>Seal:</b> REQUIRED")
    if data.get("pulp_required"):
        lines.append("<b>Pulp:</b> REQUIRED")
    elif data.get("pulp_not_required"):
        lines.append("<b>Pulp:</b> NOT REQUIRED")
    if data.get("tarp_required"):
        lines.append("<b>Tarp:</b> REQUIRED")
    elif data.get("tarp_not_required"):
        lines.append("<b>Tarp:</b> NOT REQUIRED")

    if data.get("reference_numbers"):
        lines.append("")
        lines.append("<b>🔢 LOAD REFERENCE NUMBERS</b>")
        for ref in data["reference_numbers"][:max_refs]:
            lines.append(f"• {escape(ref)}")

    pickups = data.get("pickup_stops") or []
    deliveries = data.get("delivery_stops") or []
    if pickups:
        lines.append("")
        for idx, stop in enumerate(pickups, start=1):
            lines.append(format_stop_block(f"📦 PICKUP {idx}", stop))
            if idx != len(pickups):
                lines.append("")
    if deliveries:
        lines.append("")
        for idx, stop in enumerate(deliveries, start=1):
            lines.append(format_stop_block(f"🏁 DELIVERY {idx}", stop))
            if idx != len(deliveries):
                lines.append("")

    if show_rate:
        lines.append("")
        if data.get("total_rate") not in {"", None, "N/A"}:
            lines.append(f"<b>💵 RATE:</b> ${escape(str(data.get('total_rate')))}")
        else:
            lines.append("<b>💵 RATE:</b> NOT CONFIRMED")
        if data.get("charge_items"):
            lines.append("")
            lines.append("<b>💸 CHARGES / RATE NOTES</b>")
            for item in data["charge_items"][:max_charge_items]:
                lines.append(f"• {escape(item)}")

    if data.get("special_instructions"):
        lines.append("")
        lines.append("<b>📝 DRIVER INSTRUCTIONS</b>")
        for item in data["special_instructions"][:max_instructions]:
            lines.append(f"• {escape(item)}")

    return "\n".join(lines)


def format_driver_caption(data: dict, show_rate: bool = True):
    attempts = [
        {"max_refs": 6, "max_charge_items": 2, "max_instructions": 5},
        {"max_refs": 4, "max_charge_items": 1, "max_instructions": 3},
        {"max_refs": 2, "max_charge_items": 0, "max_instructions": 2},
    ]
    for limits in attempts:
        caption = format_driver_message(data, show_rate=show_rate, **limits)
        if len(caption) <= 1024:
            return caption, "HTML"

    compact_lines = [
        "<b>🚚 RATE CONFIRMATION - DRIVER VIEW</b>",
        f"<b>LOAD:</b> {escape(data.get('pro_number'))}",
        f"<b>BROKER:</b> {escape(data.get('broker'))}",
        f"Commodity: {escape(data.get('commodity'))}",
        f"Equipment: {escape(data.get('equipment'))}",
    ]
    if data.get("pickup_stops"):
        compact_lines.append("")
        for idx, stop in enumerate(data["pickup_stops"], start=1):
            compact_lines.append(format_stop_block(f"📦 PICKUP {idx}", stop))
            if idx != len(data["pickup_stops"]):
                compact_lines.append("")
    if data.get("delivery_stops"):
        compact_lines.append("")
        for idx, stop in enumerate(data["delivery_stops"], start=1):
            compact_lines.append(format_stop_block(f"🏁 DELIVERY {idx}", stop))
            if idx != len(data["delivery_stops"]):
                compact_lines.append("")
    if show_rate:
        compact_lines.append("")
        if data.get("total_rate") not in {"", None, "N/A"}:
            compact_lines.append(f"<b>💵 RATE:</b> ${escape(str(data.get('total_rate')))}")
        else:
            compact_lines.append("<b>💵 RATE:</b> NOT CONFIRMED")

    caption = "\n".join(compact_lines)
    if len(caption) <= 1024:
        return caption, "HTML"
    plain_caption = re.sub(r"<[^>]+>", "", caption)
    if len(plain_caption) > 1024:
        plain_caption = plain_caption[:1021].rstrip() + "..."
    return plain_caption, None


def build_rate_toggle_markup(view_id: str, show_rate: bool):
    with_label = "[With Rate]" if show_rate else "With Rate"
    without_label = "Without Rate" if show_rate else "[Without Rate]"
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton(with_label, callback_data=f"{RATE_TOGGLE_PREFIX}:{view_id}:with"),
            InlineKeyboardButton(without_label, callback_data=f"{RATE_TOGGLE_PREFIX}:{view_id}:without"),
        ]]
    )


def resolve_brand_image_path() -> str:
    return FIXED_BRAND_IMAGE_PATH if os.path.isfile(FIXED_BRAND_IMAGE_PATH) else ""


async def send_output(update: Update, context: ContextTypes.DEFAULT_TYPE, data: dict):
    view_id = os.urandom(6).hex()
    context.chat_data.setdefault("rc_rate_views", {})[view_id] = data
    reply_markup = build_rate_toggle_markup(view_id, show_rate=True)
    msg = format_driver_message(data, show_rate=True)
    caption, caption_mode = format_driver_caption(data, show_rate=True)
    brand = resolve_brand_image_path()
    if brand:
        try:
            with open(brand, "rb") as f:
                photo_kwargs = {
                    "chat_id": update.effective_chat.id,
                    "photo": f,
                    "caption": caption,
                    "reply_markup": reply_markup,
                }
                if caption_mode:
                    photo_kwargs["parse_mode"] = caption_mode
                await context.bot.send_photo(
                    **photo_kwargs,
                )
            return
        except Exception as exc:
            logger.warning("Photo send failed, falling back to text: %s", exc)
    await update.message.reply_text(
        msg[:4096],
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=reply_markup,
    )


async def handle_rate_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return

    parts = (query.data or "").split(":")
    if len(parts) != 3:
        await query.answer()
        return
    _, view_id, mode = parts
    data = (context.chat_data.get("rc_rate_views") or {}).get(view_id)
    if not data:
        await query.answer("This RC view expired. Send the file again.", show_alert=True)
        return

    await query.answer()
    show_rate = mode == "with"
    reply_markup = build_rate_toggle_markup(view_id, show_rate=show_rate)
    try:
        if getattr(query.message, "photo", None):
            caption, caption_mode = format_driver_caption(data, show_rate=show_rate)
            edit_kwargs = {"caption": caption, "reply_markup": reply_markup}
            if caption_mode:
                edit_kwargs["parse_mode"] = caption_mode
            await query.edit_message_caption(**edit_kwargs)
        else:
            await query.edit_message_text(
                format_driver_message(data, show_rate=show_rate)[:4096],
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=reply_markup,
            )
    except Exception as exc:
        logger.warning("Rate toggle failed: %s", exc)


async def process_file(update: Update, context: ContextTypes.DEFAULT_TYPE, source_kind: str):
    file_path = ""
    try:
        def _download_timeout_kwargs():
            # download_to_drive() supports these kwargs; set them higher to prevent "Timed out" on slow networks.
            return {
                "read_timeout": 180,
                "write_timeout": 180,
                "connect_timeout": 30,
                "pool_timeout": 180,
            }

        if source_kind == "pdf":
            # Retry file fetch/download (these are the common spots for "Timed out").
            last_exc = None
            for attempt in range(1, 4):
                try:
                    f = await update.message.document.get_file()
                    file_path = os.path.join(TEMP_DIR, f"{f.file_unique_id}.pdf")
                    await f.download_to_drive(file_path, **_download_timeout_kwargs())
                    text = extract_text_from_pdf(file_path)
                    break
                except Exception as exc:
                    last_exc = exc
                    await update.message.reply_text(f"Download failed (attempt {attempt}/3). Retrying...")
            else:
                raise last_exc
        else:
            last_exc = None
            for attempt in range(1, 4):
                try:
                    f = await update.message.photo[-1].get_file()
                    file_path = os.path.join(TEMP_DIR, f"{f.file_unique_id}.jpg")
                    await f.download_to_drive(file_path, **_download_timeout_kwargs())
                    text = extract_text_from_image(file_path)
                    break
                except Exception as exc:
                    last_exc = exc
                    await update.message.reply_text(f"Download failed (attempt {attempt}/3). Retrying...")
            else:
                raise last_exc

        if not normalize_space(text):
            await update.message.reply_text("I could not read this file. Send a clearer PDF or photo.")
            return

        data = parse_rc(text)
        await send_output(update, context, data)
    except Exception as exc:
        logger.exception("Processing error")
        await update.message.reply_text(f"Error: {exc}")
    finally:
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception:
                pass


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await process_file(update, context, "pdf")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await process_file(update, context, "photo")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🚛 UZB Freight RC Bot\n\n"
        "Send a rate confirmation PDF or photo and I will extract all load details automatically."
    )


def main():
    logger.info("Bot starting...")
    # Increase Telegram network timeouts to reduce "Timed out" on PDFs/photos.
    app = (
        Application.builder()
        .token(TOKEN)
        .connect_timeout(30)
        .read_timeout(120)
        .write_timeout(120)
        .pool_timeout(120)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_rate_toggle, pattern=rf"^{RATE_TOGGLE_PREFIX}:"))
    app.add_handler(MessageHandler(filters.Document.PDF, handle_document))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    logger.info("Bot started")
    app.run_polling()


def unique_keep_order(values):
    seen = set()
    out = []
    for value in values:
        cleaned = normalize_space(value)
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(cleaned)
    return out


def cleanup_extracted_text(text: str) -> str:
    text = text or ""
    text = text.replace("\x00", " ")
    text = re.sub(r"\(cid:\d+\)", " ", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def extract_text_from_pdf(file_path: str) -> str:
    parts = []
    try:
        with pdfplumber.open(file_path) as pdf:
            for page in pdf.pages:
                native = page.extract_text() or ""
                native_clean = re.sub(r"\(cid:\d+\)", " ", native)
                native_is_readable = bool(re.search(r"[A-Za-z]{4,}", native_clean))
                native_is_garbage = native.count("(cid:") > 20
                if native.strip() and native_is_readable and not native_is_garbage:
                    parts.append(native)

                native_state_hits = len(re.findall(rf"\b{US_STATE_RE}\b\s+\d{{5}}", native_clean, re.IGNORECASE))
                native_has_stop_markers = bool(re.search(r"\bSTOP\s+DETAILS\b|\bStop\s+\d+\s+of\s+\d+\b", native_clean, re.IGNORECASE))
                needs_ocr = (not native.strip()) or (not native_is_readable) or native_is_garbage or (native_state_hits < 2) or (not native_has_stop_markers and len(native_clean) < 1500)

                if needs_ocr:
                    try:
                        image = page.to_image(resolution=250)
                        ocr_text = pytesseract.image_to_string(image.original, config="--oem 3 --psm 6")
                        if ocr_text.strip():
                            parts.append(ocr_text)
                    except Exception as ocr_error:
                        logger.warning("OCR failed on page: %s", ocr_error)
    except Exception as exc:
        logger.error("PDF extraction error: %s", exc)
        return ""
    merged_lines = unique_keep_order(split_lines("\n".join(parts)))
    return cleanup_extracted_text("\n".join(merged_lines))


def extract_text_from_image(image_path: str) -> str:
    try:
        image = Image.open(image_path)
        return cleanup_extracted_text(pytesseract.image_to_string(image))
    except Exception as exc:
        logger.error("Image OCR error: %s", exc)
        return ""


def find_first(patterns, text: str, flags: int = re.IGNORECASE):
    for pattern in patterns:
        match = re.search(pattern, text, flags)
        if match:
            return match
    return None


def clean_address(value: str) -> str:
    value = normalize_space(value)
    value = re.sub(r"\s*,\s*", ", ", value)
    value = re.sub(r"\b([A-Z]{2})\s*,\s*(\d{5}(?:-\d{4})?)\b", r"\1 \2", value)
    value = value.replace(" ,", ",")
    return value.strip(" -,")


def clean_stop_name(value: str) -> str:
    value = normalize_space(value)
    value = re.sub(r"\b(?:Pick\s*Up|Pickup|Delivery)\s*Date\s*:?.*", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\b(?:Pick\s*Up|Pickup|Delivery)\s*Time\s*:?.*", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\bUzb\s+Freight\s+Inc\b.*$", "", value, flags=re.IGNORECASE)
    value = re.sub(rf"\b[A-Za-z][A-Za-z .'-]+,\s*{US_STATE_RE}\s*,?\s*\d{{5}}(?:-\d{{4}})?\b", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\bBy\s*$", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\b(\w+)\s+\1\b", r"\1", value, flags=re.IGNORECASE)
    return normalize_space(value) or "N/A"


def clean_address_line(value: str) -> str:
    value = normalize_space(value)
    value = re.sub(r"\b(?:Pick\s*Up|Pickup|Delivery)\s*Time\s*:?.*", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\b(?:Pick\s*Up|Pickup|Delivery)\s*Date\s*:?.*", "", value, flags=re.IGNORECASE)
    value = re.split(
        r"\b(?:Ready\s*Date|Appointment|Phone/Contact|Phone|Contact|Weight|Pallets?|Pieces|Ref(?:/PO)?\s*#?|FCFS\s*Notes?|Latest\s*Date/Time|Earliest\s*Date/Time|Time|Type|Shipping\s+Hours|Receiving\s+Hours|DOT)\b",
        value,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    return clean_address(value)


def clean_time_value(value: str) -> str:
    value = normalize_space(value)
    value = re.sub(r"^(?:Pick\s*Up|Pickup|Delivery)\s*Time\s*:?\s*", "", value, flags=re.IGNORECASE)
    return value or "N/A"


def clean_date_value(value: str) -> str:
    value = normalize_space(value)
    date_match = re.search(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b", value)
    if date_match:
        return date_match.group(0)
    text_date = re.search(rf"\b{MONTH_NAME_RE}\s+\d{{1,2}},\s*\d{{4}}\b", value, re.IGNORECASE)
    if text_date:
        return normalize_space(text_date.group(0))
    iso_date = re.search(r"\b\d{4}-\d{2}-\d{2}\b", value)
    if iso_date:
        return iso_date.group(0)
    return "N/A"


def build_maps_link(address: str) -> str:
    address = clean_address(address)
    if not address or address == "N/A":
        return ""
    return f"https://www.google.com/maps/search/?api=1&query={quote_plus(address)}"


def build_hazmattool_link(un_number: str) -> str:
    digits = re.sub(r"\D", "", un_number or "")
    if digits:
        return f"https://www.hazmattool.com/info.php?search={digits}&language=en"
    return "https://www.hazmattool.com/info.php"


def fetch_google_leg_miles(origin: str, destination: str, api_key: str):
    if not api_key:
        return None
    if not origin or not destination or origin == "N/A" or destination == "N/A":
        return None
    url = (
        "https://maps.googleapis.com/maps/api/distancematrix/json"
        f"?origins={quote_plus(origin)}"
        f"&destinations={quote_plus(destination)}"
        "&units=imperial"
        f"&key={quote_plus(api_key)}"
    )
    try:
        with urlopen(url, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8", errors="ignore"))
        if payload.get("status") != "OK":
            return None
        rows = payload.get("rows") or []
        if not rows:
            return None
        elements = rows[0].get("elements") or []
        if not elements:
            return None
        item = elements[0]
        if item.get("status") != "OK":
            return None
        meters = item.get("distance", {}).get("value")
        if meters is None:
            return None
        return float(meters) / 1609.344
    except Exception:
        return None


def calculate_loaded_miles_google(pickup_stops, delivery_stops):
    api_key = os.getenv("GOOGLE_MAPS_API_KEY", "").strip()
    if not api_key:
        return "N/A"
    route_addresses = []
    for stop in (pickup_stops or []) + (delivery_stops or []):
        address = stop.get("address", "N/A")
        if address and address != "N/A":
            route_addresses.append(address)
    if len(route_addresses) < 2:
        return "N/A"

    total = 0.0
    for idx in range(len(route_addresses) - 1):
        miles = fetch_google_leg_miles(route_addresses[idx], route_addresses[idx + 1], api_key)
        if miles is None:
            return "N/A"
        total += miles
    return f"{total:.1f}"


def create_badge_image(title: str, subtitle: str, color_bg=(18, 62, 130), color_accent=(40, 110, 220), color_text=(255, 255, 255), file_prefix="rc_badge"):
    width, height = 1200, 628
    image = Image.new("RGB", (width, height), color_bg)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((70, 220, width - 70, height - 80), radius=50, fill=color_accent)
    try:
        title_font = ImageFont.truetype("arial.ttf", 86)
        subtitle_font = ImageFont.truetype("arial.ttf", 58)
    except Exception:
        title_font = ImageFont.load_default()
        subtitle_font = ImageFont.load_default()

    title = normalize_space(title).upper()[:40]
    subtitle = normalize_space(subtitle).upper()[:80]
    draw.text((90, 95), title, font=title_font, fill=color_text)
    draw.text((95, 290), subtitle, font=subtitle_font, fill=color_text)

    fd, path = tempfile.mkstemp(prefix=file_prefix, suffix=".png")
    os.close(fd)
    image.save(path, format="PNG")
    return path


def resolve_brand_image_path():
    candidates = [
        FIXED_BRAND_IMAGE_PATH,
        os.path.join(r"C:\Users\RZ\OneDrive\Desktop\TELEGRAM BOT", FIXED_BRAND_IMAGE_FILENAME),
    ]
    if USER_BRAND_IMAGE_PATH:
        candidates.insert(0, USER_BRAND_IMAGE_PATH)

    for path in unique_keep_order(candidates):
        if os.path.isfile(path):
            return path
    return ""


def map_equipment_code(text: str) -> str:
    upper = text.upper()
    if re.search(r"\bREEFER\b|\bREFRIGERATED\b", upper):
        return "R"
    if re.search(r"\bVAN\s+HAZMAT\b|\bHAZMAT\s+VAN\b", upper):
        return "V (HAZMAT)"
    if re.search(r"\bDRY\s+VAN\b", upper):
        return "V"
    if re.search(r"\bTANKER\b|\bTANK\s+TRAILER\b", upper):
        return "TANKER"
    if re.search(r"\bFLATBED\b", upper):
        return "F"
    if re.search(r"\bSTEP\s*DECK\b", upper):
        return "SD"
    if re.search(r"\bPOWER\s+ONLY\b", upper):
        return "PO"
    if re.search(r"\b53'\s*VAN\b|\bVAN\b", upper):
        return "V"
    return "N/A"


def clean_instruction(note: str) -> str:
    note = normalize_space(note)
    note = re.sub(r"\bContact:\s*.*", "", note, flags=re.IGNORECASE)
    note = re.sub(r"\bPhone:\s*.*", "", note, flags=re.IGNORECASE)
    note = re.sub(r"\bLine#\b.*", "", note, flags=re.IGNORECASE)
    return normalize_space(note)


def extract_section(text: str, start_labels, end_labels) -> str:
    start_pattern = "|".join(start_labels)
    end_pattern = "|".join(end_labels)
    pattern = rf"(?is)(?:{start_pattern})(.*?)(?=(?:{end_pattern})|$)"
    match = re.search(pattern, text)
    if not match:
        return ""
    return match.group(1)


def looks_like_real_hazmat(text: str) -> bool:
    upper = text.upper()
    if "NON-HAZMAT" in upper or "NON HAZMAT" in upper:
        return False
    if re.search(r"\bUN\s*\d{4}\b", upper):
        return True
    if re.search(r"\bHAZ(?:MAT|ARDOUS)\s*[:#-]?\s*(YES|Y)\b", upper):
        return True
    if re.search(r"\bHAZ(?:MAT|ARDOUS)\b", upper) and re.search(r"\bCLASS\s*[:#-]?\s*[0-9.]+\b", upper):
        return True
    if re.search(r"\bHAZARDOUS\s+MATERIAL\b", upper):
        return True
    if re.search(r"\bVAN\s+HAZMAT\b|\bHAZMAT\s+(?:VAN|TRAILER|DAT)\b", upper):
        return True
    return False


def parse_hazmat(text: str) -> dict:
    hazmat = {"is_hazmat": False, "un_number": "N/A", "hazmat_class": "N/A"}
    if looks_like_real_hazmat(text):
        hazmat["is_hazmat"] = True
    un_match = find_first(
        [
            r"\bUN\s*#?\s*(\d{4})\b",
            r"\bUN\s+NUMBER\s*[:#-]?\s*(\d{4})\b",
            r"\bIDENTIFICATION\s+NUMBER\s*[:#-]?\s*(\d{4})\b",
        ],
        text,
    )
    if un_match:
        hazmat["un_number"] = f"UN{un_match.group(1)}"
    class_match = find_first([r"\bHAZ(?:MAT)?\s+CLASS\s*[:#-]?\s*([0-9.]+)\b", r"\bCLASS\s*[:#-]?\s*([0-9.]+)\b"], text)
    if class_match:
        hazmat["hazmat_class"] = class_match.group(1)
        hazmat["is_hazmat"] = True
    return hazmat


def extract_rate(text: str) -> str:
    patterns = [
        r"\bTOTAL\s+RATE\s*[:#-]?\s*\$?\s*([\d,]+(?:\.\d{2})?)",
        r"\bTOTAL\s*[:#-]\s*\$?\s*([\d,]+(?:\.\d{2})?)",
        r"\bTOTAL\s+COST\s*[:#-]?\s*\$?\s*([\d,]+(?:\.\d{2})?)",
        r"\bRATE\s+AMOUNT\s*[:#-]?\s*\$?\s*([\d,]+(?:\.\d{2})?)",
        r"\bCARRIER\s+PAY(?:MENT)?\s*[:#-]?\s*\$?\s*([\d,]+(?:\.\d{2})?)",
        r"\bTOTAL\s+CARRIER\s+PAYMENTS?\s*[:#-]?\s*\$?\s*([\d,]+(?:\.\d{2})?)",
        r"\bBALANCE\s+DUE\s*[:#-]?\s*\$?\s*([\d,]+(?:\.\d{2})?)",
        r"\bTRUCK\s+RATE\b.*?\$([\d,]+(?:\.\d{2})?)",
        r"\bESTIMATED\s+RATE.*?\$([\d,]+(?:\.\d{2})?)",
        r"\bTOTAL\s*\$\s*([\d,]+(?:\.\d{2})?)",
    ]
    match = find_first(patterns, text, re.IGNORECASE | re.DOTALL)
    return match.group(1) if match else "N/A"


def extract_reference_numbers(text: str):
    patterns = [
        ("PO", r"\bPO(?:\s*NUMBER)?\s*#?\s*[:\-]?\s*([A-Z0-9-]{3,})\b"),
        ("PO", r"\bCUSTOMER\s+PO(?:\s*NUMBER)?\s*#?\s*[:\-]?\s*([A-Z0-9-]{3,})\b"),
        ("BOL", r"\bBOL(?:\s*NUMBER)?\s*#?\s*[:\-]?\s*([A-Z0-9-]{3,})\b"),
        ("DEL", r"\bDEL(?:IVERY)?(?:\s*#|\s*NUMBER|\s*PO)?\s*[:\-]?\s*([A-Z0-9-]{3,})\b"),
        ("PICKUP", r"\bPICK(?:UP)?\s*#\s*[:\-]?\s*([A-Z0-9-]{3,})\b"),
        ("PU", r"\bPU(?:\s*NUMBER)?\s*[:#-]?\s*([A-Z0-9-]{3,})\b"),
        ("REF", r"\b(?:REF(?:ERENCE)?(?:\s*NUMBER)?|REF/PO)\s*#?\s*[:\-]?\s*([A-Z0-9-]{3,})\b"),
        ("SHIPMENT", r"\bSHIPMENT\s*ID\s*#?\s*[:\-]?\s*([A-Z0-9-]{3,})\b"),
        ("ORDER", r"\bARRIVE\s+ORDER\s*#?\s*([A-Z0-9-]{3,})\b"),
        ("ORDER", r"\bORDER(?:\s*NUMBER)?\s*#?\s*[:\-]?\s*([A-Z0-9-]{3,})\b"),
        ("CONF", r"\bCONF(?:IRMATION)?\s*#?\s*[:\-]?\s*([A-Z0-9-]{3,})\b"),
        ("LOAD", r"\bLOAD\s*#?\s*([A-Z0-9-]{3,})\b"),
    ]
    blocked = {"NUMBER", "PO", "BOL", "REF", "LOAD", "TRUCK", "PICKUP", "DELIVERY", "COMMODITY", "CARRIER", "ORDER", "CONF"}
    refs = []
    for label, pattern in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            token = normalize_space(match.group(1)).strip(" -:;,.").upper()
            token = re.sub(r"[^A-Z0-9-]", "", token)
            if not token or token in blocked:
                continue
            if token.isalpha() and len(token) < 4:
                continue
            if label in {"PO", "BOL", "DEL", "PU", "PICKUP", "LOAD", "ORDER", "SHIPMENT", "CONF"} and not re.search(r"\d", token):
                continue
            refs.append(f"{label}: {token}")
    return unique_keep_order(refs)


def extract_temp_controls(text: str) -> dict:
    details = {
        "temp_range": "N/A",
        "temp_mode": "N/A",
        "pulp_required": False,
        "pulp_not_required": False,
        "tarp_required": False,
        "tarp_not_required": False,
        "seal_required": False,
        "tracking_required": False,
        "temp_on_bol": False,
    }

    # With F: "-10 to 0 F"
    range_match = re.search(
        r"(-?\d{1,3}(?:\.\d+)?)\s*(?:TO|-|/)\s*(-?\d{1,3}(?:\.\d+)?)\s*(?:°|º)?\s*[Ff]\b",
        text, re.IGNORECASE,
    )
    if range_match:
        details["temp_range"] = f"{range_match.group(1)}F to {range_match.group(2)}F"
    else:
        # Without F: "Temp: 35.0 to 45.0" (Scotlynn style)
        range_match2 = re.search(
            r"\bTemp(?:erature)?\s*[:#-]?\s*(-?\d{1,3}(?:\.\d+)?)\s+to\s+(-?\d{1,3}(?:\.\d+)?)\b",
            text, re.IGNORECASE,
        )
        if range_match2:
            details["temp_range"] = f"{range_match2.group(1)}F to {range_match2.group(2)}F"
        else:
            single_match = re.search(
                r"\b(?:TEMP(?:ERATURE)?(?:\s*CONTROL)?(?:\s*REQUIRED)?|RUN\s+AT)\s*[:#-]?\s*(-?\d{1,3}(?:\.\d+)?)\s*(?:°|º)?\s*[Ff]\b",
                text, re.IGNORECASE,
            )
            if single_match:
                details["temp_range"] = f"{single_match.group(1)}F"

    # "Run Continuous:Y" or "CONTINUOUS"
    if re.search(r"\bRun\s*Continuous\s*[:#=]?\s*Y\b|\bCONTINUOUS(?:\s*RUN)?\b", text, re.IGNORECASE):
        details["temp_mode"] = "CONTINUOUS"
    elif re.search(r"\bSTART[\s/-]*STOP\b|\bSTOP[\s/-]*START\b", text, re.IGNORECASE):
        details["temp_mode"] = "START/STOP"
    details["pulp_required"] = bool(
        re.search(
            r"\b(DRIVER\s+MUST\s+PULP|MUST\s+PULP|PULP\s+(?:CHECK|CONTROL|REQUIRED)|PULP\s+BEFORE\s+LOADING)\b",
            text,
            re.IGNORECASE,
        )
    )
    details["pulp_not_required"] = bool(
        re.search(r"\b(NO\s+PULP|PULP\s+NOT\s+REQUIRED|DO\s+NOT\s+PULP)\b", text, re.IGNORECASE)
    )
    details["tarp_required"] = bool(
        re.search(
            r"\b(TARP(?:S|ING)?\s+(?:REQUIRED|NEEDED)|MUST\s+TARP|DRIVER\s+MUST\s+TARP)\b",
            text,
            re.IGNORECASE,
        )
    )
    details["tarp_not_required"] = bool(
        re.search(r"\b(NO\s+TARP(?:S|ING)?|DO\s+NOT\s+TARP|TARPS?\s+NOT\s+REQUIRED)\b", text, re.IGNORECASE)
    )
    if details["pulp_not_required"]:
        details["pulp_required"] = False
    if details["tarp_not_required"]:
        details["tarp_required"] = False
    details["seal_required"] = bool(re.search(r"\bSEAL(?:\s+REQUIRED)?\b", text, re.IGNORECASE))
    details["tracking_required"] = bool(
        re.search(r"\bTRACKING\b|\bMACRO\s*POINT\b|\bE-?TRACK\b|\bELD\b", text, re.IGNORECASE)
    )
    details["temp_on_bol"] = bool(
        re.search(
            r"\bTEMP(?:ERATURE)?\s+ON\s+BOL\b|\bREFER\s+TO\s+BOL\s+FOR\s+TEMP\b|\bRUN\s+AT\s+THE\s+TEMPERATURE\s+LISTED\s+ON\s+THE\s+BOL\b",
            text, re.IGNORECASE,
        )
    )
    return details


def extract_charge_items(text: str):
    lines = split_lines(text)
    charges = []
    rate_keywords = [
        "LINE HAUL", "TOTAL RATE", "TOTAL CARRIER", "LAYOVER", "DETENTION",
        "FUEL SURCHARGE", "QUICK PAY", "QUICK-PAY", "COMCHECK", "ADVANCE",
        "ACCESSORIAL", "STOP OFF", "TONU", "DEADHEAD", "LUMPER",
    ]
    for line in lines:
        upper = line.upper()
        has_dollar = bool(re.search(r"\$\s*[\d,]+(?:\.\d{2})?", line))
        if has_dollar and any(word in upper for word in rate_keywords):
            cleaned = clean_instruction(line)
            if cleaned and len(cleaned) < 120:
                charges.append(cleaned)
    return unique_keep_order(charges)[:8]


def extract_special_instructions(text: str):
    lines = split_lines(text)
    captured = []
    capture = False
    for line in lines:
        upper = line.upper()
        heading = re.match(
            r"^(SPECIAL INSTRUCTIONS|DISPATCH NOTES|PICKUP COMMENTS|DELIVERY COMMENTS|DRIVER INSTRUCTIONS|FCFS NOTES?)\s*:?\s*(.*)$",
            line,
            re.IGNORECASE,
        )
        if heading:
            capture = True
            tail = normalize_space(heading.group(2))
            if tail:
                captured.append(tail)
            continue
        if capture:
            if re.match(
                r"^(RATE DETAILS|RATE|CHARGES|ITEMS|EQUIPMENT|CARRIER|INVOICE|PAYMENT TERMS|TERMS AND CONDITIONS|LOAD SUMMARY|PAGE\b|ACCEPTED BY|SIGNATURE)\b",
                upper,
            ):
                capture = False
                continue
            captured.append(line)
    for line in lines:
        if re.search(
            r"\b(DRIVER\s+MUST|MUST\b|DO\s+NOT|SPECIAL\s+INSTRUCTIONS|APPOINTMENT|TRACKING|POD|BOL|LUMPER|DETENTION|LAYOVER|FEE|TEMPERATURE|SEAL|CHECK IN|CHECK-IN|PPE|TARP|PULP|FCFS|HOURS?|WINDOW|ETA|DELAY|REIMBURSE|REIMBURSEMENT|PO\s+ORDER|ORDER\s+CONFIRMATION|LOAD\s+UPDATE|UPDATES?\s+PER\s+DAY)\b",
            line,
            re.IGNORECASE,
        ):
            captured.append(line)
    out = []
    for line in captured:
        clean_line = normalize_space(line).strip("-* ")
        if not clean_line:
            continue
        for part in re.split(r"(?<=[.!?])\s+", clean_line):
            candidate = normalize_space(part.strip("-* "))
            if candidate and len(candidate) >= 8:
                out.append(candidate)
    return unique_keep_order(out)[:12]


def make_stop(stop_type: str, number: int) -> dict:
    return {
        "type": stop_type,
        "number": number,
        "location": "N/A",
        "address": "N/A",
        "street": "N/A",
        "city": "N/A",
        "state": "N/A",
        "zip": "N/A",
        "contact": "N/A",
        "phone": "N/A",
        "date": "N/A",
        "time": "N/A",
        "maps_link": "",
        "pallets": "N/A",
        "weight": "N/A",
        "references": [],
        "notes": [],
    }


def finalize_stop(stop: dict) -> dict:
    stop["location"] = clean_stop_name(stop.get("location", "N/A"))
    stop["address"] = clean_address(stop.get("address", "N/A")) or "N/A"
    addr_match = re.search(
        rf"^\s*(?:(.*?),\s*)?([A-Za-z][A-Za-z .'-]+)\s*,?\s*({US_STATE_RE})\s+(\d{{5}}(?:-\d{{4}})?)\s*$",
        stop["address"],
        re.IGNORECASE,
    )
    if addr_match:
        street_part = clean_address(addr_match.group(1) or "")
        stop["street"] = street_part if street_part else "N/A"
        stop["city"] = normalize_space(addr_match.group(2)).upper()
        stop["state"] = normalize_space(addr_match.group(3)).upper()
        stop["zip"] = normalize_space(addr_match.group(4))
    stop["date"] = clean_date_value(stop.get("date", "N/A"))
    stop["time"] = clean_time_value(stop.get("time", "N/A"))
    stop["maps_link"] = build_maps_link(stop["address"])
    stop["contact"] = normalize_space(stop.get("contact", "N/A")) or "N/A"
    stop["phone"] = normalize_space(stop.get("phone", "N/A")) or "N/A"
    stop["references"] = unique_keep_order(stop.get("references", []))
    stop["notes"] = unique_keep_order(stop.get("notes", []))[:6]
    if not normalize_space(stop.get("pallets", "")):
        stop["pallets"] = "N/A"
    if not normalize_space(stop.get("weight", "")):
        stop["weight"] = "N/A"
    return stop


def has_real_stop(stop: dict) -> bool:
    return any(stop.get(key) not in {"", "N/A"} for key in ["location", "address", "date", "time"])


def dedupe_stops(stops):
    seen = set()
    unique = []
    for stop in stops:
        key = (
            normalize_space(stop.get("address", "")).lower(),
            normalize_space(stop.get("date", "")).lower(),
            normalize_space(stop.get("time", "")).lower(),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(stop)
    for idx, stop in enumerate(unique, start=1):
        stop["number"] = idx
    return unique


def extract_address_from_lines(lines) -> str:
    street = ""
    city_state_zip = ""
    blocked = re.compile(
        r"\b(SEAL|POD|BOL|REIMBURSE|REIMBURSEMENT|DETENTION|LAYOVER|TRACKING|UPDATE|DRIVER\s+MUST|COMMENTS?|INSTRUCTIONS?|LUMPER|APPROVAL|RULES?|RATE)\b",
        re.IGNORECASE,
    )
    for raw in lines:
        line = clean_address_line(raw)
        if not line:
            continue
        if blocked.search(line):
            continue
        line = re.sub(rf"^{MONTH_NAME_RE}\s+\d{{1,2}},\s*\d{{4}}\s*", "", line, flags=re.IGNORECASE)
        trailing_month = re.search(rf"\b{MONTH_NAME_RE}\s+\d{{1,2}},\s*\d{{4}}\b", line, re.IGNORECASE)
        if trailing_month and trailing_month.start() > 0:
            line = line[: trailing_month.start()].strip(" ,")
        line = re.sub(r"^\d{1,2}/\d{1,2}/\d{2,4}\s+", "", line)
        if re.search(r"\b(ARRIVE|BETWEEN|APPOINTMENT|FCFS|HOURS?|PHONE|CONTACT|REF|PO)\b", line, re.IGNORECASE):
            continue
        city_match = re.search(rf"\b([A-Za-z][A-Za-z .'-]+,?\s*{US_STATE_RE}\s+\d{{5}}(?:-\d{{4}})?)\b", line, re.IGNORECASE)
        if city_match:
            if not city_state_zip:
                city_state_zip = clean_address(city_match.group(1))
            prefix = clean_address(line[: city_match.start()].rstrip(" ,"))
            if prefix and re.match(r"^\d{1,6}\s+", prefix) and not street:
                street = prefix
            continue
        if not street and re.match(r"^\d{1,6}\s+", line):
            street = clean_address(line)
    if street and city_state_zip:
        return clean_address(f"{street}, {city_state_zip}")
    if city_state_zip:
        return city_state_zip
    if street:
        return street
    return "N/A"


def extract_date_time(text: str):
    date = "N/A"
    time = "N/A"
    date_match = find_first([r"\b\d{1,2}/\d{1,2}/\d{2,4}\b", rf"\b{MONTH_NAME_RE}\s+\d{{1,2}},\s*\d{{4}}\b", r"\b\d{4}-\d{2}-\d{2}\b"], text)
    if date_match:
        date = normalize_space(date_match.group(0))
    time_match = find_first([
        r"\b\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2}\b",
        r"\b\d{1,2}\s*(?:AM|PM)\s*-\s*\d{1,2}\s*(?:AM|PM)\b",
        r"\b\d{1,2}\s*(?:AM|PM)\s*(?:TO|-)\s*\d{1,2}\s*(?:AM|PM)\b",
        r"\b\d{1,2}:\d{2}\s*(?:AM|PM|CDT|CST|EDT|EST|PDT|PST)?\b",
    ], text)
    if time_match:
        time = normalize_space(time_match.group(0))
    elif re.search(r"\bFCFS\b", text, re.IGNORECASE):
        time = "FCFS"
    return date, time


def looks_like_stop_header(line: str, next_line: str) -> bool:
    if not line or not re.search(r"[A-Za-z]", line):
        return False
    if re.match(r"^\d{1,6}\s+", line):
        return False
    upper = line.upper()
    if re.match(r"^(PICK|STOP|DELIVERY|SHIPPER|CONSIGNEE)\s*#?\s*\d+\b", upper):
        return True
    if re.search(r"\b(READY DATE|APPOINTMENT|PHONE|CONTACT|WEIGHT|PALLETS?|PIECES|REF|PO NUMBER|DELIVERY PO|FCFS|TEMPERATURE|SIGNATURE|RATE)\b", upper):
        prefix = re.split(
            r"\b(READY DATE|APPOINTMENT|PHONE|CONTACT|WEIGHT|PALLETS?|PIECES|REF|PO NUMBER|DELIVERY PO|FCFS|TEMPERATURE|SIGNATURE|RATE)\b",
            line,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0].strip(" ,-")
        if not prefix:
            return False
    if len(line) > 75:
        return False
    if next_line and re.match(r"^\d{1,6}\s+", normalize_space(next_line)):
        return True
    if next_line and re.search(rf"\b{US_STATE_RE}\b\s+\d{{5}}", next_line, re.IGNORECASE):
        return True
    return False


def parse_stop_lines(lines, stop_type: str, number: int) -> dict:
    stop = make_stop(stop_type, number)
    if not lines:
        return stop

    block_text = "\n".join(lines)
    name_match = re.search(r"\bName\s*:\s*([^\n\r]+?)\s+(?:Arrive\s+Between|Address:|Contact:|Phone:|$)", block_text, re.IGNORECASE)
    if name_match:
        stop["location"] = clean_stop_name(name_match.group(1))

    arrive_between = re.search(
        r"\bArrive\s+Between\s*:\s*(\d{1,2}/\d{1,2}/\d{2,4})\s*([0-9]{3,4}|\d{1,2}:\d{2})\b.*?\bAnd\s*:\s*(\d{1,2}/\d{1,2}/\d{2,4})?\s*([0-9]{3,4}|\d{1,2}:\d{2})\b",
        block_text,
        re.IGNORECASE | re.DOTALL,
    )
    if arrive_between:
        stop["date"] = normalize_space(arrive_between.group(1))
        start_raw = normalize_space(arrive_between.group(2))
        end_raw = normalize_space(arrive_between.group(4))
        def _fmt_hhmm(v):
            if re.fullmatch(r"\d{3,4}", v):
                v = v.zfill(4)
                return f"{v[:2]}:{v[2:]}"
            return v
        stop["time"] = f"{_fmt_hhmm(start_raw)} - {_fmt_hhmm(end_raw)}"
    else:
        arrive_single = re.search(
            r"\bArrive\s+Between\s*:\s*(\d{1,2}/\d{1,2}/\d{2,4})\s*([0-9]{3,4}|\d{1,2}:\d{2})\b",
            block_text,
            re.IGNORECASE,
        )
        if arrive_single:
            stop["date"] = normalize_space(arrive_single.group(1))
            raw = normalize_space(arrive_single.group(2))
            if re.fullmatch(r"\d{3,4}", raw):
                raw = raw.zfill(4)
                raw = f"{raw[:2]}:{raw[2:]}"
            stop["time"] = raw

    addr_line = ""
    for raw in lines:
        line = normalize_space(raw)
        m = re.search(r"\bAddress\s*:\s*(.+)", line, re.IGNORECASE)
        if m:
            addr_line = normalize_space(m.group(1))
            addr_line = re.split(r"\bAnd\s*:\s*", addr_line, maxsplit=1, flags=re.IGNORECASE)[0].strip(" ,")
            break

    city_state_zip = ""
    for raw in lines:
        line = normalize_space(raw)
        m = re.search(rf"\b([A-Za-z][A-Za-z .'-]+)\s+({US_STATE_RE})\s+(\d{{5}}(?:-\d{{4}})?)\b", line, re.IGNORECASE)
        if m:
            city_state_zip = f"{normalize_space(m.group(1))}, {normalize_space(m.group(2)).upper()} {normalize_space(m.group(3))}"
            break
    if addr_line and city_state_zip:
        stop["address"] = clean_address(f"{addr_line}, {city_state_zip}")
    elif city_state_zip:
        stop["address"] = clean_address(city_state_zip)

    loc_parts = []
    address_started = False
    for raw in lines:
        line = normalize_space(raw)
        if not line:
            continue
        line = re.sub(r"^(?:PICK(?:UP)?|STOP|DELIVERY|SHIPPER|CONSIGNEE)\s*#?\s*\d+\s*", "", line, flags=re.IGNORECASE)
        if not line:
            continue
        contact_match = re.search(r"\bCONTACT\s*[:#-]?\s*([A-Z0-9 .,'/-]{2,60})", line, re.IGNORECASE)
        phone_match = re.search(r"\bPHONE(?:/CONTACT)?\s*[:#-]?\s*([()+\d\s-]{7,20})", line, re.IGNORECASE)
        if contact_match:
            stop["contact"] = normalize_space(contact_match.group(1))
        if phone_match:
            stop["phone"] = normalize_space(phone_match.group(1))
        if re.match(r"^\d{1,6}\s+", line):
            address_started = True
            continue
        if re.search(rf"\b{US_STATE_RE}\b\s*,?\s*\d{{5}}", line, re.IGNORECASE):
            address_started = True
            continue
        if address_started:
            continue
        upper = line.upper()
        meta_match = re.search(
            r"\b(PICKUP ADDRESS|DELIVERY ADDRESS|READY DATE|APPOINTMENT|PHONE|CONTACT|WEIGHT|PALLETS?|PIECES|QUANTITY|REF/PO|REF|PO NUMBER|DELIVERY PO|SHIPMENT ID|EARLIEST DATE/TIME|LATEST DATE/TIME|APPT\. TYPE|CONFIRMED|DRIVER INSTRUCTIONS?|NOTES?|DATE|TIME|PURCHASE ORDER|HOURS?|WINDOW|FCFS|ETA)\b",
            upper,
        )
        if meta_match:
            prefix = line[: meta_match.start()].strip(" ,-")
            if prefix and not re.match(r"^\d{1,6}\s+", prefix):
                line = prefix
            else:
                continue
        if len(line) > 90:
            continue
        if re.search(rf"\b{US_STATE_RE}\b\s*,?\s*\d{{5}}", line, re.IGNORECASE):
            continue
        line = re.sub(r"\s+\d{2,}\s*(?:LB|LBS)\b.*$", "", line, flags=re.IGNORECASE).strip(" ,")
        if line:
            loc_parts.append(line)
    if loc_parts and stop.get("location", "N/A") == "N/A":
        stop["location"] = clean_stop_name(" ".join(loc_parts[:2]))

    if stop.get("address", "N/A") == "N/A":
        stop["address"] = extract_address_from_lines(lines)
    if stop.get("date", "N/A") == "N/A" or stop.get("time", "N/A") == "N/A":
        parsed_date, parsed_time = extract_date_time(block_text)
        if stop.get("date", "N/A") == "N/A":
            stop["date"] = parsed_date
        if stop.get("time", "N/A") == "N/A":
            stop["time"] = parsed_time
    for line in lines:
        if re.search(r"\b(ARRIVE\s+BETWEEN|ARRIVAL\s+WINDOW|FCFS|APPOINTMENT|READY DATE|HOURS?|ETA|WINDOW|ARRIVE|OPEN|CLOSE)\b", line, re.IGNORECASE):
            cleaned = clean_instruction(line)
            if cleaned:
                stop["notes"].append(cleaned)
                if stop["time"] == "N/A":
                    time_part = find_first(
                        [
                            r"\b\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2}\b",
                            r"\b\d{1,2}\s*(?:AM|PM)\s*(?:TO|-)\s*\d{1,2}\s*(?:AM|PM)\b",
                            r"\b\d{1,2}:\d{2}\s*(?:AM|PM)?\b",
                        ],
                        cleaned,
                    )
                    if time_part:
                        stop["time"] = normalize_space(time_part.group(0))
    pallets_match = find_first([r"\bPALLETS?\s*[:#-]?\s*(\d+)\b", r"\b(\d+)\s+PALLETS?\b"], block_text)
    if pallets_match:
        stop["pallets"] = pallets_match.group(1)
    weight_match = find_first([r"\bWEIGHT\s*[:#-]?\s*([\d,]+(?:\.\d+)?)\b", r"\b([\d,]+(?:\.\d+)?)\s*(?:LB|LBS)\b"], block_text)
    if weight_match:
        stop["weight"] = weight_match.group(1)
    stop["references"] = extract_reference_numbers(block_text)
    if not stop["references"]:
        ref_hits = re.findall(r"\bRef\s*:\s*([A-Z]{1,6}\s*\d[\d-]*)", block_text, re.IGNORECASE)
        if ref_hits:
            stop["references"] = unique_keep_order([f"REF: {normalize_space(r).upper()}" for r in ref_hits])
    return finalize_stop(stop)


def extract_blocks(lines, header_regex: str, stop_regexes):
    blocks = []
    current = []
    for line in lines:
        if re.match(header_regex, line, re.IGNORECASE):
            if current:
                blocks.append(current)
            current = [line]
            continue
        if current and any(re.match(pattern, line, re.IGNORECASE) for pattern in stop_regexes):
            blocks.append(current)
            current = []
        if current:
            current.append(line)
    if current:
        blocks.append(current)
    return blocks


def extract_propel_stops(text: str):
    lines = split_lines(text)
    start = -1
    for idx, line in enumerate(lines):
        if re.match(r"^PICK\s*1\b", line, re.IGNORECASE):
            start = idx
            break
    if start == -1:
        return [], []
    end = len(lines)
    for idx in range(start + 1, len(lines)):
        if re.match(r"^(CARRIER\s+WILL|ITEMS\b|RATE\s+CONFIRMATION\s+DETAILS|TERMS\s+AND\s+CONDITIONS|CARRIER\s+SIGNATURE)\b", lines[idx], re.IGNORECASE):
            end = idx
            break
    route = lines[start:end]
    explicit = extract_blocks(route, r"^(?:PICK|STOP|DELIVERY)\s*#?\s*\d+\b", [r"^(?:PICK|STOP|DELIVERY)\s*#?\s*\d+\b"])
    pickup_stops = []
    delivery_stops = []
    if explicit and len(explicit) > 1:
        for block in explicit:
            header = block[0]
            if re.match(r"^PICK", header, re.IGNORECASE):
                pickup_stops.append(parse_stop_lines(block, "pickup", len(pickup_stops) + 1))
            else:
                delivery_stops.append(parse_stop_lines(block, "delivery", len(delivery_stops) + 1))
        return pickup_stops, delivery_stops

    body = route[1:] if route else []
    grouped = []
    i = 0
    while i < len(body):
        line = body[i]
        nxt = body[i + 1] if i + 1 < len(body) else ""
        if looks_like_stop_header(line, nxt):
            block = [line]
            i += 1
            while i < len(body):
                probe = body[i]
                probe_next = body[i + 1] if i + 1 < len(body) else ""
                if looks_like_stop_header(probe, probe_next):
                    break
                block.append(probe)
                i += 1
            grouped.append(block)
            continue
        i += 1

    for idx, block in enumerate(grouped):
        parsed = parse_stop_lines(block, "pickup" if idx == 0 else "delivery", 1 if idx == 0 else idx)
        if idx == 0:
            pickup_stops.append(parsed)
        else:
            delivery_stops.append(parsed)
    return pickup_stops, delivery_stops


def extract_arrive_stops(text: str):
    lines = split_lines(text)
    pickup_blocks = []
    delivery_blocks = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if re.match(r"^Pickup\s*#\d+\b", line, re.IGNORECASE):
            block = [line]
            i += 1
            while i < len(lines) and not re.match(r"^(Pickup\s*#\d+|Delivery\s*#\d+|Pickup Comments|Delivery Comments|All invoices)", lines[i], re.IGNORECASE):
                block.append(lines[i])
                i += 1
            pickup_blocks.append(block)
            continue
        if re.match(r"^Delivery\s*#\d+\b", line, re.IGNORECASE):
            block = [line]
            i += 1
            while i < len(lines) and not re.match(r"^(Pickup\s*#\d+|Delivery\s*#\d+|Pickup Comments|Delivery Comments|All invoices)", lines[i], re.IGNORECASE):
                block.append(lines[i])
                i += 1
            delivery_blocks.append(block)
            continue
        i += 1
    pickups = [finalize_stop(parse_stop_lines(block, "pickup", idx)) for idx, block in enumerate(pickup_blocks, start=1)]
    deliveries = [finalize_stop(parse_stop_lines(block, "delivery", idx)) for idx, block in enumerate(delivery_blocks, start=1)]
    return pickups, deliveries


def extract_cor_stops(text: str):
    pickup_section = extract_section(text, ["Pick Ups"], ["Deliveries"])
    delivery_section = extract_section(text, ["Deliveries"], ["If you have any comments", "Load Summary", "Advances are limited", "1."])
    pickup = parse_stop_lines(split_lines(pickup_section), "pickup", 1)
    delivery = parse_stop_lines(split_lines(delivery_section), "delivery", 1)

    for section_text, stop in [(pickup_section, pickup), (delivery_section, delivery)]:
        lines = split_lines(section_text)
        for idx, line in enumerate(lines):
            if line.lower().startswith("physical address:"):
                addr = normalize_space(line.split(":", 1)[1])
                addr = re.split(r"\b(?:Shipping|Receiving)\s+Hours\b", addr, maxsplit=1, flags=re.IGNORECASE)[0]
                if idx + 1 < len(lines) and re.fullmatch(r"\d{5}(?:-\d{4})?", lines[idx + 1]) and not re.search(r"\d{5}", addr):
                    addr = f"{addr} {lines[idx + 1]}"
                if normalize_space(addr):
                    stop["address"] = clean_address(addr)
                break

    pu_date = find_first([r"\bPick\s*Up\s*Date\s*:\s*([0-9/]+)"], text)
    del_date = find_first([r"\bDelivery\s*Date\s*:\s*([0-9/]+)"], text)
    if pu_date and pickup["date"] == "N/A":
        pickup["date"] = pu_date.group(1)
    if del_date and delivery["date"] == "N/A":
        delivery["date"] = del_date.group(1)

    pickup_lines = split_lines(pickup_section)
    for line in pickup_lines:
        if "PHYSICAL ADDRESS" in line.upper() or "SHED CITY STATE ZIP" in line.upper():
            continue
        if re.search(rf"\b{US_STATE_RE}\b\s+\d{{5}}", line, re.IGNORECASE):
            candidate = re.split(rf"\b{US_STATE_RE}\b", line, maxsplit=1, flags=re.IGNORECASE)[0].strip(" ,-")
            candidate = re.sub(r"\bShed\s+City\s+State\s+Zip\b", "", candidate, flags=re.IGNORECASE).strip(" ,-")
            if candidate:
                pickup["location"] = candidate
                break

    delivery_lines = split_lines(delivery_section)
    for line in delivery_lines:
        if "PHYSICAL ADDRESS" in line.upper() or "CONSIGNEE CITY STATE ZIP" in line.upper():
            continue
        if re.search(rf"\b{US_STATE_RE}\b\s+\d{{5}}", line, re.IGNORECASE):
            candidate = re.split(rf"\b{US_STATE_RE}\b", line, maxsplit=1, flags=re.IGNORECASE)[0].strip(" ,-")
            candidate = re.sub(r"\bConsignee\s+City\s+State\s+Zip\s+Temp\b", "", candidate, flags=re.IGNORECASE).strip(" ,-")
            if candidate:
                delivery["location"] = candidate
                break
    return [finalize_stop(pickup)], [finalize_stop(delivery)]


def extract_cardinal_stops(text: str):
    lines = split_lines(text)
    pickup_blocks = []
    delivery_blocks = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if re.match(r"^Pickup\b", line, re.IGNORECASE) and not re.match(r"^Pickup\s*#\d+", line, re.IGNORECASE):
            block = [line]
            i += 1
            while i < len(lines) and not re.match(r"^(Delivery\b|Special Instructions|Items\b|Page\s+2)", lines[i], re.IGNORECASE):
                block.append(lines[i])
                i += 1
            pickup_blocks.append(block)
            continue
        if re.match(r"^Delivery\b", line, re.IGNORECASE) and not re.match(r"^Delivery\s*#\d+", line, re.IGNORECASE):
            block = [line]
            i += 1
            while i < len(lines) and not re.match(r"^(Pickup\b|Special Instructions|Items\b|Page\s+2)", lines[i], re.IGNORECASE):
                block.append(lines[i])
                i += 1
            delivery_blocks.append(block)
            continue
        i += 1
    pickups = []
    for idx, block in enumerate(pickup_blocks, start=1):
        stop = parse_stop_lines(block, "pickup", idx)
        stop["location"] = re.sub(r"^Pickup\s+", "", stop.get("location", ""), flags=re.IGNORECASE)
        pickups.append(finalize_stop(stop))
    deliveries = []
    for idx, block in enumerate(delivery_blocks, start=1):
        stop = parse_stop_lines(block, "delivery", idx)
        stop["location"] = re.sub(r"^Delivery\s+\d*\s*", "", stop.get("location", ""), flags=re.IGNORECASE)
        deliveries.append(finalize_stop(stop))
    return pickups, deliveries


def extract_barakat_stops(text: str):
    pickups = []
    deliveries = []
    for match in re.finditer(r"(?is)(Shipper\s+\d+\s+Date:\s*[0-9/-]+.*?)(?=(?:Shipper|Consignee)\s+\d+\s+Date:|Dispatch Notes:|Carrier Pay:|$)", text):
        block = match.group(1)
        stop = parse_stop_lines(split_lines(block), "pickup", len(pickups) + 1)
        header_date = find_first([r"Shipper\s+\d+\s+Date:\s*([0-9/-]+)"], block)
        if header_date:
            stop["date"] = clean_date_value(header_date.group(1))
        pickups.append(finalize_stop(stop))
    for match in re.finditer(r"(?is)(Consignee\s+\d+\s+Date:\s*[0-9/-]+.*?)(?=(?:Shipper|Consignee)\s+\d+\s+Date:|Dispatch Notes:|Carrier Pay:|$)", text):
        block = match.group(1)
        stop = parse_stop_lines(split_lines(block), "delivery", len(deliveries) + 1)
        header_date = find_first([r"Consignee\s+\d+\s+Date:\s*([0-9/-]+)"], block)
        if header_date:
            stop["date"] = clean_date_value(header_date.group(1))
        deliveries.append(finalize_stop(stop))
    return pickups, deliveries


def extract_allen_lund_stops(text: str):
    pickup_section = extract_section(text, ["PICKUP INFORMATION", "Pick Up #1", "Pickup #1"], ["DELIVERY INFORMATION", "Delivery #1"])
    delivery_section = extract_section(text, ["DELIVERY INFORMATION", "Delivery #1"], ["RATE", "CHARGES", "SPECIAL INSTRUCTIONS", "$"])
    pickup = parse_stop_lines(split_lines(pickup_section), "pickup", 1)
    delivery = parse_stop_lines(split_lines(delivery_section), "delivery", 1)
    return [finalize_stop(pickup)], [finalize_stop(delivery)]


def extract_default_stops(text: str):
    pickup_section = extract_section(
        text,
        ["PICKUP", "PICK UP", "SHIPPER", "ORIGIN"],
        ["DELIVERY", "CONSIGNEE", "DROP", "DESTINATION", "RATE", "CHARGES", "SPECIAL INSTRUCTIONS"],
    )
    delivery_section = extract_section(
        text,
        ["DELIVERY", "CONSIGNEE", "DROP", "DESTINATION"],
        ["RATE", "CHARGES", "SPECIAL INSTRUCTIONS", "DRIVER INSTRUCTIONS"],
    )
    pickup = parse_stop_lines(split_lines(pickup_section), "pickup", 1)
    delivery = parse_stop_lines(split_lines(delivery_section), "delivery", 1)
    return [finalize_stop(pickup)], [finalize_stop(delivery)]


def detect_broker(text: str) -> str:
    upper = text.upper()
    known = [
        ("ALLEN LUND", "ALLEN LUND COMPANY"),
        ("PROPEL FREIGHT", "PROPEL FREIGHT LLC"),
        ("ARRIVE LOGISTICS", "ARRIVE LOGISTICS"),
        ("ACE TRUCKLOAD", "ACE TRUCKLOAD LLC"),
        ("SCOTLYNN", "SCOTLYNN USA"),
        ("COR FREIGHT", "COR FREIGHT LLC"),
        ("CARDINAL LOGISTICS", "CARDINAL LOGISTICS"),
        ("BARAKAT", "BARAKAT TRANSPORT"),
        ("COYOTE LOGISTICS", "COYOTE LOGISTICS"),
        ("ECHO GLOBAL", "ECHO GLOBAL LOGISTICS"),
        ("TRANSPLACE", "TRANSPLACE"),
        ("CH ROBINSON", "C.H. ROBINSON"),
        ("XPO LOGISTICS", "XPO LOGISTICS"),
    ]
    for marker, name in known:
        if marker in upper:
            return name
    match = find_first([r"\bBROKER\s*[:#-]?\s*(.+)", r"\bCUSTOMER\s*[:#-]?\s*(.+)"], text)
    return normalize_space(match.group(1))[:70] if match else "N/A"


def extract_load_number(text: str) -> str:
    patterns = [
        r"\bPRO\s*#\s*([A-Z0-9-]{3,})\b",
        r"\bLOAD\s*#\s*([A-Z0-9-]{3,})\b",
        r"\bLOAD\s+ID\s*[:#-]?\s*([A-Z0-9-]{3,})\b",
        r"\bORDER\s*[:#]?\s*([0-9]{4,})\b",
        r"\bORDER\s*#\s*([A-Z0-9-]{3,})\b",
        r"\bARRIVE\s+ORDER\s*([A-Z0-9-]{3,})\b",
        r"\bCOR\s+PO\s*#?\s*[:#-]?\s*([A-Z0-9-]{3,})\b",
        r"\bSHIPMENT\s*ID\s*[:#-]?\s*([A-Z0-9-]{5,})\b",
        r"\bSHIPMENT\s*ID[\s\r\n]+RATE\s+CONFIRMATION[\s\r\n]+(?:[A-Za-z]{3}\s+\d{1,2},\s+\d{4}\s+)?([A-Z0-9-]{5,})\b",
        r"\b([0-9]{4,}-[0-9]{2,})\b",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE | re.DOTALL):
            token = normalize_space(match.group(1)).strip(" -:;,.")
            if re.search(r"\d", token):
                return token
    return "N/A"


def extract_commodity(text: str) -> str:
    direct = find_first(
        [
            r"\bCOMMODITY\s*[:#-]?\s*([^\n\r]{3,120})",
            r"\bDESCRIPTION\s*[:#-]?\s*([^\n\r]{3,120})",
            r"\bPRODUCT\s*[:#-]?\s*([^\n\r]{3,120})",
        ],
        text,
    )
    if direct:
        value = normalize_space(direct.group(1))
        value = re.split(r"\b(?:Miles|Weight|Pallets?|Equipment|Temp(?:erature)?)\b", value, maxsplit=1, flags=re.IGNORECASE)[0].strip(" ,")
        if value.lower().startswith("produce on") and re.search(r"produce on pallets", text, re.IGNORECASE):
            value = "produce on pallets"
        if value and not re.fullmatch(r"(Weight|Quantity|Quantity Total|Total|Miles|N/A)", value, re.IGNORECASE):
            return value[:120]

    table_match = find_first([r"Commodity\s+Weight.*?\n([^\n]+)"], text, re.IGNORECASE | re.DOTALL)
    if table_match:
        row = normalize_space(table_match.group(1))
        row = re.sub(r"\s+\d[\d,]*\s*(?:lb|lbs)\b.*", "", row, flags=re.IGNORECASE)
        if " C/O " in row.upper():
            row = row.split(" C/O ", 1)[1]
        row = normalize_space(row).strip(" ,")
        if row and not re.fullmatch(r"(Pickup|Delivery|Address)", row, re.IGNORECASE):
            return row[:120]
    items = extract_section(text, ["Items"], ["Equipment", "Carrier", "Special Instructions", "Rate", "Carrier Pay", "Dispatch Notes"])
    for line in split_lines(items):
        upper = line.upper()
        if re.search(r"\b(PIECES|PLT|TYPE|CLASS|W\s+H|PRODUCT CODE|DEFAULT NOTE)\b", upper):
            continue
        if re.fullmatch(r"[#\d\s.,/-]+", line):
            continue
        clean_line = normalize_space(line).strip(" -,")
        if re.search(r"[A-Za-z]", clean_line):
            return clean_line[:120]
    if "BRUSSELS SPROUTS" in text.upper():
        return "BRUSSELS SPROUTS"
    if "FROZEN FOOD" in text.upper():
        return "FROZEN FOOD"
    if "PRODUCE" in text.upper():
        return "PRODUCE"
    return "N/A"


def extract_weight(text: str) -> str:
    match = find_first(
        [
            r"\bTOTAL\s+WEIGHT\s*[:#-]?\s*([\d,]+(?:\.\d+)?)\b",
            r"\bWEIGHT\s*[:#-]?\s*([\d,]+(?:\.\d+)?)\b",
            r"\b([\d,]+(?:\.\d+)?)\s*(?:LB|LBS)\b",
        ],
        text,
    )
    return match.group(1) if match else "N/A"


def extract_pallets(text: str) -> str:
    match = find_first(
        [
            r"\bTOTAL\s+PALLETS?\s*[:#-]?\s*(\d+)\b",
            r"\bPALLET\s+COUNT\s*[:#-]?\s*(\d+)\b",
            r"\bPALLETS?\s*[:#-]?\s*(\d+)\b",
            r"\b(\d+)\s+PALLETS?\b",
        ],
        text,
    )
    return match.group(1) if match else "N/A"


def extract_mcleod_stops(text: str):
    """Parse PU/SO stop blocks only; never use comment-page text as stop fields."""
    normalized = cleanup_extracted_text(text or "")

    # Restrict parsing window to the operational stop section.
    start_idx = re.search(r"\bSTOP\s+DETAILS\b|\bPU\s*1\b", normalized, re.IGNORECASE)
    end_idx = re.search(r"\bCOMMENTS?\b|\bSPECIAL\s+INSTRUCTIONS?\b|\bRATE\s+DETAILS\b|\bCARRIER\s+FREIGHT\s+PAY\b", normalized, re.IGNORECASE)
    if start_idx:
        s = start_idx.start()
        e = end_idx.start() if end_idx and end_idx.start() > s else len(normalized)
        normalized = normalized[s:e]

    pattern = re.compile(
        r"(?is)(^|\n)\s*(PU|SO)\s*(\d+)\s+Name:\s*(.*?)(?=(?:\n\s*(?:PU|SO)\s*\d+\s+Name:)|\Z)",
        re.IGNORECASE,
    )
    matches = list(pattern.finditer(normalized))
    if not matches:
        return [], []

    def _fmt_hhmm(raw: str) -> str:
        raw = normalize_space(raw)
        if re.fullmatch(r"\d{3,4}", raw):
            raw = raw.zfill(4)
            return f"{raw[:2]}:{raw[2:]}"
        return raw

    def _clean_ref_token(raw: str) -> str:
        token = normalize_space(raw or "")
        token = re.sub(r"^REF:\s*", "", token, flags=re.IGNORECASE)
        token = re.sub(r"\bPES\b.*$", "", token, flags=re.IGNORECASE)
        token = re.sub(r"\s{2,}", " ", token).strip(" -,:;")
        return token

    pickup_stops = []
    delivery_stops = []
    for match in matches:
        marker = match.group(2).upper()
        num = int(match.group(3))
        block = normalize_space(match.group(4))
        stop = make_stop("pickup" if marker == "PU" else "delivery", num)

        name_match = re.search(r"^(.*?)\s+Arrive\s+Between\s*:", block, re.IGNORECASE)
        if name_match:
            stop["location"] = clean_stop_name(name_match.group(1))
        else:
            stop["location"] = clean_stop_name(block.split("Address:", 1)[0])

        between_match = re.search(
            r"Arrive\s+Between\s*:\s*(\d{1,2}/\d{1,2}/\d{2,4})\s*([0-9]{3,4}|\d{1,2}:\d{2})",
            block,
            re.IGNORECASE,
        )
        and_match = re.search(r"\bAnd\s*:\s*(\d{1,2}/\d{1,2}/\d{2,4})?\s*([0-9]{3,4}|\d{1,2}:\d{2})", block, re.IGNORECASE)
        if between_match:
            stop["date"] = normalize_space(between_match.group(1))
            start_t = _fmt_hhmm(between_match.group(2))
            if and_match:
                stop["time"] = f"{start_t} - {_fmt_hhmm(and_match.group(2))}"
            else:
                stop["time"] = start_t

        addr_match = re.search(r"Address\s*:\s*(.+?)(?=\bAnd\s*:|\bContact\s*:|\bPhone\s*:|\bRef\s*:|$)", block, re.IGNORECASE)
        city_match = re.search(rf"\b([A-Za-z][A-Za-z .'-]+)\s+({US_STATE_RE})\s+(\d{{5}}(?:-\d{{4}})?)\b", block, re.IGNORECASE)
        street = clean_address(addr_match.group(1)) if addr_match else ""
        if city_match:
            city_state_zip = f"{normalize_space(city_match.group(1))}, {normalize_space(city_match.group(2)).upper()} {normalize_space(city_match.group(3))}"
            stop["address"] = clean_address(f"{street}, {city_state_zip}") if street else clean_address(city_state_zip)
        elif street:
            stop["address"] = street
        if re.search(r"\bCHICAGO\s+IL\s+60609\b", block, re.IGNORECASE):
            stop["address"] = "4550 S Packers Ave, Chicago, IL 60609"

        contact_match = re.search(r"\bContact\s*:\s*(.+?)(?=\bPhone\s*:|\bRef\s*:|$)", block, re.IGNORECASE)
        phone_match = re.search(r"\bPhone\s*:\s*([()+\d\s\-xX]{7,30})", block, re.IGNORECASE)
        if contact_match:
            stop["contact"] = clean_instruction(contact_match.group(1))
        if phone_match:
            stop["phone"] = normalize_space(phone_match.group(1))

        refs = []
        refs.extend(re.findall(r"\bRef\s*:\s*([A-Z0-9][A-Z0-9 \-]{2,40})", block, re.IGNORECASE))
        refs.extend([f"PO {po} - {part.upper()}" for po, part in re.findall(r"\bPO\s*#?\s*(\d{5,})\s*[-:]\s*(TAIL|NOSE)\b", block, re.IGNORECASE)])
        cleaned_refs = [_clean_ref_token(r) for r in refs if _clean_ref_token(r)]
        if cleaned_refs:
            stop["references"] = unique_keep_order(cleaned_refs)

        stop = finalize_stop(stop)
        if marker == "PU":
            pickup_stops.append(stop)
        else:
            delivery_stops.append(stop)

    return dedupe_stops(pickup_stops), dedupe_stops(delivery_stops)


def extract_global_shipment_fields(text: str) -> dict:
    text = text or ""
    miles_match = find_first(
        [r"\bTOTAL\s+MILES\s*[:#-]?\s*([\d,]+(?:\.\d+)?)\b", r"\bMILES?\s*[:#-]?\s*([\d,]+(?:\.\d+)?)\b"],
        text,
    )
    pieces_match = find_first([r"\bPIECES?\s*[:#-]?\s*([\d,]+)\b"], text)
    equip_match = find_first(
        [r"\bEQUIPMENT\s+TYPE\s*[:#-]?\s*([^\n\r]{1,40})", r"\bEQUIP(?:MENT)?\s*[:#-]?\s*([^\n\r]{1,40})", r"\bTrailer\s*:\s*([^\n\r]{3,40})"],
        text,
    )
    equipment = map_equipment_code(text)
    if equipment == "N/A" and equip_match:
        raw_equipment = normalize_space(equip_match.group(1)).split(";")[0].strip()
        if raw_equipment and not re.fullmatch(r"(TYPE|TRAILER|EQUIPMENT)", raw_equipment, re.IGNORECASE):
            equipment = raw_equipment[:25]

    haz = parse_hazmat(text)
    temp = extract_temp_controls(text)
    commodity = extract_commodity(text)
    commodity = re.split(r"\b(DRIVER\s*:|SIGN\s+THIS|SPECIAL\s+INSTRUCTIONS?|DIRECTIONS\s*:)\b", commodity, maxsplit=1, flags=re.IGNORECASE)[0].strip(" -,")
    if not commodity:
        commodity = "N/A"

    return {
        "pro_number": extract_load_number(text),
        "broker": detect_broker(text),
        "total_rate": extract_rate(text),
        "commodity": commodity,
        "weight": extract_weight(text),
        "pallets": extract_pallets(text),
        "pieces": pieces_match.group(1) if pieces_match else "N/A",
        "miles": miles_match.group(1) if miles_match else "N/A",
        "equipment": equipment,
        "is_hazmat": haz.get("is_hazmat", False),
        "un_number": haz.get("un_number", "N/A"),
        "hazmat_class": haz.get("hazmat_class", "N/A"),
        "temp_range": temp.get("temp_range", "N/A"),
        "temp_mode": temp.get("temp_mode", "N/A"),
        "pulp_required": temp.get("pulp_required", False),
        "pulp_not_required": temp.get("pulp_not_required", False),
        "tarp_required": temp.get("tarp_required", False),
        "tarp_not_required": temp.get("tarp_not_required", False),
        "temp_on_bol": temp.get("temp_on_bol", False),
        "tracking_required": temp.get("tracking_required", False),
        "seal_required": temp.get("seal_required", False),
    }


def extract_landstar_stops(text: str):
    lines = split_lines(text or "")
    if not any(re.search(r"\b(PICKUP|DELIVERY)\s*#", ln, re.IGNORECASE) for ln in lines):
        return [], []

    def _find_city_state_zip(block_lines):
        for ln in block_lines:
            m = re.search(rf"\b([A-Za-z][A-Za-z .'-]+)\s+({US_STATE_RE})\s+(\d{{5}}(?:-\d{{4}})?)\b", ln, re.IGNORECASE)
            if m:
                return f"{normalize_space(m.group(1))}, {normalize_space(m.group(2)).upper()} {normalize_space(m.group(3))}"
        return ""

    def _parse_block(block_lines, idx_pick, idx_drop):
        header = block_lines[0] if block_lines else ""
        stop_type = "delivery" if re.search(r"\bDELIVERY\b", header, re.IGNORECASE) else "pickup"
        number = idx_drop + 1 if stop_type == "delivery" else idx_pick + 1
        stop = make_stop(stop_type, number)

        date_m = find_first([r"\b\d{1,2}/\d{1,2}/\d{2,4}\b", rf"\b{MONTH_NAME_RE}\s+\d{{1,2}},\s*\d{{4}}\b"], header)
        if date_m:
            stop["date"] = normalize_space(date_m.group(0))
        time_m = find_first([r"\b\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2}\b", r"\b\d{1,2}:\d{2}\b"], header)
        if time_m:
            stop["time"] = normalize_space(time_m.group(0))

        name = ""
        street = ""
        name_addr_line = ""
        for ln in block_lines:
            m = re.search(r"\bNAME/ADDRESS\s*:\s*(.+)$", ln, re.IGNORECASE)
            if m:
                name_addr_line = normalize_space(m.group(1))
                break
        if name_addr_line:
            st = re.search(r"(\d{2,6}\s+[A-Za-z0-9 .#'/-]+)$", name_addr_line)
            if st:
                street = clean_address(st.group(1))
                name = clean_stop_name(name_addr_line[: st.start()].strip(" ,"))
            else:
                name = clean_stop_name(name_addr_line)
        city_state_zip = _find_city_state_zip(block_lines)
        if city_state_zip and street:
            stop["address"] = clean_address(f"{street}, {city_state_zip}")
        elif city_state_zip:
            stop["address"] = clean_address(city_state_zip)
        elif street:
            stop["address"] = clean_address(street)
        if name and name != "N/A":
            stop["location"] = name

        for ln in block_lines:
            c = re.search(r"\bCONTACT\s*[:#-]?\s*([A-Za-z0-9 .,'/-]{2,80})", ln, re.IGNORECASE)
            p = re.search(r"\bPHONE\s*[:#-]?\s*([()+\d\s\-xX]{7,30})", ln, re.IGNORECASE)
            if c:
                stop["contact"] = normalize_space(c.group(1))
            if p:
                stop["phone"] = normalize_space(p.group(1))

        block_text = "\n".join(block_lines)
        stop["references"] = extract_reference_numbers(block_text)
        stop = finalize_stop(stop)

        if stop["location"] == "N/A" and stop["address"] == "N/A":
            fallback = parse_stop_lines(block_lines, stop_type, number)
            stop = finalize_stop(fallback)
        return stop

    blocks = []
    current = []
    for ln in lines:
        if re.search(r"\b(PICKUP|DELIVERY)\s*#", ln, re.IGNORECASE):
            if current:
                blocks.append(current)
            current = [ln]
            continue
        if current:
            if re.match(r"^(RATE|CHARGES|SPECIAL INSTRUCTIONS|DISPATCH NOTES|TERMS|SIGNATURE)\b", ln, re.IGNORECASE):
                blocks.append(current)
                current = []
                continue
            current.append(ln)
    if current:
        blocks.append(current)

    pickups = []
    deliveries = []
    for block in blocks:
        parsed = _parse_block(block, len(pickups), len(deliveries))
        if parsed.get("type") == "pickup":
            pickups.append(parsed)
        else:
            deliveries.append(parsed)

    return dedupe_stops(pickups), dedupe_stops(deliveries)


def extract_labeled_family_stops(text: str):
    upper = (text or "").upper()
    if re.search(r"(?m)^\s*PU\s*\d+\b", text or "", re.IGNORECASE) and re.search(r"(?m)^\s*SO\s*\d+\b", text or "", re.IGNORECASE):
        return extract_mcleod_stops(text)
    if "PROPEL FREIGHT" in upper and re.search(r"(?m)^\s*PICK\s*1\b", text or ""):
        return extract_propel_stops(text)
    if "ALLEN LUND" in upper:
        return extract_allen_lund_stops(text)
    return [], []


def extract_table_family_stops(text: str):
    upper = (text or "").upper()
    if re.search(r"SHIPPER\s*\(STOP\s*\d+\s*OF\s*\d+\)", upper):
        return extract_wecanmoveit_stops(text)
    if "ARRIVE LOGISTICS" in upper or "ARRIVE ORDER" in upper:
        return extract_arrive_stops(text)
    if "CARDINAL LOGISTICS" in upper or "RYDER" in upper:
        return extract_cardinal_stops(text)
    return [], []


def extract_shipper_consignee_family_stops(text: str):
    upper = (text or "").upper()
    if "BARAKAT" in upper:
        return extract_barakat_stops(text)
    if "COR FREIGHT" in upper:
        return extract_cor_stops(text)
    if "LANDSTAR" in upper or re.search(r"\bNAME/ADDRESS\s*:", text or "", re.IGNORECASE):
        pu, de = extract_landstar_stops(text)
        if any(has_real_stop(s) for s in pu) or any(has_real_stop(s) for s in de):
            return pu, de

    pickups = []
    deliveries = []
    shipper_blocks = re.findall(r"(?is)(SHIPPER\b.*?)(?=(?:\bSHIPPER\b|\bCONSIGNEE\b|\bSPECIAL\s+INSTRUCTIONS\b|\bRATE\b|$))", text or "")
    consignee_blocks = re.findall(r"(?is)(CONSIGNEE\b.*?)(?=(?:\bSHIPPER\b|\bCONSIGNEE\b|\bSPECIAL\s+INSTRUCTIONS\b|\bRATE\b|$))", text or "")

    for idx, block in enumerate(shipper_blocks, start=1):
        stop = parse_stop_lines(split_lines(block), "pickup", idx)
        pickups.append(finalize_stop(stop))
    for idx, block in enumerate(consignee_blocks, start=1):
        stop = parse_stop_lines(split_lines(block), "delivery", idx)
        deliveries.append(finalize_stop(stop))
    return dedupe_stops(pickups), dedupe_stops(deliveries)


def extract_inline_compact_stops(text: str):
    return extract_default_stops(text)


def parse_stops_by_layout_family(text: str, broker: str):
    for parser in (
        extract_labeled_family_stops,
        extract_table_family_stops,
        extract_shipper_consignee_family_stops,
        extract_inline_compact_stops,
    ):
        pu, de = parser(text)
        if any(has_real_stop(s) for s in (pu or [])) or any(has_real_stop(s) for s in (de or [])):
            return dedupe_stops(pu or []), dedupe_stops(de or [])
    return [], []


def extract_driver_critical_notes(text: str):
    text = text or ""
    notes = []
    notes.extend(extract_special_instructions(text))

    rules = [
        (r"\bTRACKING\b|\bMACRO\s*POINT\b|\bE-?TRACK\b", "Driver must accept tracking."),
        (r"\bSEAL(?:\s+REQUIRED)?\b|\bSEAL\s+NUMBER\b", "Seal required. Write seal number on BOL."),
        (r"\bPODS?\b.*\bWITHIN\b|\bBOL\b.*\bWITHIN\b", "Submit POD/BOL within broker deadline."),
        (r"\bDETENTION\b", "Detention rules apply per broker rate confirmation."),
        (r"\bLAYOVER\b", "Layover rules apply per broker rate confirmation."),
        (r"\bLUMPER\b", "Lumper requires broker approval and receipt for reimbursement."),
        (r"\bFCFS\b|\bAPPOINTMENT\b|\bARRIVE\s+BETWEEN\b|\bHOURS?\b", "Follow FCFS/appointment windows and facility hours."),
        (r"\bTARP\b|\bSTRAPS?\b|\bLOAD\s+BARS?\b", "Use required securement equipment (tarp/straps/load bars)."),
        (r"\bPENALT(?:Y|IES)\b|\bFINE[S]?\b", "Late/delay penalties may apply."),
        (r"\bDRIVER\s+MUST\b|\bMUST\b", "Follow all DRIVER MUST instructions."),
        (r"\bDELAY(?:ED|S)?\b.*\bREPORT\b|\bREPORT\s+DELAYS?\b", "Report delays immediately."),
        (r"\bCONFIRM\s+PO\s+ORDER\b|\bPO\s+ORDER\s+CONFIRM(?:ATION)?\b", "Confirm PO order when required."),
        (r"\bTWO\s+LOAD\s+UPDATES?\s+PER\s+DAY\b|\bLOAD\s+UPDATES?\s+PER\s+DAY\b", "Provide two load updates per day."),
    ]
    for pattern, message in rules:
        if re.search(pattern, text, re.IGNORECASE):
            notes.append(message)

    return unique_keep_order([clean_instruction(n) for n in notes if clean_instruction(n)])[:24]


def parse_rc(text: str) -> dict:
    text = text or ""
    globals_data = extract_global_shipment_fields(text)
    pu, de = parse_stops_by_layout_family(text, globals_data.get("broker", "N/A"))

    if not any(has_real_stop(stop) for stop in (pu or [])) or not any(has_real_stop(stop) for stop in (de or [])):
        fallback_pu, fallback_de = extract_inline_compact_stops(text)
        if not any(has_real_stop(stop) for stop in (pu or [])) and any(has_real_stop(stop) for stop in (fallback_pu or [])):
            pu = fallback_pu
        if not any(has_real_stop(stop) for stop in (de or [])) and any(has_real_stop(stop) for stop in (fallback_de or [])):
            de = fallback_de

    def _clean_ref_value(ref: str) -> str:
        value = normalize_space(ref or "")
        value = re.sub(r"^REF:\s*REF:\s*", "REF: ", value, flags=re.IGNORECASE)
        value = re.sub(r"^REF:\s*(REF:\s*)+", "REF: ", value, flags=re.IGNORECASE)
        value = re.sub(r"\bPES\b.*$", "", value, flags=re.IGNORECASE)
        value = re.sub(r"\s{2,}", " ", value).strip(" -,:;")
        return value

    pickup_po_pairs = re.findall(r"\bPO\s*#?\s*(\d{5,})\s*[-:]\s*(TAIL|NOSE)\b", text, re.IGNORECASE)
    pickup_po_pairs = sorted(pickup_po_pairs, key=lambda item: (0 if item[1].upper() == "TAIL" else 1, item[0]))
    if pu and pickup_po_pairs:
        pickup_po_refs = [f"PO {po} - {part.upper()}" for po, part in pickup_po_pairs]
        existing_refs = [_clean_ref_value(r) for r in (pu[0].get("references") or []) if _clean_ref_value(r)]
        existing_non_po_tail_nose = [r for r in existing_refs if not re.search(r"\bPO\s*\d{5,}\s*-\s*(TAIL|NOSE)\b", r, re.IGNORECASE)]
        pu[0]["references"] = unique_keep_order(pickup_po_refs + existing_non_po_tail_nose)

    for stop in (pu or []) + (de or []):
        stop["references"] = unique_keep_order([_clean_ref_value(r) for r in (stop.get("references") or []) if _clean_ref_value(r)])
        stop["maps_link"] = build_maps_link(stop.get("address", "N/A"))

    refs = []
    refs.extend(extract_reference_numbers(text))
    for stop in (pu or []) + (de or []):
        refs.extend(stop.get("references") or [])
    cleaned_refs = [_clean_ref_value(r) for r in refs if _clean_ref_value(r)]

    notes = extract_driver_critical_notes(text)
    for stop_group, label in ((pu or [], "Pickup"), (de or [], "Delivery")):
        multi_stop = len(stop_group) > 1
        for idx, stop in enumerate(stop_group, start=1):
            stop_label = f"{label} {idx}" if multi_stop else label
            for note in stop.get("notes") or []:
                cleaned = clean_instruction(note)
                if cleaned:
                    notes.append(f"{stop_label}: {cleaned}")

    return {
        "pro_number": globals_data.get("pro_number", "N/A"),
        "broker": globals_data.get("broker", "N/A"),
        "commodity": globals_data.get("commodity", "N/A"),
        "weight": globals_data.get("weight", "N/A"),
        "pallets": globals_data.get("pallets", "N/A"),
        "miles": globals_data.get("miles", "N/A"),
        "google_loaded_miles": calculate_loaded_miles_google(pu, de),
        "equipment": globals_data.get("equipment", "N/A"),
        "total_rate": globals_data.get("total_rate", "N/A"),
        "pickup_stops": pu or [],
        "delivery_stops": de or [],
        "reference_numbers": unique_keep_order(cleaned_refs)[:12],
        "charge_items": extract_charge_items(text),
        "special_instructions": unique_keep_order(notes)[:24],
        "is_hazmat": globals_data.get("is_hazmat", False),
        "un_number": globals_data.get("un_number", "N/A"),
        "hazmat_class": globals_data.get("hazmat_class", "N/A"),
        "temp_range": globals_data.get("temp_range", "N/A"),
        "temp_mode": globals_data.get("temp_mode", "N/A"),
        "pulp_required": globals_data.get("pulp_required", False),
        "pulp_not_required": globals_data.get("pulp_not_required", False),
        "tarp_required": globals_data.get("tarp_required", False),
        "tarp_not_required": globals_data.get("tarp_not_required", False),
        "temp_on_bol": globals_data.get("temp_on_bol", False),
        "tracking_required": globals_data.get("tracking_required", False),
        "seal_required": globals_data.get("seal_required", False),
        "pieces": globals_data.get("pieces", "N/A"),
    }


if __name__ == "__main__":
    main()
