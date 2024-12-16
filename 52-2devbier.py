#!/usr/bin/env python3
import time
import threading
import board
import RPi.GPIO as GPIO
from RPLCD.i2c import CharLCD
import neopixel
from mfrc522 import SimpleMFRC522
import mysql.connector
from flask import Flask, render_template, request, redirect, url_for, jsonify, flash
from flask_login import LoginManager, login_required, current_user, login_user, UserMixin, logout_user
from forms import DeleteUserForm, ResetCountersForm
import logging
import os

app = app = Flask(__name__)

# Initialize Logging
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s',
                    handlers=[
                        logging.FileHandler("biercounter.log"),
                        logging.StreamHandler()
                    ])
logger = logging.getLogger(__name__)

# Initialize Login Manager
#login_manager = LoginManager()
#login_manager.init_app(app)
#login_manager.login_view = 'login'  # Route für den Login

# Initialize Flask
app = Flask(__name__)
app.config['SECRET_KEY'] = 'IhrSicheresSecretKey'

# Initialize GPIO
GPIO.setwarnings(False)
GPIO.setmode(GPIO.BCM)

# Initialize NeoPixels
LED_PIN = board.D12  # GPIO 12
NUM_LEDS = 35        # Number of LEDs in NeoPixel ring
pixels = neopixel.NeoPixel(LED_PIN, NUM_LEDS, brightness=0.5, auto_write=False)

# Initialize RFID reader
reader = SimpleMFRC522()

# Initialize LCD
I2C_ADDR = 0x27  # I2C address of LCD
I2C_PORT = 1     # I2C port
lcd = CharLCD(
    i2c_expander='PCF8574',
    address=I2C_ADDR,
    port=I2C_PORT,
    cols=20,
    rows=4,
    dotsize=8,
    charmap='A02',
    auto_linebreaks=True,
    backlight_enabled=True
)

# Initialize MySQL connection
try:
    db = mysql.connector.connect(
        host="localhost",
        user="biercounter_user",
        password="K61d61ARbJF3",  # Replace with your password or use environment variables
        database="biercounter_db"
    )
    db.autocommit = True  # Enable autocommit
    cursor = db.cursor(buffered=True)
    cursor.execute("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
    logger.info("Database connection established.")
except mysql.connector.Error as err:
    logger.error(f"Database connection error: {err}")
    lcd.clear()
    lcd.write_string("DB Fehler")
    time.sleep(5)
    GPIO.cleanup()
    exit(1)

# Benutzerklasse definieren
#class User:
#   def __init__(self, id, name, rfid):
#        self.id = id
#        self.name = name
#        self.rfid = rfid

# Define GPIO buttons (if any; per user description)
BUTTON_PINS = [4, 17, 27, 22]  # Example GPIO pins for buttons K1-K4
for pin in BUTTON_PINS:
    GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)

# Define states
IDLE = "idle"
PROCESSING = "processing"
ADD_USER_SCAN = "add_user_scan"

current_state = IDLE

# Synchronization variables
scan_request_event = threading.Event()
scan_complete_event = threading.Event()
scanned_rfid = None

# Display lock to prevent concurrent writes
display_lock = threading.Lock()

# Warning control
warning_active = False
warning_thread = None

# Function to display idle message
def display_idle_message():
    with display_lock:
        lcd.clear()
        lcd.cursor_pos = (0, 0)
        lcd.write_string("Biercounter V3.0")
        lcd.cursor_pos = (1, 0)
        lcd.write_string("bn electronics")
        lcd.cursor_pos = (2, 0)
        lcd.write_string("--------------------")
        lcd.cursor_pos = (3, 0)
        lcd.write_string("Bitte RFID scannen")
    logger.info("Idle message displayed.")

# Function to display arbitrary text
def display_text(line1='', line2='', line3='', line4=''):
    with display_lock:
        lcd.clear()
        if line1:
            lcd.cursor_pos = (0, 0)
            lcd.write_string(line1[:20])
        if line2:
            lcd.cursor_pos = (1, 0)
            lcd.write_string(line2[:20])
        if line3:
            lcd.cursor_pos = (2, 0)
            lcd.write_string(line3[:20])
        if line4:
            lcd.cursor_pos = (3, 0)
            lcd.write_string(line4[:20])
    logger.debug(f"Display updated: {line1}, {line2}, {line3}, {line4}")

# Function to display selection menu
def display_selection_menu(user_name):
    with display_lock:
        lcd.clear()
        lcd.cursor_pos = (0, 0)
        lcd.write_string(user_name[:20])
        lcd.cursor_pos = (1, 0)
        lcd.write_string("***Bitte waehlen***")
        lcd.cursor_pos = (2, 0)
        lcd.write_string("--------------------")
        lcd.cursor_pos = (3, 0)
        lcd.write_string("Bier|AfG|Shot|Kaff")
    logger.info("Selection menu displayed.")

# Function to display consumption
def display_consumption(user_id):
    try:
        sql = "SELECT category, COUNT(*) FROM consumptions WHERE user_id = %s GROUP BY category"
        cursor.execute(sql, (user_id,))
        results = cursor.fetchall()

        consumption = {'Bier': 0, 'AfG': 0, 'Shot': 0, 'Kaff': 0}
        for category, count in results:
            if category in consumption:
                consumption[category] = count

        with display_lock:
            lcd.clear()
            lcd.cursor_pos = (0, 0)
            lcd.write_string("Aktueller Verbrauch")
            lcd.cursor_pos = (1, 0)
            lcd.write_string(f"Bier: {consumption['Bier']}")
            lcd.cursor_pos = (2, 0)
            lcd.write_string(f"AfG: {consumption['AfG']}")
            lcd.cursor_pos = (3, 0)
            lcd.write_string(f"Shot:{consumption['Shot']} Kaff:{consumption['Kaff']}")
        logger.info("Consumption displayed.")

        # NeoPixel Ring anzeigen
        show_consumption_neopixels(consumption)
    except Exception as e:
        logger.error(f"Error displaying consumption: {e}")
        display_text("Fehler:", str(e))

# Funktion zur Anzeige der Verbrauchsdaten auf dem NeoPixel-Ring
def show_consumption_neopixels(consumption):
    try:
        # Beispiel: Bier - Blau, AfG - Grün, Shot - Gelb, Kaff - Orange
        colors = {
            'Bier': (0, 0, 255),    # Blau
            'AfG': (0, 255, 0),     # Grün
            'Shot': (255, 255, 0),  # Gelb
            'Kaff': (255, 165, 0)   # Orange
        }

        # Berechne die Anzahl der LEDs für jede Kategorie
        total = sum(consumption.values())
        if total == 0:
            pixels.fill((0, 0, 0))
            pixels.show()
            return

        leds_per_category = {
            'Bier': int((consumption['Bier'] / total) * NUM_LEDS),
            'AfG': int((consumption['AfG'] / total) * NUM_LEDS),
            'Shot': int((consumption['Shot'] / total) * NUM_LEDS),
            'Kaff': NUM_LEDS - (int((consumption['Bier'] / total) * NUM_LEDS) +
                                 int((consumption['AfG'] / total) * NUM_LEDS) +
                                 int((consumption['Shot'] / total) * NUM_LEDS))
        }

        # Farben auf dem NeoPixel-Ring anzeigen
        index = 0
        for category, count in leds_per_category.items():
            for _ in range(count):
                if index < NUM_LEDS:
                    pixels[index] = colors[category]
                    index += 1
        pixels.show()
        logger.debug("NeoPixel consumption display updated.")
    except Exception as e:
        logger.error(f"Error displaying consumption on NeoPixel ring: {e}")

# Function to save consumption
def save_consumption(user_id, selection):
    try:
        category_map = {1: 'Bier', 2: 'AfG', 3: 'Shot', 4: 'Kaff'}
        category = category_map.get(selection)
        if category:
            sql = "INSERT INTO consumptions (user_id, category) VALUES (%s, %s)"
            cursor.execute(sql, (user_id, category))
            db.commit()
            logger.info(f"Consumption saved: {category}")
    except mysql.connector.Error as err:
        logger.error(f"Database error saving consumption: {err}")
        display_text("DB Fehler:", str(err))

# Function to handle button selection
def wait_for_selection():
    selection = None
    start_time = time.time()
    warning_triggered = False

    # Wait until all buttons are released
    while any(GPIO.input(pin) == GPIO.HIGH for pin in BUTTON_PINS):
        logger.debug("Waiting for buttons to be released...")
        time.sleep(0.1)

    while True:
        current_time = time.time()
        elapsed_time = current_time - start_time

        # Check if a button is pressed
        for pin in BUTTON_PINS:
            if GPIO.input(pin) == GPIO.HIGH:
                selection = BUTTON_PINS.index(pin) + 1  # Assuming buttons 1-4
                logger.info(f"Button {selection} pressed.")
                if warning_active:
                    stop_warning()
                # Wait until button is released
                while GPIO.input(pin) == GPIO.HIGH:
                    time.sleep(0.1)
                return selection

        # After 5 seconds without selection, trigger warning
        if elapsed_time >= 5 and not warning_triggered:
            warning_triggered = True
            start_warning()
            logger.info("Warning triggered due to no selection.")

        # After 10 seconds total, stop and return None
        if elapsed_time >= 10:
            if warning_active:
                stop_warning()
            logger.info("No selection made within 10 seconds.")
            return None

        time.sleep(0.1)

# Functions for warning animation
def start_warning():
    global warning_active, warning_thread
    if not warning_active:
        warning_active = True
        if warning_thread is None or not warning_thread.is_alive():
            warning_thread = threading.Thread(target=warning_animation)
            warning_thread.daemon = True
            warning_thread.start()
            logger.info("Warning thread started.")
    pixels.fill((0, 0, 0))
    pixels.show()


def stop_warning():
    global warning_active
    warning_active = False
    logger.info("Warning stopped.")
    # Reset display
    display_idle_message()
    # Reset NeoPixels
    pixels.fill((0, 0, 0))
    pixels.show()

def warning_animation():
    logger.info("Warning animation started.")
    display_blink = False  # Zustand zur Steuerung des Blinkens
    while warning_active:
        # NeoPixel-Ring blinkt rot
        for i in range(0, NUM_LEDS, 2):
            pixels[i] = (255, 0, 0)  # Rot
        pixels.show()

        # Blinken der zweiten Zeile
        with display_lock:
            lcd.cursor_pos = (1, 0)
            if display_blink:
                lcd.write_string("***Bitte waehlen***")
            else:
                lcd.write_string("                    ")  # Leere Zeichenfolge zum Löschen
        display_blink = not display_blink  # Zustand umschalten

        time.sleep(0.3)  # Blinktakt (0,3 Sekunden an/aus)

        # NeoPixel-Ring ausschalten
        pixels.fill((0, 0, 0))
        pixels.show()

        # Blinken der dritten Zeile
        with display_lock:
            lcd.cursor_pos = (1, 0)
            if display_blink:
                lcd.write_string("***Bitte waehlen***")
            else:
                lcd.write_string("                    ")  # Leere Zeichenfolge zum Löschen
        display_blink = not display_blink  # Zustand umschalten

        time.sleep(0.3)  # Blinktakt (0,3 Sekunden an/aus)
    logger.info("Warning animation ended.")

# Function to handle successful selection
def handle_success_flow(user_id):
    # Green symmetric build animation
    green_symmetric_build_animation()
    # Delay 2 seconds
    time.sleep(2)
    # Return to idle
    display_idle_message()
    set_state_idle()
    logger.info("Returned to idle after selection.")

def green_symmetric_build_animation():
    logger.info("Green symmetric build animation started.")
    pixels.fill((0, 0, 0))
    pixels.show()

    num_steps = NUM_LEDS // 2
    total_time = 1.0  # seconds
    delay = total_time / num_steps

    for step in range(num_steps + 1):
        left_index = (step) % NUM_LEDS
        right_index = (NUM_LEDS - step -1) % NUM_LEDS
        pixels[left_index] = (0, 255, 0)  # Green
        pixels[right_index] = (0, 255, 0)
        pixels.show()
        time.sleep(delay)

    pixels.fill((0, 255, 0))
    pixels.show()

    time.sleep(0.2)

    num_steps = NUM_LEDS // 2
    total_time = 1.0  # seconds
    delay = total_time / num_steps

    for step in range(num_steps + 1):
        left_index = (step) % NUM_LEDS
        right_index = (NUM_LEDS - step -1) % NUM_LEDS
        pixels[left_index] = (0, 0, 0)  #Off
        pixels[right_index] = (0, 0, 0)
        pixels.show()
        time.sleep(delay)

   # pixels.fill((0, 0, 0))
    pixels.show()
    logger.info("Green symmetric build animation completed.")

# Function to handle unknown RFID
def unknown_rfid_animation(rfid_id):
    logger.info(f"Unknown RFID detected: {rfid_id}. Starting animation.")
    for _ in range(2):  # Wiederholen Sie die Animation 4 Mal
        for offset in range(NUM_LEDS):
            pixels.fill((0, 0, 0))
            for group in range(4):
                index = (offset + group * (NUM_LEDS // 4)) % NUM_LEDS
                for i in range(3):
                    pixel_index = (index + i) % NUM_LEDS
                    pixels[pixel_index] = (255, 0, 0)  # Rot
            pixels.show()
            time.sleep(0.05)
        pixels.fill((0, 0, 0))
        pixels.show()
       # time.sleep(0.05)
    logger.info("Unknown RFID animation completed.")

# Function to handle RFID detection
def on_rfid_detected(rfid_id):
    global current_state
    try:
        user_rfid = str(rfid_id)
        logger.info(f"RFID ID read: {user_rfid}")
        sql = "SELECT id, name FROM users WHERE rfid = %s"
        cursor.execute(sql, (user_rfid,))
        result = cursor.fetchone()

        if result:
            if isinstance(result, tuple) and len(result) >= 2:
                user_db_id, user_name = result
                logger.info(f"User recognized: {user_name}")
                display_selection_menu(user_name)
                selection = wait_for_selection()
                if selection:
                    save_consumption(user_db_id, selection)
                    display_consumption(user_db_id)
                    # Start success flow in a new thread
                    success_thread = threading.Thread(target=handle_success_flow, args=(user_db_id,))
                    success_thread.start()
                else:
                    # No selection made
                    logger.info("No selection made. Returning to idle.")
                    display_idle_message()
                    set_state_idle()
            else:
                # Incomplete database entry
                logger.error("Incomplete database entry.")
                display_text("DB Fehler:", "Eintrag unvollst.")
                set_state_idle()
        else:
            # Unknown RFID
            display_text("RFID unbekannt")
            lcd.cursor_pos = (2, 0)
            lcd.write_string(f"{rfid_id}")
            unknown_rfid_animation(user_rfid)
            set_state_idle()
    except Exception as e:
        logger.error(f"Error processing RFID: {e}")
        display_text("Fehler:", str(e))
        set_state_idle()
    finally:
        if warning_active:
            stop_warning()
        logger.info("RFID detection completed.")

# Function to set state to IDLE
def set_state_idle():
    global current_state
    current_state = IDLE
    display_idle_message()
    logger.info("State set to IDLE.")

# Main loop function
def main_loop():
    global current_state, scanned_rfid
    while True:
        if current_state == IDLE:
            # Blue pulsate animation
            pixels.fill((0, 0, 128))  # Medium brightness blue
            pixels.show()
            time.sleep(0.05)

            # Check for RFID tag
            try:
                result = reader.read_id_no_block()
                logger.debug(f"RFID read result: {result} (Type: {type(result)})")
                if result is not None:
                    if isinstance(result, tuple):
                        if len(result) >=1:
                            rfid = result[0]
                        else:
                            rfid = None
                    elif isinstance(result, int):
                        rfid = result
                    else:
                        rfid = None
                    if rfid:
                        logger.info(f"RFID Tag detected: {rfid}")
                        current_state = PROCESSING
                        on_rfid_detected(rfid)
            except Exception as e:
                logger.error(f"Error in RFID reading: {e}")

        elif current_state == PROCESSING:
            # Processing state: do nothing, wait for RFID detection to complete
            pass

        elif current_state == ADD_USER_SCAN:
            # Handle scan requested via web
            try:
                display_text("Bitte RFID scannen", "", "", "")
                start_warning_scan()
                logger.info("Scan initiated by web.")

                # Read RFID
                id, text = reader.read()
                scanned_rfid = str(id)
                logger.info(f"RFID scanned: {scanned_rfid}")

                # Stop warning animation
                stop_warning_scan()

                # Reset to IDLE
                set_state_idle()
            except Exception as e:
                logger.error(f"Error during web-initiated scan: {e}")
                display_text("Fehler beim Scan", str(e), "", "")
                set_state_idle()

        else:
            # Undefined state, reset to IDLE
            current_state = IDLE
            display_idle_message()
            logger.warning("Undefined state. Reset to IDLE.")

        time.sleep(0.1)

def start_warning_scan():
    global warning_active, warning_thread
    if not warning_active:
        warning_active = True
        if warning_thread is None or not warning_thread.is_alive():
            warning_thread = threading.Thread(target=warning_scan_animation)
            warning_thread.daemon = True
            warning_thread.start()
            logger.info("Warning thread for scan started.")

def stop_warning_scan():
    global warning_active
    warning_active = False
    logger.info("Warning thread for scan stopped.")
    # Reset display
    display_idle_message()
    # Reset NeoPixels
    pixels.fill((0, 0, 0))
    pixels.show()

def warning_scan_animation():
    logger.info("Scan warning animation started.")
    while warning_active and current_state == ADD_USER_SCAN:
        # NeoPixel ring blinking in red, fast
        for i in range(0, NUM_LEDS, 2):
            pixels[i] = (255, 0, 0)  # Red
        pixels.show()
        time.sleep(0.3)
        pixels.fill((0, 0, 0))
        pixels.show()
        time.sleep(0.3)
    logger.info("Scan warning animation ended.")

# Flask routes
#@login_manager.user_loader
#def load_user(user_id):
#    try:
#        sql = "SELECT id, name, rfid FROM users WHERE id = %s"
#        cursor.execute(sql, (user_id,))
#        user = cursor.fetchone()
#        if user:
#            return User(id=user[0], name=user[1], rfid=user[2])
#        return None
#    except Exception as e:
#        logger.error(f"Error loading user with ID {user_id}: {e}")
#        return None


@app.route('/')
#@login_required
def index():
    try:
        sql = """
            SELECT users.name, users.rfid, 
                   COALESCE(SUM(CASE WHEN consumptions.category = 'Bier' THEN 1 ELSE 0 END), 0) AS Bier,
                   COALESCE(SUM(CASE WHEN consumptions.category = 'AfG' THEN 1 ELSE 0 END), 0) AS AfG,
                   COALESCE(SUM(CASE WHEN consumptions.category = 'Shot' THEN 1 ELSE 0 END), 0) AS Shot,
                   COALESCE(SUM(CASE WHEN consumptions.category = 'Kaff' THEN 1 ELSE 0 END), 0) AS Kaff
            FROM users
            LEFT JOIN consumptions ON users.id = consumptions.user_id
            GROUP BY users.id
        """
        cursor.execute(sql)
        users = cursor.fetchall()
        user_list = []
        for user in users:
            user_dict = {
                'name': user[0],
                'rfid': user[1],
                'Bier': user[2],
                'AfG': user[3],
                'Shot': user[4],
                'Kaff': user[5]
            }
            user_list.append(user_dict)
        logger.info(f"Fetched {len(user_list)} users.")
        delete_form = DeleteUserForm()
        reset_form = ResetCountersForm()
        logger.info(f"Fetched {len(user_list)} users.")
        return render_template('index.html', users=user_list, form=delete_form, reset_form=reset_form)
    except Exception as e:
        logger.error(f"Error fetching users: {e}")
        return "Error fetching users.", 500

@app.route('/manage_users')
#@login_required
def manage_users():
    try:
        # Beispielhafte SQL-Abfrage, um alle Benutzer abzurufen
        sql = "SELECT id, name, rfid FROM users"
        cursor.execute(sql)
        users = cursor.fetchall()
        user_list = []
        for user in users:
            user_dict = {
                'id': user[0],
                'name': user[1],
                'rfid': user[2]
            }
            user_list.append(user_dict)
        logger.info(f"Fetched {len(user_list)} users for management.")
        return render_template('manage_users.html', users=user_list)
    except Exception as e:
        logger.error(f"Error fetching users for management: {e}")
        flash('Fehler beim Abrufen der Benutzer zur Verwaltung.', 'danger')
        return redirect(url_for('index'))

@app.route('/delete_user/<int:user_id>', methods=['POST'])
#@login_required
def delete_user(user_id):
    form = DeleteUserForm()
    if form.validate_on_submit():
        try:
            sql = "DELETE FROM users WHERE id = %s"
            cursor.execute(sql, (user_id,))
            db.commit()
            flash('Benutzer erfolgreich gelöscht.', 'success')
            logger.info(f"Deleted user with ID {user_id}.")
            return redirect(url_for('index'))
        except Exception as e:
            logger.error(f"Error deleting user with ID {user_id}: {e}")
            flash('Fehler beim Löschen des Benutzers.', 'danger')
            return redirect(url_for('index'))
    else:
        flash('Ungültige Anfrage.', 'danger')
        return redirect(url_for('index'))

@app.route('/reset_counters/<int:user_id>', methods=['POST'])
#@login_required
def reset_counters(user_id):
    form = ResetCountersForm()
    if form.validate_on_submit():
        try:
            # Angenommen, die Zähler sind in der Tabelle 'consumptions' gespeichert
            # Setzen Sie die Zähler zurück, indem Sie die Einträge löschen oder die Zähler auf 0 setzen
            sql = "DELETE FROM consumptions WHERE user_id = %s"
            cursor.execute(sql, (user_id,))
            db.commit()
            flash('Zähler erfolgreich zurückgesetzt.', 'success')
            logger.info(f"Reset counters for user with ID {user_id}.")
            return redirect(url_for('index'))
        except Exception as e:
            logger.error(f"Error resetting counters for user with ID {user_id}: {e}")
            flash('Fehler beim Zurücksetzen der Zähler.', 'danger')
            return redirect(url_for('index'))
    else:
        flash('Ungültige Anfrage.', 'danger')
        return redirect(url_for('index'))

@app.route('/add_user', methods=['GET', 'POST'])
def add_user():
    if request.method == 'POST':
        name = request.form.get('name')
        rfid = request.form.get('rfid')
        if not name or not rfid:
            return render_template('add_user.html', error="Name und RFID müssen ausgefüllt sein.")
        try:
            # Add new user to database
            sql = "INSERT INTO users (name, rfid) VALUES (%s, %s)"
            cursor.execute(sql, (name, rfid))
            db.commit()
            logger.info(f"New user added: {name} with RFID {rfid}")
            return redirect(url_for('index'))
        except mysql.connector.Error as err:
            logger.error(f"Database error adding user: {err}")
            return render_template('add_user.html', error="Database error.")
    else:
        return render_template('add_user.html')

@app.route('/scan_rfid', methods=['POST'])
def scan_rfid():
    global scanned_rfid, current_state
    if current_state != IDLE:
        return jsonify({"status": "error", "message": "Das System ist beschäftigt."}), 400
    try:
        # Signal main loop to perform a scan
        current_state = ADD_USER_SCAN
        logger.info("RFID scan initiated via web.")
        # The main loop will handle the scan and set scanned_rfid
        return jsonify({"status": "success", "message": "Bitte neuen RFID Tag an den Scanner halten."})
    except Exception as e:
        logger.error(f"Error initiating scan: {e}")
        return jsonify({"status": "error", "message": "Fehler beim Initiieren des Scans."}), 500

@app.route('/get_scan_result', methods=['GET'])
def get_scan_result():
    global scanned_rfid
    if scanned_rfid:
        rfid = scanned_rfid
        scanned_rfid = None  # Reset after retrieval
        return jsonify({"status": "success", "rfid": rfid})
    else:
        return jsonify({"status": "pending", "rfid": None})

# Start Flask in a separate thread
def start_flask():
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)

flask_thread = threading.Thread(target=start_flask)
flask_thread.daemon = True
flask_thread.start()
logger.info("Flask server started.")

# Start the main loop
try:
    display_idle_message()
    main_loop()
except KeyboardInterrupt:
    GPIO.cleanup()
    pixels.fill((0, 0, 0))
    pixels.show()
    lcd.clear()
    logger.info("Program terminated by user.")
except Exception as e:
    logger.error(f"Unhandled exception: {e}")
    display_text("Programmfehler", "", "", "")
    time.sleep(5)
    lcd.clear()
    GPIO.cleanup()
