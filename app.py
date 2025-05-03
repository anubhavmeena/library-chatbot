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

logging.basicConfig(level=logging.INFO)

from twilio.rest import Client

app = Flask(__name__)

# AWS credentials (set securely via environment or IAM roles)
s3 = boto3.client(
    's3',
    aws_access_key_id='AKIAUIU535QGLA5X6PGS',
    aws_secret_access_key='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'
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

def upload_to_s3(file_path, bucket_name, object_name):
    s3.upload_file(file_path, bucket_name, object_name})
    url = f"https://{bucket_name}.s3.amazonaws.com/{object_name}"
    return url
    
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

    card = Image.new('RGB', (600, 400), (255, 255, 255))
    draw = ImageDraw.Draw(card)
    from PIL import ImageFont
    font = ImageFont.load_default()
    draw.text((20, 20), f"Name: {data['name']}", font=font)
    draw.text((20, 60), f"Father's Name: {data['father_name']}", font=font)
    draw.text((20, 100), f"Age: {data['age']}", font=font)
    draw.text((20, 140), f"Shift: {data['shift']} Hours", font=font)
    draw.text((20, 180), f"Phone: {data['phone']}", font=font)
    draw.text((20, 220), f"Paid: Rs. {data['amount']}", font=font)

    user_img = Image.open(photo_path).resize((100, 100))
    card.paste(user_img, (450, 20))

    path = f"static/id_{data['phone']}.png"
    card.save(path)
    head, tail = os.path.split(path)
    id_card_s3_path = upload_to_s3(path, 'library-id-cards', tail)
    logging.info("ID path is:",id_card_s3_path)
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
                    send_whatsapp(f"whatsapp:{phone}", "✅ Payment received! Here is your Library ID Card:", media_url=f"http://ec2-16-16-216-109.eu-north-1.compute.amazonaws.com:5000/{card_path}")
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
