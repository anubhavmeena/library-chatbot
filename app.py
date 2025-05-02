from flask import Flask, request, jsonify
from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError
import razorpay
import os
import requests
import hmac
import hashlib

from twilio.rest import Client

app = Flask(__name__)

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
    font = ImageFont.load_default()
    draw.text((20, 20), f"Name: {data['name']}", font=font)
    draw.text((20, 60), f"Father's Name: {data['father_name']}", font=font)
    draw.text((20, 100), f"Age: {data['age']}", font=font)
    draw.text((20, 140), f"Shift: {data['shift']} Hours", font=font)
    draw.text((20, 180), f"Phone: {data['phone']}", font=font)
    draw.text((20, 220), f"Paid: Rs. {data['amount']}", font=font)

    try:
        user_img = Image.open(photo_path).resize((100, 100))
        card.paste(user_img, (450, 20))
    except Exception as e:
        print("🖼️ Failed to open photo:", str(e))

    path = f"static/id_{data['phone']}.png"
    card.save(path)

    # ✅ Auto-delete photo after use
    try:
        os.remove(photo_path)
    except Exception as e:
        print("⚠️ Failed to delete photo:", str(e))

    return path

@app.route("/webhook", methods=["POST"])
def whatsapp_bot():
    try:
        incoming = request.form
        phone = incoming.get('From').split(':')[-1]
        msg = incoming.get('Body', '').strip()
        media_url = incoming.get('MediaUrl0', '')

        if phone not in sessions:
            sessions[phone] = {'stage': 'name', 'phone': phone}
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
                r = requests.get(media_url)
                with open(photo_path, 'wb') as f:
                    f.write(r.content)

                # ✅ Validate image with Pillow
                try:
                    with Image.open(photo_path) as img:
                        img.verify()
                except UnidentifiedImageError:
                    os.remove(photo_path)
                    send_whatsapp(f"whatsapp:{phone}", "The file you sent is not a valid image. Please resend.")
                    return "OK"

                session['photo'] = photo_path

                order = razorpay_client.order.create({
                    "amount": session['amount'] * 100,
                    "currency": "INR",
                    "payment_capture": "1"
                })
                session['order_id'] = order['id']
                session['stage'] = 'payment'

                pay_link = f"https://rzp.io/l/{order['id']}"
                send_whatsapp(f"whatsapp:{phone}", f"Please pay Rs. {session['amount']} using this link: {pay_link}")
        elif session['stage'] == 'payment':
            send_whatsapp(f"whatsapp:{phone}", "Waiting for payment confirmation...")

        return "OK"
    except Exception as e:
        print("Error in /webhook:", str(e))
        return "Error", 500
