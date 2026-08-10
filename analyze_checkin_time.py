"""本地脚本:统计 GLaDOS 历史网络波动,给出最佳签到时段

用法:
    python analyze_checkin_time.py --cookie "<GLADOS_COOKIES>" [--json out.json] [--dow]

原理:
    - GLaDOS 新版签到奖励与全球网络质量(iqi score)挂钩,score 越低 cable 分越高
      (score<80→+8, <85→+5, <90→+3, <=95→+2, >95→+1)
    - score 由服务端计算、全局同一,是当前带宽/延迟相对"过去 12 个月分布"的百分位,
      无法被客户端 hack;唯一可控变量是签到时机
    - 本脚本拉取:
        /api/user/iqi               当前 score/带宽/延迟(作为校准点)
        /api/user/iqi/timeseries    近 ~28 天小时级带宽/延迟
        /api/user/iqi/12months      12 个月小时级带宽/延迟(百分位参照系)
      估算每小时 score,按小时聚合,输出最佳签到窗口(UTC + 北京时间)

    由于 timeseries 不含历史 score,脚本用"质量差指数(badness)"+ 已收集的
    (带宽, 延迟, 真实score) 校准点来估计历史 score。每次运行都会把当前 iqi 追加到
    ~/.glados_iqi_calib.json,校准点攒够后线性拟合自动变准,建议隔段时间手动多跑几次。
"""

import argparse
import bisect
import json
import os
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import requests

from logging_config import init_logger

logger = init_logger()

ENV_COOKIES = "GLADOS_COOKIES"
CALIB_CACHE = os.path.join(os.path.expanduser("~"), ".glados_iqi_calib.json")
BEIJING_TZ = timezone(timedelta(hours=8))

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/102.0.0.0 Safari/537.36"
)


def cable_points(score: Optional[float]) -> int:
    """根据前端 C(score) 阈值表计算 cable 积分"""
    if score is None or score <= 0:
        return 0
    if score < 80:
        return 8
    if score < 85:
        return 5
    if score < 90:
        return 3
    if score <= 95:
        return 2
    return 1


def percentile_rank(sorted_values: List[float], value: float) -> float:
    """value 在参照分布中的底部百分位(0-100)"""
    if not sorted_values:
        return 50.0
    return 100.0 * bisect.bisect_left(sorted_values, value) / len(sorted_values)


def fetch_json(session: requests.Session, domain: str, cookie: str, path: str) -> dict:
    """GET 一个 JSON 接口"""
    url = f"https://{domain}{path}"
    resp = session.get(
        url,
        headers={
            "origin": f"https://{domain}",
            "user-agent": USER_AGENT,
            "cookie": cookie,
            "accept": "application/json, text/plain, */*",
        },
        timeout=(60, 120),
    )
    resp.raise_for_status()
    return resp.json()


def load_calibration() -> List[dict]:
    """读取本地校准缓存(每次运行的 (带宽, 延迟, 真实score))"""
    if not os.path.exists(CALIB_CACHE):
        return []
    try:
        with open(CALIB_CACHE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return [p for p in data if isinstance(p, dict) and "score" in p]
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"读取校准缓存失败: {e}")
        return []


def save_calibration(points: List[dict]) -> None:
    """写入校准缓存"""
    try:
        with open(CALIB_CACHE, "w", encoding="utf-8") as f:
            json.dump(points, f, ensure_ascii=False, indent=2)
    except OSError as e:
        logger.warning(f"写入校准缓存失败: {e}")


def solve_linear_system(a: List[List[float]], b: List[float]) -> Optional[List[float]]:
    """高斯消元解 3x3 线性方程组,失败返回 None"""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        pivot = next((r for r in range(col, n) if abs(m[r][col]) > 1e-12), None)
        if pivot is None:
            return None
        m[col], m[pivot] = m[pivot], m[col]
        pv = m[col][col]
        for j in range(col, n + 1):
            m[col][j] /= pv
        for r in range(n):
            if r != col and abs(m[r][col]) > 1e-12:
                factor = m[r][col]
                for j in range(col, n + 1):
                    m[r][j] -= factor * m[col][j]
    return [m[i][n] for i in range(n)]


def fit_score_model(calib: List[dict]) -> Optional[Tuple[float, float, float]]:
    """最小二乘拟合 score = a + b*badness_bw + c*badness_lat

    校准点 >= 4 时解正规方程;3 个点时为精确解;不足返回 None(走单点锚定启发式)
    """
    if len(calib) < 3:
        return None
    rows = []
    targets = []
    for p in calib:
        if not all(k in p for k in ("badness_bw", "badness_lat", "score")):
            continue
        rows.append([1.0, p["badness_bw"], p["badness_lat"]])
        targets.append(float(p["score"]))
    if len(rows) < 3:
        return None
    n = len(rows)
    xtx = [[0.0] * 3 for _ in range(3)]
    xty = [0.0] * 3
    for row, y in zip(rows, targets):
        for i in range(3):
            xty[i] += row[i] * y
            for j in range(3):
                xtx[i][j] += row[i] * row[j]
    coeffs = solve_linear_system(xtx, xty)
    if coeffs is None:
        return None
    a, b, c = coeffs  # noqa: S001
    return (a, b, c)


def estimate_score(badness_bw: float, badness_lat: float, model: Optional[Tuple[float, float, float]], anchor: Optional[dict]) -> float:
    """估算 score:优先用拟合模型,否则用单点锚定启发式 score=100-k*badness"""
    if model is not None:
        a, b, c = model
        return max(0.0, min(100.0, a + b * badness_bw + c * badness_lat))
    if anchor is None or anchor["badness"] <= 1e-9:
        return 100.0
    slope = (100.0 - float(anchor["score"])) / anchor["badness"]
    combined = (badness_bw + badness_lat) / 2.0
    return max(0.0, min(100.0, 100.0 - slope * combined))


def to_beijing(utc_hour: int) -> int:
    return (utc_hour + 8) % 24


def format_beijing(utc_hour: int) -> str:
    bj = to_beijing(utc_hour)
    suffix = "次日" if bj < utc_hour else ""
    return f"{bj:02d}:00{suffix}"


def parse_samples(points: List[dict]) -> List[Tuple[datetime, float]]:
    """把 [{timestamp, value}] 解析为 (datetime, value)"""
    out = []
    for p in points:
        try:
            ts = datetime.fromisoformat(p["timestamp"].replace("Z", "+00:00"))
            out.append((ts, float(p["value"])))
        except (KeyError, ValueError):
            continue
    return out


def aggregate(rows: List[dict], key_fn) -> List[dict]:
    """按 key_fn 分桶聚合"""
    buckets: Dict[object, List[dict]] = {}
    for row in rows:
        buckets.setdefault(key_fn(row), []).append(row)
    result = []
    for key, items in buckets.items():
        result.append(
            {
                "key": key,
                "n": len(items),
                "avg_bw": statistics.mean([i["bw"] for i in items]),
                "avg_lat": statistics.mean([i["lat"] for i in items]),
                "avg_badness": statistics.mean([i["badness"] for i in items]),
                "avg_score": statistics.mean([i["score_est"] for i in items]),
                "hit80": 100.0 * sum(1 for i in items if i["score_est"] < 80) / len(items),
                "avg_cable": statistics.mean([i["cable"] for i in items]),
            }
        )
    return sorted(result, key=lambda r: (-r["avg_cable"], -r["hit80"], -r["avg_badness"]))


def print_table(rows: List[dict], show_dow: bool) -> None:
    if not rows:
        logger.warning("没有可用的历史样本。")
        return
    if show_dow:
        print(f"\n{'weekday':<10}{'n':>4}{'avg_bw':>8}{'avg_lat':>8}{'badness':>9}{'est_score':>10}{'cable':>6}{'<80率':>8}")
        for r in rows:
            print(f"{r['key']:<10}{r['n']:>4}{r['avg_bw']:>8.2f}{r['avg_lat']:>8.1f}{r['avg_badness']:>9.1f}{r['avg_score']:>10.1f}{r['avg_cable']:>6.1f}{r['hit80']:>7.0f}%")
    else:
        print(f"\n{'UTC时':<7}{'北京时间':<14}{'n':>4}{'avg_bw':>8}{'avg_lat':>8}{'badness':>9}{'est_score':>10}{'cable':>6}{'<80率':>8}")
        for r in rows:
            print(f"{r['key']:>3}h    {format_beijing(r['key']):<14}{r['n']:>4}{r['avg_bw']:>8.2f}{r['avg_lat']:>8.1f}{r['avg_badness']:>9.1f}{r['avg_score']:>10.1f}{r['avg_cable']:>6.1f}{r['hit80']:>7.0f}%")
    print()


def recommend(rows: List[dict], total: int, show_dow: bool) -> None:
    if not rows:
        return
    if show_dow:
        best = rows[0]
        print(f"推荐: 按星期{best['key']}签到最好(预期 {best['avg_cable']:.1f} 分/次, <80 命中 {best['hit80']:.0f}%)")
        return
    best = rows[0]
    bj = to_beijing(best["key"])
    print("推荐签到窗口(按预期 cable 分排序取 top3):")
    for r in rows[:3]:
        print(
            f"  UTC {r['key']:>2}:00 ~ {r['key'] + 1:>2}:00"
            f"  ≈ 北京时间 {format_beijing(r['key'])} ~ {format_beijing(r['key'] + 1)}"
            f"  (预期 {r['avg_cable']:.1f} 分/次, <80 命中 {r['hit80']:.0f}%, 样本 {r['n']})"
        )
    print(f"\n注意: 以上为 {total} 条近 28 天样本的统计(含 '4.20 网络问题' 故障期),")
    print("score 是相对 12 个月分布的百分位,会随参照系滚动而漂移,建议隔段时间重跑。")


def main() -> None:
    parser = argparse.ArgumentParser(description="分析 GLaDOS 历史网络波动,统计最佳签到时段")
    parser.add_argument("--cookie", default="", help="GLaDOS cookie(优先于环境变量 GLADOS_COOKIES)")
    parser.add_argument("--domain", default="glados.space", help="请求域名,默认 glados.space")
    parser.add_argument("--json", default="", help="把聚合结果另存为 JSON 文件")
    parser.add_argument("--dow", action="store_true", help="按星期几(周一~周日)聚合,替代按小时")
    args = parser.parse_args()

    cookie = args.cookie.strip() or os.environ.get(ENV_COOKIES, "").strip()
    if not cookie:
        logger.error("未提供 cookie: 请用 --cookie 传入,或设置环境变量 GLADOS_COOKIES")
        sys.exit(1)
    cookie = cookie.split("&")[0].strip()

    session = requests.Session()
    try:
        iqi = fetch_json(session, args.domain, cookie, "/api/user/iqi").get("data", {})
        timeseries = fetch_json(session, args.domain, cookie, "/api/user/iqi/timeseries").get("data", {})
        months = fetch_json(session, args.domain, cookie, "/api/user/iqi/12months").get("data", [])
    except requests.RequestException as e:
        logger.error(f"请求 GLaDOS API 失败: {e}")
        sys.exit(1)
    finally:
        session.close()

    if not months:
        logger.error("未取到 12 个月参照数据,无法分析。")
        sys.exit(1)

    # 参照系:12 个月全部小时级带宽/延迟
    ref_bw = sorted(v for _, v in parse_samples(
        [x for mo in months for x in mo.get("data", {}).get("bandwidth", [])]
    ))
    ref_lat = sorted(v for _, v in parse_samples(
        [x for mo in months for x in mo.get("data", {}).get("latency", [])]
    ))
    logger.info(f"参照系(12个月): 带宽 {len(ref_bw)} 条 / 延迟 {len(ref_lat)} 条")

    # 当前 iqi(校准点)
    quality = iqi.get("quality", {})
    cur_score = quality.get("score")
    cur_level = quality.get("level", "unknown")
    cur_bw = iqi.get("bandwidth")
    cur_lat = iqi.get("latency")
    logger.info(
        f"当前 iqi: score={cur_score} ({cur_level}), 带宽={cur_bw:.2f} Mbps, 延迟={cur_lat:.1f} ms"
    )

    # 校准点:当前 (badness_bw, badness_lat) -> 真实 score
    calib = load_calibration()
    if cur_score and cur_bw is not None and cur_lat is not None:
        cb_bw = 100.0 - percentile_rank(ref_bw, cur_bw)
        cb_lat = percentile_rank(ref_lat, cur_lat)
        cur_badness = (cb_bw + cb_lat) / 2.0
        now = int(time.time())
        # 同一个小小时内已有校准点则替换,避免重复计入
        calib = [p for p in calib if abs(now - p.get("ts", 0)) >= 3600]
        calib.append(
            {
                "ts": now,
                "bw": cur_bw,
                "lat": cur_lat,
                "badness_bw": cb_bw,
                "badness_lat": cb_lat,
                "badness": cur_badness,
                "score": cur_score,
            }
        )
        save_calibration(calib)
        logger.info(f"已追加校准点(本地 {len(calib)} 个): badness≈{cur_badness:.1f} -> score={cur_score}")

    model = fit_score_model(calib)
    anchor = calib[-1] if calib else None
    if model is not None:
        logger.info(f"score 拟合模型: score = {model[0]:.2f} {model[1]:.2f}*badness_bw {model[2]:.2f}*badness_lat")
    else:
        logger.info("校准点不足 3 个,使用单点锚定启发式估计(多跑几次会自动转线性拟合)")

    # 近 28 天小时级样本
    bw_series = parse_samples(timeseries.get("bandwidth", []))
    lat_series = parse_samples(timeseries.get("latency", []))
    lat_by_hour = {ts.replace(minute=0, second=0, microsecond=0): v for ts, v in lat_series}
    if not bw_series:
        logger.warning("timeseries 中没有带宽数据,无法分析。")
        sys.exit(1)

    rows: List[dict] = []
    for ts, bw in bw_series:
        bucket = ts.replace(minute=0, second=0, microsecond=0)
        lat = lat_by_hour.get(bucket)
        if lat is None:
            continue
        badness_bw = 100.0 - percentile_rank(ref_bw, bw)
        badness_lat = percentile_rank(ref_lat, lat)
        badness = (badness_bw + badness_lat) / 2.0
        score_est = estimate_score(badness_bw, badness_lat, model, anchor)
        rows.append(
            {
                "ts": ts,
                "bw": bw,
                "lat": lat,
                "badness": badness,
                "score_est": score_est,
                "cable": cable_points(score_est),
            }
        )
    logger.info(f"近 28 天有效小时样本: {len(rows)} 条({rows[0]['ts']:%Y-%m-%d %H:%M} UTC ~ {rows[-1]['ts']:%Y-%m-%d %H:%M} UTC)")

    if args.dow:
        table = aggregate(rows, lambda r: r["ts"].strftime("%A"))
    else:
        table = aggregate(rows, lambda r: r["ts"].hour)

    print_table(table, args.dow)
    recommend(table, len(rows), args.dow)

    if args.json:
        payload = {
            "current": {"score": cur_score, "level": cur_level, "bw": cur_bw, "lat": cur_lat},
            "model": model,
            "calib_points": len(calib),
            "show_dow": args.dow,
            "table": table,
        }
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        logger.info(f"聚合结果已写入: {args.json}")


if __name__ == "__main__":
    main()
