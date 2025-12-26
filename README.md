### Bybit AO Bot (Direct)

- Discord Signal -> Bybit conditional entry
- TPs reduce-only, DCAs conditional add
- SL moves to Breakeven after TP1 fill (via WS execution stream)
- Entry expires after X minutes

Run:
1) cp .env.example .env
2) pip install -r requirements.txt
3) python main.py
