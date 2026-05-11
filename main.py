print("Initializing KeyFlow... (This may take a moment on first launch)")

import sys
from datetime import timedelta
import os
import time
import json
import pickle
import threading
import numpy as np
import re
from sentence_transformers import SentenceTransformer
from pynput import keyboard
from datetime import datetime, timedelta, timezone
import webview
import pystray
from PIL import Image
import logging
import random

# Suppress Hugging Face unauthenticated and "position_ids" unexpected warnings
logging.getLogger("transformers.modeling_utils").setLevel(logging.ERROR)
logging.getLogger("transformers").setLevel(logging.ERROR)
logging.getLogger("sentence_transformers").setLevel(logging.ERROR)
from huggingface_hub.utils import logging as hf_logging
hf_logging.set_verbosity_error()
os.environ["TOKENIZERS_PARALLELISM"] = "false"
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'  # Silences TF logs
os.environ['TRANSFORMERS_VERBOSITY'] = 'error' # Silences HF logs
os.environ['KMP_WARNINGS'] = '0' # Silences Intel math library logs

# Force local-only mode for Hugging Face and Transformers (ensures no "phone home" checks)
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"

def resource_path(relative_path):
    """ Get absolute path to resource, works for dev and for PyInstaller """
    try:
        # PyInstaller creates a temp folder and stores path in _MEIPASS
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)

# OAuth & Google API
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from google.auth.transport.requests import Request

MODEL_ID = "gemini-3.1-flash-lite-preview"

APP_NAME = "KeyFlow"
if sys.platform == "win32":
    # Ensures the path is explicitly within the user's Roaming AppData folder
    appdata = os.environ.get('APPDATA') or os.path.expandvars(r'%USERPROFILE%\AppData\Roaming')
    DATA_DIR = os.path.join(appdata, APP_NAME)
else:
    DATA_DIR = os.path.join(os.path.expanduser('~'), '.local', 'share', APP_NAME)

os.makedirs(DATA_DIR, exist_ok=True)

DB_FILE = os.path.join(DATA_DIR, "music_db.json")
TOKEN_FILE = os.path.join(DATA_DIR, "token.pickle")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")

SCOPES = ['https://www.googleapis.com/auth/youtube.readonly']

DEFAULT_CONFIG = {
    "years": 15,
    "interval_min": 10
}

class KeyFlowState:
    def __init__(self):
        self._lock = threading.RLock()
        self.music_metadata_cache = []
        self.active_songs = []
        self.current_candidates = []
        self.candidate_index = 0
        self.window = None
        self.settings_window = None
        self.window_ready = False
        self.current_playing_video_id = None
        self.max_candidates = 100
        self.num_songs = 10000
        self.buffer_max_length = 120
        self.text_buffer = ""
        self.pressed_keys = set()
        self.sync_in_progress = False
        self.last_key_time = time.time()
        self.config = DEFAULT_CONFIG.copy()
        self._load_config()
        self._load_metadata()
        self.fill_candidates()

    def _load_config(self):
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r') as f:
                    self.config.update(json.load(f))
            except Exception: pass
        else:
            self.save_config()

    def _load_metadata(self):
        if os.path.exists(DB_FILE):
            try:
                with open(DB_FILE, 'r') as f:
                    data = json.load(f)
                with self._lock:
                    self.music_metadata_cache = data
                    self.filter_songs()
                return True
            except Exception: pass
        return False

    def save_config(self):
        with self._lock:
            with open(CONFIG_FILE, 'w') as f:
                json.dump(self.config, f)

    def set_sync_status(self, status):
        with self._lock:
            self.sync_in_progress = status

    def get_sync_status(self):
        with self._lock:
            return self.sync_in_progress

    def get_active_songs(self):
        with self._lock:
            return list(self.active_songs)

    def get_metadata_cache(self):
        with self._lock:
            return list(self.music_metadata_cache)

    def get_config(self, key):
        with self._lock:
            return self.config.get(key)

    def fill_candidates(self):
        with self._lock:
            if self.active_songs:
                self.current_candidates = random.sample(self.active_songs, min(self.max_candidates, len(self.active_songs)))
                self.candidate_index = 0
                print(f"Filled candidates with {len(self.current_candidates)} random songs.")

    def save_metadata(self, data):
        with self._lock:
            self.music_metadata_cache = data
            with open(DB_FILE, 'w') as f:
                json.dump(data, f)
            self.filter_songs()

    def filter_songs(self):
        with self._lock:
            if not self.music_metadata_cache:
                self.active_songs = []
                return
            years_ago = datetime.now(timezone.utc) - timedelta(days=self.config["years"]*365)
            filtered = []
            if self.music_metadata_cache and 'publishedAt' not in self.music_metadata_cache[0]:
                print("Note: Local database is using an older format without dates. History filtering is disabled until your next sync.")
                self.active_songs = self.music_metadata_cache
                return
            for s in self.music_metadata_cache:
                try:
                    date_obj = datetime.strptime(s['publishedAt'], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                    if date_obj >= years_ago: filtered.append(s)
                except Exception: filtered.append(s)
            if not filtered and self.music_metadata_cache:
                self.active_songs = self.music_metadata_cache
            else:
                self.active_songs = filtered
            print(f"Active library updated: {len(self.active_songs)} songs available.")

    def update_buffer(self, key):
        """Updates the text buffer with a sliding window approach (max length)."""
        with self._lock:
            self.last_key_time = time.time()
            char_to_add = ""
            if hasattr(key, 'char') and key.char is not None and ord(key.char) >= 32:
                char_to_add = key.char
            elif key == keyboard.Key.space: char_to_add = " "
            elif key == keyboard.Key.enter: char_to_add = "\n"
            elif key == keyboard.Key.backspace:
                if len(self.text_buffer) > 0: self.text_buffer = self.text_buffer[:-1]
                return self.text_buffer
            if char_to_add:
                self.text_buffer += char_to_add
                # Sliding window: keep only the most recent characters up to buffer_max_length
                max_len = self.buffer_max_length
                if len(self.text_buffer) > max_len:
                    self.text_buffer = self.text_buffer[-max_len:]
                
                # Filter out password-like words from the buffer
                self._filter_passwords_from_buffer()
            return self.text_buffer

    def get_buffer(self):
        """Safely returns the current text buffer."""
        with self._lock:
            return self.text_buffer

    def clear_buffer(self):
        """Safely clears the text buffer."""
        with self._lock:
            self.text_buffer = ""

    def delete_last_word(self):
        """Deletes the last word from the text buffer (CTRL+BACKSPACE behavior)."""
        with self._lock:
            text = self.text_buffer.rstrip(' ')
            idx = max(text.rfind(' '), text.rfind('\n'))
            self.text_buffer = text[:idx + 1] if idx >= 0 else ""
            return self.text_buffer

    def _is_password_like(self, word):
        """Check if a word looks like a password or code snippet."""
        if not word or len(word) < 8:
            return False
        has_upper = any(c.isupper() for c in word)
        has_lower = any(c.islower() for c in word)
        has_number = any(c.isdigit() for c in word)
        has_special = any(c in '!@#$%^&*()_+-=[]{};\'"\\|,.<>\/?' for c in word)
        type_count = sum([has_upper, has_lower, has_number, has_special])
        return type_count >= 4

    def _clean_pasted_text(self, text):
        """Remove password-like words from pasted text."""
        words = text.split()
        cleaned_words = [w for w in words if not self._is_password_like(w)]
        return ' '.join(cleaned_words)

    def paste_to_buffer(self, pasted_text):
        """Pastes text into the buffer, filtering passwords and respecting max length."""
        with self._lock:
            cleaned = self._clean_pasted_text(pasted_text)
            # Add cleaned text to buffer, respecting max length
            available_space = self.buffer_max_length - len(self.text_buffer)
            if available_space > 0:
                to_add = cleaned[:available_space]
                self.text_buffer += to_add
            return self.text_buffer

    def _filter_passwords_from_buffer(self):
        """Remove password-like words from the current buffer while preserving whitespace."""
        parts = re.split(r'(\s+)', self.text_buffer)
        filtered_parts = []
        for part in parts:
            if part.strip():  # If it's a word (not whitespace)
                if not self._is_password_like(part):
                    filtered_parts.append(part)
            else:  # It's whitespace, keep it
                filtered_parts.append(part)
        self.text_buffer = ''.join(filtered_parts)
        # Ensure buffer doesn't exceed max length after filtering
        if len(self.text_buffer) > self.buffer_max_length:
            self.text_buffer = self.text_buffer[-self.buffer_max_length:]

    def consume_buffer(self):
        """Returns the current buffer and clears it atomically."""
        with self._lock:
            snapshot = self.text_buffer
            self.text_buffer = ""
            return snapshot

    def set_candidates(self, matches):
        with self._lock:
            self.current_candidates = matches
            self.candidate_index = 0
            return self.current_candidates[0] if self.current_candidates else None

    def get_next_candidate(self):
        with self._lock:
            self.candidate_index += 1
            if self.current_candidates and self.candidate_index < len(self.current_candidates):
                return self.current_candidates[self.candidate_index], self.candidate_index, len(self.current_candidates)
            return None, 0, 0

    def remove_song(self, video_id):
        with self._lock:
            original = len(self.music_metadata_cache)
            self.music_metadata_cache = [s for s in self.music_metadata_cache if s['id'] != video_id]
            if len(self.music_metadata_cache) < original:
                with open(DB_FILE, 'w') as f:
                    json.dump(self.music_metadata_cache, f)
                self.filter_songs()
                return True
            return False

    def update_settings(self, years, interval_min):
        with self._lock:
            self.config['years'] = years
            self.config['interval_min'] = interval_min
            self.save_config()
            self.filter_songs()

    def check_and_prepare_song_update(self, video_id):
        with self._lock:
            if video_id == self.current_playing_video_id:
                next_match, idx, total = self.get_next_candidate()
                if next_match:
                    print(f"Song {video_id} is already playing. Skipping to next candidate...")
                    print(f"Skipping duplicate, trying candidate {idx+1}/{total}: {next_match['title']}")
                    return next_match['id'], True  # True means skip, so call again
                else:
                    print("No more unique candidates left in current search results.")
                    return None, False
            else:
                self.current_playing_video_id = video_id
                return video_id, False

state = KeyFlowState()

print("Loading embedding model...")
local_model = SentenceTransformer('all-MiniLM-L6-v2')
INJECTED_JS = """
(function() {
    const UNLIKE_SVG = "M8.041 1.635a2.447 2.447 0 011.763 3.047l-.53 1.858";

    const setupBufferView = () => {
        if (document.getElementById('buffer-view')) return;
        const div = document.createElement('div');
        div.id = 'buffer-view';
        Object.assign(div.style, {
            position: 'fixed', bottom: '70px', left: '10px', right: '10px',
            height: '70px', backgroundColor: 'rgba(10, 10, 10, 0.7)',
            color: '#00ff00', zIndex: '2147483647', padding: '8px 15px',
            fontFamily: 'monospace', borderRadius: '8px', border: '1px solid #444',
            overflowY: 'auto', cursor: 'default', whiteSpace: 'pre-wrap', pointerEvents: 'auto'
        });
        div.innerText = '> KeyFlow Active (Hover to capture keys) \\n\\t[CTRL]+[SHIFT]+M to trigger\\n\\t[CTRL]+[BACKSPACE] delete word\\n\\t[CTRL]+[DELETE] clear\\n\\tOR paste text';

        div.addEventListener('mouseenter', () => {
            window._kf_active = true;
            div.style.borderColor = '#00ff00';
        });
        div.addEventListener('mouseleave', () => {
            window._kf_active = false;
            div.style.borderColor = '#444';
        });
        
        div.addEventListener('paste', (e) => {
            e.preventDefault();
            const pasted = e.clipboardData.getData('text/plain');
            if (pasted && window.pywebview?.api?.paste_to_buffer) {
                window.pywebview.api.paste_to_buffer(pasted);
            }
        });

        document.body.appendChild(div);
    };

    // --- 2. LOGIC & OBSERVERS ---
    const runLogic = () => {
        setupBufferView();

        // Handle Ad Muting
        const v = document.querySelector('video');
        const ad = document.querySelector('.ad-showing, .ad-interrupting');
        if (ad && v) { v.muted = false; v.volume = 0.02; }
        else if (v && v.muted) { v.muted = false; v.playbackRate = 1.0; }

        // Handle Unavailable Songs
        document.querySelectorAll('ytmusic-notification-action-renderer').forEach(msg => {
            if (!msg.hasAttribute('data-kf-c') && msg.querySelector('#sub-text')?.textContent.trim()) {
                msg.setAttribute('data-kf-c', '1');
                const id = new URLSearchParams(location.search).get('v');
                if (id && window.pywebview?.api?.handle_unavailable_song) {
                    window.pywebview.api.handle_unavailable_song(id);
                }
            }
        });

        // Handle Unlike Logging
        document.querySelectorAll('ytmusic-toggle-menu-service-item-renderer').forEach(item => {
            const p = item.querySelector('path');
            const d = p ? p.getAttribute('d') : "";
            const t = item.textContent || "";
            if (!item.hasAttribute('data-kf-h') && (d.includes(UNLIKE_SVG) || t.includes("Remove from Liked songs"))) {
                item.setAttribute('data-kf-h', '1');
                item.addEventListener('click', () => {
                    const id = new URLSearchParams(location.search).get('v');
                    const titleEl = document.querySelector('ytmusic-player-bar .title');
                    if (id && window.pywebview?.api?.log_unlike) {
                        window.pywebview.api.log_unlike(id, titleEl ? titleEl.textContent.trim() : "Unknown");
                    }
                });
            }
        });
    };

    if (!window._kf_initialized) {
        window._kf_initialized = true;
        window.addEventListener('keydown', (e) => {
            if (window._kf_active) {
                e.stopPropagation();
                if(e.code === "Space") e.preventDefault();
            }
        }, true);
        window.addEventListener('play', () => runLogic(), true);
        new MutationObserver(runLogic).observe(document.body, { 
            childList: true, subtree: true, attributes: true 
        });
    }

    runLogic();
})();
"""

# --- AUTH & SYNC LOGIC ---

def get_yt_service():
    creds = None
    
    # 1. Use the absolute path for the token
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, 'rb') as token:
            creds = pickle.load(token)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            # 2. client_secret.json should be in the application root (where the .exe or .py lives)
            app_root = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.dirname(os.path.abspath(__file__))
            secret_path = os.path.join(app_root, "client_secret.json")

            if not os.path.exists(secret_path):
                print("\n" + "!"*60)
                print("MISSING CONFIGURATION: 'client_secret.json' not found.")
                print(f"Searched in: {app_root}")
                print("\nTo use KeyFlow, you must provide your own Google OAuth secrets:")
                print("1. Visit the Google Cloud Console (https://console.cloud.google.com/)")
                print("2. Create a project and enable the 'YouTube Data API v3'.")
                print("3. Go to 'Credentials' -> 'Create Credentials' -> 'OAuth client ID'.")
                print("4. Choose 'Desktop app', name it 'KeyFlow', and create.")
                print("5. Download the JSON, rename it to 'client_secret.json', and place")
                print(f"   it in the root folder: {app_root}")
                print("\nSECURITY: Never share 'client_secret.json' as it identifies your app.")
                print("!"*60 + "\n")
                input("Press Enter to exit...")
                sys.exit(1)

            print("\nAuthorization required. A browser tab will open for Google Sign-In.")
            print("   This allows KeyFlow to read your 'Liked' songs to create your local library.")
            print(f"   The access token will be saved to: {DATA_DIR}")
            print("   IMPORTANT: Do not share 'token.pickle'. It grants access to your account.\n")

            flow = InstalledAppFlow.from_client_secrets_file(secret_path, SCOPES)
            creds = flow.run_local_server(port=0)

        # 3. Save the token back to the writable BASE_DIR
        with open(TOKEN_FILE, 'wb') as token:
            pickle.dump(creds, token)

    return build('youtube', 'v3', credentials=creds)

def sync_library_if_needed():
    if state.get_sync_status(): return
    state.set_sync_status(True)

    # Load existing songs to check for duplicates and avoid re-embedding
    existing_songs = state.get_metadata_cache()

    try:
        existing_ids = {s['id'] for s in existing_songs}
        youtube = get_yt_service()
        new_songs = []
        next_page_token = None
        stop_loop = False
        
        # --- 1. FETCHING WITH FILTERS ---
        print(f"Fetching liked songs... \
              This may take a while if you have a large library or it's your first sync.")
        
        num_songs_limit = state.num_songs
        while not stop_loop and (len(new_songs) + len(existing_songs)) < state.num_songs:
            request = youtube.playlistItems().list(
                playlistId="LL",
                part="snippet,contentDetails",
                maxResults=50,
                pageToken=next_page_token
            )
            response = request.execute()

            batch_ids = []
            liked_dates = {}

            for item in response.get('items', []):
                vid_id = item['contentDetails']['videoId']
                liked_dates[vid_id] = item['snippet']['publishedAt']
                batch_ids.append(vid_id)

            if batch_ids:
                # Get Category and Duration for all 50 videos at once
                v_request = youtube.videos().list(
                    part="snippet,contentDetails",
                    id=",".join(batch_ids)
                )
                v_response = v_request.execute()

                for video in v_response.get('items', []):
                    # 1. Check if Category is Music
                    ALLOWED_CATEGORIES = ["10", "1", "24", "20"] # Music, Film, Entertainment, Gaming
                    is_music = video['snippet'].get('categoryId') in ALLOWED_CATEGORIES

                    # 2. Parse Duration (ISO 8601 format like 'PT3M45S')
                    dur = video['contentDetails']['duration']
                    if 'H' not in dur:
                        mins = int(dur.split('M')[0].split('T')[-1]) if 'M' in dur else 0
                        secs = int(dur.split('S')[0].split('M' if 'M' in dur else 'T')[-1]) if 'S' in dur else 0
                        total_sec_approx = mins * 60 + secs

                        # Feel free to adjust these duration filters as needed. 
                        if is_music and 60 <= total_sec_approx <= 600: 
                            if video['id'] not in existing_ids:
                                new_songs.append({
                                    "title": video['snippet']['title'],
                                    "id": video['id'],
                                    "duration": total_sec_approx,
                                    "publishedAt": liked_dates[video['id']]
                                })
                            else: 
                                # If we hit an existing song, we can assume we've caught up to the last sync point
                                stop_loop = True
                                break

            if not stop_loop:
                next_page_token = response.get('nextPageToken')
                if not next_page_token: break

        # --- 2. LOCAL EMBEDDING (No Quota!) ---
        if new_songs:
            print(f"Generating embeddings for {len(new_songs)} new tracks...")
            titles = [s['title'] for s in new_songs]
            
            vectors = local_model.encode(titles, show_progress_bar=True)

            for i, vec in enumerate(vectors):
                new_songs[i]['vector'] = vec.tolist()

            # Combine: New songs first, then existing. Limit to max songs config.
            final_songs = (new_songs + existing_songs)[:num_songs_limit]
            state.save_metadata(final_songs)
            print(f"Database updated! Added {len(new_songs)} new songs.")
        else:
            print("Music library is already up to date.")
    finally:
        state.set_sync_status(False)

# --- SEARCH & PLAYBACK ---

def find_best_matches(vibe_query):
    songs = state.get_active_songs()
    if not songs: return []
        
    vibe_vec = local_model.encode([vibe_query])[0]
    vibe_vec = np.array(vibe_vec)
    scored_songs = []
    for song in songs:
        song_vec = np.array(song['vector'])
        score = np.dot(vibe_vec, song_vec) / (np.linalg.norm(vibe_vec) * np.linalg.norm(song_vec))
        scored_songs.append((score, song))
    scored_songs.sort(key=lambda x: x[0], reverse=True)
    return [s[1] for s in scored_songs[:state.max_candidates]]

class PlayerAPI:
    def play_next_candidate(self):
        next_match, idx, total = state.get_next_candidate()
        if next_match:
            print(f"Match {idx + 1}/{total}: {next_match['title']}")
            threading.Thread(target=update_song, args=(next_match['id'],), daemon=True).start()
        else:
            print("No more candidates left for this vibe search.")

    def log_js_message(self, message):
        print(f"JS Console: {message}")

    def paste_to_buffer(self, pasted_text):
        """Handles pasted text from the clipboard."""
        state.paste_to_buffer(pasted_text)
        update_buffer_ui(state.get_buffer())
        print(f"Pasted text added to buffer (filtered for sensitive data).")

    def log_unlike(self, video_id, song_title):
        print(f"\nUnlike action detected in UI for video: {song_title}")
        if state.remove_song(video_id):
            print(f"Successfully removed song {song_title} from music_db.json.")
        else:
            print(f"Note: Song {song_title} was removed from Liked list, but it wasn't in your local library anyway.")
        
        print("Picking next candidate...")
        self.play_next_candidate()

    def handle_unavailable_song(self, video_id):
        print(f"\nSong unavailable detected: {video_id}. Removing from library...")
        state.remove_song(video_id)
        self.play_next_candidate()

class SettingsAPI:
    def save_settings(self, years, interval_min):
        state.update_settings(years, interval_min)
        print("Settings updated and saved.")
        if state.settings_window:
            state.settings_window.hide()

def on_settings_closing():
    if state.settings_window:
        state.settings_window.hide()
    return False  # Returning False prevents the window from being destroyed

def show_settings_window():
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <style>
            body {{ font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; padding: 20px; background: #121212; color: white; }}
            .control {{ margin-bottom: 25px; }}
            label {{ display: block; margin-bottom: 10px; color: #b3b3b3; }}
            input[type=range] {{ width: 100%; accent-color: #1db954; }}
            button {{ background: #1db954; color: white; border: none; padding: 10px 25px; border-radius: 20px; cursor: pointer; font-weight: bold; }}
            button:hover {{ background: #1ed760; }}
            .val {{ color: #1db954; font-weight: bold; }}
        </style>
    </head>
    <body>
        <h2 style="margin-top: 0;">KeyFlow Settings</h2>
        <div class="control">
            <label>History Depth: <span class="val" id="years_val">{state.get_config('years')}</span> years</label>
            <input type="range" id="years" min="1" max="15" value="{state.get_config('years')}" oninput="document.getElementById('years_val').innerText = this.value">
        </div>
        <div class="control">
            <label>Auto-Switch Interval: <span class="val" id="interval_val">{state.get_config('interval_min')}</span> min</label>
            <input type="range" id="interval" min="1" max="59" value="{state.get_config('interval_min')}" oninput="document.getElementById('interval_val').innerText = this.value">
        </div>
        <button onclick="save()">Save Settings</button>
        <script>
            function save() {{
                const years = document.getElementById('years').value;
                const interval = document.getElementById('interval').value;
                window.pywebview.api.save_settings(parseInt(years), parseInt(interval));
            }}
        </script>
    </body>
    </html>
    """

    if state.settings_window:
        state.settings_window.load_html(html)
        state.settings_window.show()
        return

    state.settings_window = webview.create_window('Keyflow Settings', html=html, js_api=SettingsAPI(), width=400, height=350, hidden=True)
    state.settings_window.events.closing += on_settings_closing

def start_player_init():
    # Close the splash screen if it's still open (PyInstaller only)
    try:
        import pyi_splash
        pyi_splash.close()
    except ImportError:
        pass

    # Create the window, loading YouTube Music directly as the main content
    state.window = webview.create_window(APP_NAME,
                                   'https://music.youtube.com/home',
                                   js_api=PlayerAPI())

    # Prepare settings window (it will stay hidden until Tray Icon calls it)
    show_settings_window()

    state.window.events.closing += lambda: os._exit(0)  

    # Start the engine (this BLOCKS the thread)
    state.window.events.loaded += on_page_finished
    webview.start(on_loaded, state.window, private_mode=False)

def on_loaded(window):
    state.window_ready = True
    window.evaluate_js(INJECTED_JS)

def on_page_finished(window):
    """This runs every time a new URL finishes loading."""
    window.evaluate_js(INJECTED_JS)

def update_song(video_id):
    if state.window and state.window_ready:
        target_id, should_skip = state.check_and_prepare_song_update(video_id)
        if target_id is None:
            return
        if should_skip:
            threading.Thread(target=update_song, args=(target_id,), daemon=True).start()
            return
        
        # This script finds the actual video element and toggles its state
        script = """
            try {
                var video = document.querySelector('video');
                if (video && !video.paused) {
                    video.pause();
                }
            } catch(e) {
                console.error("Cleanup/Pause failed", e);
            }
        """
        state.window.evaluate_js(script)

        # Tiny delay to let the browser engine process the JS state change before navigation
        time.sleep(0.1)

        new_url = f"https://music.youtube.com/watch?v={target_id}"
        state.window.load_url(new_url)

def process_and_play(captured_text):
    """Finds and plays a song based on captured text. Triggered manually by user."""
    text_to_process = captured_text.strip()

    if not text_to_process:
        songs = state.get_active_songs()
        if songs:
            match = random.choice(songs)
            print(f"\nBuffer empty. Picking a random song: {match['title']}")
            update_song(match['id'])
        return

    try:
        matches = find_best_matches(text_to_process)
        if matches:
            match = state.set_candidates(matches)
            if match:
                print(f"\nMatch 1/{len(matches)}: {match['title']}")
                update_song(match['id'])
    
    except Exception as e:
        print(f"\nError in matching: {e}")
        
def auto_process_loop():
    """Background thread that periodically triggers process_and_play."""
    while True:
        interval = state.get_config("interval_min")
        time.sleep(interval * 60)
        print(f"\n[AUTO] Periodic song selection triggered (every {interval} min)")
        process_and_play(state.consume_buffer())

# --- LISTENER ---

# The Function your Keyboard Listener calls
def update_buffer_ui(text):
    if state.window:
        # Use json.dumps to safely handle all JS string escaping (quotes, backslashes, etc.)
        safe_text = json.dumps("> " + text.replace("\n", " "))
        js = f"var el = document.getElementById('buffer-view'); if(el) el.innerText = {safe_text};"
        state.window.evaluate_js(js)

def on_press(key):
    try:
        state.pressed_keys.add(key)

        # Check for CTRL + SHIFT + M combination
        ctrl_pressed = any(k in state.pressed_keys for k in [keyboard.Key.ctrl_l, keyboard.Key.ctrl_r])
        shift_pressed = any(k in state.pressed_keys for k in [keyboard.Key.shift, keyboard.Key.shift_l, keyboard.Key.shift_r])
        
        m_pressed = False
        if hasattr(key, 'vk') and key.vk == 77:
            m_pressed = True

        if ctrl_pressed and shift_pressed and m_pressed:
            # Trigger the vibe search with whatever is in the buffer
            buffer_snapshot = state.consume_buffer()
            print(f"\n[TRIGGER] Manual song selection activated via CTRL+SHIFT+M")
            threading.Thread(target=process_and_play, args=(buffer_snapshot,), daemon=True).start()
            return # Don't add 'M' to the buffer when it's part of the trigger

        # Check for CTRL + BACKSPACE to delete last word
        if ctrl_pressed and key == keyboard.Key.backspace:
            result = state.delete_last_word()
            update_buffer_ui(result)
            return

        # Check for CTRL + DELETE to clear buffer
        if ctrl_pressed and key == keyboard.Key.delete:
            state.clear_buffer()
            update_buffer_ui("")
            print("Buffer cleared.")

        result = state.update_buffer(key)

        # Showing the buffer in the console (for debugging) and in the UI.
        display = result.replace('\n', '↵')
        if len(display) > 70: display = "..." + display[-67:]
        print(f"\rCurrent Buffer: {display}     ", end="", flush=True)

        update_buffer_ui(result)
        
    except Exception:
        pass

def on_release(key):
    try:
        if key in state.pressed_keys:
            state.pressed_keys.remove(key)
    except Exception:
        pass

def run_listener():
    print("Keyboard listener started. Use CTRL+SHIFT+M to pick a song matching your typing.")
    with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
        listener.join()

def create_tray_icon():
    image = Image.open(resource_path(os.path.join('media', 'KeyFlow Logo.png'))).convert('RGBA').resize((64, 64))

    def on_settings(icon, item):
        show_settings_window()

    def on_sync(icon, item):
        threading.Thread(target=sync_library_if_needed, daemon=True).start()

    def on_exit(icon, item):
        icon.stop()
        os._exit(0)

    menu = pystray.Menu(
        pystray.MenuItem("Settings", on_settings),
        pystray.MenuItem("Sync Library", on_sync),
        pystray.MenuItem("Exit", on_exit)
    )
    icon = pystray.Icon("KeyFlow", image, "KeyFlow", menu)
    icon.run()

def print_startup_guide():
    guide = f"""
{'='*60}
   KEYFLOW: Keystroke-Driven YouTube Music
{'='*60}

INTERACTIVE FEATURES:
- In the player window, unliking a song (clicking 'Remove from Liked' in ytmusic's own UI) will immediately remove it from your local KeyFlow database and queue up a better candidate.
- Not writing anything? Copying text to your clipboard can also trigger vibe detection.

SYSTEM TRAY:
- Look for the 'KF' icon in your system tray to: Adjust history depth (how many years back to pull music). Set the selection frequency.

PRIVACY:
- All processing is local. Your typing never leaves your machine.
- Library data and access tokens are stored in: {DATA_DIR}
- Passwords and code snippets are ignored by our heuristics.

SECURITY: Never share 'client_secret.json' or 'token.pickle' files!
{'='*60}
"""
    print(guide)

# --- 2. The "Main" Entry Point ---
if __name__ == "__main__":
    print_startup_guide()
    
    sync_library_if_needed()

    # START AUTO PROCESSOR
    threading.Thread(target=auto_process_loop, daemon=True).start()

    # START TRAY ICON
    threading.Thread(target=create_tray_icon, daemon=True).start()

    # START LISTENER IN BACKGROUND
    t = threading.Thread(target=run_listener, daemon=True)
    t.start()

    # START PLAYER ON MAIN THREAD
    start_player_init()

"""
TODO: 
User should be able to choose between Youtube, Spotify, and Apple Music. Currently only a Youtube pipeline is implemented.
"""
