import os
from flask import Flask
from threading import Thread

app = Flask('')

@app.route('/')
def home():
    return "Bot is running 24/7"

def run():
    # Render-এর ডাইনামিক পোর্ট ধরবে, না পেলে ডিফল্ট 8080 ব্যবহার করবে
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run)
    t.start()
