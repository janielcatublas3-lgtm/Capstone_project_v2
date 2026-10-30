import os
import json
import base64
import uuid
from io import BytesIO
from functools import wraps
from datetime import datetime, timezone

import bleach
import qrcode
import firebase_admin

from flask import Flask, render_template, request, redirect, url_for, session
from firebase_admin import credentials, firestore
from google.oauth2 import id_token
from google.auth.transport import requests
from werkzeug.security import generate_password_hash, check_password_hash


# =========================================================
# CONFIGURATION
# =========================================================

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-secret-key")
BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:5000").rstrip("/")
CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")

if not CLIENT_ID:
    raise RuntimeError("GOOGLE_CLIENT_ID environment variable is not set")


# =========================================================
# FIREBASE CONFIGURATION
# =========================================================

def initialize_firebase():
    if firebase_admin._apps:
        return

    firebase_credentials = os.environ.get("FIREBASE_CREDENTIALS")

    if firebase_credentials:
        service_account_info = json.loads(firebase_credentials)
        cred = credentials.Certificate(service_account_info)
        firebase_admin.initialize_app(cred)
        print("Firebase initialized using FIREBASE_CREDENTIALS.")
    elif os.path.exists("serviceAccountKey.json"):
        cred = credentials.Certificate("serviceAccountKey.json")
        firebase_admin.initialize_app(cred)
        print("Firebase initialized using serviceAccountKey.json.")
    else:
        raise RuntimeError(
            "Firebase credentials not found. Set FIREBASE_CREDENTIALS "
            "or provide serviceAccountKey.json for local development."
        )


initialize_firebase()
db = firestore.client()


# =========================================================
# HELPER FUNCTIONS
# =========================================================

def safe_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def normalize_customer_key(name):
    return " ".join((name or "").strip().casefold().split())


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "uid" not in session:
            return redirect(url_for("index"))
        return f(*args, **kwargs)
    return wrapper


def driver_login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "driver_id" not in session:
            return redirect(url_for("driver_login"))
        return f(*args, **kwargs)
    return wrapper


# =========================================================
# ADMIN LOGIN
# =========================================================

@app.route("/")
def index():
    return render_template("admin_log_in.html", google_client_id=CLIENT_ID)


@app.route("/google-auth", methods=["POST"])
def login_g_auth():
    token = request.form.get("token")
    if not token:
        return "Missing Token", 400

    try:
        google_account = id_token.verify_oauth2_token(
            token, requests.Request(), CLIENT_ID
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


# =========================================================
# ADMIN DASHBOARD
# =========================================================

@app.route("/admin_dashboard", methods=["GET", "POST"])
@login_required
def admin_dashboard():
    # Add a driver-assigned delivery (existing behavior)
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
            "driver_id": request.form.get("driver_id"),
            "driver_name": request.form.get("driver_name"),
            "delivery_date": request.form.get("delivery_date"),
            "date_assigned": now.strftime("%Y-%m-%d %H:%M:%S"),
            "fuel_cost": 0,
            "profit": 0
        })
        return redirect(url_for("admin_dashboard"))

    # Assignments
    data = []
    for doc in db.collection("assignments").stream():
        item = doc.to_dict()
        item["id"] = doc.id
        data.append(item)

    # Drivers
    drivers = []
    for doc in db.collection("drivers").stream():
        driver_data = doc.to_dict()
        driver_data["id"] = doc.id
        drivers.append(driver_data)

    # Completed delivery history
    history = [item for item in data if item.get("status") == "done"]

    # Active / pending delivery list
    deliveries = [
        item for item in data
        if item.get("status") in ["pending", "accepted", "declined"]
    ]

    # Business counts
    total_deliveries = len(data)
    completed_deliveries = sum(1 for item in data if item.get("status") == "done")
    pending_deliveries = sum(1 for item in data if item.get("status") == "pending")
    declined_deliveries = sum(1 for item in data if item.get("status") == "declined")
    total_drivers = len(drivers)

    customers = {
        normalize_customer_key(item.get("Customer_Name", ""))
        for item in data if item.get("Customer_Name")
    }
    total_customers = len(customers)

    # Walk-in sales and customer summaries (separate from delivery analytics)
    walk_in_sales = []
    for doc in db.collection("walk_in_sales").stream():
        sale = doc.to_dict()
        sale["id"] = doc.id
        sale["customer_name"] = sale.get("customer_name", "Unknown")
        sale["customer_key"] = sale.get(
            "customer_key", normalize_customer_key(sale["customer_name"])
        )
        sale["transaction_no"] = sale.get("transaction_no", doc.id)
        sale["date"] = sale.get("date", "")
        sale["water_tons"] = safe_float(sale.get("water_tons", 0))
        sale["price_per_ton"] = safe_float(sale.get("price_per_ton", 0))
        sale["total_income"] = safe_float(sale.get("total_income", 0))
        sale["profit"] = safe_float(sale.get("profit", sale["total_income"]))
        walk_in_sales.append(sale)

    walk_in_sales.sort(
        key=lambda sale: (sale.get("date", ""), sale.get("created_at_sort", ""),
                          sale.get("transaction_no", "")),
        reverse=True
    )

    customer_map = {}
    for sale in walk_in_sales:
        key = sale["customer_key"]
        if key not in customer_map:
            customer_map[key] = {
                "key": key,
                "name": sale["customer_name"],
                "transaction_count": 0,
                "total_income": 0.0,
                "total_profit": 0.0
            }
        customer_map[key]["transaction_count"] += 1
        customer_map[key]["total_income"] += sale["total_income"]
        customer_map[key]["total_profit"] += sale["profit"]

    walk_in_customers = sorted(
        customer_map.values(), key=lambda customer: customer["name"].casefold()
    )
    for customer in walk_in_customers:
        customer["total_income"] = round(customer["total_income"], 2)
        customer["total_profit"] = round(customer["total_profit"], 2)

 
    # =========================================================
    # DELIVERY FINANCIAL ANALYTICS
    # Uses scheduled delivery_date for all date-based reports
    # =========================================================

    today = datetime.now(timezone.utc).date()

    daily = 0.0
    weekly = 0.0
    monthly = 0.0
    total_income = 0.0
    total_fuel = 0.0

    analytics = {}
    monthly_analytics = {}

    for item in history:
        try:
            # Use scheduled delivery date, not completion date
            date_str = item.get("delivery_date")

            if not date_str:
                continue

            delivery_date = datetime.strptime(
                date_str, "%Y-%m-%d"
            ).date()

            income = safe_float(item.get("total_price", 0))
            fuel = safe_float(item.get("fuel_cost", 0))
            profit = safe_float(item.get("profit", income - fuel))

            # Overall totals for completed deliveries
            total_income += income
            total_fuel += fuel

            # Daily profit based on scheduled delivery date
            if delivery_date == today:
                daily += profit

            # Weekly profit based on scheduled delivery date
            if delivery_date.isocalendar()[:2] == today.isocalendar()[:2]:
                weekly += profit

            # Monthly profit based on scheduled delivery date
            if (
                delivery_date.year == today.year
                and delivery_date.month == today.month
            ):
                monthly += profit

            # Group profit by scheduled delivery month
            month_key = delivery_date.strftime("%Y-%m")

            monthly_analytics[month_key] = (
                monthly_analytics.get(month_key, 0.0) + profit
            )

            # Chart data grouped by scheduled delivery day
            chart_day = delivery_date.strftime("%b %d")

            analytics[chart_day] = (
                analytics.get(chart_day, 0.0) + profit
            )

        except (ValueError, TypeError) as e:
            print("Analytics date error:", e)

    # Chart values
    chart_labels = list(analytics.keys())
    chart_values = list(analytics.values())

    # Peak Month and Lowest Month
    if monthly_analytics:
        peak_key = max(
            monthly_analytics,
            key=monthly_analytics.get
        )

        lowest_key = min(
            monthly_analytics,
            key=monthly_analytics.get
        )

        peak_month = datetime.strptime(
            peak_key, "%Y-%m"
        ).strftime("%B %Y")

        lowest_month = datetime.strptime(
            lowest_key, "%Y-%m"
        ).strftime("%B %Y")

    else:
        peak_month = "No Data"
        lowest_month = "No Data"


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
        total_deliveries=total_deliveries,
        completed_deliveries=completed_deliveries,
        pending_deliveries=pending_deliveries,
        declined_deliveries=declined_deliveries,
        total_drivers=total_drivers,
        total_customers=total_customers,
        chart_labels=chart_labels,
        chart_values=chart_values,
        peak_month=peak_month,
        lowest_month=lowest_month,
        walk_in_customers=walk_in_customers,
        walk_in_sales=walk_in_sales,
        today=datetime.now().strftime("%Y-%m-%d"),
    )


# =========================================================
# WALK-IN CUSTOMER TRANSACTIONS
# =========================================================

@app.route("/add_walk_in_sale", methods=["POST"])
@login_required
def add_walk_in_sale():

    customer_name = bleach.clean(
        request.form.get("customer_name", "").strip()
    )
    customer_name = " ".join(customer_name.split())

    tons = safe_float(request.form.get("tons"))
    price = safe_float(request.form.get("price"))

    if not customer_name:
        return "Customer name is required.", 400

    if tons <= 0 or price < 0:
        return "Enter a valid water quantity and price.", 400

    # Current transaction creation time
    now = datetime.now(timezone.utc)

    # Get the actual purchase date selected by the admin
    transaction_date = request.form.get("date", "").strip()

    # Use today's date if no date was provided
    if not transaction_date:
        transaction_date = now.strftime("%Y-%m-%d")

    # Validate the selected date
    try:
        selected_date = datetime.strptime(
            transaction_date, "%Y-%m-%d"
        ).date()
    except ValueError:
        return "Invalid transaction date.", 400

    if selected_date > now.date():
        return "Transaction date cannot be in the future.", 400

    # Generate transaction details
    transaction_id = str(uuid.uuid4())

    transaction_no = (
        f"WI-{transaction_date.replace('-', '')}-"
        f"{uuid.uuid4().hex[:6].upper()}"
    )

    total_income = round(tons * price, 2)

    # Save walk-in transaction to Firestore
    db.collection("walk_in_sales").document(transaction_id).set({
        "transaction_no": transaction_no,
        "customer_name": customer_name,
        "customer_key": normalize_customer_key(customer_name),
        "water_tons": tons,
        "price_per_ton": price,
        "total_income": total_income,
        "profit": total_income,

        # Actual purchase date selected by the admin
        "date": transaction_date,

        # Actual time the transaction was recorded
        "created_at": now
    })

    return redirect(url_for("admin_dashboard"))


# =========================================================
# DRIVER LOGIN
# =========================================================

@app.route("/driver_login", methods=["GET", "POST"])
def driver_login():
    if request.method == "GET":
        return render_template("driver_login.html")

    username = request.form.get("username", "").strip()
    password = request.form.get("password", "").strip()

    drivers = db.collection("drivers").where("username", "==", username).stream()
    for doc in drivers:
        data = doc.to_dict()
        if data.get("username") == username:
            stored_password = data.get("password")
            if stored_password and check_password_hash(stored_password, password):
                session.clear()
                session["driver_id"] = doc.id
                session["driver_name"] = data.get("name")
                return redirect("/driver")
            return "Incorrect password."

    return "Username not found."


# =========================================================
# DRIVER DASHBOARD
# =========================================================

@app.route("/driver")
@driver_login_required
def driver():
    driver_id = session["driver_id"]
    docs = db.collection("assignments").where("driver_id", "==", driver_id).stream()

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


# =========================================================
# ACCEPT DELIVERY
# =========================================================

@app.route("/accept_delivery/<id>", methods=["POST"])
@driver_login_required
def accept_delivery(id):
    doc_ref = db.collection("assignments").document(id)
    doc = doc_ref.get()

    if not doc.exists:
        return "Delivery not found.", 404

    data = doc.to_dict()
    if data.get("driver_id") != session.get("driver_id"):
        return "Unauthorized.", 403

    doc_ref.update({
        "status": "accepted",
        "accepted_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    })
    return redirect("/driver")


# =========================================================
# DECLINE DELIVERY
# =========================================================

@app.route("/decline_delivery/<id>", methods=["POST"])
@driver_login_required
def decline_delivery(id):
    doc_ref = db.collection("assignments").document(id)
    doc = doc_ref.get()

    if not doc.exists:
        return "Delivery not found.", 404

    data = doc.to_dict()
    if data.get("driver_id") != session.get("driver_id"):
        return "Unauthorized.", 403

    doc_ref.update({
        "status": "declined",
        "declined_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    })
    return redirect("/driver")


# =========================================================
# DRIVER DELIVERY UPDATE
# =========================================================

@app.route("/driver_update/<id>", methods=["POST"])
@driver_login_required
def driver_update(id):
    start = safe_float(request.form.get("startOdo"))
    end = safe_float(request.form.get("endOdo"))
    fuel = safe_float(request.form.get("FuelUsed"))
    price = safe_float(request.form.get("FuelPrice"))
    fuel_cost = fuel * price

    doc_ref = db.collection("assignments").document(id)
    doc = doc_ref.get()

    if not doc.exists:
        return "Delivery not found.", 404

    data = doc.to_dict()
    if data.get("driver_id") != session.get("driver_id"):
        return "Unauthorized.", 403

    income = safe_float(data.get("total_price", 0))
    profit = income - fuel_cost

    doc_ref.update({
        "distance": max(0, end - start),
        "fuel_cost": fuel_cost,
        "profit": profit,
        "status": "done",
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d")
    })
    return redirect("/driver")


# =========================================================
# LOGOUT
# =========================================================

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")


@app.route("/driver_logout")
def driver_logout():
    session.clear()
    return redirect("/driver_login")


# =========================================================
# HEALTH CHECK
# =========================================================

@app.route("/health")
def health():
    return {"status": "ok"}, 200


# =========================================================
# LOCAL DEVELOPMENT
# =========================================================

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000)),
        debug=False
    )
