import pdfplumber
import pytesseract
from PIL import Image
import os
import re

# Set Tesseract path
pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'

# Test with your RC document - CHANGE THESE PATHS TO YOUR FILES
pdf_path = r"C:\Users\RZ\OneDrive\Desktop\RateConfirmation.pdf"
image_path = r"C:\Users\RZ\OneDrive\Desktop\rc.jpg"  # If you have an image

print("=" * 80)
print("TESTING EXTRACTION FROM YOUR RATE CONFIRMATION")
print("=" * 80)

# Try PDF first
if os.path.exists(pdf_path):
    print(f"\n✅ Found PDF: {pdf_path}")
    try:
        with pdfplumber.open(pdf_path) as pdf:
            print(f"📄 PDF has {len(pdf.pages)} pages\n")
            for i, page in enumerate(pdf.pages):
                text = page.extract_text()
                print(f"\n{'='*80}")
                print(f"PAGE {i+1} EXTRACTED TEXT:")
                print(f"{'='*80}")
                if text:
                    print(text)
                    print(f"\n[Text length: {len(text)} characters]")
                else:
                    print("❌ NO TEXT EXTRACTED FROM THIS PAGE")
    except Exception as e:
        print(f"❌ PDF ERROR: {e}")
else:
    print(f"❌ PDF not found at: {pdf_path}")

# Try image if it exists
if os.path.exists(image_path):
    print(f"\n✅ Found Image: {image_path}")
    try:
        img = Image.open(image_path)
        text = pytesseract.image_to_string(img)
        print(f"\n{'='*80}")
        print(f"IMAGE EXTRACTED TEXT:")
        print(f"{'='*80}")
        if text:
            print(text)
            print(f"\n[Text length: {len(text)} characters]")
        else:
            print("❌ NO TEXT EXTRACTED FROM IMAGE")
    except Exception as e:
        print(f"❌ IMAGE ERROR: {e}")
else:
    print(f"⚠️ Image not found at: {image_path}")

print("\n" + "=" * 80)