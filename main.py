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

# Production secret key
app.secret_key = os.environ.get("SECRET_KEY", "change-this-secret-key")

# Public website URL
# Example:
# https://rpvwaterdelivery.com
#
# For Render without custom domain:
# https://your-app-name.onrender.com
BASE_URL = os.environ.get(
    "BASE_URL",
    "http://127.0.0.1:5000"
).rstrip("/")


# Google OAuth Client ID
CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")

if not CLIENT_ID:
    raise RuntimeError("GOOGLE_CLIENT_ID environment variable is not set")


# =========================================================
# FIREBASE CONFIGURATION
# =========================================================
#
# Production:
# Store Firebase service account JSON in the
# FIREBASE_CREDENTIALS environment variable.
#
# Local development:
# You can still use serviceAccountKey.json.
#

def initialize_firebase():

    if firebase_admin._apps:
        return

    firebase_credentials = os.environ.get("FIREBASE_CREDENTIALS")

    if firebase_credentials:
        try:
            # If the environment variable contains JSON directly
            service_account_info = json.loads(firebase_credentials)

            cred = credentials.Certificate(service_account_info)

            firebase_admin.initialize_app(cred)

            print("Firebase initialized using FIREBASE_CREDENTIALS.")

        except Exception as e:
            print("Firebase environment credential error:", e)
            raise

    elif os.path.exists("serviceAccountKey.json"):

        # Local development fallback
        cred = credentials.Certificate("serviceAccountKey.json")

        firebase_admin.initialize_app(cred)

        print("Firebase initialized using serviceAccountKey.json.")

    else:

        raise RuntimeError(
            "Firebase credentials not found. "
            "Set FIREBASE_CREDENTIALS environment variable "
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

    return render_template(
        "admin_log_in.html",
        google_client_id=CLIENT_ID
    )


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

        db.collection("users").document(
            session["uid"]
        ).set(
            {
                "email": session["email"],
                "name": session["name"],
                "last_login": datetime.now(timezone.utc)
            },
            merge=True
        )

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

    # -----------------------------------------------------
    # ADD DELIVERY
    # -----------------------------------------------------

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
                request.form.get(
                    "CustomerName",
                    ""
                )
            ),

            "location": bleach.clean(
                request.form.get(
                    "location",
                    ""
                )
            ),

            "water_tons": tons,

            "price_per_ton": price,

            "total_price": total_price,

            "status": "pending",

            # Driver information
            "driver_id": request.form.get(
                "driver_id"
            ),

            "driver_name": request.form.get(
                "driver_name"
            ),

            "delivery_date": request.form.get(
                "delivery_date"
            ),

            "date_assigned": now.strftime(
                "%Y-%m-%d %H:%M:%S"
            ),

            "fuel_cost": 0,

            "profit": 0

        })

        return redirect(
            url_for("admin_dashboard")
        )


    # -----------------------------------------------------
    # GET ASSIGNMENTS
    # -----------------------------------------------------

    docs = db.collection(
        "assignments"
    ).stream()

    data = []

    for doc in docs:

        item = doc.to_dict()

        item["id"] = doc.id

        data.append(item)


    # -----------------------------------------------------
    # GET DRIVERS
    # -----------------------------------------------------

    drivers = []

    for doc in db.collection(
        "drivers"
    ).stream():

        d = doc.to_dict()

        d["id"] = doc.id

        drivers.append(d)


    # -----------------------------------------------------
    # HISTORY
    # -----------------------------------------------------

    history = [
        x for x in data
        if x.get("status") == "done"
    ]


    # -----------------------------------------------------
    # ACTIVE DELIVERIES
    # -----------------------------------------------------

    deliveries = [
        x for x in data
        if x.get("status")
        in [
            "pending",
            "accepted",
            "declined"
        ]
    ]


    # -----------------------------------------------------
    # BUSINESS COUNTS
    # -----------------------------------------------------

    total_deliveries = len(data)

    completed_deliveries = sum(
        1
        for x in data
        if x.get("status") == "done"
    )

    pending_deliveries = sum(
        1
        for x in data
        if x.get("status") == "pending"
    )

    declined_deliveries = sum(
        1
        for x in data
        if x.get("status") == "declined"
    )

    total_drivers = len(drivers)


    # -----------------------------------------------------
    # CUSTOMERS
    # -----------------------------------------------------

    customers = set()

    for item in data:

        customer = item.get(
            "Customer_Name"
        )

        if customer:
            customers.add(customer)

    total_customers = len(customers)


    # -----------------------------------------------------
    # DATE / FINANCIAL ANALYTICS
    # -----------------------------------------------------

    today = datetime.now(
        timezone.utc
    ).date()

    daily = 0
    weekly = 0
    monthly = 0

    total_income = 0
    total_fuel = 0

    analytics = {}

    monthly_analytics = {}


    # -----------------------------------------------------
    # HISTORY ANALYTICS
    # -----------------------------------------------------

    for h in history:

        try:

            date_str = h.get("date")

            if not date_str:
                continue

            delivery_date = datetime.strptime(
                date_str,
                "%Y-%m-%d"
            ).date()

            income = safe_float(
                h.get("total_price", 0)
            )

            fuel = safe_float(
                h.get("fuel_cost", 0)
            )

            profit = safe_float(
                h.get("profit", 0)
            )


            # ---------------------------------------------
            # MONTHLY ANALYTICS
            # ---------------------------------------------

            month_key = delivery_date.strftime(
                "%Y-%m"
            )

            monthly_analytics[
                month_key
            ] = monthly_analytics.get(
                month_key,
                0
            ) + profit


            # ---------------------------------------------
            # TOTALS
            # ---------------------------------------------

            total_income += income

            total_fuel += fuel


            # ---------------------------------------------
            # DAILY
            # ---------------------------------------------

            if delivery_date == today:

                daily += profit


            # ---------------------------------------------
            # WEEKLY
            # ---------------------------------------------

            if (
                delivery_date.isocalendar()[:2]
                == today.isocalendar()[:2]
            ):

                weekly += profit


            # ---------------------------------------------
            # MONTHLY
            # ---------------------------------------------

            if (
                delivery_date.year == today.year
                and delivery_date.month == today.month
            ):

                monthly += profit


            # ---------------------------------------------
            # CHART DATA
            # ---------------------------------------------

            chart_day = delivery_date.strftime(
                "%b %d"
            )

            analytics[
                chart_day
            ] = analytics.get(
                chart_day,
                0
            ) + profit


        except Exception as e:

            print(
                "Analytics error:",
                e
            )

            pass


    # -----------------------------------------------------
    # CHART
    # -----------------------------------------------------

    chart_labels = list(
        analytics.keys()
    )

    chart_values = list(
        analytics.values()
    )


    # -----------------------------------------------------
    # PEAK / LOWEST MONTH
    # -----------------------------------------------------

    if monthly_analytics:

        peak_month_key = max(
            monthly_analytics,
            key=monthly_analytics.get
        )

        lowest_month_key = min(
            monthly_analytics,
            key=monthly_analytics.get
        )


        peak_month = datetime.strptime(
            peak_month_key,
            "%Y-%m"
        ).strftime(
            "%B %Y"
        )


        lowest_month = datetime.strptime(
            lowest_month_key,
            "%Y-%m"
        ).strftime(
            "%B %Y"
        )

    else:

        peak_month = "No Data"

        lowest_month = "No Data"


    # -----------------------------------------------------
    # RENDER DASHBOARD
    # -----------------------------------------------------

    return render_template(

        "admin_dashboard.html",

        Input_Dashboard=data,

        drivers=drivers,

        history=history,

        deliveries=deliveries,

        daily=round(
            daily,
            2
        ),

        weekly=round(
            weekly,
            2
        ),

        monthly=round(
            monthly,
            2
        ),

        total_income=round(
            total_income,
            2
        ),

        total_fuel=round(
            total_fuel,
            2
        ),

        net_profit=round(
            total_income - total_fuel,
            2
        ),

        total_deliveries=total_deliveries,

        completed_deliveries=completed_deliveries,

        pending_deliveries=pending_deliveries,

        declined_deliveries=declined_deliveries,

        total_drivers=total_drivers,

        total_customers=total_customers,

        chart_labels=chart_labels,

        chart_values=chart_values,

        peak_month=peak_month,

        lowest_month=lowest_month

    )


# =========================================================
# ADD DRIVER
# =========================================================

@app.route(
    "/add_driver",
    methods=["POST"]
)
@login_required
def add_driver():

    driver_id = str(
        uuid.uuid4()
    )


    # -----------------------------------------------------
    # PUBLIC QR URL
    # -----------------------------------------------------

    qr_url = (
        f"{BASE_URL}/register/{driver_id}"
    )


    # -----------------------------------------------------
    # CREATE QR CODE
    # -----------------------------------------------------

    qr = qrcode.make(
        qr_url
    )

    buffer = BytesIO()

    qr.save(
        buffer,
        format="PNG"
    )

    qr_base64 = base64.b64encode(
        buffer.getvalue()
    ).decode()


    # -----------------------------------------------------
    # SAVE DRIVER
    # -----------------------------------------------------

    db.collection(
        "drivers"
    ).document(
        driver_id
    ).set({

        "name": request.form.get(
            "name"
        ),

        "address": request.form.get(
            "address"
        ),

        "contact": request.form.get(
            "contact"
        ),

        "registered": False,

        "username": "",

        "password": "",

        "qr_url": qr_url,

        "qr_image": qr_base64,

        "created_at":
            datetime.now(
                timezone.utc
            )

    })


    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Driver Created</title>
    </head>

    <body>

        <h2>Driver Created</h2>

        <img
            src="data:image/png;base64,{qr_base64}"
            width="250"
        >

        <br><br>

        <p>
            Scan this QR code using the driver's phone.
        </p>

        <p>
            <strong>Registration URL:</strong>
            <br>
            {qr_url}
        </p>

        <br>

        <a href="/admin_dashboard">
            Back to Dashboard
        </a>

    </body>
    </html>
    """
# =========================================================
# DELETE DRIVER
# =========================================================

@app.route("/delete_driver", methods=["POST"])
@login_required
def delete_driver():

    driver_id = request.form.get("driver_id")

    if not driver_id:
        return redirect(url_for("admin_dashboard"))

    try:

        # -------------------------------------------------
        # CHECK IF DRIVER EXISTS
        # -------------------------------------------------

        driver_ref = db.collection("drivers").document(driver_id)
        driver_doc = driver_ref.get()

        if not driver_doc.exists:
            return redirect(url_for("admin_dashboard"))

        # -------------------------------------------------
        # DELETE DRIVER ACCOUNT
        # -------------------------------------------------

        driver_ref.delete()

        # -------------------------------------------------
        # DELETE ONLY ACTIVE ASSIGNMENTS
        # PRESERVE COMPLETED DELIVERY HISTORY
        # -------------------------------------------------

        assignments = (
            db.collection("assignments")
            .where("driver_id", "==", driver_id)
            .stream()
        )

        for assignment in assignments:

            data = assignment.to_dict()

            status = data.get("status")

            # Delete only unfinished deliveries
            if status in ["pending", "accepted", "declined"]:
                assignment.reference.delete()

        print(f"Driver {driver_id} deleted successfully.")

        return redirect(url_for("admin_dashboard"))

    except Exception as e:

        print("DELETE DRIVER ERROR:", e)

        return redirect(url_for("admin_dashboard"))

# =========================================================
# DRIVER REGISTRATION
# =========================================================

@app.route(
    "/register/<driver_id>",
    methods=["GET", "POST"]
)
def register(driver_id):

    driver_ref = db.collection(
        "drivers"
    ).document(
        driver_id
    )

    driver = driver_ref.get()


    if not driver.exists:

        return "Invalid QR Code", 404


    data = driver.to_dict()


    # -----------------------------------------------------
    # SHOW REGISTRATION PAGE
    # -----------------------------------------------------

    if request.method == "GET":

        if data.get("registered"):

            return redirect(
                "/driver_login"
            )


        return render_template(

            "register.html",

            driver_name=data.get(
                "name"
            ),

            driver_id=driver_id

        )


    # -----------------------------------------------------
    # REGISTER DRIVER
    # -----------------------------------------------------

    username = request.form.get(
        "username",
        ""
    ).strip()

    password = request.form.get(
        "password",
        ""
    ).strip()

    confirm = request.form.get(
        "confirm_password",
        ""
    ).strip()


    if not username:

        return "Username is required."


    if not password:

        return "Password is required."


    if password != confirm:

        return "Passwords do not match."


    # -----------------------------------------------------
    # CHECK USERNAME
    # -----------------------------------------------------

    drivers = db.collection(
        "drivers"
    ).stream()


    for d in drivers:

        info = d.to_dict()

        if info.get(
            "username"
        ) == username:

            return "Username already exists."


    # -----------------------------------------------------
    # SAVE ACCOUNT
    # -----------------------------------------------------

    driver_ref.update({

        "username": username,

        "password":
            generate_password_hash(
                password
            ),

        "registered": True

    })


    return redirect(
        "/driver_login"
    )


# =========================================================
# DRIVER LOGIN
# =========================================================

@app.route(
    "/driver_login",
    methods=["GET", "POST"]
)
def driver_login():

    if request.method == "GET":

        return render_template(
            "driver_login.html"
        )


    username = request.form.get(
        "username",
        ""
    ).strip()

    password = request.form.get(
        "password",
        ""
    ).strip()


    drivers = db.collection(
        "drivers"
    ).where(
        "username",
        "==",
        username
    ).stream()


    for doc in drivers:

        data = doc.to_dict()


        if data.get(
            "username"
        ) == username:

            stored_password = data.get(
                "password"
            )


            if (
                stored_password
                and
                check_password_hash(
                    stored_password,
                    password
                )
            ):

                session.clear()

                session["driver_id"] = doc.id

                session["driver_name"] = data.get(
                    "name"
                )


                return redirect(
                    "/driver"
                )


            return "Incorrect password."


    return "Username not found."


# =========================================================
# DRIVER DASHBOARD
# =========================================================

@app.route("/driver")
@driver_login_required
def driver():

    driver_id = session[
        "driver_id"
    ]


    docs = (
        db.collection(
            "assignments"
        )
        .where(
            "driver_id",
            "==",
            driver_id
        )
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

        driver_name=session.get(
            "driver_name"
        )

    )


# =========================================================
# ACCEPT DELIVERY
# =========================================================

@app.route(
    "/accept_delivery/<id>",
    methods=["POST"]
)
@driver_login_required
def accept_delivery(id):

    # Security check:
    # Make sure this delivery belongs to
    # the currently logged-in driver.

    doc_ref = db.collection(
        "assignments"
    ).document(id)

    doc = doc_ref.get()


    if not doc.exists:

        return "Delivery not found.", 404


    data = doc.to_dict()


    if data.get(
        "driver_id"
    ) != session.get(
        "driver_id"
    ):

        return "Unauthorized.", 403


    doc_ref.update({

        "status": "accepted",

        "accepted_at":
            datetime.now(
                timezone.utc
            ).strftime(
                "%Y-%m-%d %H:%M:%S"
            )

    })


    return redirect(
        "/driver"
    )


# =========================================================
# DECLINE DELIVERY
# =========================================================

@app.route(
    "/decline_delivery/<id>",
    methods=["POST"]
)
@driver_login_required
def decline_delivery(id):

    # Security check

    doc_ref = db.collection(
        "assignments"
    ).document(id)

    doc = doc_ref.get()


    if not doc.exists:

        return "Delivery not found.", 404


    data = doc.to_dict()


    if data.get(
        "driver_id"
    ) != session.get(
        "driver_id"
    ):

        return "Unauthorized.", 403


    doc_ref.update({

        "status": "declined",

        "declined_at":
            datetime.now(
                timezone.utc
            ).strftime(
                "%Y-%m-%d %H:%M:%S"
            )

    })


    return redirect(
        "/driver"
    )


# =========================================================
# DRIVER DELIVERY UPDATE
# =========================================================

@app.route(
    "/driver_update/<id>",
    methods=["POST"]
)
@driver_login_required
def driver_update(id):

    start = safe_float(
        request.form.get(
            "startOdo"
        )
    )

    end = safe_float(
        request.form.get(
            "endOdo"
        )
    )

    fuel = safe_float(
        request.form.get(
            "FuelUsed"
        )
    )

    price = safe_float(
        request.form.get(
            "FuelPrice"
        )
    )


    fuel_cost = fuel * price


    # -----------------------------------------------------
    # GET DELIVERY
    # -----------------------------------------------------

    doc_ref = db.collection(
        "assignments"
    ).document(id)

    doc = doc_ref.get()


    if not doc.exists:

        return "Delivery not found.", 404


    data = doc.to_dict()


    # -----------------------------------------------------
    # SECURITY CHECK
    # -----------------------------------------------------

    if data.get(
        "driver_id"
    ) != session.get(
        "driver_id"
    ):

        return "Unauthorized.", 403


    # -----------------------------------------------------
    # CALCULATE PROFIT
    # -----------------------------------------------------

    income = safe_float(
        data.get(
            "total_price",
            0
        )
    )

    profit = income - fuel_cost


    # -----------------------------------------------------
    # UPDATE DELIVERY
    # -----------------------------------------------------

    doc_ref.update({

        "distance": max(
            0,
            end - start
        ),

        "fuel_cost": fuel_cost,

        "profit": profit,

        "status": "done",

        "date":
            datetime.now(
                timezone.utc
            ).strftime(
                "%Y-%m-%d"
            )

    })


    return redirect(
        "/driver"
    )


# =========================================================
# ADMIN LOGOUT
# =========================================================

@app.route("/logout")
def logout():

    session.clear()

    return redirect("/")


# =========================================================
# DRIVER LOGOUT
# =========================================================

@app.route("/driver_logout")
def driver_logout():

    session.clear()

    return redirect(
        "/driver_login"
    )


# =========================================================
# HEALTH CHECK
# =========================================================

@app.route("/health")
def health():

    return {
        "status": "ok"
    }, 200


# =========================================================
# LOCAL DEVELOPMENT
# =========================================================
#
# IMPORTANT:
# Render will normally use:
#
# gunicorn main:app
#
# This block is only for running locally.
# =========================================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=int(
            os.environ.get(
                "PORT",
                5000
            )
        ),
        debug=False
    )