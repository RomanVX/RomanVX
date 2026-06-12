from __future__ import annotations
from typing import Any
import pandas as pd


def _sales_df(sales: list[dict]) -> pd.DataFrame:
    if not sales:
        return pd.DataFrame(columns=["date", "nmId", "subject", "finishedPrice", "forPay"])
    df = pd.DataFrame(sales)
    df["date"] = pd.to_datetime(df["date"]).dt.normalize()
    return df


def _orders_df(orders: list[dict]) -> pd.DataFrame:
    if not orders:
        return pd.DataFrame(columns=["date", "nmId", "isCancel"])
    df = pd.DataFrame(orders)
    df["date"] = pd.to_datetime(df["date"]).dt.normalize()
    return df


def _stocks_df(stocks: list[dict]) -> pd.DataFrame:
    if not stocks:
        return pd.DataFrame(columns=["nmId", "subject", "quantity", "Price"])
    return pd.DataFrame(stocks)


def kpi_summary(sales: list[dict], orders: list[dict], stocks: list[dict]) -> dict[str, Any]:
    sdf = _sales_df(sales)
    odf = _orders_df(orders)
    stdf = _stocks_df(stocks)
    total_revenue = float(sdf["forPay"].sum()) if not sdf.empty else 0.0
    total_sales = len(sdf)
    total_orders = len(odf)
    buyout_rate = (total_sales / total_orders * 100) if total_orders > 0 else 0.0
    stock_value = 0.0
    if not stdf.empty:
        qty_col = "quantity" if "quantity" in stdf.columns else "quantityFull"
        price_col = "Price" if "Price" in stdf.columns else "totalPrice"
        if qty_col in stdf.columns and price_col in stdf.columns:
            stock_value = float((stdf[qty_col] * stdf[price_col]).sum())
    return {
        "total_revenue": round(total_revenue, 2),
        "total_sales": total_sales,
        "total_orders": total_orders,
        "buyout_rate": round(buyout_rate, 1),
        "stock_value": round(stock_value, 2),
    }


def sales_dynamics(sales: list[dict], orders: list[dict]) -> list[dict]:
    sdf = _sales_df(sales)
    odf = _orders_df(orders)
    if sdf.empty:
        return []
    daily_rev = sdf.groupby("date")["forPay"].sum().reset_index()
    daily_rev.columns = ["date", "revenue"]
    daily_cnt = sdf.groupby("date").size().reset_index(name="sales_count")
    merged = daily_rev.merge(daily_cnt, on="date", how="outer").fillna(0)
    if not odf.empty:
        daily_orders = odf.groupby("date").size().reset_index(name="orders_count")
        merged = merged.merge(daily_orders, on="date", how="outer").fillna(0)
    else:
        merged["orders_count"] = 0
    merged = merged.sort_values("date")
    merged["date"] = merged["date"].dt.strftime("%Y-%m-%d")
    return merged.to_dict(orient="records")


def calc_daily_sales(sales: list[dict], days: int) -> pd.DataFrame:
    sdf = _sales_df(sales)
    if sdf.empty:
        return pd.DataFrame(columns=["nmId", "subject", "avg_daily_sales", "total_revenue", "total_qty"])
    by_sku = sdf.groupby(["nmId", "subject"]).agg(
        total_revenue=("forPay", "sum"),
        total_qty=("nmId", "count"),
    ).reset_index()
    by_sku["avg_daily_sales"] = (by_sku["total_qty"] / days).round(2)
    return by_sku


def abc_by_revenue(sales: list[dict], days: int) -> list[dict]:
    df = calc_daily_sales(sales, days)
    if df.empty:
        return []
    df = df.sort_values("total_revenue", ascending=False).reset_index(drop=True)
    total = df["total_revenue"].sum()
    df["revenue_share"] = (df["total_revenue"] / total * 100).round(2)
    df["cumulative_share"] = df["revenue_share"].cumsum().round(2)
    def _cat(cum):
        if cum <= 80: return "A"
        if cum <= 95: return "B"
        return "C"
    df["abc_category"] = df["cumulative_share"].apply(_cat)
    df["total_revenue"] = df["total_revenue"].round(2)
    return df.to_dict(orient="records")


def abc_by_turnover(sales: list[dict], stocks: list[dict], days: int) -> list[dict]:
    daily = calc_daily_sales(sales, days)
    stdf = _stocks_df(stocks)
    if daily.empty or stdf.empty:
        return []
    qty_col = "quantity" if "quantity" in stdf.columns else "quantityFull"
    stock_agg = stdf.groupby("nmId").agg(stock_qty=(qty_col, "sum")).reset_index()
    merged = daily.merge(stock_agg, on="nmId", how="outer").fillna(0)
    def _coverage(row):
        if row["avg_daily_sales"] <= 0: return 999.0
        return min(999.0, round(row["stock_qty"] / row["avg_daily_sales"], 1))
    merged["coverage_days"] = merged.apply(_coverage, axis=1)
    merged = merged.sort_values("coverage_days").reset_index(drop=True)
    n = len(merged)
    merged["rank_pct"] = (merged.index + 1) / n * 100
    def _cat(pct):
        if pct <= 50: return "A"
        if pct <= 80: return "B"
        return "C"
    merged["abc_category"] = merged["rank_pct"].apply(_cat)
    merged["coverage_days"] = merged["coverage_days"].round(1)
    merged["stock_qty"] = merged["stock_qty"].astype(int)
    return merged[["nmId", "subject", "stock_qty", "avg_daily_sales", "coverage_days", "abc_category"]].to_dict(orient="records")


def reorder_forecast(sales: list[dict], stocks: list[dict], days: int) -> list[dict]:
    daily = calc_daily_sales(sales, days)
    stdf = _stocks_df(stocks)
    if daily.empty:
        return []
    qty_col = "quantity" if "quantity" in stdf.columns else "quantityFull"
    if not stdf.empty:
        stock_agg = stdf.groupby("nmId")[qty_col].sum().reset_index()
        stock_agg.columns = ["nmId", "stock_qty"]
    else:
        stock_agg = pd.DataFrame({"nmId": daily["nmId"], "stock_qty": 0})
    merged = daily.merge(stock_agg, on="nmId", how="left").fillna(0)
    for d in [30, 60, 90]:
        merged[f"need_{d}d"] = (merged["avg_daily_sales"] * d - merged["stock_qty"]).clip(lower=0).round(0).astype(int)
    merged["stock_qty"] = merged["stock_qty"].astype(int)
    return merged[["nmId", "subject", "stock_qty", "avg_daily_sales", "need_30d", "need_60d", "need_90d"]].to_dict(orient="records")


def top_skus(sales: list[dict], days: int, n: int = 20) -> list[dict]:
    df = calc_daily_sales(sales, days)
    if df.empty:
        return []
    df = df.sort_values("total_revenue", ascending=False).head(n)
    df["total_revenue"] = df["total_revenue"].round(2)
    return df.to_dict(orient="records")
