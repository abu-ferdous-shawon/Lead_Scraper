"""Business Lead Finder - entry point. Run with:  streamlit run app.py"""

import streamlit as st

st.set_page_config(page_title="Business Lead Finder", layout="wide")

pages = st.navigation([
    st.Page("search_page.py", title="Find companies", default=True),
    st.Page("outreach_page.py", title="Audit and email"),
])
pages.run()
