from __future__ import annotations

import os
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv


APP_DIR = Path(__file__).resolve().parent
load_dotenv(APP_DIR / ".env")

st.set_page_config(
    page_title="OCI ChatKit",
    page_icon="💬",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.title("OCI ChatKit")
st.caption("ChatKit interface · OCI Generative AI Responses API")

region = os.getenv("OCI_REGION", "").strip()
project_ocid = os.getenv("OCI_PROJECT_OCID", "").strip()
model_id = os.getenv("OCI_MODEL_ID", "").strip()
oci_auth_mode = os.getenv("OCI_AUTH_MODE", "user").strip().lower()
oci_profile = os.getenv("OCI_PROFILE_NAME", "DEFAULT").strip()

with st.sidebar:
    st.subheader("Connection")
    st.write(f"**OCI region:** {region or 'Not configured'}")
    st.write(f"**OCI model:** {model_id or 'Not configured'}")
    st.write(f"**OCI project:** {'Configured' if project_ocid else 'Not configured'}")
    if oci_auth_mode in {"instance", "instance_principal"}:
        st.write("**OCI authentication:** Instance principal")
    else:
        st.write(f"**OCI authentication:** User profile ({oci_profile or 'Not configured'})")
    st.caption("OCI IAM request signing happens in the Python backend; credentials are never sent to the browser.")
    st.warning("End-user Oracle SSO is not configured in this local starter.")

missing = [
    name
    for name, value in (
        ("OCI_REGION", region),
        ("OCI_PROJECT_OCID", project_ocid),
        ("OCI_MODEL_ID", model_id),
    )
    if not value
]
if oci_auth_mode in {"user", "user_principal"} and not oci_profile:
    missing.append("OCI_PROFILE_NAME")
if oci_auth_mode not in {"user", "user_principal", "instance", "instance_principal"}:
    missing.append("OCI_AUTH_MODE (use 'user' or 'instance')")

if missing:
    st.warning("Complete the OCI settings in the app's `.env` file before starting a chat.")
    st.code("\n".join(f"{name}=..." for name in missing), language="text")
    st.info("See the included README for setup steps and OCI credential guidance.")
    st.stop()

chatkit_ui_url = os.getenv("CHATKIT_UI_URL", "http://127.0.0.1:8001/ui").strip()
st.info("Open the chat in a separate tab. ChatKit needs its own browser origin to connect to the local custom server.")
st.link_button("Open OCI ChatKit", chatkit_ui_url, type="primary")

st.caption(
    "Local proof of concept. ChatKit threads are held in memory and clear when the backend restarts."
)
