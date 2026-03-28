import os
import re
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, MessageHandler, filters, ContextTypes, CallbackQueryHandler
import pytesseract
from PIL import Image
import pdfplumber
import tempfile

pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = "8042412126:AAEtcZWM52JnYcHvPHDfdRgj0VWh3ypeMgw"
TEMP_DIR = tempfile.gettempdir()

def extract_text_from_pdf_ocr(file_path):
    """Convert PDF pages to images and OCR - NO POPPLER NEEDED"""
    text = ""
    try:
        with pdfplumber.open(file_path) as pdf:
            for page in pdf.pages:
                # Convert page to image
                img = page.to_image(resolution=300)
                # OCR the image
                ocr_text = pytesseract.image_to_string(img.original)
                text += ocr_text + "\n"
    except Exception as e:
        logger.error(f"PDF OCR error: {e}")
        return ""
    return text

def extract_text_from_image(image_path):
    """Extract text from image using OCR"""
    try:
        img = Image.open(image_path)
        text = pytesseract.image_to_string(img)
        return text
    except Exception as e:
        logger.error(f"Image OCR error: {e}")
        return ""

def parse_rc(text):
    """Parse rate confirmation with ALL details"""
    
    data = {
        "pro_number": "N/A",
        "carrier": "UZB FREIGHT",
        "contact": "N/A",
        "phone": "N/A",
        "equipment": "N/A",
        "commodity": "N/A",
        "weight": "N/A",
        "pallets": "N/A",
        "miles": "N/A",
        "total_rate": "N/A",
        "has_rate": True,
        "pickup_location": "N/A",
        "pickup_address": "N/A",
        "pickup_date": "N/A",
        "pickup_time": "N/A",
        "delivery_location": "N/A",
        "delivery_address": "N/A",
        "delivery_date": "N/A",
        "delivery_time": "N/A",
        "temp_range": "N/A",
        "is_reefer": False,
        "hazmat": "NO",
        "hazmat_class": "N/A",
        "special_instructions": [],
        "broker": "N/A",
    }
    
    # PRO Number
    pro_match = re.search(r'PRO\s*#\s*(\d{5,})', text, re.IGNORECASE)
    if pro_match:
        data["pro_number"] = pro_match.group(1)
    
    # Broker
    if "ALLEN LUND" in text.upper():
        data["broker"] = "ALLEN LUND COMPANY"
    elif "PROPEL" in text.upper():
        data["broker"] = "PROPEL FREIGHT LLC"
    
    # Phones
    phone_matches = re.findall(r'\(?(\d{3})\)?[\s.-]?(\d{3})[\s.-]?(\d{4})', text)
    if phone_matches:
        p = phone_matches[0]
        data["phone"] = f"({p[0]}) {p[1]}-{p[2]}"
    
    # Contact person
    contact_match = re.search(r'(?:FROM|Contact|CARRIER CONTACT):\s*([A-Z][A-Za-z\s]+?)(?:\n|\()', text, re.IGNORECASE)
    if contact_match:
        name = contact_match.group(1).strip()
        if len(name) < 50:
            data["contact"] = name
    
    # REEFER CHECK
    if re.search(r'53.*?REEFER|REFRIGERATED', text, re.IGNORECASE):
        data["is_reefer"] = True
        data["equipment"] = "53' REEFER"
    elif re.search(r'DRY|VAN', text, re.IGNORECASE):
        data["equipment"] = "DRY VAN"
    
    # Commodity
    if "FROZEN" in text.upper():
        data["commodity"] = "FROZEN FOOD"
    elif "BRUSSELS" in text.upper():
        data["commodity"] = "BRUSSELS SPROUTS"
    else:
        commodity_match = re.search(r'Description:\s*([A-Za-z\s&]+?)(?:\n|Weight)', text, re.IGNORECASE)
        if commodity_match:
            data["commodity"] = commodity_match.group(1).strip()[:50]
    
    # Weight
    weight_match = re.search(r'Weight\s*[:\s=]+(\d+)', text, re.IGNORECASE)
    if weight_match:
        data["weight"] = f"{weight_match.group(1)} lbs"
    
    # Pallets
    pallets_match = re.search(r'Pallets?\s*[:\s=]+(\d+)', text, re.IGNORECASE)
    if pallets_match:
        data["pallets"] = pallets_match.group(1)
    
    # Miles
    miles_match = re.search(r'Miles\s*[:\s=]+(\d+)', text, re.IGNORECASE)
    if miles_match:
        data["miles"] = miles_match.group(1)
    
    # Temperature
    temp_match = re.search(r'TEMP.*?(-?\d+)\s*(?:TO|to|and)\s*(-?\d+)\s*F', text, re.IGNORECASE)
    if temp_match:
        data["temp_range"] = f"{temp_match.group(1)}°F to {temp_match.group(2)}°F"
    
    # Hazmat
    if "YES" in text.upper() and "HAZMAT" in text.upper():
        data["hazmat"] = "YES"
        class_match = re.search(r'Class\s*[:\s=]+(\d+\.?\d*)', text, re.IGNORECASE)
        if class_match:
            data["hazmat_class"] = class_match.group(1)
    
    # PICKUP - extract location, address, and date
    # Look for PICK or LIBRADO section
    pickup_section = re.search(r'(?:PICK|LIBRADO).*?\n\s*([A-Z\s]+?)\n\s*(\d+.*?(?:TX|CA|WA|AZ|NJ|IL).*?\d{5}).*?(\d{1,2}/\d{1,2}/\d{2,4})', text, re.IGNORECASE | re.DOTALL)
    if pickup_section:
        data["pickup_location"] = pickup_section.group(1).strip()[:40]
        data["pickup_address"] = pickup_section.group(2).strip()[:60]
        data["pickup_date"] = pickup_section.group(3).strip()
    
    # Pickup time
    time_match = re.search(r'(?:Ready Date|Pick.*?Time).*?(\d{1,2}:\d{2})', text, re.IGNORECASE | re.DOTALL)
    if time_match:
        data["pickup_time"] = time_match.group(1)
    
    # DELIVERY - extract location, address, and date
    # Look for STOP 1 or FOODCO or similar
    delivery_section = re.search(r'(?:STOP|FOODCO|Delivery).*?\n\s*([A-Z\s]+?)\n\s*(\d+.*?(?:TX|CA|WA|AZ|NJ|IL).*?\d{5}).*?(?:Appointment|Delivery Date).*?(\d{1,2}/\d{1,2}/\d{2,4})', text, re.IGNORECASE | re.DOTALL)
    if delivery_section:
        data["delivery_location"] = delivery_section.group(1).strip()[:40]
        data["delivery_address"] = delivery_section.group(2).strip()[:60]
        data["delivery_date"] = delivery_section.group(3).strip()
    
    # Delivery time
    dtime_match = re.search(r'Appointment.*?@\s*(\d{1,2}:\d{2})|Delivery Time\s*[:\s=]+(\d{1,2}:\d{2})', text, re.IGNORECASE | re.DOTALL)
    if dtime_match:
        data["delivery_time"] = dtime_match.group(1) or dtime_match.group(2)
    
    # Rate
    rate_match = re.search(r'TOTAL\s*RATE\s*[:\s=]*\$?\s*([\d,]+\.?\d*)', text, re.IGNORECASE)
    if rate_match:
        data["total_rate"] = rate_match.group(1)
    else:
        data["has_rate"] = False
    
    # Special instructions
    instructions = []
    
    if "COSTCO" in text.upper():
        instructions.append("⚠️ COSTCO: Appointment-only")
        instructions.append("📱 CW app + fast pass QR")
        instructions.append("📋 Call if missing appointment")
    
    if "POD" in text.upper():
        instructions.append("📄 Send POD within 72 HOURS")
        instructions.append("⏰ $100 FINE if late")
    
    if "MACRO POINT" in text.upper():
        instructions.append("📡 Macro Point tracking")
    
    data["special_instructions"] = instructions[:5]
    
    return data

def format_driver_message(data):
    """Format for driver"""
    
    msg = f"""
╔═══════════════════════════════════════╗
║  📋 RATE CONFIRMATION - DRIVER VIEW  ║
╚═══════════════════════════════════════╝

🔖 **LOAD:** `{data['pro_number']}`
📦 **BROKER:** {data['broker']}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🎯 **SHIPMENT**
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

📦 Commodity: {data['commodity']}
⚖️  Weight: {data['weight']}
📫 Pallets: {data['pallets']}
📏 Miles: {data['miles']}
🚛 Equipment: {data['equipment']}"""
    
    if data['is_reefer']:
        msg += f"\n🌡️  **TEMPERATURE: {data['temp_range']}** ⚠️ CRITICAL"
    
    msg += f"\n☢️  Hazmat: {data['hazmat']}"
    
    msg += f"""

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🛣️  **ROUTE**
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

📤 **PICKUP:**
   {data['pickup_location']}
   📍 {data['pickup_address']}
   📅 {data['pickup_date']} @ {data['pickup_time']}

📥 **DELIVERY:**
   {data['delivery_location']}
   📍 {data['delivery_address']}
   📅 {data['delivery_date']} @ {data['delivery_time']}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
💰 **RATE**
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
    
    if data['has_rate']:
        msg += f"💵 **Rate: ${data['total_rate']}**\n"
    else:
        msg += "💵 **Rate: TO BE CONFIRMED**\n"
    
    msg += f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⚠️  **DRIVER INSTRUCTIONS**
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
    
    if data['special_instructions']:
        for idx, instruction in enumerate(data['special_instructions'], 1):
            msg += f"\n{idx}. {instruction}"
    else:
        msg += "\n✅ No special instructions"
    
    msg += f"""

📞 {data['broker']} | {data['contact']} | {data['phone']}
"""
    
    return msg

async def handle_rate_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle rate button"""
    query = update.callback_query
    await query.answer()
    
    if 'rc_data' in context.user_data:
        data = context.user_data['rc_data']
        if query.data == "no_rate":
            data['has_rate'] = False
        msg = format_driver_message(data)
        await query.edit_message_text(msg, parse_mode="Markdown")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle PDF uploads"""
    try:
        await update.message.reply_text("⏳ Reading PDF (OCR)...")
        
        file = await update.message.document.get_file()
        file_path = os.path.join(TEMP_DIR, f"{file.file_unique_id}.pdf")
        await file.download_to_drive(file_path)
        
        text = extract_text_from_pdf_ocr(file_path)
        data = parse_rc(text)
        
        context.user_data['rc_data'] = data
        
        keyboard = [
            [
                InlineKeyboardButton("✅ WITH RATE", callback_data="with_rate"),
                InlineKeyboardButton("❌ NO RATE YET", callback_data="no_rate")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            f"📄 Load `{data['pro_number']}` loaded.\n\n**Does this load have a RATE agreed?**",
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )
        
        if os.path.exists(file_path):
            os.remove(file_path)
        
    except Exception as e:
        logger.error(f"Error: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle photos"""
    try:
        await update.message.reply_text("⏳ Scanning image...")
        
        file = await update.message.photo[-1].get_file()
        file_path = os.path.join(TEMP_DIR, f"{file.file_unique_id}.jpg")
        await file.download_to_drive(file_path)
        
        text = extract_text_from_image(file_path)
        data = parse_rc(text)
        
        context.user_data['rc_data'] = data
        
        keyboard = [
            [
                InlineKeyboardButton("✅ WITH RATE", callback_data="with_rate"),
                InlineKeyboardButton("❌ NO RATE YET", callback_data="no_rate")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            f"📸 Load `{data['pro_number']}` scanned.\n\n**Does this load have a RATE agreed?**",
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )
        
        if os.path.exists(file_path):
            os.remove(file_path)
        
    except Exception as e:
        logger.error(f"Error: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start"""
    await update.message.reply_text(
        "🤖 **UZB FREIGHT - RC Bot**\n\n"
        "📄 PDF or 📸 Photo\n\n"
        "✅ Full extraction\n"
        "💰 Rate tracking",
        parse_mode="Markdown"
    )

def main():
    print("🚀 Starting RC Bot...")
    app = Application.builder().token(TOKEN).build()
    
    app.add_handler(MessageHandler(filters.Document.PDF, handle_document))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CallbackQueryHandler(handle_rate_response))
    app.add_handler(MessageHandler(filters.COMMAND, start))
    
    print("✅ Bot RUNNING!")
    print("Send /start to test\n")
    
    app.run_polling()

if __name__ == "__main__":
    main()