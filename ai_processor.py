#!/usr/bin/env python3
"""
AI Vision Processor for Blink Camera feeds.

Uses YOLOv8-nano for real-time object detection on camera snapshots.
Draws bounding boxes with labels and confidence scores.
Generates motion heatmaps by diffing consecutive frames.
"""

import io
import logging
import os
import time
from collections import defaultdict
from datetime import datetime

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter

logger = logging.getLogger(__name__)

# Detection state
_model = None
_previous_frames: dict[str, np.ndarray] = {}
_detection_history: list[dict] = []  # last N detections across all cameras
_detection_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

# Colors for different object classes
CLASS_COLORS = {
    "person": (255, 50, 50),
    "car": (50, 150, 255),
    "truck": (50, 150, 255),
    "bicycle": (255, 200, 50),
    "motorcycle": (255, 200, 50),
    "dog": (50, 255, 100),
    "cat": (50, 255, 100),
    "bird": (50, 255, 100),
    "bus": (255, 150, 50),
    "backpack": (200, 100, 255),
    "suitcase": (200, 100, 255),
    "handbag": (200, 100, 255),
    "umbrella": (150, 150, 150),
}

# Classes we care about for security cameras
SECURITY_CLASSES = {
    0: "person", 1: "bicycle", 2: "car", 3: "motorcycle", 5: "bus",
    7: "truck", 14: "bird", 15: "cat", 16: "dog",
    24: "backpack", 25: "umbrella", 26: "handbag", 28: "suitcase",
}

MAX_HISTORY = 100


def load_model():
    """Load YOLOv8-nano model (lazy init on first use)."""
    global _model
    if _model is not None:
        return _model

    try:
        from ultralytics import YOLO
        model_path = os.environ.get("YOLO_MODEL", "yolov8n.pt")
        logger.info(f"Loading YOLO model: {model_path}")
        _model = YOLO(model_path)
        # Warm up
        _model.predict(np.zeros((640, 640, 3), dtype=np.uint8), verbose=False)
        logger.info("YOLO model loaded and warmed up")
        return _model
    except ImportError:
        logger.warning("ultralytics not installed - AI detection disabled")
        return None
    except Exception as e:
        logger.error(f"Failed to load YOLO model: {e}")
        return None


def detect_objects(image_bytes: bytes, camera_name: str, confidence: float = 0.35) -> dict:
    """
    Run object detection on an image.

    Returns:
        {
            "detections": [{"class": "person", "confidence": 0.92, "bbox": [x1,y1,x2,y2]}],
            "annotated_image": bytes (JPEG with bounding boxes),
            "summary": "2 persons, 1 car",
            "alert_level": "high" | "medium" | "low" | "none"
        }
    """
    model = load_model()
    if model is None:
        return {
            "detections": [],
            "annotated_image": image_bytes,
            "summary": "AI disabled",
            "alert_level": "none",
        }

    # Decode image
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    img_array = np.array(img)

    # Run detection
    results = model.predict(img_array, conf=confidence, verbose=False)

    detections = []
    for result in results:
        for box in result.boxes:
            cls_id = int(box.cls[0])
            if cls_id not in SECURITY_CLASSES:
                continue
            cls_name = SECURITY_CLASSES[cls_id]
            conf = float(box.conf[0])
            bbox = box.xyxy[0].tolist()
            detections.append({
                "class": cls_name,
                "confidence": round(conf, 2),
                "bbox": [int(b) for b in bbox],
            })

    # Draw annotations
    annotated = draw_detections(img, detections)

    # Generate summary
    class_counts = defaultdict(int)
    for d in detections:
        class_counts[d["class"]] += 1

    summary_parts = []
    for cls, count in sorted(class_counts.items(), key=lambda x: -x[1]):
        summary_parts.append(f"{count} {cls}{'s' if count > 1 else ''}")
    summary = ", ".join(summary_parts) if summary_parts else "No objects detected"

    # Determine alert level
    if any(d["class"] == "person" and d["confidence"] > 0.6 for d in detections):
        alert_level = "high"
    elif any(d["class"] in ("car", "truck", "bus") for d in detections):
        alert_level = "medium"
    elif detections:
        alert_level = "low"
    else:
        alert_level = "none"

    # Update history
    if detections:
        event = {
            "camera": camera_name,
            "timestamp": datetime.now().isoformat(),
            "summary": summary,
            "alert_level": alert_level,
            "detections": detections,
        }
        _detection_history.append(event)
        if len(_detection_history) > MAX_HISTORY:
            _detection_history.pop(0)

        # Update counts
        for d in detections:
            _detection_counts[camera_name][d["class"]] += 1

    # Convert annotated image to bytes
    buf = io.BytesIO()
    annotated.save(buf, format="JPEG", quality=90)
    annotated_bytes = buf.getvalue()

    return {
        "detections": detections,
        "annotated_image": annotated_bytes,
        "summary": summary,
        "alert_level": alert_level,
    }


def draw_detections(img: Image.Image, detections: list) -> Image.Image:
    """Draw bounding boxes with labels on image."""
    draw = ImageDraw.Draw(img)

    # Try to load a decent font
    font = None
    for font_path in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    ]:
        try:
            font = ImageFont.truetype(font_path, 16)
            break
        except (OSError, IOError):
            continue
    if font is None:
        font = ImageFont.load_default()

    for det in detections:
        cls = det["class"]
        conf = det["confidence"]
        x1, y1, x2, y2 = det["bbox"]
        color = CLASS_COLORS.get(cls, (255, 255, 255))

        # Draw box with slight transparency effect (thicker outer, thinner inner)
        draw.rectangle([x1, y1, x2, y2], outline=color, width=3)
        draw.rectangle([x1+1, y1+1, x2-1, y2-1], outline=(*color, 180), width=1)

        # Label background
        label = f"{cls} {conf:.0%}"
        bbox_text = draw.textbbox((x1, y1), label, font=font)
        text_w = bbox_text[2] - bbox_text[0]
        text_h = bbox_text[3] - bbox_text[1]
        draw.rectangle([x1, y1 - text_h - 8, x1 + text_w + 8, y1], fill=color)
        draw.text((x1 + 4, y1 - text_h - 4), label, fill=(255, 255, 255), font=font)

        # Corner accents
        corner_len = min(20, (x2 - x1) // 4, (y2 - y1) // 4)
        for cx, cy, dx, dy in [
            (x1, y1, 1, 1), (x2, y1, -1, 1),
            (x1, y2, 1, -1), (x2, y2, -1, -1),
        ]:
            draw.line([(cx, cy), (cx + corner_len * dx, cy)], fill=color, width=3)
            draw.line([(cx, cy), (cx, cy + corner_len * dy)], fill=color, width=3)

    return img


def generate_motion_heatmap(image_bytes: bytes, camera_name: str) -> bytes | None:
    """
    Generate a motion heatmap by diffing current frame with previous frame.
    Returns None if no previous frame exists.
    """
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    current = np.array(img, dtype=np.float32)

    if camera_name not in _previous_frames:
        _previous_frames[camera_name] = current.astype(np.uint8)
        return None

    previous = _previous_frames[camera_name].astype(np.float32)

    # Resize if dimensions changed
    if previous.shape != current.shape:
        _previous_frames[camera_name] = current.astype(np.uint8)
        return None

    # Compute absolute difference
    diff = np.abs(current - previous)
    diff_gray = np.mean(diff, axis=2)

    # Normalize and apply threshold
    diff_gray = np.clip(diff_gray * 3, 0, 255).astype(np.uint8)

    # Create heatmap coloring (blue -> green -> yellow -> red)
    heatmap = np.zeros((*diff_gray.shape, 3), dtype=np.uint8)
    # Blue channel (low motion)
    heatmap[:, :, 2] = np.clip(255 - diff_gray * 4, 0, 255).astype(np.uint8)
    # Green channel (medium motion)
    mask_med = diff_gray > 30
    heatmap[mask_med, 1] = np.clip(diff_gray[mask_med] * 2, 0, 255).astype(np.uint8)
    # Red channel (high motion)
    mask_high = diff_gray > 60
    heatmap[mask_high, 0] = np.clip(diff_gray[mask_high] * 3, 0, 255).astype(np.uint8)

    # Blend with original image
    overlay = (current * 0.4 + heatmap.astype(np.float32) * 0.6).astype(np.uint8)

    # Update previous frame
    _previous_frames[camera_name] = current.astype(np.uint8)

    result = Image.fromarray(overlay)
    buf = io.BytesIO()
    result.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def get_detection_history(limit: int = 20) -> list[dict]:
    """Get recent detection events."""
    return list(reversed(_detection_history[-limit:]))


def get_detection_stats() -> dict:
    """Get aggregate detection counts per camera."""
    return {cam: dict(counts) for cam, counts in _detection_counts.items()}


def format_alert_message(camera_name: str, result: dict) -> str:
    """Format a smart Telegram alert message with detection details."""
    alert_icons = {
        "high": "🚨",
        "medium": "🚗",
        "low": "🐾",
        "none": "📹",
    }
    icon = alert_icons.get(result["alert_level"], "📹")

    lines = [f"{icon} <b>Motion: {camera_name}</b>"]
    lines.append(f"🔍 {result['summary']}")
    lines.append(f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    if result["detections"]:
        lines.append("")
        for d in result["detections"][:5]:
            conf_bar = "█" * int(d["confidence"] * 10) + "░" * (10 - int(d["confidence"] * 10))
            lines.append(f"  • {d['class']}: {conf_bar} {d['confidence']:.0%}")

    return "\n".join(lines)
