import re
import hashlib
from typing import Any, Dict, Optional, List

NUM = r"([0-9]+(?:\.[0-9]+)?)"

RE_SYMBOL_SIDE = re.compile(r"\*\*([A-Z0-9]+)\*\*\s+(LONG|SHORT)\s+Signal", re.I)
RE_ENTER_TRIGGER = re.compile(r"Enter\s+on\s+Trigger\s*:\s*`?\$?"+NUM+r"`?", re.I)
# Support both: "Entry $0.08850" (header) and "**Entry:** `$0.08850000`" (body)
RE_ENTRY = re.compile(r"(?:\*\*)?Entry:?(?:\*\*)?\s*`?\$?"+NUM+r"`?", re.I)

RE_TP = re.compile(r"\*\*TP(\d+)\:\*\*\s*`?\$?"+NUM+r"`?", re.I)
RE_DCA = re.compile(r"\*\*DCA\s*#?\s*(\d+)\:\*\*\s*`?\$?"+NUM+r"`?", re.I)
RE_SL = re.compile(r"\*\*Stop\s+Loss\:\*\*\s*`?\$?"+NUM+r"`?", re.I)

RE_AWAITING = re.compile(r"AWAITING\s+ENTRY", re.I)
RE_CLOSED = re.compile(r"TRADE\s+CLOSED", re.I)

def parse_signal(text: str, quote: str = "USDT") -> Optional[Dict[str, Any]]:
    # We only want fresh "NEW SIGNAL" entries, not closed summaries
    if "NEW SIGNAL" not in text.upper():
        return None
    if RE_CLOSED.search(text):
        return None

    ms = RE_SYMBOL_SIDE.search(text)
    if not ms:
        return None
    base = ms.group(1).upper()
    side_word = ms.group(2).upper()
    side = "sell" if side_word == "SHORT" else "buy"
    symbol = f"{base}{quote}"

    mtr = RE_ENTER_TRIGGER.search(text) or RE_ENTRY.search(text)
    if not mtr:
        return None
    trigger = float(mtr.group(1))

    tps: List[float] = []
    for m in RE_TP.finditer(text):
        idx = int(m.group(1))
        price = float(m.group(2))
        # keep in order
        while len(tps) < idx:
            tps.append(0.0)
        tps[idx-1] = price
    tps = [p for p in tps if p > 0]

    dcas: List[float] = []
    for m in RE_DCA.finditer(text):
        idx = int(m.group(1))
        price = float(m.group(2))
        while len(dcas) < idx:
            dcas.append(0.0)
        dcas[idx-1] = price
    dcas = [p for p in dcas if p > 0]

    sl = None
    msl = RE_SL.search(text)
    if msl:
        sl = float(msl.group(1))

    return {
        "base": base,
        "symbol": symbol,
        "side": side,          # buy/ sell
        "trigger": trigger,
        "tp_prices": tps,
        "dca_prices": dcas,
        "sl_price": sl,
        "raw": text,
    }

def signal_hash(sig: Dict[str, Any]) -> str:
    # Support both 'trigger' (old) and 'entry' (new) field names
    entry = sig.get('trigger') or sig.get('entry')
    core = f"{sig.get('symbol')}|{sig.get('side')}|{entry}|{sig.get('tp_prices')}|{sig.get('dca_prices')}"
    return hashlib.md5(core.encode("utf-8")).hexdigest()


def parse_webhook_signal(data: Dict[str, Any], quote: str = "USDT") -> Optional[Dict[str, Any]]:
    """
    Parse a webhook signal with Entry + SL + 5 TPs.

    Flexible parser that tries multiple common field name patterns.
    Logs the raw data so we can see the exact format in Railway logs.

    Expected structure (we'll adapt after seeing real signals):
    {
        "symbol": "BTC" or "BTCUSDT",
        "side": "long" / "short" / "buy" / "sell",
        "entry": 42000.0,
        "sl": 41000.0,
        "tp1": 43000.0, "tp2": 44000.0, ... "tp5": 47000.0
    }
    """
    if not data or not isinstance(data, dict):
        return None

    raw_body = data.get("_raw_body", str(data))

    # --- Extract symbol ---
    symbol = None
    for key in ["symbol", "pair", "coin", "ticker", "asset", "market"]:
        if key in data:
            symbol = str(data[key]).upper().strip()
            break

    if not symbol:
        return None

    # Normalize symbol: add quote if missing
    if not symbol.endswith(quote):
        base = symbol.replace("USDT", "").replace("PERP", "").replace("/", "")
        symbol = f"{base}{quote}"

    # --- Extract side ---
    side = None
    for key in ["side", "direction", "type", "action", "position"]:
        if key in data:
            side_raw = str(data[key]).lower().strip()
            if side_raw in ("long", "buy", "b", "1"):
                side = "buy"
            elif side_raw in ("short", "sell", "s", "-1", "0"):
                side = "sell"
            break

    if not side:
        return None

    # --- Extract entry price ---
    entry = None
    for key in ["entry", "entry_price", "entryPrice", "price", "open", "limit", "limitPrice"]:
        if key in data:
            try:
                entry = float(data[key])
                break
            except (ValueError, TypeError):
                pass

    if not entry or entry <= 0:
        return None

    # --- Extract stop loss ---
    sl = None
    for key in ["sl", "stop", "stopLoss", "stop_loss", "stoploss", "SL"]:
        if key in data:
            try:
                sl = float(data[key])
                break
            except (ValueError, TypeError):
                pass

    # --- Extract take profits (TP1-TP5) ---
    tps: List[float] = []

    # Try numbered TPs first: tp1, tp2, ..., tp5
    for i in range(1, 6):
        tp_val = None
        for key in [f"tp{i}", f"TP{i}", f"tp_{i}", f"take_profit_{i}", f"takeProfit{i}", f"target{i}"]:
            if key in data:
                try:
                    tp_val = float(data[key])
                    break
                except (ValueError, TypeError):
                    pass
        if tp_val and tp_val > 0:
            tps.append(tp_val)

    # Fallback: try 'tps' or 'targets' as array
    if not tps:
        for key in ["tps", "targets", "take_profits", "takeProfits", "tp", "TP"]:
            if key in data and isinstance(data[key], (list, tuple)):
                for v in data[key]:
                    try:
                        tps.append(float(v))
                    except (ValueError, TypeError):
                        pass
                break

    return {
        "base": symbol.replace(quote, ""),
        "symbol": symbol,
        "side": side,
        "entry": entry,
        "trigger": entry,  # For backward compatibility
        "tp_prices": tps,
        "dca_prices": [],  # No DCAs in new format
        "sl_price": sl,
        "raw": raw_body,
    }
