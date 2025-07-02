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


# ── Utility helpers ────────────────────────────────────────────────────────────
import zipfile

_EXCEL_EXT = (".xlsx", ".xlsm", ".xls")


def read_tabular(uploaded_file):
    """
    Return a DataFrame from a Streamlit UploadedFile that might be CSV **or** Excel.
    • Tries Excel first when the extension suggests it.
    • Falls back to CSV if Excel parsing fails for any reason.
    """
    name = uploaded_file.name.lower()
    is_excel = name.endswith(_EXCEL_EXT)
    if is_excel:
        try:
            return pd.read_excel(uploaded_file, sheet_name=0)  # pandas chooses engine
        except (ValueError, ImportError, OSError, zipfile.BadZipFile):
            pass  # couldn’t parse as Excel → try CSV

    uploaded_file.seek(0)  # rewind buffer in case read_excel consumed it
    return pd.read_csv(uploaded_file)


def ingest_uploads(files, store):
    """
    Populate *store* (dict filename → DataFrame) with new uploads.
    Shows inline Streamlit errors but never raises, so the caller stays clean.
    """
    for f in files:
        if f.name in store:
            continue
        try:
            store[f.name] = read_tabular(f)
        except Exception as e:
            st.error(f"❌ Could not read **{f.name}**: {e}")


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

# ── Update notice (simple info box) ─────────────────────────────────
if "show_update" not in st.session_state:
    st.session_state["show_update"] = False  # one‑time init

title_col, update_col = st.columns([5, 1])
with title_col:
    st.subheader("Sunderstorm DevTools 🔧")
with update_col:
    if st.button("Update – July 2, 2024"):
        st.session_state["show_update"] = True

if st.session_state["show_update"]:
    st.info(
        """
**Update – July 2, 2024**

### What’s new
- **Excel upload support** – you can now upload `.xlsx`, `.xlsm`, and `.xls` files alongside CSV.
- **Projects filters** – quickly narrow saved projects by *name*, *author*, and sort by date.
- **No‑expiry toggle** – set a project to “∞ no expiration” or re‑enable auto‑expiry with one click.
- **Project editing** – load any project, make changes, then click **⟲ Update this Project** to overwrite it.
- **Update panel** – click **“Update – July 2 2024”** in the header to reopen these notes at any time.
- **Simplified UI** – cleaner workspace and projects layout.
"""
    )

    st.button(
        "Close update", on_click=lambda: st.session_state.update(show_update=False)
    )

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


# --- Update existing project helper ---
def update_project_s3(
    project_id: str, author: str, name: str, snippet: str, data_map: dict
):
    """
    Overwrite an existing project ZIP + meta (keep its current expires_at).
    """
    zip_key = f"projects/{project_id}.zip"
    meta_key = f"index/{project_id}.json"

    # Preserve existing expires_at (incl. `"never"`)
    meta_obj = s3.get_object(Bucket=S3_BUCKET, Key=meta_key)
    meta = json.loads(meta_obj["Body"].read())
    expires = meta.get("expires_at", "")

    body = _zip_project(author, name, snippet, data_map)
    s3.put_object(Bucket=S3_BUCKET, Key=zip_key, Body=body)

    meta.update(
        {
            "author": author,
            "name": name,
            "saved_at": datetime.datetime.now(ZoneInfo("America/Los_Angeles")).strftime(
                "%B %d, %Y"
            ),
            "expires_at": expires,  # unchanged
        }
    )
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=meta_key,
        Body=json.dumps(meta).encode(),
        ContentType="application/json",
    )


# ── Expiry badge helper ──────────────────────────────────────────────────────────
def expiry_badge(expires_at_str: str) -> str:
    """
    Returns emoji badge and text like '🟢 3 mo', '🔴 1 wk', or
    '∞ no expiration' when expiry is disabled.
    """
    if expires_at_str in ("", "never", None):
        return "∞ no expiration"
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


def set_project_expiration(project_id: str, expires_at: str | None):
    """
    Overwrite the project's meta index JSON with a new expires_at value.
    Pass expires_at='never' to disable expiry, or an ISO date string to re‑enable.
    """
    meta_key = f"index/{project_id}.json"
    meta_obj = s3.get_object(Bucket=S3_BUCKET, Key=meta_key)
    meta = json.loads(meta_obj["Body"].read())
    meta["expires_at"] = expires_at
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=meta_key,
        Body=json.dumps(meta).encode(),
        ContentType="application/json",
    )


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
        for k in [
            "data_map",
            "snippet",
            "show_save_form",
            "ace_editor",
            "current_project_id",
            "current_project_author",
            "current_project_name",
        ]:
            st.session_state.pop(k, None)
        # Make sure uploader logic starts clean
        st.session_state["uploader_active"] = False
        st.session_state["uploader_key"] += 1  # new key resets widget
        st.session_state["editor_key"] += 1  # reset code editor
        st.rerun()

    # ── 1) Upload files ──────────────────────────────────────────────────────────
    uploaded_files = st.file_uploader(
        "Upload one or more CSV or Excel files",
        type=("csv", "xls", "xlsx", "xlsm"),
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
        ingest_uploads(uploaded_files, st.session_state["data_map"])

        # Remove files that were deselected in the uploader
        current_names = {f.name for f in uploaded_files}
        removed = [k for k in st.session_state["data_map"] if k not in current_names]
        for k in removed:
            st.session_state["data_map"].pop(k, None)
        if removed:
            st.rerun()

    # Build/refresh combined dataframe
    df_all = build_df_all(st.session_state["data_map"])

    # ── Show previews regardless of how data_map was populated (upload or project load)
    if st.session_state["data_map"]:
        st.markdown("#### 📄 File previews")
        for name, df_tmp in st.session_state["data_map"].items():
            st.success(f"Preview of **{name}**:")
            st.dataframe(df_tmp.head())

    # --- Safe import wrapper (uses global safe_import) -----------------------------
    # (Already defined at module level)

    # ── 2) Code box ──────────────────────────────────────────────────────────────
    default_code = textwrap.dedent("""
        # Paste your Python code here! 
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
        # Inline success banner shown right where the user clicked “Save”
        if st.session_state.get("save_success"):
            st.success(st.session_state.pop("save_success"))

        project_id = st.session_state.get("current_project_id")

        # Two buttons when editing an existing project; otherwise just "Save as new"
        if project_id:
            col_new, col_update, _ = st.columns([0.12, 0.12, 0.50])
            if col_new.button("💾 Save as New"):
                st.session_state["show_save_form"] = True
                st.session_state["saving_mode"] = "new"
                st.rerun()
            if col_update.button("⟲ Update this Project"):
                update_project_s3(
                    project_id,
                    st.session_state.get("current_project_author", "Unknown").strip(),
                    st.session_state.get("current_project_name", "").strip(),
                    st.session_state["snippet"],
                    st.session_state["data_map"],
                )
                st.success("✅ Project updated!")
        else:
            if st.button("💾 Save to Projects"):
                st.session_state["show_save_form"] = True
                st.session_state["saving_mode"] = "new"
                st.rerun()

        if st.session_state.get("show_save_form"):
            with st.form("save_project_form", clear_on_submit=False):
                st.info(
                    "Tip: choose a descriptive title like “Q2 Regional Sales – CA” so you and your peers can recognize it later."
                )
                author_ws = st.text_input("Your name", key="author_ws")
                proj_ws = st.text_input("Project name", key="proj_ws")
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
                        # Persist success message so it shows *after* the rerun
                        st.session_state["save_success"] = "✅ New project saved!"
                        # Reset form flags
                        for k in ["show_save_form", "saving_mode"]:
                            st.session_state.pop(k, None)
                        st.rerun()

# ── Projects page (S3-backed) ───────────────────────────────────────────────
elif page == "Projects":
    st.header("📂 Saved Projects")

    st.subheader("🗂️ Your saved projects")
    projects = list_projects_s3()
    # ── Filters ─────────────────────────────────────────────────────
    proj_names = sorted({p["name"] for p in projects})
    selected_name = st.selectbox(
        "Filter by project name", ["(All)"] + proj_names, index=0
    )

    # Author filter
    authors = sorted({p["author"] for p in projects})
    selected_author = st.selectbox("Filter by author", ["(All)"] + authors, index=0)

    sort_order = st.radio(
        "Sort by date", ["Newest first", "Oldest first"], horizontal=True, index=0
    )

    # Apply name filter
    if selected_name != "(All)":
        projects = [p for p in projects if p["name"] == selected_name]

    # Apply author filter
    if selected_author != "(All)":
        projects = [p for p in projects if p["author"] == selected_author]

    # Apply sort order
    projects = sorted(
        projects,
        key=lambda x: datetime.datetime.strptime(x["saved_at"], "%B %d, %Y"),
        reverse=(sort_order == "Newest first"),
    )
    if not projects:
        st.info("No projects saved yet.")
    else:
        for p in projects:
            col1, col_exp, col_perm, col_load, col_del = st.columns([3, 2, 1, 1, 1])
            badge = expiry_badge(p.get("expires_at", ""))
            col1.write(f"{p['author']} — **{p['name']}**  \n*saved {p['saved_at']}*")

            col_exp.write(f"Expiration: {badge}")

            # Toggle expiration
            safe_id = p["key"].replace("/", "_").replace(".", "_").replace("-", "_")
            perm_key = f"perm_{safe_id}"
            if p.get("expires_at") == "never":
                if col_perm.button("🔓 Enable", key=perm_key):
                    new_date = (
                        datetime.date.today() + datetime.timedelta(days=90)
                    ).strftime("%Y-%m-%d")
                    set_project_expiration(Path(p["key"]).stem, new_date)
                    st.rerun()
            else:
                if col_perm.button("🔒 No expiry", key=perm_key):
                    set_project_expiration(Path(p["key"]).stem, "never")
                    st.rerun()

            if col_load.button("Load", key=f"load_{safe_id}"):
                snippet, data_map = load_project_s3(p["key"])
                st.session_state["data_map"] = data_map
                # Track loaded project id
                st.session_state["current_project_id"] = Path(p["key"]).stem
                st.session_state["current_project_author"] = p["author"]
                st.session_state["current_project_name"] = p["name"]
                # Project loads should not be treated as “uploader active”
                st.session_state["uploader_active"] = False
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
                # Overwrite any previous ID
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
                        if st.session_state.get("current_project_id") == project_id:
                            st.session_state.pop("current_project_id", None)
                        st.session_state.pop(confirm_key, None)
                        st.rerun()
                    if c2.button("No", key=f"no_{del_btn_key}"):
                        st.session_state[confirm_key] = False
                        st.rerun()
