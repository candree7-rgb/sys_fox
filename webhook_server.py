"""
Railway Webhook Server for receiving trading signals.

Runs a Flask server that receives POST requests with trading signals
and queues them for processing by the trade engine.
"""

import json
import logging
import threading
from queue import Queue
from typing import Any, Dict, Optional
from flask import Flask, request, jsonify

from config import WEBHOOK_PORT, WEBHOOK_SECRET

app = Flask(__name__)

# Disable Flask's default logging (we use our own)
log = logging.getLogger('werkzeug')
log.setLevel(logging.WARNING)

# Signal queue for passing signals to main loop
signal_queue: Queue = Queue()

# Logger (set by main)
logger: Optional[logging.Logger] = None


def set_logger(log: logging.Logger):
    global logger
    logger = log


def get_signal_queue() -> Queue:
    return signal_queue


@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint for Railway."""
    return jsonify({"status": "ok"}), 200


@app.route('/webhook', methods=['POST'])
def webhook():
    """
    Main webhook endpoint for receiving trading signals.

    Logs everything for debugging, then queues valid signals.
    """
    global logger

    # Log everything about the request for debugging
    if logger:
        logger.info("=" * 60)
        logger.info("INCOMING WEBHOOK REQUEST")
        logger.info("=" * 60)
        logger.info(f"Method: {request.method}")
        logger.info(f"Content-Type: {request.content_type}")
        logger.info(f"Headers: {dict(request.headers)}")

        # Log raw body
        raw_body = request.get_data(as_text=True)
        logger.info(f"Raw Body: {raw_body[:2000] if raw_body else '(empty)'}")

        # Try to parse as JSON
        try:
            json_body = request.get_json(force=True, silent=True)
            if json_body:
                logger.info(f"JSON Body: {json.dumps(json_body, indent=2)}")
        except Exception as e:
            logger.info(f"JSON Parse Error: {e}")

        # Log form data if present
        if request.form:
            logger.info(f"Form Data: {dict(request.form)}")

        # Log query params
        if request.args:
            logger.info(f"Query Params: {dict(request.args)}")

        logger.info("=" * 60)

    # Optional: Check webhook secret for security
    if WEBHOOK_SECRET:
        auth_header = request.headers.get('Authorization', '')
        secret_param = request.args.get('secret', '')
        x_secret = request.headers.get('X-Webhook-Secret', '')

        if WEBHOOK_SECRET not in (auth_header, secret_param, x_secret, f"Bearer {WEBHOOK_SECRET}"):
            if logger:
                logger.warning("Webhook request rejected: invalid secret")
            return jsonify({"error": "unauthorized"}), 401

    # Try to get the signal data
    data: Dict[str, Any] = {}

    # Try JSON first
    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:
        pass

    # Fallback to form data
    if not data and request.form:
        data = dict(request.form)

    # Fallback to raw body as string
    if not data:
        raw = request.get_data(as_text=True)
        if raw:
            data = {"raw": raw}

    if data:
        # Add timestamp
        import time
        data["_received_ts"] = time.time()
        data["_raw_body"] = request.get_data(as_text=True)

        # Queue for processing
        signal_queue.put(data)

        if logger:
            logger.info(f"Signal queued for processing")

        return jsonify({"status": "received", "queued": True}), 200

    return jsonify({"status": "empty request"}), 400


@app.route('/', methods=['GET', 'POST'])
def root():
    """Root endpoint - redirect to webhook or show status."""
    if request.method == 'POST':
        # Treat root POST as webhook
        return webhook()
    return jsonify({
        "status": "running",
        "endpoints": {
            "/webhook": "POST - receive trading signals",
            "/health": "GET - health check"
        }
    }), 200


def run_server(host: str = "0.0.0.0", port: int = None):
    """Run the Flask server (blocking)."""
    port = port or WEBHOOK_PORT
    if logger:
        logger.info(f"Starting webhook server on {host}:{port}")
    app.run(host=host, port=port, threaded=True, use_reloader=False)


def start_server_thread(host: str = "0.0.0.0", port: int = None) -> threading.Thread:
    """Start the Flask server in a background thread."""
    port = port or WEBHOOK_PORT
    t = threading.Thread(target=run_server, args=(host, port), daemon=True)
    t.start()
    return t
