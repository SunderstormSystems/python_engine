"""
FlashDash – CSV Visualizer — multi‑file
=========================================

• Upload one or many CSVs  → instant preview per file.
• Paste Python that uses:
      • df_all     → concatenation of every upload, with column __source__
      • df_<stem>  → individual dataframe per file (filename stem becomes variable name)
• Click ▶️ Run code  → snippet executes on the server in a minimal sandbox.
• If the snippet sets a variable named `fig`, an interactive HTML download button appears.
"""
# Requires Google OAuth secrets in .streamlit/secrets.toml
# (see README for the expected [auth] block)

import base64
from pathlib import Path
import textwrap
import io
import builtins  # for automatic safe built‑ins
import math

import pandas as pd
import plotly.express as px
import plotly.io as pio
import streamlit as st
import altair as alt
from streamlit_ace import st_ace  # requires: pip install streamlit-ace

import boto3, json, zipfile, datetime, os, io, uuid
from zoneinfo import ZoneInfo

from botocore.exceptions import ClientError

# ── Sandbox safety helpers ──────────────────────────────────────
DANGEROUS_MODULES = {
    "os",
    "sys",
    "subprocess",
    "socket",
    "shutil",
    "pathlib",
    "importlib",
    "inspect",
    "builtins",
    "pkg_resources",
}


def safe_import(name, globals=None, locals=None, fromlist=(), level=0):
    """Custom __import__ that blocks risky top‑level modules inside user snippet."""
    base = name.split(".")[0]
    if base in DANGEROUS_MODULES:
        raise ImportError(f"Import of '{base}' is blocked in this sandbox.")
    return __import__(name, globals, locals, fromlist, level)


# Helper to build combined dataframe
def build_df_all(data_map: dict[str, pd.DataFrame]) -> pd.DataFrame | None:
    """Return a concatenated dataframe with __source__ column or None."""
    if data_map:
        return pd.concat(
            [df.assign(__source__=name) for name, df in data_map.items()],
            ignore_index=True,
        )
    return None


# ── S3 CONFIG ─────────────────────────────────────────────────────────────
AWS_REGION = os.getenv("AWS_REGION", "us-east-2")
S3_BUCKET = os.getenv("VIZ_BUCKET", "csv-visualizer-sunderdev")

s3 = boto3.client("s3", region_name=AWS_REGION)

# ── Navigation state helper ───────────────────────────────────────────────
if "page" not in st.session_state:
    st.session_state["page"] = "Workspace"

# Handle programmatic navigation request
if st.session_state.get("goto_workspace"):
    st.session_state["page"] = "Workspace"
    st.session_state.pop("goto_workspace")

# ── Page setup ───────────────────────────────────────────────────────────────
st.set_page_config(page_title="FlashDash – CSV Visualizer", layout="wide")
st.title("📊 FlashDash – CSV Visualizer")
st.subheader("Sunderstorm DevTools 🔧")

# ── Authentication (Google OAuth) ────────────────────────────────────
if not st.user.is_logged_in:
    st.markdown("## 🔐 Private application")
    st.write("Please log in with Google to continue.")
    st.button("Log in with Google", on_click=st.login, type="primary")
    st.stop()  # halt script until authenticated
else:
    with st.sidebar:
        st.success(f"Signed in as **{st.user.name}**")
        if st.button("Log out"):
            st.logout()
            st.stop()

# ── Main‑page help expander ───────────────────────────────────────
with st.expander("🤓 How to use this tool", expanded=False):
    st.markdown(
        """
**Step 1 – Upload CSV files**  
Drag one or more files into **Upload**. You’ll see a quick preview.

**Step 2 – Paste your Python snippet**  
Add the *Prompt Template* below into ChatGPT or Claude in addition to your main query, then paste the **code** it gives you here.

**Step 3 – Save and share**  
After you run the code and done analyzing the output, click **💾 Save to Projects** to snapshot the code and data.

**Step 4 – Re‑open later**  
Go to the **Projects** tab to reload or delete saved snapshots.

**Tip – Reset workspace**  
Click **🔄 Reset workspace** (top‑left of the page) any time you want to clear uploads, code, and start fresh.

**Prompt template for your AI assistant**

> *“Write Streamlit‑ready Python that assumes a dataframe named **df_all** is already loaded in memory. Build an interactive Plotly (or Altair) figure, then display it with `st.plotly_chart(fig, use_container_width=True)`. Use only pandas, numpy, plotly, or altair, and do not include Dash or any file‑I/O or network code.”*
"""
    )


# ── S3 HELPERS ────────────────────────────────────────────────────────────
def _zip_project(author: str, name: str, snippet: str, data_map: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("snippet.py", snippet)
        meta = {
            "author": author,
            "name": name,
            "saved_at": datetime.datetime.now(ZoneInfo("America/Los_Angeles")).strftime(
                "%B %d, %Y"
            ),  # PST/PDT
            "expires_at": (
                datetime.datetime.now(ZoneInfo("America/Los_Angeles"))
                + datetime.timedelta(days=90)
            ).strftime("%Y-%m-%d"),
        }
        z.writestr("meta.json", json.dumps(meta))
        for fname, df in data_map.items():
            z.writestr(f"data/{fname}", df.to_csv(index=False))
    buf.seek(0)
    return buf.read()


def save_project_s3(author: str, name: str, snippet: str, data_map: dict) -> None:
    author = author.strip()
    name = name.strip()
    project_id = str(uuid.uuid4())
    zip_key = f"projects/{project_id}.zip"
    meta_key = f"index/{project_id}.json"

    body = _zip_project(author, name, snippet, data_map)
    # 1️⃣ upload ZIP
    s3.put_object(Bucket=S3_BUCKET, Key=zip_key, Body=body)

    # 2️⃣ upload small meta
    meta = {
        "author": author,
        "name": name,
        "saved_at": datetime.datetime.now(ZoneInfo("America/Los_Angeles")).strftime(
            "%B %d, %Y"
        ),
        "expires_at": (
            datetime.datetime.now(ZoneInfo("America/Los_Angeles"))
            + datetime.timedelta(days=90)
        ).strftime("%Y-%m-%d"),
    }
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=meta_key,
        Body=json.dumps(meta).encode(),
        ContentType="application/json",
    )


# ── Expiry badge helper ──────────────────────────────────────────────────────────
def expiry_badge(expires_at_str: str) -> str:
    """
    Returns emoji badge and text like '🟢 3 mo', '🟡 2 mo', '🔴 1 mo',
    '🔴🔴🔴 1 wk', or '❌ expired'.
    """
    try:
        expires = datetime.datetime.strptime(expires_at_str, "%Y-%m-%d").date()
    except Exception:
        return ""
    today = datetime.date.today()
    delta = (expires - today).days

    if delta < 0:
        return "❌ expired"
    if delta <= 7:
        return "🔴🔴🔴 1 wk"

    # Round *up* to the nearest month so that e.g. 89 days → 3 mo (not 2 mo)
    months = math.ceil(delta / 30)

    if months >= 3:
        return f"🟢 {months} mo"
    if months == 2:
        return "🟡 2 mo"
    return "🔴 1 mo"


def list_projects_s3() -> list[dict]:
    """
    Return list of {key, author, name, saved_at} using meta index objects.
    """
    paginator = s3.get_paginator("list_objects_v2")
    projects = []
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix="index/"):
        for obj in page.get("Contents", []):
            meta = json.loads(
                s3.get_object(Bucket=S3_BUCKET, Key=obj["Key"])["Body"].read()
            )
            meta["author"] = meta.get("author", "").strip()
            meta["name"] = meta.get("name", "").strip()
            project_id = Path(obj["Key"]).stem
            zip_key = f"projects/{project_id}.zip"
            projects.append({"key": zip_key, **meta})
    return sorted(projects, key=lambda x: x["saved_at"], reverse=True)


def load_project_s3(key: str) -> tuple[str, dict]:
    """Return (snippet, data_map) for project stored at *key*."""
    obj = s3.get_object(Bucket=S3_BUCKET, Key=key)
    with zipfile.ZipFile(io.BytesIO(obj["Body"].read())) as z:
        snippet = z.read("snippet.py").decode()
        data_map = {
            name[len("data/") :]: pd.read_csv(z.open(name))
            for name in z.namelist()
            if name.startswith("data/")
        }
    return snippet, data_map


# ── Sidebar navigation ──────────────────────────────────────────────────────

page = st.sidebar.radio("Navigate:", ["Workspace", "Projects"], key="page")

if page == "Workspace":
    # ── Header & Reset ────────────────────────────────────────────
    st.markdown("### Workspace")

    # maintain an uploader_key to force file_uploader to forget selected files
    if "uploader_key" not in st.session_state:
        st.session_state["uploader_key"] = 0
    # maintain an editor_key to force st_ace to reset
    if "editor_key" not in st.session_state:
        st.session_state["editor_key"] = 0

    if st.button(
        "🔄 Reset workspace",
        help="Clear uploads, code, and start fresh",
        key="reset_ws",
    ):
        for k in ["data_map", "snippet", "show_save_form", "ace_editor"]:
            st.session_state.pop(k, None)
        st.session_state["uploader_key"] += 1  # new key resets widget
        st.session_state["editor_key"] += 1  # reset code editor
        st.rerun()

    # ── 1) Upload files ──────────────────────────────────────────────────────────
    uploaded_files = st.file_uploader(
        "Upload one or more CSV files",
        type="csv",
        accept_multiple_files=True,
        key=f"uploader_{st.session_state.get('uploader_key', 0)}",
    )

    # Cache dataframes across reruns
    if "data_map" not in st.session_state:
        st.session_state["data_map"] = {}

    # ── Sync previews with current uploader state ─────────────────────────────
    # `st.file_uploader` returns None when no files are selected; treat as []
    uploaded_files = uploaded_files or []

    # Track whether the user has interacted with the uploader during this session.
    # This lets us distinguish “no files because a project was loaded” (uploader never used)
    # from “no files because the user just removed the last CSV” (uploader previously used).
    if uploaded_files and not st.session_state.get("uploader_active"):
        st.session_state["uploader_active"] = True

    # If the uploader had files earlier but is now empty, clear previews / data_map.
    if not uploaded_files and st.session_state.get("uploader_active"):
        if st.session_state["data_map"]:
            st.session_state["data_map"].clear()
        st.session_state["uploader_active"] = False
        st.rerun()

    # Read new uploads (avoid re‑reading already stored files)
    if uploaded_files:
        for f in uploaded_files:
            if f.name not in st.session_state["data_map"]:
                df_tmp = pd.read_csv(f)
                st.session_state["data_map"][f.name] = df_tmp

        # Only sync removals *when* the uploader actually has selections.
        # Remove files that were deselected **only** when the uploader currently
        # holds at least one file. This prevents us from wiping out dataframes that
        # were loaded via “Load” (uploader starts empty in that scenario).
        if uploaded_files:  # truthy when at least one CSV is selected
            current_names = {f.name for f in uploaded_files}
            removed_any = False
            for stored in list(st.session_state["data_map"].keys()):
                if stored not in current_names:
                    st.session_state["data_map"].pop(stored, None)
                    removed_any = True
            if removed_any:
                st.rerun()  # refresh UI so previews disappear

        # Preview – no extra delete icons; removing in uploader auto‑removes preview
        for name, df_tmp in list(st.session_state["data_map"].items()):
            st.success(f"Preview of **{name}**:")
            st.dataframe(df_tmp.head())

        # Build/refresh combined dataframe
        df_all = build_df_all(st.session_state["data_map"])
    else:
        # Build/refresh combined dataframe
        df_all = build_df_all(st.session_state["data_map"])
        # Show previews when data were loaded from a project (uploader is empty)
        if not uploaded_files and st.session_state["data_map"]:
            for name, df_tmp in st.session_state["data_map"].items():
                st.success(f"Preview of **{name}**:")
                st.dataframe(df_tmp.head())

    # --- Safe import wrapper (uses global safe_import) -----------------------------
    # (Already defined at module level)

    # ── 2) Code box ──────────────────────────────────────────────────────────────
    default_code = textwrap.dedent("""
        import plotly.express as px
        # Example: line chart from combined dataframe
        fig = px.line(df_all, x=df_all.columns[0], y=df_all.columns[1], color="__source__")
        st.plotly_chart(fig, use_container_width=True)
    """).strip()

    st.markdown("✍️ **Paste your Plotly/Altair Python snippet below.** ")

    code_initial = st.session_state.get("snippet", default_code)
    code = st_ace(
        value=code_initial,
        language="python",
        theme="twilight",  # black background theme
        key=f"ace_editor_{st.session_state.get('editor_key', 0)}",
        height="300px",
        font_size=14,
        tab_size=4,
        wrap=True,
    )

    # ── 3) Run button ────────────────────────────────────────────────────────────
    run_clicked = st.button("▶️ Run code", disabled=not st.session_state["data_map"])

    if run_clicked:
        # Persist the latest snippet so it re‑executes on every widget interaction
        st.session_state["snippet"] = code

    # ── Execute the saved snippet on every rerun (if present) ────────────────────
    if "snippet" in st.session_state and st.session_state["data_map"]:
        code_to_run = st.session_state["snippet"]

        # Auto‑expose every harmless builtin; block only dangerous ones
        safe_builtins = {
            name: getattr(builtins, name)
            for name in dir(builtins)
            if name
            not in {
                "open",
                "compile",
                "eval",
                "exec",
                "input",
                "exit",
                "quit",
                "help",
                "breakpoint",
                "importlib",  # explicit module blocked
            }
            and not name.startswith("_")
        }
        safe_builtins["__import__"] = safe_import  # guarded import

        sandbox = {
            "__builtins__": safe_builtins,
            "st": st,
            "pd": pd,
            "px": px,
            "alt": alt,
            "df_all": df_all,
            "dfs": st.session_state["data_map"],
        }

        # Per‑file variables
        for name, df in st.session_state["data_map"].items():
            stem = Path(name).stem.replace("-", "_").replace(" ", "_")
            sandbox[f"df_{stem}"] = df

        # Hint logic
        if len(st.session_state["data_map"]) == 1:
            single_name, single_df = next(iter(st.session_state["data_map"].items()))
            sandbox["df"] = single_df
            sandbox["__hint__"] = (
                f"Single file uploaded → use df or dfs['{single_name}'] "
                "(plus df_<stem> alias)."
            )
        else:
            sandbox["__hint__"] = (
                "Multiple files uploaded → use df_all, dfs['<filename>'], "
                "or per‑file variables df_<stem>."
            )

        # Execute
        try:
            exec(code_to_run, sandbox)
        except Exception as e:
            st.exception(e)

        # Show variables hint
        st.info(sandbox["__hint__"])

        # Download Plotly figure if present
        if "fig" in sandbox:
            html = pio.to_html(sandbox["fig"], include_plotlyjs="cdn")
            b64 = base64.b64encode(html.encode()).decode()
            st.download_button(
                "💾 Save interactive HTML",
                b64,
                file_name="figure.html",
                mime="text/html",
            )

        # ── Save to Projects (S3) ───────────────────────────────────────
        st.markdown("### ✅ Save this result")
        if "show_save_form" not in st.session_state:
            st.session_state["show_save_form"] = False

        if not st.session_state["show_save_form"]:
            if st.button("💾 Save to Projects"):
                st.session_state["show_save_form"] = True
                st.rerun()
        else:
            with st.form("save_project_form", clear_on_submit=False):
                author_ws = st.text_input("Your name", key="author_ws")
                proj_ws = st.text_input("Project name", key="proj_ws")
                st.caption(
                    "Tip: choose a descriptive title like “Q2 Regional Sales – CA” so you and your peers can recognize it later."
                )
                submitted = st.form_submit_button("Save")
                if submitted:
                    if not (author_ws and proj_ws):
                        st.warning("Please fill in both fields.")
                    else:
                        save_project_s3(
                            author_ws.strip(),
                            proj_ws.strip(),
                            st.session_state["snippet"],
                            st.session_state["data_map"],
                        )
                        st.session_state["show_save_form"] = False
                        st.success(
                            f"✅ “{proj_ws}” saved! You’ll find it in the Projects tab."
                        )

# ── Projects page (S3-backed) ───────────────────────────────────────────────
elif page == "Projects":
    st.header("📂 Saved Projects")

    st.subheader("🗂️ Your saved projects")
    projects = list_projects_s3()
    if not projects:
        st.info("No projects saved yet.")
    else:
        for p in projects:
            col1, col_exp, col_load, col_del = st.columns([3, 2, 1, 1])
            badge = expiry_badge(p.get("expires_at", ""))
            col1.write(f"{p['author']} — **{p['name']}**  \n*saved {p['saved_at']}*")

            col_exp.write(f"Expiration: {badge}")

            safe_id = p["key"].replace("/", "_").replace(".", "_").replace("-", "_")

            if col_load.button("Load", key=f"load_{safe_id}"):
                snippet, data_map = load_project_s3(p["key"])
                st.session_state["data_map"] = data_map
                st.session_state["snippet"] = snippet
                st.session_state["snippet_ready"] = True  # NEW
                st.session_state["goto_workspace"] = True  # request navigation
                # Force widgets to reset with new content
                st.session_state["uploader_key"] = (
                    st.session_state.get("uploader_key", 0) + 1
                )
                st.session_state["editor_key"] = (
                    st.session_state.get("editor_key", 0) + 1
                )
                st.rerun()

            confirm_key = f"confirm_{safe_id}"  # separate from button key
            del_btn_key = f"del_{safe_id}"

            if confirm_key not in st.session_state:
                st.session_state[confirm_key] = False

            if not st.session_state[confirm_key]:
                if col_del.button("🗑️", key=del_btn_key):
                    st.session_state[confirm_key] = True
                    st.rerun()
            else:
                with col_del:
                    st.warning("Confirm?", icon="⚠️")
                    c1, c2 = st.columns(2)
                    if c1.button("Yes", key=f"yes_{del_btn_key}"):
                        project_id = Path(p["key"]).stem
                        s3.delete_object(
                            Bucket=S3_BUCKET, Key=f"projects/{project_id}.zip"
                        )
                        s3.delete_object(
                            Bucket=S3_BUCKET, Key=f"index/{project_id}.json"
                        )
                        st.session_state.pop(confirm_key, None)
                        st.rerun()
                    if c2.button("No", key=f"no_{del_btn_key}"):
                        st.session_state[confirm_key] = False
                        st.rerun()
