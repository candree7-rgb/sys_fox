"""
Main entry point for Railway Webhook mode.

Receives trading signals via HTTP webhook instead of Discord polling.
"""

import sys
import time
import random
import threading
import logging

from config import (
    BYBIT_API_KEY, BYBIT_API_SECRET, BYBIT_TESTNET, BYBIT_DEMO, RECV_WINDOW,
    CATEGORY, QUOTE, LEVERAGE, RISK_PCT,
    MAX_CONCURRENT_TRADES, MAX_TRADES_PER_DAY, TC_MAX_LAG_SEC,
    POLL_SECONDS,
    STATE_FILE, DRY_RUN, LOG_LEVEL,
    WEBHOOK_PORT
)
from bybit_v5 import BybitV5
from signal_parser import parse_webhook_signal, signal_hash
from state import load_state, save_state, utc_day_key
from trade_engine import TradeEngine
import webhook_server
import db_export


def setup_logger() -> logging.Logger:
    log = logging.getLogger("bot")
    log.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    h = logging.StreamHandler(sys.stdout)
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", "%H:%M:%S")
    h.setFormatter(fmt)
    log.handlers[:] = [h]
    return log


def main():
    log = setup_logger()

    # Basic env checks (no Discord needed!)
    missing = [k for k, v in {
        "BYBIT_API_KEY": BYBIT_API_KEY,
        "BYBIT_API_SECRET": BYBIT_API_SECRET,
    }.items() if not v]
    if missing:
        raise SystemExit(f"Missing ENV(s): {', '.join(missing)}")

    st = load_state(STATE_FILE)

    bybit = BybitV5(BYBIT_API_KEY, BYBIT_API_SECRET, testnet=BYBIT_TESTNET, demo=BYBIT_DEMO, recv_window=RECV_WINDOW)
    engine = TradeEngine(bybit, st, log)

    log.info("=" * 60)
    mode_str = " | DRY_RUN" if DRY_RUN else ""
    mode_str += " | DEMO" if BYBIT_DEMO else ""
    mode_str += " | TESTNET" if BYBIT_TESTNET else ""
    log.info("Webhook -> Bybit Bot" + mode_str)
    log.info("=" * 60)
    log.info(f"Webhook server starting on port {WEBHOOK_PORT}")
    log.info(f"Config: CATEGORY={CATEGORY}, QUOTE={QUOTE}, LEVERAGE={LEVERAGE}x")
    log.info(f"Config: RISK_PCT={RISK_PCT}%, MAX_CONCURRENT={MAX_CONCURRENT_TRADES}, MAX_DAILY={MAX_TRADES_PER_DAY}")
    log.info(f"Config: DRY_RUN={DRY_RUN}, LOG_LEVEL={LOG_LEVEL}")

    # Initialize database if enabled
    if db_export.is_enabled():
        log.info("Initializing database...")
        if db_export.init_database():
            log.info("Database ready")
        else:
            log.warning("Database initialization failed (continuing without DB export)")

    # Startup sync - check for orphaned positions
    engine.startup_sync()

    # Heartbeat tracking
    last_heartbeat = time.time()
    HEARTBEAT_INTERVAL = 300  # 5 min

    # ----- Start Webhook Server -----
    webhook_server.set_logger(log)
    signal_queue = webhook_server.get_signal_queue()
    webhook_server.start_server_thread(port=WEBHOOK_PORT)
    log.info(f"Webhook server running on port {WEBHOOK_PORT}")

    # ----- WS thread for Bybit executions -----
    ws_err = {"err": None}

    def on_execution(ev):
        try:
            engine.on_execution(ev)
        except Exception as e:
            log.warning(f"WS execution handler error: {e}")

    def on_order(ev):
        return

    def on_ws_error(err):
        ws_err["err"] = err
        log.debug(f"WS reconnecting: {err}")

    def ws_loop():
        while True:
            try:
                bybit.run_private_ws(on_execution=on_execution, on_order=on_order, on_error=on_ws_error)
            except Exception as e:
                on_ws_error(e)
            time.sleep(3)

    t = threading.Thread(target=ws_loop, daemon=True)
    t.start()

    # ----- Helper: limits -----
    def trades_today() -> int:
        return int(st.get("daily_counts", {}).get(utc_day_key(), 0))

    def inc_trades_today():
        k = utc_day_key()
        st.setdefault("daily_counts", {})[k] = int(st.get("daily_counts", {}).get(k, 0)) + 1

    # ----- Main loop -----
    while True:
        try:
            # Heartbeat log
            if time.time() - last_heartbeat > HEARTBEAT_INTERVAL:
                active = [tr for tr in st.get("open_trades", {}).values() if tr.get("status") in ("pending", "open")]
                log.info(f"Heartbeat: {len(active)} active trade(s), {trades_today()} today")
                last_heartbeat = time.time()

            # Maintenance
            engine.cancel_expired_entries()
            engine.cleanup_closed_trades()
            engine.check_tp_fills_fallback()
            engine.check_position_alerts()
            engine.log_daily_stats()

            # Entry-fill fallback (polling) and post-orders placement
            for tid, tr in list(st.get("open_trades", {}).items()):
                if tr.get("status") == "pending":
                    sz, avg = engine.position_size_avg(tr["symbol"])
                    if sz > 0 and avg > 0:
                        tr["status"] = "open"
                        tr["entry_price"] = avg
                        tr["filled_ts"] = time.time()
                        log.info(f"ENTRY (poll) {tr['symbol']} @ {avg}")
                if tr.get("status") == "open" and not tr.get("post_orders_placed"):
                    engine.place_post_entry_orders(tr)

            # Check limits before processing new signals
            active = [tr for tr in st.get("open_trades", {}).values() if tr.get("status") in ("pending", "open")]
            at_limit = len(active) >= MAX_CONCURRENT_TRADES or trades_today() >= MAX_TRADES_PER_DAY

            if at_limit:
                # Drain queue but don't process
                while not signal_queue.empty():
                    try:
                        discarded = signal_queue.get_nowait()
                        log.info(f"Signal discarded (at limit): {discarded.get('symbol', 'unknown')}")
                    except Exception:
                        break
            else:
                # Process signals from webhook queue
                while not signal_queue.empty():
                    try:
                        raw_signal = signal_queue.get_nowait()
                    except Exception:
                        break

                    received_ts = raw_signal.get("_received_ts", time.time())
                    age = time.time() - received_ts
                    if age > TC_MAX_LAG_SEC:
                        log.debug(f"Skipping old signal (age={age:.0f}s)")
                        continue

                    # Parse the webhook signal
                    sig = parse_webhook_signal(raw_signal, quote=QUOTE)
                    if not sig:
                        log.warning(f"Could not parse webhook signal: {raw_signal}")
                        continue

                    log.info(f"Signal parsed: {sig['symbol']} {sig['side'].upper()} entry={sig['entry']}")

                    sh = signal_hash(sig)
                    seen = set(st.get("seen_signal_hashes", []))
                    if sh in seen:
                        log.debug(f"Signal {sig['symbol']} already seen, skipping")
                        continue

                    # Mark seen
                    seen.add(sh)
                    st["seen_signal_hashes"] = list(seen)[-500:]

                    trade_id = f"{sig['symbol']}|{sig['side']}|{int(time.time())}"
                    log.info(f"Placing limit entry for {sig['symbol']}...")

                    # Use limit entry (not conditional) since trade is immediately active
                    oid = engine.place_limit_entry(sig, trade_id)
                    if not oid:
                        log.warning(f"Entry order failed for {sig['symbol']}")
                        continue

                    # Store trade with signal's SL and TPs
                    st.setdefault("open_trades", {})[trade_id] = {
                        "id": trade_id,
                        "symbol": sig["symbol"],
                        "order_side": "Sell" if sig["side"] == "sell" else "Buy",
                        "pos_side": "Short" if sig["side"] == "sell" else "Long",
                        "entry": float(sig["entry"]),
                        "trigger": float(sig["entry"]),  # For compatibility
                        "tp_prices": sig.get("tp_prices") or [],
                        "tp_splits": None,
                        "dca_prices": [],  # No DCAs in new format
                        "sl_price": sig.get("sl_price"),
                        "entry_order_id": oid,
                        "status": "pending",
                        "placed_ts": time.time(),
                        "base_qty": engine.calc_base_qty(sig["symbol"], float(sig["entry"])),
                        "raw": sig.get("raw", ""),
                    }
                    inc_trades_today()
                    log.info(f"ENTRY PLACED {sig['symbol']} {sig['side'].upper()} @ {sig['entry']} (id={trade_id})")

                    # Check limits after each trade
                    active = [tr for tr in st.get("open_trades", {}).values() if tr.get("status") in ("pending", "open")]
                    if len(active) >= MAX_CONCURRENT_TRADES or trades_today() >= MAX_TRADES_PER_DAY:
                        break

            save_state(STATE_FILE, st)

        except KeyboardInterrupt:
            log.info("Shutting down...")
            break
        except Exception as e:
            log.exception(f"Loop error: {e}")
            time.sleep(3)

        time.sleep(max(1, POLL_SECONDS))


if __name__ == "__main__":
    main()
