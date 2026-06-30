import os
import re
import glob
import json
import time
import urllib.parse
import threading
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, render_template, request, Response, jsonify, stream_with_context, redirect, url_for, session
import requests

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = 'livetv-secure-admin-session-secret-key-9812'

@app.context_processor
def inject_global_variables():
    """Inject banner and advertisement settings globally into all templates."""
    return {
        "banner_config": load_banner_config()
    }

# Global variables for channels
channels_list = []
channels_by_id = {}
categories_list = []
data_lock = threading.Lock()

# Global variables for live analytics
active_sessions = {}

BANNER_CONFIG_FILE = "banner_config.json"

def load_banner_config():
    """Load banner configuration from file or return defaults."""
    defaults = {
        "primary_channel_id": "",
        "recommended_channel_ids": [],
        "title": "High Quality Buffer-Free Streaming",
        "description": "Watch your favorite live events, cricket matches, movies, news and kids entertainment channels with no buffer latency and premium proxy routing.",
        "ad_popunder": "",
        "ad_socialbar": "",
        "ad_leaderboard": "",
        "ad_sidebar": "",
        "admin_pin": ""
    }
    if os.path.exists(BANNER_CONFIG_FILE):
        try:
            with open(BANNER_CONFIG_FILE, 'r') as f:
                config = json.load(f)
                # Merge defaults for missing keys
                for k, v in defaults.items():
                    if k not in config:
                        config[k] = v
                return config
        except Exception as e:
            logger.error(f"Error loading banner config: {e}")
            
    return defaults

def save_banner_config(config):
    """Save banner configuration to file."""
    try:
        with open(BANNER_CONFIG_FILE, 'w') as f:
            json.dump(config, f, indent=4)
        return True
    except Exception as e:
        logger.error(f"Error saving banner config: {e}")
        return False

def slugify(text):
    """Generate a clean URL-friendly ID from channel name."""
    text = text.lower()
    text = re.sub(r'[^a-z0-9\s-]', '', text)
    text = re.sub(r'[\s-]+', '-', text)
    return text.strip('-')

def normalize_channel_name(name):
    """Normalize channel name by removing suffixes to group servers correctly."""
    clean = name.strip()
    clean = re.sub(r'(?i)\s+(hd|sd|vip|fhd|live|\[live\]|\(local\)|\(ads only\))', '', clean)
    clean = re.sub(r'\s+', ' ', clean)
    return clean.strip()

def get_device_from_ua(ua):
    """Parse device type from User-Agent string."""
    if not ua:
        return "Unknown"
    ua = ua.lower()
    if any(x in ua for x in ["smarttv", "smart-tv", "tizen", "web0s", "appletv", "googletv", "hbbptv", "netcast", "tstream"]):
        return "Smart TV"
    elif any(x in ua for x in ["exoplayer", "vlc", "kodi", "player", "mxplayer", "libvlc"]):
        return "Media Player"
    elif any(x in ua for x in ["ipad", "tablet", "playbook", "kindle"]):
        return "Tablet"
    elif any(x in ua for x in ["android", "iphone", "ipod", "windows phone", "mobile", "phone"]):
        return "Mobile"
    elif any(x in ua for x in ["windows", "macintosh", "linux", "cros"]):
        return "Desktop"
    return "Desktop"

@app.before_request
def track_user():
    """Middleware to track online users and their devices."""
    path = request.path
    # Ignore asset and backend proxy requests to avoid analytics noise
    if path.startswith('/static') or path.startswith('/proxy') or path == '/api/channels':
        return
        
    ip = request.remote_addr
    ua = request.headers.get('User-Agent', 'Unknown')
    
    with data_lock:
        active_sessions[ip] = {
            "last_active": time.time(),
            "user_agent": ua,
            "device": get_device_from_ua(ua)
        }

def auto_inject_headers(url, headers):
    """Automatically inject required Referer/Origin headers for known geo-blocked streaming domains."""
    url_lower = url.lower()
    injected = {k: v for k, v in headers.items()}
    
    # Binge / Grameenphone CDN streams
    if "rockstreamer" in url_lower or "gpcdn.net" in url_lower:
        if 'Referer' not in injected:
            injected['Referer'] = 'https://binge.valyote.com/'
        if 'Origin' not in injected:
            injected['Origin'] = 'https://valyote.com'
            
    # Bioscope CDN streams
    elif "aynascope" in url_lower or "bioscopelive" in url_lower:
        if 'Referer' not in injected:
            injected['Referer'] = 'https://www.bioscopelive.com/'
        if 'Origin' not in injected:
            injected['Origin'] = 'https://www.bioscopelive.com'
            
    # Toffee streams
    elif "toffeelive" in url_lower:
        if 'Referer' not in injected:
            injected['Referer'] = 'https://toffeelive.com/'
            
    return injected

def is_stream_working(url, headers):
    """Check if a stream URL is active and connecting using the given headers."""
    if not (url.startswith("http://") or url.startswith("https://")):
        return False
        
    req_headers = auto_inject_headers(url, headers)
    if 'User-Agent' not in req_headers:
        req_headers['User-Agent'] = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        
    try:
        r = requests.head(url, headers=req_headers, timeout=2.5, verify=False, allow_redirects=True)
        if r.status_code in [200, 206, 301, 302]:
            return True
            
        r = requests.get(url, headers=req_headers, timeout=2.5, verify=False, stream=True, allow_redirects=True)
        if r.status_code in [200, 206, 301, 302]:
            r.close()
            return True
    except Exception:
        pass
        
    return False

def parse_m3u_content(m3u_text):
    """Parse M3U text and extract channel metadata and headers."""
    parsed_channels = []
    lines = m3u_text.splitlines()

    current_channel = None
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
            
        if line.startswith("#EXTINF:"):
            logo = ""
            group = "General"
            
            logo_match = re.search(r'tvg-logo=["\']([^"\']+)["\']', line)
            if logo_match:
                logo = logo_match.group(1)
                
            group_match = re.search(r'group-title=["\']([^"\']+)["\']', line)
            if group_match:
                group = group_match.group(1).strip()
                if group.upper().startswith("SM "):
                    group = group[3:].strip()
                elif group.upper().startswith("SM_"):
                    group = group[3:].strip()
                
            name = "Unknown Channel"
            comma_index = line.rfind(',')
            if comma_index != -1:
                name = line[comma_index+1:].strip()
                
            current_channel = {
                "name": name,
                "logo": logo,
                "group": group,
                "headers": {},
                "url": ""
            }
            
        elif line.startswith("#EXTVLCOPT:"):
            if current_channel is not None:
                opt = line[len("#EXTVLCOPT:"):].strip()
                if '=' in opt:
                    key, val = opt.split('=', 1)
                    key = key.strip().lower()
                    val = val.strip()
                    
                    if key == "http-user-agent":
                        current_channel["headers"]["User-Agent"] = val
                    elif key in ["http-referrer", "http-referer"]:
                        current_channel["headers"]["Referer"] = val
                    elif key == "http-origin":
                        current_channel["headers"]["Origin"] = val
                    elif key in ["http-cookie", "cookie"]:
                        current_channel["headers"]["Cookie"] = val
                        
        elif line.startswith("#EXTHTTP:"):
            if current_channel is not None:
                http_opt = line[len("#EXTHTTP:"):].strip()
                try:
                    headers_dict = json.loads(http_opt)
                    for k, v in headers_dict.items():
                        standard_k = k.strip().title()
                        current_channel["headers"][standard_k] = v
                except Exception:
                    pass
                    
        elif not line.startswith("#"):
            if current_channel is not None:
                current_channel["url"] = line
                if line.startswith("http://") or line.startswith("https://"):
                    parsed_channels.append(current_channel)
                current_channel = None
                
    return parsed_channels

def reload_channels():
    """Reload M3U playlists directly from raw GitHub URL, validate streams in parallel, group by channel, and map categories."""
    global channels_list, channels_by_id, categories_list
    
    url = "https://raw.githubusercontent.com/sm-monirulislam/SM-Live-TV/main/Combined_Live_TV.m3u"
    logger.info(f"Fetching raw M3U playlist from: {url}")
    
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        m3u_text = r.text
    except Exception as e:
        logger.error(f"Error fetching raw M3U from git: {e}")
        return
        
    unique_candidates_by_url = {}
    
    try:
        file_channels = parse_m3u_content(m3u_text)
        for ch in file_channels:
            url_str = ch["url"]
            if not url_str or url_str.startswith("http://localhost") or "placeholder" in url_str:
                continue
                
            if url_str in unique_candidates_by_url:
                unique_candidates_by_url[url_str]["headers"].update(ch["headers"])
                if ch["group"] not in unique_candidates_by_url[url_str]["groups"]:
                    unique_candidates_by_url[url_str]["groups"].append(ch["group"])
            else:
                ch["groups"] = [ch["group"]]
                unique_candidates_by_url[url_str] = ch
    except Exception as e:
        logger.error(f"Error parsing raw M3U text: {e}")
        return

    candidates_to_check = list(unique_candidates_by_url.values())
    if not candidates_to_check:
        logger.warning("No candidate streams found to validate.")
        return

    logger.info(f"Validating {len(candidates_to_check)} streams in parallel using ThreadPoolExecutor...")
    
    working_channels = []
    with ThreadPoolExecutor(max_workers=30) as executor:
        future_to_ch = {
            executor.submit(is_stream_working, ch["url"], ch["headers"]): ch 
            for ch in candidates_to_check
        }
        
        for future in as_completed(future_to_ch):
            ch = future_to_ch[future]
            try:
                if future.result():
                    working_channels.append(ch)
            except Exception as e:
                logger.error(f"Error checking stream {ch['url']}: {e}")
                
    logger.info(f"Validation completed. Working streams: {len(working_channels)}/{len(candidates_to_check)}")

    # Group working streams by normalized name to build multi-server architecture
    grouped_channels = {}
    for ch in working_channels:
        norm_name = normalize_channel_name(ch["name"])
        
        if norm_name in grouped_channels:
            parent = grouped_channels[norm_name]
            
            # Merge categories
            for grp in ch["groups"]:
                if grp not in parent["groups"]:
                    parent["groups"].append(grp)
                    
            # Merge stream server URLs
            urls_in_parent = [srv["url"] for srv in parent["servers"]]
            if ch["url"] not in urls_in_parent:
                srv_num = len(parent["servers"]) + 1
                parent["servers"].append({
                    "name": f"Server {srv_num}",
                    "url": ch["url"],
                    "headers": ch["headers"]
                })
                
            if not parent["logo"] and ch["logo"]:
                parent["logo"] = ch["logo"]
        else:
            grouped_channels[norm_name] = {
                "name": norm_name,
                "logo": ch["logo"],
                "groups": ch["groups"],
                "servers": [
                    {
                        "name": "Server 1",
                        "url": ch["url"],
                        "headers": ch["headers"]
                    }
                ]
            }

    # Finalize channels list, slug IDs, and sort categories with Sports and World Cup priority
    temp_list = []
    temp_by_id = {}
    temp_categories = set()
    used_ids = {}
    
    for norm_name, ch in grouped_channels.items():
        base_id = slugify(ch["name"])
        if not base_id:
            base_id = "channel"
            
        slug_id = base_id
        count = 1
        while slug_id in used_ids:
            slug_id = f"{base_id}-{count}"
            count += 1
        used_ids[slug_id] = True
        
        ch["id"] = slug_id
        temp_list.append(ch)
        temp_by_id[slug_id] = ch
        
        for grp in ch["groups"]:
            temp_categories.add(grp)
            
    # PRIORITIZE CATEGORIES: Sports Channels/Sports first, then World Cup, then others alphabetically
    sorted_categories = sorted(list(temp_categories))
    
    sports_cats = []
    wc_cats = []
    other_cats = []
    
    for cat in sorted_categories:
        cat_lower = cat.lower()
        if "sports" in cat_lower:
            sports_cats.append(cat)
        elif "world_cup" in cat_lower or "world cup" in cat_lower:
            wc_cats.append(cat)
        else:
            other_cats.append(cat)
            
    # Combine lists
    prioritized_categories = sports_cats + wc_cats + other_cats
    sorted_channels = sorted(temp_list, key=lambda x: x["name"])
    
    with data_lock:
        channels_list = sorted_channels
        channels_by_id = temp_by_id
        categories_list = prioritized_categories

def start_scheduler():
    """Spawn the 1-minute background reloader thread."""
    def run_scheduler():
        while True:
            time.sleep(60)
            try:
                reload_channels()
            except Exception as e:
                logger.error(f"Scheduler error: {e}")
            
    thread = threading.Thread(target=run_scheduler, daemon=True)
    thread.start()

# Load channels initially
reload_channels()
start_scheduler()

# ==============================================================================
# HLS Playlist and TS Chunk Rewrite Proxy
# ==============================================================================

def rewrite_m3u8(playlist_text, base_url, headers_dict):
    """Rewrite absolute/relative links in m3u8 file to pass through proxy."""
    lines = playlist_text.split('\n')
    new_lines = []
    headers_str = json.dumps(headers_dict)
    
    for line in lines:
        line_strip = line.strip()
        if not line_strip:
            new_lines.append(line)
            continue
            
        if line_strip.startswith('#'):
            if 'URI=' in line_strip:
                def replace_uri(match):
                    uri = match.group(1)
                    abs_uri = urllib.parse.urljoin(base_url, uri)
                    proxied_url = f"/proxy/ts?url={urllib.parse.quote_plus(abs_uri)}&headers={urllib.parse.quote_plus(headers_str)}"
                    return f'URI="{proxied_url}"'
                
                rewritten_line = re.sub(r'URI=["\']([^"\']+)["\']', replace_uri, line_strip)
                new_lines.append(rewritten_line)
            else:
                new_lines.append(line)
        else:
            abs_url = urllib.parse.urljoin(base_url, line_strip)
            if '.m3u8' in abs_url.lower() or 'playlist' in abs_url.lower():
                proxied_url = f"/proxy/m3u8?url={urllib.parse.quote_plus(abs_url)}&headers={urllib.parse.quote_plus(headers_str)}"
            else:
                proxied_url = f"/proxy/ts?url={urllib.parse.quote_plus(abs_url)}&headers={urllib.parse.quote_plus(headers_str)}"
            new_lines.append(proxied_url)
            
    return '\n'.join(new_lines)

@app.route('/proxy/m3u8')
def proxy_m3u8():
    """Proxy endpoint for HLS playlists (.m3u8) to bypass CORS and inject headers."""
    target_url = request.args.get('url')
    headers_str = request.args.get('headers', '{}')
    
    if not target_url:
        return "Missing URL parameter", 400
        
    try:
        headers = json.loads(headers_str)
    except Exception:
        headers = {}
        
    headers = auto_inject_headers(target_url, headers)
    if 'User-Agent' not in headers:
        headers['User-Agent'] = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        
    try:
        r = requests.get(target_url, headers=headers, timeout=15, verify=False)
        r.raise_for_status()
        
        parsed_url = urllib.parse.urlparse(target_url)
        base_path = os.path.dirname(parsed_url.path)
        if not base_path.endswith('/'):
            base_path += '/'
        base_url = f"{parsed_url.scheme}://{parsed_url.netloc}{base_path}"
        
        rewritten_content = rewrite_m3u8(r.text, base_url, headers)
        
        response = Response(rewritten_content, content_type='application/x-mpegURL')
        response.headers['Access-Control-Allow-Origin'] = '*'
        return response
    except Exception as e:
        logger.error(f"Error proxying playlist {target_url}: {e}")
        return f"Proxying error: {e}", 502

@app.route('/proxy/ts')
def proxy_ts():
    """Proxy endpoint for video segments (.ts / .mp4 / key files) to inject headers."""
    target_url = request.args.get('url')
    headers_str = request.args.get('headers', '{}')
    
    if not target_url:
        return "Missing URL parameter", 400
        
    try:
        headers = json.loads(headers_str)
    except Exception:
        headers = {}
        
    headers = auto_inject_headers(target_url, headers)
    if 'User-Agent' not in headers:
        headers['User-Agent'] = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        
    try:
        r = requests.get(target_url, headers=headers, stream=True, timeout=15, verify=False)
        r.raise_for_status()
        
        def generate():
            for chunk in r.iter_content(chunk_size=128 * 1024):
                if chunk:
                    yield chunk
                    
        response_headers = {
            'Access-Control-Allow-Origin': '*',
            'Content-Type': r.headers.get('Content-Type', 'video/MP2T')
        }
        
        if 'Content-Length' in r.headers:
            response_headers['Content-Length'] = r.headers['Content-Length']
            
        return Response(stream_with_context(generate()), headers=response_headers)
    except Exception as e:
        logger.error(f"Error proxying segment {target_url}: {e}")
        return f"Segment proxying error: {e}", 502

# ==============================================================================
# Web Application Routing
# ==============================================================================

@app.route('/')
def index():
    """Dashboard homepage listing all channels, categories and dynamic banner."""
    with data_lock:
        channels = channels_list
        categories = categories_list
        all_channels_by_id = channels_by_id
        
    # Find all World Cup channels
    world_cup_channels = [ch for ch in channels if any("world_cup" in g.lower() or "world cup" in g.lower() for g in ch["groups"])]
    
    # Load dynamic banner configuration
    banner_config = load_banner_config()
    primary_banner_ch = all_channels_by_id.get(banner_config.get("primary_channel_id", ""))
    
    featured = []
    # If a primary channel is configured in admin panel, set it as the featured header channel
    if primary_banner_ch:
        featured = [primary_banner_ch]
    elif world_cup_channels:
        featured = [world_cup_channels[0]]
    else:
        # Fallback to first sports/racing channel
        featured = [ch for ch in channels if any(g in ["Sports Channels", "Racing", "Cricket"] for g in ch["groups"])][:1]
        if not featured:
            featured = channels[:1]
            
    return render_template('index.html', channels=channels, categories=categories, featured=featured, banner_config=banner_config)

@app.route('/category/<name>')
def category_page(name):
    """View channels within a specific category."""
    with data_lock:
        all_categories = categories_list
        filtered_channels = [ch for ch in channels_list if name.lower() in [g.lower() for g in ch["groups"]]]
        
    return render_template('index.html', channels=filtered_channels, categories=all_categories, active_category=name, featured=[])

@app.route('/play/<channel_id>')
def player_page(channel_id):
    """Render the stream player page with a custom video player and server selector."""
    server_idx = request.args.get('server', 0, type=int)
    ref = request.args.get('ref')
    
    with data_lock:
        ch = channels_by_id.get(channel_id)
        all_channels = channels_list
        all_channels_by_id = channels_by_id
        
    if not ch:
        return "Channel not found", 404
        
    if server_idx < 0 or server_idx >= len(ch["servers"]):
        server_idx = 0
        
    active_server = ch["servers"][server_idx]
    
    banner_config = load_banner_config()
    recommendations = []
    
    # Check if this is a World Cup watch session (ref == 'worldcup' or channel is in World Cup category)
    is_wc_session = ref == 'worldcup' or any("world_cup" in g.lower() or "world cup" in g.lower() for g in ch["groups"])
    
    if is_wc_session:
        # Display ONLY the other World Cup channels
        recommendations = [c for c in all_channels if any("world_cup" in g.lower() or "world cup" in g.lower() for g in c["groups"]) and c["id"] != ch["id"]]
    
    # If not a World Cup session, fallback to regular banner recommendation override or category recommendations
    if not recommendations:
        if ref == 'banner' or channel_id == banner_config.get("primary_channel_id"):
            rec_ids = banner_config.get("recommended_channel_ids", [])
            for r_id in rec_ids:
                rec_ch = all_channels_by_id.get(r_id)
                if rec_ch and rec_ch["id"] != ch["id"]:
                    recommendations.append(rec_ch)
    
    # Fallback to category recommendations
    if not recommendations:
        for c in all_channels:
            if c["id"] == ch["id"]:
                continue
            if set(c["groups"]) & set(ch["groups"]):
                recommendations.append(c)
                
    recommendations = recommendations[:15]
    if not recommendations:
        recommendations = [c for c in all_channels if c["id"] != ch["id"]][:15]
        
    stream_url = active_server["url"]
    stream_url_lower = stream_url.lower()
    is_proxied = False
    
    headers = active_server["headers"]
    if (headers or 
        "toffeelive" in stream_url_lower or 
        "sm-monirul" in stream_url_lower or 
        "fancode" in stream_url_lower or 
        "rockstreamer" in stream_url_lower or 
        "gpcdn.net" in stream_url_lower or 
        "aynascope" in stream_url_lower):
        
        is_proxied = True
        headers = auto_inject_headers(stream_url, headers)
        headers_encoded = json.dumps(headers)
        if '.mpd' in stream_url_lower:
            stream_url = f"/proxy/ts?url={urllib.parse.quote_plus(stream_url)}&headers={urllib.parse.quote_plus(headers_encoded)}"
        else:
            stream_url = f"/proxy/m3u8?url={urllib.parse.quote_plus(stream_url)}&headers={urllib.parse.quote_plus(headers_encoded)}"
            
    return render_template('player.html', channel=ch, stream_url=stream_url, 
                           recommendations=recommendations, is_proxied=is_proxied, 
                           current_server_index=server_idx)

# ==============================================================================
# Admin Panel & Live Analytics Routes
# ==============================================================================

@app.route('/admin')
def admin_panel():
    """Render the administrator control panel with live session diagnostics."""
    banner_config = load_banner_config()
    pin = banner_config.get("admin_pin", "").strip()
    
    # If no PIN is configured, redirect to setup
    if not pin:
        return redirect(url_for('admin_setup_pin'))
        
    # If not logged in, redirect to login page
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin_login'))
        
    now = time.time()
    
    with data_lock:
        # Clear sessions inactive for > 2 minutes (120s)
        stale_keys = [k for k, v in active_sessions.items() if now - v["last_active"] > 120]
        for k in stale_keys:
            active_sessions.pop(k, None)
            
        online_sessions = list(active_sessions.values())
        channels = channels_list
        
    active_count = len(online_sessions)
    
    # Count device distributions
    device_counts = {"Desktop": 0, "Mobile": 0, "Tablet": 0, "Smart TV": 0, "Media Player": 0, "Unknown": 0}
    for session_info in online_sessions:
        dev = session_info["device"]
        device_counts[dev] = device_counts.get(dev, 0) + 1
        
    return render_template('admin.html', 
                           active_count=active_count, 
                           device_counts=device_counts, 
                           banner_config=banner_config, 
                           channels=channels,
                           online_sessions=online_sessions)

@app.route('/admin/setup-pin', methods=['GET', 'POST'])
def admin_setup_pin():
    """First-time admin setup to configure the access PIN."""
    banner_config = load_banner_config()
    pin = banner_config.get("admin_pin", "").strip()
    
    # If a PIN is already configured, redirect to login
    if pin:
        return redirect(url_for('admin_login'))
        
    error = None
    if request.method == 'POST':
        new_pin = request.form.get("pin", "").strip()
        confirm_pin = request.form.get("confirm_pin", "").strip()
        
        if not new_pin:
            error = "PIN cannot be empty."
        elif new_pin != confirm_pin:
            error = "PIN codes do not match."
        else:
            banner_config["admin_pin"] = new_pin
            save_banner_config(banner_config)
            session['admin_logged_in'] = True
            return redirect(url_for('admin_panel'))
            
    return render_template('admin_login.html', is_setup=True, error=error)

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    """Login portal using the administrator PIN."""
    banner_config = load_banner_config()
    pin = banner_config.get("admin_pin", "").strip()
    
    # If no PIN is configured, redirect to setup
    if not pin:
        return redirect(url_for('admin_setup_pin'))
        
    # If already authenticated, redirect to dashboard
    if session.get('admin_logged_in'):
        return redirect(url_for('admin_panel'))
        
    error = None
    if request.method == 'POST':
        entered_pin = request.form.get("pin", "").strip()
        if entered_pin == pin:
            session['admin_logged_in'] = True
            return redirect(url_for('admin_panel'))
        else:
            error = "Incorrect PIN code. Please try again."
            
    return render_template('admin_login.html', is_setup=False, error=error)

@app.route('/admin/logout')
def admin_logout():
    """Logout administrative session."""
    session.pop('admin_logged_in', None)
    return redirect(url_for('index'))

@app.route('/admin/save-banner', methods=['POST'])
def save_banner():
    """Save the banner configuration parameters submitted from the admin panel."""
    existing_config = load_banner_config()
    
    primary_channel_id = request.form.get("primary_channel_id", "")
    recommended_channel_ids = request.form.getlist("recommended_channel_ids")
    
    ad_popunder = request.form.get("ad_popunder", "").strip()
    ad_socialbar = request.form.get("ad_socialbar", "").strip()
    ad_leaderboard = request.form.get("ad_leaderboard", "").strip()
    ad_sidebar = request.form.get("ad_sidebar", "").strip()
    
    config = {
        "primary_channel_id": primary_channel_id,
        "recommended_channel_ids": recommended_channel_ids,
        "title": "FIFA World Cup 2026 Live",
        "description": "Stream all FIFA World Cup 2026 matches live and buffer-free in premium high-definition proxy quality.",
        "ad_popunder": ad_popunder,
        "ad_socialbar": ad_socialbar,
        "ad_leaderboard": ad_leaderboard,
        "ad_sidebar": ad_sidebar,
        "admin_pin": existing_config.get("admin_pin", "")
    }
    
    save_banner_config(config)
    return redirect(url_for('admin_panel'))

@app.route('/api/channels')
def api_channels():
    """API endpoint returning all loaded channels in JSON format."""
    with data_lock:
        channels = channels_list
    return jsonify(channels)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
