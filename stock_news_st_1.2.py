# stock_news_st
# 改善:
# - エイリアスのプレビューを表示＆フィルタ
# - 各行に「コピー」「挿入」ボタン（コピーは安定化、挿入は入力欄へ即反映）
# - クリアボタンは安全なフラグ方式でエラー回避
# - 入力欄と「クリア」ボタンのズレ解消（共通ラベル＋縦中央揃え）
# - 「挿入」ボタンのラベルを “<ticker> を挿入” に動的化
# - 【今回】配当TTMの堅牢化：TTM=400日許容 + 直近最大2回合計のフォールバック

import io
import re
import unicodedata
from pathlib import Path
from html import escape
import urllib.parse

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
import yfinance as yf
import feedparser  # type: ignore
import altair as alt


# ========= ユーティリティ =========
def _norm(s: str) -> str:
    return unicodedata.normalize("NFKC", str(s)).strip()

def _has_japanese(s: str) -> bool:
    return bool(re.search(r"[ぁ-んァ-ン一-龥ｦ-ﾟ・ｰー]", s))


DATA_DIR = Path("data")
ALIAS_PATH = DATA_DIR / "aliases.xlsx"

def _ensure_dir(p: Path):
    p.parent.mkdir(parents=True, exist_ok=True)

def _validate_alias_df(df: pd.DataFrame) -> pd.DataFrame:
    # 日本語/英語ヘッダー両対応
    rename_map = {
        "Ticker": "ticker", "Alias": "alias",
        "ticker": "ticker", "alias": "alias",
        "コード": "ticker", "銘柄名": "alias",
    }
    df = df.rename(columns=rename_map)
    if not {"ticker", "alias"}.issubset(df.columns):
        raise ValueError("必要列がありません。'コード/銘柄名' または 'Ticker/Alias' を用意してください。")
    df = df[["ticker", "alias"]].copy()
    df["ticker"] = df["ticker"].map(_norm)
    df["alias"]  = df["alias"].map(_norm)
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
        df = pd.read_excel(uploaded_file); ext = ".xlsx"
    elif fname.endswith((".csv", ".txt")):
        df = pd.read_csv(uploaded_file); ext = ".csv"
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

    # 旧形式の掃除
    for other in (".xlsx", ".xls", ".csv", ".txt"):
        p = ALIAS_PATH.with_suffix(other)
        if p.exists() and p != target:
            try: p.unlink()
            except Exception: pass

    load_alias_from_disk.clear()
    return str(target)

def download_current_alias_button(df: pd.DataFrame):
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="xlsxwriter") as w:
        (df if not df.empty else pd.DataFrame(columns=["ticker","alias"])).to_excel(w, index=False)
    st.download_button(
        "現在のエイリアス表をダウンロード（xlsx）",
        data=buf.getvalue(), file_name="aliases.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
    )

# ---- クリップボード：安定版（iframe内でnavigator.clipboard→フォールバック） ----
def copy_button(text: str, label: str, key: str):
    safe_text  = escape(text).replace("'", "&#39;")
    safe_label = escape(label)
    html = f"""
    <button id="{key}" style="
        padding:6px 10px;border-radius:8px;border:1px solid #555;
        background:#1f6feb;color:#fff;cursor:pointer">
        {safe_label}
    </button>
    <script>
      (function(){{
        const btn = document.getElementById("{key}");
        if (!btn) return;
        btn.addEventListener("click", async () => {{
          const val = "{safe_text}";
          async function modernCopy() {{
            try {{
              await navigator.clipboard.writeText(val);
              return true;
            }} catch(e) {{ return false; }}
          }}
          function legacyCopy(){{
            try {{
              const ta = document.createElement('textarea');
              ta.value = val;
              ta.style.position='fixed';
              ta.style.left='-9999px';
              document.body.appendChild(ta);
              ta.select();
              document.execCommand('copy');
              document.body.removeChild(ta);
              return true;
            }} catch(e) {{ return false; }}
          }}
          const ok = (navigator.clipboard && await modernCopy()) || legacyCopy();
          const prev = btn.innerText;
          btn.innerText = ok ? "コピー済" : "コピー失敗";
          setTimeout(()=>{{ btn.innerText = prev; }}, 1000);
        }});
      }})();
    </script>
    """
    components.html(html, height=42)

# ========= タイトル名（日本語優先） =========
def display_name_for(code: str, alias_df: pd.DataFrame | None, info: dict | None) -> str:
    code = _norm(code)
    if alias_df is not None and not alias_df.empty:
        cand = alias_df.loc[alias_df["ticker"] == code, "alias"].tolist()
        jp = [a for a in cand if _has_japanese(a)]
        if jp: return max(jp, key=len)
    if info:
        for k in ("longName","shortName"):
            v = info.get(k)
            if v: return str(v)
    return code

# ========= ニュース =========
def _aliases_for(code: str, alias_df: pd.DataFrame | None = None):
    code = _norm(code)
    aliases = {code, code.replace(".T","")}
    info = {}
    try:
        t = yf.Ticker(code)
        try: info = t.get_info()
        except Exception: pass
        for k in ("longName","shortName"):
            v = info.get(k)
            if v: aliases.add(_norm(v))
    except Exception:
        pass
    if alias_df is not None and not alias_df.empty:
        extra = alias_df.loc[alias_df["ticker"] == code, "alias"].tolist()
        for a in extra:
            if a: aliases.add(_norm(a))
    manual = {"7611.T": ["ハイデイ日高","日高屋"], "5020.T": ["ＥＮＥＯＳ","ENEOS"]}
    for v in manual.get(code, []):
        aliases.add(_norm(v))
    return [a for a in aliases if a]

def _score_title(title: str, terms: list[str], code: str) -> int:
    t = _norm(title).lower(); score = 0
    for a in terms:
        a_norm = _norm(a).lower()
        if a_norm and a_norm in t: score += 2
    core = code.replace(".T","")
    if re.search(rf"(?:\(|（|【){core}(?:\)|）|】)", t): score += 2
    if core in t: score += 1
    return score

_EXCLUDE_TERMS = ["ゲーム","スプラ","splatoon","ギア","フェス","OCEANS","オーシャンズ"]

def fetch_news_for(code: str, alias_df: pd.DataFrame | None, days: int = 30, max_items: int = 10,
                   strict_title=True, min_score=2):
    terms = _aliases_for(code, alias_df=alias_df)
    quoted_terms = [f'"{t}"' for t in terms]
    must_have = "(株価 OR 決算 OR IR OR 業績 OR 出店 OR 既存店 OR 月次 OR 売上)"
    exclude   = "-ゲーム -スプラ -Splatoon -ギア -eスポーツ -フェス -OCEANS"
    q = f'({" OR ".join(quoted_terms)}) {must_have} {exclude} when:{days}d'

    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode({
        "q": q, "hl":"ja", "gl":"JP", "ceid":"JP:ja",
    })
    feed = feedparser.parse(url)

    pool = []
    for e in feed.entries[: max_items*3]:
        title = getattr(e,"title","")
        if not title: continue
        low = title.lower()
        if any(x.lower() in low for x in _EXCLUDE_TERMS): continue
        score = _score_title(title, terms, code)
        if (not strict_title) or (score >= min_score):
            pool.append({
                "title": title,
                "link": getattr(e,"link",""),
                "published": getattr(e,"published",""),
                "score": score,
            })
    pool.sort(key=lambda x: (x["score"], x.get("published","")), reverse=True)
    return pool[:max_items]

# ========= 配当（TTM強化版） =========
def get_dividend_info(code: str, last_close: float, ttm_days: int = 400) -> dict:
    """
    - yfinanceのdividends（Series, index=Datetime）を取得
    - TTMは過去ttm_days（デフォ400日）で合計
    - TTM=0なら『直近最大2回の合計』を代替として返す（日本株の年2回/年1回対策）
    """
    out = {
        "ttm_div": None, "yield_pct": None,
        "recent": pd.DataFrame(),
        "alt_div": None, "alt_yield": None,
        "method": "none"
    }
    try:
        ticker = yf.Ticker(code)
        s = ticker.dividends

        # 代替ダウンロード（actions=Trueで配当列確保）
        if s is None or len(s)==0 or float(getattr(s,"sum",lambda:0)())==0:
            df_dl = yf.download(code, period="5y", interval="1d",
                                actions=True, auto_adjust=False, progress=False)
            if isinstance(df_dl, pd.DataFrame) and "Dividends" in df_dl.columns:
                s = df_dl["Dividends"]
            else:
                s = pd.Series(dtype="float64")

        if s is None or len(s)==0:
            return out

        s = s[s > 0]
        if len(s)==0:
            return out

        # タイムゾーン無効化（あれば）
        if getattr(s.index, "tz", None) is not None:
            s.index = s.index.tz_convert(None)

        now = pd.Timestamp.utcnow().tz_localize(None)
        ttm = s[s.index >= now - pd.Timedelta(days=ttm_days)]
        ttm_sum = float(ttm.sum()) if len(ttm) else 0.0

        if ttm_sum > 0:
            out["ttm_div"] = ttm_sum
            out["method"] = "ttm"
        else:
            # フォールバック：直近最大2回（日本株の半期配当/年1回にも対応）
            last_two = s.sort_index(ascending=False).head(2)
            alt_sum = float(last_two.sum()) if len(last_two) else 0.0
            if alt_sum > 0:
                out["alt_div"] = alt_sum
                out["method"] = "fallback_last2"

        # 利回り計算
        if last_close:
            if out["ttm_div"] is not None:
                out["yield_pct"] = out["ttm_div"] / float(last_close) * 100.0
            if out["alt_div"] is not None:
                out["alt_yield"] = out["alt_div"] / float(last_close) * 100.0

        # 直近履歴（見やすさ優先で8件）
        recent = s.sort_index(ascending=False).head(8).reset_index()
        recent.columns = ["date","dividend"]
        out["recent"] = recent
    except Exception:
        pass
    return out


# ========= UI =========
st.set_page_config(page_title="銘柄ダッシュ - プロト", layout="wide")
st.title("📈 銘柄ワンクリック要約")

# サイドバー: エイリアス管理
st.sidebar.markdown("### エイリアス表（Excel/CSV）")
alias_df = load_alias_from_disk()
if ALIAS_PATH.exists():
    st.sidebar.caption(f"保存先: `{ALIAS_PATH}`")
else:
    st.sidebar.caption("保存先: なし（初回はアップロードして保存）")

uploaded = st.sidebar.file_uploader("エイリアス表をアップロード（xlsx/csv/txt）", type=["xlsx","xls","csv","txt"])
c1, c2 = st.sidebar.columns(2)
with c1:
    if uploaded is not None and st.sidebar.button("保存/差し替え", use_container_width=True):
        try:
            path = save_uploaded_alias(uploaded)
            st.sidebar.success(f"保存しました: {path}")
        except Exception as e:
            st.sidebar.error(f"保存失敗: {e}")
with c2:
    if st.sidebar.button("現在の表をDL", use_container_width=True):
        st.session_state["dl_alias"] = True
if st.session_state.get("dl_alias"):
    st.info("現在のエイリアス表（xlsx）をダウンロードできます。")
    download_current_alias_button(alias_df)
    st.session_state["dl_alias"] = False

# ===== 入力欄：クリア/挿入フラグの適用を“描画前”にやる =====
if "code_input" not in st.session_state:
    st.session_state["code_input"] = "5108.T"

# クリア要求フラグ
if st.session_state.get("clear_code_flag"):
    st.session_state["code_input"] = ""
    st.session_state["clear_code_flag"] = False

# 挿入要求フラグ
if "pending_code" in st.session_state:
    st.session_state["code_input"] = st.session_state["pending_code"]
    del st.session_state["pending_code"]

# ===== 入力欄＆クリアボタン（ズレ解消：共通ラベル＋縦中央揃え） =====
st.markdown("**証券コード or ティッカー（例：5020.T / 5108.T / 7611.T / AAPL）**")
col_code, col_clear = st.columns([8, 2], vertical_alignment="center")
with col_code:
    st.text_input(
        label="コード",
        key="code_input",
        label_visibility="collapsed",   # ラベルは上のmarkdownに集約
        placeholder="例）5108.T",
    )
with col_clear:
    if st.button("クリア", use_container_width=True):
        st.session_state["clear_code_flag"] = True
        st.rerun()

# ===== エイリアス検索＆プレビュー（フィルタ＋コピー/挿入） =====
st.subheader("🔎 エイリアス検索 & プレビュー")
search_q = st.text_input("キーワードでフィルタ（コード/銘柄名の部分一致）", value="", key="alias_search_q")

filtered_df = alias_df
if search_q:
    qn = _norm(search_q).lower()
    filtered_df = alias_df[alias_df.apply(
        lambda r: qn in str(r["ticker"]).lower() or qn in str(r["alias"]).lower(), axis=1
    )]

with st.expander("エイリアス表プレビュー（フィルタ適用）", expanded=False):
    st.dataframe(filtered_df, use_container_width=True, hide_index=True)
    if not filtered_df.empty:
        st.markdown("**クイック操作（先頭50件）**")
        for i, row in filtered_df.head(50).iterrows():
            col1, col2, col3, col4 = st.columns([2, 5, 2, 2], vertical_alignment="center")
            with col1:
                st.write(f"{row['ticker']}")
            with col2:
                st.write(f"{row['alias']}")
            with col3:
                copy_button(row["ticker"], "コピー", key=f"copy-{i}")
            with col4:
                if st.button(f"{row['ticker']} を挿入", key=f"ins-{i}", use_container_width=True):
                    st.session_state["pending_code"] = row["ticker"]
                    st.rerun()
    else:
        st.info("一致する銘柄が見つかりませんでした。")

# 表示パラメータ
st.sidebar.markdown("---")
period = st.sidebar.selectbox("期間", ["1mo","3mo","6mo","1y"], index=1)
interval = st.sidebar.selectbox("足", ["1d","1wk","1mo"], index=0)
use_zero_base = st.sidebar.checkbox("Y軸を0からにする", value=False)

# ===== メイン処理 =====
if st.button("生成", type="primary"):
    code = st.session_state.get("code_input","").strip()
    if not code:
        st.warning("コードを入力してください")
    else:
        ticker = yf.Ticker(code)
        df = ticker.history(period=period, interval=interval)

        info = {}
        try: info = ticker.get_info()
        except Exception: pass
        name = display_name_for(code, alias_df, info)
        title_text = f"{name}（{code}）"

        if not df.empty:
            st.subheader(f"{title_text} – チャート")
            copy_button(code, "コードをコピー", key="title-copy")

            chart_df = df.reset_index().rename(columns={"Date":"date"})
            ymin, ymax = float(df["Close"].min()), float(df["Close"].max())
            y_scale = alt.Scale(domain=[0, ymax*1.05]) if use_zero_base else alt.Scale(domain=[ymin*0.98, ymax*1.02])
            line = (
                alt.Chart(chart_df)
                .mark_line()
                .encode(
                    x=alt.X("date:T", axis=alt.Axis(title=None)),
                    y=alt.Y("Close:Q", axis=alt.Axis(title=None), scale=y_scale),
                    tooltip=[
                        alt.Tooltip("date:T",  title="日付"),
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
            change = (df["Close"][-1]-df["Close"][0]) / df["Close"][0] * 100
            st.caption(f"期間: {len(df)}本, 始値={df['Open'][0]:.2f}, 終値={df['Close'][-1]:.2f}, 変化率={change:+.2f}%")
        else:
            st.info("価格データが見つかりませんでした。")

        # 配当
        st.subheader("💴 配当（直近1年TTM／参考）")
        last_close = float(df["Close"][-1]) if not df.empty else 0.0
        div_info = get_dividend_info(code, last_close)

        if div_info["ttm_div"] is not None:
            yld = f"{div_info['yield_pct']:.2f}%" if div_info['yield_pct'] is not None else "—"
            st.write(f"TTM配当合計: **{div_info['ttm_div']:.2f}** / 参考利回り: **{yld}**（方法: TTM {400}日）")
        elif div_info["alt_div"] is not None:
            alt_yld = f"{div_info['alt_yield']:.2f}%" if div_info['alt_yield'] is not None else "—"
            st.write(f"TTM配当合計: — / 代替（直近最大2回の合計）: **{div_info['alt_div']:.2f}** / 参考利回り: **{alt_yld}**")
        else:
            st.write("配当データが見つかりませんでした。")

        if not div_info["recent"].empty:
            st.dataframe(
                div_info["recent"].rename(columns={"date":"日付","dividend":"配当(1株)"}),
                use_container_width=True
            )

        # 株主優待リンク
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
