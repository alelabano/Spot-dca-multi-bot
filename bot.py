import json
import os
import time
import traceback
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from datetime import datetime, timezone

from dotenv import load_dotenv
from eth_account import Account

from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants


# ============================================================
# CONFIGURAZIONE
# ============================================================

load_dotenv()

PRIVATE_KEY = os.getenv("HYPERLIQUID_PRIVATE_KEY")
ACCOUNT_ADDRESS = os.getenv("HYPERLIQUID_ACCOUNT_ADDRESS")

# COIN gestite (nome base, senza prefisso U)
COINS = [c.strip().upper() for c in os.getenv("COINS", "HYPE,ZEC,ETH,SOL").split(",") if c.strip()]

# LOOP: verifica ogni 4 ore, niente piu' candele
LOOP_INTERVAL_SECONDS = int(os.getenv("LOOP_INTERVAL_SECONDS", "14400"))

BUY_USD = float(os.getenv("BUY_USD", "12"))
MAX_POSITION_USD = float(os.getenv("MAX_POSITION_USD", "200"))
MAX_WEEKLY_BUYS = int(os.getenv("MAX_WEEKLY_BUYS", "10"))

DIP_PERCENT = float(os.getenv("DIP_PERCENT", "2"))
TAKE_PROFIT_PERCENT = float(os.getenv("TAKE_PROFIT_PERCENT", "4"))

MAX_SLIPPAGE = float(os.getenv("MAX_SLIPPAGE", "0.01"))

MAX_LOTS_TO_SELL_PER_RUN = int(os.getenv("MAX_LOTS_TO_SELL_PER_RUN", "1"))

STATE_DIR = os.getenv("STATE_DIR", "/data")
STATE_FILE = os.path.join(STATE_DIR, "state.json")

STARTUP_DELAY = int(os.getenv("STARTUP_DELAY", "5"))

POST_ORDER_DELAY = int(os.getenv("POST_ORDER_DELAY", "3"))

FILL_CHECK_ATTEMPTS = int(os.getenv("FILL_CHECK_ATTEMPTS", "5"))

FILL_CHECK_DELAY = float(os.getenv("FILL_CHECK_DELAY", "1"))

POSITION_TOLERANCE = float(os.getenv("POSITION_TOLERANCE", "0.00003"))

# Valore minimo di un ordine spot su Hyperliquid
MIN_ORDER_USD = float(os.getenv("MIN_ORDER_USD", "10"))

# Percentuale del lotto venduta al raggiungimento del target
SELL_PERCENT = float(os.getenv("SELL_PERCENT", "95"))


# ============================================================
# VALIDAZIONE
# ============================================================

if not PRIVATE_KEY:
    raise RuntimeError("HYPERLIQUID_PRIVATE_KEY mancante")

if not ACCOUNT_ADDRESS:
    raise RuntimeError("HYPERLIQUID_ACCOUNT_ADDRESS mancante")


# ============================================================
# CONNESSIONE
# ============================================================

wallet = Account.from_key(PRIVATE_KEY)

info = Info(constants.MAINNET_API_URL, skip_ws=True)

exchange = Exchange(wallet, constants.MAINNET_API_URL, account_address=ACCOUNT_ADDRESS)


# ============================================================
# RISOLUZIONE COPPIE SPOT
# ============================================================
# Su HyperCore il pair mostrato sull'interfaccia puo' corrispondere
# a un nome diverso nei metadata (es. UBTC/USDC, UETH/USDC, USOL/USDC).
# Risolviamo una sola volta all'avvio, per ogni coin: nome del mercato,
# nome del token e szDecimals.

def resolve_spot_markets():
    meta = info.spot_meta()

    quote_token_idx = None

    for idx, token in enumerate(meta["tokens"]):
        if token.get("name") == "USDC" and quote_token_idx is None:
            quote_token_idx = idx

    if quote_token_idx is None:
        raise RuntimeError("Nessun token USDC trovato nei metadata Spot")

    markets = {}

    for coin in COINS:
        found = None

        for idx, token in enumerate(meta["tokens"]):
            if token.get("name") not in (coin, "U" + coin):
                continue

            for market in meta["universe"]:
                tokens = market.get("tokens", [])

                if len(tokens) == 2 and tokens[0] == idx and tokens[1] == quote_token_idx:
                    found = {
                        "market": market["name"],
                        "base": token["name"],
                        "decimals": int(token["szDecimals"])
                    }
                    break

            if found:
                break

        if not found:
            print(f"ATTENZIONE: nessun mercato spot {coin}/USDC nei metadata: coin ignorata", flush=True)
            continue

        markets[coin] = found

    if not markets:
        token_names = [t.get("name") for t in meta["tokens"]]
        print(f"DEBUG TOKENS DISPONIBILI | {token_names}", flush=True)
        raise RuntimeError("Nessun mercato spot trovato per le coin configurate")

    return markets


MARKETS = resolve_spot_markets()


# ============================================================
# LOG
# ============================================================

def log(message):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{now}] {message}", flush=True)


# ============================================================
# STATE
# ============================================================

def default_state():
    return {
        "version": 8,

        "performance_start_ms": int(datetime.now(timezone.utc).timestamp() * 1000),

        # Prezzo dell'ultimo trade (buy o sell): riferimento per
        # decidere il prossimo acquisto su dip.
        "last_trade_price": None,

        "week_id": None,
        "weekly_buys": 0,

        "next_lot_id": 1,

        "open_lots": [],
        "sell_trades": [],

        "last_buy": None,
        "last_sell": None
    }


def load_state():
    os.makedirs(STATE_DIR, exist_ok=True)

    if not os.path.exists(STATE_FILE):
        return default_state()

    with open(STATE_FILE, "r") as f:
        state = json.load(f)

    base = default_state()
    base.update(state)

    return base


def save_state(state):
    os.makedirs(STATE_DIR, exist_ok=True)

    tmp_file = STATE_FILE + ".tmp"

    with open(tmp_file, "w") as f:
        json.dump(state, f, indent=2)

    os.replace(tmp_file, STATE_FILE)


# ============================================================
# SETTIMANA
# ============================================================

def current_week_id():
    now = datetime.now(timezone.utc)
    iso = now.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def refresh_week(state):
    week_id = current_week_id()

    if state.get("week_id") != week_id:
        state["week_id"] = week_id
        state["weekly_buys"] = 0
        save_state(state)


# ============================================================
# SPOT METADATA
# ============================================================

# Size: multipli di szDecimals della coin (default per difetto, per non
# superare il saldo; per i BUY si passa ROUND_UP per non scendere sotto
# il minimo d'ordine).
def round_size(coin, size, rounding=ROUND_DOWN):
    tick = Decimal(1).scaleb(-MARKETS[coin]["decimals"])
    return float(Decimal(str(round(float(size), 8))).quantize(tick, rounding=rounding))


# Prezzo: max 5 cifre significative (gli interi sono sempre validi) e
# max (8 - szDecimals) decimali.
def round_spot_price(coin, price, is_buy):
    rounding = ROUND_DOWN if is_buy else ROUND_UP

    d = Decimal(str(price))
    d = d.quantize(Decimal(1).scaleb(min(d.adjusted() - 4, 0)), rounding=rounding)
    d = d.quantize(Decimal(1).scaleb(-(8 - MARKETS[coin]["decimals"])), rounding=rounding)

    return float(d)


def get_24h_high_price(coin):
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = end_ms - (24 * 60 * 60 * 1000)

    # Recupera le candele da 1 ora (1h) per la coppia spot
    candles = info.candles_snapshot(MARKETS[coin]["market"], "1h", start_ms, end_ms)

    if not candles:
        raise RuntimeError(f"Impossibile recuperare i dati OHLCv delle ultime 24 ore per {coin}")

    # Estrae il massimo (tasti 'h' nell'oggetto candela)
    high_24h = max(float(candle["h"]) for candle in candles)
    return high_24h


# ============================================================
# SALDI SPOT
# ============================================================

def get_spot_balances():
    data = info.spot_user_state(ACCOUNT_ADDRESS)

    usdc_total = 0.0
    usdc_hold = 0.0

    coins = {coin: {"total": 0.0, "available": 0.0} for coin in MARKETS}

    for balance in data.get("balances", []):
        name = balance.get("coin")

        total = float(balance.get("total", 0) or 0)
        hold = float(balance.get("hold", 0) or 0)

        if name == "USDC":
            usdc_total = total
            usdc_hold = hold
        else:
            for coin, market in MARKETS.items():
                if name == market["base"]:
                    coins[coin] = {"total": total, "available": max(0.0, total - hold)}

    return {
        "usdc_total": usdc_total,
        "usdc_available": max(0.0, usdc_total - usdc_hold),

        "coins": coins
    }


# ============================================================
# PREZZO SPOT
# ============================================================

def get_spot_price(coin):
    book = info.l2_snapshot(MARKETS[coin]["market"])

    levels = book.get("levels", [])

    if len(levels) < 2:
        raise RuntimeError(
            f"Orderbook {coin}/USDC non disponibile"
        )

    bids = levels[0]
    asks = levels[1]

    if not bids or not asks:
        raise RuntimeError(
            f"Bid/Ask {coin}/USDC non disponibili"
        )

    best_bid = float(bids[0]["px"])
    best_ask = float(asks[0]["px"])

    if best_bid <= 0 or best_ask <= 0:
        raise RuntimeError(
            f"Prezzo Spot non valido: "
            f"bid={best_bid}, ask={best_ask}"
        )

    return (best_bid + best_ask) / 2.0


# ============================================================
# ORDINE SPOT AGGRESSIVO IOC
# ============================================================

def get_spot_execution_price(coin, is_buy, slippage):
    book = info.l2_snapshot(MARKETS[coin]["market"])

    levels = book.get("levels", [])

    if len(levels) < 2:
        raise RuntimeError(f"Orderbook {coin}/USDC non disponibile")

    bids = levels[0]
    asks = levels[1]

    if is_buy:
        if not asks:
            raise RuntimeError(f"Ask {coin}/USDC non disponibile")

        best_ask = float(asks[0]["px"])

        return best_ask * (1 + slippage)
    else:
        if not bids:
            raise RuntimeError(f"Bid {coin}/USDC non disponibile")

        best_bid = float(bids[0]["px"])

        return best_bid * (1 - slippage)


def spot_market_order(coin, is_buy, size):
    raw_price = get_spot_execution_price(coin, is_buy, MAX_SLIPPAGE)
    price = round_spot_price(coin, raw_price, is_buy)

    if price <= 0:
        raise RuntimeError(f"Prezzo Spot non valido: {price}")

    notional = float(size) * price

    if notional > 500000:
        raise RuntimeError(
            f"Ordine Spot bloccato: controvalore ${notional:.2f} "
            f"> limite Hyperliquid $500000."
        )

    log(
        f"ORDINE SPOT | "
        f"{coin} | "
        f"{'BUY' if is_buy else 'SELL'} | "
        f"{size:.8f} {coin} | "
        f"limite aggressivo ${price}"
    )

    result = exchange.order(
        MARKETS[coin]["market"],
        is_buy,
        size,
        price,
        {
            "limit": {
                "tif": "Ioc"
            }
        }
    )

    log(f"ORDINE SPOT RISPOSTA | {result}")

    if result.get("status") != "ok":
        raise RuntimeError(
            f"Ordine Spot rifiutato: {result}"
        )

    statuses = (
        result.get("response", {})
        .get("data", {})
        .get("statuses", [])
    )

    for status in statuses:
        if "error" in status:
            raise RuntimeError(
                f"Ordine Spot rifiutato: {status['error']}"
            )

    return result


# ============================================================
# FILLS SPOT
# ============================================================

def get_spot_fills_since(coin, start_ms):
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    fills = info.user_fills_by_time(ACCOUNT_ADDRESS, start_ms, end_ms)

    result = []

    for fill in fills:
        if fill.get("coin") == MARKETS[coin]["market"]:
            result.append(fill)

    return result


def calculate_fill(fills, is_buy):
    selected = []

    for fill in fills:
        side = fill.get("side")

        if is_buy and side == "B":
            selected.append(fill)
        elif not is_buy and side == "A":
            selected.append(fill)

    if not selected:
        return None

    total_size = 0.0
    total_notional = 0.0

    for fill in selected:
        size = abs(float(fill.get("sz", 0) or 0))
        price = float(fill.get("px", 0) or 0)

        total_size += size
        total_notional += size * price

    if total_size <= 0:
        return None

    return {
        "size": total_size,
        "price": total_notional / total_size,
        "notional": total_notional
    }


def extract_order_fill(result):
    try:
        statuses = result["response"]["data"]["statuses"]
    except (KeyError, TypeError):
        return None

    for status in statuses:
        if "filled" in status:
            filled = status["filled"]

            size = float(
                filled.get("totalSz", 0) or 0
            )
            price = float(
                filled.get("avgPx", 0) or 0
            )

            if size > 0 and price > 0:
                return {
                    "size": size,
                    "price": price,
                    "notional": size * price,
                    "oid": filled.get("oid"),
                }

        if "error" in status:
            raise RuntimeError(
                f"Ordine Spot rifiutato: {status['error']}"
            )

    return None


# ============================================================
# FEE STIMATA
# ============================================================

def estimate_fee(notional):
    try:
        fees = info.user_fees(ACCOUNT_ADDRESS)

        # per ordine IOC aggressivo
        rate = float(fees.get("userCrossRate", 0) or 0)

        return notional * rate
    except Exception:
        return 0.0


# ============================================================
# CONTROLLO POSIZIONE SPOT
# ============================================================

def verify_spot_position(state):
    balances = get_spot_balances()

    for coin in MARKETS:
        real_size = balances["coins"][coin]["total"]

        local_size = sum(float(lot["remaining_size"]) for lot in state["open_lots"] if lot["coin"] == coin)

        difference = abs(real_size - local_size)

        log(
            f"CONTROLLO SPOT | "
            f"{coin} reale {real_size:.8f} | "
            f"lotti {local_size:.8f} | "
            f"diff {difference:.8f}"
        )

        if difference > POSITION_TOLERANCE:
            raise RuntimeError(
                f"INCOERENZA {coin} SPOT: "
                f"saldo reale={real_size:.8f}, "
                f"lotti locali={local_size:.8f}. "
                "BOT BLOCCATO."
            )

    return balances


# ============================================================
# BUY SPOT
# ============================================================

def place_buy(state, coin):
    balances = get_spot_balances()

    current_size = balances["coins"][coin]["total"]

    current_price = get_spot_price(coin)

    current_position_value = current_size * current_price

    if current_position_value + BUY_USD > MAX_POSITION_USD:
        log(
            f"BUY BLOCCATO | "
            f"{coin} attuale ${current_position_value:.2f} | "
            f"BUY ${BUY_USD:.2f} | "
            f"MAX ${MAX_POSITION_USD:.2f}"
        )
        return False

    if state["weekly_buys"] >= MAX_WEEKLY_BUYS:
        log(f"BUY BLOCCATO | limite settimanale {MAX_WEEKLY_BUYS}")
        return False

    if balances["usdc_available"] < BUY_USD:
        log(
            f"BUY BLOCCATO | "
            f"USDC disponibili ${balances['usdc_available']:.4f} | "
            f"necessari ${BUY_USD:.2f}"
        )
        return False

    # size indicativa
    if current_price <= 0:
        raise RuntimeError(
            f"Prezzo {coin} Spot non valido per il BUY: {current_price}"
        )

    buy_size = round_size(coin, BUY_USD / current_price, ROUND_UP)

    if buy_size <= 0:
        log(f"BUY BLOCCATO | size {coin} non valida")
        return False

    order_start_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    result = spot_market_order(coin, True, buy_size)

    if result.get("status") != "ok":
        raise RuntimeError(f"BUY SPOT rifiutato: {result}")

    fill = extract_order_fill(result)

    if not fill:
        time.sleep(POST_ORDER_DELAY)

        for _ in range(FILL_CHECK_ATTEMPTS):
            fills = get_spot_fills_since(coin, order_start_ms - 2000)
            fill = calculate_fill(fills, True)

            if fill:
                break

            time.sleep(FILL_CHECK_DELAY)

    if not fill:
        raise RuntimeError("BUY IOC eseguito senza fill Spot rilevato.")

    actual_size = fill["size"]
    actual_price = fill["price"]
    actual_notional = fill["notional"]

    # Se la fee del BUY e' prelevata in coin, il lotto tiene la quantita'
    # realmente accreditata (variazione di saldo), altrimenti il controllo
    # posizione diverge a ogni acquisto.
    net_size = 0.0

    for _ in range(FILL_CHECK_ATTEMPTS):
        net_size = round(get_spot_balances()["coins"][coin]["total"] - current_size, 8)

        if net_size > 0:
            break

        time.sleep(FILL_CHECK_DELAY)

    if 0 < net_size < actual_size:
        actual_size = net_size

    fee = estimate_fee(actual_notional)

    target_price = actual_price * (1 + TAKE_PROFIT_PERCENT / 100)

    lot = {
        "id": state["next_lot_id"],
        "coin": coin,
        "buy_time": int(datetime.now(timezone.utc).timestamp() * 1000),
        "buy_price": actual_price,
        "buy_size": actual_size,
        "remaining_size": actual_size,
        "buy_notional": actual_notional,
        "buy_fee": fee,
        "target_price": target_price
    }

    state["next_lot_id"] += 1

    state["open_lots"].append(lot)

    state["weekly_buys"] += 1

    state["last_buy"] = lot

    # Riferimento per il prossimo giro: prezzo di questo acquisto.
    state["last_trade_price"] = actual_price

    save_state(state)

    log(
        f"BUY SPOT CONFERMATO | "
        f"lotto #{lot['id']} | "
        f"{coin} | "
        f"{actual_size:.8f} {coin} | "
        f"prezzo ${actual_price} | "
        f"investiti ${actual_notional:.4f} | "
        f"TP ${target_price}"
    )

    return True


# ============================================================
# LOTTI VENDIBILI
# ============================================================

def get_sellable_lots(state, prices):
    eligible = []

    for lot in state["open_lots"]:
        coin = lot["coin"]
        current_price = prices[coin]

        if lot["remaining_size"] <= POSITION_TOLERANCE:
            continue

        # size vendibile (multiplo di szDecimals) sotto il minimo d'ordine
        if round_size(coin, lot["remaining_size"] * SELL_PERCENT / 100) * current_price < MIN_ORDER_USD:
            continue

        if current_price >= lot["target_price"]:
            eligible.append(lot)

    # Rendimento maggiore per primo
    return sorted(eligible, key=lambda x: prices[x["coin"]] / x["buy_price"], reverse=True)


# ============================================================
# SELL SPOT
# ============================================================

def sell_lot(state, lot):
    coin = lot["coin"]

    sell_size = round_size(coin, lot["remaining_size"] * SELL_PERCENT / 100)

    if sell_size <= 0:
        return False

    current_price = get_spot_price(coin)

    if current_price < lot["target_price"]:
        return False

    balances = get_spot_balances()

    if balances["coins"][coin]["available"] + POSITION_TOLERANCE < sell_size:
        raise RuntimeError(f"{coin} Spot disponibile inferiore al lotto da vendere.")

    log(
        f"SELL SPOT | "
        f"lotto #{lot['id']} | "
        f"{sell_size:.8f} {coin} | "
        f"prezzo ${current_price} | "
        f"target ${lot['target_price']}"
    )

    order_start_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    result = spot_market_order(coin, False, sell_size)

    if result.get("status") != "ok":
        raise RuntimeError(f"SELL SPOT rifiutato: {result}")

    fill = extract_order_fill(result)

    if not fill:
        time.sleep(POST_ORDER_DELAY)

        for _ in range(FILL_CHECK_ATTEMPTS):
            fills = get_spot_fills_since(coin, order_start_ms - 2000)
            fill = calculate_fill(fills, False)

            if fill:
                break

            time.sleep(FILL_CHECK_DELAY)

    if not fill:
        raise RuntimeError("SELL IOC eseguito senza fill Spot rilevato.")

    sold_size = min(fill["size"], lot["remaining_size"])

    sell_price = fill["price"]
    sell_notional = fill["notional"]

    allocated_buy_cost = lot["buy_price"] * sold_size

    allocated_buy_fee = lot["buy_fee"] * (sold_size / lot["buy_size"])

    sell_fee = estimate_fee(sell_notional)

    gross_pnl = sell_notional - allocated_buy_cost

    net_pnl = gross_pnl - allocated_buy_fee - sell_fee

    lot["remaining_size"] = max(0.0, lot["remaining_size"] - sold_size)

    trade = {
        "lot_id": lot["id"],
        "coin": coin,
        "sell_time": int(datetime.now(timezone.utc).timestamp() * 1000),
        "sell_size": sold_size,
        "sell_price": sell_price,
        "sell_notional": sell_notional,
        "buy_cost_allocated": allocated_buy_cost,
        "buy_fee_allocated": allocated_buy_fee,
        "sell_fee": sell_fee,
        "gross_pnl": gross_pnl,
        "net_pnl": net_pnl
    }

    state["sell_trades"].append(trade)

    if lot["remaining_size"] <= POSITION_TOLERANCE:
        state["open_lots"].remove(lot)

    state["last_sell"] = trade

    # Riferimento per il prossimo giro: prezzo di questa vendita.
    state["last_trade_price"] = sell_price

    save_state(state)

    log(
        f"SELL SPOT CONFERMATO | "
        f"lotto #{lot['id']} | "
        f"{coin} | "
        f"{sold_size:.8f} {coin} | "
        f"prezzo ${sell_price} | "
        f"PnL netto ${net_pnl:.4f}"
    )

    return True


# ============================================================
# SELL CHECK
# ============================================================

def check_sell(state, prices):
    eligible = get_sellable_lots(state, prices)

    if not eligible:
        return False

    # Lotto con il rendimento maggiore (tra tutte le coin)
    lot = eligible[0]

    return sell_lot(state, lot)


# ============================================================
# BUY CHECK
# ============================================================

def check_buy(state, prices):
    drops = []

    for coin in MARKETS:
        high_24h = get_24h_high_price(coin)

        # Calcola la percentuale di ribasso rispetto al massimo a 24 ore
        drop_percent = ((high_24h - prices[coin]) / high_24h) * 100

        log(f"ANALISI 24H | {coin} | Prezzo Corrente: ${prices[coin]} | Max 24h: ${high_24h} | Ribasso: {drop_percent:.2f}%")

        drops.append((drop_percent, coin))

    # Ribasso piu' forte per primo
    drops.sort(reverse=True)

    # Dip_PERCENT impostato nel file .env (es. DIP_PERCENT=2)
    for drop_percent, coin in drops:
        if drop_percent < DIP_PERCENT:
            break

        log(f"DIP 24H RILEVATO | {coin} | Il prezzo è sceso del {drop_percent:.2f}% (>= {DIP_PERCENT:.2f}%) dal massimo 24h.")

        # Se il BUY della coin col ribasso maggiore e' bloccato (limiti), prova la successiva
        if place_buy(state, coin):
            return True

    return False



# ============================================================
# PERFORMANCE
# ============================================================

def calculate_performance(state):
    balances = get_spot_balances()

    prices = {coin: get_spot_price(coin) for coin in MARKETS}

    open_size = {coin: 0.0 for coin in MARKETS}
    open_cost_by_coin = {coin: 0.0 for coin in MARKETS}
    open_buy_fees = 0.0

    for lot in state["open_lots"]:
        coin = lot["coin"]

        size = float(lot["remaining_size"])

        open_size[coin] += size

        open_cost_by_coin[coin] += size * float(lot["buy_price"])

        if lot["buy_size"] > 0:
            open_buy_fees += float(lot["buy_fee"]) * (size / float(lot["buy_size"]))

    open_cost = sum(open_cost_by_coin.values())

    # Il valore delle coin usato nel PnL si basa solo sui lotti tracciati
    # (open_size), non sul saldo reale totale: eventuale coin non
    # tracciata (dust residuo, arrotondamenti) non deve comparire
    # come profitto gratuito nel rendimento.
    coins_value = sum(open_size[coin] * prices[coin] for coin in MARKETS)

    closed_buy_cost = sum(float(trade["buy_cost_allocated"]) for trade in state["sell_trades"])

    historical_buy_cost = open_cost + closed_buy_cost

    sold_notional = sum(float(trade["sell_notional"]) for trade in state["sell_trades"])

    realized_net = sum(float(trade["net_pnl"]) for trade in state["sell_trades"])

    unrealized_gross = coins_value - open_cost

    estimated_exit_fee = estimate_fee(coins_value)

    total_net_pnl = realized_net + unrealized_gross - open_buy_fees - estimated_exit_fee

    if historical_buy_cost > 0:
        return_percent = (total_net_pnl / historical_buy_cost) * 100
    else:
        return_percent = 0.0

    return {
        "usdc_available": balances["usdc_available"],
        "coins_value": coins_value,
        "open_cost": open_cost,
        "weighted_avg_price": {
            coin: (open_cost_by_coin[coin] / open_size[coin] if open_size[coin] > 0 else 0.0)
            for coin in MARKETS
        },
        "sold_notional": sold_notional,
        "realized_net": realized_net,
        "unrealized_gross": unrealized_gross,
        "total_net_pnl": total_net_pnl,
        "return_percent": return_percent
    }


# ============================================================
# LOG CAPITALE
# ============================================================

def log_capital():
    balances = get_spot_balances()

    holdings = " | ".join(
        f"{coin} {balances['coins'][coin]['total']:.8f} "
        f"(~${balances['coins'][coin]['total'] * get_spot_price(coin):.4f})"
        for coin in MARKETS
    )

    log(
        f"CAPITALE SPOT | "
        f"USDC disponibile ${balances['usdc_available']:.4f} | "
        f"{holdings}"
    )


# ============================================================
# MAIN
# ============================================================

def run():
    global state

    state = load_state()

    refresh_week(state)

    log("=" * 50)

    log("AVVIO BOT HYPERLIQUID SPOT MULTI-COIN")

    log(
        f"PARAMETRI | "
        f"COIN {','.join(MARKETS)} | "
        f"BUY ${BUY_USD:.2f} | "
        f"DIP {DIP_PERCENT:.2f}% | "
        f"TP {TAKE_PROFIT_PERCENT:.2f}% | "
        f"MAX PER COIN ${MAX_POSITION_USD:.2f} | "
        f"MAX BUY SETT {MAX_WEEKLY_BUYS}"
    )

    log_capital()

    # --------------------------------------------------------
    # CONTROLLO SALDO COIN
    # --------------------------------------------------------

    verify_spot_position(state)

    prices = {coin: get_spot_price(coin) for coin in MARKETS}

    log("PREZZI ATTUALI | " + " | ".join(f"{coin} ${price}" for coin, price in prices.items()))

    # --------------------------------------------------------
    # SELL PRIMA DEL BUY
    # --------------------------------------------------------

    sold = check_sell(state, prices)

    if sold:
        log("SELL SPOT eseguito: nessun BUY in questo ciclo.")

        verify_spot_position(state)

        log_capital()

        return

    # --------------------------------------------------------
    # BUY
    # --------------------------------------------------------

    bought = check_buy(state, prices)

    if bought:
        log("BUY SPOT eseguito.")
    else:
        log("Nessun BUY SPOT.")

    verify_spot_position(state)

    log_capital()

    performance = calculate_performance(state)

    avg_prices = ", ".join(f"{coin} ${price:.2f}" for coin, price in performance["weighted_avg_price"].items())

    log(
        f"PERFORMANCE SPOT | "
        f"USDC disponibile ${performance['usdc_available']:.4f} | "
        f"valore coin ${performance['coins_value']:.4f} | "
        f"investito ${performance['open_cost']:.4f} | "
        f"venduto ${performance['sold_notional']:.4f} | "
        f"media acquisto {avg_prices} | "
        f"realizzato ${performance['realized_net']:.4f} | "
        f"unrealizzato ${performance['unrealized_gross']:.4f} | "
        f"PnL totale ${performance['total_net_pnl']:.4f} | "
        f"rendimento {performance['return_percent']:.2f}%"
    )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    time.sleep(STARTUP_DELAY)

    while True:
        try:
            run()
        except Exception as e:
            log(f"ERRORE FATALE | {e}\n{traceback.format_exc()}")

        time.sleep(LOOP_INTERVAL_SECONDS)
