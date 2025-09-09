# stock_news_st

# -*- coding: utf-8 -*-
"""
銘柄ワンクリック要約（Google Sheets 連携・エイリアス正本）
- エイリアス表は Google スプレッドシートを正本（優先）
- GS が空/接続不可ならローカル aliases.xlsx に自動フォールバック
- アップロード保存時はローカル→GS 同期を試行（失敗時はローカルのみ）
- サイドバーに「GS→ローカル」「ローカル→GS」「GS再読込」ボタン
- 日本語検出はコードポイント範囲で安全に（文字化け耐性）
- ワークシート名が合わない時は**先頭タブにフォールバック**
- ★ 認証改善：JSONファイル直読み→secrets の順に試行（テストと同じ手順を最優先）
- ★ 設定改善：sheet_id / worksheet は secrets が無ければフォールバック定数を使用
"""

from __future__ import annotations

import io
import os
import re
import unicodedata
from pathlib import Path
from html import escape
import urllib.parse
from typing import Tuple

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
import yfinance as yf
import feedparser  # type: ignore
import altair as alt

# === Google Sheets ===
import gspread  # type: ignore
from gspread.exceptions import WorksheetNotFound  # フォールバック用
from google.oauth2.service_account import Credentials  # type: ignore

# ========= 基本設定 =========
st.set_page_config(page_title="銘柄ダッシュ - GS同期", layout="wide")

DATA_DIR = Path("data")
ALIAS_PATH = DATA_DIR / "aliases.xlsx"  # ローカルのフォールバック保存先
DEFAULT_COLUMNS = ["ticker", "alias"]
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# ========= ★ フォールバック設定（あなたの環境に合わせて変更可） =========
# テストで通った JSON ファイルのフルパス（存在すればこれを最優先）
SERVICE_ACCOUNT_JSON_PATH = r"C:\STOCK_NEWS_ST_1.0\stock-news-st-f1bc5054f582.json"
# 環境変数からも指定可能（優先度：この環境変数 > 上の定数）
ENV_JSON_PATH_KEY = "SERVICE_ACCOUNT_JSON_PATH"

# secrets が無い/未設定でも動くようにフォールバック用のシート情報
SHEET_ID_FALLBACK = "1wfjQnNGJhmhZI7bZ3lK_dIjrpi8Ilwn8Es_9X0vQFqo"
WORKSHEET_NAME_FALLBACK = "aliases"

def _cfg_sheet_id() -> str:
    try:
        return st.secrets["gsheet"]["sheet_id"]
    except Exception:
        return SHEET_ID_FALLBACK

def _cfg_worksheet_name() -> str:
    try:
        return st.secrets["gsheet"].get("worksheet", "aliases")
    except Exception:
        return WORKSHEET_NAME_FALLBACK

# ========= ユーティリティ =========
def _norm(s: str) -> str:
    return unicodedata.normalize("NFKC", str(s)).strip()

def _has_japanese(s: str) -> bool:
    """文字化けに強い日本語検出（コードポイント判定）"""
    for ch in str(s):
        cp = ord(ch)
        if (
            0x3040 <= cp <= 0x309F   # ひらがな
            or 0x30A0 <= cp <= 0x30FF  # カタカナ
            or 0x4E00 <= cp <= 0x9FFF  # CJK統合漢字
            or 0xFF66 <= cp <= 0xFF9D  # 半角ｶﾅ
            or cp in (0x30FB, 0x30FC, 0xFF70)  # ・, ー, ｰ
        ):
            return True
    return False

def _ensure_dir(p: Path):
    p.parent.mkdir(parents=True, exist_ok=True)

def _validate_alias_df(df: pd.DataFrame) -> pd.DataFrame:
    """列名・型を標準化し、重複/欠損を整理（ヘッダーの揺れにもある程度耐性）"""
    if df is None or df.empty:
        return pd.DataFrame(columns=DEFAULT_COLUMNS)

    df = df.copy()

    # 列名カノナイズ（空白・記号除去、前方一致許容）
    def _canon(c: str) -> str:
        c = unicodedata.normalize("NFKC", str(c)).strip().lower()
        c = re.sub(r"[\s\-\_\.\(\)　]+", "", c)
        return c

    col_map: dict[str, str] = {}
    for c in list(df.columns):
        key = _canon(c)
        if key.startswith("ticker") or key in {"code", "ティッカー", "コード"}:
            col_map[c] = "ticker"
        elif key.startswith("alias") or key in {"name", "エイリアス", "銘柄名"}:
            col_map[c] = "alias"

    df.columns = [col_map.get(c, c) for c in df.columns]

    for col in DEFAULT_COLUMNS:
        if col not in df.columns:
            df[col] = ""

    df = df[DEFAULT_COLUMNS].copy()
    df["ticker"] = df["ticker"].map(_norm)
    df["alias"]  = df["alias"].map(_norm)

    # 空ticker行は削除、重複は最後採用
    df = df[df["ticker"] != ""]
    df = df.drop_duplicates(subset=["ticker"], keep="last").reset_index(drop=True)
    return df

def _read_any_to_df(uploaded_file) -> pd.DataFrame:
    """xlsx/csv/txt(タブ/カンマ)に対応してDataFrame化"""
    fname = uploaded_file.name.lower()
    try:
        if fname.endswith((".xlsx", ".xls")):
            return pd.read_excel(uploaded_file)
        elif fname.endswith((".csv", ".txt")):
            # カンマ → タブの順に試す
            try:
                return pd.read_csv(uploaded_file)
            except Exception:
                uploaded_file.seek(0)
                return pd.read_csv(uploaded_file, sep="\t")
        else:
            raise ValueError("対応拡張子は .xlsx/.xls/.csv/.txt です。")
    except Exception as e:
        raise RuntimeError(f"読込失敗: {e}")

# ========= ローカル I/O =========
@st.cache_data(ttl=600, show_spinner=False)
def load_alias_from_disk() -> pd.DataFrame:
    if ALIAS_PATH.exists():
        ext = ALIAS_PATH.suffix.lower()
        try:
            if ext in (".xlsx", ".xls"):
                df = pd.read_excel(ALIAS_PATH)
            elif ext in (".csv", ".txt"):
                df = pd.read_csv(ALIAS_PATH)
            else:
                return pd.DataFrame(columns=DEFAULT_COLUMNS)
            return _validate_alias_df(df)
        except Exception:
            pass
    return pd.DataFrame(columns=DEFAULT_COLUMNS)

def save_alias_to_disk(df: pd.DataFrame) -> Path:
    _ensure_dir(ALIAS_PATH)
    target = ALIAS_PATH.with_suffix(".xlsx")
    tmp = target.with_suffix(".xlsx.tmp")
    with pd.ExcelWriter(tmp, engine="xlsxwriter") as w:
        _validate_alias_df(df).to_excel(w, index=False)
    if target.exists():
        target.unlink()
    tmp.rename(target)
    load_alias_from_disk.clear()
    return target

def save_uploaded_alias(uploaded_file) -> Path:
    df = _read_any_to_df(uploaded_file)
    path = save_alias_to_disk(df)
    return path

def download_current_alias_button(df: pd.DataFrame):
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="xlsxwriter") as w:
        (_validate_alias_df(df) if not df.empty else pd.DataFrame(columns=DEFAULT_COLUMNS)).to_excel(w, index=False)
    st.download_button(
        "現在のエイリアス表をダウンロード（xlsx）",
        data=buf.getvalue(),
        file_name="aliases.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
    )

# ========= Google Sheets I/O（★ 改善点あり） =========
def _gs_client():
    """
    認証優先度:
      1) 環境変数 SERVICE_ACCOUNT_JSON_PATH のパス
      2) 定数 SERVICE_ACCOUNT_JSON_PATH のパス
      3) st.secrets['gcp_service_account'] のJSON内容
    """
    # 1) 環境変数
    json_path_env = os.environ.get(ENV_JSON_PATH_KEY, "").strip()
    if json_path_env and Path(json_path_env).exists():
        try:
            creds = Credentials.from_service_account_file(json_path_env, scopes=SCOPES)
            return gspread.authorize(creds)
        except Exception:
            pass
    # 2) 定数のパス
    if SERVICE_ACCOUNT_JSON_PATH and Path(SERVICE_ACCOUNT_JSON_PATH).exists():
        try:
            creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_JSON_PATH, scopes=SCOPES)
            return gspread.authorize(creds)
        except Exception:
            pass
    # 3) secrets.toml の JSON 本文
    try:
        creds = Credentials.from_service_account_info(st.secrets["gcp_service_account"], scopes=SCOPES)
        return gspread.authorize(creds)
    except Exception:
        return None

def _gs_ws():
    """ワークシートを取得。指定名が無ければ**先頭タブ**へフォールバック"""
    try:
        gc = _gs_client()
        if not gc:
            return None
        sh = gc.open_by_key(_cfg_sheet_id())
        ws_name = _cfg_worksheet_name()
        try:
            return sh.worksheet(ws_name)
        except WorksheetNotFound:
            return sh.get_worksheet(0)  # 先頭タブ
    except Exception:
        return None

@st.cache_data(ttl=600, show_spinner=False)
def load_alias_from_gs() -> pd.DataFrame:
    """
    get_all_values ベースで取得し、最初の非空行をヘッダーとして解釈。
    get_all_records の癖（空行・不可視文字・結合セル等）に強くする。
    """
    ws = _gs_ws()
    if not ws:
        return pd.DataFrame(columns=DEFAULT_COLUMNS)
    try:
        vals = ws.get_all_values()  # [[cell,...], ...]
        # 最初の非空行をヘッダーにする
        header_idx = next((i for i, row in enumerate(vals) if any(str(c).strip() for c in row)), None)
        if header_idx is None:
            return pd.DataFrame(columns=DEFAULT_COLUMNS)

        header = [str(c).strip() for c in vals[header_idx]]
        data = vals[header_idx + 1:]

        # 列数を合わせる（足りない列は空で埋める）
        width = max([len(header)] + [len(r) for r in data]) if data else len(header)
        header = (header + [""] * (width - len(header)))[:width]
        rows = [(r + [""] * (width - len(r)))[:width] for r in data]

        df = pd.DataFrame(rows, columns=header)
        return _validate_alias_df(df)
    except Exception:
        return pd.DataFrame(columns=DEFAULT_COLUMNS)

def save_alias_to_gs(df: pd.DataFrame) -> str:
    ws = _gs_ws()
    if not ws:
        raise RuntimeError("Google Sheets に接続できません。secrets と共有設定を確認してください。")
    df = _validate_alias_df(df)
    # 全置換で更新
    ws.clear()
    header = list(df.columns)
    values = [header] + df.astype(str).values.tolist()
    ws.update(values)
    # 任意で最終更新メモ
    try:
        ws.update_acell("D1", "last_updated")
        ws.update_acell("E1", pd.Timestamp.now(tz="Asia/Tokyo").strftime("%Y-%m-%d %H:%M:%S"))
    except Exception:
        pass
    load_alias_from_gs.clear()
    return "Google Sheets へ同期しました。"

# ========= クリップボタン =========
def copy_button(text: str, label: str, key: str):
    """iframe内でも動くコピー（モダンAPI→フォールバック）"""
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

# ========= 表示名 =========
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

# ========= 配当（TTM + 代替） =========
def get_dividend_info(code: str, last_close: float, ttm_days: int = 400) -> dict:
    """
    - yfinanceのdividends（Series, index=Datetime）を取得
    - TTMは過去ttm_daysで合計、0なら直近最大2回の合計を代替
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

        if getattr(s.index, "tz", None) is not None:
            s.index = s.index.tz_convert(None)

        now = pd.Timestamp.utcnow().tz_localize(None)
        ttm = s[s.index >= now - pd.Timedelta(days=ttm_days)]
        ttm_sum = float(ttm.sum()) if len(ttm) else 0.0

        if ttm_sum > 0:
            out["ttm_div"] = ttm_sum
            out["method"] = "ttm"
        else:
            last_two = s.sort_index(ascending=False).head(2)
            alt_sum = float(last_two.sum()) if len(last_two) else 0.0
            if alt_sum > 0:
                out["alt_div"] = alt_sum
                out["method"] = "fallback_last2"

        if last_close:
            if out["ttm_div"] is not None:
                out["yield_pct"] = out["ttm_div"] / float(last_close) * 100.0
            if out["alt_div"] is not None:
                out["alt_yield"] = out["alt_div"] / float(last_close) * 100.0

        recent = s.sort_index(ascending=False).head(8).reset_index()
        recent.columns = ["date","dividend"]
        out["recent"] = recent
    except Exception:
        pass
    return out

# ========= UI（サイドバー：GS同期コントロール） =========
st.title("📈 銘柄ワンクリック要約（GS同期版）")
st.caption("エイリアス表は Google スプレッドシートを優先。失敗時はローカルへ自動フォールバック。")

@st.cache_data(ttl=600, show_spinner=False)
def _load_alias_preferring_gs() -> tuple[pd.DataFrame, bool]:
    df_gs = load_alias_from_gs()
    if not df_gs.empty:
        return df_gs, True
    return load_alias_from_disk(), False

def sidebar_controls() -> Tuple[pd.DataFrame, bool]:
    alias_df, using_gs = _load_alias_preferring_gs()

    st.sidebar.markdown("### エイリアス表（Excel/CSV ⇄ GS）")
    if using_gs:
        st.sidebar.success("GS（10分キャッシュ）から読み込み中")
    else:
        if ALIAS_PATH.exists():
            st.sidebar.warning("GSが空/接続不可のためローカルを使用中")
        else:
            st.sidebar.warning("GS未接続 & ローカルも未設定。まずはアップロードしてください。")

    if ALIAS_PATH.exists():
        st.sidebar.caption(f"ローカル保存先: `{ALIAS_PATH}`")

    uploaded = st.sidebar.file_uploader("エイリアス表をアップロード（xlsx/csv/txt）", type=["xlsx","xls","csv","txt"])
    c1, c2 = st.sidebar.columns(2)
    with c1:
        if uploaded is not None and st.sidebar.button("保存/差し替え", use_container_width=True):
            try:
                path = save_uploaded_alias(uploaded)  # ローカル保存
                st.sidebar.success(f"ローカル保存: {path}")
                # GS 同期
                try:
                    df_local = load_alias_from_disk()
                    msg = save_alias_to_gs(df_local)
                    st.sidebar.success(msg)
                except Exception as e:
                    st.sidebar.info(f"GS同期はスキップ: {e}")
                _load_alias_preferring_gs.clear()
                st.rerun()
            except Exception as e:
                st.sidebar.error(f"保存失敗: {e}")
    with c2:
        if st.sidebar.button("現在の表をDL", use_container_width=True):
            st.session_state["dl_alias"] = True

    st.sidebar.markdown("---")
    s1, s2 = st.sidebar.columns(2)
    with s1:
        if st.button("GS→ローカル", use_container_width=True):
            try:
                df_gs_now = load_alias_from_gs()
                if df_gs_now.empty:
                    st.warning("GSが空/取得失敗です。")
                else:
                    _ensure_dir(ALIAS_PATH)
                    df_gs_now.to_excel(ALIAS_PATH.with_suffix(".xlsx"), index=False)
                    load_alias_from_disk.clear()
                    _load_alias_preferring_gs.clear()
                    st.success("Google Sheets からローカルに同期しました。")
            except Exception as e:
                st.error(f"GS→ローカル失敗: {e}")
    with s2:
        if st.button("ローカル→GS", use_container_width=True):
            try:
                df_local = load_alias_from_disk()
                if df_local.empty:
                    st.warning("ローカルが空です。")
                else:
                    msg = save_alias_to_gs(df_local)
                    _load_alias_preferring_gs.clear()
                    st.success(msg)
            except Exception as e:
                st.error(f"ローカル→GS失敗: {e}")

    if st.sidebar.button("GSを再読込", use_container_width=True):
        load_alias_from_gs.clear()
        _load_alias_preferring_gs.clear()
        st.rerun()

    # ★ 診断＆キャッシュクリア（任意）
    with st.sidebar.expander("🔧 GS診断 / キャッシュ"):
        if st.button("GSキャッシュをクリア", use_container_width=True):
            load_alias_from_gs.clear()
            _load_alias_preferring_gs.clear()
            st.success("キャッシュをクリアしました。ページ更新で再取得。")
        try:
            client_ok = _gs_client() is not None
            st.write("client:", "OK" if client_ok else "NG")
            ws = _gs_ws()
            st.write("worksheet:", getattr(ws, "title", None))
            if ws:
                vals = ws.get_all_values()
                st.write("rows:", len(vals))
                st.write("head:", vals[:3])
        except Exception as e:
            st.error(str(e))

    return alias_df, using_gs

alias_df, using_gs = sidebar_controls()

# ========= 入力欄＆クリア =========
if "code_input" not in st.session_state:
    st.session_state["code_input"] = "5108.T"  # デフォルト例

# クリア要求フラグ処理
if st.session_state.get("clear_code_flag"):
    st.session_state["code_input"] = ""
    st.session_state["clear_code_flag"] = False

st.markdown("**証券コード or ティッカー（例：5020.T / 5108.T / 7611.T / AAPL）**")
col_code, col_clear = st.columns([8, 2])
with col_code:
    st.text_input(
        label="コード",
        key="code_input",
        label_visibility="collapsed",
        placeholder="例）5108.T",
    )
with col_clear:
    if st.button("クリア", use_container_width=True):
        st.session_state["clear_code_flag"] = True
        st.rerun()

# ========= エイリアス検索＆プレビュー =========
st.subheader("🔎 エイリアス検索 & プレビュー")
search_q = st.text_input("キーワードでフィルタ（コード/銘柄名の部分一致）", value="", key="alias_search_q")

filtered_df = alias_df
if not alias_df.empty and search_q:
    qn = _norm(search_q).lower()
    filtered_df = alias_df[alias_df.apply(
        lambda r: qn in str(r["ticker"]).lower() or qn in str(r["alias"]).lower(), axis=1
    )]

with st.expander("エイリアス表プレビュー（フィルタ適用）", expanded=False):
    st.dataframe(filtered_df, use_container_width=True, hide_index=True)
    if not filtered_df.empty:
        st.markdown("**クイック操作（先頭50件）**")
        for i, row in filtered_df.head(50).iterrows():
            col1, col2, col3, col4 = st.columns([2, 5, 2, 2])
            with col1:
                st.write(f"{row['ticker']}")
            with col2:
                st.write(f"{row['alias']}")
            with col3:
                copy_button(row["ticker"], "コピー", key=f"copy-{i}")
            with col4:
                if st.button(f"{row['ticker']} を挿入", key=f"ins-{i}", use_container_width=True):
                    st.session_state["code_input"] = row["ticker"]
                    st.rerun()
    else:
        st.info("一致する銘柄が見つかりませんでした。")

# ========= サイドバー：表示パラメータ =========
st.sidebar.markdown("---")
period = st.sidebar.selectbox("期間", ["1mo","3mo","6mo","1y"], index=1)
interval = st.sidebar.selectbox("足", ["1d","1wk","1mo"], index=0)
use_zero_base = st.sidebar.checkbox("Y軸を0からにする", value=False)

# ========= 生成ボタン押下で実行 =========
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

        # ---- チャート ----
        if not df.empty:
            st.subheader(f"{title_text} – チャート")
            copy_button(code, "コードをコピー", key="title-copy")

            chart_df = df.reset_index().rename(columns={"Date":"date"})
            ymin, ymax = float(df["Close"].min()), float(df["Close"].max())
            if use_zero_base:
                y_scale = alt.Scale(domain=[0, ymax*1.05])
            else:
                y_scale = alt.Scale(domain=[ymin*0.98, ymax*1.02])

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

        # ---- 配当 ----
        st.subheader("💴 配当（直近1年TTM／参考）")
        last_close = float(df["Close"][-1]) if not df.empty else 0.0
        div_info = get_dividend_info(code, last_close)

        if div_info["ttm_div"] is not None:
            yld = f"{div_info['yield_pct']:.2f}%" if div_info['yield_pct'] is not None else "—"
            st.write(f"TTM配当合計: **{div_info['ttm_div']:.2f}** / 参考利回り: **{yld}**（方法: TTM 400日）")
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

        # ---- 株主優待リンク（参考）----
        st.markdown(
            "🔗 株主優待情報は以下の外部サイトで確認できます：\n"
            "[株主優待を探す（kabuyutai.com）](https://www.kabuyutai.com/)"
        )

        # ---- ニュース ----
        st.subheader("📰 ニュース（Google News RSS）")
        news = fetch_news_for(code, alias_df=alias_df, days=30, max_items=8, strict_title=True, min_score=2)
        if news:
            for n in news:
                st.markdown(f"- [{n['title']}]({n['link']}) 〔score={n['score']}〕")
        else:
            st.write("ニュースが見つかりませんでした。")

# ========= ダウンロード要求（サイドバー） =========
if st.session_state.get("dl_alias"):
    st.info("現在のエイリアス表（xlsx）をダウンロードできます。")
    download_current_alias_button(alias_df)
    st.session_state["dl_alias"] = False

# ========= フッター =========
st.caption("Powered by Streamlit / yfinance / Google News RSS / Google Sheets")
