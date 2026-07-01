import requests
from datetime import datetime, timedelta


def fetch_rate(date_str: str):
    """JPY/KRW exchange rate per 100 yen. Goes back up to 5 days on weekends."""
    date = datetime.strptime(date_str, "%Y-%m-%d")
    headers = {'User-Agent': 'Mozilla/5.0'}
    for offset in range(6):
        check = date - timedelta(days=offset)
        unix_start = int(check.timestamp())
        unix_end = unix_start + 86400
        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/JPYKRW=X"
            f"?period1={unix_start}&period2={unix_end}&interval=1d"
        )
        try:
            r = requests.get(url, headers=headers, timeout=5)
            if r.status_code != 200:
                continue
            data = r.json()
            closes = data['chart']['result'][0]['indicators']['quote'][0]['close']
            if closes and closes[0] is not None:
                return round(closes[0] * 100, 2)
        except Exception:
            continue
    return None
