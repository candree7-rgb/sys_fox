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
    Parse a webhook signal from Tasker screen-scrape format.

    Format example:
    - LONG !! / SHORT !!
    - BATUSDT
    - Entry price\n0.2143
    - Stop Loss\n5.26%\n0.2036
    - Target 1 (tap to copy)\n0.2158
    - Target 2-5 similarly
    """
    if not data or not isinstance(data, dict):
        return None

    # Get raw body text
    raw_body = data.get("_raw_body") or data.get("raw") or str(data)
    if not raw_body or not isinstance(raw_body, str):
        return None

    text = raw_body

    # --- Extract side (LONG/SHORT) ---
    side = None
    if re.search(r'LONG\s*!+|LONG\s*\|\s*BUY|🟩.*LONG', text, re.I):
        side = "buy"
    elif re.search(r'SHORT\s*!+|SHORT\s*\|\s*SELL|🟥.*SHORT', text, re.I):
        side = "sell"

    if not side:
        return None

    # --- Extract symbol (e.g., BATUSDT, BTCUSDT) ---
    # Look for standalone symbol pattern: uppercase letters + USDT
    symbol_match = re.search(r'\b([A-Z0-9]{2,10}USDT)\b', text)
    if not symbol_match:
        # Try without USDT suffix
        symbol_match = re.search(r'(?:^|\n)([A-Z]{2,10})(?:\n|$)', text)
        if symbol_match:
            symbol = symbol_match.group(1) + quote
        else:
            return None
    else:
        symbol = symbol_match.group(1)

    base = symbol.replace(quote, "")

    # --- Extract entry price ---
    # Pattern: "Entry price\n0.2143" or "Entry price,0.2143"
    entry = None
    entry_match = re.search(r'Entry\s*price[,\s]*\n?\s*([0-9]+\.?[0-9]*)', text, re.I)
    if entry_match:
        try:
            entry = float(entry_match.group(1))
        except ValueError:
            pass

    if not entry or entry <= 0:
        return None

    # --- Extract stop loss ---
    # Pattern: "Stop Loss\n5.26%\n0.2036" - we want the price, not the %
    sl = None
    # Look for Stop Loss followed by % then the actual price
    sl_match = re.search(r'Stop\s*Loss[,\s]*\n?\s*[0-9.]+%[,\s]*\n?\s*([0-9]+\.?[0-9]*)', text, re.I)
    if sl_match:
        try:
            sl = float(sl_match.group(1))
        except ValueError:
            pass

    # --- Extract targets (TP1-TP5) ---
    tps: List[float] = []

    # Pattern: "Target 1 (tap to copy)\n0.2158" or "Target 1\n0.2158"
    for i in range(1, 6):
        tp_match = re.search(
            rf'Target\s*{i}\s*(?:\([^)]*\))?[,\s]*\n?\s*([0-9]+\.?[0-9]*)',
            text, re.I
        )
        if tp_match:
            try:
                tp_val = float(tp_match.group(1))
                if tp_val > 0:
                    tps.append(tp_val)
            except ValueError:
                pass

    return {
        "base": base,
        "symbol": symbol,
        "side": side,
        "entry": entry,
        "trigger": entry,  # For backward compatibility with conditional orders
        "tp_prices": tps,
        "dca_prices": [],  # No DCAs in this format
        "sl_price": sl,
        "raw": raw_body,
    }
