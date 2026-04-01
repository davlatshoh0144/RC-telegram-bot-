import sys
import os

# Set Tesseract path FIRST
os.environ["PATH"] += r";C:\Program Files\Tesseract-OCR"

import pytesseract
pytesseract.pytesseract.pytesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

import logging, tempfile, re
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from PIL import Image

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = "8042412126:AAFk64jpqGgxX98bLTw6krBj9ORmSiIA6eM"
TEMP_DIR = tempfile.gettempdir()

def pdf_to_text_ocr(pdf_path):
    text = ""
    try:
        import fitz
        doc = fitz.open(pdf_path)
        print(f"📄 PDF has {len(doc)} pages")
        
        for page_num in range(len(doc)):
            page = doc[page_num]
            mat = fitz.Matrix(3, 3)
            pix = page.get_pixmap(matrix=mat)
            
            img_path = os.path.join(TEMP_DIR, f"page_{page_num}.png")
            pix.save(img_path)
            
            img = Image.open(img_path)
            page_text = pytesseract.image_to_string(img)
            print(f"✓ Page {page_num}: {len(page_text)} chars")
            
            text += page_text + "\n"
            os.remove(img_path)
        
        doc.close()
    except Exception as e:
        print(f"❌ PDF Error: {e}")
        return ""
    
    print(f"✅ Total: {len(text)} chars extracted")
    return text

def extract_image_text(img_path):
    try:
        img = Image.open(img_path)
        return pytesseract.image_to_string(img)
    except Exception as e:
        print(f"Image error: {e}")
        return ""

def parse_rc(text):
    print(f"\n=== PARSING ===")
    print(f"Length: {len(text)} chars")
    
    data = {
        "pro": "N/A", "carrier": "UZB", "contact": "N/A", "phone": "N/A",
        "equip": "N/A", "commodity": "N/A", "weight": "N/A", "pallets": "N/A",
        "miles": "N/A", "rate": "N/A", "has_rate": True, "pickup_loc": "N/A",
        "pickup_addr": "N/A", "pickup_date": "N/A", "pickup_time": "N/A",
        "delivery_loc": "N/A", "delivery_addr": "N/A", "delivery_date": "N/A",
        "delivery_time": "N/A", "temp": "N/A", "reefer": False,
        "instructions": [], "broker": "N/A",
    }
    
    m = re.search(r"PRO\s*#\s*(\d{5,})|Load\s*#\s*(\d{5,})", text, re.I)
    if m: 
        data["pro"] = m.group(1) or m.group(2)
        print(f"✓ PRO: {data['pro']}")
    
    if "ALLEN" in text.upper(): 
        data["broker"] = "ALLEN LUND"
    elif "PROPEL" in text.upper(): 
        data["broker"] = "PROPEL"
        print(f"✓ Broker: PROPEL")
    
    m = re.findall(r"\(?(\d{3})\)?[\s.-]?(\d{3})[\s.-]?(\d{4})", text)
    if m: 
        data["phone"] = f"({m[0][0]}) {m[0][1]}-{m[0][2]}"
        print(f"✓ Phone: {data['phone']}")
    
    if re.search(r"REEFER", text, re.I):
        data["reefer"] = True
        data["equip"] = "53' REEFER"
        print(f"✓ REEFER")
    
    if "FROZEN" in text.upper(): 
        data["commodity"] = "FROZEN FOOD"
        print(f"✓ Commodity: FROZEN")
    
    m = re.search(r"Weight\s*[:\s=]+(\d+)", text, re.I)
    if m: 
        data["weight"] = f"{m.group(1)} lbs"
        print(f"✓ Weight: {data['weight']}")
    
    m = re.search(r"PICK.*?\n\s*([A-Z\s]+?)\n\s*(\d+.*?(?:TX|CA|WA|AZ|NJ|IL).*?\d{5}).*?(\d{1,2}/\d{1,2}/\d{2,4})", text, re.I | re.DOTALL)
    if m:
        data["pickup_loc"] = m.group(1).strip()[:35]
        data["pickup_addr"] = m.group(2).strip()[:55]
        data["pickup_date"] = m.group(3)
        print(f"✓ Pickup: {data['pickup_loc']}")
    
    m = re.search(r"(?:STOP|FOODCO).*?\n\s*([A-Z\s]+?)\n\s*(\d+.*?(?:TX|CA|WA|AZ|NJ|IL).*?\d{5}).*?(\d{1,2}/\d{1,2}/\d{2,4})", text, re.I | re.DOTALL)
    if m:
        data["delivery_loc"] = m.group(1).strip()[:35]
        data["delivery_addr"] = m.group(2).strip()[:55]
        data["delivery_date"] = m.group(3)
        print(f"✓ Delivery: {data['delivery_loc']}")
    
    return data

def format_msg(d):
    return f"📋 **{d['pro']}**\n📦 {d['commodity']}\n⚖️ {d['weight']}\n📤 {d['pickup_loc']} → 📥 {d['delivery_loc']}\n💰 ${d['rate'] if d['has_rate'] else 'TBD'}"

async def rate_response(update, context):
    q = update.callback_query
    await q.answer()
    if "d" in context.user_data:
        if q.data == "no": context.user_data["d"]["has_rate"] = False
        await q.edit_message_text(format_msg(context.user_data["d"]), parse_mode="Markdown")

async def handle_pdf(update, context):
    try:
        await update.message.reply_text("📄 Converting...")
        f = await update.message.document.get_file()
        fp = os.path.join(TEMP_DIR, f"{f.file_unique_id}.pdf")
        await f.download_to_drive(fp)
        
        txt = pdf_to_text_ocr(fp)
        d = parse_rc(txt)
        context.user_data["d"] = d
        
        kb = [[InlineKeyboardButton("✅ WITH", callback_data="yes"),
               InlineKeyboardButton("❌ NO", callback_data="no")]]
        
        await update.message.reply_text(f"{d['pro']}\n\n**Has RATE?**", 
                                       reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
        if os.path.exists(fp): os.remove(fp)
    except Exception as e:
        await update.message.reply_text(f"❌ {str(e)}")

async def handle_photo(update, context):
    try:
        await update.message.reply_text("📸 Scanning...")
        f = await update.message.photo[-1].get_file()
        fp = os.path.join(TEMP_DIR, f"{f.file_unique_id}.jpg")
        await f.download_to_drive(fp)
        
        txt = extract_image_text(fp)
        d = parse_rc(txt)
        context.user_data["d"] = d
        
        kb = [[InlineKeyboardButton("✅ WITH", callback_data="yes"),
               InlineKeyboardButton("❌ NO", callback_data="no")]]
        
        await update.message.reply_text(f"{d['pro']}\n\n**Has RATE?**", 
                                       reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
        if os.path.exists(fp): os.remove(fp)
    except Exception as e:
        await update.message.reply_text(f"❌ {str(e)}")

async def start(update, context):
    await update.message.reply_text("🤖 RC Bot Ready")

def main():
    print("🚀 Starting...")
    app = Application.builder().token(TOKEN).build()
    app.add_handler(MessageHandler(filters.Document.PDF, handle_pdf))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CallbackQueryHandler(rate_response))
    app.add_handler(MessageHandler(filters.COMMAND, start))
    print("✅ RUNNING\n")
    app.run_polling()

if __name__ == "__main__":
    main()