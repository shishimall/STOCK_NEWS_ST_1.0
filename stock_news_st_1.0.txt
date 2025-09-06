# stock_news_st

# Excelエイリアス + ニュース + Altairチャート + 配当(TTM) + 株主優待リンク + タイトル日本語社名（コード）

import os
import io
import re
import unicodedata
from pathlib import Path

import pandas as pd
import streamlit as st
import yfinance as yf
import feedparser  # type: ignore
import urllib.parse
import altair as alt

# ================= ユーティリティ =================
def _norm(s: str) -> str:
    return unicodedata.normalize("NFKC", str(s)).strip()

def _has_japanese(s: str) -> bool:
    return bool(re.search(r"[ぁ-んァ-ン一-龥ｦ-ﾟ・ｰー]", s))

DATA_DIR = Path("data")
ALIAS_PATH = DATA_DIR / "aliases.xlsx"

def _ensure_dir(p: Path):
    p.parent.mkdir(parents=True, exist_ok=True)

def _validate_alias_df(df: pd.DataFrame) -> pd.DataFrame:
    df = df.rename(columns={"Ticker": "ticker", "Alias": "alias"})
    if not {"ticker", "alias"}.issubset(df.columns):
        raise ValueError("必要列がありません。'ticker','alias' 列を用意してください。")
    df = df[["ticker", "alias"]].copy()
    df["ticker"] = df["ticker"].map(_norm)
    df["alias"] = df["alias"].map(_norm)
    df = df.dropna(subset=["ticker", "alias"])
    return df

@st.cache_data(show_spinner=False)
def load_alias_from_disk() -> pd.DataFrame:
    if ALIAS_PATH.exists():
        if ALIAS_PATH.suffix.lower() in (".xlsx", ".xls"):
            df = pd.read_excel(ALIAS_PATH)
        elif ALIAS_PATH.suffix.lower() in (".csv", ".txt"):
            df = pd.read_csv(ALIAS_PATH)
        else:
            return pd.DataFrame(columns=["ticker", "alias"])
        return _validate_alias_df(df)
    return pd.DataFrame(columns=["ticker", "alias"])

def save_uploaded_alias(uploaded_file) -> str:
    fname = uploaded_file.name.lower()
    if fname.endswith((".xlsx", ".xls")):
        df = pd.read_excel(uploaded_file)
        ext = ".xlsx"
    elif fname.endswith((".csv", ".txt")):
        df = pd.read_csv(uploaded_file)
        ext = ".csv"
    else:
        raise ValueError("対応拡張子は .xlsx/.xls/.csv/.txt です。")

    df = _validate_alias_df(df)
    _ensure_dir(ALIAS_PATH)
    target = ALIAS_PATH.with_suffix(ext)
    tmp = target.with_suffix(ext + ".tmp")

    if ext == ".xlsx":
        with pd.ExcelWriter(tmp, engine="xlsxwriter") as w:
            df.to_excel(w, index=False)
    else:
        df.to_csv(tmp, index=False, encoding="utf-8-sig")

    if target.exists():
        target.unlink()
    tmp.rename(target)

    # 旧形式掃除
    for other in (".xlsx", ".xls", ".csv", ".txt"):
        p = ALIAS_PATH.with_suffix(other)
        if p.exists() and p != target:
            try:
                p.unlink()
            except Exception:
                pass

    load_alias_from_disk.clear()
    return str(target)

def download_current_alias_button(df: pd.DataFrame):
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="xlsxwriter") as w:
        (df if not df.empty else pd.DataFrame(columns=["ticker", "alias"])).to_excel(w, index=False)
    st.download_button(
        "現在のエイリアス表をダウンロード（xlsx）",
        data=buf.getvalue(),
        file_name="aliases.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
    )

# ================= タイトル用：日本語社名を選ぶ =================
def display_name_for(code: str, alias_df: pd.DataFrame | None, info: dict | None) -> str:
    code = _norm(code)
    # 1) エイリアスから日本語らしいものを最優先
    if alias_df is not None and not alias_df.empty:
        cand = alias_df.loc[alias_df["ticker"] == code, "alias"].tolist()
        jp = [a for a in cand if _has_japanese(a)]
        if jp:
            return max(jp, key=len)
    # 2) yfinanceの社名
    if info:
        for k in ("longName", "shortName"):
            v = info.get(k)
            if v:
                return str(v)
    # 3) フォールバック：コード
    return code

# ================= ニュース取得 =================
def _aliases_for(code: str, alias_df: pd.DataFrame | None = None):
    code = _norm(code)
    aliases = {code, code.replace(".T", "")}
    info = {}
    try:
        t = yf.Ticker(code)
        try:
            info = t.get_info()
        except Exception:
            pass
        for k in ("longName", "shortName"):
            v = info.get(k)
            if v:
                aliases.add(_norm(v))
    except Exception:
        pass
    if alias_df is not None and not alias_df.empty:
        extra = alias_df.loc[alias_df["ticker"] == code, "alias"].tolist()
        for a in extra:
            if a:
                aliases.add(_norm(a))
    manual = {"7611.T": ["ハイデイ日高", "日高屋"], "5020.T": ["ＥＮＥＯＳ", "ENEOS"]}
    for v in manual.get(code, []):
        aliases.add(_norm(v))
    return [a for a in aliases if a]

def _score_title(title: str, terms: list[str], code: str) -> int:
    t = _norm(title).lower()
    score = 0
    for a in terms:
        a_norm = _norm(a).lower()
        if a_norm and a_norm in t:
            score += 2
    core = code.replace(".T", "")
    if re.search(rf"(?:\(|（|【){core}(?:\)|）|】)", t):
        score += 2
    if core in t:
        score += 1
    return score

_EXCLUDE_TERMS = ["ゲーム", "スプラ", "splatoon", "ギア", "フェス", "OCEANS", "オーシャンズ"]

def fetch_news_for(code: str, alias_df: pd.DataFrame | None, days: int = 30, max_items: int = 10, strict_title=True, min_score=2):
    terms = _aliases_for(code, alias_df=alias_df)
    quoted_terms = [f'"{t}"' for t in terms]
    must_have = "(株価 OR 決算 OR IR OR 業績 OR 出店 OR 既存店 OR 月次 OR 売上)"
    exclude = "-ゲーム -スプラ -Splatoon -ギア -任天堂 -eスポーツ -フェス -OCEANS"
    when = f"when:{days}d"
    q = f'({" OR ".join(quoted_terms)}) {must_have} {exclude} {when}'

    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode({
        "q": q, "hl": "ja", "gl": "JP", "ceid": "JP:ja",
    })
    feed = feedparser.parse(url)

    pool = []
    for e in feed.entries[: max_items * 3]:
        title = getattr(e, "title", "")
        if not title:
            continue
        low = title.lower()
        if any(x.lower() in low for x in _EXCLUDE_TERMS):
            continue
        score = _score_title(title, terms, code)
        if (not strict_title) or (score >= min_score):
            pool.append({
                "title": title,
                "link": getattr(e, "link", ""),
                "published": getattr(e, "published", ""),
                "score": score,
            })

    pool.sort(key=lambda x: (x["score"], x.get("published", "")), reverse=True)
    return pool[:max_items]

# ================= 配当（TTM + 直近） =================
def get_dividend_info(code: str, last_close: float) -> dict:
    out = {"ttm_div": None, "yield_pct": None, "recent": pd.DataFrame()}
    try:
        ticker = yf.Ticker(code)
        s = ticker.dividends
        if s is None or len(s) == 0 or float(getattr(s, "sum", lambda: 0)()) == 0:
            df_dl = yf.download(code, period="5y", interval="1d", actions=True, auto_adjust=False, progress=False)
            if isinstance(df_dl, pd.DataFrame) and "Dividends" in df_dl.columns:
                s = df_dl["Dividends"]
            else:
                s = pd.Series(dtype="float64")
        if s is None or len(s) == 0:
            return out
        s = s[s > 0]
        if len(s) == 0:
            return out
        if getattr(s.index, "tz", None) is not None:
            s.index = s.index.tz_convert(None)
        cutoff = pd.Timestamp.utcnow() - pd.Timedelta(days=365)
        cutoff = cutoff.tz_localize(None)
        ttm = s[s.index >= cutoff]
        ttm_sum = float(ttm.sum()) if len(ttm) else 0.0
        out["ttm_div"] = ttm_sum if ttm_sum > 0 else None
        if out["ttm_div"] and last_close:
            out["yield_pct"] = out["ttm_div"] / float(last_close) * 100.0
        recent = s.sort_index(ascending=False).head(5).reset_index()
        recent.columns = ["date", "dividend"]
        out["recent"] = recent
    except Exception:
        pass
    return out

# ================= UI =================
st.set_page_config(page_title="銘柄ダッシュ - プロト", layout="wide")
st.title("📈 銘柄ワンクリック要約")

# --- サイドバー: エイリアス管理 ---
st.sidebar.markdown("### エイリアス表（Excel/CSV）")
alias_df = load_alias_from_disk()
if ALIAS_PATH.exists():
    st.sidebar.caption(f"保存先: `{ALIAS_PATH}`")
else:
    st.sidebar.caption("保存先: なし（初回はアップロードして保存）")

uploaded = st.sidebar.file_uploader("エイリアス表をアップロード（xlsx/csv/txt）", type=["xlsx", "xls", "csv", "txt"])
c1, c2 = st.sidebar.columns(2)
with c1:
    if uploaded is not None and st.button("保存/差し替え", use_container_width=True):
        try:
            path = save_uploaded_alias(uploaded)
            st.sidebar.success(f"保存しました: {path}")
        except Exception as e:
            st.sidebar.error(f"保存失敗: {e}")
with c2:
    if st.button("現在の表をDL", use_container_width=True):
        st.session_state["dl_alias"] = True

if st.session_state.get("dl_alias"):
    st.info("現在のエイリアス表（xlsx）をダウンロードできます。")
    download_current_alias_button(alias_df)
    st.session_state["dl_alias"] = False

with st.expander("エイリアス表プレビュー", expanded=False):
    st.dataframe(alias_df, use_container_width=True)

# --- パラメータ ---
st.sidebar.markdown("---")
period = st.sidebar.selectbox("期間", ["1mo", "3mo", "6mo", "1y"], index=1)
interval = st.sidebar.selectbox("足", ["1d", "1wk", "1mo"], index=0)
use_zero_base = st.sidebar.checkbox("Y軸を0からにする", value=False)

code = st.text_input("証券コード or ティッカー（例：5020.T / 5108.T / 7611.T / AAPL）", "5108.T")

if st.button("生成", type="primary"):
    if not code:
        st.warning("コードを入力してください")
    else:
        ticker = yf.Ticker(code)
        df = ticker.history(period=period, interval=interval)

        info = {}
        try:
            info = ticker.get_info()
        except Exception:
            pass
        name = display_name_for(code, alias_df, info)
        title_text = f"{name}（{code}）"

        if not df.empty:
            st.subheader(f"{title_text} – チャート")
            chart_df = df.reset_index().rename(columns={"Date": "date"})
            ymin, ymax = float(df["Close"].min()), float(df["Close"].max())
            if use_zero_base:
                y_scale = alt.Scale(domain=[0, ymax * 1.05])
            else:
                y_scale = alt.Scale(domain=[ymin * 0.98, ymax * 1.02])
            line = (
                alt.Chart(chart_df)
                .mark_line()
                .encode(
                    x=alt.X("date:T", axis=alt.Axis(title=None)),
                    y=alt.Y("Close:Q", axis=alt.Axis(title=None), scale=y_scale),
                    tooltip=[
                        alt.Tooltip("date:T", title="日付"),
                        alt.Tooltip("Close:Q", title="終値", format=".2f"),
                        alt.Tooltip("Open:Q",  title="始値", format=".2f"),
                        alt.Tooltip("High:Q",  title="高値", format=".2f"),
                        alt.Tooltip("Low:Q",   title="安値", format=".2f"),
                    ],
                )
                .properties(height=260)
                .interactive()
            )
            st.altair_chart(line, use_container_width=True)
            change = (df["Close"][-1] - df["Close"][0]) / df["Close"][0] * 100
            st.caption(f"期間: {len(df)}本, 始値={df['Open'][0]:.2f}, 終値={df['Close'][-1]:.2f}, 変化率={change:+.2f}%")
        else:
            st.info("価格データが見つかりませんでした。")

        # 配当
        st.subheader("💴 配当（直近1年TTM／参考）")
        last_close = float(df["Close"][-1]) if not df.empty else None
        div_info = get_dividend_info(code, last_close if last_close else 0.0)
        if div_info["ttm_div"]:
            yld = f"{div_info['yield_pct']:.2f}%" if div_info["yield_pct"] is not None else "—"
            st.write(f"TTM配当合計: **{div_info['ttm_div']:.2f}** / 参考利回り: **{yld}**")
        else:
            st.write("配当データが見つかりませんでした。（銘柄や期間によっては yfinance から取得できないことがあります）")
        if not div_info["recent"].empty:
            st.dataframe(div_info["recent"].rename(columns={"date": "日付", "dividend": "配当(1株)"}), use_container_width=True)

        # 🔗 株主優待リンクを追加
        st.markdown(
            "🔗 株主優待情報は以下の外部サイトで確認できます：\n"
            "[株主優待を探す（kabuyutai.com）](https://www.kabuyutai.com/)"
        )

        # ニュース
        st.subheader("📰 ニュース（Google News RSS）")
        news = fetch_news_for(code, alias_df=alias_df, days=30, max_items=8, strict_title=True, min_score=2)
        if news:
            for n in news:
                st.markdown(f"- [{n['title']}]({n['link']}) 〔score={n['score']}〕")
        else:
            st.write("ニュースが見つかりませんでした。")

st.caption("Powered by Streamlit / yfinance / Google News RSS")
