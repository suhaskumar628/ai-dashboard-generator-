import os
from typing import List
from flask import Flask, render_template, request, redirect, url_for, abort, jsonify
import pandas as pd
import google.generativeai as genai
import stripe

# ===================== CONFIG =====================

# Gemini (reads from environment/Fly secrets)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
else:
    print("WARNING: GEMINI_API_KEY not set. AI features will not work.")

# Stripe (use TEST keys until you go live)
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_PUBLISHABLE_KEY = os.getenv("STRIPE_PUBLISHABLE_KEY", "")
if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY
else:
    print("WARNING: STRIPE_SECRET_KEY not set. Checkout is disabled.")

# Flask app
app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10 MB upload cap


# ===================== AI HELPER =====================
def run_gemini(csv_preview: str, columns: List[str]) -> str:
    """
    Sends a compact CSV preview + columns to Gemini and returns markdown text
    with Cleaning steps, SQL queries, and Dashboard recommendations.
    NOTE: The prompt avoids markdown code fences to prevent rendering confusion.
    """
    if not GEMINI_API_KEY:
        return "Gemini API key not configured. Set GEMINI_API_KEY as an environment variable."

    prompt = (
        "You are a senior data analyst. The user uploaded a CSV.\n\n"
        "=== CSV PREVIEW (first rows) ===\n"
        f"{csv_preview}\n\n"
        "=== COLUMNS ===\n"
        f"{columns}\n\n"
        "=== TASKS ===\n"
        "1) Cleaning/Prep: list concrete steps (dedupe, type fixes, missing values, outliers).\n"
        "2) SQL: produce 4–8 helpful ANSI SQL queries for table 'uploaded_table'.\n"
        "   - Include a one-line title as a SQL comment above each query.\n"
        "   - Prefer GROUP BY, WHERE, and window functions when useful.\n"
        "3) Dashboard (Power BI / Tableau): recommend 4–8 visuals with:\n"
        "   - Visual type, fields (dimension × measure), and one-line rationale.\n\n"
        "=== OUTPUT FORMAT (markdown) ===\n"
        "## Cleaning\n"
        "- step...\n\n"
        "## SQL\n"
        "-- title\n"
        "SELECT ...\n\n"
        "## Dashboard\n"
        "- Visual: <type> | Fields: <dimension> × <measure> | Why: <1-liner>\n"
    )

    try:
        model = genai.GenerativeModel("gemini-1.5-flash")
        resp = model.generate_content(prompt)
        return getattr(resp, "text", None) or "No response from Gemini."
    except Exception as e:
        return f"Gemini error: {e}"


# ===================== ROUTES =====================
@app.route("/health", methods=["GET"])
def health():
    return {"ok": True}


@app.route("/", methods=["GET"])
def home():
    # index.html may optionally show a Buy button; publishable key is passed if you need it there.
    return render_template("index.html", stripe_key=STRIPE_PUBLISHABLE_KEY)


@app.route("/upload", methods=["POST"])
def upload():
    f = request.files.get("csv_file")
    if not f:
        return redirect(url_for("home"))

    # Try default UTF-8 first, then fallback to latin-1
    try:
        df = pd.read_csv(f)
    except UnicodeDecodeError:
        f.stream.seek(0)
        df = pd.read_csv(f, encoding="latin-1")
    except Exception as e:
        abort(400, f"Unable to read CSV: {e}")

    if df.empty:
        abort(400, "Uploaded CSV is empty.")

    head_df = df.head(8)
    csv_preview = head_df.to_csv(index=False)
    columns = list(df.columns)

    ai_output = run_gemini(csv_preview, columns)
    return render_template("result.html", output=ai_output)


# ---------- STRIPE CHECKOUT ----------
@app.route("/create-checkout-session", methods=["POST"])
def create_checkout_session():
    if not STRIPE_SECRET_KEY:
        return jsonify({"error": "Stripe is not configured"}), 400

    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "product_data": {"name": "AI Data Dashboard Generator"},
                    "unit_amount": 2900,  # $29.00 in cents
                },
                "quantity": 1,
            }],
            success_url=url_for("home", _external=True) + "?success=true",
            cancel_url=url_for("home", _external=True) + "?canceled=true",
        )
        return redirect(session.url, code=303)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ===================== MAIN (LOCAL DEV) =====================
if __name__ == "__main__":
    # On Fly.io, gunicorn (via Procfile) will run this app.
    # This is only for local development:
    app.run(host="0.0.0.0", port=8080, debug=True)
