import os
from flask import Flask, render_template, request, redirect, url_for, abort
import pandas as pd
import google.generativeai as genai

# ===================== CONFIG =====================
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
if not GEMINI_API_KEY:
    print("WARNING: GEMINI_API_KEY not set. The app will start, but AI calls will fail.")
else:
    genai.configure(api_key=GEMINI_API_KEY)

app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10 MB upload cap


# ===================== AI HELPER =====================
def run_gemini(csv_preview: str, columns: list[str]) -> str:
    """
    Sends a compact CSV preview + columns to Gemini and returns markdown text
    with Cleaning steps, SQL queries, and Dashboard recommendations.
    """
    if not GEMINI_API_KEY:
        return "Gemini API key not configured. Set GEMINI_API_KEY as an environment variable."

    model = genai.GenerativeModel("gemini-1.5-flash")
    prompt = f"""
You are a senior data analyst. The user uploaded a CSV.
Analyze only the preview shown and propose immediate next steps.

# CSV PREVIEW (first rows)

