import os
import sys
import re
import json
import time
import random
import csv
import sqlite3
import datetime
from urllib.parse import urlparse, parse_qs
import requests

# Windowsコマンドプロンプト等の標準出力をUTF-8に変更（文字化け・エンコード落ち対策）
if sys.platform == "win32":
    import sys
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

# =====================================================================
# 定数・設定定義
# =====================================================================
USER_AGENT = "PersonalTweetArchiver/1.0 (+https://github.com/<your-account>/<your-repo>; personal archival use)"
HEADERS = {"User-Agent": USER_AGENT}

CSV_PATH = "extracted_tweets.csv"
TXT_PATH = "extracted_tweets_readable.txt"
LIST_PATH = "list.txt"
SKIPPED_URLS_PATH = "skipped_other_urls.txt"
FAILED_TWEETS_PATH = "failed_tweets.txt"
DB_PATH = "data/tweets.db"

CSV_HEADERS = [
    "id", "url", "fetched_at", "created_at", "author_name", "author_screen_name",
    "author_avatar_local", "text", "likes", "retweets", "replies", "views",
    "photos", "videos", "links", "replying_to", "replying_to_status",
    "quote_id", "quote_text", "quote_author", "quote_author_handle", "quote_url",
    "poll", "memo", "tags"
]

# =====================================================================
# データベース初期化 ＆ スキーマ自動移行 (WALモード設定)
# =====================================================================
def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("PRAGMA busy_timeout=5000;")
    
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS tweets (
        id TEXT PRIMARY KEY,
        url TEXT,
        fetched_at TEXT,
        created_at INTEGER,
        author_name TEXT,
        author_screen_name TEXT,
        author_avatar_local TEXT,
        text TEXT,
        likes INTEGER DEFAULT 0,
        retweets INTEGER DEFAULT 0,
        replies INTEGER DEFAULT 0,
        views INTEGER DEFAULT 0,
        photos TEXT DEFAULT '[]',
        videos TEXT DEFAULT '[]',
        links TEXT DEFAULT '[]',
        replying_to TEXT DEFAULT '',
        replying_to_status TEXT DEFAULT '',
        quote_id TEXT DEFAULT '',
        quote_text TEXT DEFAULT '',
        quote_author TEXT DEFAULT '',
        quote_author_handle TEXT DEFAULT '',
        quote_url TEXT DEFAULT '',
        poll TEXT DEFAULT '',
        memo TEXT DEFAULT '',
        tags TEXT DEFAULT '[]'
    );
    """)
    
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS all_tags (
        tag TEXT PRIMARY KEY,
        color TEXT DEFAULT '#1D9BF0'
    );
    """)
    conn.commit()
    
    cursor.execute("PRAGMA table_info(tweets);")
    existing_columns = [row[1] for row in cursor.fetchall()]
    
    required_columns = [
        ("id", "TEXT"), ("url", "TEXT"), ("fetched_at", "TEXT"), ("created_at", "INTEGER"),
        ("author_name", "TEXT"), ("author_screen_name", "TEXT"), ("author_avatar_local", "TEXT"),
        ("text", "TEXT"), ("likes", "INTEGER"), ("retweets", "INTEGER"), ("replies", "INTEGER"),
        ("views", "INTEGER"), ("photos", "TEXT"), ("videos", "TEXT"), ("links", "TEXT"),
        ("replying_to", "TEXT"), ("replying_to_status", "TEXT"), ("quote_id", "TEXT"),
        ("quote_text", "TEXT"), ("quote_author", "TEXT"), ("quote_author_handle", "TEXT"),
        ("quote_url", "TEXT"), ("poll", "TEXT"), ("memo", "TEXT"), ("tags", "TEXT")
    ]
    
    for col_name, col_type in required_columns:
        if col_name not in existing_columns:
            default_val = " DEFAULT 0" if col_type == "INTEGER" else " DEFAULT ''"
            if col_name in ["photos", "videos", "links", "tags"]:
                default_val = " DEFAULT '[]'"
            cursor.execute(f"ALTER TABLE tweets ADD COLUMN {col_name} {col_type}{default_val};")
            print(f"[SCHEMA] Added missing column: {col_name} ({col_type})")
            
    conn.commit()
    conn.close()

def load_existing_ids() -> set:
    existing_ids = set()
    if os.path.exists(DB_PATH):
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute("SELECT id FROM tweets")
            existing_ids = {row[0] for row in cursor.fetchall()}
            conn.close()
        except sqlite3.Error as e:
            print(f"[WARN] Error loading existing IDs: {e}")
    return existing_ids

# =====================================================================
# ファイルI/O ＆ アトミック書き込み
# =====================================================================
def init_csv():
    if not os.path.exists(CSV_PATH):
        with open(CSV_PATH, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(CSV_HEADERS)

def save_list_atomic(lines: list[str], path: str = LIST_PATH):
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.writelines(lines)
    os.replace(tmp_path, path)

def append_to_file(path: str, content: str):
    with open(path, "a", encoding="utf-8") as f:
        f.write(content + "\n")

# =====================================================================
# ネットワーク・URL追跡・ダウンロード
# =====================================================================
def resolve_final_url(url: str, max_redirects: int = 10) -> str:
    current_url = url
    for _ in range(max_redirects):
        try:
            # すでにAPI URL化している場合は追跡をスキップ
            if "api.fxtwitter.com" in current_url:
                break
            response = requests.head(current_url, headers=HEADERS, allow_redirects=False, timeout=5)
            if 300 <= response.status_code < 400 and 'Location' in response.headers:
                current_url = response.headers['Location']
            else:
                break
        except requests.RequestException:
            try:
                if "api.fxtwitter.com" in current_url:
                    break
                response = requests.get(current_url, headers=HEADERS, allow_redirects=False, stream=True, timeout=5)
                response.close()
                if 300 <= response.status_code < 400 and 'Location' in response.headers:
                    current_url = response.headers['Location']
                else:
                    break
            except requests.RequestException:
                break
    return current_url

def download_file(url: str, save_path: str) -> bool:
    try:
        res = requests.get(url, headers=HEADERS, timeout=15, stream=True)
        if res.status_code == 200:
            os.makedirs(os.path.dirname(save_path), exist_ok=True)
            with open(save_path, "wb") as f:
                for chunk in res.iter_content(chunk_size=8192):
                    f.write(chunk)
            return True
    except Exception as e:
        print(f"   [Download Failed] {url} : {e}")
    return False

def get_file_extension(url: str, default: str = ".jpg") -> str:
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    if 'format' in qs:
        return f".{qs['format'][0]}"
    ext = os.path.splitext(parsed.path)[1]
    if ext:
        if '?' in ext:
            ext = ext.split('?')[0]
        return ext
    return default

def extract_tweet_id(url: str) -> str:
    match = re.search(r'status/(\d+)', url)
    return match.group(1) if match else None

def make_api_url(url: str) -> str:
    """XのURLを重複置換なしで正確に FxTwitter APIエンドポイントへ変換"""
    if "api.fxtwitter.com" in url:
        return url
    
    # 完全にホスト部分のみを置換対象にする
    parsed = urlparse(url)
    netloc = parsed.netloc
    if netloc in ["x.com", "twitter.com", "www.x.com", "www.twitter.com"]:
        new_netloc = "api.fxtwitter.com"
    else:
        new_netloc = netloc
        
    new_url = parsed._replace(netloc=new_netloc).geturl()
    if "?" in new_url:
        new_url = new_url.split("?")[0]
    return new_url

# =====================================================================
# 堅牢なエラーハンドリング付きリクエスト処理
# =====================================================================
def fetch_tweet_with_retry(api_url: str) -> dict:
    retry_count = 0
    max_retries = 3
    while True:
        try:
            res = requests.get(api_url, headers=HEADERS, timeout=10)
            
            if res.status_code == 200:
                return {"success": True, "data": res.json()}
                
            elif res.status_code == 429:
                print("[RATE LIMIT] Rate limit detected. Sleeping for 5 minutes...")
                time.sleep(300)
                continue
                
            elif res.status_code in (401, 404):
                reason = "PRIVATE_TWEET" if res.status_code == 401 else "NOT_FOUND"
                return {"success": False, "type": "permanent", "code": res.status_code, "reason": reason}
                
            elif res.status_code == 500:
                retry_count += 1
                if retry_count <= max_retries:
                    wait_time = 2 ** retry_count
                    print(f"[API ERROR] Server Error (500). Retrying in {wait_time}s... ({retry_count}/{max_retries})")
                    time.sleep(wait_time)
                    continue
                else:
                    return {"success": False, "type": "temporary", "code": 500, "reason": "API_FAIL"}
            else:
                retry_count += 1
                if retry_count <= max_retries:
                    time.sleep(2)
                    continue
                return {"success": False, "type": "temporary", "code": res.status_code, "reason": f"HTTP_{res.status_code}"}
                
        except (requests.Timeout, requests.ConnectionError) as e:
            retry_count += 1
            if retry_count <= max_retries:
                wait_time = 2 ** retry_count
                print(f"[NETWORK ERROR] Connection intermittent. Retrying in {wait_time}s... ({retry_count}/{max_retries}): {e}")
                time.sleep(wait_time)
                continue
            else:
                return {"success": False, "type": "temporary", "code": None, "reason": "CONNECTION_ERROR"}

# =====================================================================
# データパース・保存ロジック
# =====================================================================
def parse_and_download_media(api_data: dict, original_url: str) -> dict:
    tweet_data = api_data.get("tweet", {})
    id_str = tweet_data.get("id", "") or extract_tweet_id(original_url) or ""
    
    url_str = tweet_data.get("url", original_url)
    if "?" in url_str:
        url_str = url_str.split("?")[0]
        
    raw_text = tweet_data.get("text", "")
    
    author = tweet_data.get("author", {})
    screen_name = author.get("screen_name", "")
    avatar_url = author.get("avatar_url", "")
    avatar_local_path = ""
    if screen_name and avatar_url:
        avatar_local_path = f"media/avatars/{screen_name}.jpg"
        download_file(avatar_url, avatar_local_path)
        
    media = tweet_data.get("media", {})
    photos_list = media.get("photos", [])
    videos_list = media.get("videos", [])
    
    local_photos = []
    for i, p in enumerate(photos_list):
        p_url = p.get("url", "")
        if p_url:
            ext = get_file_extension(p_url, ".jpg")
            p_path = f"media/content/{id_str}/photo_{i}{ext}"
            if download_file(p_url, p_path):
                local_photos.append(p_path)
                
    local_videos = []
    for i, v in enumerate(videos_list):
        v_url = v.get("url", "")
        if v_url:
            ext = get_file_extension(v_url, ".mp4")
            v_path = f"media/content/{id_str}/video_{i}{ext}"
            if download_file(v_url, v_path):
                local_videos.append(v_path)
                
    tco_urls = re.findall(r'https?://t\.co/\w+', raw_text)
    expanded_links = []
    for tco in tco_urls:
        f_url = resolve_final_url(tco)
        if f_url and f_url != tco:
            expanded_links.append(f_url)
    expanded_links = list(set(expanded_links))
    
    quote = tweet_data.get("quote", {})
    quote_id = quote.get("id", "")
    quote_text = quote.get("text", "")
    quote_author_obj = quote.get("author", {})
    quote_author = quote_author_obj.get("name", "")
    quote_author_handle = quote_author_obj.get("screen_name", "")
    quote_url = quote.get("url", "")
    if quote_url and "?" in quote_url:
        quote_url = quote_url.split("?")[0]
        
    poll_data = tweet_data.get("poll", "")
    poll_json = json.dumps(poll_data, ensure_ascii=False) if poll_data else ""
    
    return {
        "id": id_str,
        "url": url_str,
        "fetched_at": datetime.datetime.now().isoformat(),
        "created_at": tweet_data.get("created_timestamp", 0),
        "author_name": author.get("name", ""),
        "author_screen_name": screen_name,
        "author_avatar_local": avatar_local_path,
        "text": raw_text,
        "likes": tweet_data.get("likes", 0),
        "retweets": tweet_data.get("retweets", 0),
        "replies": tweet_data.get("replies", 0),
        "views": tweet_data.get("views", 0),
        "photos": json.dumps(local_photos, ensure_ascii=False),
        "videos": json.dumps(local_videos, ensure_ascii=False),
        "links": json.dumps(expanded_links, ensure_ascii=False),
        "replying_to": tweet_data.get("replying_to", ""),
        "replying_to_status": tweet_data.get("replying_to_status", ""),
        "quote_id": quote_id,
        "quote_text": quote_text,
        "quote_author": quote_author,
        "quote_author_handle": quote_author_handle,
        "quote_url": quote_url,
        "poll": poll_json,
        "memo": "",
        "tags": "[]"
    }

def write_to_storages(data_dict: dict):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("PRAGMA busy_timeout=5000;")
    
    query = f"""
    INSERT OR REPLACE INTO tweets ({', '.join(CSV_HEADERS)})
    VALUES ({', '.join(['?'] * len(CSV_HEADERS))});
    """
    params = [data_dict.get(h, "") for h in CSV_HEADERS]
    cursor.execute(query, params)
    conn.commit()
    conn.close()
    
    with open(CSV_PATH, "a", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([data_dict.get(h, "") for h in CSV_HEADERS])
        
    with open(TXT_PATH, "a", encoding="utf-8") as f:
        f.write(f"[Tweet ID]: {data_dict['id']}\n")
        f.write(f"[URL]: {data_dict['url']}\n")
        f.write(f"[Author]: {data_dict['author_name']} (@{data_dict['author_screen_name']})\n")
        f.write(f"[Text]:\n{data_dict['text']}\n")
        f.write(f"[Stats]: Likes:{data_dict['likes']} | RT:{data_dict['retweets']} | Replies:{data_dict['replies']} | Views:{data_dict['views']}\n")
        if data_dict['quote_id']:
            f.write(f"[Quote]: @{data_dict['quote_author_handle']} - {data_dict['quote_text']} (URL: {data_dict['quote_url']})\n")
        if data_dict['poll']:
            f.write(f"[Poll]: {data_dict['poll']}\n")
        f.write("-" * 60 + "\n\n")

# =====================================================================
# メイン処理
# =====================================================================
def main():
    print("=== X Knowledge Extractor (CUI Backend) Start ===")
    init_db()
    init_csv()
    
    if not os.path.exists(LIST_PATH):
        with open(LIST_PATH, "w", encoding="utf-8") as f:
            pass
        print(f"'{LIST_PATH}' not found. Created empty file. Please add URLs and rerun.")
        sys.exit(0)
        
    with open(LIST_PATH, "r", encoding="utf-8") as f:
        lines = f.readlines()
        
    lines = [l for l in lines if l.strip()]
    save_list_atomic(lines)
    
    if not lines:
        print("No active lines found in list.txt. Exiting.")
        sys.exit(0)
        
    existing_ids = load_existing_ids()
    print(f"Database currently holds {len(existing_ids)} active synchronized tweets.")
    
    while lines:
        current_line = lines[0]
        line_str = current_line.strip()
        
        match = re.search(r'https?://\S+', line_str)
        if not match:
            print(f"-> [SKIP - NO URL] Line removed: '{line_str}'")
            lines.pop(0)
            save_list_atomic(lines)
            continue
            
        raw_url = match.group(0)
        print(f"\n[Analyzing URL] {raw_url}")
        
        final_url = resolve_final_url(raw_url)
        
        if "x.com" not in final_url and "twitter.com" not in final_url and "api.fxtwitter.com" not in final_url:
            print(f"-> [EXTERNAL URL] Isolating to {SKIPPED_URLS_PATH}")
            append_to_file(SKIPPED_URLS_PATH, line_str)
            lines.pop(0)
            save_list_atomic(lines)
            continue
            
        tweet_id = extract_tweet_id(final_url)
        if not tweet_id:
            print(f"-> [CANNOT EXTRACT ID] Isolating to {SKIPPED_URLS_PATH}")
            append_to_file(SKIPPED_URLS_PATH, line_str)
            lines.pop(0)
            save_list_atomic(lines)
            continue
            
        if tweet_id in existing_ids:
            print(f"-> [SKIP - DUPLICATE] ID {tweet_id} already exists. Removing line.")
            lines.pop(0)
            save_list_atomic(lines)
            continue
            
        api_url = make_api_url(final_url)
        
        result = fetch_tweet_with_retry(api_url)
        
        if result["success"]:
            try:
                parsed_data = parse_and_download_media(result["data"], final_url)
                write_to_storages(parsed_data)
                
                existing_ids.add(tweet_id)
                print(f"[SUCCESS] Saved (ID: {tweet_id} / @{parsed_data['author_screen_name']})")
                
                lines.pop(0)
                save_list_atomic(lines)
                
                wait_time = random.uniform(1.5, 2.0)
                time.sleep(wait_time)
                
            except Exception as e:
                print(f"[ERROR - PARSE FAILED] Processing failed unexpectedly: {e}")
                print("Emergency exit to protect current line in list.txt.")
                sys.exit(1)
        else:
            if result["type"] == "permanent":
                print(f"[PERMANENT ERROR] Code {result['code']} ({result['reason']}). Logged & skipping.")
                append_to_file(FAILED_TWEETS_PATH, f"URL: {line_str} | Reason: {result['reason']}")
                lines.pop(0)
                save_list_atomic(lines)
            else:
                print(f"[TEMPORARY ERROR CAP] Reason: {result['reason']}")
                print("Line preserved in list.txt. Please check connection and retry later.")
                sys.exit(1)

    print("\n=== All lines processed successfully! ===")

if __name__ == "__main__":
    main()
