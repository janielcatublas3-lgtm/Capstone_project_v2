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

# ================= FIREBASE =================
cred = credentials.Certificate("serviceAccountKey.json")

if not firebase_admin._apps:
    firebase_admin.initialize_app(cred)

db = firestore.client()

# ================= APP =================
app = Flask(__name__)
app.secret_key = "your_super_secret_key"

CLIENT_ID = "737562037990-n21m7uc217rhihqs83r5j5hene4b2jf6.apps.googleusercontent.com"

# ================= GET LOCAL IP =================
hostname = socket.gethostname()
local_ip = socket.gethostbyname(hostname)

# ================= LOGIN REQUIRED =================
def login_required(f):

    @wraps(f)

    def wrapper(*args, **kwargs):

        if 'uid' not in session and 'driver_id' not in session:
            return redirect(url_for('index'))

        return f(*args, **kwargs)

    return wrapper


# ================= SAFE FLOAT =================
def safe_float(value):

    try:
        return float(value)

    except:
        return 0.0


# ================= HOME =================
@app.route("/")
def index():

    return render_template(
        "admin_log_in.html"
    )


# ================= GOOGLE LOGIN =================
@app.route("/google-auth", methods=["POST"])
def login_g_auth():

    token = request.form.get("token")

    try:

        google_account = id_token.verify_oauth2_token(
            token,
            requests.Request(),
            CLIENT_ID
        )

        session["uid"] = google_account["sub"]

        session["email"] = google_account.get(
            "email"
        )

        session["name"] = google_account.get(
            "name",
            "Admin"
        )

        return redirect(
            url_for("admin_dashboard")
        )

    except Exception as e:

        return f"Login Failed: {str(e)}", 400


# ================= ADMIN DASHBOARD =================
@app.route("/admin_dashboard", methods=["GET", "POST"])
@login_required
def admin_dashboard():

    # ================= ADD DELIVERY =================
    if request.method == "POST":

        uid = str(uuid.uuid4())

        now = datetime.now(timezone.utc)

        tons = safe_float(
            request.form.get("tons")
        )

        price = safe_float(
            request.form.get("price")
        )

        total_price = tons * price

        db.collection("assignments").document(uid).set({

            "Customer_Name": bleach.clean(
                request.form.get("CustomerName", "")
            ),

            "location": bleach.clean(
                request.form.get("location", "")
            ),

            "water_tons": tons,

            "price_per_ton": price,

            "total_price": total_price,

            "status": "pending",

            "date_assigned": now.strftime(
                "%Y-%m-%d %H:%M:%S"
            ),

            "fuel_cost": 0,

            "profit": 0
        })

        return redirect(
            url_for("admin_dashboard")
        )

    # ================= GET FIRESTORE DATA =================
    docs = db.collection("assignments").stream()

    data = []

    for doc in docs:

        item = doc.to_dict()

        item["id"] = doc.id

        data.append(item)

    # ================= HISTORY =================
    history = [

        x for x in data

        if x.get("status") == "done"

    ]

    # ================= DATE =================
    today = datetime.now().date()

    current_week = today.isocalendar()[1]

    current_month = today.month

    current_year = today.year

    # ================= TOTALS =================
    daily = 0

    weekly = 0

    monthly = 0

    total_income = 0

    total_fuel = 0

    analytics = {}

    # ================= ANALYTICS =================
    for h in history:

        try:

            date_str = h.get("date")

            if not date_str:
                continue

            delivery_date = datetime.strptime(
                date_str,
                "%Y-%m-%d"
            ).date()

            income = float(
                h.get("total_price", 0)
            )

            fuel = float(
                h.get("fuel_cost", 0)
            )

            profit = float(
                h.get("profit", 0)
            )

            total_income += income

            total_fuel += fuel

            # ================= DAILY =================
            if delivery_date == today:

                daily += profit

            # ================= WEEKLY =================
            if (
                delivery_date.isocalendar()[1]
                == current_week
                and delivery_date.year
                == current_year
            ):

                weekly += profit

            # ================= MONTHLY =================
            if (
                delivery_date.month
                == current_month
                and delivery_date.year
                == current_year
            ):

                monthly += profit

            # ================= CHART =================
            chart_day = delivery_date.strftime(
                "%b %d"
            )

            if chart_day not in analytics:

                analytics[chart_day] = 0

            analytics[chart_day] += profit

        except:
            pass

    # ================= SORT CHART =================
    sorted_analytics = dict(
        sorted(analytics.items())
    )

    chart_labels = list(
        sorted_analytics.keys()
    )

    chart_values = list(
        sorted_analytics.values()
    )

    # ================= NET PROFIT =================
    net_profit = total_income - total_fuel

    return render_template(

        "admin_dashboard.html",

        Input_Dashboard=data,

        history=history,

        daily=round(daily, 2),

        weekly=round(weekly, 2),

        monthly=round(monthly, 2),

        total_income=round(total_income, 2),

        total_fuel=round(total_fuel, 2),

        net_profit=round(net_profit, 2),

        chart_labels=chart_labels,

        chart_values=chart_values
    )


# ================= ADD DRIVER =================
@app.route("/add_driver", methods=["POST"])
@login_required
def add_driver():

    driver_id = str(uuid.uuid4())

    # ================= DRIVER QR URL =================
    qr_url = f"http://{local_ip}:5000/driver_qr_login/{driver_id}"

    # ================= GENERATE QR =================
    qr = qrcode.make(qr_url)

    buffer = BytesIO()

    qr.save(buffer)

    qr_base64 = base64.b64encode(
        buffer.getvalue()
    ).decode()

    # ================= SAVE DRIVER =================
    db.collection("drivers").document(driver_id).set({

        "name": request.form.get("name"),

        "address": request.form.get("address"),

        "contact": request.form.get("contact"),

        "qr_url": qr_url,

        "qr_image": qr_base64,

        "created_at": datetime.now(timezone.utc)
    })

    return f"""
    <h2>Driver Created Successfully</h2>

    <p>Scan this QR to login:</p>

    <img src="data:image/png;base64,{qr_base64}" width="250">

    <br><br>

    <a href="/admin_dashboard">Back to Dashboard</a>
    """


# ================= QR LOGIN =================
@app.route("/driver_qr_login/<driver_id>")
def driver_qr_login(driver_id):

    driver = db.collection(
        "drivers"
    ).document(driver_id).get()

    if not driver.exists:
        return "Invalid QR Code", 400

    data = driver.to_dict()

    session["driver_id"] = driver_id

    session["driver_name"] = data.get("name")

    return redirect(
        url_for("driver")
    )


# ================= DRIVER PAGE =================
@app.route("/driver")
def driver():

    if "driver_id" not in session:
        return redirect("/")

    docs = db.collection("assignments").stream()

    data = []

    for doc in docs:

        item = doc.to_dict()

        item["id"] = doc.id

        data.append(item)

    return render_template(

        "driver_side.html",

        Input_Dashboard=data
    )


# ================= ACCEPT DELIVERY =================
@app.route("/accept_delivery/<id>", methods=["POST"])
def accept_delivery(id):

    db.collection(
        "assignments"
    ).document(id).update({

        "status": "accepted"
    })

    return redirect("/driver")


# ================= DECLINE DELIVERY =================
@app.route("/decline_delivery/<id>", methods=["POST"])
def decline_delivery(id):

    db.collection(
        "assignments"
    ).document(id).update({

        "status": "declined"
    })

    return redirect("/driver")


# ================= DRIVER UPDATE =================
@app.route("/driver_update/<id>", methods=["POST"])
def driver_update(id):

    start = safe_float(
        request.form.get("startOdo")
    )

    end = safe_float(
        request.form.get("endOdo")
    )

    fuel = safe_float(
        request.form.get("FuelUsed")
    )

    price = safe_float(
        request.form.get("FuelPrice")
    )

    distance = max(0, end - start)

    fuel_cost = fuel * price

    doc = db.collection(
        "assignments"
    ).document(id).get().to_dict()

    income = float(
        doc.get("total_price", 0)
    )

    profit = income - fuel_cost

    db.collection(
        "assignments"
    ).document(id).update({

        "distance": distance,

        "fuel_cost": fuel_cost,

        "profit": profit,

        "status": "done",

        "date": datetime.now(
            timezone.utc
        ).strftime("%Y-%m-%d")
    })

    return redirect("/driver")


# ================= LOGOUT =================
@app.route("/logout")
def logout():

    session.clear()

    return redirect("/")


# ================= AUTO OPEN BROWSER =================
def open_browser():

    webbrowser.open_new(
        "http://127.0.0.1:5000/"
    )


# ================= RUN APP =================
if __name__ == "__main__":

    Timer(1, open_browser).start()

    app.run(

        host="0.0.0.0",

        port=5000,

        debug=True
    )