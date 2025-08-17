import os, time
from typing import List
from flask import Flask, render_template, request, redirect, url_for, abort, jsonify, session
import pandas as pd
import google.generativeai as genai
import stripe

# ===================== APP & SECRETS =====================
SECRET_KEY = os.getenv("SECRET_KEY") or os.getenv("FLASK_SECRET_KEY") or ""
app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10 MB upload cap
app.secret_key = SECRET_KEY or os.urandom(32)
if not SECRET_KEY:
    print("WARNING: SECRET_KEY not set. Sessions may not persist across restarts.")

# ===================== HYBRID PRICING CONFIG =====================
# Free tier (per browser session)
FREE_RUNS_PER_WINDOW = int(os.getenv("FREE_RUNS_PER_WINDOW", "1"))
FREE_WINDOW_SECONDS = int(os.getenv("FREE_WINDOW_SECONDS", "3600"))  # default: 1 hour
# Credits
CREDITS_PER_PACK = int(os.getenv("CREDITS_PER_PACK", "10"))
CREDITS_PACK_PRICE_USD = int(os.getenv("CREDITS_PACK_PRICE_USD", "9"))  # used only if no Stripe Price ID provided

# ===================== GEMINI =====================
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
else:
    print("WARNING: GEMINI_API_KEY not set. AI features will not work.")

# ===================== STRIPE =====================
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_PUBLISHABLE_KEY = os.getenv("STRIPE_PUBLISHABLE_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")  # optional

# Optional: use Dashboard Price IDs (recommended). If blank, we'll fall back to ad-hoc prices.
STRIPE_PRICE_ID_SUBSCRIPTION = os.getenv("STRIPE_PRICE_ID_SUBSCRIPTION", "")  # e.g. monthly unlimited
STRIPE_PRICE_ID_CREDITS      = os.getenv("STRIPE_PRICE_ID_CREDITS", "")       # e.g. 10-run pack
STRIPE_PRICE_ID_ONE_TIME     = os.getenv("STRIPE_PRICE_ID_ONE_TIME", "")      # optional lifetime/unlimited

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY
else:
    print("WARNING: STRIPE_SECRET_KEY not set. Checkout is disabled.")

# ===================== SESSION HELPERS =====================
def _now() -> int:
    return int(time.time())

def _is_pro() -> bool:
    return bool(session.get("pro"))

def _get_credits() -> int:
    return int(session.get("credits", 0))

def _add_credits(n: int) -> None:
    session["credits"] = _get_credits() + max(0, int(n))

def _consume_credit() -> bool:
    cur = _get_credits()
    if cur > 0:
        session["credits"] = cur - 1
        return True
    return False

def _runs_within_window() -> int:
    ts = session.get("runs", [])
    cutoff = _now() - FREE_WINDOW_SECONDS
    ts = [t for t in ts if t >= cutoff]
    session["runs"] = ts
    return len(ts)

def _remaining_free_runs() -> int:
    return max(0, FREE_RUNS_PER_WINDOW - _runs_within_window())

def _record_free_run() -> None:
    ts = session.get("runs", [])
    ts.append(_now())
    session["runs"] = ts[-10:]  # keep the last few to avoid cookie bloat

# ===================== AI CORE =====================
def run_gemini(csv_preview: str, columns: List[str]) -> str:
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
        "## Cleaning\n- step...\n\n## SQL\n-- title\nSELECT ...\n\n## Dashboard\n- Visual: <type> | Fields: <dimension> × <measure> | Why: <1-liner>\n"
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
    return {
        "ok": True,
        "pro": _is_pro(),
        "credits": _get_credits(),
        "remaining_free_runs": _remaining_free_runs(),
        "free_window_seconds": FREE_WINDOW_SECONDS
    }

@app.route("/", methods=["GET"])
def home():
    # Apply entitlement on redirect success (session-based MVP)
    if request.args.get("success"):
        plan = request.args.get("plan", "")
        if plan in ("subscription", "one_time"):
            session["pro"] = True
        elif plan.startswith("credits"):
            _add_credits(CREDITS_PER_PACK)

    return render_template(
        "index.html",
        stripe_key=STRIPE_PUBLISHABLE_KEY,
        pro=_is_pro(),
        credits=_get_credits(),
        remaining_free=_remaining_free_runs(),
        window_minutes=FREE_WINDOW_SECONDS // 60
    )

@app.route("/upload", methods=["POST"])
def upload():
    # Gating logic: pro → allowed; else credits → consume; else free → check window
    if not _is_pro():
        if _get_credits() > 0:
            _consume_credit()
        else:
            if _remaining_free_runs() <= 0:
                return redirect(url_for("home", limit="true"))
            _record_free_run()

    f = request.files.get("csv_file")
    if not f:
        return redirect(url_for("home"))

    # Try default UTF-8, then latin-1
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
    return render_template(
        "result.html",
        output=ai_output,
        stripe_key=STRIPE_PUBLISHABLE_KEY,
        pro=_is_pro(),
        credits=_get_credits()
    )

# ---------- STRIPE CHECKOUT ----------
@app.route("/create-checkout-session", methods=["POST"])
def create_checkout_session():
    if not STRIPE_SECRET_KEY:
        return jsonify({"error": "Stripe is not configured"}), 400

    # plan comes from hidden input: subscription | credits10 | one_time
    plan = request.form.get("plan", "").strip() or "subscription"

    try:
        # Decide mode + line_items based on plan
        if plan == "subscription":
            if STRIPE_PRICE_ID_SUBSCRIPTION:
                session_args = dict(
                    mode="subscription",
                    line_items=[{"price": STRIPE_PRICE_ID_SUBSCRIPTION, "quantity": 1}],
                )
            else:
                return jsonify({"error": "STRIPE_PRICE_ID_SUBSCRIPTION not set"}), 400

        elif plan.startswith("credits"):
            if STRIPE_PRICE_ID_CREDITS:
                session_args = dict(
                    mode="payment",
                    line_items=[{"price": STRIPE_PRICE_ID_CREDITS, "quantity": 1}],
                )
            else:
                # Ad-hoc price fallback for credits pack
                session_args = dict(
                    mode="payment",
                    line_items=[{
                        "price_data": {
                            "currency": "usd",
                            "product_data": {"name": f"{CREDITS_PER_PACK} Runs Credit Pack"},
                            "unit_amount": CREDITS_PACK_PRICE_USD * 100,
                        },
                        "quantity": 1,
                    }],
                )

        elif plan == "one_time":
            if STRIPE_PRICE_ID_ONE_TIME:
                session_args = dict(
                    mode="payment",
                    line_items=[{"price": STRIPE_PRICE_ID_ONE_TIME, "quantity": 1}],
                )
            else:
                # Ad-hoc price fallback
                session_args = dict(
                    mode="payment",
                    line_items=[{
                        "price_data": {
                            "currency": "usd",
                            "product_data": {"name": "AI Data Dashboard Generator — Lifetime"},
                            "unit_amount": 2900,  # $29
                        },
                        "quantity": 1,
                    }],
                )
        else:
            return jsonify({"error": f"Unknown plan: {plan}"}), 400

        checkout_session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            allow_promotion_codes=True,
            automatic_tax={"enabled": False},
            success_url=url_for("home", _external=True) + f"?success=true&plan={plan}",
            cancel_url=url_for("home", _external=True) + "?canceled=true",
            **session_args,
        )
        return redirect(checkout_session.url, code=303)

    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ---------- OPTIONAL: STRIPE WEBHOOK (later, for real entitlements) ----------
@app.route("/webhook", methods=["POST"])
def stripe_webhook():
    if not STRIPE_WEBHOOK_SECRET:
        return "", 200
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature", "")
    try:
        event = stripe.Webhook.construct_event(payload=payload, sig_header=sig_header, secret=STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        return jsonify({"error": f"Webhook error: {e}"}), 400

    # Example: when a checkout is completed, you could set a persistent entitlement in your DB
    if event["type"] == "checkout.session.completed":
        pass
    return "", 200

# ===================== MAIN =====================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")), debug=True)
