import os
import re
import logging
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
import pdfplumber
import pytesseract
pytesseract.pytesseract.pytesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'
from PIL import Image
from io import BytesIO

# Setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = "8042412126:AAEtcZWM52JnYcHvPHDfdRgj0VWh3ypeMgw" # Get from @BotFather on Telegram

def extract_from_pdf(file_path):
    """Extract text from PDF"""
    text = ""
    try:
        with pdfplumber.open(file_path) as pdf:
            for page in pdf.pages:
                text += page.extract_text() or ""
    except Exception as e:
        logger.error(f"PDF extraction error: {e}")
    return text

def extract_from_image(image_path):
    """Extract text from image using OCR"""
    try:
        img = Image.open(image_path)
        text = pytesseract.image_to_string(img)
        return text
    except Exception as e:
        logger.error(f"Image OCR error: {e}")
        return ""

def parse_rc(text):
    """Parse rate confirmation data"""
    data = {
        "pro_number": "N/A",
        "carrier": "N/A",
        "contact": "N/A",
        "equipment": "N/A",
        "commodity": "N/A",
        "weight": "N/A",
        "miles": "N/A",
        "total_rate": "N/A",
        "pickup": "N/A",
        "pickup_date": "N/A",
        "delivery": "N/A",
        "delivery_date": "N/A",
        "temp": "N/A",
        "special": "N/A"
    }
    
    # Extract PRO/Load number
    pro_match = re.search(r'PRO\s*#\s*(\d+)', text)
    if pro_match:
        data["pro_number"] = pro_match.group(1)
    
    # Extract carrier name
    carrier_match = re.search(r'UZB\s*FREIGHT\s*INC', text)
    if carrier_match:
        data["carrier"] = "UZB FREIGHT INC"
    
    # Extract contact person
    contact_match = re.search(r'(JOE\s+HERNANDEZ|TED)', text)
    if contact_match:
        data["contact"] = contact_match.group(1)
    
    # Extract equipment
    equip_match = re.search(r"(?:53['\"]?\s*)?(?:REEFER|REFRIGERATED|DRY|FLATBED)", text)
    if equip_match:
        data["equipment"] = equip_match.group(0).strip()
    
    # Extract commodity
    commodity_match = re.search(r'(?:FROZEN\s+FOOD|BRUSSELS\s+SPROUTS|Description:\s+([^,\n]+))', text)
    if commodity_match:
        data["commodity"] = commodity_match.group(1) if commodity_match.lastindex else commodity_match.group(0)
    
    # Extract weight
    weight_match = re.search(r'Weight:?\s*(\d+)\s*(?:lbs?|kg)?', text, re.IGNORECASE)
    if weight_match:
        data["weight"] = weight_match.group(1)
    
    # Extract miles
    miles_match = re.search(r'Miles:?\s*(\d+)', text)
    if miles_match:
        data["miles"] = miles_match.group(1)
    
    # Extract total rate
    rate_match = re.search(r'TOTAL\s*RATE\s*\$?([\d,]+\.?\d*)', text)
    if rate_match:
        data["total_rate"] = rate_match.group(1)
    
    # Extract temperature
    temp_match = re.search(r'TEMP(?:ERATURE)?.*?(-?\d+)\s*(?:TO|and)\s*(-?\d+)\s*[F°]', text)
    if temp_match:
        data["temp"] = f"{temp_match.group(1)} to {temp_match.group(2)}°F"
    
    # Extract pickup info
    pickup_match = re.search(r'PICK.*?\n\s*([A-Z\s]+)\n.*?(\d{1,2}/\d{1,2}/\d{2,4})', text)
    if pickup_match:
        data["pickup"] = pickup_match.group(1).strip()
        data["pickup_date"] = pickup_match.group(2)
    
    # Extract delivery info
    delivery_match = re.search(r'(?:STOP|DELIVERY).*?\n\s*([A-Z\s]+)\n.*?(\d{1,2}/\d{1,2}/\d{2,4})', text)
    if delivery_match:
        data["delivery"] = delivery_match.group(1).strip()
        data["delivery_date"] = delivery_match.group(2)
    
    return data

def format_message(data):
    """Format extracted data for Telegram"""
    msg = f"""
📋 **RATE CONFIRMATION EXTRACTED**

**Load Info:**
🔖 PRO/Load #: `{data['pro_number']}`
🚛 Carrier: {data['carrier']}
👤 Contact: {data['contact']}

**Equipment & Cargo:**
📦 Equipment: {data['equipment']}
🏷️ Commodity: {data['commodity']}
⚖️ Weight: {data['weight']} lbs
📏 Miles: {data['miles']}
🌡️ Temp: {data['temp']}

**Shipment:**
📍 Pickup: {data['pickup']} on {data['pickup_date']}
📍 Delivery: {data['delivery']} on {data['delivery_date']}

**Rate:**
💰 Total Rate: ${data['total_rate']}

**Special Notes:**
ℹ️ Check for signing requirements & POD submission (72hrs)
"""
    return msg

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle PDF uploads"""
    try:
        file = await update.message.document.get_file()
        file_path = f"/tmp/{file.file_unique_id}.pdf"
        await file.download_to_drive(file_path)
        
        text = extract_from_pdf(file_path)
        data = parse_rc(text)
        msg = format_message(data)
        
        await update.message.reply_text(msg, parse_mode="Markdown")
        os.remove(file_path)
        
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle image uploads"""
    try:
        file = await update.message.photo[-1].get_file()
        file_path = f"/tmp/{file.file_unique_id}.jpg"
        await file.download_to_drive(file_path)
        
        text = extract_from_image(file_path)
        data = parse_rc(text)
        msg = format_message(data)
        
        await update.message.reply_text(msg, parse_mode="Markdown")
        os.remove(file_path)
        
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start command"""
    await update.message.reply_text(
        "🤖 **Rate Confirmation Bot**\n\n"
        "Send me:\n"
        "📄 PDF of rate confirmation\n"
        "📸 Photo/screenshot of RC\n\n"
        "I'll extract all the details!"
    )

def main():
    app = Application.builder().token(TOKEN).build()
    
    app.add_handler(MessageHandler(filters.Document.PDF, handle_document))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.COMMAND, start))
    
    print("🤖 Bot running... Press Ctrl+C to stop")
    app.run_polling()

if __name__ == "__main__":
    main()