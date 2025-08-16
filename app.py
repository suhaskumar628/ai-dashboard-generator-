import os
from flask import Flask, render_template, request, redirect, url_for, abort, jsonify
import pandas as pd
import google.generativeai as genai
import stripe

# ===================== CONFIG =====================

# --- Gemini ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
else:
    print("WARNING: GEMINI_API_KEY not set. AI features will not work.")

# --- Stripe ---
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_PUBLISHABLE_KEY = os.getenv("STRIPE_PUBLISHABLE_KEY", "")
if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY
else:
    print("WARNING: STRIPE_SECRET_KEY not set. Checkout disabled.")

# --- Flask App ---
app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10 MB upload cap


# ===================== AI HELPER =====================
def run_gemini(csv_preview: str, columns: list[str]) -> str:
    """Send a compact preview to Gemini and get analysis."""
    if not GEMINI_API_KEY:
        return "Gemini API key not configured."

    try:
        model = genai.GenerativeModel("gemini-1.5-flash")
        prompt = f"""
        You are a senior data analyst. The user uploaded a CSV.

        # CSV PREVIEW
        ```
        {csv_preview}
        ```

        # COLUMNS
        {columns}

        # TASKS
        1. Suggest cleaning steps
        2. Write 2–3 SQL queries with comments
        3. Recommend dashboard visuals
        """

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
    return render_template("index.html", stripe_key=STRIPE_PUBLISHABLE_KEY)


@app.route("/upload", methods=["POST"])
def upload():
    f = request.files.get("csv_file")
    if not f:
        return redirect(url_for("home"))

    try:
        df = pd.read_csv(f)
    except UnicodeDecodeError:
        f.stream.seek(0)
        df = pd.read_csv(f, encoding="latin-1")
    except Exception as e:
        abort(400, f"Unable to read CSV: {e}")

    if df.empty:
        abort(400, "Uploaded CSV is empty.")

    csv_preview = df.head(8).to_csv(index=False)
    columns = list(df.columns)
    ai_output = run_gemini(csv_preview, columns)

    return render_template("result.html", output=ai_output)


# ---------- STRIPE CHECKOUT ----------
@app.route("/create-checkout-session", methods=["POST"])
def create_checkout_session():
    if not STRIPE_SECRET_KEY:
        return jsonify({"error": "Stripe not configured"}), 400

    try:
        checkout_session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "product_data": {
                        "name": "AI Dashboard Generator",
                    },
                    "unit_amount": 2900,  # $29.00
                },
                "quantity": 1,
            }],
            mode="payment",
            success_url=url_for("home", _external=True) + "?success=true",
            cancel_url=url_for("home", _external=True) + "?canceled=true",
        )
        return redirect(checkout_session.url, code=303)
    except Exception as e:
        return jsonify(error=str(e)), 500


# ===================== MAIN =====================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=True)
