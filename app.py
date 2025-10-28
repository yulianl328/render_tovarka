from flask import Flask, request, jsonify
from datetime import datetime
import os

app = Flask(__name__)

def fetch_trends_score(keyword: str, region: str, months: int = 12):
    base = {
        'gaba tea': (8.6, 'rising'),
        "oolong tea": (5.1, 'stable'),
        "lion's mane": (9.0, 'rising'),
        "ginger tea": (7.2, 'rising'),
        "keemun tea": (6.4, 'stable'),
    }
    return base.get(keyword.lower(), (6.0, 'stable'))

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
