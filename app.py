from flask import Flask, request, jsonify
from PIL import Image, ImageDraw, ImageFont
import razorpay
import os
import requests
import hmac
import hashlib
import imghdr
import logging
import boto3
import urllib.parse
import qrcode

logging.basicConfig(level=logging.INFO)

from twilio.rest import Client

app = Flask(__name__)

# AWS credentials (set securely via environment or IAM roles)
s3 = boto3.client(
    's3',
    aws_access_key_id = os.getenv("AWS_ACCESS_KEY_ID"),
    aws_secret_access_key = os.getenv("AWS_SECRET_ACCESS_KEY")
)

# Load secrets from environment variables
razorpay_client = razorpay.Client(auth=(
    os.getenv("RAZORPAY_KEY_ID"),
    os.getenv("RAZORPAY_SECRET")
))

RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET")

twilio_client = Client(
    os.getenv("TWILIO_ACCOUNT_SID"),
    os.getenv("TWILIO_AUTH_TOKEN")
)

sessions = {}

LIBRARY_PLANS = {
    "6": 400,
    "12": 500,
    "24": 600
}
try:
    font = ImageFont.truetype("fonts/Roboto-Regular.ttf", 24)
except IOError:
    print("Font not found, using default.")
    font = ImageFont.load_default()
    
def upload_to_s3(file_path, bucket_name, s3_key):
    s3 = boto3.client('s3')

    try:
        s3.upload_file(
            Filename=file_path,
            Bucket=bucket_name,
            Key=s3_key,
            ExtraArgs={'ACL': 'public-read',  # ✅ Enable public read
            'ContentType': 'image/png'}
        )

        encoded_key = urllib.parse.quote(s3_key)
        public_url = f"https://{bucket_name}.s3.eu-north-1.amazonaws.com/{encoded_key}"

        return public_url
    except Exception as e:
        print(f"Upload failed: {e}")
        return None

def send_whatsapp(to, body, media_url=None):
    message_data = {
        'from_': 'whatsapp:+14155238886',
        'body': body
    }
    if media_url:
        message_data['media_url'] = [media_url]

    message = twilio_client.messages.create(to=to, **message_data)
    return message.sid

def generate_id_card(data, photo_path):
    if not os.path.exists("static"):
        os.makedirs("static")

    print("User Data:", data)

    WIDTH, HEIGHT = 600, 400
    MARGIN = 10

    # Create base card with white background
    card = Image.new('RGB', (WIDTH, HEIGHT), (255, 255, 255))
    draw = ImageDraw.Draw(card)

    # Load fonts
    try:
        title_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 28)
        body_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 20)
    except:
        title_font = body_font = ImageFont.load_default()

    # Draw border
    border_color = (0, 0, 0)
    draw.rectangle([0, 0, WIDTH-1, HEIGHT-1], outline=border_color, width=3)

    # Header background inside border
    header_height = 50
    border_margin = 7  # already defined later; define earlier if not
    draw.rectangle(
        [border_margin, border_margin, WIDTH - border_margin, border_margin + header_height],
        fill=(0, 0, 0)
    )

    # Header text (white, centered inside black box)
    header_text = "TARGET ZONE LIBRARY"
    text_width = draw.textlength(header_text, font=title_font)
    text_x = (WIDTH - text_width) // 2
    text_y = border_margin + (header_height - title_font.size) // 2  # vertical centering
    draw.text((text_x, text_y), header_text, font=title_font, fill=(255, 255, 255))

    # Starting Y position after header
    y_start = 100
    spacing = 37

    # Draw user info
    fields = [
        ("Name", data.get("name", "")),
        ("Father's Name", data.get("father_name", "")),
        ("Age", data.get("age", "")),
        ("Shift", f"{data.get('shift', '')} Hours"),
        ("Phone", data.get("phone", "")),
        ("Paid", f"Rs. {data.get('amount', '')}")
    ]

    for i, (label, value) in enumerate(fields):
        draw.text((20, y_start + i * spacing), f"{label}: {value}", font=body_font, fill=(0, 0, 0))

    # Paste photo (larger)
    try:
        user_img = Image.open(photo_path).resize((130, 130))
        card.paste(user_img, (440, 80))
    except Exception as e:
        logging.warning(f"Failed to load or paste user image: {e}")

    # Generate QR code
    qr_data = "\n".join([f"{k}: {v}" for k, v in data.items()])
    qr = qrcode.make(qr_data).resize((130, 130))
    card.paste(qr, (440, 220))

    border_color = (0, 0, 0)
    border_thickness = 5
    border_margin = 7  # margin from the card edge

    draw.rectangle(
        [border_margin, border_margin, WIDTH - border_margin, HEIGHT - border_margin],
        outline=border_color,
        width=border_thickness
    )
    #draw.rectangle([0, 0, WIDTH-1, HEIGHT-1], outline=border_color, width=10)

    # Save and upload
    path = f"static/id_{data.get('phone', 'unknown')}.png"
    card.save(path)

    head, tail = os.path.split(path)
    id_card_s3_path = upload_to_s3(path, 'library-id-cards', tail)

    logging.info(f"ID path is: {id_card_s3_path}")
    return id_card_s3_path

@app.route('/webhook', methods=['POST'])
def whatsapp_bot():
    try:
        incoming = request.form
        phone = incoming.get('From').split(':')[-1]
        msg = incoming.get('Body', '').strip()
        media_url = incoming.get('MediaUrl0', '')

        if phone not in sessions:
            sessions[phone] = {
                'stage': 'name',
                'phone': phone  # ✅ store phone for later use
            }
            send_whatsapp(f"whatsapp:{phone}", "Welcome to the Library. Please enter your full name:")
            return "OK"

        session = sessions[phone]

        if session['stage'] == 'name':
            session['name'] = msg
            session['stage'] = 'father_name'
            send_whatsapp(f"whatsapp:{phone}", "Enter your father's name:")
        elif session['stage'] == 'father_name':
            session['father_name'] = msg
            session['stage'] = 'age'
            send_whatsapp(f"whatsapp:{phone}", "Enter your age:")
        elif session['stage'] == 'age':
            session['age'] = msg
            session['stage'] = 'shift'
            send_whatsapp(f"whatsapp:{phone}", "Select shift (6/12/24 hours):")
        elif session['stage'] == 'shift':
            if msg not in LIBRARY_PLANS:
                send_whatsapp(f"whatsapp:{phone}", "Please enter a valid shift: 6, 12, or 24.")
            else:
                session['shift'] = msg
                session['amount'] = LIBRARY_PLANS[msg]
                session['stage'] = 'photo'
                send_whatsapp(f"whatsapp:{phone}", "Please upload your photo.")
        elif session['stage'] == 'photo':
            if not media_url:
                send_whatsapp(f"whatsapp:{phone}", "Please send a photo to continue.")
            else:
                if not os.path.exists("static"):
                    os.makedirs("static")
                photo_path = f"static/{phone}.jpg"
                r = requests.get(media_url, auth=(os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN")))
                with open(photo_path, 'wb') as f:
                    f.write(r.content)

        
                session['photo'] = photo_path

                # Create Razorpay payment link instead of raw order
                payment_link = razorpay_client.payment_link.create({
                    "amount": session['amount'] * 100,
                    "currency": "INR",
                    "accept_partial": False,
                    "description": "Library Membership",
                    "customer": {
                        "name": session['name'],
                        "contact": phone,
                        "email": f"{phone}@example.com"  # Dummy email
                    },
                    "notify": {"sms": False, "email": False}
                })

                session['payment_link_id'] = payment_link['id']
                session['stage'] = 'payment'
                send_whatsapp(f"whatsapp:{phone}", f"Please pay Rs. {session['amount']} using this link: {payment_link['short_url']}")

        elif session['stage'] == 'payment':
            send_whatsapp(f"whatsapp:{phone}", "Waiting for payment confirmation...")

        return "OK"
    except Exception as e:
        logging.info("Error in /webhook:", str(e))
        return "Error", 500

@app.route("/razorpay_webhook", methods=["POST"])
def razorpay_webhook():
    try:
        logging.info("🔔 Webhook triggered")
        payload = request.data
        received_signature = request.headers.get('X-Razorpay-Signature')

        logging.info(f"📦 Received headers: {dict(request.headers)}")
        logging.info(f"📨 Payload: {payload}")

        generated_signature = hmac.new(
            bytes(RAZORPAY_WEBHOOK_SECRET, 'utf-8'),
            msg=payload,
            digestmod=hashlib.sha256
        ).hexdigest()

        if hmac.compare_digest(received_signature, generated_signature):
            data = request.get_json()
            logging.info("✅ Verified webhook payload:", data)
            if data.get("event") == "payment_link.paid":
                entity = data['payload']['payment_link']['entity']
                contact = entity['customer']['contact']
                phone = contact.replace('+91', '')
                logging.info(f"Looking for session with phone: {phone}")
                session = sessions.get(phone) or sessions.get(f"+91{phone}") or sessions.get(f"whatsapp:+91{phone}")

                if session:
                    logging.info("📇 Session found, generating ID card...")
                    card_path = generate_id_card(session, session['photo'])
                    logging.info("ID_CARD path:",card_path)
                    send_whatsapp(f"whatsapp:+919071356842", "✅ Payment received! Here is your Library ID Card:", media_url=f"{card_path}")
                    session['stage'] = 'done'
                else:
                    logging.info(f"⚠️ No session found for phone:{ phone}")

            return jsonify({"status": "ok"}), 200
        else:
            logging.info("❌ Signature verification failed")
            return jsonify({"status": "invalid signature"}), 403
    except Exception as e:
        logging.info("Webhook error:", str(e))
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
