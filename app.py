from flask import Flask, request, jsonify
from datetime import datetime
import os
from pytrends.request import TrendReq
import numpy as np
import os
import math
from typing import Tuple, List
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException
import logging
log = logging.getLogger("pf")
logging.basicConfig(level=logging.INFO)
# Language & Geo для України
# Ukrainian language constant: 1029  -> "languageConstants/1029"
# Ukraine geo target ID:       2276  -> "geoTargetConstants/2276"
GA_LANGUAGE_UA = "languageConstants/1029"
GA_GEO_UA = "geoTargetConstants/2276"

# Кеш клієнта, щоб не створювати щоразу
_google_ads_client = None

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

        # Якщо зовсім порожньо або даних < 10 — вважаємо, що недостатньо
        if df.empty or kw not in df.columns or len(df) < 10:
            return (5.0, "stable")

        series = df[kw].astype(float)
        # Якщо середнє менше 2 — даних реально замало (низький інтерес)
        if series.mean() < 2:
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

def _load_google_ads_client() -> GoogleAdsClient:
    global _google_ads_client
    if _google_ads_client is not None:
        return _google_ads_client

    dev   = (os.getenv("GOOGLE_ADS_DEVELOPER_TOKEN")   or "").strip()
    cid   = (os.getenv("GOOGLE_ADS_CLIENT_ID")         or "").strip()
    csec  = (os.getenv("GOOGLE_ADS_CLIENT_SECRET")     or "").strip()
    rtok  = (os.getenv("GOOGLE_ADS_REFRESH_TOKEN")     or "").strip()
    login = (os.getenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID") or "").strip()

    def mask(v): return f"{len(v)}:{v[-4:] if len(v)>=4 else v}"
    log.info(f"[ADS][ENV] dev={mask(dev)} cid={mask(cid)} csec={mask(csec)} rtok={mask(rtok)} login_cust={login}")

    if not dev:
        raise RuntimeError("[ADS] Missing GOOGLE_ADS_DEVELOPER_TOKEN")
    if not (cid and csec and rtok):
        raise RuntimeError("[ADS] Missing OAuth2 creds: CLIENT_ID / CLIENT_SECRET / REFRESH_TOKEN")

    # 👉 ПЛОСКА конфігурація (без 'oauth2' всередині)
    cfg = {
        "developer_token": dev,
        "client_id": cid,
        "client_secret": csec,
        "refresh_token": rtok,
        "use_proto_plus": True,
    }
    if login:
        cfg["login_customer_id"] = login  # ID MCC (без дефісів)

    _google_ads_client = GoogleAdsClient.load_from_dict(cfg)
    log.info("[ADS] Client loaded OK")
    return _google_ads_client
    
def _competition_to_0_100(comp_enum_val: int) -> int:
    """
    Перемаплює Google Ads competition enum у 0..100:
    LOW=0, MEDIUM=50, HIGH=100, UNSPECIFIED/UNKNOWN=25
    """
    # Enum у різних версіях трохи відрізняється, але значення зводимо вручну:
    # 0-UNSPECIFIED, 1-UNKNOWN, 2-LOW, 3-MEDIUM, 4-HIGH
    mapping = {2: 20, 3: 60, 4: 90, 0: 25, 1: 25}
    return mapping.get(comp_enum_val, 50)

def micros_to_usd(micros: int) -> float:
    # Google повертає ставки у мікро-валюті
    if micros is None:
        return 0.0
    return round(float(micros) / 1_000_000.0, 2)

def fetch_keyword_metrics(keyword: str, region: str) -> Tuple[int, int, float, int]:
    """
    Реальні дані з Google Ads Keyword Plan Idea Service:
    - volume_monthly
    - kd            (беремо як proxy: конкуренція у 0..100)
    - cpc_usd       (Top of page bid high)
    - competition_0_100
    """
    try:
        client = _load_google_ads_client()
        customer_id = os.getenv("GOOGLE_ADS_CUSTOMER_ID")
        if not customer_id:
            raise RuntimeError("GOOGLE_ADS_CUSTOMER_ID is not set")

        srv = client.get_service("KeywordPlanIdeaService")
        req = client.get_type("GenerateKeywordIdeasRequest")

        # Країна: тільки UA зараз. Якщо region != 'UA', можна додати мапу.
        req.customer_id = customer_id
        req.language = GA_LANGUAGE_UA
        req.geo_target_constants.append(GA_GEO_UA)
        req.keyword_plan_network = client.enums.KeywordPlanNetworkEnum.GOOGLE_SEARCH_AND_PARTNERS

        # Джерело ідей — сам ключ
        req.keyword_seed.keywords.append(keyword)

        resp = srv.generate_keyword_ideas(request=req)

        # Беремо перший збіг по ключу (або найрелевантніший)
        best = None
        for idea in resp:
            text = idea.text or ""
            metrics = idea.keyword_idea_metrics
            if not metrics:
                continue

            avg = metrics.avg_monthly_searches or 0
            comp_enum = int(metrics.competition) if metrics.competition is not None else 1
            comp_0_100 = _competition_to_0_100(comp_enum)
            cpc_high = micros_to_usd(metrics.high_top_of_page_bid_micros)
            # простий пріоритет — за найбільшим avg_monthly_searches
            row = (avg, comp_0_100, cpc_high, text)
            if best is None or row[0] > best[0]:
                best = row

        if best is None:
            return (0, 0, 0.0, 0)

        volume = int(best[0])
        competition_0_100 = int(best[1])
        cpc_usd = float(best[2])

        # kd як proxy: можна дорівняти конкуренції або трансформувати
        kd = competition_0_100

        return (volume, kd, cpc_usd, competition_0_100)

    except GoogleAdsException as gae:
        # Лог у консолі Render
        print(f"GoogleAdsException: {gae}")
        return (0, 0, 0.0, 0)
    except Exception as e:
        print(f"fetch_keyword_metrics error for '{keyword}': {e}")
        return (0, 0, 0.0, 0)
        
def fetch_keyword_metrics_variant(keyword: str, lang_const: str, network_name: str):
    try:
        client = _load_google_ads_client()
        customer_id = os.getenv("GOOGLE_ADS_CUSTOMER_ID")
        srv = client.get_service("KeywordPlanIdeaService")
        req = client.get_type("GenerateKeywordIdeasRequest")
        req.customer_id = customer_id
        req.language = lang_const
        req.geo_target_constants.append(GA_GEO_UA)
        net_enum = client.enums.KeywordPlanNetworkEnum
        req.keyword_plan_network = getattr(net_enum, network_name)
        req.keyword_seed.keywords.append(keyword)

        best = None
        cnt = 0
        for idea in srv.generate_keyword_ideas(request=req):
            cnt += 1
            m = idea.keyword_idea_metrics
            if not m: 
                continue
            avg = m.avg_monthly_searches or 0
            comp = _competition_to_0_100(int(m.competition) if m.competition is not None else 1)
            cpc = micros_to_usd(m.high_top_of_page_bid_micros)
            row = (avg, comp, cpc)
            if best is None or row[0] > best[0]:
                best = row
        # повернемо нулі, якщо ідей 0
        return (0,0,0) if best is None else (int(best[0]), int(best[1]), float(best[2]))
    except Exception:
        return (0,0,0)


def score_potential(volume, kd, trend_score, competition):
    # volume: 0..∞ → нормалізуємо логарифмічно
    vol_norm = min(math.log10(max(volume, 1) + 1) * 3.3, 10.0)  # 0..~10
    kd_norm = (100 - min(max(kd, 0), 100)) / 10.0               # 0..10 (низька складність = краще)
    comp_norm = (100 - min(max(competition, 0), 100)) / 10.0    # 0..10
    # Ваги: volume 45%, trend 35%, конкуренція 20%
    score = (vol_norm * 0.45) + (trend_score * 0.35) + (comp_norm * 0.20)
    return round(min(max(score, 0.0), 10.0), 1)

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

@app.route('/selftest', methods=['GET'])
def selftest():
    out = {"env": {}, "ads": {}, "trends": {}}
    keys = ["GOOGLE_ADS_DEVELOPER_TOKEN","GOOGLE_ADS_CLIENT_ID","GOOGLE_ADS_CLIENT_SECRET",
            "GOOGLE_ADS_REFRESH_TOKEN","GOOGLE_ADS_CUSTOMER_ID","GOOGLE_ADS_LOGIN_CUSTOMER_ID"]
    for k in keys:
        out["env"][k] = bool(os.getenv(k))

    try:
        client = _load_google_ads_client()
        # 1) Список доступних акаунтів під цим OAuth
        svc = client.get_service("CustomerService")
        custs = [c for c in svc.list_accessible_customers().resource_names]
        out["ads"]["accessible_customers"] = custs  # вигляд 'customers/1234567890'

        # 2) Перевіримо GAQL на customer для вказаного CUSTOMER_ID
        ga = client.get_service("GoogleAdsService")
        customer_id = os.getenv("GOOGLE_ADS_CUSTOMER_ID")
        query = "SELECT customer.id, customer.descriptive_name FROM customer LIMIT 1"
        rows = ga.search(customer_id=customer_id, query=query)
        out["ads"]["gaql_customer_probe"] = [
            {"id": r.customer.id, "name": r.customer.descriptive_name} for r in rows
        ]
    except Exception as e:
        out["ads"]["access_error"] = str(e)

    # 3) Спробуємо Keyword Ideas різними налаштуваннями
    try:
       out["ads"]["ideas"] = {}
       for lang in ("languageConstants/1029", "languageConstants/1000"):
           for net in ("GOOGLE_SEARCH", "GOOGLE_SEARCH_AND_PARTNERS"):
               vol, comp, cpc = fetch_keyword_metrics_variant("weather", lang, net)
               out["ads"]["ideas"][f"{lang}_{net}"] = {"vol": vol, "cpc": cpc, "comp": comp}
    except Exception as e:
        out["ads"]["ideas_error"] = str(e)

    # Trends як було
    try:
        ts, td = fetch_trends_score("купити чай", "UA", 12)
        out["trends"]["sample"] = {"kw":"купити чай","score": ts,"dir": td}
    except Exception as e:
        out["trends"]["error"] = str(e)

    return jsonify(out)




if __name__ == '__main__':
    # Render задає PORT у змінній середовища. Ми це врахували:
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
