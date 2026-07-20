from flask import Flask, render_template, request, redirect, url_for, session
import firebase_admin
from firebase_admin import credentials, firestore
from google.oauth2 import id_token
from google.auth.transport import requests
from datetime import datetime, timezone
import bleach
import uuid
from functools import wraps
import qrcode
import base64
from io import BytesIO
import webbrowser
from threading import Timer
import socket
from werkzeug.security import generate_password_hash, check_password_hash

# ================= FIREBASE =================
cred = credentials.Certificate("serviceAccountKey.json")

if not firebase_admin._apps:
    firebase_admin.initialize_app(cred)

db = firestore.client()

# ================= APP =================
app = Flask(__name__)
app.secret_key = "your_super_secret_key"

CLIENT_ID = "737562037990-n21m7uc217rhihqs83r5j5hene4b2jf6.apps.googleusercontent.com"

# ================= LOCAL IP =================
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.connect(("8.8.8.8", 80))
local_ip = s.getsockname()[0]
s.close()

# ================= SAFE FLOAT =================
def safe_float(value):
    try:
        return float(value)
    except:
        return 0.0

# ================= LOGIN REQUIRED =================
def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "uid" not in session:
            return redirect(url_for("index"))
        return f(*args, **kwargs)
    return wrapper

# ================= HOME =================
@app.route("/")
def index():
    return render_template("admin_log_in.html")

# ================= GOOGLE AUTH =================
@app.route("/google-auth", methods=["POST"])
def login_g_auth():
    token = request.form.get("token")

    if not token:
        return "Missing Token", 400

    try:
        google_account = id_token.verify_oauth2_token(
            token,
            requests.Request(),
            CLIENT_ID
        )

        session.clear()

        session["uid"] = google_account["sub"]
        session["email"] = google_account["email"]
        session["name"] = google_account.get("name", "User")

        db.collection("users").document(session["uid"]).set({
            "email": session["email"],
            "name": session["name"],
            "last_login": datetime.now(timezone.utc)
        }, merge=True)

        return redirect(url_for("admin_dashboard"))

    except Exception as e:
        print("GOOGLE LOGIN ERROR:", e)
        return "Invalid Login", 400

# ================= ADMIN DASHBOARD =================
@app.route("/admin_dashboard", methods=["GET", "POST"])
@login_required
def admin_dashboard():

    # ================= ADD DELIVERY =================
    if request.method == "POST":

        uid = str(uuid.uuid4())
        now = datetime.now(timezone.utc)

        tons = safe_float(request.form.get("tons"))
        price = safe_float(request.form.get("price"))

        total_price = tons * price

        db.collection("assignments").document(uid).set({

            "Customer_Name": bleach.clean(request.form.get("CustomerName", "")),
            "location": bleach.clean(request.form.get("location", "")),
            "water_tons": tons,
            "price_per_ton": price,
            "total_price": total_price,

            "status": "pending",

            # DRIVER INFO (FIXED)
            "driver_id": request.form.get("driver_id"),
            "driver_name": request.form.get("driver_name"),

            "delivery_date": request.form.get("delivery_date"),

            "date_assigned": now.strftime("%Y-%m-%d %H:%M:%S"),
            "fuel_cost": 0,
            "profit": 0
        })

        return redirect(url_for("admin_dashboard"))

    # ================= ASSIGNMENTS =================
    docs = db.collection("assignments").stream()
    data = []

    for doc in docs:
        item = doc.to_dict()
        item["id"] = doc.id
        data.append(item)

    # ================= DRIVERS =================
    drivers = []
    for doc in db.collection("drivers").stream():
        d = doc.to_dict()
        d["id"] = doc.id
        drivers.append(d)

    # ================= HISTORY =================
    history = [x for x in data if x.get("status") == "done"]
    deliveries = [
    x for x in data
    if x.get("status") in ["pending", "accepted", "declined"]
]

    today = datetime.now().date()
    daily = weekly = monthly = 0
    total_income = 0
    total_fuel = 0
    analytics = {}

    for h in history:
        try:
            date_str = h.get("date")
            if not date_str:
                continue

            delivery_date = datetime.strptime(date_str, "%Y-%m-%d").date()

            income = float(h.get("total_price", 0))
            fuel = float(h.get("fuel_cost", 0))
            profit = float(h.get("profit", 0))

            total_income += income
            total_fuel += fuel

            if delivery_date == today:
                daily += profit

            chart_day = delivery_date.strftime("%b %d")
            analytics[chart_day] = analytics.get(chart_day, 0) + profit

        except:
            pass

    chart_labels = list(analytics.keys())
    chart_values = list(analytics.values())

    return render_template(
        "admin_dashboard.html",
        Input_Dashboard=data,
        drivers=drivers,
        history=history,
        deliveries=deliveries,
        daily=round(daily, 2),
        weekly=round(weekly, 2),
        monthly=round(monthly, 2),
        total_income=round(total_income, 2),
        total_fuel=round(total_fuel, 2),
        net_profit=round(total_income - total_fuel, 2),
        chart_labels=chart_labels,
        chart_values=chart_values
    )

# ================= ADD DRIVER =================
@app.route("/add_driver", methods=["POST"])
@login_required
def add_driver():

    driver_id = str(uuid.uuid4())
    qr_url = f"http://{local_ip}:5000/register/{driver_id}"

    qr = qrcode.make(qr_url)
    buffer = BytesIO()
    qr.save(buffer)

    qr_base64 = base64.b64encode(buffer.getvalue()).decode()

    db.collection("drivers").document(driver_id).set({
    "name": request.form.get("name"),
    "address": request.form.get("address"),
    "contact": request.form.get("contact"),

    "registered": False,
    "username": "",
    "password": "",

    "qr_url": qr_url,
    "qr_image": qr_base64,
    "created_at": datetime.now(timezone.utc)
})

    return f"""
    <h2>Driver Created</h2>
    <img src="data:image/png;base64,{qr_base64}" width="250">
    <br><a href="/admin_dashboard">Back</a>
    """


# ================= DRIVER REGISTER =================
@app.route("/register/<driver_id>", methods=["GET", "POST"])
def register(driver_id):

    driver_ref = db.collection("drivers").document(driver_id)
    driver = driver_ref.get()

    if not driver.exists:
        return "Invalid QR Code", 404

    data = driver.to_dict()

    if request.method == "GET":

        if data.get("registered"):
            return redirect("/driver_login")

        return render_template(
            "register.html",
            driver_name=data.get("name"),
            driver_id=driver_id
        )

    username = request.form.get("username", "").strip()
    password = request.form.get("password", "").strip()
    confirm = request.form.get("confirm_password", "").strip()

    if password != confirm:
        return "Passwords do not match."

    # Check if username already exists
    drivers = db.collection("drivers").stream()

    for d in drivers:
        info = d.to_dict()
        if info.get("username") == username:
            return "Username already exists."

    driver_ref.update({
        "username": username,
        "password": generate_password_hash(password),
        "registered": True
    })

    return redirect("/driver_login")


# ================= DRIVER LOGIN =================
@app.route("/driver_login", methods=["GET", "POST"])
def driver_login():

    if request.method == "GET":
        return render_template("driver_login.html")

    username = request.form.get("username", "").strip()
    password = request.form.get("password", "").strip()

    drivers = db.collection("drivers").where("username","==",username).stream()

    for doc in drivers:

        data = doc.to_dict()

        if data.get("username") == username:

            if check_password_hash(data.get("password"), password):

                session.clear()

                session["driver_id"] = doc.id
                session["driver_name"] = data.get("name")

                return redirect("/driver")

            return "Incorrect password."

    return "Username not found."

# ================= DRIVER PAGE =================
@app.route("/driver")
def driver():

    if "driver_id" not in session:
        return redirect("/driver_login")

    driver_id = session["driver_id"]

    docs = (
    db.collection("assignments")
    .where("driver_id", "==", driver_id)
    .stream()
)
    data = []
    for doc in docs:
        item = doc.to_dict()
        item["id"] = doc.id
        data.append(item)

    return render_template(
        "driver_side.html",
        Input_Dashboard=data,
        driver_name=session.get("driver_name")
    )

# ================= ACCEPT =================
@app.route("/accept_delivery/<id>", methods=["POST"])
def accept_delivery(id):

    if "driver_id" not in session:
        return redirect("/")

    db.collection("assignments").document(id).update({
        "status": "accepted",
"accepted_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    })

    return redirect("/driver")

# ================= DECLINE =================
@app.route("/decline_delivery/<id>", methods=["POST"])
def decline_delivery(id):

    if "driver_id" not in session:
        return redirect("/")

    db.collection("assignments").document(id).update({
       "status": "declined",
"declined_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    })

    return redirect("/driver")

# ================= UPDATE =================
@app.route("/driver_update/<id>", methods=["POST"])
def driver_update(id):

    if "driver_id" not in session:
        return redirect("/")

    start = safe_float(request.form.get("startOdo"))
    end = safe_float(request.form.get("endOdo"))
    fuel = safe_float(request.form.get("FuelUsed"))
    price = safe_float(request.form.get("FuelPrice"))

    fuel_cost = fuel * price

    doc = db.collection("assignments").document(id).get()
    data = doc.to_dict()

    income = float(data.get("total_price", 0))
    profit = income - fuel_cost

    db.collection("assignments").document(id).update({
        "distance": max(0, end - start),
        "fuel_cost": fuel_cost,
        "profit": profit,
        "status": "done",
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d")
    })

    return redirect("/driver")

# ================= LOGOUT =================
@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")


# ================= DRIVER LOGOUT =================
@app.route("/driver_logout")
def driver_logout():

    session.clear()

    return redirect("/driver_login")


# ================= RUN =================
if __name__ == "__main__":
    Timer(1, lambda: webbrowser.open("http://127.0.0.1:5000/")).start()
    app.run(host="0.0.0.0", port=5000, debug=False)