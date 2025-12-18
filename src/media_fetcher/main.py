import json
import os
import sys
import re
import time
import shutil
import subprocess
import shlex
import requests
from datetime import datetime
from typing import Optional, Tuple, Any
from urllib.parse import urlparse, parse_qs

# Third-party imports
import boto3
import yt_dlp
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException


# --- Configuration ---
S3_BUCKET = os.environ.get("S3_BUCKET")

def setup_driver() -> webdriver.Chrome:
    """
    Initializes Headless Chrome for AWS Lambda.
    """
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-gpu")
    options.add_argument("--single-process")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36")
    
    # Locations for Chrome/Chromedriver in Lambda environment
    chrome_bin = os.environ.get("CHROME_BIN", "/opt/chrome/chrome")
    driver_bin = os.environ.get("CHROMEDRIVER", "/opt/chromedriver/chromedriver") 
    
    options.binary_location = chrome_bin
    service = Service(executable_path=driver_bin)
    
    print("[INFO] Initializing Chrome Driver...")
    return webdriver.Chrome(service=service, options=options)

# --- Regex definitions ---
# (You may need to add all the _RX definitions from your main.py if get_legmedia_stream_url needs them)
_DATE_RX = re.compile(
    r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+[A-Za-z]+\s+\d{1,2},\s+\d{4}"
)

# --- Helper Functions (Parsing and Logic) ---

def _parse_date_anywhere(text: str) -> Optional[str]:
    m = _DATE_RX.search(text or "")
    if not m: return None
    try:
        d = datetime.strptime(m.group(0), "%A, %B %d, %Y")
        return d.strftime("%Y-%m-%d")
    except Exception:
        return None

def _parse_agenda_date_from_url(url: str) -> Optional[str]:
    try:
        qs = parse_qs(urlparse(url).query)
        ad = qs.get("agendaDate", [None])[0]
        if ad and re.match(r"\d{4}-\d{2}-\d{2}", ad):
            return ad[:10]
    except Exception: pass
    return None

def committee_code(driver:webdriver.Chrome, user_input_name:str) -> str:
    """
    Determines the 3-letter committee code (e.g., 'SBA', 'AST') by:
    1. Checking a static dictionary of common/irregular codes (Fastest).
    2. Scraping the official Committees page to find the link (Robust).
    3. Fallback to guessing.
    """
    print(f"[DEBUG] Resolving code for: '{user_input_name}'")
    
    clean_input = user_input_name.lower().replace("committee", "").strip()

    # --- LEVEL 1: Static Dictionary (Instant Lookup) ---
    static_map = {
        "senate budget and appropriations": "SBA",
        "Senate Budget and Appropriations Committee": "SBAB",
        "Senate Commerce Committee": "SCM",
        "Senate Community and Urban Affairs Committee": "SCU",
        "Senate Economic Growth Committee": "SEG",
        "senate environment and energy": "SEN",
        "Senate Health, Human Services and Senior Citizens Committee": "SHH",
        "Senate Labor Committee": "SLA",
        "Senate Law and Public Safety": "SLP",
        "Senate Military and Veterans' Affairs": "SMV",
        "Senate State Government, Wagering, Tourism & Historic Preservation": "SSG",
        "Senate Legislative Oversight Committee": "SLO",
        "assembly science, innovation and technology": "AST",
        "assembly budget": "ABU",
        "senate judiciary": "SJU",
        "assembly judiciary": "AJU",
        "senate education": "SED",
        "assembly education": "AED",
        "senate health, human services and senior citizens": "SHH",
        "assembly health": "AHE"
    }
    
    if clean_input in static_map:
        code = static_map[clean_input]
        print(f"[INFO] Found code '{code}' in static map.")
        return code

    # --- LEVEL 2: Dynamic Scraping (The Safety Net) ---
    print(f"[INFO] Code not in static map. Scraping NJLeg website for match...")
    
    try:
        driver.get("https://www.njleg.state.nj.us/committees")
        
        # Wait for links to load
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "a[href*='/committees/']"))
        )
        
        links = driver.find_elements(By.CSS_SELECTOR, "a[href*='/committees/']")
        search_terms = clean_input.split()
        
        for link in links:
            link_text = link.text.lower()
            link_href = link.get_attribute("href")
            
            # Check if all search words exist in the link text
            if all(term in link_text for term in search_terms):
                # Extract code from URL (e.g. /committees/AST)
                code = link_href.rstrip('/').split('/')[-1].upper()
                if len(code) == 3:
                    print(f"[SUCCESS] Dynamically found code '{code}' for '{link_text}'")
                    return code

        print("[WARN] Could not find a matching committee link on the website.")
    except Exception as e:
        print(f"[ERROR] Failed to scrape committee code: {e}")

    # --- LEVEL 3: Fallback Algorithm (Guessing) ---
    print("[WARN] Fallback to algorithmic guessing.")
    words = [w for w in clean_input.split() if w not in ("and", "&", "the", "of")]
    if not words: return "XXX"
    
    chamber = "S" if "senate" in clean_input else "A"
    clean_words = [w for w in words if "senate" not in w and "assembly" not in w]
    
    if not clean_words: return chamber + "XX"
    
    code = chamber
    for w in clean_words:
        code += w[0].upper()
        if len(code) == 3: break
    return code

##Browser Scraping
def find_meeting_list_url(driver: webdriver.Chrome, year: str, committee_name: str, code:str) -> Optional[str]:
    
    candidates = [
        f"https://www.njleg.state.nj.us/archived-media/{year}/{code}",
        f"https://www.njleg.state.nj.us/archived-media/{year}/{code}-meeting-list",
    ]
    def _page_has_media_links(d):
        return d.find_elements(By.CSS_SELECTOR, "a[href*='media-player']")
    for url in candidates:
        try:
            print(f"[DEBUG] Attempting to load: {url}")
            driver.get(url)
            WebDriverWait(driver, 20).until(_page_has_media_links) # 20 sec timeout
            print(f"[DEBUG] Successfully found media-player links at: {url}")
            return url
        except Exception as e:
            print(f"[DEBUG] No media links found on {url} ({e})")
            continue
    print(f"[INFO] No valid meeting list URL found for {committee_name} in {year}.")
    return None

def select_media_link_with_fallback(driver: webdriver.Chrome) -> Optional[Tuple[str, str, Optional[str], str]]:
    try:
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "a[href*='media-player']"))
        )
    except Exception:
        print("[WARN] Waited for media links, but none appeared.")
        return None
    links = driver.find_elements(By.CSS_SELECTOR, "a[href*='media-player']")
    if not links: return None
    audio_el = next((el for el in links if "av=A" in (el.get_attribute("href") or "")), None)
    video_el = next((el for el in links if "av=V" in (el.get_attribute("href") or "")), None)
    primary_el   = audio_el or video_el or links[0] # Prefer Audio, then Video
    primary_href = (primary_el.get_attribute("href") or "").strip()
    primary_kind = "audio" if "av=A" in primary_href else ("video" if "av=V" in primary_href else "audio")
    alternate_el = (video_el if primary_kind == "audio" else audio_el)
    alternate    = (alternate_el.get_attribute("href").strip() if alternate_el else None)
    meeting_date = None
    try:
        container = primary_el.find_element(By.XPATH, "./ancestor::*[self::tr or @role='row'][1]")
        meeting_date = _parse_date_anywhere(container.text)
    except Exception: pass
    if not meeting_date:
        meeting_date = _parse_agenda_date_from_url(primary_href) or datetime.now().strftime("%Y-%m-%d")
    print(f"[DEBUG] Selected media link. Kind: {primary_kind}, Date: {meeting_date}, URL: {primary_href}")
    return (primary_href, primary_kind, alternate, meeting_date)

# --- CORE MEDIA FUNCTIONS ---

def get_legmedia_stream_url(agenda_date, agenda_type, av, committee_code, session, index="0") -> Optional[str]:
    """
    Reverse-engineered API call to get the HLS stream URL.
    This bypasses the unstable Selenium player page completely.
    """
    # URL structure was found in from the website structure
    api_url = f"https://www.njleg.state.nj.us/api/videoRetrieval/getLegMedia/{agenda_date}/{agenda_type}/{av}/{committee_code}/{index}/{session}"

    print(f"[DEBUG] Calling NJLeg API with URL: {api_url}")
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://www.njleg.state.nj.us/"
    }

    try:
        resp = requests.get(api_url, headers=headers, timeout=20)
        resp.raise_for_status() # Raise error for bad responses (4xx, 5xx)

        # The API response is plain text, just the URL.
        media_url = resp.text.strip()

        if media_url and (media_url.lower().endswith(".m3u8") or ".m3u8?" in media_url.lower()):
            print(f"[DEBUG] API SUCCESS: Found .m3u8 URL: {media_url}")
            return media_url
        else:
            print(f"[WARN] API returned text, but it wasn't an .m3u8 URL: {media_url}")
            return None

    except requests.RequestException as e:
        print(f"[ERROR] API call failed: {e}")
        return None

class NullLogger:
    """
    Absorbs all logs. Does absolutely nothing.
    This ensures yt-dlp never attempts to call print().
    """
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass

# --- DOWNLOAD FUNCTION ---

def download_with_ffmpeg(hls_url: str, referer_url: str, output_filename_base: str) -> str:
    print(f"[INFO] Starting download via DIRECT FFMPEG (Bypassing Python I/O)...")
    
    # 1. Define Paths
    safe_filename = os.path.basename(output_filename_base)
    final_mp3_path = f"/tmp/{safe_filename}.mp3"
    
    # Clean up previous files
    if os.path.exists(final_mp3_path):
        os.remove(final_mp3_path)

    # 2. PREPARE FFMPEG (Copy to /tmp for execution permissions)
    # We cannot execute directly from /opt sometimes depending on the layer config,
    # but copying to /tmp is the safest "nuclear" option.
    original_ffmpeg = shutil.which('ffmpeg') or '/opt/bin/ffmpeg' or '/usr/local/bin/ffmpeg'
    ffmpeg_path = "/tmp/ffmpeg"
    
    if not os.path.exists(ffmpeg_path):
        if os.path.exists(original_ffmpeg):
            print(f"[INFO] Copying ffmpeg from {original_ffmpeg} to {ffmpeg_path}...")
            shutil.copy(original_ffmpeg, ffmpeg_path)
            os.chmod(ffmpeg_path, 0o755) # Make executable
        else:
            print(f"[ERROR] Could not find system ffmpeg. Download will fail.")
            return None

    # 3. CONSTRUCT FFMPEG COMMAND
    # We pass headers directly to ffmpeg. Note the syntax for -headers.
    headers_str = f"Referer: {referer_url}\r\nUser-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

    cmd = [
        ffmpeg_path,
        '-headers', headers_str,
        '-i', hls_url,          # Input URL
        '-vn',                  # No Video (Audio only)
        '-c:a', 'libmp3lame',   # Codec: MP3
        '-q:a', '2',            # Quality: VBR Standard (roughly 190kbps)
        '-y',                   # Overwrite output
        '-nostdin',             # Do not expect input (Critical for Lambda)
        '-loglevel', 'warning', # Reduce log noise
        final_mp3_path          # Output file
    ]

    print(f"[DEBUG] Executing command: {shlex.join(cmd)}")

    try:
        # 4. RUN SUBPROCESS
        result = subprocess.run(
            cmd,
            capture_output=True, 
            text=True,
            check=True,
            timeout=900 # 15 minute timeout for large files
        )
        
        if os.path.exists(final_mp3_path):
            print(f"[SUCCESS] File created: {final_mp3_path}")
            return final_mp3_path
        else:
            print("[ERROR] FFmpeg finished successfully but file is missing.")

    except subprocess.CalledProcessError as e:
        print(f"[ERROR] FFmpeg failed with exit code: {e.returncode}")
        print("--- FFMPEG STDERR ---")
        print(e.stderr) 
        print("--- END STDERR ---")

    except subprocess.TimeoutExpired:
        print("[ERROR] FFmpeg timed out.")
        
    except Exception as e:
        print(f"[ERROR] Unexpected error in ffmpeg subprocess: {e}")

    return None
    
def build_stream_url(agenda_date_full: str, committee: str, agenda_type: str, original_av: str) -> Optional[str]:
        """
        Builds the HLS stream URL directly based on URL query parameters.
        This bypasses the failing getLegMedia API.
        Based on reverse-engineered patterns:
        - Video (av=V): .../YEAR/COMMITTEE/smil:MMDD-HHMMPM-TYPE0-1.smil/playlist.m3u8
        - Audio (av=A): .../YEAR/COMMITTEE/MMDD-HHMMPM-TYPE0-1.m4a/playlist.m3u8
        """
        print(f"[DEBUG] Building stream URL from params: Date={agenda_date_full}, Comm={committee}, Type={agenda_type}, AV={original_av}")

        try:
            # 1. Parse the full datetime string
            # Example: "2025-11-13-13:00:00"
            dt = datetime.strptime(agenda_date_full, "%Y-%m-%d-%H:%M:%S")
        except ValueError as e:
            print(f"[ERROR] Could not parse full agendaDate: {agenda_date_full}. Error: {e}")
            return None

    # 2. Format the components
        base_url = "https://5b73e41adb3b9.streamlock.net/archive/_definst_"
        year = dt.strftime("%Y")           # e.g., "2025"
        mmdd = dt.strftime("%m%d")           # e.g., "1113"
        time_str = dt.strftime("%I%M%p")   # e.g., "0100PM" (AM/PM is uppercase)

        # 3. Create the unique file part
        # Example: "1113-0100PM-M0-1"
        file_part = f"{mmdd}-{time_str}-{agenda_type}0-1"

        # 4. Create the media-specific path part based on *original* AV type
        if original_av == "V":
            # This was a video link, so use the .smil pattern
            media_path = f"smil:{file_part}.smil"
            print("[DEBUG] Using 'smil' (video) path pattern.")
        else:
            # This was an audio-only link, use the .m4a pattern
            media_path = f"{file_part}.m4a"
            print("[DEBUG] Using 'm4a' (audio) path pattern.")

        # 5. Assemble the final URL
        final_url = f"{base_url}/{year}/{committee}/{media_path}/playlist.m3u8"

        print(f"[SUCCESS] Built HLS URL: {final_url}")
        return final_url

## storage path helper
def determine_committee_folder(committee_name: str) -> str:
    """
    Determines the subfolder based on the committee name.
    Returns: 'Senate', 'Assembly', 'Joint', or 'Other'
    """
    name_lower = committee_name.lower()
    
    if "joint" in name_lower:
        return "Joint"
    elif "senate" in name_lower:
        return "Senate"
    elif "assembly" in name_lower:
        return "Assembly"
    else:
        return "Other"

def lambda_handler(event, context):
    print("Received event:", json.dumps(event))

    # 1. --- Extract values from the top-level 'event' object (Direct Lambda/Test Invocation) ---
    # Your sample input {"committee_name": "...", "session": "..."} is here.
    COMMITTEE_NAME = event.get('committee_name') 
    SESSION = event.get('session')
    
    # 2. --- Parse the body for potential overrides (e.g., API Gateway POST request) ---
    try:
        body = json.loads(event.get('body', '{}'))
    except (json.JSONDecodeError, TypeError):
        body = {}
        
    # 3. --- Fallback/Override with 'body' content ---
    # Only update if the 'body' has a non-None value. 
    # Use the current variable value (from step 1) as the fallback default.
    #COMMITTEE_NAME = body.get('committeeName', COMMITTEE_NAME)
    #SESSION = body.get('session', SESSION)
    
    # --- Check for missing required values ---
    if not COMMITTEE_NAME or not SESSION:
        print("[ERROR] Missing required parameters 'committee_name' or 'session'.")
        return {
            'statusCode': 400,
            'body': json.dumps({'message': 'Missing required parameters: committee_name and session'})
        }

    years_to_try = []
    if "-" in SESSION:
        left, right = SESSION.split("-", 1)
        left, right = left.strip(), right.strip()
        if len(right) == 2: right = left[:2] + right
        years_to_try = [right, left]
    else:
        years_to_try = [SESSION.strip()]

    print(f"[INFO] Target Committee: {COMMITTEE_NAME}")
    print(f"[INFO] Target Session: {SESSION} (Will check years: {years_to_try})")

    driver = None
    final_output_file = None
    list_url = None
    try:
        driver = setup_driver()
        committee_code_str = committee_code(driver, COMMITTEE_NAME)
        print(f"[INFO] Determined committee code: {committee_code_str}")

        for year in years_to_try:
            print(f"[INFO] Checking year: {year}...")
            list_url = find_meeting_list_url(driver, year, COMMITTEE_NAME, committee_code_str)
            if list_url:
                print(f"[INFO] Found valid meeting list at URL for year: {year}")
                break

        if list_url:
            choice = select_media_link_with_fallback(driver)
            if choice:
                href, kind, alt_href, meeting_date = choice
                print("[INFO] Shutting down Selenium driver...")
                driver.quit()
                driver = None

                from urllib.parse import urlparse, parse_qs
                qs = parse_qs(urlparse(href).query)
                agenda_date_full = qs.get("agendaDate", [""])[0]
                original_av = qs.get("av", ["A"])[0]
                agenda_type = qs.get("agendaType", ["M"])[0]
                committee = committee_code_str

                if not agenda_date_full:
                    print("[ERROR] Could not parse agendaDate from URL. Cannot build stream URL.")
                    media_url = None
                else:
                    print("[INFO] Bypassing API, building stream URL directly from parameters.")
                    media_url = build_stream_url(
                        agenda_date_full=agenda_date_full,
                        committee=committee,
                        agenda_type=agenda_type,
                        original_av=original_av
                    )

                if media_url:
                    output_base_filename = f"{committee}_{meeting_date}"
                    ## check for duplicate file in s3
                    # --- NEW: DUPLICATE CHECK ---
                    subfolder = determine_committee_folder(COMMITTEE_NAME)
                    s3_check_key = f"audio/{subfolder}/{output_base_filename}.mp3"
                    
                    try:
                        s3_client.head_object(Bucket=S3_BUCKET, Key=s3_check_key)
                        print(f"[INFO] Skipping: {s3_check_key} already exists.")
                        return {
                            'statusCode': 200,
                            'body': json.dumps({'message': 'Meeting already processed', 'skipped': True})
                        }
                    except:
                        print(f"[INFO] New meeting found. Proceeding to download...")
                        
                    final_output_file = download_with_ffmpeg(
                        hls_url=media_url,
                        referer_url=href,
                        output_filename_base=output_base_filename
                    )

                    if final_output_file:
                        # --- 2. UPLOAD TO S3 (The Final Step) ---
                        subfolder = determine_committee_folder(COMMITTEE_NAME)
                        filename = os.path.basename(final_output_file)
                        s3_key = f"audio/{subfolder}/{filename}"
                        s3_client = boto3.client('s3')
                        
                        print(f"[INFO] Uploading to S3 bucket: {S3_BUCKET} key: {s3_key}")
                        try:
                            s3_client.upload_file(final_output_file, S3_BUCKET, s3_key)
                            print(f"[SUCCESS] Uploaded to s3://{S3_BUCKET}/{s3_key}")
                            
                            # Clean up local file to free space in /tmp
                            os.remove(final_output_file)
                            
                            return {
                                'statusCode': 200,
                                'body': json.dumps({
                                    'message': 'Download and Upload successful', 
                                    's3_uri': f"s3://{S3_BUCKET}/{s3_key}",
                                    'base_filename': output_base_filename
                                })
                            }
                        except Exception as e:
                            print(f"[ERROR] S3 Upload failed: {e}")
                            return {
                                'statusCode': 500,
                                'body': json.dumps({'message': f'S3 Upload failed: {str(e)}'})
                            }
                            
                    else:
                         return {
                            'statusCode': 500,
                            'body': json.dumps({'message': 'Download failed (no file created)'})
                        }
            else:
                print(f"[ERROR] Found meeting list page ({list_url}), but no media links were found on it.")
                return {
                    'statusCode': 404,
                    'body': json.dumps({'message': 'No media links found on the page'})
                }
        else:
            print(f"[ERROR] Could not find any valid meeting list page for {COMMITTEE_NAME} in session {SESSION} (tried years: {years_to_try}).")
            return {
                'statusCode': 404,
                'body': json.dumps({'message': 'No valid meeting list page found'})
            }

    except Exception as e:
        print(f"[FATAL ERROR] An unexpected error occurred: {e}")
        return {
            'statusCode': 500,
            'body': json.dumps({'message': f'An unexpected error occurred: {str(e)}'})
        }
    finally:
        if driver:
            print("[INFO] Shutting down lingering driver in 'finally' block...")
            driver.quit()