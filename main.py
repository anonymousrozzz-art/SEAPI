import os
import re
import logging
import requests
import urllib.parse
import random
from flask import Flask, request, jsonify, send_from_directory
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor

# --- MANUAL .ENV LOADER ---
def force_load_env():
    try:
        if os.path.exists('.env'):
            print(" [SYSTEM] Found .env file. Loading variables...")
            with open('.env', 'r') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#') and '=' in line:
                        key, value = line.split('=', 1)
                        key = key.strip()
                        value = value.strip().strip('"').strip("'")
                        os.environ[key] = value
    except Exception as e:
        print(f" [SYSTEM] Error reading .env: {e}")

force_load_env()

# --- CONFIGURATION ---
GROQ_API_KEY = os.environ.get("GROQ_API_KEY") 
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY") 
GOOGLE_CX_ID = os.environ.get("GOOGLE_CX_ID")     

print("\n" + "="*40)
print(" STARTUP DIAGNOSTICS")
print("="*40)
print(f" GROQ KEY   : {'[OK]' if GROQ_API_KEY else '[MISSING]'}")
print(f" GOOGLE KEY : {'[OK]' if GOOGLE_API_KEY else '[MISSING]'}")
print(f" GOOGLE CX  : {'[OK]' if GOOGLE_CX_ID else '[MISSING]'}")
print("="*40 + "\n")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder='.')

# --- SEARCH ENGINES ---

def google_search_api(query, page=1):
    """Google Custom Search API with Pagination"""
    if not GOOGLE_API_KEY or not GOOGLE_CX_ID:
        return None 

    # Google CS API: 'start' parameter is 1-based index (Page 1 = 1, Page 2 = 11, etc.)
    start_index = ((page - 1) * 10) + 1
    
    url = "https://www.googleapis.com/customsearch/v1"
    params = {
        "key": GOOGLE_API_KEY,
        "cx": GOOGLE_CX_ID,
        "q": query,
        "start": start_index,
        "num": 10
    }

    try:
        logger.info(f"Fetching Google: {query} (Start Index: {start_index})")
        resp = requests.get(url, params=params, timeout=5)
        
        if resp.status_code == 200:
            data = resp.json()
            results = []
            for item in data.get("items", []):
                results.append({
                    "title": item.get("title"),
                    "url": item.get("link"),
                    "snippet": item.get("snippet"),
                    "source": "Google"
                })
            return results
        elif resp.status_code == 429:
            logger.warning("Google API Quota Exceeded.")
            return None
        else:
            logger.error(f"Google API Error: {resp.status_code}")
            return None
            
    except Exception as e:
        logger.error(f"Google Connection Failed: {e}")
        return None

def ddg_lite_search(query, page=1):
    """DuckDuckGo Lite with simulated Pagination"""
    logger.info(f"Fetching DDG Lite (Background): {query} (Page {page})")
    url = "https://lite.duckduckgo.com/lite/"
    
    # DDG Lite uses 's' parameter for skip/offset. 
    skip = (page - 1) * 20
    
    payload = {
        'q': query,
        's': skip
    }
    
    try:
        resp = requests.post(url, data=payload, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
            "Content-Type": "application/x-www-form-urlencoded"
        }, timeout=8)
        
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            results = []
            rows = soup.find_all('tr')
            current_result = {}
            
            for row in rows:
                link_tag = row.find('a', class_='result-link')
                snippet_tag = row.find('td', class_='result-snippet')
                
                if link_tag:
                    current_result = {
                        "title": link_tag.get_text(strip=True),
                        "url": link_tag['href'],
                        "source": "DuckDuckGo"
                    }
                elif snippet_tag and current_result:
                    current_result['snippet'] = snippet_tag.get_text(strip=True)
                    results.append(current_result)
                    current_result = {} 
            
            # Filter out internal DDG links if any
            clean_results = [r for r in results if not "duckduckgo.com" in r['url']]
            return clean_results[:15]
            
    except Exception as e:
        logger.error(f"DDG Lite Failed: {e}")
        
    return []

# --- ROUTES ---

@app.route("/")
def serve_index():
    return send_from_directory('.', 'index.html')

@app.route("/chat", methods=["POST"])
def chat_proxy():
    if not GROQ_API_KEY:
        return jsonify({"choices": [{"message": {"content": "**Error:** GROQ_API_KEY not configured."}}]})
    
    try:
        data = request.json
        # Only sending the first user message + system prompt to keep it single-turn
        messages = data.get("messages", [])
        
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            json={
                "model": "llama-3.3-70b-versatile",
                "messages": messages,
                "temperature": 0.7
            },
            timeout=30
        )
        return jsonify(resp.json())
    except Exception as e:
        return jsonify({"choices": [{"message": {"content": f"**System Error:** {str(e)}"}} ]})

@app.route("/search")
def search():
    q = request.args.get("q", "")
    page = int(request.args.get("page", 1))
    
    if not q: return jsonify({"results": []})
    
    final_results = []
    
    # Execute both searches in parallel threads
    with ThreadPoolExecutor(max_workers=2) as executor:
        future_google = executor.submit(google_search_api, q, page)
        future_ddg = executor.submit(ddg_lite_search, q, page)
        
        # Wait for Google first
        google_results = future_google.result()
        
        if google_results and len(google_results) > 0:
            final_results = google_results
        else:
            # If Google Failed, take DDG result
            logger.info("Google failed or empty, using DDG result.")
            final_results = future_ddg.result()

    # Fallback if both failed
    if not final_results and page == 1:
         final_results = [{
            "title": "No Results Found",
            "url": "#",
            "snippet": "Both Google and DuckDuckGo failed to return results.",
            "source": "System"
        }]

    return jsonify({"results": final_results or []})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
