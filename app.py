from flask import Flask, request, jsonify
from datetime import datetime
import os
from pytrends.request import TrendReq
import numpy as np

app = Flask(__name__)

def fetch_trends_score(keyword: str, region: str, months: int = 12):
    """
    Повертає (trend_score_0_10, trend_direction)
    На основі реальних даних Google Trends за останні N місяців.
    """
    try:
        pytrends = TrendReq(hl='uk-UA', tz=180)
        geo = _geo_from_region(region)
        timeframe = f"today {months}-m"
        kw = keyword.strip()
        if not kw:
            return (5.0, "stable")

        pytrends.build_payload([kw], timeframe=timeframe, geo=geo)
        df = pytrends.interest_over_time()

        if df.empty or kw not in df.columns:
            return (5.0, "stable")

        series = df[kw].astype(float)
        if len(series) < 3:
            return (5.0, "stable")

        # Нормалізуємо середнє та нахил
        arr = series.values
        mean_val = np.mean(arr)              # 0..100
        x = np.arange(len(arr))
        slope = np.polyfit(x, arr, 1)[0]     # нахил лінійного тренду

        # Перетворимо в 0..10:
        # середнє -> 0..10, нахил -> -2..+2 (обрізання), потім додаємо до бази
        mean_score = (mean_val / 10.0)                  # 0..10
        slope_norm = max(min(slope, 2.0), -2.0) / 2.0   # -1..+1
        base = mean_score + slope_norm

        # Легке згладжування/обрізання
        trend_score = float(max(0.0, min(10.0, round(base, 1))))
        direction = "rising" if slope > 0.25 else ("falling" if slope < -0.25 else "stable")
        return (trend_score, direction)
    except Exception:
        # На випадок лімітів чи помилок — нейтральне значення
        return (5.0, "stable")
        
def _geo_from_region(region: str) -> str:
    # UA / PL / US ... -> код для Google Trends
    # Якщо регіон невідомий — беремо глобально (geo="")
    region = (region or "").upper().strip()
    return region if len(region) in (0, 2) else ""

def fetch_keyword_metrics(keyword: str, region: str):
    demo = {
        'gaba tea': (8100, 32, 0.42, 38),
        'oolong tea': (12000, 45, 0.36, 62),
        "lion's mane": (22000, 28, 1.20, 33),
        "ginger tea": (6600, 35, 0.27, 41),
        "keemun tea": (5400, 31, 0.29, 37),
    }
    return demo.get(keyword.lower(), (2000, 40, 0.30, 45))

def score_potential(volume, kd, trend_score, competition):
    vol_norm = min(volume / 10000, 1.0) * 10.0
    kd_norm = (100 - min(max(kd, 0), 100)) / 10.0
    comp_norm = (100 - min(max(competition, 0), 100)) / 10.0
    base = (vol_norm + trend_score + kd_norm + comp_norm) / 4.0
    return round(base, 1)

@app.route('/')
def home():
    return "Server is up ✅"

@app.route('/analyze', methods=['POST'])
def analyze():
    data = request.get_json(force=True)
    keywords = data.get('keywords', [])
    config = data.get('config', {})
    trend_months = int(config.get('TREND_WINDOW_MONTHS', 12))
    now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')

    results = []
    for item in keywords:
        kw = (item.get('keyword') or '').strip()
        if not kw:
            continue
        region = item.get('region', 'UA')
        platform = (item.get('platform') or 'google').lower()

        volume, kd, cpc, competition = fetch_keyword_metrics(kw, region)
        trend_score, trend_dir = fetch_trends_score(kw, region, trend_months)
        potential = score_potential(volume, kd, trend_score, competition)
        rec = 'test' if potential >= 7.5 else 'review'

        results.append({
            'keyword': kw,
            'region': region,
            'platform': platform,
            'volume_monthly': volume,
            'kd': kd,
            'cpc_usd': round(float(cpc), 2),
            'trend_score_0_10': trend_score,
            'trend_direction': trend_dir,
            'competition_0_100': competition,
            'potential_score_0_10': potential,
            'recommendation': 'high-potential' if potential >= 8.5 else rec,
            'updated_at': now
        })

    return jsonify({'results': results})

if __name__ == '__main__':
    # Render задає PORT у змінній середовища. Ми це врахували:
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
