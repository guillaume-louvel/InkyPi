import logging
import os
import socket
import subprocess

from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageOps

logger = logging.getLogger(__name__)

FONT_FAMILIES = {
    "Dogica": [{
        "font-weight": "normal",
        "file": "dogicapixel.ttf"
    },{
        "font-weight": "bold",
        "file": "dogicapixelbold.ttf"
    }],
    "Jost": [{
        "font-weight": "normal",
        "file": "Jost.ttf"
    },{
        "font-weight": "bold",
        "file": "Jost-SemiBold.ttf"
    }],
    "Napoli": [{
        "font-weight": "normal",
        "file": "Napoli.ttf"
    }],
    "DS-Digital": [{
        "font-weight": "normal",
        "file": os.path.join("DS-DIGI", "DS-DIGI.TTF")
    }]
}

FONTS = {
    "ds-gigi": "DS-DIGI.TTF",
    "napoli": "Napoli.ttf",
    "jost": "Jost.ttf",
    "jost-semibold": "Jost-SemiBold.ttf"
}

def resolve_path(file_path):
    src_dir = os.getenv("SRC_DIR")
    if src_dir is None:
        # Default to the src directory
        src_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    
    src_path = Path(src_dir)
    return str(src_path / file_path)

def get_ip_address():
    """Returns the LAN IP address, or None when there is no network."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return None

def get_wifi_name():
    try:
        output = subprocess.check_output(['iwgetid', '-r']).decode('utf-8').strip()
        return output
    except subprocess.CalledProcessError:
        return None

def is_connected():
    """Check if the Raspberry Pi has an internet connection."""
    try:
        # Try to connect to Google's public DNS server
        socket.create_connection(("8.8.8.8", 53), timeout=2)
        return True
    except OSError:
        return False

def get_font(font_name, font_size=50, font_weight="normal"):
    if font_name in FONT_FAMILIES:
        font_variants = FONT_FAMILIES[font_name]

        font_entry = next((entry for entry in font_variants if entry["font-weight"] == font_weight), None)
        if font_entry is None:
            font_entry = font_variants[0]  # Default to first available variant

        if font_entry:
            font_path = resolve_path(os.path.join("static", "fonts", font_entry["file"]))
            return ImageFont.truetype(font_path, font_size)
        else:
            logger.warning(f"Requested font weight not found: font_name={font_name}, font_weight={font_weight}")
    else:
        logger.warning(f"Requested font not found: font_name={font_name}")

    return None

def get_fonts():
    fonts_list = []
    for font_family, variants in FONT_FAMILIES.items():
        for variant in variants:
            fonts_list.append({
                "font_family": font_family,
                "url": resolve_path(os.path.join("static", "fonts", variant["file"])),
                "font_weight": variant.get("font-weight", "normal"),
                "font_style": variant.get("font-style", "normal"),
            })
    return fonts_list

def get_font_path(font_name):
    return resolve_path(os.path.join("static", "fonts", FONTS[font_name]))

def generate_startup_image(dimensions=(800,480)):
    bg_color = (255,255,255)
    text_color = (0,0,0)
    width, height = dimensions

    hostname = socket.gethostname()
    ip = get_ip_address()

    image = Image.new("RGBA", dimensions, bg_color)
    image_draw = ImageDraw.Draw(image)

    title_font_size = width * 0.145
    image_draw.text((width/2, height/2), "inkypi", anchor="mm", fill=text_color, font=get_font("Jost", title_font_size))

    text = f"To get started, visit http://{hostname}.local"
    text_font_size = width * 0.032

    # Draw the instructions
    y_text = height * 3 / 4
    image_draw.text((width/2, y_text), text, anchor="mm", fill=text_color, font=get_font("Jost", text_font_size))

    # Draw the IP on a line below
    if ip:
        ip_text = f"or http://{ip}"
        ip_text_font_size = width * 0.032
        bbox = image_draw.textbbox((0, 0), text, font=get_font("Jost", text_font_size))
        text_height = bbox[3] - bbox[1]
        ip_y = y_text + text_height * 1.35
        image_draw.text((width/2, ip_y), ip_text, anchor="mm", fill=text_color, font=get_font("Jost", ip_text_font_size))

    return image

def generate_message_image(dimensions, message):
    """Startup-style screen with a single status message, e.g. while connecting to WiFi."""
    width, height = dimensions
    image = Image.new("RGBA", dimensions, (255, 255, 255))
    image_draw = ImageDraw.Draw(image)

    image_draw.text((width/2, height/2), "inkypi", anchor="mm", fill=(0, 0, 0), font=get_font("Jost", width * 0.145))
    font = _fit_font(image_draw, message, width * 0.9, width * 0.04)
    image_draw.text((width/2, height * 3 / 4), message, anchor="mm", fill=(0, 0, 0), font=font)
    return image

def generate_wifi_setup_image(dimensions, ssid, password, url, error=None):
    """Screen shown while the WiFi setup hotspot is running: how to join it, plus a QR code that joins it."""
    width, height = dimensions
    image = Image.new("RGBA", dimensions, (255, 255, 255))
    image_draw = ImageDraw.Draw(image)
    black, red = (0, 0, 0), (200, 0, 0)
    margin = min(width, height) * 0.06

    landscape = width >= height
    qr_size = int(min(height * 0.62, width * 0.38) if landscape else min(width * 0.55, height * 0.38))
    qr = _wifi_qr_image(ssid, password, qr_size)

    title_font = get_font("Jost", min(width, height) * 0.1, "bold")
    image_draw.text((margin, margin), "WiFi setup", anchor="lt", fill=black, font=title_font)
    title_bottom = image_draw.textbbox((margin, margin), "WiFi setup", anchor="lt", font=title_font)[3]

    qr_side = qr.width if qr else 0

    if landscape:
        qr_pos = (int(margin), int(title_bottom + (height - title_bottom - qr_side) / 2))
        text_x = qr_pos[0] + qr_side + (margin if qr else 0)
        text_y = title_bottom + margin * 0.8
    else:
        qr_pos = (int((width - qr_side) / 2), int(title_bottom + margin * 0.8))
        text_x = margin
        text_y = qr_pos[1] + qr_side + margin * 0.8
    if qr:
        image.paste(qr, qr_pos)

    text_width = width - text_x - margin
    base_size = min(width, height) * 0.055
    lines = [
        ("Scan the code, or join this WiFi:", "normal", black),
        (ssid, "bold", black),
        (f"Password: {password}", "bold", black),
        ("The setup page opens by itself.", "normal", black),
        (f"If not, go to {url}", "normal", black),
    ]
    if error:
        lines.append((error, "normal", red))

    for text, weight, color in lines:
        for line in _wrap_text(image_draw, text, text_width, get_font("Jost", base_size, weight)):
            font = _fit_font(image_draw, line, text_width, base_size, weight)
            image_draw.text((text_x, text_y), line, anchor="lt", fill=color, font=font)
            text_y += base_size * 1.35
        text_y += base_size * 0.25

    return image

def _wifi_qr_image(ssid, password, size):
    """QR code that phones read as 'join this WiFi network'. None if the qrcode package is missing."""
    try:
        import qrcode
    except ImportError:
        logger.warning("qrcode package not installed, WiFi setup screen will have no QR code")
        return None

    def escape(value):
        return "".join("\\" + c if c in '\\;,:"' else c for c in value)

    qr = qrcode.QRCode(border=1, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(f"WIFI:T:WPA;S:{escape(ssid)};P:{escape(password)};;")
    qr.make(fit=True)
    matrix = qr.get_matrix()

    box = max(1, size // len(matrix))
    qr_image = Image.new("RGB", (box * len(matrix), box * len(matrix)), (255, 255, 255))
    draw = ImageDraw.Draw(qr_image)
    for y, row in enumerate(matrix):
        for x, filled in enumerate(row):
            if filled:
                draw.rectangle([x * box, y * box, (x + 1) * box - 1, (y + 1) * box - 1], fill=(0, 0, 0))
    return qr_image

def _fit_font(image_draw, text, max_width, size, weight="normal"):
    """Largest font up to `size` that fits `text` in `max_width`."""
    font = get_font("Jost", size, weight)
    while size > 8 and image_draw.textlength(text, font=font) > max_width:
        size *= 0.9
        font = get_font("Jost", size, weight)
    return font

def _wrap_text(image_draw, text, max_width, font):
    lines, current = [], ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if current and image_draw.textlength(candidate, font=font) > max_width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines

def parse_form(request_form):
    request_dict = request_form.to_dict()
    for key in request_form.keys():
        if key.endswith('[]'):
            request_dict[key] = request_form.getlist(key)
    return request_dict

def handle_request_files(request_files, form_data={}):
    allowed_file_extensions = {'pdf', 'png', 'avif', 'jpg', 'jpeg', 'gif', 'webp', 'heif', 'heic'}
    file_location_map = {}
    # handle existing file locations being provided as part of the form data
    for key in set(request_files.keys()):
        is_list = key.endswith('[]')
        if key in form_data:
            file_location_map[key] = form_data.getlist(key) if is_list else form_data.get(key)
    # add new files in the request
    for key, file in request_files.items(multi=True):
        is_list = key.endswith('[]')
        file_name = file.filename
        if not file_name:
            continue

        extension = os.path.splitext(file_name)[1].replace('.', '')
        if not extension or extension.lower() not in allowed_file_extensions:
            continue

        file_name = os.path.basename(file_name)

        file_save_dir = resolve_path(os.path.join("static", "images", "saved"))
        file_path = os.path.join(file_save_dir, file_name)

        # Open the image and apply EXIF transformation before saving
        if extension in {'jpg', 'jpeg'}:
            try:
                with Image.open(file) as img:
                    img = ImageOps.exif_transpose(img)
                    img.save(file_path)
            except Exception as e:
                logger.warning(f"EXIF processing error for {file_name}: {e}")
                file.save(file_path)
        else:
            # Directly save non-JPEG files
            file.save(file_path)

        if is_list:
            file_location_map.setdefault(key, [])
            file_location_map[key].append(file_path)
        else:
            file_location_map[key] = file_path
    return file_location_map
